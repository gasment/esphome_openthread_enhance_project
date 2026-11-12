"""Adapter/lifecycle contract tests with explicit HA stand-ins, not a live HA runtime."""
from copy import deepcopy
from enum import IntFlag, StrEnum
import importlib
import sys
import types
import unittest
from unittest.mock import patch

from _load import protocol as p


class BaseEntity:
    added = False
    removed = False
    writes = 0
    async def async_added_to_hass(self): pass
    async def async_will_remove_from_hass(self): pass
    async def async_remove(self): self.added = False; self.removed = True
    def async_write_ha_state(self): self.writes += 1
    def _trigger_event(self, value): self.last_event = value


class Flag(IntFlag):
    TURN_ON=1; TURN_OFF=2; SET_SPEED=4; DIRECTION=8; OSCILLATE=16; PRESET_MODE=32
    OPEN=64; CLOSE=128; SET_POSITION=256; STOP=512; SET_TILT_POSITION=1024; OPEN_TILT=2048; CLOSE_TILT=4096
    TARGET_TEMPERATURE=8192; TARGET_TEMPERATURE_RANGE=16384; TARGET_HUMIDITY=32768; FAN_MODE=65536; SWING_MODE=131072
    FLASH=262144; TRANSITION=524288; EFFECT=1048576


class Mode(StrEnum):
    AUTO='auto'; BOX='box'; SLIDER='slider'; TEXT='text'; PASSWORD='password'
    ONOFF='onoff'; BRIGHTNESS='brightness'; WHITE='white'; RGB='rgb'; RGBW='rgbw'; RGBWW='rgbww'; COLOR_TEMP='color_temp'
    OFF='off'; HEAT_COOL='heat_cool'; COOL='cool'; HEAT='heat'; FAN_ONLY='fan_only'; DRY='dry'
    COOLING='cooling'; HEATING='heating'; IDLE='idle'; DRYING='drying'; FAN='fan'; DEFROSTING='defrosting'
    MEASUREMENT='measurement'; TOTAL='total'; TOTAL_INCREASING='total_increasing'; DIAGNOSTIC='diagnostic'; CONFIG='config'
    CELSIUS='°C'; FAHRENHEIT='°F'


class Registry:
    def __init__(self): self.entities = {}; self.removed = []
    def async_remove(self, entity_id): self.removed.append(entity_id); self.entities.pop(entity_id)


REGISTRY = Registry()
fake_modules = {}
def module(name, **members):
    obj = types.ModuleType(name)
    obj.__dict__.update(members)
    fake_modules[name] = obj
    return obj


module('homeassistant')
module('homeassistant.components')
module('homeassistant.helpers')
module('homeassistant.helpers.entity', Entity=BaseEntity, DeviceInfo=dict)
module('homeassistant.const', EntityCategory=Mode, UnitOfTemperature=Mode)
module('homeassistant.exceptions', HomeAssistantError=RuntimeError)
module('homeassistant.helpers.entity_registry', async_get=lambda _: REGISTRY,
       async_entries_for_config_entry=lambda registry, _: list(registry.entities.values()))
module('homeassistant.helpers.device_registry', async_get=lambda _: types.SimpleNamespace(async_get_device=lambda **_: None))
for platform, cls in {'sensor':'SensorEntity','binary_sensor':'BinarySensorEntity','event':'EventEntity','switch':'SwitchEntity',
                      'number':'NumberEntity','select':'SelectEntity','button':'ButtonEntity','text':'TextEntity',
                      'light':'LightEntity','fan':'FanEntity','cover':'CoverEntity','climate':'ClimateEntity','lock':'LockEntity','valve':'ValveEntity'}.items():
    module('homeassistant.components.'+platform, **{cls:type(cls,(BaseEntity,),{})})
for path, names in {'sensor':['SensorStateClass'], 'number':['NumberMode'], 'text':['TextMode'],
                    'light':['ColorMode'], 'climate':['HVACMode','HVACAction']}.items():
    for name in names: setattr(fake_modules['homeassistant.components.'+path], name, Mode)
for path, name in {'light':'LightEntityFeature','fan':'FanEntityFeature','cover':'CoverEntityFeature',
                   'climate':'ClimateEntityFeature','lock':'LockEntityFeature','valve':'ValveEntityFeature'}.items():
    setattr(fake_modules['homeassistant.components.'+path], name, Flag)
fake_modules['homeassistant.components.fan'].DIRECTION_FORWARD='forward'
fake_modules['homeassistant.components.fan'].DIRECTION_REVERSE='reverse'
def filter_modes(modes):
    if modes == {Mode.WHITE}: raise RuntimeError('HA rejects white-only color mode')
    return modes - {Mode.ONOFF,Mode.BRIGHTNESS} if len(modes)>1 else modes
fake_modules['homeassistant.components.light'].filter_supported_color_modes=filter_modes
with patch.dict(sys.modules, fake_modules):
    entities = importlib.import_module('otgw_under_test.entities')


class FakeHub:
    def __init__(self):
        self.nodes={'0123456789abcdef':{'name':'Door','node_type':'reed','values':{},'selected':None,'manifest':{'entities':[]}}}
        self.calls=[]
    def available(self, uid): return uid in self.nodes
    def exposed(self, uid):
        if uid not in self.nodes: return []
        node=self.nodes[uid]
        return [item for item in node['manifest']['entities'] if node['selected'] is None or item['id'] in node['selected']]
    async def command(self, uid, entity, value): self.calls.append((uid,entity,value))


class EntityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global REGISTRY
        REGISTRY=Registry()
        self.hub=FakeHub()
        self.uid='0123456789abcdef'
        self.manager=entities.EntityManager(object(),types.SimpleNamespace(entry_id='entry'),self.hub)
        self.added=[]
        def add(platform, items):
            self.added.extend(items)
            for item in items:
                identity=platform+'.'+item._attr_unique_id
                if identity not in REGISTRY.entities:
                    REGISTRY.entities[identity]=types.SimpleNamespace(entity_id=identity,unique_id=item._attr_unique_id,domain=platform,custom_name='User name')
        for platform in entities.KIND_PLATFORM.values():
            self.manager.adders[platform]=lambda items, platform=platform: add(platform,items)

    def entity(self, kind, caps=None):
        description={'id':kind,'type':kind,'name':'Example','capabilities':caps or {}}
        p.descriptor(description)
        return entities.ENTITY_TYPES[kind](self.manager,self.uid,description)

    async def test_all_fifteen_wire_kinds_construct(self):
        caps={'number':{'min':0,'max':10,'step':1},'select':{'options':['a','b']},'event':{'event_types':['click']},
              'light':{'color_modes':[7],'effects':[],'min_mireds':0,'max_mireds':0},
              'climate':{'modes':[0,2,3],'fan_modes':[2,3],'swing_modes':[0,2],'presets':[0,5],
                         'visual_min_temperature':16,'visual_max_temperature':30,'visual_target_temperature_step':0.5}}
        for kind in entities.ENTITY_TYPES:
            self.assertIsNotNone(self.entity(kind,caps.get(kind)))

    async def test_dynamic_add_remove_update_preserves_registry_identity(self):
        node=self.hub.nodes[self.uid]
        node['manifest']['entities']=[{'id':'temp','type':'numeric','name':'Temperature'},{'id':'door','type':'binary'}]
        await self.manager.async_sync(self.uid)
        self.assertEqual(len(self.added),2)
        original=self.manager.live[(self.uid,'temp')]
        original.added=True
        await self.manager.async_sync(self.uid)
        self.assertIs(self.manager.live[(self.uid,'temp')],original)
        self.assertEqual(len(self.added),2)
        node['manifest']['entities'][0]['name']='Temperature updated'
        await self.manager.async_sync(self.uid)
        self.assertTrue(original.removed)
        self.assertEqual(REGISTRY.entities['sensor.'+self.uid+'_temp'].custom_name,'User name')
        node['selected']=['door']
        await self.manager.async_sync(self.uid)
        self.assertNotIn((self.uid,'temp'),self.manager.live)
        self.assertIn('sensor.'+self.uid+'_temp',REGISTRY.removed)
        node['selected']=None
        await self.manager.async_sync(self.uid)
        self.assertIn((self.uid,'temp'),self.manager.live)
        self.hub.nodes.clear()
        await self.manager.async_sync(self.uid)
        self.assertFalse(self.manager.live)

    async def test_type_change_cleans_old_platform_and_adds_new(self):
        self.hub.nodes[self.uid]['manifest']['entities']=[{'id':'value','type':'numeric'}]
        await self.manager.async_sync(self.uid)
        self.hub.nodes[self.uid]['manifest']['entities']=[{'id':'value','type':'binary'}]
        await self.manager.async_sync(self.uid)
        self.assertIn('sensor.'+self.uid+'_value',REGISTRY.removed)
        self.assertIn('binary_sensor.'+self.uid+'_value',REGISTRY.entities)

    async def test_cover_fan_light_and_climate_unit_mapping(self):
        cover=self.entity('cover',{'position':True})
        await cover.async_set_cover_position(position=25)
        self.assertEqual(self.hub.calls[-1][2],{'position':0.25})
        fan=self.entity('fan',{'speed':True,'speed_count':3})
        await fan.async_set_percentage(50)
        self.assertEqual(self.hub.calls[-1][2],{'state':True,'speed':2})
        light=self.entity('light',{'color_modes':[35],'min_mireds':0,'max_mireds':0})
        await light.async_turn_on(brightness=128,rgb_color=(255,0,128),transition=0.2)
        command=self.hub.calls[-1][2]
        self.assertEqual(command['transition_length'],200)
        self.assertEqual(command['red'],1)
        self.assertAlmostEqual(command['brightness'],128/255)
        climate=self.entity('climate',{'modes':[0,2,3],'fan_modes':[2,3],'visual_min_temperature':16,'visual_max_temperature':30,'visual_target_temperature_step':0.5})
        await climate.async_set_temperature(temperature=22,hvac_mode=Mode.HEAT)
        self.assertEqual(self.hub.calls[-1][2],{'target_temperature':22,'mode':3})
        await climate.async_set_fan_mode('low')
        self.assertEqual(self.hub.calls[-1][2],{'fan_mode':3})

    async def test_event_arriving_before_platform_add_is_buffered(self):
        event=self.entity('event',{'event_types':['click']})
        event.receive_event('click')
        self.assertEqual(event.pending_events,['click'])
        await event.async_added_to_hass()
        self.assertEqual(event.last_event,'click')
        self.assertFalse(event.pending_events)

    async def test_stable_identity_does_not_include_network_or_backend(self):
        sensor=self.entity('numeric')
        self.hub.nodes[self.uid]['network']='other-network'
        self.hub.nodes[self.uid]['name']='Other name'
        other=self.entity('numeric')
        self.assertEqual(sensor._attr_unique_id,other._attr_unique_id)


if __name__=='__main__': unittest.main(verbosity=2)
