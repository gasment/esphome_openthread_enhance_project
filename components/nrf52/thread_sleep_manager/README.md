# thread_sleep_manager

`thread_sleep_manager` centralizes nRF52 System ON wake-cycle decisions for
OpenThread nodes. It does not replace `deep_sleep_nrf52`: wake sources, sleep
duration, GPIO SENSE, VBUS wake and peripheral suspension remain owned by that
component.

## Dependencies

Required:

- `deep_sleep_nrf52`, bound with `deep_sleep_id`
- `openthread_nrf52`, bound with `openthread_id`

Optional activity sources:

- one `thread_proxy_client_nrf52` via `proxy_client_id`
- one `thread_p2p_transporter_nrf52` via `p2p_transporter_id`

The manager waits for all configured transports to become idle before allowing
sleep. OpenThread joiner activity freezes reconnect watchdog timing.

## Configuration

```yaml
thread_sleep_manager:
  id: sleep_manager
  deep_sleep_id: sleep_control
  openthread_id: openthread_main
  proxy_client_id: sed_api
  # p2p_transporter_id: p2p_transport

  binary_sensor_input:
    - key
    - auxiliary_key
  indicator_output_id: indicator_led

  wake_timeout: 45s
  reconnect_after: 10s
  reconnect_timeout: 20s
  quiet_delay: 200ms

  activity_blink_duration: 120ms
  success_duration: 500ms
  error_blinks: 3
  error_blink_interval: 200ms
  joiner_blink_interval: 500ms
```

`binary_sensor_input` accepts multiple IDs. The logical OR of their states is
used: any ON holds the LED on, and all OFF release it.

LED visual priority:

```text
binary_sensor_input ON
  > joiner
  > error
  > activity
  > success
  > OFF
```

The input override is above the automated patterns so that a held input always
produces a solid LED. Automated patterns are non-blocking.

## State behavior

- A wake starts a bounded wake cycle.
- Transport activity blocks sleep.
- Send success starts the success indication and then permits sleep after the
  quiet and LED hold periods.
- Send failure starts the error indication. If a transport remains unavailable,
  the manager waits `reconnect_after`, requests an OpenThread reattach, and
  applies a bounded `reconnect_timeout`.
- Joiner activity suspends reconnect counting and sleep.
- `KEEP_AWAKE` GPIO/VBUS policy, external wake and peripheral PM remain in
  `deep_sleep_nrf52`.

Do not use manual `deep_sleep_nrf52.prevent` transitions in managed mode. A
user-set prevent value remains an external veto; the manager does not override
it.
