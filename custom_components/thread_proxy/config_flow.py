"""Bind only explicitly advertised Thread Proxy BRs, or manually entered hosts."""
from __future__ import annotations
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from .const import DOMAIN, DEFAULT_PORT, SERVICE_TYPE
from .models import Discovery, host_name, api_port, encryption_key, proxy_id, discovery_host
from .manager import get_manager

class ThreadProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.discovered = None

    def fields(self, values=None):
        values = values or {}
        result = {
            vol.Required("host", default=values.get("host", "")): str,
            vol.Optional("port", default=values.get("port", DEFAULT_PORT)): int,
            vol.Optional("name", default=values.get("name", "")): str,
            vol.Optional("noise_psk"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        }
        return vol.Schema(result)

    async def async_step_zeroconf(self, discovery_info):
        if discovery_info.type != SERVICE_TYPE:
            return self.async_abort(reason="invalid_discovery")
        try:
            host = discovery_host(getattr(discovery_info, "addresses", []), str(discovery_info.host))
            record = Discovery.parse(discovery_info.properties, host, discovery_info.port,
                                     discovery_info.name, discovery_info.hostname)
        except ValueError as err:
            return self.async_abort(reason=str(err) if str(err) == "unsupported_protocol" else "invalid_discovery")
        get_manager(self.hass).remember(record)
        await self.async_set_unique_id(record.id)
        self._abort_if_unique_id_configured(updates={"host": record.host, "port": record.port}, reload_on_update=False)
        self.discovered = record
        self.context["title_placeholders"] = {"name": record.name}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        record = self.discovered
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({
                vol.Optional("noise_psk"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))}),
                description_placeholders={"name": record.name, "host": record.host, "port": str(record.port)})
        return await self.async_step_user({"host": record.host, "port": record.port, "name": record.name,
                                           "noise_psk": user_input.get("noise_psk", "")})

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                host = host_name(user_input["host"])
                port = api_port(user_input.get("port", DEFAULT_PORT))
                key = encryption_key(user_input.get("noise_psk", ""))
                name = str(user_input.get("name", "")).strip()[:120] or host.removesuffix(".local")
                self._async_abort_entries_match({"host": host, "port": port})
                if "_proxy_id" in user_input:
                    # A HA discovery confirmation card may already be open.
                    await self.async_set_unique_id(proxy_id(user_input["_proxy_id"]), raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                if self.discovered is not None:
                    self._abort_if_unique_id_configured()
                data = {"host": host, "port": port, "noise_psk": key, "binding_source": "discovery" if self.discovered or "_proxy_id" in user_input else "manual"}
                return self.async_create_entry(title=name, data=data)
            except (ValueError, KeyError, TypeError) as err:
                code = str(err)
                errors["base"] = code if code in ("invalid_host", "invalid_port", "invalid_key") else "invalid_input"
        return self.async_show_form(step_id="user", data_schema=self.fields(user_input), errors=errors)

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                data = {**entry.data, "host": host_name(user_input["host"]),
                        "port": api_port(user_input.get("port", DEFAULT_PORT))}
                if "noise_psk" in user_input:
                    data["noise_psk"] = encryption_key(user_input["noise_psk"])
                name = str(user_input.get("name", entry.title)).strip()[:120] or entry.title
                for other in self._async_current_entries():
                    if other.entry_id != entry.entry_id and other.data.get("host") == data["host"] and other.data.get("port", DEFAULT_PORT) == data["port"]:
                        raise ValueError("already_configured")
                # The entry update listener owns reloading (HA 2026.11+).
                return self.async_update_and_abort(entry, data_updates=data, title=name)
            except (ValueError, KeyError, TypeError) as err:
                code = str(err)
                errors["base"] = code if code in ("invalid_host", "invalid_port", "invalid_key", "already_configured") else "invalid_input"
        defaults = {**entry.data, "name": entry.title}
        return self.async_show_form(step_id="reconfigure", data_schema=self.fields(defaults), errors=errors)
