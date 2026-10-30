# ESPHome nRF52 Thread

网络配置已改为互斥的 `thread_network_config` / `joiner` 两种模式。
配置、action、迁移回滚和自动休眠保护见 [无线配网说明](../../esp32/openthread/JOINER.md)。

节点附着后自动监听 BR 拓扑页的手动重启请求（Thread UDP 端口 18514），
核对本机扩展地址、回执后延迟执行正常重启。此功能无需额外 YAML 配置。

System ON 休眠组件已移至独立项目 [esphome_nrf52_deepsleep](../esphome_nrf52_deepsleep/README.md)，
配置示例见工作区根目录的 [deepsleep.yaml](../deepsleep.yaml)。
当前 P2P/REED 尚未接入休眠锁，通信场景需要自行完成待确认任务与休眠门禁的衔接。

独立的 nRF52/Zephyr OpenThread 组件，首版基线为 ESPHome 2026.9.1、
NCS 2.9.2（Zephyr 3.7.99）。从 `esphome_enhance_thread` 的 OpenThread 与
诊断组件派生，源码和 Python 校验均独立保存，不导入原项目或官方 OpenThread
组件。来源和原始文件哈希保存在组件的 `upstream.json`。

当前包含 `openthread_nrf52`、`openthread_info_nrf52`、
`thread_p2p_transporter_nrf52`、`thread_reed_transporter_nrf52` 和
`thread_entity_bridge_nrf52`、`thread_sed_transporter_nrf52`，不包含 BR 或网关。
与现有 ESP32 Thread 网络使用相同的网络凭据。
P2P 配置与互通说明见 [P2P 组件文档](components/thread_p2p_transporter_nrf52/README.md)，
FTD 示例见 [p2p_ftd.yaml](examples/p2p_ftd.yaml)。
REED 与原 ESP32 网关互通说明见 [REED 文档](components/thread_reed_transporter_nrf52/README.md)，
共存示例见 [reed_ftd.yaml](examples/reed_ftd.yaml)。
MTD 的队列上报、独立事件上报和 System ON 协同见
[SED 文档](components/thread_sed_transporter_nrf52/README.md) 与
[thread_sed_system_on.yaml](examples/thread_sed_system_on.yaml)。
SED 已接入可选休眠 guard，并保留 ACK 耗尽后的网关重新发现恢复；不能与 FTD/REED 配置同时启用。

## 接入

仓库本地使用：

```yaml
external_components:
  - source:
      type: local
      path: esphome_nrf52_thread/components
    components: [openthread_nrf52, openthread_info_nrf52]

network:
  enable_ipv6: true

openthread_nrf52:
  id: openthread_main
  device_type: FTD
  thread_network_config:
    channel: 20
    network_name: "Your Thread Net"
    network_key: !secret thread_network_key
    pan_id: 0x1234
    ext_pan_id: !secret thread_ext_pan_id
    pskc: !secret thread_pskc
    mesh_local_prefix: fd01:2345:6789:abcd::/64
    force_dataset: true
  output_power: 8dBm

text_sensor:
  - platform: openthread_info_nrf52
    ip_address:
      name: Thread OMR Address
    rloc16:
      name: Thread RLOC16
    role:
      name: Thread Role
    best_router_link_quality:
      name: Router Link Quality

binary_sensor:
  - platform: openthread_info_nrf52
    thread_attach:
      id: thread_attach
      name: Thread Attached
```

`network_key`、`ext_pan_id` 和 `pskc` 支持整数十六进制或固定长度十六进制字符串。
整数按 16、8、16 字节补足前导零。`network_name` 按 UTF-8 字节长度校验。
也可用 `tlv` 替代整个独立凭据配置，两种形式不能混用。

独立字段模式生成包含 Active Timestamp、Channel Mask 和 Security Policy 的完整
Active Dataset；FTD 与 MTD 使用完全相同的编码，不依赖 IDF sdkconfig。
为避免依赖其他框架的默认值，已有网络应显式填写上述全部凭据字段。
省略字段时的本组件默认值为 channel=15、pan_id=0x1234、
ext_pan_id=dead00beef00cafe、network_name=OpenThread-ESP、
pskc=全零、mesh_local_prefix=fd00:db8::/64。

`force_dataset` 默认 false：已有完整持久化 Dataset 优先；true 则应用 YAML 的
Dataset。组件启用 Zephyr 手动启动，先写入 Dataset、应用链路模式和功率，再入网。
错误会将组件标为 failed，不会静默使用 Zephyr 默认网络继续入网。

## MTD 与轮询

```yaml
openthread_nrf52:
  # 同上，填写相同网络凭据
  device_type: MTD
  poll_period: 250ms
```

MTD 的 `poll_period` 恢复官方语义：`0s` 或不配置均为持续接收的 MED；
配置正值（10ms–67108863ms）时为周期轮询 SED。
持续接收表示父节点可直接发送下行，不是持续发送 Data Poll。
此前本 fork 的“0s 手动 SED”语义已经移除。
`poll_period`、`child_timeout`、`keep_alive` 与 `set_poll_period` 动作均仅允许 MTD；
FTD 即使配置零轮询也会在校验时被拒绝。
运行时动作更名为 `openthread_nrf52.set_poll_period`，参数仍为 `id`、
`poll_period`，也支持 lambda。链路模式在持有 OT 锁时更新。
SED 是 Thread 无线接收模式，不会自动令 MCU 进入 System OFF。

