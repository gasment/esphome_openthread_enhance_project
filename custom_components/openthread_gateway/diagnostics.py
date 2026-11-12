"""Diagnostics exclude binding secrets, tokens, raw packets and sensor values."""
async def async_get_config_entry_diagnostics(hass, entry):
    return entry.runtime_data.diagnostics()
