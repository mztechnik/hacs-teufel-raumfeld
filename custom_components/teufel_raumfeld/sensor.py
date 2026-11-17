"""Diagnose-Sensor: WLAN-Signalstärke der Box."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
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
    """Legt je Raum den Signal-Sensor an."""
    async_setup_rooms(
        entry, async_add_entities, lambda coordinator, udn: [RaumfeldSignal(coordinator, udn)]
    )


class RaumfeldSignal(RaumfeldEntity, SensorEntity):
    """WLAN-Signalstärke in Prozent, wie die Box sie meldet."""

    _attr_translation_key = "wifi_signal"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: RaumfeldCoordinator, room_udn: str) -> None:
        super().__init__(coordinator, room_udn, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        room = self._room
        return room.details.signal if room and room.details else None
