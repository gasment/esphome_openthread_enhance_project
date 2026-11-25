"""Human-readable BR connection status."""
from datetime import timedelta
from homeassistant.core import callback
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from .summary import calculate, registered_nodes
from .const import DOMAIN
from .catalog import is_summary
from .const import STATES
from .entity import ProxyEntity
from .node_entity import NodeSensor

async def async_setup_entry(hass, entry, async_add_entities):
    if is_summary(entry):
        async_add_entities([SummarySensor(entry.runtime_data, "offline", entry), SummarySensor(entry.runtime_data, "battery", entry)])
        return
    await entry.runtime_data.register_platform("sensor", lambda runtime: [BRStatus(runtime)], async_add_entities)
    await entry.runtime_data.nodes.register_platform("sensor",NodeSensor,async_add_entities)

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


class SummarySensor(SensorEntity):
    """Independent Summary device, alongside BRs and proxied nodes."""
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager, kind, entry=None):
        self.manager, self.kind, self.summary_entry = manager, kind, entry
        self._attr_unique_id = f"{manager.root_entry.entry_id}_summary_{kind}"
        self._attr_translation_key = "summary_offline" if kind == "offline" else "summary_battery"
        self._attr_icon = "mdi:lan-disconnect" if kind == "offline" else "mdi:battery-alert"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "summary_" + manager.root_entry.entry_id)},
            name="Summary", manufacturer="Thread Proxy", model="Node statistics")
        self.stats = calculate({}, set(), manager.summary_settings)

    @property
    def native_value(self):
        key = "suspect_offline" if self.kind == "offline" else "suspect_low_battery"
        return f"{int(self.stats[key])}/{int(self.stats['total'])}"

    @property
    def extra_state_attributes(self):
        keys = ("suspect_offline", "total", "online") if self.kind == "offline" else ("suspect_low_battery", "total", "battery_healthy")
        return {key: self.stats[key] for key in keys}

    @callback
    def refresh(self, event=None):
        root = self.manager.root_entry
        self._attr_available = root is not None
        self.stats = calculate(self.manager.nodes.nodes,
            registered_nodes(self.hass, root.entry_id) if root is not None else set(), self.manager.summary_settings)
        if self.summary_entry is not None:
            total = int(self.stats["total"])
            title = f"Summary · 离线 {int(self.stats['suspect_offline'])}/{total} · 低电量 {int(self.stats['suspect_low_battery'])}/{total}"
            if root is None: title = "Summary · 管理服务未加载"
            if self.summary_entry.title != title:
                self.hass.config_entries.async_update_entry(self.summary_entry, title=title)
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        from homeassistant.helpers.event import async_track_time_interval
        from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
        await super().async_added_to_hass()
        self.manager.nodes.listeners.add(self.refresh)
        self.async_on_remove(lambda: self.manager.nodes.listeners.discard(self.refresh))
        self.async_on_remove(async_track_time_interval(self.hass, self.refresh, timedelta(seconds=30)))
        self.async_on_remove(self.hass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, self.refresh))
        self.refresh()
