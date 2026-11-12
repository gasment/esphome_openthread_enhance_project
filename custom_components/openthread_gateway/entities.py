"""Dynamic entity lifecycle and adapters for all existing bridge entity kinds."""
from __future__ import annotations
import asyncio
import math
from copy import deepcopy

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.event import EventEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.components.light import LightEntity, LightEntityFeature, ColorMode, filter_supported_color_modes
from homeassistant.components.fan import FanEntity, FanEntityFeature, DIRECTION_FORWARD, DIRECTION_REVERSE
from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode, HVACAction
from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.components.valve import ValveEntity, ValveEntityFeature
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity, DeviceInfo
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN, KIND_PLATFORM
from .protocol import ProtocolError


class EntityManager:
    def __init__(self, hass, entry, hub):
        self.hass, self.entry, self.hub = hass, entry, hub
        self.adders, self.live = {}, {}
        self.lock = asyncio.Lock()

    async def setup_platform(self, platform, add_entities):
        self.adders[platform] = add_entities
        for uid in self.hub.nodes:
            await self.async_sync(uid)

    async def async_sync(self, uid):
        async with self.lock:
            expected = {item["id"]: item for item in self.hub.exposed(uid)}
            registry = er.async_get(self.hass)
            # Registry entries include entities currently disabled by the user.
            for item in er.async_entries_for_config_entry(registry, self.entry.entry_id):
                if not item.unique_id.startswith(uid + "_"):
                    continue
                entity_id = item.unique_id[len(uid) + 1:]
                descriptor = expected.get(entity_id)
                if not descriptor or item.domain != KIND_PLATFORM[descriptor["type"]]:
                    registry.async_remove(item.entity_id)
            for key, entity in list(self.live.items()):
                if key[0] != uid:
                    continue
                descriptor = expected.get(key[1])
                if descriptor != entity.descriptor:
                    if entity.added:
                        await entity.async_remove()
                    self.live.pop(key, None)
            for entity_id, descriptor in expected.items():
                key = (uid, entity_id)
                platform = KIND_PLATFORM[descriptor["type"]]
                if key not in self.live and platform in self.adders:
                    entity = ENTITY_TYPES[descriptor["type"]](self, uid, descriptor)
                    self.live[key] = entity
                    self.adders[platform]([entity])
            if uid not in self.hub.nodes:
                devices = dr.async_get(self.hass)
                device = devices.async_get_device(identifiers={(DOMAIN, uid)})
                if device:
                    devices.async_update_device(device.id, remove_config_entry_id=self.entry.entry_id)

    def notify(self, kind, uid, entity_id, value):
        if kind == "event":
            entity = self.live.get((uid, entity_id))
            if isinstance(entity, RemoteEvent):
                entity.receive_event(value)
            return
        for (device, identity), entity in self.live.items():
            if device == uid and (entity_id is None or entity_id == identity) and entity.added:
                entity.async_write_ha_state()


class RemoteEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, manager, uid, descriptor):
        self.manager, self.hub, self.uid, self.descriptor = manager, manager.hub, uid, deepcopy(descriptor)
        self.identity, self.caps = self.descriptor["id"], self.descriptor.get("capabilities", {})
        self.added = False
        self._attr_unique_id = f"{uid}_{self.identity}"
        self._attr_name = descriptor.get("name", self.identity)
        self._attr_icon = descriptor.get("icon") or None
        self._attr_device_class = descriptor.get("device_class") or None
        category = descriptor.get("entity_category")
        self._attr_entity_category = EntityCategory(category) if category in {"config", "diagnostic"} else None
        self._attr_entity_registry_enabled_default = not descriptor.get("disabled_by_default", False)
        self._attr_force_update = descriptor.get("force_update", self.caps.get("force_update", False))

    @property
    def device_info(self):
        node = self.hub.nodes.get(self.uid, {})
        return DeviceInfo(identifiers={(DOMAIN, self.uid)}, name=node.get("name", self.uid),
                          manufacturer="ESPHome", model=f"Thread {node.get('node_type', '').upper()}")

    @property
    def assumed_state(self): return self.caps.get("assumed_state", False)

    @property
    def available(self):
        if not self.hub.available(self.uid):
            return False
        kind = self.descriptor["type"]
        if kind not in {"numeric", "binary", "text", "event"} and self.hub.nodes[self.uid]["node_type"] == "sed":
            runtime = self.hub.runtime[self.uid]
            if not runtime.control_ready or self.hub.clock() - runtime.last_seen > 10:
                return False
        return kind in {"button", "event"} or self.identity in self.hub.nodes[self.uid]["values"]

    @property
    def value(self):
        return self.hub.nodes.get(self.uid, {}).get("values", {}).get(self.identity)

    def value_field(self, key, default=None):
        value = self.value
        return value.get(key, default) if isinstance(value, dict) else default

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.added = True

    async def async_will_remove_from_hass(self):
        self.added = False
        await super().async_will_remove_from_hass()

    async def command(self, value):
        try:
            return await self.hub.command(self.uid, self.identity, value)
        except ProtocolError as err:
            raise HomeAssistantError(str(err)) from err


