# Thread SED UDP 更新记录（2026-11-12）

## 逻辑变更

- thread_sed_api 默认改为 IPv6 UDP+批次 ACK；ESP32/nRF52 复用现有实体序列化和 send action，HA domain 和实体身份保持。
- 移除 UDP 成功路径的 TCP 建连、原生握手和单独休眠 ACK；普通唤醒直接业务上报，实体描述按指纹补取。
- 新增有界分片重组、ACK 重发、事件去重、命令 ACK/去重和唤醒标识校验；临时断网有发送及休眠截止，不无限阻止休眠。
- 可选 encryption.key 沿用 32 字节 Base64 格式，UDP 改为 AES-GCM；未配置时明文。
- HA ACK 改为“内存处理完成”，磁盘缓存后台合并；不再把磁盘写入延迟计入通信 ACK，也不承诺掉电持久化。
- 增加 HA 现有条目重新配置入口，可验证原 MAC 后改为 UDP；避免删除实体。mDNS 改为 _thread-sed._udp，UDP 6064。
- 保留 transport: tcp 用于回退；旧 TCP 相关测试继续覆盖。

## ACK 配置分层

- auto：组件级 ack_timeout / ack_resend 和 on_ack / on_ack_error；状态和事件共享批次 ACK，即使未配置回调也启用确认。不接受 send action。
- manual：组件级禁止上述四项；send action 使用 ack_timeout / ack_resend 和 on_success / on_error，支持不同 action 使用不同参数与 lambda。
- 默认每轮 500ms、额外重发 2 次。ACK 从最后一个分片发出开始计时；500ms、2 的名义 ACK 等待合计约 1.5s，不包括分片发出及首次描述同步。UDP 同组重发冻结数据，不重新采样，回调只完成一次。
- 移除 auto_ack_timeout 和 send.timeout，旧名称直接提示迁移，不做静默兼容。用户本地现有 send 已设置 500ms、3，保留该选择（最多四轮）。

## YAML diff（注释）

```diff
 thread_sed_api:
   id: sed_api
+  transport: udp             # 默认 UDP，显式标明此次迁移
-  port: 6053
+  # port: 6064               # UDP 默认值，无需填写
   send_mode: manual
   deep_sleep_id: sleep_control
   # send 保留 entity_id / on_success / on_error
@@
 - thread_sed_api.send:
     id: sed_api
-    timeout: 2s
+    ack_timeout: 500ms      # 每轮等待 ACK 时间
+    ack_resend: 2           # 额外重发次数，0 表示不重发
```

必须同步更新 HA 集成及设备组件。完整操作与 ACK 语义见 thread_sed/MIGRATION.md，协议格式见 thread_sed/PROTOCOL.md。

## 验证

配置/代码生成覆盖 ESP32/nRF52、UDP/TCP、auto/manual、明文/加密 16 组，加上两个无回调的 auto 默认值用例，共 18 组有效 ACK 配置；34 组错误作用域、旧字段及越界值均被拒绝。用户当前完整手动发送节点也已通过配置/代码生成验证。实体指纹在独立代码生成进程中一致，避免重复清单同步。

71 项 Python 测试通过，包含真实 IPv6 UDP 明文/AES-GCM 接入、重传、去重、描述补取、分片、睡眠通知、后台写盘、混合状态/事件批次以新报文序列号重发仍只处理一次，以及迁移同一条目和拒绝不同设备身份的逻辑测试。HA 配置流使用 stand-in，未在真实 HA 中部署验证。

未运行固件编译；新 UDP 固件的实际编译、上板通信延迟、丢包恢复和功耗待用户验证。
