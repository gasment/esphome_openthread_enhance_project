"""Nodes belong to the integration, independent of the BR that delivered them."""
from __future__ import annotations
import asyncio
import base64
from collections import defaultdict, deque
from dataclasses import dataclass, field
import logging
from time import monotonic
from datetime import datetime, UTC
from aioesphomeapi import api_pb2, model
from google.protobuf.message import DecodeError
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from aioesphomeapi.model_conversions import LIST_ENTITIES_SERVICES_RESPONSE_TYPES, SUBSCRIBE_STATES_RESPONSE_TYPES
from .uplink import NodePacket, hex_value

_LOGGER=logging.getLogger(__name__)
BINARY={"BinarySensor","Switch","Light","Fan"}
SENSORS={"Sensor","TextSensor","Number","Select","Text","Cover","Climate","Lock","Valve","AlarmControlPanel"}
SUPPORTED=BINARY|SENSORS|{"Event"}

def entity_kind(value):
    name=type(value).__name__
    if name=="LockEntityState": return "Lock"
    if name.endswith("Info"): return name[:-4]
    if name.endswith("State"): return name[:-5]
    return name

def identity(value):
    return (entity_kind(value),value.device_id,value.key)

def platform_for(kind):
    return "binary_sensor" if kind in BINARY else "event" if kind=="Event" else "sensor" if kind in SENSORS else None

@dataclass
class Node:
    mac: str
    info: object=None
    schema: str=""
    descriptor: bytes=b""
    infos: dict=field(default_factory=dict)
    states: dict=field(default_factory=dict)
    state_records: dict=field(default_factory=dict)
    pending: deque=field(default_factory=lambda:deque(maxlen=8))
    events: dict=field(default_factory=lambda:defaultdict(list))
    cursors: dict=field(default_factory=dict)
    listeners: set=field(default_factory=set)
    last_br: str=""
    last_report_at: str=""
    last_br_name: str=""
    last_br_host: str=""
    suspended: bool=False
    def changed(self,key=None):
        for listener in tuple(self.listeners): listener(key)

