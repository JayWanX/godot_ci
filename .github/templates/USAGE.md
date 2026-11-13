# Godot CI 使用说明

这份说明随模板包落位，是**通用文档**：只讲模板的默认形态与接入步骤，不描述任何接入
项目的具体实况，也不需要随 CI 仓库演进而修改。你的项目实际配置只看
`.github/ci-contract.json`——每个字段的取值说明就写在它的注释里。

## 一、包里有什么

解压到项目仓库根后，文件落在 `.github/` 下：

```
.github/
├── CI-USAGE.md            # 本文件
├── ci-contract.json       # 项目契约：你唯一需要改的文件
├── ci-hooks.json          # 扩展清单样例（可选，不用就删）
└── workflows/
    ├── build.yml            # 编译引擎 → Artifacts
    ├── export.yml           # 导出项目成品 → Artifacts
    ├── project-release.yml  # 推 v* Tag → 导出并发布项目成品 Release
    └── engine-release.yml   # 编译引擎 → 发布引擎快照 Release 附件
```

包内没有顶层 `README.md`，全部条目都在 `.github/` 下，解压不会覆盖你放在仓库根的 README。

**不需要重命名任何文件。** 四个触发器里的 `uses:` 已指向 CI 仓库（坐标见契约 `ci` 段），
按默认路径放好即可运行。提交一次，让触发器出现在 Actions 页面（GitHub 只显示默认分支上的工作流）。

## 二、改配置

### 2.1 必填：替换两处版本占位符

模板 `ci-contract.json` 里有两处 `<version>`，换成你的真实版本：

| 字段 | 填什么 | 例子 |
| --- | --- | --- |
| `engine.source` | 引擎仓库与版本 | `godotengine/godot@4.7.2-stable` |
| `export.editor_version` | 官方编辑器版本 | `4.7.2-stable` |

两者 **major.minor 必须一致**，否则自编译模板与官方编辑器不兼容，解析阶段直接失败。

### 2.2 其余字段按项目调整

其余字段都有默认值，不改也能跑；要调整时直接改 `.github/ci-contract.json`，每个字段的
取值说明写在契约文件的注释里。常改的几处：

- 编译与导出范围：`engine.target` / `engine.platform` / `export.presets`（预设名须与
  `export_presets.cfg` 里的 `name` 一字不差，拼错会在解析阶段报错）。
- 自定义模块与编译配置：`build.modules` / `build.paths.*`。
- 产物命名与保留天数：`artifacts`。
- 是否启用扩展：`hooks.policy`（见 2.4）。

字段定义的权威源是 CI 仓库的 `contract/contract.schema.json`（由契约 `$schema` URL 指向，
编辑器据此补全）；填错取值解析阶段即报错，不会静默生效。

### 2.3 secrets（按需）

CI 只按固定 secret 名取值，名字清单见 CI 仓库的 `.github/contract/README.md`。
私有仓库检出、`remote:` 来源、PCK 加密、iOS / visionOS 签名等场景才需要配置。secret 名字
与固定名不同时，在触发器的 `secrets:` 段映射一次（`build.yml` / `export.yml` 模板已给出
示例）；名字本来就一致时，把整段 `secrets:` 换成 `secrets: inherit` 更省事。

### 2.4 扩展清单（默认关闭）

扩展默认关闭：契约 `hooks.policy` 缺省 `off`，随包的 `.github/ci-hooks.json` 是样例清单，
不会被读取，留着也不影响。

- **要启用**：把 `hooks.policy` 改成 `declared`，并把清单里样例块的 `run` 指向项目里真实
  存在的脚本（路径相对项目根，支持 `.py` / `.sh`）。样例脚本不随包提供，照抄不改会在
  校验期报「引用的文件不存在」。启用后清单会被真实校验并执行。
- **要停用**：`policy` 保持 / 改回 `off`，或删掉清单文件。

钩子的锚点时机、能力授权与可写面见 CI 仓库的 `docs/hooks.md`；清单位置由契约
`hooks.manifest` 决定，默认 `.github/ci-hooks.json`。

