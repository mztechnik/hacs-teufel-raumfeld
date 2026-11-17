"""Teufel Raumfeld: Raumfeld-Lautsprecher (z. B. Teufel One S) als Media-Player."""

from __future__ import annotations

import logging
import socket

from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DEFAULT_PORT, RaumfeldHost
from .coordinator import RaumfeldConfigEntry, RaumfeldCoordinator
from .events import EventListener

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.UPDATE,
]


def _local_ip_towards(host: str) -> str:
    """Eigene IP-Adresse, über die der Raumfeld-Host uns erreicht."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, DEFAULT_PORT))
        return sock.getsockname()[0]


async def async_setup_entry(hass: HomeAssistant, entry: RaumfeldConfigEntry) -> bool:
    """Richtet ein Raumfeld-System anhand eines Konfigurationseintrags ein."""
    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]
    api = RaumfeldHost(session, host)

    # Ereignis-Empfang ist ein Komfortgewinn; scheitert er, bleibt die Abfrage im Takt.
    events: EventListener | None = None
    coordinator = RaumfeldCoordinator(hass, entry, api)
    try:
        local_ip = await hass.async_add_executor_job(_local_ip_towards, host)
        events = EventListener(session, coordinator.async_event_received)
        await events.async_start(local_ip)
    except OSError as err:
        _LOGGER.warning("Ereignis-Empfang nicht möglich, es wird nur abgefragt: %s", err)
        events = None
    coordinator.events = events
    if events is not None:
        entry.async_on_unload(events.async_stop)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_create_background_task(
        hass, coordinator.async_watch_zones(), "teufel_raumfeld_zone_watch"
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RaumfeldConfigEntry) -> bool:
    """Entlädt einen Konfigurationseintrag."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
