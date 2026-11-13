"""Central BR ownership, migration, reconfiguration, and key redaction."""
import asyncio
import importlib
import json
import types
import unittest
from unittest.mock import AsyncMock
import test_proxy as fixture
import test_br_device as device_fixture

catalog=importlib.import_module('proxy_fixture.catalog')
migration=importlib.import_module('proxy_fixture.migration')
option=importlib.import_module('proxy_fixture.br_device')

class Registry(device_fixture.Registry):
    def async_update_entity(self,identity,**values):
        for key,value in values.items():setattr(self.items[identity],key,value)
    def async_update_device(self,identity,**values):
        for key,value in values.items():setattr(self.items[identity],'config_entry_id' if key=='new_config_entry_id' else key,value)

class Entries(fixture.Entries):
    def __init__(self,entries):super().__init__(entries);self.removed=[]
    def async_update_entry(self,entry,**values):
        allowed={'data','discovery_keys','minor_version','options','pref_disable_new_entities','pref_disable_polling','title','unique_id','version'}
        if set(values)-allowed:raise TypeError('Unsupported HA 2026.11 config-entry update argument')
        super().async_update_entry(entry,**values)
    async def async_remove(self,identity):
        self.removed.append(identity);self.items=[x for x in self.items if x.entry_id!=identity]
        return {'require_restart':False}

def root_entry(bindings=()):
    return types.SimpleNamespace(entry_id='root',domain='thread_proxy',title='Thread Proxy',unique_id=catalog.MANAGER_ID,
        source='user',data={'kind':'manager','bindings':[b.public_record() for b in bindings],'migration_pending':[]},options={},version=2)

