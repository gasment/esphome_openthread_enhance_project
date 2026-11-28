# 更新日志

## 2026-11-28 · ESP32 P2P 支持 BR

- `thread_p2p_transporter` 接受 `openthread` 或 `thread_br` 作为协议栈提供方，BR 模式复用已有实例与锁，不再要求额外配置 openthread；支持 native 和 uart_rcp。
- BR 按 FTD 检查固定 P2P 应用地址，使用 BR Dataset 的 Mesh-Local 前缀校验目标；现有收发协议、ACK 和回调不变，nRF52 组件不修改。

## 2026-11-27 · nRF52 UART 异步 DMA

- nRF52 UART 收发改为 Zephyr 异步 EasyDMA，接收使用两个固定 256 字节 DMA 缓冲，替代旧的逐字节中断重启方式；继续支持标准及 256000 自定义波特率。
- nRF52 默认软件 RX 缓冲增至 1024 字节，显式 rx_buffer_size 保持有效；保留硬件接收错误和软件溢出统计。发送使用持久 RAM 缓冲，确认完成或中止后才能复用。
- 与 deep_sleep_nrf52 联动，在用户休眠动作完成后、外设挂起前确认 DMA 停止；失败取消休眠，唤醒或取消休眠后恢复接收。搭配休眠时需同时更新两个组件，现有 YAML 无需新增配置。

## 2026-11-27 · nRF52 UART 校验接口

- UART 设备参数校验使用标准 `uart.final_validate_device_schema()` Python 接口；修复兼容层内部旧接口调用导致包含 UART 头文件即出现弃用警告的问题。
- 旧 C++ 入口保留兼容并提示迁移至 `uart` 新接口，不改变串口收发行为或 YAML 配置。

## 2026-11-26 · ESP32 P2P 动作级回调

- 将已验收的 nRF52 send 动作级 `on_send_success`、`on_send_fail` 同步到 ESP32 `thread_p2p_transporter`；动作级回调须配置 ack，组件级回调保持兼容。
- 按消息保存独立回调，成功、拒绝及重试耗尽的派发顺序与 nRF52 一致；保持 ESP32 网络/锁实现和 P2P 报文不变，接收端无需升级。

## 2026-11-25 · 统计标题精简

- 集成首页统计条目标题移除 `Summary ·` 前缀，直接显示离线、低电量和弱信号计数，减少手机端占用；设备名称仍为 Summary。

## 2026-11-25 · 弱信号统计

- Summary 新增“弱信号设备”，状态为弱信号节点数/代理节点总数，提供浮点属性 `low_rssi`、`total`、`rssi_healthy`；集成首页 Summary 标题同步增加弱信号统计。
- 统计设置新增弱信号阈值，默认 -75dBm，可持久保存；旧统计设置自动补充默认值，原阈值保留。按节点单位为 dBm 的有效数值判断，同一节点任一读数低于阈值即计入，达到阈值为健康，无读数不计正常或异常。

## 2026-11-25 · 统计设置持久化修复

- 修复 HA 启动迁移时重建管理条目、丢失 Summary 统计阈值的问题；保留已有管理设置，重启和条目迁移后继续使用已保存的离线/低电量策略。

## 2026-11-25 · Summary 独立条目

- 修正中文实体名称为“疑似低电量设备”。Summary 改用独立配置条目，在集成首页与 Thread Proxy 管理条目并列；标题直接展示疑似离线与低电量统计。
- 自动迁移已有 Summary 设备和实体，保留注册标识及用户命名，不再归属管理条目；统计设置仍从 BR 接入管理页维护。

## 2026-11-25 · Summary 统计设备

