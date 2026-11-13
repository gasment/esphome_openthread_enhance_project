"""Admin-only BR catalog management; keys never leave the HA backend."""
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView, KEY_HASS_USER, StaticPathConfig
from homeassistant.components import panel_custom
from .const import DOMAIN, PANEL_PATH

class ProxyView(HomeAssistantView):
    url = "/api/thread_proxy"
    extra_urls = [f"/api/thread_proxy/{action}" for action in ("bind", "unbind", "reconnect", "create_device", "reconfigure")]
    name = "api:thread_proxy"
    requires_auth = True

    def __init__(self, hass, manager): self.hass, self.manager = hass, manager

    def admin(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not user.is_admin: raise web.HTTPForbidden()

    async def get(self, request):
        self.admin(request)
        return self.json(self.manager.snapshot())

    async def post(self, request):
        self.admin(request)
        try: data = await request.json()
        except (ValueError, TypeError): return self.json({"error": "invalid_input"}, status_code=400)
        if not isinstance(data, dict): return self.json({"error": "invalid_input"}, status_code=400)
        action = request.path.rsplit("/", 1)[-1]
        if action not in ("bind", "unbind", "reconnect", "create_device", "reconfigure"):
            return self.json({"error": "invalid_action"}, status_code=400)
        from .br_device import BrDeviceError
        try:
            if action == "bind":
                inputs = {key: data[key] for key in ("host", "port", "name", "noise_psk", "create_device") if key in data}
                if "candidate_id" in data:
                    if not isinstance(data["candidate_id"], str): raise ValueError("invalid_input")
                    record = self.manager.discovered.get(data["candidate_id"])
                    if record is None or not record.present: raise ValueError("discovery_expired")
                    inputs.update(host=record.host, port=record.port, name=record.name, _proxy_id=record.id)
                if "host" not in inputs: raise ValueError("invalid_host")
                if self.manager.root_entry is None:
                    # First-use bootstrap creates one shared manager, never a BR entry.
                    result = await self.hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"}, data=inputs)
                    if result["type"] != "create_entry":
                        raise ValueError(result.get("reason") or result.get("errors", {}).get("base", "invalid_input"))
                else: await self.manager.bind(inputs)
                return self.json({"ok": True})
            identity = data.get("entry_id")
            binding = self.manager.get_binding(identity) if isinstance(identity, str) else None
            if binding is None: return self.json({"error": "unknown_binding"}, status_code=404)
            if action == "create_device":
                if type(data.get("enabled")) is not bool: raise ValueError("invalid_input")
                await self.manager.set_device(binding, data["enabled"])
            elif action == "reconfigure":
                await self.manager.reconfigure(binding, data)
            elif action == "unbind": await self.manager.unbind(binding)
            else: await self.manager.reconnect(binding)
            return self.json({"ok": True})
        except (ValueError, BrDeviceError, KeyError, TypeError) as err:
            code = str(err)
            allowed = {"invalid_input", "invalid_host", "invalid_port", "invalid_key", "already_configured", "discovery_expired", "manager_not_ready", "br_device_busy", "br_device_has_children"}
            return self.json({"error": code if code in allowed else "invalid_input"},
                             status_code=400 if code in {"invalid_input", "invalid_host", "invalid_port", "invalid_key"} else 409)

async def async_setup_frontend(hass, manager):
    if manager.panel_registered: return
    await hass.http.async_register_static_paths([StaticPathConfig("/thread_proxy_static", str(Path(__file__).parent / "frontend"), cache_headers=False)])
    hass.http.register_view(ProxyView(hass, manager))
    from homeassistant.components.frontend import add_extra_js_url
    add_extra_js_url(hass, "/thread_proxy_static/integration-entry.js?v=0.9.0")
    await panel_custom.async_register_panel(hass, frontend_url_path=PANEL_PATH, webcomponent_name="thread-proxy-panel",
        sidebar_title=None, sidebar_icon="mdi:router-network", module_url="/thread_proxy_static/panel.js?v=0.9.0", require_admin=True)
    manager.panel_registered = True
