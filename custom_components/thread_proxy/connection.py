"""Persistent Native API connection to one BR proxy, never to an SED."""
from __future__ import annotations
import asyncio
import logging
import random
from aioesphomeapi import APIClient, APIConnectionError
from .const import INFO_SERVICE, STATUS_SERVICE
from .models import BRState, now, parse_metadata, proxy_id

_LOGGER = logging.getLogger(__name__)

class ProxyConnection:
    def __init__(self, state: BRState, key: str, changed, identify,
                 client_factory=APIClient, metadata_timeout=5):
        self.state, self.key = state, key
        self.changed, self.identify = changed, identify
        self.client_factory = client_factory
        self.metadata_timeout = metadata_timeout
        self.client = None
        self.stopping = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.metadata = asyncio.Event()

    def update(self, **values):
        for key, value in values.items():
            setattr(self.state, key, value)
        self.changed()

    def receive(self, call):
        if call.service not in (INFO_SERVICE, STATUS_SERVICE):
            return  # Node frames are not acknowledged by this management release.
        try:
            values = parse_metadata(call.data, self.state.proxy_id)
        except (ValueError, TypeError) as err:
            reason = str(err) if str(err) in ("identity_mismatch", "unsupported_protocol") else "invalid_metadata"
            self.update(status=reason if reason == "identity_mismatch" else "unsupported",
                        proxy_ready=False, last_error=reason)
            self.metadata.set()
            return
        if not self.identify(self.state.proxy_id):
            self.update(status="duplicate_identity", proxy_ready=False, last_error="duplicate_identity")
        else:
            self.update(**values, status="ready", proxy_ready=True, last_seen=now(), last_error=None)
        self.metadata.set()

    async def stopped(self, expected):
        self.update(connected=False, proxy_ready=False, connected_since=None)
        self.disconnected.set()

    async def run(self):
        delay = 2
        while not self.stopping.is_set():
            self.disconnected.clear()
            self.metadata.clear()
            self.update(status="connecting", connected=False, proxy_ready=False, last_error=None)
            client = self.client_factory(self.state.host, self.state.port,
                                         noise_psk=self.key or None, client_info="Thread Proxy", keepalive=15)
            self.client = client
            try:
                await client.connect(on_stop=self.stopped, login=False, log_errors=False)
                info = await client.device_info()
                actual_id = proxy_id(info.mac_address)
                if self.state.proxy_id and self.state.proxy_id != actual_id:
                    self.update(status="identity_mismatch", last_error="identity_mismatch")
                    raise ValueError("identity_mismatch")
                if not self.identify(actual_id):
                    self.update(status="duplicate_identity", last_error="duplicate_identity")
                    raise ValueError("duplicate_identity")
                self.update(proxy_id=actual_id, status="connected", connected=True,
                            firmware=info.esphome_version, model=info.model,
                            connected_since=now(), last_seen=now())
                client.subscribe_service_calls(self.receive)
                try:
                    await asyncio.wait_for(self.metadata.wait(), self.metadata_timeout)
                except asyncio.TimeoutError:
                    self.update(status="unsupported", last_error="proxy_not_announced")
                delay = 2
                await self.disconnected.wait()
            except asyncio.CancelledError:
                raise
            except (APIConnectionError, OSError, ValueError) as err:
                if self.state.status not in ("identity_mismatch", "duplicate_identity"):
                    kind = type(err).__name__
                    auth = any(word in kind for word in ("Auth", "Encryption", "Noise"))
                    self.update(status="auth_error" if auth else "disconnected",
                                last_error="authentication_failed" if auth else "cannot_connect")
                _LOGGER.debug("BR API connection to %s:%s failed (%s)", self.state.host, self.state.port, type(err).__name__)
            finally:
                try:
                    await client.disconnect(force=True)
                except (APIConnectionError, OSError):
                    pass
                self.client = None
                self.update(connected=False, proxy_ready=False, connected_since=None)
            if self.stopping.is_set():
                break
            if self.state.status in ("connected", "ready"):
                self.update(status="disconnected", last_error="connection_lost")
            try:
                await asyncio.wait_for(self.stopping.wait(), delay + random.random())
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)

    async def close(self):
        self.stopping.set()
        self.disconnected.set()
        if self.client is not None:
            try:
                await self.client.disconnect(force=True)
            except (APIConnectionError, OSError):
                pass
