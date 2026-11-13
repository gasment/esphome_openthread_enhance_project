"""Read-only projections of Native API records received through any bound BR."""
import asyncio
from enum import Enum
import math
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.event import EventEntity
from homeassistant.helpers.entity import Entity, DeviceInfo, EntityCategory
from .const import DOMAIN

def safe(value):
    if isinstance(value,Enum):return value.name.lower()
    if isinstance(value,dict):return {k:safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [safe(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):return None
    return value

class NodeEntity(Entity):
    _attr_should_poll=False
    _attr_has_entity_name=True
    def __init__(self,registry,node,key,info):
        self.registry=registry;self.node=node;self.key=key;self.initial_info=info
        self._attr_unique_id=f"node_{node.mac}_{key[0].lower()}_{key[1]}_{key[2]}"
        self._attr_entity_registry_enabled_default=not info.disabled_by_default
        category=getattr(info.entity_category,"value",0)
        self._attr_entity_category={1:EntityCategory.CONFIG,2:EntityCategory.DIAGNOSTIC}.get(category)
        self.added=asyncio.Event();self.registered=False
    @property
    def info(self):return self.node.infos.get(self.key,self.initial_info)
    @property
    def name(self):return self.info.name or self.info.object_id.replace("_"," ")
    @property
    def icon(self):return self.info.icon or None
    @property
    def device_info(self):
        info=self.node.info
        return DeviceInfo(identifiers={(DOMAIN,f"node_{self.node.mac}")},
                          name=info.friendly_name or info.name or self.node.mac,
                          manufacturer=info.manufacturer or "ESPHome",model=info.model or None,sw_version=info.esphome_version or None)
    @property
    def value(self):return self.node.states.get(self.key)
    @property
    def extra_state_attributes(self):
        value=self.value
        attributes=safe(value.to_dict()) if value else {}
        for key in ("key","device_id","state","missing_state"):attributes.pop(key,None)
        return attributes or None
    async def async_prepare_to_add_to_hass(self):
        await super().async_prepare_to_add_to_hass()
        def removed():
            self.added.set()
            if self.key[0]=="Event":self.node.events.pop(self.key,None)
        self.async_on_remove(removed)
    async def async_added_to_hass(self):
        await super().async_added_to_hass();self.registered=True
        self.node.listeners.add(self.updated);self.async_on_remove(lambda:self.node.listeners.discard(self.updated))
        self.added.set();self.updated()
    async def async_will_remove_from_hass(self):
        self.registered=False;await super().async_will_remove_from_hass()
    def updated(self,key=None):
        if self.registered and (key is None or key==self.key):self.async_write_ha_state()

class NodeSensor(NodeEntity,SensorEntity):
    platform_domain="sensor"
    @property
    def native_value(self):
        value=self.value
        if value is None or getattr(value,"missing_state",False):return None
        if self.key[0] in ("Cover","Valve"):return safe(value.position*100)
        if self.key[0]=="Climate":return safe(value.current_temperature)
        return safe(getattr(value,"state",None))
    @property
    def native_unit_of_measurement(self):
        if self.key[0] in ("Cover","Valve"):return "%"
        if self.key[0]=="Climate":return "°C"
        return getattr(self.info,"unit_of_measurement",None) or None
    @property
    def device_class(self):
        if self.key[0]=="Climate":return "temperature"
        return getattr(self.info,"device_class",None) or None if self.key[0] in ("Sensor","Number") else None
    @property
    def state_class(self):
        value=getattr(self.info,"state_class",None)
        return value.name.lower() if value and getattr(value,"value",0) else None
    @property
    def suggested_display_precision(self):return getattr(self.info,"accuracy_decimals",None)
    @property
    def force_update(self):return getattr(self.info,"force_update",False)

class NodeBinarySensor(NodeEntity,BinarySensorEntity):
    platform_domain="binary_sensor"
    @property
    def is_on(self):
        value=self.value
        return None if value is None or getattr(value,"missing_state",False) else value.state
    @property
    def device_class(self):return (getattr(self.info,"device_class",None) or None) if self.key[0]=="BinarySensor" else None

class NodeEvent(NodeEntity,EventEntity):
    platform_domain="event"
    @property
    def event_types(self):return self.info.event_types
    @property
    def device_class(self):return self.info.device_class or None
    def updated(self,key=None):
        if not self.registered or key is not None and key!=self.key:return
        pending=self.node.events.pop(self.key,[])
        if not pending:return
        for event_type in pending:
            self._trigger_event(event_type)
            self.async_write_ha_state()
