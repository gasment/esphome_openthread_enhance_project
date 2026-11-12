"""Thread SED button entities."""
from homeassistant.components.button import ButtonEntity
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'button', NativeEntity)

class NativeEntity(ThreadEntity, ButtonEntity):
    async def async_press(self): self.command()

