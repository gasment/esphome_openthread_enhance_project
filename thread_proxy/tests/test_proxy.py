"""Real Native API network tests; HA plumbing uses explicit lightweight stand-ins."""
import asyncio
import base64
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock
from aiohttp import web
from aioesphomeapi import api_pb2 as pb
from noise.connection import NoiseConnection
from cryptography.exceptions import InvalidTag

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'custom_components' / 'thread_proxy'

def module(name, **values):
    obj = types.ModuleType(name)
    obj.__dict__.update(values)
    sys.modules[name] = obj
    return obj

class Abort(Exception):
    pass

class Flow:
    def __init_subclass__(cls, **kw): pass
    async def async_set_unique_id(self, value, **kwargs):
        self.unique_id = value; self.unique_id_options = kwargs
    def _async_current_entries(self): return self.hass.config_entries.async_entries('thread_proxy')
    def _abort_if_unique_id_configured(self, updates=None, **kwargs):
        for entry in self._async_current_entries():
            if entry.unique_id == getattr(self, 'unique_id', None):
                if updates: self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
                raise Abort('already_configured')
    def _async_abort_entries_match(self, data):
        if any(all(entry.data.get(k)==v for k,v in data.items()) for entry in self._async_current_entries()):
            raise Abort('already_configured')
    def async_show_form(self, **kw): return {'type':'form', **kw}
    def async_abort(self, **kw): return {'type':'abort', **kw}
    def async_create_entry(self, **kw): return {'type':'create_entry', **kw, 'unique_id':getattr(self,'unique_id',None)}
    def _get_reconfigure_entry(self): return self.entry
    def async_update_and_abort(self, entry, data_updates, title):
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, **data_updates}, title=title)
        return {'type':'abort', 'reason':'reconfigure_successful'}

class View:
    def json(self, value, status_code=200): return web.json_response(value,status=status_code)

module('homeassistant')
module('homeassistant.config_entries', ConfigFlow=Flow)
module('homeassistant.helpers')
module('homeassistant.helpers.selector', TextSelector=lambda x:str, TextSelectorConfig=lambda **x:x,
       TextSelectorType=types.SimpleNamespace(PASSWORD='password'))
module('homeassistant.components')
module('homeassistant.components.zeroconf')
module('homeassistant.components.panel_custom')
module('homeassistant.components.http', HomeAssistantView=View, KEY_HASS_USER='user', StaticPathConfig=object)
fixture = module('proxy_fixture')
fixture.__path__ = [str(PACKAGE)]
models = importlib.import_module('proxy_fixture.models')
connection = importlib.import_module('proxy_fixture.connection')
manager_mod = importlib.import_module('proxy_fixture.manager')
config = importlib.import_module('proxy_fixture.config_flow')
frontend = importlib.import_module('proxy_fixture.frontend')
diagnostics = importlib.import_module('proxy_fixture.diagnostics')

MAC = 'aa:bb:cc:dd:ee:ff'
ID = MAC.replace(':','')
KEY = bytes(range(32))
PSK = base64.b64encode(KEY).decode()
def record(identity=ID,host='192.0.2.1'):
    return models.Discovery.parse({'component':'thread_proxy_api','id':identity,'version':'1'},host,6065,hostname='test-br.local.')
def entry(identity=ID,host='192.0.2.1', key='secret'):
    return types.SimpleNamespace(entry_id=identity or 'manual',title='BR',unique_id=identity,domain='thread_proxy',
                                 data={'host':host,'port':6065,'noise_psk':key})
class Entries:
    def __init__(self, entries=()): self.items=list(entries); self.flow=types.SimpleNamespace(async_init=AsyncMock(return_value={'type':'create_entry'}))
    def async_entries(self, domain): return [e for e in self.items if e.domain==domain]
    def async_update_entry(self, entry, **kw):
        for k,v in kw.items(): setattr(entry,k,v)
    def async_get_entry(self, identity): return next((e for e in self.items if e.entry_id==identity),None)
    async_remove=AsyncMock(return_value={'require_restart':False})
    async_reload=AsyncMock(return_value=True)