class RemoteSensor(RemoteEntity, SensorEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_native_unit_of_measurement = self.descriptor.get("unit") or None
        self._attr_suggested_display_precision = self.caps.get("accuracy_decimals", self.descriptor.get("accuracy_decimals"))
        state_class = self.descriptor.get("state_class")
        try:
            self._attr_state_class = SensorStateClass(state_class) if state_class else None
        except ValueError:
            self._attr_state_class = None

    @property
    def native_value(self): return self.value


class RemoteBinary(RemoteEntity, BinarySensorEntity):
    @property
    def is_on(self): return self.value


class RemoteEvent(RemoteEntity, EventEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_event_types = self.caps["event_types"]
        self.pending_events = []

    def receive_event(self, value):
        if self.added:
            self._trigger_event(value)
            self.async_write_ha_state()
        elif len(self.pending_events) < 32:
            self.pending_events.append(value)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        for value in self.pending_events:
            self._trigger_event(value)
        if self.pending_events:
            self.async_write_ha_state()
        self.pending_events.clear()


class RemoteSwitch(RemoteEntity, SwitchEntity):
    @property
    def is_on(self): return self.value
    @property
    def assumed_state(self): return self.caps.get("assumed_state", False)
    async def async_turn_on(self, **kwargs): await self.command(True)
    async def async_turn_off(self, **kwargs): await self.command(False)


class RemoteNumber(RemoteEntity, NumberEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_native_min_value, self._attr_native_max_value = self.caps["min"], self.caps["max"]
        self._attr_native_step = self.caps["step"]
        self._attr_native_unit_of_measurement = self.descriptor.get("unit") or None
        self._attr_mode = {0: NumberMode.AUTO, 1: NumberMode.BOX, 2: NumberMode.SLIDER}.get(self.caps.get("mode"), NumberMode.AUTO)
    @property
    def native_value(self): return self.value
    async def async_set_native_value(self, value): await self.command(value)


class RemoteSelect(RemoteEntity, SelectEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_options = self.caps["options"]
    @property
    def current_option(self): return self.value
    async def async_select_option(self, option): await self.command(option)


class RemoteButton(RemoteEntity, ButtonEntity):
    async def async_press(self): await self.command(None)


class RemoteText(RemoteEntity, TextEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_native_min = self.caps.get("min_length", 0)
        self._attr_native_max = self.caps.get("max_length", 255)
        self._attr_mode = TextMode.PASSWORD if self.caps.get("mode") == 1 else TextMode.TEXT
    @property
    def native_value(self): return self.value
    async def async_set_value(self, value): await self.command(value)


class RemoteFan(RemoteEntity, FanEntity):
    def __init__(self, *args):
        super().__init__(*args)
        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        for key, feature in (("speed", FanEntityFeature.SET_SPEED), ("direction", FanEntityFeature.DIRECTION), ("oscillation", FanEntityFeature.OSCILLATE)):
            if self.caps.get(key): features |= feature
        if self.caps.get("preset_modes"): features |= FanEntityFeature.PRESET_MODE
        self._attr_supported_features = features
        self._attr_preset_modes = self.caps.get("preset_modes", [])
        self._attr_speed_count = self.caps.get("speed_count", 0)
    @property
    def is_on(self): return self.value_field("state")
    @property
    def percentage(self):
        return round(100 * self.value_field("speed", 0) / self._attr_speed_count) if self._attr_speed_count else None
    @property
    def current_direction(self): return DIRECTION_REVERSE if self.value_field("direction") == 1 else DIRECTION_FORWARD
    @property
    def oscillating(self): return self.value_field("oscillating")
    @property
    def preset_mode(self): return self.value_field("preset_mode")
    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        value = {"state": True}
        if percentage is not None: value["speed"] = math.ceil(percentage * self._attr_speed_count / 100)
        if preset_mode is not None: value["preset_mode"] = preset_mode
        await self.command(value)
    async def async_turn_off(self, **kwargs): await self.command({"state": False})
    async def async_set_percentage(self, percentage):
        await self.command({"state": percentage > 0, "speed": math.ceil(percentage * self._attr_speed_count / 100)})
    async def async_set_direction(self, direction): await self.command({"direction": int(direction == DIRECTION_REVERSE)})
    async def async_oscillate(self, oscillating): await self.command({"oscillating": oscillating})
    async def async_set_preset_mode(self, preset_mode): await self.command({"preset_mode": preset_mode})


class PositionEntity(RemoteEntity):
    @property
    def assumed_state(self): return self.caps.get("assumed_state", False)
    @property
    def is_closed(self):
        position = self.value_field("position")
        return position == 0 if position is not None else None
    @property
    def is_opening(self): return self.value_field("current_operation") == 1
    @property
    def is_closing(self): return self.value_field("current_operation") == 2


class RemoteCover(PositionEntity, CoverEntity):
    def __init__(self, *args):
        super().__init__(*args)
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if self.caps.get("position"): features |= CoverEntityFeature.SET_POSITION
        if self.caps.get("stop"): features |= CoverEntityFeature.STOP
        if self.caps.get("tilt"): features |= CoverEntityFeature.SET_TILT_POSITION | CoverEntityFeature.OPEN_TILT | CoverEntityFeature.CLOSE_TILT
        self._attr_supported_features = features
    @property
    def current_cover_position(self):
        value = self.value_field("position")
        return round(value * 100) if value is not None else None
    @property
    def current_cover_tilt_position(self):
        value = self.value_field("tilt")
        return round(value * 100) if value is not None else None
    async def async_open_cover(self, **kwargs): await self.command({"position": 1})
    async def async_close_cover(self, **kwargs): await self.command({"position": 0})
    async def async_stop_cover(self, **kwargs): await self.command({"stop": True})
    async def async_set_cover_position(self, **kwargs): await self.command({"position": kwargs["position"] / 100})
    async def async_open_cover_tilt(self, **kwargs): await self.command({"tilt": 1})
    async def async_close_cover_tilt(self, **kwargs): await self.command({"tilt": 0})
    async def async_set_cover_tilt_position(self, **kwargs): await self.command({"tilt": kwargs["tilt_position"] / 100})


class RemoteValve(PositionEntity, ValveEntity):
    def __init__(self, *args):
        super().__init__(*args)
        features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
        if self.caps.get("position"): features |= ValveEntityFeature.SET_POSITION
        if self.caps.get("stop"): features |= ValveEntityFeature.STOP
        self._attr_supported_features = features
        self._attr_reports_position = bool(self.caps.get("position"))
    @property
    def current_valve_position(self):
        value = self.value_field("position")
        return round(value * 100) if value is not None else None
    async def async_open_valve(self, **kwargs): await self.command({"position": 1})
    async def async_close_valve(self, **kwargs): await self.command({"position": 0})
    async def async_stop_valve(self, **kwargs): await self.command({"stop": True})
    async def async_set_valve_position(self, **kwargs): await self.command({"position": kwargs["position"] / 100})


class RemoteLock(RemoteEntity, LockEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_supported_features = LockEntityFeature.OPEN if self.caps.get("open") else LockEntityFeature(0)
        self._attr_code_format = r".+" if self.caps.get("requires_code") else None
    @property
    def is_locked(self):
        value = self.value_field("state")
        return value == 1 if value not in {None, 0} else None
    @property
    def is_locking(self): return self.value_field("state") == 4
    @property
    def is_unlocking(self): return self.value_field("state") == 5
    @property
    def is_jammed(self): return self.value_field("state") == 3
    @property
    def is_opening(self): return self.value_field("state") == 6
    @property
    def is_open(self): return self.value_field("state") == 7
    async def _lock_command(self, value, kwargs):
        if kwargs.get("code") is not None: value["code"] = kwargs["code"]
        await self.command(value)
    async def async_lock(self, **kwargs): await self._lock_command({"state": 1}, kwargs)
    async def async_unlock(self, **kwargs): await self._lock_command({"state": 2}, kwargs)
    async def async_open(self, **kwargs): await self._lock_command({"open": True}, kwargs)


MODES = {0: HVACMode.OFF, 1: HVACMode.HEAT_COOL, 2: HVACMode.COOL, 3: HVACMode.HEAT, 4: HVACMode.FAN_ONLY, 5: HVACMode.DRY, 6: HVACMode.AUTO}
FANS = {0: "on", 1: "off", 2: "auto", 3: "low", 4: "medium", 5: "high", 6: "middle", 7: "focus", 8: "diffuse", 9: "quiet"}
SWINGS = {0: "off", 1: "both", 2: "vertical", 3: "horizontal"}
PRESETS = {0: "none", 1: "home", 2: "away", 3: "boost", 4: "comfort", 5: "eco", 6: "sleep", 7: "activity"}
ACTIONS = {0: HVACAction.OFF, 2: HVACAction.COOLING, 3: HVACAction.HEATING, 4: HVACAction.IDLE, 5: HVACAction.DRYING, 6: HVACAction.FAN, 7: HVACAction.DEFROSTING}


class RemoteClimate(RemoteEntity, ClimateEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT if self.caps.get("temperature_unit") == 1 else UnitOfTemperature.CELSIUS
        self._attr_min_temp, self._attr_max_temp = self.caps["visual_min_temperature"], self.caps["visual_max_temperature"]
        self._attr_target_temperature_step = self.caps["visual_target_temperature_step"]
        self._attr_hvac_modes = [MODES[mode] for mode in self.caps["modes"] if mode in MODES]
        self._attr_fan_modes = [FANS[mode] for mode in self.caps.get("fan_modes", []) if mode in FANS] + ["custom:" + mode for mode in self.caps.get("custom_fan_modes", [])]
        self._attr_swing_modes = [SWINGS[mode] for mode in self.caps.get("swing_modes", []) if mode in SWINGS]
        self._attr_preset_modes = [PRESETS[mode] for mode in self.caps.get("presets", []) if mode in PRESETS] + ["custom:" + mode for mode in self.caps.get("custom_presets", [])]
        flags = self.caps.get("feature_flags", 0)
        features = ClimateEntityFeature.TARGET_TEMPERATURE_RANGE if flags & 4 else ClimateEntityFeature.TARGET_TEMPERATURE
        if flags & 2: features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if flags & 16: features |= ClimateEntityFeature.TARGET_HUMIDITY
        if self._attr_fan_modes: features |= ClimateEntityFeature.FAN_MODE
        if self._attr_swing_modes: features |= ClimateEntityFeature.SWING_MODE
        if self._attr_preset_modes: features |= ClimateEntityFeature.PRESET_MODE
        if HVACMode.OFF in self._attr_hvac_modes: features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        self._attr_supported_features = features
    @property
    def hvac_mode(self): return MODES.get(self.value_field("mode"))
    @property
    def hvac_action(self): return ACTIONS.get(self.value_field("action"))
    @property
    def current_temperature(self): return self.value_field("current_temperature")
    @property
    def current_humidity(self): return self.value_field("current_humidity")
    @property
    def target_temperature(self): return self.value_field("target_temperature")
    @property
    def target_temperature_low(self): return self.value_field("target_temperature_low")
    @property
    def target_temperature_high(self): return self.value_field("target_temperature_high")
    @property
    def target_humidity(self): return self.value_field("target_humidity")
    @property
    def fan_mode(self): return "custom:" + self.value_field("custom_fan_mode") if self.value_field("custom_fan_mode") else FANS.get(self.value_field("fan_mode"))
    @property
    def swing_mode(self): return SWINGS.get(self.value_field("swing_mode"))
    @property
    def preset_mode(self): return "custom:" + self.value_field("custom_preset") if self.value_field("custom_preset") else PRESETS.get(self.value_field("preset"))
    async def async_set_hvac_mode(self, hvac_mode): await self.command({"mode": next(key for key, value in MODES.items() if value == hvac_mode)})
    async def async_set_temperature(self, **kwargs):
        value = {wire: kwargs[ha] for ha, wire in (("temperature", "target_temperature"), ("target_temp_low", "target_temperature_low"), ("target_temp_high", "target_temperature_high")) if ha in kwargs}
        if "hvac_mode" in kwargs: value["mode"] = next(key for key, mode in MODES.items() if mode == kwargs["hvac_mode"])
        await self.command(value)
    async def async_set_humidity(self, humidity): await self.command({"target_humidity": humidity})
    async def async_set_fan_mode(self, fan_mode):
        await self.command({"custom_fan_mode": fan_mode[7:]} if fan_mode.startswith("custom:") else {"fan_mode": next(key for key, value in FANS.items() if value == fan_mode)})
    async def async_set_swing_mode(self, swing_mode): await self.command({"swing_mode": next(key for key, value in SWINGS.items() if value == swing_mode)})
    async def async_set_preset_mode(self, preset_mode):
        await self.command({"custom_preset": preset_mode[7:]} if preset_mode.startswith("custom:") else {"preset": next(key for key, value in PRESETS.items() if value == preset_mode)})
    async def async_turn_off(self): await self.async_set_hvac_mode(HVACMode.OFF)
    async def async_turn_on(self):
        mode = next((mode for mode in self._attr_hvac_modes if mode != HVACMode.OFF), HVACMode.OFF)
        await self.async_set_hvac_mode(mode)


def ha_color_mode(mode):
    if mode & 32:
        return ColorMode.RGBWW if mode & 16 else ColorMode.RGBW if mode & 4 else ColorMode.RGB
    if mode & 24: return ColorMode.COLOR_TEMP
    # A white-only lamp is dimmable in HA. HA's WHITE mode requires an RGB mode alongside it.
    if mode & 4: return ColorMode.BRIGHTNESS
    return ColorMode.BRIGHTNESS if mode & 2 else ColorMode.ONOFF


class RemoteLight(RemoteEntity, LightEntity):
    def __init__(self, *args):
        super().__init__(*args)
        self._attr_supported_color_modes = filter_supported_color_modes({ha_color_mode(mode) for mode in self.caps["color_modes"]})
        self._attr_supported_features = LightEntityFeature.FLASH
        if any(mode & 2 for mode in self.caps["color_modes"]): self._attr_supported_features |= LightEntityFeature.TRANSITION
        if self.caps.get("effects"): self._attr_supported_features |= LightEntityFeature.EFFECT
        self._attr_effect_list = ["None", *self.caps.get("effects", [])]
        self._attr_min_color_temp_kelvin = round(1000000 / self.caps["max_mireds"]) if self.caps.get("max_mireds", 0) else 2000
        self._attr_max_color_temp_kelvin = round(1000000 / self.caps["min_mireds"]) if self.caps.get("min_mireds", 0) else 6500
    @property
    def is_on(self): return self.value_field("state")
    @property
    def brightness(self):
        value = self.value_field("brightness")
        return round(value * 255) if value is not None else None
    @property
    def color_mode(self):
        mode = ha_color_mode(self.value_field("color_mode", self.caps["color_modes"][0]))
        return mode if mode in self._attr_supported_color_modes else next(iter(self._attr_supported_color_modes))
    @property
    def effect(self): return self.value_field("effect")
    def channels(self, keys): return tuple(round(self.value_field(key, 0) * 255) for key in keys)
    @property
    def rgb_color(self): return self.channels(("red", "green", "blue"))
    @property
    def rgbw_color(self): return self.channels(("red", "green", "blue", "white"))
    @property
    def rgbww_color(self): return self.channels(("red", "green", "blue", "cold_white", "warm_white"))
    @property
    def color_temp_kelvin(self):
        mireds = self.value_field("color_temperature", 0)
        if not mireds:
            cold, warm = self.value_field("cold_white", 0), self.value_field("warm_white", 0)
            if cold + warm:
                mireds = self.caps.get("min_mireds", 0) + (self.caps.get("max_mireds", 0) - self.caps.get("min_mireds", 0)) * warm / (cold + warm)
        return round(1000000 / mireds) if mireds else None
    async def async_turn_on(self, **kwargs):
        value = {"state": True}
        if "brightness" in kwargs: value["brightness"] = kwargs["brightness"] / 255
        if "white" in kwargs: value["white"] = kwargs["white"] / 255
        if "effect" in kwargs: value["effect"] = kwargs["effect"]
        if "transition" in kwargs: value["transition_length"] = round(kwargs["transition"] * 1000)
        if "flash" in kwargs: value["flash_length"] = 500 if kwargs["flash"] == "short" else 2000
        for argument, keys, color_mode in (("rgb_color", ("red", "green", "blue"), ColorMode.RGB),
                                         ("rgbw_color", ("red", "green", "blue", "white"), ColorMode.RGBW),
                                         ("rgbww_color", ("red", "green", "blue", "cold_white", "warm_white"), ColorMode.RGBWW)):
            if argument in kwargs:
                value.update(zip(keys, (channel / 255 for channel in kwargs[argument])))
                value["color_mode"] = next(mode for mode in self.caps["color_modes"] if ha_color_mode(mode) == color_mode)
        if "color_temp_kelvin" in kwargs:
            mode = next(mode for mode in self.caps["color_modes"] if ha_color_mode(mode) in {ColorMode.COLOR_TEMP, ColorMode.RGBWW} or mode & 8)
            value["color_mode"] = mode
            mireds = 1000000 / kwargs["color_temp_kelvin"]
            if mode & 8: value["color_temperature"] = mireds
            else:
                low, high = self.caps["min_mireds"], self.caps["max_mireds"]
                warm = min(1, max(0, (mireds - low) / (high - low))) if high != low else 0.5
                value.update(cold_white=1 - warm, warm_white=warm)
        await self.command(value)
    async def async_turn_off(self, **kwargs):
        value = {"state": False}
        if "transition" in kwargs: value["transition_length"] = round(kwargs["transition"] * 1000)
        await self.command(value)


ENTITY_TYPES = {"numeric": RemoteSensor, "text": RemoteSensor, "binary": RemoteBinary, "event": RemoteEvent,
                "switch": RemoteSwitch, "number": RemoteNumber, "select": RemoteSelect, "button": RemoteButton,
                "input_text": RemoteText, "light": RemoteLight, "fan": RemoteFan, "cover": RemoteCover,
                "climate": RemoteClimate, "lock": RemoteLock, "valve": RemoteValve}
