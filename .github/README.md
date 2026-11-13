# godot_ci

Godot 引擎与项目的 GitHub Actions CI：在云端编译 Godot 引擎、导出 Godot 项目、发布项目成品与引擎快照。接入即得四条链，所有参数收敛到项目仓库的一份契约文件——引擎来源与版本、目标平台、编译配置、自定义模块、导出预设都在契约里声明，改契约即切换构建形态，无需改任何工作流。SCons 编译缓存跨次复用，重复构建只做增量；四条链在关键节点提供扩展钩子，项目可注入自己的脚本（如发布前跑测试闸门、产出校验和）。

## 四条链

| 工作流 | 作用 |
| --- | --- |
| 🔨 Engine Build | 云端编译任意来源的 Godot 引擎（官方 tag / fork），支持 Linux / Windows / macOS / Android / iOS / visionOS / Web 七平台，可注入自定义 C++ 模块与编译配置 |
| 🏗️ Engine Release | 把编译产物固化为带 sha256 指纹的 Release 附件，按引擎版本多版本并存 |
| 📦 Project Export | 按项目的导出预设导出各平台成品，官方模板或与引擎同源的自编译模板可选，逐预设独立打包 |
| 🚀 Project Release | 推送 `v*` Tag 自动导出并发布为 GitHub Release：附件自动上传、变更说明自动生成、改名后的陈旧附件自动清理 |

## 接入

从 [Releases](https://github.com/JayWanX/godot_ci/releases) 的 **templates-latest** 下载 `godot_ci_trigger_templates_latest.zip` 解压到项目仓库根，提交推送后四个工作流即出现在 Actions 页——之后调参数只改契约文件，无需改任何工作流。需要锁定版本时下载对应 `v*` Release 的模板包；模板一有变更，templates-latest 会自动更新。扩展默认关闭，不配置也能跑通。

## 文档

| 文档 | 读者 | 内容 |
| --- | --- | --- |
| [templates/USAGE.md](templates/USAGE.md) | 接入者 / 日常使用者 | 接入三步、契约字段、来源声明语法、锁版本与搬家（随包落位项目仓库 `.github/CI-USAGE.md`） |
| [docs/workflows.md](docs/workflows.md) | 日常使用者 | 四链使用手册：触发方式、输入参数、产物命名与获取 |
| [docs/hooks.md](docs/hooks.md) | 钩子作者 | 扩展机制：锚点注册表、清单 schema、只读上下文、可写面、安全信任 |
| [docs/ci-architecture.md](docs/ci-architecture.md) | CI 维护者 | 架构判据与内部机制：边界、部署形态、职责划分、权限、引用语法、契约、缓存键、执行器 |
| [docs/maintenance.md](docs/maintenance.md) | CI 维护者 | 维护者规范：脚本位置、校验入口、`.github/` 布局、文档角色划分 |
| [docs/naming.md](docs/naming.md) | CI 维护者 | 全仓分层命名规则 |
| [contract/README.md](contract/README.md) | 维护者 + 接入者 | 契约文件清单、固定 secret 名、字段归属、校验方式 |
| [templates/README.md](templates/README.md) | 维护者 | 模板包结构与对拷契约 |