def hass(entries=()):
    return types.SimpleNamespace(data={},config_entries=Entries(entries),loop=asyncio.get_running_loop(),
        async_create_background_task=lambda coro,name:asyncio.create_task(coro,name=name))

class ModelTests(unittest.TestCase):
    def test_proxy_has_independent_default_port(self):
        self.assertEqual(config.DEFAULT_PORT,6065)
        self.assertNotEqual(config.DEFAULT_PORT,6053)
    def test_discovery_preserves_custom_proxy_port(self):
        result=models.Discovery.parse({'component':'thread_proxy_api','id':ID,'version':'1'},'192.0.2.1',16065)
        self.assertEqual(result.port,16065)
    def test_dual_stack_discovery_does_not_flip_endpoint(self):
        addresses=['fd00::1','192.0.2.2','fe80::1%eth0']
        self.assertEqual(models.discovery_host(addresses),'192.0.2.2')
        self.assertEqual(models.discovery_host(list(reversed(addresses))),'192.0.2.2')
    def test_only_proxy_component_discovered(self):
        for props in ({}, {'component':'thread_br'}, {'component':'thread_proxy_api','id':ID,'version':'2'}):
            with self.assertRaises(ValueError): models.Discovery.parse(props,'192.0.2.1',6065)
    def test_name_is_mdns_host_without_local(self): self.assertEqual(record().name,'test-br')
    def test_address_validation(self):
        self.assertEqual(models.host_name('[fd00::1]'),'fd00::1')
        self.assertEqual(models.host_name('BR.local.'),'br.local')
        for value in ('',None,'http://host','fd00::zzz','fd00::1%','.', 'a b'):
            with self.subTest(value=value), self.assertRaises(ValueError): models.host_name(value)
    def test_ports(self):
        self.assertEqual(models.api_port('6065'),6065)
        for value in (True,0,65536,None,'x',1.5):
            with self.subTest(value=value), self.assertRaises(ValueError): models.api_port(value)
    def test_key_optional_or_valid(self):
        self.assertEqual(models.encryption_key(''),'')
        self.assertEqual(models.encryption_key(PSK),PSK)
        with self.assertRaises(ValueError): models.encryption_key('not-a-key')
    def test_public_state_excludes_credentials(self):
        public=models.BRState('id','BR','::1',6065).public()
        self.assertFalse(public['data_plane_ready']); self.assertNotIn('noise_psk',public)
    def test_metadata_identity_and_version(self):
        for changes in ({'id':'112233445566'},{'version':'9'},{'component':'thread_br'}):
            with self.assertRaises(ValueError): models.parse_metadata({'component':'thread_proxy_api','version':'1','id':ID,**changes},ID)

class FlowTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, entries=()):
        obj=config.ThreadProxyConfigFlow(); obj.hass=hass(entries); obj.context={}; return obj
    async def test_discovery_requires_confirmation(self):
        flow=self.flow()
        info=types.SimpleNamespace(type='_thread-proxy._tcp.local.',properties={'component':'thread_proxy_api','id':ID,'version':'1'},host='192.0.2.1',port=6065,name='test',hostname='br.local.')
        result=await flow.async_step_zeroconf(info)
        self.assertEqual(result['step_id'],'confirm')
        result=await flow.async_step_confirm({})
        self.assertEqual(result['type'],'create_entry'); self.assertEqual(result['unique_id'],ID)
    async def test_meshcop_not_discovered(self):
        result=await self.flow().async_step_zeroconf(types.SimpleNamespace(type='_meshcop._udp.local.'))
        self.assertEqual(result['reason'],'invalid_discovery')
    async def test_manual_offline_and_optional_key(self):
        result=await self.flow().async_step_user({'host':'[fd00::1]'})
        self.assertEqual(result['data']['noise_psk'],''); self.assertEqual(result['data']['binding_source'],'manual')
    async def test_panel_candidate_identity_preserved(self):
        flow=self.flow(); result=await flow.async_step_user({'host':'br.local','_proxy_id':ID})
        self.assertEqual(result['unique_id'],ID); self.assertEqual(result['data']['binding_source'],'discovery')
        self.assertEqual(flow.unique_id_options,{'raise_on_progress':False})
    async def test_duplicate_endpoint_rejected(self):
        with self.assertRaises(Abort): await self.flow([entry()]).async_step_user({'host':'192.0.2.1','port':6065})
    async def test_duplicate_identity_rejected(self):
        with self.assertRaises(Abort): await self.flow([entry()]).async_step_user({'host':'other.local','_proxy_id':ID})
    async def test_reconfigure_key_preserved_or_cleared(self):
        saved=entry(); flow=self.flow([saved]); flow.entry=saved
        await flow.async_step_reconfigure({'host':'new.local','name':'New BR'})
        self.assertEqual(saved.data['noise_psk'],'secret'); self.assertEqual(saved.title,'New BR')
        await flow.async_step_reconfigure({'host':'new.local','noise_psk':''})
        self.assertEqual(saved.data['noise_psk'],'')
    async def test_invalid_key_form(self):
        result=await self.flow().async_step_user({'host':'br.local','noise_psk':'bad'})
        self.assertEqual(result['errors']['base'],'invalid_key')

class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unloaded_bindings_remain_visible(self):
        m=manager_mod.ProxyManager(hass([entry()]))
        result=m.snapshot()['bound']
        self.assertEqual(len(result),1); self.assertFalse(result[0]['connected']); self.assertTrue(result[0]['bound'])
    async def test_ignored_discovery_not_a_binding(self):
        ignored=entry(); ignored.source='ignore'
        m=manager_mod.ProxyManager(hass([ignored]))
        self.assertEqual(m.snapshot()['bound'],[])
    async def test_bound_not_repeated_in_discovery(self):
        saved=entry(); m=manager_mod.ProxyManager(hass([saved])); m.remember(record())
        self.assertEqual(m.snapshot()['discovered'],[])
        m.hass.config_entries.items.clear()
        self.assertEqual(len(m.snapshot()['discovered']),1)
    async def test_manual_endpoint_also_filtered(self):
        m=manager_mod.ProxyManager(hass([entry(None)])); m.remember(record())
        self.assertEqual(m.snapshot()['discovered'],[])
    async def test_discovery_updates_endpoint_preserves_key(self):
        saved=entry(); m=manager_mod.ProxyManager(hass([saved])); m.remember(record(host='192.0.2.2'))
        self.assertEqual(saved.data['host'],'192.0.2.2'); self.assertEqual(saved.data['noise_psk'],'secret')
    async def test_discovery_removal_hidden(self):
        m=manager_mod.ProxyManager(hass()); r=record(); m.remember(r); r.present=False
        self.assertEqual(m.snapshot()['discovered'],[])
    async def test_unloading_one_binding_preserves_another(self):
        m=manager_mod.ProxyManager(hass())
        one=types.SimpleNamespace(connection=types.SimpleNamespace(close=AsyncMock()),task=None)
        two=types.SimpleNamespace(connection=types.SimpleNamespace(close=AsyncMock()),task=None)
        m.runtimes={'one':one,'two':two}
        await m.remove_entry('one'); self.assertIs(m.runtimes['two'],two); two.connection.close.assert_not_called()
    async def test_diagnostics_redacted(self):
        saved=entry(); saved.runtime_data=types.SimpleNamespace(state=models.BRState('id','BR','::1',6065))
        result=await diagnostics.async_get_config_entry_diagnostics(hass(),saved)
        self.assertNotIn('secret',json.dumps(result)); self.assertNotIn('noise_psk',json.dumps(result))

