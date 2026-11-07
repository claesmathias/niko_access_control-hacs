"""Push notifications to Home Assistant Companion app devices on each ring.

Same approach as discovering every `notify.mobile_app_*` action: no notify
group or automation needed. Configured in the integration options.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .api import CallingInfo
from .const import (
    CONF_NOTIFY_DEVICES,
    CONF_NOTIFY_MESSAGE,
    CONF_NOTIFY_MODE,
    CONF_NOTIFY_TIME_SENSITIVE,
    CONF_NOTIFY_TITLE,
    DEFAULT_NOTIFY_MESSAGE,
    DEFAULT_NOTIFY_TITLE,
    DOMAIN,
    NOTIFY_MODE_ALL,
    NOTIFY_MODE_OFF,
    NOTIFY_MODE_SELECTED,
    SIGNAL_RING,
)
from .coordinator import NikoCoordinator
from .sensor import localise

_LOGGER = logging.getLogger(__name__)

# How long to wait for the call (and its snapshot) to reach the cloud history
# after a ring detected from the live call state.
_PICTURE_WAIT = 120
_SNAPSHOT_LINK_VALID = timedelta(days=1)


def mobile_devices(hass: HomeAssistant) -> list[str]:
    """All registered Companion app notify actions, e.g. notify.mobile_app_pixel."""
    return sorted(
        f"notify.{name}"
        for name in hass.services.async_services().get("notify", {})
        if name.startswith("mobile_app_")
    )


def destinations(hass: HomeAssistant, options: dict[str, Any]) -> list[str]:
    mode = options.get(CONF_NOTIFY_MODE, NOTIFY_MODE_OFF)
    if mode == NOTIFY_MODE_ALL:
        return mobile_devices(hass)
    if mode == NOTIFY_MODE_SELECTED:
        available = set(mobile_devices(hass))
        return sorted(set(options.get(CONF_NOTIFY_DEVICES, [])) & available)
    return []


def _locale_name(hass: HomeAssistant) -> str:
    """Babel locale from Settings → System → General (language + country), e.g. nl_BE."""
    language = (hass.config.language or "en").replace("-", "_")
    country = hass.config.country
    return f"{language}_{country}" if country and "_" not in language else language


def format_date_time(hass: HomeAssistant, when: datetime) -> tuple[str, str]:
    """(date, time) in the style of the system language/country.

    Blocking (Babel reads locale data from disk): run in the executor.
    """
    fallback = (when.date().isoformat(), when.strftime("%H:%M"))
    try:
        from babel import Locale, UnknownLocaleError  # noqa: PLC0415
        from babel.dates import format_date, format_time  # noqa: PLC0415
    except ImportError:
        return fallback
    for name in (_locale_name(hass), (hass.config.language or "en").split("-")[0], "en"):
        try:
            locale = Locale.parse(name)
            return (
                format_date(when.date(), format="medium", locale=locale),
                # narrow no-break space (e.g. "2:32\u202fPM") renders oddly in some notification views
                format_time(when.time(), format="short", locale=locale).replace("\u202f", " "),
            )
        except (UnknownLocaleError, ValueError):
            continue
    return fallback


def format_date(hass: HomeAssistant, day: date) -> str:
    """Date only (kept for callers/tests)."""
    return format_date_time(hass, datetime.combine(day, datetime.min.time()))[0]


def media_panel_path(entry_id: str) -> str:
    """Frontend path that opens this doorbell's folder in the Media panel."""
    root = quote(f"app,media-source://{DOMAIN}", safe="")
    folder = quote(f",media-source://{DOMAIN}/{entry_id}", safe="")
    return f"/media-browser/browser/{root}/{folder}"


