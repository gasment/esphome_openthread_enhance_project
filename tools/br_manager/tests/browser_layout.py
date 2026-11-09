"""Optional browser regression: pip install playwright, then playwright install chromium.

Run: python tests/browser_layout.py
Set BR_BROWSER_EXECUTABLE to use an existing Chrome/Edge, and BR_SCREENSHOT_DIR for screenshots.
Uses only temporary data and local simulated BRs; no LAN discovery or real device access.
"""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from app import Manager, Store


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def check_fits(page):
    await page.wait_for_function("""() => {
      const card = document.querySelector('.card.is-open');
      if (!card) return false;
      const box = card.getBoundingClientRect();
      return box.top >= -1 && box.top <= 25 && box.bottom <= innerHeight + 1;
    }""", timeout=5000)
    assert await page.locator('#frame').evaluate('(node) => node.getBoundingClientRect().height > 200')
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')


@contextmanager
def temporary_store(directory):
    store = Store(directory, 'admin', 'strong-test-password')
    try:
        yield store
    finally:
        store.db.close()


async def main():
    with tempfile.TemporaryDirectory() as directory, temporary_store(directory) as store:
        requests = []
        async def fake(request):
            requests.append(request.path)
            if request.path == '/control':
                await request.read()
                return web.json_response({'ok': True})
            return web.Response(text='''<!doctype html><html><body>
                <h1 id="ok">模拟 BR · 页面状态保留测试</h1><input id="draft" placeholder="未提交的设置">
                <p>下面的内容在 BR 页面内滚动。</p><div style="height:1500px;background:linear-gradient(#eef5ff,#fff)"></div>
                <script>window.pageSession=Math.random().toString();fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});</script>
                </body></html>''', content_type='text/html')
        fake_app = web.Application()
        fake_app.router.add_route('*', '/{path:.*}', fake)
        async with TestServer(fake_app) as br:
            ports = set()
            for i, name in enumerate(['一楼客厅 BR', '二楼卧室 BR', '书房 BR', '地下室 BR']):
                port = unused_port()
                while port in ports:
                    port = unused_port()
                ports.add(port)
                store.save({'id': str(i), 'name': name, 'tags': ['二楼', '卧室'] if i == 1 else ['区域' + str(i)],
                            'roles': ['br', 'gateway', 'commissioner'], 'upstream': str(br.make_url('/')),
                            'proxy_port': port, 'public_url': '', 'service': 'saved._meshcop._udp.local.' if i == 0 else '',
                            'network_name': 'Thread-Area', 'ext_pan_id': '0123456789abcdef'},
                           {'username': 'br-user', 'password': 'br-password'})
            manager = Manager(store, bind='127.0.0.1', discovery=False)
            for name, address in [('saved', '192.0.2.10'), ('manual', '127.0.0.1'), ('new', '192.0.2.222')]:
                service = name + '._meshcop._udp.local.'
                manager.discovered[service] = {'service': service, 'server': name + '.local.', 'addresses': [address],
                    'network_name': 'Thread-Area', 'ext_pan_id': '0123456789abcdef', 'vendor': 'ESPHome',
                    'model': 'ESP32-C5', 'border_agent_port': 49191, 'present': True, 'last_seen': 0}
            async with TestServer(manager.application()) as server:
                manager.port = server.port
                async with async_playwright() as playwright:
                    executable = os.getenv('BR_BROWSER_EXECUTABLE')
                    browser = await playwright.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
                    page = await browser.new_page(viewport={'width': 1440, 'height': 900})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(str(server.make_url('/')))
                    await page.locator('#login').wait_for(state='visible')
                    await page.locator('#loginPassword').fill('strong-test-password')
                    await page.locator('#loginForm button').click()
                    await page.locator('#workspace').wait_for(state='visible')
                    cards = page.locator('#cards > .card')
                    assert await cards.count() == 4
                    boxes = [await card.bounding_box() for card in await cards.all()]
                    assert all(abs(box['x'] - boxes[0]['x']) < 1 for box in boxes)
                    assert all(box['height'] < 100 for box in boxes), boxes
                    # Exclude saved service and manually added address; keep another BR on the same Thread network.
                    assert await page.locator('#discovered .discovery').count() == 1
                    assert '192.0.2.222' in await page.locator('#discovered').inner_text()
                    assert '192.0.2.10' not in await page.locator('#discovered').inner_text()
                    second = page.locator('.card[data-id="1"]')
                    await second.get_by_role('button', name='网关', exact=True).click()
                    await page.frame_locator('#frame').locator('#ok').wait_for()
                    await check_fits(page)
                    assert await page.locator('#viewer').evaluate('(node) => node.parentElement.dataset.id') == '1'
                    assert await page.evaluate("document.querySelector('.card[data-id=\"2\"]').getBoundingClientRect().top >= document.querySelector('.card[data-id=\"1\"]').getBoundingClientRect().bottom")
                    await page.frame_locator('#frame').locator('#draft').fill('不能因刷新丢失')
                    session = await page.frames[1].evaluate('window.pageSession')
                    before = requests.count('/thread_gateway')
                    await page.evaluate('refresh()')
                    assert await page.frames[1].evaluate('window.pageSession') == session
                    assert await page.frame_locator('#frame').locator('#draft').input_value() == '不能因刷新丢失'
                    assert requests.count('/thread_gateway') == before
                    await page.locator('#filter').fill('卧室')
                    assert await cards.count() == 1
                    assert await page.frames[1].evaluate('window.pageSession') == session
                    await page.locator('#filter').fill('')
                    await page.locator('.card[data-id="1"]').get_by_role('button', name='网关', exact=True).click()
                    await check_fits(page)
                    assert await page.frames[1].evaluate('window.pageSession') == session
                    screenshots = Path(os.getenv('BR_SCREENSHOT_DIR', directory))
                    screenshots.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(screenshots / 'br_manager_compact_desktop.png'))
                    # Switching moves the same viewer below the new row and collapses the old one.
                    await page.locator('.card[data-id="3"]').get_by_role('button', name='配网', exact=True).click()
                    await page.frame_locator('#frame').locator('#ok').wait_for()
                    await check_fits(page)
                    assert await page.locator('.card.is-open').count() == 1
                    assert await page.locator('#viewer').evaluate('(node) => node.parentElement.dataset.id') == '3'
                    for width, height in [(1280, 720), (390, 844), (844, 390)]:
                        await page.set_viewport_size({'width': width, 'height': height})
                        await check_fits(page)
                        await page.screenshot(path=str(screenshots / f'br_manager_compact_{width}x{height}.png'))
                    await page.set_viewport_size({'width': 1440, 'height': 900})
                    await page.locator('#frameClose').click()
                    assert await page.locator('.card.is-open').count() == 0
                    await page.locator('.card[data-id="1"]').get_by_role('button', name='网关', exact=True).click()
                    await page.frame_locator('#frame').locator('#ok').wait_for()
                    await page.locator('#filter').fill('不存在')
                    assert await page.locator('#viewer').is_hidden()
                    await page.locator('#filter').fill('')
                    await page.locator('.card[data-id="1"]').get_by_role('button', name='网关', exact=True).click()
                    await page.frame_locator('#frame').locator('#ok').wait_for()
                    await page.locator('.card[data-id="1"]').get_by_role('button', name='移除', exact=True).scroll_into_view_if_needed()
                    page.once('dialog', lambda dialog: dialog.accept())
                    await page.locator('.card[data-id="1"]').get_by_role('button', name='移除', exact=True).click()
                    await page.wait_for_function("document.querySelectorAll('#cards>.card').length === 3")
                    assert await page.locator('#viewer').is_hidden()
                    await page.locator('#logout').click()
                    await page.locator('#login').wait_for(state='visible')
                    assert not errors, errors
                    await browser.close()
                    print('Browser layout checks passed: compact single column, inline viewer, viewport fit at four sizes, iframe state preservation, discovery exclusion, switching/filtering/deletion and logout.')


if __name__ == '__main__':
    asyncio.run(main())
