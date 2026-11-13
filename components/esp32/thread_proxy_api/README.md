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

已增加 Thread 内部 UDP 6064 接收、TSD2 分片重组、可选 AES-GCM、本地队列 ACK，以及通过独立 TCP API 向 HA 转发并等待 HA 确认。`data_plane_ready` 表示 Thread UDP 端点已附着可用。无业务下行控制。

默认无需增加 YAML。`thread_proxy_client`（ESP32）或 `thread_proxy_client_nrf52`（nRF52）在 Thread 内主动探测本 BR；不依赖节点 SRP 业务记录，不需要手填节点地址。探测与 ACK 使用 BR 的 Thread 地址，不经 HA 主机的 IPv6 下一跳路由。请先在 HA 中绑定参与代理的 BR，再更新节点。
```yaml
thread_proxy_api:
  thread_br_id: thread_br_main
  # 下列均可省略；默认 UDP 端口固定为 6064，与当前节点端一致。
  udp:
    queue_groups: 16    # 完整报文组数上限，1–128
    queue_bytes: 131072 # 完整报文载荷总字节上限，32768–1048576
    # encryption:
    #   key: !secret node_udp_key # 必须与节点端 encryption.key 一致
```

UDP 密钥与代理 TCP Noise 密钥相互独立；UDP 首版使用一把共享密钥，未配置时仅接收明文节点。所有接入该 BR 的加密节点必须使用这把 UDP 密钥。

**节点收到 ACK 表示报文已进入 BR 的有界 RAM 队列，不表示 HA 已更新实体。** HA 断线时继续缓存，队列满则不发成功 ACK；BR 断电/重启会丢失待转交报文。载荷缓冲优先使用 PSRAM，另有分片重组、接收队列、工作任务和去重表的内存开销。TCP 重连后使用同一投递标识重传，HA 跨 BR 去重。

HA 接收原始实体描述、状态及事件记录，按稳定节点 MAC 直接创建设备和只读实体，不走逐节点接入流程；同时保留接收缓存和 HA 事件。见 [功能变更与配置迁移](../../../CHANGELOG/CHANGELOG.md)。

## 验证

已在现有 ESPHome 2026.10.2 环境验证 S3 Wi-Fi＋RCP、C5/C6 Ethernet＋native、官方 API 共存及 Noise 组合的配置和内存代码生成，并检查 C++ 静态语法与符号隔离。**按项目要求未运行固件编译，编译及上板测试待用户执行。**

Native 帧缓冲/Noise 辅助代码从本仓库 ESPHome 派生的 `thread_sed_api` 抽取并隔离；没有复制其实体或会话逻辑；UDP 沿用其 TSD2 报文格式，也没有包含官方 `api/api_pb2.h`。轻量 Protobuf 编码用于管理和报文转发封装。

基础功能优先：仅在 OT 锁空闲时采样，默认每 5 秒更新快照，锁忙时跳过该轮；没有修改 thread_br 的 Dataset、路由、配网或 LED 实现。
