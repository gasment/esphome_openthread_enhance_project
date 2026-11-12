"""HA-independent, serialized v3 state machine. Storage and entity callbacks are injected."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import ipaddress
import secrets
import time

from .const import MAX_CANDIDATES, MAX_MANIFEST, MAX_NODES, MAX_PARTS, MAX_REVOKED
from .protocol import ProtocolError, authenticated, decode, decode_chunk, encode, hex_id, identifier, manifest, string, uint, valid_value


@dataclass
class Runtime:
    peer: tuple | None = None
    session: str = ""
    context: str = ""
    boot: int = -1
    last_seen: float = 0
    control_ready: bool = False
    sleeping_until: float = 0
    heartbeat: float = 10
    online: bool = False
    manifests: dict = field(default_factory=dict)
    reports: OrderedDict = field(default_factory=OrderedDict)
    source_seq: dict = field(default_factory=dict)
    event_sequences: set = field(default_factory=set)
    latest_event_seq: int | None = None


class GatewayHub:
    def __init__(self, backend_id, save, send, notify, clock=time.time):
        self.backend_id, self.save, self.send, self.notify, self.clock = backend_id, save, send, notify, clock
        self.nodes, self.revoked, self.generations = {}, {}, {}
        self.candidates, self.offers, self.runtime, self.commands = {}, {}, {}, {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.rejected = 0
        self.command_id = secrets.randbelow(0x7FFFFFFF) + 1

    def snapshot(self):
        return deepcopy({"backend_id": self.backend_id, "nodes": self.nodes,
                         "revoked": self.revoked, "generations": self.generations})

    def restore(self, data):
        if data is None:
            return
        if not isinstance(data, dict) or data.get("backend_id") != self.backend_id:
            raise ProtocolError("stored backend identity differs")
        nodes, revoked, generations = data.get("nodes"), data.get("revoked"), data.get("generations")
        if not isinstance(nodes, dict) or not isinstance(revoked, dict) or not isinstance(generations, dict) or len(nodes) > MAX_NODES or len(revoked) > MAX_REVOKED:
            raise ProtocolError("invalid persistent ledger")
        for uid, record in nodes.items():
            if not hex_id(uid, 16) or not isinstance(record, dict) or not hex_id(record.get("secret"), 64) or not hex_id(record.get("binding_id"), 32) or not uint(record.get("generation")) or record.get("status") not in {"pending", "active"}:
                raise ProtocolError("invalid stored binding")
            if not uint(record.get("boot", 0), 0xFFFFFFFFFFFFFFFF):
                raise ProtocolError("invalid boot counter")
            if not identifier(record.get("node")) or not string(record.get("name")) or not hex_id(record.get("network"), 16) or record.get("node_type") not in {"sed", "reed"}:
                raise ProtocolError("invalid stored node metadata")
            if not isinstance(record.get("values"), dict) or not isinstance(record.get("manifest_hash"), str) or record["manifest_hash"] and not hex_id(record["manifest_hash"], 64):
                raise ProtocolError("invalid stored state")
            if record.get("manifest") is not None:
                import json
                manifest(json.dumps(record["manifest"]).encode())
                descriptors = {item["id"]: item for item in record["manifest"]["entities"]}
                if any(key not in descriptors or descriptors[key]["type"] == "event" or not valid_value(descriptors[key], value) for key, value in record["values"].items()):
                    raise ProtocolError("invalid cached entity state")
            selected = record.get("selected")
            if selected is not None and (not isinstance(selected, list) or not all(identifier(item) for item in selected)):
                raise ProtocolError("invalid entity selection")
        for token, record in revoked.items():
            if not hex_id(token, 32) or not isinstance(record, dict) or not hex_id(record.get("device"), 16) or not hex_id(record.get("secret"), 64) or not uint(record.get("generation")):
                raise ProtocolError("invalid revocation")
        if any(not hex_id(uid, 16) or not uint(generation) for uid, generation in generations.items()):
            raise ProtocolError("invalid generation ledger")
        self.nodes, self.revoked, self.generations = deepcopy(nodes), deepcopy(revoked), deepcopy(generations)

    def reply(self, peer, kind, record=None, **fields):
        message = {"v": 3, "type": kind, "backend_id": self.backend_id, **fields}
        self.send(encode(message, record["secret"] if record else None), peer)

    async def durable(self, previous):
        try:
            await self.save(self.snapshot(), True)
        except Exception:
            self.nodes, self.revoked, self.generations = previous["nodes"], previous["revoked"], previous["generations"]
            raise

    async def admit(self, uid):
        async with self.lock:
            candidate = self.candidates.get(uid)
            if not candidate or self.clock() - candidate["seen"] > 1800:
                raise ProtocolError("node discovery expired; wait for next wake")
            if uid in self.nodes:
                raise ProtocolError("node already admitted; revoke existing binding first")
            if len(self.nodes) >= MAX_NODES:
                raise ProtocolError("node safety limit reached")
            previous = self.snapshot()
            generation = max(self.generations.get(uid, 0), candidate["hello"]["generation"]) + 1
            if generation > 0xFFFFFFFF:
                raise ProtocolError("binding generation exhausted")
            hello = candidate["hello"]
            self.generations[uid] = generation
            self.nodes[uid] = {"binding_id": secrets.token_hex(16), "secret": secrets.token_hex(32),
                               "generation": generation, "status": "pending", "boot": hello["boot"],
                               "bind_session": hello["session"], "bind_request": hello["request_id"],
                               "node": hello["node"], "name": hello["name"], "network": hello["network"],
                               "node_type": hello["node_type"], "manifest": None, "manifest_hash": "",
                               "selected": None, "values": {}}
            await self.durable(previous)
            self._binding_offer(uid, candidate)
            await self.notify("inventory", uid, None, None)

    def _binding_offer(self, uid, candidate):
        record, hello = self.nodes[uid], candidate["hello"]
        self.reply(candidate["peer"], "bind_offer", device=uid, node=hello["node"], session=hello["session"],
                   request_id=hello["request_id"], binding_id=record["binding_id"], generation=record["generation"],
                   secret=record["secret"])

    async def remove(self, uid):
        async with self.lock:
            if uid not in self.nodes:
                raise ProtocolError("unknown node")
            if len(self.revoked) >= MAX_REVOKED:
                raise ProtocolError("revocation ledger full; refusing to forget revoked bindings")
            previous = self.snapshot()
            record = self.nodes.pop(uid)
            self.revoked[record["binding_id"]] = {"device": uid, "secret": record["secret"], "generation": record["generation"]}
            await self.durable(previous)
            runtime = self.runtime.pop(uid, None)
            if runtime and runtime.peer:
                self.reply(runtime.peer, "binding_revoked", record, device=uid, binding_id=record["binding_id"],
                           session=runtime.session, ctx=runtime.context)
            self.offers.pop(uid, None)
            self._fail_commands(uid, "Node unbound")
            await self.notify("manifest", uid, None, None)

    async def select_entities(self, uid, selected):
        async with self.lock:
            record = self.nodes.get(uid)
            if not record or not record["manifest"]:
                raise ProtocolError("manifest not ready")
            known = {item["id"] for item in record["manifest"]["entities"]}
            if selected is not None and (not isinstance(selected, list) or not set(selected) <= known):
                raise ProtocolError("unknown entity selection")
            previous = self.snapshot()
            record["selected"] = list(dict.fromkeys(selected)) if selected is not None else None
            await self.durable(previous)
            await self.notify("manifest", uid, None, None)

    def exposed(self, uid):
        record = self.nodes.get(uid)
        if not record or record["status"] != "active" or not record["manifest"]:
            return []
        selected = record["selected"]
        return [item for item in record["manifest"]["entities"] if selected is None or item["id"] in selected]

    def available(self, uid):
        runtime = self.runtime.get(uid)
        if not runtime or not runtime.online:
            return False
        record = self.nodes[uid]
        age = self.clock() - runtime.last_seen
        return age <= runtime.heartbeat * 3 if record["node_type"] == "reed" else self.clock() <= max(runtime.last_seen + 300, runtime.sleeping_until + 120)

    async def receive(self, packet, peer):
        try:
            message, signature, raw = decode(packet)
            address = ipaddress.ip_address(peer[0].split("%")[0])
            if address.version != 6 or address.is_multicast or address.is_link_local or address.is_unspecified or address.ipv4_mapped:
                raise ProtocolError("routable IPv6 source required")
            uid = message.get("device")
            if not hex_id(uid, 16):
                raise ProtocolError("invalid device")
            async with self.lock:
                if self.closed:
                    return
                if message["type"] == "hello":
                    await self._hello(uid, message, signature, raw, peer)
                    return
                record = self.nodes.get(uid)
                token = message.get("binding_id")
                revoked = self.revoked.get(token) if isinstance(token, str) else None
                if revoked and revoked["device"] == uid and authenticated(signature, raw, revoked["secret"]):
                    self.reply(peer, "binding_revoked", revoked, device=uid, binding_id=token,
                               session=message.get("session", ""), ctx=message.get("ctx", ""))
                    return
                if not record or token != record["binding_id"] or message.get("backend_id") != self.backend_id or not authenticated(signature, raw, record["secret"]):
                    raise ProtocolError("unauthorized binding")
                if message["type"] in {"bind_ack", "session_ack"}:
                    await self._activate(uid, message, peer)
                    return
                runtime = self.runtime.get(uid)
                if not runtime or peer != runtime.peer or message.get("session") != runtime.session or message.get("ctx") != runtime.context:
                    self.reply(peer, "context_expired", record, device=uid, binding_id=record["binding_id"], session=message.get("session", ""))
                    return
                await self._business(uid, message, runtime, record, peer)
        except (ProtocolError, ValueError, TypeError, KeyError, OverflowError):
            self.rejected += 1

    async def _hello(self, uid, message, signature, raw, peer):
        if (not identifier(message.get("node")) or not string(message.get("name")) or not hex_id(message.get("network"), 16)
                or message.get("node_type") not in {"sed", "reed"} or not hex_id(message.get("session"), 16)
                or not hex_id(message.get("request_id"), 16) or not uint(message.get("generation"))
                or not uint(message.get("boot"), 0xFFFFFFFFFFFFFFFF)):
            raise ProtocolError("invalid hello")
        if message.get("expected_backend", "") not in {"", self.backend_id}:
            return
        owner, token = message.get("backend_id", ""), message.get("binding_id", "")
        if owner not in {"", self.backend_id}:
            return
        revoked = self.revoked.get(token) if isinstance(token, str) else None
        if revoked and revoked["device"] == uid and authenticated(signature, raw, revoked["secret"]):
            self.reply(peer, "binding_revoked", revoked, device=uid, binding_id=token,
                       session=message["session"], request_id=message["request_id"])
            return
        record = self.nodes.get(uid)
        candidate = {"hello": message, "peer": peer, "seen": self.clock()}
        if not record:
            if owner or token or signature != "-":
                return
            if uid not in self.candidates and len(self.candidates) >= MAX_CANDIDATES:
                return
            self.candidates[uid] = candidate
            return
        if record["status"] == "pending" and not owner and not token and signature == "-":
            if record.get("bind_session") != message["session"] or record.get("bind_request") != message["request_id"]:
                previous = self.snapshot()
                record.update(bind_session=message["session"], bind_request=message["request_id"])
                await self.durable(previous)
            self.candidates[uid] = candidate
            self._binding_offer(uid, candidate)
            return
        if (owner != self.backend_id or token != record["binding_id"] or message["generation"] != record["generation"]
                or not authenticated(signature, raw, record["secret"]) or message["boot"] < record["boot"]):
            raise ProtocolError("hello cannot replace existing ownership")
        existing = self.offers.get(uid)
        if not existing or existing["hello"] != message or existing["peer"] != peer or self.clock() - existing["seen"] > 30:
            existing = {**candidate, "ctx": secrets.token_hex(8)}
            self.offers[uid] = existing
        self.reply(peer, "session_offer", record, device=uid, binding_id=token, session=message["session"],
                   request_id=message["request_id"], ctx=existing["ctx"], generation=record["generation"])

    async def _activate(self, uid, message, peer):
        record = self.nodes[uid]
        if message["type"] == "bind_ack":
            if record["status"] == "active" and message.get("session") == record.get("bind_session") and message.get("request_id") == record.get("bind_request"):
                self.reply(peer, "bind_complete", record, device=uid, binding_id=record["binding_id"], session=message["session"], request_id=message["request_id"])
                return
            candidate = self.candidates.get(uid)
            if record["status"] != "pending" or not candidate or peer != candidate["peer"] or message.get("request_id") != candidate["hello"]["request_id"] or message.get("session") != candidate["hello"]["session"]:
                raise ProtocolError("invalid binding acknowledgment")
            previous = self.snapshot()
            record["status"] = "active"
            await self.durable(previous)
            self.candidates.pop(uid, None)
            self.reply(peer, "bind_complete", record, device=uid, binding_id=record["binding_id"], session=message["session"], request_id=message["request_id"])
            return  # A signed hello/session handshake is required after binding.
        offer = self.offers.get(uid)
        current = self.runtime.get(uid)
        if current and current.peer == peer and message.get("session") == current.session and message.get("ctx") == current.context:
            self._reply(uid, "session_ready", manifest_hash=record["manifest_hash"], request_state=True)
            return
        if (not offer or self.clock() - offer["seen"] > 30 or peer != offer["peer"] or message.get("ctx") != offer["ctx"]
                or message.get("request_id") != offer["hello"]["request_id"] or message.get("session") != offer["hello"]["session"]):
            raise ProtocolError("invalid context acknowledgment")
        previous = self.snapshot()
        record.update(status="active", boot=offer["hello"]["boot"], network=offer["hello"]["network"], node=offer["hello"]["node"], node_type=offer["hello"]["node_type"])
        await self.durable(previous)
        self._fail_commands(uid, "Node context changed")
        self.runtime[uid] = Runtime(peer=peer, session=message["session"], context=message["ctx"], boot=record["boot"], last_seen=self.clock(), online=True)
        self.offers.pop(uid, None)
        self.reply(peer, "session_ready", record, device=uid, binding_id=record["binding_id"], session=message["session"], ctx=message["ctx"],
                   manifest_hash=record["manifest_hash"], request_state=True)
        await self.notify("availability", uid, None, None)

    def _reply(self, uid, kind, **fields):
        runtime, record = self.runtime[uid], self.nodes[uid]
        self.reply(runtime.peer, kind, record, device=uid, binding_id=record["binding_id"], session=runtime.session, ctx=runtime.context, **fields)

    async def _business(self, uid, message, runtime, record, peer):
        kind = message["type"]
        if kind not in {"presence", "manifest", "state", "command_result"}:
            raise ProtocolError("unknown message type")
        if "control_ready" in message and type(message["control_ready"]) is not bool:
            raise ProtocolError("invalid control readiness")
        if "sleeping" in message and type(message["sleeping"]) is not bool:
            raise ProtocolError("invalid sleep flag")
        if "heartbeat_interval_ms" in message and (not uint(message["heartbeat_interval_ms"], 3600000) or message["heartbeat_interval_ms"] < 1000):
            raise ProtocolError("invalid heartbeat")
        if "sleep_for_s" in message and not uint(message["sleep_for_s"], 604800):
            raise ProtocolError("invalid sleep duration")
        if kind == "manifest":
            await self._manifest(uid, message)
        elif kind == "state":
            await self._state(uid, message)
        elif kind == "command_result":
            command = self.commands.get((uid, message.get("command_id"))) if uint(message.get("command_id")) else None
            if not command or type(message.get("success")) is not bool:
                return
            entity = next((e for e in record["manifest"]["entities"] if e["id"] == command["entity"]), None)
            if "value" in message:
                if not entity or not valid_value(entity, message["value"]) or not uint(message.get("state_seq")):
                    raise ProtocolError("invalid command state")
                await self._apply_value(uid, entity, message["value"], message["state_seq"], False)
            if not command["future"].done():
                command["future"].set_result(message)
        runtime.last_seen, runtime.online = self.clock(), True
        runtime.control_ready = message.get("control_ready", runtime.control_ready)
        runtime.heartbeat = message.get("heartbeat_interval_ms", int(runtime.heartbeat * 1000)) / 1000
        runtime.sleeping_until = self.clock() + message.get("sleep_for_s", 0)
        if kind == "presence":
            if message.get("sleeping"):
                runtime.control_ready = False
            matched = record["manifest_hash"] and message.get("manifest_hash") == record["manifest_hash"]
            self._reply(uid, "presence_ack", manifest_hash=record["manifest_hash"], request_manifest=not bool(matched))
        await self.notify("availability", uid, None, None)

    async def _manifest(self, uid, message):
        runtime, record = self.runtime[uid], self.nodes[uid]
        digest, part, parts = message.get("manifest_hash"), message.get("part"), message.get("parts")
        if not hex_id(digest, 64) or not uint(part, MAX_PARTS - 1) or not uint(parts, MAX_PARTS) or not 1 <= parts or part >= parts:
            raise ProtocolError("invalid manifest fragment")
        chunk = decode_chunk(message.get("payload"))
        assembly = runtime.manifests.get(digest)
        if not assembly or self.clock() - assembly["at"] > 120:
            runtime.manifests.clear()
            assembly = {"at": self.clock(), "parts": parts, "chunks": {}}
            runtime.manifests[digest] = assembly
        if assembly["parts"] != parts or part in assembly["chunks"] and assembly["chunks"][part] != chunk:
            raise ProtocolError("conflicting manifest fragment")
        assembly["chunks"][part] = chunk
        if sum(map(len, assembly["chunks"].values())) > MAX_MANIFEST:
            runtime.manifests.clear()
            raise ProtocolError("manifest too large")
        complete = len(assembly["chunks"]) == parts
        if complete:
            raw = b"".join(assembly["chunks"][index] for index in range(parts))
            if hashlib.sha256(raw).hexdigest() != digest:
                runtime.manifests.clear()
                raise ProtocolError("manifest hash mismatch")
            parsed = manifest(raw)
            if digest != record["manifest_hash"]:
                previous = self.snapshot()
                record.update(manifest=parsed, manifest_hash=digest, name=parsed["name"])
                old_entities = {e["id"]: e for e in (previous["nodes"][uid]["manifest"] or {"entities": []})["entities"]}
                record["values"] = {e["id"]: record["values"][e["id"]] for e in parsed["entities"]
                                    if e["id"] in record["values"] and old_entities.get(e["id"], {}).get("type") == e["type"] and valid_value(e, record["values"][e["id"]])}
                await self.durable(previous)
                await self.notify("manifest", uid, None, None)
            runtime.manifests.clear()
        self._reply(uid, "manifest_ack", manifest_hash=digest, part=part, complete=complete, request_state=complete)

    async def _state(self, uid, message):
        runtime, record = self.runtime[uid], self.nodes[uid]
        if not record["manifest"] or message.get("manifest_hash") != record["manifest_hash"]:
            self._reply(uid, "manifest_request")
            return
        seq, report, part, parts = (message.get(key) for key in ("seq", "report_id", "part", "parts"))
        values = message.get("values")
        if not uint(seq) or not uint(report) or not uint(part, MAX_PARTS - 1) or not uint(parts, MAX_PARTS) or not 1 <= parts or part >= parts or not isinstance(values, dict) or type(message.get("snapshot", False)) is not bool:
            raise ProtocolError("invalid state fragment")
        entities = {entity["id"]: entity for entity in record["manifest"]["entities"]}
        if any(key not in entities or not valid_value(entities[key], value) for key, value in values.items()):
            raise ProtocolError("invalid report value")
        key = (report, seq)
        receipt = runtime.reports.get(key)
        if not receipt:
            receipt = {"parts": parts, "snapshot": message.get("snapshot", False), "values": {}, "hashes": {}, "at": self.clock(), "complete": False}
            runtime.reports[key] = receipt
            while len(runtime.reports) > 64:
                runtime.reports.popitem(last=False)
        if receipt["parts"] != parts or receipt["snapshot"] != message.get("snapshot", False):
            raise ProtocolError("inconsistent report")
        import json
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if part in receipt["hashes"] and receipt["hashes"][part] != fingerprint:
            raise ProtocolError("conflicting report fragment")
        receipt["hashes"][part] = fingerprint
        if receipt["complete"]:
            self._reply(uid, "report_ack", report_id=report, seq=seq, complete=True)
            return
        receipt["values"][part] = values
        if sum(len(json.dumps(item["values"])) for item in runtime.reports.values()) > 65536:
            runtime.reports.pop(key, None)
            raise ProtocolError("incomplete report memory limit exceeded")
        if len(receipt["values"]) < parts:
            return
        if not receipt["complete"]:
            all_values = {}
            for page in receipt["values"].values():
                if all_values.keys() & page.keys():
                    raise ProtocolError("entity repeated across report fragments")
                all_values.update(page)
            event_fresh = self._event_fresh(runtime, seq)
            for entity_id, value in all_values.items():
                await self._apply_value(uid, entities[entity_id], value, seq, event_fresh and not receipt["snapshot"])
            receipt["complete"] = True
            receipt["values"].clear()
            await self.save(None, False)  # Snapshot at checkpoint time, not for every sensor packet.
        self._reply(uid, "report_ack", report_id=report, seq=seq, complete=True)

    @staticmethod
    def _event_fresh(runtime, seq):
        latest = runtime.latest_event_seq
        if latest is None or 0 < ((seq - latest) & 0xFFFFFFFF) < 0x80000000:
            runtime.latest_event_seq = seq
        elif ((latest - seq) & 0xFFFFFFFF) >= 64:
            return False
        if seq in runtime.event_sequences:
            return False
        runtime.event_sequences.add(seq)
        runtime.event_sequences = {item for item in runtime.event_sequences if ((runtime.latest_event_seq - item) & 0xFFFFFFFF) < 64}
        return True

    async def _apply_value(self, uid, entity, value, seq, event_fresh):
        runtime = self.runtime[uid]
        if entity["type"] == "event":
            if event_fresh:
                await self.notify("event", uid, entity["id"], value)
            return
        old = runtime.source_seq.get(entity["id"])
        if old is not None and not 0 < ((seq - old) & 0xFFFFFFFF) < 0x80000000:
            return
        runtime.source_seq[entity["id"]] = seq
        self.nodes[uid]["values"][entity["id"]] = value
        await self.notify("state", uid, entity["id"], value)

    async def command(self, uid, entity_id, value):
        async with self.lock:
            record, runtime = self.nodes.get(uid), self.runtime.get(uid)
            entity = next((item for item in self.exposed(uid) if item["id"] == entity_id), None)
            if not record or not runtime or not self.available(uid) or not record["manifest"] or not entity:
                raise ProtocolError("entity unavailable")
            if record["node_type"] == "sed" and (not runtime.control_ready or self.clock() - runtime.last_seen > 10):
                raise ProtocolError("SED is not in a control-ready window")
            if not valid_value(entity, value, command=True):
                raise ProtocolError("unsupported command value")
            if len(self.commands) >= 64 or any(key[0] == uid for key in self.commands):
                raise ProtocolError("node command already pending")
            self.command_id = self.command_id % 0xFFFFFFFF + 1
            command_id = self.command_id
            future = asyncio.get_running_loop().create_future()
            pending = {"future": future, "entity": entity_id}
            self.commands[(uid, command_id)] = pending
            context = runtime.context
        try:
            for _ in range(3):
                async with self.lock:
                    if uid not in self.runtime or self.runtime[uid].context != context:
                        raise ProtocolError("node context changed")
                    self._reply(uid, "command", command_id=command_id, entity=entity_id,
                                operation="press" if entity["type"] == "button" else "set", value=value)
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), 1)
                    if not result["success"]:
                        raise ProtocolError(str(result.get("error", "node rejected command"))[:160])
                    return result
                except asyncio.TimeoutError:
                    continue
            raise ProtocolError("command timeout; physical execution is unconfirmed")
        finally:
            self.commands.pop((uid, command_id), None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Retrieve a failure even if HA cancelled the service task during unload.

    def _fail_commands(self, uid, reason):
        for (device, _), command in list(self.commands.items()):
            if device == uid and not command["future"].done():
                command["future"].set_exception(ProtocolError(reason))

    async def maintenance(self):
        async with self.lock:
            now = self.clock()
            self.candidates = {uid: item for uid, item in self.candidates.items() if now - item["seen"] <= 1800}
            self.offers = {uid: item for uid, item in self.offers.items() if now - item["seen"] <= 30}
            for uid, runtime in self.runtime.items():
                runtime.reports = OrderedDict((key, value) for key, value in runtime.reports.items() if now - value["at"] <= 120)
                if runtime.online and not self.available(uid):
                    runtime.online = False
                    await self.notify("availability", uid, None, None)

    async def close(self):
        async with self.lock:
            self.closed = True
            for uid in self.nodes:
                self._fail_commands(uid, "integration unloaded")
