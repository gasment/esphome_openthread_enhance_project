"""Protocol v2 config/codegen and packaged-resource checks. No compiler is invoked."""
import gzip
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[2]
WORKER="""
from pathlib import Path
from esphome.core import CORE
from esphome.config import read_config
from esphome.__main__ import generate_cpp_contents
import sys
CORE.config_path=Path(sys.argv[1]).resolve()
config=read_config({})
if config is None: sys.exit(1)
CORE.config=config
generate_cpp_contents(config)
assert 'revision_' not in CORE.cpp_main_section
print('CONFIG_AND_CODEGEN_OK')
"""

def endpoint(platform,sed):
    nrf=platform=='nrf52';suffix='_nrf52' if nrf else ''
    component='thread_'+('sed' if sed else 'reed')+'_transporter'+suffix
    ot='openthread'+suffix
    data={'esphome':{'name':'transport-v2-check'},
          'external_components':[{'source':{'type':'local','path':str(ROOT/'components'/platform)},'components':[component,ot,'thread_entity_bridge'+suffix]}],
          'network':{'enable_ipv6':True},'sensor':[{'platform':'template','id':'reading','lambda':'return 1.0;'}],
          component:{'id':'transport','openthread_id':'ot','node_id':'sample','name':'Sample','sensors':[{'id':'reading'}] if sed else ['reading']}}
    if nrf:
        data['nrf52']={'board':'nrf52840dk/nrf52840'}
        data[ot]={'id':'ot','device_type':'MTD' if sed else 'FTD',
                  'thread_network_config':{'network_key':'00112233445566778899aabbccddeeff'}}
        if sed:data[ot]['poll_period']='1s'
    else:
        data['esp32']={'variant':'esp32c6','toolchain':'esp-idf','framework':{'type':'esp-idf','version':'5.5.6'}}
        data[ot]={'id':'ot','device_type':'MTD' if sed else 'FTD','thread_network_config':{'network_key':'00112233445566778899aabbccddeeff'}}
        if sed:data[ot]['poll_period']='1s'
    return data,component

def gateway():
    return {'esphome':{'name':'gateway-v2-check'},'esp32':{'variant':'esp32s3','toolchain':'esp-idf','framework':{'type':'esp-idf','version':'5.5.6'}},
            'external_components':[{'source':{'type':'local','path':str(ROOT/'components/esp32')},'components':['thread_br','thread_gateway','thread_entity_bridge']}],
            'wifi':{'ssid':'test','password':'test-password'},'network':{'enable_ipv6':True},
            'thread_br':{'id':'br','used_psram':False,'web':{},'rcp':{'uart_tx':18,'uart_rx':17,'rcp_reset_pin':7,'rcp_boot_pin':8},
              'thread_network_config':{'channel':20,'network_name':'Testing','network_key':'00112233445566778899aabbccddeeff','pan_id':4660,'ext_pan_id':'0011223344556677','pskc':'00112233445566778899aabbccddeeff','mesh_local_prefix':'fd12:3456:789a:1::/64'}},
            'thread_gateway':{'thread_br_id':'br','announcement':{'gateway_id':'gw-test','udp_port':12139,'address_ttl':'30min','announce_interval':'60s'}}}

class ProtocolV2Tests(unittest.TestCase):
    def check(self,data,error=None):
        with tempfile.TemporaryDirectory(prefix='thread-v2-') as d:
            p=Path(d)/'config.yaml';p.write_text(yaml.safe_dump(data,sort_keys=False),encoding='utf-8')
            result=subprocess.run([sys.executable,'-c',WORKER,str(p)],capture_output=True,text=True,encoding='utf-8',errors='replace',cwd=ROOT)
        output=result.stdout+result.stderr
        if error is None:self.assertEqual(result.returncode,0,output)
        else:
            self.assertNotEqual(result.returncode,0,output);self.assertIn(error,output)

    def test_gateway_announcement(self):self.check(gateway())
    def test_gateway_legacy_keys_rejected(self):
        for key,value in [('gateway_id','old'),('discovery',{}),('udp',{'port':12139})]:
            with self.subTest(key=key):
                data=gateway();data['thread_gateway'][key]=value;self.check(data,'Failed config')
    def test_business_public_port_collision(self):
        data=gateway();data['thread_gateway']['announcement']['udp_port']=18512;self.check(data,'ports must be different')
    def test_all_transporter_configs(self):
        for platform in ('esp32','nrf52'):
            for sed in (True,False):
                with self.subTest(platform=platform,sed=sed):self.check(endpoint(platform,sed)[0])
    def test_fixed_gateway_configs(self):
        for platform in ('esp32','nrf52'):
            for sed in (True,False):
                with self.subTest(platform=platform,sed=sed):
                    data,component=endpoint(platform,sed)
                    if sed:data[component]['discovery']={'gateway_id':'gw-fixed','timeout':'1s'}
                    else:data[component]['gateway_id']='gw-fixed'
                    self.check(data)
    def test_revision_removed(self):
        for platform in ('esp32','nrf52'):
            for sed in (True,False):
                with self.subTest(platform=platform,sed=sed):
                    data,component=endpoint(platform,sed);data[component]['manifest_revision']=12;self.check(data,'manifest_revision was removed')
    def test_local_unbind_actions(self):
        for platform in ('esp32','nrf52'):
            for sed in (True,False):
                with self.subTest(platform=platform,sed=sed):
                    data,component=endpoint(platform,sed)
                    data['button']=[{'platform':'template','name':'Local Unbind','on_press':[ {component+'.unbind':'transport'} ]}]
                    self.check(data)
    def test_custom_discovery_removed(self):
        for platform in ('esp32','nrf52'):
            for sed in (True,False):
                for key,value in [('port',12138),('multicast_address','ff13::8000:1234')]:
                    with self.subTest(platform=platform,sed=sed,key=key):
                        data,component=endpoint(platform,sed);data[component]['discovery']={key:value};self.check(data,'Discovery is built in')
    def test_common_header_identical(self):
        self.assertEqual((ROOT/'components/esp32/thread_entity_bridge/transport_protocol.h').read_bytes(),(ROOT/'components/nrf52/thread_entity_bridge_nrf52/transport_protocol.h').read_bytes())
    def test_embedded_management_assets(self):
        base=ROOT/'components/esp32/thread_br/thread_br_web';header=(base/'web_assets.h').read_text()
        for asset in ('thread_gateway.html','static/thread_gateway.js'):
            symbol=re.search(r'\{"/'+re.escape(asset)+r'",\s*"[^"]+",\s*(br_asset_\d+)',header)[1]
            body=re.search(symbol+r'\[\] = \{(.*?)\};',header,re.S)[1]
            compressed=bytes(int(x,16) for x in re.findall(r'0x([0-9a-f]{2})',body))
            self.assertEqual(gzip.decompress(compressed),(base/'frontend'/asset).read_bytes())

if __name__=='__main__':unittest.main(verbosity=2)
