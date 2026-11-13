# Thread Proxy（0.8.1）

面向 Home Assistant 2026.11+ 的 BR 接入管理集成，仅发现声明 `thread_proxy_api` 的 BR，发现后由用户确认接入。ESP32 固件配置见 [BR 组件说明](../components/esp32/thread_proxy_api/README.md)。已提供发现、管理协议、长连接，以及 BR 本地 UDP ACK → RAM 队列 → HA TCP 确认的上行链路。协议及限制见 [上行说明](UPLINK.md)。

## 接入与管理

1. 将 `custom_components/thread_proxy` 复制到 `/config/custom_components/thread_proxy`（或通过 HACS 更新），重启 HA 并刷新浏览器。
2. 首次在“设备与服务 → 添加集成”中添加 **Thread Proxy**，启用共享管理入口。也可以从发现的 BR 卡片确认接入并初始化管理入口。
3. 集成详情页使用唯一的 **BR 接入管理** 按钮打开管理页。侧栏不再单独增加入口。
4. 管理页可以选择发现的 BR 接入，或手工添加 LAN IPv4、IPv6/主机名与代理 TCP 端口（默认 6065）。可选 Noise 密钥必须与 BR 的代理密钥一致。
5. 每个已绑定 BR 提供“创建设备”“重连”“重新配置”“解除绑定”。“重新配置”位于重连与解绑之间。

## 配置条目与 BR 本体设备

**HA 仅保留一条共享的 Thread Proxy 管理配置，用于加载集成及重启恢复。各 BR 的绑定保存在内部绑定表，不再逐个生成配置条。** 勾选或取消“创建设备”都不会产生新的 BR 配置条。

“创建设备”默认勾选，控制该 BR 本体的 HA 设备和四个诊断实体。取消后即时移除本体设备/诊断实体，重启后也不会创建；重新勾选可动态恢复。此设置不解绑 BR、不关闭 TCP 连接、不删除代理节点的设备或实体，也不会删除官方 ESPHome 集成独立创建的同一硬件设备。

共享管理配置与某个 BR 不同：删除或停用这条共享配置是对整个集成的操作。单独修改或移除某个 BR，请使用管理页的“重新配置”或“解除绑定”。

## 重新配置

- 名称、地址、端口可直接编辑。仅改名称不重连，改变地址/端口/密钥只重连对应 BR。
- 已保存的密钥不回传浏览器。留空保留原密钥；输入新密钥替换；勾选“清除已保存密钥”显式清除。
- 编辑不会重置“创建设备”设置，也不会创建该 BR 的配置条。

管理页和 HTTP 接口仅管理员可用。状态、诊断导出不包含密钥。代理 TCP 6065 独立于官方 ESPHome API 6053，自动发现以 `_thread-proxy._tcp.local.` SRV 实际端口为准。

## 从 0.5.x 升级

重启 HA 时自动将逐 BR 配置合并为共享管理配置，保留绑定 ID、代理 ID、名称、地址、密钥和“创建设备”设置。先保存完整绑定表并迁移注册表所属配置，再删除旧配置条；保持诊断实体 unique_id、entity_id 和设备 ID。迁移中断可在下次启动继续。

原先停用的 BR 不会自动连接；可在管理页点击重连恢复。旧 Thread SED HA 代码仍归档在 `thread_sed/legacy_component`，不随本集成加载。

## 验证与协议

`python -m unittest discover -s thread_proxy/tests -v`：验证使用 HA 轻量替身和真实 aioesphomeapi 会话。集中式迁移、内部绑定重启恢复、注册表范围、重新配置及密钥脱敏已覆盖；不替代实际 HA 2026.11 加载验证。

固件非编译验证入口：`python thread_proxy/validate_firmware.py`。此次仅修改 HA 集成，无需改 BR YAML 或刷固件。[管理协议](PROTOCOL.md)。


## 节点直接建实体（0.8.1）

节点使用 [thread_proxy_client](../components/nrf52/thread_proxy_client/README.md)（ESP32 和 nRF52 均提供）。节点不发布 SRP 业务记录；Thread 网内探测 BR 后，通过已绑定 BR 上报描述，HA 直接创建设备及只读实体，没有逐节点发现/接入确认流程。

按节点 MAC 聚合，跨 BR 切换保留设备、实体身份。HA 本地保存描述、普通状态及事件游标，动态处理 schema 增删；首次 KEY EVENT 等待实体注册后发布。BR 诊断设备勾选项不影响节点设备。

需同步更新 BR 固件、HA 集成和节点配置；完整迁移 diff、ACK 配置规则及只读实体映射见客户端说明。未执行固件编译，待上板测试。


## 节点设备独立删除与恢复（0.8.1）

可以在 HA 的节点设备页单独删除该设备。集成只移除这个节点的设备/实体注册，保留节点描述、普通状态和事件游标，不影响 BR 绑定及其他节点。BR 的删除仍从“BR 接入管理”操作。

删除状态会保存，HA 重启不会只根据旧缓存自动恢复节点。再次收到该节点的非重复 DATA 或 HEARTBEAT 上报时，会用原稳定 MAC、设备 identifier 和实体 unique_id 恢复注册；通过 HA 的删除记录恢复原 device_id、可用的 entity_id 及用户名称、区域等信息，不产生新的节点身份。仅重传已经接受过的组或旧 SLEEP 提示不触发恢复。

节点仍显示来源为 Thread Proxy 集成，这是 HA 的设备归属关系；各节点是独立主设备，没有挂在 BR 下，不需要逐节点 ConfigEntry。恢复依赖 HA 仍保留原删除记录及原 entity_id 未被其他实体占用。

升级只需更新 HA 集成并重启 HA，无需修改 YAML 或刷写 BR/节点固件。
