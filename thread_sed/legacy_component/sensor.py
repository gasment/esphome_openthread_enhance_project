"""Thread SED sensor entities."""
from homeassistant.components.sensor import SensorEntity, SensorStateClass
import math
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'sensor', NativeEntity)

class NativeEntity(ThreadEntity, SensorEntity):
    @property
    def native_value(self):
        value = self.current
        if value is None or getattr(value,'missing_state',False): return None
        return None if isinstance(value.state,float) and not math.isfinite(value.state) else value.state
    @property
    def state_class(self):
        return {1:SensorStateClass.MEASUREMENT,2:SensorStateClass.TOTAL_INCREASING,3:SensorStateClass.TOTAL}.get(getattr(self.info,'state_class',0))
    @property
    def suggested_display_precision(self):
        return getattr(self.info,'accuracy_decimals',None)

