"""Button platform for Niko Access Control — call control."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CALL_STATE_ON_CALL, CALL_STATE_RINGING, CONF_DEVICE_SERIAL, DOMAIN
from .coordinator import NikoCallStatusCoordinator, NikoCoordinator
from .entity import NikoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NikoCoordinator = hass.data[DOMAIN][entry.entry_id]
    serial = entry.data[CONF_DEVICE_SERIAL]
    if not coordinator.ring_coordinator.supported:
        # Devices without live call state (e.g. 510-31001) also refuse call
        # control: the cloud answers "device not online" (2003) and the ISAPI
        # callSignal relay returns -1. Don't offer buttons that can't work.
        return
    async_add_entities([
        _NikoCallButton(coordinator, serial, "answer_call", "btn_answer",
                        "answer_call", {CALL_STATE_RINGING}),
        _NikoCallButton(coordinator, serial, "reject_call", "btn_reject",
                        "reject_call", {CALL_STATE_RINGING}),
        _NikoCallButton(coordinator, serial, "hang_up", "btn_hangup",
                        "hangup_call", {CALL_STATE_ON_CALL}),
    ])


class _NikoCallButton(NikoEntity, ButtonEntity):
    """Call-control button, available only in the call states where it applies.

    Follows the 5 s live call-state poll. If the state is unknown (status
    endpoint not answering), the button stays available so it can still be tried.
    """

    coordinator: NikoCallStatusCoordinator

    def __init__(
        self,
        coordinator: NikoCoordinator,
        serial: str,
        key: str,
        uid_suffix: str,
        api_method: str,
        active_states: set[str],
    ) -> None:
        super().__init__(coordinator.ring_coordinator, serial, key)
        self._main = coordinator
        self._api_method = api_method
        self._active_states = active_states
        self._attr_unique_id = f"{serial}_{uid_suffix}"

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return True  # live state unknown — let the user try
        state = self.coordinator.call_state
        return state is None or state in self._active_states

    async def async_press(self) -> None:
        method = getattr(self._main.api, self._api_method)
        ok = await self._main.async_api_call(lambda: method(self._serial))
        if not ok:
            raise HomeAssistantError(
                f"{self.name} failed for {self._serial} — the call may already have ended"
            )
        await self.coordinator.async_request_refresh()
