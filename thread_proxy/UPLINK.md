# Thread Proxy 上行 v0.7.0

本轮实现节点 → BR → HA 的传输闭环。保留现有 `thread_sed_api` UDP 格式和节点手动/自动发送、ACK 超时/重发语义；不要求节点改为 TCP，也没有业务下行控制。

## 确认语义

1. BR 使用 OpenThread 内部 UDP socket，固定监听 6064。完整分片经过模式匹配、可选 AES-GCM 校验和记录边界检查后进入有界 RAM 队列。
2. 入队成功立即从节点发送目标的同一个 BR Thread 地址返回 ACK，载荷为原节点 wake nonce（8 字节）。HA 无需在线。相同组完整重传且载荷一致时仅重发 ACK，不再次入队。队列满/分配失败/分片未齐不回成功 ACK。
3. HA 从 BR 的独立 TCP 6065 接收原始记录，接受后返回投递确认，BR 才释放队首。重连/5 秒未确认时重发，投递 ID 不变。每个 BR 只允许一个显式启用的上行消费者；普通管理订阅不会取走数据。
4. HA 用节点 MAC、wake、group、schema、kind、flags 聚合去重，不把 BR ID 当节点 ID；另外按节点事件 token 去重。缓存有界，无法保证无限时间或 HA 重启后的永久去重。

节点 ACK 表示 **BR RAM 已接收**；TCP 确认表示 **HA RAM 已接收**，均非磁盘持久化承诺。BR 重启会丢失队列，HA 重启会丢失接收缓存及去重窗口。需要持久化投递时应另加存储机制，不能把本版 ACK 当作 HA 实体已更新的证明。

## 节点发现与迁移

BR 每秒检查自身 SRP 表中的 `_thread-sed._udp`，从 BR Mesh-Local EID 向节点发送现有 PROBE；可用 `udp.nodes` 补充节点地址。初次探测携带空 schema，节点按原机制回传描述。维护探测至少间隔 30 秒，最近有上报的节点跳过，单轮最多 2 个地址。

此版**不做跨 BR 的 SRP 服务目录同步或 BR 负载优选**。节点可通过另一 BR 的 PROBE 切换目标，当前在途组仍向原发送目标重传。不会依赖 HA 主机选择节点 OMR 的下一跳来返回 ACK。BR 之间 Thread 无线不可达时，必须由能到达该节点的 BR 探测。旧 Thread SED 集成应停用，避免旧 HA PROBE 抢回节点目标。

节点原有 YAML 可保持不变（UDP 模式、端口 6064）；手动 `send` 成功回调现在表示本地 BR 入队。若节点启用 UDP 加密，在 BR `udp.encryption.key` 配置同一密钥；代理顶层 `encryption.key` 单独保护 BR↔HA TCP。

## TSD2 节点报文

全部整数为网络字节序，头 48 字节：`!4sBB6s8sIIHH16s`。

| 字段 | 内容 |
|---|---|
| magic / kind / flags | `TSD2`，DATA=2、ACK=3、SLEEP=6、HEARTBEAT=7；encrypted=1、descriptors=2 |
| MAC / wake | 节点 MAC 6 字节、节点当前 wake 8 字节 |
| sequence / group | 各 4 字节；sequence 每次实际发送递增，group 重传不变 |
| part / parts | 各 2 字节；768 字节分片，最多 43 片，完整载荷最大 32768 字节 |
| schema | 原 16 字节 schema 指纹 |

DATA 为若干 `uint16 type + uint16 length + Native API protobuf`；允许 type=19 的零长度消息。AES-256-GCM 沿用 nonce=`发送方 boot8+sequence4`、AAD=48 字节头、tag16。BR ACK 头的 MAC/group 为目标节点；boot/sequence 为 BR 自身随机会话 nonce 与递增计数，ACK 载荷是节点 wake8。

## BR TCP 封装

沿用 Native API 消息 35（HomeassistantActionRequest），service=`thread_proxy.packet`，字符串 map：

```json
{"delivery":"<BR随机boot16hex>-<计数8hex>","mac":"<12hex>","wake":"<16hex>","group":"<8hex>","schema":"<32hex>","kind":"2","flags":"0","part":"0","parts":"1","payload":"<Base64>"}
```

TCP 内每片最多 384 字节原始载荷（Base64 最多512字符），完整组仍最大32768字节；每条消息小于1024字节 Protobuf 上限。原始 TSD2 头关键信息单独携带，解密后载荷原样转发，不让 HA 保存节点 UDP 密钥。

身份/能力验证后，HA 用 Native API 消息40（HomeAssistantStateResponse）显式启用：`entity_id=thread_proxy.uplink,state=tsd2-v1`；成功接受完整组后发送 `entity_id=thread_proxy.delivery_ack,state=<delivery>`。载荷已组齐但解码/描述身份无效时发送 `thread_proxy.delivery_reject`，BR 记录警告并移除毒化队首，避免堵住其他节点。未组齐或封装错误不确认。

## HA 接收接口与当前范围

`/api/thread_proxy` 与诊断包含上行接收数、重复数、已见节点摘要；管理页显示 UDP 就绪状态、队列组数和已见地址数。

- `thread_proxy_packet` HA 事件：完整原始记录组（Base64），附节点 MAC、来源 BR、schema、kind、flags、wake、group；跨 BR 重复报文仅发布一次。
- `thread_proxy_node_event` HA 事件：原 KEY EVENT token 解包得到的 `mac`、`key`、`event_type`、`token`；不会调用任意节点指定的 HA 服务。
- 接收缓存保留每节点最近描述和最近状态报文，最多64个节点；全局组去重和事件去重各最多2048项。

**本次实现传输及接收缓存，不创建代理节点的 HA 设备/实体，不提供控制实体或业务下行。** 现有“创建设备”仍仅控制 BR 自身诊断，与报文上行独立；后续可直接消费原始 Native API 实体描述完成节点实体映射。

## YAML diff

```diff
 thread_proxy_api:
   thread_br_id: thread_br_main
+  # 默认无需添加 udp 段；这些是可选覆盖值。
+  udp:
+    queue_groups: 16      # 完整报文组上限，队列满时不返回成功 ACK
+    queue_bytes: 131072   # 载荷总字节上限，优先 PSRAM，缓存不跨重启
+    # nodes: ["fdea:1234::123"] # 无本地 SRP 记录时补充
+    # encryption:
+    #   key: !secret node_udp_key # 仅节点 UDP，与顶层 TCP Noise 密钥无关
```

## 待上板测试

按项目要求未编译固件。请先更新 BR 固件与 HA 集成，保持节点唤醒等待 BR PROBE，再验证：明文/加密，描述大报文，SED 手动/自动上报及 ACK，HA 断网后积压与恢复，队列满背压，BR 重启丢队列，两个可达 BR 间切换及事件去重。上报耗时是否改善仍需实际无线测试确认。
