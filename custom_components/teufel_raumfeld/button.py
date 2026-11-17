"""Tasten für die eingebauten Hinweistöne der Box."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SYSTEM_SOUNDS
from .coordinator import RaumfeldConfigEntry, RaumfeldCoordinator
from .entity import RaumfeldEntity, async_setup_rooms, raumfeld_command

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RaumfeldConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Legt je Raum eine Taste pro Hinweiston an."""
    async_setup_rooms(
        entry,
        async_add_entities,
        lambda coordinator, udn: [
            RaumfeldSystemSound(coordinator, udn, sound) for sound in SYSTEM_SOUNDS
        ],
    )


class RaumfeldSystemSound(RaumfeldEntity, ButtonEntity):
    """Spielt einen Hinweiston ab, ohne die laufende Wiedergabe zu unterbrechen."""

    def __init__(self, coordinator: RaumfeldCoordinator, room_udn: str, sound: str) -> None:
        super().__init__(coordinator, room_udn, f"sound_{sound.lower()}")
        self._sound = sound
        self._attr_translation_key = f"sound_{sound.lower()}"

    @raumfeld_command
    async def async_press(self) -> None:
        await self.coordinator.api.async_play_system_sound(
            self._renderer_udn(), self._sound
        )
