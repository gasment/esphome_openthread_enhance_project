"""Diagnostics omit API encryption keys."""
async def async_get_config_entry_diagnostics(hass, entry):
    return {"binding_source": entry.data.get("binding_source", "manual"),
            "br": entry.runtime_data.state.public()}
