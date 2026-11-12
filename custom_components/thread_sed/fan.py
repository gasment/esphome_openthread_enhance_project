"""Thread SED fan entities."""
from homeassistant.components.fan import FanEntity, FanEntityFeature
from aioesphomeapi.model import FanDirection
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'fan', NativeEntity)

class NativeEntity(ThreadEntity, FanEntity):
    @property
    def is_on(self): return self.current.state if self.current else None
    @property
    def supported_features(self):
        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self.info.supports_speed: features |= FanEntityFeature.SET_SPEED
        if self.info.supports_oscillation: features |= FanEntityFeature.OSCILLATE
        if self.info.supports_direction: features |= FanEntityFeature.DIRECTION
        if self.info.supported_preset_modes: features |= FanEntityFeature.PRESET_MODE
        return features
    @property
    def percentage(self):
        return round(self.current.speed_level*100/max(1,self.info.supported_speed_count)) if self.current else None
    @property
    def oscillating(self): return self.current.oscillating if self.current else None
    @property
    def current_direction(self): return ('reverse' if self.current.direction == 1 else 'forward') if self.current else None
    @property
    def preset_modes(self): return self.info.supported_preset_modes
    @property
    def preset_mode(self): return self.current.preset_mode if self.current else None
    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        args = {'state':True}
        if percentage is not None: args['speed_level'] = round(percentage*self.info.supported_speed_count/100)
        if preset_mode is not None: args['preset_mode'] = preset_mode
        self.command(**args)
    async def async_turn_off(self, **kwargs): self.command(state=False)
    async def async_set_percentage(self, percentage):
        self.command(state=percentage>0,speed_level=round(percentage*self.info.supported_speed_count/100))
    async def async_oscillate(self, oscillating): self.command(oscillating=oscillating)
    async def async_set_direction(self, direction): self.command(direction=FanDirection.REVERSE if direction=='reverse' else FanDirection.FORWARD)
    async def async_set_preset_mode(self, preset_mode): self.command(preset_mode=preset_mode)

