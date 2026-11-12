# battery_level_nrf52

使用内置 nRF52/Zephyr SAADC 采样电压并估算电量，不加载或引用 ESPHome 的 `adc` 组件。累计样本保存在 RAM，适合跨 System ON 休眠周期采样，不写 Flash。

```yaml
sensor:
  - platform: battery_level_nrf52
    id: battery_level
    name: "Battery Level"
    adc:
      pin: VDD
      # samples: 1               # 每次 update 内连续读取的次数
      # sampling_mode: avg       # avg / min / max
      # voltage_multiplier: 1.0  # 分压还原或电压校准系数
    voltage:                    # 可选；不需要独立电压实体时删除整个 voltage
      id: sys_vdd
      name: "Battery Voltage"
    samples: 3                  # 累计 3 次有效 update 后发布一次电量
    curve: CR2450
    update_interval: never      # 或 10s 等常规轮询周期
```

`adc` 可省略，默认 `pin: VDD`、单次读取、平均模式、倍率 1。输入支持 `VDD`、`VDDHDIV5`、`AIN0..AIN7` 及 nRF52 的合法模拟 GPIO 引脚。使用外部电池分压时配置 `voltage_multiplier`，同时按实际电池修改曲线和有效电压范围。

ADC 使用内部参考、14 位分辨率、硬件 oversampling=8；VDDHDIV5 增益为 1/2，其余输入为 1/6。硬件通道在代码生成时分配，所有使用同一分配器的 ADC 输入合计最多 8 个。

手动更新：

```yaml
- component.update: battery_level
```

每次 update 直接读取硬件，不读取实体缓存。`adc.samples` 在同一次更新内连续采样并聚合；外层 `samples`（默认 3，1..255）累计多次有效更新的电压，取平均后按曲线插值、限制到 0..100 并取整。每次有效更新发布可选 `voltage` 实体，首次凑齐样本前电量没有状态。

两个输出共享采样时序，`voltage` 是普通 sensor，不是独立 PollingComponent；不要对 `sys_vdd` 使用 `component.update`。采样时序只由电量组件的 `update_interval` 和 `component.update: battery_level` 控制。输出自动化同步回调更新自身时有重入保护。

`min_voltage` 默认 1.7V，`max_voltage` 默认 3.6V。驱动失败、非有限或超范围读数不计数、不发布新电压、不覆盖已有电量；已累计样本保留。倍率在范围检查和曲线估算之前生效；`voltage.filters` 仅影响电压实体展示，不影响电量计算。setup 失败会将组件标为失败。

默认不轮询，组件不自行唤醒设备；重启后重新累计。组件不关联具体 deep_sleep 或 API，采样和发送仍由自动化控制。

内置 CR2450 曲线是通用工程估算，不是电芯厂商的官方标定。可配置 2..32 个显式曲线点，电压严格递增，百分比在 0..100 且不递减：

```yaml
curve:
  - voltage: 2.0
    percentage: 0
  - voltage: 2.9
    percentage: 50
  - voltage: 3.2
    percentage: 100
```

## 迁移 YAML diff

此次重构移除 `voltage_id`，需要同时删除外部 ADC 实体及其驱动电量更新的自动化：

```diff
 sensor:
-  - platform: adc
-    pin: VDD
-    id: sys_vdd
-    name: "Battery Voltage"
-    update_interval: 10s
-    on_value:
-      - component.update: battery_level
   - platform: battery_level_nrf52
     id: battery_level
     name: "Battery Level"
-    voltage_id: sys_vdd
+    adc:
+      pin: VDD
+    voltage:                      # 保留原电压实体的 ID 和名称
+      id: sys_vdd
+      name: "Battery Voltage"
     samples: 3
     curve: CR2450
-    update_interval: 60s
+    update_interval: 10s          # 统一采样周期；手动控制则使用 never
```

不需要电压实体可以省略 `voltage`。不再需要其他 ADC 输入时，配置中不应保留 `platform: adc`。
