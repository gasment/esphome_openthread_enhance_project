"""Native HA setup and runtime admission/entity selection. Node changes never reload the entry."""
import ipaddress
import secrets
import probatio as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DEFAULT_PORT, DOMAIN
from .coordinator import ipv6_addresses
from .protocol import ProtocolError, identifier


def settings_schema(defaults, initial=False):
    schema = {
        vol.Required("name", default=defaults.get("name", "OpenThread Gateway")): str,
        vol.Required("port", default=defaults.get("port", DEFAULT_PORT)): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
        vol.Optional("advertise_address", default=defaults.get("advertise_address", "")): str,
    }
    if initial:
        schema[vol.Required("backend_id", default=defaults.get("backend_id", "ha_" + secrets.token_hex(4)))] = str
    return vol.Schema(schema)


def validate_settings(data):
    address = data.get("advertise_address", "").strip()
    if address:
        parsed = ipaddress.IPv6Address(address)
        if parsed.is_link_local or parsed.is_loopback or parsed.is_multicast or parsed.is_unspecified or parsed.ipv4_mapped:
            raise ValueError("unroutable address")
        address = str(parsed)
    if "backend_id" in data and not identifier(data["backend_id"]):
        raise ValueError("invalid backend identity")
    return {**data, "advertise_address": address}


class GatewayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        defaults = {}
        if user_input is not None:
            try:
                data = validate_settings(user_input)
                return self.async_create_entry(title=data["name"], data=data)
            except ValueError:
                errors["base"] = "invalid_settings"
                defaults = user_input
        else:
            addresses = await ipv6_addresses(self.hass)
            if addresses:
                defaults["advertise_address"] = addresses[0]
        return self.async_show_form(step_id="user", data_schema=settings_schema(defaults, True), errors=errors)

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                data = {**entry.data, **validate_settings(user_input)}
                return self.async_update_reload_and_abort(entry, data_updates=data, title=data["name"])
            except ValueError:
                errors["base"] = "invalid_settings"
        return self.async_show_form(step_id="reconfigure", data_schema=settings_schema(entry.data), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return GatewayOptionsFlow()


class GatewayOptionsFlow(config_entries.OptionsFlow):
    def __init__(self):
        self.device = None

    @property
    def hub(self):
        return self.config_entry.runtime_data.hub

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["admit", "remove", "entities"])

    def device_selector(self, nodes):
        return selector.SelectSelector(selector.SelectSelectorConfig(options=[{"value": uid, "label": label} for uid, label in nodes]))

    async def async_step_admit(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self.hub.admit(user_input["device"])
                return self.async_create_entry(title="", data=dict(self.config_entry.options))
            except ProtocolError:
                errors["base"] = "node_changed"
        choices = [(uid, f'{item["hello"]["name"]} · {uid} · {item["hello"]["network"]}')
                   for uid, item in self.hub.candidates.items() if uid not in self.hub.nodes]
        if not choices:
            return self.async_abort(reason="no_candidates")
        return self.async_show_form(step_id="admit", data_schema=vol.Schema({vol.Required("device"): self.device_selector(choices)}), errors=errors)

    async def async_step_remove(self, user_input=None):
        if user_input is not None:
            self.device = user_input["device"]
            return await self.async_step_confirm_remove()
        choices = [(uid, f'{record["name"]} · {uid} · {record["status"]}') for uid, record in self.hub.nodes.items()]
        if not choices:
            return self.async_abort(reason="no_nodes")
        return self.async_show_form(step_id="remove", data_schema=vol.Schema({vol.Required("device"): self.device_selector(choices)}))

    async def async_step_confirm_remove(self, user_input=None):
        if user_input is not None:
            try:
                await self.hub.remove(self.device)
            except ProtocolError:
                return self.async_abort(reason="node_changed")
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        return self.async_show_form(step_id="confirm_remove", data_schema=vol.Schema({}), description_placeholders={"device": self.device})

    async def async_step_entities(self, user_input=None):
        if user_input is not None:
            self.device = user_input["device"]
            return await self.async_step_select_entities()
        choices = [(uid, f'{record["name"]} · {uid}') for uid, record in self.hub.nodes.items() if record["manifest"]]
        if not choices:
            return self.async_abort(reason="no_nodes")
        return self.async_show_form(step_id="entities", data_schema=vol.Schema({vol.Required("device"): self.device_selector(choices)}))

    async def async_step_select_entities(self, user_input=None):
        record = self.hub.nodes.get(self.device)
        if not record or not record["manifest"]:
            return self.async_abort(reason="node_changed")
        if user_input is not None:
            try:
                await self.hub.select_entities(self.device, None if user_input["all_entities"] else user_input.get("entities", []))
            except ProtocolError:
                return self.async_abort(reason="node_changed")
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        choices = [{"value": item["id"], "label": f'{item.get("name", item["id"])} ({item["type"]})'} for item in record["manifest"]["entities"]]
        schema = vol.Schema({vol.Required("all_entities", default=record["selected"] is None): bool,
            vol.Optional("entities", default=record["selected"] or []): selector.SelectSelector(selector.SelectSelectorConfig(options=choices, multiple=True))})
        return self.async_show_form(step_id="select_entities", data_schema=schema)
