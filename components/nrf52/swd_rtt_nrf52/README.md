# SWD + RTT 日志

同一组 SWD 接线完成烧写和日志读取，无需 UART TX/RX 或目标 USB CDC。
适配当前 ESPHome 2026.9.1 / NCS 2.9.2，不修改安装的 ESPHome 源码。

## 配置与原理

参见根目录 `swd_rtt_loger.yaml`。要求 `logger.baud_rate: 0`，
`task_log_buffer_size` 大于 0（默认 768），不能同时加载会强制阻塞 RTT 的 `debug`。
RTT 不启用、禁用或重映射任何 UART，外设 UART 的状态和引脚由 UART 组件管理。

ESPHome 在 baud_rate=0 时也跳过 write_msg_()，仅启用 PRINTK 不会输出 ESPHome
日志。本组件通过 logger 的公开回调，将格式化日志经 printk 送入 Zephyr 日志队列，
再由 RTT channel 0 输出。工作线程日志经 ESPHome 任务缓冲在主循环转发。
日志级别、过滤和用户 on_message 保持有效。
ESPHome 2026.9.1 在 baud_rate=0 时仍检查物理日志设备，产生 USB_CDC/UARTx
not ready 误报。RTT 桥接仅把这条已禁用物理日志端口的特定误报替换为
`Physical logger port disabled; RTT active`。真实 UART 组件错误及其他日志保留；
用户 on_message 仍会收到 logger 的原始消息，安装的 logger 源码未修改。

## 使用

DAPLink attach 到 WSL 后，在项目目录执行：

```bash
export ESPHOME_SDK_NRF_PREFIX=/home/esphome/sdk-nrf
python esphome_nrf52.py run config.yaml --device pyocd
python esphome_nrf52.py logs config.yaml --device pyocd
```

run 沿用 ESPHome 的编译、烧写流程，成功后接入 RTT；logs 只读取日志。
支持 pyocd / PYOCD / rtt 设备别名；Ctrl+C 退出；--no-logs 保持有效。
脚本只在当前进程挂接命令路由，不安装源码补丁。
兼容 ESPHome 2026.9.x 的无参 get_build_env 和 2026.10.0 的 ccache 参数接口。
RTT 仅取 SDK 环境，向新版传 ccache=None，不解析或安装编译缓存工具。
固件设置 LOG_PROCESS_TRIGGER_THRESHOLD=1，有日志即唤醒 Zephyr 日志线程，避免默认
攒 10 条或等待约 1 秒的延迟；保留 deferred 和非阻塞 RTT，不增加空闲轮询。
脚本在 POSIX/WSL 下保存终端设置，Ctrl+C 先让 pyOCD 正常退出，超时才终止进程，
最后兜底恢复终端，避免 ECHO/ICANON 被遗留为关闭。若旧会话已造成不回显，可盲输
`stty sane` 回车恢复。脚本修复立即生效，固件日志阈值需用户重新编译刷入后生效。
脚本给 RTT 输出的每一行加上主机本地接收时间 `[HH:MM:SS.mmm]`，包括多行配置的
续行和 pyOCD 诊断。它不是设备产生日志的时刻；迟连接时读出的缓冲日志也使用接收时间。
Zephyr 自带的启动时长保留。此功能只需更新脚本，无需重新编译固件。

默认 attach，不主动复位或停核。需要重看启动过程时可显式执行：

```bash
python esphome_nrf52.py logs config.yaml --device pyocd --reset
```

--reset 先复位再启动读取，缓冲有限，不能保证完整获取最早日志。
不要因暂时没有新日志而自动复位，应用可能只是没有日志产生。

直接读取命令：

```bash
/home/esphome/sdk-nrf/penvs/v2.9.2/bin/pyocd rtt -t nrf52840 -M attach -a 0x20000000 -s 0x40000
```

## 验证和限制

启动后应看到 `[swd_rtt_nrf52] ESPHome log bridge ready`，随后是 ESPHome 配置和
实体日志。桥接安装前 logger 的 pre_setup 日志不经回调转发；Zephyr 启动日志独立于
桥接。MCUboot 是独立镜像，本组件没有为它配置 RTT。
桥接安装后还会以 INFO 级别打印 LFCLK 运行状态、时钟源及 LFCLKSTAT/LFCLKSRC 原始值。
状态来自启动时只读硬件寄存器快照，区分内部 RC、外部晶振、外部时钟输入和 HFCLK
派生时钟；不启动、停止或切换时钟。后续时钟源若发生切换，此启动快照不会自动更新。

常规日志非阻塞，未连接或缓冲满时允许丢弃；panic 路径需硬件验证。
刷写前退出已有 RTT 会话，避免两个进程占用同一探针；测低功耗时断开调试会话。
接线需要 SWDIO、SWCLK、GND 及探针要求的参考电压，建议保留 RESET。

仅完成配置和内存代码生成验证，固件编译和硬件验证由用户执行。
