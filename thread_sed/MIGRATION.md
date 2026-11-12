# UDP 迁移

0.4.0 保留 HA domain=thread_sed、节点组件名 thread_sed_api 和实体唯一 ID 规则。设备和 HA 集成需要一起更新。

```diff
 thread_sed_api:
   id: sed_api
+  transport: udp          # 默认改为 UDP；不再建立 TCP 会话
-  port: 6053
+  # port: 6064            # UDP 默认端口，通常省略
   send_mode: manual
   deep_sleep_id: sleep_control
-  # sleep_ack_timeout: 10s
+  # UDP 成功路径无需独立 sleep ACK；该项仅限制异常后台交付的休眠等待
   # encryption:
-  #   key: !secret noise_key
+  #   key: !secret node_key  # 格式不变；UDP 使用 AES-GCM，HA 填同一密钥
```

`thread_sed_api.send` 保留 id、entity_id、on_success、on_error，仍不代为采样。ACK 选项变为：

```diff
 - thread_sed_api.send:
     id: sed_api
     entity_id: [battery_level, key_event]
-    timeout: 2s
+    ack_timeout: 500ms  # 每轮等待时间；不再是整个 action 的总截止
+    ack_resend: 2       # 额外重发次数，0 表示不重发
     on_success:
       - deep_sleep_nrf52.enter: sleep_control
```

auto 将 `auto_ack_timeout` 改为组件级 `ack_timeout`，可增加组件级 `ack_resend`，回调仍为组件级 on_ack/on_ack_error。manual 只能使用 action 级策略和回调，不接受组件级 ACK 项；auto 不能调用 send action。具体示例和默认值见 [README.md](README.md)。

HA 操作：

1. 复制新版 custom_components/thread_sed，重启 HA。
2. 保持节点唤醒，更新设备组件并刷入 UDP 固件。
3. 在原 Thread SED 条目的“重新配置”中设置 udp、节点 IPv6/主机名、端口 6064，保留或填写对应密钥；流程验证原 MAC 后重载同一条目。无需删除实体。
4. 允许 HA 和节点之间的 IPv6 UDP 6064。发现使用 _thread-sed._udp SRP/mDNS；Zeroconf 可以更新已知节点的端点和模式。

明文节点密钥留空。重新配置时未提交密钥字段会保留原密钥；显式提交空字符串会移除。需要回退时，两端一起选择 tcp 并恢复节点端口 6053。

与此前行为不同：UDP 的业务 ACK 不等待 HA 磁盘写入；它确认 HA 已处理，后台保存有短暂的掉电丢失窗口。首次接入、固件配置变更或 HA 缓存丢失时仍需补取实体描述，普通唤醒只发业务。

本地 D:/stair-light-bt-nrf52840/openthread.yaml 已增加 transport: udp。未运行固件编译，待上板验证。
