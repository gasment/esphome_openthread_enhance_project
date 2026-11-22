# ESPHome OpenThread & Thread BR <br>Enhance Project

* 此项目基于esphome为底板，依托esphome强大且方便集成的组件架构，拓展原有的OpenThread和OpenThread BR边缘路由功能，使其适应更多使用场景

* esphome在2026.11.0 dev版开始支持Thread Border Router组件，如只需基础BR功能，可以等待官方组件正式上线

* 乐鑫官方提供了ESP Thread Border Router/Zigbee Gateway的固件方案，你可以选择官方固件实现Thread BR功能

## 目前实现的功能
基于测试版本：ESPHome 2026.10.2 / ESP-IDF 5.5.6 /Zephyr 2.9.2
|项目|平台适配|功能|
| :----------------------------------------------------: | :----------------------------------------------: | :---------------------------------------------: |
|[Thread边界路由](Thread边界路由/Thread边界路由.md)|✅ESP32 ❌nRF52|负责Thread内外部的通讯转换|
|[Thread代理](Thread代理/Thread代理.md)|✅HA 2026.11+<br>✅ESP32 ❌nRF52|负责转发Thread节点消息并代理HA实体|
|[Thread节点-API模式](Thread节点/API模式.md)|✅ESP32 ✅nRF52|接入Thread网络并直接连接到HA|
|[Thread节点-代理模式](Thread节点/代理模式.md)|✅ESP32 ✅nRF52|接入Thread网络并通过Thread BR代理转发到HA|
|[Thread节点-P2P](Thread节点/P2P接收端.md)|✅ESP32 ✅nRF52|Thread节点间的应用层点对点通讯|
|Thread节点-访问|✅ESP32 ❌nRF52|Thread节点可访问外部网络资源|
|[BR Web 管理台](Thread边界路由/BR_Web管理台.md)| ✅Docker✅Python|自动发现并集中登录局域网内的BR|
::: warning
本项目两个平台的组件路径和组件名有所区分，esp32为原始组件名，nrf52则添加`_nrf52`后缀，如文档某些部分遗留指明，请手动为nrf52相关组件添加`_nrf52`后缀
:::
## nRF52平台额外功能

|项目|功能|
| :----------------------------------------------------: | :---------------------------------------------: |
|[深度睡眠扩展](/nRF52平台额外功能/深度睡眠扩展)|拓展官方deepsleep组件，以支持完善的休眠功能|
|[板级预配置 & RTT日志](/nRF52平台额外功能/板级预配置%20&%20RTT日志)|实现SWD烧录+日志双功能|
|[无线OTA&日志](/nRF52平台额外功能/无线OTA)|通过Thread网络进行OTA和日志读取|
|[双路I2C & UART配置](/nRF52平台额外功能/I2C与UART)|修复nRF52平台缺失的UART组件功能|
|[电池电量](/nRF52平台额外功能/电池电量)|通过VDD电压估算电池电量|


## 使用限制
* esphome版本不低于2026.10.2，高版本需要自行验证，如esphome版本更新导致编译与运行问题，建议退回到示列版本2026.10.2，也可等待项目更新修复
* 本项目未经大规模部署验证，仅用于学习研究与个人使用
* 本项目内各组件高度耦合，必须同一套使用，不能混用其他类似组件
* 本项目不考虑适配Thread市场商品，只针对运行esphome的ESP32 / nRF52840 设备

## 使用指导
参阅文档：https://esphome-openthread-enhance-pj.netlify.app/