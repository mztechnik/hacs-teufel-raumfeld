"""Firmware-Anzeige: installierte und verfügbare Version der Box.

Bewusst nur Anzeige: Das Einspielen eines Updates bleibt der Teufel-App überlassen.
"""

from __future__ import annotations

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RaumfeldConfigEntry, RaumfeldCoordinator
from .entity import RaumfeldEntity, async_setup_rooms

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RaumfeldConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt je Raum die Firmware-Entität an."""
    async_setup_rooms(
        entry, async_add_entities, lambda coordinator, udn: [RaumfeldFirmware(coordinator, udn)]
    )


class RaumfeldFirmware(RaumfeldEntity, UpdateEntity):
    """Zeigt an, ob für die Box eine neue Firmware bereitsteht."""

    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    def __init__(self, coordinator: RaumfeldCoordinator, room_udn: str) -> None:
        super().__init__(coordinator, room_udn, "firmware")

    @property
    def installed_version(self) -> str | None:
        room = self._room
        return room.details.installed_version if room and room.details else None

    @property
    def latest_version(self) -> str | None:
        room = self._room
        return room.details.latest_version if room and room.details else None
