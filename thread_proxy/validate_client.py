"""Configuration and in-memory C++ generation only; never compile firmware."""
from pathlib import Path
import importlib
import subprocess
import sys
import tempfile

if len(sys.argv)>1 and sys.argv[1]=='--one':
    from esphome.core import CORE
    from esphome.config import read_config
    from esphome.__main__ import generate_cpp_contents
    CORE.config_path=Path(sys.argv[2])
    config=read_config({},skip_external_update=True,snapshot_user_config=True)
    if config is None:sys.exit(2)
    CORE.config=config;generate_cpp_contents(config)
    defines={d.name for d in CORE.defines};code=CORE.cpp_main_section
    assert 'thread_proxy_client::ThreadProxyClient' in code
    assert 'USE_THREAD_PROXY_CLIENT' in defines and 'USE_THREAD_PROXY_CLIENT_UDP' in defines
    assert not any('NOISE' in d or 'OUTGOING_CONNECTION' in d or d=='USE_API' or 'THREAD_SED' in d for d in defines)
    assert 'noise' not in CORE.loaded_integrations
    assert 'USE_THREAD_PROXY_CLIENT_UDP_ENCRYPTION' in defines if 'encryption' in config['thread_proxy_client'] else 'USE_THREAD_PROXY_CLIENT_UDP_ENCRYPTION' not in defines
    for service in config.get('mdns',{}).get('services',[]):assert service['service'] not in ('_thread-sed','_thread-proxy-client')
    if config['thread_proxy_client']['send_mode']=='manual':assert 'SendAction' in code and 'set_entities' in code
    assert 'get_ready_trigger' in code and 'get_disconnect_trigger' in code
    if 'encryption' in config['thread_proxy_client']:assert 'set_udp_key' in code
    print('PASS codegen',CORE.target_platform,config['thread_proxy_client']['send_mode']);sys.exit(0)

root=Path(__file__).resolve().parents[1]
common='''esphome:
  name: thread-proxy-client-check
network:
  enable_ipv6: true
logger:
  baud_rate: 0
sensor:
  - platform: template
    id: voltage
    name: Voltage
    lambda: return 3.1;
binary_sensor:
  - platform: template
    id: door
    name: Door
event:
  - platform: template
    id: key_event
    name: Key
    event_types: [press, hold]
text_sensor:
  - platform: template
    id: version_text
    name: Version
thread_proxy_client:
  id: proxy_client
  send_mode: MODE
  on_ready:
    - logger.log: BR ready
  on_disconnect:
    - logger.log: BR unavailable
'''
manual='''script:
  - id: report
    then:
      - component.update: voltage
      - wait_until:
          condition:
            thread_proxy_client.is_ready: proxy_client
          timeout: 3s
      - thread_proxy_client.send:
          id: proxy_client
          entity_id: [voltage, door, key_event, version_text]
          ack_timeout: 500ms
          ack_resend: 2
          on_success:
            - logger.log: Queued at BR
          on_error:
            - logger.log: Failed
'''
def config(platform,mode='manual',encryption=False):
    directory=root/'components'/platform
    if platform=='nrf52':
        header='''nrf52:
  board: adafruit_feather_nrf52840/nrf52840
  framework:
    version: 2.9.2
external_components:
  - source:
      type: local
      path: DIRECTORY
    components: [openthread_nrf52, thread_proxy_client, deep_sleep_nrf52]
openthread_nrf52:
  id: thread_main
  device_type: MTD
  poll_period: 5s
  thread_network_config:
    network_key: "00112233445566778899aabbccddeeff"
deep_sleep_nrf52:
  id: sleep_control
  sleep_duration: 30min
'''
    else:
        header='''esp32:
  board: esp32-c6-devkitc-1
  framework:
    type: esp-idf
external_components:
  - source:
      type: local
      path: DIRECTORY
    components: [openthread, thread_proxy_client]
openthread:
  id: thread_main
  device_type: MTD
  poll_period: 5s
  thread_network_config:
    network_key: "00112233445566778899aabbccddeeff"
'''
    body=common.replace('MODE',mode)
    if platform=='nrf52':body+='  deep_sleep_id: sleep_control\n'
    if encryption:body+='  encryption:\n    key: "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="\n'
    if mode=='auto':body+='  ack_timeout: 500ms\n  ack_resend: 2\n  on_ack:\n    - logger.log: Auto queued\n  on_ack_error:\n    - logger.log: Auto failed\n'
    return header.replace('DIRECTORY',str(directory))+body+(manual if mode=='manual' else '')

positives={f'{p}_{m}_{e}':config(p,m,e) for p in ('nrf52','esp32') for m in ('auto','manual') for e in (False,True)}
positives['reed']=config('esp32').replace('device_type: MTD\n  poll_period: 5s','device_type: FTD')
base=config('nrf52')
negatives={
 'manual_component_ack':base.replace('  send_mode: manual','  send_mode: manual\n  ack_timeout: 1s'),
 'manual_component_callback':base.replace('  send_mode: manual','  send_mode: manual\n  on_ack:\n    - logger.log: Bad'),
 'auto_send':config('nrf52','auto')+manual,
 'tcp':base.replace('  send_mode: manual','  send_mode: manual\n  transport: tcp'),
 'bad_key':base.replace('  send_mode: manual','  send_mode: manual\n  encryption:\n    key: bad'),
 'bad_ack':base.replace('ack_resend: 2','ack_resend: -1'),
 'bad_timeout':base.replace('ack_timeout: 500ms','ack_timeout: 0ms'),
 'api_conflict':base+'api:\n',
 'old_client_conflict':base.replace('thread_proxy_client, deep_sleep_nrf52','thread_proxy_client, thread_sed_api, deep_sleep_nrf52')+'thread_sed_api:\n',
}
with tempfile.TemporaryDirectory(prefix='proxy-client-validation-') as temp:
    for name,content in positives.items():
        file=Path(temp)/(name+'.yaml');file.write_text(content)
        result=subprocess.run([sys.executable,__file__,'--one',str(file)],capture_output=True,text=True)
        if result.returncode:print(result.stdout,result.stderr);raise AssertionError(name)
        print('PASS',name,flush=True)
    for name,content in negatives.items():
        file=Path(temp)/(name+'.yaml');file.write_text(content)
        result=subprocess.run([sys.executable,'-m','esphome','config',str(file)],capture_output=True,text=True)
        assert result.returncode,(name,result.stdout,result.stderr)
        print('PASS rejected',name,flush=True)
print(f'{len(positives)+len(negatives)} client checks passed; no firmware compilation invoked.')
