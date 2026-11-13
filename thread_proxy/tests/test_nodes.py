"""Real Native API descriptors/states with small explicit HA entity stand-ins."""
import asyncio
from enum import Enum
import importlib
import struct
import types
import unittest
import test_proxy as fixture
from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO

nodes=importlib.import_module('proxy_fixture.nodes')
uplink=importlib.import_module('proxy_fixture.uplink')
TYPE={value:key for key,value in MESSAGE_TYPE_TO_PROTO.items()}
MAC='001122334455'
def packet(messages,schema='11'*16,flags=0,group='00000001'):
    body=b''.join(struct.pack('!HH',TYPE[type(msg)],len(msg.SerializeToString()))+msg.SerializeToString() for msg in messages)
    return uplink.NodePacket(MAC,'22'*8,group,schema,2,flags,body)
def descriptor(*extra,schema='11'*16):
    return packet([pb.DeviceInfoResponse(name='door',friendly_name='Door',mac_address='00:11:22:33:44:55',manufacturer='ESPHome',model='nRF52840'),
                   pb.ListEntitiesSensorResponse(key=1,object_id='voltage',name='Voltage',device_class='voltage',unit_of_measurement='V',accuracy_decimals=2,state_class=1),
                   pb.ListEntitiesBinarySensorResponse(key=2,object_id='contact',name='Contact',device_class='door'),
                   pb.ListEntitiesEventResponse(key=3,object_id='key',name='Key',event_types=['press','hold']),*extra,pb.ListEntitiesDoneResponse()],schema=schema,flags=2)
def event(seq=1,kind='press',schema='11'*16):
    msg=pb.HomeassistantActionRequest(service='thread_sed.batch_event');msg.data.add(key='token',value=f'boot:{seq}:3:{kind}')
    return packet([msg],schema=schema)

class FakeEntity:
    async def async_prepare_to_add_to_hass(self):pass
    async def async_added_to_hass(self):pass
    async def async_will_remove_from_hass(self):pass
    def async_on_remove(self,callback):
        if not hasattr(self,'removers'):self.removers=[]
        self.removers.append(callback)
    def async_write_ha_state(self):
        if not hasattr(self,'writes'):self.writes=[]
        self.writes.append(getattr(self,'last_event',getattr(self,'native_value',getattr(self,'is_on',None))))
    @property
    def unique_id(self):return self._attr_unique_id
    async def async_remove(self,force_remove=False):
        await self.async_will_remove_from_hass()
        for cb in getattr(self,'removers',[]):cb()
class FakeSensor(FakeEntity):pass
class FakeBinary(FakeEntity):pass
class FakeEvent(FakeEntity):
    def _trigger_event(self,event_type):
        if event_type not in self.event_types:raise ValueError('bad event')
        self.last_event=event_type
class Category(Enum):CONFIG='config';DIAGNOSTIC='diagnostic'
fixture.module('homeassistant.helpers.entity',Entity=FakeEntity,DeviceInfo=dict,EntityCategory=Category)
fixture.module('homeassistant.components.sensor',SensorEntity=FakeSensor,SensorDeviceClass=types.SimpleNamespace(ENUM='enum'))
fixture.module('homeassistant.components.binary_sensor',BinarySensorEntity=FakeBinary,BinarySensorDeviceClass=types.SimpleNamespace(CONNECTIVITY='connectivity'))
fixture.module('homeassistant.components.event',EventEntity=FakeEvent)
entities=importlib.import_module('proxy_fixture.node_entity')

