"""Fragt den Raumfeld-Host ab und hält den Zustand aller Räume.

Drei Quellen lösen eine Abfrage aus:
  * der regelmäßige Takt (5 s; 30 s, sobald Ereignisse ankommen),
  * UPnP-Ereignisse der Zonen (Wiedergabe, Lautstärke, Titel),
  * das Long-Polling von getZones (Räume, Zonen, Standby).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AV_TRANSPORT,
    OBJECT_FAVORITES,
    OBJECT_LINE_IN,
    RENDERING_CONTROL,
    RaumfeldConnectionError,
    RaumfeldError,
    RaumfeldHost,
    Room,
    object_id_from_uri,
    parse_didl,
    parse_duration,
)
from .const import (
    DOMAIN,
    SCAN_INTERVAL,
    SCAN_INTERVAL_WITH_EVENTS,
    SLOW_INTERVAL,
    SOURCE_LINE_IN,
    ZONE_WAIT_ATTEMPTS,
    ZONE_WAIT_DELAY,
)
from .events import EventListener

_LOGGER = logging.getLogger(__name__)

type RaumfeldConfigEntry = ConfigEntry[RaumfeldCoordinator]


@dataclass
class ZoneState:
    """Wiedergabezustand einer Zone (virtueller Renderer)."""

    transport_state: str | None = None
    play_mode: str | None = None
    uri: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    channel: str | None = None
    image_url: str | None = None
    duration: float | None = None
    position: float | None = None
    position_updated_at: datetime | None = None
    sleep_seconds: int | None = None


@dataclass
class DeviceDetails:
    """Selten wechselnde Gerätedaten eines Lautsprechers."""

    signal: int | None = None
    installed_version: str | None = None
    latest_version: str | None = None


@dataclass
class RoomState:
    """Zustand eines Raums: Stammdaten, Lautstärke, Klang und Zustand seiner Zone."""

    room: Room
    model: str | None = None
    volume: int | None = None
    muted: bool | None = None
    tone: dict[str, float] | None = None
    details: DeviceDetails | None = None
    zone: ZoneState | None = None


class RaumfeldCoordinator(DataUpdateCoordinator[dict[str, RoomState]]):
    """Koordinator: ein Abruf liefert den Zustand aller Räume (Schlüssel: Raum-UDN)."""

    config_entry: RaumfeldConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RaumfeldConfigEntry,
        api: RaumfeldHost,
        events: EventListener | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            # Ereignisse kommen oft im Paket; kurz sammeln, dann einmal abfragen.
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=0.3, immediate=False
            ),
        )
        self.api = api
        self.events = events
        # Auswählbare Quellen: Anzeigename -> ID in der Medienbibliothek.
        self.sources: dict[str, str] = {}
        self._models: dict[str, str | None] = {}
        self._details: dict[str, DeviceDetails] = {}
        self._slow_updated: float | None = None

    @callback
    def async_event_received(self) -> None:
        """Ein Gerät hat eine Änderung gemeldet."""
        self.hass.async_create_task(self.async_request_refresh())

    async def async_watch_zones(self) -> None:
        """Dauerläufer: meldet Änderungen an Räumen und Zonen sofort."""
        update_id: str | None = None
        while True:
            try:
                new_id = await self.api.async_wait_zone_change(update_id)
            except RaumfeldError:
                await asyncio.sleep(15)
                continue
            if update_id is not None and new_id != update_id:
                await self.async_request_refresh()
            update_id = new_id

    # ------------------------------------------------------------ Abfrage

    async def _async_update_data(self) -> dict[str, RoomState]:
        try:
            rooms = await self.api.async_get_zones()
            await self.api.async_refresh_devices()
        except RaumfeldConnectionError as err:
            raise UpdateFailed(str(err)) from err
        except RaumfeldError as err:
            raise UpdateFailed(f"Unerwartete Antwort des Raumfeld-Hosts: {err}") from err

        zone_udns = sorted({room.zone_udn for room in rooms if room.zone_udn})
        await self._sync_events(zone_udns)

        now = time.monotonic()
        if rooms and (
            self._slow_updated is None or now - self._slow_updated >= SLOW_INTERVAL
        ):
            self._slow_updated = now
            await asyncio.gather(
                self._update_sources(),
                *(self._update_details(room) for room in rooms),
            )

        zone_states = dict(
            zip(
                zone_udns,
                await asyncio.gather(*(self._zone_state(udn) for udn in zone_udns)),
                strict=True,
            )
        )
        states = await asyncio.gather(
            *(self._room_state(room, zone_states.get(room.zone_udn)) for room in rooms)
        )
        self.update_interval = (
            SCAN_INTERVAL_WITH_EVENTS
            if self.events is not None and self.events.working
            else SCAN_INTERVAL
        )
        return {state.room.udn: state for state in states}

    async def _sync_events(self, zone_udns: list[str]) -> None:
        """Abonniert die Ereignisse aller aktuellen Zonen."""
        if self.events is None:
            return
        urls: set[str] = set()
        for udn in zone_udns:
            try:
                device = await self.api.async_get_device(udn)
            except RaumfeldError:
                continue
            for name in (AV_TRANSPORT, RENDERING_CONTROL):
                service = device.services.get(name)
                if service is not None and service.event_url:
                    urls.add(service.event_url)
        await self.events.async_sync(urls)

    async def _update_sources(self) -> None:
        """Liest Favoriten und Line-In-Eingänge als auswählbare Quellen."""
        try:
            favorites, _ = await self.api.async_browse(OBJECT_FAVORITES)
            line_ins, _ = await self.api.async_browse(OBJECT_LINE_IN)
        except RaumfeldError as err:
            _LOGGER.debug("Quellen nicht lesbar: %s", err)
            return
        sources: dict[str, str] = {}
        for favorite in favorites:
            if favorite.can_play:
                sources.setdefault(favorite.title, favorite.object_id)
        for line_in in line_ins:
            name = (
                SOURCE_LINE_IN
                if len(line_ins) == 1
                else f"{SOURCE_LINE_IN} {line_in.title}"
            )
            sources.setdefault(name, line_in.object_id)
        self.sources = sources

    async def _update_details(self, room: Room) -> None:
        if not room.renderer_udn:
            return
        try:
            info = await self.api.async_get_device_info(room.renderer_udn)
        except RaumfeldError as err:
            _LOGGER.debug("Gerätedaten von %s nicht lesbar: %s", room.name, err)
            return
        self._details[room.renderer_udn] = DeviceDetails(
            signal=info["signal"],  # type: ignore[arg-type]
            installed_version=info["installed_version"],  # type: ignore[arg-type]
            latest_version=info["latest_version"],  # type: ignore[arg-type]
        )

    async def _zone_state(self, zone_udn: str) -> ZoneState | None:
        api = self.api
        try:
            transport, position, media = await asyncio.gather(
                api.async_transport(zone_udn, "GetTransportInfo"),
                api.async_transport(zone_udn, "GetPositionInfo"),
                api.async_transport(zone_udn, "GetMediaInfo"),
            )
        except RaumfeldError as err:
            _LOGGER.debug("Zone %s nicht abfragbar: %s", zone_udn, err)
            return None

        duration = parse_duration(position.get("TrackDuration")) or None
        state = ZoneState(
            transport_state=(transport.get("CurrentTransportState") or "").upper() or None,
            uri=media.get("CurrentURI") or None,
            duration=duration,
            # Ohne Dauer (Live-Stream) ist eine Position nicht sinnvoll darstellbar.
            position=parse_duration(position.get("RelTime")) if duration else None,
            position_updated_at=dt_util.utcnow(),
        )
        # Track-Metadaten: der laufende Titel. Medien-Metadaten: das gestartete
        # Objekt – bei Radio der Sender, während der Track den Sendungstext trägt.
        container = parse_didl(media.get("CurrentURIMetaData"))
        meta = {**container, **parse_didl(position.get("TrackMetaData"))}
        if "audioBroadcast" in meta.get("upnp_class", ""):
            state.channel = container.get("title")
        state.title = meta.get("title")
        state.artist = meta.get("artist")
        state.album = meta.get("album")
        state.image_url = meta.get("image")
        try:
            settings, state.sleep_seconds = await asyncio.gather(
                api.async_transport(zone_udn, "GetTransportSettings"),
                api.async_get_sleep_timer(zone_udn),
            )
            state.play_mode = (settings.get("PlayMode") or "").upper() or None
        except RaumfeldError:
            pass
        return state

    async def _room_state(self, room: Room, zone: ZoneState | None) -> RoomState:
        state = RoomState(room=room, zone=zone)
        if room.renderer_udn:
            if room.renderer_udn not in self._models:
                try:
                    device = await self.api.async_get_device(room.renderer_udn)
                    self._models[room.renderer_udn] = device.model_name
                except RaumfeldError:
                    pass
            state.model = self._models.get(room.renderer_udn)
            state.details = self._details.get(room.renderer_udn)
            try:
                state.tone = await self.api.async_get_tone(room.renderer_udn)
            except RaumfeldError:
                pass
        try:
            state.volume = await self.api.async_get_volume(room)
            state.muted = await self.api.async_get_mute(room)
        except RaumfeldError as err:
            _LOGGER.debug("Lautstärke von %s nicht abfragbar: %s", room.name, err)
        return state

    # ------------------------------------------------------------- Helfer

    def source_of(self, zone: ZoneState | None) -> str | None:
        """Name der gerade laufenden Quelle, falls sie aus der Quellenliste stammt."""
        if zone is None or (object_id := object_id_from_uri(zone.uri)) is None:
            return None
        return next(
            (name for name, oid in self.sources.items() if oid == object_id), None
        )

    async def async_ensure_zone(self, room_udn: str) -> str:
        """Liefert die Zone des Raums; legt bei Bedarf eine neue an."""
        for attempt in range(ZONE_WAIT_ATTEMPTS):
            room = next(
                (r for r in await self.api.async_get_zones() if r.udn == room_udn), None
            )
            if room is None:
                raise RaumfeldError("Der Raum wird vom Raumfeld-Host nicht mehr gemeldet")
            if room.zone_udn:
                try:
                    # Erst zurückkehren, wenn der virtuelle Renderer wirklich da ist.
                    await self.api.async_get_device(room.zone_udn)
                except RaumfeldError:
                    pass
                else:
                    return room.zone_udn
            elif attempt == 0:
                await self.api.async_connect_room_to_zone(room_udn)
            await asyncio.sleep(ZONE_WAIT_DELAY)
        raise RaumfeldError("Die Zone für den Raum wurde nicht rechtzeitig bereit")
