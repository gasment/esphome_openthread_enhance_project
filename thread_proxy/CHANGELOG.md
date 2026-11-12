# Thread Proxy 第一版（0.5.0）

- 将 HA 接入模型从 Thread SED 单节点改为 Thread Proxy 多 BR 绑定；独立集成域 `thread_proxy`。
- 仅发现声明 `thread_proxy_api` 的 BR。使用专用 mDNS 服务并要求用户确认接入；支持 IPv4/IPv6/主机名手工绑定，密钥可选。
- 每个 BR 一条持久化 HA 配置，固定身份校验、地址公告更新、重复绑定拦截、自动重连；解绑不修改 BR 配置。
- 新增管理员侧栏：已绑定/待接入列表、连接/代理验证状态、错误提示、重连与解绑；已绑定 BR 不在待接入重复显示。
- 提供 BR 诊断实体；状态接口和诊断导出不包含密钥。
- 原 HA Thread SED 源码归档到 `thread_sed/legacy_component`；HACS 仅安装新的 `thread_proxy` 组件。
- 本次不改 ESPHome YAML，不新增可编译的 `thread_proxy_api` 固件组件；节点转发、BR 本地 ACK 和多 BR 聚合待后续实现。本版不可替代旧版节点数据接入。

安装目录变化：

```diff
- /config/custom_components/thread_sed/    # 原单节点集成；迁移时先停用并备份
+ /config/custom_components/thread_proxy/  # 新 BR 管理集成，需重启 HA 加载
```

接入操作变化（HA 界面配置，不是 configuration.yaml）：

```diff
- Thread SED → 填写 SED 的 OMR 地址与节点 UDP 端口
+ Thread Proxy → 确认发现的 BR，或填写 BR 的 LAN 地址与独立代理 TCP 端口（默认 6065）
+ 名称：可手工指定
+ API 加密密钥：可选，BR 启用 Noise 时才填写
```

ESPHome YAML：**本阶段无变更**。`thread_proxy_api` 尚未实现，因此不要在设备 YAML 中提前添加该组件。

验证结果：新增 35 项测试、原有 79 项回归测试通过；已验证真实 Native API 明文/Noise 会话、身份检查与断线恢复，配置流程、权限与密钥脱敏使用 HA 轻量替身。桌面/手机管理页交互及 Python AST、JSON、单活动 HACS 目录结构检查通过。尚未在实际 HA 2026.11 实例和 BR 硬件联调，未运行固件编译。

端口修正：代理默认端口为 **6065**，独立于官方 ESPHome API 的 6053；自动发现以专用服务的 SRV 端口为准，保留手工自定义端口。旧试用绑定需手工重新配置代理端口，不自动重定向。
