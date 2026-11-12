# Thread SED

Thread SED 是面向 ESPHome Thread SED / REED 的 HA 自定义集成。设备使用独立的 `thread_sed_api`，保留原生 API 的 TCP、Protobuf 实体协议与可选的 Noise 加密；不再经过业务 UDP 网关或实体桥。

## 安装与接入

1. 将仓库的 `custom_components/thread_sed` 复制到 HA 的 `config/custom_components/thread_sed`，重启 HA。仓库也保留 HACS 自定义仓库布局；新版本需要发布包含此目录的 release。
2. HA 最低版本为 **2026.11**。集成依赖 HA 自带的 ESPHome 客户端依赖，不自行升级或降级公共 `aioesphomeapi`。设备侧 fork 基线为 ESPHome **2026.10.2**，后续版本需验证。
3. 节点启用 IPv6、OpenThread 与 `thread_sed_api`，不要配置官方 `api:`。见 [配置迁移](MIGRATION.md)。
4. BR 将节点的 `_thread-sed._tcp` SRP 服务发布到局域网 mDNS 后，HA 显示发现卡片。保持节点唤醒直至接入完成；未配置 `encryption` 时密钥留空，启用后输入节点的 Noise key。也可手动输入节点 IPv6 / 主机名和 API 端口。

每个节点一个配置条目，以 MAC 标识身份；用户确认之前不会创建实体。公告不包含密钥。默认明文模式不使用共享密钥认证，Noise 模式提供加密与密钥认证。HA 需要能路由到节点 OMR IPv6，Docker 使用 host 网络，允许节点 TCP 6053 和 HA TCP **6064**。6064 与官方 ESPHome 的反向连接端口分开。BR 只需 IPv6 转发、SRP/mDNS 代理，不再运行 `thread_gateway`。

## 连接与休眠

- 首次由 HA 连接节点；Thread SED 客户端完成当前模式的原生握手后，设备保存其地址；Noise 模式先验证密钥。以后唤醒时节点主动拨回 HA，加密时 HA 仍是 Noise initiator。HA 2026.11 直接使用原生客户端的 `start_connection_from_socket()` 接收连接，不再需要本机 TCP 转接层。
- nRF52 的 `deep_sleep_id` 自动接入 System ON 休眠 guard。准备休眠时，等待初始实体状态发送完成、事件队列确认和 HA 应用层 ACK。默认最多等待 10 秒；超时仍允许休眠并记录日志，避免网络故障耗尽电池。
- 每次正常休眠关闭 TCP，唤醒立即重连，不要求连接在休眠期间保活。醒着时临时缩短 SED Data Poll，休眠前恢复；不切换 RxOnWhenIdle，不把 SED 改成 MED。
- 传感器状态依靠重连后的原生初始状态同步；事件使用最多 32 条的 RAM 队列，HA 确认后删除，普通重连去重。队列满时丢弃最新事件并告警；断电会丢失 RAM 队列。HA 崩溃与磁盘写入之间不承诺严格 exactly-once。
- HA 缓存实体描述与最新状态。固件配置指纹、编译时间或 ESPHome 版本变化时重取实体清单；普通唤醒跳过清单枚举。实体增删在 HA 中即时处理，不需要重启。休眠 ACK 前保存状态；定时休眠超过预期时长加 60 秒仍未唤醒会变为不可用。
- 未设置休眠时长的 GPIO 唤醒节点保留休眠状态，无法凭“长期安静”区分正常休眠和电池耗尽。HA 启动/条目重载会尝试一次正向连接；正常休眠期间不持续探测。
- 控制只发往在线节点；休眠、离线命令不缓存，也不显示乐观执行成功。

HA 主机地址变化后，仍可由 HA 正向连接节点重新学习反向目标；节点地址变化通过 Zeroconf 更新条目并重载连接。首次接入或地址恢复阶段需留出足够唤醒窗口。跨 VLAN / 缺少 mDNS 转发时使用手动接入，并确保双向 IPv6 路由。

## 实体与第一版范围

支持 sensor、binary_sensor、text_sensor（映射 HA sensor）、event、switch、button、number、select、text、light、fan、cover、climate、lock、valve。节点不再维护另一份实体映射，`internal: true` 的实体不暴露。传感器限流改用 ESPHome 自身 filters / update_interval。