- 新增与 BR/节点并列的独立 Summary 设备，内置“疑似离线设备”和“疑似低电量设备”传感器，状态为异常节点数/代理节点总数，并提供所要求的浮点计数属性。
- 最近上报来源标题右侧新增“统计设置”弹窗，统一设置离线时长，以及 battery 百分比或 voltage 电压判断阈值。默认 30min、20%、3.0V；设置持久化，无 YAML 配置变更。
- 节点按设备去重，总数不包含 BR、Summary 和已删除节点；缺少对应数据时不计为正常或异常。节点上报、设备增删、保存设置和每 30 秒时间检查均可刷新统计；仅需更新 HA 集成。

## 2026-11-13 · nRF52 主动重新附着

- 新增 `openthread_nrf52.reattach: openthread_main` 动作：删除已保存的父节点记录并重置当前附着过程，保留 Dataset、网络身份、安全计数器和应用设置，无需重启或重新配网。
- 连接在后台完成；动作不自动阻止休眠、不改变轮询周期，也不保证更换到不同父节点。配网进行中或无有效 Dataset 时拒绝执行。

## 2026-11-13 · 来源列表布局（HA 0.9.1）

- “代理节点 · 最近上报来源”使用紧凑单行布局，仅显示节点名称、BR 名称和接收时间；移除节点编码、BR IP 和连接说明。窄屏允许时间另起一行。

## 2026-11-13 · BR 切换补发与节点来源显示（HA 0.9.0）

### 功能变更

- 客户端允许尚未发现 BR 时提交 manual send；发现或重新选择可用 BR 后，在本次发送时限和重试额度内继续补发。已生成的报文和事件标识保持不变，切换 BR 不重新获得重试次数；动作仍仅完成一次。
- HA 的“BR 接入管理”新增节点最近上报来源，显示 BR 名称、地址、最近接收时间及 HA 与该 BR 的连接状态。节点实体同时提供 `last_br_id`、`last_br_name`、`last_br_host`、`last_report_at`、`last_br_api_connected` 属性；来源记录跨 HA 重启保留。
- 来源表示最后将该节点报文转交 HA 的 BR，不代表 Thread 父节点或完整路由；BR 缓存后延迟转交时，以 HA 收到的来源为准。重复报文可更新来源，不重复发布事件。

### 配置变更

```yaml
thread_proxy_client_nrf52:  # ESP32 使用 thread_proxy_client
  discovery_timeout: 3s    # 可省略；每次等待 BR 发现/重新发现的上限
```

原 ACK 策略层级保持不变：auto 在组件级配置，manual 在 send 动作级配置。最终失败后不会无限后台补发；未确认事件供下次 send 重试。补发需更新节点固件；来源显示只需更新 HA 集成并重启 HA，BR 固件无需更改。

## 2026-11-13 · Thread Proxy

本次为破坏性更新：节点接入由旧 Thread SED / `thread_sed_api` 改为 **Thread Proxy + `thread_proxy_api` + 平台客户端（ESP32：`thread_proxy_client`；nRF52：`thread_proxy_client_nrf52`）**。HA 集成版本为 **0.8.1**，要求 Home Assistant **2026.11.0 或更新版本**。

### 功能变更

