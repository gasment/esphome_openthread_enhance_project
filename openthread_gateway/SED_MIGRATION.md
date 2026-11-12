# SED transporter 迁移至 HA

本轮迁移 `thread_sed_transporter`（ESP32）和 `thread_sed_transporter_nrf52`；两个 REED transporter 与旧 BR thread_gateway 保持 v2，不受本次新增 HA 协议头影响。已有 v2 绑定不会导入 HA。

## YAML diff（nRF52；ESP32 使用 thread_sed_transporter）

```diff
 thread_sed_transporter_nrf52:
   id: sed_transport
   openthread_id: openthread_main
   node_id: ${project_name}
   name: ${friendly_name}
-  manifest_revision: 12
-  discovery:
-    gateway_id: thread-br-gw-2f-mainbedroom
-    multicast_address: "ff13::8000:1234"
-    port: 12138
-    timeout: 1s
+  # backend 整段可省略；留空时让所有已发现 HA 显示候选，人工选一个接入。
+  backend:
+    id: ha_main  # HA 集成中配置的后端 ID，不能填写旧 BR gateway_id
+    # discovery_timeout: 8s
   deep_sleep_id: sleep_control  # 保留，自动读取本次实际睡眠时长
   control_ready: !lambda return id(keep_awake).state;
   heartbeat_interval: 10s
   sensors:
     - id: sys_vdd
       delta: 0.02
     - id: battery_level
       delta: 1
   text_sensors:
     - id: thread_identity
       max_length: 42
   events:
     - id: key_event
```

ESP32 没有 deep_sleep_id，可设置 `sleep_for: 30min`，供 shutdown 公告；实际 deep_sleep 配置仍由原休眠组件控制。send 自动化、entity_state、批量 entities、delta 等可保留。需要刷新 external_components 中的 SED 和实体桥；nRF52 同时刷新 deep_sleep_nrf52（新增只读休眠时长访问接口）。

## 逻辑变更

- 固定公共 Thread 广播改为 DNS-SD：HA 公告 `.local.`，节点向 BR 查询 `default.service.arpa.`；源地址交给 OpenThread 选择，不再固定 Mesh-Local。
- 节点使用工厂 EUI64，持久保存 HA 后端/绑定代数/密钥和重启计数；Network/BR/IP 变化不清除有效绑定。真实重启和 System ON 唤醒分开处理。
- 未绑定 HELLO 发送至所有已发现且匹配 backend.id 的 HA；不自动绑定，指定 backend.id 也要人工批准。
- OTG3 原始 JSON HMAC；先保存邀请再确认，session_offer/session_ack/session_ready 确认端点与上下文。撤销和控制都必须通过认证。
- 清单原始 JSON SHA-256、Base64 分片；报告同轮分片共用 seq/report_id。值冻结、ACK 后提交基线，重连不重新采样。全量同步排除 Event。
- 省略 ack 也执行默认 ACK 等待（2 秒、重发 2 次）。回调与休眠 guard 保留，on_complete 仍为本地提交边界。
- nRF52 睡前向 HA 公告实际休眠时间，短暂等 ACK；网络故障最多延迟 2 秒，不阻止电池节点长期休眠。

## 联调顺序

1. 保留现有 BR，确认它提供 SRP、LAN DNS-SD Discovery Proxy、OMR 到 HA 的双向路由。仅有 Thread mesh 不代表 LAN 服务已可发现。
2. 更新节点 YAML 和本轮组件。首次保持唤醒，在 HA 集成配置中选择新节点接入；之后观察清单和原生实体自动出现。
3. 测试传感器/事件、重复 ACK、控制、睡眠唤醒、HA 重启、解绑后重新接入，最后测试同网多 BR 切换。

已验证：ESP32/nRF52 最小 YAML 的 esphome config、Python schema 解析、C++ tree-sitter 语法与实际 SDK API/Kconfig 核对。未运行编译、刷机或无线联调；C++ 语义/链接和实际线程时序仍需用户编译与上板确认。跨 HA 重启丢失事件去重记录时不承诺恰好一次。

## DNS-SD 超时修正

节点默认 `backend.discovery_timeout: 8s`、`timeout: 30s`；HELLO 重试保持独立的 1 秒间隔。旧版 2 秒 DNS 等待短于 ESP-IDF 5.5.6 BR 的 3 秒 mDNS 查询窗口，可能持续报 `ResponseTimeout (28)`，5 秒就绪预算也无法覆盖多步发现与解析。若 YAML 显式配置短等待，需要一并修改；正常保留会话的 System ON 唤醒不重复完整发现。

DNS browse/resolve 超时属于服务发现阶段，不足以证明 HA 业务端口不通，也不能证明公告地址有误。先看新日志中的 DNS 服务器、实例数量与解析结果，再核对 HA 宿主机上是否确有公告 IPv6、LAN mDNS 是否能被 BR 收到。

## 业务失联恢复修正

ACK 耗尽不再立即删除可用 HA 地址。优先在最后已认证端点上用新 request_id 重建签名会话（5 秒），无响应才回退 DNS-SD，整个恢复仍受 timeout 预算限制。确认前不提交基线，重发仍使用冻结值。显式 discovery 保留强制刷新语义；解绑不会保留旧授权。

长睡眠后启用 control_ready 会重新开始清醒心跳观察期，避免把睡眠时间算成连续心跳丢失。失联诊断记录真实 Thread 角色、poll、父节点 RSSI、累计 MAC/UDP 计数与选源地址。单凭 HA ACK 丢失不能断言 Thread detached；需要把新诊断与 HA/BR 日志或抓包对照。
