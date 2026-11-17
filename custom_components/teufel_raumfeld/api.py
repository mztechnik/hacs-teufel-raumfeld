"""Asynchroner Client für Teufel-Raumfeld-Systeme (Host-Webservice + UPnP/SOAP).

Dieses Modul hängt bewusst nur von aiohttp ab (kein Home-Assistant-Import),
damit es sich auch eigenständig testen lässt.

Aufbau eines Raumfeld-Systems:
  * Der *Host* bietet auf Port 47365 einen kleinen HTTP-Webservice
    (getZones, listDevices, connectRoomToZone, leaveStandby, ...).
  * Jeder *Raum* hat einen physischen Renderer (den Lautsprecher).
  * Abgespielt wird über *Zonen*: Jede Zone ist ein virtueller UPnP-Renderer,
    dessen UDN zugleich die Zonen-ID ist. Transport-Befehle (Play, Pause,
    SetAVTransportURI, ...) gehen immer an diesen virtuellen Renderer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from urllib.parse import parse_qs, quote, urljoin, urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 47365
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)
# getZones mit updateID-Header antwortet erst bei einer Änderung (Long-Polling).
LONG_POLL_TIMEOUT = aiohttp.ClientTimeout(total=120)

TYPE_CONFIG_DEVICE = "urn:schemas-raumfeld-com:device:ConfigDevice:1"
TYPE_MEDIA_SERVER = "urn:schemas-upnp-org:device:MediaServer:1"
TYPE_RAUMFELD_DEVICE = "urn:schemas-raumfeld-com:device:RaumfeldDevice:1"

AV_TRANSPORT = "AVTransport"
RENDERING_CONTROL = "RenderingControl"
CONTENT_DIRECTORY = "ContentDirectory"
SETUP_SERVICE = "SetupService"

OBJECT_ROOT = "0"
OBJECT_FAVORITES = "0/Favorites/MyFavorites"
OBJECT_LINE_IN = "0/Line In"

SYSTEM_SOUNDS = ("Success", "Failure")

# Klangregler: die Box rechnet in 1/256 dB, erlaubt sind ±1536 (= ±6 dB).
# Intern rastet sie auf 10 Stufen je Richtung ein (0,6 dB pro Stufe).
_DB_UNITS = 256
TONE_MAX_DB = 6.0
TONE_STEP_DB = 0.6

_CONTENT_DIRECTORY_SID = "urn:upnp-org:serviceId:ContentDirectory"
_PLAYABLE_CONTAINERS = ("album", "playlistContainer", "trackContainer")

_SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body><u:{action} xmlns:u="{service_type}">{args}</u:{action}></s:Body>'
    "</s:Envelope>"
)

_DIDL_TEMPLATE = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    '<item id="0" parentID="-1" restricted="1">'
    "<dc:title>{title}</dc:title>"
    "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
    '<res protocolInfo="http-get:*:{mime}:*">{url}</res>'
    "</item></DIDL-Lite>"
)


class RaumfeldError(Exception):
    """Basisklasse für alle Fehler dieses Moduls."""


class RaumfeldConnectionError(RaumfeldError):
    """Host oder Gerät ist nicht erreichbar."""


class RaumfeldActionError(RaumfeldError):
    """Ein Gerät hat einen UPnP-Befehl abgelehnt."""


@dataclass(frozen=True)
class Room:
    """Ein Raum aus getZones."""

    udn: str
    name: str
    power_state: str
    renderer_udn: str | None
    zone_udn: str | None


@dataclass(frozen=True)
class DeviceRef:
    """Eintrag aus listDevices."""

    udn: str
    device_type: str
    location: str
    name: str


@dataclass
class UpnpService:
    """Ein UPnP-Dienst eines Geräts samt der von ihm angebotenen Aktionen."""

    service_type: str
    control_url: str
    actions: frozenset[str]
    event_url: str | None = None


@dataclass(frozen=True)
class MediaObject:
    """Ein Eintrag der Raumfeld-Medienbibliothek (ContentDirectory)."""

    object_id: str
    title: str
    upnp_class: str
    image: str | None
    is_container: bool

    @property
    def can_play(self) -> bool:
        """Einzeltitel immer, Ordner nur wenn sie eine Titelliste darstellen."""
        if not self.is_container:
            return True
        return any(kind in self.upnp_class for kind in _PLAYABLE_CONTAINERS)


@dataclass
class UpnpDevice:
    """Ein UPnP-Gerät mit seinen Diensten (Schlüssel: Kurzname, z. B. AVTransport)."""

    udn: str
    location: str
    friendly_name: str | None = None
    model_name: str | None = None
    services: dict[str, UpnpService] = field(default_factory=dict)

    def has_action(self, service: str, action: str) -> bool:
        """Prüft, ob das Gerät die Aktion im genannten Dienst anbietet."""
        svc = self.services.get(service)
        return svc is not None and action in svc.actions


def _local(tag: str) -> str:
    """Entfernt den XML-Namespace von einem Tag-Namen."""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def _parse_xml(text: str, what: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as err:
        raise RaumfeldError(f"Ungültiges XML ({what}): {err}") from err


def parse_zones(xml: str) -> list[Room]:
    """Wertet die Antwort von getZones aus."""
    root = _parse_xml(xml, "getZones")
    rooms: dict[str, Room] = {}

    def add(room_el: ET.Element, zone_udn: str | None) -> None:
        udn = room_el.get("udn")
        if not udn:
            return
        renderer_udn = None
        for child in room_el:
            if _local(child.tag) == "renderer" and child.get("udn"):
                renderer_udn = child.get("udn")
                break
        rooms[udn] = Room(
            udn=udn,
            name=room_el.get("name") or udn,
            power_state=(room_el.get("powerState") or "ACTIVE").upper(),
            renderer_udn=renderer_udn,
            zone_udn=zone_udn,
        )

    for element in root.iter():
        tag = _local(element.tag)
        if tag == "zone":
            for room_el in element:
                if _local(room_el.tag) == "room":
                    add(room_el, element.get("udn"))
        elif tag == "unassignedRooms":
            for room_el in element:
                if _local(room_el.tag) == "room":
                    add(room_el, None)
    return list(rooms.values())


def parse_devices(xml: str) -> dict[str, DeviceRef]:
    """Wertet die Antwort von listDevices aus."""
    root = _parse_xml(xml, "listDevices")
    devices: dict[str, DeviceRef] = {}
    for element in root.iter():
        if _local(element.tag) != "device":
            continue
        udn, location = element.get("udn"), element.get("location")
        if udn and location:
            devices[udn] = DeviceRef(
                udn=udn,
                device_type=element.get("type") or "",
                location=location,
                name=(element.text or "").strip(),
            )
    return devices


def parse_didl(xml: str | None) -> dict[str, str]:
    """Liest Titel, Interpret, Album und Cover aus DIDL-Lite-Metadaten."""
    if not xml or xml == "NOT_IMPLEMENTED":
        return {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    result: dict[str, str] = {}
    wanted = {
        "title": "title",
        "artist": "artist",
        "creator": "creator",
        "album": "album",
        "albumArtURI": "image",
        "class": "upnp_class",
    }
    for element in root.iter():
        key = wanted.get(_local(element.tag))
        text = (element.text or "").strip()
        if key and text and key not in result:
            result[key] = text
    if "artist" not in result and "creator" in result:
        result["artist"] = result["creator"]
    result.pop("creator", None)
    return result


def parse_media_objects(xml: str | None) -> list[MediaObject]:
    """Wertet das Ergebnis eines ContentDirectory-Browse aus."""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    objects: list[MediaObject] = []
    for element in root:
        kind = _local(element.tag)
        object_id = element.get("id")
        if kind not in ("container", "item") or not object_id:
            continue
        objects.append(
            MediaObject(
                object_id=object_id,
                title=_child_text(element, "title") or object_id,
                upnp_class=_child_text(element, "class") or "",
                image=_child_text(element, "albumArtURI"),
                is_container=kind == "container",
            )
        )
    return objects


def object_id_from_uri(uri: str | None) -> str | None:
    """Liest die Bibliotheks-ID aus einer dlna-playsingle/-playcontainer-URI."""
    if not uri or not uri.startswith("dlna-play"):
        return None
    query = parse_qs(urlsplit(uri).query)
    for key in ("iid", "cid"):
        if query.get(key):
            return query[key][0]
    return None


def parse_duration(value: str | None) -> float | None:
    """Wandelt eine UPnP-Zeitangabe (H:MM:SS[.f]) in Sekunden um."""
    if not value or ":" not in value:
        return None
    try:
        seconds = 0.0
        for part in value.split(":"):
            seconds = seconds * 60 + float(part)
    except ValueError:
        return None
    # Live-Streams melden als Dauer INT_MAX Millisekunden (596523:14:07).
    return seconds if seconds < 2_000_000 else None


def format_duration(seconds: float) -> str:
    """Wandelt Sekunden in eine UPnP-Zeitangabe (H:MM:SS) um."""
    total = max(0, int(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


class RaumfeldHost:
    """Zugriff auf einen Raumfeld-Host und die von ihm verwalteten Geräte."""

    def __init__(
        self, session: aiohttp.ClientSession, host: str, port: int = DEFAULT_PORT
    ) -> None:
        self._session = session
        self.host = host
        self._base = f"http://{host}:{port}"
        self._refs: dict[str, DeviceRef] = {}
        self._devices: dict[str, UpnpDevice] = {}
        # Renderer-UDN -> UDN des zugehörigen RaumfeldDevice (SetupService)
        self._setup_devices: dict[str, str] = {}

    # ------------------------------------------------------------------ HTTP

    async def _request(self, method: str, url: str, **kwargs) -> tuple[int, str]:
        try:
            async with self._session.request(
                method, url, timeout=REQUEST_TIMEOUT, **kwargs
            ) as response:
                return response.status, await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise RaumfeldConnectionError(
                f"{url} nicht erreichbar: {str(err) or type(err).__name__}"
            ) from err

    async def _host_get(self, path: str, params: dict[str, str] | None = None) -> str:
        status, text = await self._request("GET", f"{self._base}/{path}", params=params)
        if status != 200:
            raise RaumfeldError(f"Host antwortet auf {path} mit HTTP {status}")
        return text

    # ---------------------------------------------------------- Host-Dienste

    async def async_get_host_info(self) -> dict[str, str]:
        """Liefert hostName und roomName des Hosts."""
        root = _parse_xml(await self._host_get("getHostInfo"), "getHostInfo")
        return {
            _local(el.tag): (el.text or "").strip() for el in root if el.text
        }

    async def async_get_zones(self) -> list[Room]:
        """Liefert alle Räume samt Zonen-Zuordnung."""
        return parse_zones(await self._host_get("getZones"))

    async def async_refresh_devices(self) -> dict[str, DeviceRef]:
        """Aktualisiert die Geräteliste (UDN -> Beschreibungs-URL)."""
        self._refs = parse_devices(await self._host_get("listDevices"))
        # Beschreibungen verwerfen, deren Gerät verschwunden oder umgezogen ist
        # (GUPnP vergibt nach einem Neustart neue Ports).
        for udn, device in list(self._devices.items()):
            ref = self._refs.get(udn)
            if ref is None or ref.location != device.location:
                del self._devices[udn]
        return self._refs

    async def async_get_config_device_udn(self) -> str | None:
        """UDN des ConfigDevice – eindeutige Kennung des Raumfeld-Systems."""
        for ref in (await self.async_refresh_devices()).values():
            if ref.device_type == TYPE_CONFIG_DEVICE:
                return ref.udn
        return None

    async def async_connect_room_to_zone(
        self, room_udn: str, zone_udn: str | None = None
    ) -> None:
        """Hängt einen Raum an eine Zone; ohne Zone entsteht eine neue."""
        params = {"roomUDN": room_udn}
        if zone_udn:
            params["zoneUDN"] = zone_udn
        await self._host_get("connectRoomToZone", params)

    async def async_drop_room(self, room_udn: str) -> None:
        """Löst einen Raum aus seiner Zone."""
        await self._host_get("dropRoomJob", {"roomUDN": room_udn})

    async def async_wait_zone_change(self, update_id: str | None) -> str | None:
        """Wartet auf eine Änderung der Zonen-Konfiguration (Long-Polling).

        Liefert die neue updateID; bei Zeitablauf ohne Änderung die alte.
        """
        headers = {"updateID": update_id} if update_id else {}
        try:
            async with self._session.get(
                f"{self._base}/getZones", headers=headers, timeout=LONG_POLL_TIMEOUT
            ) as response:
                await response.read()
                if response.status != 200:
                    raise RaumfeldError(f"getZones: HTTP {response.status}")
                return response.headers.get("updateID") or update_id
        except asyncio.TimeoutError:
            return update_id
        except aiohttp.ClientError as err:
            raise RaumfeldConnectionError(f"getZones nicht erreichbar: {err}") from err

    async def async_leave_standby(self, room_udn: str) -> None:
        """Weckt einen Raum aus dem Standby."""
        await self._host_get("leaveStandby", {"roomUDN": room_udn})

    async def async_enter_manual_standby(self, room_udn: str) -> None:
        """Schickt einen Raum in den Standby."""
        await self._host_get("enterManualStandby", {"roomUDN": room_udn})

    # ------------------------------------------------------------------ UPnP

    async def async_get_device(self, udn: str) -> UpnpDevice:
        """Liefert die (zwischengespeicherte) Beschreibung eines Geräts."""
        if (device := self._devices.get(udn)) is not None:
            return device
        if udn not in self._refs:
            await self.async_refresh_devices()
        ref = self._refs.get(udn)
        if ref is None:
            raise RaumfeldError(f"Gerät {udn} ist dem Host nicht bekannt")
        device = await self._async_describe(ref)
        self._devices[udn] = device
        return device

    async def _async_describe(self, ref: DeviceRef) -> UpnpDevice:
        status, text = await self._request("GET", ref.location)
        if status != 200:
            raise RaumfeldError(f"Gerätebeschreibung {ref.location}: HTTP {status}")
        root = _parse_xml(text, "Gerätebeschreibung")
        base = _child_text(root, "URLBase") or ref.location
        device = UpnpDevice(udn=ref.udn, location=ref.location)
        for element in root.iter():
            tag = _local(element.tag)
            if tag == "device" and device.friendly_name is None:
                device.friendly_name = _child_text(element, "friendlyName")
                device.model_name = _child_text(element, "modelName")
            elif tag == "service":
                service_type = _child_text(element, "serviceType")
                control_url = _child_text(element, "controlURL")
                scpd_url = _child_text(element, "SCPDURL")
                event_url = _child_text(element, "eventSubURL")
                if not service_type or not control_url:
                    continue
                parts = service_type.split(":")
                short = parts[-2] if len(parts) >= 2 else service_type
                device.services[short] = UpnpService(
                    service_type=service_type,
                    control_url=urljoin(base, control_url),
                    actions=await self._async_actions(urljoin(base, scpd_url))
                    if scpd_url
                    else frozenset(),
                    event_url=urljoin(base, event_url) if event_url else None,
                )
        return device

    async def _async_actions(self, scpd_url: str) -> frozenset[str]:
        """Liest die Aktionsnamen aus einer Dienstbeschreibung (SCPD)."""
        try:
            status, text = await self._request("GET", scpd_url)
            if status != 200:
                return frozenset()
            root = _parse_xml(text, "SCPD")
        except RaumfeldError as err:
            _LOGGER.debug("SCPD %s nicht lesbar: %s", scpd_url, err)
            return frozenset()
        names = set()
        for element in root.iter():
            if _local(element.tag) == "action" and (name := _child_text(element, "name")):
                names.add(name)
        return frozenset(names)

    async def async_call(
        self, udn: str, service: str, action: str, **args: str | int
    ) -> dict[str, str]:
        """Führt eine UPnP-Aktion aus und liefert die Ausgabe-Argumente."""
        device = await self.async_get_device(udn)
        svc = device.services.get(service)
        if svc is None:
            raise RaumfeldError(f"Gerät {udn} bietet den Dienst {service} nicht an")
        body = _SOAP_ENVELOPE.format(
            action=action,
            service_type=svc.service_type,
            args="".join(
                f"<{key}>{escape(str(value))}</{key}>" for key, value in args.items()
            ),
        )
        try:
            status, text = await self._request(
                "POST",
                svc.control_url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPACTION": f'"{svc.service_type}#{action}"',
                },
            )
        except RaumfeldConnectionError:
            # Vermutlich ist der Dienst neu gestartet und hat einen neuen Port.
            self._devices.pop(udn, None)
            raise
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            root = None
        if status != 200:
            code = description = None
            if root is not None:
                for element in root.iter():
                    if _local(element.tag) == "errorCode":
                        code = (element.text or "").strip()
                    elif _local(element.tag) == "errorDescription":
                        description = (element.text or "").strip()
            raise RaumfeldActionError(
                f"{action} fehlgeschlagen (HTTP {status}"
                + (f", UPnP-Fehler {code}" if code else "")
                + (f": {description}" if description else "")
                + ")"
            )
        if root is None:
            raise RaumfeldError(f"{action}: Antwort ist kein gültiges XML")
        for element in root.iter():
            if _local(element.tag) == f"{action}Response":
                return {_local(child.tag): child.text or "" for child in element}
        return {}

    # ------------------------------------------------- Transport (Zonen-Ebene)

    async def async_transport(self, zone_udn: str, action: str, **args: str | int) -> dict[str, str]:
        """AVTransport-Aktion am virtuellen Renderer der Zone."""
        return await self.async_call(zone_udn, AV_TRANSPORT, action, InstanceID=0, **args)

    async def async_play(self, zone_udn: str) -> None:
        await self.async_transport(zone_udn, "Play", Speed="1")

    async def async_pause(self, zone_udn: str) -> None:
        await self.async_transport(zone_udn, "Pause")

    async def async_stop(self, zone_udn: str) -> None:
        await self.async_transport(zone_udn, "Stop")

    async def async_next(self, zone_udn: str) -> None:
        await self.async_transport(zone_udn, "Next")

    async def async_previous(self, zone_udn: str) -> None:
        await self.async_transport(zone_udn, "Previous")

    async def async_seek(self, zone_udn: str, position: float) -> None:
        await self.async_transport(
            zone_udn, "Seek", Unit="ABS_TIME", Target=format_duration(position)
        )

    async def async_set_play_mode(self, zone_udn: str, mode: str) -> None:
        await self.async_transport(zone_udn, "SetPlayMode", NewPlayMode=mode)

    async def async_play_uri(
        self, zone_udn: str, url: str, title: str | None = None, mime: str | None = None
    ) -> None:
        """Spielt eine beliebige HTTP-Audio-URL in der Zone ab."""
        metadata = _DIDL_TEMPLATE.format(
            title=escape(title or "Home Assistant"),
            mime=escape(mime or "audio/mpeg"),
            url=escape(url),
        )
        try:
            await self.async_transport(
                zone_udn,
                "SetAVTransportURI",
                CurrentURI=url,
                CurrentURIMetaData=metadata,
            )
        except RaumfeldActionError as err:
            _LOGGER.debug("SetAVTransportURI mit Metadaten abgelehnt (%s), ohne erneut", err)
            await self.async_transport(
                zone_udn, "SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=""
            )
        await self.async_play(zone_udn)

    async def async_get_sleep_timer(self, zone_udn: str) -> int:
        """Restzeit des Sleep-Timers in Sekunden (0 = nicht aktiv)."""
        result = await self.async_transport(zone_udn, "GetSleepTimerState")
        if result.get("Active", "0").strip().lower() not in ("1", "true"):
            return 0
        try:
            return int(result.get("SecondsUntilSleep", "0"))
        except ValueError:
            return 0

    async def async_set_sleep_timer(self, zone_udn: str, seconds: int) -> None:
        """Startet den Sleep-Timer; 0 bricht ihn ab."""
        if seconds <= 0:
            await self.async_transport(zone_udn, "CancelSleepTimer")
            return
        seconds = max(50, min(86400, seconds))
        await self.async_transport(
            zone_udn,
            "StartSleepTimer",
            SecondsUntilSleep=seconds,
            # Ausblenden über die letzte Minute (die Box verlangt mindestens 50 s).
            SecondsForVolumeRamp=60 if seconds >= 120 else 50,
        )

    # ------------------------------------------------------ Medienbibliothek

    async def _media_server_udn(self) -> str:
        for attempt in range(2):
            for ref in self._refs.values():
                if ref.device_type == TYPE_MEDIA_SERVER:
                    return ref.udn
            if attempt == 0:
                await self.async_refresh_devices()
        raise RaumfeldError("Der Raumfeld-Medienserver ist nicht erreichbar")

    async def async_browse(
        self, object_id: str, metadata: bool = False
    ) -> tuple[list[MediaObject], str]:
        """Liefert die Kinder eines Ordners – oder mit metadata=True das Objekt selbst.

        Zweiter Rückgabewert ist das rohe DIDL-Lite (als Metadaten fürs Abspielen).
        """
        result = await self.async_call(
            await self._media_server_udn(),
            CONTENT_DIRECTORY,
            "Browse",
            ObjectID=object_id,
            BrowseFlag="BrowseMetadata" if metadata else "BrowseDirectChildren",
            Filter="*",
            StartingIndex=0,
            RequestedCount=500,
            SortCriteria="",
        )
        raw = result.get("Result", "")
        return parse_media_objects(raw), raw

    async def async_play_object(self, zone_udn: str, object_id: str) -> None:
        """Spielt einen Eintrag der Medienbibliothek (Favorit, Sender, Album, Line-In)."""
        objects, raw = await self.async_browse(object_id, metadata=True)
        if not objects:
            raise RaumfeldError(f"Eintrag {object_id} nicht in der Medienbibliothek gefunden")
        server = quote(await self._media_server_udn(), safe="")
        sid = quote(_CONTENT_DIRECTORY_SID, safe="")
        oid = quote(object_id, safe="")
        if objects[0].is_container:
            uri = f"dlna-playcontainer://{server}?sid={sid}&cid={oid}&md=0"
        else:
            uri = f"dlna-playsingle://{server}?sid={sid}&iid={oid}"
        await self.async_transport(
            zone_udn, "SetAVTransportURI", CurrentURI=uri, CurrentURIMetaData=raw
        )
        await self.async_play(zone_udn)

    # ------------------------------------------- Klang und Systemtöne (Gerät)

    async def async_get_tone(self, renderer_udn: str) -> dict[str, float]:
        """Klangregler des Lautsprechers in dB (bass, mid, treble)."""
        result = await self.async_call(
            renderer_udn, RENDERING_CONTROL, "GetFilter", InstanceID=0
        )
        tone: dict[str, float] = {}
        for key, name in (("bass", "LowDB"), ("mid", "MidDB"), ("treble", "HighDB")):
            try:
                tone[key] = round(int(result[name]) / _DB_UNITS, 1)
            except (KeyError, ValueError):
                continue
        return tone

    async def async_set_tone(
        self, renderer_udn: str, bass: float, mid: float, treble: float
    ) -> None:
        def units(value: float) -> int:
            return round(max(-TONE_MAX_DB, min(TONE_MAX_DB, value)) * _DB_UNITS)

        await self.async_call(
            renderer_udn,
            RENDERING_CONTROL,
            "SetFilter",
            InstanceID=0,
            LowDB=units(bass),
            MidDB=units(mid),
            HighDB=units(treble),
        )

    async def async_play_system_sound(self, renderer_udn: str, sound: str) -> None:
        """Spielt einen eingebauten Hinweiston (Success oder Failure)."""
        await self.async_call(
            renderer_udn, RENDERING_CONTROL, "PlaySystemSound", InstanceID=0, Sound=sound
        )

    # ------------------------------------------------ Gerätedaten (Diagnose)

    async def _setup_udn(self, renderer_udn: str) -> str:
        """Findet das RaumfeldDevice (SetupService), das zu einem Renderer gehört."""
        if renderer_udn not in self._setup_devices:
            for ref in self._refs.values():
                if (
                    ref.device_type != TYPE_RAUMFELD_DEVICE
                    or ref.udn in self._setup_devices.values()
                ):
                    continue
                try:
                    result = await self.async_call(
                        ref.udn, SETUP_SERVICE, "GetDevice", Service="renderer"
                    )
                except RaumfeldError:
                    continue
                if udn := result.get("UniqueDeviceName"):
                    self._setup_devices[udn] = ref.udn
        if renderer_udn not in self._setup_devices:
            raise RaumfeldError("Kein SetupService für diesen Lautsprecher gefunden")
        return self._setup_devices[renderer_udn]

    async def async_get_device_info(self, renderer_udn: str) -> dict[str, str | int | None]:
        """WLAN-Signal sowie installierte und verfügbare Firmware eines Lautsprechers."""
        udn = await self._setup_udn(renderer_udn)
        network, info, update = await asyncio.gather(
            self.async_call(udn, SETUP_SERVICE, "GetNetworkInfo"),
            self.async_call(udn, SETUP_SERVICE, "GetInfo"),
            self.async_call(udn, SETUP_SERVICE, "GetUpdateInfo"),
        )
        try:
            signal: int | None = int(network.get("SignalStrength", ""))
        except ValueError:
            signal = None
        installed = info.get("SoftwareVersion") or None
        return {
            "signal": signal,
            "installed_version": installed,
            # Ohne anstehendes Update meldet die Box eine leere Version.
            "latest_version": update.get("Version") or installed,
        }

    # --------------------------------------------------- Lautstärke (Raum)

    async def _volume_targets(self, room: Room) -> list[tuple[str, bool]]:
        """Mögliche Ziele für Lautstärke-Befehle: (UDN, Raum-Aktionen nutzen?)."""
        targets: list[tuple[str, bool]] = []
        if room.zone_udn:
            try:
                zone = await self.async_get_device(room.zone_udn)
                if zone.has_action(RENDERING_CONTROL, "SetRoomVolume"):
                    targets.append((room.zone_udn, True))
            except RaumfeldError:
                pass
        if room.renderer_udn:
            targets.append((room.renderer_udn, False))
        if not targets:
            raise RaumfeldError(f"Raum {room.name} hat keinen ansprechbaren Renderer")
        return targets

    async def _volume_call(
        self, room: Room, zone_action: str, renderer_action: str, **args: str | int
    ) -> dict[str, str]:
        last_error: RaumfeldError | None = None
        for udn, room_actions in await self._volume_targets(room):
            try:
                if room_actions:
                    return await self.async_call(
                        udn, RENDERING_CONTROL, zone_action, InstanceID=0, Room=room.udn, **args
                    )
                return await self.async_call(
                    udn, RENDERING_CONTROL, renderer_action, InstanceID=0, Channel="Master", **args
                )
            except RaumfeldError as err:
                last_error = err
        assert last_error is not None
        raise last_error

    async def async_get_volume(self, room: Room) -> int | None:
        """Lautstärke des Raums (0-100)."""
        result = await self._volume_call(room, "GetRoomVolume", "GetVolume")
        try:
            return int(float(result.get("CurrentVolume", "")))
        except ValueError:
            return None

    async def async_get_mute(self, room: Room) -> bool | None:
        result = await self._volume_call(room, "GetRoomMute", "GetMute")
        value = result.get("CurrentMute")
        return None if value is None else value.strip().lower() in ("1", "true", "yes")

    async def async_set_volume(self, room: Room, volume: int) -> None:
        await self._volume_call(
            room, "SetRoomVolume", "SetVolume", DesiredVolume=max(0, min(100, volume))
        )

    async def async_set_mute(self, room: Room, mute: bool) -> None:
        await self._volume_call(room, "SetRoomMute", "SetMute", DesiredMute=int(mute))
