"""Separate bound, TCP API connected and proxy verified indicators."""
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from .entity import ProxyEntity
from .node_entity import NodeBinarySensor

async def async_setup_entry(hass, entry, async_add_entities):
    await entry.runtime_data.register_platform("binary_sensor",
                         lambda runtime: [BRFlag(runtime, key) for key in ("bound", "api_connected", "proxy_ready")], async_add_entities)
    await entry.runtime_data.nodes.register_platform("binary_sensor",NodeBinarySensor,async_add_entities)

class BRFlag(ProxyEntity, BinarySensorEntity):
    def __init__(self, runtime, key):
        super().__init__(runtime, key)
        self.key = key
        self._attr_icon = "mdi:link-variant" if key == "bound" else "mdi:lan-connect"
        if key != "bound":
            self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self):
        if self.key == "bound":
            return True
        return self.runtime.state.connected if self.key == "api_connected" else self.runtime.state.proxy_ready
