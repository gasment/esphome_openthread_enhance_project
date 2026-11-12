# Thread Proxy 管理协议 v1

本文是 HA 端已实现的管理接口契约；ESP32 BR 固件 `thread_proxy_api` 已实现此管理协议；编译及上板测试待执行。此处不定义节点报文与队列确认格式。

## 发现

仅 `thread_proxy_api` 注册 `_thread-proxy._tcp.local.` 服务。SRV 端口为独立的 thread_proxy_api TCP 监听端口（默认 6065，支持自定义），不复用官方 api 的 6053 监听器；A/AAAA 为 BR 在 LAN 上的可达地址。不得直接把普通 `_meshcop` 公告当作代理能力。

TXT 必填：`component=thread_proxy_api`、`version=1`、`id=<BR Native API DeviceInfo MAC，12位十六进制，无冒号>`；可选 `name`、`network_id`。名称未提供时使用 mDNS 主机名并去掉 `.local`。`network_id` 建议为 Active Dataset 的 Extended PAN ID，仅用于标记，不能替代稳定 BR ID，也不能证明两个 BR 的 Thread 无线分区互通。

HA 发现后发起确认流程，不静默创建绑定。已绑定 ID 再次公告时自动刷新地址/端口，保留名称和密钥。删除绑定后，可从仍有效的发现记录重新接入。

## TCP 管理会话

在独立代理监听器上复用 ESPHome Native API 帧及 Protobuf 消息作为当前管理协议，HA 为 TCP 客户端；协议帧复用不等于共享官方 api 服务端口或要求绑定官方 ESPHome 集成。BR 固件需独立实现 thread_proxy_api 服务，不能仅配置 api 代替。可选 Noise；未配置密钥时使用明文 Native API。HA 发送 Hello 和 DeviceInfoRequest，校验 DeviceInfo MAC 与绑定 ID 一致，然后发送 SubscribeHomeassistantServicesRequest（消息 34）。

BR 必须随后发送 HomeassistantActionRequest（消息 35，兼容旧命名 HomeassistantServiceResponse），`service=thread_proxy.info`，`is_event=false`，`data` 为字符串键值对：

```json
{"component":"thread_proxy_api","version":"1","id":"aabbccddeeff","network_id":"591cbc42f1a3afe5","partition_id":"12345678","node_count":"0","queue_size":"0"}
```

必填 component/version/id；其他可选。后续通过同类型消息 `service=thread_proxy.status` 更新状态。HA **仅消费元数据，不将任意 service 转发到 HA 服务总线**。收到无关服务消息忽略，不对未知节点业务数据发确认。

HA 等待管理声明 5 秒，未收到则标记 `unsupported / proxy_not_announced`，但接受之后到来的声明。Native API keepalive 15 秒；连接断开采用 2–60 秒退避并加抖动自动重连。发现和手工配置入口使用相同连接逻辑。

固定 BR ID 必须跨重启、地址变化保持不变；API 的 DeviceInfo MAC、TXT id 和声明 id 三者一致。发现协议 v1 不兼容时拒绝发现接入；手工绑定可保存，但显示协议不兼容。

本阶段所有状态响应显式带 `data_plane_ready=false`。将来节点经 BR 上报时，HA 按稳定节点 ID 聚合，不把 BR ID 当节点 ID；跨 BR 去重、缓存持久化和上行提交确认需要另行定义，不能用此处元数据验证冒充消息送达确认。

## ESP32 固件管理状态

`thread_available` 与 `thread_attached` 为字符串 `true`/`false`；`thread_role` 为 disabled/detached/child/router/leader。HA 保存为诊断状态属性。网络数据由后台任务读取；短暂 OT 锁竞争保留上次快照，BR 未就绪时清空网络数据。当前没有节点缓存，不发送虚构的 node_count/queue_size 数字。

固件每 30 秒（可配置）发送状态，收包空闲 15 秒主动 Ping，60 秒无入站流量关闭连接；未完成 Hello/订阅 10 秒关闭。客户端与固件各自可发起 Ping。服务没有自动重启 BR 的逻辑。若官方 api 同时配置且未被 HA 连接，应显式设置其 reboot_timeout: 0s。
