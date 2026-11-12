"""Plaintext native API and reverse socket tests using the real HA client."""
import asyncio
import types
import unittest
from aioesphomeapi import APIClient
from aioesphomeapi import api_pb2 as pb
from test_native import NativePeer, MAC, transport
import test_session as session_fixture

def varint(value):
    result=bytearray()
    while value>127:
        result.append((value&127)|128); value>>=7
    return bytes(result)+bytes([value])

async def read_varint(reader):
    value=0
    for shift in range(0,35,7):
        byte=(await reader.readexactly(1))[0]
        value|=(byte&127)<<shift
        if byte<128:return value
    raise ValueError('Oversized varint')

class PlainPeer(NativePeer):
    async def handle(self,reader,writer,outgoing=False):
        self.writer=writer
        try:
            if outgoing:
                writer.write(b'TSED1 '+MAC.encode()+b'\n');await writer.drain()
            async def send(ident,message):
                data=message.SerializeToString()
                writer.write(b'\0'+varint(len(data))+varint(ident)+data);await writer.drain()
            while True:
                if await reader.readexactly(1) != b'\0': return
                size=await read_varint(reader); ident=await read_varint(reader)
                data=await reader.readexactly(size)
                if ident==1: await send(2,pb.HelloResponse(api_version_major=1,api_version_minor=14,name='test-node'))
                elif ident==9: await send(10,pb.DeviceInfoResponse(name='test-node',mac_address=MAC,friendly_name='Plain Node',esphome_version='2026.10.2'))
                elif ident==11:
                    self.lists+=1
                    await send(16,pb.ListEntitiesSensorResponse(key=7,name='Temperature',object_id='temperature'))
                    await send(19,pb.ListEntitiesDoneResponse())
                elif ident==34:
                    msg=pb.HomeassistantActionRequest(service='thread_sed.session')
                    msg.data.add(key='schema',value='plain-v1');msg.data.add(key='sleep_seconds',value='30')
                    await send(35,msg)
                elif ident==20: await send(25,pb.SensorStateResponse(key=7,state=21.5))
                elif ident==40:
                    msg=pb.HomeAssistantStateResponse();msg.ParseFromString(data);self.controls.append(msg.state)
                elif ident==7: await send(8,pb.PingResponse())
                elif ident==5:
                    await send(6,pb.DisconnectResponse());return
        except (asyncio.IncompleteReadError,ConnectionError,asyncio.CancelledError):
            pass
        finally:writer.close()

class PlaintextTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_native_plaintext(self):
        node=PlainPeer();server=await asyncio.start_server(node.accept,'::1',0)
        client=APIClient('::1',server.sockets[0].getsockname()[1],noise_psk=None,client_info='Thread SED')
        try:
            await client.connect(login=False)
            self.assertEqual((await client.device_info()).mac_address,MAC)
            entities,_=await client.list_entities_services();self.assertEqual(entities[0].key,7)
        finally:
            await client.disconnect(force=True);server.close();await server.wait_closed();await node.close()

    def test_plain_identity_parser(self):
        raw=b'TSED1 '+MAC.encode()+b'\n'
        self.assertEqual(len(raw),24)
        for size in range(24):self.assertIsNone(transport.decode_plain_identity(raw[:size]))
        self.assertEqual(transport.decode_plain_identity(raw+b'\0\0\1'),'aabbccddeeff')
        with self.assertRaises(ValueError):transport.decode_plain_identity(raw[:-1]+b'!')

class PlainSessionTests(session_fixture.SessionTests):
    # Reuse the lifecycle tests against plaintext rather than a second fake API.
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.server.close();await self.server.wait_closed()
        self.node=PlainPeer()
        self.server=await asyncio.start_server(self.node.accept,'127.0.0.1',0)
        self.port=self.server.sockets[0].getsockname()[1]
        self.session.entry.data['port']=self.port
        self.session.entry.data.pop('noise_psk',None)

    async def test_initial_entities_and_states(self):
        await self.connect()
        self.assertTrue(self.session.online)
        self.assertEqual(self.session.states[(0,7)].state,21.5)
        self.assertTrue(self.session.store.value['schema'].startswith('plain-v1/'))

    async def test_configured_encryption_rejects_plain_reverse(self):
        listener=transport.ReverseListener({self.session.mac:self.session})
        self.session.entry.data['noise_psk']='configured-encrypted-device'
        await listener.start('::1',0)
        reader,writer=await asyncio.open_connection('::1',listener.socket.getsockname()[1])
        writer.write(b'TSED1 '+MAC.encode()+b'\n');await writer.drain()
        try:
            try:
                self.assertEqual(await asyncio.wait_for(reader.read(1),2),b'')
            except ConnectionError:
                pass  # A refused, unread preface may close with TCP RST.
            self.assertFalse(self.session.online)
        finally:
            writer.close()
            try: await writer.wait_closed()
            except ConnectionError: pass
            await listener.close()
