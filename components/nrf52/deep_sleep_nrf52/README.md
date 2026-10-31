# deep_sleep_nrf52

独立的 nRF52/Zephyr **System ON** 待机组件。基于本机 ESPHome 2026.9.1 的
deep_sleep 接口重新实现生命周期；不加载官方组件，不修改已安装的 ESPHome。
不调用 `sys_poweroff()`，不 teardown 组件，不重启、不断开 OpenThread。
普通 RAM 和启动内单调时钟继续有效；不需要 RTC preferences 或保留 RAM。

## 配置

完整独立示例：[sleep_system_on.yaml](../../examples/sleep_system_on.yaml)。
此示例只测试休眠，不自动改动当前 FTD/REED 主配置。

```yaml
external_components:
  - source:
      type: local
      path: esphome_nrf52_deepsleep/components
    components: [deep_sleep_nrf52]

deep_sleep_nrf52:
  id: sleep_control
  run_duration: 5s
  sleep_duration: 60s
  prepare_timeout: 30s
  wakeup_pin:
    number: P0.24
    inverted: true
    mode:
      input: true
      pullup: true
  wakeup_pin_mode: KEEP_AWAKE
  on_sleep:
    - logger.log: "Prepare peripherals for sleep"
  on_wake:
    - lambda: |-
        ESP_LOGI("sleep", "cause=%u pin=%ld", unsigned(cause), long(pin));
  on_sleep_aborted:
    - lambda: |-
        ESP_LOGW("sleep", "aborted=%u", unsigned(reason));
```

- `run_duration` 可省略：此时仅显式 `enter` 请求待机。计时从组件 setup、每次唤醒或取消待机后开始。
- `sleep_duration` 可省略：无限期等待 GPIO 或其他线程通知，不设置周期喂狗唤醒。
- 至少配置时长或一个唤醒引脚。时长为 1..4294967294 ms。
- 多引脚使用 `wakeup_pins: [{pin: ..., wakeup_pin_mode: KEEP_AWAKE}, ...]`，
  与单引脚形式互斥。默认任意一个引脚即可唤醒，也支持下述组级组合电平判断。
- 无编号电源输入支持 `pin: VBUS`，无需分配 GPIO。它是明确支持的硬件源；
  其他无编号外设不能随意填入 pin 名称。
- 不提供 System OFF、ESP ext1、touch 或基于日期的 `until`。

GPIO 条目使用 `on_trigger_event`，在本次 GPIO 唤醒后的有限窗口内识别事件，例如：

**唤醒和事件识别是两个阶段。** SENSE/组级条件先通知唤醒，随后才开始事件识别；
`event` 筛选、debounce、点击间隔、hold 阈值和识别超时都不参与唤醒判断。
即使只配置 `two_click`，第一次符合电平条件仍会唤醒；醒后判定为单击时只是不执行双击动作。

```yaml
deep_sleep_nrf52:
  id: sleep_control
  sleep_duration: 60s
  wakeup_pins:
    - pin:
        number: P0.24
        inverted: true
        mode: input
        allow_other_uses: true
      wakeup_pin_mode: KEEP_AWAKE
      trigger_event:
        debounce: 20ms
        click_interval: 400ms
        hold_time: 1s
        timeout: 10s
      on_trigger_event:
        one_click:
          then:
            - logger.log: "one_click"
        two_click:
          then:
            - logger.log: "two_click"
        hold:
          then:
            - logger.log: "hold"
    - pin:
        number: P1.15
        inverted: true
        mode:
          input: true
          pullup: true
      on_trigger_event:
        - lambda: |-
            ESP_LOGI("key", "P1.15 event=%u", unsigned(event));
```

`on_trigger_event(event, press_duration)` 只在本轮唤醒的 GPIO 完成判定后触发，在主线程执行。
非 lambda 写法直接以 `one_click / two_click / hold` 为分组键，然后执行标准 ESPHome `then` 动作；
可使用 logger、switch、script、transporter 等动作，不必写条件 lambda。
只需配置关心的分组；分组内也支持自动化列表，例如同一个 hold 下配置两组 then。
原来的 `- event: ...` 列表和不筛选的 lambda 写法仍兼容；lambda 中仍可访问 event 和 press_duration。
同一个 on_trigger_event 使用分组形式或旧列表形式，不混合两种结构。
只启动匹配的分支，所有匹配分支按配置顺序启动，异步动作可并行。
事件筛选仅影响动作分发，不改变识别器的时间参数或本轮何时释放。
旧 GPIO 条目下的 `on_wake` 已移除，配置验证会提示迁移；全局 `on_wake(cause, pin)`
仍在恢复时立即触发，供恢复外设和通信窗口，不等待事件判定。
VBUS 仍使用自己的 `on_wake`，不做 GPIO 频次识别。
单个 GPIO 也使用只有一项的 `wakeup_pins` 配置事件；旧 `wakeup_pin` 简写仍支持纯唤醒。
近乎同时唤醒时只为第一次锁存的 GPIO 识别一轮，不为其他引脚创建并行识别任务。

