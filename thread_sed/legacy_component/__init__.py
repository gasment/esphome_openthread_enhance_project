"""Thread SED: one UDP/ACK or TCP-compatible device per config entry."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from .const import DOMAIN, PLATFORMS
from .session import Session
from .udp_session import UdpSession
from .udp_listener import UdpListener
from .transport import ReverseListener

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {'sessions':{}, 'listener':None, 'udp_listener':None})
    session = (UdpSession if entry.data.get('transport','tcp')=='udp' else Session)(hass, entry)
    await session.load()
    data['sessions'][session.mac] = session
    try:
        if data['listener'] is None and entry.data.get('transport','tcp')=='tcp':
            listener = ReverseListener(data['sessions'])
            await listener.start()
            data['listener'] = listener
        if data.get('udp_listener') is None:
            listener = UdpListener(data['sessions'])
            await listener.start()
            data['udp_listener'] = listener
        if isinstance(session,UdpSession): session.listener=data['udp_listener']
        entry.runtime_data = session
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        session.task = hass.async_create_background_task(session.run(), f'Thread SED {session.mac}')
        entry.async_on_unload(entry.add_update_listener(_reload))
    except BaseException as err:
        data['sessions'].pop(session.mac, None)
        await session.close()
        if not data['sessions'] and data.get('udp_listener'):
            await data['udp_listener'].close()
            data['udp_listener']=None
        if not data['sessions'] and data['listener']:
            await data['listener'].close()
            data['listener'] = None
        if isinstance(err,OSError):
            raise ConfigEntryNotReady('Cannot listen for Thread SED IPv6 UDP/TCP on port 6064') from err
        raise
    return True

async def _reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    session = entry.runtime_data
    await session.close()
    data = hass.data[DOMAIN]
    data['sessions'].pop(session.mac, None)
    if not data['sessions'] and data.get('udp_listener'):
        await data['udp_listener'].close()
        data['udp_listener']=None
    if not data['sessions'] and data['listener']:
        await data['listener'].close()
        data['listener'] = None
    return True
