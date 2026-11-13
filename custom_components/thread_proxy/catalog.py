"""BR bindings stored under one HA manager entry, never per-BR config entries."""
from dataclasses import dataclass, field
from uuid import uuid4
from .const import DOMAIN, DEFAULT_PORT
from .models import host_name, api_port, encryption_key, proxy_id

MANAGER_ID = "thread_proxy_manager"

def is_manager(entry):
    return entry.data.get("kind") == "manager"

@dataclass
class Binding:
    entry_id: str
    title: str
    data: dict
    unique_id: str = ""
    options: dict = field(default_factory=dict)
    owner_entry_id: str = ""
    domain: str = DOMAIN

    def public_record(self):
        # Persisted record: callers must NEVER put this into HTTP/diagnostics.
        return {"id": self.entry_id, "name": self.title, "proxy_id": self.unique_id,
                "data": dict(self.data), "options": dict(self.options)}

    @classmethod
    def restore(cls, record, owner):
        return cls(record["id"], record["name"], dict(record["data"]),
                   record.get("proxy_id", ""), dict(record.get("options", {})), owner)

def new_binding(values, owner=""):
    host = host_name(values["host"])
    port = api_port(values.get("port", DEFAULT_PORT))
    name = values.get("name", "")
    if not isinstance(name, str): raise ValueError("invalid_input")
    identity = proxy_id(values["_proxy_id"]) if values.get("_proxy_id") else ""
    create = values.get("create_device", True)
    if type(create) is not bool: raise ValueError("invalid_input")
    return Binding(uuid4().hex, name.strip()[:120] or host.removesuffix(".local"),
                   {"host": host, "port": port, "noise_psk": encryption_key(values.get("noise_psk", "")),
                    "binding_source": "discovery" if identity else "manual", "enabled": True}, identity,
                   {"create_device": create}, owner)
