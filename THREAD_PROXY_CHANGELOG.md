# Thread Proxy client 与节点直接建实体（0.8.0）

- 新增 ESP32/nRF52 `thread_proxy_client`，复用原 Native API 实体序列化，移除 TCP/Noise、节点直连 HA 和业务下行；与官方 api / 原 thread_sed_api 互斥。
- 不发布/查询节点 SRP 服务。节点通过 Thread realm-local DISCOVER 找 BR，BR 返回带 wake 回显的 PROBE；缓存地址仅是候选，确认后才就绪，投递失败/重新入网后重选。
- 保留手动单个/多个实体 send、auto 批次、ACK 超时/额外重发及原回调作用域；增加 on_ready/on_disconnect，保留 is_ready/can_sleep 和 nRF52 System ON guard、快轮询。
- HA 从已绑定 BR 收到描述后直接创建设备/只读实体，无节点接入确认和节点 ConfigEntry；按 MAC 聚合，跨 BR 保留身份；schema 更新动态增删，清理只作用于对应节点实体。
- HA 保存实体描述、普通状态和事件游标；未知 schema 时请 BR 补发描述，首次事件等待实体注册，连续事件逐条发布。BR 设备勾选项不影响节点。
- 节点仍以 BR RAM 入队 ACK 为成功，不等待 HA 或磁盘；无业务控制下行。BR 重启丢失待转交队列，事件持久化仍有合并写入窗口。

带注释 YAML diff 和完整示例：[客户端迁移说明](components/nrf52/thread_proxy_client/README.md)。配置前缀改为 thread_proxy_client，删除 transport，ACK 配置位置保持；BR 的旧 udp.nodes 如有则删除。

验证：93 项 HA/协议回归，18 项客户端配置/内存代码生成检查，BR 配置检查与 C++ 静态语法/平台源码一致性检查。未运行固件编译，待上板测试首次探测、SED ACK、重配网和多 BR 切换。

---

# Thread Proxy UDP ACK 与 TCP 上行（0.7.0）

- BR 新增 Thread 内部 UDP 6064 接收，沿用 thread_sed_api 的 TSD2、768 字节分片、32768 字节组上限和可选 AES-GCM；与 thread_br 基础功能隔离。
- 完整报文入有界 RAM 队列后本地回复 ACK，重复组只重发 ACK，队列满不返回成功；HA TCP 确认后才释放队列。BR 重启会丢失未转交缓存。
- 从本 BR 的 SRP 记录发现节点，支持手填 IPv6 补充；Thread 地址探测和 ACK 不走 HA 的 OMR 下一跳选择。schema 未知/变化时补发探测获取描述。
- 经独立 Native API TCP 6065 转发原始描述/状态/事件记录；显式上行订阅、稳定投递 ID、断线重发，HA 跨 BR 去重和事件 token 去重。明文与 Noise 均支持。
- HA 提供上行缓存、thread_proxy_packet / thread_proxy_node_event 事件和诊断摘要；管理页增加 UDP 就绪、队列和地址数。当前不创建代理节点设备/实体，无业务控制下行。
- UDP 共享密钥和 BR TCP Noise 密钥独立；默认均不强制加密。停用旧 Thread SED 直连探测，避免抢回节点目标。
- 默认 YAML 无需增加字段；可选 udp.queue_groups / queue_bytes / nodes / encryption.key，带注释 diff 见 [上行说明](thread_proxy/UPLINK.md)。

验证：83 项 Python 回归（含真实 aioesphomeapi 明文/Noise 分片转发及重复确认），25 项配置/内存代码生成校验；C++ 静态语法与符号隔离检查。未运行固件编译，待编译及上板测试。

---

# Thread Proxy 第一版（0.5.0）

- 将 HA 接入模型从 Thread SED 单节点改为 Thread Proxy 多 BR 绑定；独立集成域 `thread_proxy`。
- 仅发现声明 `thread_proxy_api` 的 BR。使用专用 mDNS 服务并要求用户确认接入；支持 IPv4/IPv6/主机名手工绑定，密钥可选。
- 每个 BR 一条持久化 HA 配置，固定身份校验、地址公告更新、重复绑定拦截、自动重连；解绑不修改 BR 配置。
- 新增管理员侧栏：已绑定/待接入列表、连接/代理验证状态、错误提示、重连与解绑；已绑定 BR 不在待接入重复显示。
- 提供 BR 诊断实体；状态接口和诊断导出不包含密钥。
- 原 HA Thread SED 源码归档到 `thread_sed/legacy_component`；HACS 仅安装新的 `thread_proxy` 组件。
- 本次不改 ESPHome YAML，不新增可编译的 `thread_proxy_api` 固件组件；节点转发、BR 本地 ACK 和多 BR 聚合待后续实现。本版不可替代旧版节点数据接入。

