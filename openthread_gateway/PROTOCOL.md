# OpenThread Gateway protocol v3

本文件定义 HA 实现的协议，供 transporter 适配（两个 SED 已实现初版，REED 待迁移）；不兼容 v1/v2。所有 UDP payload（含头）不得超过 **1232 bytes**。发现采用 DNS-SD `_esphome-tgw._udp.local.`，不使用 Thread realm-local 业务广播。

## Wire framing / authentication

```text
OTG3 <signature>\n<JSON UTF-8 bytes>
```

以上 `\n` 是一个 LF 字节，不是两个可见字符。JSON 根对象有整数 `v:3`、字符串 `type`，拒绝重复成员、NaN、Infinity 和非有限浮点数。

- 未认证：`signature` 为一个 `-`，仅允许未绑定节点的 `hello` 和后端的 `bind_offer`。
- 已认证：`signature` 为 64 位小写十六进制 HMAC-SHA256。
- HMAC 输入是 `b"OTG3\0" + 原始 JSON UTF-8 字节`，密钥为 64 位十六进制 `secret` 解码后的 32 字节。
- 接收端先保留原始 JSON bytes，再解析、查找绑定密钥和验证 HMAC，不能重新序列化后验签。不要求发送端使用相同的浮点格式或字段顺序。
- 初次邀请下发密钥依赖可信 LAN/Thread 和管理员确认，不是加密配对，不防恶意 LAN 中间人。后续报文有认证但无加密。

设备标识 `device` 是 16 位小写十六进制稳定物理标识（工厂 EUI64）。它不随 Thread 网络变化，不等同于可变的 Thread 扩展地址。实体/节点名字不承担身份认证。

## HELLO and admission

未绑定节点周期发送，重试同一轮时保持 request_id：

```json
{"v":3,"type":"hello","device":"0123456789abcdef","node":"door","name":"Door","network":"1234567890abcdef","node_type":"sed","boot":1,"generation":0,"session":"1111111111111111","request_id":"2222222222222222","expected_backend":"ha_main"}
```

`expected_backend` 可省略。填写只限制接收后端，不跳过人工接入。`network` 为当前 Extended PAN ID，`node_type` 是 `sed` 或 `reed`。`node` 为 1..32 位 ASCII 标识，`name` 最多 96 UTF-8 bytes。

`boot` 为持久化递增的 uint64 启动计数，在每次真正重启创建 session 前提交；System ON 唤醒不递增。`session` 为本次启动的 8-byte 随机值（16 hex）；`request_id` 为本轮发现/上下文协商的 8-byte 随机值。重复 HELLO 不得换 request_id；开始新一轮才更换。

HA 只记入候选，不创建实体。管理员选择接入后先持久化 `pending` 绑定，再发未签名 `bind_offer`：

```json
{"v":3,"type":"bind_offer","backend_id":"ha_main","device":"0123456789abcdef","node":"door","session":"1111111111111111","request_id":"2222222222222222","binding_id":"<32 hex>","generation":1,"secret":"<64 hex>"}
```

节点必须核对 device/session/request_id/expected_backend，原子保存 backend_id、binding_id、generation、secret 后，用新 secret 签名回复：

```json
{"v":3,"type":"bind_ack","device":"0123456789abcdef","backend_id":"ha_main","binding_id":"<32 hex>","session":"1111111111111111","request_id":"2222222222222222"}
```

HA 持久化激活并回复签名 `bind_complete`（同 device/binding_id/session/request_id）。丢失邀请或回执可以重复；HA 重启也保留 pending 密钥。节点已保存绑定但回执丢失时可直接进行签名 HELLO，由成功会话确认恢复。

## Session / endpoint confirmation

已绑定节点发送签名 HELLO，在上述字段上增加 `backend_id`、`binding_id`，`generation` 必须等于已绑定代数。HA 拒绝 boot 小于已确认启动计数的请求。

