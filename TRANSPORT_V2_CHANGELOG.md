# Thread Gateway / Transporter 协议 v2 更新日志

日期：2026-11-07。**破坏性更新，网关与节点需要配套升级，不兼容 v1 业务报文。**

适用组件：`thread_gateway`、`thread_sed_transporter`、`thread_sed_transporter_nrf52`、
`thread_reed_transporter`、`thread_reed_transporter_nrf52`。

## 一、逻辑变更

### 1. 内置公共发现与管理通道

- 所有组件使用内置 `ff13::8000:8562` / UDP `18512`，协议字段 `v: 2`。
- 不再允许配置发现组播地址或端口；公共入口不能被业务公告覆盖。
- 仅在当前 Thread 网络可达范围内工作；不同 Thread 网络不互通。
- `Joiner` 仍只负责加入 Thread。附着后公共接口可在新网络继续工作，无需知道新网关业务端口。
- 网关通过 `announcement` 声明稳定身份、业务 UDP 端口、地址缓存 TTL 和公告周期。
  业务 IPv6 地址来自运行中的 Mesh-Local EID，不能写死在节点绑定记录中。
- `address_ttl` 仅控制地址缓存，不控制绑定寿命。地址过期会寻找原归属网关，不自动换网关。

### 2. 固定目标与人工接入

- 节点配置目标 `gateway_id`：仅允许指定网关，在首次未绑定且未被撤销时自动完成指定目标接入。
- 节点未配置目标：不再接受首个网关自动接入；所有网关都可发现，在“新发现节点”中选择接入。
- 不增加 `selection` 字段，也不保留旧自动抢先接入模式。
- 点击接入先保存待处理事务。节点持久化归属并返回确认，网关才激活授权。
- 节点使用出厂 EUI-64 作为物理设备标识；同名 `node_id` 的物理设备冲突不会被静默覆盖。
- 节点串行处理绑定请求，已绑定时拒绝其他绑定。重复事务幂等处理。
- 接入授权与 `entity_auto_register` 独立：接入不等于所有实体自动加入 HA。
- SED 不为人工接入无限保持唤醒；页面可以显示等待节点下次唤醒确认。

### 3. 多网关补同步

- 绑定确认、撤销后通过公共通道通知。
- 网关重新附着时请求同步；公告携带归属摘要，不一致时请求补发。
- 补发一条归属记录一个报文，至少间隔 50ms，避免同一循环发送整张表。
- 已确认记录和撤销记录持久化。掉线的非归属网关恢复后可通过在线副本补齐。
- 绑定代次由节点持久化维护，用于归属排序；它与已移除的清单版本号无关。
- 撤销针对具体 `binding_id`，不会撤销节点后来建立的新绑定。
- 旧归属快照不能覆盖更高代次或已撤销的绑定；一致性是最终一致，网络分区期间不承诺页面瞬时一致。
- 所有最新副本均离线时无法知道最新结果；重新通信后补齐，不依据超时推断解绑。

### 4. 网关解绑与休眠节点解耦

- “解绑并移除节点”先持久化撤销记录，即刻停止旧授权的数据接收和控制。
- 在线节点尽可能立即收到通知；休眠节点下次发现或向旧网关上报时收到 `binding_revoked`。
- 节点清除对应绑定后重新进入发现/接入流程，不需要整机重启。
- 曾被撤销的固定目标节点也需要人工重新确认，避免删除后立即自动恢复授权。
- 旧 `manifest/state/presence` 不能再自行创建正式节点。
- HA 的 App 注册对象不能在运行中直接释放，继续沿用“保存后重启应用实体清单”的机制；授权撤销即时生效。
- 增加四端 `<组件名>.unbind` 自动化，用于旧网关不可达时从节点本地清除绑定。
  原 `.discovery` 动作仅重新查找当前归属网关，不等于解绑。
- 同一 Thread 网络内迁移采用“旧网关解绑 → 节点清除旧绑定 → 新网关确认接入”。
  本版不提供跨网关原子一键迁移或离线强制抢占。
