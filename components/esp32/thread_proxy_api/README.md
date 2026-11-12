# thread_proxy_api（ESP32）

为本仓库 `thread_br` 提供独立的 Thread Proxy 管理 API，适用于 Wi-Fi＋UART RCP 和 Ethernet＋native/UART RCP。仅 ESP32、ESP-IDF；不提供 nRF52 版本。

## 最小配置

在已有可用的 `thread_br` 配置中添加：

```yaml
external_components:
  - source: github://gasment/esphome_openthread_enhance_project@dev
    components: [thread_br, thread_proxy_api]
    # 如果已有 external_components，只需将 thread_proxy_api 加入原列表。

thread_proxy_api:
  thread_br_id: thread_br_main  # 填写现有 thread_br 的 id；只有一个 BR 时可省略
```

默认 TCP 端口 **6065**，默认明文，默认最多 2 个管理连接；不会自动加载官方 `api`。启用后发布 `_thread-proxy._tcp.local.`，Thread Proxy HA 集成发现后由用户确认接入。手工接入填写 **BR 的 LAN 地址与 6065 端口**，不填写 SED 地址。

可选参数：

```yaml
thread_proxy_api:
  id: proxy_main
  thread_br_id: thread_br_main
  port: 6065
  max_connections: 2       # 1–4；达到上限时拒绝新连接，不踢掉现有连接
  status_interval: 30s     # 1s–5min；与节点上报周期无关
  encryption:             # 整段省略即使用明文；与官方 api 的密钥相互独立
    key: !secret thread_proxy_key
```

密钥为 32 字节 Base64，拒绝全零。启用 Noise 后该监听器仅接受 Noise，不同时开放明文；HA 绑定填写对应代理密钥。密钥不写入 mDNS 或日志。

## 与官方 API 共存

官方 `api` 保留在 6053，代理在 6065；两者独立连接、命名空间、编译宏和 Noise 上下文。支持两者使用不同密钥，也支持任一侧使用明文。与官方 `api`、`web_server`、`thread_br.web` 端口相同时配置校验失败。

如果启用了官方 `api`、但 HA 仅连接 Thread Proxy，应在官方 API 配置中设：

```yaml
api:
  reboot_timeout: 0s  # 代理连接不计入官方 API 的连接数，避免官方 API 自身的离线重启
```

这项原有 API 行为不会被代理组件隐式改写。无需官方 API 时可直接省略 `api`。

## 本版实现范围

- 独立 TCP 管理服务、明文/Noise 帧、Hello、DeviceInfo、状态订阅、双向 Ping 和断线清理。
- 专用 mDNS 公告，SRV 使用实际端口；稳定 ID 为与 DeviceInfo 相同的 BR MAC，去除冒号。
- 上报网络 Extended PAN ID、分区 ID、Thread 角色及附着状态；RCP 查询在独立 FreeRTOS 任务执行，不阻塞 ESPHome 主循环。
- 10 秒握手/订阅时限、60 秒接收空闲时限、每轮限量处理、2048 字节接收帧上限、每连接最多 4 条发送积压；监听失败每 5 秒重试。
- 没有管理客户端时不会使 BR 重启；不会注册 BR 业务实体、调用 HA 服务或执行控制动作。

**节点 UDP 接收、本地 ACK、消息队列与 HA 上行聚合尚未实现，`data_plane_ready=false`。** 本组件现阶段完成 BR 的接入和连接管理，不能替代已有节点传输。不会给尚未转发的数据返回成功 ACK，也不提供下行控制。

## 验证

已在现有 ESPHome 2026.10.2 环境验证 S3 Wi-Fi＋RCP、C5/C6 Ethernet＋native、官方 API 共存及 Noise 组合的配置和内存代码生成，并检查 C++ 静态语法与符号隔离。**按项目要求未运行固件编译，编译及上板测试待用户执行。**

Native 帧缓冲/Noise 辅助代码从本仓库 ESPHome 派生的 `thread_sed_api` 抽取并隔离；没有复制其实体、会话或 UDP 通信逻辑，也没有包含官方 `api/api_pb2.h`。新增轻量 Protobuf 编码仅用于管理消息。

基础功能优先：仅在 OT 锁空闲时采样，默认每 5 秒更新快照，锁忙时跳过该轮；没有修改 thread_br 的 Dataset、路由、配网或 LED 实现。