```yaml
openthread_nrf52:
  # dataset 配置继续保留
  device_type: MTD
  poll_period: 25min  # 日常低频 SED；设为 0s 即 MED 持续接收
  child_timeout: 1h
```

`child_timeout` 为 1–4294967295 的整数秒，支持 `1h` 等时长写法，省略时不覆盖 OT
默认值。组件在 Thread 启动前持锁设置，日志打印请求值和 OT 实际值；OT 可能上调过小
的超时。运行日志中的有效值并不代表父节点已成功收到更新，需结合父节点 Child Table 验证。

应用可额外触发 keep_alive，但它只提交请求，不保证父节点收到。等待业务 ACK 时可
临时缩短 SED 轮询周期，完成后恢复长周期。实际间隔可能因 child_timeout、附着、
重试或快速轮询缩短；System ON 下 OT 轮询计时跨应用休眠继续运行。

OTA 维护窗口应设为 `0s` 切换 MED，并确保 MCU 主循环继续运行；窗口结束后恢复原
SED 周期。若切换命令来自网络，在设备仍为长周期 SED 时也需等待下行被轮询取回。
该模式切换只解决 Thread 接收行为，不补齐 Zephyr 的 ESPHome OTA 后端。

手动轮询源码补丁、CMake 钩子和 SDK 源码校验限制均已移除。组件直接使用原生 OT
调度，不再禁用或重写 Child Supervision，也不修改已安装 ESPHome 或共享 SDK。

nRF52840 发射功率上限为 8dBm，不能沿用 ESP32 的 10dBm/20dBm 配置。
配置接受 -40..8dBm 整数，实际射频驱动不接受的值会在初始化时报错。

## OT 协议栈日志

启动及角色、IPv6 地址、Network Data 变化后，组件以 INFO 打印实际 OT 单播地址
（含 valid/preferred/mesh_local/rloc 标记）及收到的 On-Mesh 前缀标记，供排查 OMR
未生成与诊断筛选问题。日志在主循环持 OT 锁读取，不在 OT 回调中大量打印；不打印密钥，
不写入或人为创建地址。该诊断受 ESPHome logger.level 控制，与下面的栈日志等级独立。

```yaml
openthread_nrf52:
  # 其他网络配置继续保留
  log_level: INFO
```

支持 NONE、ERROR、WARN、NOTICE、INFO、DEBUG，不区分大小写，默认 NONE。
FTD 和 MTD 均可配置。此字段设置 OpenThread 编译日志等级，与 ESPHome logger.level
独立；logger 仍控制本组件自己的 ESP_LOG 日志。修改需要重新编译刷入。
组件设置 OPENTHREAD_DEBUG 总开关并选择对应的 CRIT/WARN/NOTE/INFO/DEBG Kconfig；
NONE 只关闭 OT 栈日志，不关闭其他 Zephyr 日志。非 NONE 时启用 Zephyr LOG。
已有 swd_rtt_nrf52 后端会把 OT 平台日志输出到 RTT，无需配置物理 UART。

不再拦截原生 CMake 生成入口，平台 PCH 和构建环境完全沿用安装版 ESPHome。

## 服务注册与 DNS 边界

本组件不提供 `ipv4_dns` 或其他 DNS 服务器配置项，不设置 OpenThread DNS client
或 Zephyr 原生 resolver 的服务器，不提供应用层 DNS64 适配。
旧 YAML 中的 `ipv4_dns` 需要删除，否则配置校验会报不支持的字段。
需要域名解析的其他组件应由其自身或平台的独立配置提供解析能力；
本组件不会禁用它们的 DNS 功能。

SRP 沿用 ESPHome mDNS 服务描述，向现有 Thread 网络的 SRP server 注册服务。
无需加载官方 `openthread`；mDNS 需要保持启用。

ESPHome 2026.9.1 的 network、mDNS 和 factory-reset 使用 `USE_OPENTHREAD`
能力标志及固定的头文件路径。组件只在生成目录提供转发头，将该命名空间映射到
独立 `openthread_nrf52` 实现；不修改已安装的 ESPHome，也不加载官方组件源码。
不要同时配置官方/ESP-IDF 的 `openthread`、`openthread_info`。

## 诊断

保留原组件的 text_sensor、sensor、binary_sensor 配置字段，只更改 platform 名。
支持附着状态、角色、信道、RLOC16、Mesh-Local/RLOC/Link-Local 地址、硬件地址、
网络信息、父节点 RSSI/链路质量、MAC/MLE 计数器等。

`ip_address` 仅返回符合网络数据的 OMR 地址；没有 BR/OMR 时为空，不用内部地址
代替。`best_router_link_quality` 仅在 FTD 实际成为 Router/Leader 时有评级，
MTD 或 Child 角色为 unknown。父节点指标只在相关 OT API 返回有效结果时更新。
`thread_attach` 读取 OT 回调维护的原子状态，不轮询 OT 锁，可直接驱动本地自动化。

