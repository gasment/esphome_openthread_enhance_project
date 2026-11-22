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

## Timing and indicator parameters

### Wake-cycle and reconnect deadlines

| Parameter | Definition |
|---|---|
| `wake_timeout` | Hard upper bound for one cold-boot or wake cycle. The manager forces the phase to `READY` after this duration so a missing callback or forgotten send cannot leave the node awake forever. Active transport operations still keep `can_sleep` false after the timeout. |
| `reconnect_after` | Reconnect grace period. When there is no active operation and OpenThread or a bound transport is unavailable, the manager waits this long before requesting `openthread_nrf52.reattach()`. Joiner activity freezes the timer. |
| `reconnect_timeout` | Additional window after reattach has been requested. If the link recovers sooner, normal activity processing resumes; otherwise the manager forces `READY` at this deadline. Joiner activity freezes this timer too. |

With the defaults, the worst reconnect path is approximately
`reconnect_after + reconnect_timeout = 30s`, earlier than `wake_timeout = 45s`.

### Send completion and indicator timing

| Parameter | Definition |
|---|---|
| `quiet_delay` | Minimum quiet period after the last transport activity completes. It gives immediately following events and callbacks a chance to run before the cycle becomes `READY`. |
| `activity_blink_duration` | One activity pulse at the start of a transport operation. It does not keep the LED on for the whole send and does not define the end of the operation. |
| `success_duration` | Duration of the success indication after a successful transport completion. Sleep is also held for this period. |
| `error_blinks` | Number of error LED blink cycles. |
| `error_blink_interval` | Duration of one half-period of the error pattern. One complete ON+OFF cycle takes `2 × error_blink_interval`. |
| `joiner_blink_interval` | Duration of one half-period of the joiner pattern. One complete ON+OFF cycle takes `2 × joiner_blink_interval`. This only controls the visual pattern; OpenThread joiner state itself blocks sleep and freezes reconnect timing. |

The main timing relationships are:

```text
success hold = max(quiet_delay, success_duration)
error hold   = error_blinks × error_blink_interval × 2
joiner cycle = joiner_blink_interval × 2
```

Examples with the defaults:

```text
success hold = max(200ms, 500ms) = 500ms
error hold   = 3 × 200ms × 2 = 1200ms
joiner LED   = 1000ms ON + 1000ms OFF when joiner_blink_interval is 1000ms
```

The error pattern for `error_blinks: 3` and
`error_blink_interval: 200ms` is:

```text
ON 200ms -> OFF 200ms -> ON 200ms -> OFF 200ms -> ON 200ms -> OFF 200ms
```

An ACK followed by the default success indication therefore enters `READY`
about 500ms after the ACK. The actual System ON entry may be tens of
milliseconds later while the DeepSleep guard closes transport work and runs
the sleep preparation.

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

## YAML conditions

The manager exposes its state as conditions without creating extra entities:

```yaml
- if:
    condition:
      thread_sleep_manager.is_ready: sleep_manager
    then:
      - logger.log: "sleep cycle is ready"
```

Available conditions:

```text
thread_sleep_manager.is_awake
thread_sleep_manager.is_activity
thread_sleep_manager.is_wait_reconnect
thread_sleep_manager.is_reattaching
thread_sleep_manager.is_ready
thread_sleep_manager.is_sleeping
thread_sleep_manager.link_ready
thread_sleep_manager.transport_busy
thread_sleep_manager.joiner_active
thread_sleep_manager.can_sleep
```

## Sleep state machine

The manager runs one bounded wake cycle at a time. A new wake or cold boot
starts in `AWAKE`; `wake_timeout` prevents an unknown callback or missing send
from keeping the device awake forever.

| State | Entered when | Meaning and behavior | Normal exits |
|---|---|---|---|
| `AWAKE` | The manager starts, a wake occurs, the link recovers, or a sleep request is aborted | The wake cycle is active, but no transport operation is currently running. Timeout and LED hold timers continue. Sleep is not yet requested. | `ACTIVITY` when a transport starts, `WAIT_RECONNECT` when the link is unavailable, or `READY` after a completed activity and the quiet/LED hold periods |
| `ACTIVITY` | A bound Proxy or P2P transport starts an operation | At least one send or ACK operation is active. Activity blocks reattach and sleep; `active_operations` may be greater than one. | `AWAKE` when the operation finishes and the link is usable, `WAIT_RECONNECT` when it finishes while the link is unavailable, or `READY` when the hard wake timeout expires |
| `WAIT_RECONNECT` | There is no active operation and OpenThread or a bound transport is unavailable | The reconnect grace period starts. `reconnect_after` controls how long the manager waits before calling `openthread_nrf52.reattach`; joiner activity freezes this timer. Sleep is blocked. | `AWAKE` if the link recovers, `ACTIVITY` if a new operation starts, or `REATTACHING` after `reconnect_after` |
| `REATTACHING` | The reconnect delay expires | OpenThread reattach has been requested. The manager waits for the link to recover or for `reconnect_timeout`. Joiner activity restarts and freezes this timer. | `AWAKE` if the link recovers, `ACTIVITY` for a new operation, or `READY` when the reconnect timeout expires |
| `READY` | All activity is complete and the quiet/success/error display periods have finished, or a wake/reconnect deadline expires | The manager no longer considers the wake cycle active. It requests System ON sleep when `can_sleep` is true. Transport queues, joiner state and external DeepSleep vetoes are checked again by the sleep guard. | `ACTIVITY` if a new send starts, `AWAKE` after a sleep abort, or `SLEEPING` when DeepSleep actually enters System ON sleep |
| `SLEEPING` | `deep_sleep_nrf52` reports that System ON sleep has started | The manager turns the indicator off and the ESPHome loop is suspended. This state mainly documents the period until the next wake callback. | `AWAKE` on the next wake event |

The following conditions inspect these values without creating an entity:

| Condition | Meaning |
|---|---|
| `is_awake` | `phase == AWAKE` |
| `is_activity` | `phase == ACTIVITY` |
| `is_wait_reconnect` | `phase == WAIT_RECONNECT` |
| `is_reattaching` | `phase == REATTACHING` |
| `is_ready` | `phase == READY` |
| `is_sleeping` | `phase == SLEEPING` |
| `link_ready` | OpenThread is connected and every configured transport reports a usable link |
| `transport_busy` | At least one configured transport still has a batch, pending ACK or queued transport work |
| `joiner_active` | OpenThread joiner is manually active; this blocks sleep and freezes reconnect timing |
| `can_sleep` | The complete sleep gate: `READY`, no active operations, no transport work, no joiner and no remaining LED hold |

Important transitions and override rules:

- A successful send starts the success indication. The manager enters `READY`
  after both `quiet_delay` and `success_duration` have elapsed.
- A failed send starts the error indication. If the link remains usable, the
  failure is treated as a terminal operation and the manager eventually enters
  `READY`; if the link is unavailable, it enters `WAIT_RECONNECT`.
- Any new transport operation moves the state back to `ACTIVITY`, even if the
  manager had already reached `READY`.
- Joiner activity overrides normal reconnect counting. The reconnect grace
  period restarts after joiner activity ends.
- A sleep abort returns the manager to `AWAKE` and starts a fresh wake deadline;
  it does not report a successful sleep cycle.
- `KEEP_AWAKE` GPIO/VBUS policy, external wake sources and peripheral suspension
  remain owned by `deep_sleep_nrf52`.

Do not use manual `deep_sleep_nrf52.prevent` transitions in managed mode. A
user-set prevent value remains an external veto; the manager does not override
it.
