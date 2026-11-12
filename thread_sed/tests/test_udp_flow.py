"""Migration logic with HA flow stand-ins; network probing uses the UDP tests."""
import asyncio
import base64
import importlib.util
import sys
import types
import unittest
from unittest.mock import AsyncMock,patch
from test_session import TRANSPORT

class FlowAbort(RuntimeError): pass

class FakeFlow:
    def __init_subclass__(cls,**kwargs): super().__init_subclass__()
    async def async_set_unique_id(self,value): self.selected_id=value
    def _get_reconfigure_entry(self): return self.entry
    def _abort_if_unique_id_mismatch(self):
        if self.selected_id!=self.entry.unique_id: raise FlowAbort('unique_id_mismatch')
    def _abort_if_unique_id_configured(self,*args,**kwargs): raise AssertionError('must update existing entry')
    def async_update_reload_and_abort(self,entry,*,data_updates):
        return {'entry':entry,'data':data_updates,'type':'abort'}

config_entries=types.ModuleType('homeassistant.config_entries')
config_entries.ConfigFlow=FakeFlow
const=types.ModuleType('homeassistant.const');const.CONF_HOST='host';const.CONF_PORT='port'
probatio=types.ModuleType('probatio')
with patch.dict(sys.modules,{'homeassistant.config_entries':config_entries,'homeassistant.const':const,'probatio':probatio}):
    spec=importlib.util.spec_from_file_location('sed_fixture.udp_config_flow_fixture',TRANSPORT.with_name('config_flow.py'))
    flow_module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flow_module)

class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_udp_migration_updates_same_entry_and_preserves_optional_key(self):
        key=base64.b64encode(bytes(range(1,33))).decode()
        entry=types.SimpleNamespace(unique_id='aabbccddeeff',data={'host':'::1','port':6053,'mac':'AA:BB:CC:DD:EE:FF','noise_psk':key})
        flow=flow_module.ThreadSedConfigFlow();flow.entry=entry
        info=types.SimpleNamespace(mac_address='AA:BB:CC:DD:EE:FF',friendly_name='Fixture',name='fixture')
        with patch.object(flow_module,'probe_device',AsyncMock(return_value=info)) as probe:
            result=await flow.async_step_reconfigure({'host':'::1','port':6064,'transport':'udp'})
        self.assertIs(result['entry'],entry)
        self.assertEqual(result['data']['transport'],'udp')
        self.assertEqual(result['data']['noise_psk'],key)
        self.assertEqual(probe.await_args.args[2],bytes(range(1,33)))

    async def test_migration_refuses_another_device_identity(self):
        flow=flow_module.ThreadSedConfigFlow()
        flow.entry=types.SimpleNamespace(unique_id='aabbccddeeff',data={'host':'::1','port':6053,'mac':'AA:BB:CC:DD:EE:FF'})
        wrong=types.SimpleNamespace(mac_address='11:22:33:44:55:66',friendly_name='Wrong',name='wrong')
        with patch.object(flow_module,'probe_device',AsyncMock(return_value=wrong)):
            with self.assertRaises(FlowAbort):
                await flow.async_step_reconfigure({'host':'::1','port':6064,'transport':'udp'})