## 三、运行与产物

| 想做什么 | 操作 |
| --- | --- |
| 编译引擎 | Actions → **🔨 Engine Build** → Run workflow |
| 发布引擎快照 | Actions → **🏗️ Engine Release** → Run workflow（可填 `tag`，留空用 CI 侧默认） |
| 导出成品 | Actions → **📦 Project Export** → Run workflow |
| 发布项目成品 | 推一个 `v1.0.0` 这样的 Tag，或在 Actions → **🚀 Project Release** 手动运行并填 `tag` |

产物都落在**本项目仓库**的 Artifacts / Release。产物命名与保留天数由契约 `artifacts` 段
决定（说明见契约注释），改命名只改契约、不改工作流；引擎快照附件的 sha256 指纹写在该次
运行的 Summary 里。

触发器不带业务参数：参数一律来自契约文件，避免同一份参数表在两处维护；发布链只多暴露
一个 `tag`——发布到哪个 Tag 是当次运行的事实，不属于契约。确实需要临时改参数而不想动
契约时，到 CI 仓库直接运行被调工作流，那里才有这些输入项。

## 四、来源声明语法

契约里 `build.paths.profile`、`build.paths.build_profile`、`build.modules` 与覆盖入参
`profile` / `build-profile`、`project` 共用一套语法，**前缀必须显式写**，漏写会在解析
阶段失败：

```
remote:<owner/repo>[@<ref>][:<path>]    从 GitHub 仓库取
local:<path>                            项目仓库内的相对路径
```

- `remote` 的 `ref` 可以是分支名、Tag 或 40 位 commit SHA（锁版本用 SHA），省略即默认分支；
  `path` 省略即仓库根，指向文件还是目录由具体字段决定（见契约注释）。
- `local` 没有 `ref` 维度：检出用的 ref 由触发上下文统一决定，条目里再写一次只会造成漂移。
- 前缀必须写，因为两种形态存在字面重合；全部前缀化之后，漏写前缀变成明确报错，而不会被
  悄悄解释成另一种来源。

`engine.source` 恒为远程仓库，直接写裸值 `<owner/repo>[@<ref>]`（引擎源码就是整个仓库，
没有子目录定位）；`export.paths` 三个字段与 `hooks.manifest` 恒为项目内相对路径，同样写裸值。

## 五、锁版本与 CI 仓库搬家

默认 `@main` 跟随 CI 仓库更新，平时无需维护任何 SHA；同一次运行的全部 CI 资产检出自
同一 commit。仅两种情况要动坐标，变更面与漏改拦截见 CI 仓库内 `.github/templates/README.md`
的「锁版本与搬家」节（按你锁定的 ref 查看对应版本）：

- **锁版本**（让历史构建可复现）：把相关 ref 换成同一 40 位 commit SHA。
- **搬家 / fork 后自用**：把各文件里的 CI 仓库坐标换成新地址。

两处都有机器校验兜底：解析阶段断言 `$schema` URL 的 ref 段 = `ci.ref` = 调用方 `uses:@ref`
三者一致，静态校验另查模板各文件一致——漏改一处当场报错，不会静默漂移。

## 六、改用单仓形态

不把 CI 当独立仓库、而把整套 CI 资产（`.github/contract`、`.github/actions`、`.github/scripts`、
`.github/workflows`）放进项目仓库自用：

1. 删掉契约里的 `ci` 整段；
2. 把四个触发器的 `uses:` 改成调用本仓库的工作流路径（如 `./.github/workflows/build.yml`），
   并去掉 `caller-uses-ref` 输入。

## 七、延伸阅读

以上内容足以完成接入与日常使用。实现细节（工作流输入参数、契约边界、扩展机制、字段
清单、命名规则）都在 CI 仓库内、不随包分发，从仓库 README 进入、按你锁定的 ref 查看
对应版本：

https://github.com/JayWanX/godot_ci