安装目录变化：

```diff
- /config/custom_components/thread_sed/    # 原单节点集成；迁移时先停用并备份
+ /config/custom_components/thread_proxy/  # 新 BR 管理集成，需重启 HA 加载
```

接入操作变化（HA 界面配置，不是 configuration.yaml）：

```diff
- Thread SED → 填写 SED 的 OMR 地址与节点 UDP 端口
+ Thread Proxy → 确认发现的 BR，或填写 BR 的 LAN 地址与独立代理 TCP 端口（默认 6065）
+ 名称：可手工指定
+ API 加密密钥：可选，BR 启用 Noise 时才填写
```

ESPHome YAML：**本阶段无变更**。`thread_proxy_api` 尚未实现，因此不要在设备 YAML 中提前添加该组件。

验证结果：新增 35 项测试、原有 79 项回归测试通过；已验证真实 Native API 明文/Noise 会话、身份检查与断线恢复，配置流程、权限与密钥脱敏使用 HA 轻量替身。桌面/手机管理页交互及 Python AST、JSON、单活动 HACS 目录结构检查通过。尚未在实际 HA 2026.11 实例和 BR 硬件联调，未运行固件编译。

端口修正：代理默认端口为 **6065**，独立于官方 ESPHome API 的 6053；自动发现以专用服务的 SRV 端口为准，保留手工自定义端口。旧试用绑定需手工重新配置代理端口，不自动重定向。

## 0.5.2：统一 BR 接入管理入口

- 集成详情页复用原“添加中枢”按钮，改为“BR 接入管理”，直接进入现有 BR 管理页，不再打开手工添加配置对话框。
- 保留单一入口，不再增加并列按钮或侧栏重复入口；发现接入、手工添加、解绑和重连统一在管理页完成。
- 使用 HA config.initiate_flow.user 翻译覆盖本集成按钮文案，不修改全局“中枢”翻译；仅在 Thread Proxy 详情页接管按钮点击。
- 保留原生 HA 按钮样式和键盘操作，离开详情页时清理点击接管；管理页提供返回集成链接。
- 更新前端缓存版本。更新 HA 集成后重启 HA 并刷新浏览器，无需修改 BR YAML 或刷固件。

## 0.5.3：BR 自身设备可选

- 在已绑定 BR 条目的“重连”左侧增加“创建设备”，按 BR 持久化，默认勾选。
- 取消仅移除当前 BR 的诊断设备及四个诊断实体；勾选可动态恢复。BR 绑定、连接与管理状态始终保留。
- 仅处理固定 BR 诊断唯一 ID，避免按整个配置条目批量删除；不卸载节点实体平台，不删除代理节点设备/实体或其他集成设备。对 HA 真子设备的级联删除设防护。
- 修正入口文字更新以保留 HA Lit 的注释锚点和文本节点，保持原按钮和键盘行为。更新前端缓存版本。

验证：54 项本地回归测试通过；浏览器模拟验证单入口不会触发原添加流程、保留 Lit 标记、键盘/返回操作，以及复选框位置、持久状态、错误回滚和桌面/平板/手机布局。BR 本体设备/实体删除范围、代理节点保留、连接不重建及选项持久化已覆盖。尚未部署到实际 HA 实例验证；本次未修改或编译固件。

## 0.6.0：集中式绑定与重新配置

- 将逐 BR HA 配置条改为一条共享 Thread Proxy 管理配置，BR 绑定独立持久化。即使不创建设备也能连接、编辑、重启恢复，不再为单个 BR 生成配置条。
- “创建设备”仍只控制 BR 本体设备和诊断实体，不控制代理节点；勾选同样使用共享配置，不产生额外 BR 配置条。
- 在“重连”和“解除绑定”之间增加“重新配置”，编辑名称、地址、端口及可选密钥。留空保留密钥，支持显式清除；仅修改名称不重连，通信参数变化只重连当前 BR。
- 旧配置自动合并；保留绑定和注册表 ID，先保存再迁移所属配置，最后清理旧配置条。迁移中断可继续，不静默开启已停用的 BR。
- 共享管理配置是集成启动入口，需保留一条；单个 BR 的增删改统一由管理页处理。
- 更新前端缓存版本。无 BR YAML 或固件变化。

验证：72 项本地回归测试通过，包含集中绑定、迁移及恢复、同配置下 BR/代理节点范围隔离、密钥保留/清除与连接隔离。浏览器模拟验证重新配置按钮顺序、编辑模式切换、密钥行为及桌面/手机布局；实际 HA 升级迁移仍需用户环境验证。