## 组级 wakeup_mode

在 `deep_sleep_nrf52` 下配置，默认 `ANY`。例如两键同时按下唤醒：

```yaml
deep_sleep_nrf52:
  id: sleep_control
  wakeup_mode: ALL_LOW
  wakeup_pins:
    - pin:
        number: P0.24
        mode: input
        allow_other_uses: true
      wakeup_pin_mode: KEEP_AWAKE
    - pin:
        number: P0.25
        mode:
          input: true
          pullup: true
      wakeup_pin_mode: KEEP_AWAKE
```

| wakeup_mode | GPIO 唤醒条件 |
|---|---|
| `ANY` | 任意 GPIO 达到它自己的有效电平，使用各自 `inverted` |
| `ANY_LOW` | 任意 GPIO 为物理低电平 |
| `ANY_HIGH` | 任意 GPIO 为物理高电平 |
| `ALL_LOW` | 全部配置的 GPIO 同时为物理低电平 |
| `ALL_HIGH` | 全部配置的 GPIO 同时为物理高电平 |

显式 LOW/HIGH 模式覆盖 GPIO 唤醒目标电平，不改变引脚对象及共享传感器的 `inverted`。
它们需要 `wakeup_pins` 中至少一个 GPIO，不允许 GPIO 使用 `INVERT_WAKEUP`。
ALL 模式的 GPIO 必须统一使用 KEEP_AWAKE 或统一使用 IGNORE。
KEEP_AWAKE 在整组条件已经满足时保持清醒；只满足部分条件允许入睡。
IGNORE 在整组已经满足时允许进入并立即恢复，这种情况没有完成组合的边沿，
全局 `on_wake` 收到 GPIO、pin=-1，不虚构某个引脚回调。

组级设置**只作用于 GPIO**。VBUS 仍按自己的 inverted/模式独立唤醒，
不参与 ALL 条件；定时和外部线程通知也保持独立。

Nordic SENSE 硬件是任意引脚触发。ALL 模式在 GPIO ISR 读取整组电平并判断：
尚未满足时，重新启用该引脚对相反电平的 SENSE，继续待机；不恢复 ESPHome 主循环。
满足后锁存本次观察到组合成立的触发引脚，再恢复主线程。
全局回调的 cause 为 GPIO，pin 为该触发引脚；不会将先前已经按住的其他键伪装为本轮新增事件。
ALL 模式也可配置 on_trigger_event：组合成立先唤醒，再为本轮锁存的 GPIO 进行醒后判断，
不根据其他已经按住的键推导点击次数。全局 on_wake 可单独处理组合成立这一事实。
这属于软件判定的电平组合，不等同于硬件 AND，也不保证捕获短于中断响应时间的组合脉冲。
组内电平采用顺序采样，不保证跨端口的原子快照，不用于严格同步的高速信号。

## 唤醒窗口内的触发事件

识别由本组件独立实现，不加载 `binary_sensor`，也不调用 `on_multi_click`。
独立示例：[button_gestures.yaml](../../examples/button_gestures.yaml)。

| event | 判定 |
|---|---|
| `TriggerEvent::one_click` | 一次消抖后的有效电平及释放，click_interval 内没有第二次有效按下 |
| `TriggerEvent::two_click` | 第一轮释放后在 click_interval 内再次按下，第二次消抖释放后立即触发 |
| `TriggerEvent::hold` | 持续处于有效电平达到 hold_time，保持期间触发，不必先释放 |

有效电平由 GPIO `inverted` 定义：true=低有效，false=高有效。
`press_duration` 单位为 ms：点击为最后一次按压的时长，hold 为达到的阈值时长。
第一击需等待间隔结束才能排除双击；第二击确认后即结束本轮，不做三击识别或持续重复触发。
若第二次按住达到阈值，结果为 hold，本轮仍只报告一次。

