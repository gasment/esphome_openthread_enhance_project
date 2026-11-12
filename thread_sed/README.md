# Thread SED

面向 ESPHome Thread SED / REED 的 HA 自定义集成，设备组件仍为 `thread_sed_api`。0.4.0 默认使用 **IPv6 UDP + 批次 ACK**，保留 Protobuf 实体描述、动态实体增删、auto / manual 上报及指定实体的批量 send。日常唤醒不建立 TCP，不执行原生 API 握手，也不等待第二次休眠确认。

## 安装与接入

1. 更新 `custom_components/thread_sed` 并重启 HA，同时更新 ESP32 或 nRF52 的 `thread_sed_api`。HA 最低版本仍为 2026.11，实体模型使用 HA 自带的 aioesphomeapi 依赖。
2. HA 需具备到节点 OMR IPv6 的路由，Docker 使用 host 网络。默认双方业务端口均为 **UDP 6064**。BR 负责 IPv6 转发和 SRP/mDNS 代理。
3. 节点公告 `_thread-sed._udp`；HA 发现后由用户确认接入。首次接入保持节点唤醒，HA 探测并读取实体描述后，节点保存 HA 的 UDP 地址和端口。
4. 旧 TCP 条目可在 HA 条目菜单中选择“重新配置”，设置 transport=udp、节点端口 6064；验证同一 MAC 后更新原条目。Zeroconf 也会更新已知 MAC 的协议与地址。实体唯一 ID 规则不变，无需删除旧条目。

默认无密钥。需要加密时仍配置 `encryption.key`（32 字节 Base64），HA 输入同一密钥；UDP 使用 AES-256-GCM。未配置密钥的明文模式不提供共享密钥认证。端口不同时需同步配置 HA 的节点端口；HA 接收端固定 UDP 6064。

## 设备配置

```yaml
thread_sed_api:
  id: sed_api
  transport: udp  # 默认值；明确标出以便迁移
  send_mode: manual
  deep_sleep_id: sleep_control
```

手动发送读取指定实体的**当前缓存状态**，不代为采样：

```yaml
- component.update: battery_level
- wait_until:
    condition:
      thread_sed_api.is_ready: sed_api
    timeout: 1s
- thread_sed_api.send:
    id: sed_api
    entity_id: [battery_level, sys_vdd, key_event]
    ack_timeout: 500ms  # 每轮等待 ACK 的时间
    ack_resend: 2       # 额外重发两次，共最多三轮
    on_success:
      - deep_sleep_nrf52.enter: sleep_control
    on_error:
      - logger.log: "UDP 批次未获得确认"
      - deep_sleep_nrf52.enter: sleep_control
```

`entity_id` 支持一个 ID 或多个混合实体 ID；省略时发送全部可发布状态及本批事件。 手动 send 的快照在当前自动化回调返回后的下一轮建立，因此 `on_event` 中直接调用也会包含本次事件，不需要额外 delay；快照边界之后的新事件留给下批。`is_ready` 表示 Thread 已连接、存在已学习的 HA 端点且没有发送/描述同步在占用；它不通过握手证明 HA 在线，最终以 send 的 ACK 判断交付。

自动模式继续支持：

```yaml
thread_sed_api:
  id: sed_api
  transport: udp
  send_mode: auto
  ack_timeout: 500ms
  ack_resend: 2
  deep_sleep_id: sleep_control
  on_ack:
    - if:
        condition:
          lambda: return !has_pending;
        then:
          - deep_sleep_nrf52.enter: sleep_control
```

auto 只使用组件级 `ack_timeout`、`ack_resend`、`on_ack` 和 `on_ack_error`，事件和状态合并为一个确认批次；不配置回调也会等待 ACK。`on_ack` 参数为 batch_id / elapsed_ms / has_pending，`on_ack_error` 为 batch_id / reason。auto 不允许调用 `thread_sed_api.send`。

manual 不允许在组件级设置上述四项；每个 `send` action 独立配置 `ack_timeout`、`ack_resend`、`on_success`、`on_error`，两个数值也支持 lambda。两种模式的默认值均为 `500ms`、`2`。旧 `auto_ack_timeout` 和 send 的 `timeout` 已移除，会明确报配置错误。

`ack_resend` 表示额外重发次数：0 只发送一次，2 最多发送三次。`ack_timeout` 是每轮最后一个分片发出后等待 ACK 的时限；500ms、2 的名义 ACK 等待上限约 1.5s，另加采集缓存序列化、分片发出、循环调度和必要的描述同步时间。范围分别为 1ms–60s、0–255。UDP 重发复用同一组号、实体数据和事件快照，不重新采样；成功或最终失败只触发一次所属回调。取消 action 不再触发回调，并停止该批次的待发数据。

周期传感器在上一批确认期间继续发布时，auto 会产生后续批次；需要固定时序时使用 manual。失败的事件保留在 RAM，供后续发送；失败回调本身不会启动无限重试。

## 交付与休眠

- 业务批次用一个组 ID 确认；丢失 ACK 时重发，HA 对同一组重复确认、不重复处理事件。原 RAM 事件队列保留到对应批次确认，指定实体和截断序列规则不变。
- HA ACK 表示批次已解码并处理进 HA 内存状态/事件模型。缓存写盘在后台合并执行，不阻塞 ACK；不承诺 ACK 后立刻断电仍严格持久，也不承诺跨 HA/节点掉电的 exactly-once。
- 实体描述按配置指纹缓存。HA 缺少匹配描述时请求补取，暂存本批数据，完成描述后再处理和 ACK；普通唤醒不做清单枚举。
- 业务 ACK 后直接允许 System ON 休眠，发送一次不等待回复的休眠通知以更新 HA 可用性。发送/回调未结束时 guard 继续等待；后台交付最终有超时上限。
- 每次 System ON 唤醒生成新的传输随机标识；ACK 绑定该标识。HA 命令也带当前唤醒标识，节点拒绝路由器/父节点队列中滞留到下次唤醒的旧命令。
- HA 命令最多重传 4 次，节点最近 16 个命令组做去重。睡眠/离线命令不由集成缓存；超时记录告警，不乐观更新状态。
- HA 启动及每 30 秒发出一次维护探测，用于重新学习 HA 地址。指纹未变时仅回复轻量心跳，不重新枚举实体。常醒节点连续 90 秒无业务或心跳会标为离线。长睡眠、地址变化或 Thread 路由尚未恢复时，首批仍可能超时；需保留错误分支和后续重试机会。

单组负载上限 32 KiB，超过一个报文时分片，每片应用负载最多 768 字节。ACK、重传和分片也会产生流量；详细格式见 [PROTOCOL.md](PROTOCOL.md)。首次描述同步需留出比日常发送更长的唤醒窗口。

## 验证状态

已验证 Python 协议与 HA 会话、真实 IPv6 UDP 套接字、明文/AES-GCM、重复事件、分片、接入和命令 ACK，以及 ESP32/nRF52 的代码生成和源文件筛选。依用户要求，未运行固件编译；固件编译与上板延迟、丢包和功耗测试均待用户执行。

## TCP 兼容模式

旧实现保留为 `transport: tcp`，可显式使用 `port: 6053`。它继续使用 TCP 原生 API、可选 Noise 和旧休眠确认；上述 UDP 优化不适用于该模式。两端需使用相同模式。
