"""Thread SED number entities."""
from homeassistant.components.number import NumberEntity
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'number', NativeEntity)

class NativeEntity(ThreadEntity, NumberEntity):
    @property
    def native_value(self): return self.current.state if self.current and not self.current.missing_state else None
    @property
    def native_min_value(self): return self.info.min_value
    @property
    def native_max_value(self): return self.info.max_value
    @property
    def native_step(self): return self.info.step
    async def async_set_native_value(self, value): self.command(state=value)

