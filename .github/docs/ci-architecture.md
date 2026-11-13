# CI 架构与判据

CI 与项目的边界、职责划分与可机读判据，兼收四链内部机制（编译参数注入、引擎源码兼容、缓存键、两阶段执行器）。怎么用见 [workflows.md](workflows.md)，扩展机制见 [hooks.md](hooks.md)，命名规则见 [naming.md](naming.md)，契约目录与固定 secret 名见 [contract/README.md](../contract/README.md)，维护者规范（校验入口、`.github/` 布局）见 [maintenance.md](maintenance.md)。

## 核心原则与判据

> **CI 代码不内嵌任何特定项目的业务逻辑 / 路径 / 版本；项目相关的一切通过一份稳定、机器可读的公共契约进来。**

1. **字面量判据**：CI 本体不得出现特定项目的仓库名、引擎版本、项目侧文件路径、产物名前缀、secret 具体值；这些只允许出现在 `contract/` 与单测夹具。
2. **切换判据**：只改契约、不改 CI 代码，即可切换引擎来源与版本、项目侧路径、工具链版本、构建机（runner）、产物命名与保留、启用的扩展。
3. **接入判据**：新建空仓库，只放一份契约 + 4 个薄触发器（build / export / project-release / engine-release）+ Godot 原生文件，即可跑通四条链。

判据只管「什么进契约」。CI 自身的布局常量、action pin、固定 secret 名、SDK 脚本名留在 CI 代码。runner 归属「项目可覆写、CI 维护者按引擎演进调默认」：默认值在平台基线（构建机 glibc / SDK 决定产物最低运行环境，**覆写 runner = 覆写产物最低运行环境，项目自担**）。契约的覆写入口按维度分两段：`platforms`（平台级字段）与 `roles`（链级字段），两段的字段清单以 `contract/contract.schema.json` 对应段的 `description` 为准，本文不复述。`resolve` 不在任何契约段内——resolve 作业、self-check 与 prefetch 的 runner 分配先于契约可读（runs-on 求值不依赖任何 step 输出），由基线 `roles.resolve` 唯一决定，仲裁见 `workflows/runner.yml`。

## 部署形态

两种形态，检出路径都由 `job.workflow_repository` / `job.workflow_sha` 自描述——它们是「正在运行的这份工作流所属仓库与 commit」，跨仓时解析到 CI 资产仓库、单仓时解析到项目仓库自己，同一套代码在两种形态下都正确。

| 形态 | 布局 | 契约 `ci` 段 |
| --- | --- | --- |
| **跨仓**（推荐） | CI 资产住独立仓库，触发器住项目仓库：薄触发器 `uses: <ci.repository>/.github/workflows/build.yml@<ci.ref>`。项目仓库只留一份契约 + 该触发器 | **必填**：用于断言 `$schema` URL 的 ref 段 / `ci.ref` / 触发器 `uses:@ref` 三者一致，缺失时跨仓场景 fail-fast |
| **单仓自用** | 全套 `.github/` 在项目仓库内，项目自己触发 `build.yml` | **可省略**：没有 `uses:@ref` 可比对，无需断言 |

`ci.ref` 默认 `main` 跟随更新，换 40 位 SHA 即锁版本；同一次 run 的 CI 资产检出同属一个 commit，由 runner 把引用解析为运行时 commit 保证。`dependabot.yml` 随 CI 资产所在仓库自持。契约 `ci` 段承担两件事：resolve job 的一致性断言（用 `job.workflow_repository` / `job.workflow_sha` 自描述本次运行所属仓库），以及下游 job 的检出坐标（`ci.repository` + `ci.ref` 分叉检出 CI 资产）；单仓自用可省略该段，此时下游回落到 `job.workflow_*`。**CI 资产检出**的落点是工作区下的 `ci-support/`（composite 内部相对路径依赖该布局，每个 job 各自 checkout），鉴权一律 `GIT_PAT`（缺省回落 `github.token`），私有 CI 资产仓库需在组织 / 仓库设置允许被复用。

