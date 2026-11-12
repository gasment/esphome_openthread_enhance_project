"""OpenThread application gateway, independent of any particular border router."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PLATFORMS
from .coordinator import GatewayCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass, entry: ConfigEntry):
    coordinator = GatewayCoordinator(hass, entry)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, [Platform(name) for name in PLATFORMS])
    except Exception as err:
        await coordinator.async_stop()
        raise ConfigEntryNotReady(str(err)) from err
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, [Platform(name) for name in PLATFORMS]):
        return False
    await entry.runtime_data.async_stop()
    return True


async def async_remove_config_entry_device(hass, entry, device_entry):
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return False
    for domain, uid in device_entry.identifiers:
        if domain == DOMAIN and uid in coordinator.hub.nodes:
            await coordinator.hub.remove(uid)
    return True
