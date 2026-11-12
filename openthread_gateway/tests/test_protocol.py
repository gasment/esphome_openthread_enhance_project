import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import unittest

from _load import protocol as p, hub as h

UID = "0123456789abcdef"
PEER = ("fd12::10", 19000, 0, 0)
ENTITIES = [
    {"id": "temperature", "type": "numeric", "name": "Temperature", "unit": "°C"},
    {"id": "door", "type": "binary", "name": "Door"},
    {"id": "relay", "type": "switch", "name": "Relay"},
    {"id": "key", "type": "event", "capabilities": {"event_types": ["click", "hold"]}},
]


class ProtocolTests(unittest.TestCase):
    def test_wire_hmac_exact_bytes_and_v2_rejection(self):
        secret = "ab" * 32
        message = {"v": 3, "type": "hello", "name": "门磁", "value": 0.1}
        packet = p.encode(message, secret)
        result, signature, raw = p.decode(packet)
        self.assertEqual(result, message)
        self.assertTrue(p.authenticated(signature, raw, secret))
        self.assertFalse(p.authenticated(signature, raw + b" ", secret))
        for bad in [b'{"v":2,"type":"hello"}', b'OTG3 -\n{"v":3,"v":3,"type":"hello"}', b'OTG3 -\n{"v":3,"type":"hello","x":NaN}', b'OTG3 -\n{"v":3,"type":"hello","x":1e999}', b'x' * 1233]:
            with self.assertRaises(p.ProtocolError): p.decode(bad)

    def test_descriptor_and_command_validation(self):
        number = {"id": "n", "type": "number", "capabilities": {"min": 0, "max": 10, "step": 0.5}}
        p.descriptor(number)
        self.assertTrue(p.valid_value(number, 2.5, True))
        self.assertFalse(p.valid_value(number, 2.6, True))
        self.assertFalse(p.valid_value(number, True, True))
        lock = {"id": "lock", "type": "lock", "capabilities": {"requires_code": True}}
        self.assertFalse(p.valid_value(lock, {"state": 1}, True))
        self.assertTrue(p.valid_value(lock, {"state": 1, "code": "1234"}, True))
        for bad in [{"id": "x", "type": "old_type"}, {"id": "x", "type": "event", "capabilities": {"event_types": []}}]:
            with self.assertRaises(p.ProtocolError): p.descriptor(bad)


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1000.0
        self.saved, self.sent, self.notices = [], [], []
        self.fail_save = False
        async def save(snapshot, urgent):
            if self.fail_save: raise OSError("simulated storage failure")
            self.saved.append((deepcopy(snapshot), urgent))
        async def notify(*args): self.notices.append(args)
        self.gateway = h.GatewayHub("ha_main", save, lambda packet, peer: self.sent.append((packet, peer)), notify, clock=lambda: self.now)
        self.hello = {"v": 3, "type": "hello", "device": UID, "node": "door", "name": "门磁", "network": "1234567890abcdef",
                      "node_type": "reed", "boot": 1, "generation": 0, "session": "11" * 8, "request_id": "22" * 8}

    def last(self): return p.decode(self.sent[-1][0])[0]

    async def admit(self):
        await self.gateway.receive(p.encode(self.hello), PEER)
        self.assertIn(UID, self.gateway.candidates)
        self.assertNotIn(UID, self.gateway.nodes)
        await self.gateway.admit(UID)
        self.record = self.gateway.nodes[UID]
        self.secret = self.record["secret"]
        self.assertEqual(self.record["status"], "pending")
        ack = {"v": 3, "type": "bind_ack", "device": UID, "backend_id": "ha_main", "binding_id": self.record["binding_id"],
               "session": self.hello["session"], "request_id": self.hello["request_id"]}
        await self.gateway.receive(p.encode(ack, self.secret), PEER)
        self.assertEqual(self.last()["type"], "bind_complete")
        return ack

    async def connect(self, peer=PEER, boot=1):
        if UID not in self.gateway.nodes:
            await self.admit()
        self.record = self.gateway.nodes[UID]
        self.secret = self.record["secret"]
        hello = {**self.hello, "backend_id": "ha_main", "binding_id": self.record["binding_id"], "generation": self.record["generation"], "boot": boot}
        await self.gateway.receive(p.encode(hello, self.secret), peer)
        offer = self.last()
        self.context = offer["ctx"]
        ack = {"v": 3, "type": "session_ack", "device": UID, "backend_id": "ha_main", "binding_id": self.record["binding_id"],
               "session": hello["session"], "ctx": self.context, "request_id": hello["request_id"]}
        await self.gateway.receive(p.encode(ack, self.secret), peer)
        self.assertEqual(self.last()["type"], "session_ready")
        return ack

    async def packet(self, kind, peer=PEER, **fields):
        record, runtime = self.gateway.nodes[UID], self.gateway.runtime[UID]
        message = {"v": 3, "type": kind, "device": UID, "backend_id": "ha_main", "binding_id": record["binding_id"],
                   "session": runtime.session, "ctx": runtime.context, **fields}
        await self.gateway.receive(p.encode(message, self.secret), peer)

    async def manifest(self, entities=ENTITIES, parts=1, order=None):
        raw = json.dumps({"name": "门磁", "entities": entities}, ensure_ascii=False, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        length = (len(raw) + parts - 1) // parts
        chunks = [raw[index * length:(index + 1) * length] for index in range(parts)]
        for part in (order if order is not None else range(parts)):
            await self.packet("manifest", manifest_hash=digest, part=part, parts=parts, payload=base64.b64encode(chunks[part]).decode())
        return digest

    async def state(self, seq, values, **extra):
        await self.packet("state", manifest_hash=self.gateway.nodes[UID]["manifest_hash"], seq=seq, report_id=seq, part=0, parts=1, values=values, **extra)

    async def test_manual_admission_and_duplicate_handshakes(self):
        ack = await self.admit()
        await self.gateway.receive(p.encode(ack, self.secret), PEER)
        self.assertEqual(self.last()["type"], "bind_complete")
        ack = await self.connect()
        context = self.gateway.runtime[UID].context
        await self.gateway.receive(p.encode(ack, self.secret), PEER)
        self.assertEqual(self.last()["type"], "session_ready")
        self.assertEqual(self.gateway.runtime[UID].context, context)

    async def test_admission_survives_sleep_and_restart(self):
        await self.gateway.receive(p.encode(self.hello), PEER)
        await self.gateway.admit(UID)
        snapshot = self.gateway.snapshot()
        self.gateway.nodes = {}
        self.gateway.candidates = {}
        self.gateway.restore(snapshot)
        await self.gateway.receive(p.encode(self.hello), PEER)
        self.assertEqual(self.last()["type"], "bind_offer")
        self.assertEqual(self.last()["secret"], snapshot["nodes"][UID]["secret"])

    async def test_storage_failure_rolls_back_admission(self):
        await self.gateway.receive(p.encode(self.hello), PEER)
        self.fail_save = True
        with self.assertRaises(OSError): await self.gateway.admit(UID)
        self.assertNotIn(UID, self.gateway.nodes)
        self.assertEqual(self.sent, [])

    async def test_out_of_order_manifest_atomic_commit_and_removal(self):
        await self.connect()
        raw = json.dumps({"name": "Door", "entities": ENTITIES}).encode()
        digest = hashlib.sha256(raw).hexdigest()
        await self.packet("manifest", manifest_hash=digest, part=1, parts=2, payload=base64.b64encode(raw[100:]).decode())
        self.assertEqual(self.gateway.exposed(UID), [])
        await self.packet("manifest", manifest_hash=digest, part=0, parts=2, payload=base64.b64encode(raw[:100]).decode())
        self.assertEqual(len(self.gateway.exposed(UID)), 4)
        self.assertTrue(self.last()["complete"])
        await self.manifest(ENTITIES[:1])
        self.assertEqual([item["id"] for item in self.gateway.exposed(UID)], ["temperature"])
        await self.manifest([])
        self.assertEqual(self.gateway.exposed(UID), [])

    async def test_bad_manifest_does_not_delete_entities(self):
        await self.connect()
        await self.manifest()
        before = deepcopy(self.gateway.nodes[UID]["manifest"])
        await self.packet("manifest", manifest_hash="00" * 32, part=0, parts=1, payload=base64.b64encode(b'{}').decode())
        self.assertEqual(self.gateway.nodes[UID]["manifest"], before)

    async def test_entity_selection_and_future_additions(self):
        await self.connect()
        await self.manifest()
        await self.gateway.select_entities(UID, ["door"])
        self.assertEqual([item["id"] for item in self.gateway.exposed(UID)], ["door"])
        await self.manifest([*ENTITIES, {"id": "vdd", "type": "numeric"}])
        self.assertEqual(len(self.gateway.exposed(UID)), 1)
        await self.gateway.select_entities(UID, None)
        self.assertEqual(len(self.gateway.exposed(UID)), 5)

    async def test_report_fragments_ack_and_duplicate_events(self):
        await self.connect()
        await self.manifest()
        self.sent.clear()
        await self.packet("state", manifest_hash=self.gateway.nodes[UID]["manifest_hash"], seq=1, report_id=1, part=1, parts=2, values={"key": "click"})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.gateway.nodes[UID]["values"], {})
        await self.packet("state", manifest_hash=self.gateway.nodes[UID]["manifest_hash"], seq=1, report_id=1, part=0, parts=2, values={"temperature": 22.5})
        self.assertEqual(self.last()["type"], "report_ack")
        await self.packet("state", manifest_hash=self.gateway.nodes[UID]["manifest_hash"], seq=1, report_id=1, part=0, parts=2, values={"temperature": 22.5})
        self.assertEqual(len([notice for notice in self.notices if notice[0] == "event"]), 1)

    async def test_stale_state_and_snapshot_events(self):
        await self.connect()
        await self.manifest()
        await self.state(10, {"temperature": 25})
        await self.state(9, {"temperature": 10, "key": "click"})
        self.assertEqual(self.gateway.nodes[UID]["values"]["temperature"], 25)
        await self.state(11, {"key": "hold"}, snapshot=True)
        self.assertEqual(len([notice for notice in self.notices if notice[0] == "event"]), 1)

    async def test_invalid_values_never_receive_success_ack(self):
        await self.connect()
        await self.manifest()
        self.sent.clear()
        await self.state(1, {"door": "true"})
        self.assertFalse(self.sent)
        self.assertNotIn("door", self.gateway.nodes[UID]["values"])

    async def test_auth_context_and_route_validation(self):
        await self.connect()
        await self.manifest()
        before = deepcopy(self.gateway.nodes[UID]["values"])
        message = {"v": 3, "type": "presence", "device": UID, "binding_id": self.record["binding_id"], "backend_id": "ha_main", "ctx": self.context, "session": self.hello["session"]}
        await self.gateway.receive(p.encode(message, "00" * 32), PEER)
        self.assertEqual(self.gateway.nodes[UID]["values"], before)
        self.assertGreater(self.gateway.rejected, 0)
        await self.packet("presence", peer=("fd12::11", 19000, 0, 0))
        self.assertEqual(self.last()["type"], "context_expired")
        self.assertEqual(self.gateway.runtime[UID].peer, PEER)

    async def test_route_change_requires_authenticated_ack(self):
        await self.connect()
        old = self.gateway.runtime[UID].peer
        new = ("fd34::20", 19100, 0, 0)
        hello = {**self.hello, "backend_id": "ha_main", "binding_id": self.record["binding_id"], "generation": self.record["generation"]}
        await self.gateway.receive(p.encode(hello, self.secret), new)
        self.assertEqual(self.gateway.runtime[UID].peer, old)
        await self.connect(new)
        self.assertEqual(self.gateway.runtime[UID].peer, new)

    async def test_revoke_offline_node_and_restore_tombstone(self):
        await self.connect()
        token, secret = self.record["binding_id"], self.secret
        await self.gateway.remove(UID)
        self.assertNotIn(UID, self.gateway.nodes)
        self.gateway.restore(self.gateway.snapshot())
        hello = {**self.hello, "backend_id": "ha_main", "binding_id": token, "generation": 1}
        await self.gateway.receive(p.encode(hello, secret), PEER)
        self.assertEqual(self.last()["type"], "binding_revoked")
        self.assertTrue(p.authenticated(*p.decode(self.sent[-1][0])[1:], secret))
        await self.gateway.receive(p.encode({**self.hello, "generation": 2}), PEER)
        await self.gateway.admit(UID)
        self.assertEqual(self.gateway.nodes[UID]["generation"], 3)

    async def test_sed_sleep_and_reed_availability(self):
        self.hello["node_type"] = "sed"
        await self.connect()
        await self.manifest()
        await self.packet("presence", manifest_hash=self.record["manifest_hash"], sleeping=True, sleep_for_s=1800)
        self.now += 1700
        await self.gateway.maintenance()
        self.assertTrue(self.gateway.available(UID))
        with self.assertRaises(p.ProtocolError): await self.gateway.command(UID, "relay", True)
        self.now += 300
        await self.gateway.maintenance()
        self.assertFalse(self.gateway.available(UID))

    async def test_command_result_and_no_optimistic_state(self):
        await self.connect()
        await self.manifest()
        await self.state(1, {"relay": False})
        task = asyncio.create_task(self.gateway.command(UID, "relay", True))
        await asyncio.sleep(0)
        message = self.last()
        self.assertEqual(message["type"], "command")
        self.assertFalse(self.gateway.nodes[UID]["values"]["relay"])
        await self.packet("command_result", command_id=message["command_id"], success=True, value=True, state_seq=2)
        self.assertTrue((await task)["success"])
        self.assertTrue(self.gateway.nodes[UID]["values"]["relay"])

    async def test_command_is_cancelled_on_context_change(self):
        await self.connect()
        await self.manifest()
        task = asyncio.create_task(self.gateway.command(UID, "relay", True))
        await asyncio.sleep(0)
        await self.connect()
        with self.assertRaises(p.ProtocolError): await task

    async def test_reboot_does_not_reuse_context(self):
        await self.connect()
        old = self.context
        self.gateway.restore(self.gateway.snapshot())
        self.gateway.runtime.clear()
        await self.connect(boot=2)
        self.assertNotEqual(self.context, old)
        hello = {**self.hello, "backend_id": "ha_main", "binding_id": self.record["binding_id"], "generation": self.record["generation"], "boot": 1}
        before = len(self.sent)
        await self.gateway.receive(p.encode(hello, self.secret), PEER)
        self.assertEqual(len(self.sent), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