## 职责划分

**CI 仓库是加工方，不承担发布职责。** 两条并列的构建路径，按「构建对象是自己还是别人」区分，职责严格分开：

| | 自有项目构建（默认） | 外部项目构建 |
| --- | --- | --- |
| 用途 | 项目自己出成品、自己发 Release | 便捷获取外部仓库的导出产物 |
| 触发点 | 项目仓库的薄触发器 / 手动 / API 打给项目仓库 | CI 仓库的 `workflow_dispatch` / `repository_dispatch` |
| 构建导出对象 | 只能是自己（`github.repository`） | 由 CI 侧 `project: remote:<owner/repo>[@<ref>]` 指定 |
| `project` 输入 | 不暴露，恒等于自己 | 保留，仅 CI 仓库侧可填 |
| 契约 / Godot 原生文件 / `build/` / 扩展脚本 | 全在自己的仓库 | 从 `project` 指向的外部项目仓库读取 |
| 产物去向 | Artifact + Release 都在自己仓库 | 仅 Artifact，落在 CI 仓库 |
| Release | 有（`project-release.yml` 推 `v*` Tag） | 无：不调用 `project-release.yml`，不创建任何 Release |
| token | 自己的 `GITHUB_TOKEN` | 自己 `GITHUB_TOKEN` 上传 Artifact；读源码用只读 `GIT_PAT` |

1. **触发器仓库只能指向自己**：项目仓库侧不暴露 `project` 输入，构建对象恒由 `github.repository` 推导，Release 落点恒为自己。契约归属与 `context.project.root` 因此唯一。
2. **外部项目只由 CI 仓库指定**：`project` 输入（来源声明 `remote:<owner/repo>[@<ref>]`，build 链另接受 `local:<path>`）只在 CI 仓库侧可用，由**导出链**（`export.yml` 的 `workflow_dispatch` / `repository_dispatch`）承载，再经 `workflow_call` 下传给构建链编模板；构建链自身的手动入口不暴露该输入（编引擎与项目无关）。外部项目构建只做构建与导出，产物落 CI 仓库的 Artifact，不发布。
3. **CI 资产来源是代码来源，不是角色**：CI 代码住独立仓库，由契约 `ci.repository` / `ci.ref` 与薄触发器 `uses:@<ref>` 指定；它不持外部项目写凭证、不产出外部项目的 Release、不提供契约。
4. **Release 不是可配项**：只有自有项目构建（推 `v*` Tag）触发 `project-release.yml`，落点恒为 `github.repository`；契约里没有 `release` 段，`tag` 恒取触发上下文。
5. **引擎快照发布是链级能力**：`engine-release.yml` 复用构建链编引擎（构建入参留空读契约、填了覆盖）、把产物上传为 `github.repository`（触发仓库）的 Release 附件；它不接收 `project` 输入、不改变「外部项目构建不创建任何 Release」的边界，与 `template-release.yml` 发布模板包同属 CI 自有内容 / 自有构建的发布。

## 跨仓库权限

`github` 上下文与 `GITHUB_TOKEN` 绑定**触发仓库**，不是被调用 workflow 所在的 CI 资产仓库；token 是仓库粒度，不能跨仓写。

| 场景 | 需要的权限 |
| --- | --- |
| 自有项目构建，Artifact + Release 落自己 | 薄触发器声明 `permissions: contents: write` |
| 外部项目构建，Artifact 落 CI 仓库 | `GIT_PAT` 对外部项目 `Contents: Read`；Artifact 用 CI 仓库自己的 `GITHUB_TOKEN` |
| 外部项目构建：私有外部项目检出 | 同一个只读 `GIT_PAT` |

规则：外部项目构建不需要外部项目 `contents: write`、不需要跨仓发布凭证，CI 侧静态清单里也没有对应 secret 名；`permissions` 不可被调用方提升，由触发方自行声明，`secrets: inherit` 同样显式；同一 run 内 artifact 上传下载正常，`actions/cache` 与 `concurrency` 按触发仓库 + ref 隔离。

