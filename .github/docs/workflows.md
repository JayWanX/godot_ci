# 工作流用法

四条 GitHub Actions 链，在 GitHub 托管的云服务上编译 Godot 引擎、导出 Godot 项目、发布项目成品与引擎快照。能改什么见 [ci-architecture.md](ci-architecture.md)；扩展机制见 [hooks.md](hooks.md)；契约字段以 `contract/*.schema.json` 为准。

| 入口 | 工作流名 | 作用 |
| --- | --- | --- |
| `workflows/build.yml` | 🔨 Engine Build | 递归编译指定引擎源码并上传构建产物 |
| `workflows/engine-release.yml` | 🏗️ Engine Release | 编译引擎并把产物上传为 Release 附件（引擎快照；手动触发可覆盖契约用任意配方，项目侧薄触发器按契约） |
| `workflows/export.yml` | 📦 Project Export | 用官方编辑器 + 官方或自编译模板导出项目成品 |
| `workflows/project-release.yml` | ~ 🚀 Project Release | 推送 `v*` Tag 时导出成品并上传为 Release 附件（本仓库内只被 `workflow_call` 调用；手动入口在项目仓库的薄触发器上） |

支持的引擎编译平台：Linux / Windows / macOS / Android / iOS / visionOS / Web。

---

## 编译引擎（🔨 Engine Build）

### 结构

```
build.yml（总调度：手动 / API / 被调用 三种触发）
├── 🚦 runner                Chain Runner Bootstrap（workflows/runner.yml，供 resolve 作业分配 runner）
├── 🔍 resolve              解析输入参数 → ref 解析为 engine-sha → 分发到各平台
├── 🐧 linux-build          → linux-build.yml
├── 🏁 windows-build        → windows-build.yml
├── 🍎 macos-build          → macos-build.yml
├── 🤖 android-build        → android-build.yml
├── 🍏 ios-build            → ios-visionos-build.yml（platform=ios）
├── 🥽 visionos-build       → ios-visionos-build.yml（platform=visionos）
└── 🌐 web-build            → web-build.yml
```

- **build.yml**：总调度。`resolve` 负责输入校验、`ref`→commit SHA 解析（`engine-sha`）、平台分发，按需调用各平台构建工作流
- **各平台构建工作流**（`linux-build.yml` 等）：仅 `workflow_call`，由 `build.yml` 调用，不可独立手动触发；`ios-visionos-build.yml` 通过 `platform` 参数复用于 iOS 与 visionOS
- **复合 Actions**（`.github/actions/`）：构建流程共享步骤——依赖安装、缓存恢复与保存、配置注入、编译、依赖探测、产物上传

### 触发方式

**手动触发（UI）**：仓库 **Actions** → **🔨 Engine Build** → **Run workflow**，按需填参数。默认参数即契约 `engine.source` 指定引擎的 release 模板（target / platform 由契约决定）。运行完成后在各平台 job 的 **Artifacts** 下载产物——手动直触发需勾选 `force-upload`。

**API 触发（`repository_dispatch`）**：发送类型为 `godot-build` 的 dispatch；参数经 `client_payload` 传入，可覆盖字段为 `source` / `ref` / `target` / `platform` / `force-upload`：

```bash
curl -X POST -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <GITHUB_TOKEN>" -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/<owner>/<repo>/dispatches \
  -d '{"event_type":"godot-build","client_payload":{
    "source":"godotengine/godot@master","ref":"4.7-stable",
    "target":"template_release","platform":"linux,windows"}}'
```

**被调用（`workflow_call`）**：`export.yml` 等通过 `uses: $/.github/workflows/build.yml` 调用，传 `with` 与 `secrets`。

