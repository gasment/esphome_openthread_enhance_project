"""One manager entry, optional BR diagnostics, independent proxy connections."""
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from .const import PLATFORMS
from .manager import get_manager
from .catalog import is_manager
from .br_device import async_set_br_device, br_device_enabled
from .migration import async_consolidate, async_finish_migration

async def async_setup(hass, config):
    from .frontend import async_setup_frontend
    await async_consolidate(hass)
    manager = get_manager(hass)
    await async_setup_frontend(hass, manager)
    await manager.start_discovery()
    async def shutdown(event): await manager.close()
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    return True

async def async_migrate_entry(hass, entry):
    await async_consolidate(hass)
    return entry.version == 2

async def async_setup_entry(hass, entry):
    if not is_manager(entry): return True  # Migrated tombstone, removed below.
    manager = get_manager(hass)
    await manager.setup_root(entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        for binding in manager.entries():
            if not br_device_enabled(binding): await async_set_br_device(hass, binding)
        await async_finish_migration(hass, entry)
    except BaseException:
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        await manager.unload_root()
        raise
    entry.async_on_unload(entry.add_update_listener(_updated))
    return True

async def _updated(hass, entry):
    if is_manager(entry): await get_manager(hass).sync_root(entry)

async def async_unload_entry(hass, entry):
    if not is_manager(entry): return True
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS): return False
    await get_manager(hass).unload_root()
    return True
