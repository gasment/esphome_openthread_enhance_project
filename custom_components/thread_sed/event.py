"""Thread SED event entities."""
from homeassistant.components.event import EventEntity
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'event', NativeEntity)

class NativeEntity(ThreadEntity, EventEntity):
    @property
    def event_types(self): return self.info.event_types
    def update_from_state(self):
        if self.current is not None and self.current is not getattr(self,'_last_event',None):
            self._last_event = self.current
            self._trigger_event(self.current.event_type)

