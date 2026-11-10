"""Niko Access Control integration."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    HikConnectAPI,
    HikConnectAuthError,
    HikConnectError,
)
from .const import (
    CONF_DEVICE_SERIAL,
    CONF_NOTIFY_MODE,
    DOMAIN,
    FALLBACK_SCAN_INTERVAL,
    NOTIFY_MODE_OFF,
)
from .coordinator import NikoCallStatusCoordinator, NikoCoordinator
from .media_source import NikoSnapshotView
from .notifications import NikoNotifier

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.IMAGE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_ANSWER_CALL = "answer_call"
SERVICE_REJECT_CALL = "reject_call"
SERVICE_HANGUP_CALL = "hangup_call"

# device_id (device picker) is preferred; device_serial is kept for existing automations.
_TARGET = {
    vol.Optional(ATTR_DEVICE_ID): cv.string,
    vol.Optional(CONF_DEVICE_SERIAL): cv.string,
}
_CALL_SCHEMA = vol.Schema(_TARGET)

# Entities removed in v3.0.0: folded into the device card, replaced by the
# doorbell event / Calls today, or by the Media panel (all call snapshots).
_OBSOLETE_UNIQUE_ID = re.compile(
    r"_(info_(?!ip_address$).+|call_history_\d+|history_\d+|call_count|snapshot_camera)$"
)


# Options of the local device access feature, removed in v3.3.0.
_REMOVED_OPTIONS = ("local_host", "local_username", "local_password")

# Created only when the doorbell reports a live call state (call control needs it too).
_LIVE_STATE_ENTITIES = (
    "{serial}_ringing {serial}_call_state {serial}_btn_answer {serial}_btn_reject {serial}_btn_hangup"
)


def _coordinators(hass: HomeAssistant) -> list[NikoCoordinator]:
    return [c for c in hass.data.get(DOMAIN, {}).values() if isinstance(c, NikoCoordinator)]


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> NikoCoordinator:
    """Resolve the target doorbell from device_id, device_serial, or the only one configured."""
    serial = call.data.get(CONF_DEVICE_SERIAL)
    if device_id := call.data.get(ATTR_DEVICE_ID):
        device = dr.async_get(hass).async_get(device_id)
        serial = next(
            (ident for dom, ident in (device.identifiers if device else ()) if dom == DOMAIN),
            None,
        )
        if serial is None:
            raise HomeAssistantError(f"Device {device_id} is not a Niko Access Control doorbell")

    coordinators = _coordinators(hass)
    if serial is None:
        if len(coordinators) == 1:
            return coordinators[0]
        raise HomeAssistantError("Select the doorbell to use (more than one is configured)")
    for coordinator in coordinators:
        if coordinator.device_serial == serial:
            return coordinator
    raise HomeAssistantError(f"No Niko doorbell configured with serial {serial!r}")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions and snapshot view once."""
    hass.http.register_view(NikoSnapshotView(hass))

    def _call_handler(method: str, label: str):
        async def _handler(call: ServiceCall) -> None:
            coordinator = _coordinator_for_call(hass, call)
            serial = coordinator.device_serial
            ok = await coordinator.async_api_call(lambda: getattr(coordinator.api, method)(serial))
            if not ok:
                raise HomeAssistantError(f"{label} failed for {serial}")
            await coordinator.ring_coordinator.async_request_refresh()

        return _handler

    hass.services.async_register(
        DOMAIN, SERVICE_ANSWER_CALL, _call_handler("answer_call", "Answer call"), _CALL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REJECT_CALL, _call_handler("reject_call", "Reject call"), _CALL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_HANGUP_CALL, _call_handler("hangup_call", "Hang up"), _CALL_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = HikConnectAPI(session)
    try:
        await api.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    except HikConnectAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except HikConnectError as err:
        raise ConfigEntryNotReady(f"Cannot reach the Hik-Connect cloud: {err}") from err

    # Local device access was removed in v3.3.0: drop its stored credentials.
    if any(key in entry.options for key in _REMOVED_OPTIONS):
        hass.config_entries.async_update_entry(
            entry,
            options={k: v for k, v in entry.options.items() if k not in _REMOVED_OPTIONS},
        )

    serial = entry.data[CONF_DEVICE_SERIAL]

    coordinator = NikoCoordinator(
        hass, api, serial,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )
    await coordinator.async_config_entry_first_refresh()

    ring_coordinator = NikoCallStatusCoordinator(hass, api, serial)
    await ring_coordinator.async_refresh()
    coordinator.ring_coordinator = ring_coordinator
    if not ring_coordinator.supported:
        # No live call state for this device (e.g. 510-31001: the cloud reports
        # it "offline" on the call-status endpoint). Stop polling it and detect
        # rings from the call history instead, checked more often.
        _LOGGER.info(
            "Live call state not available for %s; detecting rings from call history every %ss",
            serial, FALLBACK_SCAN_INTERVAL,
        )
        ring_coordinator.update_interval = None
        coordinator.update_interval = timedelta(seconds=FALLBACK_SCAN_INTERVAL)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _remove_obsolete_entities(hass, entry, serial, ring_coordinator.supported)
    if entry.options.get(CONF_NOTIFY_MODE, NOTIFY_MODE_OFF) == NOTIFY_MODE_OFF:
        _remove_entity(hass, "button", f"{serial}_btn_test_notification")
    _sync_device(hass, entry, coordinator)
    entry.async_on_unload(
        coordinator.async_add_listener(lambda: _sync_device(hass, entry, coordinator))
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    coordinator.notifier = NikoNotifier(hass, entry, coordinator)
    entry.async_on_unload(coordinator.notifier.async_start())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply Options changes without a restart."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _remove_entity(hass: HomeAssistant, domain: str, unique_id: str) -> None:
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(domain, DOMAIN, unique_id):
        registry.async_remove(entity_id)


@callback
def _remove_obsolete_entities(
    hass: HomeAssistant, entry: ConfigEntry, serial: str, live_state: bool
) -> None:
    """Drop entities superseded by device-card fields, the event entity, and the Media panel."""
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        uid = ent.unique_id
        if not uid.startswith(f"{serial}_"):
            continue
        if _OBSOLETE_UNIQUE_ID.search(uid) or (
            not live_state and uid in _LIVE_STATE_ENTITIES.format(serial=serial).split()
        ):
            _LOGGER.debug("Removing obsolete entity %s (%s)", ent.entity_id, uid)
            registry.async_remove(ent.entity_id)


@callback
def _sync_device(hass: HomeAssistant, entry: ConfigEntry, coordinator: NikoCoordinator) -> None:
    """Write model / firmware / serial / MAC / IP into the device registry entry."""
    data = coordinator.data
    info = data.device_info if data else None
    cloud = (data.cloud_device if data else None) or {}
    serial = coordinator.device_serial

    connections: set[tuple[str, str]] = set()
    if info and info.mac_address:
        connections.add((dr.CONNECTION_NETWORK_MAC, dr.format_mac(info.mac_address)))

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, serial)},
        connections=connections,
        manufacturer="Niko",
        name=cloud.get("name") or f"Niko doorbell {serial}",
        model=(info.model if info else "") or cloud.get("deviceType") or "Access Control doorbell",
        sw_version=(info.firmware_version if info else "") or cloud.get("version") or None,
        hw_version=(info.hardware_version_display if info else "") or None,
        serial_number=(info.serial_number if info else "") or cloud.get("fullSerial") or serial,
        configuration_url=f"http://{info.ip_address}" if info and info.ip_address else None,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
