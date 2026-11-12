"""Connectionless SED sessions retaining native entity descriptors and commands."""
from __future__ import annotations
import asyncio
import base64
import logging
from time import monotonic, time
from aioesphomeapi import APIClient, api_pb2, model
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from aioesphomeapi.model_conversions import LIST_ENTITIES_SERVICES_RESPONSE_TYPES, SUBSCRIBE_STATES_RESPONSE_TYPES
from .session import Session, json_safe
from .transport import normalize_mac
from .udp_wire import DESCRIPTORS, decode_frames, encode_frames

_LOGGER=logging.getLogger(__name__)
PROTO_TO_TYPE={value:key for key,value in MESSAGE_TYPE_TO_PROTO.items()}

def protobuf_frames(payload):
    result=[]
    for message_type,data in decode_frames(payload):
        cls=MESSAGE_TYPE_TO_PROTO.get(message_type)
        if cls is not None:
            message=cls()
            message.ParseFromString(data)
            result.append(message)
    return result

class CommandEncoder:
    """Reuse command-to-protobuf conversion only; never open an API socket."""
    api_version=model.APIVersion(1,14)
    def __init__(self, send): self.send_message=send
    def _get_connection(self): return self

class UdpSession(Session):
    def __init__(self,hass,entry):
        super().__init__(hass,entry)
        self.udp_key=base64.b64decode(entry.data['noise_psk']) if entry.data.get('noise_psk') else None
        self.listener=None
        self.peer=None
        self.last_seen=0
        self.last_udp_boot=None
        self.retired_udp_boots=[]
        self.has_sleep=False
        self._save_task=None
        self._save_dirty=False
        self._packet_lock=asyncio.Lock()
        self._encoder=CommandEncoder(self._command_message)

    async def load(self):
        await super().load()
        saved=await self.store.async_load() or {}
        self.sleep_seconds=saved.get('sleep_seconds',0)
        self.has_sleep=saved.get('has_sleep',False)

    async def save(self):
        await self.store.async_save(json_safe({
            'schema':self.schema,'info':self.info.to_dict() if self.info else None,
            'entities':[{'type':type(e).__name__,'data':e.to_dict()} for e in self.entities],
            'event_seen':self.event_seen,'sleep_seconds':self.sleep_seconds,'has_sleep':self.has_sleep,
            'sleep_until':time()+max(0,self.sleep_until-monotonic()),'sleep_indefinite':self.sleep_indefinite,
            'states':[{'type':type(s).__name__,'data':s.to_dict()} for s in self.states.values() if not isinstance(s,model.Event)]}))

    def schedule_save(self):
        self._save_dirty=True
        if self._save_task is None or self._save_task.done():
            self._save_task=self.hass.async_create_task(self._flush_store())
            self._save_task.add_done_callback(self._saved)

    def _saved(self,task):
        if not task.cancelled() and task.exception():
            _LOGGER.error("Thread SED cache persistence failed for %s: %s",self.mac,task.exception())

    async def _flush_store(self):
        # ACK means processed by HA. Disk I/O is coalesced off the radio path.
        while self._save_dirty:
            self._save_dirty=False
            await self.save()

    def note_wake(self,boot):
        if boot in self.retired_udp_boots: return False
        if self.last_udp_boot is not None and self.last_udp_boot!=boot:
            self.retired_udp_boots.append(self.last_udp_boot)
            self.retired_udp_boots=self.retired_udp_boots[-64:]
        self.last_udp_boot=boot
        return True

    async def receive(self,packet,payload,peer):
        async with self._packet_lock:
            if self.stopping: return False
            if packet.boot in self.retired_udp_boots: return True
            messages=protobuf_frames(payload)
            descriptors=bool(packet.flags & DESCRIPTORS)
            if descriptors:
                info=next((m for m in messages if isinstance(m,api_pb2.DeviceInfoResponse)),None)
                if info is None or normalize_mac(info.mac_address)!=self.mac:
                    raise ValueError('Descriptor identity mismatch')
                if not any(isinstance(m,api_pb2.ListEntitiesDoneResponse) for m in messages):
                    raise ValueError('Incomplete descriptor group')
                entities=[]
                for message in messages:
                    cls=LIST_ENTITIES_SERVICES_RESPONSE_TYPES.get(type(message))
                    if cls is not None:
                        entities.append(cls.from_pb(message))
                self.info=model.DeviceInfo.from_pb(info)
                self.entities=entities
                self.schema=packet.schema.hex()
                valid={(e.device_id,e.key) for e in entities}
                self.states={key:value for key,value in self.states.items() if key in valid}
                for callback in tuple(self.schema_callbacks): callback()
            elif self.schema!=packet.schema.hex() or self.info is None:
                return False
            self.note_wake(packet.boot)
            self.peer=peer
            self.last_seen=monotonic()
            self.online=True
            for message in messages:
                if isinstance(message,api_pb2.HomeassistantActionRequest):
                    call=model.HomeassistantServiceCall.from_pb(message)
                    if call.service=='thread_sed.session':
                        self.has_sleep=call.data.get('sleep')=='1'
                        self.sleep_seconds=min(31536000,max(0,int(call.data.get('sleep_seconds','0'))))
                    elif call.service in ('thread_sed.event','thread_sed.batch_event'):
                        boot,seq,key,event_type=call.data['token'].split(':',3)
                        seq=int(seq); entity_key=(0,int(key))
                        info=next((e for e in self.entities if (e.device_id,e.key)==entity_key),None)
                        if info is None or not isinstance(info,model.EventInfo) or event_type not in info.event_types:
                            raise ValueError('Invalid event descriptor/type')
                        identity=f'{boot}:{key}'
                        if seq>self.event_seen.get(identity,self.event_seen.get(boot,-1)):
                            self.event_seen={k:v for k,v in self.event_seen.items() if k==boot or k.startswith(f'{boot}:')}
                            self.event_seen[identity]=seq
                            self.states[entity_key]=model.Event(key=int(key),event_type=event_type)
                            self.changed(entity_key)
                else:
                    cls=SUBSCRIBE_STATES_RESPONSE_TYPES.get(type(message))
                    if cls is not None:
                        self.state(cls.from_pb(message))
            if self.has_sleep:
                self.sleep_until=monotonic()+self.sleep_seconds+60
                self.sleep_indefinite=self.sleep_seconds==0
            self.changed()
            self.schedule_save()
            return True

    def sleeping(self,seconds):
        self.online=False
        self.sleep_seconds=seconds
        self.has_sleep=True
        self.sleep_until=monotonic()+seconds+60
        self.sleep_indefinite=seconds==0
        self.changed()
        self.schedule_save()

    def _command_message(self,message):
        payload=encode_frames([(PROTO_TO_TYPE[type(message)],message.SerializeToString())])
        self.listener.command(self,payload)

    def command(self,kind,key,device_id=0,**kwargs):
        if self.stopping or not self.online or self.peer is None:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError('Device is sleeping or offline; commands are not queued')
        getattr(APIClient,f'{kind}_command')(self._encoder,key,device_id=device_id,**kwargs)

    async def run(self):
        # Probes repair HA-address changes and HA restarts. Steady-state data
        # and manual send never wait for this maintenance loop or a handshake.
        next_probe=0
        try:
            while not self.stopping:
                now=monotonic()
                if now>=next_probe:
                    next_probe=now+30
                    await self.listener.probe(self)
                if self.online and now-self.last_seen>(3 if self.has_sleep else 90):
                    self.online=False
                    self.changed()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def close(self):
        self.stopping=True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        if self._save_task:
            await asyncio.gather(self._save_task,return_exceptions=True)
        self.online=False
