"""BR catalog and runtime ownership for the Thread Proxy integration."""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from homeassistant.components import zeroconf
from zeroconf import ServiceStateChange, IPVersion
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
from .connection import ProxyConnection
from .const import DOMAIN, SERVICE_TYPE, DEFAULT_PORT
from .models import BRState, Discovery, discovery_host

_LOGGER = logging.getLogger(__name__)

@dataclass
class Runtime:
    entry: object
    state: BRState
    data: dict
    listeners: set = field(default_factory=set)
    connection: ProxyConnection | None = None
    task: asyncio.Task | None = None

    def changed(self):
        for listener in tuple(self.listeners):
            listener()

class ProxyManager:
    def __init__(self, hass):
        self.hass = hass
        self.runtimes: dict[str, Runtime] = {}
        self.discovered: dict[str, Discovery] = {}
        self.browser = None
        self.tasks = set()
        self.panel_registered = False
        self.closing = False

    def entries(self):
        return [entry for entry in self.hass.config_entries.async_entries(DOMAIN)
                if getattr(entry, "source", None) != "ignore"]

    def remember(self, record: Discovery):
        self.discovered[record.id] = record
        for entry in self.entries():
            if entry.unique_id == record.id:
                changes = {**entry.data, "host": record.host, "port": record.port}
                if changes != dict(entry.data):
                    self.hass.config_entries.async_update_entry(entry, data=changes)

    async def start_discovery(self):
        if self.browser is not None:
            return
        instance = await zeroconf.async_get_async_instance(self.hass)
        self.browser = AsyncServiceBrowser(instance.zeroconf, [SERVICE_TYPE], handlers=[self.service_changed])

    def service_changed(self, zeroconf, service_type, name, state_change):
        # Zeroconf may invoke the callback outside the HA event-loop thread.
        self.hass.loop.call_soon_threadsafe(self._schedule_resolution, zeroconf, service_type, name, state_change)

    def _schedule_resolution(self, zc, service_type, name, change):
        if self.closing:
            return
        if change == ServiceStateChange.Removed:
            for record in self.discovered.values():
                if record.service_name == name:
                    record.present = False
            return
        task = self.hass.async_create_background_task(self.resolve(zc, service_type, name), "Thread Proxy discovery")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def resolve(self, zc, service_type, name):
        info = AsyncServiceInfo(service_type, name)
        try:
            if not await info.async_request(zc, 3000):
                return
            addresses = info.parsed_scoped_addresses(IPVersion.All)
            if not addresses:
                return
            properties = {(key.decode() if isinstance(key, bytes) else key):
                          (value.decode() if isinstance(value, bytes) else value)
                          for key, value in info.properties.items()}
            self.remember(Discovery.parse(properties, discovery_host(addresses, info.server or ""), info.port, name, info.server or ""))
        except (ValueError, UnicodeError, OSError):
            _LOGGER.debug("Ignored invalid Thread Proxy announcement: %s", name)

    async def add_entry(self, entry):
        state = BRState(entry.entry_id, entry.title, entry.data["host"], entry.data.get("port", DEFAULT_PORT),
                        proxy_id=entry.unique_id or entry.data.get("proxy_id", ""))
        runtime = Runtime(entry, state, dict(entry.data))
        self.runtimes[entry.entry_id] = runtime

        def identify(identity):
            if any(other.entry_id != entry.entry_id and other.unique_id == identity for other in self.entries()):
                return False
            if entry.unique_id != identity:
                self.hass.config_entries.async_update_entry(entry, unique_id=identity)
            return True

        runtime.connection = ProxyConnection(state, entry.data.get("noise_psk", ""), runtime.changed, identify)
        entry.runtime_data = runtime
        runtime.task = self.hass.async_create_background_task(runtime.connection.run(), f"Thread Proxy BR {entry.entry_id}")
        return runtime

    async def remove_entry(self, entry_id):
        runtime = self.runtimes.pop(entry_id, None)
        if runtime is None:
            return
        await runtime.connection.close()
        if runtime.task is not None:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)

    def snapshot(self):
        bound = []
        for entry in self.entries():
            runtime = self.runtimes.get(entry.entry_id)
            if runtime is not None:
                bound.append(runtime.state.public())
            else:
                # A disabled or failed setup remains a binding and can be removed.
                state = BRState(entry.entry_id, entry.title, entry.data.get("host", ""),
                                entry.data.get("port", DEFAULT_PORT), proxy_id=entry.unique_id or "",
                                status="disconnected", last_error="not_loaded")
                bound.append(state.public())
        identities = {entry.unique_id for entry in self.entries() if entry.unique_id}
        endpoints = {(entry.data.get("host"), entry.data.get("port", DEFAULT_PORT)) for entry in self.entries()}
        found = [item.public() for item in self.discovered.values()
                 if item.id not in identities and (item.host, item.port) not in endpoints and item.present]
        return {"bound": sorted(bound, key=lambda item: item["name"].casefold()),
                "discovered": sorted(found, key=lambda item: item["name"].casefold()),
                "data_plane_ready": False}

    async def close(self):
        self.closing = True
        for entry_id in list(self.runtimes):
            await self.remove_entry(entry_id)
        if self.browser is not None:
            await self.browser.async_cancel()
            self.browser = None
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

def get_manager(hass):
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = ProxyManager(hass)
    return hass.data[DOMAIN]
