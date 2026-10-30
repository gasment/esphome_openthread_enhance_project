# openthread_info_nrf52

`text_sensor` 平台提供 Thread 状态与地址信息。新增 `thread_identity` 用于 BR 从 HA 查找节点名称。

```yaml
text_sensor:
  - platform: openthread_info_nrf52
    thread_identity:
      id: node_identity
      name: "Thread Identity"  # 名称可任意修改
```

状态格式：`ot1:<ext_pan_id>:<ext_addr>:<rloc16>`，十六进制长度分别为 16、16、4，
均为小写、无 `0x`，共 42 字节。使用当前 Thread 扩展地址，不是工厂 EUI64。
默认每秒检查，只有状态改变时发布；未附着时清空，重新附着后发布当前身份。
身份查询及发布分开处理，`on_value` 在释放 OT 锁之后执行，可以安全调用发送 action。

直接连接 Native API 的节点无需额外发送配置。经网关代理的 SED 必须注册此实体，
文本长度至少为 42；可用变化事件自动发送，使身份更新不依赖普通传感器的手动上报批次：

```yaml
text_sensor:
  - platform: openthread_info_nrf52
    thread_identity:
      id: node_identity
      name: "Thread Identity"
      on_value:
        then:
          - if:
              condition:
                lambda: 'return !x.empty();'
              then:
                - thread_sed_transporter_nrf52.send:
                    id: sed_transport
                    entity_id: node_identity
                    ack:
                      timeout: 2s
                      resend: 2

thread_sed_transporter_nrf52:
  id: sed_transport
  # 其余 openthread_id、node_id、deep_sleep_id 等配置照旧
  text_sensors:
    - id: node_identity
      max_length: 42
```

未附着时无法通过 Thread 发送，上例跳过空值，在重新附着并取得当前身份后上报。
上例会走发送器既有的 ACK、全局回调和休眠保护；
若已有 `on_report_success/on_complete` 自动休眠逻辑，应一并考虑此身份上报，避免抢在业务上报前休眠。
建议绑定已有 `deep_sleep_id`，使待发送及待 ACK 阶段受休眠保护。
其他读取或批量发送动作仍由原有脚本控制。
