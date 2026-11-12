"""Thread SED cover entities."""
from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'cover', NativeEntity)

class NativeEntity(ThreadEntity, CoverEntity):
    @property
    def supported_features(self):
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        if self.info.supports_position: features |= CoverEntityFeature.SET_POSITION
        if self.info.supports_tilt: features |= CoverEntityFeature.SET_TILT_POSITION
        return features
    @property
    def current_cover_position(self): return round(self.current.position*100) if self.current else None
    @property
    def current_cover_tilt_position(self): return round(self.current.tilt*100) if self.current else None
    @property
    def is_closed(self): return self.current.position == 0 if self.current else None
    @property
    def is_opening(self): return self.current.current_operation == 1 if self.current else None
    @property
    def is_closing(self): return self.current.current_operation == 2 if self.current else None
    async def async_open_cover(self, **kwargs): self.command(position=1.0)
    async def async_close_cover(self, **kwargs): self.command(position=0.0)
    async def async_stop_cover(self, **kwargs): self.command(stop=True)
    async def async_set_cover_position(self, **kwargs): self.command(position=kwargs['position']/100)
    async def async_set_cover_tilt_position(self, **kwargs): self.command(tilt=kwargs['tilt_position']/100)

