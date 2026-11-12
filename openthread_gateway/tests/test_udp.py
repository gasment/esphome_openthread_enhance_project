"""Exercise real IPv6 UDP loopback packets, not a firmware or radio test."""
import asyncio
import unittest
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
from _load import protocol as p, hub as h


class IPv6Tests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_node_binding_manifest_state_control_and_revocation(self):
        module_path=Path(__file__).resolve().parents[1]/'tools/simulate_node.py'
        spec=importlib.util.spec_from_file_location('otgw_demo_test',module_path)
        demo_module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo_module)
        loop=asyncio.get_running_loop()
        discovered=asyncio.Event();reported=asyncio.Event();revoked=asyncio.Event()
        async def save(*_):pass
        async def notify(kind,*_):
            if kind=='state':reported.set()
        gateway=h.GatewayHub('ha_main',save,lambda data,peer:server.sendto(data,peer),notify)
        tasks=set()
        class Server(asyncio.DatagramProtocol):
            def datagram_received(self,data,peer):
                async def process():
                    await gateway.receive(data,peer)
                    discovered.set()
                    if p.decode(data)[0]['type']=='hello' and not p.decode(data)[0].get('binding_id') and gateway.revoked:revoked.set()
                task=asyncio.create_task(process());tasks.add(task);task.add_done_callback(tasks.discard)
        try:server,_=await loop.create_datagram_endpoint(Server,local_addr=('::1',0))
        except OSError:self.skipTest('IPv6 loopback unavailable')
        with tempfile.TemporaryDirectory() as directory:
            demo=demo_module.Demo(SimpleNamespace(state=str(Path(directory)/'demo.json'),network='1234567890abcdef',backend_id='ha_main'))
            client,_=await loop.create_datagram_endpoint(lambda:demo,remote_addr=server.get_extra_info('sockname')[:2])
            running=asyncio.create_task(demo.run())
            try:
                await asyncio.wait_for(discovered.wait(),1)
                uid=demo.saved['device']
                await gateway.admit(uid)
                await asyncio.wait_for(reported.wait(),2)
                self.assertEqual(len(gateway.exposed(uid)),3)
                result=await gateway.command(uid,'relay',True)
                self.assertTrue(result['success'])
                self.assertTrue(demo.relay)
                await gateway.remove(uid)
                await asyncio.wait_for(revoked.wait(),6)
                self.assertNotIn('secret',demo.saved)
            finally:
                running.cancel()
                try:await running
                except asyncio.CancelledError:pass
                await gateway.close()
                server.close();client.close()
                if tasks:await asyncio.gather(*tasks)

    async def test_real_udp_manual_binding(self):
        loop=asyncio.get_running_loop()
        packets=asyncio.Queue()
        discovered=asyncio.Event()
        tasks=set()
        async def save(*_): pass
        async def notify(*_): pass
        gateway=h.GatewayHub('ha_main',save,lambda data,peer:server.sendto(data,peer),notify)
        class Server(asyncio.DatagramProtocol):
            def datagram_received(self,data,peer):
                async def process():
                    await gateway.receive(data,peer)
                    discovered.set()
                task=asyncio.create_task(process());tasks.add(task);task.add_done_callback(tasks.discard)
        class Client(asyncio.DatagramProtocol):
            def datagram_received(self,data,peer): packets.put_nowait(p.decode(data)[0])
        try:
            server,_=await loop.create_datagram_endpoint(Server,local_addr=('::1',0))
            client,_=await loop.create_datagram_endpoint(Client,local_addr=('::1',0))
        except OSError:
            self.skipTest('IPv6 loopback unavailable')
        try:
            target=server.get_extra_info('sockname')
            hello={'v':3,'type':'hello','device':'0123456789abcdef','node':'demo','name':'Demo','network':'1234567890abcdef',
                   'node_type':'reed','session':'11'*8,'request_id':'22'*8,'boot':1,'generation':0}
            client.sendto(p.encode(hello),target)
            await asyncio.wait_for(discovered.wait(),1)
            self.assertTrue(packets.empty())
            await gateway.admit(hello['device'])
            offer=await asyncio.wait_for(packets.get(),1)
            self.assertEqual(offer['type'],'bind_offer')
            ack={'v':3,'type':'bind_ack','device':hello['device'],'backend_id':'ha_main',
                 'binding_id':offer['binding_id'],'session':hello['session'],'request_id':hello['request_id']}
            client.sendto(p.encode(ack,offer['secret']),target)
            reply=await asyncio.wait_for(packets.get(),1)
            self.assertEqual(reply['type'],'bind_complete')
        finally:
            await gateway.close()
            server.close();client.close()
            if tasks: await asyncio.gather(*tasks)


if __name__=='__main__':unittest.main(verbosity=2)
