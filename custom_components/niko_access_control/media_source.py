"""Media source: browse doorbell call snapshots in the HA Media panel.

Media → Niko Access Control → <doorbell> → <day> → "14:32 · Missed"

Identifiers:
    <entry_id>                      doorbell (list of days)
    <entry_id>/day/<YYYY-MM-DD>     calls on that day
    <entry_id>/call/<calling_id>    one snapshot
"""
from __future__ import annotations

from datetime import date, timedelta

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .api import CallingInfo
from .const import DOMAIN
from .coordinator import NikoCoordinator
from .sensor import localise

SNAPSHOT_URL = "/api/niko_access_control/snapshot/{entry_id}/{calling_id}"
_MIME = "image/jpeg"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return NikoMediaSource(hass)


def _coordinator(hass: HomeAssistant, entry_id: str) -> NikoCoordinator:
    entry = hass.config_entries.async_get_entry(entry_id)
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    if not entry or entry.state is not ConfigEntryState.LOADED or coordinator is None:
        raise Unresolvable(f"Doorbell {entry_id} is not loaded")
    return coordinator


def _snapshot_url(entry_id: str, calling_id: str) -> str:
    return SNAPSHOT_URL.format(entry_id=entry_id, calling_id=calling_id)


def _call_date(call: CallingInfo) -> date | None:
    dt = localise(call.calling_datetime)
    return dt.date() if dt else None


def _day_title(day: date) -> str:
    today = dt_util.now().date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%a %-d %b %Y")


class NikoMediaSource(MediaSource):
    name = "Niko Access Control"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    # ── resolve ──────────────────────────────────────────────────────────────

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) != 3 or parts[1] != "call":
            raise Unresolvable(f"Not a snapshot: {item.identifier}")
        entry_id, _, calling_id = parts
        if _coordinator(self.hass, entry_id).call_by_id(calling_id) is None:
            raise Unresolvable("This call is no longer in the cloud history")
        return PlayMedia(_snapshot_url(entry_id, calling_id), _MIME)

    # ── browse ───────────────────────────────────────────────────────────────

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        if not item.identifier:
            return self._browse_root()
        parts = item.identifier.split("/")
        try:
            coordinator = _coordinator(self.hass, parts[0])
        except Unresolvable as err:
            raise BrowseError(str(err)) from err
        if len(parts) == 1:
            return self._browse_doorbell(parts[0], coordinator)
        if len(parts) == 3 and parts[1] == "day":
            return self._browse_day(parts[0], coordinator, date.fromisoformat(parts[2]))
        raise BrowseError(f"Unknown media item: {item.identifier}")

    def _doorbell_title(self, coordinator: NikoCoordinator, entry_id: str) -> str:
        device = dr.async_get(self.hass).async_get_device({(DOMAIN, coordinator.device_serial)})
        if device:
            return device.name_by_user or device.name or coordinator.device_serial
        entry = self.hass.config_entries.async_get_entry(entry_id)
        return entry.title if entry else coordinator.device_serial

    def _folder(self, identifier: str | None, title: str, children, thumbnail=None) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            thumbnail=thumbnail,
            children=children,
            children_media_class=MediaClass.DIRECTORY if identifier is None else None,
        )

    def _browse_root(self) -> BrowseMediaSource:
        children = []
        for entry_id, coordinator in self.hass.data.get(DOMAIN, {}).items():
            if not isinstance(coordinator, NikoCoordinator):
                continue
            last = coordinator.data.last_call if coordinator.data else None
            children.append(self._folder(
                entry_id,
                self._doorbell_title(coordinator, entry_id),
                None,
                thumbnail=_snapshot_url(entry_id, last.calling_id) if last and last.pic_url else None,
            ))
        return self._folder(None, self.name, children)

    def _browse_doorbell(self, entry_id: str, coordinator: NikoCoordinator) -> BrowseMediaSource:
        calls = coordinator.data.calls if coordinator.data else []
        by_day: dict[date, list[CallingInfo]] = {}
        for call in calls:
            if (day := _call_date(call)) is not None:
                by_day.setdefault(day, []).append(call)

        children = []
        for day in sorted(by_day, reverse=True):
            day_calls = by_day[day]
            missed = sum(1 for c in day_calls if not c.is_answered)
            title = f"{_day_title(day)} · {len(day_calls)} call{'s' if len(day_calls) != 1 else ''}"
            if missed:
                title += f", {missed} missed"
            newest = max((c for c in day_calls if c.pic_url), key=lambda c: c.calling_time, default=None)
            children.append(self._folder(
                f"{entry_id}/day/{day.isoformat()}",
                title,
                None,
                thumbnail=_snapshot_url(entry_id, newest.calling_id) if newest else None,
            ))
        folder = self._folder(entry_id, self._doorbell_title(coordinator, entry_id), children)
        folder.children_media_class = MediaClass.DIRECTORY
        return folder

    def _browse_day(self, entry_id: str, coordinator: NikoCoordinator, day: date) -> BrowseMediaSource:
        calls = sorted(
            (c for c in (coordinator.data.calls if coordinator.data else [])
             if _call_date(c) == day and c.pic_url),
            key=lambda c: c.calling_time,
            reverse=True,
        )
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}/call/{call.calling_id}",
                media_class=MediaClass.IMAGE,
                media_content_type=_MIME,
                title=f"{localise(call.calling_datetime):%H:%M:%S} · {call.status_label.capitalize()}",
                can_play=True,
                can_expand=False,
                thumbnail=_snapshot_url(entry_id, call.calling_id),
            )
            for call in calls
        ]
        folder = self._folder(f"{entry_id}/day/{day.isoformat()}", _day_title(day), children)
        folder.children_media_class = MediaClass.IMAGE
        return folder


class NikoSnapshotView(HomeAssistantView):
    """Serves call snapshots to the frontend (signed URLs / bearer auth)."""

    url = SNAPSHOT_URL
    name = "api:niko_access_control:snapshot"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entry_id: str, calling_id: str) -> web.Response:
        try:
            coordinator = _coordinator(self.hass, entry_id)
        except Unresolvable as err:
            raise web.HTTPNotFound from err
        call = coordinator.call_by_id(calling_id)
        if call is None:
            raise web.HTTPNotFound
        try:
            data = await coordinator.async_get_picture(call)
        except Exception as err:  # noqa: BLE001
            raise web.HTTPBadGateway(text=f"Snapshot download failed: {err}") from err
        if not data:
            raise web.HTTPNotFound
        return web.Response(
            body=data,
            content_type=_MIME,
            headers={"Cache-Control": "private, max-age=86400"},
        )