class Request(dict):
    def __init__(self,path='/api/thread_proxy',data=None,admin=True):
        super().__init__(user=types.SimpleNamespace(is_admin=admin)); self.path=path; self.data=data
    async def json(self): return self.data
class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass=hass(); self.manager=manager_mod.ProxyManager(self.hass); self.view=frontend.ProxyView(self.hass,self.manager)
    async def test_non_admin_forbidden(self):
        with self.assertRaises(web.HTTPForbidden): await self.view.get(Request(admin=False))
        with self.assertRaises(web.HTTPForbidden): await self.view.post(Request('/api/thread_proxy/bind',{},False))
    async def test_malformed_candidate(self):
        response=await self.view.post(Request('/api/thread_proxy/bind',{'candidate_id':[]}))
        self.assertEqual(response.status,400)
    async def test_foreign_entry_denied(self):
        other=entry(); other.domain='esphome'; self.hass.config_entries.items=[other]
        response=await self.view.post(Request('/api/thread_proxy/unbind',{'entry_id':other.entry_id}))
        self.assertEqual(response.status,404)
    async def test_candidate_bind_passes_known_identity(self):
        self.manager.remember(record())
        response=await self.view.post(Request('/api/thread_proxy/bind',{'candidate_id':ID}))
        self.assertEqual(response.status,200)
        self.assertEqual(self.hass.config_entries.flow.async_init.call_args.kwargs['data']['_proxy_id'],ID)
    async def test_stale_candidate_rejected(self):
        response=await self.view.post(Request('/api/thread_proxy/bind',{'candidate_id':ID}))
        self.assertEqual(response.status,409)

def varint(value):
    result=bytearray()
    while value>127: result.append((value&127)|128); value>>=7
    result.append(value); return bytes(result)
async def read_varint(reader):
    value=shift=0
    while True:
        b=(await reader.readexactly(1))[0]; value|=(b&127)<<shift
        if not b&128: return value
        shift+=7

class Peer:
    def __init__(self,encrypted=False,metadata=True,identity=ID):
        self.encrypted=encrypted; self.metadata=metadata; self.identity=identity; self.tasks=set(); self.writers=set(); self.connections=0
    def accept(self,reader,writer):
        task=asyncio.create_task(self.handle(reader,writer)); self.tasks.add(task); task.add_done_callback(self.tasks.discard)
    async def handle(self,reader,writer):
        self.connections+=1; self.writers.add(writer)
        try:
            if self.encrypted:
                noise=NoiseConnection.from_name(b'Noise_NNpsk0_25519_ChaChaPoly_SHA256')
                noise.set_as_responder(); noise.set_psks(KEY); noise.set_prologue(b'NoiseAPIInit\x00\x00'); noise.start_handshake()
                async def read_frame():
                    head=await reader.readexactly(3); assert head[0]==1
                    return await reader.readexactly(int.from_bytes(head[1:],'big'))
                def frame(raw): return b'\x01'+len(raw).to_bytes(2,'big')+raw
                assert await read_frame()==b''
                writer.write(frame(b'\x01test-br\x00'+MAC.encode()+b'\x00')); await writer.drain()
                request=await read_frame(); noise.read_message(request[1:])
                writer.write(frame(b'\x00'+noise.write_message())); await writer.drain()
                async def read():
                    raw=noise.decrypt(await read_frame()); return int.from_bytes(raw[:2],'big'),raw[4:]
                async def send(ident,msg):
                    raw=msg.SerializeToString(); writer.write(frame(noise.encrypt(ident.to_bytes(2,'big')+len(raw).to_bytes(2,'big')+raw))); await writer.drain()
            else:
                async def read():
                    assert await reader.readexactly(1)==b'\x00'
                    size=await read_varint(reader); ident=await read_varint(reader)
                    return ident,await reader.readexactly(size)
                async def send(ident,msg):
                    raw=msg.SerializeToString(); writer.write(b'\x00'+varint(len(raw))+varint(ident)+raw); await writer.drain()
            while True:
                ident,raw=await read()
                if ident==1: await send(2,pb.HelloResponse(api_version_major=1,api_version_minor=14,name='test-br'))
                elif ident==9: await send(10,pb.DeviceInfoResponse(name='test-br',mac_address=MAC,esphome_version='2026.11.0',model='ESP32-C5'))
                elif ident==34 and self.metadata:
                    response=pb.HomeassistantActionRequest(service='thread_proxy.info')
                    for k,v in {'component':'thread_proxy_api','version':'1','id':self.identity,'network_id':'591cbc42f1a3afe5','node_count':'3'}.items(): response.data.add(key=k,value=v)
                    await send(35,response)
                elif ident==7: await send(8,pb.PingResponse())
                elif ident==5: await send(6,pb.DisconnectResponse()); return
        except (asyncio.IncompleteReadError,ConnectionError,asyncio.CancelledError,InvalidTag): pass
        finally:
            self.writers.discard(writer); writer.close()
    async def close(self):
        for writer in tuple(self.writers): writer.close()
        for task in tuple(self.tasks): task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)

