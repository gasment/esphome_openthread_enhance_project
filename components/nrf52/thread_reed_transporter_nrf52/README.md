# thread_reed_transporter_nrf52

独立的 nRF52/Zephyr 常在线实体 UDP transporter，基线为 ESPHome 2026.9.1、
NCS 2.9.2。依赖 `openthread_nrf52`，自动加载 `thread_entity_bridge_nrf52`。
原 ESP32/ESP-IDF 组件和网关保持不变，来源及原文件哈希见 `upstream.json`。

本组件引用现有实体 ID，实体自行采样、过滤、发布状态和运行本地自动化。
不依赖 Native API、DNS、RTC 或 Flash 存储，不主动调用 component.update。
必须配置 `openthread_nrf52.device_type: FTD`；实际 Child/Router/Leader 角色由
Thread 协议决定。当前工作区 `openthread.yaml` 已采用 FTD P2P/REED 共存配置；
MTD 节点不能直接启用本组件。

## 配置

可验证的完整配置为 [reed_ftd.yaml](../../examples/reed_ftd.yaml)，复用工作区
板卡和 Thread 凭据，并同时启用 FTD P2P，演示两种 UDP 业务共存。

```yaml
external_components:
  - source:
      type: local
      path: esphome_nrf52_thread/components
    components:
      - openthread_nrf52
      - openthread_info_nrf52
      - thread_reed_transporter_nrf52
      - thread_entity_bridge_nrf52

thread_reed_transporter_nrf52:
  id: node_transporter
  openthread_id: openthread_main
  node_id: nrf52-node
  name: nRF52 Node
  manifest_revision: 1
  # gateway_id: thread-br-2f
  discovery:
    multicast_address: ff13::8000:8562
    port: 18512
  heartbeat_interval: 10s
  batch_delay: 100ms
  full_sync_interval: 60s
  ack:
    timeout: 2s
    resend: 2
  sensors: [existing_sensor]
  switches: [existing_switch]
```

列表只能填写已定义的本地实体 ID，不接受二次 name、filters、lambda 配置。
支持 sensors、binary_sensors、text_sensors、switches、numbers、selects、
buttons、texts、lights、fans、covers、climates、locks、valves，共 14 类。
名称、单位、图标、分类和能力从源实体及 traits 读取，内部实体也可绑定。
具体实体驱动能否运行在 nRF52 上由相应驱动支持情况决定。

配置字段和默认值与原组件一致，只更改组件及自动化名称。
node_id 为稳定的 1..32 位 ASCII 字母、数字、下划线或连字符；名称运行时要求
1..96 个 UTF-8 字节。每节点最多 128 个 ID，不允许重复。
heartbeat_interval=1s..15s；batch_delay 最大 10s；full_sync_interval 最小 1s；
ack.timeout=100ms..60s，resend=0..10。实体名称、能力或列表修改后手动增加
manifest_revision；普通状态变化不需要增加。

## 与现有 ESP32 网关互通

发现多播地址和端口须与现有 `thread_gateway` 一致。gateway_id 可选，填写时须
匹配网关实际 ID。数据地址、端口及 TTL 从网关 offer 获取，节点使用 ML-EID
和临时 UDP 端口，无需像 P2P 接收端那样配置固定应用地址。

原 JSON v1 协议、字段和实体描述保持一致，`node_type` 仍为 `reed`，
不增加 nrf52 平台后缀。网关无须识别新平台或更改固件协议。

1. 多播 discover，校验 offer 请求编号、来源地址/端口、网关身份及 TTL。
2. presence 核对清单 revision；缺失或变化时发送分包 manifest。
3. 同步确认后上报完整有效状态，后续按 batch_delay 合并置脏实体。
4. state 分包整轮确认，重发保留 report_id、seq、报文与 generation 快照。
5. ACK 只清除尚未出现新变化的对应 generation；旧 ACK 不清除新状态。
6. 有效完整确认续期 TTL，心跳失联或 TTL 到期重新发现。

ACK 耗尽后每批次最多执行一次重新发现恢复，保留冻结报文；恢复最长等待
10s，仍失败时放弃当前批次并保留待同步状态。周期全量同步继续校正状态。
清单/状态最多 16 页，单包 UDP payload 最大 1232 字节，单实体不跨页。

控制调用源实体标准接口，实际状态由源实体发布。command_result.success 表示
控制入口已接受，不代表物理动作完成。最近 32 条命令结果用于重复请求去重；
同一网关重新发现保留结果缓存，节点重启则更换 session。

手动发现动作沿用原参数形式：

```yaml
- thread_reed_transporter_nrf52.discovery: node_transporter
# 或：
- thread_reed_transporter_nrf52.discovery:
    id: node_transporter
```

网关通讯状态可读 `id(node_transporter).is_gateway_connencted()`，保留原接口拼写。
它与 `thread_attach` 分别代表应用网关通讯及 Thread 附着状态。

## 平台实现

Zephyr `k_msgq` 保存最多 12 个报文指针，回调只检查长度、复制及非阻塞入队。
队满、分配或读取失败释放报文并累计丢包；JSON 处理、实体控制在主循环执行。
OpenThread 访问使用独立组件的 InstanceLock；NCS 2.9.2 的 UDP 接口标识为
`OT_NETIF_THREAD`。会话使用随机字节，发现编号使用 ESPHome 随机数接口。
关闭时在 OT 锁保护下停止接收，再释放报文；锁忙时由 teardown 重试。

实体桥启用 NCS 自带 `CJSON_LIB`，使用默认 malloc/free，不安装全局分配钩子。
当前 ESPHome nRF52 层启用 Newlib 和浮点 printf，满足 cJSON 的数值序列化依赖。
不使用 ESPHome 的 ArduinoJson 组件替代 cJSON。

内存包括接收报文、清单页、冻结状态页、命令结果、实体能力及 JSON 临时对象。
应先用少量实体上板，观察实际堆余量；128 实体/16 页是协议上限，不代表所有
nRF52840 配置都能以此容量运行。

## 验证

在项目根目录使用 ESPHome Python 环境运行 `python tests/validate_reed.py`。
覆盖 19 项配置/内存代码生成检查，包括全部 14 类实体、默认值、发现动作、
FTD 限制、ID 类型/存在性/重复、数量、时序参数及错误配置。
同时对照原实体桥 C++、JSON 处理和 ACK/TTL/恢复状态机源码，检查 NCS cJSON
及 UDP API 标识。示例验证覆盖 REED、P2P 和诊断共存。

本轮未编译或上板。源码/配置检查不能替代实际 C++ 编译及网关互通验证。
后续应验证发现、清单、各类状态/控制、重复命令、ACK 丢失、网关重启和重连。
