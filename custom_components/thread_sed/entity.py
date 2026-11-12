"""Native API descriptors mapped directly to Home Assistant entities."""
from __future__ import annotations
import asyncio
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN
from .session import domain_of

class ThreadEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    def __init__(self, session, info):
        self.session, self.info = session, info
        self.identity = (info.device_id, info.key)
        self._attr_unique_id = f'{session.mac}_{info.device_id}_{info.key}'
        self._attr_name = info.name or None
        self._attr_icon = info.icon or None
        self._attr_entity_registry_enabled_default = not info.disabled_by_default
        self._attr_entity_category = {1:EntityCategory.CONFIG, 2:EntityCategory.DIAGNOSTIC}.get(info.entity_category)
        device = session.info
        self._attr_device_info = {'identifiers':{(DOMAIN,session.mac)}, 'name':session.entry.title,
            'manufacturer':getattr(device,'manufacturer','ESPHome'),
            'model':getattr(device,'model','Thread SED'), 'sw_version':getattr(device,'esphome_version',None)}
        self._attr_device_class = getattr(info,'device_class','') or None
        self._attr_native_unit_of_measurement = getattr(info,'unit_of_measurement','') or None

    @property
    def current(self):
        return self.session.states.get(self.identity)

    @property
    def available(self):
        return self.session.available and (self.current is not None or domain_of(self.info) == 'button')

    async def async_added_to_hass(self):
        def update(identity):
            if identity is None or identity == self.identity:
                self.update_from_state()
                self.async_write_ha_state()
        self.session.callbacks.add(update)
        self.async_on_remove(lambda: self.session.callbacks.discard(update))
        self.update_from_state()

    def update_from_state(self):
        pass

    def command(self, kind=None, **kwargs):
        self.session.command(kind or domain_of(self.info), self.info.key, self.info.device_id, **kwargs)

async def setup_platform(entry, add_entities, domain, entity_class):
    session = entry.runtime_data
    known = {}
    lock = asyncio.Lock()
    tasks = set()
    async def _reconcile():
        if not session.schema:
            return
        descriptors = { (i.device_id,i.key):i for i in session.entities if domain_of(i) == domain }
        registry = er.async_get(session.hass)
        valid_ids = {f'{session.mac}_{i.device_id}_{i.key}' for i in descriptors.values()}
        for key in list(known):
            if key not in descriptors or known[key].info != descriptors[key]:
                await known.pop(key).async_remove()
        for item in er.async_entries_for_config_entry(registry, entry.entry_id):
            if item.domain == domain and item.unique_id not in valid_ids:
                registry.async_remove(item.entity_id)
        additions = []
        for key, info in descriptors.items():
            if key not in known:
                entity = known[key] = entity_class(session, info)
                additions.append(entity)
            else:
                known[key].info = info
                known[key].update_from_state()
                if known[key].hass: known[key].async_write_ha_state()
        if additions: add_entities(additions)
    async def reconcile():
        async with lock:
            if not session.stopping:
                await _reconcile()
    def changed():
        task = session.hass.async_create_task(reconcile())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    session.schema_callbacks.add(changed)
    entry.async_on_unload(lambda:session.schema_callbacks.discard(changed))
    entry.async_on_unload(lambda:[task.cancel() for task in tuple(tasks)])
    await reconcile()
