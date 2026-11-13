const statusNames = {
  connecting: "正在连接", connected: "API 已连接，等待代理能力", ready: "代理协议已验证",
  disconnected: "未连接", unsupported: "未提供兼容代理协议", auth_error: "认证失败",
  identity_mismatch: "BR 身份不符", duplicate_identity: "BR 已由其他绑定占用"
};
const errors = {
  manager_not_ready: "管理服务尚未就绪，请稍后重试。",
  binding_disabled: "此绑定此前已停用，点击重连可恢复。",
  br_device_busy: "BR 诊断实体正在初始化，请稍后重试。",
  br_device_has_children: "BR 设备存在 HA 子设备，为避免级联删除，未更改此选项。",
  invalid_host: "请输入有效的 IP 地址或主机名。", invalid_port: "端口应为 1–65535。",
  invalid_key: "加密密钥应为 32 字节 Base64；没有启用加密时留空。",
  already_configured: "这个 BR 已经绑定。", discovery_expired: "发现记录已失效，请等待重新发现。",
  invalid_input: "请检查填写内容。", unknown_binding: "绑定已不存在，请刷新。",
  cannot_connect: "无法连接 API，正在重试。", proxy_not_announced: "API 可连接，但没有声明 thread_proxy_api 能力。",
  authentication_failed: "请检查加密密钥。", connection_lost: "连接已断开，正在重试。",
  unsupported_protocol: "代理协议版本不兼容。", identity_mismatch: "端点返回的身份与已绑定 BR 不符。",
  duplicate_identity: "同一 BR 已有另一条绑定，请解除重复绑定。", invalid_metadata: "代理能力信息无效。",
  not_loaded: "绑定已保存，运行实例未加载或已停用。"
};

