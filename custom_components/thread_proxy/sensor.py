"""Human-readable BR connection status."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from .const import STATES
from .entity import ProxyEntity
from .br_device import register_br_entities

async def async_setup_entry(hass, entry, async_add_entities):
    register_br_entities(entry.runtime_data, "sensor", lambda runtime: [BRStatus(runtime)], async_add_entities)

class BRStatus(ProxyEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES
    _attr_icon = "mdi:router-network"

    def __init__(self, runtime):
        super().__init__(runtime, "status")

    @property
    def native_value(self):
        return self.runtime.state.status

    @property
    def extra_state_attributes(self):
        return self.runtime.state.public()