class NikoNotifier:
    """Sends a notification for each ring to the configured mobile devices."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: NikoCoordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._waiting_since: float | None = None
        self._waiting_after: str | None = None  # newest calling_id at ring time

    @property
    def enabled(self) -> bool:
        return self.entry.options.get(CONF_NOTIFY_MODE, NOTIFY_MODE_OFF) != NOTIFY_MODE_OFF

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        unsub_ring = async_dispatcher_connect(
            self.hass, SIGNAL_RING.format(entry_id=self.entry.entry_id), self._on_ring
        )
        unsub_history = self.coordinator.async_add_listener(self._on_history)

        def _stop() -> None:
            unsub_ring()
            unsub_history()

        return _stop

    # ── ring handling ────────────────────────────────────────────────────────

    @callback
    def _on_ring(self, call: CallingInfo | None) -> None:
        if not self.enabled:
            return
        if call is not None:  # detected from the call history: snapshot available
            self._waiting_since = None
            self.hass.async_create_task(self.async_send(call))
            return
        # Live call state: notify now, then replace it once the call (with its
        # snapshot) shows up in the history.
        last = self.coordinator.data.last_call if self.coordinator.data else None
        self._waiting_after = last.calling_id if last else None
        self._waiting_since = time.monotonic()
        self.hass.async_create_task(self.async_send(None))

    @callback
    def _on_history(self) -> None:
        if self._waiting_since is None:
            return
        if time.monotonic() - self._waiting_since > _PICTURE_WAIT:
            self._waiting_since = None
            return
        last = self.coordinator.data.last_call if self.coordinator.data else None
        if last and last.calling_id != self._waiting_after and last.pic_url:
            self._waiting_since = None
            self.hass.async_create_task(self.async_send(last))

    # ── delivery ─────────────────────────────────────────────────────────────

    async def _payload(self, call: CallingInfo | None, options: dict[str, Any]) -> dict[str, Any]:
        when = localise(call.calling_datetime) if call else None
        when = when or dt_util.now()
        date_text, time_text = await self.hass.async_add_executor_job(
            format_date_time, self.hass, when
        )
        values = {"time": time_text, "date": date_text}
        try:
            message = options.get(CONF_NOTIFY_MESSAGE, DEFAULT_NOTIFY_MESSAGE).format(**values)
        except (KeyError, IndexError, ValueError):
            message = DEFAULT_NOTIFY_MESSAGE.format(**values)

        tap = media_panel_path(self.entry.entry_id)
        data: dict[str, Any] = {
            "tag": f"{DOMAIN}_{self.coordinator.device_serial}",
            "url": tap,          # iOS
            "clickAction": tap,  # Android
        }
        if options.get(CONF_NOTIFY_TIME_SENSITIVE, True):
            data["push"] = {"interruption-level": "time-sensitive"}  # iOS
            data["priority"] = "high"  # Android
            data["ttl"] = 0
        if call is not None and call.pic_url:
            path = f"/api/{DOMAIN}/snapshot/{self.entry.entry_id}/{call.calling_id}"
            data["image"] = async_sign_path(
                self.hass, path, _SNAPSHOT_LINK_VALID, use_content_user=True
            )
        return {
            "title": options.get(CONF_NOTIFY_TITLE) or DEFAULT_NOTIFY_TITLE,
            "message": message,
            "data": data,
        }

    async def async_send_test(self) -> tuple[int, list[str]]:
        """Send the last call as a test. Returns (devices tried, failed actions)."""
        targets = destinations(self.hass, self.entry.options)
        last = self.coordinator.data.last_call if self.coordinator.data else None
        failed = await self.async_send(last) if targets else []
        return len(targets), failed

    async def async_send(
        self, call: CallingInfo | None, options: dict[str, Any] | None = None
    ) -> list[str]:
        """Send to every destination; returns the actions that failed."""
        options = self.entry.options if options is None else options
        payload = await self._payload(call, options)
        failed: list[str] = []
        for service in destinations(self.hass, options):
            domain, name = service.split(".", 1)
            try:
                async with asyncio.timeout(15):
                    await self.hass.services.async_call(domain, name, payload, blocking=True)
            except Exception as err:  # noqa: BLE001
                failed.append(service)
                _LOGGER.warning("Doorbell notification to %s failed: %s", service, err)
        return failed
