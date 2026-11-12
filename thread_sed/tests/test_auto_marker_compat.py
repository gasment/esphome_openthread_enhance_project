"""Auto confirmations reuse the existing HA application batch ACK."""
import types
import unittest
import test_batch_ack as fixture

class AutoMarkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_token_uses_same_ack_and_does_not_mark_sleep(self):
        session=fixture.BatchAckTests.session(self)
        await session.control(session.client,types.SimpleNamespace(service='thread_sed.batch',data={'token':'auto:123:1'}))
        self.assertEqual(session.client.acks,['batch:auto:123:1'])
        self.assertFalse(session.sleep_indefinite)
        self.assertEqual(session.sleep_until,0)
