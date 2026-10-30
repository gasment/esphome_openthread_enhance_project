# thread_entity_bridge_nrf52

供 nRF52 transporter 使用的独立实体适配层，由 `thread_reed_transporter_nrf52`
自动加载。需要在 external_components 的显式组件列表中包含本目录。
基线 ESPHome 2026.9.1 / NCS 2.9.2；来源快照见 `upstream.json`。

`entity_bridge.h/.cpp` 只修改独立命名空间，保留原业务逻辑与条件编译结构：

- 14 类本地实体的 Endpoint、描述、状态读取、变化订阅及标准控制调用。
- JSON 能力和值校验，元数据和协议兼容字段。
- 原 `USE_THREAD_GATEWAY` 条件下的代理代码也保留，节点未定义该宏，不启用这些代理。

Python 配置接入 NCS 的 `CONFIG_CJSON_LIB` 并要求浮点 printf，避免依赖 IDF
json 或引入 ArduinoJson。使用 cJSON 默认 malloc/free；不调用 `cJSON_Init`
切换到 Nordic cJSON_os 的 k_malloc 钩子，也不修改其他组件的全局分配方式。
不依赖 ESP-IDF、FreeRTOS、OpenThread 实例、DNS 或存储。

源实体的名称、单位、图标、traits 和状态决定传输内容；本组件不会重新定义实体、
主动采样或改变 filters。仅在相应 `USE_*` 启用时编入实体类型支持。
具体硬件驱动的平台支持仍需由该驱动确认。

与 REED 的配置、协议及验证说明见 [REED 文档](../thread_reed_transporter_nrf52/README.md)。
原桥与本桥不能同时加载；独立版本不在运行时引用原项目源码。
