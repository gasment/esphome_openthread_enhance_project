"""Transport tests against the real aioesphomeapi client and Noise protocol."""
import asyncio
import base64
import importlib.util
from pathlib import Path
import sys
import unittest
from aioesphomeapi import APIClient, APIConnectionError
from aioesphomeapi import api_pb2 as pb
from noise.connection import NoiseConnection
from cryptography.exceptions import InvalidTag

ROOT = Path(__file__).resolve().parents[2] if (Path(__file__).resolve().parent.name == 'tests') else Path(__file__).resolve().parent
TRANSPORT = ROOT/'thread_sed'/'legacy_component'/'transport.py'
if not TRANSPORT.exists(): TRANSPORT = Path(__file__).resolve().parent.parent/'transport.py'
spec = importlib.util.spec_from_file_location('sed_transport',TRANSPORT)
transport=importlib.util.module_from_spec(spec); spec.loader.exec_module(transport)
KEY=bytes(range(1,33)); PSK=base64.b64encode(KEY).decode(); MAC='aa:bb:cc:dd:ee:ff'

async def frame(reader):
    header=await reader.readexactly(3)
    assert header[0]==1
    return await reader.readexactly(int.from_bytes(header[1:],'big'))

def packet(data): return b'\x01'+len(data).to_bytes(2,'big')+data

