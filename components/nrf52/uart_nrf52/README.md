# uart_nrf52：nRF52840 / Zephyr 外部 UART

基于当前 ESPHome 2026.9.1 的 UART 组件独立复制，新增 nRF52840 Zephyr 后端。
使用独立的 `uart_nrf52` 组件域及 `esphome::uart_nrf52` 命名空间，
类名保留 `UARTComponent` / `UARTDevice`。总线引用字段为 `uart_nrf52_id`，
发送动作为 `uart_nrf52.write`，调试配置为总线下的 `debug`。
没有修改安装的 ESPHome 源码；
原始文件校验值保存在 `upstream.json`。升级 ESPHome 后需检查公共接口兼容性。

当前配置见 `nrf52840-wireless-bt-mtd/io_bus.yaml`，已由 `config.yaml` 引用。
外部组件加载列表需写 `components: [uart_nrf52]`。

```yaml
uart_nrf52:
  - id: uart_bus_0
    hardware_uart: UART0
    tx_pin: P0.06
    rx_pin: P0.08
    baud_rate: 115200
    data_bits: 8
    parity: NONE
    stop_bits: 1
    rx_buffer_size: 256
  - id: uart_bus_1
    hardware_uart: UART1
    tx_pin: P1.13
    rx_pin: P0.02
    baud_rate: 256000
```

原来的 `uart:`、`uart.write` 和 `uart_id` 分别改为 `uart_nrf52:`、
`uart_nrf52.write` 和 `uart_nrf52_id`。内置 button、switch、event、
packet_transport 子平台使用 `platform: uart_nrf52`。
Python 外设适配导入 `esphome.components.uart_nrf52`，调用
`register_uart_nrf52_device()`，继承 `UARTDevice` 并使用 `UART_DEVICE_SCHEMA`；
C++ 引入 `esphome/components/uart_nrf52/uart_nrf52.h`，使用
`uart_nrf52::UARTDevice` 和 `set_uart_nrf52_parent()`。
标准 UART 外设依赖原 `uart` 模块及类型，需要适配后才能接入本组件。
硬件端口名 `UART0/UART1`、Zephyr 驱动接口及 Kconfig 名称保持不变。

`hardware_uart` 可省略：先保留显式指定的端口，再按配置顺序分配空闲 UART0/UART1。
最多两路；不允许重复分配。TX/RX 至少指定一个，支持单向串口。
组件生成 pinctrl 和设备树；引脚沿用 ESPHome 的冲突检查，不额外需要手写 overlay。
RX 默认弱上拉以保持 UART 空闲高电平，支持显式下拉覆盖；不支持反相、开漏和硬件流控。

nRF52840 支持 8 数据位、NONE/EVEN 校验、1/2 停止位，本后端接受 1200–1000000
波特率。NCS 2.9.2 legacy nrfx 的 uart_configure 只接受固定速率表，没有 256000。
表内速率沿用驱动；非标准速率先通过驱动配置帧格式，再通过 Nordic HAL 设置实际
BAUDRATE 寄存器，保持请求速率。256000 对应 0x04189000，按 nRF52840 的 16MHz
时钟及分频量化计算；双方速率和格式需一致。
本适配针对 nRF52840 legacy nrfx 中断后端；驱动 uart_config_get 对自定义速率仍返回
配置阶段的占位值，组件日志使用实际请求值。未提供通用异步驱动或自动 runtime PM 策略。
量化公式参考 [Nordic 技术讨论](https://devzone.nordicsemi.com/f/nordic-q-a/101609/custom-uart-baud-rate-117600/435259)。

收发使用 NCS nrfx 的中断驱动接口。接收数据存入可配置环形缓冲；缓冲满时丢弃新数据
并在主循环告警，UART 接收错误也在主循环报告。告警包含端口和错误说明，每个端口
每秒最多汇总一次，避免异常输入刷屏。读取等待沿用 ESPHome 的 100ms 上限，
单次 read_array 长度不能超过接收缓冲。接收调试回调在实际读取时触发。
发送分块复制到驱动 DMA 缓冲，在中断推进，调用者通过信号量等待发送完成；
超时为预计线上时间加 100ms，flush 返回最近一次发送结果。ISR 中不接受写入。
debug 的 TX 回调仅对成功发送的消息触发。

RTT 与 UART 外设独立：RTT 只调整 RTT/日志后端，不修改 UART 节点。
logger.baud_rate=0 时 logger 不占物理 UART；若 logger 使用 UART0/UART1，配置阶段
会拒绝外设重复使用该端口。UART power transitions are managed centrally by deep_sleep_nrf52.peripheral_auto_mgr.

验证方式（只验证配置和内存代码生成）：

```bash
python tools/validate_uart_nrf52.py
```

没有执行编译或硬件测试。需要用户验证两路 TX/RX 回环、目标外设协议，以及 Thread
并发负载下的连续接收。当前使用 nrfx interrupt-driven 接收路径，不能承诺高负载下
最高速率无丢字节；增大软件缓冲不能解决硬件接收服务不及时造成的丢字节。

## System ON 统一硬件管理

UART 不再使用 deep_sleep_id，不向休眠组件注册生命周期回调。
由 deep_sleep_nrf52 的 peripheral_auto_mgr 自动发现硬件 UART/UARTE 控制器并调用
Zephyr 驱动 PM 接口。正常发送期间使用标准 pm_device_busy 标志；暂停状态拒绝新发送。
总线软件 RX 缓冲不被清空；是否已完成业务收发由应用判断。

UART/UARTE 暂停后不接收数据。使用独立 GPIO 中断脚唤醒，并在 on_wake 恢复业务通信。
统一管理器在应用 on_wake 前恢复接口，并保留实际 BAUDRATE，包括 HAL 设置的 256000。
外接模块的供电、业务休眠指令，以及睡眠期间到来的串口数据不属于接口管理器的保证范围。

配置与验证见 deep_sleep_nrf52/README.md 和 tools/validate_peripheral_auto_mgr.py。
