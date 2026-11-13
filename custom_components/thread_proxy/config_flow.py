"""Bootstrap one manager entry; all BR admissions remain internal bindings."""
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from .const import DOMAIN, NAME, SERVICE_TYPE, DEFAULT_PORT
from .models import Discovery, discovery_host
from .catalog import MANAGER_ID, new_binding, is_manager
from .manager import get_manager

class ThreadProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    def __init__(self): self.discovered = None

    async def async_step_zeroconf(self, discovery_info):
        if discovery_info.type != SERVICE_TYPE: return self.async_abort(reason="invalid_discovery")
        try:
            record = Discovery.parse(discovery_info.properties,
                discovery_host(getattr(discovery_info, "addresses", []), str(discovery_info.host)),
                discovery_info.port, discovery_info.name, discovery_info.hostname)
        except ValueError as err:
            return self.async_abort(reason="unsupported_protocol" if str(err) == "unsupported_protocol" else "invalid_discovery")
        manager = get_manager(self.hass)
        manager.remember(record)
        if any(binding.unique_id == record.id for binding in manager.entries()):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(record.id)
        self.discovered = record
        self.context["title_placeholders"] = {"name": record.name}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        record = self.discovered
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({
                vol.Optional("noise_psk"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
                vol.Optional("create_device", default=True): bool}),
                description_placeholders={"name": record.name, "host": record.host, "port": str(record.port)})
        return await self.async_step_user({"host": record.host, "port": record.port, "name": record.name,
            "_proxy_id": record.id, "noise_psk": user_input.get("noise_psk", ""), "create_device": user_input.get("create_device", True)})

    async def async_step_user(self, user_input=None):
        if user_input is None:
            if any(is_manager(entry) for entry in self.hass.config_entries.async_entries(DOMAIN)):
                return self.async_abort(reason="already_configured")
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        errors = {}
        try:
            manager = get_manager(self.hass)
            binding = new_binding(user_input) if "host" in user_input else None
            if binding is not None: manager.check_duplicate(binding)
            if manager.root_entry is not None:
                if binding is None: return self.async_abort(reason="already_configured")
                await manager.bind(user_input)
                return self.async_abort(reason="binding_added")
            if any(is_manager(entry) for entry in self.hass.config_entries.async_entries(DOMAIN)):
                if binding is not None: raise ValueError("manager_not_ready")
                return self.async_abort(reason="already_configured")
            await self.async_set_unique_id(MANAGER_ID, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=NAME, data={"kind": "manager",
                "bindings": [binding.public_record()] if binding else [], "migration_pending": []})
        except (ValueError, KeyError, TypeError) as err:
            code = str(err)
            errors["base"] = code if code in ("already_configured", "invalid_host", "invalid_port", "invalid_key", "manager_not_ready") else "invalid_input"
        # Discovery confirmation keeps its encrypted-key input on validation errors.
        if self.discovered is not None:
            result = await self.async_step_confirm()
            result["errors"] = errors
            return result
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}), errors=errors)
