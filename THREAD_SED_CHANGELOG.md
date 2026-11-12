# Thread SED 改版更新日志（2026-11-12）

## 逻辑变更

- 新增 ESP32 / nRF52 的同名 `thread_sed_api`，独立 fork 原生 TCP + Noise + Protobuf，不加载官方 `api`。强制配置 Noise key，以原生 MAC 标识设备，实体按原生描述自动暴露。
- HA 集成更名为 Thread SED，目录改为 `custom_components/thread_sed`，每个节点一个条目；最低 HA 2026.6。使用 HA ESPHome 依赖提供的客户端，避免自行替换公共依赖。
- 节点公告 `_thread-sed._tcp`，经 BR 的 SRP/mDNS 代理进入 HA 发现卡片；用户确认并认证后接入。设备唤醒后拨回 HA TCP 6064，首次/地址恢复时可由 HA 正向连接。
- nRF52 自动接入 deep_sleep_nrf52 guard；ESP32 提供休眠确认条件供脚本调用。缩短唤醒窗口中的 SED Data Poll，不切换 Thread 模式；休眠前等待 HA 应用层确认、恢复轮询、关闭 TCP。发送截止时间默认 10s，故障时允许休眠。
- HA 持久化实体清单、最后状态和事件去重进度；按固件配置指纹、编译时间与版本更新清单，常规重连跳过枚举；实体增删不重启 HA。离线控制不排队。
- 事件保留于最多 32 条的 RAM 队列，HA 确认后删除；断电、队列溢出和 HA 崩溃边界不是严格 exactly-once。
- 移除四个 SED/REED transporter、两个实体桥、BR thread_gateway、旧 custom_components/openthread_gateway 和旧网关文档/工具。
- 删除 BR 网关 Web 路由、导航、资源与构建开关，保留 BR 路由、配网、拓扑及其余 Web 功能；使用 thread_br/build_web_assets.py 重新打包。
- P2P、OTA、Thread 平台组件和 tools/br_manager 保留。

## YAML diff 与安装

完整带注释 diff：[thread_sed/MIGRATION.md](thread_sed/MIGRATION.md)。

```diff
-api:
-  ...
-thread_sed_transporter_nrf52:
-  ...
-thread_entity_bridge_nrf52:
-  ...
+thread_sed_api:
+  id: sed_api
+  encryption:
+    key: !secret thread_sed_noise_key # 32 字节 base64，接入 HA 时输入同一密钥
+  deep_sleep_id: sleep_control       # 仅 nRF52 的自动休眠 guard
```

同步修改 external_components 的组件名单，删除旧 transporter 的发送/休眠自动化。常供电 REED 使用同一个组件，不配置 deep_sleep_id。ESP32 的休眠脚本见迁移文档。BR 中删除整个 thread_gateway 块。HA 旧绑定与旧实体历史不自动迁移。

## 验证范围

19 项自动化测试通过，4 项无效配置拒绝检查通过。完成 ESP32 / nRF52 配置验证、内存内 C++ 生成、Python 静态检查，以及真实原生客户端/Noise/TCP 的自动化测试。测试覆盖反向连接、IPv6、错误密钥拒绝、状态上报、实体缓存、事件去重与休眠确认。

**未运行固件编译；待用户编译及上板测试。真实 HA 上的发现卡片、实体动态增删、长时间休眠、BR/HA 重启和 IPv6 前缀变化仍待联调验收。**

## HA 2026.11 基线调整

- 当前最低 HA 版本调整为 2026.11.0；CI 和协议测试使用该版本的 aioesphomeapi 46.25.0。
- 删除为 HA 2026.6 编写的 ReverseBridge / 随机本机端口和密文转接任务。
- 节点反向连接由原生客户端直接接收；共享监听器只窥视 hello 来定位已配置的设备，不消费握手数据。Noise 认证仍由原生客户端完成。
- 20 项测试通过，新增完整 Session 直接接收反向 socket 的验证；未运行固件编译，未在真实 HA 实例上验收。
- 澄清当前没有旧式手动 send action；传感器发布自动上报、连接后同步缓存，唤醒采样仍由节点自动化安排。本次没有修改 Noise key 的配置/接入策略。

## encryption 改为可选

- ESP32/nRF52 配置的 encryption 不再必填；省略时使用原生明文 TCP，不加载 Noise。配置 encryption 时仍须填写有效 key，保留既有加密行为。
- HA 表单 Noise key 可留空；运行时无 key 使用明文客户端。已配置加密的条目不会自动降级。
- 明文模式同样支持节点主动连接。反向连接增加固定长度 TSED1/MAC 路由前缀，HA 仅消耗前缀后交给原生客户端；加密模式继续使用 Noise server hello。
- 32 项测试通过，覆盖两种传输的状态、缓存、事件/休眠确认、反向 socket 以及加密条目拒绝明文降级。ESP32/nRF52 × 明文/Noise 四组配置与内存内代码生成通过；未运行固件编译，待上板及真实 HA 验证。

