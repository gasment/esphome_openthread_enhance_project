# Thread Proxy

面向 Home Assistant **2026.11+** 的 BR 代理管理集成。每个 BR 对应一个持久化配置条目；局域网发现只接受声明 `thread_proxy_api` 的设备，发现后必须由用户确认接入。

本版完成发现、绑定和 TCP API 连接管理。**ESP32 BR 固件组件 `thread_proxy_api` 已实现管理协议，尚未接入节点报文转发。** 固件配置参见 [BR 端说明](../components/esp32/thread_proxy_api/README.md)，编译及上板测试待执行。 现有 BR 的 `_meshcop` 或 ESPHome `_esphomelib` 公告不会触发接入。普通 API 端口可连通也不代表代理功能就绪。

## 安装与接入

`thread_proxy_api` 使用独立 TCP 监听端口，默认 **6065**，与官方 ESPHome API 的 6053 分开，可同时启用。自动发现使用 `_thread-proxy._tcp.local.` 的 SRV 端口，手工接入填写实际代理端口。已有保存为 6053 的试用绑定请重新配置为代理实际端口；不会静默改写已保存的自定义端口。


1. 将 `custom_components/thread_proxy` 复制到 HA 的 `/config/custom_components/thread_proxy`，或通过 HACS 自定义 Integration 仓库安装，重启 HA。
2. 在「设置 → 设备与服务 → 添加集成」搜索 **Thread Proxy**，手工输入 BR 地址、API 端口（默认 6065）、名称和可选 Noise 密钥。地址支持 IPv4、IPv6 和主机名。手工绑定可以离线保存。
3. 支持新协议的 BR 会出现在 HA 发现卡片中，确认后接入；也可以在集成详情页 **BR 接入管理** 页面选择发现的 BR 接入。不会自动绑定。
4. 集成详情页仅保留一个 **BR 接入管理** 按钮，点击进入现有管理页；页内可以发现接入、手工添加，并展示已绑定 BR、API 连接状态、代理协议验证状态、Thread 网络标识和错误原因，提供重新连接、解除绑定。已绑定设备不重复显示在待接入列表。
5. 地址、名称、密钥需要修改时，在 HA 集成条目菜单使用「重新配置」。未填写密钥保留原值；显式提交空字符串清除密钥。

管理页及其 HTTP 接口仅管理员可用。侧栏不再单独显示 Thread Proxy。密钥保存在 HA 配置条目中，不进入管理页状态接口、实体属性或诊断导出。明文 API 与可选 Noise 均支持，不强制加密。

## 状态含义

| 状态 | 含义 |
|---|---|
| 已绑定 | HA 已保存这个 BR 的接入配置，不要求当前在线 |
| API 已连接 | 已完成 BR 的 Native API 握手和 DeviceInfo 校验 |
| 代理协议已验证 | BR 通过 API 声明了匹配身份及版本的 `thread_proxy_api` 能力 |
| 未提供兼容代理协议 | TCP API 可连接，但未提供有效代理能力信息 |

每台 BR 另有三个诊断二元传感器（绑定、API 连接、代理验证）及一个状态传感器。离线自动重连，解绑只删除 HA 绑定，不修改固件或 Thread 网络。

**“代理协议已验证”仅表示管理协议验证通过。本版 `data_plane_ready` 始终为 false，不表示节点消息已可转发。**

## 从 Thread SED 迁移

这是新的集成域 `thread_proxy`，不把原有单节点条目转换成 BR，也不再监听旧节点 UDP 服务。请先备份；停用旧 Thread SED 条目后，安装新目录并添加 BR。旧集成源码保留在 `thread_sed/legacy_component` 用于回归与查阅，不由 HACS 安装、不随新集成加载。已装在 HA 上的旧目录不会被本次仓库重构自动删除。

现有 `thread_sed_api` 固件保持原样；本阶段不要求改节点 YAML。BR 固件已实现 [管理协议](PROTOCOL.md)，后续再独立实现节点 UDP 接收、本地 ACK、队列与 HA 上行聚合。下行控制不在当前设计内。

## 验证

`python -m unittest discover -s thread_proxy/tests -v`（依赖 `aioesphomeapi==46.25.0`、`aiohttp`、`voluptuous`）。测试使用 HA 轻量替身验证配置流程和 HTTP 权限，并使用真实 aioesphomeapi TCP/Protobuf 客户端测试连接；不替代 HA 2026.11 实机加载和 BR 上板测试。

固件配置验证（不编译）：`python thread_proxy/validate_firmware.py`，需在本项目使用的 ESPHome Python 环境运行。

界面更新至 0.5.3 后需重启 HA 并刷新浏览器。入口适配 HA 2026.11 的集成详情页，仅对 Thread Proxy 页面生效，不改写其他集成或 HA 全局翻译。

## BR 自身设备（0.5.3）

已绑定 BR 的“重连”左侧有“创建设备”复选框，默认勾选以保留已有行为，设置保存在各 BR 的 HA 配置条目选项中。取消时即时移除 **Thread Proxy 创建的该 BR 本身的诊断设备及四个诊断实体**；重新勾选恢复创建，无需重启 HA。

该选项不解绑 BR、不关闭 TCP 连接，也不卸载节点实体平台。代理接入的 SED/REED 节点使用独立身份，与 BR 本身的可选诊断设备分离，不能因取消此项被删除。也不会移除官方 ESPHome 集成单独创建的同一硬件设备。