配置 `on_trigger_event` 即启用；`trigger_event` 设置可省略，默认如下：

| 设置 | 默认值 | 含义 |
|---|---|---|
| `debounce` | 20ms | 电平持续稳定多久才接受边沿 |
| `click_interval` | 400ms | 第一次释放到第二次按下的最大间隔 |
| `hold_time` | 1s | 持续有效电平的保持阈值 |
| `timeout` | 10s | 从首次唤醒 ISR 开始的整个识别窗口上限 |

各设置为 1..60000ms，间隔和保持阈值须大于 debounce，timeout 须大于 hold_time+debounce。
事件识别不限制唤醒模式。ANY/ANY_LOW/ANY_HIGH/ALL_LOW/ALL_HIGH 和原有入睡电平策略
继续决定唤醒；醒后识别始终使用 GPIO 自己的 inverted，不要求与组级 LOW/HIGH 一致。
例如 ANY_HIGH 可按物理高电平唤醒，而 inverted=true 让醒后识别按低有效进行。
唤醒时读取到非事件有效电平，就等待识别窗口内的后续有效变化，不伪造第一次按下。
VBUS 没有 GPIO 事件识别设置。

GPIO 唤醒条件成立后先发出恢复通知，再启动边沿捕获；ISR 将实际观察到的醒后电平和
64 位单调时间写入该引脚的 32 项队列，然后把 SENSE 改为相反电平，观察后续变化。
识别不依赖主循环恢复后的轮询，但不会从唤醒条件反推或伪造已经错过的按下/释放。
它记录的是 ISR 观察时间，不是硬件捕获时间；小于中断响应或 debounce 的脉冲不保证被识别。
组件恢复时只保留本轮唤醒 GPIO 的捕获，其他 GPIO 关闭检测。

主线程按原始时间顺序消抖、判定；识别窗口自动阻止再次休眠，不修改用户 prevent 标记或其他组件的锁。
事件检测中断配置失败只取消识别，不取消已经发生的唤醒或全局恢复回调。
结果产生后立即关闭该引脚的检测中断、释放内部阻止，再触发 on_trigger_event。
回调的异步自动化尚未结束时仍不允许进入下一轮待机。
首个脉冲未通过消抖、超时、队列溢出或 IO 错误会取消本轮、释放检测并记录日志，不伪造三种有效事件。
hold 后检测已经释放，但 KEEP_AWAKE 仍按当前电平阻止设备在按键未释放时入睡。

平时清醒运行不持续检测；冷启动、定时或 VBUS 唤醒也不会为 GPIO 启动识别窗口。
窗口结束后的按键变化不再识别，下一轮 GPIO 唤醒才开始新的检测。

## VBUS 硬件唤醒

与 GPIO 混合配置：GPIO 使用事件回调，VBUS 使用独立 `on_wake`：

```yaml
wakeup_pins:
  - pin:
      number: P0.24
      inverted: true
      mode: input
      allow_other_uses: true
    on_trigger_event:
      - lambda: |-
          ESP_LOGI("key", "P0.24 event=%u", unsigned(event));
  - pin: VBUS
    wakeup_pin_mode: KEEP_AWAKE
    on_wake:
      - logger.log: "Woken by USB power"
```

`VBUS` 检测芯片专用 VBUS 输入的 USB 供电，不要求 USB 主机枚举或打开串口。
它不是 GPIO，不能配置 `mode/pullup/pulldown/allow_other_uses`。
支持的目标为具有 POWER USBREG 的 nRF52，例如本项目 nRF52840。

- `pin: VBUS` 默认以“供电存在”为有效状态：插线唤醒。
- `pin: {number: VBUS, inverted: true}` 以“供电消失”为有效状态：拔线唤醒。
- `KEEP_AWAKE`：有效状态已经存在时保持清醒。因此默认配置下，USB 一直插着不会进入待机。
- `IGNORE`：有效状态已经存在时允许进入，但会立即恢复。
- `INVERT_WAKEUP`：等待与入睡时相反的状态；插着时等拔线，未插时等插线。

