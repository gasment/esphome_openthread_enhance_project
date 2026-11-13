"""Diagnostics expose live BR state, never binding records or saved keys."""
async def async_get_config_entry_diagnostics(hass, entry):
    if hasattr(entry.runtime_data, "snapshot"):
        return entry.runtime_data.snapshot()
    return {"binding_source": entry.data.get("binding_source", "manual"), "br": entry.runtime_data.state.public()}
