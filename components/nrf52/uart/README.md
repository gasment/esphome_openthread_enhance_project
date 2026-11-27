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



## UART device validation

New device components should use the standard Python final-validation interface:

```python
from esphome.components import uart

FINAL_VALIDATE_SCHEMA = uart.final_validate_device_schema(
    "my_device", baud_rate=9600, require_tx=True, require_rx=True,
    data_bits=8, parity="NONE", stop_bits=1,
)
```

The requirements above are an example; use the actual device parameters. The external nRF52 UART implements this API under `uart`, including Zephyr UART pin validation. Current LD2410 already uses this interface. Do not call C++ `check_uart_settings()` or `check_uart_nrf52_settings()` in new components.

Both old C++ names remain deprecated compatibility entry points. They now share a non-deprecated internal check, so including the compatibility header no longer emits an internal deprecated-call warning; actual third-party calls to the legacy API still receive migration guidance. No YAML or serial I/O behavior changes are required.
