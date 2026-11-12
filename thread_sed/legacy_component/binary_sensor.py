"""Thread SED binary_sensor entities."""
from homeassistant.components.binary_sensor import BinarySensorEntity
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'binary_sensor', NativeEntity)

class NativeEntity(ThreadEntity, BinarySensorEntity):
    @property
    def is_on(self):
        return self.current.state if self.current and not self.current.missing_state else None

