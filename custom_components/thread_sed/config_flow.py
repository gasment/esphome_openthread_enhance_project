"""Zeroconf discovery, optional encryption and device onboarding."""
import asyncio
import base64
import logging
from cryptography.exceptions import InvalidTag
from aioesphomeapi import APIClient, APIConnectionError
from aioesphomeapi.core import RequiresEncryptionAPIError
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
try:
    import probatio as vol
except ImportError:
    import voluptuous as vol
from .const import DOMAIN
from .transport import normalize_mac
from .udp_listener import probe_device

_LOGGER=logging.getLogger(__name__)

class ThreadSedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    def __init__(self):
        self.discovered = {}
        self.reauth_entry = None
        self.reconfigure_entry = None

    async def async_step_zeroconf(self, discovery_info):
        props = discovery_info.properties
        try:
            mac = normalize_mac(props.get('mac', ''))
        except (ValueError, InvalidTag):
            return self.async_abort(reason='invalid_discovery')
        await self.async_set_unique_id(mac)
        host = str(discovery_info.host)
        # IPv6 host is supplied by HA's interface-aware Zeroconf resolver.
        self._abort_if_unique_id_configured(updates={CONF_HOST:host, CONF_PORT:discovery_info.port, 'transport':props.get('transport','udp' if '._udp.' in discovery_info.type else 'tcp')})
        self.discovered = {'host':host, 'port':discovery_info.port, 'mac':mac, 'transport':props.get('transport','udp' if '._udp.' in discovery_info.type else 'tcp')}
        self.context['title_placeholders'] = {'name':props.get('name', discovery_info.hostname.removesuffix('.local.'))}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            data = {**self.discovered, **user_input}
            client = None
            try:
                key = (data.get('noise_psk') or '').strip()
                data['noise_psk'] = key
                if key and len(base64.b64decode(key, validate=True)) != 32:
                    raise ValueError('Encryption key must contain 32 bytes')
                data.setdefault('transport','udp')
                if data['transport']=='udp':
                    info=await probe_device(data['host'],data.get('port',6064),
                        base64.b64decode(key) if key else None,data.get('mac','000000000000'))
                    mac=normalize_mac(info.mac_address)
                else:
                    client = APIClient(data['host'], data.get('port',6053), noise_psk=key or None, client_info='Thread SED')
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
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(self.reauth_entry, data_updates=data)
                if self.reconfigure_entry:
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(self.reconfigure_entry, data_updates=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info.friendly_name or info.name, data=data)
            except RequiresEncryptionAPIError:
                errors['base'] = 'encryption_required'
            except (APIConnectionError, OSError, asyncio.TimeoutError) as err:
                _LOGGER.warning('Thread SED cannot connect to [%s]:%s using %s: %s (%s)',data.get('host'),data.get('port',6064),data.get('transport','udp'),err,type(err).__name__)
                errors['base'] = 'cannot_connect'
            except (ValueError, InvalidTag) as err:
                _LOGGER.warning('Thread SED onboarding authentication/identity rejected for [%s]:%s: %s (%s)',data.get('host'),data.get('port',6064),err,type(err).__name__)
                errors['base'] = 'invalid_auth'
            finally:
                if client: await client.disconnect(force=True)
        fields = {}
        if self.reconfigure_entry:
            fields[vol.Required('host',default=self.discovered['host'])]=str
            fields[vol.Required('port',default=self.discovered.get('port',6064))]=int
            fields[vol.Required('transport',default=self.discovered.get('transport','udp'))]=vol.In(['udp','tcp'])
        elif not self.discovered:
            fields[vol.Required('host')] = str
            fields[vol.Optional('port', default=6064)] = int
        fields[vol.Optional('noise_psk')] = str
        return self.async_show_form(step_id='user', data_schema=vol.Schema(fields), errors=errors)

    async def async_step_reauth(self, entry_data):
        self.reauth_entry = self._get_reauth_entry()
        self.discovered = {k:v for k,v in entry_data.items() if k != 'noise_psk'}
        self.discovered.setdefault('transport','tcp')
        return await self.async_step_user()

    async def async_step_reconfigure(self,user_input=None):
        # Update the existing config entry so entity registry identities remain.
        if self.reconfigure_entry is None:
            self.reconfigure_entry=self._get_reconfigure_entry()
            self.discovered=dict(self.reconfigure_entry.data)
            self.discovered.update(transport='udp',port=6064)
        return await self.async_step_user(user_input)
