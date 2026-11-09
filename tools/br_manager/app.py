"""Standalone BR inventory, MeshCoP discovery and authenticated HTTP reverse proxy."""
import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, encode_basic_auth, web
from cryptography.fernet import Fernet
from yarl import URL
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

LOG = logging.getLogger("br_manager")
COOKIE = "br_manager_session"
SERVICE = "_meshcop._udp.local."
ROLES = {"br", "gateway", "commissioner"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade"}


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def password_matches(password, saved):
    return hmac.compare_digest(password_hash(password, bytes.fromhex(saved.split(":")[0])), saved)


def base_url(value):
    """Validate an origin, including bracketed IPv6 and optional link-local scope."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.path not in {"", "/"} or parts.query or parts.fragment or
                any(c.isspace() for c in value) or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        return str(URL(value.strip()).with_path("/"))
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="地址应为 http(s)://主机:端口；IPv6 地址请加方括号，不带路径或凭据")


def addresses_from_info(info):
    addresses = info.parsed_scoped_addresses(IPVersion.All)
    def score(address):
        ip = ipaddress.ip_address(address.split("%")[0])
        return (ip.is_link_local, ip.version != 4, address)
    return sorted(set(addresses), key=score)


def discovery_record(info):
    props = info.properties
    def value(key, binary=False):
        raw = props.get(key.encode(), b"") or b""
        return raw.hex() if binary else raw.decode("utf-8", errors="replace")
    return {"service": info.name, "server": info.server, "addresses": addresses_from_info(info),
            "network_name": value("nn"), "ext_pan_id": value("xp", True),
            "vendor": value("vn"), "model": value("mn"), "thread_version": value("tv"),
            "border_agent_port": info.port, "last_seen": int(time.time()), "present": True}


class Store:
    def __init__(self, directory, username, password):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        keyfile = directory / "credentials.key"
        if not keyfile.exists():
            # Never silently replace a missing key for an existing inventory.
            if (directory / "manager.sqlite3").exists():
                raise RuntimeError("credentials.key missing; restore it together with the database")
            with keyfile.open("xb") as f:
                os.chmod(keyfile, 0o600)
                f.write(Fernet.generate_key())
        self.crypto = Fernet(keyfile.read_bytes())
        self.db = sqlite3.connect(directory / "manager.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, config TEXT NOT NULL, secret TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
        """)
        if not self.setting("password"):
            if len(password) < 12 or password == "replace-with-a-long-random-password":
                raise RuntimeError("Set BR_ADMIN_PASSWORD to at least 12 characters for first startup")
            self.db.executemany("INSERT INTO settings VALUES (?,?)", [("username", username), ("password", password_hash(password))])
            self.db.commit()

    def setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def devices(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT config FROM devices ORDER BY rowid")]

    def get(self, device_id):
        row = self.db.execute("SELECT config FROM devices WHERE id=?", (device_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def credentials(self, device_id):
        row = self.db.execute("SELECT secret FROM devices WHERE id=?", (device_id,)).fetchone()
        return json.loads(self.crypto.decrypt(row[0].encode())) if row else {"username": "", "password": ""}

    def save(self, config, credentials):
        secret = self.crypto.encrypt(json.dumps(credentials).encode()).decode()
        self.db.execute("INSERT OR REPLACE INTO devices VALUES (?,?,?)", (config["id"], json.dumps(config), secret))
        self.db.commit()

    def delete(self, device_id):
        self.db.execute("DELETE FROM devices WHERE id=?", (device_id,))
        self.db.commit()

    def session(self, request):
        token = request.cookies.get(COOKIE, "")
        if not token:
            return False
        row = self.db.execute("SELECT expires FROM sessions WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return bool(row and row[0] > time.time())

    def new_session(self, hours):
        token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM sessions WHERE expires<=?", (int(time.time()),))
        self.db.execute("INSERT INTO sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), int(time.time() + hours * 3600)))
        self.db.commit()
        return token

    def logout(self, request):
        token = request.cookies.get(COOKIE, "")
        self.db.execute("DELETE FROM sessions WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        self.db.commit()


class Manager:
    def __init__(self, store, bind="0.0.0.0", port=8080, port_start=18001, port_end=18100,
                 secure=False, session_hours=12, discovery=True):
        self.store, self.bind, self.port = store, bind, port
        if not (1024 <= port <= 65535 and 1024 <= port_start <= port_end <= 65535 and 1 <= session_hours <= 720):
            raise ValueError("管理台/代理端口应为 1024–65535，端口范围有序；会话时长应为 1–720 小时")
        self.port_start, self.port_end = port_start, port_end
        self.secure, self.session_hours, self.discovery_enabled = secure, session_hours, discovery
        self.listeners, self.status, self.discovered = {}, {}, {}
        self.discovery_error = ""
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.login_attempts = {}
        self.client = self.zc = self.browser = self.poller = None

    def authenticated(self, request):
        if not self.store.session(request):
            raise web.HTTPUnauthorized(text="请先登录管理台，再刷新此入口")

    @staticmethod
    def check_origin(request):
        # SameSite cookies do not isolate ports. Require an exact origin authority.
        origin = request.headers.get("Origin", "")
        if not origin or urlsplit(origin).netloc.lower() != request.host.lower():
            raise web.HTTPForbidden(text="请求来源不匹配；反向代理请保留原始 Host")

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(completed):
            self.tasks.discard(completed)
            if not completed.cancelled() and completed.exception():
                LOG.error("Background task failed: %s", type(completed.exception()).__name__)
        task.add_done_callback(finished)
        return task

    def application(self):
        @web.middleware
        async def guard(request, handler):
            try:
                if request.path.startswith("/api/"):
                    if request.method not in {"GET", "HEAD"}:
                        self.check_origin(request)
                    if request.path != "/api/login":
                        self.authenticated(request)
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.Response(status=exc.status, text=exc.text, headers=exc.headers)
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = web.Response(status=400, text="无效的 JSON 请求")
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            return response
        app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
        app.add_routes([web.get("/", self.index), web.get("/static/app.js", self.javascript),
                        web.post("/api/login", self.login), web.post("/api/logout", self.logout),
                        web.get("/api/state", self.state), web.post("/api/devices", self.save_device),
                        web.put("/api/devices/{id}", self.save_device),
                        web.delete("/api/devices/{id}", self.delete_device),
                        web.post("/api/devices/{id}/probe", self.manual_probe)])
        app.cleanup_ctx.append(self.lifecycle)
        return app

    async def index(self, request):
        return web.FileResponse(Path(__file__).parent / "static/index.html")

    async def javascript(self, request):
        return web.FileResponse(Path(__file__).parent / "static/app.js")

    async def login(self, request):
        now = time.time()
        # Bound the limiter; use socket peer, never trust arbitrary forwarded headers.
        self.login_attempts = {key: value for key, value in self.login_attempts.items() if value[0] > now - 300}
        key = request.remote or "unknown"
        started, count = self.login_attempts.get(key, (now, 0))
        if count >= 10 or len(self.login_attempts) >= 1024:
            raise web.HTTPTooManyRequests(text="登录尝试过多，请五分钟后重试")
        self.login_attempts[key] = (started, count + 1)
        data = await request.json()
        user_ok = hmac.compare_digest(str(data.get("username", "")).encode(), self.store.setting("username").encode())
        saved = self.store.setting("password")
        password_ok = await asyncio.to_thread(password_matches, str(data.get("password", "")), saved)
        if not user_ok or not password_ok:
            raise web.HTTPUnauthorized(text="用户名或密码错误")
        self.login_attempts.pop(key, None)
        self.store.logout(request)
        response = web.json_response({"ok": True})
        response.set_cookie(COOKIE, self.store.new_session(self.session_hours),
                            httponly=True, secure=self.secure, samesite="Strict", path="/",
                            max_age=self.session_hours * 3600)
        return response

    async def logout(self, request):
        self.store.logout(request)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def state(self, request):
        devices = []
        for config in self.store.devices():
            devices.append({**config, "has_credentials": bool(self.store.credentials(config["id"])["username"]),
                            "status": self.status.get(config["id"], {"state": "unknown"}),
                            "proxy_error": self.listeners.get(config["id"], {}).get("error", "")})
        return web.json_response({"devices": devices,
                                  "discovered": sorted(self.discovered.values(), key=lambda x: x["service"]),
                                  "discovery_error": self.discovery_error,
                                  "port_start": self.port_start, "port_end": self.port_end})

    def validate_config(self, data, old=None):
        try:
            name = str(data.get("name", "")).strip()
            tags = list(dict.fromkeys(str(x).strip() for x in data.get("tags", []) if str(x).strip()))
            roles = list(dict.fromkeys(data.get("roles", ["br"])))
            if not name or len(name) > 80 or len(tags) > 16 or any(len(t) > 32 for t in tags) or not set(roles) <= ROLES:
                raise ValueError
            port = int(data.get("proxy_port") or 0)
            used = {d["proxy_port"] for d in self.store.devices() if not old or d["id"] != old["id"]}
            if not port:
                port = next((p for p in range(self.port_start, self.port_end + 1) if p not in used and p != self.port), 0)
            if not 1024 <= port <= 65535 or port == self.port or port in used:
                raise ValueError
            return {"id": old["id"] if old else secrets.token_hex(8), "name": name, "tags": tags,
                    "roles": roles, "upstream": base_url(str(data.get("upstream", ""))), "proxy_port": port,
                    "public_url": base_url(str(data["public_url"])) if data.get("public_url") else "",
                    "service": str(data.get("service", ""))[:255],
                    "network_name": str(data.get("network_name", ""))[:80],
                    "ext_pan_id": str(data.get("ext_pan_id", ""))[:32]}
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="检查名称、TAG、能力和代理端口；端口必须唯一且位于 1024–65535")

    async def save_device(self, request):
        data = await request.json()
        async with self.lock:
            old = self.store.get(request.match_info["id"]) if "id" in request.match_info else None
            if "id" in request.match_info and not old:
                raise web.HTTPNotFound(text="BR 不存在")
            config = self.validate_config(data, old)
            credentials = self.store.credentials(config["id"]) if old else {"username": "", "password": ""}
            if data.get("replace_credentials") or not old:
                credentials = {"username": str(data.get("username", "")), "password": str(data.get("password", ""))}
                if ":" in credentials["username"] or len(credentials["username"]) > 128 or len(credentials["password"]) > 256:
                    raise web.HTTPBadRequest(text="凭据过长或用户名包含冒号")
            previous = self.listeners.get(config["id"])
            replacement = None
            if not previous or previous.get("error") or previous["port"] != config["proxy_port"]:
                try:
                    replacement = await self.listen(config["id"], config["proxy_port"])
                except OSError:
                    raise web.HTTPConflict(text="代理端口无法监听，可能已被其他程序占用")
            try:
                self.store.save(config, credentials)
            except Exception:
                if replacement:
                    await replacement["runner"].cleanup()
                raise
            if replacement:
                self.listeners[config["id"]] = replacement
                if previous and previous.get("runner"):
                    await previous["runner"].cleanup()
            self.status.pop(config["id"], None)
            self.spawn(self.probe(config["id"]))
            return web.json_response(config)

    async def delete_device(self, request):
        async with self.lock:
            device_id = request.match_info["id"]
            self.store.delete(device_id)
            listener = self.listeners.pop(device_id, None)
            if listener and listener.get("runner"):
                await listener["runner"].cleanup()
            self.status.pop(device_id, None)
        return web.json_response({"ok": True})

    async def manual_probe(self, request):
        device_id = request.match_info["id"]
        if not self.store.get(device_id):
            raise web.HTTPNotFound()
        return web.json_response(await self.probe(device_id))

    async def probe(self, device_id):
        config = self.store.get(device_id)
        if not config:
            return {}
        creds = self.store.credentials(device_id)
        status = {"checked_at": int(time.time())}
        try:
            async with self.client.get(config["upstream"], headers={"Authorization": encode_basic_auth(creds["username"], creds["password"])}, allow_redirects=False,
                                       timeout=ClientTimeout(total=5)) as response:
                await response.content.read(1024)
                status.update(state="auth_error" if response.status in {401, 403} else
                              "online" if 200 <= response.status < 400 else "http_error", code=response.status)
        except (ClientError, asyncio.TimeoutError, OSError):
            status["state"] = "offline"
        if self.store.get(device_id) == config:
            self.status[device_id] = status
        return status

    async def listen(self, device_id, port):
        app = web.Application(client_max_size=1024 * 1024)
        async def handler(request):
            return await self.proxy(request, device_id)
        app.router.add_route("*", "/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.bind, port).start()
        except Exception:
            await runner.cleanup()
            raise
        return {"runner": runner, "port": port, "error": ""}

    async def proxy(self, request, device_id):
        self.authenticated(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.check_origin(request)
        config = self.store.get(device_id)
        if not config:
            raise web.HTTPNotFound()
        # The target is selected only by this listening port, never a caller URL/header.
        if request.headers.get("Upgrade", "").lower() == "websocket":
            raise web.HTTPNotImplemented(text="第一版代理支持 HTTP；当前 BR Web 不使用 WebSocket")
        excluded = HOP | {"host", "cookie", "authorization", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}
        excluded |= {x.strip().lower() for x in request.headers.get("Connection", "").split(",")}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded}
        target = URL(config["upstream"].rstrip("/") + request.rel_url.raw_path_qs, encoded=True)
        credentials = self.store.credentials(device_id)
        headers["Authorization"] = encode_basic_auth(credentials["username"], credentials["password"])
        try:
            async with self.client.request(request.method, target, headers=headers,
                                           data=request.content.iter_chunked(65536) if request.can_read_body else None,
                                           allow_redirects=False, timeout=ClientTimeout(total=None, sock_connect=5, sock_read=60)) as upstream:
                if upstream.status == 401:
                    raise web.HTTPBadGateway(text="BR 认证失败，请在管理台修改 BR 凭据")
                excluded_response = HOP | {"set-cookie", "www-authenticate"}
                excluded_response |= {x.strip().lower() for x in upstream.headers.get("Connection", "").split(",")}
                response = web.StreamResponse(status=upstream.status)
                for k, v in upstream.headers.items():
                    if k.lower() not in excluded_response:
                        response.headers.add(k, v)
                location = response.headers.get("Location")
                if location:
                    try:
                        redirect = target.join(URL(location))
                        if redirect.origin() != URL(config["upstream"]).origin():
                            raise web.HTTPBadGateway(text="BR 返回了外部重定向，已阻止")
                        response.headers["Location"] = str(redirect.relative())
                    except ValueError:
                        raise web.HTTPBadGateway(text="BR 返回了不支持的重定向")
                response.headers["Cache-Control"] = "no-store"
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, asyncio.TimeoutError, OSError):
            raise web.HTTPBadGateway(text="BR 无法连接或响应超时")

    def service_changed(self, zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Removed:
            if name in self.discovered:
                self.discovered[name]["present"] = False
        else:
            self.spawn(self.resolve(service_type, name))

    async def resolve(self, service_type, name):
        try:
            info = AsyncServiceInfo(service_type, name)
            if await info.async_request(self.zc.zeroconf, 3000):
                self.discovered[name] = discovery_record(info)
                # Bound stale inventory; discoveries never change a saved credential target.
                if len(self.discovered) > 512:
                    oldest = min(self.discovered, key=lambda x: self.discovered[x]["last_seen"])
                    del self.discovered[oldest]
        except Exception as exc:
            LOG.warning("mDNS resolve failed: %s", type(exc).__name__)

    async def poll(self):
        while True:
            # Sequential, lightweight HTTP reachability only; never trigger topology scans.
            for config in self.store.devices():
                await self.probe(config["id"])
            await asyncio.sleep(60)

    async def lifecycle(self, app):
        self.client = ClientSession(auto_decompress=False, cookie_jar=DummyCookieJar(), trust_env=False)
        try:
            for config in self.store.devices():
                try:
                    self.listeners[config["id"]] = await self.listen(config["id"], config["proxy_port"])
                except OSError:
                    self.listeners[config["id"]] = {"port": config["proxy_port"], "error": "端口无法监听，请编辑并更换端口"}
            if self.discovery_enabled:
                try:
                    interfaces = [x.strip() for x in os.getenv("BR_MDNS_INTERFACES", "").split(",") if x.strip()]
                    self.zc = AsyncZeroconf(ip_version=IPVersion.All, **({"interfaces": interfaces} if interfaces else {}))
                    self.browser = AsyncServiceBrowser(self.zc.zeroconf, SERVICE, handlers=[self.service_changed])
                except Exception as exc:
                    self.discovery_error = "mDNS 初始化失败：" + type(exc).__name__ + "；检查网卡、UDP 5353 和 host 网络"
                    LOG.warning(self.discovery_error)
            self.poller = asyncio.create_task(self.poll())
            yield
        finally:
            if self.browser:
                await self.browser.async_cancel()
            if self.poller:
                self.poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.poller
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.zc:
                await self.zc.async_close()
            for listener in self.listeners.values():
                if listener.get("runner"):
                    await listener["runner"].cleanup()
            await self.client.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    os.umask(0o077)
    directory = os.getenv("BR_DATA_DIR", "data")
    store = Store(directory, os.getenv("BR_ADMIN_USER", "admin"), os.getenv("BR_ADMIN_PASSWORD", ""))
    if sys.argv[1:] == ["reset-password"]:
        import getpass
        password = getpass.getpass("New administrator password: ")
        if len(password) < 12 or password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords must match and contain at least 12 characters")
        store.db.execute("UPDATE settings SET value=? WHERE key='password'", (password_hash(password),))
        store.db.execute("DELETE FROM sessions")
        store.db.commit()
        print("Administrator password updated; all sessions revoked.")
        return
    manager = Manager(store, bind=os.getenv("BR_BIND", "0.0.0.0"), port=int(os.getenv("BR_PORT", "8080")),
                      port_start=int(os.getenv("BR_PROXY_PORT_START", "18001")), port_end=int(os.getenv("BR_PROXY_PORT_END", "18100")),
                      secure=os.getenv("BR_COOKIE_SECURE", "false").lower() == "true",
                      session_hours=int(os.getenv("BR_SESSION_HOURS", "12")))
    web.run_app(manager.application(), host=manager.bind, port=manager.port, access_log=None)


if __name__ == "__main__":
    main()
