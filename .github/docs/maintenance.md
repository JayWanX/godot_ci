# 维护者规范

CI 资产仓库自己的开发约定。面向使用者（项目仓库接入）的内容见 [.github/README.md](../README.md) 与 [templates/USAGE.md](../templates/USAGE.md)。

## 脚本位置

- **action 专属脚本**住该 action 的 `scripts/` 子目录，composite 内经 `$GITHUB_ACTION_PATH/scripts/<名>` 调用（先例：`actions/export/scripts/export_steps.py`、`actions/config-inject/scripts/config_inject.py`）。
- **被多个消费者使用**的脚本才住 `.github/scripts/`（先例：`check_ci_assets.py` 被 self-check 与 verify_local 消费、`godot_context.py` 被三个 resolve 作业消费）。
- action 内脚本引用共享库时，`sys.path` 按 `export_steps.py` 的先例爬回共享目录（`Path(__file__).resolve().parents[3] / "scripts"`）。

## 校验入口

在 CI 仓库根执行（一条命令等价于 Self-check，零第三方依赖、不联网）：

| 命令 | 覆盖 |
| --- | --- |
| `python .github/scripts/verify_local.py` | 静态校验 + 全部自测 + 模板打包校验；`--quick` 只跑静态校验 |
| `python .github/scripts/check_ci_assets.py` | 静态校验（需 PyYAML，其余零依赖）：YAML 语法、重复键、workflow_call 与 composite action 的 input 交叉一致性、硬编码产物名、job 级 `./` 引用、字面量扫描、锚点接线双向校验、env 命名防回潮、runs-on 字面量防回潮、平台 target 门控存在性与取值一致、兜底常量拒绝、下载/上传 artifact 的 needs 覆盖、缓存键两侧一致、retention 兜底与 schema 一致、模板 ref 四方一致性、sparse-checkout 清单覆盖本 job 引用的仓库内资产目录（脚本 / 本地 action） |
| `for f in .github/tests/test_*.py; do python "$f"; done` | 零依赖逐文件自测；扩展机制的自测（`run_hooks.py selftest`）由 `test_run_hooks.py` 调用同一实现，不单独列项 |
| `python .github/scripts/godot_context.py --check` | 契约校验（只校验不写），在项目仓库根执行；契约路径经 `CI_CONTRACT` 环境变量指定，缺省解析到该仓库 `.github/ci-contract.json`——本仓库无契约，直接跑会报缺文件，故不在 `verify_local.py` 范围内 |

判据：

1. **字面量扫描**：对 `workflows/` / `actions/` 的 `run:` 块跑禁用清单正则（具体引擎版本、token 形态、`export_presets.cfg` 硬编码），命中即失败。`scripts/` 与下载 URL 里的固定坐标（`godotengine/godot`、`editor-*.zip`）属 CI 固有事实，不纳入扫描。
2. **schema 一致性**：`--check` 校验契约、扩展清单、`context.json` 三份 schema；断言 `$schema` URL 的 ref 段 = `ci.ref` = 薄触发器 `uses:@ref`；`context.json` 由 resolve 生成后校验，不允许手写。
3. **缓存键**：关闭扩展时键与未启用时一致（workflow 层短路、`plan.json` 为空常量、hooks-sha 段省略）；启用且清单非空时键必须变化；`restore-keys` 按 `cache_impact` 条件化后不得抵消精确换键（分层键定义见 [ci-architecture.md](ci-architecture.md#缓存键)）。
4. **职责分离**：自有项目构建在项目仓库推 `v*`，断言 Release 与附件落在该仓库；外部项目构建在 CI 仓库用 `project` 指向外部项目，断言只产出 Artifact 且落在 CI 仓库、不创建任何 Release、不使用任何外部写凭证；契约与 CI 侧 secret 清单中不得出现外部写凭证或 `release` 段。

## `.github/` 布局

| 路径 | 内容 |
| --- | --- |
| `workflows/` | 四条链（`build.yml` / `export.yml` / `project-release.yml` / `engine-release.yml`）、Chain Runner Bootstrap（`runner.yml`）、模板包发布（`template-release.yml`）、Self-check（`self-check.yml`）与各平台 reusable workflow |
| `actions/` | 复合 Action：平台构建共享步骤、参数解析、缓存、扩展钩子分发、Release 附件发布（`release-upload`，供发布链复用） |
| `contract/` | 契约与 schema，CI 与项目之间唯一稳定的机器可读契约面 |
| `scripts/` | 契约归一化、参数解析、钩子执行器等零依赖 Python 实现 |
| `tests/` | 上述脚本的零依赖自测 |
| `templates/` | 项目仓库侧的可复制模板：契约、扩展清单、4 个薄触发器 |
| `docs/` | 架构、用法、扩展机制与命名规则文档 |

## 文档角色划分

文档按读者分层，改内容先对号入座；本表是防漂移判据，同一事实只落在唯一归属。

| 文档 | 读者 | 定位 |
| --- | --- | --- |
| `.github/README.md` | 访客 / 接入者落地页 | 是什么、四链一览、接入从哪开始（指向模板包与 USAGE）、文档索引 |
| `templates/USAGE.md`（随包 → 项目 `.github/CI-USAGE.md`） | 接入者 / 日常使用者 | 接入三步、契约字段、运行与产物、来源声明语法、锁版本与搬家 |
| `docs/workflows.md` | 日常使用者 | 四链使用手册：结构概览、触发、入参、产物命名与获取、使用者注意事项 |
| `docs/hooks.md` | 钩子作者（项目侧扩展开发者） | 锚点注册表、清单 schema、上下文、可写面、安全信任 |
| `docs/ci-architecture.md` | CI 维护者 | 架构判据与内部机制：核心原则、部署形态、职责划分、权限、引用语法、公共契约、缓存键、执行器内部、安全模型 |
| `docs/maintenance.md` | CI 维护者 | 维护者规范汇总：脚本位置、校验入口、`.github/` 布局、文档角色划分 |
| `docs/naming.md` | CI 维护者 | 全仓命名规则（纯命名域） |
| `contract/README.md` | 维护者 + 接入者（配 secret） | 契约文件清单、固定 secret 名、字段归属、校验方式 |
| `templates/README.md` | 维护者 | 模板包结构与对拷契约 |
| `templates/RELEASE-NOTES.md` | 使用者 | 模板包 Release 正文模板 |
