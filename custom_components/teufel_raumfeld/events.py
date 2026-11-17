"""Empfang von UPnP-Ereignissen (GENA), damit Änderungen sofort ankommen.

Die Box meldet Zustandsänderungen per HTTP-NOTIFY an eine Rückruf-Adresse. Dafür
läuft hier ein winziger eigener HTTP-Server auf einem freien Port. Der Inhalt der
Meldungen wird nicht ausgewertet – jede Meldung löst lediglich eine neue Abfrage
aus. Bleiben die Meldungen aus (z. B. wegen einer Firewall), arbeitet die
Integration einfach mit der regelmäßigen Abfrage weiter.

Wie api.py hängt dieses Modul nur von aiohttp ab.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import time

import aiohttp
from aiohttp import web

_LOGGER = logging.getLogger(__name__)

SUBSCRIPTION_SECONDS = 300
RENEW_AFTER = SUBSCRIPTION_SECONDS / 2
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)


class EventListener:
    """Verwaltet den Rückruf-Server und die Abonnements bei den Geräten."""

    def __init__(self, session: aiohttp.ClientSession, on_event: Callable[[], None]) -> None:
        self._session = session
        self._on_event = on_event
        self._runner: web.AppRunner | None = None
        self._callback_url: str | None = None
        # Ereignis-URL -> (Abo-Kennung, Zeitpunkt der letzten Verlängerung)
        self._subscriptions: dict[str, tuple[str, float]] = {}
        self._known_sids: set[str] = set()
        self._last_event: float | None = None

    @property
    def working(self) -> bool:
        """True, wenn Abos bestehen und mindestens eine Meldung angekommen ist.

        Jedes Gerät schickt direkt nach dem Abonnieren eine erste Meldung; kommt
        die nicht an, ist der Rückweg (Firewall, Netz) versperrt.
        """
        return bool(self._subscriptions) and self._last_event is not None

    async def async_start(self, local_ip: str) -> None:
        """Startet den Rückruf-Server auf einem freien Port."""
        app = web.Application()
        app.router.add_route("NOTIFY", "/notify", self._handle_notify)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self._callback_url = f"http://{local_ip}:{port}/notify"
        _LOGGER.debug("Ereignis-Empfang unter %s", self._callback_url)

    async def async_stop(self) -> None:
        """Kündigt alle Abos und beendet den Server."""
        await self.async_sync(set())
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_notify(self, request: web.Request) -> web.Response:
        await request.read()
        sid = request.headers.get("SID")
        if sid not in self._known_sids:
            # 412 veranlasst das Gerät, ein verwaistes Abo zu verwerfen.
            return web.Response(status=412)
        self._last_event = time.monotonic()
        self._on_event()
        return web.Response()

    async def async_sync(self, urls: set[str]) -> None:
        """Gleicht die Abos mit der gewünschten Menge an Ereignis-URLs ab."""
        if self._callback_url is None:
            return
        now = time.monotonic()
        for url in list(self._subscriptions):
            if url not in urls:
                sid, _ = self._subscriptions.pop(url)
                self._known_sids.discard(sid)
                await self._request("UNSUBSCRIBE", url, {"SID": sid})
        for url in urls:
            current = self._subscriptions.get(url)
            if current is not None and now - current[1] < RENEW_AFTER:
                continue
            sid = None
            if current is not None:
                sid = await self._request(
                    "SUBSCRIBE",
                    url,
                    {"SID": current[0], "TIMEOUT": f"Second-{SUBSCRIPTION_SECONDS}"},
                )
                if sid is None:
                    self._known_sids.discard(current[0])
                    self._subscriptions.pop(url, None)
            if sid is None:
                sid = await self._request(
                    "SUBSCRIBE",
                    url,
                    {
                        "CALLBACK": f"<{self._callback_url}>",
                        "NT": "upnp:event",
                        "TIMEOUT": f"Second-{SUBSCRIPTION_SECONDS}",
                    },
                    register=True,
                )
            if sid is not None:
                self._known_sids.add(sid)
                self._subscriptions[url] = (sid, now)

    async def _request(
        self, method: str, url: str, headers: dict[str, str], register: bool = False
    ) -> str | None:
        """Sendet SUBSCRIBE/UNSUBSCRIBE; liefert bei Erfolg die Abo-Kennung."""
        try:
            async with self._session.request(
                method, url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status != 200:
                    _LOGGER.debug("%s %s: HTTP %s", method, url, response.status)
                    return None
                sid = response.headers.get("SID") or headers.get("SID")
                if register and sid:
                    # Die erste Meldung kann eintreffen, bevor async_sync weiterläuft.
                    self._known_sids.add(sid)
                return sid
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("%s %s fehlgeschlagen: %s", method, url, err)
            return None