class NodesTests(unittest.IsolatedAsyncioTestCase):
    async def setup_registry(self):
        self.hass=fixture.hass();self.hass.async_create_task=lambda coro,name=None:asyncio.create_task(coro,name=name)
        self.registry=nodes.NodeRegistry(self.hass);self.added=[]
        await self.registry.load(types.SimpleNamespace(entry_id='root'))
        for kind,factory in (('sensor',entities.NodeSensor),('binary_sensor',entities.NodeBinarySensor),('event',entities.NodeEvent)):
            await self.registry.register_platform(kind,factory,self.added.extend)
        return self.registry
    async def register(self):
        for entity in self.added:
            if not entity.registered:
                await entity.async_prepare_to_add_to_hass();await entity.async_added_to_hass()
    async def test_descriptors_create_entities_without_a_node_config_entry(self):
        r=await self.setup_registry();r.accept(descriptor(),'br-a')
        self.assertEqual(len(self.added),3);self.assertEqual(len(self.hass.config_entries.items),0)
        self.assertEqual({e.platform_domain for e in self.added},{'sensor','binary_sensor','event'})
        self.assertTrue(all(e.device_info['identifiers']=={('thread_proxy','node_'+MAC)} for e in self.added))
        self.assertTrue(all('via_device' not in e.device_info for e in self.added))
    async def test_states_through_another_br_keep_device_and_entity_identity(self):
        r=await self.setup_registry();r.accept(descriptor(),'br-a');await self.register()
        before=list(self.added);r.accept(packet([pb.SensorStateResponse(key=1,state=3.1),pb.BinarySensorStateResponse(key=2,state=True)]),'br-b')
        self.assertEqual(self.added,before)
        self.assertAlmostEqual(self.added[0].native_value,3.1,places=5)
        binary=next(e for e in self.added if e.platform_domain=='binary_sensor');self.assertTrue(binary.is_on)
    async def test_first_event_waits_for_entity_registration_and_preserves_multiple_events(self):
        r=await self.setup_registry();r.accept(descriptor(),'br-a');r.accept(event(1),'br-a');r.accept(event(2,'hold'),'br-a')
        target=next(e for e in self.added if e.platform_domain=='event')
        self.assertFalse(hasattr(target,'writes'));await self.register()
        self.assertEqual(target.writes,['press','hold'])
        r.accept(event(2,'hold'),'br-b');self.assertEqual(target.writes,['press','hold'])
    async def test_unknown_schema_buffers_event_then_applies_after_descriptors(self):
        r=await self.setup_registry();incoming=event();r.accept(incoming,'br-a')
        self.assertEqual(self.added,[]);self.assertTrue(r.need_schema(incoming));self.assertFalse(r.need_schema(incoming))
        r.accept(descriptor(),'br-b');await self.register()
        target=next(e for e in self.added if e.platform_domain=='event');self.assertEqual(target.writes,['press'])
    async def test_cache_restores_entities_states_and_event_cursor_after_ha_restart(self):
        r=await self.setup_registry();r.accept(descriptor(),'br');r.accept(packet([pb.SensorStateResponse(key=1,state=3.2)]),'br');r.accept(event(),'br')
        await r.close();r2=nodes.NodeRegistry(self.hass);await r2.load(types.SimpleNamespace(entry_id='root'))
        node=r2.nodes[MAC];self.assertAlmostEqual(node.states[('Sensor',0,1)].state,3.2,places=5)
        r2.accept(event(),'other-br');self.assertFalse(node.events)
        self.assertIn('boot:3',node.cursors)
    async def test_switch_and_light_are_read_only_binary_sensors(self):
        r=await self.setup_registry();r.accept(descriptor(pb.ListEntitiesSwitchResponse(key=4,name='Output'),pb.ListEntitiesLightResponse(key=5,name='Light')),'br')
        self.assertEqual([e.platform_domain for e in self.added if e.key[0] in ('Switch','Light')],['binary_sensor','binary_sensor'])
    async def test_renamed_schema_adds_entities_without_restarting_other_br_devices(self):
        r=await self.setup_registry();r.accept(descriptor(),'br');await self.register()
        old=list(self.added);r.accept(descriptor(pb.ListEntitiesTextSensorResponse(key=4,name='Version'),schema='33'*16),'other')
        self.assertEqual(len(self.added),4);self.assertEqual(self.added[:3],old)
    async def test_missing_state_stays_unknown_and_nonfinite_values_are_sanitized(self):
        r=await self.setup_registry();r.accept(descriptor(),'br')
        sensor=next(e for e in self.added if e.key[0]=='Sensor')
        self.assertIsNone(sensor.native_value)
        r.accept(packet([pb.SensorStateResponse(key=1,state=float('nan'))]),'br');self.assertIsNone(sensor.native_value)

class Registry:
    def __init__(self,values):self.items=dict(values);self.removed=[]
    def async_get_entity_id(self,domain,platform,uid):return next((key for key,value in self.items.items() if value==uid),None)
    def async_remove(self,key):self.removed.append(key);self.items.pop(key,None)

class NodeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    setup_registry=NodesTests.setup_registry
    register=NodesTests.register
    async def test_schema_removal_only_removes_own_node_entities(self):
        r=await self.setup_registry();r.accept(descriptor(),'br');await self.register()
        sensor=next(e for e in self.added if e.key[0]=='Sensor')
        target=next(e for e in self.added if e.key[0]=='Event')
        self.hass.entities=Registry({'sensor.br':'binding_status','sensor.voltage':sensor.unique_id,'event.key':target.unique_id})
        removed=packet([pb.DeviceInfoResponse(name='door',mac_address='00:11:22:33:44:55'),pb.ListEntitiesBinarySensorResponse(key=2,name='Contact'),pb.ListEntitiesDoneResponse()],schema='33'*16,flags=2)
        r.accept(removed,'br')
        await asyncio.gather(*r.tasks)
        self.assertEqual(set(self.hass.entities.removed),{'sensor.voltage','event.key'})
        self.assertIn('sensor.br',self.hass.entities.items)
        self.assertEqual(len(r.entities),1)

    async def test_remove_then_readd_waits_for_previous_entity_cleanup(self):
        r=await self.setup_registry();r.accept(descriptor(),'br');await self.register()
        self.hass.entities=Registry({})
        smaller=packet([pb.DeviceInfoResponse(name='door',mac_address='00:11:22:33:44:55'),pb.ListEntitiesBinarySensorResponse(key=2,name='Contact'),pb.ListEntitiesDoneResponse()],schema='33'*16,flags=2)
        r.accept(smaller,'br');r.accept(descriptor(schema='44'*16),'br')
        self.assertEqual(len(self.added),3)
        await asyncio.gather(*r.tasks)
        self.assertEqual(len(self.added),5)
        self.assertEqual(len(r.entities),3)

if __name__=='__main__':unittest.main()
