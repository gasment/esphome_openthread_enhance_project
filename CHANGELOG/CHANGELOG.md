# 更新日志

## 2026-11-13 · Thread Proxy

本次为破坏性更新：节点接入由旧 Thread SED / `thread_sed_api` 改为 **Thread Proxy + `thread_proxy_api` + `thread_proxy_client`**。HA 集成版本为 **0.8.1**，要求 Home Assistant **2026.11.0 或更新版本**。

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

#### 3. 节点迁移为 thread_proxy_client

ESP32、nRF52 均提供客户端。沿用原来的 Thread 配网设置；把 external_components 列表中的 `thread_sed_api` 替换为 `thread_proxy_client`。

```diff
-thread_sed_api:
+thread_proxy_client:
   id: sed_api                  # 原 id 可以保留。
-  transport: udp               # 若有此项，删除；新客户端仅支持 UDP。
   send_mode: manual
   deep_sleep_id: sleep_control  # 仅 nRF52 休眠联动；未使用时省略。

 # 自动化中的组件前缀也要替换：
-  - thread_sed_api.send:
+  - thread_proxy_client.send:
       id: sed_api
       entity_id: [sys_vdd, battery_level, key_event]
       ack_timeout: 500ms
       ack_resend: 2
       # 原有 on_success / on_error 可以保留。

-  # thread_sed_api.is_ready: sed_api
+  # thread_proxy_client.is_ready: sed_api
-  # thread_sed_api.can_sleep:
+  # thread_proxy_client.can_sleep:
```

旧 `thread_sed_api` 已移除。新客户端不能与官方 `api` 同时配置，也不再使用 TCP/Noise。可选的节点 `encryption.key` 用于 UDP AES-GCM，需与 BR 的 `udp.encryption.key` 相同；默认不强制加密。

#### 4. ACK 配置规则

| 模式 | ACK 配置位置 | 回调 |
|---|---|---|
| `auto` | 组件级 `ack_timeout`、`ack_resend` | 组件级 `on_ack`、`on_ack_error` |
| `manual` | 每个 `thread_proxy_client.send` 下 | action 级 `on_success`、`on_error` |

manual 不允许配置组件级 ACK 策略或 ACK 回调；auto 不允许调用手动 send。

`ack_timeout` 为每轮等待 ACK 的时限，`ack_resend` 为额外重发次数。例如 `500ms`、`2` 最多发送三轮，等待 ACK 的预算约 1.5 秒，另计快照与实际发送耗时。

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

详细配置：[BR 代理](components/esp32/thread_proxy_api/README.md)、[nRF52 客户端](components/nrf52/thread_proxy_client/README.md)、[ESP32 客户端](components/esp32/thread_proxy_client/README.md)。