使用 POWER 的 USBDETECTED/USBREMOVED 硬件事件，经 CLOCK/POWER IRQ 和 nrfx 电源回调通知
主线程，无定时 VBUS 轮询，不注册第二个硬件 IRQ handler。
组件取得原电源回调，并将所有事件（含 USB READY）继续转发，保留 USB CDC 驱动流程。
注册时保留原中断使能；退出待机移除本轮额外启用的事件；关机时恢复原回调。
不重新初始化电源调节器，不改变 DC/DC 配置，也不主动 enable/disable USB 协议栈。
第三方不能在组件注册后重新占用 nrfx USB 电源回调；检测到回调被替换时拒绝进入待机。

全局 `on_wake` 的 `cause` 为 `deep_sleep_nrf52::VBUS`，`pin` 为 -1。
VBUS 条目下的回调仅在 VBUS 被选为本轮唤醒源时运行。多个源同时捕获时 GPIO 优先，
其次 VBUS，最后 EXTERNAL/定时；不承诺把同一轮的全部事件逐一回放。
短暂毛刺或在 ISR 处理前已经恢复的电源变化不会作为有效状态唤醒。
如果其他电源切换会导致芯片掉电，则属于重新启动，不是 System ON 恢复。

## GPIO SENSE

使用 Zephyr `gpio_pin_interrupt_configure(..., GPIO_INT_LEVEL_HIGH/LOW)`。
已核对 NCS 2.9.2 的 `gpio_nrfx.c`：电平模式使用 nrfx LOW/HIGH trigger，走
SENSE/PORT 路径，不申请边沿模式的专用 GPIOTE 通道。GPIO0、GPIO1 按设备树实际设备检查。
不直接写 NRF 寄存器，避免破坏 Zephyr/nrfx 的中断管理状态。

`inverted: true` 表示低电平唤醒，否则高电平唤醒。引脚模式与电路上拉/下拉应一致。
模式含义：

| 模式 | 入睡前已处于有效电平 |
|---|---|
| `KEEP_AWAKE`（默认） | 等待引脚退出有效电平，再准备/入睡 |
| `IGNORE` | 仍允许进入；有效电平可立即唤醒 |
| `INVERT_WAKEUP` | 本轮改为相反电平唤醒，例如等待按键释放 |

待机期间启用 SENSE。纯电平唤醒锁存引脚后禁用中断；启用 on_trigger_event 的唤醒 GPIO
会在本轮有限窗口内继续按相反电平捕获边沿，事件结束再关闭。这样持续按住不会造成电平中断风暴。
GPIO 唤醒优先于同时发生的其他唤醒；多引脚近乎同时触发时只选择第一个捕获的引脚。

与 `binary_sensor` 共用引脚时，两处都需要 `allow_other_uses: true`，输入及上下拉配置一致，
传感器设置 `use_interrupt: false`。其他组件不能在同一引脚配置自己的中断或改变引脚方向。
主循环暂停期间不会采样二进制传感器：短按可能在恢复前已经结束，应通过 `on_wake` 的 GPIO
原因处理这类事件，不能只依赖传感器的 `on_press/on_release`。

## 自动化和生命周期

`deep_sleep_nrf52.enter` 接受简写 ID，或 `{id: sleep_control, sleep_duration: 60s}`；
时长支持 lambda，覆盖后用于后续周期。动作只提交请求，不在当前回调栈中阻塞。

1. 等待 `prevent`、独立唤醒锁、sleep guard 和 KEEP_AWAKE 引脚全部放行。
2. 触发 `on_sleep`，主循环继续运行，等待所有准备自动化结束，支持 `delay/wait_until`。
3. 再次检查就绪条件，启用 SENSE，暂停当前 ESPHome 主线程并让 Zephyr 调度其他线程。
4. 到期或事件发生后恢复状态，触发全局 `on_wake`。仅本轮触发 GPIO 按需继续事件捕获，不重新运行 setup/on_boot。
5. GPIO 事件判定完成后触发 `on_trigger_event`，释放检测；其他源不启动该过程。

`prepare_timeout` 只限制第 2 步及其后的就绪等待；最初的 prevent/锁等待不超时。
超时取消本轮，停止仍在运行的准备自动化，触发 `on_sleep_aborted`。
应在该回调恢复准备阶段已经关闭的外设；组件无法自动撤销用户自动化的副作用。
`on_wake`、`on_trigger_event` 和取消回调的异步自动化未结束时，不会开始下一轮待机准备。

