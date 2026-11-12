"""Thread SED lock entities."""
from homeassistant.components.lock import LockEntity, LockEntityFeature
from aioesphomeapi.model import LockCommand
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'lock', NativeEntity)

class NativeEntity(ThreadEntity, LockEntity):
    @property
    def is_locked(self): return self.current.state == 1 if self.current else None
    @property
    def is_locking(self): return self.current.state == 4 if self.current else None
    @property
    def is_unlocking(self): return self.current.state == 5 if self.current else None
    @property
    def is_jammed(self): return self.current.state == 3 if self.current else None
    @property
    def supported_features(self): return LockEntityFeature.OPEN if self.info.supports_open else 0
    async def async_lock(self, **kwargs): self.command(command=LockCommand.LOCK,code=kwargs.get('code'))
    async def async_unlock(self, **kwargs): self.command(command=LockCommand.UNLOCK,code=kwargs.get('code'))
    async def async_open(self, **kwargs): self.command(command=LockCommand.OPEN,code=kwargs.get('code'))