本工作区已启用 `openthread.yaml` 中的诊断，并用实际附着传感器替换
`sensor.yaml` 的同名模板占位实体，保留 LED 对 `thread_attach` 的引用。

## IPv6 地址槽与同步恢复

配置生成时将 Zephyr 的 `CONFIG_NET_IF_UNICAST_IPV6_ADDR_COUNT` 设为 8，
为新旧 OMR 过渡和应用地址预留空间。OT 地址、角色或 Network Data 变化后
核对地址表，突发变化和失败最多每秒处理一次；正常时每 5 秒仍核对一次。
补入原生适配层因槽满而遗漏的有效地址，并校正 preferred/deprecated 和 Mesh-Local 标志。
RLOC/ALOC 不加入 Zephyr 表。对外地址列表按 ESPHome 固定数组容量截断，优先返回可用的外部地址。

失效地址清理由 NCS 2.9.2 原生 OT 状态处理器执行，其删除步骤先于组件回调。
本组件在此基础上补入、复核和重试，不周期性重复调用 `net_if_ipv6_addr_rm()`：
该 API 每调用一次都会递减引用计数，SDK 已释放接口引用后，剩余引用可能属于绑定地址的 socket。
这类槽位需在 socket 释放引用后回收，不能强行复用；持续不一致会通过
`openthread_nrf52.ip6` 告警。首次核对成功记录一次 OT/Zephyr 地址数和容量；
仅首次异常、地址/状态变化时告警，恢复一致时记录一次；
持续相同状态不重复刷屏，后台重试照常执行。
仍在 OT 有效表中的弃用地址继续保留，不因非首选而删除。

地址修复沿用 SDK 的 OT mutex -> net_if mutex 顺序，不修改 SDK 文件，不在状态回调中做修复。
Zephyr 的 net_if_addr 在禁用 DAD/ACD 时包含空 union，C 与 C++ 的结构布局可能不同；
因此由 ip6_native.c 读取原生地址表及修改字段，仅通过固定宽度记录与 C++ 交换数据。
detached/disabled、teardown 期间不修复。System ON 暂停主循环期间不增加定时唤醒，
恢复主循环后继续检查；通信动作仍需正常管理休眠门禁。

仅配置/内存代码生成及 SDK 接口检查：

```sh
python tools/validate_nrf52_ip6_sync.py --sdk-root /path/to/ncs/zephyr
```

这不等于固件编译或设备验证。上板需覆盖 BR 重启、OMR 变化、槽满后旧地址释放、
遗漏地址重新补入，以及旧地址仍被 socket 引用时不发生重复释放。

## 验证

在安装 ESPHome 的 Python 环境中运行：

```text
python tests/validate.py
python tests/validate_sed.py
```

历史回归覆盖 FTD、MED、SED、运行时动作、完整 TLV、前导零、
全零凭据、全部诊断字段、互斥配置、功率限制、IPv6/mDNS 依赖、官方组件冲突
和已移除 DNS 字段的拒绝。
同时在内存生成 C++ 并检查 Zephyr 配置与兼容头注册。
测试不调用编译器、west、刷写或 ESPHome compile。

交付时尚未编译或上板验证。附着、OMR、SRP、发射功率和 MTD 轮询的实际
行为需要用户选择编译后通过串口日志及网络环境确认。

## 手动 SED 保活

在定时唤醒自动化的 `then` 中调用：

```yaml
- openthread_nrf52.keep_alive:
    id: openthread_main
# 简写：
- openthread_nrf52.keep_alive: openthread_main
```

要求当前已附着为 child，且为 SED（poll_period > 0）。MED、router、
leader、未附着和 OT 锁不可用时跳过并告警。FTD 的动作引用在配置阶段拒绝。
当前 openthread.yaml 为 0s MED 连通性测试配置、child_timeout 1h，提供
thread_manual_keep_alive 脚本供 SED 时调用，未自动创建定时任务。

动作持有 OT 锁调用公开 `otLinkSendDataRequest()`，提交一次 MAC Data Poll；实际发送
由 Zephyr OT 线程完成。不修改 dataset、角色、轮询周期，不强制重附着，不增加应用重试。
提交失败会记录 OT 错误，成功仅打印 `Keep-alive Data Poll queued`，不代表父节点已经
收到，也不证明业务网关在线。它不是 Child Supervision 检查或 Child Update 交换。

在 System ON 主循环休眠但 OT 工作线程继续运行的情况下，动作后进入休眠不会主动
停止 OT；若之后关闭 Thread/无线电、进入 System OFF 或复位，未完成的发送不能保证。
这个动作自身不会改变轮询策略或 child_timeout，是否自动轮询由 poll_period 决定。

`tools/validate_ot_keep_alive.py`（项目根目录）只做配置和内存代码生成验证；
包括两种 YAML 写法、带参数自动化、MED/SED 切换、FTD 拒绝及超时边界。
同时检查未注册任何手动轮询构建补丁、未替换原生 CMake 入口。
未执行固件编译或硬件保活验证。