- 统一反向连接 MAC 的冒号格式，与原生 DeviceInfo 一致；明文路由前缀固定 24 字节。

## 修复可选源文件误编译

- 补回 fork 时遗漏的 FILTER_SOURCE_FILES，按 Thread SED API 自身宏筛选自定义服务、Noise/明文帧处理器、wizard、反向连接，以及按日志等级筛选 protobuf dump。
- user_services.cpp 增加与头文件声明一致的 USER_DEFINED_ACTIONS 保护，避免在未开启自定义服务时引用 UserServiceStatic / ExecuteServiceArgument 等不存在的类型。
- 此次修复不通过开启无用的用户服务功能来消除报错；已保留的实体上报、明文/加密与休眠确认行为不变。
- 不运行固件编译；通过配置、内存内代码生成和实际源文件筛选检查验证，固件构建由用户执行。

## 修复 StringRef 显式构造

- 修复 sed_session.cpp 中 service/token/value 的赋值，使用 StringRef 显式构造。
- 同步修复会话元数据消息中的 service、schema、sleep、sleep_seconds；字面量使用 from_lit，动态 std::string 使用显式 StringRef。
- 核对消息同步编码路径，确保动态字符串在编码期间保持有效；未修改生成的 protobuf 字段类型或 ESPHome 核心 StringRef。
- 两个平台同步修复；仅做静态及配置/代码生成验证，不运行固件编译。

## 手动 send action、轻量批次 ACK 与 is_ready

- 新增 thread_sed_api.is_ready 条件，区分原生握手与 HA 清单/订阅准备就绪；ready 与 ACK 按连接处理。
- 新增异步 thread_sed_api.send，发送实体缓存及当前待发事件，等 HA 回复同一批次 ID 后执行 on_success；timeout、未就绪、忙、断线或写失败执行 on_error。动作分支完成后才继续外层动作，取消脚本不会遗留完成回调。
- 新增 send_mode: manual；默认仍为 auto。manual 禁止发布时及连接初始的自动状态上传，由 send 显式刷新；事件共享批次 ACK，确认后才移除，后续新事件留到下一批。
- HA 对控制消息串行处理，批次结束标记到达后保存缓存并回传一个 ACK；不重建逐传感器确认/报文重传协议。
- 保留采样与事件识别在 YAML 中的控制权。为去掉固定 5 秒清醒窗口，应由脚本主动进入休眠，不再依赖独立 run_duration。
- 41 项测试通过，包含明文/Noise 的 ready 和批次 ACK 实际 TCP 往返、ACK 顺序、旧连接拒绝及存储失败不确认。四组平台/传输配置与包含异步成功/失败分支、bool 触发参数的代码生成通过；未编译固件，待用户构建及实机验证。

## send 支持单个/多个实体 ID

- entity_id 支持单个 ID 或混合实体列表；一次 action、一个批次 ACK，仅序列化所选实体缓存，省略时保留全量行为。
- 发送选择由真实实体类型验证，支持嵌套在自定义组件中的状态实体。空列表、未知 ID、非实体 ID、无状态按钮及 internal 实体拒绝发送。
- 事件队列按实体选择发送和确认移除，未选中的事件保留；HA 去重由启动全局序号改为每事件实体的序号，兼容已有存储的旧水位，避免跨实体乱序分批造成误丢。
- 不隐式触发采样；采样完成后可在一个 send 中提交所需多个实体。manual 模式保证未选状态不会由自动发布额外发送。
- 43 项测试通过；四组平台/传输的单 ID、混合列表代码生成通过，五类无效配置拒绝检查通过。未运行固件编译，待用户构建与上板验证。

## auto 组件级 ACK 回调

- 新增 on_ack / on_ack_error 和可选 auto_ack_timeout（默认 2s），仅 auto 模式可配置；未配置回调时不增加确认流量。
- 跟踪真正进入自动发送路径的更新、初始状态同步及事件，合并连续发布后使用现有 HA 批次 ACK，不重复发送全量状态。
- 成功回调携带 batch_id、elapsed_ms、has_pending；失败携带 batch_id、reason。等待期间的新更新分到下一确认周期，旧 ACK 不会替新数据确认。
- 回调与手动 send 的 action 分支独立；保留旧自动事件 ACK，组件回调不按每个传感器/事件重复触发。睡眠前等待确认和异步回调，原有故障交付期限仍生效。
- 已确认批次在后续断线时仍报告成功；未覆盖的发送错误保留到相应周期。修正手动发送的 ACK/断线同时到达时的判定优先级。
- HA 沿用既有 thread_sed.batch 应用层回复，无需额外 HA 配置。44 项测试通过，四组回调配置及异步分支代码生成通过，非法 manual 回调和零超时配置拒绝检查通过。未编译固件，待用户构建与实机验证。


