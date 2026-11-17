"""Einrichtungsdialog der Teufel-Raumfeld-Integration."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from .api import RaumfeldConnectionError, RaumfeldError, RaumfeldHost
from .const import DOMAIN


class RaumfeldConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung per Hand (IP-Adresse) oder per SSDP-Erkennung."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._title: str = "Teufel Raumfeld"

    async def _async_probe(self, host: str) -> str | None:
        """Prüft den Host, merkt sich den Namen und liefert die System-Kennung."""
        api = RaumfeldHost(async_get_clientsession(self.hass), host)
        info = await api.async_get_host_info()
        self._title = info.get("hostName") or info.get("roomName") or self._title
        return await api.async_get_config_device_udn()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manuelle Einrichtung über die IP-Adresse."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                unique_id = await self._async_probe(host)
            except RaumfeldConnectionError:
                errors["base"] = "cannot_connect"
            except RaumfeldError:
                errors["base"] = "not_raumfeld"
            else:
                await self.async_set_unique_id(unique_id or host)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(title=self._title, data={CONF_HOST: host})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str}
            ),
            errors=errors,
        )

    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> ConfigFlowResult:
        """Automatisch im Netzwerk gefundener Raumfeld-Host."""
        host = urlparse(discovery_info.ssdp_location or "").hostname
        if not host:
            return self.async_abort(reason="cannot_connect")
        try:
            unique_id = await self._async_probe(host)
        except RaumfeldError:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(unique_id or host)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._host = host
        self.context["title_placeholders"] = {"name": self._title}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bestätigung eines gefundenen Geräts."""
        if user_input is not None:
            return self.async_create_entry(title=self._title, data={CONF_HOST: self._host})
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._title, "host": self._host or ""},
        )
