# 无线配网与预配置

ESP32 的 `openthread` 和 nRF52 的 `openthread_nrf52` 使用相同配置语义。
`joiner`、`thread_network_config` 必须且只能选择一个，不支持根层的旧网络参数。
`device_type`、`poll_period`、`child_timeout`（仅 nRF52）、`output_power` 等运行参数仍在根层。

## 预配置模式

```yaml
openthread:
  id: thread_control
  device_type: MTD
  thread_network_config:
    tlv: !secret thread_dataset
    force_dataset: false
```

也可在 `thread_network_config` 内使用 `network_key`、`channel`、`pan_id`、
`network_name`、`ext_pan_id`、`pskc`、`mesh_local_prefix` 独立字段；不能与 `tlv` 混用。
默认优先使用持久化 Dataset，`force_dataset: true` 每次启动应用 YAML Dataset。
此模式不启用 Joiner/DTLS，不能调用 Joiner action。

## 无线配网模式

```yaml
openthread:
  id: thread_control
  device_type: MTD
  joiner:
    name: "USB 开关"  # 可省略，默认使用 esphome.name
    pskd: !secret thread_joiner_pskd
    on_joiner_start:
      - logger.log: "开始配网"
    on_joiner_success:
      - logger.log: "Dataset 已保存，并已附着"
    on_joiner_fail:
      - lambda: 'ESP_LOGW("pairing", "配网失败: %s", reason.c_str());'
    on_joiner_cancel:
      - logger.log: "已取消配网"
```

无凭据启动后等待手动 action，不生成默认网络，不自动配网。
有持久化 Dataset 时正常附着；掉线不自动重新配网。
在按键长按等自动化中调用：

```yaml
- openthread.joiner.start:
    id: thread_control
    timeout: 120s
# 取消时调用：
- openthread.joiner.cancel: thread_control
```

`joiner: {}` 也合法，此时 start 必须提供 `pskd`。action 的 PSKd 优先于默认值，
支持 lambda；省略时使用 `joiner.pskd`，显式传入空串不会退回默认值。

```yaml
- openthread.joiner.start:
    id: thread_control
    pskd: !lambda return id(pairing_code);
    timeout: 120s
```

PSKd 为 6～32 位大写字母/数字，排除 I、O、Q、Z；两处均没有有效 PSKd 时触发
`on_joiner_fail`，reason 为 `invalid_pskd_or_timeout`。默认 120s，总时限可配置为
1s～10min，支持 lambda（返回毫秒数 `uint32_t`）。总时限包含锁等待、发现重试和附着。
无可用 Commissioner 或响应超时时，1s 后重试，仍受总时限限制；认证失败直接回滚。
忙碌时重复 start 仅警告，不中断当前流程。cancel 空闲时无操作。

可使用 `openthread.joiner.is_active: thread_control` 条件判断进行中状态。
所有自动化回调均在 ESPHome 主循环、释放 OpenThread 锁后执行。
成功表示收到 Dataset 并附着（至少 child），不保证 HA/API/业务连接已建立。
Joiner 成功回调可能只提供 Network Key、信道和 PAN ID；此时先启动 Thread，
在附着过程中获取其余 Dataset 字段。组件等到附着和完整性检查都通过后才提交并触发成功回调，
期间继续保留回滚备份、休眠保护和原有总超时。
常见失败 reason：`timeout`、`backup_failed`、`cannot_stop_thread`、
`cannot_clear_pending` / `cannot_set_joiner_advertisement`、`cannot_attach_received_dataset`、`dataset_not_saved`，以及 OT 错误名称。

BR 网页需要输入相同 PSKd。建议每台设备独立设置并通过标签交给用户。
候选扫描地址不等于出厂 EUI64；网页若填写 EUI64，必须使用真实出厂地址。
BR 与节点都要启动各自的配网流程；仅打开网页不会使节点进入 Joiner。

### 待配网名称与稳定标识

