"""Session lifecycle tests using real TCP/Noise with lightweight HA stand-ins."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from test_native import NativePeer, PSK, MAC, TRANSPORT

class MemoryStore:
    def __init__(self,*args): self.value=None
    async def async_load(self): return self.value
    async def async_save(self,value): self.value=value

for name in ('homeassistant','homeassistant.helpers','homeassistant.helpers.storage','homeassistant.exceptions'):
    sys.modules.setdefault(name,types.ModuleType(name))
sys.modules['homeassistant.helpers.storage'].Store=MemoryStore
sys.modules['homeassistant.exceptions'].HomeAssistantError=RuntimeError
package=types.ModuleType('sed_fixture'); package.__path__=[str(TRANSPORT.parent)]; sys.modules['sed_fixture']=package
spec=importlib.util.spec_from_file_location('sed_fixture.session',TRANSPORT.with_name('session.py'))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node=NativePeer()
        self.server=await asyncio.start_server(self.node.accept,'127.0.0.1',0)
        self.port=self.server.sockets[0].getsockname()[1]
        hass=types.SimpleNamespace(async_create_task=asyncio.create_task)
        entry=types.SimpleNamespace(unique_id='aabbccddeeff',title='Fixture',data={'host':'127.0.0.1','port':self.port,'noise_psk':PSK,'mac':MAC})
        self.session=module.Session(hass,entry)
    async def asyncTearDown(self):
        await self.session.close(); self.server.close(); await self.server.wait_closed(); await self.node.close()
    async def connect(self):
        await self.session.connect('127.0.0.1',self.port)
        async with asyncio.timeout(2):
            while (0,7) not in self.session.states:
                await asyncio.sleep(.01)
    async def test_initial_entities_and_states(self):
        await self.connect()
        self.assertTrue(self.session.online)
        self.assertEqual(self.session.entities[0].key,7)
        self.assertEqual(self.session.states[(0,7)].state,21.5)
        self.assertTrue(self.session.store.value['schema'].startswith('fixture-v1/'))
    async def test_reconnect_uses_schema_cache(self):
        await self.connect()
        await self.session.client.disconnect(force=True); await asyncio.sleep(.03)
        await self.connect()
        self.assertEqual(self.node.lists,1)
        self.assertTrue(self.session.available)
    async def test_schema_change_reenumerates(self):
        await self.connect()
        await self.session.client.disconnect(force=True); await asyncio.sleep(.03)
        self.session.schema='previous-firmware'
        await self.connect()
        self.assertEqual(self.node.lists,2)
    async def test_sleep_ack_preserves_availability(self):
        await self.connect()
        await self.session.control(self.session.client,types.SimpleNamespace(service='thread_sed.sleep',data={'token':'123'}))
        await asyncio.sleep(.03)
        self.assertIn('123',self.node.controls)
        await self.session.client.disconnect(force=True); await asyncio.sleep(.03)
        self.assertFalse(self.session.online)
        self.assertTrue(self.session.available)
        with self.assertRaises(RuntimeError): self.session.command('switch',7,state=True)
    async def test_event_ack_deduplicates_replay(self):
        await self.connect()
        seen=[]; self.session.callbacks.add(seen.append)
        call=types.SimpleNamespace(service='thread_sed.event',data={'token':'555:1:8:single'})
        await self.session.control(self.session.client,call)
        await self.session.control(self.session.client,call)
        self.assertEqual(seen.count((0,8)),1)
        self.assertEqual(self.session.store.value['event_seen'],{'555:8':1})
        await asyncio.sleep(.03)
        self.assertEqual(self.node.controls.count('event:555:1'),2)
    async def test_cached_models_roundtrip(self):
        await self.connect()
        clone=module.Session(self.session.hass,self.session.entry)
        clone.store.value=self.session.store.value
        await clone.load()
        self.assertEqual(clone.entities,self.session.entities)
        self.assertEqual(clone.info,self.session.info)
    async def test_sleep_snapshot_restores_state(self):
        await self.connect()
        await self.session.control(self.session.client,types.SimpleNamespace(service='thread_sed.sleep',data={'token':'456'}))
        clone=module.Session(self.session.hass,self.session.entry)
        clone.store.value=self.session.store.value
        await clone.load()
        self.assertEqual(clone.states[(0,7)].state,21.5)
        self.assertTrue(clone.available)
    async def test_unknown_sleep_duration_does_not_probe_continuously(self):
        await self.connect()
        self.session.sleep_seconds=0
        await self.session.control(self.session.client,types.SimpleNamespace(service='thread_sed.sleep',data={'token':'789'}))
        self.assertTrue(self.session.sleep_indefinite)
        self.assertTrue(self.session.store.value['sleep_indefinite'])

    async def test_reverse_listener_adopts_full_session(self):
        from sed_fixture.transport import ReverseListener
        listener=ReverseListener({self.session.mac:self.session})
        await listener.start('::1',0)
        reader,writer=await asyncio.open_connection('::1',listener.socket.getsockname()[1])
        task=asyncio.create_task(self.node.handle(reader,writer,outgoing=True))
        try:
            async with asyncio.timeout(3):
                while (0,7) not in self.session.states:
                    await asyncio.sleep(.01)
            self.assertTrue(self.session.online)
            self.assertEqual(self.session.states[(0,7)].state,21.5)
        finally:
            await self.session.close()
            await listener.close()
            task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_ready_handshake_roundtrip(self):
        await self.connect()
        async with asyncio.timeout(2):
            while 'ready' not in self.node.controls:
                await asyncio.sleep(.01)

    async def test_batch_ack_over_native_connection(self):
        from aioesphomeapi import api_pb2 as pb
        await self.connect()
        event=pb.HomeassistantActionRequest(service='thread_sed.batch_event')
        event.data.add(key='token',value='789:1:8:single')
        marker=pb.HomeassistantActionRequest(service='thread_sed.batch')
        marker.data.add(key='token',value='456:1')
        await self.node.send(35,event)
        await self.node.send(35,marker)
        async with asyncio.timeout(2):
            while 'batch:456:1' not in self.node.controls:
                await asyncio.sleep(.01)
        self.assertNotIn('event:789:1',self.node.controls)
        self.assertEqual(self.session.store.value['event_seen'],{'789:8':1})

if __name__=='__main__': unittest.main()