HA 返回签名 `session_offer`：包含 device/binding_id/session/request_id/generation，以及新的 16-hex `ctx`。节点核对后回复签名 `session_ack`，携带 device/backend_id/binding_id/session/request_id/ctx。

**只有有效 session_ack 才更新活跃来源 IP/UDP 端口及上下文。** 单独 HELLO 不会抢走正在使用的路由。返回签名 `session_ready`，携带 device/binding_id/session/ctx、已保存的 manifest_hash 和 `request_state:true`。重复 ACK 幂等。

同一轮 HELLO 的 offer 有效 30 秒；超时重新握手。HA 重启上下文不恢复，节点收到签名 `context_expired` 应发送新的签名 HELLO。不得重放旧上下文控制；节点在切换 ctx 时清除旧命令结果缓存、丢弃旧上下文待执行命令，但可保留尚未确认的状态快照并在新上下文重新编号同步。

后续节点消息公共字段：

```json
{"v":3,"type":"...","device":"0123456789abcdef","backend_id":"ha_main","binding_id":"<32 hex>","session":"<16 hex>","ctx":"<16 hex>"}
```

所有业务消息签名，必须匹配当前 ctx、session 和已确认来源端点。使用 OMR 等可跨 BR 路由的 IPv6 地址；不能固定 Mesh-Local EID 作为发往 HA 的源地址。

## Manifest

构造完整 UTF-8 JSON 文档 `{"name":"Door","entities":[...]}`。**manifest_hash 是这份文档原始 bytes 的 SHA-256**，不是 revision，也不依赖后端排序或数字格式。

将原始文档按 bytes 分片（允许在 UTF-8 字符中间切分），每片标准 Base64 编码，发送：

```json
{"type":"manifest","manifest_hash":"<64 hex>","part":0,"parts":2,"payload":"<base64 raw chunk>","...":"公共字段省略"}
```

`part` 从 0 开始，最多 128 片；文档最多 65536 bytes，每节点最多 256 个实体。必须按实际完整 wire 长度分包，建议原始块 512 bytes。分片可乱序/重传，但相同 part 内容必须相同。

HA 每片返回 `manifest_ack`，有 manifest_hash、part、complete。**只有全部到齐、完整哈希正确、所有实体均校验通过后才替换清单**，返回 complete/request_state=true。空 entities 是合法完整清单，会移除所有公开实体；不完整清单绝不用于删除。

实体 descriptor：

```json
{"id":"temperature","type":"numeric","name":"Temperature","unit":"°C","state_class":"measurement","entity_category":"diagnostic","capabilities":{"accuracy_decimals":1}}
```

`id` 唯一且符合 1..32 ASCII 标识；可选 name/unit/device_class/icon/state_class/entity_category/disabled_by_default/force_update。type 必须是 numeric、binary、text、event、switch、number、select、button、input_text、light、fan、cover、climate、lock、valve。

capabilities 延续节点源实体的描述格式（这是描述字段复用，不是 v2 报文兼容）：

- number：min/max/step、mode=0(auto)/1(box)/2(slider)。
- select：options；event：非空 event_types；input_text：min_length/max_length、mode=0(text)/1(password)。
- fan：speed/direction/oscillation、speed_count、preset_modes；direction wire 值 0=forward、1=reverse。
- cover/valve：position/stop/toggle/assumed_state；cover 额外 tilt。
- lock：open/requires_code/assumed_state；控制可附加 `code`，requires_code 为 true 时必须提供。后续节点适配需将 code 传入本地锁驱动。
- light：color_modes（ESPHome 位掩码）、effects、min_mireds/max_mireds；温度能力要求正的 mired 范围。
- climate：modes/fan_modes/swing_modes/presets（ESPHome 枚举）、custom_fan_modes/custom_presets、feature_flags、visual_min_temperature/visual_max_temperature/visual_target_temperature_step、temperature_unit=0(C)/1(F)。