`joiner.name` 是显示名称，允许 1～48 个 UTF-8 字节，不允许控制字符。
省略时使用 `esphome.name`，超长默认名称会按 UTF-8 边界截到 48 字节；无需新增 `id` 配置。
同名设备由自动取得的工厂地址派生 Joiner ID 区分，随机扫描地址改变时 BR 更新原行，
不会新增一行。该标识不是出厂 EUI64，也不代替 PSKd 认证。

本项目通过 OpenThread 实验性 Joiner Advertisement 扩展携带信息，仅在配网扫描期间发送。
格式为实验项目标记 OUI `FFFFFF`（不代表已分配厂商 OUI），AdvData 内容为
`ESJ` + 版本字节 `1` + 8 字节 Joiner ID + 1～48 字节 UTF-8 名称；最多 60 字节。
PSKd、Network Key 和 Dataset 不包含在广播中。名称和标识属于未认证的发现信息。

需要同时更新节点与启用 Commissioner 的 BR。BR 未收到有效项目扩展时按原方式显示
“未知节点”和扫描地址，不能保证对旧版或第三方节点去重。
BR 的源码适配只生成构建目录中的副本，不修改已安装 SDK；不支持的源码布局会明确报错。
ESP32 节点使用 `joiner_ot_config.h` 启用扩展；已有自定义 OT 头文件时需要合并宏。
nRF52 使用 `CONFIG_OPENTHREAD_CUSTOM_PARAMETERS` 传入扩展宏。

## 已配网节点迁移与恢复

主动 start 会暂停当前 Thread 附着，备份 Active/Pending Dataset 和当前扩展地址，
再进行新配网。新的 Dataset 成功附着后删除备份，后续启动使用新 Dataset。
失败、取消或超时恢复旧 Dataset，并恢复原有 Thread 启用状态；回调不等待旧网络重新附着。
首次配网失败则恢复为无 Dataset 的等待状态。

ESP32 备份使用独立 NVS 命名空间 `esphome_ot_jnr` 的 `rollback` 项，不使用 OT settings 厂商键。
当前 ESP-IDF 的 OT 后端会将 settings 键截为 8 位，`0x8001` 与 Active Dataset 的 `0x01` 冲突；
因此不能使用这一路径保存 ESP32 的回滚记录。nRF52/Zephyr 保留完整 16 位，继续使用 `ot/8001`。
清理回滚记录后会再次读取并验证 Active Dataset，确认未改变才报告配网成功。
备份写入并回读通过后才开始更改网络；进程在提交前重启，下次初始化先恢复备份。
记录包含版本标记和校验，恢复失败时保留记录、不继续入网，需要排查存储或执行恢复出厂设置。
运行中恢复失败保持 busy 并重试，不误报成功或解除休眠保护。
使用旧实现成功配网后，ESP32 的 Dataset 可能已经在清理回滚记录时被删除。
更新到独立 NVS 存储实现后需重新配网一次；已删除且没有其他备份的旧 Dataset 无法自动恢复。
恢复 Pending Dataset 使用备份时剩余的延时，未计算断电期间经过的时间；它不承担跨网络迁移协议。
此版采用失败自动回滚，不提供跳过备份的强制覆盖入口，也没有网络侧 Pending Dataset 编辑入口。

Joiner 模式额外启用 DTLS/ECJPAKE，会增加固件和内存占用。nRF52 为 mbedTLS 配置 16KiB 堆。
预配置模式继续关闭 Joiner，保留既有加密依赖。

## nRF52

将上述组件名和 action/condition 前缀替换为 `openthread_nrf52`。
启用 Joiner 时自动为当前配置中的 `deep_sleep_nrf52` 实例加入休眠 guard；
从排队开始到配网提交/回滚完成都阻止进入睡眠，不修改用户的 `prevent` 标志，
不需要额外配置 `deep_sleep_id`。普通运行保持原来的 SED/MED 配置。

## 验证范围

配置互斥、PSKd 校验、动态覆盖、action/condition、回调及两端代码生成有自动检查：
`tools/validate_joiner.py`。未运行固件编译；无线配网、存储错误与断电恢复需上板验证。
