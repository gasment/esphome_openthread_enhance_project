<!-- ---

Thread Gateway 与四类 Transporter 已升级到不兼容 v1 的公共发现/绑定协议，参见 [逻辑变更与带注释 YAML diff](TRANSPORT_V2_CHANGELOG.md)。
# https://vitepress.dev/reference/default-theme-home-page
layout: home

hero:
  name: "ESPHome Openthread 扩展版"
  text: ""
  tagline: 适用于ESP32 & nRF52平台
  actions:
    - theme: brand
      text: Markdown Examples
      link: /markdown-examples
    - theme: alt
      text: API Examples
      link: /api-examples

features:
  - title: Feature A
    details: Lorem ipsum dolor sit amet, consectetur adipiscing elit
  - title: Feature B
    details: Lorem ipsum dolor sit amet, consectetur adipiscing elit
  - title: Feature C
    details: Lorem ipsum dolor sit amet, consectetur adipiscing elit
---

 -->
# ESPHome OpenThread & Thread BR <br>Enhance Project

* 此项目基于esphome为底板，依托esphome强大且方便集成的组件架构，拓展原有的OpenThread和OpenThread BR边缘路由功能，使其适应更多使用场景

* esphome在2026.11.0 dev版开始支持Thread Border Router组件，如只需基础BR功能，可以等待官方组件正式上线

* 乐鑫官方提供了ESP Thread Border Router/Zigbee Gateway的固件方案，你可以选择官方固件实现Thread BR功能

## 目前实现的功能
基于测试版本：ESPHome 2026.10.1 / ESP-IDF 5.5.6 /Zephyr 2.9.2
|项目|平台适配|功能|
| :----------------------------------------------------: | :----------------------------------------------: | :---------------------------------------------: |
|Thread边界路由|✅ESP32 ❌nRF52|负责Thread内外部的通讯转换|
|Thread网关|✅ESP32 ❌nRF52|负责管理Thread节点并代理HA实体|
|Thread节点-API模式|✅ESP32 ✅nRF52|接入Thread网络并直接连接到HA|
|Thread节点-网关模式|✅ESP32 ✅nRF52|接入Thread网络并通过Thread网关代理HA实体|
|Thread节点-P2P|✅ESP32 ✅nRF52|Thread节点间的应用层点对点通讯|
|Thread节点-访问LAN/WAN|✅ESP32 ❌nRF52|Thread节点可访问外部网络资源|
|BR Web 管理台| ✅Docker✅Python|自动发现并集中登录局域网内的BR|


## nRF52平台额外功能

|项目|功能|
| :----------------------------------------------------: | :---------------------------------------------: |
|深度睡眠扩展|拓展官方deepsleep组件，以支持完善的休眠功能|
|RTT日志|实现SWD烧录+日志双功能|
|无线OTA&日志|通过Thread网络进行OTA和日志读取|
|UART配置|修复nRF52平台缺失的UART组件功能|
|电池电量|通过VDD电压估算电池电量|


## 使用限制
* esphome版本不低于2026.9.1，高版本需要自行验证，如esphome版本更新导致编译与运行问题，建议退回到示列版本2026.10.1，也可等待项目更新修复
* 本项目未经大规模部署验证，仅用于学习研究与个人使用
* 本项目内各组件高度耦合，必须同一套使用，不能混用其他类似组件
* 本项目不考虑适配Thread市场商品，只针对运行esphome的ESP32 / nRF52840 设备

## 使用指导
参阅文档：https://esphome-openthread-enhance-pj.netlify.app/

## HA 业务网关集成

独立的 [OpenThread Gateway HA 集成](openthread_gateway/README.md)，提供 v3 IPv6 UDP 后端、人工接入及动态实体管理。ESP32 和 nRF52 SED 已增加 v3 适配（待编译上板），两个 REED transporter 仍待迁移。详见 [SED YAML diff](openthread_gateway/SED_MIGRATION.md)。

集成运行文件位于 `custom_components/openthread_gateway`，本仓库根目录提供 `hacs.json`，可按 Integration 类型添加到 HACS / Marketplace。安装后重启 HA，再添加 OpenThread Gateway 集成。详见 [安装说明](openthread_gateway/README.md)和[发布说明](openthread_gateway/PUBLISHING.md)。
