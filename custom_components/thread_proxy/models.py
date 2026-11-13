"""Validation and public BR state, independent of Home Assistant."""
from __future__ import annotations
import base64
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
import ipaddress
import re
from .const import COMPONENT, PROTOCOL_VERSION

def now() -> str:
    return datetime.now(UTC).isoformat()

def proxy_id(value: str) -> str:
    value = str(value).replace(":", "").replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", value):
        raise ValueError("invalid_proxy_id")
    return value

def host_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_host")
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value or len(value) > 253 or any(c in value for c in "/\\?#@ \t\r\n"):
        raise ValueError("invalid_host")
    if ":" in value:
        try:
            ipaddress.IPv6Address(value.split("%", 1)[0])
            if "%" in value and not re.fullmatch(r"[A-Za-z0-9_.-]+", value.split("%", 1)[1]):
                raise ValueError
        except ValueError as err:
            raise ValueError("invalid_host") from err
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise ValueError("invalid_host")
    result = value.lower().rstrip(".")
    if not result or result.startswith("."):
        raise ValueError("invalid_host")
    return result

def api_port(value) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid_port")
    try:
        result = int(value)
    except (ValueError, TypeError) as err:
        raise ValueError("invalid_port") from err
    if str(value).strip() != str(result) or not 1 <= result <= 65535:
        raise ValueError("invalid_port")
    return result

def discovery_host(addresses, fallback="") -> str:
    """Use the same deterministic choice in HA flows and the live catalog."""
    candidates = []
    for raw in addresses:
        address = host_name(str(raw))
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not (ip.is_unspecified or ip.is_multicast or ip.is_link_local):
            candidates.append((ip.version != 4, address))
    if candidates:
        return min(candidates)[1]
    return host_name(fallback)

def encryption_key(value: str) -> str:
    value = str(value).strip()
    if value:
        try:
            if len(base64.b64decode(value, validate=True)) != 32:
                raise ValueError
        except (ValueError, TypeError) as err:
            raise ValueError("invalid_key") from err
    return value

@dataclass
class Discovery:
    id: str
    name: str
    host: str
    port: int
    version: int
    network_id: str = ""
    service_name: str = ""
    present: bool = True
    last_seen: str = ""

    @classmethod
    def parse(cls, properties, host, port, service_name="", hostname=""):
        props = {str(k): str(v) for k, v in properties.items()}
        if props.get("component") != COMPONENT:
            raise ValueError("invalid_discovery")
        identity = proxy_id(props.get("id", ""))
        try:
            version = int(props.get("version", "0"))
        except ValueError as err:
            raise ValueError("invalid_discovery") from err
        if version != PROTOCOL_VERSION:
            raise ValueError("unsupported_protocol")
        label = props.get("name") or hostname.rstrip(".").removesuffix(".local") or identity
        return cls(identity, label[:120], host_name(host), api_port(port), version,
                   props.get("network_id", "")[:64], service_name, True, now())

    def public(self):
        return asdict(self)

@dataclass
class BRState:
    entry_id: str
    name: str
    host: str
    port: int
    proxy_id: str = ""
    status: str = "connecting"
    connected: bool = False
    proxy_ready: bool = False
    data_plane_ready: bool = False
    thread_available: bool | None = None
    thread_attached: bool | None = None
    thread_role: str = ""
    network_id: str = ""
    partition_id: str = ""
    protocol_version: int | None = None
    firmware: str = ""
    model: str = ""
    last_seen: str | None = None
    connected_since: str | None = None
    last_error: str | None = None
    node_count: int | None = None
    queue_size: int | None = None

    def public(self):
        result = asdict(self)
        result.update(bound=True)
        return result

def parse_metadata(data, expected_id):
    if data.get("component") != COMPONENT:
        raise ValueError("unsupported_protocol")
    if int(data.get("version", "0")) != PROTOCOL_VERSION:
        raise ValueError("unsupported_protocol")
    if proxy_id(data.get("id", "")) != expected_id:
        raise ValueError("identity_mismatch")
    result = {"network_id": str(data.get("network_id", ""))[:64],
              "partition_id": str(data.get("partition_id", ""))[:32],
              "protocol_version": PROTOCOL_VERSION}
    result["data_plane_ready"] = data.get("data_plane_ready") == "true" and data.get("uplink_protocol") == "tsd2-v1"
    for key in ("thread_available", "thread_attached"):
        raw = data.get(key)
        if raw is not None and raw not in ("true", "false"):
            raise ValueError("invalid_metadata")
        result[key] = raw == "true" if raw is not None else None
    role = data.get("thread_role", "")
    if role not in ("", "disabled", "detached", "child", "router", "leader"):
        raise ValueError("invalid_metadata")
    result["thread_role"] = role
    for key in ("node_count", "queue_size"):
        result[key] = max(0, min(1000000, int(data[key]))) if key in data else None
    return result