**在 CI 资产仓库构建外部项目**：CI 资产仓库（如 `godot_ci`）的**导出链** `workflow_dispatch` 填 `project: remote:<owner/repo>[@<ref>]` 即可构建外部项目，产物落 CI 资产仓库的 Artifacts，不创建任何 Release（见 [ci-architecture.md](ci-architecture.md#职责划分)）。构建链的 `workflow_call` 入口由导出链下传该值；构建链自身的手动入口不暴露 `project`。

**契约是可选的**：构建链编译的是**引擎**而非 Godot 项目，没有任何项目配置也该能编。判据只看契约文件在不在——有 `.github/ci-contract.json` 就静默使用（被调用时 `project` 决定去哪个仓库找它；手动触发与 `project` 留空时取当前仓库）；没有就用默认契约：引擎取 `godotengine/godot`，`engine-ref` 留空则自动解析该仓库最新 `<N>.<N>[.<N>]-stable` tag（日志打 notice，解析失败 fail-fast，不回落隐藏版本），其余取平台基线与 schema 默认值。

### 输入参数

手动触发可填以下参数（留空读契约；`target` 为单值，其余逗号分隔字段可多值）。`project` 不在手动入口：构建链编的是引擎、与项目无关；它由导出链经 `workflow_call` 下传，语法 `remote:<owner/repo>[@<ref>]`（另接受 `local:<path>`）。`caller-uses-ref` 只在被调用时传入，不出现在手动面板。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `engine-source` | 读契约 | 引擎源码仓库 `owner/repo[@ref]`。留空读契约 `engine.source`；无契约时取 `godotengine/godot` |
| `engine-ref` | 空 | 源码分支 / 标签 / commit，覆盖 `engine-source` 中的 `@ref`。两者都空时自动解析该仓库最新 `<N>.<N>[.<N>]-stable` |
| `target` | 读契约 | 构建目标：`editor` / `template_debug` / `template_release`，**单值**；一次运行只产一个 target，传多值 resolve 期 fail-fast（不静默截断） |
| `platform` | 读契约 | 构建平台，逗号分隔（如 `linux,windows`）。留空读契约 `engine.platform`；无契约时取全部平台（iOS / visionOS 消耗 macOS 配额，建议显式指定） |
| `scons-flags` | 读契约 | 附加 SCons 标志；留空读契约 `build.scons_flags` |
| `profile` / `build-profile` | 读契约 | 编译 profile 来源覆写；留空读契约 `build.paths.*` |
| `custom-modules` | 读契约 | 自定义模块来源条目，逗号分隔；留空读契约 `build.modules` |
| `force-upload` | `false` | 是否上传编译产物到 Artifacts（需要下载产物时勾选）；被上游 `workflow_call` 复用时默认 `true`，因下游必须下载产物 |
| `caller-uses-ref` | 空 | 调用方 `uses: …@<ref>` 里的 ref；resolve 期断言与契约 `ci.ref` / `$schema` URL 的 ref 段三者一致 |

编译参数的**归属**：CI 命令行只传构建对象参数（`platform` / `target` / `arch` / Web 的 `threads` 变体）与注入通道（`profile` / `build_profile` / `custom_modules` / `cache_path`）；优化等级、功能裁剪、调试符号、Windows 子系统等全部由项目侧 `build/profile.py` 与 `build/project.gdbuild` 控制，CI 只提供来源覆写入口（`scons-flags` / `profile` / `build-profile` / `custom-modules`）。

### 配置注入

编译用的 `profile.py` 与 `project.gdbuild` 由契约 `build.paths.profile` / `build.paths.build_profile` 以**来源声明**指定，语法唯一归属 [templates/USAGE.md「来源声明语法」](../templates/USAGE.md#五来源声明语法)：`local:<path>` 项目内相对路径，`remote:<owner/repo>[@<ref>][:<path>]` 浅克隆远程仓库后取 `path` 指向的文件（多项目共享同一份 profile 时用，配合 `GIT_PAT` 可访问私有仓库）。`profile` / `build-profile` 可用 `workflow_dispatch` 或 `workflow_call` 入参传入，逐次覆盖契约声明的来源：`local:` 指向的文件缺失时 resolve 期 notice 并跳过注入，`remote:` 的可达性留到检出期判定。导出链编译导出模板时，`profile` / `build-profile` 同样从契约或入参透传给 `build.yml`。覆盖与补传机制的内部实现见 [ci-architecture.md](ci-architecture.md#编译参数注入内部)。

### 所需 Secrets

在仓库 **Settings → Secrets and variables → Actions** 配置。名字与用途是 CI 的公共接口约定，清单见 [contract/README.md](../contract/README.md)，本文不重复；全部可选，缺失时降级为只访问公开仓库 / 不加密 / 不做 Apple 签名。

`GIT_PAT` 以 `git insteadOf` URL 重写注入，子模块同样生效。`ENCRYPTION_KEY` 编译时作为 `SCRIPT_AES256_ENCRYPTION_KEY` 注入模板，导出时经 `GODOT_SCRIPT_ENCRYPTION_KEY` 传给编辑器；加密开关由 CI 按密钥与导出模式自动设置，与预设文件里的值无关。项目若用自定义 secret 名，在薄触发器 `secrets:` 里映射一次（示例见 [templates/workflows/build.yml](../templates/workflows/build.yml)），同名时 `secrets: inherit` 连映射都省。

### 各平台构建明细

Runner 与工具链版本取自平台基线（`contract/platform-baseline.json`，契约可覆写），本文不重复。

| 平台 | 构建特征 |
| --- | --- |
| Linux | 产物可 `godot --version` 直接验证 |
| Windows | MSVC；D3D12 / ANGLE / WinRT / AccessKit 按需自动下载 |
| macOS | x86_64 与 arm64 并行构建，再由 `merge` job 用 `lipo` 合并为 universal |
| Android | `editor` 产 arm64，含 Horizon OS / PICO OS 变体；导出模板含 Gradle 模板生成 |
| iOS | 经 `ios-visionos-build.yml`，按所选 `target` 构建单任务 |
| visionOS | 经 `ios-visionos-build.yml`（`platform=visionos`），按所选 `target` 构建单任务 |
| Web | 变体集合与编译参数由平台基线 `platforms.web.variants` 渲染，均为 wasm32（wasm64 产物尚不可用）；不要在工作流里内联变体或 flags |

**构建目标**：除 `web` 外全部平台支持 `editor` / `template_debug` / `template_release` 三档；`web` 只支持后两档——Emscripten 目标构建不出桌面编辑器。支持表判据是**该 target 在该平台上能否编出来**，不是 CI 能否在 runner 上跑起来验证：wasm / iOS / visionOS 产物无法在 runner 执行，但编译完全成立、产物照常上传，验证由使用者在本机完成。6 个 `*-build.yml` 结构统一：一个 `build` job（macOS 另加 `merge` job），公共步骤「恢复缓存 → 安装依赖 → 注入配置 → 探测依赖 → 编译 → 保存缓存」收在 `build-pipeline` composite 里，各平台只写检出与产物处理；`target` 不是矩阵维度而是 job 内直接取输入，一次触发只产出所选 target，不产生空跑 job。

构建完成后：Linux / Windows / macOS（含 merge job）对产物跑 `godot --version`；Android 检查 APK / AAR / zip 产物存在；Web wasm、iOS / visionOS 产物无法在 runner 执行，仅上传不测试。

### 产物命名

契约 `artifacts` 段有两套模板，**按对象划分**（不是按链）：

| 模板 | 对象 | 占位符 | 默认值 |
| --- | --- | --- | --- |
| `build_naming` | 编译产物（引擎模板 / 编辑器） | `{prefix}` `{platform}` `{target}` `{arch}` `{variant}` | `{prefix}_{platform}_{target}` |
| `export_naming` | 导出产物（预设 zip / Release 附件） | `{prefix}` `{platform}` `{preset}` | `{prefix}_{preset}` |

- 模板未含 `{arch}` 时按平台默认追加（android=arm64）；未含 `{variant}` 时调用方给了变体就作为末段追加。前缀取契约 `artifacts.prefix`，留空取项目仓库名安全形式。
- **macOS**：单架构产物名带 `_<arch>` 段，`merge` job 合并后去掉该段；template_release 的 universal 二进制装入引擎源码 `misc/dist/macos_template.app` 脚手架打包为 `macos.zip`——macOS 导出模板必须是含 app bundle 的 zip，裸二进制不被接受。
- **变体**：Android 有 `_horizonos` / `_picoos`，Web 有 `_threads` / `_nothreads`，作为末段追加。
- `export_naming` 默认不拼 `{platform}`，因为 Godot 预设名本身普遍带平台（`macOS` / `Windows Desktop` / `Linux/X11`），再拼一次会得到 `windows_windows_desktop` 这类重复名；预设名跨平台通用（如各平台都叫 `Release`）时改用 `{prefix}_{platform}_{preset}`。
- 占位符与渲染冲突在 resolve 期 fail-fast（未知占位符拒绝、全部预设全局去重），渲染机制见 [ci-architecture.md](ci-architecture.md#产物命名渲染内部)。

所有编译 Artifact 的内容均为 `<artifact 名>.7z`（`-mx=9` 极限压缩，包名与 artifact 名一致）。不属于预设维度的产物不归 `export_naming` 管，是 CI 布局常量：钩子 `write:artifacts` 散文件打包为 `<repo-safe>_<平台>_extra`（对应 Artifact `export-<平台>-extra`）。

### Release 陈旧附件清理

`gh release upload --clobber` 只覆盖同名附件、**不删已改名的旧附件**，产物命名一旦变化（改名、增删预设、模板调整），上一轮旧附件会滞留并与本次产物混合。`prune_release_assets.py` 在附件上传后按三条判据清理，任一不成立即跳过并打印原因：

1. **完整性门控**：本轮选中的预设须覆盖项目 `export_presets.cfg` 的全部预设。只导出部分预设时，其余附件是上一轮的有效交付物，清理会静默丢产物。
2. **家族限定**：只处理 `<prefix>_` 开头的附件，手工上传的说明文件等一律不碰；本次产物不以该前缀开头（`export_naming` 未含 `{prefix}`）时家族判定失效，同样跳过。
3. **期望集合**：本次实际上传的文件名永不删除。

### 缓存

SCons 编译缓存自动恢复与保存（目录 `.scons_cache/`），恢复失败不影响构建（`continue-on-error`）。键构成与分层规则见 [ci-architecture.md](ci-architecture.md#缓存键)。

---

## 导出项目（📦 Project Export）

`export.yml` 把指定 Godot 项目导出为各平台成品。两种导出类型（`choice`，默认 `custom_build`）：

| 类型 | 导出模板来源 | 是否编译 |
| --- | --- | --- |
| `official` | 下载 Godot 官方 `export_templates.tpz` | 不编译任何东西；模板版本 = `editor_version`，可能与项目引擎版本不一致 |
| `custom_build` | 按项目契约的引擎版本 + profile **编译模板** | 跑完整 `build.yml`；模板与引擎版本必然一致，可用 `scons-flags` / `profile` 裁剪 |

编辑器两种模式都用官方版。

- **official**：用官方编辑器 + 官方模板导出（不编译任何东西，需指定 `editor-version`）
- **custom_build**：仍用官方编辑器，只调用 `build.yml` 编译同源导出模板

**编辑器与模板预取**：官方编辑器由独立 `prefetch` job 按基线 `platforms.<平台>.editor_archive` 分档下载上传——同一档案被多平台共用时只下载一份（resolve 按档案去重后转 matrix-include 形态 JSON），换档案只改该字段；official 模式的 tpz 在 `resolve` 作业下载上传（单文件，名称无平台维度）。7 个导出作业只下载这些 artifact（pattern `*_editor_*` 与 `<repo-safe>_export-templates`），不各自跨公网拉取大文件。导出作业在 `needs` 上依赖 `prefetch`（建立上传 → 下载的 happens-before）；预取只是优化，失败不阻断导出，`prepare-editor` 命中预取目录则复用、否则在线下载。

### 触发方式

- **手动触发（UI）**：仓库 **Actions** → **📦 Project Export** → **Run workflow**
- **被调用（`workflow_call`）**：由 `project-release.yml` 复用

### 输入参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `project` | 当前仓库 | 外部项目构建时 CI 仓库运行时输入的待导出项目，来源声明语法 `remote:<owner/repo>[@<ref>]`；项目仓库薄触发器不暴露该输入。指定外部仓库时预设与平台取自该仓库自己的 `export_presets.cfg` |
| `presets` | 读契约 | 要导出的预设名（逗号分隔）。留空读契约 `export.presets`；名单含 `export_presets.cfg` 中不存在的预设名 → resolve 期 fail-fast（拼错名 = 静默丢产物）。指定外部 `project` 时退化为白名单：白名单中项目没有的预设告警并跳过（kept 为空则失败），且 `presets` 与契约白名单都须留空才导出该项目全部预设 |
| `export-type` | `custom_build` | `official` / `custom_build`（见上表；契约 `export.type` 是默认值的来源，UI 选择恒生效） |
| `editor-version` | 读契约 | 官方编辑器版本，两种模式都用。必须与模板版本兼容，否则 PCK 版本校验会拒绝加载；custom_build 时 resolve 期即比对引擎 ref 与它的 major.minor（ref 为分支 / SHA 无法静态判定时跳过） |
| `scons-flags` | 读契约 | 编译导出模板时的附加 SCons 标志，仅 `custom_build` 生效 |
| `profile` / `build-profile` | 读契约 | 编译导出模板时的来源覆写（`local:` / `remote:`），仅 `custom_build` 生效 |
| `caller-uses-ref` | 空 | 调用方 `uses: …@<ref>` 里的 ref；resolve 期断言与契约 `ci.ref` / `$schema` URL 的 ref 段三者一致 |

导出相关默认值全在契约 `export` 段。custom_build 编译模板时契约**以待导出项目仓库为准**：`project` 指向哪个仓库就读哪个仓库的 `.github/ci-contract.json`（`project` 留空则是触发器仓库自身）。**该契约是可选的**——缺失时用默认契约（引擎与编辑器都取最新 stable、预设读项目 `export_presets.cfg` 全部）；契约存在但非法则 resolve 期 fail-fast。

### 流程

```
export.yml
├── 🚦 runner                   Chain Runner Bootstrap（workflows/runner.yml）
├── 🔍 resolve                  契约归一化、按预设的 platform 字段推导各平台预设列表；official 模式下载 tpz
├── ⬇️ prefetch                 按基线 platforms.<平台>.editor_archive 去重后逐档案下载官方编辑器
├── 🔨 build-templates          （custom_build）调用 build.yml 编译导出模板
├── 📦 export-windows ─┐        needs: prefetch（上传 → 下载的 happens-before；预取失败不阻断）
├── 🐧 export-linux    │
├── 🍎 export-macos    ├─ 7 个 uses: export-platform.yml（平台差异全由 with 传入）
├── 🌐 export-web      │    导出 job：检出 → 预取/模板 → 逐预设导出（每预设产物落
├── 🤖 export-android  │    dist/<preset-safe>/ 子目录）→ 钩子 → 中转 dist
└── 📦 export-ios / export-visionos ─┘
                                 upload job：matrix 逐预设打包上传
```

7 个导出作业收口到单个 `workflow_call` reusable workflow `export-platform.yml`；各平台作业互不依赖、并行执行，无预设的作业自动跳过。**产物按预设粒度交付**：每预设打独立 zip 并上传为独立 Artifact `export-<平台>-<preset-safe>`（包名由契约 `artifacts.export_naming` 渲染，导出执行器把每个预设的 `export_path` 收敛到 `<dist>/<preset-safe>/` 独立子目录，同名产物结构上无法互相覆盖）；钩子 `write:artifacts` 落在 dist 根的散文件打包为平台级附加包 `export-<平台>-extra`（仅存在时产生）。

### 项目目录要求

被导出的项目需在根目录含：

- `project.godot`：项目配置
- `export_presets.cfg`：导出预设；CI 导出前自动清洗本机绝对路径、按导出模式设置自定义模板与加密开关。清洗**原地覆写**该文件（`.ci.bak` 留一份备份），仅在 CI 检出的副本上执行；本地直接运行脚本会改动工作区文件
- `build/`：引擎编译配置——`profile.py`（SCons 参考配置）、`project.gdbuild`（类型裁剪）。两者均可选，缺失时 `resolve` 输出空值并打印 notice，`scons` 命令行不追加对应参数；`official` 模式不使用这两个文件

三个文件的相对位置由契约 `export.paths` 决定（`project_godot` / `export_presets` / `dist`），改契约即生效。`dist` 同时用于预设的 `export_path` 与实际输出目录，两者必须一致，否则预设写的路径与产物落点不符。

### 注意事项

- 导出成品按预设粒度上传至 **Actions → 本次运行 → Artifacts**：每预设一个 Artifact（`export-<平台>-<preset-safe>`，内含 `<package>.zip`），钩子散文件附加包为 `export-<平台>-extra`；保留天数取契约 `artifacts.retention.export`
- **加密只取决于模板**：运行时用编译进模板的密钥解密，故模板必须用同一 `SCRIPT_AES256_ENCRYPTION_KEY` 编译；编辑器无需带密钥，官方版即可。配置 `ENCRYPTION_KEY` 后 custom_build 导出启用 PCK 加密、official 模式一律关闭（开关由 CI 重写，与预设文件里的值无关）。官方模板配加密会导出成功但运行时无法解密（[官方文档](https://docs.godotengine.org/en/4.7/engine_details/development/compiling/compiling_with_script_encryption_key.html)）
- **导出链恒用 `template_release` 模板**：导出成品是发行态游戏，导出平台作业不接受 target 参数——传 `template_debug` 只会得到与模板不匹配的包。要产 template_debug 成品请在构建链编 `template_debug` 并自行使用
- `ENCRYPTION_KEY` 必须恰好 **64 位十六进制字符（32 字节）**：引擎编译期与导出期都按此判定，长度不符会在模板编译期报错或静默产出未加密 PCK。两个 action 入口均做校验，快速失败
- **ETC2/ASTC 自动开启**：macOS、iOS、visionOS、Android 与 Web 全部预设，引擎导出前会硬校验 `rendering/textures/vram_compression/import_etc2_astc`，未开启则失败。该开关默认值依赖宿主 OS 的偏好纹理格式，而 CI 的 Linux runner 偏好 S3TC/BPTC，故必须显式开启；CI 在清洗步骤按本次导出的**平台**整集自动写入 `project.godot`（多开一个纹理导入目标只增加构建期编码时间，不影响产物行为），且发生在 `--import` 之前，否则已导入资源不含 ETC2/ASTC 变体
- macOS 编译期与 iOS/visionOS 导出期消耗 macOS runner 配额；`export-macos` 实为 Linux 交叉导出（runner 见平台基线 `platforms.macos.export_runner`），Linux 上无法执行 macOS codesign

---

## 发布 Release（~ 🚀 Project Release）

`project-release.yml` 在推送 `v*` Tag 时自动导出成品并上传为该 Tag 对应 Release 的附件（Release 不存在则自动创建）。它复用 `export.yml`（`workflow_call`）。

### 触发方式

| 触发 | 说明 |
| --- | --- |
| 推送 `v*` Tag | 自动导出并创建（或复用）同名 Release。发布 Release 时 GitHub 会同时新建 Tag，故无需单独的 Release 事件触发（避免双触发互取消） |
| 手动触发 | 在**项目仓库**的薄触发器上手动运行（需填 `tag`）；CI 资产仓库侧本工作流只有 `workflow_call` |

### 输入参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `tag` | 触发上下文 | 要发布到的 Tag。留空取 Tag 推送的 `ref_name` |
| `presets` | 读契约 | 要导出的预设名，逗号分隔 |
| `export-type` | `custom_build` | `official` / `custom_build`（契约 `export.type` 是默认值的来源） |
| `editor-version` | 读契约 | 官方编辑器版本 |
| `draft` | `false` | Release 不存在时创建为草稿 |
| `caller-uses-ref` | 空 | 调用方 `uses: …@<ref>` 里的 ref；resolve 期断言与契约 `ci.ref` / `$schema` URL 的 ref 段三者一致 |

Release 只在自有项目构建时由项目仓库推 `v*` 触发，附件恒落在 `github.repository`，触发器不暴露 `project` 输入。外部项目构建不调用 `project-release.yml`、不创建 Release（见 [ci-architecture.md](ci-architecture.md#职责划分)）。

### 流程

```
project-release.yml
├── 🚦 runner          Chain Runner Bootstrap（workflows/runner.yml）
├── 📦 export          调用 export.yml（force-upload=true，保证产物必定上传为 Artifacts）
├── 🔍 resolve-hooks   release 链 resolve 期：校验扩展清单 → 产 plan → 上传 hooks-bundle
└── 🚀 publish         检出 CI 资产 → 下载产物 → 收集附件 → publish:before 钩子
                       → 合并钩子产物 → 创建/复用 Release → 上传附件 → publish:after 钩子
```

### 注意事项

- Release 不存在时自动创建（`--generate-notes` 生成变更说明）；已存在则跳过创建，直接上传附件
- 附件名即导出产物名；同一 Release 内同名附件会被覆盖（`--clobber`），故重跑安全
- 产物按 artifact 分目录下载，同名文件不会在下载阶段互相覆盖
- 需要 `contents: write` 权限（内置 token 即可，无需额外 secret）
- **扩展链**：`publish` 锚点（chain=release，见 [hooks.md](hooks.md#锚点注册表)）的两相都在 `publish` job——before 在附件上传前（`write:artifacts` 产出合并进附件集），after 在上传后；无清单 / `policy: off` 时 `resolve-hooks` 输出空 bundle 名，publish job 扩展步骤全部短路。`publish:before` 钩子失败（`on_error: fail`）即不上传附件。

---

## 发布引擎（🏗️ Engine Release）

`engine-release.yml` 把**编译好的引擎二进制**固化为 Release 附件（引擎快照）：调用 `build.yml` 编译引擎（构建入参透传，留空读契约、填了覆盖——覆盖语义由 build.yml 的 resolve 归一化处理），`publish` 作业下载全部编译 Artifact 并上传为 `github.repository` 的 Release 附件。与 release 链的分工：**release 固化项目成品**（推 `v*` Tag 导出预设产物），**engine-release 固化引擎二进制**（引擎快照 / 多版本引擎并存）。Release 落点恒为 `github.repository`（触发仓库）：项目侧触发落项目仓库，CI 仓库手动触发落 CI 仓库（可用 `remote:` 来源声明编任意配方引擎并固化，与 template-release 发布模板包同属 CI 自有内容 / 自有构建的发布）。

### 触发方式

| 触发 | 说明 |
| --- | --- |
| 手动触发（项目仓库） | Actions → **🏗️ Engine Release** 薄触发器 → **Run workflow**（可填 `tag`）；入参留空读项目契约，Release 落在项目仓库 |
| 手动触发（CI 仓库） | Actions → **🏗️ Engine Release** → **Run workflow**；入参留空用默认契约（`godotengine/godot` 最新 stable），填了（如 `profile: remote:…` / `custom-modules: remote:…`）可固化任意配方引擎——Release 落 CI 仓库 |
| 被调用（`workflow_call`） | 由项目仓库的薄触发器调用，入参面与手动入口相同 |

### 输入参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `tag` | `engine-snapshot` | 发布到该 Tag 的 Release，`--clobber` 覆盖同名附件；按引擎版本填 tag（如 `4.7.2-stable`）即多版本并存。tag 不存在时 `gh release create` 以默认分支头自动建 tag |
| `engine-source` | 空 | 引擎来源 `owner/repo[@ref]`。留空读契约 `engine.source`；无契约时取 `godotengine/godot` |
| `engine-ref` | 空 | 引擎引用，覆盖 `engine-source` 里的 `@ref` |
| `target` | 空 | 构建目标（editor / template_debug / template_release，单值）。留空读契约 `engine.target` |
| `platform` | 空 | 构建平台，逗号分隔。留空读契约 `engine.platform` |
| `scons-flags` | 空 | 附加 SCons 标志（覆写契约 `build.scons_flags`） |
| `profile` / `build-profile` | 空 | 编译配置来源覆写（`local:` / `remote:`）。留空读契约 `build.paths.*` |
| `custom-modules` | 空 | 自定义模块来源条目，逗号分隔。留空读契约 `build.modules` |
| `caller-uses-ref` | 空 | 调用方 `uses: …@<ref>` 里的 ref；resolve 期断言与契约 `ci.ref` / `$schema` URL 的 ref 段三者一致 |

构建入参全部留空时本链行为 = 「契约引擎的固化发布」（项目薄触发器的常态）；任意一个填了则覆盖契约对应维度（覆盖语义与优先级由 build.yml 的 resolve 归一化处理）。默认 tag 名 `engine-snapshot` 是布局常量。

### 流程

```
engine-release.yml
├── 🚦 runner    Chain Runner Bootstrap（workflows/runner.yml，publish 作业读基线 roles.publish）
├── 🔨 build     调用 build.yml（构建入参透传：留空读契约、填了覆盖；force-upload=true）
└── 🚀 publish   检出 CI 资产 → 下载全部编译 Artifact → 按 *.7z 收集附件 → 创建/复用 Release → 上传附件
                → sha256 指纹写 GITHUB_STEP_SUMMARY
```

### 注意事项

- 附件名即编译产物名：契约 `artifacts.build_naming` 渲染的 `<prefix>_<platform>_<target>[_<arch>][_<variant>]` + `.7z`；一次运行的全部平台产物落在同一 Release
- publish 按 `*.7z` 后缀收集附件，产物名不进 workflow；构建链的扩展包 artifact（hooks-bundle）内容非 `.7z`，被天然排除
- 需要 `contents: write` 权限（内置 token 即可，无需额外 secret）；`--clobber` 覆盖同名附件，重跑安全；不调用 `prune_release_assets.py`（附件名不属于 `export_naming` 的产物家族）
- **无扩展链**：本链不注册任何钩子锚点（`hooks-anchors.json` 无本链条目），构建链自身的钩子仍随 `build.yml` 生效

---

## SCons 构建选项

编译时的 SCons 参数通过 `profile.py`（项目 `build/` 下的键值赋值文件，随引擎源码一起注入）控制。工作流固定附加 `redirect_build_objects=no`、`cache_path=<scons-cache>` 与 `platform_flags` 覆盖兜底（见「编译参数的归属」），其余例如：

- 构建目标 / 架构：`target = "template_release"`、`arch = "auto"`
- 功能裁剪：`disable_3d = "yes"`、`disable_physics_2d = "yes"`
- 优化 / 发布：`optimize = "size_extra"`、`production = "yes"`、`lto = "full"`
- 模块开关：`module_freetype_enabled = "yes"`

直接编辑项目 `build/profile.py` 配置这些选项；需要按平台或变体区分时，用 `platform_flags`。引擎源码的标准头兼容由 CI 自动处理，开关由契约 `build.compat` 控制（机制见 [ci-architecture.md](ci-architecture.md#引擎源码兼容性处理)）。

## 通用注意事项

- **resolve 期校验**：`target` / `platform` 做白名单校验，并按平台 × target 支持表逐组合拒绝（如 web+editor）；注入防护与并发组构成的内部设计见 [ci-architecture.md](ci-architecture.md#编译参数注入内部)
- **依赖固定**：所有第三方 Actions 均固定到 40 位 commit SHA；更新策略见 [`.github/dependabot.yml`](../dependabot.yml)（周期、分组、PR 上限以该文件为准）
- **并发控制**：`build.yml` / `export.yml` 相同组内重复触发排队执行，`project-release.yml` / `engine-release.yml` 相同 ref 的新触发会取消进行中的旧运行

## 来源声明语法

所有来源声明字段（契约字段与 workflow 入参）共用一套语法，唯一归属 [templates/USAGE.md「来源声明语法」](../templates/USAGE.md#五来源声明语法)。进入体系的判据：字段的值域有两种可能位置才前缀化；`engine.source` 恒为远程仓库裸值，`export.paths.*` 与 `hooks.manifest` 恒为项目内相对路径裸值。语法校验分层见 [ci-architecture.md](ci-architecture.md#公共契约)。

## 项目仓库怎么接入

从 [.github/templates/](../templates/) 对拷：一份 `.github/ci-contract.json`、4 个薄触发器（build / export / project-release / engine-release）、可选 `.github/ci-hooks.json`，加 Godot 原生文件（`project.godot` / `export_presets.cfg` / `build/`）；`uses:` 已固化指向本 CI 仓库，复制后零必改。接入三步、契约字段与日常运行见 [templates/USAGE.md](../templates/USAGE.md)（随包落位 `.github/CI-USAGE.md`）；模板包结构与锁版本 / 搬家的变更点见 [templates/README.md](../templates/README.md)。
