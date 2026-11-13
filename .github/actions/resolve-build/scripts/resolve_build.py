#!/usr/bin/env python3
"""Godot 引擎构建参数解析脚本（由 resolve-build action 调用）。

配置源是单一契约 ci-contract.json（经 godot_context.py 归一化）。
project 仅在 CI 仓库构建外部项目时由运行时输入传入。
输出（GITHUB_OUTPUT）供 build.yml 的平台 job 与下游复用。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from jsonc import escape_error, fail  # noqa: E402, fail
from ref_guard import assert_valid_ref  # noqa: E402
import godot_context  # noqa: E402
import artifact_name  # noqa: E402
import git_ops  # noqa: E402


def resolve_engine_sha(repo, ref):
    """引擎 ref → 40 位 SHA；解析失败直接 fail（不回退 ref_name）。

    40 位完整 commit SHA 直接采纳（不可变标识，白名单天然通过）：resolve 期
    无法轻量校验其可达性（ls-remote 不匹配裸 SHA），拼写错误在构建期检出时
    fail-fast。分支/tag 走 ls-remote 模式匹配。
    """
    assert_valid_ref(ref, "引擎 ref")
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    wanted = (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", ref)
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo}.git", *wanted],
        capture_output=True, text=True, encoding="utf-8",
    )
    found = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            found[parts[1]] = parts[0]
    for name in wanted:
        if name in found:
            return found[name].lower()
    detail = proc.stderr.strip() or "无匹配 ref"
    fail(f"无法把引擎 ref '{ref}' 解析为 commit SHA（git ls-remote 失败：{detail}）；请检查 ref 与网络。")


def main():
    # ---- UI 输入（含 repository_dispatch client_payload 覆写）----
    ui_project = os.environ.get("INPUT_PROJECT", "")
    ui_source = os.environ.get("INPUT_ENGINE_SOURCE", "")
    ui_engine_ref = os.environ.get("INPUT_ENGINE_REF", "")
    ui_target = os.environ.get("INPUT_TARGET", "")
    ui_platform = os.environ.get("INPUT_PLATFORM", "")
    ui_profile = os.environ.get("INPUT_PROFILE", "")
    ui_build_profile = os.environ.get("INPUT_BUILD_PROFILE", "")
    ui_custom_modules = os.environ.get("INPUT_CUSTOM_MODULES", "")
    ui_scons_flags = os.environ.get("INPUT_SCONS_FLAGS", "")
    ui_force_upload = os.environ.get("INPUT_FORCE_UPLOAD", "").strip().lower() == "true"
    payload = os.environ.get("PAYLOAD", "")
    event_name = os.environ.get("EVENT_NAME", "")
    current_repo = os.environ.get("CURRENT_REPO", "")
    current_sha = os.environ.get("CURRENT_SHA", "")

    if event_name == "repository_dispatch" and payload:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {}
        ui_source = data.get("source", ui_source)
        ui_engine_ref = data.get("ref", ui_engine_ref)
        ui_target = data.get("target", ui_target)
        ui_platform = data.get("platform", ui_platform)
        if "force-upload" in data:
            ui_force_upload = str(data.get("force-upload")).strip().lower() == "true"

    # ---- 构建外部项目：克隆该项目以读取其契约 ----
    # 来源声明语法（docs/workflows.md「来源声明语法」）：remote:/local: 前缀必须显式。
    #
    # 契约是**可选**的：构建链编译的是**引擎**（editor / template_*），不是 Godot 项目，
    # 所以「没有任何项目配置也要能编引擎」是正当场景。判据只看契约文件在不在：
    # - 有契约 → 用它（project 填了就取那个仓库的，没填就取当前仓库的）
    # - 无契约 → 合成默认契约（引擎取官方仓库 + 自动解析最新 stable，其余走基线）
    # 与 project 填没填无关——project 只决定「去哪个仓库找契约」，不是「是否允许无契约」。
    if ui_project:
        parsed = godot_context.parse_source_entry(ui_project, "project 入参")
        if parsed["kind"] == "remote":
            project_repo, project_ref = parsed["repo"], parsed["ref"]
            if parsed["path"]:
                fail(f"project 入参的 remote: 不支持仓库内路径段：{ui_project}")
            temp_dir = os.environ.get("RUNNER_TEMP", "/tmp")
            project_dir = Path(temp_dir) / "project"
            git_ops.clone_shallow(project_repo, project_ref, str(project_dir),
                                  os.environ.get("INPUT_REPO_TOKEN", ""))
            os.environ["CI_CONTRACT"] = str(project_dir / ".github" / "ci-contract.json")
            os.environ["PROJECT_ROOT"] = str(project_dir)
        else:
            rel = parsed["path"]
            project_dir = Path(os.environ.get("GITHUB_WORKSPACE", ".")) / rel
            if not project_dir.is_dir():
                fail(f"local: 项目目录不存在：{project_dir}")
            # 当前检出的仓库与 SHA（外部项目场景下即 CI 仓库）
            project_repo, project_ref = current_repo, current_sha
            os.environ["CI_CONTRACT"] = str(project_dir / ".github" / "ci-contract.json")
            os.environ["PROJECT_ROOT"] = str(project_dir)
    else:
        project_repo, project_ref = current_repo, current_sha

    # ---- 契约归一化（单一来源）：缺失即合成默认契约 ----
    os.environ["INPUT_ENGINE_SOURCE"] = ui_source
    os.environ["INPUT_ENGINE_REF"] = ui_engine_ref
    # repo-safe 是产物名前缀的兜底来源之一（契约 artifacts.prefix 为空时用），
    # 必须在 resolve_outputs 之前就位：否则前缀算成空串，产物名渲染期才炸，
    # 且错误信息指向模板而看不出是前缀缺失。
    os.environ["REPO_SAFE"] = project_repo.rsplit("/", 1)[-1] if project_repo else ""
    # 路径口径必须与 load_contract 一致：用 godot_context.paths() 而非裸取环境变量
    # ——后者在未设置时得到空串（Path("") == "."），合成契约会写到 cwd 上。
    contract_path = Path(godot_context.paths()[0])
    allow_missing_contract = not contract_path.is_file()
    if allow_missing_contract:
        print(f"::notice::无 ci-contract.json（{contract_path}），"
              "改用默认契约（引擎取默认仓库与最新 stable tag、其余取平台基线与 schema 默认值）")
    else:
        print(f"::notice::使用契约：{contract_path}")
    contract, baseline, _ = godot_context.load_contract(allow_missing=allow_missing_contract)
    # 合成契约写回「契约本该在的路径」（CI_CONTRACT 当前指向处）：后续 step（生成运行时
    # 上下文 / 钩子 validate）都按 contract-path 重读契约，不落盘会再撞一次「缺少
    # ci-contract.json」。写原位而非另造临时路径，是为了让 contract-path 保持
    # 「本次运行实际使用的契约在哪」这个语义；allow_missing 为真意味着该文件本不存在，
    # 不会覆盖真实契约。
    if allow_missing_contract:
        # 相对路径的 CI_CONTRACT（默认值 .github/ci-contract.json）以项目根为基准，
        # 与 load_contract 的读取口径一致——否则会写到 cwd 的 . 上
        if not contract_path.is_absolute():
            base = os.environ.get("PROJECT_ROOT") or os.environ.get("GITHUB_WORKSPACE") or "."
            contract_path = Path(base) / contract_path
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"::notice::已把默认契约写入 {contract_path}（committed 契约不存在，此为合成值）")
        os.environ["CI_CONTRACT"] = str(contract_path)
    godot_context.assert_sha_consistency(contract)
    out = godot_context.resolve_outputs(contract, baseline, "build")

    eng_repo = out["engine-repo"]
    eng_ref = out["engine-ref"]
    engine_sha = resolve_engine_sha(eng_repo, eng_ref)
    os.environ["ENGINE_SHA"] = engine_sha

    # ---- target / platform：UI 覆写契约 ----
    # target 是单值档位（一次触发只产一个 target，见 docs/workflows.md）：契约
    # engine.target 数组取第一个，UI 多值则在 resolve 期显式拒绝，不静默截断。
    target = ""
    if ui_target:
        ui_targets = [t.strip() for t in ui_target.split(",") if t.strip()]
        if len(ui_targets) > 1:
            fail(f"target 一次只能指定一个档位，收到多值 {ui_targets}；"
                 f"请拆成多次运行或改契约 engine.target（逗号分隔会被截断，属静默丢配置）")
        target = ui_targets[0] if ui_targets else ""
    else:
        targets = [t for t in out["targets"].split(",") if t]
        if len(targets) > 1:
            fail(f"契约 engine.target 含多个档位 {targets}，构建链一次只产一个；"
                 f"请在契约里只留一个 target（UI 可用单值覆盖）")
        target = targets[0] if targets else ""
    if not target:
        fail("未解析到 target")
    if target not in ("editor", "template_debug", "template_release"):
        fail(f"unsupported target '{target}'")

    platform_row = ui_platform or " ".join(out["platforms"].split(","))
    if not platform_row.strip():
        fail("未解析到 platform")
    platforms = [p.strip() for p in platform_row.replace(",", " ").split() if p.strip()]
    allowed = set(godot_context.PLATFORMS)
    for p in platforms:
        if p not in allowed:
            fail(f"unsupported platform '{p}'")
    # 组合合法性：平台 × target 逐个校验（web+editor 这类静默零产物组合在此拒绝）
    godot_context.assert_platform_target_support(platforms, target)
    platforms_json = json.dumps(platforms)

    # 命名模板校验前移：未知占位符/空模板在 resolve 期一次暴露，
    # 不让各平台 job 渲染产物名时才发现
    for p in platforms:
        artifact_name.render(out["artifact-build-naming"], out["artifact-prefix"], p, target)

    # ---- profile / build-profile：来源声明（契约 build.paths 或 workflow_call 入参覆盖）----
    project_root = Path(os.environ.get("PROJECT_ROOT", "."))
    scons_flags = ui_scons_flags or out["scons-flags"]
    os.environ["INPUT_SCONS_FLAGS"] = scons_flags
    # 清单路径解析基准固定项目仓库根：外部项目克隆到 RUNNER_TEMP 时，
    # cwd 仍是 CI 仓库检出根，相对路径会找不到外部项目的清单。
    hooks_manifest = Path(out["hooks-manifest"])
    if not hooks_manifest.is_absolute():
        hooks_manifest = project_root / hooks_manifest

    def source_of(ui_value, declared, label):
        """入参优先；否则用契约声明的来源。local: 指向的文件不存在时跳过（remote: 留到检出期判定）。"""
        source = ui_value or declared
        if not source:
            return ""
        if source.startswith("local:"):
            rel = source[len("local:"):].strip("/")
            if not (project_root / rel).is_file():
                print(f"::notice::{label} 未找到 {rel}，使用引擎默认配置。")
                return ""
        return source

    profile = source_of(ui_profile, out["profile"], "profile.py")
    build_profile = source_of(ui_build_profile, out["build-profile"], ".gdbuild")

    # ---- 自定义模块：UI 覆写契约 build.modules ----
    # 语法与字面重复在 resolve 期校验（仓库存在性/注册名留构建期 config-inject 兜底）
    modules = out["custom-modules"]
    if ui_custom_modules.strip():
        modules = ui_custom_modules
        godot_context.assert_modules(
            [e.strip() for e in modules.split(",") if e.strip()], "custom-modules 入参")
    if target == "editor" and (profile or build_profile):
        # 编辑器构建用引擎默认配置；显式传入的 profile / build-profile 会被忽略，
        # 打 notice 避免用户以为配置已生效。
        print("::notice::target=editor 忽略 profile / build-profile（使用引擎默认配置）")
        profile = ""
        build_profile = ""

    # force-upload 输入生效：手动直触发默认不传产物，被调用（workflow_call 默认 true）保证下游可下载
    upload = ui_force_upload
    repo_safe = project_repo.rsplit("/", 1)[-1]

    # ---- CI 资产坐标：跨仓形态取契约 ci 段；单仓自用输出空串（prepare-build 从当前仓库检出）----
    ci = contract.get("ci") or {}
    ci_repo = ci.get("repository", "")
    ci_ref = ci.get("ref", "")

    # ---- 输出：契约归一化字段 + 解析结果 ----
    lines = [
        ("platforms", platforms_json),
        ("engine-repo", eng_repo),
        ("repo-safe", repo_safe),
        ("engine-ref", eng_ref),
        ("engine-sha", engine_sha),
        ("target", target),
        ("profile", profile),
        ("build-profile", build_profile),
        ("custom-modules", modules),
        ("upload-artifacts", str(upload).lower()),
        ("scons-flags", scons_flags),
        ("project-repo", project_repo),
        ("project-ref", project_ref),
        ("compat-standard-includes", out["compat-standard-includes"]),
        ("compat-standard-includes-linux", out["compat-standard-includes-linux"]),
        ("compat-standard-includes-windows", out["compat-standard-includes-windows"]),
        ("compat-standard-includes-macos", out["compat-standard-includes-macos"]),
        ("compat-standard-includes-android", out["compat-standard-includes-android"]),
        ("compat-standard-includes-ios", out["compat-standard-includes-ios"]),
        ("compat-standard-includes-visionos", out["compat-standard-includes-visionos"]),
        ("compat-standard-includes-web", out["compat-standard-includes-web"]),
        ("retention-build", out["retention-build"]),
        ("retention-contract", out["retention-contract"]),
        ("toolchain-json", out["toolchain-json"]),
        ("runner-json", out["runner-json"]),
        ("platforms-json", out["platforms-json"]),
        ("artifact-prefix", out["artifact-prefix"]),
        ("artifact-build-naming", out["artifact-build-naming"]),
        ("ci-repository", ci_repo),
        ("ci-ref", ci_ref),
        ("hooks-manifest", str(hooks_manifest)),
        # 外部项目构建强制 off：清单来自外部仓库，不在 CI runner 执行（单一判据在 godot_context）
        ("hooks-policy", godot_context.resolve_hooks_policy(contract, project_repo)),
        ("contract-path", os.environ.get("CI_CONTRACT", godot_context.paths()[0])),
        ("project-root", str(project_root)),
    ]
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    text = "\n".join(f"{k}={str(v).replace(chr(13), '').replace(chr(10), '')}" for k, v in lines) + "\n"
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(text)
    print(text, end="")
    print(f"repo={eng_repo} ref={eng_ref} target={target} platforms={platforms_json}")


if __name__ == "__main__":
    main()