## 跨仓库引用语法

两类 `uses:` 的解析基准不同，**混用会静默失效**：

| 形式 | 解析基准 | 跨仓库下的写法 |
| --- | --- | --- |
| job 级 `uses: ./.github/workflows/x.yml` | **顶层调用方仓库**（不是 callee） | `uses: $/.github/workflows/x.yml` |
| job 级 `uses: owner/repo/.github/workflows/x.yml@sha` | 显式仓库 + ref | 可用 |
| step 级 `uses: ./path/to/action` | runner 工作区 `$GITHUB_WORKSPACE` | 每个 job 各自 checkout 到子目录 |

1. **job 级同仓库 reusable workflow 调用一律用 `$/`**：`$/` 解析到「引用所在文件所属仓库」的当前运行 commit；跨仓库 called workflow 内部 `$/` 解析到 **callee（CI 资产仓库）**，同仓场景同样正确。`./` 在跨仓库下会去项目仓库找文件，报 `workflow was not found`。
2. **适用边界**：`$/` 不得带 `@ref`，GHES 不支持；若要支持 GHES 须改回 `owner/repo/.github/workflows/x.yml@<sha>` 显式引用。
3. **callee 身份**：called workflow 内 `github.*` 上下文绑定 **caller**（触发仓库）。取 callee 仓库与被 pin 的 commit 用 `job.workflow_repository` / `job.workflow_sha`（后者由 runner 从 `uses:` pin 计算，caller 改不了，可用于校验 pin 与实际运行版本是否一致）。

依据：GitHub 官方文档 [workflow-syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)、[reuse-workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)、[contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)。

## 公共契约

契约字段的唯一权威源是 `contract/*.schema.json`，任何文档不重复维护字段清单。目录文件清单、谁消费、固定 secret 名与契约校验方式见 [contract/README.md](../contract/README.md)；可复制样例见 [templates/ci-contract.json](../templates/ci-contract.json)。

**归属判据**按「谁会改它」：项目会改的值 → 项目契约 `ci-contract.json`；项目可覆写、但由 CI 维护者按引擎版本演进全局调整的默认值 → 平台基线；不随项目变化、也不服务切换判据的 → 留在 CI 代码，不进基线、不进 context；只在运行时确定的 → 运行时输入（`project` 仅外部项目构建、`tag` 仅自有项目构建、secret 值）或只读 context。

**默认值单一来源**：默认值只写在 schema `default` 与平台基线；CI 代码不写 `or` 兜底常量。产物保留天数同理：schema `artifacts.retention.<类>` 是唯一源，`resolve` 恒产出 `retention-<类>` 输出并逐层下传，各 action 的 `retention-days` input 默认值只是「绕过 resolve 直调」时的兜底，不构成第二份权威值（一致性由静态校验断言）。

**执行形态可配**：平台基线 `platforms.<平台>` 段的字段清单以 `contract/contract.schema.json` 的 `platforms` 段描述为准，本文不复述；构建与导出矩阵直接从该段取值。默认导出全部走 Linux 交叉导出（成本最低）；需要代码签名的平台把 `export_runner` 与 `editor_archive` 改成原生即可，**不动任何 workflow step**。iOS/visionOS 因导出器依赖 macOS 工具链（codesign / xcrun），恒为原生。链级作业（publish / upload）的 runner 在基线 `roles` 段，契约可按同名键覆写。