第一版没有移植 ESPHome 的蓝牙代理、语音、相机、交互 wizard、用户自定义 API 服务和 HA 状态导入组件。灯光、空调等提供基础控制；设备端保留原生描述，但 HA 的高级控制项仍需后续补齐。日志/OTA 继续使用各自已有组件，不能直接假定官方 ESPHome Dashboard 能识别新的服务类型。

## 验证

```sh
pip install aioesphomeapi==46.25.0
python -m unittest discover -s thread_sed/tests -v
```

测试使用真实原生客户端、真实 Noise 握手及本地 TCP 对端；HA 生命周期使用轻量 stand-in。ESP32/nRF52 已做配置校验和内存内 C++ 生成检查；**未运行固件编译，未上板验证，也未在真实 HA 上完成验收**。重点验收长时间休眠、HA/BR 重启、IPv6 前缀变化和高频事件。

## 传感器采样与上报

默认 `send_mode: auto` 保持实体发布后自动上报、重连同步缓存；设置 `send_mode: manual` 后，发布仅更新缓存，初次连接也不自动上传状态。由 `thread_sed_api.send` 发送 entity_id 指定实体的当前缓存及相应待发事件，并等待一个批次 ACK；省略 entity_id 时沿用全部可暴露实体。两种模式都支持 send action。

**状态同步不等于重新采样。** nRF52 System ON 唤醒会保留上次状态，PollingComponent 也不保证恰好在唤醒时完成更新。需要新读数的节点应在既有唤醒自动化中触发传感器更新，等待转换及发布完成后再请求休眠。休眠 guard 只确认已发布的数据和排队事件，不负责调用传感器采样。

## 可选 encryption

默认不加密、不需要 key：

```yaml
thread_sed_api:
  id: sed_api
  deep_sleep_id: sleep_control  # nRF52 自动休眠 guard；常供电 REED 省略
```

仅在需要加密时配置：

```yaml
thread_sed_api:
  id: sed_api
  deep_sleep_id: sleep_control
  encryption:
    key: !secret thread_sed_noise_key
```

启用 encryption 后 key 为必填的 32 字节 base64。未配置 encryption 时不加载 Noise 组件，HA 接入密钥留空。两种模式均支持正向与反向 TCP、实体缓存、事件确认和休眠确认。已配置密钥的 HA 条目不会自动退回明文模式。

## 手动 send 与 is_ready

设备组件和 HA 集成需要一起更新。`connected` 仅表示原生握手完成；`thread_sed_api.is_ready` 要求 HA 完成清单处理、状态/服务/控制订阅及 ready 回复，且连接当前可启动一次发送。它不代表传感器已采样完成。

```yaml
thread_sed_api:
  id: sed_api
  deep_sleep_id: sleep_control
  send_mode: manual  # 默认 auto；manual 只在 send 时上传状态和待发事件
  # encryption 可省略；需要加密时沿用既有 encryption 配置

script:
  - id: report_once
    mode: single
    then:
      # 在此安排采样，并等待异步转换及 publish_state 完成。
      - wait_until:
          condition:
            thread_sed_api.is_ready: sed_api
          timeout: 3s
      - thread_sed_api.send:
          id: sed_api
          entity_id: [sys_vdd, battery_level, thread_identity]  # 替换为实际实体 ID
          timeout: 2s  # 从 send 开始，覆盖状态发送和等待本批 ACK
          on_success:
            - logger.log: 本次数据已由 HA 确认
            - deep_sleep_nrf52.enter: sleep_control
          on_error:
            - logger.log: 本次发送失败，按当前策略休眠
            - deep_sleep_nrf52.enter: sleep_control
```

`wait_until` 超时会继续执行，send 会检查 ready 并进入 on_error。未 ready、发送忙、TCP 写失败、断线或 ACK 超时均走 on_error；只有同一连接回复匹配批次 ID 才走 on_success。两个分支均为普通动作列表，可使用 delay / script.wait 等；分支完成后才继续外层动作。

为严格控制整次唤醒，不要另设会抢先进入休眠的 run_duration：去掉固定 run_duration，在采样/事件处理自动化中调用 report_once，最后由成功/失败分支主动请求休眠。GPIO 事件应先完成识别并触发 event，再调用 send。API 不负责监听休眠期间的任意传感器变化，也不负责采样。

