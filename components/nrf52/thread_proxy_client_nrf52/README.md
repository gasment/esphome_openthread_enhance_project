# thread_proxy_client_nrf52

nRF52 Thread 节点的只读上行组件，取代 `thread_sed_api` 的节点通信入口。适用于 SED、MED 和 REED；不建立 HA TCP 连接，不使用 Noise，不注册或查找节点 SRP 服务。

节点在 Thread 内主动探测 BR，确认后把实体描述、状态及事件通过 TSD2 UDP 上报；BR 入队即回复 ACK，再经独立 TCP 转交 HA。HA 从**已绑定 BR**收到节点描述后直接创建设备和只读实体，无逐节点发现、IP 填写或接入确认。

## 迁移

从此前 nRF52 的同名 `thread_proxy_client` 升级时，将 external_components 列表、顶层配置及 `.send` / `.is_ready` / `.can_sleep` 前缀统一改为 `thread_proxy_client_nrf52`。原组件 id、发送和 ACK 配置可保留。

先更新 Thread Proxy HA 集成并重启 HA，再更新、绑定参与代理的 BR，最后更新节点。停止旧 Thread SED 的 HA 直连探测。节点沿用现有 `openthread` / `openthread_nrf52` 配网设置。

```diff
 external_components:
   # source/path 保持原来对应平台的设置
-  # components: [..., thread_sed_api, ...]
+  # components: [..., thread_proxy_client_nrf52, ...]

-thread_sed_api:
+thread_proxy_client_nrf52:
   id: sed_api                 # 原 id 可保留，不必改所有 id(...) 引用
-  transport: udp              # 若原先有此项，删除；新组件仅支持 UDP
   send_mode: manual
   deep_sleep_id: sleep_control # 仅 nRF52 System ON；不使用时省略

 # 自动化中的前缀也要一起改：
-  # thread_sed_api.is_ready: sed_api
+  # thread_proxy_client_nrf52.is_ready: sed_api
-  # thread_sed_api.send:
+  # thread_proxy_client_nrf52.send:
```

默认 UDP 6064；BR 的 HA TCP 仍是 6065，不占用 ESPHome API 6053。不能与官方 `api` 或旧 `thread_sed_api` 同时配置。旧 `thread_sed_api` 已移除，需要迁移为新组件。

## 手动发送

```yaml
thread_proxy_client_nrf52:
  id: proxy_client
  send_mode: manual
  deep_sleep_id: sleep_control   # 仅 nRF52 System ON
  on_ready:
    - logger.log: "BR 已确认，初次描述同步完成"
  on_disconnect:
    - logger.log: "BR 投递失败或 Thread 断开，重新探测"

script:
  - id: report_once
    then:
      - component.update: sys_vdd  # send 只读取已发布状态，不代为采样
      - wait_until:
          condition:
            thread_proxy_client_nrf52.is_ready: proxy_client
          timeout: 3s
      - thread_proxy_client_nrf52.send:
          id: proxy_client
          entity_id: [sys_vdd, battery_level, key_event] # 单个 id 或多个；省略则全量
          ack_timeout: 500ms      # 每轮等待 ACK 的时限
          ack_resend: 2           # 额外重发次数；0 为不重发，最多三轮
          on_success:
            - logger.log: "BR 已接收，可继续休眠时序"
          on_error:
            - logger.log: "本次未获 BR 确认"
```

manual 不允许组件级 `ack_timeout` / `ack_resend` / `on_ack` / `on_ack_error`；这些策略与成功、失败动作都配置在单次 send 下。发送实体必须是非 internal 的状态/事件实体；实体采样时序由 YAML 控制。

## 自动发送

```yaml
thread_proxy_client_nrf52:
  id: proxy_client
  send_mode: auto
  deep_sleep_id: sleep_control   # 仅 nRF52 System ON
  ack_timeout: 500ms
  ack_resend: 2
  on_ack:
    - lambda: |-
        ESP_LOGD("proxy", "batch=%s ACK=%u ms pending=%d",
                 batch_id.c_str(), (unsigned) elapsed_ms, has_pending);
  on_ack_error:
    - lambda: |-
        ESP_LOGW("proxy", "batch=%s failed=%s", batch_id.c_str(), reason.c_str());
```

auto 由实体发布状态/事件触发批次发送，ACK 策略和回调只在组件级配置，不允许调用手动 send。沿用原组件的事件先入队、再触发自动化及快照截止序号逻辑。

## 连接、休眠和可选参数

