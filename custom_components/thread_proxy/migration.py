"""Consolidate legacy BR config entries while preserving registry identities."""
from .catalog import Binding, MANAGER_ID, is_manager
from .const import DOMAIN, NAME

async def async_consolidate(hass):
    entries = [entry for entry in hass.config_entries.async_entries(DOMAIN)
               if getattr(entry, "source", None) != "ignore"]
    root = next((entry for entry in entries if is_manager(entry)), None)
    legacy = [entry for entry in entries if "host" in entry.data]
    if root is None and not legacy: return None
    if root is None:
        root = next((entry for entry in legacy if not getattr(entry, "disabled_by", None)), legacy[0])
    records = {record["id"]: record for record in root.data.get("bindings", [])}
    pending = set(root.data.get("migration_pending", []))
    for entry in legacy:
        if entry.entry_id not in records:
            data = {**entry.data, "enabled": not bool(getattr(entry, "disabled_by", None))}
            binding = Binding(entry.entry_id, entry.title, data, entry.unique_id or "",
                              dict(entry.options), root.entry_id)
            records[entry.entry_id] = binding.public_record()
        if entry.entry_id != root.entry_id: pending.add(entry.entry_id)
    # Save every BR before moving registries or deleting any old entry.
    hass.config_entries.async_update_entry(root, title=root.title if is_manager(root) else NAME, unique_id=MANAGER_ID,
        data={"kind": "manager", "bindings": list(records.values()), "migration_pending": sorted(pending)},
        options={}, version=2)
    if pending:
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        devices, entities = dr.async_get(hass), er.async_get(hass)
        for identity in sorted(pending):
            old = hass.config_entries.async_get_entry(identity)
            if old is None: continue
            # Moving the entry owner keeps entity_id, unique_id, device id, names,
            # user settings and any proxied-node records; no bulk deletion.
            for device in dr.async_entries_for_config_entry(devices, identity):
                devices.async_update_device(device.id, new_config_entry_id=root.entry_id)
            for entity in er.async_entries_for_config_entry(entities, identity):
                entities.async_update_entity(entity.entity_id, config_entry_id=root.entry_id)
            hass.config_entries.async_update_entry(old,
                data={"kind": "migrated", "manager": root.entry_id}, version=2)
    return root

async def async_finish_migration(hass, root):
    remaining = []
    for identity in root.data.get("migration_pending", []):
        old = hass.config_entries.async_get_entry(identity)
        if old is None: continue
        if old.data.get("kind") != "migrated" or old.data.get("manager") != root.entry_id:
            remaining.append(identity); continue
        result = await hass.config_entries.async_remove(identity)
        if result.get("require_restart"): remaining.append(identity)
    if remaining != root.data.get("migration_pending", []):
        hass.config_entries.async_update_entry(root, data={**root.data, "migration_pending": remaining})
