"""Thread SED climate entities."""
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode, HVACAction
from homeassistant.const import UnitOfTemperature
from aioesphomeapi.model import ClimateMode, ClimateFanMode, ClimateSwingMode, ClimatePreset
from .entity import ThreadEntity, setup_platform

async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(entry, async_add_entities, 'climate', NativeEntity)

class NativeEntity(ThreadEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _enable_turn_on_off_backwards_compatibility = False
    @property
    def supported_features(self):
        features=ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self.fan_modes: features |= ClimateEntityFeature.FAN_MODE
        if self.swing_modes: features |= ClimateEntityFeature.SWING_MODE
        if self.preset_modes: features |= ClimateEntityFeature.PRESET_MODE
        if self.info.supports_target_humidity or self.info.feature_flags&16: features |= ClimateEntityFeature.TARGET_HUMIDITY
        return features | (ClimateEntityFeature.TARGET_TEMPERATURE_RANGE if self.info.supports_two_point_target_temperature else ClimateEntityFeature.TARGET_TEMPERATURE)
    @property
    def fan_modes(self): return [m.name.lower() for m in self.info.supported_fan_modes]+self.info.supported_custom_fan_modes
    @property
    def fan_mode(self): return (self.current.custom_fan_mode or self.current.fan_mode.name.lower()) if self.current and self.current.fan_mode is not None else None
    @property
    def swing_modes(self): return [m.name.lower() for m in self.info.supported_swing_modes]
    @property
    def swing_mode(self): return self.current.swing_mode.name.lower() if self.current and self.current.swing_mode is not None else None
    @property
    def preset_modes(self): return [m.name.lower() for m in self.info.supported_presets]+self.info.supported_custom_presets
    @property
    def preset_mode(self): return (self.current.custom_preset or self.current.preset.name.lower()) if self.current and self.current.preset is not None else None
    @property
    def hvac_action(self):
        return {0:HVACAction.OFF,2:HVACAction.COOLING,3:HVACAction.HEATING,4:HVACAction.IDLE,5:HVACAction.DRYING,6:HVACAction.FAN,7:HVACAction.DEFROSTING}.get(self.current.action) if self.current else None
    @property
    def current_humidity(self): return self.current.current_humidity if self.current else None
    @property
    def target_humidity(self): return self.current.target_humidity if self.current else None
    @property
    def min_humidity(self): return self.info.visual_min_humidity
    @property
    def max_humidity(self): return self.info.visual_max_humidity
    async def async_set_humidity(self, humidity): self.command(target_humidity=humidity)
    async def async_set_fan_mode(self, fan_mode):
        self.command(**({'custom_fan_mode':fan_mode} if fan_mode in self.info.supported_custom_fan_modes else {'fan_mode':ClimateFanMode[fan_mode.upper()]}))
    async def async_set_swing_mode(self, swing_mode): self.command(swing_mode=ClimateSwingMode[swing_mode.upper()])
    async def async_set_preset_mode(self, preset_mode):
        self.command(**({'custom_preset':preset_mode} if preset_mode in self.info.supported_custom_presets else {'preset':ClimatePreset[preset_mode.upper()]}))
    @property
    def hvac_modes(self):
        modes={0:HVACMode.OFF,1:HVACMode.HEAT_COOL,2:HVACMode.COOL,3:HVACMode.HEAT,4:HVACMode.FAN_ONLY,5:HVACMode.DRY,6:HVACMode.AUTO}
        return [modes[m] for m in self.info.supported_modes if m in modes]
    @property
    def hvac_mode(self):
        return {0:HVACMode.OFF,1:HVACMode.HEAT_COOL,2:HVACMode.COOL,3:HVACMode.HEAT,4:HVACMode.FAN_ONLY,5:HVACMode.DRY,6:HVACMode.AUTO}.get(self.current.mode) if self.current else None
    @property
    def current_temperature(self): return self.current.current_temperature if self.current else None
    @property
    def target_temperature(self): return self.current.target_temperature if self.current else None
    @property
    def target_temperature_low(self): return self.current.target_temperature_low if self.current else None
    @property
    def target_temperature_high(self): return self.current.target_temperature_high if self.current else None
    @property
    def min_temp(self): return self.info.visual_min_temperature
    @property
    def max_temp(self): return self.info.visual_max_temperature
    @property
    def target_temperature_step(self): return self.info.visual_target_temperature_step
    async def async_set_hvac_mode(self, hvac_mode):
        self.command(mode=ClimateMode({'off':0,'heat_cool':1,'cool':2,'heat':3,'fan_only':4,'dry':5,'auto':6}[hvac_mode]))
    async def async_turn_off(self): await self.async_set_hvac_mode(HVACMode.OFF)
    async def async_turn_on(self):
        await self.async_set_hvac_mode(next(m for m in self.hvac_modes if m != HVACMode.OFF))
    async def async_set_temperature(self, **kwargs):
        args={k:kwargs[v] for k,v in {'target_temperature':'temperature','target_temperature_low':'target_temp_low','target_temperature_high':'target_temp_high'}.items() if v in kwargs}
        self.command(**args)

