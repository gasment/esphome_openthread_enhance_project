# thread\_sleep\_manager

`thread_sleep_manager` 集中管理 OpenThread 节点的 nRF52 System ON 唤醒周期决策。它不替代 `deep_sleep_nrf52`：唤醒源、睡眠时长、GPIO SENSE、VBUS 唤醒和外设挂起仍由该组件负责。

## 依赖项

必需：

*   `deep_sleep_nrf52`，通过 `deep_sleep_id 绑定`
*   `openthread_nrf52`，通过 `openthread_id 绑定`

可选活动源：

*   一个 `thread_proxy_client_nrf52`，通过 `proxy_client_id 绑定`
*   一个 `thread_p2p_transporter_nrf52`，通过 `p2p_transporter_id`

管理器会等待所有已配置的传输器进入空闲状态后才允许休眠。OpenThread joiner 活动会冻结重连看门狗计时。

## 配置

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

## 时序与指示器参数

### 唤醒周期与重连截止时间

| 参数 | 定义 |
| --- | --- |
| wake\_timeout | 单次冷启动或唤醒周期的硬性上限。超过此时长后，管理器会强制将阶段切换为 READY，以免因回调缺失或发送被遗忘而导致节点永久保持唤醒状态。超时后，活动的传输操作仍会使 can\_sleep 保持为 false。 |
| reconnect\_after | 重连宽限期。当没有活动操作且 OpenThread 或绑定的传输不可用时，管理器会等待此时长，然后请求 openthread\_nrf52.reattach()。Joiner 活动会冻结该计时器。 |
| reconnect\_timeout | 已请求在重新附加后增加额外窗口。如果链路更快恢复，则恢复正常活动处理；否则管理器在此截止时间强制进入 READY。加入者活动也会冻结此计时器。 |

在默认配置下，最坏的重连路径大约为 `reconnect_after + reconnect_timeout = 30s`，早于 `wake_timeout = 45s`。

### 发送完成与指示器时序

| 参数 | 定义 |
| --- | --- |
| quiet\_delay | 最后一次传输活动完成后的最小静默期。它让紧随其后的事件和回调有机会在周期进入 READY 之前运行。 |
| activity\_blink\_duration | 传输操作开始时的一次活动脉冲。它不会在整个发送过程中保持 LED 点亮，也不定义操作的结束。 |
| success\_duration | 传输成功完成后成功指示的持续时间。在此期间也会保持休眠。 |
| error\_blinks | 错误 LED 闪烁循环的次数。 |
| error\_blink\_interval | 错误模式一个半周期的持续时间。一个完整的亮+灭循环耗时 2 × error\_blink\_interval。 |
| joiner\_blink\_interval | joiner 模式半个周期的持续时间。一个完整的 ON+OFF 周期需要 2 × joiner\_blink\_interval。这仅控制视觉模式；OpenThread joiner 状态本身会阻止睡眠并冻结重连时序。 |

主要的时序关系如下：

```text
success hold = max(quiet_delay, success_duration)
error hold   = error_blinks × error_blink_interval × 2
joiner cycle = joiner_blink_interval × 2
```

使用默认值的示例：

```text
success hold = max(200ms, 500ms) = 500ms
error hold   = 3 × 200ms × 2 = 1200ms
当 joiner_blink_interval 为 1000ms 时，joiner LED = 亮 1000ms + 灭 1000ms
```

`error_blinks: 3` 和 `error_blink_interval: 200ms` 的错误闪烁模式为：

```text
亮 200ms -> 灭 200ms -> 亮 200ms -> 灭 200ms -> 亮 200ms -> 灭 200ms
```

因此，在收到 ACK 并随后给出默认成功指示后，会在 ACK 之后约 500ms 进入 `READY` 实际进入 System ON 可能还会晚几十 毫秒，在此期间 DeepSleep 保护逻辑会结束传输工作并执行 睡眠准备工作。

`binary_sensor_input` 接受多个 ID。使用它们状态的逻辑或：任一为 ON 即保持 LED 点亮，全部为 OFF 则释放。

LED 视觉优先级：

```text
binary_sensor_input 开启
> 连接符
> 错误
> 活动
> 成功
> 关闭
```

输入覆盖位于自动模式之上，因此保持输入时始终产生常亮 LED。自动模式是非阻塞的。

## YAML 条件

管理器以条件的形式暴露其状态，而不会创建额外的实体：

```yaml
- if:
condition:
thread_sleep_manager.is_ready: sleep_manager
then:
- logger.log: "sleep cycle is ready"
```

可用条件：

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

## 睡眠状态机

