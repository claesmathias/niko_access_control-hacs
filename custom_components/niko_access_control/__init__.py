"""Niko Access Control integration."""
from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HikConnectAPI, LocalISAPIClient, audio_to_mulaw
from .const import (
    CONF_DEVICE_SERIAL,
    CONF_LOCAL_HOST,
    CONF_LOCAL_PASSWORD,
    CONF_LOCAL_USERNAME,
    DOMAIN,
)
from .coordinator import NikoCallStatusCoordinator, NikoCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
]

SERVICE_ANSWER_CALL = "answer_call"
SERVICE_ANNOUNCE = "announce"

_ANSWER_SCHEMA = vol.Schema({vol.Required(CONF_DEVICE_SERIAL): cv.string})
_ANNOUNCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_SERIAL): cv.string,
        vol.Optional("message"): cv.string,
        vol.Optional("media_url"): cv.string,
        vol.Optional("language", default="en"): cv.string,
        vol.Optional("answer_first", default=True): cv.boolean,
    }
)


def _coordinator_for(hass: HomeAssistant, serial: str) -> NikoCoordinator:
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if isinstance(coordinator, NikoCoordinator) and coordinator.device_serial == serial:
            return coordinator
    raise HomeAssistantError(f"No Niko device configured with serial {serial!r}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = HikConnectAPI(session)
    await api.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    # Optional local ISAPI client (set via Options flow)
    local_client: LocalISAPIClient | None = None
    opts = entry.options
    if opts.get(CONF_LOCAL_HOST) and opts.get(CONF_LOCAL_USERNAME) and opts.get(CONF_LOCAL_PASSWORD):
        local_client = LocalISAPIClient(
            opts[CONF_LOCAL_HOST],
            opts[CONF_LOCAL_USERNAME],
            opts[CONF_LOCAL_PASSWORD],
        )

    serial = entry.data[CONF_DEVICE_SERIAL]

    coordinator = NikoCoordinator(
        hass, api, serial,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        local_client=local_client,
    )
    await coordinator.async_config_entry_first_refresh()

    ring_coordinator = NikoCallStatusCoordinator(hass, api, serial)
    await ring_coordinator.async_config_entry_first_refresh()
    coordinator.ring_coordinator = ring_coordinator

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ── services (registered once) ──────────────────────────────────────────
    if not hass.services.has_service(DOMAIN, SERVICE_ANSWER_CALL):

        async def _answer_call(call: ServiceCall) -> None:
            serial = call.data[CONF_DEVICE_SERIAL]
            coordinator = _coordinator_for(hass, serial)
            ok = await coordinator.api.answer_call(serial)
            if not ok:
                raise HomeAssistantError(f"answer_call failed for {serial}")

        async def _announce(call: ServiceCall) -> None:
            serial = call.data[CONF_DEVICE_SERIAL]
            message = call.data.get("message")
            media_url = call.data.get("media_url")
            language = call.data.get("language", "en")
            answer_first = call.data.get("answer_first", True)

            if not message and not media_url:
                raise HomeAssistantError("Provide either 'message' or 'media_url'")

            coordinator = _coordinator_for(hass, serial)
            local_client = coordinator.local_client
            if not local_client:
                raise HomeAssistantError(
                    "Local device not configured. Go to Settings → Integrations → "
                    "Niko Access Control → Configure to add the local IP and credentials."
                )

            if answer_first:
                await coordinator.api.answer_call(serial)
                await asyncio.sleep(1)

            # Get audio bytes
            if media_url:
                async with session.get(media_url, timeout=None) as resp:
                    resp.raise_for_status()
                    audio_bytes = await resp.read()
                    mime_type = resp.content_type or "audio/mpeg"
            else:
                audio_bytes, mime_type = await _tts_audio(hass, message, language)

            # Convert to G.711 µ-law 8 kHz
            pcm_bytes = await audio_to_mulaw(audio_bytes, mime_type)

            ok = await local_client.speak(pcm_bytes)
            if not ok:
                raise HomeAssistantError("Failed to stream audio to doorbell via ISAPI")

        hass.services.async_register(DOMAIN, SERVICE_ANSWER_CALL, _answer_call, _ANSWER_SCHEMA)
        hass.services.async_register(DOMAIN, SERVICE_ANNOUNCE, _announce, _ANNOUNCE_SCHEMA)

    return True


async def _tts_audio(hass: HomeAssistant, message: str, language: str) -> tuple[bytes, str]:
    """Generate TTS audio bytes using the HA TTS component."""
    from homeassistant.components.tts import DOMAIN as TTS_DOMAIN  # noqa: PLC0415

    manager = hass.data.get(TTS_DOMAIN)
    if manager is None:
        raise HomeAssistantError(
            "TTS is not configured. Add a TTS integration first "
            "(Settings → Devices & Services → Add Integration → e.g. Google Translate TTS)."
        )

    engine = getattr(manager, "default_engine", None)
    if not engine:
        providers = getattr(manager, "providers", {})
        if not providers:
            raise HomeAssistantError("No TTS engine available")
        engine = next(iter(providers))

    try:
        result = await manager.async_get_tts_audio(engine, language, {}, message)
        audio_bytes, mime_type = result if isinstance(result, tuple) else (bytes(result), "audio/mpeg")
    except (AttributeError, TypeError) as err:
        raise HomeAssistantError(
            f"TTS API incompatible with this HA version: {err}. "
            "Use 'media_url' parameter instead."
        ) from err

    if not audio_bytes:
        raise HomeAssistantError("TTS returned no audio data")
    return audio_bytes, mime_type


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
