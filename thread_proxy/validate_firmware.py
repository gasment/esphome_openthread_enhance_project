"""WSL: config validation and in-memory code generation only; no build commands."""
from pathlib import Path
import subprocess
import sys
import tempfile

if len(sys.argv) > 1 and sys.argv[1] == '--one':
    from esphome.core import CORE
    from esphome.config import read_config
    from esphome.__main__ import generate_cpp_contents
    path=Path(sys.argv[2]); CORE.config_path=path
    config=read_config({},skip_external_update=True,snapshot_user_config=True)
    if config is None: sys.exit(2)
    CORE.config=config
    generate_cpp_contents(config)
    code=CORE.cpp_main_section
    defines={d.name for d in CORE.defines}
    assert 'thread_proxy_api::ThreadProxyAPI' in code
    assert '_thread-proxy' in code
    service=next(s for s in config['mdns']['services'] if s['service']=='_thread-proxy')
    assert service['port']==config['thread_proxy_api']['port']
    assert service['txt']['component']=='thread_proxy_api'
    assert 'get_mac_address()' in str(service['txt']['id'])
    assert ('USE_API' in defines)==('api' in config)
    assert ('USE_THREAD_PROXY_API_NOISE' in defines)==('encryption' in config['thread_proxy_api'])
    assert ('USE_THREAD_PROXY_API_PLAINTEXT' in defines)!=('encryption' in config['thread_proxy_api'])
    assert 'THREAD_SED_API' not in repr(defines)
    if 'api' in config:
        assert 'api::APIServer' in code
    print('PASS in-memory codegen:',path.stem)
    sys.exit(0)

base='''esphome:
  name: br-proxy-test
esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf
wifi:
  ssid: test-network
  password: 12345678
network:
  enable_ipv6: true
logger:
external_components:
  - source:
      type: local
      path: /mnt/c/Users/LZzzW/Documents/GitHub/esphome_openthread_enhance_project/components/esp32
    components: [thread_br, thread_proxy_api]
thread_br:
  id: br_main
  used_psram: false
  thread_network_config:
    channel: 20
    network_name: Proxy-Test
    network_key: "00112233445566778899aabbccddeeff"
    pan_id: 0x1234
    ext_pan_id: "0011223344556677"
    pskc: "00112233445566778899aabbccddeeff"
    mesh_local_prefix: fd12:3456:789a:1::/64
  rcp:
    uart_tx: GPIO18
    uart_rx: GPIO17
    rcp_reset_pin: GPIO7
    rcp_boot_pin: GPIO8
thread_proxy_api:
  id: br_proxy
  thread_br_id: br_main
'''
base=base.replace('password: 12345678','password: "12345678"')
base=base.replace('/mnt/c/Users/LZzzW/Documents/GitHub/esphome_openthread_enhance_project/components/esp32', str(Path(__file__).resolve().parents[1]/'components/esp32'))
key='AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA='
noise=f'  encryption:\n    key: "{key}"\n'
positives={
 'plain':base,
 'implicit_parent':base.replace('  thread_br_id: br_main\n',''),
 'noise':base+noise,
 'coexist_plain':base+'api:\n  reboot_timeout: 0s\n  encryption:\n    key: "'+key+'"\n',
 'coexist_noise':base+noise+'api:\n  reboot_timeout: 0s\n',
 'coexist_both_noise':base+noise+'api:\n  reboot_timeout: 0s\n  encryption:\n    key: "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="\n',
 'custom_port':base+'  port: 16065\n',
}
native=base.replace('esp32-s3-devkitc-1','esp32-c6-devkitc-1').replace('''wifi:
  ssid: test-network
  password: "12345678"
''','''ethernet:
  type: W5500
  clk_pin: GPIO6
  mosi_pin: GPIO7
  miso_pin: GPIO2
  cs_pin: GPIO10
  interrupt_pin: GPIO3
''').replace('''  rcp:
    uart_tx: GPIO18
    uart_rx: GPIO17
    rcp_reset_pin: GPIO7
    rcp_boot_pin: GPIO8
''','''  backbone: eth
  radio: native
''')
positives['native_eth']=native
positives['native_eth_c5']=native.replace('esp32-c6-devkitc-1','esp32-c5-devkitc-1')
negatives={
 'api_port_collision':(base+'api:\n  port: 6065\n','must differ from api.port'),
 'mdns_disabled':(base+'mdns:\n  disabled: true\n','requires mdns'),
 'bad_key':(base+'  encryption:\n    key: invalid\n','key'),
 'zero_key':(base+'  encryption:\n    key: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n','all-zero'),
 'bad_status_interval':(base+'  status_interval: 10ms\n','status_interval'),
 'too_many_clients':(base+'  max_connections: 5\n','max_connections'),
 'duplicate_service':(base+'mdns:\n  services:\n    - service: _thread-proxy\n      protocol: _tcp\n      port: 6065\n','managed by thread_proxy_api'),
 'missing_thread_br':(base[:base.index('thread_br:\n')]+base[base.index('thread_proxy_api:\n'):],'thread_br'),
 'br_web_port_collision':(base.replace('  id: br_main\n','  id: br_main\n  web:\n    port: 6065\n'),'must differ from thread_br.web.port'),
 'unsupported_platform':(base.replace('esp32:\n  board: esp32-s3-devkitc-1\n  framework:\n    type: esp-idf\n','esp8266:\n  board: d1_mini\n'),'esp32'),
 'invalid_parent_id':(base.replace('thread_br_id: br_main','thread_br_id: missing_br'),'missing_br'),
}
workspace=tempfile.TemporaryDirectory(prefix='thread-proxy-validation-')
directory=Path(workspace.name)
for name,content in positives.items():
    path=directory/(name+'.yaml'); path.write_text(content,encoding='utf-8')
    result=subprocess.run([sys.executable,__file__,'--one',str(path)],text=True,capture_output=True)
    if result.returncode:
        print(result.stdout); print(result.stderr); raise AssertionError(name)
    print('PASS',name,flush=True)
for name,(content,error) in negatives.items():
    path=directory/(name+'.yaml'); path.write_text(content,encoding='utf-8')
    result=subprocess.run([sys.executable,'-m','esphome','config',str(path)],text=True,capture_output=True)
    assert result.returncode!=0,(name,result.stdout,result.stderr)
    assert error.lower() in (result.stdout+result.stderr).lower(),(name,result.stdout,result.stderr)
    print('PASS rejected',name,flush=True)
print(f'All {len(positives)+len(negatives)} configuration/code-generation checks passed; no firmware compilation was invoked.')