- Thread Extended PAN ID、物理设备标识或配置的节点 ID 改变时，清除不再适用的本地绑定。
  显式配置的固定目标仍是限制条件；如需自由选择其他网关，应删除该配置。

### 5. 清单指纹替代 manifest_revision

- 删除 YAML `manifest_revision`，不再要求用户手工递增，不再判断清单数字版本大小。
- 四端对运行时完整实体描述计算 SHA-256。实体按 ID 排序，对象字段按名称排序；
  能力数组中的顺序保留，数字采用统一规范表示，包含节点显示名。
- 当前读数、分页方式、网关地址、绑定关系和网关侧批准设置不参与清单指纹。
- 新上下文先协商清单；网关有完整匹配缓存时复用，否则请求分片上传，重组并校验指纹后确认。
- 固件回退、迁移到保留旧缓存的网关，都不再触发“版本太小”或“未递增版本”错误。
- 业务报文携带 16 个十六进制字符的短 `ctx`，对应本次绑定、节点 session 和网关运行时上下文。
  清单分片/确认也验证上下文，迟到确认不能完成另一次同步。
- 完整 hash 主要用于清单协商和分片。正常上报/控制和已就绪心跳不重复发送完整 hash。
- 网关重启或上下文失效时重新发现/握手，不撤销持久化归属。
- 清单校验、容量或持久化失败不等于解绑。业务就绪与归属独立管理。
- 实体审批尽量按稳定实体 ID 和类型保留；HA 元数据变化仍需“重启并应用清单”。
- 保持 1232 字节应用报文上限，分片预算包含新增头部；超限报文不会发送。

### 6. 存储与容量边界

- ESP32 节点：独立 NVS 命名空间 `thread_bind_v2`，不写入 IDF 会截断键值的 OpenThread settings 空间。
- nRF52 节点：OpenThread/Zephyr settings 键 `0x8002`，避开 Joiner 日志的 `0x8001`。
- 绑定/撤销成功提交后才改变有效状态；读取损坏或写入失败时不假装授权成功。
- 业务地址、端口及上下文只缓存于 RAM，不随心跳写 Flash。
- 网关账本与实体注册表分开持久化；撤销账本优先于残留的实体缓存。
- 网关最多保存 256 条归属/待处理/撤销记录，候选节点最多 128 个。
  新节点确认状态可淘汰更旧的非撤销快照；撤销记录不按时间自动删除。
  到达账本上限时明确拒绝增加记录，不静默丢弃撤销历史。本版没有自动历史压缩/归档操作。
- 管理页面候选记录 30 分钟未再发现且没有待处理接入时过期；这不是解除绑定。
- 清单注册表升级到存储版本 2。旧 v1 注册表不会被当作 v2 授权使用，需要重新接入并选择实体。
  旧数据不会因读取失败直接擦除，但后续保存 v2 注册表会替换原注册表内容。
- 网关 ID、网络身份和节点存储应保持稳定。擦除所有副本或回滚绑定存储不属于正常迁移，需人工重新接入；
  保留历史记录的其他网关可能要求先处理归属冲突，不能仅靠超时自动覆盖。

## 二、带注释 YAML diff

### Gateway

```diff
 thread_gateway:
   id: thread_gateway_main
   thread_br_id: thread_br_main
   reed_heartbeat_misses: 3
-  gateway_id: "thread-br-gw-2f-mainbedroom"
   entity_auto_register: false

-  discovery:
-    multicast_address: "ff13::8000:1234"
-    port: 12138
+  announcement:  # 公共发现入口内置；这里声明本网关的业务参数
+    gateway_id: "thread-br-gw-2f-mainbedroom"  # 稳定且各网关唯一
+    udp_port: 12139  # 业务 UDP，不能与内置公共端口 18512 相同
     address_ttl: 30min  # 仅业务地址缓存 TTL，不是归属租期
     announce_interval: 60s  # 公告摘要周期；差异记录按需补同步
-  udp:
-    port: 12139
   capacity:
     # 原容量配置全部保留
```

### SED（ESP32 / nRF52 共用的字段变化）

保留固定目标时：

