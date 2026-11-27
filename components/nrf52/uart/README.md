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


## nRF52840 asynchronous EasyDMA backend

nRF52 UART uses Zephyr's async API for both RX and TX. RX supplies two persistent 256-byte RAM buffers and reuses each only after `UART_RX_BUF_RELEASED`; it copies only the reported `UART_RX_RDY` slice into the software ring. NCS 2.9.2 enhanced RX handles idle-tail delivery (5ms timeout) without relying on a software byte counter for every incoming byte. No dedicated hardware counting TIMER is reserved.

`rx_buffer_size` defaults to **1024 bytes** on nRF52 (an explicit configured size is honored). The DMA buffers are separate from this ring. Larger buffers absorb bursts but cannot compensate for indefinitely blocked consumers. Software overflow and hardware RX stop/error counters remain visible; they are not suppressed.

TX copies data into a persistent 256-byte RAM staging buffer, waits for DONE/ABORTED per chunk, and prevents reuse until the driver releases it. Flash-backed and transient caller buffers are not given directly to DMA. `write_array` remains synchronous and `flush` reports its last result; timeout abort is bounded, and an unconfirmed abort fails the UART rather than reusing live DMA storage. Dynamic settings stop RX before applying the new format. Standard rates and the custom 256000 divider remain supported.

When `deep_sleep_nrf52` is configured, generated hooks stop RX after user sleep actions and confirm DMA release before peripheral PM. A failed stop cancels sleep. Wake/abort hooks restore RX before user automations, and retain the configured divider. No new YAML fields are needed. The async backend requires the matching updated `deep_sleep_nrf52` component when used together; do not mix this UART update with an older sleep component lacking the pre-PM hook.

Verification covers configuration and in-memory code generation only. Continuous LD2410 traffic at 115200/230400/256000, command responses, error recovery and repeated sleep/wake still require hardware testing.
