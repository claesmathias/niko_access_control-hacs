"""Config flow for Niko Access Control."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import HikConnectAPI, HikConnectAuthError, HikConnectError, LocalISAPIClient
from .const import CONF_DEVICE_SERIAL, CONF_LOCAL_HOST, CONF_LOCAL_PASSWORD, CONF_LOCAL_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

_TEXT = TextSelector()
_USERNAME = TextSelector(TextSelectorConfig(autocomplete="username"))
_PASSWORD = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


class DeviceNotFound(Exception):
    """The serial is not registered on this account."""


async def _validate(hass: HomeAssistant, username: str, password: str, serial: str) -> str:
    """Log in and confirm the serial belongs to the account. Returns the device title."""
    api = HikConnectAPI(async_get_clientsession(hass))
    await api.login(username, password)
    try:
        devices = await api.get_user_devices()
    except HikConnectError as err:
        _LOGGER.debug("Device list unavailable, skipping serial check: %s", err)
        return f"Niko {serial}"
    if not devices:
        return f"Niko {serial}"
    match = next((d for d in devices if d.get("deviceSerial") == serial), None)
    if match is None:
        raise DeviceNotFound
    return match.get("name") or f"Niko {serial}"


def _errors_for(err: Exception) -> str:
    if isinstance(err, HikConnectAuthError):
        return "invalid_auth"
    if isinstance(err, DeviceNotFound):
        return "device_not_found"
    if isinstance(err, HikConnectError):
        return "cannot_connect"
    _LOGGER.exception("Unexpected error during config flow")
    return "unknown"


class NikoAccessControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Niko Access Control."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return NikoOptionsFlowHandler(config_entry)

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._devices: dict[str, str] = {}  # serial → title

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: sign in. Step 2 picks the doorbell from the account's device list."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api = HikConnectAPI(async_get_clientsession(self.hass))
            devices: list[dict[str, Any]] = []
            try:
                await api.login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
                try:
                    devices = await api.get_user_devices()
                except HikConnectError as err:
                    _LOGGER.debug("Device list unavailable, falling back to manual serial: %s", err)
            except Exception as err:  # noqa: BLE001
                errors["base"] = _errors_for(err)
            else:
                self._credentials = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                configured = self._async_current_ids()
                self._devices = {
                    d["deviceSerial"]: d.get("name") or f"Niko {d['deviceSerial']}"
                    for d in devices
                    if d.get("deviceSerial")
                }
                if not self._devices:
                    return await self.async_step_manual()
                if all(serial in configured for serial in self._devices):
                    return self.async_abort(reason="no_new_devices")
                return await self.async_step_device()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): _USERNAME,
                vol.Required(CONF_PASSWORD): _PASSWORD,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Pick a doorbell from the account (already configured ones are left out)."""
        configured = self._async_current_ids()
        available = {s: t for s, t in self._devices.items() if s not in configured}

        if user_input is not None:
            serial = user_input[CONF_DEVICE_SERIAL]
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=available.get(serial, f"Niko {serial}"),
                data={**self._credentials, CONF_DEVICE_SERIAL: serial},
            )

        options = [
            SelectOptionDict(value=serial, label=f"{title} ({serial})")
            for serial, title in available.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_SERIAL, default=options[0]["value"]): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Fallback when the device list can't be loaded: type the serial."""
        errors: dict[str, str] = {}

        if user_input is not None:
            serial = user_input[CONF_DEVICE_SERIAL].strip().upper()
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            try:
                title = await _validate(
                    self.hass,
                    self._credentials[CONF_USERNAME],
                    self._credentials[CONF_PASSWORD],
                    serial,
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = _errors_for(err)
            else:
                return self.async_create_entry(
                    title=title, data={**self._credentials, CONF_DEVICE_SERIAL: serial}
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_SERIAL): _TEXT}),
            errors=errors,
        )

    # ── re-authentication (triggered when the stored password stops working) ──

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self._credentials_step("reauth_confirm", entry, user_input)

    # ── reconfigure (change account credentials from the integration page) ──

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self._credentials_step("reconfigure", entry, user_input)

    async def _credentials_step(
        self,
        step_id: str,
        entry: config_entries.ConfigEntry,
        user_input: dict[str, Any] | None,
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        serial = entry.data[CONF_DEVICE_SERIAL]

        if user_input is not None:
            try:
                await _validate(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD], serial
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = _errors_for(err)
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **user_input},
                    reason="reauth_successful" if step_id == "reauth_confirm" else "reconfigure_successful",
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): _USERNAME,
                vol.Required(CONF_PASSWORD): _PASSWORD,
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            description_placeholders={"serial": serial},
            errors=errors,
        )


class NikoOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow: configure local device access for two-way audio."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        current = self._entry.options

        if user_input is not None:
            host = user_input.get(CONF_LOCAL_HOST, "").strip()
            username = user_input.get(CONF_LOCAL_USERNAME, "").strip()
            password = user_input.get(CONF_LOCAL_PASSWORD, "")

            if host and username and password:
                try:
                    client = LocalISAPIClient(host, username, password)
                    if not await client.test_connection():
                        errors["base"] = "cannot_connect_local"
                except Exception:  # noqa: BLE001
                    errors["base"] = "cannot_connect_local"
            elif host or password:
                errors["base"] = "incomplete_local"

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_LOCAL_HOST: host,
                        CONF_LOCAL_USERNAME: username,
                        CONF_LOCAL_PASSWORD: password,
                    },
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_LOCAL_HOST): _TEXT,
                vol.Optional(CONF_LOCAL_USERNAME): _TEXT,
                vol.Optional(CONF_LOCAL_PASSWORD): _PASSWORD,
            }
        )
        suggested = user_input or {
            CONF_LOCAL_HOST: current.get(CONF_LOCAL_HOST, ""),
            CONF_LOCAL_USERNAME: current.get(CONF_LOCAL_USERNAME, "admin"),
            CONF_LOCAL_PASSWORD: current.get(CONF_LOCAL_PASSWORD, ""),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )
