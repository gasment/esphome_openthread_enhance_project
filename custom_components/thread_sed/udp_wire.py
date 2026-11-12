"""Thread SED v2 datagrams: bounded fragmentation, optional AES-GCM, no stream handshake."""
from __future__ import annotations
from dataclasses import dataclass
import os
import struct
import time

HEADER = struct.Struct('!4sBB6s8sIIHH16s')
MAGIC = b'TSD2'
PROBE, DATA, ACK, SCHEMA, COMMAND, SLEEP, HEARTBEAT = range(1, 8)
ENCRYPTED, DESCRIPTORS = 1, 2
CHUNK, MAX_BODY, MAX_PARTS = 768, 32768, 43

@dataclass(frozen=True)
class Packet:
    kind: int
    flags: int
    mac: str
    boot: bytes
    sequence: int
    group: int
    part: int
    parts: int
    schema: bytes
    payload: bytes

def decode(data: bytes, key: bytes | None = None) -> Packet:
    if len(data) < HEADER.size or len(data) > HEADER.size + CHUNK + 16:
        raise ValueError('Invalid datagram size')
    magic, kind, flags, mac, boot, sequence, group, part, parts, schema = HEADER.unpack_from(data)
    if magic != MAGIC or kind not in range(1,8) or flags & ~(ENCRYPTED | DESCRIPTORS):
        raise ValueError('Invalid datagram header')
    if not 1 <= parts <= MAX_PARTS or part >= parts:
        raise ValueError('Invalid fragment index')
    if bool(flags & ENCRYPTED) != bool(key):
        raise ValueError('Encryption mode mismatch')
    payload = data[HEADER.size:]
    if key:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        payload = AESGCM(key).decrypt(boot + sequence.to_bytes(4,'big'), payload, data[:HEADER.size])
    if len(payload) > CHUNK:
        raise ValueError('Fragment too large')
    return Packet(kind,flags,mac.hex(),boot,sequence,group,part,parts,schema,payload)

class Encoder:
    def __init__(self, mac: str, key: bytes | None = None):
        self.mac = bytes.fromhex(mac.replace(':','').replace('-',''))
        if len(self.mac) != 6 or (key is not None and len(key) != 32):
            raise ValueError('Invalid identity or AES key')
        self.key, self.boot, self.sequence = key, os.urandom(8), 0
        self.group = int.from_bytes(os.urandom(4),'big')

    def encode(self, kind, payload=b'', *, group=None, schema=bytes(16), flags=0):
        if len(payload) > MAX_BODY or len(schema) != 16:
            raise ValueError('Message too large or invalid schema')
        if group is None:
            self.group = (self.group + 1) & 0xffffffff
            group = self.group
        parts = max(1,(len(payload)+CHUNK-1)//CHUNK)
        packets=[]
        for part in range(parts):
            self.sequence = (self.sequence+1) & 0xffffffff
            if self.sequence == 0:
                self.boot = os.urandom(8)
            bits = flags | (ENCRYPTED if self.key else 0)
            header = HEADER.pack(MAGIC,kind,bits,self.mac,self.boot,self.sequence,group,part,parts,schema)
            chunk = payload[part*CHUNK:(part+1)*CHUNK]
            if self.key:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                chunk = AESGCM(self.key).encrypt(self.boot+self.sequence.to_bytes(4,'big'),chunk,header)
            packets.append(header+chunk)
        return group, packets

class Reassembler:
    def __init__(self, limit=16, ttl=10):
        self.limit,self.ttl,self.pending = limit,ttl,{}

    def feed(self, packet: Packet, peer=None):
        now=time.monotonic()
        self.pending={k:v for k,v in self.pending.items() if now-v[0]<self.ttl}
        identity=(packet.mac,packet.boot,packet.group,packet.kind,peer)
        if packet.parts == 1:
            return packet.payload
        if identity not in self.pending:
            if len(self.pending)>=self.limit:
                raise ValueError('Reassembly capacity reached')
            self.pending[identity]=(now,packet.parts,packet.schema,packet.flags,{})
        _,parts,schema,flags,chunks=self.pending[identity]
        if (parts,schema,flags)!=(packet.parts,packet.schema,packet.flags):
            raise ValueError('Inconsistent fragments')
        if packet.part in chunks and chunks[packet.part]!=packet.payload:
            raise ValueError('Conflicting fragment')
        chunks[packet.part]=packet.payload
        if sum(map(len,chunks.values()))>MAX_BODY:
            self.pending.pop(identity,None)
            raise ValueError('Reassembled message too large')
        if len(chunks)!=parts:
            return None
        self.pending.pop(identity,None)
        return b''.join(chunks[i] for i in range(parts))

def encode_frames(messages):
    output=bytearray()
    for message_type,payload in messages:
        if not 0<message_type<65536 or len(payload)>65535:
            raise ValueError('Invalid protobuf record')
        output.extend(struct.pack('!HH',message_type,len(payload)))
        output.extend(payload)
    if len(output)>MAX_BODY:
        raise ValueError('Frame batch too large')
    return bytes(output)

def decode_frames(payload):
    frames=[]
    offset=0
    while offset<len(payload):
        if len(payload)-offset<4:
            raise ValueError('Truncated record header')
        message_type,size=struct.unpack_from('!HH',payload,offset)
        offset+=4
        if message_type==0 or offset+size>len(payload):
            raise ValueError('Truncated protobuf record')
        frames.append((message_type,payload[offset:offset+size]))
        offset+=size
    return frames
