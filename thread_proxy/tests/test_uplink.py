"""Ingress protocol boundary tests; real protobuf, no live BR or compilation."""
import asyncio
import base64
import importlib
import struct
import types
import unittest
from unittest.mock import Mock
import test_proxy as fixture
from aioesphomeapi import api_pb2 as pb

uplink=importlib.import_module('proxy_fixture.uplink')

MAC='001122334455'
def records(*items):
    return b''.join(struct.pack('!HH',kind,len(data))+data for kind,data in items)
def envelope(body=b'',**overrides):
    return dict(delivery='1234567890abcdef-00000001',mac=MAC,wake='11'*8,group='00000001',schema='22'*16,
                kind='2',flags='0',part='0',parts='1',payload=base64.b64encode(body).decode(),**overrides)
def chunks(body):
    count=max(1,(len(body)+383)//384)
    return [{**envelope(), 'part':str(i),'parts':str(count),'payload':base64.b64encode(body[i*384:(i+1)*384]).decode()} for i in range(count)]

class UplinkTests(unittest.TestCase):
    def test_fragmented_descriptor_body_and_zero_length_type19(self):
        body=records((10,b'x'*800),(19,b''))
        receiver=uplink.UplinkReceiver()
        result=None
        for item in chunks(body): result=receiver.feed(item)
        self.assertEqual(result[1].payload,body)
        self.assertEqual(result[1].frames()[-1],(19,b''))

    def test_full_size_and_oversize(self):
        body=records((35,b'x'*32764))
        receiver=uplink.UplinkReceiver()
        for item in chunks(body): result=receiver.feed(item)
        self.assertEqual(len(result[1].payload),32768)
        items=chunks(body+b'x')
        receiver=uplink.UplinkReceiver()
        with self.assertRaises(ValueError):
            for item in items: receiver.feed(item)

    def test_fragment_conflict_and_missing_zero(self):
        data=chunks(records((10,b'x'*500)))
        receiver=uplink.UplinkReceiver()
        with self.assertRaises(ValueError): receiver.feed(data[1])
        self.assertIsNone(receiver.feed(data[0]))
        with self.assertRaises(ValueError): receiver.feed({**data[0],'payload':base64.b64encode(b'y'*384).decode()})

    def test_invalid_envelopes_do_not_complete(self):
        for changes in ({'mac':'bad'},{'payload':'!!!!'},{'part':'-1'},{'parts':'999'},{'kind':'5'},{'delivery':'bad'},{'flags':'4'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): uplink.UplinkReceiver().feed({**envelope(),**changes})
        for body in (b'x',b'\x00\x00\x00\x00',b'\x00\x01\x00\x05x'):
            with self.assertRaises(ValueError): uplink.UplinkReceiver().feed(envelope(body))

    def test_housekeeping_body_size(self):
        self.assertEqual(uplink.UplinkReceiver().feed({**envelope(b'1234'),'kind':'6'})[1].kind,6)
        with self.assertRaises(ValueError): uplink.UplinkReceiver().feed({**envelope(b'1234'),'kind':'7'})

    def test_global_dedup_conflicting_body_and_event_token(self):
        calls=[]; hub=uplink.PacketHub(lambda *args:calls.append(args))
        msg=pb.HomeassistantActionRequest(service='thread_sed.batch_event')
        msg.data.add(key='token',value='boot:1:42:press')
        body=records((35,msg.SerializeToString()))
        packet=uplink.UplinkReceiver().feed(envelope(body))[1]
        hub.accept(packet,'br-a');hub.accept(packet,'br-b')
        self.assertEqual(hub.duplicates,1)
        self.assertEqual([name for name,_ in calls],['thread_proxy_packet','thread_proxy_node_event'])
        next_packet=uplink.UplinkReceiver().feed({**envelope(body),'group':'00000002'})[1]
        hub.accept(next_packet,'br-b')
        self.assertEqual(sum(name=='thread_proxy_node_event' for name,_ in calls),1)
        conflicting=dataclasses_replace(packet,payload=records((19,b'')))
        with self.assertRaises(ValueError): hub.accept(conflicting,'br-c')

    def test_descriptor_identity_and_cache(self):
        hub=uplink.PacketHub(lambda *args:None)
        info=pb.DeviceInfoResponse(name='door',mac_address='00:11:22:33:44:55')
        body=records((10,info.SerializeToString()),(19,b''))
        packet=uplink.UplinkReceiver().feed({**envelope(body),'flags':'2'})[1]
        hub.accept(packet,'br-a')
        self.assertEqual(hub.snapshot()['nodes'][0]['name'],'door')
        with self.assertRaises(ValueError):hub.accept(dataclasses_replace(packet,mac='aabbccddeeff'),'br-a')
        with self.assertRaises(ValueError):hub.accept(dataclasses_replace(packet,group='00000002',payload=records((19,b''))),'br-a')

    def test_invalid_protobuf_is_not_receipted(self):
        hub=uplink.PacketHub(lambda *args:None)
        packet=uplink.UplinkReceiver().feed(envelope(records((10,b'\xff'))))[1]
        with self.assertRaises(ValueError):hub.accept(packet,'br')
        self.assertFalse(hub.receipts)

    def test_connection_optin_ack_only_after_acceptance(self):
        state=fixture.models.BRState('id','BR','::1',6065,proxy_id=fixture.ID)
        connection=fixture.connection.ProxyConnection(state,'',lambda:None,lambda x:True)
        connection.client=Mock()
        accepted=[]
        connection.packet_handler=lambda packet,br:accepted.append(packet)
        metadata=dict(component='thread_proxy_api',version='1',id=fixture.ID,uplink_protocol='tsd2-v1',data_plane_ready='true')
        connection.receive(types.SimpleNamespace(service='thread_proxy.info',data=metadata))
        connection.client.send_home_assistant_state.assert_called_once_with('thread_proxy.uplink',None,'tsd2-v1')
        connection.receive(types.SimpleNamespace(service='thread_proxy.packet',data=envelope(records((19,b'')))))
        self.assertEqual(len(accepted),1)
        connection.client.send_home_assistant_state.assert_called_with('thread_proxy.delivery_ack',None,'1234567890abcdef-00000001')
        connection.client.reset_mock()
        connection.packet_handler=Mock(side_effect=ValueError('reject'))
        connection.receive(types.SimpleNamespace(service='thread_proxy.packet',data=envelope(records((19,b'')))))
        connection.client.send_home_assistant_state.assert_called_once_with("thread_proxy.delivery_reject",None,"1234567890abcdef-00000001")

from dataclasses import replace as dataclasses_replace


class NativeUplinkTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, encrypted):
        event=pb.HomeassistantActionRequest(service='thread_sed.batch_event')
        event.data.add(key='token',value='live:1:42:press')
        body=records((999,b'x'*900),(35,event.SerializeToString()),(19,b''))
        first=chunks(body)
        second=[{**item,'delivery':'1234567890abcdef-00000002'} for item in first]
        peer=fixture.Peer(encrypted=encrypted,uplink_frames=first+second)
        server=await asyncio.start_server(peer.accept,'127.0.0.1',0)
        state=fixture.models.BRState('id','BR','127.0.0.1',server.sockets[0].getsockname()[1],proxy_id=fixture.ID)
        connection=fixture.connection.ProxyConnection(state,fixture.PSK if encrypted else '',lambda:None,lambda identity:True)
        published=[];hub=uplink.PacketHub(lambda *args:published.append(args))
        connection.packet_handler=hub.accept
        task=asyncio.create_task(connection.run())
        try:
            async with asyncio.timeout(5):
                while len(peer.acks)<2:
                    await peer.acked.wait();peer.acked.clear()
            self.assertTrue(state.data_plane_ready)
            self.assertEqual(peer.acks,[('thread_proxy.delivery_ack','1234567890abcdef-00000001'),('thread_proxy.delivery_ack','1234567890abcdef-00000002')])
            self.assertEqual(hub.received,1);self.assertEqual(hub.duplicates,1)
            self.assertEqual([name for name,_ in published],['thread_proxy_packet','thread_proxy_node_event'])
        finally:
            await connection.close();task.cancel();await asyncio.gather(task,return_exceptions=True)
            server.close();await server.wait_closed();await peer.close()

    async def test_fragmented_native_plaintext_and_delivery_retry(self):
        await self.exercise(False)

    async def test_fragmented_native_noise_and_delivery_retry(self):
        await self.exercise(True)