复杂实体状态/控制字段见 `protocol.valid_value()`，HA 单位转换见 `entities.py`：亮度/颜色通道 0..1、位置/倾斜 0..1、颜色温度 mired、过渡/闪烁毫秒；HA 展示转换为 0..255、0..100、Kelvin、秒。Climate mode=0 off/1 heat_cool/2 cool/3 heat/4 fan_only/5 dry/6 auto；custom fan/preset 在 HA 显示 `custom:<值>`，避免与标准枚举名称冲突。

## Presence, state and events

presence 可携带 manifest_hash、heartbeat_interval_ms、control_ready、sleeping、sleep_for_s。返回 presence_ack；manifest 不匹配时 request_manifest=true。签名会话刚建立后应先确认/发送 manifest，再上报普通状态。

```json
{"type":"state","manifest_hash":"<64 hex>","report_id":1,"seq":1,"part":0,"parts":1,"snapshot":false,"values":{"temperature":22.5,"door":true,"key":"click"},"...":"公共字段省略"}
```

report_id/seq 为 uint32。每份报告冻结内容，重试不重新采样、不换 ID。完整报告的所有分片具有相同 seq/report_id/parts/snapshot；一个实体不能出现在多个分片。

HA 收齐且验证整份报告后才发布状态/事件，并回复 report_ack（report_id、seq、complete=true）。未收齐不发送 complete ACK；节点按原冻结报告重试，重复 ACK/报告幂等。普通状态按实体 seq 用 uint32 半范围比较，旧报告不会覆盖新状态；事件有 64 序号滑动去重窗口，允许窗口内乱序新事件。

完整状态同步设置 snapshot=true，**不得将最后一个历史事件当作新事件**；HA 不重放快照内 event。事件只在实际发生时放进增量报告。

`sleep_for_s` 最大 7 天，SED 应在睡前通过 presence 明确声明预期睡眠时长。REED 的 heartbeat_interval_ms 可为 1000..3600000。恢复后的可用性由有效通信更新，不以 mDNS/SRP 缓存当作在线证明。

## Commands

后端发签名 command：command_id（非零 uint32）、entity、operation（button=press，其余=set）、value。value 为桥接原生值：switch bool、number number、select/text string、button null、复杂实体 object。

节点在当前 ctx 内按 command_id 去重，重复命令返回已保存结果，不能重复执行动作。返回 command_result：command_id、success bool，可带 error string；若带执行后的 value，必须同时带新的 uint32 state_seq。HA 不乐观修改状态。

一节点只允许一个在途命令；同 ID 最多发三次，每次等待一秒。超时表示执行未确认，不等同于物理动作失败。SED 只有近十秒内上报 control_ready=true 时接受控制；不持久排队离线命令。重启/换 ctx 后旧命令不重放。

## Revocation

HA 解绑时先持久化 binding_id 对应撤销记录及旧认证密钥，再移除实体。收到旧绑定的已认证 HELLO/业务消息，返回签名 binding_revoked，携带 device/binding_id/session（若有 ctx/request_id 也回显）。

节点只在签名有效、backend/device/binding_id 匹配时清除本地绑定，持久递增 generation，再发送未绑定 HELLO。后续必须人工确认，不能依据“同一个 backend_id”自动恢复被撤销的授权。节点本地 unbind 也清除绑定并递增 generation；HA 中仍保存旧绑定时，应先在 HA 移除后再接入，不能凭未认证 HELLO 覆盖已授权记录。

## Test vectors and reference

`tests/test_protocol.py` 定义握手、分片、乱序、回滚及撤销测试。`tools/simulate_node.py` 是不依赖 HA 包的发送端参考实现。协议只提供本轮已实现行为；后续 C++ 适配如需改变 wire contract，应同时更新此文档和测试。

LAN mDNS 公告域为 `.local.`；Thread 节点通过 BR DNS-SD Discovery Proxy 查询 `_esphome-tgw._udp.default.service.arpa.`，不是向 Thread 组播 `.local.`。SED 实现见 [迁移说明](SED_MIGRATION.md)。
