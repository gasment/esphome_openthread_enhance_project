"""Local fake-BR tests: no hardware access, firmware generation or compilation."""
import asyncio
import base64
import gzip
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer
from zeroconf import ServiceInfo
from yarl import URL
from app import COOKIE, Manager, Store, base_url, discovery_record, password_matches


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class StoreTests(unittest.TestCase):
    def test_credentials_restart_and_password(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 'admin', 'strong-test-password')
            self.assertTrue(password_matches('strong-test-password', store.setting('password')))
            self.assertFalse(password_matches('wrong', store.setting('password')))
            store.save({'id': 'a', 'name': 'BR A'}, {'username': 'br-user', 'password': 'not-in-cleartext'})
            stored = store.db.execute('SELECT secret FROM devices').fetchone()[0]
            self.assertNotIn('not-in-cleartext', stored)
            store.db.close()
            reopened = Store(directory, 'ignored', '')
            self.assertEqual(reopened.credentials('a')['password'], 'not-in-cleartext')
            self.assertEqual(reopened.setting('username'), 'admin')
            reopened.db.close()

    def test_missing_key_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 'admin', 'strong-test-password')
            store.db.close()
            (Path(directory) / 'credentials.key').unlink()
            with self.assertRaises(RuntimeError):
                Store(directory, 'admin', 'strong-test-password')

    def test_address_and_meshcop_parsing(self):
        self.assertEqual(base_url('http://[fd12::1]:80'), 'http://[fd12::1]/')
        self.assertIn('fe80', base_url('http://[fe80::1%25eth0]:80/'))
        for invalid in ['file:///etc/passwd', 'http://user:pass@host', 'http://host/path', 'http://host/?x=1']:
            with self.assertRaises(web.HTTPBadRequest):
                base_url(invalid)
        info = ServiceInfo('_meshcop._udp.local.', 'test._meshcop._udp.local.', port=49191,
                           server='test.local.', parsed_addresses=['192.168.1.2', 'fd12::1'],
                           properties={b'nn': b'MyThread', b'xp': bytes.fromhex('0123456789abcdef')})
        record = discovery_record(info)
        self.assertEqual(record['addresses'][0], '192.168.1.2')
        self.assertEqual(record['network_name'], 'MyThread')
        self.assertEqual(record['ext_pan_id'], '0123456789abcdef')
        self.assertEqual(record['border_agent_port'], 49191)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, 'admin', 'strong-test-password')
        self.received = []
        self.gzip = gzip.compress(b'<html>fake BR</html>', mtime=0)
        self.fake_status = 200
        async def fake(request):
            self.received.append({'path': request.raw_path, 'headers': dict(request.headers), 'body': await request.read()})
            if request.path == '/redirect':
                return web.Response(status=302, headers={'Location': str(self.br.make_url('/landing'))})
            if request.path == '/external':
                return web.Response(status=302, headers={'Location': 'http://elsewhere.invalid/'})
            return web.Response(status=self.fake_status, body=self.gzip,
                                headers={'Content-Encoding': 'gzip', 'Content-Type': 'text/html', 'Set-Cookie': 'upstream-secret=yes'})
        fake_app = web.Application()
        fake_app.router.add_route('*', '/{path:.*}', fake)
        self.br = TestServer(fake_app)
        await self.br.start_server()
        self.manager = Manager(self.store, bind='127.0.0.1', port=unused_port(), discovery=False)
        self.server = TestServer(self.manager.application())
        await self.server.start_server()
        self.manager.port = self.server.port
        self.client = ClientSession(cookie_jar=CookieJar(unsafe=True), auto_decompress=False)
        self.port = unused_port()
        self.device = None

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()
        await self.br.close()
        self.store.db.close()
        self.directory.cleanup()

    async def request(self, path, method='GET', body=None, origin=True):
        url = self.server.make_url(path)
        headers = {'Origin': str(self.server.make_url('/')).rstrip('/')} if origin else {}
        return await self.client.request(method, url, json=body, headers=headers)

    async def login(self):
        response = await self.request('/api/login', 'POST', {'username': 'admin', 'password': 'strong-test-password'})
        self.assertEqual(response.status, 200, await response.text())
        await response.read()

    async def add(self):
        response = await self.request('/api/devices', 'POST', {
            'name': 'Living room', 'tags': ['2F', 'BR'], 'roles': ['br', 'gateway'],
            'upstream': str(self.br.make_url('/')), 'proxy_port': self.port,
            'username': 'br-user', 'password': 'br-password'})
        self.assertEqual(response.status, 200, await response.text())
        self.device = await response.json()
        # Drain the optional asynchronous connectivity probe before assertions.
        await asyncio.gather(*list(self.manager.tasks))
        self.received.clear()
        return self.device

    async def test_login_csrf_inventory_and_no_secret_response(self):
        response = await self.request('/api/state')
        self.assertEqual(response.status, 401)
        await response.read()
        response = await self.request('/api/login', 'POST', {'username': 'admin', 'password': 'wrong'})
        self.assertEqual(response.status, 401)
        await response.read()
        await self.login()
        cookie = self.client.cookie_jar.filter_cookies(self.server.make_url('/'))[COOKIE].value
        self.assertNotIn(cookie, self.store.db.execute('SELECT hash FROM sessions').fetchone()[0])
        await self.add()
        response = await self.request('/api/state')
        payload = await response.text()
        self.assertNotIn('br-password', payload)
        self.assertNotIn('br-user', payload)
        self.assertEqual(json.loads(payload)['devices'][0]['tags'], ['2F', 'BR'])
        response = await self.request('/api/devices', 'POST', {}, origin=False)
        self.assertEqual(response.status, 403)
        await response.read()

    async def test_proxy_auth_gzip_path_and_cookie_isolation(self):
        await self.login()
        await self.add()
        url = f'http://127.0.0.1:{self.port}/static/a.js?value=a%2Fb&x=1'
        async with self.client.get(URL(url, encoded=True)) as response:
            self.assertEqual(response.status, 200, await response.read())
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
            self.assertEqual(await response.read(), self.gzip)
            self.assertNotIn('Set-Cookie', response.headers)
        last = self.received[-1]
        self.assertEqual(last['headers']['Authorization'], 'Basic ' + base64.b64encode(b'br-user:br-password').decode())
        self.assertNotIn('Cookie', last['headers'])
        self.assertEqual(last['path'], '/static/a.js?value=a%2Fb&x=1')

    async def test_proxy_mutation_origin_and_logout_revoke(self):
        await self.login()
        await self.add()
        url = f'http://127.0.0.1:{self.port}/thread_gateway/api/adopt'
        async with self.client.post(url, json={'node': 'x'}, headers={'Origin': str(self.server.make_url('/')).rstrip('/')}) as response:
            self.assertEqual(response.status, 403)
            await response.read()
        async with self.client.post(url, json={'node': 'x'}, headers={'Origin': f'http://127.0.0.1:{self.port}'}) as response:
            self.assertEqual(response.status, 200, await response.read())
            await response.read()
        self.assertEqual(json.loads(self.received[-1]['body']), {'node': 'x'})
        # Retain old cookie manually, proving revocation is server-side, not just cookie removal.
        token = self.client.cookie_jar.filter_cookies(self.server.make_url('/'))[COOKIE].value
        response = await self.request('/api/logout', 'POST', {})
        self.assertEqual(response.status, 200)
        await response.read()
        async with self.client.get(url, headers={'Cookie': f'{COOKIE}={token}'}) as response:
            self.assertEqual(response.status, 401)
            await response.read()

    async def test_update_preserves_credentials_and_port_conflict(self):
        await self.login()
        device = await self.add()
        response = await self.request('/api/devices/' + device['id'], 'PUT', {**device, 'name': 'Renamed'})
        self.assertEqual(response.status, 200, await response.text())
        await response.read()
        self.assertEqual(self.store.credentials(device['id'])['password'], 'br-password')
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            response = await self.request('/api/devices/' + device['id'], 'PUT', {**device, 'proxy_port': occupied.getsockname()[1]})
            self.assertEqual(response.status, 409)
            await response.read()
        self.assertEqual(self.store.get(device['id'])['proxy_port'], self.port)
        self.assertEqual(self.store.get(device['id'])['name'], 'Renamed')

    async def test_auth_failure_redirect_and_delete(self):
        await self.login()
        device = await self.add()
        url = f'http://127.0.0.1:{self.port}'
        async with self.client.get(url + '/redirect', allow_redirects=False) as response:
            self.assertEqual(response.status, 302)
            self.assertEqual(response.headers['Location'], '/landing')
            await response.read()
        async with self.client.get(url + '/external') as response:
            self.assertEqual(response.status, 502)
            await response.read()
        self.fake_status = 401
        response = await self.request('/api/devices/' + device['id'] + '/probe', 'POST', {})
        self.assertEqual((await response.json())['state'], 'auth_error')
        async with self.client.get(url) as response:
            self.assertEqual(response.status, 502)
            self.assertNotIn('WWW-Authenticate', response.headers)
            await response.read()
        response = await self.request('/api/devices/' + device['id'], 'DELETE', {})
        self.assertEqual(response.status, 200)
        await response.read()
        self.assertIsNone(self.store.get(device['id']))
        self.assertNotIn(device['id'], self.manager.listeners)

    async def test_proxy_rejects_missing_and_expired_session_before_upstream(self):
        await self.login()
        await self.add()
        self.client.cookie_jar.clear()
        async with self.client.get(f'http://127.0.0.1:{self.port}/') as response:
            self.assertEqual(response.status, 401)
            await response.read()
        self.assertEqual(self.received, [])
        await self.login()
        self.store.db.execute('UPDATE sessions SET expires=0')
        self.store.db.commit()
        async with self.client.get(f'http://127.0.0.1:{self.port}/') as response:
            self.assertEqual(response.status, 401)
            await response.read()
        self.assertEqual(self.received, [])

    async def test_restart_restores_proxy_and_session(self):
        await self.login()
        await self.add()
        token = self.client.cookie_jar.filter_cookies(self.server.make_url('/'))[COOKIE].value
        await self.server.close()
        self.manager = Manager(self.store, bind='127.0.0.1', port=unused_port(), discovery=False)
        self.server = TestServer(self.manager.application())
        await self.server.start_server()
        self.manager.port = self.server.port
        async with self.client.get(f'http://127.0.0.1:{self.port}/', headers={'Cookie': f'{COOKIE}={token}'}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), self.gzip)

    async def test_ipv6_upstream(self):
        await self.login()
        device = await self.add()
        async def fake_ipv6(request):
            return web.Response(text='IPv6 BR')
        app = web.Application()
        app.router.add_get('/', fake_ipv6)
        server = TestServer(app, host='::1')
        try:
            await server.start_server()
        except OSError:
            await server.close()
            self.skipTest('IPv6 loopback unavailable on test host')
        try:
            response = await self.request('/api/devices/' + device['id'], 'PUT', {**device, 'upstream': str(server.make_url('/'))})
            self.assertEqual(response.status, 200, await response.read())
            async with self.client.get(f'http://127.0.0.1:{self.port}/') as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.text(), 'IPv6 BR')
        finally:
            await server.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
