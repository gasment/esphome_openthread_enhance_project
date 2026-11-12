# 发布 HACS / Marketplace 集成

当前项目根目录就是 HACS / Marketplace 仓库根目录，不需要单独的 `Integration` 层级，也无需另建仓库。运行文件、安装元数据和 GitHub 校验工作流位于根目录约定位置；文档、测试和模拟工具统一放在 `openthread_gateway/`。

```text
<repository>/
  hacs.json
  README.md
  LICENSE
  .github/workflows/validate.yml
  custom_components/openthread_gateway/
    manifest.json
    __init__.py
    translations/
    brand/icon.png
  openthread_gateway/
    README.md
    PROTOCOL.md
    PUBLISHING.md
    tests/
    tools/
  components/
  tools/
```

## 首次发布

1. 使用当前公开 GitHub 仓库，启用 Issues；建议 Topics 包含 `home-assistant`、`hacs`、`openthread`。
2. manifest 的文档链接指向本仓库 `dev/openthread_gateway`，问题链接指向本仓库 Issues。若更换仓库或文档分支，同步修改 `documentation`、`issue_tracker`；维护者不同则更新 `codeowners`。
3. 推送这些文件，查看 Actions 中 Tests、hassfest、HACS 的结果。本地单元测试通过不代表这两个官方校验已通过。
4. 创建 GitHub Release，首版 tag 使用 `0.1.0`，与 manifest 的 `version` 一致。后续先同步修改版本，再发布相同 tag 的 Release。无需额外上传 ZIP，HACS / Marketplace 从仓库标准目录安装运行文件。
5. 在 HACS / Marketplace 中添加 `https://github.com/gasment/esphome_openthread_enhance_project`，类型选择 Integration，安装后重启 HA。尚无 Release 时 HACS 可以使用默认分支；建议日常更新发布正式 Release。

`hacs.json` 的最低 HA 版本为 `2026.6.0`。配置流程支持 HA 2026.6 使用的 `voluptuous`，同时保留新版 HA 的 `probatio` 支持，不额外安装两种库。最低版本声明和静态 API 核对不替代真实 HA 加载测试。

## 校验与范围

在项目根目录运行：

```sh
python -m unittest discover -s openthread_gateway/tests -v
```

工作流在项目根目录运行，Python 测试不依赖 ESPHome 编译环境。测试与模拟器也从 `custom_components/openthread_gateway` 加载纯协议模块，不导入集成入口。运行时文件全部位于集成目录，测试、模拟工具和文档不会复制进 HA。

本轮未在真实 HA 中加载，也未执行 GitHub 上的 HACS / hassfest 校验；工作流需推送后运行。没有编译 ESPHome 固件。现有四类 transporter 仍需按 PROTOCOL.md 适配 v3 后才能接入。

## 默认目录与图标

自定义仓库安装和默认目录收录是两个步骤。发布公开仓库后即可手动添加；需要让用户在目录中直接搜索到，再按官方流程提交目录收录，不能把准备好文件视为已经上架。

新版 HA 使用集成内 `brand/icon.png`；这里附带原创通用网状节点图标。旧版 HACS 文档另有 `home-assistant/brands` 注册要求，申请对应目录时应按其当时的校验结果补充，不以本地 PNG 视为已完成远端品牌注册。

用户绑定与状态存放在 HA 的 `.storage`，位于集成安装目录之外，正常更新不会覆盖它们，不需要配置 `persistent_directory`。

参考：[Marketplace 仓库结构](https://contributors.home-assistant.io/marketplace/reference/repository-structure/)、[发布自定义集成](https://contributors.home-assistant.io/marketplace/guides/integrations/)、[HACS 集成要求](https://hacs.dev/docs/publish/integration/)。

## Repository structure is not compliant

例如 `Repository structure for v2026.11.10 is not compliant` 中的 `v2026.11.10` 是 HACS 选中的仓库 tag/ref，不是最低 HA 版本。这条错误表示该 ref 下找不到可安装的 `custom_components/<domain>/` 目录。

如果旧 Release 仍只有 ESPHome 组件或旧的 Integration 子目录，单独修改默认分支上的 `hacs.json` 无法修复旧 Release：先将当前目录结构推送，再让 HACS 下载包含新结构的分支或新 Release。推荐发布一个新的正式 Release，tag 与集成 manifest 的版本保持一致；已存在其他项目 Release 时，要核对 HACS 实际选中的版本，不要覆盖旧 tag。

参考：[HACS 对目录结构的检查源码](https://github.com/hacs/integration/blob/main/custom_components/hacs/repositories/integration.py)、[HA 2026.6 配置流程源码](https://github.com/home-assistant/core/blob/2026.6.0/homeassistant/config_entries.py)。