class ThreadProxyPanel extends HTMLElement {
  constructor() {
    super(); this.attachShadow({mode: "open"}); this._busy = false; this._loading = false;
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;height:100%;background:var(--primary-background-color);color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,system-ui)}
        *{box-sizing:border-box}header{height:64px;display:flex;align-items:center;gap:10px;padding:0 20px;background:var(--app-header-background-color,var(--primary-color));color:var(--app-header-text-color,#fff)}
        .back-link{margin-left:auto;color:inherit;text-decoration:none;white-space:nowrap;font-size:14px;padding:8px 12px;border:1px solid currentColor;border-radius:9px}header h1{font-size:20px;font-weight:500;margin:0}main{max-width:1180px;margin:auto;padding:24px}h2{font-size:17px;margin:0}.top{display:flex;justify-content:space-between;align-items:center;gap:12px}.muted{color:var(--secondary-text-color);font-size:13px}
        .create-device{display:flex;flex-direction:row;align-items:center;gap:6px;font-size:13px;white-space:nowrap;cursor:pointer}.create-device input{width:16px;height:16px;margin:0;padding:0;accent-color:var(--primary-color)}button,input{font:inherit}button{cursor:pointer;white-space:nowrap;flex-shrink:0;border:1px solid var(--divider-color);border-radius:9px;padding:9px 13px;color:var(--primary-text-color);background:var(--card-background-color)}button:hover{border-color:var(--primary-color)}button:disabled{opacity:.5;cursor:wait}.primary{background:var(--primary-color);color:var(--text-primary-color,#fff);border-color:transparent}.danger{color:var(--error-color,#c33)}
        section{margin-top:26px}.list{display:flex;flex-direction:column;gap:9px;margin-top:12px}.row{padding:15px 17px;border:1px solid var(--divider-color);border-radius:12px;background:var(--card-background-color);display:grid;grid-template-columns:minmax(160px,1.2fr) minmax(190px,1.4fr) minmax(150px,1fr) auto;gap:12px;align-items:center}.title{font-weight:600;overflow-wrap:anywhere}.address{font-family:monospace;font-size:13px;overflow-wrap:anywhere}.badge{display:inline-block;font-size:12px;border-radius:20px;padding:5px 9px;background:var(--secondary-background-color)}.good{color:var(--success-color,#22834b)}.bad{color:var(--error-color,#c33)}.actions{display:flex;gap:7px;justify-content:flex-end}.details{margin-top:5px;overflow-wrap:anywhere}.empty{padding:25px;text-align:center;border:1px dashed var(--divider-color);border-radius:12px;color:var(--secondary-text-color)}
        .note{margin-top:14px;padding:11px 14px;border-left:3px solid var(--primary-color);background:var(--card-background-color);font-size:13px;line-height:1.6}.error{color:var(--error-color,#c33);min-height:20px;margin-top:12px;white-space:pre-wrap}form{margin-top:15px;border:1px solid var(--divider-color);border-radius:12px;background:var(--card-background-color);padding:18px;display:grid;grid-template-columns:2fr 1fr 2fr;gap:12px}form[hidden],#clear-key-label[hidden]{display:none}label{font-size:13px;display:flex;flex-direction:column;gap:6px}input{min-width:0;width:100%;border:1px solid var(--divider-color);border-radius:7px;padding:10px;background:var(--primary-background-color);color:var(--primary-text-color)}.wide{grid-column:span 2}.form-actions{align-self:end;display:flex;gap:8px;justify-content:flex-end}.counts{margin:6px 0 0}.hint{margin-top:6px}
        @media(max-width:1150px){main{padding:16px}.row{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.actions{grid-column:1 / -1;grid-row:3;flex-wrap:wrap}.row>.state{grid-column:1;grid-row:2}form{grid-template-columns:1fr 90px}.wide{grid-column:span 2}.form-actions{grid-column:span 2}header{padding:0 12px}}
      </style>
      <header><ha-menu-button></ha-menu-button><h1>BR 接入管理</h1><a class="back-link" href="/config/integrations/integration/thread_proxy">返回集成</a></header>
      <main>
        <div class="top"><div><h2>BR 绑定管理</h2><div class="muted counts" id="counts">正在加载…</div></div><div class="actions"><button id="refresh">刷新</button><button id="add" class="primary">手工添加 BR</button></div></div>
        <div class="note">仅发现启用了 <b>thread_proxy_api</b> 的 BR，确认后才会绑定。节点描述经已绑定 BR 到达后，直接创建设备和只读实体。节点 ACK 表示 BR 已接收；未转交 HA 的队列会在 BR 重启后丢失。</div>
        <form id="manual" hidden>
          <label>IP 地址或主机名<input name="host" required autocomplete="off" placeholder="192.168.1.20 或 br.local"></label>
          <label>API 端口<input name="port" type="number" min="1" max="65535" value="6065" required></label>
          <label>显示名称（可选）<input name="name" maxlength="120" autocomplete="off"></label>
          <label class="wide">API 加密密钥（可选）<input name="noise_psk" type="password" autocomplete="new-password" placeholder="未启用加密时留空"></label>
          <label id="clear-key-label" class="create-device" hidden><input name="clear_key" type="checkbox">清除已保存密钥</label>
          <div class="form-actions"><button type="button" id="cancel">取消</button><button class="primary" type="submit">保存绑定</button></div>
        </form>
        <div id="error" class="error" role="alert"></div>
        <section><h2>已绑定 BR</h2><div id="bound" class="list"></div></section>
        <section><h2>发现的 BR</h2><div class="muted hint">点击接入后建立绑定；已绑定的 BR 不会在此重复显示。</div><div id="discovered" class="list"></div></section>
      </main>`;
    this.shadowRoot.getElementById("add").onclick = () => {this.clearCandidate();this.form.reset();this.form.hidden=false;};
    this.shadowRoot.getElementById("cancel").onclick = () => {this.form.hidden = true; this.form.reset();this.clearCandidate();};
    this.shadowRoot.getElementById("refresh").onclick = () => this.refresh();
    this.form.elements.clear_key.onchange=()=>{this.form.elements.noise_psk.disabled=this.form.elements.clear_key.checked;};
    this.form.onsubmit = async e => {
      e.preventDefault(); const data=Object.fromEntries(new FormData(this.form));data.port=Number(data.port);
      let inputs,action="bind";
      if(this._editing){
        action="reconfigure";inputs={entry_id:this._editing,host:data.host,port:data.port,name:data.name,
          clear_key:this.form.elements.clear_key.checked};
        if(data.noise_psk)inputs.noise_psk=data.noise_psk;
      }else inputs=this._candidate?{candidate_id:this._candidate,noise_psk:data.noise_psk}:data;
      if(await this.action(action,inputs)){this.form.reset();this.form.hidden=true;this.clearCandidate();}
    };
  }
  get form(){return this.shadowRoot.getElementById("manual");}
  set hass(value){this._hass=value;this.shadowRoot.querySelector("ha-menu-button").hass=value;if(this.isConnected&&!this._started)this.start();}
  set narrow(value){this.shadowRoot.querySelector("ha-menu-button").narrow=value;}
  connectedCallback(){if(this._hass)this.start();}
  disconnectedCallback(){clearInterval(this.timer);this._started=false;}
  start(){if(this._started)return;this._started=true;this.refresh();this.timer=setInterval(()=>this.refresh(),5000);}
  async refresh(){
    if(this._loading||!this._hass||!this.isConnected)return;this._loading=true;
    try{const data=await this._hass.callApi("GET","thread_proxy");if(this.isConnected)this.render(data);}
    catch(e){this.showError(e);}finally{this._loading=false;}
  }
  showError(e){const code=typeof e==="string"?e:e?.body?.error;this.shadowRoot.getElementById("error").textContent=errors[code]||e?.message||"操作失败，请检查连接或刷新页面。";}
  async action(name,data){
    if(this._busy)return false;this._busy=true;this.shadowRoot.getElementById("error").textContent="";
    this.shadowRoot.querySelectorAll("button,input[type=checkbox]").forEach(b=>b.disabled=true);
    try{const result=await this._hass.callApi("POST",`thread_proxy/${name}`,data);if(!result.ok)throw new Error("操作尚未完成，请刷新检查。");await this.refresh();return true;}
    catch(e){this.showError(e);return false;}finally{this._busy=false;this.shadowRoot.querySelectorAll("button,input[type=checkbox]").forEach(b=>b.disabled=false);}
  }
  text(tag,value,className=""){const e=document.createElement(tag);e.textContent=value;e.className=className;return e;}
  button(label,fn,danger=false){const b=this.text("button",label,danger?"danger":"");b.onclick=fn;return b;}
  endpoint(item){return `${item.host.includes(":")?`[${item.host}]`:item.host}:${item.port}`;}
  render(data){
    const online=data.bound.filter(x=>x.connected).length;
    this.shadowRoot.getElementById("counts").textContent=`${data.bound.length} 台已绑定 · ${online} 台 API 在线 · ${data.discovered.length} 台待接入 · ${data.uplink?.node_devices??0} 个节点`;
    for(const category of ["bound","discovered"]){
      const container=this.shadowRoot.getElementById(category);container.replaceChildren();
      if(!data[category].length){container.append(this.text("div",category==="bound"?"尚未绑定 BR，可通过自动发现或手工添加接入。":"暂无待接入 BR。","empty"));continue;}
      for(const item of data[category]){
        const row=this.text("div","","row"), identity=this.text("div","");
        identity.append(this.text("div",item.name,"title"),this.text("div",item.proxy_id||item.id||"身份待验证","muted details"));
        const address=this.text("div","");address.append(this.text("div",this.endpoint(item),"address"));
        address.append(this.text("div",item.network_id?`Thread 网络 ${item.network_id}`:"Thread 网络信息待获取","muted details"));
        const state=this.text("div","","state");
        state.append(this.text("span",category==="bound"?(statusNames[item.status]||item.status):"已发现，未绑定",`badge ${item.proxy_ready?"good":item.last_error?"bad":""}`));
        if(category==="bound")state.append(this.text("div",`${item.data_plane_ready?"UDP 就绪":"UDP 未就绪"} · 队列 ${item.queue_size??"—"} · 已见地址 ${item.node_count??"—"}`,"muted details"));
        if(item.last_error)state.append(this.text("div",errors[item.last_error]||item.last_error,"muted details"));
        const actions=this.text("div","","actions");
        if(category==="bound"){
          const label=this.text("label","","create-device");
          label.title="仅创建 BR 本身的设备与诊断实体，不影响代理节点";
          const checkbox=document.createElement("input");checkbox.type="checkbox";
          checkbox.checked=item.create_device!==false;checkbox.disabled=this._busy;
          checkbox.setAttribute("aria-label",`${item.name} 创建设备`);
          checkbox.onchange=async()=>{
            const before=item.create_device!==false;
            if(!await this.action("create_device",{entry_id:item.entry_id,enabled:checkbox.checked}))checkbox.checked=before;
          };
          label.append(checkbox,this.text("span","创建设备"));actions.append(label);
          actions.append(this.button("重连",()=>this.action("reconnect",{entry_id:item.entry_id})));
          actions.append(this.button("重新配置",()=>this.editBinding(item)));
          actions.append(this.button("解除绑定",()=>{if(window.confirm(`解除“${item.name}”的绑定？此操作不会修改 BR 固件。`))this.action("unbind",{entry_id:item.entry_id});},true));
        }else{
          actions.append(this.button("接入",()=>this.discoveredBind(item)));
        }
        row.append(identity,address,state,actions);container.append(row);
      }
    }
  }
  discoveredBind(item){
    this.clearCandidate();this.form.reset();
    // Use the regular form so encrypted proxies never need a browser prompt.
    this.form.hidden=false;this.form.elements.host.value=item.host;this.form.elements.port.value=item.port;
    this.form.elements.name.value=item.name;this.form.elements.noise_psk.value="";
    this._candidate=item.id;
    for(const field of ["host","port","name"])this.form.elements[field].readOnly=true;
    this.form.scrollIntoView({behavior:"smooth",block:"nearest"});
  }
  editBinding(item){
    this.clearCandidate();this.form.reset();this._editing=item.entry_id;this.form.hidden=false;
    this.form.elements.host.value=item.host;this.form.elements.port.value=item.port;this.form.elements.name.value=item.name;
    this.form.elements.noise_psk.placeholder=item.has_key?"留空保留已保存密钥":"未启用加密时留空";
    this.shadowRoot.getElementById("clear-key-label").hidden=false;
    this.form.querySelector("button[type=submit]").textContent="保存配置";
    this.form.scrollIntoView({behavior:"smooth",block:"nearest"});
  }
  clearCandidate(){
    this._candidate=null;this._editing=null;
    for(const field of ["host","port","name"])this.form.elements[field].readOnly=false;
    this.form.elements.noise_psk.disabled=false;this.form.elements.noise_psk.placeholder="未启用加密时留空";
    this.form.elements.clear_key.checked=false;this.shadowRoot.getElementById("clear-key-label").hidden=true;
    this.form.querySelector("button[type=submit]").textContent="保存绑定";
  }
}
if(!customElements.get("thread-proxy-panel"))customElements.define("thread-proxy-panel",ThreadProxyPanel);
