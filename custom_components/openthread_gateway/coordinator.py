"""HA lifecycle, bounded IPv6 UDP transport, persistence and DNS-SD announcement."""
import asyncio
from contextlib import suppress
import ipaddress
import hashlib
import logging
import socket
import time

from zeroconf.asyncio import AsyncServiceInfo
from homeassistant.components import network, zeroconf
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

from .const import DEFAULT_PORT, DOMAIN, MAX_DATAGRAM, SERVICE_TYPE
from .entities import EntityManager
from .hub import GatewayHub

_LOGGER = logging.getLogger(__name__)


async def ipv6_addresses(hass):
    result = []
    for adapter in await network.async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for item in adapter["ipv6"]:
            address = ipaddress.IPv6Address(item["address"].split("%")[0])
            if not address.is_loopback and not address.is_link_local and not address.is_unspecified and not address.is_multicast:
                result.append(str(address))
    return sorted(set(result), key=lambda address: (not address.startswith("fd"), address))


class DatagramReceiver(asyncio.DatagramProtocol):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.rates = {}

    def datagram_received(self, data, peer):
        if not 0 < len(data) <= MAX_DATAGRAM or self.coordinator.stopping:
            return
        now = int(time.monotonic())
        key = peer[0]
        if len(self.rates) > 512:
            self.rates = {key: value for key, value in self.rates.items() if value[0] == now}
        second, count = self.rates.get(key, (now, 0))
        count = count + 1 if second == now else 1
        self.rates[key] = (now, count)
        if count > 100:
            return
        try:
            self.coordinator.queue.put_nowait((data, peer))
        except asyncio.QueueFull:
            self.coordinator.dropped += 1

    def error_received(self, error):
        _LOGGER.debug("UDP transport error: %s", type(error).__name__)


class GatewayCoordinator:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.storage = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}", private=True)
        self.transport = self.worker = self.zc = self.service = self.unsubscribe = None
        self.stopping = False
        self.loaded = False
        self.queue = asyncio.Queue(maxsize=256)
        self.dropped = 0
        self.hub = GatewayHub(entry.data["backend_id"], self._save, self._send, self._notify)
        self.entities = EntityManager(hass, entry, self.hub)

    async def _save(self, snapshot, urgent):
        if urgent:
            await self.storage.async_save(snapshot)
        else:
            self.storage.async_delay_save(self.hub.snapshot, 60)

    def _send(self, packet, peer):
        if self.transport and not self.stopping:
            self.transport.sendto(packet, peer)

    async def _notify(self, kind, uid, entity_id, value):
        if kind == "manifest":
            await self.entities.async_sync(uid)
        else:
            self.entities.notify(kind, uid, entity_id, value)

    async def _work(self):
        while True:
            packet, peer = await self.queue.get()
            try:
                await self.hub.receive(packet, peer)
            except Exception:
                _LOGGER.exception("Gateway packet processing failed; no success acknowledgment sent")
            finally:
                self.queue.task_done()

    async def _addresses(self):
        configured = self.entry.data.get("advertise_address", "")
        return [configured] if configured else await ipv6_addresses(self.hass)

    async def async_start(self):
        self.hub.restore(await self.storage.async_load())
        self.loaded = True
        addresses = await self._addresses()
        if not addresses:
            raise ValueError("No LAN IPv6 address available; enable IPv6 or reconfigure the advertised address")
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.setblocking(False)
            sock.bind(("::", self.entry.data.get("port", DEFAULT_PORT)))
            self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: DatagramReceiver(self), sock=sock)
        except Exception:
            sock.close()
            raise
        self.worker = asyncio.create_task(self._work(), name=f"{DOMAIN}-receiver")
        self.zc = await zeroconf.async_get_async_instance(self.hass)
        backend_id = self.entry.data["backend_id"]
        self.service = AsyncServiceInfo(SERVICE_TYPE, f"{backend_id}.{SERVICE_TYPE}",
            server=f"otgw-{hashlib.sha256(backend_id.encode()).hexdigest()[:16]}.local.", port=self.entry.data.get("port", DEFAULT_PORT),
            properties={"v": "3", "id": backend_id}, parsed_addresses=addresses)
        await self.zc.async_register_service(self.service)
        self.unsubscribe = async_track_time_interval(self.hass, self._maintenance, timedelta(seconds=10))
        self.entry.async_on_unload(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop))

    async def _on_stop(self, _event):
        await self.async_stop()

    async def _maintenance(self, _now):
        await self.hub.maintenance()
        if self.stopping or not self.service:
            return
        addresses = await self._addresses()
        if addresses and sorted(self.service.parsed_addresses()) != sorted(addresses):
            self.service = AsyncServiceInfo(SERVICE_TYPE, self.service.name, server=self.service.server,
                port=self.service.port, properties=self.service.properties, parsed_addresses=addresses)
            await self.zc.async_update_service(self.service)

    async def async_stop(self):
        if self.stopping:
            return
        self.stopping = True
        if self.unsubscribe:
            self.unsubscribe()
        if self.service and self.zc:
            with suppress(Exception):
                await self.zc.async_unregister_service(self.service)
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
        await self.hub.close()
        if self.transport:
            self.transport.close()
        # The shared HA zeroconf instance belongs to HA, never close it here.
        if self.loaded:
            await self.storage.async_save(self.hub.snapshot())

    def diagnostics(self):
        return {"backend_id": self.hub.backend_id, "port": self.entry.data.get("port", DEFAULT_PORT),
                "advertised_addresses": self.service.parsed_addresses() if self.service else [],
                "rejected_packets": self.hub.rejected, "queue_drops": self.dropped,
                "candidates": len(self.hub.candidates), "revocations": len(self.hub.revoked),
                "nodes": [{"device": uid, "name": record["name"], "status": record["status"],
                           "node_type": record["node_type"], "network": record["network"],
                           "entities": len(self.hub.exposed(uid)), "available": self.hub.available(uid)}
                          for uid, record in self.hub.nodes.items()]}
