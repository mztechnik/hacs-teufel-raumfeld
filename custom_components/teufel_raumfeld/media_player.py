"""Media-Player-Entitäten: eine pro Raumfeld-Raum."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import OBJECT_ROOT, MediaObject, RaumfeldError
from .const import (
    ANNOUNCE_MAX_DURATION,
    ANNOUNCE_START_TIMEOUT,
    DOMAIN,
    MEDIA_ID_PREFIX,
)
from .coordinator import RaumfeldConfigEntry, RaumfeldCoordinator
from .entity import RaumfeldEntity, async_setup_rooms, raumfeld_command

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.GROUPING
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
)

_TRANSPORT_STATES = {
    "PLAYING": MediaPlayerState.PLAYING,
    "PAUSED_PLAYBACK": MediaPlayerState.PAUSED,
    "TRANSITIONING": MediaPlayerState.BUFFERING,
}
_BUSY_STATES = ("PLAYING", "TRANSITIONING")


@dataclass
class _Snapshot:
    """Was vor einer Durchsage lief – zum Wiederherstellen danach."""

    uri: str | None
    metadata: str
    was_playing: bool
    was_standby: bool


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RaumfeldConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt für jeden Raum einen Media-Player an."""
    async_setup_rooms(
        entry, async_add_entities, lambda coordinator, udn: [RaumfeldRoom(coordinator, udn)]
    )


def _browse_item(obj: MediaObject) -> BrowseMedia:
    return BrowseMedia(
        title=obj.title,
        media_class=MediaClass.DIRECTORY if obj.is_container else MediaClass.MUSIC,
        media_content_id=f"{MEDIA_ID_PREFIX}{obj.object_id}",
        media_content_type=MediaType.MUSIC,
        can_play=obj.can_play,
        can_expand=obj.is_container,
        thumbnail=obj.image,
    )


