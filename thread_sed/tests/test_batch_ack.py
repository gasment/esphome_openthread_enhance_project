"""Application batch ACK ordering and ready-handshake tests."""
import asyncio
import types
import unittest
from test_session import module

class Client:
    def __init__(self): self.acks=[]
    def send_home_assistant_state(self,entity,attribute,state): self.acks.append(state)

class BatchAckTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        hass=types.SimpleNamespace(async_create_task=asyncio.create_task)
        entry=types.SimpleNamespace(unique_id='aabbccddeeff',title='Fixture',data={})
        session=module.Session(hass,entry);session.client=Client()
        return session
    async def test_batch_marker_saves_then_acknowledges(self):
        session=self.session(); order=[]
        async def save():
            order.append('save');self.assertFalse(session.client.acks)
        session.save=save
        await session.control(session.client,types.SimpleNamespace(service='thread_sed.batch',data={'token':'123:1'}))
        self.assertEqual(order,['save'])
        self.assertEqual(session.client.acks,['batch:123:1'])
    async def test_events_share_batch_ack(self):
        session=self.session(); seen=[];session.callbacks.add(seen.append)
        event=types.SimpleNamespace(service='thread_sed.batch_event',data={'token':'555:1:8:single'})
        await session.control(session.client,event)
        await session.control(session.client,event)
        self.assertEqual(seen.count((0,8)),1)
        self.assertEqual(session.client.acks,[])
        await session.control(session.client,types.SimpleNamespace(service='thread_sed.batch',data={'token':'123:2'}))
        self.assertEqual(session.client.acks,['batch:123:2'])
        self.assertEqual(session.store.value['event_seen'],{'555:8':1})
    async def test_old_connection_cannot_ack_new_session(self):
        session=self.session(); old=Client()
        await session.control(old,types.SimpleNamespace(service='thread_sed.batch',data={'token':'old'}))
        self.assertEqual(old.acks,[])
        self.assertEqual(session.client.acks,[])
    async def test_storage_failure_does_not_ack(self):
        session=self.session()
        async def save():raise OSError('disk failure')
        session.save=save
        with self.assertRaises(OSError):
            await session.control(session.client,types.SimpleNamespace(service='thread_sed.batch',data={'token':'123:3'}))
        self.assertEqual(session.client.acks,[])
    async def test_ack_waits_for_earlier_control(self):
        session=self.session(); entered=asyncio.Event(); release=asyncio.Event()
        async def save():entered.set();await release.wait()
        session.save=save
        first=asyncio.create_task(session.control(session.client,types.SimpleNamespace(service='thread_sed.event',data={'token':'555:1:8:single'})))
        await entered.wait()
        last=asyncio.create_task(session.control(session.client,types.SimpleNamespace(service='thread_sed.batch',data={'token':'123:4'})))
        await asyncio.sleep(0)
        self.assertFalse(session.client.acks)
        release.set();await asyncio.gather(first,last)
        self.assertEqual(session.client.acks,['event:555:1','batch:123:4'])
