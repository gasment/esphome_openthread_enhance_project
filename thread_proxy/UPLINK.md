# Thread Proxy 上行 v0.8.0

本轮实现节点 → BR → HA 的传输闭环。保留现有 `thread_sed_api` UDP 格式和节点手动/自动发送、ACK 超时/重发语义；不要求节点改为 TCP，也没有业务下行控制。

## 确认语义

1. BR 使用 OpenThread 内部 UDP socket，固定监听 6064。完整分片经过模式匹配、可选 AES-GCM 校验和记录边界检查后进入有界 RAM 队列。
2. 入队成功立即从节点发送目标的同一个 BR Thread 地址返回 ACK，载荷为原节点 wake nonce（8 字节）。HA 无需在线。相同组完整重传且载荷一致时仅重发 ACK，不再次入队。队列满/分配失败/分片未齐不回成功 ACK。
3. HA 从 BR 的独立 TCP 6065 接收原始记录，接受后返回投递确认，BR 才释放队首。重连/5 秒未确认时重发，投递 ID 不变。每个 BR 只允许一个显式启用的上行消费者；普通管理订阅不会取走数据。
4. HA 用节点 MAC、wake、group、schema、kind、flags 聚合去重，不把 BR ID 当节点 ID；另外按节点事件 token 去重。缓存有界，无法保证无限时间或 HA 重启后的永久去重。

节点 ACK 表示 **BR RAM 已接收**；TCP 确认表示 **HA RAM 已接收**，均非磁盘持久化承诺。BR 重启会丢失队列，HA 重启会丢失接收缓存及去重窗口。需要持久化投递时应另加存储机制，不能把本版 ACK 当作 HA 实体已更新的证明。

## 节点探测和直接建实体（0.8.0）

`thread_proxy_client` 不注册/消费节点 SRP 记录，也不依赖 HA 到节点的 IPv6 路由。客户端绑定自身 Thread Mesh-Local 地址，用 TSD2 DISCOVER（kind=8，空载荷）向保存的 BR 候选及 `[ff03::1]:6064` 发起探测，未确认时每秒重试。Realm-local 组播可沿 Thread mesh 转发。

BR 从自身 Mesh-Local EID 返回 PROBE（kind=1），载荷回显节点当前 wake8；节点验证 Thread 前缀、6064端口及回显，选择首个有效 BR。保持当前 BR，投递失败或重新入网后重新探测。BR 队列满时不响应新的探测，允许其他 BR 接管；不是持续按信号强度选优。

节点原来的 HA 直连缓存使用另一 NVS key，不会迁入。新固件需把顶层、send/is_ready/can_sleep 前缀从 thread_sed_api 改为 thread_proxy_client，并删除 transport 配置。

HA 经已绑定 BR 收到描述后直接创建设备/只读实体，不增加节点 ConfigEntry 或接入确认。缺少 schema 时 HA 通过消息40 `thread_proxy.request_schema` 携带节点 MAC，请 BR 转发 SCHEMA（kind=4）；后续补齐描述并回放有界缓存中的状态/事件。描述、普通状态及事件游标由 HA Store 合并保存。

BR 不再扫描 SRP，也不再提供 `udp.nodes` 配置。参与代理的 BR 请先绑定 HA，再更新节点。普通 OpenThread 组件自身的 SRP 功能保持原样，客户端不需要其业务服务记录。

## TSD2 节点报文

全部整数为网络字节序，头 48 字节：`!4sBB6s8sIIHH16s`。

| 字段 | 内容 |
|---|---|
| magic / kind / flags | `TSD2`，DATA=2、ACK=3、SLEEP=6、HEARTBEAT=7、DISCOVER=8；encrypted=1、descriptors=2 |
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

0.8.0 已直接创建节点设备及只读实体；普通 sensor/binary_sensor/text/event 保留状态，控制类状态投影为只读 sensor/binary_sensor，不提供业务下行。描述变化动态增删实体，HA 重启恢复普通状态和事件游标。现有“创建设备”仍仅控制 BR 自身诊断，与节点实体独立。

## YAML diff

```diff
 thread_proxy_api:
   thread_br_id: thread_br_main
+  # 默认无需添加 udp 段；这些是可选覆盖值。
+  udp:
+    queue_groups: 16      # 完整报文组上限，队列满时不返回成功 ACK
+    queue_bytes: 131072   # 载荷总字节上限，优先 PSRAM，缓存不跨重启
+    # encryption:
+    #   key: !secret node_udp_key # 仅节点 UDP，与顶层 TCP Noise 密钥无关
```

## 待上板测试

按项目要求未编译固件。请先更新 BR 固件与 HA 集成，保持节点唤醒等待 BR PROBE，再验证：明文/加密，描述大报文，SED 手动/自动上报及 ACK，HA 断网后积压与恢复，队列满背压，BR 重启丢队列，两个可达 BR 间切换及事件去重。上报耗时是否改善仍需实际无线测试确认。