统一外设管理在 on_sleep 完成且就绪条件满足后暂停硬件接口，在 on_wake 前恢复；
若入睡失败，则恢复本轮已经暂停的接口。失败的取消原因是 `PERIPHERAL_ERROR`。
此机制不新增 YAML action，也不要求用户配置取消回调或逐路关联 UART。

## peripheral_auto_mgr

默认开启，支持布尔简写和展开配置：

```yaml
deep_sleep_nrf52:
  # 其他休眠与唤醒配置继续保留
  peripheral_auto_mgr: true
```

```yaml
deep_sleep_nrf52:
  peripheral_auto_mgr:
    enabled: true
    force_offload: false  # 默认 false；true 时不因 busy/接口唤醒标志拒绝停机
```

`peripheral_auto_mgr: false` 完全关闭本组件的接口 PM 调度；
`force_offload: true` 与 enabled: false 互斥。展开形式省略 enabled 默认 true。

管理对象来自设备树和 Zephyr 已创建的设备表，不读取 ESPHome 总线组件列表。
当前覆盖 Nordic UART/UARTE、TWI/TWIM（I²C）、SPI/SPIM 主控制器。
按设备初始化逆序暂停，反向恢复本轮成功暂停的接口；原来已暂停或关闭的接口保持原状。
未创建驱动或初始化失败的接口也必须通过硬件关闭核验。QSPI、SPI 从设备、USB 及总线上连接的传感器、
屏幕和 Flash 芯片不在当前范围。GPIO/VBUS 唤醒源、Thread 无线及系统时钟不关闭。

正常模式检查 busy 和接口唤醒标志；强制模式忽略这些准入保护，直接请求驱动停止/卸载，
可能截断事务。两种模式都不伪造驱动停机成功：不支持 PM、驱动失败或接口被 runtime PM
接管时取消本轮休眠并报告错误。停机过程中失败会尝试恢复该接口，并回滚先前已暂停接口。
恢复失败将标记休眠组件 failed，避免继续循环入睡。

### 停机兜底

- 驱动暂停返回成功后逐个检查硬件 ENABLE=0；PM 状态已是 SUSPENDED/OFF、
  初始化失败或没有创建驱动的设备树接口也检查，不能仅凭软件状态跳过。
- 唤醒源配置完毕、打印 System ON sleep 前再次统一核验。任何接口仍开启都禁止入睡。
- force_offload=true 时，已返回的驱动停机流程仍留下硬件开启，可尝试 Nordic HAL STOP。
  UARTE 等待 RXTO 与 TXSTOPPED，TWI/TWIM/SPIM 等待 STOPPED，最长 50ms；
  确认停止后才 disable 并复查 ENABLE。失败恢复中断掩码，取消休眠。
  Legacy SPI 无 DMA/STOP 任务，直接 disable；Legacy UART 没有可核验的 TXSTOPPED，
  不提供盲目禁用补救，必须由驱动完成停机。共享寄存器块的角色不符时不直接操作。
- 驱动 PM 调用在单独的 2KB 栈、最低应用优先级线程执行，主线程限时等待 100ms。
  超时后锁存 PM 故障，禁止进一步入睡，并将休眠组件标记 failed。
  不终止可能持锁的驱动线程，也不与尚未退出的驱动并发复位/恢复寄存器；需复位恢复。
  此故障分支可能无法回滚已暂停接口，不保证仍能维持低功耗，不自动反复重启设备。
- 时限依赖 Zephyr 调度与系统时钟正常运行；驱动关闭全局中断后卡死、内核或硬件故障
  不能靠软件线程保证超时，应由现有硬件看门狗兜底。没有禁用或替代看门狗。

硬件 ENABLE 核验只证明当前管理范围内的控制器关闭，不代表外接模块已断电，
也不等同于实测电流保证。上述错误、超时和硬件补救路径尚未做故障注入或实机验证。

统一管理器保留 UART 实际 BAUDRATE，在恢复应用回调前还原 256000 等自定义速率。
普通接口配置由底层驱动恢复，有的驱动会在下一次业务事务时重新初始化硬件。
GPIO 独立唤醒后再通信；UART RX 本身在暂停期间无法接收或唤醒。
应用仍负责外接模块业务状态、供电以及何时允许进入休眠。

验证：`python tools/validate_peripheral_auto_mgr.py`，覆盖默认值、布尔/展开形式、
强制/关闭配置及互斥拒绝、旧 UART 关联字段拒绝、PM 配置和当前项目内存代码生成。
没有执行固件编译、C++ 运行时测试或硬件验证。
可加 `--sdk-root /home/gasment/esphome/sdk-nrf/frameworks/v2.9.2`，静态核对
兜底使用的 HAL 符号及 nRF52840 寄存器字段；该检查仍不执行 C++。

