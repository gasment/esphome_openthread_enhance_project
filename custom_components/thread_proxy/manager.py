"""One HA config entry owns a persistent catalog of independently connected BRs."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from homeassistant.components import zeroconf
from zeroconf import ServiceStateChange, IPVersion
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
from .br_device import br_device_enabled, register_br_entities, async_set_br_device
from .catalog import Binding, new_binding, is_manager
from .connection import ProxyConnection
from .uplink import PacketHub
from .nodes import NodeRegistry
from .const import DOMAIN, SERVICE_TYPE, DEFAULT_PORT
from .models import BRState, Discovery, discovery_host, host_name, api_port, encryption_key

_LOGGER = logging.getLogger(__name__)

@dataclass
class Runtime:
    entry: object
    state: BRState
    data: dict
    br_entities: list = field(default_factory=list)
    br_entity_factories: dict = field(default_factory=dict)
    br_device_enabled: bool = True
    br_device_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    listeners: set = field(default_factory=set)
    connection: ProxyConnection | None = None
    task: asyncio.Task | None = None
    def changed(self):
        for listener in tuple(self.listeners): listener()

class ProxyManager:
    def __init__(self, hass):
        self.hass = hass
        self.root_entry = None
        self.nodes = NodeRegistry(hass)
        self.uplink = PacketHub(lambda event, data: self.hass.bus.async_fire(event, data))
        self.bindings = {}
        self.runtimes = {}
        self.platforms = {}
        self.lock = asyncio.Lock()
        self.discovered = {}
        self.browser = None
        self.tasks = set()
        self.panel_registered = False
        self.closing = False

    def entries(self):
        if self.root_entry is not None: return list(self.bindings.values())
        # Read-only view of unconverted entries during migration/startup.
        return [entry for entry in self.hass.config_entries.async_entries(DOMAIN)
                if getattr(entry, "source", None) != "ignore" and "host" in entry.data]

    def get_binding(self, identity):
        binding = self.bindings.get(identity)
        return binding if binding is not None and binding.domain == DOMAIN else None

    def persist(self):
        if self.root_entry is None: raise ValueError("manager_not_ready")
        data = {**self.root_entry.data, "kind": "manager",
                "bindings": [binding.public_record() for binding in self.bindings.values()]}
        self.hass.config_entries.async_update_entry(self.root_entry, data=data)

    def check_duplicate(self, binding, exclude=None):
        for other in self.entries():
            if other.entry_id == exclude: continue
            if ((other.data.get("host"), other.data.get("port", DEFAULT_PORT)) ==
                (binding.data["host"], binding.data["port"]) or
                binding.unique_id and binding.unique_id == other.unique_id):
                raise ValueError("already_configured")

    async def setup_root(self, entry):
        self.root_entry = entry
        await self.nodes.load(entry)
        self.bindings = {record["id"]: Binding.restore(record, entry.entry_id) for record in entry.data.get("bindings", [])}
        for binding in self.bindings.values(): await self.add_entry(binding)
        entry.runtime_data = self

    async def register_platform(self, platform, factory, add_entities):
        # Serialize startup platform additions against checkbox/BR mutations.
        async with self.lock:
            self.platforms[platform] = (factory, add_entities)
            for runtime in self.runtimes.values():
                async with runtime.br_device_lock:
                    register_br_entities(runtime, platform, factory, add_entities)

    async def bind(self, values):
        async with self.lock:
            if self.root_entry is None: raise ValueError("manager_not_ready")
            binding = new_binding(values, self.root_entry.entry_id)
            self.check_duplicate(binding)
            self.bindings[binding.entry_id] = binding
            self.persist()  # Durable catalog first; never create a BR config entry.
            await self.add_entry(binding)
            for platform, (factory, add) in self.platforms.items():
                register_br_entities(self.runtimes[binding.entry_id], platform, factory, add)
            return binding

    async def reconfigure(self, binding, values):
        async with self.lock:
            host = host_name(values["host"])
            port = api_port(values.get("port", DEFAULT_PORT))
            name = values.get("name", binding.title)
            if not isinstance(name, str): raise ValueError("invalid_input")
            data = {**binding.data, "host": host, "port": port}
            if type(values.get("clear_key", False)) is not bool: raise ValueError("invalid_input")
            if values.get("clear_key"):
                if values.get("noise_psk"): raise ValueError("invalid_input")
                data["noise_psk"] = ""
            elif "noise_psk" in values:
                data["noise_psk"] = encryption_key(values["noise_psk"])
            proposed = Binding(binding.entry_id, name.strip()[:120] or binding.title, data,
                               binding.unique_id, dict(binding.options), binding.owner_entry_id)
            self.check_duplicate(proposed, exclude=binding.entry_id)
            binding.data, binding.title = data, proposed.title
            self.persist()
            await self.sync_one(binding)

    async def sync_one(self, binding):
        runtime = self.runtimes.get(binding.entry_id)
        if runtime is None: return
        reconnect = runtime.data != dict(binding.data)
        renamed = runtime.state.name != binding.title
        runtime.state.name = binding.title
        if reconnect:
            await self.stop_connection(runtime)
            runtime.data = dict(binding.data)
            runtime.state.host, runtime.state.port = binding.data["host"], binding.data["port"]
            self.start_connection(runtime)
        if renamed:
            from homeassistant.helpers import device_registry as dr
            registry = dr.async_get(self.hass)
            for device in dr.async_entries_for_config_entry(registry, binding.owner_entry_id):
                if (DOMAIN, binding.entry_id) in device.identifiers:
                    registry.async_update_device(device.id, name=binding.title)
        runtime.changed()

    async def sync_root(self, entry):
        # Internal writes already updated live objects before persisting. This
        # callback applies diagnostics preferences without resetting TCP sessions.
        async with self.lock:
            for binding in list(self.bindings.values()):
                await self.sync_one(binding)
                if self.runtimes[binding.entry_id].br_device_enabled != br_device_enabled(binding):
                    await async_set_br_device(self.hass, binding)

    async def set_device(self, binding, enabled):
        async with self.lock:
            await async_set_br_device(self.hass, binding, enabled, persist=True)

    async def unbind(self, binding):
        async with self.lock:
            await async_set_br_device(self.hass, binding, False)
            await self.remove_entry(binding.entry_id)
            self.bindings.pop(binding.entry_id)
            self.persist()

    async def reconnect(self, binding):
        async with self.lock:
            runtime = self.runtimes[binding.entry_id]
            await self.stop_connection(runtime)
            binding.data["enabled"] = True
            runtime.data = dict(binding.data)
            self.persist()
            self.start_connection(runtime)

    def remember(self, record):
        self.discovered[record.id] = record
        for entry in self.entries():
            if entry.unique_id == record.id:
                changes = {**entry.data, "host": record.host, "port": record.port}
                if changes == dict(entry.data): continue
                if isinstance(entry, Binding):
                    self.track(self.reconfigure(entry, {"host": record.host, "port": record.port}), "Thread Proxy address update")
                else:
                    self.hass.config_entries.async_update_entry(entry, data=changes)

    def track(self, coro, name):
        task = self.hass.async_create_background_task(coro, name)
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)
        return task

    async def start_discovery(self):
        if self.browser is not None: return
        instance = await zeroconf.async_get_async_instance(self.hass)
        self.browser = AsyncServiceBrowser(instance.zeroconf, [SERVICE_TYPE], handlers=[self.service_changed])

    def service_changed(self, zeroconf, service_type, name, state_change):
        self.hass.loop.call_soon_threadsafe(self._schedule_resolution, zeroconf, service_type, name, state_change)

    def _schedule_resolution(self, zc, service_type, name, change):
        if self.closing: return
        if change == ServiceStateChange.Removed:
            for record in self.discovered.values():
                if record.service_name == name: record.present = False
            return
        self.track(self.resolve(zc, service_type, name), "Thread Proxy discovery")

    async def resolve(self, zc, service_type, name):
        info = AsyncServiceInfo(service_type, name)
        try:
            if not await info.async_request(zc, 3000): return
            addresses = info.parsed_scoped_addresses(IPVersion.All)
            if not addresses: return
            properties = {(key.decode() if isinstance(key, bytes) else key):
                          (value.decode() if isinstance(value, bytes) else value) for key, value in info.properties.items()}
            self.remember(Discovery.parse(properties, discovery_host(addresses, info.server or ""), info.port, name, info.server or ""))
        except (ValueError, UnicodeError, OSError): _LOGGER.debug("Ignored invalid Thread Proxy announcement: %s", name)

    async def add_entry(self, entry):
        state = BRState(entry.entry_id, entry.title, entry.data["host"], entry.data.get("port", DEFAULT_PORT), proxy_id=entry.unique_id or "")
        runtime = Runtime(entry, state, dict(entry.data), br_device_enabled=br_device_enabled(entry))
        self.runtimes[entry.entry_id] = runtime
        self.start_connection(runtime)
        return runtime

    def start_connection(self, runtime):
        binding, state = runtime.entry, runtime.state
        if binding.data.get("enabled", True) is False:
            state.status, state.last_error = "disconnected", "binding_disabled"
            return
        def identify(identity):
            if any(other.entry_id != binding.entry_id and other.unique_id == identity for other in self.entries()): return False
            if binding.unique_id != identity:
                if isinstance(binding, Binding):
                    binding.unique_id = identity; self.persist()
                else: self.hass.config_entries.async_update_entry(binding, unique_id=identity)
            return True
        runtime.connection = ProxyConnection(state, binding.data.get("noise_psk", ""), runtime.changed, identify)
        runtime.connection.packet_handler = self.receive_node
        runtime.task = self.hass.async_create_background_task(runtime.connection.run(), f"Thread Proxy BR {binding.entry_id}")

    def receive_node(self,packet,br_id):
        if self.uplink.accept(packet,br_id): self.nodes.accept(packet,br_id)
        return self.nodes.need_schema(packet)

    async def stop_connection(self, runtime):
        if runtime.connection is not None: await runtime.connection.close()
        if runtime.task is not None:
            runtime.task.cancel(); await asyncio.gather(runtime.task, return_exceptions=True)
        runtime.connection, runtime.task = None, None

    async def remove_entry(self, identity):
        runtime = self.runtimes.pop(identity, None)
        if runtime is not None: await self.stop_connection(runtime)

    def snapshot(self):
        bound = []
        for entry in self.entries():
            runtime = self.runtimes.get(entry.entry_id)
            state = runtime.state if runtime else BRState(entry.entry_id, entry.title, entry.data["host"], entry.data.get("port", DEFAULT_PORT),
                                                         proxy_id=entry.unique_id or "", status="disconnected", last_error="not_loaded")
            bound.append({**state.public(), "create_device": br_device_enabled(entry), "has_key": bool(entry.data.get("noise_psk"))})
        identities = {entry.unique_id for entry in self.entries() if entry.unique_id}
        endpoints = {(entry.data.get("host"), entry.data.get("port", DEFAULT_PORT)) for entry in self.entries()}
        found = [item.public() for item in self.discovered.values() if item.id not in identities and (item.host, item.port) not in endpoints and item.present]
        return {"bound": sorted(bound, key=lambda item: item["name"].casefold()),
                "discovered": sorted(found, key=lambda item: item["name"].casefold()), "data_plane_ready": any(item["data_plane_ready"] for item in bound), "uplink": {**self.uplink.snapshot(),"node_devices":sum(node.info is not None for node in self.nodes.nodes.values()),"node_entities":len(self.nodes.entities)}}

    async def unload_root(self):
        async with self.lock:
            for identity in list(self.runtimes): await self.remove_entry(identity)
            await self.nodes.close()
            self.platforms.clear(); self.bindings.clear(); self.root_entry = None

    async def close(self):
        self.closing = True
        for task in tuple(self.tasks): task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.unload_root()
        if self.browser is not None: await self.browser.async_cancel(); self.browser = None

def get_manager(hass):
    if DOMAIN not in hass.data: hass.data[DOMAIN] = ProxyManager(hass)
    return hass.data[DOMAIN]
