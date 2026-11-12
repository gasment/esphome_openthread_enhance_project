"""Per-device native API session and durable entity schema cache."""
from __future__ import annotations
import asyncio
import logging
import math
from time import monotonic, time
from aioesphomeapi import APIClient, APIConnectionError
from aioesphomeapi import model
from homeassistant.helpers.storage import Store
from .transport import normalize_mac

_LOGGER = logging.getLogger(__name__)

def json_safe(value):
    if isinstance(value,float) and not math.isfinite(value): return None
    if isinstance(value,dict): return {k:json_safe(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [json_safe(v) for v in value]
    return value

def domain_of(info):
    name = type(info).__name__.removesuffix('Info')
    return {'BinarySensor':'binary_sensor', 'TextSensor':'sensor'}.get(name, name.lower())

class Session:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.mac = normalize_mac(entry.unique_id)
        self.client = None
        self.connecting = False
        self.online = False
        self.sleep_until = 0
        self.sleep_indefinite = False
        self.initial_probe = True
        self.info = None
        self.schema = ''
        self.entities = []
        self.states = {}
        self.callbacks = set()
        self.schema_callbacks = set()
        self.store = Store(hass, 1, f'thread_sed.{self.mac}')
        self.task = None
        self.stopping = False
        self.sleep_seconds = 0
        self.event_seen = {}
        self._control_tasks = set()
        self._control_lock = asyncio.Lock()

    async def load(self):
        saved = await self.store.async_load() or {}
        self.schema = saved.get('schema', '')
        if saved.get('info'):
            self.info = model.DeviceInfo.from_dict(saved['info'])
        self.entities = [getattr(model, e['type']).from_dict(e['data']) for e in saved.get('entities', [])
                         if e['type'].endswith('Info') and hasattr(model, e['type'])]
        self.event_seen = saved.get('event_seen', {})
        self.sleep_until = monotonic() + max(0, saved.get('sleep_until',0)-time())
        self.sleep_indefinite = saved.get('sleep_indefinite',False)
        for item in saved.get('states',[]):
            cls = getattr(model,item.get('type',''),None)
            if cls and issubclass(cls, model.EntityState) and cls is not model.Event:
                try:
                    value = cls.from_dict(item['data'])
                    self.states[(value.device_id,value.key)] = value
                except (TypeError,ValueError):
                    pass

    async def save(self):
        await self.store.async_save(json_safe({'schema':self.schema, 'info':self.info.to_dict() if self.info else None,
            'entities':[{'type':type(e).__name__, 'data':e.to_dict()} for e in self.entities],
            'event_seen':self.event_seen,
            'sleep_until':time()+max(0,self.sleep_until-monotonic()),
            'sleep_indefinite':self.sleep_indefinite,
            'states':[{'type':type(s).__name__,'data':s.to_dict()} for s in self.states.values() if not isinstance(s,model.Event)]}))

    def changed(self, key=None):
        for callback in tuple(self.callbacks):
            callback(key)

    @property
    def available(self):
        return self.online or self.sleep_indefinite or monotonic() < self.sleep_until

    def state(self, value):
        # Standard EventResponse is intentionally ignored: reliable queued events
        # arrive through thread_sed.event and are acknowledged/deduplicated there.
        if isinstance(value, model.Event):
            return
        self.states[(value.device_id, value.key)] = value
        self.changed((value.device_id, value.key))

    async def connect(self, host, port, sock=None):
        if self.online or self.connecting or self.stopping:
            if sock: sock.close()
            return
        self.connecting = True
        client = APIClient(host, port, noise_psk=self.entry.data.get('noise_psk') or None,
                           expected_mac=self.entry.data['mac'], client_info='Thread SED', keepalive=30)
        metadata = asyncio.get_running_loop().create_future()
        async def on_stop(expected):
            if self.client is client:
                self.online = False
                self.changed()
        def service(call):
            if call.service == 'thread_sed.session':
                if not metadata.done(): metadata.set_result(call.data)
            elif call.service in ('thread_sed.sleep', 'thread_sed.event', 'thread_sed.batch_event', 'thread_sed.batch') and self.client is client:
                task = self.hass.async_create_task(self.control(client, call))
                self._control_tasks.add(task)
                task.add_done_callback(self._control_tasks.discard)
        try:
            if sock is None:
                await client.connect(on_stop=on_stop, login=False, log_errors=False)
            else:
                client.start_connection_from_socket(sock,on_stop=on_stop,log_errors=False)
                await client.finish_connection(login=False)
            info = await client.device_info()
            if normalize_mac(info.mac_address) != self.mac:
                raise ValueError('Device identity mismatch')
            client.subscribe_service_calls(service)
            meta = await asyncio.wait_for(metadata, 10)
            schema = meta.get('schema', '')
            if not schema:
                raise ValueError('Not a Thread SED API device')
            fingerprint = f'{schema}/{info.compilation_time}/{info.esphome_version}'
            self.sleep_seconds = min(31536000, max(0, int(meta.get('sleep_seconds', '0'))))
            if fingerprint != self.schema or not self.entities:
                entities, _services = await client.list_entities_services()
                self.entities = entities
                self.states.clear()
                self.schema = fingerprint
                self.info = info
                await self.save()
                for callback in tuple(self.schema_callbacks): callback()
            self.info = info
            self.client = client
            self.online, self.sleep_until = True, 0
            self.sleep_indefinite = False
            client.subscribe_states(self.state)
            def control_subscription(entity_id, attribute):
                if entity_id == 'thread_sed.control' and self.client is client:
                    client.send_home_assistant_state('thread_sed.control',None,'ready')
            client.subscribe_home_assistant_states(control_subscription)
            self.changed()
        except BaseException:
            await client.disconnect(force=True)
            if sock: sock.close()
            raise
        finally:
            self.connecting = False

    async def control(self, client, call):
        # Control messages follow TCP order, including all batch events before
        # the ending marker. Serialize asynchronous storage and ACK handling.
        async with self._control_lock:
            if client is not self.client: return
            await self._control_unlocked(client,call)

    async def _control_unlocked(self, client, call):
        try:
            if call.service == 'thread_sed.sleep':
                # State callbacks write HA state synchronously before this task.
                token = call.data['token']
                self.sleep_until = monotonic() + self.sleep_seconds + 60
                self.sleep_indefinite = self.sleep_seconds == 0
                await self.save()
                client.send_home_assistant_state('thread_sed.control', None, token)
            elif call.service == 'thread_sed.batch':
                token = call.data['token']
                if not token or len(token)>80: raise ValueError('Invalid batch token')
                await self.save()
                client.send_home_assistant_state('thread_sed.control',None,f'batch:{token}')
            elif call.service in ('thread_sed.event','thread_sed.batch_event'):
                boot, seq, key, event_type = call.data['token'].split(':', 3)
                seq = int(seq)
                # Different event entities may be sent in separate batches.
                # Track their watermarks separately so selecting B before A
                # cannot suppress older, still-unsent events belonging to A.
                identity = f'{boot}:{key}'
                legacy = self.event_seen.get(boot,-1)
                if seq > self.event_seen.get(identity,legacy):
                    self.event_seen = {k:v for k,v in self.event_seen.items() if k == boot or k.startswith(f'{boot}:')}
                    self.event_seen[identity] = seq
                    self.states[(0, int(key))] = model.Event(key=int(key), event_type=event_type)
                    self.changed((0, int(key)))
                    if call.service == 'thread_sed.event': await self.save()
                if call.service == 'thread_sed.event':
                    client.send_home_assistant_state('thread_sed.control', None, f'event:{boot}:{seq}')
        except (APIConnectionError, ValueError, KeyError):
            _LOGGER.debug('Session control interrupted for %s', self.mac)

    async def run(self):
        try:
            while not self.stopping:
                if not self.online and not self.connecting and (self.initial_probe or (not self.sleep_indefinite and monotonic() >= self.sleep_until)):
                    self.initial_probe = False
                    try:
                        await self.connect(self.entry.data['host'], self.entry.data.get('port',6053))
                    except (APIConnectionError, OSError, ValueError, asyncio.TimeoutError):
                        pass
                    self.changed()
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    def command(self, kind, key, device_id=0, **kwargs):
        if not self.online or self.client is None:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError('Device is sleeping or offline; commands are not queued')
        getattr(self.client, f'{kind}_command')(key, device_id=device_id, **kwargs)

    async def close(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for task in tuple(self._control_tasks): task.cancel()
        await asyncio.gather(*self._control_tasks, return_exceptions=True)
        if self.client: await self.client.disconnect(force=True)
        self.online = False
