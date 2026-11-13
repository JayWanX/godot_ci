# 项目仓库侧模板

本目录**镜像项目仓库 `.github/` 的真实布局**：文件名与相对路径即复制后的最终位置，把本目录内容（除本 README）对拷到项目仓库 `.github/` 即落位，无需重命名。

本 README 是**维护者视角**的目录说明，不进入模板包；使用者拿到的是随包落位的 `USAGE.md`（包内为 `.github/CI-USAGE.md`，面向使用者：落位、必填业务值、运行与产物、来源声明语法；它是通用文档，不随接入项目实况变化）。

| 本目录 | 复制到项目仓库 | 复制后 |
| --- | --- | --- |
| `workflows/build.yml`、`engine-release.yml`、`export.yml`、`project-release.yml` | `.github/workflows/` | 默认路径零必改：`uses:` 已固化指向本 CI 仓库 |
| `ci-contract.json` | `.github/ci-contract.json` | 坐标零必改；业务值按项目填（引擎来源、导出预设、产物命名等，见模板内注释）。`hooks.policy` 默认 `off`，扩展不启用 |
| `ci-hooks.json` | `.github/ci-hooks.json`（可选，路径由契约 `hooks.manifest` 声明） | 随包样例；`policy=off` 时不读取，不删也能直接跑通。启用 = 改契约为 `declared` + 把 `run` 指向项目仓库真实脚本（样例脚本 `ci-hooks/*.py` 不在模板内，照抄不改会在 validate 期因依赖闭包找不到文件而失败） |
| `USAGE.md` | `.github/CI-USAGE.md`（打包时改名） | 使用者文档本体。改名是为了自带归属：直接叫 `USAGE.md` 落进项目仓库会被当成项目自身的用法说明 |

复制后项目仓库只留：契约 + 四个薄触发器 + 可选扩展清单 + Godot 原生文件（`project.godot` / `export_presets.cfg` / `build/`）。这是**跨仓形态**：CI 代码全部在 `ci.repository` 指定的独立仓库，按 `ci.ref` 引用。

**单仓自用**不复制本目录的 `workflows/`，而把 CI 资产整仓放进项目仓库、直接触发 `.github/workflows/build.yml`，并从契约删掉 `ci` 段。两种形态见 [ci-architecture.md](../docs/ci-architecture.md#部署形态)。

## 锁版本与搬家

默认 `@main` 跟随 CI 仓库更新，零 sha 维护：同一次 run 的所有 CI 资产检出同属一个 commit（runner 把引用解析为运行时 commit，见 `job.workflow_sha`）。仅两种情况要动坐标 / ref：

- **锁版本**（可复现的历史重跑）：模板与契约里所有指向 CI 仓库的 ref 坐标（触发器 `uses:` / `caller-uses-ref`、契约 `ci.ref`、两处 `$schema` URL 的 ref 段）换成同一 40 位 SHA，此后 CI 仓库每次更新须手动 bump。
- **搬家 / fork 后自用**：各文件里的 `JayWanX/godot_ci` → 新坐标（触发器 `uses:`、契约 `ci.repository`、两处 `$schema` URL 的 owner/repo 段）。

变更面用 `grep -n "main\|JayWanX/godot_ci"` 在落位的 `.github/` 下即可枚举；漏改由 `godot_context.py` 在 resolve 期断言（`$schema` URL 的 ref 段 = `ci.ref` = 调用方 `uses:@ref`，触发器以同值常量经 `caller-uses-ref` 传入）与 `check_ci_assets.py` 的模板一致性校验当场拦截，不会静默漂移。

## 扩展清单

`ci-hooks.json` 对拷落位即 `.github/ci-hooks.json`（契约 `hooks.manifest` 默认路径）。样例覆盖当前全部已接线锚点组合，复制后删除用不到的块；启停方式见 [USAGE.md「扩展清单」](USAGE.md#24-扩展清单默认关闭)，可写面与机制见 [hooks.md](../docs/hooks.md)，锚点与能力注册表见 [contract/hooks-anchors.json](../contract/hooks-anchors.json) 与 [contract/hooks-capabilities.json](../contract/hooks-capabilities.json)。

## 权限与 secrets

- `workflows/build.yml` / `workflows/export.yml`：`contents: read`（检出项目源码）。
- `workflows/project-release.yml` / `workflows/engine-release.yml`：`contents: write`（`github.token` 创建 / 上传 Release 到本项目仓库）。

`permissions` 由触发方声明、被调用方不可提升；`secrets: inherit` 需显式写出。CI 只认固定 secret 名（清单见 [contract/README.md](../contract/README.md)），项目自定义命名在薄触发器 `secrets:` 里映射一次，模板给的是映射示例，可按需删改。
