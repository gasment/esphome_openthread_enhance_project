"""Dynamic fan platform for OpenThread Gateway."""
async def async_setup_entry(hass, entry, async_add_entities):
    await entry.runtime_data.entities.setup_platform('fan', async_add_entities)
