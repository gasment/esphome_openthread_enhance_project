# thread_sed_transporter · HA v3

SED 实体传输至 Home Assistant 自定义集成 `openthread_gateway`，BR 只负责标准 IPv6 路由与 DNS-SD Discovery Proxy。本版本与 BR 内的 v2 `thread_gateway` 不兼容。

依赖 `openthread`，自动加载 `thread_entity_bridge`；显式列出 external_components 时请包含二者。四类 transporter 中本轮迁移两个 SED；REED 仍使用 v2。

```yaml
thread_sed_transporter:
  id: sed_transport
  openthread_id: openthread_main
  node_id: room_key
  name: Room Key
  # backend 可整体省略：向发现的 HA 后端发送候选 HELLO，仍需在 HA 人工接入。
  backend:
    id: ha_main  # 可选，限制目标 HA 后端 ID；指定后也不绕过人工确认
    # discovery_timeout: 2s
  sleep_for: 30min  # 可选，给 HA 的预计休眠时长；应与实际睡眠周期一致
  control_ready: !lambda return id(keep_awake).state;
  heartbeat_interval: 10s
  sensors:
    - id: temperature
      delta: 0.5
  binary_sensors: [door_state]
  text_sensors:
    - id: thread_identity
      max_length: 42
  events: [key_event]
```

以上 ID 引用源组件，不定义新传感器。`node_id`/实体 ID 为 1～32 ASCII 字符，实体总数仍为 1～16。支持 sensors、binary_sensors、text_sensors、events、switches、numbers、selects、buttons、text_inputs；button 没有状态，不能作为 send 的目标。

## 发现与绑定

HA 在 LAN 公告 `_esphome-tgw._udp.local.`；节点通过 Thread 内 BR 的 DNS-SD 代理查询 `_esphome-tgw._udp.default.service.arpa.`，解析 TXT `v=3`/`id` 与 SRV/AAAA。查询使用 SRP 自动发现的 BR 地址、DNS 端口 53。BR 必须启用 SRP 和 Discovery Proxy，HA 需有可从 Thread OMR 访问的 IPv6 地址、双向 UDP 路由和防火墙放行。

未绑定节点最多缓存 8 个后端端点，向所有匹配后端发送未绑定 HELLO。HA 端选择接入后，节点先持久保存密钥再确认。绑定保留于 ESP32 NVS `thread_ha_v3` / nRF52 OT settings `0x8003`，不会使用旧 v2 绑定。身份使用工厂 EUI64；更换 Thread 网络不会清除有效 HA 绑定。

首次接入建议保持节点唤醒（例如 `keep_awake`），在 HA 配置选项中确认；已有绑定的正常唤醒不重复接入。System ON 唤醒不重建会话、不写启动计数；实际重启先提交递增计数，再握手。只有手动解绑或收到认证撤销才删除绑定。设置不同 backend.id 前应先解绑。

## 上报与控制

源组件负责采集与发布，本组件不调用它们的 update。原有 send/entity_state/entities/force/on_complete 等动作保留：

```yaml
- thread_sed_transporter.send:
    id: sed_transport
    entity_id: [temperature, door_state]
    ack:
      timeout: 2s
      resend: 2
    on_success:
      - logger.log: "HA 已确认本轮报告"
```

send 调用时冻结数据，delta/变化过滤以 HA ACK 确认的基线为准。v3 所有报告均等待整轮 ACK；省略 ack 时使用 2 秒、重发 2 次，原全局 on_report_success/on_report_fail 仍只对显式配置 ack 的任务触发，动作级 on_success/on_fail 在终态触发。on_complete 仍表示本地提交完成，不表示 HA 已确认。

事件每次显式 send 是一个新事件，重试使用相同报告 ID/seq 和冻结值。HA 请求全量同步时只读取普通状态，排除所有历史 Event。自动全量状态同步会额外调用组件级 on_complete；需要区分业务发送结果的自动化应使用 send 自己的 on_success/on_fail。

清单使用原始 JSON SHA-256，512-byte 原始块 Base64 分片。所有业务使用 OTG3/HMAC-SHA256，单个数据报最多 1232 bytes。初次密钥邀请未加密，依赖可信 LAN/Thread；后续 HMAC 提供认证，不加密内容。

SED 控制要求 `control_ready: true`，心跳范围 1～10 秒；HA 不排队离线控制。命令在当前会话内去重，满 32 个回执后协商新上下文，避免淘汰旧回执后重复执行按钮。控制成功表示源组件接受调用，实际状态由后续上报确认。

## 恢复与休眠

`thread_sed_transporter.discovery: sed_transport` 重新发现端点并重建会话，不清除绑定；`thread_sed_transporter.unbind: sed_transport` 持久撤销本地绑定，后续重新人工接入。HA 中仍有旧绑定时需同步移除。

nRF52 使用 deep_sleep_id 在睡前发送 sleeping/sleep_for_s 公告，读取实际休眠时长（含 enter 的覆盖值），最长等 ACK 2 秒后允许休眠；不无限阻塞。ESP32 在 shutdown 中尽力发送公告，使用可选 sleep_for。sleep_for 最多 7 天，不能替代真正的休眠配置。未声明睡眠或仅使用 GPIO 无限期休眠时，HA 使用默认离线宽限。

重传在同一上下文内幂等。HA 重启/切换上下文后，未确认报告保留冻结值并重新编号；HA 若在事件已执行后、ACK 丢失且事件去重状态丢失时重启，无法保证跨故障“恰好一次”。

验证范围：两个平台最小配置通过 ESPHome 2026.10.2 的 config 验证；C++ 仅语法解析与 API 核对，未编译固件、未上板联调。详见 [SED 迁移说明](../../../openthread_gateway/SED_MIGRATION.md)。