send 复用原生实体状态迭代器，不是原子采样快照；调用期间若仍在采样，可能读到不同时间的缓存。因此采样时序由 YAML 保证。manual 下，send 开始后新产生的事件留给下一个批次；已发送事件收到批次 ACK 后才从 RAM 队列移除，失败保留。不会因为 ACK 超时自动重复整个批次，后续重试由 YAML 决定。每个节点同时只支持一个待确认批次，script 建议 single 或 queued，避免并发发送。

手动 send 的 ACK 表示 HA 已处理到批次结束标记并保存缓存；它不代表外部 HA 自动化或物理执行器动作已完成。休眠通知仍有独立确认，但 manual 模式下不会借此偷偷上传尚未 send 的数据。日志包含 send 开始、ACK 耗时或失败原因，便于测量实际发送时间。

### 一次 send 指定单个或多个实体

```yaml
- thread_sed_api.send:
    id: sed_api
    entity_id: [sys_vdd, battery_level, thread_identity]
    timeout: 2s
    on_success:
      - logger.log: 本批所选实体已确认
    on_error:
      - logger.log: 本批发送失败
```

单个实体写成 `entity_id: sys_vdd`。列表可以混合传感器、二进制传感器、文本传感器及其他支持的状态实体，同一次 action 共用一次 ACK；无需拆成多个 send。省略 entity_id 保留全量发送，空列表和无效 ID 会报错。按钮等无状态实体不能选择；internal 实体需先通过 name 或 internal: false 暴露，确保 HA 有对应描述。

选择 event 实体时，只发送该实体在 send 开始前已入队的事件，不会重新触发事件；未选中的事件继续保留。HA 按“节点启动标识 + 事件实体 + 序号”去重，因此跨实体分批、按不同顺序发送不会误丢较早的待发事件。

send 仅发送所选实体已发布的缓存，不隐式采样。严格限制上报范围时使用 send_mode: manual；auto 模式下独立的自动发布仍按原配置运行。采样/等待转换/发布完成后再调用这一个 send action。

## auto 模式的组件级 ACK 回调

```yaml
thread_sed_api:
  id: sed_api
  send_mode: auto
  # auto_ack_timeout: 2s  # 可省略，默认 2 秒
  on_ack:
    - lambda: |-
        ESP_LOGI("report", "HA ACK %s in %u ms, pending=%d",
                 batch_id.c_str(), elapsed_ms, has_pending);
  on_ack_error:
    - lambda: |-
        ESP_LOGW("report", "HA ACK %s failed: %s", batch_id.c_str(), reason.c_str());
```

只有配置 on_ack 或 on_ack_error 才启用自动批次确认；不配置时保留原来的自动发送行为，不额外发送自动确认标记。回调仅用于 send_mode: auto；manual 应使用 send action 自身的 on_success/on_error。

自动发布仍按原生 API 路径发送，不为回调重发全部实体快照。在原生初始同步/已排队状态发送完成、自动事件队列确认完成后，组件插入一个批次结束标记，并复用 HA 已有的 thread_sed.batch ACK。连续发布可以合并为一次确认，因此不会保证每次 publish 都产生一次回调；初次连接的状态同步也会确认。显式 send 的快照不重复触发组件级回调。

- on_ack 参数：batch_id（字符串）、elapsed_ms（从该周期首个自动发送/排队动作到确认的毫秒数）、has_pending（该结束标记之后是否已有新的自动更新待确认）。
- on_ack_error 参数：batch_id、reason；原因包括 timeout、disconnected、write_failed、sleep_deadline。已收到匹配 ACK 后发生断线，已覆盖的数据仍按成功处理；尚未覆盖的更新另行报告。
- 等待 ACK 时产生的新数据属于下一个确认周期，旧 ACK 不会确认新数据。消息仍由 TCP 保证传输，不另加应用层报文重传。
- 完全没有建立可用连接时，本地缓存更新不产生虚假的 ACK；恢复连接后通过初始状态同步确认。错误计时从实际进入连接的自动发送/排队开始，不在长期离线时不断触发超时回调。
- 睡眠状态通知在自动确认处理后发送，组件会等待 ACK 回调中的异步动作完成。原有 sleep_ack_timeout 仍负责故障时的最大交付等待；回调不替代采样完成条件，也不表示整次唤醒中未来任务已经完成。

回调中若再次修改对外发布的实体，会产生下一轮自动上报；避免在 on_ack 中无条件触发同一实体的循环发布。若必须精确指定一次唤醒中的数据和时序，继续使用 manual + send。

HA 侧复用已有批次 ACK，无需增加全局自动化或更改集成配置。