管理器一次只运行一个有界的唤醒周期。新的唤醒或冷启动从 `AWAKE` 开始；`wake_timeout` 可防止未知回调或缺失发送导致设备永久保持唤醒状态。

| 状态 | 进入条件 | 含义与行为 | 正常退出 |
| --- | --- | --- | --- |
| AWAKE | 管理器启动、发生唤醒、链路恢复或睡眠请求被中止 | 唤醒周期处于活动状态，但当前没有传输操作正在运行。超时和 LED 保持定时器继续运行。尚未请求休眠。 | ACTIVITY当传输启动时为 WAIT\_RECONNECT，当链路不可用时为 READY，或在活动完成且静默/LED 保持期结束后为 |
| ACTIVITY | 绑定的 Proxy 或 P2P 传输启动一个操作 | 至少有一个发送或 ACK 操作处于活动状态。活动会阻止重新连接和休眠；active\_operations 可能大于一。 | AWAKE当操作完成且链路可用时为 WAIT\_RECONNECT，当操作完成但链路不可用时为 READY，或当硬唤醒超时到期时为 |
| WAIT\_RECONNECT | 没有活动操作，且 OpenThread 或绑定的传输不可用 | 重新连接宽限期开始。reconnect\_after 控制管理器在调用 openthread\_nrf52.reattach 之前等待多长时间；joiner 活动会冻结此定时器。休眠被阻止。 | 如果链路恢复则为 AWAKE，如果新操作启动则为 ACTIVITY，或在 reconnect\_after 之后为 REATTACHING |
| REATTACHING | 重连延迟到期 | 已请求 OpenThread 重新连接。管理器等待链路恢复或等待 reconnect\_timeout 超时。Joiner 活动会重新启动并冻结此计时器。 | AWAKE如果链路恢复则为 ACTIVITY，如果有新操作则为 READY，如果重连超时到期则为 |
| READY | 所有活动已完成，且静默/成功/错误显示时段均已结束，或者唤醒/重连截止时间到期 | 管理器不再认为唤醒周期处于活动状态。当 can\_sleep 为 true 时，它请求 System ON 睡眠。睡眠保护机制会再次检查传输队列、Joiner 状态和外部 DeepSleep 否决。 | ACTIVITY如果开始新的发送则为 AWAKE，如果睡眠中止后则为 SLEEPING，如果 DeepSleep 实际进入 System ON 睡眠则为 |
| SLEEPING | deep\_sleep\_nrf52 报告 System ON 睡眠已开始 | 管理器关闭指示灯，ESPHome 循环被挂起。此状态主要用于记录直到下一次唤醒回调之前的时段。 | AWAKE 在下一个唤醒事件时 |

以下条件会检查这些值，但不会创建实体：

| 条件 | 含义 |
| --- | --- |
| is\_awake | phase == AWAKE |
| is\_activity | phase == ACTIVITY |
| is\_wait\_reconnect | phase == WAIT\_RECONNECT |
| is\_reattaching | phase == REATTACHING |
| is\_ready | phase == READY |
| is\_sleeping | phase == SLEEPING |
| link\_ready | OpenThread 已连接，且每个已配置的传输通道都报告链路可用 |
| transport\_busy | 至少有一个已配置的传输通道仍有批次、待处理的 ACK 或排队的传输工作 |
| joiner\_active | OpenThread joiner 处于手动激活状态；这会阻止休眠并冻结重连计时 |
| can\_sleep | 完整的休眠门控条件：READY、无活动操作、无传输工作、无 joiner 且无剩余的 LED 保持 |

重要的状态转换与覆盖规则：

*   发送成功后开始成功指示。管理器进入 `READY` 在 `quiet_delay` 和 `success_duration` 均已经过之后。
*   发送失败会启动错误指示。如果链路仍然可用，则该失败被视为终止性操作，管理器最终进入 `READY` 状态；如果链路不可用，则进入 `WAIT_RECONNECT` 状态。 任何新的传输操作都会将状态移回 <code>ACTIVITY</code>，即使管理器已经达到 <code>READY</code> 状态。
*   加入者活动会覆盖正常的重连计数。重连宽限期在加入者活动结束后重新开始。
*   加入者活动会覆盖正常的重连计数。加入者活动结束后，重连宽限期会重新开始计时。
*   睡眠中止会将管理器返回到 `AWAKE` 并启动新的唤醒截止时间；它不会报告一次成功的睡眠周期。
*   `KEEP_AWAKE` GPIO/VBUS 策略、外部唤醒源和外设挂起仍由 `deep_sleep_nrf52` 负责。

在托管模式下，不要使用手动的 `deep_sleep_nrf52.prevent` 转换。用户设置的 prevent 值仍作为外部否决；管理器不会覆盖它。