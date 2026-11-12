"""Datagram protocol and HA session tests; no device firmware compilation."""
import asyncio
import base64
import importlib
from pathlib import Path
import socket
import struct
import types
import unittest
from test_session import MemoryStore
from aioesphomeapi import api_pb2, model
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO

wire=importlib.import_module('sed_fixture.udp_wire')
udp_session=importlib.import_module('sed_fixture.udp_session')
udp_listener=importlib.import_module('sed_fixture.udp_listener')
MAC='aabbccddeeff'
SCHEMA=bytes.fromhex('0123456789abcdef'*2)
TYPES={v:k for k,v in MESSAGE_TYPE_TO_PROTO.items()}

def body(*messages):
    return wire.encode_frames([(TYPES[type(m)],m.SerializeToString()) for m in messages])

def descriptors():
    return body(api_pb2.DeviceInfoResponse(name='test-sed',friendly_name='Test SED',mac_address='AA:BB:CC:DD:EE:FF'),
        api_pb2.HomeassistantActionRequest(service='thread_sed.session',data=[
            api_pb2.HomeassistantServiceMap(key='sleep',value='1'),
            api_pb2.HomeassistantServiceMap(key='sleep_seconds',value='1800')]),
        api_pb2.ListEntitiesSensorResponse(key=7,name='Temperature',object_id='temperature'),
        api_pb2.ListEntitiesEventResponse(key=8,name='Button',object_id='button',event_types=['single','hold']),
        api_pb2.ListEntitiesDoneResponse())

class WireTests(unittest.TestCase):
    def test_fragment_reorder_and_duplicate(self):
        encoder=wire.Encoder(MAC)
        _,packets=encoder.encode(wire.DATA,b'x'*2000,schema=SCHEMA)
        receiver=wire.Reassembler()
        self.assertIsNone(receiver.feed(wire.decode(packets[2])))
        self.assertIsNone(receiver.feed(wire.decode(packets[2])))
        self.assertIsNone(receiver.feed(wire.decode(packets[0])))
        self.assertEqual(receiver.feed(wire.decode(packets[1])),b'x'*2000)

    def test_encryption_and_tamper_rejection(self):
        key=bytes(range(32)); encoder=wire.Encoder(MAC,key)
        _,packets=encoder.encode(wire.DATA,b'private state',schema=SCHEMA)
        self.assertEqual(wire.decode(packets[0],key).payload,b'private state')
        self.assertNotIn(b'private state',packets[0])
        from cryptography.exceptions import InvalidTag
        broken=bytearray(packets[0]);broken[-1]^=1
        with self.assertRaises(InvalidTag):wire.decode(bytes(broken),key)
        with self.assertRaises(ValueError):wire.decode(packets[0])

    def test_limits_and_truncated_frames(self):
        with self.assertRaises(ValueError):wire.Encoder(MAC).encode(wire.DATA,b'x'*(wire.MAX_BODY+1))
        with self.assertRaises(ValueError):wire.decode_frames(b'\0\x19\0\x05x')
        with self.assertRaises(ValueError):wire.decode(b'TSD2')

    def test_new_encoder_uses_new_nonce_domain(self):
        self.assertNotEqual(wire.Encoder(MAC).boot,wire.Encoder(MAC).boot)

    def test_native_command_encoding_without_tcp(self):
        calls=[]; encoder=udp_session.CommandEncoder(calls.append)
        from aioesphomeapi import APIClient
        APIClient.switch_command(encoder,7,True)
        APIClient.light_command(encoder,8,brightness=.5,rgb=(1.,0.,0.))
        APIClient.cover_command(encoder,9,position=.5)
        self.assertIsInstance(calls[0],api_pb2.SwitchCommandRequest)
        self.assertTrue(calls[0].state)
        self.assertTrue(calls[1].has_rgb)
        self.assertEqual(calls[2].position,.5)

class UdpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        hass=types.SimpleNamespace(async_create_task=asyncio.create_task)
        entry=types.SimpleNamespace(unique_id=MAC,title='UDP fixture',data={'host':'::1','port':6064,'mac':MAC,'transport':'udp'})
        self.session=udp_session.UdpSession(hass,entry)
        self.listener=udp_listener.UdpListener({MAC:self.session})
        self.session.listener=self.listener
        await self.listener.start('::1',0)
        self.address=self.listener.transport.get_extra_info('sockname')
        self.node=socket.socket(socket.AF_INET6,socket.SOCK_DGRAM)
        self.node.bind(('::1',0));self.node.setblocking(False)
        self.encoder=wire.Encoder(MAC)

    async def asyncTearDown(self):
        await self.listener.close()
        await self.session.close()
        self.node.close()

    async def transmit(self,payload,flags=0,group=None):
        group,packets=self.encoder.encode(wire.DATA,payload,group=group,schema=SCHEMA,flags=flags)
        for packet in packets: await asyncio.get_running_loop().sock_sendto(self.node,packet,self.address)
        return group,packets

    async def receive(self,timeout=1):
        data,_=await asyncio.wait_for(asyncio.get_running_loop().sock_recvfrom(self.node,2048),timeout)
        return wire.decode(data,self.session.udp_key)

    async def establish(self):
        group,_=await self.transmit(descriptors(),wire.DESCRIPTORS)
        ack=await self.receive()
        self.assertEqual((ack.kind,ack.group),(wire.ACK,group))

    async def test_descriptor_state_and_no_handshake(self):
        await self.establish()
        self.assertEqual(self.session.info.name,'test-sed')
        self.assertEqual(len(self.session.entities),2)
        group,_=await self.transmit(body(api_pb2.SensorStateResponse(key=7,state=22.5)))
        ack=await self.receive()
        self.assertEqual(ack.group,group)
        self.assertEqual(self.session.states[(0,7)].state,22.5)

    async def test_unknown_schema_requests_then_replays_state(self):
        first,_=await self.transmit(body(api_pb2.SensorStateResponse(key=7,state=18.5)))
        request=await self.receive()
        self.assertEqual(request.kind,wire.SCHEMA)
        second,_=await self.transmit(descriptors(),wire.DESCRIPTORS)
        acks=[await self.receive(),await self.receive()]
        self.assertEqual({a.group for a in acks},{first,second})
        self.assertEqual(self.session.states[(0,7)].state,18.5)

    async def test_lost_ack_retransmit_does_not_repeat_event(self):
        await self.establish()
        changes=[];self.session.callbacks.add(changes.append)
        event=api_pb2.HomeassistantActionRequest(service='thread_sed.batch_event',data=[
            api_pb2.HomeassistantServiceMap(key='token',value='555:1:8:single')])
        group,packets=await self.transmit(body(event))
        await self.receive()
        for packet in packets: await asyncio.get_running_loop().sock_sendto(self.node,packet,self.address)
        ack=await self.receive()
        self.assertEqual(ack.group,group)
        self.assertEqual(changes.count((0,8)),1)

    async def test_mixed_auto_batch_retry_with_fresh_packet_sequence(self):
        await self.establish()
        changes=[]; self.session.callbacks.add(changes.append)
        payload=body(api_pb2.SensorStateResponse(key=7,state=24.0),
            api_pb2.HomeassistantActionRequest(service='thread_sed.batch_event',data=[
                api_pb2.HomeassistantServiceMap(key='token',value='777:1:8:single')]),
            api_pb2.HomeassistantActionRequest(service='thread_sed.batch',data=[
                api_pb2.HomeassistantServiceMap(key='token',value='auto:123:1')]))
        group,packets=await self.transmit(payload)
        first=await self.receive()  # Simulate losing this ACK at the node.
        self.assertEqual((first.kind,first.group,first.payload),(wire.ACK,group,self.encoder.boot))
        retry_group,retry=await self.transmit(payload,group=group)
        self.assertEqual(retry_group,group)
        self.assertNotEqual(packets,retry)  # Fresh sequence/nonces, frozen business body.
        ack=await self.receive()
        self.assertEqual((ack.kind,ack.group,ack.payload),(wire.ACK,group,self.encoder.boot))
        self.assertEqual(self.session.states[(0,7)].state,24.0)
        self.assertEqual(changes.count((0,7)),1)
        self.assertEqual(changes.count((0,8)),1)
        # The mixed batch gets a single group ACK, not a separate event/marker ACK.
        with self.assertRaises(asyncio.TimeoutError): await self.receive(.03)

    async def test_ack_does_not_wait_for_disk(self):
        blocked=asyncio.Event()
        async def slow_save(value): await blocked.wait()
        self.session.store.async_save=slow_save
        try:
            await self.establish()
            self.assertTrue(self.session._save_task and not self.session._save_task.done())
        finally:blocked.set()

    async def test_one_way_sleep_marks_unavailable_for_commands(self):
        await self.establish()
        _,packets=self.encoder.encode(wire.SLEEP,struct.pack('!I',1800))
        for packet in packets:await asyncio.get_running_loop().sock_sendto(self.node,packet,self.address)
        await asyncio.sleep(.02)
        self.assertFalse(self.session.online)
        self.assertTrue(self.session.available)
        with self.assertRaises(RuntimeError):self.session.command('switch',7,state=True)

    async def test_command_retry_until_ack(self):
        await self.establish()
        self.session.command('button',8)
        first=await self.receive()
        second=await self.receive()
        self.assertEqual(first.payload[:8],self.encoder.boot)
        self.assertEqual((first.kind,first.group),(wire.COMMAND,second.group))
        _,acks=self.encoder.encode(wire.ACK,first.boot,group=first.group)
        for ack in acks:await asyncio.get_running_loop().sock_sendto(self.node,ack,self.address)
        await asyncio.sleep(.02)
        self.assertFalse(self.listener.commands)

    async def test_fragmented_descriptor_group(self):
        messages=[api_pb2.DeviceInfoResponse(name='test-sed',mac_address='AA:BB:CC:DD:EE:FF')]
        messages.extend(api_pb2.ListEntitiesSensorResponse(key=i+100,name='Long sensor name '+str(i)*10,object_id='sensor_'+str(i)) for i in range(40))
        messages.append(api_pb2.ListEntitiesDoneResponse())
        group,packets=await self.transmit(body(*messages),wire.DESCRIPTORS)
        self.assertGreater(len(packets),1)
        ack=await self.receive()
        self.assertEqual(ack.group,group)
        self.assertEqual(len(self.session.entities),40)

    async def test_onboarding_probe_over_ipv6(self):
        key=self.session.udp_key
        # Use the existing local node socket as the onboarding target.
        port=self.node.getsockname()[1]
        async def respond():
            data,peer=await asyncio.get_running_loop().sock_recvfrom(self.node,2048)
            request=wire.decode(data,key)
            self.assertEqual(request.kind,wire.PROBE)
            group,packets=self.encoder.encode(wire.DATA,descriptors(),schema=SCHEMA,flags=wire.DESCRIPTORS)
            for packet in packets:await asyncio.get_running_loop().sock_sendto(self.node,packet,peer)
            ack_data,_=await asyncio.get_running_loop().sock_recvfrom(self.node,2048)
            ack=wire.decode(ack_data,key)
            self.assertEqual((ack.kind,ack.group,ack.payload),(wire.ACK,group,self.encoder.boot))
        task=asyncio.create_task(respond())
        info=await udp_listener.probe_device('::1',port,key)
        await task
        self.assertEqual(info.name,'test-sed')

    async def test_old_wake_cannot_overwrite_new_state_or_sleep(self):
        await self.establish()
        old=self.encoder
        self.encoder=wire.Encoder(MAC,self.session.udp_key)
        await self.transmit(body(api_pb2.SensorStateResponse(key=7,state=25.0)))
        await self.receive()
        _,old_packets=old.encode(wire.DATA,body(api_pb2.SensorStateResponse(key=7,state=10.0)),schema=SCHEMA)
        for packet in old_packets:await asyncio.get_running_loop().sock_sendto(self.node,packet,self.address)
        await self.receive()
        _,sleep=old.encode(wire.SLEEP,struct.pack('!I',1800))
        for packet in sleep:await asyncio.get_running_loop().sock_sendto(self.node,packet,self.address)
        await asyncio.sleep(.02)
        self.assertEqual(self.session.states[(0,7)].state,25.0)
        self.assertTrue(self.session.online)

class EncryptedUdpTests(UdpTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        key=bytes(range(32))
        self.session.udp_key=key
        self.encoder=wire.Encoder(MAC,key)
