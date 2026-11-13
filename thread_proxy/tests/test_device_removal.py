"""HA per-device removal and stable-key revival; explicit registry stand-ins."""
import asyncio
import importlib
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import types
import unittest
import test_proxy as fixture
import test_br_device as device_fixture
import test_nodes as node_fixture
from aioesphomeapi import api_pb2 as pb

fixture.module('homeassistant.const',EVENT_HOMEASSISTANT_STOP='homeassistant_stop')
spec=importlib.util.spec_from_loader('proxy_fixture.integration_lifecycle',SourceFileLoader('proxy_fixture.integration_lifecycle',str(fixture.PACKAGE/'__init__.py')),is_package=False)
integration=importlib.util.module_from_spec(spec);spec.loader.exec_module(integration)
catalog=importlib.import_module('proxy_fixture.catalog')

class Devices:
    def __init__(self,items):self.items={item.id:item for item in items};self.deleted={}
    def async_get(self,identity):return self.items.get(identity)
    def async_remove_device(self,identity):self.deleted[identity]=self.items.pop(identity)
    def register(self,owner,info):
        match=lambda item:item.config_entry_id==owner and item.identifiers==info['identifiers']
        current=next((item for item in self.items.values() if match(item)),None)
        if current:return current
        old=next((item for item in self.deleted.values() if match(item)),None)
        if old:self.deleted.pop(old.id);self.items[old.id]=old;return old
        item=types.SimpleNamespace(id='restored-new-device',config_entry_id=owner,identifiers=info['identifiers'])
        self.items[item.id]=item;return item

class Entities:
    def __init__(self):self.items={};self.deleted={}
    def async_get_entity_id(self,domain,platform,uid):
        return next((key for key,item in self.items.items() if item.platform==platform and item.unique_id==uid and key.startswith(domain+'.')),None)
    def async_remove(self,identity):self.deleted[identity]=self.items.pop(identity)
    def register(self,entity,device):
        old=next((item for item in self.deleted.values() if item.unique_id==entity.unique_id),None)
        item=old or types.SimpleNamespace(entity_id=f'{entity.platform_domain}.original_{entity.key[0].lower()}',unique_id=entity.unique_id,platform='thread_proxy',config_entry_id='root',device_id=device.id)
        self.deleted.pop(item.entity_id,None);self.items[item.entity_id]=item;return item

class RemovalTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self):
        self.hass=fixture.hass();self.hass.async_create_task=lambda coro,name=None:asyncio.create_task(coro,name=name)
        self.root=types.SimpleNamespace(entry_id='root',data={'kind':'manager'})
        self.hass.bus=types.SimpleNamespace(async_fire=lambda *args:None)
        self.manager=fixture.manager_mod.get_manager(self.hass);self.manager.root_entry=self.root
        self.registry=self.manager.nodes;await self.registry.load(self.root)
        self.device=types.SimpleNamespace(id='original-node-id',config_entry_id='root',identifiers={('thread_proxy','node_'+node_fixture.MAC)},name_by_user='My door',area_id='hall',labels={'battery'})
        self.br=types.SimpleNamespace(id='br',config_entry_id='root',identifiers={('thread_proxy','br-binding')})
        self.other=types.SimpleNamespace(id='other',config_entry_id='root',identifiers={('thread_proxy','node_aabbccddeeff')})
        self.hass.devices=Devices([self.device,self.br,self.other]);self.hass.entities=Entities();self.added=[]
        def add(items):
            for entity in items:
                device=self.hass.devices.register('root',entity.device_info)
                self.hass.entities.register(entity,device);self.added.append(entity)
        self.add=add
        for platform,factory in (('sensor',node_fixture.entities.NodeSensor),('binary_sensor',node_fixture.entities.NodeBinarySensor),('event',node_fixture.entities.NodeEvent)):
            await self.registry.register_platform(platform,factory,add)
        self.registry.accept(node_fixture.descriptor(),'br-a');await self.register()
        self.hass.entities.items['sensor.br']=types.SimpleNamespace(entity_id='sensor.br',unique_id='br-binding_status',config_entry_id='root',platform='thread_proxy',device_id='br')
    async def register(self):
        for entity in self.added:
            if not entity.registered and not entity.added.is_set():
                await entity.async_prepare_to_add_to_hass();await entity.async_added_to_hass()
    async def remove(self):return await integration.async_remove_config_entry_device(self.hass,self.root,self.device)

    async def test_remove_single_node_keeps_br_other_node_and_cache(self):
        await self.prepare();before=[entity.unique_id for entity in self.added]
        self.assertTrue(await self.remove())
        self.assertNotIn(self.device.id,self.hass.devices.items)
        self.assertIn('br',self.hass.devices.items);self.assertIn('other',self.hass.devices.items);self.assertIn('sensor.br',self.hass.entities.items)
        self.assertTrue(self.registry.nodes[node_fixture.MAC].suspended)
        self.assertFalse(self.registry.entities);self.assertEqual(len(self.hass.entities.deleted),3)
        self.assertEqual(self.manager.snapshot()["uplink"]["node_devices"],0)
        self.assertEqual({item.unique_id for item in self.hass.entities.deleted.values()},set(before))

    async def test_next_live_state_restores_same_device_id_entity_ids_and_customization(self):
        await self.prepare();old_ids=set(self.hass.entities.items);await self.remove()
        self.registry.accept(node_fixture.packet([pb.SensorStateResponse(key=1,state=3.4)]),'br-b');await self.register()
        self.assertEqual(set(self.hass.devices.items),{'original-node-id','br','other'})
        self.assertEqual(set(self.hass.entities.items),old_ids)
        self.assertEqual(self.manager.snapshot()["uplink"]["node_devices"],1)
        restored=self.hass.devices.items['original-node-id'];self.assertEqual(restored.name_by_user,'My door');self.assertEqual(restored.area_id,'hall')
        sensor=next(entity for entity in self.registry.entities.values() if entity.key[0]=='Sensor')
        self.assertAlmostEqual(sensor.native_value,3.4,places=5)

    async def test_deleted_node_stays_deleted_after_ha_reload_until_a_live_packet(self):
        await self.prepare();await self.remove();await self.registry.close()
        await self.registry.load(self.root)
        for platform,factory in (('sensor',node_fixture.entities.NodeSensor),('binary_sensor',node_fixture.entities.NodeBinarySensor),('event',node_fixture.entities.NodeEvent)):
            await self.registry.register_platform(platform,factory,self.add)
        self.assertEqual(len(self.added),3);self.assertNotIn(self.device.id,self.hass.devices.items)
        self.registry.accept(node_fixture.packet([pb.SensorStateResponse(key=1,state=3.0)]),'br-b')
        self.assertIn(self.device.id,self.hass.devices.items);self.assertEqual(len(self.added),6)

    async def test_reactivation_keeps_first_key_event(self):
        await self.prepare();await self.remove()
        self.registry.accept(node_fixture.event(),'br-b');await self.register()
        target=next(entity for entity in self.registry.entities.values() if entity.key[0]=='Event')
        self.assertEqual(target.writes,['press'])

    async def test_br_foreign_and_unknown_devices_cannot_use_node_delete(self):
        await self.prepare()
        for device in (self.br,self.other,types.SimpleNamespace(config_entry_id='foreign',identifiers=self.device.identifiers)):
            if device is self.other:
                # It is an owned but offline/stale node, tested separately.
                continue
            self.assertFalse(await integration.async_remove_config_entry_device(self.hass,self.root,device))
        self.assertIn(self.device.id,self.hass.devices.items)

    async def test_offline_node_without_cached_descriptors_can_be_removed(self):
        await self.prepare()
        self.assertTrue(await integration.async_remove_config_entry_device(self.hass,self.root,self.other))
        self.assertNotIn(self.other.id,self.hass.devices.items);self.assertIn(self.device.id,self.hass.devices.items)

    async def test_sleep_hint_does_not_restore_deleted_device(self):
        await self.prepare();await self.remove()
        packet=node_fixture.uplink.NodePacket(node_fixture.MAC,'22'*8,'12345678','11'*16,6,0,b'\x00\x00\x00\x1e')
        self.registry.accept(packet,'br')
        self.assertNotIn(self.device.id,self.hass.devices.items)

    async def test_incoming_packet_during_delete_cannot_recreate_device_mid_cleanup(self):
        await self.prepare();original=self.registry.remove
        async def remove_and_receive(entity):
            self.registry.accept(node_fixture.packet([pb.SensorStateResponse(key=1,state=2.9)]),'br-b')
            return await original(entity)
        self.registry.remove=remove_and_receive
        self.assertTrue(await self.remove());self.assertNotIn(self.device.id,self.hass.devices.items)
        self.registry.remove=original
        self.registry.accept(node_fixture.packet([pb.SensorStateResponse(key=1,state=3.0)]),'br-b')
        self.assertIn(self.device.id,self.hass.devices.items)

    async def test_duplicate_delivery_does_not_resurrect_but_a_new_group_does(self):
        await self.prepare()
        report=node_fixture.packet([pb.SensorStateResponse(key=1,state=3.3)],group='00000010')
        self.manager.receive_node(report,'br-a');await self.remove()
        self.manager.receive_node(report,'br-b')
        self.assertNotIn(self.device.id,self.hass.devices.items)
        new_report=node_fixture.packet([pb.SensorStateResponse(key=1,state=3.4)],group='00000011')
        self.manager.receive_node(new_report,'br-b')
        self.assertIn(self.device.id,self.hass.devices.items)

    async def test_true_child_device_blocks_a_cascading_remove(self):
        await self.prepare()
        self.hass.devices.items['child']=types.SimpleNamespace(id='child',config_entry_id='root',identifiers={('thread_proxy','child')},parent_device_id=self.device.id)
        self.assertFalse(await self.remove());self.assertIn(self.device.id,self.hass.devices.items)
        self.assertTrue(all(entity.registered for entity in self.added))

if __name__=='__main__':unittest.main()
