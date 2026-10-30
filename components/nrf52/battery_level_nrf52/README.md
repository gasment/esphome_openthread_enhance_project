# battery_level_nrf52

将 ADC 电压转换成电量估算；累计状态保存在 RAM，适合跨 System ON 休眠周期采样，不写 Flash。

```yaml
external_components:
  - source:
      type: local
      path: ../external_components/nrf52
    components: [battery_level_nrf52]

sensor:
  - platform: adc
    pin: VDD
    id: sys_vdd
    name: "Battery Voltage"
    update_interval: never
  - platform: battery_level_nrf52
    id: battery_level
    name: "Battery Level"
    voltage_id: sys_vdd
    samples: 3
    curve: CR2450
    update_interval: never
```

在 TIMER 唤醒分支执行一次：

```yaml
- component.update: battery_level
```

每次 update 主动读取一次 ADC，同时发布原电压；每累计 `samples` 个有效更新，取算术平均，
用曲线分段线性插值、限制到 0..100 并取整，发布一次百分比。原始采样通过 ADC 的 `sample()`
取得，不读取缓存值，也不等待 ADC 的延时过滤器。估算不应用源实体的发布过滤器；需要校准时调整曲线电压点。

`samples` 默认 3，范围 1..255，表示更新次数，不是一次唤醒连续读三次 ADC。
默认不轮询，组件不自行唤醒设备；放在 25min TIMER 分支就是每三个周期更新一次，按键和冷启动不计入。
直接对 `sys_vdd` 执行 `component.update` 不会增加电量组件的样本数。
重启后重新累计，首次凑齐样本前百分比没有状态，上报时应先检查 `battery_level.has_state()`。

`min_voltage` 默认 1.7V，`max_voltage` 默认 3.6V。非有限、超范围或 ADC 返回的错误值 0V 不计数，
也不覆盖已有估算；下一次有效更新继续累计。暂时无效样本不会重置已累计的数据。
合法电压越过曲线端点时使用端点百分比。

内置 `CR2450` 为通用工程估算表，**不是飞利浦官方容量曲线**，不保证准确剩余容量。
更换品牌、负载或温度条件后应根据实测调整；电压平台区的电量估计误差尤其大。
可替换为显式曲线（下面仅演示配置格式，不是推荐标定值）：

```yaml
curve:
  - voltage: 2.0
    percentage: 0
  - voltage: 2.9
    percentage: 50
  - voltage: 3.2
    percentage: 100
```

曲线要求 2..32 个点，电压严格递增，百分比在 0..100 内且不递减。电压源必须是 ADC，且 `raw: false`。
组件不关联具体 deep_sleep 或 transporter，采样时机和发送仍由自动化控制。
