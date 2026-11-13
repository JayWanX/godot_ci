# 公共契约（`.github/contract/`）

本目录是 CI 与项目之间**唯一稳定、机器可读的契约面**。CI 代码不内嵌任何特定项目的业务逻辑 / 路径 / 版本；项目相关的一切从 `.github/ci-contract.json` 进来，字段定义以本目录 schema 为准。字段清单不复制进任何文档，改字段只改 schema 与实现。边界与判据见 [ci-architecture.md](../docs/ci-architecture.md)。

## 文件

| 文件 | 角色 | 谁消费 |
| --- | --- | --- |
| `contract.schema.json` | 项目契约 schema（唯一权威源） | 两个 resolve、`godot_context.py --check` |
| `hooks.schema.json` | 扩展清单 schema | `run_hooks.py` validate 阶段 |
| `context.schema.json` | 运行时上下文 schema | resolve 产出、执行器消费侧重验 |
| `hooks-anchors.json` | 锚点注册表（扩展机制唯一权威源；phases 全显式） | `run_hooks.py`（结构 + phase 门禁）、`check_ci_assets.py`（接线完整性双向校验） |
| `hooks-capabilities.json` | 能力注册表（扩展机制唯一权威源：能力全集 → 可产出 outputs） | `run_hooks.py`（capabilities/outputs 校验） |
| `platform-baseline.json` | 平台基线：仅项目可覆写的默认值 | resolve 归一化进 context |
| `README.md` | 本文：固定 secret 名、字段归属、校验方式 | 人 / 文档指针 |

文件名规则见 [naming.md](../docs/naming.md)。

## 固定 secret 名（CI 公共接口，不进契约）

CI 内部按固定名从 `secrets` 上下文取值，名字不进 `ci-contract.json`、`context.json`、缓存键、日志。值是唯一可变的部分。

| 名字 | 用途 | 消费点 | 适用链（secret:* 能力） |
| --- | --- | --- | --- |
| `GIT_PAT` | 私有仓库只读检出 / `remote:` 来源抓取 / `clone_project` 鉴权 | 各平台 `*-build.yml`、`config-inject`、两个 resolve | build / export / release |
| `ENCRYPTION_KEY` | Godot PCK 加密密钥（十六进制，32 字节） | `config-inject`（编译期）、`export`（导出期） | build / export / release |
| `IOS_CERT_B64` | Apple 签名证书（base64 编码的 p12） | `export.yml` → `export-platform.yml` 的 iOS / visionOS 导出 | export |
| `IOS_CERT_PASS` | Apple 签名证书密码 | 同上 | export |

「适用链」是 `hooks-capabilities.json` 的 `secret_names` 按链分组：只有该列的链才在调用方 job 级 env 注入该 secret，`secret:<NAME>` 钩子能力也只在这些链的 validate 期放行（声明在别的链 → resolve 期拒绝并给出链名）。值由调用方 job 级 env 注入，执行器按声明挑出后仅注入该 hook 子进程；声明了却取不到值，dispatch 期直接失败，不再静默跳过。引擎发布链（`engine-release.yml`）经 `runner.yml` 与 `build.yml` 间接消费 `GIT_PAT` / `ENCRYPTION_KEY`，自身无锚点、不参与 `secret:*` 能力授权。

项目自定义命名在自有项目构建的薄触发器 `secrets:` 映射一次（`GIT_PAT: ${{ secrets.MY_OWN_PAT_NAME }}`），外部项目构建在 CI 仓库 `workflow_dispatch` / `repository_dispatch` 的 secrets 配置里映射；同名时 `secrets: inherit` 连映射都省。名字不做契约字段：`secrets` 上下文不支持动态键名，且 CI 用到哪个 secret 由自身实现决定。

**不设、也不需要**外部项目写凭证：外部项目构建只产 Artifact、不发布。

## 字段归属

| 归属 | 位置 |
| --- | --- |
| 项目可配值 | `ci-contract.json`（`engine` / `build` / `export` / `artifacts` / `hooks` / `platforms` / `roles`） |
| 项目可覆写的默认值 | `platform-baseline.json`（platforms / roles / 工具链版本 / retention / compat 规则） |
| 扩展机制注册数据 | `hooks-anchors.json`（锚点时机）、`hooks-capabilities.json`（能力授权）——项目不可覆写 |
| CI 固有事实 | 留在 CI 代码：`.github/` 布局常量、action pin、SDK 脚本名、固定 secret 名 |
| 运行时输入 | `project`（仅外部项目构建，CI 仓库运行时传）、`tag`（仅自有项目构建推 Tag）、secret 值 |
| 运行时 context | `context.json`：项目可配值与运行期值的只读归一化结果，只服务钩子 |

**`ci` 段可选，按形态定**：跨仓形态（薄触发器 `uses:` 调用本仓库）必填，单仓自用（全套 `.github/` 在项目仓库内）可省略；一致性断言与下游检出的机制见 [ci-architecture.md「部署形态」](../docs/ci-architecture.md#部署形态)。

## 字段清单的边界

- `context.json` 只服务钩子，不服务 CI 本体。不含 `layout` / `engine_tree` / `toolchain` 绝对路径 / `secrets_available` / `release`——这些是 CI 固有事实或非运行时信息，留 CI 代码。
- 扩展只读 `context.json`；可写面是三条并列通道，语义与授权见 [hooks.md「可写面」](../docs/hooks.md#可写面)：产物通道（`$GODOT_HOOK_DIST`）、指令通道（`$GODOT_HOOK_DIRECTIVES`）与项目树通道（`write_paths` 声明路径），按 `capabilities` 逐项授权。`hooks-result.json` 是 CI 内部跨 step / 跨 job 的传值通道，**不是**钩子的可写面。

## 校验

- 契约不带 `version`、不设适配层 / 迁移层；字段变更直接改 schema 与 CI 实现，项目仓库同步改契约。
- resolve 用本次运行检出的**当前** schema 硬校验（读本目录本地文件，不 fetch `$schema` URL）；`$schema` 仅作编辑器校验指针，其 URL 的 ref 段与 `ci.ref` / 薄触发器 `uses:@ref` 三者必须一致，resolve 期断言（契约缺 `ci` 段时跳过断言）。
- 非法字段、缺必填、类型不符、未知字段一律 resolve 期 fail-fast；无自动迁移。
- 离线预检：`python .github/scripts/godot_context.py --check`（只校验不写），在项目仓库根执行。
- 本目录全部 JSON（含 schema）走同一 JSONC 通道（`jsonc.py` 零依赖实现：`//` 与 `/* */` 注释、尾逗号）；注释形态：数据文件（anchors / baseline）与模板用 `//`，schema 用 `description`（JSON Schema 标准元数据，编辑器消费）。
- JSONC 解析与 schema 校验均为零依赖 Python 标准库实现（runner 不预装 `jsonschema` / `PyYAML`）。
