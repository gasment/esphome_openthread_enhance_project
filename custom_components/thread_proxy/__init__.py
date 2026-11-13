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


async def async_remove_config_entry_device(hass,config_entry,device_entry):
    """Allow an individual proxied node to be removed without unbinding its BR."""
    from .const import DOMAIN
    from .uplink import hex_value
    if not is_manager(config_entry) or device_entry.config_entry_id!=config_entry.entry_id:
        return False
    manager=get_manager(hass)
    if manager.root_entry is None or manager.root_entry.entry_id!=config_entry.entry_id:
        return False
    identities=[]
    for domain,value in device_entry.identifiers:
        if domain!=DOMAIN or not value.startswith("node_"):continue
        try:identities.append(hex_value(value[5:],12))
        except ValueError:continue
    if len(identities)!=1 or {value for domain,value in device_entry.identifiers if domain==DOMAIN}!={f"node_{identities[0]}"}:return False
    return await manager.nodes.remove_device(identities[0],device_entry)