class NativePeer:
    def __init__(self):
        self.tasks=set(); self.writer=None; self.controls=[]; self.lists=0
    async def handle(self, reader, writer, outgoing=False):
        self.writer=writer
        noise=NoiseConnection.from_name(b'Noise_NNpsk0_25519_ChaChaPoly_SHA256')
        noise.set_as_responder(); noise.set_psks(KEY); noise.set_prologue(b'NoiseAPIInit\x00\x00'); noise.start_handshake()
        hello=packet(b'\x01test-node\0'+MAC.encode()+b'\0')
        try:
            if outgoing: writer.write(hello); await writer.drain()
            assert await frame(reader)==b''
            if not outgoing: writer.write(hello); await writer.drain()
            request=await frame(reader); assert request[0]==0
            noise.read_message(request[1:]); writer.write(packet(b'\x00'+noise.write_message())); await writer.drain()
            async def send(ident,msg):
                data=msg.SerializeToString()
                raw=ident.to_bytes(2,'big')+len(data).to_bytes(2,'big')+data
                writer.write(packet(noise.encrypt(raw))); await writer.drain()
            self.send = send
            while True:
                raw=noise.decrypt(await frame(reader)); ident=int.from_bytes(raw[:2],'big')
                if ident==1: await send(2,pb.HelloResponse(api_version_major=1,api_version_minor=14,name='test-node'))
                elif ident==9: await send(10,pb.DeviceInfoResponse(name='test-node',mac_address=MAC,friendly_name='Test Node',esphome_version='2026.10.2'))
                elif ident==11:
                    self.lists+=1
                    await send(16,pb.ListEntitiesSensorResponse(key=7,name='Temperature',object_id='temperature',unit_of_measurement='°C'))
                    await send(19,pb.ListEntitiesDoneResponse())
                elif ident==34:
                    msg=pb.HomeassistantActionRequest(service='thread_sed.session')
                    msg.data.add(key='schema',value='fixture-v1'); msg.data.add(key='sleep_seconds',value='30')
                    await send(35,msg)
                elif ident==38:
                    await send(39,pb.SubscribeHomeAssistantStateResponse(entity_id='thread_sed.control'))
                elif ident==20: await send(25,pb.SensorStateResponse(key=7,state=21.5))
                elif ident==40:
                    msg=pb.HomeAssistantStateResponse(); msg.ParseFromString(raw[4:]); self.controls.append(msg.state)
                elif ident==7: await send(8,pb.PingResponse())
                elif ident==5:
                    await send(6,pb.DisconnectResponse()); return
        except (asyncio.IncompleteReadError,ConnectionError,asyncio.CancelledError,InvalidTag):
            pass
        finally:
            writer.close()
    def accept(self, reader, writer):
        task=asyncio.create_task(self.handle(reader,writer)); self.tasks.add(task); task.add_done_callback(self.tasks.discard)
    async def close(self):
        if self.writer: self.writer.close()
        for task in tuple(self.tasks): task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_noise_over_ipv6(self):
        node=NativePeer()
        try:
            server=await asyncio.start_server(node.accept,'::1',0)
        except OSError:
            self.skipTest('IPv6 loopback unavailable')
        client=APIClient('::1',server.sockets[0].getsockname()[1],noise_psk=PSK,expected_mac=MAC,client_info='Thread SED')
        try:
            await client.connect(login=False)
            self.assertEqual((await client.device_info()).mac_address,MAC)
        finally:
            await client.disconnect(force=True); server.close(); await server.wait_closed(); await node.close()
    async def test_native_noise_and_protobuf(self):
        node=NativePeer(); server=await asyncio.start_server(node.accept,'127.0.0.1',0)
        client=APIClient('127.0.0.1',server.sockets[0].getsockname()[1],noise_psk=PSK,expected_mac=MAC,client_info='Thread SED')
        try:
            await client.connect(login=False)
            self.assertEqual((await client.device_info()).mac_address,MAC)
            entities,_=await client.list_entities_services(); self.assertEqual(entities[0].key,7)
            got=asyncio.get_running_loop().create_future()
            client.subscribe_states(lambda value:got.set_result(value))
            self.assertEqual((await asyncio.wait_for(got,2)).state,21.5)
        finally:
            await client.disconnect(force=True); server.close(); await server.wait_closed(); await node.close()

    async def test_reverse_socket_preserves_real_noise(self):
        node=NativePeer(); result=asyncio.get_running_loop().create_future()
        client=APIClient('::1',6064,noise_psk=PSK,expected_mac=MAC,client_info='Thread SED')
        class Session:
            entry=__import__('types').SimpleNamespace(data={'noise_psk':PSK})
            connecting=False
            online=False
            async def connect(self,host,port,sock=None):
                client.start_connection_from_socket(sock,log_errors=False)
                await client.finish_connection(login=False)
                result.set_result(True)
        listener=transport.ReverseListener({'aabbccddeeff':Session()})
        await listener.start('::1',0)
        reader,writer=await asyncio.open_connection('::1',listener.socket.getsockname()[1])
        task=asyncio.create_task(node.handle(reader,writer,outgoing=True))
        try:
            await asyncio.wait_for(result,3)
            self.assertEqual((await client.device_info()).name,'test-node')
            got=asyncio.get_running_loop().create_future()
            client.subscribe_service_calls(lambda call:got.set_result(call))
            self.assertEqual((await asyncio.wait_for(got,2)).data['schema'],'fixture-v1')
        finally:
            await client.disconnect(force=True)
            await listener.close()
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
            await node.close()

    async def test_wrong_key_rejected(self):
        node=NativePeer(); server=await asyncio.start_server(node.accept,'127.0.0.1',0)
        client=APIClient('127.0.0.1',server.sockets[0].getsockname()[1],noise_psk=base64.b64encode(b'x'*32).decode())
        try:
            with self.assertRaises(APIConnectionError):
                await asyncio.wait_for(client.connect(login=False,log_errors=False),3)
        finally:
            await client.disconnect(force=True); server.close(); await server.wait_closed(); await node.close()

    async def test_invalid_hello_rejected(self):
        with self.assertRaises(ValueError): transport.decode_server_hello(b'\x01\xff\xff')

    def test_mac_normalization(self):
        self.assertEqual(transport.normalize_mac(MAC),'aabbccddeeff')
        with self.assertRaises(ValueError): transport.normalize_mac('../bad')

if __name__=='__main__': unittest.main()
