# uart compatibility layer

This component directly occupies the official `uart` ecosystem slot for
nRF52/Zephyr. No suffixed companion component is required.

Enable the component:

```yaml
external_components:
  - source:
      type: local
      path: components/nrf52
    components:
      - uart
```

Use the official configuration domain and reference field:

```yaml
uart:
  id: uart_bus
  tx_pin: P0.06
  rx_pin: P0.08
  baud_rate: 9600

sensor:
  - platform: pmsx003
    uart_id: uart_bus
    type: PMSX003
    pm_2_5:
      name: PM2.5
```

Third-party components that declare `DEPENDENCIES = ["uart"]`, import
`esphome.components.uart`, use `UART_DEVICE_SCHEMA`,
`register_uart_device()`, inherit `uart::UARTDevice`, and reference `uart_id`
work without source changes.

The Python classes and C++ implementation are defined directly in
`esphome::uart`. The external `uart` is expected to appear in ESPHome's
"overriding built-in components" list.

