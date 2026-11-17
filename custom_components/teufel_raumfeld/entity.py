"""Gemeinsame Basis aller Raumfeld-Entitäten."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable
import functools
from typing import Any, Concatenate

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import RaumfeldError
from .const import DOMAIN, MANUFACTURER
from .coordinator import RaumfeldConfigEntry, RaumfeldCoordinator, RoomState, ZoneState

# Kurze Pause, damit die Box den Befehl verarbeitet hat, bevor neu abgefragt wird.
REFRESH_DELAY = 0.4


@callback
def async_setup_rooms(
    entry: RaumfeldConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[RaumfeldCoordinator, str], Iterable[Entity]],
) -> None:
    """Legt für jeden Raum Entitäten an – auch für später auftauchende Räume."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_new_rooms() -> None:
        new = [udn for udn in coordinator.data if udn not in known]
        if new:
            known.update(new)
            async_add_entities(
                entity for udn in new for entity in factory(coordinator, udn)
            )

    add_new_rooms()
    entry.async_on_unload(coordinator.async_add_listener(add_new_rooms))


def raumfeld_command[_EntityT: RaumfeldEntity, **P](
    func: Callable[Concatenate[_EntityT, P], Awaitable[None]],
) -> Callable[Concatenate[_EntityT, P], Coroutine[Any, Any, None]]:
    """Übersetzt Gerätefehler in HomeAssistantError und aktualisiert danach den Zustand."""

    @functools.wraps(func)
    async def wrapper(self: _EntityT, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await func(self, *args, **kwargs)
        except RaumfeldError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await asyncio.sleep(REFRESH_DELAY)
        await self.coordinator.async_refresh()

    return wrapper


class RaumfeldEntity(CoordinatorEntity[RaumfeldCoordinator]):
    """Entität, die zu einem Raumfeld-Raum gehört."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: RaumfeldCoordinator, room_udn: str, key: str | None = None
    ) -> None:
        super().__init__(coordinator)
        self._udn = room_udn
        self._attr_unique_id = f"{room_udn}_{key}" if key else room_udn
        state = coordinator.data[room_udn]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, room_udn)},
            name=state.room.name,
            manufacturer=MANUFACTURER,
            model=state.model,
            sw_version=state.details.installed_version if state.details else None,
        )

    @property
    def _room(self) -> RoomState | None:
        return self.coordinator.data.get(self._udn) if self.coordinator.data else None

    @property
    def _zone(self) -> ZoneState | None:
        return room.zone if (room := self._room) else None

    @property
    def available(self) -> bool:
        return super().available and self._room is not None

    def _zone_udn(self) -> str:
        """Zone des Raums; Fehler, wenn der Raum gerade keiner Zone angehört."""
        room = self._room
        if room is None or not room.room.zone_udn:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_zone"
            )
        return room.room.zone_udn

    def _renderer_udn(self) -> str:
        room = self._room
        if room is None or not room.room.renderer_udn:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_available"
            )
        return room.room.renderer_udn
