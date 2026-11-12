"""Check platform command arguments against the real client's public signatures."""
import ast
import inspect
from pathlib import Path
import types
import unittest
from aioesphomeapi import APIClient, model
from test_native import TRANSPORT

class Commands(unittest.IsolatedAsyncioTestCase):
    def entity(self,domain,**attrs):
        calls=[]
        class Base:
            def command(self,kind=None,**kwargs):
                inspect.signature(getattr(APIClient,f'{kind or domain}_command')).bind(None,7,device_id=0,**kwargs)
                calls.append(kwargs)
        source=ast.parse(TRANSPORT.with_name(f'{domain}.py').read_text(encoding='utf-8'))
        tree=ast.Module(body=[n for n in source.body if isinstance(n,ast.ClassDef)],type_ignores=[])
        names={**vars(model),'ThreadEntity':Base,'UnitOfTemperature':types.SimpleNamespace(CELSIUS='°C'),
               'HVACMode':types.SimpleNamespace(OFF='off',AUTO='auto',HEAT='heat',COOL='cool',HEAT_COOL='heat_cool',FAN_ONLY='fan_only',DRY='dry'),
               'NativeColorMode':model.ColorMode}
        for name in ('SwitchEntity','ButtonEntity','NumberEntity','SelectEntity','TextEntity','FanEntity','CoverEntity','ClimateEntity','LightEntity','LockEntity','ValveEntity'):
            names[name]=object
        exec(compile(tree,f'{domain}.py','exec'),names)
        entity=names['NativeEntity'](); entity.info=types.SimpleNamespace(**attrs)
        return entity,calls

    async def test_simple_controls(self):
        for domain,method,argument in [('switch','async_turn_on',None),('button','async_press',None),
            ('number','async_set_native_value',2.5),('select','async_select_option','auto'),('text','async_set_value','hello')]:
            entity,calls=self.entity(domain)
            await getattr(entity,method)(*(() if argument is None else (argument,)))
            self.assertEqual(len(calls),1)
    async def test_light_commands(self):
        entity,calls=self.entity('light',supported_color_modes=[35,39,51])
        await entity.async_turn_on(rgb_color=(255,128,0),brightness=128,transition=.2)
        self.assertEqual(calls[-1]['rgb'],(1,128/255,0))
        await entity.async_turn_on(rgbw_color=(255,0,0,128))
        await entity.async_turn_on(rgbww_color=(255,0,0,128,128))
        await entity.async_turn_on(color_temp_kelvin=4000,effect='rainbow')
        await entity.async_turn_off(transition=.5)
    async def test_fan_commands(self):
        entity,calls=self.entity('fan',supported_speed_count=4)
        await entity.async_turn_on(percentage=50)
        self.assertEqual(calls[-1]['speed_level'],2)
        await entity.async_set_direction('reverse'); await entity.async_oscillate(True)
        await entity.async_set_percentage(25); await entity.async_set_preset_mode('auto')
    async def test_cover_valve_lock_commands(self):
        entity,_=self.entity('cover')
        await entity.async_open_cover(); await entity.async_close_cover(); await entity.async_stop_cover()
        await entity.async_set_cover_position(position=50); await entity.async_set_cover_tilt_position(tilt_position=75)
        entity,_=self.entity('valve')
        await entity.async_open_valve(); await entity.async_close_valve(); await entity.async_stop_valve(); await entity.async_set_valve_position(50)
        entity,_=self.entity('lock')
        await entity.async_lock(code='1234'); await entity.async_unlock(); await entity.async_open()
    async def test_climate_commands(self):
        entity,_=self.entity('climate',supported_custom_fan_modes=['turbo'],supported_custom_presets=['custom'])
        await entity.async_set_hvac_mode('cool'); await entity.async_set_temperature(temperature=25,target_temp_low=20,target_temp_high=28)
        await entity.async_set_humidity(50); await entity.async_set_fan_mode('auto'); await entity.async_set_fan_mode('turbo')
        await entity.async_set_preset_mode('eco'); await entity.async_set_preset_mode('custom'); await entity.async_set_swing_mode('vertical')
