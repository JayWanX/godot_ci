#!/usr/bin/env python3
"""Godot 导出参数解析脚本（由 resolve-export action 调用）。

配置源是单一契约 ci-contract.json（经 godot_context.py 归一化）。
保留成熟的预设解析与平台映射逻辑。
输出（GITHUB_OUTPUT）供 export-platform.yml / build.yml 使用。
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
import preset_name  # noqa: E402
import preset_config  # noqa: E402
import git_ops  # noqa: E402


def warn(message: str) -> None:
    print(f"::warning::{escape_error(message)}", file=sys.stderr)


# 与 actions/export/scripts/export_steps.py::EDITOR_VERSION_RE 同源（与导出执行器同一判据）：
# 编辑器版本会拼进 prefetch / 官方模板下载 URL，字符白名单必须全串匹配 + 拒绝 '..' 段，
# 且必须在 resolve 期完成——晚一步就已在 prefetch 期用未校验值拼出 404 URL。
EDITOR_VERSION_RE = re.compile(r"[A-Za-z0-9._-]+")


def assert_editor_version(version: str, label: str) -> str:
    """resolve 期校验编辑器版本形态；与 export_steps 同一判据。

    'v' 前缀显式拒绝（不静默剥掉）：`v4.3-stable` 会拼成 `Godot_vv4.3...` 必 404，
    静默剥掉又会掩盖契约/输入写错的事实，不如在 resolve 期响亮失败。
    """
    if not version:
        fail(f"{label} 为空")
    if not EDITOR_VERSION_RE.fullmatch(version) or ".." in version:
        fail(f"{label} '{version}' 含非法字符或 '..' 段（仅允许 [A-Za-z0-9._-]）")
    if version.startswith("v"):
        fail(f"{label} '{version}' 带了 'v' 前缀（会拼成 Godot_vv... 的 404 URL）；"
             f"请写 '<N>.<N>[.<N>]-stable' 形态")
    return version


def read_preset_names(preset_cfg_path):
    """全部预设名；唯一实现在 preset_config（按段解析，与字段顺序无关）。"""
    path = Path(preset_cfg_path)
    if not path.is_file():
        return []
    return preset_config.read_preset_names(path.read_text(encoding="utf-8"))


def derive_platforms(preset_cfg_path, selected_presets):
    """平台推导；唯一实现在 preset_config。无法识别的平台名在此 warn。"""
    text = Path(preset_cfg_path).read_text(encoding="utf-8")
    platforms, preset_platform = preset_config.derive_platforms(text, selected_presets)
    sections, order = preset_config.parse_sections(text)
    for _, fields in preset_config.preset_entries(sections, order):
        name = fields.get("name", "")
        long_name = fields.get("platform", "")
        if name in selected_presets and long_name and name not in preset_platform:
            warn(f"预设 '{name}' 平台 '{long_name}' 无法识别")
    return platforms, preset_platform


def main():
    ui_project = os.environ.get("INPUT_PROJECT", "")
    ui_presets = os.environ.get("INPUT_PRESETS", "")
    ui_export_type = os.environ.get("INPUT_EXPORT_TYPE", "")
    ui_editor_version = os.environ.get("INPUT_EDITOR_VERSION", "")
    # 导出模板编译参数（仅 custom_build 生效）：UI > 契约，与 build.yml 的同名入参同义
    ui_scons_flags = os.environ.get("INPUT_SCONS_FLAGS", "")
    ui_profile = os.environ.get("INPUT_PROFILE", "")
    ui_build_profile = os.environ.get("INPUT_BUILD_PROFILE", "")
    current_repo = os.environ.get("CURRENT_REPO", "")
    current_sha = os.environ.get("CURRENT_SHA", "")

    # ---- 外部项目或同仓不同 ref：克隆项目，预设文件一律取目标 ref ----
    # 契约是可选的（与 build 链一致）：无契约的项目正是 CI 仓库侧要服务的对象。
    allow_missing_contract = False
    if ui_project:
        allow_missing_contract = True
        parsed = godot_context.parse_source_entry(ui_project, "project 入参")
        if parsed["kind"] != "remote" or parsed["path"]:
            fail(f"project 入参须为 remote:<owner/repo>[@<ref>]，不得带仓库内路径：{ui_project}")
        project_repo, project_ref = parsed["repo"], parsed["ref"]
    else:
        project_repo, project_ref = current_repo, current_sha
    # 仅当项目仓库/ ref 与触发仓库不同时才视为外部项目：build-templates 据此
    # 决定是否向 build.yml 传 project（自有项目传了会无谓克隆自己，私有仓库还会 401）。
    external_project = project_repo != current_repo or project_ref != current_sha
    project_dir = Path(".")
    if external_project:
        temp_dir = os.environ.get("RUNNER_TEMP", "/tmp")
        project_dir = Path(temp_dir) / "project"
        git_ops.clone_shallow(project_repo, project_ref, str(project_dir),
                              os.environ.get("INPUT_REPO_TOKEN", ""))
        os.environ["CI_CONTRACT"] = str(project_dir / ".github" / "ci-contract.json")
        os.environ["PROJECT_ROOT"] = str(project_dir)

    # ---- 契约归一化（单一来源）----
    os.environ["REPO_SAFE"] = project_repo.rsplit("/", 1)[-1]
    # 路径口径必须与 load_contract 一致（用 paths() 而非裸取环境变量）
    contract_path = Path(godot_context.paths()[0])
    allow_missing_contract = allow_missing_contract and not contract_path.is_file()
    if allow_missing_contract:
        print(f"::notice::无 ci-contract.json（{contract_path}），"
              "改用默认契约（引擎取平台基线的默认仓库与最新 stable tag、其余取 schema 默认值）")
    else:
        print(f"::notice::使用契约：{contract_path}")
    contract, baseline, _ = godot_context.load_contract(allow_missing=allow_missing_contract)
    # 合成契约写回「契约本该在的路径」（同 build 链）：后续 step 按 contract-path 重读契约
    if allow_missing_contract:
        # 相对路径以项目根为基准，与 load_contract 的读取口径一致
        if not contract_path.is_absolute():
            contract_path = Path(os.environ.get("PROJECT_ROOT", ".")) / contract_path
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"::notice::已把默认契约写入 {contract_path}（committed 契约不存在，此为合成值）")
        os.environ["CI_CONTRACT"] = str(contract_path)
    godot_context.assert_sha_consistency(contract)
    out = godot_context.resolve_outputs(contract, baseline, "export")

    export = contract["export"]
    engine = contract["engine"]
    preset_rel = out["export-presets-path"]
    project_root = Path(os.environ.get("PROJECT_ROOT", "."))
    preset_cfg = project_root / preset_rel
    # 契约声明的路径必须存在：resolve 期文件已完整在手（自仓已检出、外部项目已克隆），
    # 宽松回退会让路径错误拖到各导出 job 才失败（custom_build 场景已白编译模板）。
    if not preset_cfg.is_file():
        fail(f"契约 export.paths.export_presets 指向的预设文件不存在：{preset_rel}"
             f"（项目根 {project_root}）")
    # 清单路径解析基准固定项目仓库根（外部项目克隆目录）
    hooks_manifest = Path(out["hooks-manifest"])
    if not hooks_manifest.is_absolute():
        hooks_manifest = project_root / hooks_manifest

    # ---- 导出类型：UI > 契约 ----
    # choice 恒有值（默认 custom_build，与 schema default 一致），不存在「未指定」态；
    # 契约写 official 时想沿用就在 UI 里选 official，否则以 UI 为准。
    export_type = ui_export_type or out["export-type"]
    if export_type not in ("official", "custom_build"):
        fail(f"unsupported export-type '{export_type}'（可选 official / custom_build）")

    # ---- 编辑器版本：UI > 契约 > 从引擎 ref 推导 ----
    # 无契约时没有 export.editor_version，而它与引擎版本必须 major.minor 一致
    # （下方校验），故直接取引擎 ref：两者本就是同一个版本号形态。
    editor_version = ui_editor_version or out["editor-version"] or out["engine-ref"]
    if not editor_version:
        fail("缺少 export.editor_version（契约）或 editor-version 输入")
    assert_editor_version(editor_version, "editor-version")  # resolve 期拦下 v 前缀 / 非法字符
    if export_type == "official":
        # 官方模式恒用官方引擎仓库（默认仓库名的唯一来源在 godot_context）
        engine_repo, engine_ref = godot_context.DEFAULT_ENGINE_REPO, editor_version
    else:
        engine_repo, engine_ref = out["engine-repo"], out["engine-ref"]

    # custom_build：官方编辑器版本须与自编译模板的引擎版本兼容，ref 可解析为版本号时
    # 比对 major.minor fail-fast；ref 是分支/SHA 时无法静态判定，跳过。
    if export_type == "custom_build":
        engine_mm = re.match(r"(\d+)\.(\d+)", engine_ref)
        editor_mm = re.match(r"(\d+)\.(\d+)", editor_version)
        if engine_mm and editor_mm and engine_mm.groups() != editor_mm.groups():
            fail(f"引擎 ref '{engine_ref}' 与 editor-version '{editor_version}' 的 "
                 f"major.minor 不一致，自编译模板与官方编辑器不兼容")

    repo_safe = project_repo.rsplit("/", 1)[-1]

    # ---- 预设列表：UI > 契约 presets > 项目 export_presets.cfg ----
    whitelist = [p.strip() for p in out["presets"].split(",") if p.strip()]
    if ui_presets:
        presets = ui_presets
    elif external_project:
        # 外部项目：契约 presets 退化为白名单，留空导出该项目全部
        project_presets = read_preset_names(str(preset_cfg))
        if whitelist:
            kept = [p for p in project_presets if p in whitelist]
            for w in whitelist:
                if w not in project_presets:
                    warn(f"契约预设 '{w}' 不在外部项目的 {preset_rel} 中，已忽略")
            presets = ",".join(kept)
        else:
            presets = ",".join(project_presets)
        if not presets:
            fail(f"外部项目 {project_repo} 的 {preset_rel} 中没有可用预设")
    else:
        presets = ",".join(whitelist)
        if not presets:
            presets = ",".join(read_preset_names(str(preset_cfg)))
        if not presets:
            fail(f"未解析到任何导出预设（契约 export.presets 为空，项目也无 {preset_rel}）")
    selected_preset_list = [p.strip() for p in presets.split(",") if p.strip()]

    # ---- 平台推导 ----
    platforms, preset_platform = derive_platforms(str(preset_cfg), selected_preset_list)
    if not platforms:
        fail(f"所选预设 {selected_preset_list} 在 {preset_cfg} 中推导不到平台")

    # 预设名存在性校验（resolve 期 fail-fast）：名单里不在 cfg 实际预设中的项，
    # 走到这里 = 拼错/预设改名（静默丢产物，Release 少发），直接失败。外部项目
    # 契约白名单的缺失已在上方过滤并 warn（文档承诺的宽松语义），不会到这里。
    cfg_presets = read_preset_names(str(preset_cfg))
    missing_presets = [p for p in selected_preset_list if p not in cfg_presets]
    if missing_presets:
        fail(f"预设 {missing_presets} 不存在于 {preset_rel}；实际可用预设：{cfg_presets}")

    # safe 名冲突 fail-fast：只查同平台——跨平台 safe 名分属不同 Artifact 家族
    # （export-<平台>-<safe>）且产物目录互不相交，不构成冲突。
    seen_safes = {}
    for preset, plat in preset_platform.items():
        safe = preset_name.preset_safe(preset)
        if safe in seen_safes and seen_safes[safe][1] == plat:
            fail(f"预设 safe 名冲突（平台 {plat}）：'{preset}' 与 '{seen_safes[safe][0]}' "
                 f"都收敛为 '{safe}'，请改用可区分的预设名")
        seen_safes[safe] = (preset, plat)

    # 导出成品即交付物，恒上传；无上传开关
    upload = True

    # ---- 导出路径（契约 export.paths）----
    project_godot_rel = out["export-project-godot"]
    if not (project_root / project_godot_rel).is_file():
        fail(f"契约 export.paths.project_godot 指向的文件不存在：{project_godot_rel}"
             f"（项目根 {project_root}）")
    presets_rel = out["export-presets-path"]
    dist_rel = out["export-dist-dir"]

    # ---- 编译产物命名（契约 artifacts.build_naming）----
    # 变体集合来自 platforms-json 的 variants 字段：多变体平台用通配，单产物平台给精确名。
    build_naming = out["artifact-build-naming"]
    prefix = out["artifact-prefix"]
    platforms_cfg = json.loads(out["platforms-json"])
    patterns = {}
    for plat in platforms:
        variants = [v.get("variant", "") for v in
                    (platforms_cfg.get(plat) or {}).get("variants") or []
                    if isinstance(v, dict)]
        patterns[plat] = artifact_name.pattern(build_naming, prefix, plat,
                                               "template_release", variants=variants)

    # ---- 导出产物命名（契约 artifacts.export_naming）----
    # 按预设渲染，作为上传 matrix 的数据源（每预设一个 zip，名字 resolve 期一次定死）。
    # 冲突判据是**渲染结果全局去重**，不再按平台分段：模板默认不拼 {platform}
    # （预设名普遍自带平台），此时跨平台同名同样会让 Release 附件互相覆盖。
    export_naming = out["artifact-export-naming"]
    export_of_preset = {}
    collisions = {}
    for pname, plat in preset_platform.items():
        package = artifact_name.render_export(export_naming, prefix, plat, pname)
        if package in collisions:
            collisions[package].append(pname)
        else:
            collisions[package] = [pname]
        export_of_preset[pname] = package
    duplicates = {k: v for k, v in collisions.items() if len(v) > 1}
    if duplicates:
        detail = "；".join(f"'{k}' ← {v}" for k, v in sorted(duplicates.items()))
        fail(f"导出产物名冲突：{detail}。artifacts.export_naming 当前为 "
             f"'{export_naming}'，请改用可区分的模板（如 {{prefix}}_{{platform}}_{{preset}}）")

    # ---- 本轮导出完整性（决定 Release 陈旧附件能否清理）----
    # 选中预设覆盖 cfg 全部预设 → 本轮产物即该项目当前应交付的完整集合，
    # Release 上其余同家族附件都是陈旧残留。只导出一部分时为 False：其余附件
    # 是上一轮的有效产物，清理会静默丢交付物。
    export_complete = set(selected_preset_list) == set(cfg_presets)

    # CI 资产坐标：跨仓形态取契约 ci 段；单仓自用输出空串（消费方从当前仓库检出）
    ci = contract.get("ci") or {}
    ci_repo = ci.get("repository", "")
    ci_ref = ci.get("ref", "")

    lines = [
        ("presets", presets),
        ("export-type", export_type),
        ("upload-artifacts", str(upload).lower()),
        ("template-platforms", ",".join(platforms)),
        ("engine-source", f"{engine_repo}@{engine_ref}"),
        ("engine-ref", engine_ref),
        ("editor-version", editor_version),
        ("repo-safe", repo_safe),
        ("project-repo", project_repo),
        ("project-ref", project_ref),
        ("external-project", str(external_project).lower()),
        ("contract-path", os.environ.get("CI_CONTRACT", godot_context.paths()[0])),
        ("project-root", os.environ.get("PROJECT_ROOT", ".")),
        ("retention-export", out["retention-export"]),
        ("retention-prefetch", out["retention-prefetch"]),
        ("retention-contract", out["retention-contract"]),
        ("engine-sha", out["engine-sha"]),
        # UI 可覆写（与 build.yml 同名入参同义）；留空取契约归一化值
        ("scons-flags", ui_scons_flags or out["scons-flags"]),
        ("runner-json", out["runner-json"]),
        # 平台配置（含契约覆写）：导出矩阵据此取 export_runner / editor_archive
        ("platforms-json", out["platforms-json"]),
        # 预取 job 的 matrix.include 数据源（[{key, platform}]，platform = editor_archive）
        ("editor-archives-matrix", out["editor-archives-matrix"]),
        # 编译导出模板时透传给 build.yml：来源声明原样下传，local: 的存在性由 build 链判定
        ("profile", ui_profile or out["profile"]),
        ("build-profile", ui_build_profile or out["build-profile"]),
        ("artifact-prefix", prefix),
        ("artifact-build-naming", build_naming),
        ("artifact-export-naming", export_naming),
        ("project-godot", project_godot_rel),
        ("export-presets-path", presets_rel),
        ("dist-dir", dist_rel),
        ("ci-repository", ci_repo),
        ("ci-ref", ci_ref),
        ("hooks-manifest", str(hooks_manifest)),
        # 外部项目构建强制 off：清单来自外部仓库，不在 CI runner 执行（单一判据在 godot_context）
        ("hooks-policy", godot_context.resolve_hooks_policy(contract, project_repo)),
    ]
    # 每平台一份 presets / packages / templates-pattern 输出。平台顺序取 godot_context.PLATFORMS
    # （平台集合的唯一权威源），不在此另抄一份清单——两处顺序不同会让 GITHUB_OUTPUT 的
    # 键序随文件漂移，且新增平台时必有一处漏改。
    for p in godot_context.PLATFORMS:
        lines.append((f"presets-{p}", ",".join(n for n, plat in preset_platform.items() if plat == p)))
        # 上传 matrix 的数据源：每预设一条 {preset, preset-safe, package}
        # （preset-safe = 产物子目录名，package = zip 名；两者 resolve 期一次定死）
        lines.append((f"packages-{p}", json.dumps(
            [{"preset": n, "preset-safe": preset_name.preset_safe(n),
              "package": export_of_preset[n]}
             for n, plat in preset_platform.items() if plat == p],
            ensure_ascii=False, separators=(",", ":"))))
        lines.append((f"templates-pattern-{p}", patterns.get(p, "")))
    # Release 附件完整性判据：本次选中的预设覆盖 cfg 里全部预设，才允许清理陈旧附件。
    # 只导出部分预设时，Release 上其余附件是上一轮的有效产物，删了就静默丢失。
    lines.append(("export-complete", "true" if export_complete else "false"))

    output_path = os.environ.get("GITHUB_OUTPUT", "")
    text = "\n".join(f"{k}={str(v).replace(chr(13), '').replace(chr(10), '')}" for k, v in lines) + "\n"
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(text)
    print(text, end="")
    print(f"presets={presets} type={export_type} platforms={' '.join(platforms)}")


if __name__ == "__main__":
    main()