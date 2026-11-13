"""Bounded TCP-envelope reception for BR-local acknowledged TSD2 groups.

This layer does not open a UDP socket or contact a node. The original Native
API protobuf records are retained so entity mapping can evolve independently.
"""
from __future__ import annotations
import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import re
import struct
from time import monotonic

MAX_BODY = 32768
CHUNK = 384

def hex_value(value, size):
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{size}}}", value):
        raise ValueError("invalid_hex")
    return value

def number(value, minimum, maximum):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("invalid_number")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError("out_of_range")
    return result

@dataclass(frozen=True)
class NodePacket:
    mac: str
    wake: str
    group: str
    schema: str
    kind: int
    flags: int
    payload: bytes

    @property
    def identity(self):
        return (self.mac, self.wake, self.group, self.schema, self.kind, self.flags)

    def frames(self):
        result = []
        offset = 0
        while offset < len(self.payload):
            if len(self.payload)-offset < 4:
                raise ValueError("truncated_record")
            kind, size = struct.unpack_from("!HH", self.payload, offset)
            offset += 4
            if not kind or offset+size > len(self.payload):
                raise ValueError("invalid_record")
            result.append((kind, self.payload[offset:offset+size]))
            offset += size
        return result

class UplinkReceiver:
    """One BR connection, one FIFO head, strict immutable envelope per token."""
    def __init__(self):
        self.pending = None

    def feed(self, data):
        delivery = data.get("delivery", "")
        if not re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{8}", delivery):
            raise ValueError("invalid_delivery")
        mac = hex_value(data.get("mac"), 12)
        wake = hex_value(data.get("wake"), 16)
        group = hex_value(data.get("group"), 8)
        schema = hex_value(data.get("schema"), 32)
        kind = number(data.get("kind"), 2, 7)
        if kind not in (2, 6, 7):
            raise ValueError("unsupported_kind")
        flags = number(data.get("flags"), 0, 3)
        part = number(data.get("part"), 0, 85)
        parts = number(data.get("parts"), 1, 86)
        encoded = data.get("payload", "")
        if not isinstance(encoded, str) or len(encoded) > 512:
            raise ValueError("invalid_base64")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as err:
            raise ValueError("invalid_base64") from err
        if part >= parts or len(chunk) > CHUNK or (part+1 < parts and len(chunk) != CHUNK) or part*CHUNK+len(chunk)>MAX_BODY:
            raise ValueError("invalid_fragment")
        identity = (delivery, mac, wake, group, schema, kind, flags, parts)
        now = monotonic()
        if self.pending is None or self.pending[0] != identity or now-self.pending[1] > 15:
            # A different FIFO token must start with fragment zero.
            if part:
                raise ValueError("missing_first_fragment")
            self.pending = (identity, now, {})
        chunks = self.pending[2]
        if part in chunks and chunks[part] != chunk:
            raise ValueError("conflicting_fragment")
        chunks[part] = chunk
        if len(chunks) != parts:
            return None
        payload = b"".join(chunks[i] for i in range(parts))
        self.pending = None
        packet = NodePacket(mac, wake, group, schema, kind, flags, payload)
        if kind == 2:
            packet.frames()
        elif len(payload) != (4 if kind == 6 else 0):
            raise ValueError("invalid_housekeeping")
        return delivery, packet

class PacketHub:
    """Global deduplication across BRs plus a bounded inspectable ingress cache.

    ACK means accepted in HA RAM, not committed to disk. Node ACK already
    happened on the BR; no HA processing is on the sleepy-node critical path.
    """
    def __init__(self, publish):
        self.publish = publish
        self.receipts = OrderedDict()
        self.nodes = OrderedDict()
        self.event_tokens = OrderedDict()
        self.received = self.duplicates = 0

    def accept(self, packet, br_id):
        digest = hashlib.sha256(packet.payload).digest()
        identity = packet.identity
        if identity in self.receipts:
            if self.receipts[identity] != digest:
                raise ValueError("conflicting_delivery")
            self.duplicates += 1
            return False
        frames = packet.frames() if packet.kind == 2 else []
        # Decode before committing a receipt. Malformed protobuf is never ACKed
        # as successfully handled by the HA ingress layer.
        from aioesphomeapi import api_pb2
        from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
        events = []
        descriptor_name = None
        done = False
        for kind, payload in frames:
            cls = MESSAGE_TYPE_TO_PROTO.get(kind)
            if cls is None:
                continue
            message = cls()
            try:
                message.ParseFromString(payload)
            except Exception as err:
                raise ValueError("invalid_protobuf") from err
            if kind == 10:
                address = message.mac_address.replace(":", "").replace("-", "").lower()
                if address != packet.mac:
                    raise ValueError("descriptor_identity_mismatch")
                descriptor_name = message.friendly_name or message.name or packet.mac
            if kind == 19:
                done = True
            if isinstance(message, api_pb2.HomeassistantActionRequest) and message.service in ("thread_sed.event", "thread_sed.batch_event"):
                values = {item.key: item.value for item in message.data}
                token = values.get("token", "")
                if len(token) > 256:
                    raise ValueError("invalid_event_token")
                pieces = token.split(":", 3)
                if len(pieces) != 4 or not pieces[1].isdecimal() or not pieces[2].isdecimal():
                    raise ValueError("invalid_event_token")
                events.append((token, pieces))
        if packet.flags & 2 and (not descriptor_name or not done):
            raise ValueError("incomplete_descriptors")
        node = self.nodes.setdefault(packet.mac, {"mac": packet.mac, "name": packet.mac, "packets": 0})
        self.nodes.move_to_end(packet.mac)
        node.update(last_br=br_id, schema=packet.schema, last_seen=monotonic(), packets=node["packets"]+1)
        if descriptor_name is not None:
            node.update(name=descriptor_name, descriptors=packet.payload)
        if packet.kind == 2:
            node["latest"] = packet.payload
        self.receipts[identity] = digest
        self.received += 1
        if len(self.receipts) > 2048:
            self.receipts.popitem(last=False)
        if len(self.nodes) > 64:
            self.nodes.popitem(last=False)
        # Raw Native API record batch, forwarded once regardless of BR path.
        self.publish("thread_proxy_packet", {"mac":packet.mac, "br_id":br_id, "schema":packet.schema,
                     "kind":packet.kind, "flags":packet.flags, "wake":packet.wake, "group":packet.group,
                     "payload":base64.b64encode(packet.payload).decode()})
        for token, (boot, sequence, key, event_type) in events:
            event_identity = (packet.mac, token)
            if event_identity in self.event_tokens:
                continue
            self.event_tokens[event_identity] = True
            if len(self.event_tokens) > 2048:
                self.event_tokens.popitem(last=False)
            self.publish("thread_proxy_node_event", {"mac":packet.mac,"key":int(key),"event_type":event_type,"token":token})

        return True

    def snapshot(self):
        return {"received":self.received, "duplicates":self.duplicates,
                "nodes":[{k:v for k,v in node.items() if k not in ("latest","descriptors")} for node in self.nodes.values()]}
