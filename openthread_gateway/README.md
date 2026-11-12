# OpenThread Gateway

业务后端直接运行在 HA，节点绑定 HA 的稳定逻辑 ID，BR 仅提供 IPv6 路由和标准服务发现。不依赖 ESPHome Native API、BR 上的实体占位、容量预分配或应用实体后的 BR 重启。

**这是 v3 破坏性协议的 HA 端实现。ESP32 和 nRF52 两个 SED transporter 已增加 v3 适配，详见 [SED 迁移说明](SED_MIGRATION.md)；两个 REED transporter 仍为 v2，不能直接对接本集成。** 节点适配应以 [PROTOCOL.md](PROTOCOL.md) 为准。现有 BR 上的 v2 `thread_gateway` 不会自动迁入；先保留现网，待节点端适配和实测完成后再切换。

## 安装

面向 **Home Assistant Core 2026.6 或更新版本**，使用当前 HA 的配置流程、运行时数据和实体生命周期 API。

### HACS / Marketplace

集成运行文件位于仓库根目录的 `custom_components/openthread_gateway/`，本目录存放文档、测试和模拟工具。仓库采用 `custom_components/openthread_gateway/` 标准布局，支持 HACS 和新版 Marketplace 的自定义仓库安装。无需在 HA 的 `configuration.yaml` 中添加配置。

1. 按 [发布说明](PUBLISHING.md) 将项目根目录的变更推送到公开 GitHub 仓库。
2. 在 HACS / Marketplace 中添加 `https://github.com/gasment/esphome_openthread_enhance_project`，类型选择 **Integration（集成）**，下载 OpenThread Gateway。
3. 重启 HA，然后在“设置 → 设备与服务 → 添加集成”中添加 **OpenThread Gateway**。

文件已采用仓库根目录的标准安装布局；需要先推送到 GitHub 对应分支/Release，才能远程安装。无需另建集成仓库。尚未提交默认目录收录。

### 手动安装

只将仓库中的 `custom_components/openthread_gateway` 文件夹复制到 HA 配置目录，最终结构为：

```text
/config/custom_components/openthread_gateway/
  __init__.py
  manifest.json
  config_flow.py
  coordinator.py
  hub.py
  protocol.py
  entities.py
  sensor.py / binary_sensor.py / ...
  translations/
```

首次安装自定义集成需要重启 HA，让 HA 发现集成文件。之后节点接入、实体增删、清单变更和解绑不需要重启或重载集成。

在“设置 → 设备与服务 → 添加集成”中选择 **OpenThread Gateway**：

- **名称**：显示名称。
- **后端 ID**：首次生成，可自定义为 1–32 位英文字母、数字、下划线或短横线。它是节点绑定的稳定身份，重新配置网络时不能修改。
- **UDP 端口**：默认 `18513`。本机一个集成实例服务多个 Thread 网络。
- **公告 IPv6 地址**：填写节点可以通过 BR 访问的 HA LAN IPv6 地址；留空时从 HA 启用网卡中选择可路由 IPv6 地址并随地址变化更新公告。不要填写 BR 的地址、Mesh-Local EID、`fe80::` 或 HTTP URL。

运行条件：

- HA 有 LAN IPv6 地址和通向各 Thread OMR 前缀的路由；节点使用可跨 BR 路由的源地址。
- 允许节点与 HA 的业务 UDP 端口双向通信。
- 允许 LAN mDNS（UDP 5353）；BR 需要标准 DNS-SD Discovery Proxy，让 Thread 节点查询 LAN 上的服务。
- HA Container 应具备宿主机的 IPv6 路由和 mDNS 可见性，Linux 通常使用 host 网络。跨 VLAN 的路由、防火墙和服务发现需要网络侧配置。

**不是把 realm-local `ff13::…` 广播透传到 LAN。** 集成公告 `_esphome-tgw._udp.local.`，TXT 为 `v=3`、`id=<backend_id>`，SRV 提供监听端口，AAAA 提供 HA 地址。节点解析后使用 IPv6 单播发送 HELLO。节点端应缓存后端地址，仅在首次发现或端点失效时重新解析，避免每次 SED 唤醒都运行 DNS-SD。

## 接入与实体管理

进入集成的“配置”选项：

1. **接入新发现节点**：选择 HELLO 候选。不会自动绑定首个被发现的后端。HA 先持久保存接入决定，再发送绑定邀请；睡眠节点下次上线时继续完成，不必保持配置窗口打开。
2. **选择公开实体**：可以公开所有当前/未来实体，也可勾选固定实体列表。修改即时生效，不重载集成。完整新 manifest 到达后才执行清单差异，部分分片或错误哈希不会删除实体。
3. **解绑并移除节点**：本地立即撤销绑定并清理本集成的设备关联与实体，不等待节点 ACK。离线节点下次访问时收到已认证的撤销通知，恢复未绑定状态后需重新确认接入。

节点已被接入但尚未提交完整 manifest 时，不创建虚假实体。绑定已完成的节点可以重新建立会话、换地址、换 BR，无需重复接入。明确撤销的旧绑定不会被重传重新激活。