YAML 动作只保留 `enter` 和 `prevent`：

- `enter`：仅在没有 prevent 标志时发起休眠准备。被阻止时直接返回，不排队，
  也不应用本次 sleep_duration 覆盖值。接受请求后仍执行准备流程和就绪检查。
- `prevent`：默认 true，取消自动休眠计时及等待/准备中的请求；取消请求时触发
  `on_sleep_aborted(CANCELLED)`。重复设置 true 不重复触发取消回调。
- `prevent: false`：解除阻止，不补执行之前拒绝的 enter。如果配置了 run_duration，
  从解除时重新计时；重复设置 false 不重置计时。

```yaml
- deep_sleep_nrf52.enter: sleep_control
# 省略 value 默认 true，也支持唯一实例的布尔简写：prevent: true。
- deep_sleep_nrf52.prevent: sleep_control
- deep_sleep_nrf52.prevent: false
# 指定实例；value 支持 lambda。
- deep_sleep_nrf52.prevent:
    id: sleep_control
    value: !lambda 'return id(keep_awake).state;'
```

原 `allow`、`cancel`、YAML `wake` 动作已移除。解除阻止使用 prevent: false；
取消准备并保持清醒使用 prevent: true。C++ wake() 保留供 ISR/其他线程唤醒，
定时/GPIO/VBUS 唤醒及生命周期回调继续保留。通信完成标志和显式 enter 的调用时机由应用管理。

条件：`deep_sleep_nrf52.is_pending`、`is_sleeping`、`can_sleep`，参数为 ID。
`can_sleep` 是就绪检查，不表示准备自动化已经完成。

`on_wake` 参数：`cause` 为本命名空间的 `TIMER`、`GPIO`、`VBUS`、`EXTERNAL`；
`pin` 为 GPIO 平面编号（P0.24=24，P1.15=47），非 GPIO 为 -1。
冷启动不触发。核心其他组件的线程安全主循环通知也会提前结束本轮，记为 EXTERNAL，
以便处理它们的待办事件。仅硬件中断而没有主循环通知不必结束本轮。

`on_sleep_aborted` 参数：`CANCELLED`、`PREPARE_TIMEOUT`、`GPIO_ERROR`（也包含 VBUS 事件配置错误）、
`PERIPHERAL_ERROR`、`WATCHDOG_ERROR`（硬件看门狗未暂停于 CPU 睡眠，不能安全进入长期等待）。
GPIO 初始化失败会将组件标记 failed，日志报错；不会进入待机。

## 后续 SED/P2P 接入接口

主线程调用：

```cpp
sleep->acquire_wake_lock(this);  // 同一 owner 重复获取不会累加
// 完成所有采集、ACK、重发和恢复后：
sleep->release_wake_lock(this);
// 或注册长生命周期 guard，返回 true 才允许入睡：
sleep->add_sleep_guard([this]() { return this->ready_for_sleep(); });
```

`sleep->wake()` 可从 ISR/其他线程调用。owner 和 guard 捕获对象须与组件同寿命；
获取/释放锁、注册 guard 等管理接口仅在主线程调用。
**当前 P2P/REED 尚未接入这些接口，组件不会自动识别它们的 pending ACK。**
不要直接把独立示例的定时休眠接到现有通信配置上，下一步 SED 移植时再接入通信就绪判定。

组件不修改 Thread poll period，不停止射频线程。低功耗应使用 MTD/SED，
合理设置 poll period 和父节点超时；FTD/MED 持续接收不会因为主线程待机变成低功耗 SED。
待机/唤醒可用自动化切换通信窗口，最终休眠门禁仍应由 transporter 的全部 pending 状态决定。

## 核心兼容及功耗边界

设置 `USE_DEEP_SLEEP` 让 Zephyr HAL 请求 watchdog pause-in-sleep，同时提供最小原路径兼容头及
`deep_sleep::global_has_deep_sleep`，供 API 查询。官方组件与本 fork 互斥，不加载其源文件。
不启用 POWEROFF、保留 RAM 或 preferences。API、GPIO、自动化代码生成已单独覆盖。

