"""Thread SED select entities."""
from homeassistant.components.select import SelectEntity
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'select', NativeEntity)

class NativeEntity(ThreadEntity, SelectEntity):
    @property
    def options(self): return self.info.options
    @property
    def current_option(self): return self.current.state if self.current else None
    async def async_select_option(self, option): self.command(state=option)

