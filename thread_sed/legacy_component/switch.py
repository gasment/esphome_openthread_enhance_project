"""Thread SED switch entities."""
from homeassistant.components.switch import SwitchEntity
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'switch', NativeEntity)

class NativeEntity(ThreadEntity, SwitchEntity):
    @property
    def is_on(self):
        return self.current.state if self.current else None
    async def async_turn_on(self, **kwargs): self.command(state=True)
    async def async_turn_off(self, **kwargs): self.command(state=False)