- **节点通过 BR 接入 HA**：节点与 BR 使用 UDP + ACK，BR 与 HA 使用独立 TCP 连接。HA 支持使用 BR 的局域网 IPv4、IPv6 或主机名接入，无需直接访问节点 IPv6。
- **BR 集中管理**：自动发现配置了 `thread_proxy_api` 的 BR，由用户确认接入；支持手工添加、重连、重新配置、解除绑定。普通 BR 公告不会被当作代理能力。
- **节点免逐个接入**：客户端在 Thread 网内主动探测 BR，不依赖节点 SRP 服务记录，不需要填写节点 IP 或在 HA 中逐个确认。收到实体描述后，HA 直接创建设备和只读实体。
- **多 BR 聚合**：按节点稳定 MAC 保持设备身份，切换 BR 不重复创建设备；重复上报和 KEY EVENT 去重。客户端保留当前 BR，投递失败或重新入网后重新探测。
- **只读实体上报**：支持普通传感器、文本及事件；开关、灯、风扇等只展示状态，不提供控制下行。实体描述变化时动态增删实体，无需重启 HA。
- **单节点独立删除与恢复**：可单独删除节点设备，不影响 BR 和其他节点。删除状态可跨 HA 重启保留；再次收到该节点上报时，以原标识恢复设备和实体。原删除记录仍保留、实体名称未被占用时，复用原 device_id、entity_id 及用户设置。
- **BR 诊断可选**：“创建设备”只控制 BR 本身的设备和诊断实体，不影响代理节点；BR 绑定统一从“BR 接入管理”维护。
- **发送方式可选**：auto 模式随实体发布状态/事件自动批次发送；manual 模式按 YAML 时序发送单个、多个或全部实体的已发布状态，不代为采样。
- **休眠联动**：nRF52 客户端支持 System ON 休眠 guard、通信期间快轮询和休眠前恢复原 poll period；提供 `is_ready`、`can_sleep`、`on_ready` 和 `on_disconnect`。
- **通信恢复与日志优化**：BR 临时无法提交 ACK 时进行本地重试；客户端合并重复描述请求。正常发送仅保留一条 DEBUG 完成摘要，逐包和 poll 切换日志降为 VERBOSE。

节点 ACK 表示 **BR 已接收入 RAM 队列**，不表示 HA 已更新或落盘。HA 断线时 BR 可缓存待转交报文；队列满时不返回成功 ACK，BR 重启会丢失尚未转交的缓存。

### 配置变更

#### 1. HA 改用 Thread Proxy

- 安装目录使用 `custom_components/thread_proxy`，支持 HACS 自定义仓库安装，也可手动复制。
- 停用旧 Thread SED / OpenThread Gateway 的节点直连配置，改为在 **Thread Proxy → BR 接入管理** 中绑定 BR。
- HA 接入地址填写 **BR 的 LAN 地址**；默认代理端口为 **TCP 6065**。
- 不再为每个节点添加集成条目。旧集成条目不会自动转换为新绑定。

#### 2. BR 增加 thread_proxy_api

原有 `thread_br` 的网络、RCP/native、配网和 Web 配置保留。BR 代理仅支持 ESP32，必须同时配置 `thread_br`。

```diff
 # 在原 external_components 的 components 列表中加入 thread_proxy_api。
 thread_br:
   id: thread_br_main
   # 保留现有 BR 配置。

+thread_proxy_api:
+  thread_br_id: thread_br_main
+  # 默认 TCP 6065；无需配置官方 api 才能使用代理。
```

- 节点业务使用 UDP 6064；代理 TCP 6065 与官方 ESPHome API 的 6053 分开。
- 可与 BR 上的官方 `api` 共存；若官方 API 没有客户端连接，按需设置 `api.reboot_timeout: 0s`，避免其离线重启。
- BR 不再使用 `udp.nodes` 手填节点地址，也不扫描节点 SRP 记录；如旧试用配置中存在此项，请删除。

可选配置均可省略：

```yaml
thread_proxy_api:
  thread_br_id: thread_br_main
  # encryption:
  #   key: !secret proxy_tcp_key # BR↔HA TCP Noise；启用后在 HA 绑定中填写同一密钥。
  udp:
    queue_groups: 16            # 待转交的完整报文组数上限。
    queue_bytes: 131072         # 待转交载荷总字节上限。
    # encryption:
    #   key: !secret node_udp_key # 节点↔BR AES-GCM，与节点 encryption.key 一致。
```

#### 3. 节点迁移为对应平台客户端

ESP32 使用 `thread_proxy_client`，nRF52 使用 `thread_proxy_client_nrf52`。沿用原来的 Thread 配网设置；external_components 列表、顶层配置及 action/condition 前缀均替换为对应名称。下面以 nRF52 为例：