class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_connection_recovers_after_disconnect(self):
        peer=Peer(); server=await asyncio.start_server(peer.accept,'127.0.0.1',0)
        state=models.BRState('id','BR','127.0.0.1',server.sockets[0].getsockname()[1],proxy_id=ID)
        conn=connection.ProxyConnection(state,'',lambda:None,lambda x:True)
        task=asyncio.create_task(conn.run())
        try:
            async with asyncio.timeout(6):
                while not state.proxy_ready: await asyncio.sleep(.005)
                for writer in tuple(peer.writers): writer.close()
                while peer.connections<2 or not state.proxy_ready: await asyncio.sleep(.005)
            self.assertTrue(state.connected)
        finally:
            await conn.close(); task.cancel(); await asyncio.gather(task,return_exceptions=True)
            server.close(); await server.wait_closed(); await peer.close()
    async def scenario(self,encrypted=False,metadata=True,expected='ready',identity=ID,bound=ID):
        peer=Peer(encrypted,metadata,identity); server=await asyncio.start_server(peer.accept,'127.0.0.1',0)
        state=models.BRState('id','BR','127.0.0.1',server.sockets[0].getsockname()[1],proxy_id=bound)
        conn=connection.ProxyConnection(state,PSK if encrypted else '',lambda:None,lambda x:True,metadata_timeout=.1)
        task=asyncio.create_task(conn.run())
        try:
            async with asyncio.timeout(4):
                while state.status!=expected: await asyncio.sleep(.005)
            return state
        finally:
            await conn.close(); task.cancel(); await asyncio.gather(task,return_exceptions=True)
            server.close(); await server.wait_closed(); await peer.close()
    async def test_real_plaintext_native_api(self):
        state=await self.scenario(); self.assertEqual(state.node_count,3); self.assertEqual(state.firmware,'2026.11.0')
    async def test_real_noise_native_api(self):
        state=await self.scenario(encrypted=True); self.assertEqual(state.network_id,'591cbc42f1a3afe5')
    async def test_plain_api_without_proxy_is_unsupported(self):
        state=await self.scenario(metadata=False,expected='unsupported'); self.assertEqual(state.last_error,'proxy_not_announced')
    async def test_device_identity_mismatch(self):
        state=await self.scenario(bound='112233445566',expected='identity_mismatch'); self.assertFalse(state.proxy_ready)
    async def test_proxy_identity_mismatch(self):
        state=await self.scenario(identity='112233445566',expected='identity_mismatch'); self.assertFalse(state.proxy_ready)

if __name__=='__main__': unittest.main()