- `is_ready`：已确认 BR 且当前可开始一次发送；描述交换、待确认批次和准备休眠期间为 false。
- `on_ready`：初次描述同步完成后触发；普通批次忙闲、System ON 休眠不会反复触发。`on_disconnect` 对应选中 BR 失效/Thread 断开，不表示 HA 是否在线。
- `can_sleep`：可配合手动休眠判断；配置 nRF52 `deep_sleep_id` 时自动加入休眠 guard，先结束在途发送，再恢复原 poll period。`sleep_ack_timeout` 默认10s，必须小于 deep_sleep_nrf52 的 prepare_timeout。
- `fast_poll_interval` 默认20ms；`batch_delay` 默认0ms。未传输前已配置的 Thread poll period 在进入 System ON 时恢复。
- 首次启动必须获得 BR 回复。保存的 BR 仅作为快速探测候选，不把旧 HA 地址或未经确认的缓存地址当作就绪连接。
- 默认使用首个有效 BR，保持当前 BR；ACK 超时/重新入网后重选。不是按 RSSI、距离或负载持续优选。

加密可选，未配置即明文：

```yaml
thread_proxy_client_nrf52:
  encryption:
    key: !secret node_udp_key   # 32 字节 Base64 AES-GCM 密钥
```

BR 上 `thread_proxy_api.udp.encryption.key` 必须使用相同密钥；BR 顶层 `encryption.key` 是另一条 TCP Noise 链路的独立密钥。HA 不保存节点 UDP 密钥。

## HA 实体

节点稳定 MAC 决定设备身份，切换 BR 不新建设备；BR 的“创建设备”选项只控制 BR 自身诊断。

| 节点实体 | HA 只读实体 |
|---|---|
| sensor / number | sensor，保留单位、设备类和适用的 state_class |
| binary_sensor | binary_sensor |
| text_sensor / select / text | sensor，显示文本 |
| event | event，保留 event_types 并按事件 token 去重 |
| switch / light / fan | binary_sensor，其他状态放属性 |
| cover / valve | sensor，开度百分比 |
| climate | sensor，当前温度，其他状态放属性 |
| lock / alarm_control_panel | sensor，状态文本 |

没有上行状态的 button 不创建；没有任何业务控制下行。实体描述、最新普通状态和事件去重游标在 HA 本地保存，重启可恢复；schema 更新会动态增删节点实体。首次事件如果早于实体注册完成，会先排队再发布。

**节点成功 ACK 只表示 BR RAM 已接收，不表示 HA 已更新或已落盘。BR 重启会丢失未转交缓存；HA 异常退出仍存在最近缓存写入窗口，不能承诺事件永久精确一次。**

本次仅运行配置/内存代码生成和协议、实体回归，未编译固件。待上板验证首次探测、SED 快速 ACK、加密、重新配网、BR 失效切换及多跳 Thread。


## 精简运行日志

DEBUG 下每次正常发送只保留一条完成摘要：

```text
[D][thread_proxy_client_nrf52]: send ACK: 182 ms, 114 B, rounds=1, group=563068012
```

auto 模式使用 `auto ACK`。耗时从该批次开始计算，含快照序列化及等待 ACK；B 为此次 UDP 组的原始载荷字节，rounds 为实际发送轮数（含首轮），group 可与 BR 日志对应。

发送开始、事件快照截止序号、目标/wake、逐包 ACK、每轮发送及 fast-poll 切换细节改为 VERBOSE；需要逐包排查时将编译日志级别设为 VERBOSE。首次 BR 连接保留 INFO，描述同步完成保留一条 DEBUG 摘要，最终失败保留 WARNING。deep_sleep 的唤醒/休眠和 YAML 自定义 logger.log 来自各自组件/自动化，可按需单独设置级别。


ESP32 平台继续使用 [thread_proxy_client](../../esp32/thread_proxy_client/README.md)。

### BR 不可达与补发

`send` 可在尚未发现 BR 时调用，等待发现后继续发送。可选组件配置 `discovery_timeout: 3s`（默认 3 秒，范围 1ms～60s）限制每次发现/重新发现的等待时间；描述同步和 ACK 另有时限。

在 ACK 重试额度内，BR 不可达会触发重新发现；选中可用 BR 后，继续补发尚未确认的批次。已经生成的批次保留内容、group 和事件 token，切换 BR 不重置已消耗的发送轮次。`ack_resend` 仍表示额外发送次数，0 不重发；auto 使用组件策略，manual 使用本次 send 的策略。

同一次 send 仅触发一次成功或失败回调。发现超时或重试额度耗尽后，该动作结束，不在失败回调后继续偷偷发送；未确认事件保留供后续 send，普通状态由后续快照同步。缓存仅在 RAM 中，重启会丢失。若使用休眠 guard，`prepare_timeout` 需覆盖配置的发现、描述同步和 ACK 等待时间。
