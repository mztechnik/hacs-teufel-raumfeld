"""Zahlen-Entitäten: Klangregler (Bass, Mitten, Höhen) und Sleep-Timer."""

from __future__ import annotations

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import TONE_MAX_DB, TONE_STEP_DB
from .coordinator import RaumfeldConfigEntry, RaumfeldCoordinator
from .entity import RaumfeldEntity, async_setup_rooms, raumfeld_command

PARALLEL_UPDATES = 0

TONE_BANDS = ("bass", "mid", "treble")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RaumfeldConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt Klangregler und Sleep-Timer je Raum an."""
    async_setup_rooms(
        entry,
        async_add_entities,
        lambda coordinator, udn: [
            *(RaumfeldTone(coordinator, udn, band) for band in TONE_BANDS),
            RaumfeldSleepTimer(coordinator, udn),
        ],
    )


class RaumfeldTone(RaumfeldEntity, NumberEntity):
    """Ein Band des Klangreglers in dB."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = -TONE_MAX_DB
    _attr_native_max_value = TONE_MAX_DB
    _attr_native_step = TONE_STEP_DB
    _attr_native_unit_of_measurement = "dB"

    def __init__(self, coordinator: RaumfeldCoordinator, room_udn: str, band: str) -> None:
        super().__init__(coordinator, room_udn, band)
        self._band = band
        self._attr_translation_key = band

    @property
    def native_value(self) -> float | None:
        room = self._room
        return room.tone.get(self._band) if room and room.tone else None

    @raumfeld_command
    async def async_set_native_value(self, value: float) -> None:
        room = self._room
        tone = {band: 0.0 for band in TONE_BANDS} | ((room.tone if room else None) or {})
        tone[self._band] = value
        await self.coordinator.api.async_set_tone(self._renderer_udn(), **tone)


class RaumfeldSleepTimer(RaumfeldEntity, NumberEntity):
    """Sleep-Timer der Box in Minuten; 0 schaltet ihn ab."""

    _attr_translation_key = "sleep_timer"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 720
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: RaumfeldCoordinator, room_udn: str) -> None:
        super().__init__(coordinator, room_udn, "sleep_timer")

    @property
    def native_value(self) -> float | None:
        zone = self._zone
        if zone is None or zone.sleep_seconds is None:
            return 0 if self._room is not None else None
        return math.ceil(zone.sleep_seconds / 60)

    @raumfeld_command
    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.api.async_set_sleep_timer(
            self._zone_udn(), round(value * 60)
        )
