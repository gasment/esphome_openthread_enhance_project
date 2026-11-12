"""Thread SED text entities."""
from homeassistant.components.text import TextEntity, TextMode
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'text', NativeEntity)

class NativeEntity(ThreadEntity, TextEntity):
    @property
    def native_value(self): return self.current.state if self.current and not getattr(self.current,'missing_state',False) else None
    @property
    def native_min(self): return self.info.min_length
    @property
    def native_max(self): return self.info.max_length
    @property
    def pattern(self): return self.info.pattern or None
    @property
    def mode(self): return TextMode.PASSWORD if self.info.mode == 1 else TextMode.TEXT
    async def async_set_value(self, value): self.command(state=value)