```diff
-thread_sed_api:
+thread_proxy_client_nrf52:
   id: sed_api                  # 原 id 可以保留。
-  transport: udp               # 若有此项，删除；新客户端仅支持 UDP。
   send_mode: manual
   deep_sleep_id: sleep_control  # 仅 nRF52 休眠联动；未使用时省略。

 # 自动化中的组件前缀也要替换：
-  - thread_sed_api.send:
+  - thread_proxy_client_nrf52.send:
       id: sed_api
       entity_id: [sys_vdd, battery_level, key_event]
       ack_timeout: 500ms
       ack_resend: 2
       # 原有 on_success / on_error 可以保留。

-  # thread_sed_api.is_ready: sed_api
+  # thread_proxy_client_nrf52.is_ready: sed_api
-  # thread_sed_api.can_sleep:
+  # thread_proxy_client_nrf52.can_sleep:
```

旧 `thread_sed_api` 已移除。新客户端不能与官方 `api` 同时配置，也不再使用 TCP/Noise。可选的节点 `encryption.key` 用于 UDP AES-GCM，需与 BR 的 `udp.encryption.key` 相同；默认不强制加密。

#### 4. ACK 配置规则

| 模式 | ACK 配置位置 | 回调 |
|---|---|---|
| `auto` | 组件级 `ack_timeout`、`ack_resend` | 组件级 `on_ack`、`on_ack_error` |
| `manual` | 每个平台客户端的 `.send` action 下 | action 级 `on_success`、`on_error` |

manual 不允许配置组件级 ACK 策略或 ACK 回调；auto 不允许调用手动 send。

`ack_timeout` 为每轮等待 ACK 的时限，`ack_resend` 为额外重发次数。例如 `500ms`、`2` 最多发送三轮，等待 ACK 的预算约 1.5 秒，另计快照与实际发送耗时。

以下 auto 示例为 ESP32；nRF52 将顶层名称替换为 `thread_proxy_client_nrf52`。

```yaml
thread_proxy_client:
  id: proxy_client
  send_mode: auto
  ack_timeout: 500ms
  ack_resend: 2
  on_ack:
    - logger.log: "BR 已接收本批次"
  on_ack_error:
    - logger.log: "本批次投递失败"
```

从较早的 transporter 配置迁移时，原 `discovery.gateway_id`、`selection`、`manifest_revision` 和实体清单不再迁入客户端；改为使用 ESPHome 实体描述及新组件的发送动作。旧 transporter、实体桥与网关 YAML 不能直接用于本次接入方式。

### 升级顺序

1. 更新 HA 的 Thread Proxy 集成并重启 HA。
2. 更新参与代理的 BR，配置 `thread_proxy_api` 并在 HA 中完成绑定。
3. 迁移节点配置并更新客户端固件；首次接入时保持唤醒，等待 BR 确认和描述同步。

仅更新单节点删除/恢复功能时，只需更新 HA 集成并重启；仅更新客户端日志精简时，只需更新客户端固件。

详细配置：[BR 代理](../components/esp32/thread_proxy_api/README.md)、[nRF52 客户端](../components/nrf52/thread_proxy_client_nrf52/README.md)、[ESP32 客户端](../components/esp32/thread_proxy_client/README.md)。


### nRF52 客户端更名

ESP32 名称保持 `thread_proxy_client`；nRF52 更名为 `thread_proxy_client_nrf52`，需同步修改 external_components 列表、组件顶层及自动化前缀。组件 id 和发送、ACK、休眠配置可保留。

```diff
-thread_proxy_client:
+thread_proxy_client_nrf52:
   id: proxy_client
   send_mode: manual

-  - thread_proxy_client.send:
+  - thread_proxy_client_nrf52.send:
       id: proxy_client

-  # thread_proxy_client.is_ready: proxy_client
+  # thread_proxy_client_nrf52.is_ready: proxy_client
-  # thread_proxy_client.can_sleep:
+  # thread_proxy_client_nrf52.can_sleep:
```

仅更新 nRF52 节点配置和固件；BR 与 HA 无需变更，已有节点设备及实体身份保留。
