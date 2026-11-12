# Thread SED 迁移与 YAML diff

这是破坏性更新，不保留旧 UDP 协议、`manifest_revision`、绑定凭据、gateway_id、发现组播和实体桥配置。旧 HA 集成需手动移除；新集成按节点重新接入，旧实体 ID/历史不会自动合并。

## nRF52 SED

```diff
 external_components:
   - source: ...
-    components: [openthread_nrf52, deep_sleep_nrf52, thread_sed_transporter_nrf52, thread_entity_bridge_nrf52]
+    components: [openthread_nrf52, deep_sleep_nrf52, thread_sed_api]

-thread_sed_transporter_nrf52:
-  id: sed_transport
-  openthread_id: openthread_main
-  node_id: ${project_name}
-  name: ${friendly_name}
-  manifest_revision: 12
-  discovery:
-    gateway_id: thread-br-gw-2f-mainbedroom
-    multicast_address: ff13::8000:1234
-    port: 12138
-    timeout: 1s
-  deep_sleep_id: sleep_control
-  control_ready: !lambda return id(keep_awake).state;
-  heartbeat_interval: 10s
-  sensors:
-    - id: sys_vdd
-      delta: 0.02
-  text_sensors:
-    - id: thread_identity
-      max_length: 42
-  events:
-    - id: key_event
+thread_sed_api:
+  id: sed_api
+  encryption:
+    key: !secret thread_sed_noise_key  # 独立的 32 字节 base64 Noise key，接入 HA 时输入
+  deep_sleep_id: sleep_control        # 自动加入原 deep_sleep_nrf52 的休眠 guard

 # openthread_nrf52、deep_sleep_nrf52 和实体定义继续保留。
 # 名称使用 esphome.name / friendly_name；设备身份由原生 MAC 标识。
```

旧配置若已经使用 `backend`，同样整体删除 transporter 块，不再迁移其中的 id / PSK。旧 `thread_entity_bridge_nrf52:`、`api:` 以及旧 transporter.send / prepare_sleep 等自动化也需删除；不要只修改 external_components 名单。

```yaml
network:
  enable_ipv6: true

esphome:
  name: my-thread-sed
  friendly_name: My Thread SED

thread_sed_api:
  id: sed_api
  encryption:
    key: !secret thread_sed_noise_key
  deep_sleep_id: sleep_control
  # 可省略：默认 10s，必须短于 deep_sleep_nrf52.prepare_timeout。
  # sleep_ack_timeout: 10s
  # 可省略：默认 20ms，范围 10ms～1s，只缩短轮询、不切换 Thread 模式。
  # fast_poll_interval: 20ms
```

原来的 `control_ready` 不再由 transporter 管理，改用已有 `deep_sleep_nrf52.prevent` / `allow` 或唤醒锁管理唤醒窗口。旧 `delta` 改放入原实体的 `filters`，例如 `filters: [{delta: 0.02}]`；这会影响实体自身发布和本地自动化，请按用途选择。旧心跳由原生连接、设备唤醒与休眠确认代替。

## REED（ESP32 / nRF52）

```diff
-thread_reed_transporter:             # nRF52 原名为 thread_reed_transporter_nrf52
-  ...
-thread_entity_bridge:                # nRF52 原名为 thread_entity_bridge_nrf52
-  ...
+thread_sed_api:                      # 两个平台的新组件名相同
+  encryption:
+    key: !secret thread_sed_noise_key
```

external_components 中同步移除旧名字、加入 `thread_sed_api`，保留本平台的 OpenThread 组件。常供电 REED 不配置 `deep_sleep_id`。

## ESP32 SED

与 REED 使用相同组件，但官方 ESP32 deep_sleep 没有本项目 nRF52 的 guard 接口，因此用脚本在进入休眠前等待条件；不要同时设置一个绕过脚本的自动 run_duration。

```yaml
thread_sed_api:
  id: sed_api
  encryption:
    key: !secret thread_sed_noise_key
  sleep_duration: 30min               # 告知 HA 的预期时长，与实际休眠时长一致

deep_sleep:
  id: sleep_control
  sleep_duration: 30min

script:
  - id: report_and_sleep
    then:
      - wait_until:
          condition:
            thread_sed_api.can_sleep:
              id: sed_api             # 启动 flush；HA 确认或达到默认 10s 上限后为 true
      - deep_sleep.enter: sleep_control
```

在现有采样/发布动作完成后执行此脚本。nRF52 使用自动 guard，无需增加这份脚本。

## BR 与 HA

```diff
 thread_br:
   ...                                # backbone/radio、配网、Web 等继续保留
-thread_gateway:
-  ...                                # 整个块删除，不再在 BR 上提供业务网关
```

HA 中移除旧 OpenThread Gateway 集成，安装 `custom_components/thread_sed`，通过发现卡片逐个接入。BR 无需配置 HA 的 Noise key，HA 宿主机必须有可路由 IPv6。防火墙放行节点 TCP 6053 和 HA TCP 6064。