class NodeRegistry:
    def __init__(self,hass):
        self.hass=hass;self.entry=None;self.nodes={};self.platforms={};self.entities={}
        self.store=None;self.loading=False;self.requests={};self.tasks=set();self.removals={};self.closing=False;self.deleting=set();self.source_resolver=lambda identity:{}

    async def load(self,entry):
        from homeassistant.helpers.storage import Store
        self.entry=entry;self.closing=False
        self.store=Store(self.hass,1,f"thread_proxy_nodes_{entry.entry_id}")
        saved=await self.store.async_load() or {}
        self.loading=True
        try:
            for record in saved.get("nodes",[])[:64]:
                try:
                    mac=hex_value(record["mac"],12);schema=hex_value(record["schema"],32)
                    body=base64.b64decode(record["descriptor"],validate=True)
                    if len(body)>32768:raise ValueError("oversize_descriptor")
                    self.accept(NodePacket(mac,"00"*8,"00000000",schema,2,2,body),"")
                    node=self.nodes[mac]
                    node.suspended=bool(record.get("suspended",False))
                    for key in ('last_br','last_report_at','last_br_name','last_br_host'):
                        setattr(node,key,str(record.get(key,''))[:253])
                    node.cursors={str(k):int(v) for k,v in list(record.get("cursors",{}).items())[-256:]}
                    for item in record.get("states",[])[:512]:
                        data=base64.b64decode(item["body"],validate=True)
                        if len(data)>32768:raise ValueError("oversize_state")
                        self.state(node,int(item["type"]),data)
                except (ValueError,TypeError,KeyError,DecodeError) as err:
                    _LOGGER.warning("Ignored invalid cached Thread node: %s",err)
        finally:self.loading=False

    def dump(self):
        return {"nodes":[{"mac":n.mac,"schema":n.schema,"descriptor":base64.b64encode(n.descriptor).decode(),
                          "suspended":n.suspended,"last_br":n.last_br,"last_report_at":n.last_report_at,"last_br_name":n.last_br_name,"last_br_host":n.last_br_host,"cursors":n.cursors,"states":[{"type":kind,"body":base64.b64encode(body).decode()} for kind,body in n.state_records.values()]}
                         for n in self.nodes.values() if n.info is not None]}

    def save(self):
        if self.store is not None and not self.loading:self.store.async_delay_save(self.dump,1)

    def note_source(self,packet,br_id,notify=True):
        node=self.nodes.get(packet.mac)
        if node is None or not br_id or packet.kind not in (2,7):return
        node.last_br=br_id
        node.last_report_at=datetime.now(UTC).isoformat()
        source=self.source_resolver(br_id)
        node.last_br_name=source.get('name','')
        node.last_br_host=source.get('host','')
        if notify:node.changed();self.save()

    def source_info(self,node):
        current=self.source_resolver(node.last_br) if node.last_br else {}
        return {'last_br_id':node.last_br or None,
                'last_br_name':current.get('name') or node.last_br_name or None,
                'last_br_host':current.get('host') or node.last_br_host or None,
                'last_report_at':node.last_report_at or None,
                'last_br_api_connected':current.get('connected')}

    def proxy_changed(self,identity):
        for node in self.nodes.values():
            if node.last_br==identity:node.changed()

    def public_nodes(self):
        return [{'mac':n.mac,'name':n.info.friendly_name or n.info.name or n.mac,**self.source_info(n)}
                for n in self.nodes.values() if n.info is not None and not n.suspended]

    def need_schema(self,packet):
        node=self.nodes.get(packet.mac)
        if packet.kind!=2 or node and node.info is not None and node.schema==packet.schema:return False
        now=monotonic()
        if now-self.requests.get(packet.mac,-100)<5:return False
        self.requests[packet.mac]=now;return True

    def accept(self,packet,br_id):
        node=self.nodes.get(packet.mac)
        if node is None:
            if len(self.nodes)>=64:raise ValueError("node_capacity_reached")
            node=self.nodes[packet.mac]=Node(packet.mac)
        if not self.loading:self.note_source(packet,br_id,notify=False)
        # Cached data alone must not recreate a deliberately removed device.
        # A new DATA or HEARTBEAT from a live BR reactivates the same identity.
        reactivated=node.suspended and not self.loading and packet.mac not in self.deleting and packet.kind in (2,7)
        if reactivated:
            node.suspended=False
            self.reconcile(node)
        if packet.kind!=2:
            if reactivated or packet.kind==7:
                node.changed();self.save()
            return
        frames=packet.frames()
        if packet.flags&2:
            info=None;infos={};done=False
            for kind,data in frames:
                cls=MESSAGE_TYPE_TO_PROTO.get(kind)
                if cls is None:continue
                message=cls();message.ParseFromString(data)
                if kind==10:
                    if message.mac_address.replace(":","").replace("-","").lower()!=packet.mac:raise ValueError("descriptor_identity_mismatch")
                    info=model.DeviceInfo.from_pb(message)
                elif kind==19:done=True
                else:
                    convert=LIST_ENTITIES_SERVICES_RESPONSE_TYPES.get(cls)
                    if convert is not None:
                        item=convert.from_pb(message)
                        if isinstance(item,model.EntityInfo) and entity_kind(item) in SUPPORTED:infos[identity(item)]=item
            if info is None or not done:raise ValueError("incomplete_descriptors")
            if len(infos)>512:raise ValueError("entity_capacity_reached")
            node.info=info;node.schema=packet.schema;node.descriptor=packet.payload;node.infos=infos
            node.states={k:v for k,v in node.states.items() if k in infos}
            node.state_records={k:v for k,v in node.state_records.items() if k in infos}
            self.reconcile(node)
            pending=list(node.pending);node.pending.clear()
            for earlier in pending:
                if earlier.schema==node.schema:self.apply(node,earlier.frames())
        elif node.info is None or packet.schema!=node.schema:
            node.pending.append(packet)
            if reactivated:self.save()
            return
        self.apply(node,frames);node.changed();self.save()

    def state(self,node,kind,data):
        cls=MESSAGE_TYPE_TO_PROTO.get(kind);convert=SUBSCRIBE_STATES_RESPONSE_TYPES.get(cls)
        if convert is None:return
        message=cls();message.ParseFromString(data);value=convert.from_pb(message);key=identity(value)
        if key not in node.infos:return
        node.states[key]=value;node.state_records[key]=(kind,data);node.changed(key)

    def apply(self,node,frames):
        for kind,data in frames:
            if kind!=35:self.state(node,kind,data);continue
            call=api_pb2.HomeassistantActionRequest();call.ParseFromString(data)
            if call.service not in ("thread_sed.event","thread_sed.batch_event"):continue
            token={item.key:item.value for item in call.data}.get("token","")
            try:boot,seq,raw_key,event_type=token.split(":",3);seq=int(seq);key=("Event",0,int(raw_key))
            except (ValueError,TypeError):continue
            info=node.infos.get(key)
            if info is None or event_type not in info.event_types:continue
            cursor=f"{boot}:{raw_key}"
            if seq<=node.cursors.get(cursor,-1):continue
            node.cursors[cursor]=seq
            if len(node.cursors)>256:node.cursors.pop(next(iter(node.cursors)))
            if node.suspended:continue
            entity=self.entities.get((node.mac,*key))
            # Initial platform registration may buffer an event. A deliberately
            # disabled/aborted entity must not replay events when re-enabled.
            if entity is not None and entity.added.is_set() and not entity.registered:continue
            pending=node.events[key]
            if len(pending)>=32:pending.pop(0)
            pending.append(event_type);node.changed(key)

    async def register_platform(self,platform,factory,add):
        self.platforms[platform]=(factory,add)
        for node in self.nodes.values():self.reconcile(node)

    def reconcile(self,node):
        if self.closing or node.suspended or node.mac in self.deleting:return
        desired={(node.mac,*key) for key in node.infos if platform_for(key[0]) in self.platforms}
        for uid,entity in list(self.entities.items()):
            if uid[0]==node.mac and uid not in desired:
                self.entities.pop(uid)
                task=self.hass.async_create_task(self.remove(entity),"Thread Proxy remove stale node entity")
                self.tasks.add(task);self.removals[uid]=task
                task.add_done_callback(lambda done,uid=uid,entity=entity:self.removed(uid,entity,done))
        for key,info in node.infos.items():
            platform=platform_for(key[0]);uid=(node.mac,*key)
            if platform not in self.platforms or uid in self.entities or uid in self.removals:continue
            factory,add=self.platforms[platform]
            entity=factory(self,node,key,info);self.entities[uid]=entity;add([entity])

    def removed(self,uid,entity,task):
        self.tasks.discard(task);self.removals.pop(uid,None)
        if self.closing or task.cancelled():return
        if task.exception() is not None:
            _LOGGER.error("Failed to remove node entity %s: %s",entity.unique_id,task.exception())
            self.entities[uid]=entity;return
        if not task.result():self.entities[uid]=entity;return
        node=self.nodes.get(uid[0])
        if node is not None:self.reconcile(node)

    async def remove(self,entity):
        from homeassistant.helpers import entity_registry as er
        try:await asyncio.wait_for(entity.added.wait(),10)
        except TimeoutError:
            _LOGGER.warning("Node entity registration is busy: %s",entity.unique_id);return False
        if entity.registered:await entity.async_remove(force_remove=True)
        registry=er.async_get(self.hass)
        entity_id=registry.async_get_entity_id(entity.platform_domain,"thread_proxy",entity.unique_id)
        if entity_id:registry.async_remove(entity_id)
        return True

    async def remove_device(self,mac,device):
        """Remove HA registrations, retaining cache and stable registry keys.

        Remove the actual device before releasing the guard: HA's websocket
        handler explicitly permits this, so an incoming packet cannot recreate
        entities in the gap before the framework removes their device.
        """
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        node=self.nodes.get(mac)
        if self.entry is None or self.closing or mac in self.deleting:return False
        if device.config_entry_id!=self.entry.entry_id or ("thread_proxy",f"node_{mac}") not in device.identifiers:return False
        devices=dr.async_get(self.hass)
        if dr.async_entries_for_parent_device(devices,device.id):return False
        if node is None:
            node=self.nodes[mac]=Node(mac,suspended=True)
        was_suspended=node.suspended
        node.suspended=True;self.deleting.add(mac)
        completed=False
        try:
            pending=[task for uid,task in self.removals.items() if uid[0]==mac]
            if pending:await asyncio.gather(*pending,return_exceptions=True)
            for uid,entity in list(self.entities.items()):
                if uid[0]!=mac:continue
                if not await self.remove(entity):return False
                self.entities.pop(uid,None)
            # Also clear disabled/stale registry entities that have no runtime
            # object, using this device and owner as the deletion boundary.
            entities=er.async_get(self.hass)
            for item in er.async_entries_for_config_entry(entities,self.entry.entry_id):
                if item.device_id==device.id and item.platform=="thread_proxy" and item.unique_id.startswith(f"node_{mac}_"):
                    entities.async_remove(item.entity_id)
            # Events received while removal was in progress are not replayed.
            node.events.clear()
            devices.async_remove_device(device.id)
            completed=True
            if self.store is not None:await self.store.async_save(self.dump())
            return True
        finally:
            self.deleting.discard(mac)
            if completed and node.info is None and not node.pending:self.nodes.pop(mac,None)
            if not completed:
                node.suspended=was_suspended
                self.reconcile(node)
                self.save()

    async def close(self):
        self.closing=True
        for task in tuple(self.tasks):task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.store is not None:await self.store.async_save(self.dump())
        self.removals.clear();self.entities.clear();self.platforms.clear();self.nodes.clear();self.requests.clear();self.deleting.clear();self.entry=None;self.store=None
