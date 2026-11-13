# 命名规则

全仓命名一律遵守下表。新名字按层取值，不跨层借词。

| 层 | 规则 | 示例 |
| --- | --- | --- |
| 契约 / schema / JSON 字段 | snake_case | `build_profile`、`scons_flags`、`cache_impact` |
| 工作流 / action 文件与目录 | kebab-case，reusable workflow 用单数 | `ios-visionos-build.yml`、`build-pipeline/` |
| 工作流 / action inputs、outputs、step id | kebab-case，不带缩写 | `caller-uses-ref`、`hooks-bundle` |
| env | kebab→SCREAMING_SNAKE 一一对应 | `caller-uses-ref`→`CALLER_USES_REF` |
| `run` 块里直接消费的 `inputs.*` 透传 | `INPUT_*` + 输入名 kebab→SCREAMING_SNAKE | `INPUT_BUILD_PROFILE` |
| 喂给脚本当上下文的 env（脚本按固定名读） | 业务名 SCREAMING_SNAKE，与脚本读取名一致 | `ENGINE_SHA`、`CACHE_KEY` |
| 跨 step 的 `GITHUB_ENV` 变量 | 业务名 SCREAMING_SNAKE（不套 `INPUT_*`） | `DIST_REL`、`PRESET_LIST` |
| 脚本文件名 / 函数 / CLI 旗标 | snake_case，旗标与消费的字段同名 | `run_hooks.py`、`--build-profile` |
| 主题文档（`docs/` 下的 `.md`） | kebab-case | `ci-architecture.md`、`hooks.md` |
| 仓库入口与目录说明文档 | `README.md`（大写，GitHub 平台按名识别） | `README.md`、`.github/contract/README.md` |
| 锚点 id | kebab-case，命名具体业务事件 | `preset-clean`、`compile` |
| 平台名 | CI 内部一律契约平台名（`linux`…）；映射表在 `godot_context.py`，SCons 名 `linuxbsd` 唯一消费点是 `scons_build.py` | — |
| 布尔值 | 字符串一律 `true`/`false`；SCons 标志值域除外（`yes`/`no`） | `need-d3d12=true` |
| 同类多对象的一组模板字段 | 前缀取**对象所在域**（不是所在链），共用词干 `_naming` | `artifacts.build_naming` / `artifacts.export_naming` |

## 面向人的文案（job / step `name:`、`description:`、文档正文）

判据是**这个词会不会被读者拿去对照代码**。会，就保留英文原形；不会，写中文。三类：

| 类别 | 处理 | 例 |
| --- | --- | --- |
| 标识符 / 字段指针 / 枚举值 | **保留英文**，一字不改——改了就与 schema、代码或契约字段对不上 | `build_runner`、`export.editor_version`、`template_release`、`inject-config:after`、`owner/repo`、`local:` |
| 专有名词 / 品牌 / 技术名 | **保留英文**，翻译后读者反而认不出 | `Godot`、`SCons`、`Android`、`macOS`、`Vulkan`、`PCK`、`SDK`、`JDK`、`PyYAML` |
| 普通英文词 | **写中文** | runner→运行器、bundle→扩展包、before/after 在锚点里属标识符故保留 |

工作流级 `name:` 是 Actions 侧栏的产品名，一律英文（`Engine Build`、`Project Release`）；无法手动触发（无 `workflow_dispatch`）时前面加 `~ ` 标记。job / step 级 `name:` 写中文描述（`Linux 编译`、`扩展 compile:before`——锚点 id 与 phase 原样嵌入，不翻译）。

契约字段清单的唯一权威源是 `contract/*.schema.json`，文件名清单见 [contract/README.md](../contract/README.md)，本文不重复。
