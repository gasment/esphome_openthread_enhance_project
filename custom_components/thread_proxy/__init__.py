"""Thread Proxy: BR discovery, binding and connection management."""
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from .const import DOMAIN, PLATFORMS
from .manager import get_manager

async def async_setup(hass, config):
    from .frontend import async_setup_frontend
    manager = get_manager(hass)
    await async_setup_frontend(hass, manager)
    await manager.start_discovery()
    async def shutdown(event):
        await manager.close()
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    return True

async def async_setup_entry(hass, entry):
    manager = get_manager(hass)
    await manager.add_entry(entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await manager.remove_entry(entry.entry_id)
        raise
    entry.async_on_unload(entry.add_update_listener(_updated))
    return True

async def _updated(hass, entry):
    runtime = get_manager(hass).runtimes.get(entry.entry_id)
    # Assigning a verified unique_id must not tear down a healthy connection.
    if runtime is not None and (runtime.data != dict(entry.data) or runtime.state.name != entry.title):
        await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await get_manager(hass).remove_entry(entry.entry_id)
    return True