**`compat.standard_includes`**：`enum: [auto, on, off]`，默认 `auto`。`auto` 跟随平台基线——底值取基线 `compat.standard_includes.default`，命中基线 `skip_rules`（按 engine ref + toolchain + platform 标注）时恒 `off`（规则单向关闭）。判据数据落基线，resolve 归一化为**按平台的布尔映射**（`{platform: on|off}`，编译目标平台不同、所需标准头不同）并同时给两种形式：整体 JSON 映射（供诊断与 context）与按平台各一个标量（`compat-standard-includes-<platform>`），各平台作业只取自己那个标量——把 JSON 映射直接传给下游再与 `'off'` 比较永不成立，分档会静默失效。判定所需数据缺失 → fail-fast 并指出缺哪条分档；`ensure_standard_includes.py` 的 `--platform` 由 context 传编译目标平台。

**来源声明的校验分层**：语法（前缀存在性 / ref 形态白名单 / `path` 段无 `..` 与前导 `/`）在 resolve 期；模块根含可解析的 `register_types`、配置文件存在在检出后校验；仓库与对象可达性在构建期。ref 白名单唯一实现在 `ref_guard.py`（resolve 期与构建期执行器共用；构建期以列表参数调 git，无 shell 解释面）。语法本体见 [templates/USAGE.md「来源声明语法」](../templates/USAGE.md#五来源声明语法)。

## 编译参数注入（内部）

`resolve_scons_flags.py` 统一判定需补传的命令行标志，`detect-deps` 与 `compile` 共用：`platform_flags` 会覆盖 profile 同名赋值，故 profile 显式写了、而 `scons-flags` 未给该键具体值时，补传命令行同名值压住覆盖；非 Windows 强制追加 `d3d12=no`；`scons-flags` 显式值始终优先。SDK 相关 flag 的安装判定只发生在 `detect-deps`（依据 `SDK_FLAG_OUTPUTS`），晚于它的注入不会触发安装。Linux 作业需安装 OpenGL/X11/ALSA 构建依赖（`libgl1-mesa-dev` 等）以编译 `opengl3` 驱动。

**输入校验、注入防护与并发组**：`resolve` 对 `target` / `platform` 做白名单校验，并按平台 × target 支持表（`godot_context.PLATFORM_TARGET_SUPPORT`）逐组合拒绝（如 web+editor 在 resolve 期拒绝）；用户可控输入经 `env:` 传入脚本以避免表达式注入，打包等 `run:` 步骤仍有内联 `${{ }}`，其中 `matrix.*` 为工作流内静态常量。并发组以 ref 为基础：`build.yml` 追加引擎来源（`inputs.engine-source`，手动 / 被调用无值时取 `client_payload.source`，都没有才落 `default` 占位），`export.yml` 追加导出类型作为区分维度——同一分支上不同平台 / 不同导出模式的调试互不取消；前两者同组重复触发排队执行（`cancel-in-progress: false`），两条发布链相同 ref 的新触发取消进行中的旧运行。

## 引擎源码兼容性处理

所有构建作业在编译前运行 `build-pipeline` 内的 `ensure_standard_includes.py`（导出链的 custom_build 同样执行）：为依赖标准库传递包含的引擎源码补齐缺失头文件（如 `<memory>` / `<mutex>`），兼容新版编译器与精简配置（如 `threads=no`）。幂等：已自包含的文件零修改，仅插入头文件；匹配先剥离注释与字符串字面量，注释里的标识符不会误触发注入；对可能被按 C 编译的头（同目录存在同名 `.c` 兄弟文件，如 `thirdparty/ufbx/ufbx.h` 之于 `ufbx.c`），C++ 标准库专用头无论已存在还是新注入均用 `#if defined(__cplusplus)` 门控，仅 C++ 构建引入。引擎引入脚本未覆盖的新缺失头时，编译错误会直接暴露，扩展脚本内 `HEADER_FAMILIES` 映射即可。开关档位与 `--platform` 的来源见上文 `compat.standard_includes`。

## 缓存键

```
CACHE_KEY    = <platform>_<target>[_<arch>]|<engine-sha>|<config-hash>|<source-hash>|<hooks-sha>
config-hash  = sha256(profile, gdbuild, modules_tree, base_scons_flags, encryption_key,
                      plan_json, hook_output_digests[cache_impact=config])[:8]
source-hash  = sha256(engine-sha, source_patch_digests,
                      hook_output_digests[cache_impact=source])[:8]
```

- `source_patch_digests` 取**补丁文件自身的 sha256**（`directives/patches/` 下每个补丁的路径与内容摘要，按路径排序），不是补丁应用后被改文件的摘要——后者会随应用顺序变化，且无法区分「同一补丁」与「不同补丁改出同样结果」；由执行器写入 `hooks-result.json` 并进 `hook_output_digests["source"]`。
- 键为 5 段，`cache-restore` 与 `cache-save` 的 key **必须同步加段、段序一致**，否则永不精确命中；`engine-sha` 由 resolve 用 `git ls-remote` 按 ref 名精确解析，失败直接报错。缓存恢复失败不影响构建（`continue-on-error`）。
- **`restore-keys` 按 `cache_impact` 条件化**：存在 `cache_impact=source` 的 hook 时前缀含 `<source-hash>|`，其余保持 `<platform>_<target>|<engine-sha>|`。精确键不匹配时 `actions/cache/restore` 必然前缀回退，把 `source_hash` 只放进精确键对回退路径零作用；但收紧前缀有代价——SCons `cache_path` 是按编译单元内容哈希索引的对象缓存，按层收紧前缀会失去跨配置增量复用、首次全量重编。
- **hooks-sha 作独立第 5 段**便于诊断与让 restore-keys 按层包含；启用且清单非空时该段必须变化，关闭扩展时该段省略、`plan_json` 为空常量，键与未启用时一致。`cache-save` 条件为 `hooks-sha == '' || post-build == 'ok'`，无清单时归一化 step 被 `if` 短路，靠 `cache-save` 的 `post-build` 输入 `default: "ok"` 兜底。
- **不含仓库名**：缓存按引擎维度归集，不同项目用同一引擎与同一配置时共享缓存；artifact 名仍带 `<repo-safe>_` 前缀，两者刻意分离——但 `actions/cache` 实际按触发仓库 + ref 隔离，跨项目共享语义在 GitHub 侧不成立。export 链没有缓存键对象（7 个导出 job 不编译、不调 config_hash），分层键只作用于 build 链，export 链由 `hooks-result.json` 记录 `plan.json` / `hooks-sha`。

## 两阶段执行器

1. **validate + bundle**（仅 resolve 期，离线、不执行脚本）：JSONC → schema → 锚点表结构（字段拼错 / 缺 phases / 重复 id）→ 锚点存在性 + phase 门禁 → 能力合法性 → `with.inputs` 依赖闭包递归收集（Python import 静态扫描）→ 解释器白名单 → 路径逃逸 / 大小 / 超时上限 → 策略决策 → 按 chain 产出 `plan.json`（不含运行期字段，声明式过滤留给 dispatch，保证确定性）→ 打 bundle（摘要覆盖 plan / 闭包 / 锚点表 / 策略）→ 上传为 `<prefix>_<chain>_hooks_<sha8>` → 输出 `hooks-bundle` / `hooks-sha` 两个字符串。
2. **dispatch + collect**（job 期，锚点边界，通用 `hooks` composite）：取 `plan.json` 当前 `(anchor, phase)` 的 hooks → 对声明写面建快照（`dist/` 与 `directives/` 两个通道根 + `write_paths`；内存只留元数据 `类型/大小/摘要`，文件内容备份到 `$RUNNER_TEMP`，内存占用与产物大小无关）→ 起子进程（只读挂载 context、只注入声明的 secret、**不继承父 job 全环境**、不禁网）→ 收集各通道输出 → 按输出 schema 校验 → 应用声明式 patch（带快照）→ **post-diff 检查未声明写入**（含项目树全树比对）→ 失败即回滚（删除新增、还原备份）→ 写 `hooks-result.json`。

- **回滚边界**：声明写面内可完整回滚（预存文件有磁盘备份、新增文件删除即可）；**声明写面之外的越界改写无法自动还原**——执行器对越界只有元数据快照、没有内容备份，只能删除越界新增条目，被改写的预存文件保持被改后的状态，失败信息列出这类路径，需人工排查。
- **空转必须真正零成本**：resolve 输出 `hooks-bundle` 为空串时，各 job 用 `if: hooks-bundle != ''` 跳过整条扩展链——不下载 bundle、不生成 plan、不跑任何 dispatcher、`config_hash` 参数里根本不出现 `plan_json` 与 `hooks-sha`。
- **传值与合并时机**：`hooks-result.json` 是唯一跨 step / 跨 job 通道；composite `inputs.*` 运行期不可回写，`write:scons_flags` 的合并结果经 `hooks-result.json` 写 `merged_flags`，由 `normalize_hooks_result.py` 归一化后分发给 config_hash、detect-deps、编译三个消费点；`write:presets` 由 `preset-clean:after` dispatch 之后的 `apply_hook_outputs.py --kind presets` 应用（不让脚本原地覆写），预设名不存在 fail-fast；`write:project_tree` 的写入集文件摘要并入 `hook_digests[cache_impact]`。export / release 链只做快照与回滚。打包在 composite 返回之后执行，钩子在 `post_build` 新增的文件不会被自动打进产物；`write:artifacts` 在 `package:before` 合并进 dist 根（散文件由导出 job 打包为平台级附加包 `export-<平台>-extra`）。
- **同名与杂项**：`export.yml` 复用 `build.yml` 时两边 resolve 在同一次 run 上传同名 `hooks` artifact 会被 `upload-artifact` v4+ 拒绝，bundle 按链前缀区分；`hooks-result.json` 的 `outputs` 按输出名索引，同名输出**后执行者覆盖前者**（数组书写顺序即执行顺序），每个钩子的明细仍按 `id` 留在 `hooks_outputs`，冲突可追溯。`::error::` 拼接必须转义；导出链 sparse-checkout 白名单须显式列出实际需要的目录（hooks bundle 自带执行器脚本，不依赖 `config-inject`）；导出 job 无构建依赖，解释器由 CI 按固定名指定，不用裸 `python`；外部项目构建的临时克隆目录统一为 `$RUNNER_TEMP/project`（两个 resolve 同名），清单解析基准固定 `context.project.root`，由 resolve 显式提供。

## 产物命名渲染（内部）

渲染器是 `.github/scripts/artifact_name.py`。`build_naming` 的消费者不止 build 链：`custom_build` 模式下 export 链按同一模板渲染下载 pattern 取回编译产物，故模板归属「产物」而非「生产它的链」。模板经 resolve 归一化后作为 workflow input 传到各平台工作流，平台 job 内渲染成 `env.ARTIFACT`；导出产物名由 resolve 一次渲染并经 `packages-<平台>` 矩阵下传到 upload job（`matrix.package`），workflow 里不得自行拼 zip 名（静态校验 `check_export_package_naming_centralized` 守着）。未知占位符 resolve 期 fail-fast；渲染结果对全部选中预设**全局去重**，同名直接 fail-fast——Release 附件同名会互相覆盖。

## 安全与信任

- 凭证一律走环境变量：CI 按固定 secret 名从 `secrets` 上下文取，值不进契约、不进 context、不落日志、不写进缓存键；敏感值走进程级 env，不落 `GITHUB_ENV`。CI 仓库只持有外部项目的只读凭证。
- 清单信任 `off | declared` 由契约 `hooks.policy` 声明、CI 侧设上限；钩子与 workflow 同仓同 PR 评审，不引入签名体系。
- 外部项目构建下清单来自外部项目，会在 CI 仓库的 runner 执行：resolve 期强制 `hooks-policy=off`，不读取/执行外部清单（判据在 `godot_context.resolve_hooks_policy`）。隔离不成立，边界靠「不执行」（机制细节见 [hooks.md](hooks.md#安全与信任)）。
