"""Control the BR's own diagnostics, never proxied-node devices or entities."""
from __future__ import annotations
import asyncio
from .const import DOMAIN

OPTION = "create_device"
BR_ENTITY_KEYS = ("bound", "api_connected", "proxy_ready", "status")

class BrDeviceError(Exception):
    pass

def br_device_enabled(entry):
    return getattr(entry, "options", {}).get(OPTION, True)

def register_br_entities(runtime, platform, factory, add_entities):
    """Keep BR-only factories separate from any future proxy-node platform data."""
    runtime.br_entity_factories[platform] = (factory, add_entities)
    if runtime.br_device_enabled:
        entities = factory(runtime)
        runtime.br_entities.extend(entities)
        add_entities(entities)

def _registry_plan(hass, entry):
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    identifier = (DOMAIN, entry.entry_id)
    # Do not use an ambiguous global identifier lookup or bulk config-entry removal.
    own_devices = [device for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
                   if identifier in device.identifiers]
    for device in own_devices:
        # HA's true child devices cascade on parent deletion. Refuse that cascade;
        # proxy nodes must remain independent main devices, not children of this
        # optional BR diagnostic device.
        if hasattr(dr, "async_entries_for_parent_device") and dr.async_entries_for_parent_device(devices, device.id):
            raise BrDeviceError("br_device_has_children")
    keys = {f"{entry.entry_id}_{key}" for key in BR_ENTITY_KEYS}
    own_entities = [entity for entity in er.async_entries_for_config_entry(entities, entry.entry_id)
                    if entity.platform == DOMAIN and entity.unique_id in keys]
    return devices, entities, own_devices, own_entities

async def async_set_br_device(hass, entry, enabled=None, *, persist=False):
    """Update only diagnostic entities; keep TCP session and binding untouched."""
    from .manager import get_manager
    if enabled is not None and type(enabled) is not bool:
        raise BrDeviceError("invalid_input")
    runtime = get_manager(hass).runtimes.get(entry.entry_id)
    if runtime is None:
        # Disabled/unloaded entries can save the preference without connecting.
        target = br_device_enabled(entry) if enabled is None else enabled
        if not target:
            devices, entities, own_devices, own_entities = _registry_plan(hass, entry)
            for entity in own_entities: entities.async_remove(entity.entity_id)
            for device in own_devices: devices.async_remove_device(device.id)
        if persist:
            hass.config_entries.async_update_entry(entry, options={**getattr(entry, "options", {}), OPTION: target})
        return
    async with runtime.br_device_lock:
        target = br_device_enabled(entry) if enabled is None else enabled
        if target and not runtime.br_device_enabled:
            created = []
            for factory, add_entities in runtime.br_entity_factories.values():
                items = factory(runtime)
                created.extend(items)
                add_entities(items)
            runtime.br_entities.extend(created)
            runtime.br_device_enabled = True
        elif not target:
            devices, entities, own_devices, own_entities = _registry_plan(hass, entry)
            # A just-enabled entity may still be awaiting HA platform registration.
            try:
                if runtime.br_entities:
                    await asyncio.wait_for(asyncio.gather(*(item.br_added.wait() for item in runtime.br_entities)), 10)
            except TimeoutError as err:
                raise BrDeviceError("br_device_busy") from err
            # Recompute after pending additions; never remove proxy-node entities.
            devices, entities, own_devices, own_entities = _registry_plan(hass, entry)
            for item in tuple(runtime.br_entities):
                if item.br_registered:
                    await item.async_remove(force_remove=True)
            runtime.br_entities.clear()
            for entity in own_entities: entities.async_remove(entity.entity_id)
            for device in own_devices: devices.async_remove_device(device.id)
            runtime.br_device_enabled = False
        if persist:
            hass.config_entries.async_update_entry(entry, options={**getattr(entry, "options", {}), OPTION: target})