class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def setup_manager(self):
        root=root_entry();hass=fixture.hass();hass.config_entries=Entries([root])
        hass.devices=Registry([]);hass.entities=Registry([])
        manager=fixture.manager_mod.get_manager(hass)
        def connect(runtime):
            runtime.connection=types.SimpleNamespace(close=AsyncMock());runtime.task=None
            runtime.state.connected=True;runtime.state.status='ready'
        manager.start_connection=connect
        await manager.setup_root(root)
        self.root,self.hass,self.manager=root,hass,manager
        return manager

    def values(self,host='192.0.2.1',**extra):
        return {'host':host,'port':6065,'name':'BR','noise_psk':fixture.PSK,'create_device':False,**extra}

    async def test_unchecked_bind_creates_no_per_br_config_entry(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        self.assertEqual(len(self.hass.config_entries.items),1)
        self.assertEqual(self.hass.config_entries.items[0].entry_id,'root')
        self.assertNotEqual(binding.entry_id,'root')
        self.hass.config_entries.flow.async_init.assert_not_called()
        self.assertFalse(manager.snapshot()['bound'][0]['create_device'])
        self.assertEqual(self.hass.entities.items,{});self.assertEqual(self.hass.devices.items,{})

    async def test_catalog_roundtrip_retains_off_preference_identity_and_key(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        await manager.unload_root();await manager.setup_root(self.root)
        restored=manager.entries()[0]
        self.assertEqual(restored.entry_id,binding.entry_id);self.assertEqual(restored.data['noise_psk'],fixture.PSK)
        self.assertFalse(option.br_device_enabled(restored));self.assertTrue(manager.runtimes[binding.entry_id].state.connected)
        self.assertEqual(len(self.hass.config_entries.items),1)

    async def test_pending_manager_does_not_create_a_second_entry(self):
        root=root_entry();hass=fixture.hass();hass.config_entries=Entries([root])
        flow=fixture.config.ThreadProxyConfigFlow();flow.hass=hass;flow.context={}
        result=await flow.async_step_user(self.values())
        self.assertEqual(result['type'],'form');self.assertEqual(result['errors']['base'],'manager_not_ready')
        self.assertEqual(len(hass.config_entries.items),1)

    async def test_shared_owner_cleanup_keeps_proxy_nodes(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values(create_device=True))
        runtime=manager.runtimes[binding.entry_id]
        runtime.br_entities=[device_fixture.Entity() for _ in range(4)]
        self.hass.devices.items['br']=types.SimpleNamespace(id='br',config_entry_id='root',identifiers={('thread_proxy',binding.entry_id)})
        self.hass.devices.items['node']=types.SimpleNamespace(id='node',config_entry_id='root',identifiers={('thread_proxy','node')})
        for key in option.BR_ENTITY_KEYS:
            self.hass.entities.items['sensor.'+key]=types.SimpleNamespace(entity_id='sensor.'+key,config_entry_id='root',platform='thread_proxy',unique_id=f'{binding.entry_id}_{key}')
        self.hass.entities.items['sensor.node']=types.SimpleNamespace(entity_id='sensor.node',config_entry_id='root',platform='thread_proxy',unique_id='node_temperature')
        await manager.set_device(binding,False)
        self.assertEqual(self.hass.devices.removed,['br'])
        self.assertEqual(list(self.hass.entities.items),['sensor.node'])
        self.assertIn('node',self.hass.devices.items);self.assertEqual(len(self.hass.config_entries.items),1)

    async def test_root_platforms_create_only_selected_br_diagnostics(self):
        manager=await self.setup_manager();one=await manager.bind(self.values());two=await manager.bind(self.values('192.0.2.2',create_device=True))
        added=[]
        await manager.register_platform('sensor',lambda rt:[device_fixture.Entity()],added.extend)
        self.assertEqual(len(added),1);self.assertEqual(manager.runtimes[one.entry_id].br_entities,[])
        await manager.set_device(one,True)
        self.assertEqual(len(added),2)
        await manager.set_device(one,False)
        self.assertEqual(len(manager.runtimes[two.entry_id].br_entities),1)

    async def test_reconfigure_reconnects_only_selected_br(self):
        manager=await self.setup_manager();one=await manager.bind(self.values());two=await manager.bind(self.values('192.0.2.2'))
        first,other=manager.runtimes[one.entry_id].connection,manager.runtimes[two.entry_id].connection
        await manager.reconfigure(one,{'host':'192.0.2.3'})
        first.close.assert_awaited_once();other.close.assert_not_called()

    async def test_reconfigure_blank_omission_preserves_key_and_device_flag(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        await manager.reconfigure(binding,{'host':'new.local','port':16065,'name':'New BR'})
        self.assertEqual(binding.data['noise_psk'],fixture.PSK);self.assertEqual(binding.title,'New BR')
        self.assertEqual(manager.snapshot()['bound'][0]['host'],'new.local')
        self.assertFalse(option.br_device_enabled(binding));self.assertEqual(len(self.hass.config_entries.items),1)

    async def test_explicit_key_clear_and_replacement(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        await manager.reconfigure(binding,{'host':'192.0.2.1','clear_key':True})
        self.assertEqual(binding.data['noise_psk'],'')
        await manager.reconfigure(binding,{'host':'192.0.2.1','noise_psk':fixture.PSK})
        self.assertEqual(binding.data['noise_psk'],fixture.PSK)

    async def test_invalid_or_duplicate_reconfigure_does_not_mutate(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        await manager.bind(self.values('192.0.2.2'))
        original=binding.public_record()
        for data in ({'host':'192.0.2.2'},{'host':'bad host'},{'host':'192.0.2.1','noise_psk':'bad'},
                     {'host':'192.0.2.1','clear_key':True,'noise_psk':fixture.PSK}):
            with self.assertRaises(ValueError):await manager.reconfigure(binding,data)
            self.assertEqual(binding.public_record(),original)

    async def test_name_only_change_preserves_connection(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        connection=manager.runtimes[binding.entry_id].connection
        await manager.reconfigure(binding,{'host':'192.0.2.1','name':'Renamed'})
        connection.close.assert_not_called();self.assertEqual(binding.title,'Renamed')

    async def test_device_toggle_persists_under_root_without_new_entry_or_connection(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        connection=manager.runtimes[binding.entry_id].connection
        await manager.set_device(binding,True);await manager.set_device(binding,False)
        self.assertFalse(self.root.data['bindings'][0]['options']['create_device'])
        self.assertEqual(len(self.hass.config_entries.items),1);connection.close.assert_not_called()

    async def test_unbind_retains_manager_other_br_and_proxy_node_registry(self):
        manager=await self.setup_manager();one=await manager.bind(self.values());two=await manager.bind(self.values('192.0.2.2'))
        self.hass.devices.items['node']=types.SimpleNamespace(id='node',config_entry_id='root',identifiers={('thread_proxy','node')})
        self.hass.entities.items['sensor.node']=types.SimpleNamespace(entity_id='sensor.node',config_entry_id='root',unique_id='node_sensor',platform='thread_proxy')
        connection=manager.runtimes[two.entry_id].connection
        await manager.unbind(one)
        self.assertEqual(list(manager.bindings),[two.entry_id]);self.assertIn('node',self.hass.devices.items)
        self.assertIn('sensor.node',self.hass.entities.items);self.assertEqual(self.hass.config_entries.removed,[])
        connection.close.assert_not_called()

    async def test_http_reconfigure_works_without_a_br_config_entry(self):
        manager=await self.setup_manager();binding=await manager.bind(self.values())
        view=fixture.frontend.ProxyView(self.hass,manager)
        result=await view.post(fixture.Request('/api/thread_proxy/reconfigure',{'entry_id':binding.entry_id,'host':'new.local','name':'New'}))
        self.assertEqual(result.status,200);self.assertEqual(binding.title,'New')
        self.assertIsNone(self.hass.config_entries.async_get_entry(binding.entry_id))

    async def test_manager_http_bind_does_not_run_config_flow(self):
        manager=await self.setup_manager();view=fixture.frontend.ProxyView(self.hass,manager)
        result=await view.post(fixture.Request('/api/thread_proxy/bind',self.values()))
        self.assertEqual(result.status,200);self.assertEqual(len(manager.entries()),1)
        self.hass.config_entries.flow.async_init.assert_not_called()

    async def test_shared_diagnostics_and_http_never_expose_keys(self):
        manager=await self.setup_manager();await manager.bind(self.values())
        result=await fixture.diagnostics.async_get_config_entry_diagnostics(self.hass,self.root)
        self.assertNotIn(fixture.PSK,json.dumps(result));self.assertNotIn('noise_psk',json.dumps(result))

    async def test_existing_manager_discovery_adds_binding_not_entry(self):
        manager=await self.setup_manager()
        flow=fixture.config.ThreadProxyConfigFlow();flow.hass=self.hass;flow.context={}
        result=await flow.async_step_user(self.values(_proxy_id=fixture.ID))
        self.assertEqual(result['type'],'abort');self.assertEqual(result['reason'],'binding_added')
        self.assertEqual(len(manager.entries()),1);self.assertEqual(len(self.hass.config_entries.items),1)

class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_merge_keeps_binding_ids_keys_options_entities_and_node_devices(self):
        one=fixture.entry(identity='one');one.options={'create_device':False};one.version=1;one.source='user'
        two=fixture.entry(identity='two',host='192.0.2.2');two.options={'create_device':True};two.version=1;two.source='user'
        hass=fixture.hass();hass.config_entries=Entries([one,two])
        hass.devices=Registry([types.SimpleNamespace(id='br2',config_entry_id='two',identifiers={('thread_proxy','two')}),
                               types.SimpleNamespace(id='node',config_entry_id='two',identifiers={('thread_proxy','node')})])
        hass.entities=Registry([types.SimpleNamespace(entity_id='sensor.br',config_entry_id='two',unique_id='two_status'),
                               types.SimpleNamespace(entity_id='sensor.node',config_entry_id='two',unique_id='node_temperature')])
        root=await migration.async_consolidate(hass)
        self.assertIs(root,one);self.assertEqual(root.title,'Thread Proxy');self.assertEqual(root.version,2)
        self.assertEqual({b['id'] for b in root.data['bindings']},{'one','two'})
        self.assertFalse(root.data['bindings'][0]['options']['create_device'])
        self.assertEqual(hass.devices.items['node'].config_entry_id,'one')
        self.assertEqual(hass.entities.items['sensor.node'].unique_id,'node_temperature')
        self.assertEqual(hass.entities.items['sensor.node'].config_entry_id,'one')
        self.assertEqual(hass.devices.removed,[]);self.assertEqual(hass.entities.removed,[])
        await migration.async_consolidate(hass) # Interrupted migration is safe to resume.
        await migration.async_finish_migration(hass,root)
        self.assertEqual([x.entry_id for x in hass.config_entries.items],['one'])
        self.assertEqual(root.data['migration_pending'],[])
        self.assertEqual(len(root.data['bindings']),2)

    async def test_disabled_br_is_not_silently_enabled_during_migration(self):
        old=fixture.entry();old.options={};old.disabled_by='user';old.version=1
        hass=fixture.hass([old])
        root=await migration.async_consolidate(hass)
        self.assertFalse(root.data['bindings'][0]['data']['enabled'])
        self.assertEqual(root.disabled_by,'user')