待机使用核心 wakeable semaphore，直接等待真实休眠截止时间；无时长时使用 `K_FOREVER`。
GPIO SENSE、VBUS 和显式 `wake()` 经中断/线程通知立即结束等待，没有固定的每秒检查或喂狗定时器。
启用 Zephyr tickless 和 64 位 timeout，使用 64 位绝对截止时间；中途通知或虚假唤醒不会重置截止时间。

看门狗在 CPU 的 WFI/WFE 睡眠期间暂停。组件在阻塞等待期间使用 Zephyr 的
`z_arm_on_enter_cpu_idle()` 钩子：CPU 因真实任务短暂工作并自然返回空闲时喂狗，
不为喂狗专门唤醒 CPU。进入和退出等待也各喂一次，退出后恢复通常的主循环喂狗行为。
若其他线程持续占用 CPU、不再返回空闲且未喂狗，硬件看门狗仍可触发复位。
入睡前读取硬件 CONFIG 确认 pause-in-sleep 已生效；若看门狗正在运行但未暂停于睡眠，
拒绝入睡并以 `WATCHDOG_ERROR` 回调，不尝试修改已经启动的看门狗配置。
组件提供此 ARM idle 钩子的实现，不能与其他同名自定义钩子重复定义。

OpenThread 等 Zephyr 线程仍可按需调度，ESPHome scheduler/主循环不运行；因此普通 `delay`
和传感器轮询不会在休眠中继续执行。恢复后到期任务按核心调度器规则处理。
USB 日志、外设供电、其他线程和协议轮询仍影响实际电流；不承诺数据手册的裸芯片静态电流。

## 验证

当前工作区的动作精简回归：`python tools/validate_sleep_nrf52.py`（项目根目录）。
覆盖 enter 时长及 lambda、prevent ID/布尔/显式/lambda 写法、三个旧动作的拒绝，
以及无限期、最短/最长休眠时长与事件等待所需 Kconfig，并对当前完整项目做内存代码生成。
未执行 C++ 运行时测试、固件编译或硬件验证。

`python tests/validate_sleep.py`：配置拒绝、单/多引脚、三种模式、共享输入、全部动作/条件、
逐源自动化、独立 GPIO 触发事件及参数约束、非 lambda 事件筛选、唤醒与识别极性独立、旧 GPIO 回调迁移、VBUS 插拔与混用、
GPIO 组级模式及互斥校验、API 兼容和 Thread MTD 共存，内存生成 C++，
核对本机 NCS GPIO、POWER HAL 和 USB 电源回调 API。
不调用编译器、west、固件生成或设备连接。

交付时尚未编译或实机验证。用户编译后建议验证：定时/GPIO 唤醒、长按不重复唤醒、
INVERT_WAKEUP 释放唤醒、准备自动化延时和超时恢复、多个锁互不影响、Thread 附着是否保持、
连续休眠唤醒的看门狗稳定性，以及电池供电下的实际电流。

## 更新日志

### 2026-10-31

- 取消 System ON 等待循环固定每秒返回检查、喂狗的行为；有时长时直接等待实际截止时间，未配置时使用 `K_FOREVER`，GPIO SENSE、VBUS 或线程通知可以提前结束等待。
- 启用 Zephyr tickless 和 64 位 timeout；继续按绝对截止时间计算剩余时长，避免中途通知导致休眠计时被重置。
- 看门狗改为在阻塞休眠期间由 `z_arm_on_enter_cpu_idle()` 钩子在 CPU 自然返回空闲前喂狗，进入和退出等待也各喂一次，不创建周期喂狗定时器；CPU 睡眠期间依赖硬件 pause-in-sleep。
- 新增入睡前的看门狗硬件配置检查；已运行的看门狗若未暂停于 CPU 睡眠，则拒绝入睡、恢复外设并报告 `WATCHDOG_ERROR`。退出休眠后恢复常规主循环喂狗行为。
- 明确功耗边界：GPIO 唤醒由硬件中断通知，不是软件定时采样；OT 等独立线程仍可按需调度，其短暂 CPU 活动不等于退出应用休眠或触发 `on_wake`。
- 修正项目 P1.11 的 `allow_other_uses` 配置层级；修复回归工具注入测试脚本时覆盖包内原有脚本的问题。
- 已通过完整项目配置与内存代码生成、无限期及最短/最长休眠时长、动作兼容和所需 Kconfig 检查；未进行固件编译、硬件看门狗稳定性或电流验证。
