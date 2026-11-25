"""A separate HA config entry for Summary, preserving previous registry IDs."""
from .catalog import is_summary
from .const import DOMAIN

async def ensure_summary(hass, root):
    existing = next((entry for entry in hass.config_entries.async_entries(DOMAIN)
                     if is_summary(entry) and entry.data.get("manager_entry_id") == root.entry_id), None)
    if existing is None:
        await hass.config_entries.flow.async_init(DOMAIN, context={"source": "import"},
            data={"kind": "summary", "manager_entry_id": root.entry_id})

def migrate_summary(hass, root, summary):
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    devices, entities = dr.async_get(hass), er.async_get(hass)
    identifier = (DOMAIN, "summary_" + root.entry_id)
    device = devices.async_get_device(identifiers={identifier})
    if device is not None and device.config_entry_id == root.entry_id:
        devices.async_update_device(device.id, new_config_entry_id=summary.entry_id,
                                    new_config_subentry_id=None, via_device_id=None)
    for kind in ("offline", "battery", "rssi"):
        unique_id = f"{root.entry_id}_summary_{kind}"
        entity_id = entities.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            continue
        entity = entities.async_get(entity_id)
        if entity is not None and entity.config_entry_id == root.entry_id:
            entities.async_update_entity(entity_id, config_entry_id=summary.entry_id, config_subentry_id=None)
