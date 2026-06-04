"""Config flow for Niko Access Control."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HikConnectAPI, HikConnectAuthError, HikConnectError, LocalISAPIClient
from .const import CONF_DEVICE_SERIAL, CONF_LOCAL_HOST, CONF_LOCAL_PASSWORD, CONF_LOCAL_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_DEVICE_SERIAL): str,
    }
)


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Validate credentials and device serial by attempting a login."""
    session = async_get_clientsession(hass)
    api = HikConnectAPI(session)
    await api.login(data[CONF_USERNAME], data[CONF_PASSWORD])
    result = await api.get_last_call(data[CONF_DEVICE_SERIAL])
    return {"title": f"Niko {data[CONF_DEVICE_SERIAL]}"}


class NikoAccessControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Niko Access Control."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return NikoOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await _validate_input(self.hass, user_input)
            except HikConnectAuthError:
                errors["base"] = "invalid_auth"
            except HikConnectError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_SERIAL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
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
                except Exception:
                    errors["base"] = "cannot_connect_local"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_LOCAL_HOST,
                    default=current.get(CONF_LOCAL_HOST, ""),
                ): str,
                vol.Optional(
                    CONF_LOCAL_USERNAME,
                    default=current.get(CONF_LOCAL_USERNAME, "admin"),
                ): str,
                vol.Optional(
                    CONF_LOCAL_PASSWORD,
                    default=current.get(CONF_LOCAL_PASSWORD, ""),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )
