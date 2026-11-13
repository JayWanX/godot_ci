# 扩展机制（钩子）

项目侧脚本订阅稳定锚点，在 CI 内部阶段边界前后执行，只读写声明面。边界与判据见 [ci-architecture.md](ci-architecture.md)，锚点时机怎么用见 [workflows.md](workflows.md)，命名规则见 [naming.md](naming.md)；执行器内部实现（bundle 打包、快照备份、缓存键分层、错误回滚机制）见 [ci-architecture.md](ci-architecture.md#两阶段执行器)。

## 目标与边界

扩展机制提供**稳定、机器可读的公共契约面**：锚点注册表 + 只读上下文 + 按能力授权的可写面。CI 本体的「零项目常量」由 [ci-architecture.md](ci-architecture.md) 保证。

外置的是「订阅稳定锚点、只读写声明面的扩展」，不是「知道 CI 内部路径的脚本」。脚本不绑定检出布局，CI 内部重构不破坏扩展。新增锚点 = 注册表加一行 + 对应位置插一个 dispatcher，接线完整性由 `check_ci_assets.py` 双向校验守门——漏插 dispatcher 或先接线后登记都会当场失败。

**不属于扩展机制**（由 CI 架构治理）：引擎版本 / 工具链 / runner / 产物命名 / retention / secret 名字约定 / Release 落点。`ensure_standard_includes.py` 是内置的 `compat.standard_includes` 能力，由契约 `build.compat` 声明启用。

## 总体机制

1. **清单在项目侧**：`.github/ci-hooks.json`（JSONC，带 `$schema`），路径与策略由契约 `hooks` 段声明。文件不存在 = 不启用。
2. **锚点数据化**：CI 维护 `.github/contract/hooks-anchors.json`（唯一权威源），执行器不内置槽位列表；清单 `on: {anchor, phase}` 订阅。
3. **bundle 分发**：resolve 一次性校验清单、递归收集依赖闭包、产出执行计划并打包上传为 `hooks-bundle`（打包机制见 [ci-architecture.md](ci-architecture.md#两阶段执行器)）；下游只传一个 artifact 名 / 摘要字符串。
4. **两阶段执行器**：`validate`（resolve 期，离线，含 plan）→ `dispatch + collect`（job 期，锚点边界）；实现细节见 [ci-architecture.md](ci-architecture.md#两阶段执行器)。
5. **契约面**：只读 `context.json`；可写面 = 产物通道（`dist/`）+ 指令通道（`directives/`）+ 项目树通道（`write_paths` 声明路径），按 `capabilities` 逐项授权。未声明写入即 fail 并回滚。

## 锚点注册表

锚点 = 稳定 id，与 step 解耦（调度按 anchor id）。条目字段：`chains ∈ {build, export, release}`（可多值）、`phases` = 该锚点已接线的 phase 全集（**全显式无缺省**——缺省「双向都支持」会让忘填重新打开静默陷阱）。锚点 id 表见 `contract/hooks-anchors.json`，新增或移除直接改该表并同步清单；步骤重命名不动锚点 id。

保留的锚点均为有业务语义的外部可观察点；`deps` / `compat-patch` / `checkout` / `cache-save` / `upload` / `runtime` / `import` 等 CI 内部步骤不作为锚点发布，需要时再加一行。

**两道校验闭环**：`run_hooks.py validate` 校验钩子的 `on.phase ∈ 条目 phases`（声明了未接线组合会静默空转，故 fail-fast）与表结构（字段拼错 / 缺 phases / 重复 id 显式拒绝）；`check_ci_assets.py` 做**接线完整性双向校验**——表内每个 `(id, phase)` 组合必须在 actions/workflows 有 dispatcher 挂点，挂点必须在表内。

## 清单 schema

**唯一权威源**：`.github/contract/hooks.schema.json`。下面只写 schema 表达不了的语义约束，完整取值见 schema；可复制样例见 [templates/ci-hooks.json](../templates/ci-hooks.json)。

| 字段 | 语义约束 |
| --- | --- |
| `id` | 清单内唯一；只用于日志与错误定位，不承担去重 |
| `on.anchor` / `on.phase` | 锚点须存在于注册表，且 `phase` 须为该锚点已接线的值（未接线组合 validate 期拒绝） |
| `on.platform` / `on.target` / `on.chains` / `on.presets` | 声明式过滤，省略或空名单 = 全部；resolve 期只按 chain 分组产确定性 plan，求交判定在 dispatch 期。`on.presets`：逐预设锚点按 `current_preset` 判定，整批锚点按本次实际导出名单（`context.export.presets`）求交，名单为空（导出全部预设）不过滤；它只在导出链有意义，订阅到不含 export 的链 validate 期拒绝。不提供表达式语言，不 eval |
| `run` | 相对 `context.project.root`；禁绝对路径 / `..` / 规范化后逃逸 / 符号链接；解释器按扩展名白名单 |
| `with.inputs` | 依赖闭包显式列出（helper / 数据 / 子脚本）；resolve 期递归扫描 Python import 并校验 |
| `capabilities` / `outputs` | `outputs` 必须是 `capabilities` 能产出的子集；未知值 resolve 期 fail |
| `write_paths` | 项目树写入路径（项目根相对，`dir/**` = 整棵子树）；须与 `write:project_tree` 能力成对声明，缺一即 validate 拒绝；路径禁绝对 / `..` / 逃逸 |
| `cache_impact` | `none\|config\|source`，决定输出进入哪层 hash（分层缓存键定义见 [ci-architecture.md](ci-architecture.md#缓存键)） |
| `on_error` | `fail`（默认）\| `warn`；`warn` 必须进 job summary |
| `timeout_minutes` | 单钩子超时分钟数，1–15，默认 5；超时杀进程组 |

`read:context` 默认授予，不写进清单。**数组书写顺序即执行顺序**：不提供 `needs` / `order` / `parallel` / 拓扑排序——并行会引入 `directives/scons_flags` 写竞态与快照回滚翻倍，故固定串行。

## 只读上下文

schema 是 `context.schema.json`，由 resolve 归一化契约 + 基线生成，**只读挂载** + 只读 env 前缀 `GODOT_CONTEXT_*`。只保留面向钩子的字段；字段清单以 schema 为准，本文不复制。

- `secret:<NAME>` 用固定 secret 名清单（见 [contract/README.md](../contract/README.md)），不来自 context。
- `paths` 由 CI 显式提供，脚本只从 `context.paths.*` 取，与链无关——各链检出布局不同，硬编码布局会在换链时失效。`paths.engine_root` 按链如实：build 链 job 期为引擎检出根；export 链工作区无引擎源码、恒为空（「未配置」是事实）；release 链为空。

**context 的 platform / target 可为 null**：多平台批的 resolve 期与 release 链为 null（= 不过滤）。`on.platform` / `on.target` 过滤对 null context 视为放行（与省略同构）——否则 release 链「声明了平台过滤」的钩子会静默漏跑；单平台 job 期为单值，照常求交。

## 可写面

三条并列的写入通道，语义不同，不可互换。**通道根名即性质**：`dist/` 里的东西会被打包搬走交付，`directives/` 里的东西作用于既有对象、消费后即丢弃，项目树通道直接改项目文件。

| 通道 | 声明 | 落点 | 坐标系原点 | CI 是否消费 |
| --- | --- | --- | --- | --- |
| **产物通道** | `capabilities` + `outputs`（`artifacts`） | `dist/artifacts/` | `GODOT_HOOK_DIST` | 是：编排层合并进导出产物目录并打包上传 |
| **指令通道** | `capabilities` + `outputs`（`scons_flags` / `*_patch` / `presets`） | `directives/` 下的约定目标 | `GODOT_HOOK_DIRECTIVES` | 是：应用到既有对象（编译参数 / 文件补丁 / 预设文件）后丢弃 |
| **项目树通道** | `write:project_tree` 能力 + `write_paths` 路径 | 直接写 `context.project.root` 内声明的路径 | `GODOT_HOOK_PROJECT_ROOT` | 否：执行器只做快照、放行、失败回滚、build 链摘要 |

**坐标系原点必须从对应变量取，不能用 `__file__` 推算**：脚本被打进 bundle 后位置语义已变。声明 `write:project_tree` 的钩子若闭包未引用 `GODOT_HOOK_PROJECT_ROOT`，validate 期直接拒绝——接口完整性由静态校验保证，不依赖约定。

两条输出通道的目标见 `contract/hooks-capabilities.json`：`artifacts` 落 `dist/`，其余（编译标志 / 补丁 / 预设覆盖）落 `directives/`。

| 能力 | 可写 target | 输出 | 通道 | 消费 / 约束 |
| --- | --- | --- | --- | --- |
| `read:context` | — | — | — | 默认授予 |
| `write:artifacts` | `dist/artifacts/` | 文件树 | 产物 | 执行器在 `package:before` 合并进 `context.paths.dist`；散文件由导出 job 打包为平台级附加包 `export-<平台>-extra`；Release publish 收全部 `export-*` 附件 |
| `write:scons_flags` | `directives/scons_flags` | `{flags:[{key,value}]}` | 指令 | 执行器在 `config-hash:before` 合并进 `context.cache.scons_flags_base` 并重算 config_hash；同时喂给 detect-deps 与编译 |
| `write:profile_patch` | `directives/patches/profile.*.diff` | unified diff | 指令 | 编排层在 `config-hash:before` 应用到注入的 profile.py 并快照 |
| `write:source_patch` | `directives/patches/source.*.patch` | git-apply 兼容 | 指令 | 编排层在 `cache-restore:before` 应用到引擎源码根，纳入 `source_hash` |
| `write:presets` | `directives/presets.json` | 声明式字段覆盖 `{预设名: {字段: 值}}`；字段含 `/` 落 options 段 | 指令 | `preset-clean:after` dispatch 之后应用到 `export_presets.cfg`（不让脚本原地覆写）；预设名不存在 fail-fast |
| `write:project_tree` | `write_paths` 声明的项目内路径 | 无通道产出 | 项目树 | 直接写项目树（如装配 `python/` 运行时）；须与 `write_paths` 成对声明且脚本引用 `GODOT_HOOK_PROJECT_ROOT`，缺一即 validate 拒绝；未声明路径的写入仍判「未声明写入」 |
| `secret:<NAME>` | 不落盘 | 仅该 hook 进程 env | — | NAME 须在 `hooks-capabilities.json` 的 `secret_names` **按链分组表**：不在全集即拼错被拒，不在当前链的组即按链拒绝（消息含链名）；分组与「适用链」见 [contract/README.md](../contract/README.md#固定-secret-名ci-公共接口不进契约)。值由调用方 job 级 env 注入，执行器按声明挑出；声明了却取不到值 dispatch 期直接失败；进程结束即失效 |

**`write_paths`（项目树通道）**：条目为项目根相对路径，`dir/**` 表示整棵子树、`path` 表示该文件或目录，项目根绝对路径由 `GODOT_HOOK_PROJECT_ROOT` 提供。**build 链**会把写入集的文件摘要并入缓存摘要——直接改构建输入（如 `build/profile.py`）必须让缓存键跟着变，否则会命中旧键产出与源码不符的包；export / release 链只做快照与回滚（快照与回滚机制见 [ci-architecture.md](ci-architecture.md#两阶段执行器)）。

`hooks-result.json` 是唯一的跨 step / 跨 job 传值通道，`GITHUB_ENV` 不是钩子的可写通道。

## 锚点语义

**逐预设与整批**：`export-preset:after` 整批导出完成后触发一次；`export-preset-each:after` 每个预设导出成功后触发一次（dispatcher 在导出循环内调用，`--current-preset` 注入 `context.export.current_preset`，`on.presets` 按当前预设过滤）。逐预设钩子订阅 `export-preset-each`，整批钩子订阅 `export-preset`。

**`preset-clean` 顺序**：需要改预设字段的钩子必须在 `preset-clean:after`（预设清洗原地覆写之后），否则改动被冲掉。

**`write:scons_flags` 时机**：`scons-flags` 被 config_hash、detect-deps、编译三处消费，detect-deps 位于 SDK 安装之前——钩子必须挂 `inject-config:after`，晚于它 SDK 已判完（如 `d3d12=yes` 不会触发安装 → 编译必失败）。

## 错误与回滚

- 失败即回滚（快照 / 备份与回滚边界见 [ci-architecture.md](ci-architecture.md#两阶段执行器)）；失败信息统一 `::error::` 带 `anchor + id + script + exit + stdout tail`。
- `on_error: fail`（默认）中止并回滚；`warn` 回滚、记 job summary、继续。
- **事务后果**：build 链 post 钩子失败 ⇒ 不保存缓存；export 链 post 钩子失败 ⇒ 不上传。
- 无隐式兜底：一切失败处理由 `on_error` 强制显式声明。

## 安全与信任

- **策略随 bundle，不逐层透传 bool**：CI 侧只留 `hooks-policy: off|declared`（缺省 `off`）；契约 `hooks.policy` 声明期望值，省略该段即按 schema 缺省走 `off`。resolve 期一次性决策并写进 bundle；下游只传一个 artifact 名 / 摘要。
- **两档信任**：`off` 为缺省档（忽略清单，空转零成本），`declared` 为显式启用档（清单在项目仓库内、经评审、resolve 期静态校验）。钩子与 workflow 同仓同 PR 评审，且与触发器同仓，不突破 GitHub 既有信任边界。`write:source_patch` 与 `secret:<NAME>` 同在 `declared` 档下放，约束：清单显式声明（secret 名另受注册表白名单）+ 仅该进程内注入。
- **进程级隔离**：钩子起子进程，受限 env / 不继承父环境 / 最小 `GITHUB_TOKEN` 权限。**不代为禁网**：联网是常见需求（装配运行时、拉依赖、上传校验和），网络边界由 job 与触发形态治理，不由执行器代管。
- **跨仓库清单来源**：外部项目构建（项目仓库 ≠ 触发仓库）时强制 `hooks-policy=off`，不读取/执行外部清单（判据与边界见 [ci-architecture.md](ci-architecture.md#安全与信任)）；自有项目构建的清单只在触发仓库内、经同仓 PR 评审。
- **跨仓库 token 归属**：`GITHUB_TOKEN` 属于触发仓库（自有项目构建是项目仓库，外部项目构建是 CI 仓库），不是被调用 workflow 所在的仓库；`permissions` 由触发方声明、被调用方不可提升。职责分离见 [ci-architecture.md](ci-architecture.md#职责划分)：外部项目构建只产 Artifact、不发布，CI 仓库不持有外部项目写凭证。
