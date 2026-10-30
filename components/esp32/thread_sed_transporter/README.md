# thread_sed_transporter

ESP32/ESP-IDF MTD 的事件驱动实体发送组件，配合 ESP32 `thread_gateway` 使用。
依赖 `openthread` 和自动加载的 `thread_entity_bridge`。显式 external_components 列表需同时包含实体桥。

实体负责采集、过滤和发布；本组件不调用源组件的 update，也不自动定时发送状态。
采集异步完成或源端延迟过滤生效后，再调用发送 action。

## Event 实体

在 `events` 中登记 Event 实体，仍由自动化手动调用 `send`。同类型连续发送视为多次事件，
不受 `force: false` 的变化过滤影响。每次调用立即冻结类型，ACK 重发沿用同一序号；
不会因为网关重连或全量同步而自动发送上一次事件。

```yaml
event:
  - platform: template
    id: key_event
    name: "KEY Action"
    device_class: button
    event_types: [single, hold]
    on_event:
      then:
        - thread_sed_transporter.send:
            id: sed_transport
            entity_id: key_event
            ack:
              timeout: 200ms
              resend: 1
            on_success:
              - lambda: 'ESP_LOGI("KEY", "Event ACK, report=%u", report_id);'

thread_sed_transporter:
  id: sed_transport
  openthread_id: openthread_main
  node_id: room_key
  events: [key_event]
```

按键自动化调用 `event.trigger: {id: key_event, event_type: single}` 后，上述 on_event 会发送。
也可直接 `send: {entity_id: key_event, entity_state: single}`，不必先触发本地 Event；
在 on_event 中覆盖值的 lambda 为 `return std::string(event_type.c_str());`。
一次 `entities` 可混合 Event 与其他实体。Event 必须声明 1～32 个非空、不重复的类型，
每项不超过 64 个 UTF-8 字节，发送值必须属于该列表。未触发过的 Event 需显式提供 entity_state。
结果回调提供 report_id，不继承 on_event 的 event_type。

接收端必须同步更新 `thread_gateway` 和实体桥；网关支持 `capacity.events`（默认 16），
批准后重启网关即可创建 HA 原生 event 实体。普通 text_sensor 的重复文本不具备这一语义。


## 配置

以下 ID 均引用其他组件中已经定义的实体和实例。

```yaml
thread_sed_transporter:
  id: sed_transport
  openthread_id: openthread_main
  node_id: room_key
  name: Room Key
  manifest_revision: 1
  active_poll_period: 100ms
  sensors:
    - id: temperature
      delta: 0.5  # 可选，默认 0：任意实际变化
    - battery_voltage
  binary_sensors:
    - key_state
  text_sensors:
    - id: event_text
      max_length: 32  # UTF-8 字节，上限 128
```

清单共允许 1..16 个实体。也支持 switches、numbers、selects、buttons、text_inputs，
沿用源实体元数据和能力；button 没有状态，不能作为发送 action 的 entity_id。
控制实体仍受网关现有规则约束：SED 控制实体仅供 Web 控制。

discovery 默认多播 ff13::8000:8562、端口 18512、单次发现等待 1s；
可在 discovery 下指定 gateway_id。timeout 默认 5s，是每个任务发送前等待目标和清单
就绪的预算，不是包含排队和 ACK 恢复的总时限。

## 手动发送

统一使用 `thread_sed_transporter.send`。entity_id 接收一个 ID 或 ID 列表，
只能引用本实例登记的有状态实体或 Event，列表不允许重复或为空。

```yaml
- thread_sed_transporter.send:
    id: sed_transport
    entity_id: key_state
    force: true
    ack:
      timeout: 500ms
      resend: 2
    on_success:
      - logger.log: "本次事件已收到网关 ACK"
    on_fail:
      - logger.log: "本次事件发送失败"

- thread_sed_transporter.send:
    id: sed_transport
    entity_id: [temperature, battery_voltage, event_text]
    force: false
```

- force 默认 false，可写 lambda。false 对数值按 delta 过滤，对布尔、文本和控制实体
  按值变化过滤；true 绕过变化过滤，但仍拒绝无效值。
- 调用时冻结指定实体的当前值；排队、发现和重试均不重新采样。
  过滤在首次发送前对这份快照执行，比较当时最新的已确认基线。
- 首次发送某实体，以及网关重新同步后的首次发送该实体，会发送其有效快照。
  每个实体分别维护同步状态；发送 A 不会让尚未同步的 B 被当作已同步。
  清单仍包含全部登记实体，但状态包仅包含本次 action 选择的实体。
- 不再维护周期全量计数。需要定期全量时，在自己的定时或唤醒自动化中显式列出 ID 并设置 force: true。
- 批量中任一实体当前值无效，会拒绝整批并回调失败，避免把部分事件误报为整批成功。
  所有指定值均未变化时，没有 ACK 的请求不发状态包、按过滤成功结束；
  配置 ACK 的请求发送 values={} 空轮次以取得通信确认。
- 重复值如需作为新事件发送，可使用 force: true；这会产生新的 report_id/seq。
  网关仍按实体状态处理，相同状态不保证产生新的 HA 状态变化事件。

## 直接指定本次发送值

单实体可使用 `entity_state`，省去先发布本地状态的步骤：

