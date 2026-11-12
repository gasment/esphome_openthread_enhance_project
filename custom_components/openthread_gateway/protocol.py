"""Strict v3 wire format. No Home Assistant dependencies and no v2 fallback."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import math
import re

from .const import KIND_PLATFORM, MAX_DATAGRAM, MAX_ENTITIES


class ProtocolError(ValueError):
    """Malformed, unsupported or unauthenticated packet."""


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) is not None


def hex_id(value, length):
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def uint(value, maximum=0xFFFFFFFF):
    return type(value) is int and 0 <= value <= maximum


def finite(value, low=-3.402823466e38, high=3.402823466e38):
    return type(value) in {int, float} and math.isfinite(value) and low <= value <= high


def string(value, limit=96):
    return isinstance(value, str) and len(value.encode("utf-8")) <= limit and all(ord(c) >= 32 for c in value)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON member")
        result[key] = value
    return result


def loads(raw):
    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ProtocolError("non-finite JSON")
        return parsed
    try:
        return json.loads(raw, object_pairs_hook=_object,
                          parse_float=number,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("non-finite JSON")))
    except (ValueError, UnicodeError, RecursionError) as err:
        raise ProtocolError("invalid JSON") from err


def encode(message, secret=None):
    """HMAC covers exact JSON bytes; senders need no cross-language float canonicalizer."""
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    digest = hmac.new(bytes.fromhex(secret), b"OTG3\0" + payload, hashlib.sha256).hexdigest() if secret else "-"
    packet = b"OTG3 " + digest.encode() + b"\n" + payload
    if len(packet) > MAX_DATAGRAM:
        raise ProtocolError("datagram exceeds 1232 bytes")
    return packet


def decode(packet):
    if not 0 < len(packet) <= MAX_DATAGRAM or not packet.startswith(b"OTG3 "):
        raise ProtocolError("unsupported wire version or length")
    try:
        header, raw = packet.split(b"\n", 1)
        signature = header[5:].decode("ascii")
    except (ValueError, UnicodeError) as err:
        raise ProtocolError("invalid header") from err
    if signature != "-" and not hex_id(signature, 64):
        raise ProtocolError("invalid signature")
    message = loads(raw)
    if not isinstance(message, dict) or type(message.get("v")) is not int or message.get("v") != 3 or not isinstance(message.get("type"), str):
        raise ProtocolError("invalid message")
    return message, signature, raw


def authenticated(signature, raw, secret):
    return signature != "-" and hmac.compare_digest(signature, hmac.new(bytes.fromhex(secret), b"OTG3\0" + raw, hashlib.sha256).hexdigest())


def descriptor(item):
    if not isinstance(item, dict) or not identifier(item.get("id")) or item.get("type") not in KIND_PLATFORM:
        raise ProtocolError("invalid entity identity/type")
    for key in ("name", "unit", "device_class", "icon", "state_class", "entity_category"):
        if key in item and not string(item[key], 96 if key == "name" else 64):
            raise ProtocolError("invalid metadata")
    if item.get("entity_category", "") not in {"", "none", "config", "diagnostic"}:
        raise ProtocolError("invalid category")
    for key in ("disabled_by_default", "force_update"):
        if key in item and type(item[key]) is not bool:
            raise ProtocolError("invalid metadata flag")
    caps = item.get("capabilities", {})
    if not isinstance(caps, dict) or len(json.dumps(caps)) > 4096:
        raise ProtocolError("invalid capabilities")
    kind = item["type"]
    for key in ("assumed_state", "speed", "direction", "oscillation", "position", "stop", "toggle", "tilt", "open", "requires_code", "force_update"):
        if key in caps and type(caps[key]) is not bool:
            raise ProtocolError("invalid capability flag")
    def listing(key, numeric=False, required=False):
        values = caps.get(key, [])
        if not isinstance(values, list) or len(values) > 32 or (required and not values):
            raise ProtocolError("invalid capability list")
        if any(not (uint(v, 65535) if numeric else string(v, 64)) for v in values) or len(set(values)) != len(values):
            raise ProtocolError("invalid/duplicate capability item")
    if kind == "number":
        if not finite(caps.get("min")) or not finite(caps.get("max"), caps["min"]) or not finite(caps.get("step"), 1e-30):
            raise ProtocolError("number requires min/max/step")
        if not uint(caps.get("mode", 0), 2):
            raise ProtocolError("invalid number mode")
    if kind == "select":
        listing("options", required=True)
    if kind == "event":
        listing("event_types", required=True)
        if "" in caps["event_types"]:
            raise ProtocolError("empty event type")
    if kind == "input_text":
        if not uint(caps.get("min_length", 0), 255) or not uint(caps.get("max_length", 255), 255) or caps.get("min_length", 0) > caps.get("max_length", 255):
            raise ProtocolError("invalid text length")
        if not uint(caps.get("mode", 0), 1):
            raise ProtocolError("invalid text mode")
    for key in {"fan": ["preset_modes"], "light": ["effects"], "climate": ["custom_fan_modes", "custom_presets"]}.get(kind, []):
        listing(key)
    for key in {"light": ["color_modes"], "climate": ["modes", "fan_modes", "swing_modes", "presets"]}.get(kind, []):
        listing(key, numeric=True, required=key in {"color_modes", "modes"})
    if kind == "fan" and not uint(caps.get("speed_count", 0), 255):
        raise ProtocolError("invalid speed count")
    if kind == "light" and (not finite(caps.get("min_mireds", 0), 0) or not finite(caps.get("max_mireds", 0), caps.get("min_mireds", 0))):
        raise ProtocolError("invalid light temperature range")
    if kind == "light" and any(mode & 24 for mode in caps["color_modes"]) and caps.get("min_mireds", 0) <= 0:
        raise ProtocolError("temperature-capable light requires positive mired limits")
    if kind == "climate":
        for key in ("visual_min_temperature", "visual_max_temperature", "visual_target_temperature_step"):
            if not finite(caps.get(key)):
                raise ProtocolError("invalid climate range")
        if caps["visual_min_temperature"] > caps["visual_max_temperature"] or caps["visual_target_temperature_step"] <= 0:
            raise ProtocolError("invalid climate range")
        if not uint(caps.get("feature_flags", 0)):
            raise ProtocolError("invalid climate features")
        for key, maximum in (("modes", 6), ("fan_modes", 9), ("swing_modes", 3), ("presets", 7)):
            if any(value > maximum for value in caps.get(key, [])):
                raise ProtocolError("unsupported climate enum")
    return item


def manifest(raw):
    data = loads(raw)
    if not isinstance(data, dict) or not string(data.get("name")) or not isinstance(data.get("entities"), list) or len(data["entities"]) > MAX_ENTITIES:
        raise ProtocolError("invalid complete manifest")
    entities = [descriptor(item) for item in data["entities"]]
    if len({item["id"] for item in entities}) != len(entities):
        raise ProtocolError("duplicate entity")
    return data


def valid_value(entity, value, command=False):
    kind, caps = entity["type"], entity.get("capabilities", {})
    if kind in {"numeric", "number"}:
        if not finite(value):
            return False
        if command and kind == "number":
            steps = (value - caps["min"]) / caps["step"]
            return caps["min"] <= value <= caps["max"] and math.isclose(steps, round(steps), rel_tol=1e-6, abs_tol=1e-6)
        return not command or kind != "numeric"
    if kind in {"binary", "switch"}:
        return type(value) is bool and (not command or kind == "switch")
    if kind == "button":
        return command and value is None
    if kind == "event":
        return not command and isinstance(value, str) and value in caps["event_types"]
    if kind in {"text", "input_text", "select"}:
        if not string(value, 255):
            return False
        if kind == "select":
            return value in caps["options"]
        return not command or kind == "input_text" and caps.get("min_length", 0) <= len(value.encode()) <= caps.get("max_length", 255)
    if not isinstance(value, dict) or not value:
        return False
    for key, val in value.items():
        ok = False
        if kind == "fan":
            if key == "state": ok = type(val) is bool
            if key == "oscillating": ok = type(val) is bool and (not command or caps.get("oscillation", False))
            if key == "speed": ok = uint(val, caps.get("speed_count", 0)) and (not command or caps.get("speed", False))
            if key == "direction": ok = uint(val, 1) and (not command or caps.get("direction", False))
            if key == "preset_mode": ok = val in caps.get("preset_modes", [])
        if kind in {"cover", "valve"}:
            if key == "position": ok = finite(val, 0, 1) and (not command or caps.get("position", False) or val in {0, 1})
            if key == "tilt" and kind == "cover": ok = finite(val, 0, 1) and (not command or caps.get("tilt", False))
            if key in {"stop", "toggle"}: ok = command and type(val) is bool and caps.get(key, False)
            if key == "current_operation": ok = not command and uint(val, 2)
        if kind == "lock":
            if key == "state": ok = uint(val, 7) and (not command or val in {1, 2})
            if key == "open": ok = command and type(val) is bool and caps.get("open", False)
            if key == "code": ok = command and string(val, 64)
        if kind == "light":
            if key == "state": ok = type(val) is bool
            if key == "color_mode": ok = uint(val, 65535) and (val in caps.get("color_modes", []) or not command and val == 0)
            if key == "effect": ok = isinstance(val, str) and (not command or val == "None" or val in caps.get("effects", []))
            if key in {"brightness", "red", "green", "blue", "white", "cold_white", "warm_white", "color_brightness"}: ok = finite(val, 0, 1)
            if key == "color_temperature": ok = finite(val, 0) and (not command or caps.get("min_mireds", 0) <= val <= caps.get("max_mireds", 0))
            if key in {"transition_length", "flash_length"}: ok = command and uint(val, 3600000)
            if command and ok:
                capability = {"brightness": 2, "transition_length": 2, "white": 4, "color_temperature": 8, "cold_white": 16, "warm_white": 16, "red": 32, "green": 32, "blue": 32, "color_brightness": 32}.get(key, 0)
                if capability: ok = any(mode & capability for mode in caps.get("color_modes", []))
        if kind == "climate":
            if key in {"current_temperature", "current_humidity"}: ok = not command and finite(val)
            if key in {"target_temperature", "target_temperature_low", "target_temperature_high"}:
                ok = finite(val) and (not command or caps["visual_min_temperature"] <= val <= caps["visual_max_temperature"])
                if command and key == "target_temperature" and caps.get("feature_flags", 0) & 4: ok = False
                if command and key != "target_temperature" and not caps.get("feature_flags", 0) & 6: ok = False
            if key == "target_humidity": ok = finite(val, 0, 100) and (not command or bool(caps.get("feature_flags", 0) & 16))
            if key == "action": ok = not command and uint(val, 16)
            mapping = {"mode": "modes", "fan_mode": "fan_modes", "swing_mode": "swing_modes", "preset": "presets", "custom_fan_mode": "custom_fan_modes", "custom_preset": "custom_presets"}
            if key in mapping: ok = val in caps.get(mapping[key], [])
        if not ok:
            return False
    if command and kind == "climate":
        if ("fan_mode" in value and "custom_fan_mode" in value) or ("preset" in value and "custom_preset" in value): return False
        if value.get("target_temperature_low", -math.inf) > value.get("target_temperature_high", math.inf): return False
    if command and kind == "lock" and caps.get("requires_code") and not value.get("code"):
        return False
    return True


def decode_chunk(value):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as err:
        raise ProtocolError("invalid manifest chunk") from err
