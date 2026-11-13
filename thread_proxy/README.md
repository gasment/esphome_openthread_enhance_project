# Thread Proxy（0.7.0）

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
