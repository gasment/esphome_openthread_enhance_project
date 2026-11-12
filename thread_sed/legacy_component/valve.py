"""Thread SED valve entities."""
from homeassistant.components.valve import ValveEntity, ValveEntityFeature
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'valve', NativeEntity)

class NativeEntity(ThreadEntity, ValveEntity):
    @property
    def supported_features(self):
        features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
        if self.info.supports_position: features |= ValveEntityFeature.SET_POSITION
        if self.info.supports_stop: features |= ValveEntityFeature.STOP
        return features
    @property
    def current_valve_position(self): return round(self.current.position*100) if self.current else None
    @property
    def is_closed(self): return self.current.position == 0 if self.current else None
    @property
    def is_opening(self): return self.current.current_operation == 1 if self.current else None
    @property
    def is_closing(self): return self.current.current_operation == 2 if self.current else None
    async def async_open_valve(self): self.command(position=1.0)
    async def async_close_valve(self): self.command(position=0.0)
    async def async_stop_valve(self): self.command(stop=True)
    async def async_set_valve_position(self, position): self.command(position=position/100)