```yaml
- thread_sed_transporter.send:
    id: sed_transport
    entity_id: event_text
    entity_state: "single"
    force: true
    ack:
      timeout: 500ms
      resend: 2
```

批量发送使用 `entities`，允许混合覆盖值和源实体当前状态：

```yaml
- thread_sed_transporter.send:
    id: sed_transport
    entities:
      - entity_id: event_text
        entity_state: !lambda 'return std::string("double");'
      - entity_id: temperature
        entity_state: 23.5
      - entity_id: key_state
        entity_state: true
      - entity_id: battery_voltage  # 没有覆盖值，读取当前状态
    force: true
    ack:
      timeout: 500ms
      resend: 2
    on_success:
      - logger.log: "批量事件已收到 ACK"
```

顶层 `entity_id` 与 `entities` 互斥，顶层 `entity_state` 只能搭配一个实体。
覆盖值支持常量及 lambda，按登记实体类型确定为数值、布尔或字符串，不会隐式把字符串转换成数值。
所有实体仍须在本发送器登记，且同一批次不可重复。覆盖值不要求源实体已经发布状态，
但混合批次中未覆盖的实体仍须有有效当前值；任一无效项会拒绝整批。
数值必须有限，文本遵守登记的 UTF-8 `max_length`，动态无效值通过原有失败回调报告。

覆盖只影响发送快照，不调用源实体的 `publish_state`，不触发其 `on_value`、过滤器或硬件控制。
lambda 在 action 执行时求值一次，排队和重试保持同一快照。
`force`、ACK、回调继续作用于整个批次。成功提交的覆盖值也会更新发送基线，
下一次直接读取本地旧状态时会与此基线比较；重复发送同一事件请使用 `force: true`。

## ACK 与结果回调

ack、on_success、on_fail 均属于当前 action，互不共享重试预算。
ack.timeout 默认 2s，范围 1ms..60s；resend 默认 0，范围 0..10。
配置 ack 后，成功表示网关已确认该轮全部分包；不是 HA 送达或物理动作完成确认。
省略 ack 时，on_success 只表示本地发送成功或全部值被过滤，不能证明远端收到。

action 立即返回；最多接受 8 个未完成任务。FIFO 执行首次发送，前一个任务等待 ACK 时
可处理下一个任务。队列满会报告失败。每个请求最多一次最终回调；回调参数为
uint32_t report_id，不继承调用位置的临时参数。后续步骤需要等待确认时写在 on_success 中。

有 ACK 时仅在确认成功后更新对应实体的冻结基线，失败保留旧基线；
没有 ACK 时以每页本地发送成功为更新边界。force 发送也会更新基线。
较旧报告的迟到 ACK 不会回退较新基线，重新同步前的 ACK 不会建立新的同步基线。

保留组件级 on_report_success/on_report_fail（仅通知启用 ACK 的请求），以及
on_complete(success, sent, unchanged, invalid, errors)（本地发送阶段结束，尚未确认远端收到）。
队列拒绝的请求没有本地发送阶段，因此不触发 on_complete。

ACK 耗尽后每份冻结报告最多进行一次有界重新发现，恢复目标后重发相同字节并重新执行一轮
ACK 预算。通信预算包含初始 timeout、两轮 ack.timeout * (resend + 1) 和恢复 timeout，
另有排队及调度时间。保留手动 thread_sed_transporter.discovery 动作。

## ESP32 平台与协议

沿用 JSON v1、node_type: sed、清单、分包和 report_ack，与现有 ESP32 网关互通。
单包最大 1232 字节，最多 16 页。接收回调只复制和排队，解析、实体控制和自动化在主线程运行。

使用 ESP-IDF 的 OpenThread InstanceLock、FreeRTOS 接收队列和 esp_timer 64 位单调时间。
基线、会话及网关缓存保存在 RAM；普通运行与 light sleep 保留，deep sleep/重启后重新同步。
不再使用旧版 RTC 基线记录，也不写 Flash 保存事件历史。

通信时临时缩短 SED poll 至 active_poll_period（默认 100ms），空闲恢复 OpenThread 配置值；
0ms 禁用，MED 维持连续接收。control_ready 默认 false。
ESP32 不提供 nRF52 的 deep_sleep_id/System ON sleep guard；在 on_success/on_fail 中安排
休眠，或在外部自动化中检查 is_busy()。它包含发送、ACK、发现、接收队列和结果自动化；
在结果回调内部以及 control_ready 为 true 时仍为 busy，不要在回调内等待它变为 false。
采集及源端过滤等待期间，也需由调用方控制休眠时机。

## 迁移与验证

旧 report 和 report_independently 动作已合并为 send：分别显式列出目标 ID，
原独立强制上报使用 entity_id: 原sensor_id、force: true。
full_report_every、source_components 和 acquisition_delay 已移除并提供配置错误提示。
需要刷新硬件时，先在自动化中执行 component.update，等待源传感器发布有效值后再 send。
原 RTC 差分基线改为与 nRF52 相同的 RAM 确认基线，ESP32 deep sleep 后首轮会重新发送。

使用 ESPHome Python 环境运行 `python tools/validate_sed_send.py --esp32`，仅验证配置边界和内存代码生成，
不编译固件。队列并发、ACK 丢失恢复、休眠时序及功耗仍需设备联调验证。
