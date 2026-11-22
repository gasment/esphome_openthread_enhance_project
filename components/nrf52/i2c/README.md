# i2c compatibility layer

This component directly occupies the official `i2c` ecosystem slot for
nRF52/Zephyr. No suffixed companion component is required.

Enable the component:

```yaml
external_components:
  - source:
      type: local
      path: components/nrf52
    components:
      - i2c
```

Then configure the official domain and field names:

```yaml
i2c:
  id: sensor_bus
  sda: P0.02
  scl: P0.03
  scan: true
```

`hardware_i2c: I2C0|I2C1` is optional for compatibility and defaults to
`I2C0`. Specify it explicitly when using a second bus.

Third-party components that declare `DEPENDENCIES = ["i2c"]`, call
`i2c.i2c_device_schema()`, inherit `i2c::I2CDevice`, and use `i2c_id` work
without source changes. The external `i2c` is expected to appear in ESPHome's
"overriding built-in components" list.

