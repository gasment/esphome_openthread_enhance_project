"""Zeroconf discovery, encrypted onboarding and key reauthentication."""
import asyncio
import base64
from aioesphomeapi import APIClient, APIConnectionError
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol
from .const import DOMAIN
from .transport import normalize_mac

class ThreadSedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    def __init__(self):
        self.discovered = {}
        self.reauth_entry = None

    async def async_step_zeroconf(self, discovery_info):
        props = discovery_info.properties
        try:
            mac = normalize_mac(props.get('mac', ''))
        except ValueError:
            return self.async_abort(reason='invalid_discovery')
        await self.async_set_unique_id(mac)
        host = str(discovery_info.host)
        # IPv6 host is supplied by HA's interface-aware Zeroconf resolver.
        self._abort_if_unique_id_configured(updates={CONF_HOST:host, CONF_PORT:discovery_info.port})
        self.discovered = {'host':host, 'port':discovery_info.port, 'mac':mac}
        self.context['title_placeholders'] = {'name':props.get('name', discovery_info.hostname.removesuffix('.local.'))}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            data = {**self.discovered, **user_input}
            client = None
            try:
                if len(base64.b64decode(data['noise_psk'], validate=True)) != 32:
                    raise ValueError('Noise key must contain 32 bytes')
                client = APIClient(data['host'], data.get('port',6053), noise_psk=data['noise_psk'], client_info='Thread SED')
                await client.connect(login=False, log_errors=False)
                info = await client.device_info()
                mac = normalize_mac(info.mac_address)
                if self.discovered.get('mac') and mac != normalize_mac(self.discovered['mac']):
                    raise ValueError('Identity mismatch')
                metadata = asyncio.get_running_loop().create_future()
                def call(value):
                    if value.service == 'thread_sed.session' and not metadata.done(): metadata.set_result(value.data)
                client.subscribe_service_calls(call)
                await asyncio.wait_for(metadata,10)
                data['mac'] = info.mac_address
                await self.async_set_unique_id(mac)
                if self.reauth_entry:
                    return self.async_update_reload_and_abort(self.reauth_entry, data_updates=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info.friendly_name or info.name, data=data)
            except (APIConnectionError, OSError, asyncio.TimeoutError):
                errors['base'] = 'cannot_connect'
            except ValueError:
                errors['base'] = 'invalid_auth'
            finally:
                if client: await client.disconnect(force=True)
        fields = {}
        if not self.discovered:
            fields[vol.Required('host')] = str
            fields[vol.Optional('port', default=6053)] = int
        fields[vol.Required('noise_psk')] = str
        return self.async_show_form(step_id='user', data_schema=vol.Schema(fields), errors=errors)

    async def async_step_reauth(self, entry_data):
        self.reauth_entry = self._get_reauth_entry()
        self.discovered = {k:v for k,v in entry_data.items() if k != 'noise_psk'}
        return await self.async_step_user()
