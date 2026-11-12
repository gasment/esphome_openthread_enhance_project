# Thread SED UDP v2

本协议复用 ESPHome Protobuf 的实体描述、状态和命令**编码**，不使用 TCP 流、Hello/Noise 原生会话握手或 Subscribe 往返。正常批次是 UDP DATA → UDP ACK；首次接入或缺少描述时增加按需描述交换。

## 报文头

所有多字节整数为网络字节序，固定头长 48 字节：

|偏移|长度|内容|
|---|---|---|
|0|4|magic=TSD2|
|4|1|kind：1 PROBE / 2 DATA / 3 ACK / 4 SCHEMA / 5 COMMAND / 6 SLEEP / 7 HEARTBEAT|
|5|1|flags：bit0 AES-GCM，bit1 完整实体描述组|
|6|6|节点 MAC，PROBE 可用全零查询|
|12|8|发送者随机传输标识；节点每次 System ON 唤醒更新|
|20|4|发送者报文序列号，组成 GCM nonce|
|24|4|业务组号|
|28|2|分片编号，从 0 开始|
|30|2|总分片数|
|32|16|用户配置、已加载组件源码及 ESPHome 版本的指纹，控制报文可为零|

DATA 负载由多个 [uint16 message_type, uint16 length, protobuf bytes] 组成。单组最多 32768 字节、最多 43 片，每片负载最多 768 字节。节点按循环逐片发送并处理 EAGAIN；完整一轮的最后一片发出后开始等待 `ack_timeout`，未确认时最多额外重发 `ack_resend` 轮（默认 500ms、2）。业务组号、控制标记和已序列化数据保持不变；每轮重新分配报文序列号。auto 的策略配置在组件级，manual 配置在每个 send action，回调也按此分层。分片发出停滞另有 5 秒内部保护；描述同步使用独立内部策略，不替代业务的 ACK 策略。接收端限制并发组数量和存活时间，拒绝越界或相互冲突的分片。

ACK 组号对应 DATA/COMMAND，负载是被确认报文发送者的 8 字节随机标识。节点同时校验 MAC、选定 HA 地址/端口、组号和标识，防止旧唤醒的 ACK 误完成新批次。重传同一完整组不会重复发布事件。

## 实体描述与接入

节点发布 _thread-sed._udp，端口默认 6064；TXT 包含 mac/name/transport/encryption/sleep。用户确认接入时 HA 发 PROBE，节点回复带 DESCRIPTORS 标记的 DATA，其中有 DeviceInfoResponse、thread_sed.session 元数据、实体描述及 ListEntitiesDoneResponse。HA 校验 MAC 后保存条目和描述。

节点保存收到 PROBE 的 IPv6 地址和 UDP 端口，重启后可直接使用。HA 定期维护 PROBE 携带已缓存指纹，匹配时刷新端点并回复轻量 HEARTBEAT，不触发描述同步。HA 收到未知指纹的业务时请求 SCHEMA，暂存业务；节点暂停本组交付，优先同步描述，再恢复原组确认。未接入的 MAC 不由 HA 监听器自动创建实体。

## 状态、事件与控制

manual 快照继续用原实体遍历器和选择 ID；事件保留原 RAM 队列、截止序列和按实体选择规则。批次结束记录只用于组边界；收到组 ACK 后触发原 send action 成功分支。auto 事件与状态共用批次 ACK，仍可有多个连续确认批次。失败保留未确认事件，等待后续业务触发或唤醒再发，不在回调后无限重启重试。

HA 在内存中处理状态和事件后立即发 ACK，后台合并写缓存。ACK 不表示磁盘持久化完成。组重复缓存与按节点事件 boot/entity/sequence 去重共同抑制普通重传；重启、掉电和有界去重缓存之外不承诺 exactly-once。

COMMAND 是单片，负载为目标节点当前唤醒标识（8 字节）加一条 Protobuf 命令。节点校验唤醒标识、HA 端点和命令类型，最近 16 个命令组只执行一次并重复 ACK。HA 200ms 未确认时重传，最多 4 次，超时记录告警。原生实体状态变化通过后续 DATA 回报。

SLEEP 是单向通知，负载 uint32 预期休眠秒数；0 表示无确定计时唤醒。它不要求回复，不阻挡业务完成后的休眠。通知丢失时 HA 会按最近活动窗口及缓存休眠信息调整可用性。旧唤醒的状态/心跳/休眠通知不会覆盖已观察到的新唤醒；HA 保留最近 64 个已结束唤醒标识。常醒节点连续 90 秒无业务或维护心跳变为离线。

## 可选加密

无 key 时为明文。配置 encryption.key 后使用 AES-256-GCM，nonce 为头部随机标识 8 字节 + 报文序列号 4 字节，AAD 为完整 48 字节头，16 字节 tag 附于密文末尾。随机标识使用系统加密随机源，序列号回绕时重新生成。重传重新分配报文序列号；组号保持不变。HA 与节点分别维护独立的随机标识/计数。

nRF52 使用当前 NCS 的 PSA Crypto GCM，ESP-IDF 使用 mbedTLS GCM。明文模式不提供认证；加密模式验证 GCM tag 和模式一致性。

## TCP 兼容

transport: tcp 保留此前原生 API 实现。协议 v2 不尝试将 UDP 包转换成 TCP 字节流，也不允许把 UDP 条目交给 TCP listener。
