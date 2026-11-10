'use strict';
const $ = id => document.getElementById(id);
let state = {devices: [], discovered: []}, editing = null, draft = {}, viewing = null, noticeTimer;
const labels = {br: 'BR', gateway: 'Gateway', commissioner: 'Commissioner'};
const statusNames = {online: 'Web 在线', offline: '连接失败', auth_error: '认证失败', http_error: 'HTTP 异常', unknown: '待检测'};
function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function notice(message) {
  $('notice').textContent = message; $('notice').hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').hidden = true, 5000);
}
function loggedOut() {
  closeViewer();
  $('workspace').hidden = true; $('login').hidden = false;
  if ($('editor').open) $('editor').close();
}
async function api(path, method = 'GET', body) {
  const response = await fetch(path, {method, credentials: 'same-origin', cache: 'no-store',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) {
    if (response.status === 401) loggedOut();
    throw new Error(await response.text());
  }
  return response.json();
}
async function refresh() {
  state = await api('/api/state');
  $('workspace').hidden = false; $('login').hidden = true;
  render();
}
function action(container, title, handler, cls) {
  const button = element('button', title, cls);
  button.type = 'button';
  button.onclick = async () => {
    button.disabled = true;
    try { await handler(); } catch (error) { notice(error.message); }
    finally { button.disabled = false; }
  };
  container.append(button);
}
function render() {
  const term = $('filter').value.trim().toLowerCase();
  const devices = state.devices.filter(d => [d.name, d.upstream, d.network_name, d.ext_pan_id, ...d.tags].join(' ').toLowerCase().includes(term));
  $('summary').textContent = `${state.devices.length} 个已接入 BR · ${state.devices.filter(d => d.status.state === 'online').length} 个 Web 在线`;
  if (viewing && !devices.some(device => device.id === viewing.id)) closeViewer();
  const existing = new Map([...$('cards').children].filter(node => node.dataset.id).map(node => [node.dataset.id, node]));
  const visibleIds = new Set(devices.map(device => device.id));
  for (const node of [...$('cards').children]) {
    if (!visibleIds.has(node.dataset.id)) node.remove();
  }
  let index = 0;
  for (const device of devices) {
    const card = existing.get(device.id) || element('article', undefined, 'card');
    card.dataset.id = device.id;
    const row = element('div', undefined, 'br-row');
    const info = element('div', undefined, 'br-info');
    const headline = element('div', undefined, 'headline');
    const name = element('h3', device.name); name.title = device.name;
    headline.append(name, element('span', statusNames[device.status.state] || device.status.state, 'badge ' + device.status.state));
    for (const role of device.roles) headline.append(element('span', labels[role], 'badge'));
    const details = element('div', undefined, 'details');
    const address = element('span', device.upstream, 'address'); address.title = device.upstream;
    details.append(address);
    if (device.network_name) {
      const network = element('span', 'Thread · ' + device.network_name, 'network');
      network.title = device.network_name;
      details.append(network);
    }
    const tags = element('span', undefined, 'tags');
    for (const tag of device.tags) {
      const badge = element('button', '#' + tag, 'badge');
      badge.title = tag;
      badge.onclick = () => {$('filter').value = tag; render();}; tags.append(badge);
    }
    details.append(tags); info.append(headline, details); row.append(info);
    const controls = element('div', undefined, 'actions');
    action(controls, '打开 Web', () => showDevice(device), 'primary');
    if (device.roles.includes('gateway')) action(controls, '网关', () => showDevice(device, '/thread_gateway'));
    if (device.roles.includes('commissioner')) action(controls, '配网', () => showDevice(device, '/commissioner'));
    action(controls, '检测', async () => {await api(`/api/devices/${device.id}/probe`, 'POST', {}); await refresh();});
    action(controls, '编辑', () => edit(device));
    action(controls, '移除', async () => {
      if (!confirm(`从管理台移除「${device.name}」？不会解绑 Thread 节点或修改 BR。`)) return;
      await api(`/api/devices/${device.id}`, 'DELETE', {});
      if (viewing && viewing.id === device.id) closeViewer();
      await refresh();
    }, 'danger');
    row.append(controls);
    if (device.proxy_error) row.append(element('p', device.proxy_error, 'error'));
    const previous = card.querySelector('.br-row');
    if (previous) previous.replaceWith(row); else card.prepend(row);
    // Reuse the expanded card and iframe: a 15-second refresh must not reload the BR page.
    if ($('cards').children[index] !== card) $('cards').insertBefore(card, $('cards').children[index] || null);
    if (viewing?.id === device.id) $('viewerTitle').textContent = device.name;
    index++;
  }
  if (!devices.length) $('cards').append(element('div', term ? '没有匹配的 BR' : '从右侧发现列表接入，或手动添加第一个 BR。', 'empty'));
  $('discoveryError').textContent = state.discovery_error;
  $('discovered').replaceChildren();
  const candidates = state.discovered.filter(record => !isAdmitted(record));
  for (const record of candidates) {
    const item = element('div', undefined, 'discovery');
    item.append(element('strong', record.server?.replace(/\.local\.?$/i, '').replace(/\.$/, '') || '主机名尚未解析'));
    const ipv4 = record.addresses.filter(address => /^\d{1,3}(?:\.\d{1,3}){3}$/.test(address));
    item.append(element('small', ipv4.join(' · ') || '暂无 IPv4 地址'));
    action(item, '接入', () => edit(null, record));
    $('discovered').append(item);
  }
  if (!candidates.length) $('discovered').append(element('div', state.discovered.length ? '暂无未接入的 BR' : '等待 _meshcop 公告… 可先手动添加。', 'empty'));
}
function normalizeHost(value) {
  let host = value.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
  try {host = decodeURIComponent(host);} catch (_) { /* Preserve an incomplete discovery value. */ }
  if (host.includes(':') && !host.includes('%')) {
    try {return new URL(`http://[${host}]/`).hostname.replace(/^\[|\]$/g, '');} catch (_) { /* Compare literally. */ }
  }
  return host;
}
function upstreamHost(upstream) {
  // Browser URL parsers reject scoped IPv6 literals, so retain a literal fallback.
  try {return normalizeHost(new URL(upstream).hostname);} catch (_) {
    const host = upstream.match(/^https?:\/\/(?:\[([^\]]+)\]|([^/:]+))/i);
    return normalizeHost(host?.[1] || host?.[2] || '');
  }
}
function isAdmitted(record) {
  const hosts = new Set([record.server, ...record.addresses].filter(Boolean).map(normalizeHost));
  return state.devices.some(device => (device.service && device.service === record.service) || hosts.has(upstreamHost(device.upstream)));
}
function edit(device = null, discovery = null) {
  editing = device;
  draft = device || {service: discovery?.service || '', network_name: discovery?.network_name || '', ext_pan_id: discovery?.ext_pan_id || ''};
  $('editForm').reset();
  $('editTitle').textContent = device ? '编辑 BR' : '接入 BR';
  $('name').value = device?.name || discovery?.server?.replace(/\.local\.?$/i, '') || '';
  $('tags').value = device?.tags.join(', ') || '';
  let address = discovery?.addresses[0];
  if (address?.includes(':')) address = '[' + address.replace('%', '%25') + ']';
  $('upstream').value = device?.upstream || (address ? `http://${address}:80/` : '');
  $('proxyPort').value = device?.proxy_port || '';
  $('publicUrl').value = device?.public_url || '';
  $('networkName').value = draft.network_name;
  document.querySelectorAll('input[name=role]').forEach(input => input.checked = (device?.roles || ['br']).includes(input.value));
  $('replaceWrap').hidden = !device;
  $('replaceCredentials').checked = !device;
  $('credentials').hidden = !!device;
  $('username').value = ''; $('password').value = ''; $('editError').textContent = '';
  $('editor').showModal();
}
function entryUrl(device, path = '/') {
  const base = device.public_url ? new URL(device.public_url) : new URL(location.origin);
  if (!device.public_url) {base.port = device.proxy_port; base.pathname = '/';}
  if (base.hostname !== location.hostname) throw new Error('代理入口与管理台必须使用相同主机名/IP，才能共享登录会话。请编辑外层反代地址。');
  if (base.origin === location.origin) throw new Error('BR 代理入口不能与管理台共用同一端口。');
  if (location.protocol === 'https:' && base.protocol !== 'https:') throw new Error('HTTPS 管理台需要 HTTPS 代理入口，请填写外层反代地址。');
  return new URL(path, base).href;
}
function showDevice(device, path = '/') {
  const url = entryUrl(device, path);
  const card = [...$('cards').children].find(node => node.dataset.id === device.id);
  if (!card) return;
  const changedDevice = viewing?.id !== device.id;
  const changedUrl = viewing?.url !== url;
  document.querySelector('.card.is-open')?.classList.remove('is-open');
  card.classList.add('is-open');
  // Only move when switching BRs. Leaving the iframe attached preserves its live state.
  if ($('viewer').parentElement !== card) card.append($('viewer'));
  viewing = {id: device.id, url};
  $('viewerTitle').textContent = device.name;
  $('viewer').hidden = false; $('frameWarning').hidden = true;
  $('frameWarning').textContent = '若页面提示未登录，请检查外层反代是否保留 Cookie 和 Host，并确认代理入口已映射到对应端口。';
  if (changedDevice || changedUrl) $('frame').src = url;
  requestAnimationFrame(() => card.scrollIntoView({behavior: 'smooth', block: 'start'}));
}
function closeViewer() {
  $('frame').src = 'about:blank'; $('viewer').hidden = true;
  document.querySelector('.card.is-open')?.classList.remove('is-open');
  // Park the reusable viewer outside cards before a filtered/deleted row is removed.
  $('workspace').querySelector('main').append($('viewer'));
  viewing = null;
}
function updateViewportHeight() {
  document.documentElement.style.setProperty('--viewport-height', `${window.visualViewport?.height || innerHeight}px`);
  if (viewing) requestAnimationFrame(() => document.querySelector('.card.is-open')?.scrollIntoView({block: 'start'}));
}
updateViewportHeight();
window.addEventListener('resize', updateViewportHeight);
window.visualViewport?.addEventListener('resize', updateViewportHeight);
$('loginForm').onsubmit = async event => {
  event.preventDefault(); $('loginError').textContent = '';
  try {
    await api('/api/login', 'POST', {username: $('loginUser').value, password: $('loginPassword').value});
    $('loginPassword').value = ''; await refresh();
  } catch (error) {$('loginError').textContent = error.message;}
};
$('editForm').onsubmit = async event => {
  event.preventDefault(); $('save').disabled = true; $('editError').textContent = '';
  try {
    const body = {...draft, name: $('name').value, tags: $('tags').value.split(/[,，]/).map(x => x.trim()).filter(Boolean),
      upstream: $('upstream').value, proxy_port: $('proxyPort').value, public_url: $('publicUrl').value,
      network_name: $('networkName').value, roles: [...document.querySelectorAll('input[name=role]:checked')].map(x => x.value),
      replace_credentials: $('replaceCredentials').checked, username: $('username').value, password: $('password').value};
    await api(editing ? `/api/devices/${editing.id}` : '/api/devices', editing ? 'PUT' : 'POST', body);
    $('password').value = ''; $('editor').close(); await refresh(); notice('BR 已保存，正在检测连接');
  } catch (error) {$('editError').textContent = error.message;}
  finally {$('save').disabled = false;}
};
$('replaceCredentials').onchange = () => $('credentials').hidden = !$('replaceCredentials').checked;
$('editCancel').onclick = () => {$('password').value = ''; $('editor').close();};
$('add').onclick = () => edit();
$('filter').oninput = render;
$('refresh').onclick = () => refresh().catch(error => notice(error.message));
$('logout').onclick = async () => {try {await api('/api/logout', 'POST', {}); loggedOut();} catch (error) {notice(error.message);}};
$('frameClose').onclick = closeViewer;
$('frameHelp').onclick = () => $('frameWarning').hidden = !$('frameWarning').hidden;
$('frameReload').onclick = () => {if (viewing) $('frame').src = viewing.url;};
$('frameNew').onclick = () => {if (viewing) window.open(viewing.url, '_blank', 'noopener,noreferrer');};
refresh().catch(error => {loggedOut(); if (!error.message.includes('请先登录')) notice(error.message);});
setInterval(() => {if (!$('workspace').hidden && !$('editor').open) refresh().catch(error => notice(error.message));}, 15000);
