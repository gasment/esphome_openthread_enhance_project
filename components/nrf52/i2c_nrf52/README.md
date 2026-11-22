# i2c_nrf52

nRF52840/Zephyr 独立双 I2C 后端。它不启用 ESPHome 官方顶层 `i2c:` 组件，
但复用官方 `I2CBus` C++ 接口，因此现有 I2C 设备仍通过 `i2c_id` 绑定。

本域保留为旧配置兼容入口。接替官方生态位时直接加载 `i2c` 组件并使用
官方字段 `i2c:` / `i2c_id`；该组件自身已包含完整 nRF52 实现，不再依赖
本后缀域。详细配置见 `../i2c/README.md`。

## 配置

```yaml
external_components:
  - source:
      type: local
      path: esphome_openthread_enhance_project/components
    components: [i2c_nrf52, zephyr_board_function]

nrf52:
  board: e73_or_nd05/nrf52840
  bootloader: mcuboot

zephyr_board_function:
  swd_rtt_logs: true
  e73_or_nd05_board_config: true

i2c_nrf52:
  - id: busa
    hardware_i2c: I2C0
    sda: P0.12
    scl: P0.11
    scan: true

  - id: busb
    hardware_i2c: I2C1
    sda: P0.13
    scl: P0.14
    scan: true
```

设备继续使用通用 `i2c_id`：

```yaml
sensor:
  - platform: ...
    i2c_id: busb
    address: 0x76
```

总线对象复用官方 `I2CBus`/`ZephyrI2CBus` C++ 接口，因此 `i2c_id` 的类型是兼容的。
Python 侧声明 `DEPENDENCIES = ["i2c"]` 的平台不需要修改源码。同时加载同源
`i2c` 兼容域后，顶层的 `i2c:` 会覆盖官方内置组件并转发到本实现。
不能同时配置两个独立的 `i2c` / `i2c_nrf52` 总线实例。

## 规则

- `hardware_i2c` 必须显式指定为 `I2C0` 或 `I2C1`。
- 最多配置两条总线，同一控制器不能重复。
- 所有 SDA/SCL 引脚必须唯一。
- `i2c:` 与 `i2c_nrf52:` 不能同时存在。
- 每个配置生成独立的 `i2c0`/`i2c1` pinctrl 和状态节点。
- 休眠 pinctrl 使用 `low-power-enable`，可由 peripheral PM 管理。

## 硬件冲突

nRF52840 内部存在两组共享寄存器：

```text
I2C0 <-> SPI0
I2C1 <-> SPI1
```

本组件会对应拒绝同时启用 `I2C0/SPI0` 或 `I2C1/SPI1`。

当前 ESPHome Zephyr 官方 SPI 后端实际使用独立的 `SPIM2`，因此：

- `I2C0 + SPI2` 允许
- `I2C1 + SPI2` 允许
- `I2C0 + I2C1 + SPI2` 允许

UART0/UART1 使用独立硬件，与 I2C0/I2C1 没有内部寄存器冲突；仍需避免实际
GPIO 被两个组件重复使用。
