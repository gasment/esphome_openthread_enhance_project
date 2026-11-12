"""Selecting event entities in different orders must not lose pending events."""
import types
import unittest
from test_batch_ack import BatchAckTests

# Use the fixture constructor without exposing its TestCase as a module global.
make_session = BatchAckTests.session
del BatchAckTests

class SelectedEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_entity_does_not_hide_earlier_entity(self):
        session=make_session(self);seen=[];session.callbacks.add(seen.append)
        def event(token):return types.SimpleNamespace(service='thread_sed.batch_event',data={'token':token})
        await session.control(session.client,event('555:2:9:second'))
        await session.control(session.client,event('555:1:8:first'))
        await session.control(session.client,event('555:2:9:second'))
        await session.control(session.client,event('555:1:8:first'))
        self.assertEqual(seen,[(0,9),(0,8)])
        self.assertEqual(session.event_seen,{'555:9':2,'555:8':1})

    async def test_legacy_global_watermark_remains_effective(self):
        session=make_session(self);seen=[];session.callbacks.add(seen.append)
        session.event_seen={'555':10}
        def event(token):return types.SimpleNamespace(service='thread_sed.batch_event',data={'token':token})
        await session.control(session.client,event('555:9:8:old'))
        await session.control(session.client,event('555:12:9:new-b'))
        await session.control(session.client,event('555:11:8:new-a'))
        self.assertEqual(seen,[(0,9),(0,8)])
        self.assertEqual(session.event_seen,{'555':10,'555:9':12,'555:8':11})
