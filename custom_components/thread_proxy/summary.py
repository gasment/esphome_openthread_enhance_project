"""Summary policy and per-device statistics; no radio or node configuration."""
from datetime import datetime, UTC
import math

DEFAULTS = {"offline_after": 1800, "battery_source": "battery", "battery_threshold": 20.0, "voltage_threshold": 3.0}

def validate_settings(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("invalid_input")
    result = dict(values)
    if type(result["offline_after"]) is not int or not 1 <= result["offline_after"] <= 31536000:
        raise ValueError("invalid_input")
    if result["battery_source"] not in ("battery", "voltage"):
        raise ValueError("invalid_input")
    for key in ("battery_threshold", "voltage_threshold"):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("invalid_input")
        if not 0 <= value <= (100 if key == "battery_threshold" else 1000):
            raise ValueError("invalid_input")
        result[key] = float(value)
    return result

def get_settings(data):
    try:
        return validate_settings({**DEFAULTS, **data.get("summary_settings", {})})
    except (ValueError, TypeError):
        return dict(DEFAULTS)

def calculate(nodes, macs, settings, now=None):
    now = now or datetime.now(UTC)
    offline = online = low = healthy = 0
    source = settings["battery_source"]
    threshold = settings[source + "_threshold"]
    for mac in set(macs):
        node = nodes.get(mac)
        if node is None:
            continue
        try:
            reported = datetime.fromisoformat(node.last_report_at)
            if reported.tzinfo is None:
                raise ValueError("missing_timezone")
            if (now - reported).total_seconds() > settings["offline_after"]:
                offline += 1
            else:
                online += 1
        except (ValueError, TypeError):
            pass  # No timestamp is unknown, not evidence of online/offline.
        readings = []
        for key, info in node.infos.items():
            if key[0] not in ("Sensor", "Number") or getattr(info, "device_class", None) != source:
                continue
            state = node.states.get(key)
            value = getattr(state, "state", None)
            if getattr(state, "missing_state", False) or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            unit = getattr(info, "unit_of_measurement", "")
            if source == "voltage":
                scale = {"": 1, "V": 1, "mV": .001, "µV": .000001, "uV": .000001, "kV": 1000}.get(unit)
                if scale is None:
                    continue
                value *= scale
            elif unit not in ("", "%") or not 0 <= value <= 100:
                continue
            if value < 0:
                continue
            readings.append(value)
        if readings:
            if any(value < threshold for value in readings):
                low += 1
            else:
                healthy += 1
    return {"total": float(len(set(macs))), "suspect_offline": float(offline), "online": float(online),
            "suspect_low_battery": float(low), "battery_healthy": float(healthy)}

def registered_nodes(hass, entry_id):
    from homeassistant.helpers import device_registry as dr
    result = set()
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id):
        for domain, identifier in device.identifiers:
            if domain != "thread_proxy" or not identifier.startswith("node_"):
                continue
            mac = identifier[5:]
            if len(mac) == 12 and all(c in "0123456789abcdef" for c in mac):
                result.add(mac)
    return result
