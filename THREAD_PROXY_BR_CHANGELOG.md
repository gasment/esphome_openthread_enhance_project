# ESP32 Thread Proxy BR 管理端第一版

- 新增 `components/esp32/thread_proxy_api`，依赖现有 `thread_br`，仅 ESP32/ESP-IDF。
- 独立 TCP 6065，可自定义；专用 `_thread-proxy._tcp` 公告，HA 确认接入或手工绑定。
- 明文默认、Noise 可选；与官方 api 的端口、上下文、类与编译宏隔离，不加载官方 api，不复用实体或控制处理。
- 完成 Hello、DeviceInfo、管理能力/网络状态订阅、主动与被动 Ping、超时清理、连接数与缓冲上限及监听重试。
- Thread 网络元数据使用独立任务读取，避免 Spinel 阻塞主循环；不修改 thread_br 或 H2 RCP 的现有行为。
- HA 诊断属性新增 Thread 可用、附着状态和角色。
- 节点 UDP、本地 ACK、业务队列与 HA 聚合仍待实现；本版 data_plane_ready=false。

## YAML diff

```diff
 external_components:
   - source: github://gasment/esphome_openthread_enhance_project@dev
-    components: [thread_br]
+    components: [thread_br, thread_proxy_api] # 并入现有外部组件列表

+thread_proxy_api:
+  thread_br_id: thread_br_main # 指向已有 BR
+  # port: 6065                # 独立于官方 api 的 6053
+  # encryption:              # 需要加密时才添加整段
+  #   key: !secret thread_proxy_key

 # 若同时使用官方 API，但 HA 只连接 Thread Proxy：
 api:
+  reboot_timeout: 0s # 代理连接不算官方 API 连接；无需官方 API 时可省略 api
```

配置及内存代码生成验证：20 项通过，包括 S3 Wi-Fi+RCP、C5/C6 Ethernet+native、明文/Noise 共存组合及负向校验。C++ 静态语法和符号隔离检查通过。按 AGENTS 要求未运行编译命令，固件编译、mDNS/Noise 实机握手及长期连接待用户验证。

强制依赖已验证：缺少 thread_br 或引用不存在的 thread_br_id 均被拒绝；只配置一个 thread_br 时可自动解析父 ID。代理仅在 OT 锁空闲时采样，默认每 5 秒更新快照，未就绪时每秒检查；不修改基础 BR 源码、Dataset 或功能配置。

HA 联动验证：41 项测试通过，新增 Thread 可用/附着/角色字段测试，保留旧 v1 元数据兼容。固件未编译，不能将配置/静态验证视为已完成上板验证。
