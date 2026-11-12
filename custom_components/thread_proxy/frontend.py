"""Admin-only BR management panel and HTTP endpoints."""
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView, KEY_HASS_USER, StaticPathConfig
from homeassistant.components import panel_custom
from .const import DOMAIN, PANEL_PATH

class ProxyView(HomeAssistantView):
    url = "/api/thread_proxy"
    extra_urls = ["/api/thread_proxy/bind", "/api/thread_proxy/unbind", "/api/thread_proxy/reconnect"]
    name = "api:thread_proxy"
    requires_auth = True

    def __init__(self, hass, manager):
        self.hass, self.manager = hass, manager

    def admin(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not user.is_admin:
            raise web.HTTPForbidden()

    async def get(self, request):
        self.admin(request)
        return self.json(self.manager.snapshot())

    async def post(self, request):
        self.admin(request)
        try:
            data = await request.json()
        except (ValueError, TypeError):
            return self.json({"error": "invalid_input"}, status_code=400)
        if not isinstance(data, dict):
            return self.json({"error": "invalid_input"}, status_code=400)
        action = request.path.rsplit("/", 1)[-1]
        if action == "bind":
            if "candidate_id" in data:
                if not isinstance(data["candidate_id"], str):
                    return self.json({"error": "invalid_input"}, status_code=400)
                record = self.manager.discovered.get(data["candidate_id"])
                if record is None or not record.present:
                    return self.json({"error": "discovery_expired"}, status_code=409)
                inputs = {"host": record.host, "port": record.port, "name": record.name,
                          "_proxy_id": record.id, "noise_psk": data.get("noise_psk", "")}
            else:
                inputs = {key: data[key] for key in ("host", "port", "name", "noise_psk") if key in data}
                if "host" not in inputs:
                    return self.json({"error": "invalid_host"}, status_code=400)
            result = await self.hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"}, data=inputs)
            if result["type"] == "create_entry":
                return self.json({"ok": True})
            # Never return FlowResult schemas, config data or encryption keys.
            return self.json({"error": result.get("reason") or result.get("errors", {}).get("base", "invalid_input")}, status_code=409)
        if action not in ("unbind", "reconnect"):
            return self.json({"error": "invalid_action"}, status_code=400)
        entry_id = data.get("entry_id")
        entry = self.hass.config_entries.async_get_entry(entry_id) if isinstance(entry_id, str) else None
        if entry is None or entry.domain != DOMAIN:
            return self.json({"error": "unknown_binding"}, status_code=404)
        if action == "unbind":
            result = await self.hass.config_entries.async_remove(entry_id)
            ok = result.get("require_restart") is not True if isinstance(result, dict) else bool(result)
        else:
            ok = await self.hass.config_entries.async_reload(entry_id)
        return self.json({"ok": ok}, status_code=200 if ok else 409)

async def async_setup_frontend(hass, manager):
    if manager.panel_registered:
        return
    await hass.http.async_register_static_paths([
        StaticPathConfig("/thread_proxy_static", str(Path(__file__).parent / "frontend"), cache_headers=False)])
    hass.http.register_view(ProxyView(hass, manager))
    await panel_custom.async_register_panel(
        hass, frontend_url_path=PANEL_PATH, webcomponent_name="thread-proxy-panel",
        sidebar_title="Thread Proxy", sidebar_icon="mdi:router-network",
        module_url="/thread_proxy_static/panel.js?v=0.5.0", require_admin=True,
        config_panel_domain=DOMAIN)
    manager.panel_registered = True
