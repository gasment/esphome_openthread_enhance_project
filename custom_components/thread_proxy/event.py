"""Node events through BR proxies; no per-node config entry."""
from .node_entity import NodeEvent

async def async_setup_entry(hass,entry,async_add_entities):
    await entry.runtime_data.nodes.register_platform("event",NodeEvent,async_add_entities)
