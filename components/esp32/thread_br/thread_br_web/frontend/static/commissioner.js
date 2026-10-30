'use strict';
(function() {
  var selected = '', reveal = false, latest = null, submitting = false, timer, activePage = true;
  var byId = function(id) { return document.getElementById(id); };
  var phases = {idle:'空闲', queued:'已提交', petitioning:'申请配网角色', waiting:'等待节点',
    connecting:'节点正在连接', authenticating:'节点已认证', finalizing:'正在完成配网',
    complete:'配网会话完成', cancelled:'已取消', timeout:'配网超时', failed:'失败'};
  var labels = {channel:'信道', network_name:'网络名称', network_key:'网络密钥', pan_id:'PAN ID',
    ext_pan_id:'扩展 PAN ID', pskc:'PSKc', mesh_local_prefix:'Mesh-Local 前缀', omr_prefix:'OMR 前缀'};
  var messages = {
    'Waiting for Thread attachment':'等待 Thread 附着。', 'Ready for commissioning':'可以开始配网。',
    'Requesting commissioner role':'正在向 Thread 网络申请配网角色。', 'Waiting for Joiner':'等待节点发起配网。',
    'Joiner session started':'节点开始连接。', 'Joiner authenticated':'节点已通过认证。',
    'Joiner finalization in progress':'正在完成凭据交换。', 'Joiner session ended; waiting for retry':'本次节点会话已结束，等待重试。',
    'Commissioning session completed; node attachment is not yet verified':'凭据交换已完成。',
    'Commissioning cancelled':'已停止配网。', 'Commissioning window expired':'配网窗口已到期。',
    'Commissioner role unavailable or lost':'无法取得配网角色，或角色已丢失。',
    'Commissioner role request timed out':'申请配网角色超时。', 'Commissioner start failed':'启动配网失败。',
    'Joiner authorization failed':'添加节点授权失败。', 'Commissioning already in progress':'已有配网任务正在执行。',
    'Commissioning request queued':'配网请求已提交。', 'Cancellation queued':'停止请求已提交。',
    'Commissioner unavailable':'配网服务暂不可用。', 'Local commissioner is already running':'本机已有配网会话。',
    'Active dataset differs from firmware configuration':'运行中的网络参数与配置不一致。',
    'Thread unavailable or active dataset differs from firmware configuration':'Thread 不可用，或运行中的网络参数与配置不一致。',
    'Thread unavailable or active dataset changed':'Thread 不可用，或运行中的网络参数发生变化。',
    'Candidate expired; refresh and retry':'候选节点已过期，请刷新后重试。'
  };
  function message(text) { return messages[text] || text; }
  function renderNetwork(network) {
    var box = byId('networkConfig'); box.textContent = '';
    Object.keys(labels).forEach(function(key) {
      var row = document.createElement('div'); row.className = 'info-row';
      var label = document.createElement('span'); label.className = 'info-label'; label.textContent = labels[key];
      var value = document.createElement('span'); value.className = 'info-value';
      value.textContent = !reveal && (key === 'network_key' || key === 'pskc') ? '••••••••' :
        key === 'pan_id' && typeof network[key] === 'number' ? '0x' + network[key].toString(16).padStart(4, '0') :
        key === 'omr_prefix' && !network[key] ? '未获取' : String(network[key] === undefined ? '-' : network[key]);
      row.appendChild(label); row.appendChild(value); box.appendChild(row);
    });
  }
  function render(data) {
    latest = data;
    byId('networkMatch').hidden = data.attached && data.dataset_matches;
    byId('networkMatch').textContent = !data.attached ? 'Thread 尚未附着，无法配网。' : '运行中的 Dataset 与配置不一致，已禁止配网。';
    renderNetwork(data.network || {});
    byId('commissionState').textContent = (phases[data.phase] || data.phase) + (data.remaining_s ? ' · 剩余 ' + data.remaining_s + ' 秒' : '');
    byId('commissionState').hidden = data.phase === 'idle';
    var detail = data.phase === 'idle' ? '' : message(data.detail || '');
    byId('commissionDetail').textContent = detail;
    byId('commissionDetail').hidden = !detail;
    byId('startCommission').disabled = submitting || data.busy || !data.attached || !data.dataset_matches;
    byId('cancelCommission').disabled = submitting || !data.busy;
    var box = byId('candidateList'); box.textContent = '';
    var candidates = Array.isArray(data.candidates) ? data.candidates : [];
    if (!data.busy && !candidates.some(function(c) { return (c.id || c.address) === selected; })) selected = '';
    if (!candidates.length) {
      var emptyRow = document.createElement('tr'), emptyCell = document.createElement('td');
      emptyCell.colSpan = 5; emptyCell.className = 'empty-state'; emptyCell.textContent = '暂未发现节点';
      emptyRow.appendChild(emptyCell); box.appendChild(emptyRow);
    }
    candidates.forEach(function(candidate) {
      var key = candidate.id || candidate.address;
      var row = document.createElement('tr'); row.className = selected === key ? 'is-selected' : '';
      function cell(value, className) {
        var td = document.createElement('td'); td.textContent = value;
        if (className) td.className = className;
        row.appendChild(td); return td;
      }
      cell(candidate.name || '未知节点');
      var identity = cell(candidate.device_id || candidate.address, 'device-id');
      identity.title = '扫描地址：' + candidate.address;
      cell({2:'1.1', 3:'1.2', 4:'1.3', 5:'1.4'}[candidate.version] || '—');
      cell(Math.floor(candidate.age_ms / 1000) + ' 秒前');
      var actions = cell('');
      var button = document.createElement('button'); button.className = 'btn btn-secondary btn-sm'; button.type = 'button';
      button.textContent = selected === key ? '已选择' : '选择'; button.disabled = data.busy || submitting;
      button.addEventListener('click', function() { selected = key; render(latest); });
      actions.appendChild(button); box.appendChild(row);
    });
    var chosen = candidates.find(function(c) { return (c.id || c.address) === selected; });
    byId('selectedCandidate').textContent = selected ? '已选择：' + (chosen ? chosen.name || chosen.device_id || chosen.address : selected) : '';
    byId('selectedCandidate').hidden = !selected;
    byId('clearCandidate').disabled = data.busy || submitting || !selected;
  }
  function request(url, body) {
    return fetch(url, {method:body ? 'POST':'GET', cache:'no-store', credentials:'same-origin',
      headers:body ? {'Content-Type':'application/json'} : {}, body:body ? JSON.stringify(body) : undefined})
      .then(function(response) { return response.json().then(function(data) {
        if (!response.ok || data.error) throw new Error(message(data.message || '请求失败'));
        return data;
      }); });
  }
  function refresh() {
    return request('/commissioner/api/status').then(render).catch(function(error) {
      byId('commissionState').textContent = '读取失败：' + error.message;
      byId('commissionState').hidden = false;
      byId('commissionDetail').hidden = true;
      byId('startCommission').disabled = true;
      byId('cancelCommission').disabled = true;
    });
  }
  function control(body) {
    submitting = true; if (latest) render(latest);
    request('/commissioner/api/control', body).then(function(data) {
      byId('pskd').value = ''; showToast(message(data.message || '请求已提交'), 'success');
    }).catch(function(error) { showToast(error.message, 'error'); })
      .finally(function() { submitting = false; refresh(); });
  }
  byId('commissionForm').addEventListener('submit', function(event) {
    event.preventDefault(); if (submitting || !latest || latest.busy || !latest.attached || !latest.dataset_matches) return;
    var pskd = byId('pskd').value.trim(), eui = byId('eui64').value.trim();
    if (!/^[0-9A-HJ-NPR-Y]{6,32}$/.test(pskd)) return showToast('PSKd 需为 6–32 位大写字母或数字，不包含 I、O、Q、Z。', 'error');
    if (eui && !/^[0-9a-fA-F]{16}$/.test(eui)) return showToast('工厂 EUI64 需为 16 位十六进制。', 'error');
    var seconds = Number(byId('timeout').value);
    if (!Number.isInteger(seconds) || seconds < 10 || seconds > 600) return showToast('窗口时长应为 10–600 秒。', 'error');
    control({action:'start', pskd:pskd, eui64:eui, candidate:selected, timeout_s:seconds});
  });
  byId('cancelCommission').addEventListener('click', function() { if (!submitting) control({action:'cancel'}); });
  byId('clearCandidate').addEventListener('click', function() { selected = ''; if (latest) render(latest); });
  byId('showSecrets').addEventListener('click', function() {
    reveal = !reveal; this.textContent = reveal ? '隐藏网络凭据' : '显示网络凭据';
    if (latest) renderNetwork(latest.network || {});
  });
  function poll() {
    if (!activePage) return;
    if (document.hidden) { timer = setTimeout(poll, 1000); return; }
    refresh().finally(function() { if (activePage) timer = setTimeout(poll, 1000); });
  }
  window.addEventListener('pagehide', function() { activePage = false; clearTimeout(timer); });
  window.addEventListener('pageshow', function(event) { if (event.persisted) { activePage = true; poll(); } });
  poll();
})();
