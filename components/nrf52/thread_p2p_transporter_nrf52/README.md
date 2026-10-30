# thread_p2p_transporter_nrf52

依赖本项目的 `openthread_nrf52`，仅允许 `device_type: MTD`，用于 nRF52840/Zephyr 的 Thread 网内单播。
FTD（包括省略 device_type 而使用默认 FTD）在配置校验阶段拒绝，不改变线上协议。
不依赖 DNS、BR、网关、HA、cJSON 或存储组件。
基线为 ESPHome 2026.9.1 / NCS 2.9.2。

这是独立 fork，原 ESP-IDF 组件保持不变。相比原组件，组件、动作、条件名称统一
增加 `_nrf52` 后缀；配置字段、默认值和 `P2P1` 线上协议保持一致。
来源及源码快照哈希见 `upstream.json`。

## MTD 发送配置

在已有网络配置上使用 `device_type: MTD`。`poll_period: 0s` 或不配置时为持续接收的 MED；
正值为周期轮询 SED，额外的手动 keep_alive 只适用于 SED。
MTD 不填写本机 `address`、`port` 或 `on_receive`，使用 ML-EID
和临时 UDP 端口发送，仍会接收 ACK。

```yaml
external_components:
  - source:
      type: local
      path: components
    components: [openthread_nrf52, openthread_info_nrf52, thread_p2p_transporter_nrf52]

thread_p2p_transporter_nrf52:
  id: p2p_transport
  send_to_addr_list:
    - id: wall_switch
      address: "fdd1:dd68:9fa3:d942::1002"
      port: 54321
  on_send_success:
    then:
      - logger.log: "ACK received; application may now prepare sleep"
  on_send_fail:
    then:
      - logger.log: "ACK failed; application decides whether to sleep"
```

将以下动作放入现有按键的 `on_release.then`：

```yaml
- thread_p2p_transporter_nrf52.send:
    id: p2p_transport
    send_to: wall_switch
    send_flag: 0x12
    payload_type: string
    payload: "ask_sw_toggle"
    ack:
      timeout: 500ms
      resend: 2
```

工作区 `sensor.yaml` 保留以上发送动作及按键引脚 P0.24。
当前 `openthread.yaml` 为 MTD 配置，发送目标为 `::1002:54321`（地址与端口分开配置）。
调用前应确认 Thread 已附着、`id(p2p_transport).is_ready()` 为 true；未就绪时
不缓存业务消息等待入网。发送为非阻塞，后续动作立即继续。

## 接收端与地址

本组件现在仅提供 MTD 配置入口。此前本 fork 的 FTD 收发配置不再允许；
接收端可继续使用 ESP-IDF 原版 thread_p2p_transporter，不受本限制影响。
所有地址属于实际生效 Dataset 的 Mesh-Local /64，不能使用 OMR、RLOC、ALOC、
多播或保留 anycast 地址。本地和目标端口范围为 1..65535，默认 18520。
目标表最多 128 项，不作为接收来源白名单。

## 与 ESP32 互通

ESP32 端继续使用原 `thread_p2p_transporter`，不需要修改协议或组件实现。
双方需属于同一个 Thread 网络、使用相同 Mesh-Local 前缀，并配置匹配的目标
地址和监听端口。发送端的目标别名只在本机有效。

| 项目 | 两端保持一致 |
|---|---|
| 报文头 | `P2P1`，24 字节，字段位置和保留位不变 |
| 字节序 | session、message_id、长度及 float 位表示采用网络字节序 |
| bool | 类型 1，单字节 0/1 |
| float | 类型 2，有限 IEEE-754 float32；拒绝 NaN、Inf |
| string | 类型 3，UTF-8，按字节计算，最大 1208 字节 |
| 最大 UDP 报文 | 1232 字节，不做应用层分包；底层可发生链路分片 |
| ACK | kind=2、type=0、无负载，回显 session/message_id/send_flag |
| 匹配 | 校验 ACK 来源地址、端口、会话、编号及标志 |
| 去重 | 最近 128 条、最长 15min；重复报文补 ACK，不重复执行业务 |

