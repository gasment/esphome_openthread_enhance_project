# Thread SED

Thread SED 是面向 ESPHome Thread SED / REED 的 HA 自定义集成。设备使用独立的 `thread_sed_api`，保留原生 API 的 TCP、Protobuf 实体协议与可选的 Noise 加密；不再经过业务 UDP 网关或实体桥。

## 安装与接入

1. 将仓库的 `custom_components/thread_sed` 复制到 HA 的 `config/custom_components/thread_sed`，重启 HA。仓库也保留 HACS 自定义仓库布局；新版本需要发布包含此目录的 release。
2. HA 最低版本为 **2026.11**。集成依赖 HA 自带的 ESPHome 客户端依赖，不自行升级或降级公共 `aioesphomeapi`。设备侧 fork 基线为 ESPHome **2026.10.2**，后续版本需验证。
3. 节点启用 IPv6、OpenThread 与 `thread_sed_api`，不要配置官方 `api:`。见 [配置迁移](MIGRATION.md)。
4. BR 将节点的 `_thread-sed._tcp` SRP 服务发布到局域网 mDNS 后，HA 显示发现卡片。保持节点唤醒直至接入完成；未配置 `encryption` 时密钥留空，启用后输入节点的 Noise key。也可手动输入节点 IPv6 / 主机名和 API 端口。

每个节点一个配置条目，以 MAC 标识身份；用户确认之前不会创建实体。公告不包含密钥。默认明文模式不使用共享密钥认证，Noise 模式提供加密与密钥认证。HA 需要能路由到节点 OMR IPv6，Docker 使用 host 网络，允许节点 TCP 6053 和 HA TCP **6064**。6064 与官方 ESPHome 的反向连接端口分开。BR 只需 IPv6 转发、SRP/mDNS 代理，不再运行 `thread_gateway`。

## 连接与休眠

- 首次由 HA 连接节点；Thread SED 客户端完成当前模式的原生握手后，设备保存其地址；Noise 模式先验证密钥。以后唤醒时节点主动拨回 HA，加密时 HA 仍是 Noise initiator。HA 2026.11 直接使用原生客户端的 `start_connection_from_socket()` 接收连接，不再需要本机 TCP 转接层。
- nRF52 的 `deep_sleep_id` 自动接入 System ON 休眠 guard。准备休眠时，等待初始实体状态发送完成、事件队列确认和 HA 应用层 ACK。默认最多等待 10 秒；超时仍允许休眠并记录日志，避免网络故障耗尽电池。
- 每次正常休眠关闭 TCP，唤醒立即重连，不要求连接在休眠期间保活。醒着时临时缩短 SED Data Poll，休眠前恢复；不切换 RxOnWhenIdle，不把 SED 改成 MED。
- 传感器状态依靠重连后的原生初始状态同步；事件使用最多 32 条的 RAM 队列，HA 确认后删除，普通重连去重。队列满时丢弃最新事件并告警；断电会丢失 RAM 队列。HA 崩溃与磁盘写入之间不承诺严格 exactly-once。
- HA 缓存实体描述与最新状态。固件配置指纹、编译时间或 ESPHome 版本变化时重取实体清单；普通唤醒跳过清单枚举。实体增删在 HA 中即时处理，不需要重启。休眠 ACK 前保存状态；定时休眠超过预期时长加 60 秒仍未唤醒会变为不可用。
- 未设置休眠时长的 GPIO 唤醒节点保留休眠状态，无法凭“长期安静”区分正常休眠和电池耗尽。HA 启动/条目重载会尝试一次正向连接；正常休眠期间不持续探测。
- 控制只发往在线节点；休眠、离线命令不缓存，也不显示乐观执行成功。

HA 主机地址变化后，仍可由 HA 正向连接节点重新学习反向目标；节点地址变化通过 Zeroconf 更新条目并重载连接。首次接入或地址恢复阶段需留出足够唤醒窗口。跨 VLAN / 缺少 mDNS 转发时使用手动接入，并确保双向 IPv6 路由。

## 实体与第一版范围

支持 sensor、binary_sensor、text_sensor（映射 HA sensor）、event、switch、button、number、select、text、light、fan、cover、climate、lock、valve。节点不再维护另一份实体映射，`internal: true` 的实体不暴露。传感器限流改用 ESPHome 自身 filters / update_interval。

第一版没有移植 ESPHome 的蓝牙代理、语音、相机、交互 wizard、用户自定义 API 服务和 HA 状态导入组件。灯光、空调等提供基础控制；设备端保留原生描述，但 HA 的高级控制项仍需后续补齐。日志/OTA 继续使用各自已有组件，不能直接假定官方 ESPHome Dashboard 能识别新的服务类型。

## 验证

```sh
pip install aioesphomeapi==46.25.0
python -m unittest discover -s thread_sed/tests -v
```

测试使用真实原生客户端、真实 Noise 握手及本地 TCP 对端；HA 生命周期使用轻量 stand-in。ESP32/nRF52 已做配置校验和内存内 C++ 生成检查；**未运行固件编译，未上板验证，也未在真实 HA 上完成验收**。重点验收长时间休眠、HA/BR 重启、IPv6 前缀变化和高频事件。

## 传感器采样与上报

当前没有沿用旧 transporter 的手动 `send` action：实体 `publish_state()` 由原生 API 自动转发；重连后订阅初始状态会发送实体缓存。清单缓存只省略实体描述枚举，不省略状态同步。

**状态同步不等于重新采样。** nRF52 System ON 唤醒会保留上次状态，PollingComponent 也不保证恰好在唤醒时完成更新。需要新读数的节点应在既有唤醒自动化中触发传感器更新，等待转换及发布完成后再请求休眠。休眠 guard 只确认已发布的数据和排队事件，不负责调用传感器采样。

## 可选 encryption

默认不加密、不需要 key：

```yaml
thread_sed_api:
  id: sed_api
  deep_sleep_id: sleep_control  # nRF52 自动休眠 guard；常供电 REED 省略
```

仅在需要加密时配置：

```yaml
thread_sed_api:
  id: sed_api
  deep_sleep_id: sleep_control
  encryption:
    key: !secret thread_sed_noise_key
```

启用 encryption 后 key 为必填的 32 字节 base64。未配置 encryption 时不加载 Noise 组件，HA 接入密钥留空。两种模式均支持正向与反向 TCP、实体缓存、事件确认和休眠确认。已配置密钥的 HA 条目不会自动退回明文模式。
