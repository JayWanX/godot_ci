# Godot CI 触发器模板 $TAG（$SHA）

项目仓库接入 [godot_ci](https://github.com/JayWanX/godot_ci) 所需的全部文件，解压到项目仓库根即可，无需重命名任何文件。

## 包里有什么

```
.github/
├── CI-USAGE.md             # 完整使用说明（含来源声明语法、锁版本与搬家）
├── ci-contract.json        # 项目契约：唯一需要改的文件
├── ci-hooks.json           # 扩展清单样例（可选，不用就删）
└── workflows/
    ├── build.yml             # 编译引擎 → Artifacts
    ├── export.yml            # 导出项目成品 → Artifacts
    ├── project-release.yml   # 推 v* Tag → 导出并发布项目成品 Release
    └── engine-release.yml    # 编译引擎 → 发布引擎快照 Release 附件
```

## 接入三步

**1. 解压到项目仓库根**（项目里已有 `.github/workflows/` 同名文件时先备份）

```bash
unzip godot_ci_trigger_templates_$TAG.zip -d /path/to/your-project
```

**2. 按项目实际情况修改契约**（`.github/ci-contract.json`）
- 所有字段说明都写在契约文件的注释里。
- 扩展默认关闭，随包的 `ci-hooks.json` 不配置也能直接跑通。

**3. 提交推送**，四个触发器就会出现在 Actions 页面。

| 想做什么     | 操作                           |
| ------------ | ------------------------------ |
| 编译引擎     | Actions → **🔨 Engine Build**   |
| 发布引擎快照 | Actions → **🏗️ Engine Release** |
| 导出项目     | Actions → **📦 Project Export** |
| 发布项目成品 | 推一个 `v1.0.0` 这样的 Tag     |

## 完整文档

完整说明见包内 `.github/CI-USAGE.md`（必填项、产物命名、来源声明语法、锁版本与搬家）。
实现细节见 [godot_ci 文档](https://github.com/JayWanX/godot_ci/tree/main/.github/docs)。
