"""IPv6 UDP endpoint, bounded reassembly, duplicate suppression and batch ACKs."""
from __future__ import annotations
import asyncio
import logging
import socket
import struct
from time import monotonic
from cryptography.exceptions import InvalidTag
from google.protobuf.message import DecodeError
from .udp_wire import Encoder, Reassembler, HEADER, PROBE, DATA, ACK, SCHEMA, COMMAND, decode

_LOGGER=logging.getLogger(__name__)
SLEEP,HEARTBEAT=6,7

class UdpListener(asyncio.DatagramProtocol):
    def __init__(self,sessions):
        self.sessions=sessions
        self.transport=None
        self.reassembly=Reassembler()
        self.encoders={}
        self.acknowledged={}
        self.inflight=set()
        self.deferred={}
        self.schema_requested={}
        self.tasks=set()
        self.commands={}

    async def start(self,host='::',port=6064):
        sock=socket.socket(socket.AF_INET6,socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
            sock.setblocking(False)
            sock.bind((host,port))
            await asyncio.get_running_loop().create_datagram_endpoint(lambda:self,sock=sock)
        except BaseException:
            sock.close()
            raise

    def connection_made(self,transport): self.transport=transport

    def encoder(self,session):
        previous=self.encoders.get(session.mac)
        if previous is None or previous.key!=session.udp_key:
            previous=self.encoders[session.mac]=Encoder(session.mac,session.udp_key)
        return previous

    def send(self,session,kind,peer,*,group=None,payload=b''):
        schema=bytes.fromhex(session.schema) if len(session.schema)==32 else bytes(16)
        group,packets=self.encoder(session).encode(kind,payload,group=group,schema=schema)
        for packet in packets: self.transport.sendto(packet,peer)
        return group

    def datagram_received(self,data,peer):
        if len(data)<HEADER.size or data[:4]!=b'TSD2': return
        session=self.sessions.get(data[6:12].hex())
        if session is None or not hasattr(session,'udp_key') or session.stopping: return
        try:
            packet=decode(data,session.udp_key)
            payload=self.reassembly.feed(packet,peer)
            if payload is None: return
            if packet.kind==ACK:
                pending=self.commands.get((session.mac,packet.group))
                if pending and peer==session.peer and payload==self.encoder(session).boot and not pending.done(): pending.set_result(True)
                return
            if packet.kind==HEARTBEAT:
                if session.info is not None and session.schema==packet.schema.hex():
                    if not session.note_wake(packet.boot): return
                    session.peer=peer
                    session.last_seen=monotonic()
                    session.online=True
                    session.changed()
                else:
                    self.send(session,SCHEMA,peer)
                return
            if packet.kind==SLEEP:
                if peer==session.peer and packet.boot==session.last_udp_boot and len(payload)==4:
                    session.sleeping(min(31536000,struct.unpack('!I',payload)[0]))
                return
            if packet.kind!=DATA: return
            identity=(packet.mac,packet.boot,packet.group,peer)
            now=monotonic()
            self.acknowledged={k:v for k,v in self.acknowledged.items() if now-v<60}
            _LOGGER.debug('Thread SED %s group=%s DATA received: bytes=%s parts=%s',session.mac,packet.group,len(payload),packet.parts)
            if identity in self.acknowledged:
                _LOGGER.debug('Thread SED %s group=%s duplicate DATA; replaying ACK',session.mac,packet.group)
                self.send(session,ACK,peer,group=packet.group,payload=packet.boot)
                return
            if identity in self.inflight or len(self.inflight)>=32: return
            self.inflight.add(identity)
            task=asyncio.create_task(self._deliver(session,packet,payload,peer,identity,received_at=now))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        except (ValueError,InvalidTag,DecodeError,UnicodeError):
            _LOGGER.debug('Discarded invalid Thread SED datagram',exc_info=True)

    async def _deliver(self,session,packet,payload,peer,identity,received_at=None):
        try:
            if session.stopping: return
            processing_started=monotonic()
            accepted=await session.receive(packet,payload,peer)
            if not accepted:
                if identity not in self.deferred and len(self.deferred)<16:
                    self.deferred[identity]=(session,packet,payload,peer,monotonic())
                if monotonic()-self.schema_requested.get(session.mac,0)>.25:
                    self.schema_requested[session.mac]=monotonic()
                    self.send(session,SCHEMA,peer)
                return
            self.acknowledged[identity]=monotonic()
            if len(self.acknowledged)>256: self.acknowledged.pop(next(iter(self.acknowledged)))
            self.send(session,ACK,peer,group=packet.group,payload=packet.boot)
            _LOGGER.debug('Thread SED %s group=%s ACK queued: scheduling=%.1f ms processing=%.1f ms',session.mac,packet.group,(processing_started-(received_at if received_at is not None else processing_started))*1000,(monotonic()-processing_started)*1000)
            self.deferred.pop(identity,None)
            if packet.flags&2:
                # Replay only fully reassembled groups after descriptors exist.
                waiting=list(self.deferred.items())
                for key,(node,message,body,address,stamp) in waiting:
                    if monotonic()-stamp>10:
                        self.deferred.pop(key,None)
                    elif node is session:
                        self.deferred.pop(key,None)
                        await self._deliver(node,message,body,address,key,received_at=stamp)
        except (ValueError,InvalidTag,DecodeError,KeyError,TypeError):
            _LOGGER.debug('Thread SED batch rejected',exc_info=True)
        finally:
            self.inflight.discard(identity)

    async def probe(self,session):
        try:
            addresses=await asyncio.get_running_loop().getaddrinfo(session.entry.data['host'],
                session.entry.data.get('port',6064),family=socket.AF_INET6,type=socket.SOCK_DGRAM)
            if addresses: self.send(session,PROBE,addresses[0][4])
        except OSError:
            _LOGGER.debug('Thread SED probe address is not reachable: %s',session.mac)

    def command(self,session,payload):
        task=asyncio.create_task(self._send_command(session,payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _send_command(self,session,payload):
        encoder=self.encoder(session)
        if session.last_udp_boot is None: return
        group,packets=encoder.encode(COMMAND,session.last_udp_boot+payload)
        if len(packets)!=1: raise ValueError('Command exceeds one datagram')
        ready=asyncio.get_running_loop().create_future()
        self.commands[(session.mac,group)]=ready
        try:
            for _ in range(4):
                for packet in packets: self.transport.sendto(packet,session.peer)
                try:
                    await asyncio.wait_for(asyncio.shield(ready),.2)
                    return
                except asyncio.TimeoutError: pass
            _LOGGER.warning('Thread SED command ACK timed out for %s',session.mac)
        finally:
            self.commands.pop((session.mac,group),None)

    async def close(self):
        if self.transport: self.transport.close()
        for task in tuple(self.tasks): task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.tasks.clear()

async def probe_device(host,port=6064,key=None,mac='000000000000'):
    """One onboarding exchange, independent of the integration listener."""
    from aioesphomeapi import api_pb2,model
    from .udp_session import protobuf_frames
    from .transport import normalize_mac
    _LOGGER.info('Thread SED onboarding: resolving [%s]:%s (target MAC %s)',host,port,mac)
    try:
        addresses=await asyncio.get_running_loop().getaddrinfo(host,port,family=socket.AF_INET6,type=socket.SOCK_DGRAM)
    except OSError as err:
        _LOGGER.warning('Thread SED onboarding address resolution failed for [%s]:%s: %s',host,port,err)
        raise
    if not addresses: raise OSError('No IPv6 address')
    peer=addresses[0][4]
    sock=socket.socket(socket.AF_INET6,socket.SOCK_DGRAM)
    sock.setblocking(False)
    encoder=Encoder(mac,key)
    assembler=Reassembler()
    loop=asyncio.get_running_loop()
    received=0
    unexpected_peer=0
    fragments=0
    incomplete=0
    try:
        _LOGGER.info('Thread SED onboarding: probing [%s]:%s via IPv6 UDP',peer[0],peer[1])
        for attempt in range(5):
            _,packets=encoder.encode(PROBE)
            for packet in packets: await loop.sock_sendto(sock,packet,peer)
            deadline=loop.time()+1
            while loop.time()<deadline:
                try:
                    data,address=await asyncio.wait_for(loop.sock_recvfrom(sock,2048),deadline-loop.time())
                except asyncio.TimeoutError: break
                received+=1
                if address[0]!=peer[0] or address[1]!=peer[1]:
                    unexpected_peer+=1
                    _LOGGER.debug('Thread SED onboarding ignored unexpected reply source %s (expected %s)',address,peer)
                    continue
                packet=decode(data,key)
                if packet.kind!=DATA or not packet.flags&2: continue
                fragments+=1
                body=assembler.feed(packet,address)
                if body is None: continue
                messages=protobuf_frames(body)
                info=next((m for m in messages if isinstance(m,api_pb2.DeviceInfoResponse)),None)
                if info is None or not any(isinstance(m,api_pb2.ListEntitiesDoneResponse) for m in messages):
                    incomplete+=1
                    _LOGGER.warning('Thread SED onboarding received incomplete descriptors from [%s]:%s: device_info=%s list_done=%s',
                        address[0],address[1],info is not None,any(isinstance(m,api_pb2.ListEntitiesDoneResponse) for m in messages))
                    continue
                identity=normalize_mac(info.mac_address)
                if identity!=packet.mac or (mac!='000000000000' and identity!=normalize_mac(mac)):
                    raise ValueError('Device identity mismatch')
                reply=Encoder(identity,key)
                _,acks=reply.encode(ACK,packet.boot,group=packet.group)
                for ack in acks: await loop.sock_sendto(sock,ack,address)
                _LOGGER.info('Thread SED onboarding accepted %s from [%s]:%s; descriptor group %s ACK sent',identity,address[0],address[1],packet.group)
                return model.DeviceInfo.from_pb(info)
        _LOGGER.warning('Thread SED onboarding timed out for [%s]:%s: received=%s descriptor_fragments=%s incomplete_groups=%s unexpected_source=%s. Keep the node awake and check the HA host IPv6 route to this OMR address.',
            peer[0],peer[1],received,fragments,incomplete,unexpected_peer)
        raise asyncio.TimeoutError('No complete Thread SED UDP descriptor response')
    except OSError as err:
        if not isinstance(err,asyncio.TimeoutError):
            _LOGGER.warning('Thread SED onboarding UDP socket error for [%s]:%s: %s',peer[0],peer[1],err)
        raise
    except (ValueError,InvalidTag,DecodeError) as err:
        _LOGGER.warning('Thread SED onboarding rejected reply from [%s]:%s: %s (%s)',peer[0],peer[1],err,type(err).__name__)
        raise
    finally:
        sock.close()