```diff
 thread_sed_transporter_nrf52:
   id: sed_transport
   openthread_id: openthread_main
   node_id: ${project_name}
   name: ${friendly_name}
-  manifest_revision: 12
+  # 清单指纹自动计算，实体描述变化不再手动修改版本号
   discovery:
     gateway_id: thread-br-gw-2f-mainbedroom  # 保留此行：仍限制为指定网关
-    multicast_address: "ff13::8000:1234"
-    port: 12138
     timeout: 1s  # 单次发现窗口，不是用户点击接入的等待时间
   deep_sleep_id: sleep_control
   control_ready: !lambda |-
     return id(keep_awake).state;
   heartbeat_interval: 10s
   sensors:
     # 原实体 ID / delta 保持不变
   text_sensors:
     # 原实体描述保持不变
   events:
     - id: key_event
```

允许人工选择任意网关时，再删除目标限制：

```diff
   discovery:
-    gateway_id: thread-br-gw-2f-mainbedroom
+    # 不填写 gateway_id：出现在各网关的“新发现节点”，等待确认
     timeout: 1s
```

ESP32 使用相同字段，组件名换成 `thread_sed_transporter`，其他实体、采集和休眠自动化无需因此改变。

### REED（ESP32 / nRF52）

REED 继续把可选固定目标放在顶层，区别于 SED 的 `discovery.gateway_id`：

```diff
 thread_reed_transporter_nrf52:
   id: reed_transport
   openthread_id: openthread_main
   node_id: ${project_name}
-  manifest_revision: 12
   gateway_id: thread-br-gw-2f-mainbedroom  # 可选；删除此行即改为人工接入
-  discovery:
-    multicast_address: "ff13::8000:1234"
-    port: 12138
   heartbeat_interval: 10s
   # 原 sensors / controls / ack / batch_delay 等配置保留
```

ESP32 对应组件名为 `thread_reed_transporter`。

### 节点本地解绑示例

```yaml
# 绑定到实际按键的长按等自动化；不要放到每次 on_boot 中执行。
then:
  - thread_sed_transporter_nrf52.unbind: sed_transport
# 另外三端分别使用：
# thread_sed_transporter.unbind
# thread_reed_transporter.unbind
# thread_reed_transporter_nrf52.unbind
```

这是清除归属并重新接入，不是整机重启；如果 YAML 固定了 gateway_id，解除后仍受该目标限制。

## 三、部署与验证

- 先迁移配置，再配套更新网关和四类节点；不能混用 v1/v2 transporter 和 gateway。
- 新接口位于原 Basic 认证保护下：`GET /thread_gateway/api/discovered`、
  `POST /thread_gateway/api/adopt`；原删除节点操作现在执行撤销授权并移除节点。
- 配置/内存内代码生成回归：`python tools/tests/test_transport_v2.py`，不调用编译器。
- 已通过 10 组自动检查，覆盖 29 个配置/内存内代码生成或拒绝场景、双平台公共协议头一致性、管理页面嵌入资源一致性。
- Python AST、17 个 C++ 文件的解析基线检查以及管理页面 JavaScript 语法检查通过。
  C++ 解析检查处理了 cJSON 循环宏并与原文件比较，不代替编译器的类型检查或运行测试。
- 本次不运行固件编译。nRF52840 编译仍按项目约定由用户在 WSL 环境执行。
- **待编译和上板验证**：双网关同时接入、丢 ACK、离线网关补同步、SED 睡眠期间解绑、
  网关重启、Flash 写入失败、Joiner 切换网络、固件清单回退、实体容量和实际报文/功耗。
- 公共通道依赖当前 Thread 网络的信任边界，不是跨网络全局目录，也不提供跨网关的强一致性。

### Web 资源打包方式（补充）

- 必须通过组件内的脚本生成嵌入资源，不能只修改 `frontend`：
  `python components/esp32/thread_br/build_web_assets.py`。
- 已修正脚本残留的旧目录路径，输入/输出都相对于脚本所在组件目录定位。
- 本次已使用该脚本重新生成 `thread_br_web/web_assets.h`，校验网关 HTML/JS 解压后与源码一致，重复打包结果一致。
- 此命令只打包 Web 静态资源，不编译固件；设备上的效果仍需后续更新固件确认。
