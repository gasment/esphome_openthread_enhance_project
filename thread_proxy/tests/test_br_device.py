"""BR diagnostics are optional; proxy nodes and transport are independent."""
import asyncio
import importlib
import types
import unittest
from unittest.mock import AsyncMock
import test_proxy as fixture

option=importlib.import_module('proxy_fixture.br_device')
dr=fixture.module('homeassistant.helpers.device_registry')
er=fixture.module('homeassistant.helpers.entity_registry')
dr.async_get=lambda hass:hass.devices
er.async_get=lambda hass:hass.entities
dr.async_entries_for_config_entry=lambda registry,identity:[d for d in registry.items.values() if d.config_entry_id==identity]
dr.async_entries_for_parent_device=lambda registry,identity:[d for d in registry.items.values() if getattr(d,'parent_device_id',None)==identity]
er.async_entries_for_config_entry=lambda registry,identity:[e for e in registry.items.values() if e.config_entry_id==identity]

class Registry:
    def __init__(self,items): self.items={x.id if hasattr(x,'id') else x.entity_id:x for x in items};self.removed=[]
    def async_remove(self,identity): self.removed.append(identity);del self.items[identity]
    def async_remove_device(self,identity): self.async_remove(identity)

class Entity:
    def __init__(self): self.br_added=asyncio.Event();self.br_added.set();self.removed=False;self.br_registered=True
    async def async_remove(self,force_remove=False): self.removed=True

class BrDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setup_case(self,enabled=True):
        saved=fixture.entry(); saved.options={'create_device':enabled,'unrelated':'keep'}
        hass=fixture.hass([saved]); manager=fixture.manager_mod.get_manager(hass)
        runtime=fixture.manager_mod.Runtime(saved,fixture.models.BRState(saved.entry_id,'BR','::1',6065),dict(saved.data),br_device_enabled=enabled)
        runtime.connection=types.SimpleNamespace(close=AsyncMock());runtime.task=object()
        manager.runtimes[saved.entry_id]=runtime
        ident=saved.entry_id
        device=lambda key,ids,owner=ident,**extra:types.SimpleNamespace(id=key,identifiers=ids,config_entry_id=owner,**extra)
        hass.devices=Registry([device('br',{('thread_proxy',ident)}),device('node',{('thread_proxy','node-1')},via_device_id='br'),device('other-br',{('thread_proxy','other')},owner='other')])
        entities=[types.SimpleNamespace(entity_id=f'sensor.{key}',unique_id=f'{ident}_{key}',platform='thread_proxy',config_entry_id=ident,device_id='br') for key in option.BR_ENTITY_KEYS]
        entities += [types.SimpleNamespace(entity_id='sensor.node',unique_id='node-1_temp',platform='thread_proxy',config_entry_id=ident,device_id='node'),
                     types.SimpleNamespace(entity_id='sensor.foreign',unique_id=f'{ident}_status',platform='other',config_entry_id=ident,device_id='other')]
        hass.entities=Registry(entities)
        self.added=[]
        option.register_br_entities(runtime,'sensor',lambda rt:[Entity()],self.added.extend)
        option.register_br_entities(runtime,'binary_sensor',lambda rt:[Entity(),Entity(),Entity()],self.added.extend)
        return hass,saved,runtime

    async def test_default_preserves_existing_behavior(self):
        self.assertTrue(option.br_device_enabled(fixture.entry()))

    async def test_unchecked_does_not_create_br_entities_at_setup(self):
        hass,saved,runtime=self.setup_case(False)
        self.assertEqual(self.added,[]);self.assertEqual(runtime.br_entities,[])

    async def test_disable_removes_only_br_and_diagnostics(self):
        hass,saved,runtime=self.setup_case()
        old_entities=list(runtime.br_entities);connection=runtime.connection;task=runtime.task
        await option.async_set_br_device(hass,saved,False,persist=True)
        self.assertTrue(all(x.removed for x in old_entities))
        self.assertEqual(hass.devices.removed,['br'])
        self.assertIn('node',hass.devices.items);self.assertIn('other-br',hass.devices.items)
        self.assertIn('sensor.node',hass.entities.items);self.assertIn('sensor.foreign',hass.entities.items)
        self.assertFalse(saved.options['create_device']);self.assertEqual(saved.options['unrelated'],'keep')
        self.assertIs(runtime.connection,connection);self.assertIs(runtime.task,task);connection.close.assert_not_called()

    async def test_reenable_creates_only_four_br_diagnostics(self):
        hass,saved,runtime=self.setup_case(False)
        await option.async_set_br_device(hass,saved,True,persist=True)
        self.assertEqual(len(runtime.br_entities),4);self.assertEqual(len(self.added),4)
        await option.async_set_br_device(hass,saved)
        self.assertEqual(len(self.added),4)
        self.assertTrue(saved.options['create_device']);runtime.connection.close.assert_not_called()

    async def test_persisted_false_cleans_stale_br_without_nodes(self):
        hass,saved,runtime=self.setup_case(False)
        await option.async_set_br_device(hass,saved)
        self.assertEqual(self.added,[]);self.assertNotIn('br',hass.devices.items);self.assertIn('node',hass.devices.items)

    async def test_disabled_binding_saves_flag_without_transport(self):
        hass,saved,runtime=self.setup_case()
        fixture.manager_mod.get_manager(hass).runtimes.clear()
        await option.async_set_br_device(hass,saved,False,persist=True)
        self.assertFalse(saved.options['create_device']);self.assertIn('node',hass.devices.items)
        runtime.connection.close.assert_not_called()

    async def test_true_ha_children_cannot_be_cascade_deleted(self):
        hass,saved,runtime=self.setup_case()
        hass.devices.items['child']=types.SimpleNamespace(id='child',identifiers={('thread_proxy','child')},config_entry_id=saved.entry_id,parent_device_id='br')
        with self.assertRaisesRegex(option.BrDeviceError,'br_device_has_children'):
            await option.async_set_br_device(hass,saved,False,persist=True)
        self.assertEqual(hass.devices.removed,[]);self.assertEqual(hass.entities.removed,[])
        self.assertFalse(any(x.removed for x in runtime.br_entities));self.assertTrue(saved.options['create_device'])

    async def test_concurrent_toggles_keep_last_preference(self):
        hass,saved,runtime=self.setup_case()
        await asyncio.gather(option.async_set_br_device(hass,saved,False,persist=True),option.async_set_br_device(hass,saved,True,persist=True))
        self.assertTrue(saved.options['create_device']);self.assertEqual(len(runtime.br_entities),4)
        self.assertIn('sensor.node',hass.entities.items);runtime.connection.close.assert_not_called()

    async def test_snapshot_contains_persisted_flag(self):
        hass,saved,runtime=self.setup_case(False)
        self.assertFalse(fixture.manager_mod.get_manager(hass).snapshot()['bound'][0]['create_device'])

    async def test_invalid_value_rejected(self):
        hass,saved,runtime=self.setup_case()
        with self.assertRaises(option.BrDeviceError): await option.async_set_br_device(hass,saved,'false',persist=True)
        self.assertTrue(saved.options['create_device'])

    async def test_ha_disabled_diagnostic_is_cleaned_without_removing_aborted_entity(self):
        hass,saved,runtime=self.setup_case()
        aborted=runtime.br_entities[0];aborted.br_registered=False
        await option.async_set_br_device(hass,saved,False,persist=True)
        self.assertFalse(aborted.removed);self.assertFalse(saved.options['create_device'])
        self.assertNotIn('br',hass.devices.items);self.assertIn('sensor.node',hass.entities.items)

    async def test_http_switch_and_invalid_type(self):
        hass,saved,runtime=self.setup_case()
        view=fixture.frontend.ProxyView(hass,fixture.manager_mod.get_manager(hass))
        response=await view.post(fixture.Request('/api/thread_proxy/create_device',{'entry_id':saved.entry_id,'enabled':False}))
        self.assertEqual(response.status,200);self.assertFalse(saved.options['create_device'])
        response=await view.post(fixture.Request('/api/thread_proxy/create_device',{'entry_id':saved.entry_id,'enabled':'false'}))
        self.assertEqual(response.status,400)

    async def test_http_non_admin_and_foreign_entry_rejected(self):
        hass,saved,runtime=self.setup_case()
        view=fixture.frontend.ProxyView(hass,fixture.manager_mod.get_manager(hass))
        with self.assertRaises(fixture.web.HTTPForbidden):
            await view.post(fixture.Request('/api/thread_proxy/create_device',{'entry_id':saved.entry_id,'enabled':False},False))
        saved.domain='other'
        response=await view.post(fixture.Request('/api/thread_proxy/create_device',{'entry_id':saved.entry_id,'enabled':False}))
        self.assertEqual(response.status,404);self.assertTrue(saved.options['create_device'])