class RaumfeldRoom(RaumfeldEntity, MediaPlayerEntity):
    """Ein Raumfeld-Raum (z. B. eine Teufel One S) als Media-Player."""

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = SUPPORTED_FEATURES
    _attr_media_content_type = MediaType.MUSIC
    _attr_volume_step = 0.05

    def __init__(self, coordinator: RaumfeldCoordinator, room_udn: str) -> None:
        super().__init__(coordinator, room_udn)
        self._snapshot: _Snapshot | None = None
        self._restore_task: asyncio.Task[None] | None = None

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_announce()
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------- Zustand

    @property
    def state(self) -> MediaPlayerState | None:
        if (room := self._room) is None:
            return None
        power = room.room.power_state
        if power == "MANUAL_STANDBY":
            return MediaPlayerState.OFF
        if "STANDBY" in power:
            return MediaPlayerState.STANDBY
        if room.zone is None or room.zone.transport_state is None:
            return MediaPlayerState.IDLE
        return _TRANSPORT_STATES.get(room.zone.transport_state, MediaPlayerState.IDLE)

    @property
    def volume_level(self) -> float | None:
        room = self._room
        return None if room is None or room.volume is None else room.volume / 100

    @property
    def is_volume_muted(self) -> bool | None:
        return room.muted if (room := self._room) else None

    @property
    def media_title(self) -> str | None:
        return zone.title if (zone := self._zone) else None

    @property
    def media_artist(self) -> str | None:
        return zone.artist if (zone := self._zone) else None

    @property
    def media_album_name(self) -> str | None:
        return zone.album if (zone := self._zone) else None

    @property
    def media_channel(self) -> str | None:
        return zone.channel if (zone := self._zone) else None

    @property
    def media_image_url(self) -> str | None:
        return zone.image_url if (zone := self._zone) else None

    @property
    def media_duration(self) -> float | None:
        return zone.duration if (zone := self._zone) else None

    @property
    def media_position(self) -> float | None:
        return zone.position if (zone := self._zone) else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        zone = self._zone
        return zone.position_updated_at if zone and zone.position is not None else None

    @property
    def shuffle(self) -> bool | None:
        zone = self._zone
        return None if zone is None else zone.play_mode in ("SHUFFLE", "RANDOM")

    @property
    def repeat(self) -> RepeatMode | None:
        if (zone := self._zone) is None:
            return None
        if zone.play_mode == "REPEAT_ONE":
            return RepeatMode.ONE
        if zone.play_mode in ("REPEAT_ALL", "RANDOM"):
            return RepeatMode.ALL
        return RepeatMode.OFF

    @property
    def source_list(self) -> list[str]:
        return list(self.coordinator.sources)

    @property
    def source(self) -> str | None:
        return self.coordinator.source_of(self._zone)

    @property
    def group_members(self) -> list[str] | None:
        """Entitäten aller Räume, die mit diesem Raum in einer Zone spielen."""
        room = self._room
        if room is None or not room.room.zone_udn:
            return None
        registry = er.async_get(self.hass)
        members = [
            entity_id
            for other in self.coordinator.data.values()
            if other.room.zone_udn == room.room.zone_udn
            and (
                entity_id := registry.async_get_entity_id(
                    "media_player", DOMAIN, other.room.udn
                )
            )
        ]
        return members if len(members) > 1 else None

    # ------------------------------------------------------------- Befehle

    async def _wake(self) -> None:
        room = self._room
        if room is not None and "STANDBY" in room.room.power_state:
            await self.coordinator.api.async_leave_standby(self._udn)

    @raumfeld_command
    async def async_turn_on(self) -> None:
        await self.coordinator.api.async_leave_standby(self._udn)

    @raumfeld_command
    async def async_turn_off(self) -> None:
        self._cancel_announce()
        await self.coordinator.api.async_enter_manual_standby(self._udn)

    @raumfeld_command
    async def async_media_play(self) -> None:
        zone = self._zone_udn()
        await self._wake()
        await self.coordinator.api.async_play(zone)

    @raumfeld_command
    async def async_media_pause(self) -> None:
        await self.coordinator.api.async_pause(self._zone_udn())

    @raumfeld_command
    async def async_media_stop(self) -> None:
        self._cancel_announce()
        await self.coordinator.api.async_stop(self._zone_udn())

    @raumfeld_command
    async def async_media_next_track(self) -> None:
        await self.coordinator.api.async_next(self._zone_udn())

    @raumfeld_command
    async def async_media_previous_track(self) -> None:
        await self.coordinator.api.async_previous(self._zone_udn())

    @raumfeld_command
    async def async_media_seek(self, position: float) -> None:
        await self.coordinator.api.async_seek(self._zone_udn(), position)

    @raumfeld_command
    async def async_set_volume_level(self, volume: float) -> None:
        if (room := self._room) is not None:
            await self.coordinator.api.async_set_volume(room.room, round(volume * 100))

    @raumfeld_command
    async def async_mute_volume(self, mute: bool) -> None:
        if (room := self._room) is not None:
            await self.coordinator.api.async_set_mute(room.room, mute)

    async def _set_play_mode(self, shuffle: bool, repeat: RepeatMode) -> None:
        if shuffle:
            mode = "RANDOM" if repeat == RepeatMode.ALL else "SHUFFLE"
        elif repeat == RepeatMode.ONE:
            mode = "REPEAT_ONE"
        elif repeat == RepeatMode.ALL:
            mode = "REPEAT_ALL"
        else:
            mode = "NORMAL"
        await self.coordinator.api.async_set_play_mode(self._zone_udn(), mode)

    @raumfeld_command
    async def async_set_shuffle(self, shuffle: bool) -> None:
        await self._set_play_mode(shuffle, self.repeat or RepeatMode.OFF)

    @raumfeld_command
    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        await self._set_play_mode(bool(self.shuffle), repeat)

    @raumfeld_command
    async def async_select_source(self, source: str) -> None:
        """Startet einen Favoriten oder den Line-In-Eingang."""
        object_id = self.coordinator.sources.get(source)
        if object_id is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unknown_source",
                translation_placeholders={"source": source},
            )
        self._cancel_announce()
        await self._wake()
        zone = await self.coordinator.async_ensure_zone(self._udn)
        await self.coordinator.api.async_play_object(zone, object_id)

    @raumfeld_command
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Spielt einen Bibliothekseintrag, eine URL oder eine HA-Medienquelle.

        Mit announce=true wird danach fortgesetzt, was vorher lief.
        """
        api = self.coordinator.api
        if media_id.startswith(MEDIA_ID_PREFIX):
            self._cancel_announce()
            await self._wake()
            zone = await self.coordinator.async_ensure_zone(self._udn)
            await api.async_play_object(zone, media_id.removeprefix(MEDIA_ID_PREFIX))
            return

        mime: str | None = None
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id, mime = item.url, item.mime_type
        url = async_process_play_media_url(self.hass, media_id)
        title = (kwargs.get("extra") or {}).get("title")
        announce = bool(kwargs.get(ATTR_MEDIA_ANNOUNCE))

        room = self._room
        was_standby = room is not None and "STANDBY" in room.room.power_state
        await self._wake()
        zone = await self.coordinator.async_ensure_zone(self._udn)

        if not announce:
            self._cancel_announce()
            await api.async_play_uri(zone, url, title, mime)
            return

        # Bei mehreren Durchsagen hintereinander zählt der Zustand vor der ersten.
        if self._restore_task is not None:
            self._restore_task.cancel()
        if self._snapshot is None:
            media, transport = await asyncio.gather(
                api.async_transport(zone, "GetMediaInfo"),
                api.async_transport(zone, "GetTransportInfo"),
            )
            self._snapshot = _Snapshot(
                uri=media.get("CurrentURI") or None,
                metadata=media.get("CurrentURIMetaData") or "",
                was_playing=transport.get("CurrentTransportState") in _BUSY_STATES,
                was_standby=was_standby,
            )
        await api.async_play_uri(zone, url, title, mime)
        self._restore_task = self.coordinator.config_entry.async_create_background_task(
            self.hass, self._async_restore(zone), f"{DOMAIN}_restore_{self._udn}"
        )

    def _cancel_announce(self) -> None:
        """Verwirft eine anstehende Wiederherstellung (der Nutzer hat neu gewählt)."""
        if self._restore_task is not None:
            self._restore_task.cancel()
            self._restore_task = None
        self._snapshot = None

    async def _async_restore(self, zone: str) -> None:
        """Wartet das Ende der Durchsage ab und stellt den alten Zustand wieder her."""
        api = self.coordinator.api

        async def busy() -> bool:
            info = await api.async_transport(zone, "GetTransportInfo")
            return info.get("CurrentTransportState") in _BUSY_STATES

        try:
            # Erst auf den Start warten, dann auf das Ende.
            for _ in range(ANNOUNCE_START_TIMEOUT * 2):
                if await busy():
                    break
                await asyncio.sleep(0.5)
            for _ in range(ANNOUNCE_MAX_DURATION * 2):
                if not await busy():
                    break
                await asyncio.sleep(0.5)

            snapshot, self._snapshot = self._snapshot, None
            self._restore_task = None
            if snapshot is None:
                return
            # Nur fortsetzen, wenn vorher etwas lief: Die Box startet nach
            # SetAVTransportURI von selbst, ein stiller Zustand bliebe nicht still.
            if snapshot.uri and snapshot.was_playing and not snapshot.was_standby:
                await api.async_transport(
                    zone,
                    "SetAVTransportURI",
                    CurrentURI=snapshot.uri,
                    CurrentURIMetaData=snapshot.metadata,
                )
                await api.async_play(zone)
            if snapshot.was_standby:
                await api.async_enter_manual_standby(self._udn)
        except RaumfeldError as err:
            _LOGGER.warning("Wiedergabe nach Durchsage nicht wiederhergestellt: %s", err)
            self._snapshot = None
            self._restore_task = None
        await self.coordinator.async_request_refresh()

    # ------------------------------------------------------- Gruppierung

    @raumfeld_command
    async def async_join_players(self, group_members: list[str]) -> None:
        """Holt weitere Raumfeld-Räume in die Zone dieses Raums."""
        zone = await self.coordinator.async_ensure_zone(self._udn)
        registry = er.async_get(self.hass)
        for entity_id in group_members:
            entry = registry.async_get(entity_id)
            if entry is None or entry.platform != DOMAIN:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="not_groupable",
                    translation_placeholders={"entity": entity_id},
                )
            if entry.unique_id != self._udn:
                await self.coordinator.api.async_connect_room_to_zone(
                    entry.unique_id, zone
                )

    @raumfeld_command
    async def async_unjoin_player(self) -> None:
        """Löst diesen Raum aus seiner Zone."""
        await self.coordinator.api.async_drop_room(self._udn)

    # ------------------------------------------------------ Medienbrowser

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Raumfeld-Bibliothek (Favoriten, TuneIn, ...) und HA-Medienquellen."""
        if media_content_id and media_source.is_media_source_id(media_content_id):
            return await media_source.async_browse_media(
                self.hass,
                media_content_id,
                content_filter=lambda item: item.media_content_type.startswith("audio/"),
            )

        object_id = (media_content_id or "").removeprefix(MEDIA_ID_PREFIX) or OBJECT_ROOT
        is_root = object_id == OBJECT_ROOT
        try:
            children, _ = await self.coordinator.api.async_browse(object_id)
            title = "Teufel Raumfeld"
            if not is_root:
                current, _ = await self.coordinator.api.async_browse(object_id, metadata=True)
                title = current[0].title if current else object_id
        except RaumfeldError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        items = [_browse_item(child) for child in children]
        if is_root:
            items.append(
                BrowseMedia(
                    title="Home Assistant",
                    media_class=MediaClass.DIRECTORY,
                    media_content_id="media-source://",
                    media_content_type="",
                    can_play=False,
                    can_expand=True,
                )
            )
        return BrowseMedia(
            title=title,
            media_class=MediaClass.DIRECTORY,
            media_content_id=f"{MEDIA_ID_PREFIX}{object_id}",
            media_content_type=MediaType.MUSIC,
            can_play=False,
            can_expand=True,
            children=items,
            children_media_class=MediaClass.DIRECTORY if is_root else MediaClass.MUSIC,
        )