支持 15 种 wire 实体，映射到 14 个 HA 平台：

| 节点实体 | HA 平台 |
|---|---|
| numeric、text | sensor（数值/文本） |
| binary | binary_sensor |
| event | event |
| switch、number、select、button | 同名平台 |
| input_text | text |
| light、fan、cover、climate、lock、valve | 同名平台 |

实体 `unique_id` 为 `<工厂派生稳定设备ID>_<实体ID>`，不包含 BR、IP、Thread 网络名称或后端地址。元数据更新保留同平台的注册身份和用户名称；实体类型跨平台变更需要移除旧平台实体并创建新平台实体，HA 的旧 entity_id 和自动化引用不保证自动迁移。选择移除实体会清理注册记录；随后重新添加同一实体也不承诺保留已删除的用户设置。

状态以节点上报为准，不乐观修改。控制等待节点 `command_result`，失败、超时或上下文改变时给出明确错误。每节点串行一个待确认命令，最多发送三次，使用同一 command_id；节点端必须去重执行。回执成功表示节点接受了控制，不表示机械动作已经物理完成。

SED 只有明确的 `control_ready` 接收窗口内可控制；不自动排队危险控制，也不会假设 HA 能唤醒无线关闭的节点。SED 声明 `sleep_for_s` 后，在预期休眠时段保留状态可用性；未声明时只保留 300 秒宽限。REED 按声明心跳的三个周期判断离线。

## 持久化、认证与限制

HA `.storage/openthread_gateway.<entry_id>` 保存：稳定后端身份、绑定、待完成接入、绑定代数、撤销记录、已确认 manifest、实体选择和最近普通状态。绑定/清单/解绑立即持久化，普通状态延迟合并写入；事件不作为状态持久化或重播。HA 重启后业务上下文重建，已保存实体先显示不可用，节点重新确认会话并上报后恢复。状态写入磁盘不是每个报告 ACK 的承诺。

后续通信采用绑定密钥 HMAC-SHA256，验证会话、上下文及来源端点。**初次绑定密钥通过管理员确认后的未加密邀请下发，依赖可信 Thread/LAN 环境，不提供防恶意 LAN 中间人的首次配对保证。** HMAC 不加密报文。本版本不是互联网远程接入协议。绑定密钥保存在 HA 私有存储中，诊断导出不包含密钥、绑定令牌、原始报文、命令内容或传感器值；备份 HA 配置时应保护 `.storage`。

安全边界：最多 256 个节点、128 个候选、每节点 256 个实体、1024 条撤销记录；这些是运行时资源防护，不是预分配实体容量。撤销记录不会自动按时间删除，容量耗尽会明确拒绝继续解绑，不能悄悄忘记旧授权。UDP 包限制 1232 字节；完整清单和未完成报告有内存上限。

一个 HA 实例仍是业务后端单点；本实现消除的是“某台 BR 的网关归属和存储”这一单点。同一 Thread 网络的多 BR 路由冗余需要网络实测，不承诺零丢包切换。不同 Thread 网络仍各自需要可用无线父节点。节点保留有效 HA 绑定时，允许经新网络重新完成签名会话确认；若节点的迁网策略主动清除绑定，或要切换到另一 HA 后端，则需明确解除旧授权后重新人工接入。

HA 的“重新配置”用于修改地址、端口、名称，这类网络设置会重载集成；节点和实体管理不会。删除整个集成会释放监听端口，但不会向所有节点逐一撤销绑定；需要先解绑节点，或在节点上执行本地 unbind。不要丢失账本后使用同一个 backend_id 假装恢复原有授权。

## 验证与模拟

从项目根目录运行纯协议及实体适配契约测试（Python 3.12+，标准库）：

```sh
python -m unittest discover -s openthread_gateway/tests -v
```

包含真实 IPv6 loopback UDP 测试，不访问物理设备。实体层测试使用明确的 HA stand-ins 验证动态注册和控制映射，**不等于已在真实 HA 实例加载、通过 hassfest 或完成无线联调**。

模拟节点可直接对真实 HA 集成发送 v3 报文：

```sh
python openthread_gateway/tools/simulate_node.py --host <HA的IPv6地址> --backend-id <HA配置的后端ID>
```

随后在 HA 配置选项中接入“Protocol demo”。模拟器保存自己的绑定信息，可发送数值/二进制状态，并接受 switch 控制。它不替代节点固件适配，不会访问或修改 BR。

本轮未编译 ESPHome 固件、未刷机。待完成：两个 REED transporter v3 适配、真实 HA 加载与 UI 联调、Thread DNS-SD 查询、OMR 路由和多 BR 故障切换、长休眠及断电恢复实测。

实现参考：[HA 实体生命周期](https://contributors.home-assistant.io/core/entities/lifecycle/)、[实体注册表](https://contributors.home-assistant.io/core/integrations/entity-registry/)、[配置条目](https://contributors.home-assistant.io/core/integrations/config-entries/)。