## 2026-11-12：连接初始化与复位诊断

- ESP32/nRF52 的 API 连接初始化先填写对端地址，再计算字符串长度，消除参数求值顺序造成的未初始化读取；完成后打印 `Transport initialized`。
- nRF52 RTT 桥安装后输出原始 RESETREAS 和看门狗、软件复位、CPU lockup 等标记。仅读取寄存器，不清除复位标记，不改变看门狗与休眠设置。启动加载器可能清除标记，未清除的标记也可能累积。
- 对 light-bt-stair 配置核查：重启动作已注释；System ON 休眠不应产生整机启动。当前约两秒重启仍需以上板复位原因确认，尚未认定由 ACK、QSPI 或看门狗导致。
- 本次不运行固件编译；硬件验证待用户执行。


### 复位排查续记

- 修正正常客户端循环误置自动/手动批次断开标记的问题；现在仅移除连接时标记断开，避免正常会话被错误报告 disconnected。
- 增加非阻塞 connect 返回、TCP 建连完成、helper 初始化三个阶段日志；保存 connect 的 errno 后再输出日志，避免日志调用改写 errno。
- 上板 RESETREAS=0x6 包含 watchdog/software 位；因为当前诊断不清除累积位，仍需结合重启前阶段日志定位本次复位，未扩大或关闭看门狗。


### RTT 致命异常诊断开关

- `swd_rtt_nrf52.fault_diagnostics: true` 将 Zephyr 日志切换为立即输出，避免底层 fatal handler 直接软件复位时丢失排队的异常日志；默认 false 保持原延迟模式。
- 该选项仅用于定位，增加调用现场的日志开销；定位后应删除或关闭。未修改故障处理器、看门狗或 API 握手协议。
- 已在本地 light-bt-stair/main.yaml 临时启用。固件编译与上板由用户执行。

- 修正 RTT 诊断与 Thread OTA 的日志模式冲突：加载 swd_rtt_nrf52 时由其统一设置日志处理模式，OTA 不再强制 deferred；独立 OTA 保持原模式。


- RTT fault_diagnostics 模式额外启用线程名称、硬件栈保护及栈哨兵，便于区分线程栈越界和其他内存损坏；不调整线程栈大小，正常模式不变。18:07 故障 PC 对应当前 ELF 的 `_kernel` 数据区，尚未定位最初的破坏来源。


### nRF52 主线程栈越界

- 18:12 故障的 MMFAR=0x200177f8 落在当前 ELF 主线程栈保护区；主栈对象起点 0x20017780，可用区起点 0x20017800，原预算 4096 字节。
- thread_sed_api 将 nRF52 主线程栈最低预算提高到 8192 字节，在平台代码生成后合并，保留更大预算。ESP32 不变，用户 YAML 不变。
- 诊断保护继续保留；配置与代码生成验证后仍需用户上板确认。连接超时属于后续需单独验证的网络可达性问题。


### 循环重启根因修复：电量更新递归

- 通过 SWD 在 z_arm_fault 入口捕获现有固件 RAM/寄存器，主栈呈现重复调用链：BatteryLevel::update → ADC publish_state → on_value 的 component.update → BatteryLevel::update。此前主栈越界判断成立，但增加预算无法解决无限递归，8 KB 扩容已撤回。
- battery_level_nrf52::update 增加组件级重入保护；采样/发布期间同步回调再次更新同一组件时直接返回。作用域守卫保证正常结束、无效采样等提前返回后均解除保护。
- 保留硬件采样、ADC 状态发布、分次平均和原 on_value 自动化；无需改用户 YAML。后续独立更新不受影响。
- 实际配置和代码生成可验证；固件编译及修复后上板验证仍由用户执行，当前未声明硬件验证完成。


### battery_level_nrf52 内置 ADC 重构（破坏性配置更新）

- 移除外部 ADCSensor / voltage_id 依赖，组件直接初始化和读取 Zephyr SAADC，拥有独立 DT io-channels 节点；不再加载官方 adc 平台。
- 新增 adc 子项（pin、samples、sampling_mode、voltage_multiplier）及可选 voltage 输出实体。外层 samples 仍累计多次有效更新，不变成一次唤醒连续采样。
- 电压与电量共享更新时序，驱动错误和无效读数不污染累计，发布阶段保留重入保护，防止用户自动化同步回调形成递归。
- 已迁移本地 light-bt-stair 的 sensor.yaml，保留 sys_vdd、battery_level 和名称，统一为原电压采样的 10s 周期。完整带注释 YAML diff 见 components/nrf52/battery_level_nrf52/README.md。
- 不运行固件编译；新驱动接线与电压精度仍待上板验证。

- 修复内置 ADC 的设备树宏生成：任意自建节点缺少 binding，改用 Zephyr 标准 zephyr,user io-channels 与按索引获取 adc_dt_spec；仍不依赖外部 adc 平台，用户 YAML 不变。
