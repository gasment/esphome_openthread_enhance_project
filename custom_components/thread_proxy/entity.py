"""Diagnostic entities for one BR binding."""
import asyncio
from homeassistant.helpers.entity import Entity, EntityCategory
from .const import DOMAIN

class ProxyEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime, key):
        self.runtime = runtime
        self.br_added = asyncio.Event()
        self.br_registered = False
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = {"identifiers": {(DOMAIN, runtime.entry.entry_id)},
                                  "name": runtime.entry.title, "manufacturer": "ESPHome", "model": "Thread Proxy BR"}

    @property
    def available(self):
        return True  # Disconnected is a useful state, not an unavailable entity.

    async def async_prepare_to_add_to_hass(self):
        # Also settle an add aborted by HA because this diagnostic is disabled.
        self.async_on_remove(self.br_added.set)

    def _br_removed(self):
        self.br_registered = False

    async def async_added_to_hass(self):
        self.br_registered = True
        self.async_on_remove(self._br_removed)
        self.runtime.listeners.add(self.async_write_ha_state)
        self.async_on_remove(lambda: self.runtime.listeners.discard(self.async_write_ha_state))
        self.br_added.set()