ACK 表示报文通过接收检查，发送 ACK 的尝试发生在业务自动化之前，
不表示开关等业务动作已经完成。重传保持原始报文和消息编号。
接收端重启会丢失去重记录，不提供跨重启严格一次执行保证。

## 动作和资源

`send` 支持动作级 `on_send_success`、`on_send_fail`，必须同时配置 `ack`：

```yaml
- thread_p2p_transporter_nrf52.send:
    send_to: wall_switch
    send_flag: 0x12
    payload_type: string
    payload: "ask_sw_toggle"
    ack:
      timeout: 500ms
      resend: 2
    on_send_success:
      then:
        - deep_sleep_nrf52.enter: sleep_control
    on_send_fail:
      then:
        - logger.log: "This message failed"
```

休眠动作仅适用于已配置 deep_sleep_nrf52 的项目。发送动作仍为非阻塞，不等待回调结束。
每条待确认消息单独保存结果回调；同一 send 动作重复执行或多条消息并发时，
按匹配的消息分发，重传不新增回调，迟到或重复 ACK 不重复触发。
两个回调均提供本次消息的 `send_flag`（uint8_t）和 `message_id`（uint32_t），
支持标准 ESPHome 动作及 delay；不继承外层 GPIO 事件等自动化参数。
成功表示收到匹配 ACK；失败包括本地拒绝（未就绪、非法运行时参数、待确认队列满等）
及重试耗尽。首次 UDP 提交失败但仍在重试时，不提前报告最终失败。
本地拒绝的失败回调延后到主循环执行，避免在 send 调用栈中重入。
组件级结果回调继续保留，先于本次动作回调执行；若组件已关闭，则取消未交付的结果回调。

保留 `send`、`publish` 动作，`send_flag`、`payload` 条件，
以及 `on_receive`、`on_send_success`、`on_send_fail` 回调。
回调参数保持 `send_flag`、`message_id` 或类型化 `payload`；
延时自动化保留自己的消息上下文。`publish` 的 `sensor_id`、
`binary_sensor_id`、`text_sensor_id` 三选一。

`send` 支持 `send_to` 或 `address` 二选一，目标、端口、标志和负载支持 lambda。
`payload_type` 为 bool/float/string。省略 `ack` 时不触发发送结果回调；
配置 `ack` 后，timeout=10ms..60s（默认 500ms），resend=0..10（默认 2），
最多 8 条待确认消息。接收队列容量 16 包，满时释放报文并计入丢包。

Zephyr 使用 `k_msgq` 保存报文指针，OT 回调只检查长度、复制及非阻塞入队；
解码、ACK 匹配和自动化在 ESPHome loop 执行。启动会话使用平台随机字节生成。
关闭时拒绝新业务消息，在 OT 锁保护下关闭 socket、移除自有地址，再释放队列；
锁忙时通过 teardown 重试。关闭取消待确认消息，不触发新的结果回调。

等待 ACK 时应用应保持运行，SED 的轮询周期应与 ACK 超时匹配。
组件不自动调整 `poll_period`，也不自动阻止或管理 MCU 休眠。

## 已验证范围

当前工作区执行 `python tools/validate_p2p_send_callbacks.py`：检查三种负载、
单独/同时配置动作级成功失败回调、延时回调参数、回调中发送及请求休眠，
拒绝缺少 ACK 的动作回调，并检查原有发送写法的完整项目内存代码生成。
该检查不执行 C++ 运行时、固件编译或无线收发；并发结果分发仍需用户上板验证。

`python tests/validate_p2p.py`（在本项目根目录）检查：

- ESP32 与 nRF52 的编解码、接收 ACK/去重及自动化源码一致性。
- 7 个固定报文向量及最大长度，供后续两端抓包核对。
- 15 项配置/内存代码生成用例，包括 MTD、SED、FTD、TLV、三种负载、
  接收延时上下文、条件、发布、回调内发送和错误配置。

未运行编译，也未上板。固定向量检查不等于已执行 C++ 编解码或验证无线互通。
ESP32 ↔ nRF52840 的实际双向收发、ACK 丢失重传、SED 轮询、队满和重连
需要用户选择编译后继续上板验证。
