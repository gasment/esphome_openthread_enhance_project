"""Thread SED light entities."""
from homeassistant.components.light import LightEntity, ColorMode, LightEntityFeature
from aioesphomeapi.model import ColorMode as NativeColorMode
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'light', NativeEntity)

class NativeEntity(ThreadEntity, LightEntity):
    def _ha_mode(self, mode):
        mode=int(mode)
        if mode == 39: return ColorMode.RGBW
        if mode in (47,51): return ColorMode.RGBWW
        if mode&32: return ColorMode.RGB
        if mode&24: return ColorMode.COLOR_TEMP
        if mode&2: return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF
    @property
    def is_on(self): return self.current.state if self.current else None
    @property
    def supported_color_modes(self):
        result=set()
        for mode in self.info.supported_color_modes:
            result.add(self._ha_mode(mode))
        if len(result)>1: result.discard(ColorMode.ONOFF); result.discard(ColorMode.BRIGHTNESS)
        return result or {ColorMode.ONOFF}
    @property
    def color_mode(self):
        if not self.current: return next(iter(self.supported_color_modes))
        return self._ha_mode(self.current.color_mode)
    @property
    def brightness(self): return round(self.current.brightness*255) if self.current else None
    @property
    def rgb_color(self): return tuple(round(x*255) for x in (self.current.red,self.current.green,self.current.blue)) if self.current else None
    @property
    def rgbw_color(self): return (*self.rgb_color,round(self.current.white*255)) if self.current else None
    @property
    def rgbww_color(self): return (*self.rgb_color,round(self.current.cold_white*255),round(self.current.warm_white*255)) if self.current else None
    @property
    def color_temp_kelvin(self): return round(1000000/self.current.color_temperature) if self.current and self.current.color_temperature else None
    @property
    def min_color_temp_kelvin(self): return round(1000000/self.info.max_mireds) if self.info.max_mireds else 2000
    @property
    def max_color_temp_kelvin(self): return round(1000000/self.info.min_mireds) if self.info.min_mireds else 6500
    @property
    def effect_list(self): return self.info.effects
    @property
    def effect(self): return self.current.effect if self.current else None
    @property
    def supported_features(self):
        return LightEntityFeature.TRANSITION | LightEntityFeature.FLASH | (LightEntityFeature.EFFECT if self.info.effects else 0)
    async def async_turn_on(self, **kwargs):
        args={'state':True}
        if 'brightness' in kwargs: args['brightness']=kwargs['brightness']/255
        if 'rgb_color' in kwargs:
            args['rgb']=tuple(x/255 for x in kwargs['rgb_color'])
            args['color_mode']=NativeColorMode.RGB
        if 'rgbw_color' in kwargs:
            args['rgb']=tuple(x/255 for x in kwargs['rgbw_color'][:3])
            args['white']=kwargs['rgbw_color'][3]/255
            args['color_mode']=NativeColorMode.RGB_WHITE
        if 'rgbww_color' in kwargs:
            args['rgb']=tuple(x/255 for x in kwargs['rgbww_color'][:3])
            args['cold_white']=kwargs['rgbww_color'][3]/255
            args['warm_white']=kwargs['rgbww_color'][4]/255
            args['color_mode']=NativeColorMode.RGB_COLD_WARM_WHITE if 51 in self.info.supported_color_modes else NativeColorMode.RGB_COLOR_TEMPERATURE
        if 'color_temp_kelvin' in kwargs:
            args['color_temperature']=1000000/kwargs['color_temp_kelvin']
            args['color_mode']=NativeColorMode.COLOR_TEMPERATURE
        if 'effect' in kwargs: args['effect']=kwargs['effect']
        if 'transition' in kwargs: args['transition_length']=kwargs['transition']
        if 'flash' in kwargs: args['flash_length']=.5 if kwargs['flash']=='short' else 2
        self.command(**args)
    async def async_turn_off(self, **kwargs): self.command(state=False,transition_length=kwargs.get('transition'))

