#!/usr/bin/env python3
"""契约归一化层自测：godot_context.py 的关键判据。

覆盖：
- 契约 schema 校验（ci 段必填，缺失即 fail-fast）
- compat.standard_includes 的 auto 分档：命中 skip_rules 关闭、规则字段缺失 fail-fast
- export.paths 三字段的归一化输出；retention 直取契约键（无整数字面量兜底）
- runner 映射：基线 + 契约覆写合并、resolve 键覆写 fail-fast
- 平台 × target 支持表：合法放行、不支持组合 fail-fast
- context 分语境定值：批 / release 期 platform/target=null、cache.key 已废除
- build.modules 来源声明语法：前缀强制、ref 白名单（分支/tag/40 位 SHA）、path 段校验

用法：python .github/tests/test_godot_context.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from schema import load_schema, apply_defaults, validate  # noqa: E402
from jsonc import read_json  # noqa: E402
import godot_context  # noqa: E402
from testlib import capture, expect_fail  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def _contract(**over):
    base = {
        "ci": {"repository": "o/r", "ref": "a" * 40},
        "engine": {"source": "godotengine/godot@4.7.2-stable",
                   "target": ["template_release"],
                   "platform": ["linux", "windows"]},
        "build": {"paths": {"profile": "local:build/profile.py",
                            "build_profile": "local:build/project.gdbuild"},
                  "modules": [],
                  "scons_flags": "", "compat": {"standard_includes": "auto"},
                  "toolchain": {}},
        "export": {"type": "custom_build", "editor_version": "4.7.2-stable",
                   "presets": ["Windows Desktop"],
                   "paths": {"project_godot": "project.godot",
                             "export_presets": "export_presets.cfg", "dist": "bin"}},
        "artifacts": {"prefix": "", "build_naming": "{prefix}_{platform}_{target}",
                      "export_naming": "{prefix}_{preset}",
                      "retention": {"build": 7, "prefetch": 1, "export": 30, "contract": 1}},
        "hooks": {"manifest": ".github/ci-hooks.json", "policy": "declared"},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value
    return base


def _real_baseline():
    """读仓库里真实的 platform-baseline.json（不经契约覆写合并）。

    定位沿用 godot_context.paths() 的口径：环境变量 PLATFORM_BASELINE 可覆盖，
    默认取 .github/contract/platform-baseline.json。
    """
    _, baseline_path, _ = godot_context.paths()
    return read_json(str(baseline_path), "platform-baseline.json")


def _baseline(**over):
    base = {
        "platforms": {
            plat: {"build_runner": "ubuntu-22.04",
                   "export_runner": "ubuntu-22.04",
                   "editor_archive": "linux.x86_64"}
            for plat in godot_context.PLATFORMS
        },
        "roles": {"resolve": "ubuntu-latest", "upload": "ubuntu-22.04"},
        "toolchain": {"emsdk": "4.0.11", "java": "17"},
        # retention 不在基线：唯一源是契约 artifacts.retention（schema default 补齐）。
        # 夹具若保留这里的 retention，会掩盖「基线被误当权威源」的接线错误。
        "compat": {"standard_includes": {"default": "on", "skip_rules": []}},
    }
    base.update(over)
    return base


@check("单仓自用：契约缺 ci 段合法，跳过 ref 断言")
def t_standalone_no_ci():
    contract = _contract()
    del contract["ci"]
    os.environ.pop("CALLER_USES_REF", None)
    os.environ["GITHUB_REPOSITORY"] = "me/proj"
    os.environ["WORKFLOW_REPOSITORY"] = "me/proj"   # 同仓形态
    try:
        assert godot_context.assert_sha_consistency(contract) == ""
        assert not godot_context.is_cross_repo()
    finally:
        for k in ("GITHUB_REPOSITORY", "WORKFLOW_REPOSITORY"):
            os.environ.pop(k, None)


@check("跨仓形态：契约缺 ci 段 → fail-fast（防漂移断言不可省）")
def t_cross_repo_requires_ci():
    contract = _contract()
    del contract["ci"]
    os.environ.pop("CALLER_USES_REF", None)
    os.environ["GITHUB_REPOSITORY"] = "me/proj"
    os.environ["WORKFLOW_REPOSITORY"] = "me/ci"     # 跨仓形态
    try:
        assert godot_context.is_cross_repo()
        expect_fail(lambda: godot_context.assert_sha_consistency(contract),
                    "跨仓形态契约缺 ci 段", needle="ci")
    finally:
        for k in ("GITHUB_REPOSITORY", "WORKFLOW_REPOSITORY"):
            os.environ.pop(k, None)


@check("ci 段有值时：薄触发器 ref 与 ci.ref 不一致 → fail-fast")
def t_ci_ref_mismatch():
    os.environ["CALLER_USES_REF"] = "b" * 40
    try:
        expect_fail(lambda: godot_context.assert_sha_consistency(_contract()),
                    "薄触发器 ref 与 ci.ref 不一致", needle="不一致")
    finally:
        os.environ.pop("CALLER_USES_REF", None)


@check("compat auto：底值取基线 default（on），命中 skip_rules 恒 off")
def t_compat_auto():
    contract = _contract()
    baseline = _baseline(compat={"standard_includes": {"default": "on", "skip_rules": [
        {"engine_ref": "4.7.*", "toolchain": {"java": "8"}, "platform": "linux"},
    ]}})
    # java=8 未命中基线 toolchain（17）→ on
    toolchain = {**baseline["toolchain"], **contract["build"]["toolchain"]}
    assert godot_context.resolve_standard_includes(contract, baseline, "4.7.2-stable", toolchain, "linux") == "on"
    # 契约覆写 toolchain.java=8 → 命中 → off
    contract2 = _contract(build={"toolchain": {"java": "8"}})
    toolchain2 = {**baseline["toolchain"], **contract2["build"]["toolchain"]}
    assert godot_context.resolve_standard_includes(contract2, baseline, "4.7.2-stable", toolchain2, "linux") == "off"
    # platform 不匹配的规则不生效
    assert godot_context.resolve_standard_includes(contract2, baseline, "4.7.2-stable", toolchain2, "windows") == "on"
    # engine_ref 不匹配的规则不生效
    assert godot_context.resolve_standard_includes(contract2, baseline, "4.8.0-stable", toolchain2, "linux") == "on"


@check("compat auto：基线 default=off 全局翻转底值（规则命中仍 off，单向关闭）")
def t_compat_default_off():
    contract = _contract()
    baseline = _baseline(compat={"standard_includes": {"default": "off", "skip_rules": [
        {"engine_ref": "4.7.*", "toolchain": {"java": "8"}, "platform": "linux"},
    ]}})
    toolchain = {**baseline["toolchain"], **contract["build"]["toolchain"]}
    # 未命中 → 底值 off（CI 全局关闭标准头补齐）
    assert godot_context.resolve_standard_includes(contract, baseline, "4.8.0-stable", toolchain, "linux") == "off"
    # 命中 → 恒 off（规则单向关闭，不会反向开启）
    contract2 = _contract(build={"toolchain": {"java": "8"}})
    toolchain2 = {**baseline["toolchain"], **contract2["build"]["toolchain"]}
    assert godot_context.resolve_standard_includes(contract2, baseline, "4.7.2-stable", toolchain2, "linux") == "off"


@check("compat auto：基线 default 非 on/off → fail-fast")
def t_compat_default_invalid():
    contract = _contract()
    baseline = _baseline(compat={"standard_includes": {"default": "auto", "skip_rules": []}})
    expect_fail(lambda: godot_context.resolve_standard_includes(
        contract, baseline, "4.7.2-stable", baseline["toolchain"], "linux"),
        "基线 default='auto'", needle="须为 on/off")


@check("compat auto：基线缺 default → fail-fast（不用代码兜底）")
def t_compat_default_missing():
    contract = _contract()
    baseline = _baseline(compat={"standard_includes": {"skip_rules": []}})
    expect_fail(lambda: godot_context.resolve_standard_includes(
        contract, baseline, "4.7.2-stable", baseline["toolchain"], "linux"),
        "基线缺 compat.default", needle="须为 on/off")


@check("compat auto：规则引用的工具链字段缺失 → fail-fast")
def t_compat_failfast():
    contract = _contract()
    baseline = _baseline(compat={"standard_includes": {"default": "on", "skip_rules": [
        {"engine_ref": "*", "toolchain": {"msvc": "14.3"}},
    ]}})
    toolchain = {"emsdk": "4.0.11", "java": "17"}  # 无 msvc
    expect_fail(lambda: godot_context.resolve_standard_includes(
        contract, baseline, "4.7.2-stable", toolchain, "windows"),
        "工具链字段缺失", needle="msvc")


@check("compat 显式 on/off 直接采纳")
def t_compat_explicit():
    for value, expect in (("on", "on"), ("off", "off")):
        contract = _contract(build={"compat": {"standard_includes": value}})
        baseline = _baseline()
        got = godot_context.resolve_standard_includes(
            contract, baseline, "4.7.2-stable", baseline["toolchain"], "linux")
        assert got == expect, f"{value} → {got}"


@check("外部项目构建强制 hooks-policy=off；自有项目保持 declared")
def t_hooks_policy_external():
    contract = _contract()
    os.environ["GITHUB_REPOSITORY"] = "me/ci"
    try:
        assert godot_context.resolve_hooks_policy(contract, "me/ci") == "declared"
        # 外部项目强制 off 时打印 ::notice:: —— 收进内存免得污染 CI 注解通道
        captured = capture(lambda: godot_context.resolve_hooks_policy(contract, "other/proj"))
        assert captured.code == 0 and captured.value == "off", captured.raw_stdout
        assert "外部项目构建" in captured.text, captured.text
        # 契约已写 off 时无需 notice，仍为 off
        off_contract = _contract(hooks={"policy": "off"})
        assert godot_context.resolve_hooks_policy(off_contract, "other/proj") == "off"
        # 本地/文档场景缺触发仓库时按自有项目处理，不误关扩展
        os.environ.pop("GITHUB_REPOSITORY", None)
        assert godot_context.resolve_hooks_policy(contract, "other/proj") == "declared"
    finally:
        os.environ.pop("GITHUB_REPOSITORY", None)


@check("归一化输出含 export.paths 与两套命名模板")
def t_resolve_outputs():
    os.environ.pop("CALLER_USES_REF", None)
    os.environ["REPO_SAFE"] = "my-game"
    try:
        out = godot_context.resolve_outputs(_contract(), _baseline(), "build")
    finally:
        os.environ.pop("REPO_SAFE", None)
    assert out["export-project-godot"] == "project.godot"
    assert out["export-presets-path"] == "export_presets.cfg"
    assert out["export-dist-dir"] == "bin"
    assert out["artifact-build-naming"] == "{prefix}_{platform}_{target}"
    assert out["artifact-export-naming"] == "{prefix}_{preset}"
    assert out["artifact-prefix"] == "my-game"
    # retention 直取契约键（schema default 补齐），无整数字面量兜底
    assert out["retention-build"] == "7"
    assert out["retention-contract"] == "1"
    # compat 是按平台的 JSON 映射
    compat = json.loads(out["compat-standard-includes"])
    assert set(compat) == set(godot_context.PLATFORMS), compat
    assert all(v in ("on", "off") for v in compat.values()), compat
    # 按平台各出一个标量（各平台作业直接取自己那个，避免 JSON 映射与 'off' 比较永不成立）
    for plat in godot_context.PLATFORMS:
        assert out[f"compat-standard-includes-{plat}"] == compat[plat], plat


@check("runner 映射：基线 + 契约覆写合并；resolve 键覆写 fail-fast")
def t_runner_merge():
    os.environ.pop("CALLER_USES_REF", None)
    baseline = _baseline()
    contract = _contract()
    platforms = godot_context.resolve_platforms(contract, baseline)
    merged = godot_context.resolve_runner(contract, baseline, platforms)
    # 平台键来自 platforms.*.build_runner，角色键来自 roles 段
    assert merged["linux"] == "ubuntu-22.04", merged
    assert merged["resolve"] == "ubuntu-latest", merged
    assert merged["upload"] == "ubuntu-22.04", merged

    # 契约 platforms 覆写单个字段：未声明字段沿用基线
    contract2 = _contract(platforms={"linux": {"build_runner": "ubuntu-24.04"}})
    platforms2 = godot_context.resolve_platforms(contract2, baseline)
    assert platforms2["linux"]["build_runner"] == "ubuntu-24.04", platforms2["linux"]
    assert platforms2["linux"]["export_runner"] == "ubuntu-22.04", platforms2["linux"]
    merged2 = godot_context.resolve_runner(contract2, baseline, platforms2)
    assert merged2["linux"] == "ubuntu-24.04", merged2

    # 未知平台 / 未知字段 fail-fast
    for bad, why, needle in (
        (_contract(platforms={"plan9": {"build_runner": "x"}}), "未知平台", "未知平台"),
        (_contract(platforms={"linux": {"nope": "x"}}), "未知字段", "未知字段"),
    ):
        expect_fail(lambda b=bad: godot_context.resolve_platforms(b, baseline),
                    why, needle=needle)

    # 契约 roles 只允许 publish / upload；resolve 在 schema 层面即不可写
    contract_roles = _contract(roles={"publish": "self-hosted", "upload": "ubuntu-24.04"})
    merged_roles = godot_context.resolve_runner(contract_roles, baseline, platforms)
    assert merged_roles["publish"] == "self-hosted", merged_roles
    assert merged_roles["upload"] == "ubuntu-24.04", merged_roles
    assert merged_roles["resolve"] == "ubuntu-latest", merged_roles  # 恒取基线

    schema = load_schema(str(godot_context.CONTRACT_DIR / "contract.schema.json"))
    bad_roles = _contract(roles={"resolve": "self-hosted-x"})
    apply_defaults(bad_roles, schema)
    assert validate(bad_roles, schema), "契约 roles.resolve 应在 schema 校验期被拒绝"

    # 归一化输出含 platforms-json 与去重后的档案矩阵
    os.environ["REPO_SAFE"] = "my-game"
    try:
        out = godot_context.resolve_outputs(_contract(), baseline, "build")
    finally:
        os.environ.pop("REPO_SAFE", None)
    plats = json.loads(out["platforms-json"])
    assert set(plats) == set(godot_context.PLATFORMS), plats
    archives = json.loads(out["editor-archives-matrix"])
    # 夹具里所有平台共用 linux.x86_64，去重后应只剩 1 项
    assert len(archives) == 1 and archives[0]["platform"] == "linux.x86_64", archives


@check("runner 映射：平台缺 build_runner → fail-fast（空串会让 job 在分派期报怪错）")
def t_runner_missing_build_runner():
    contract = _contract()
    baseline = _baseline()
    baseline["platforms"]["linux"].pop("build_runner")
    expect_fail(lambda: godot_context.resolve_runner(
        contract, baseline, godot_context.resolve_platforms(contract, baseline)),
        "linux 缺 build_runner", needle="build_runner")


@check("平台 × target 支持表：合法组合放行，不支持组合 fail-fast 并列出可用 target")
def t_platform_target_support():
    godot_context.assert_platform_target_support(["linux", "windows"], "editor")
    godot_context.assert_platform_target_support(["linux", "android"], "template_debug")
    # template_debug 在三档全放开的平台与 iOS / visionOS 上都成立：CI 是否能在 runner
    # 上执行该产物属验证范畴，不构成「不支持」。
    for plat in ("windows", "macos", "ios", "visionos", "web"):
        godot_context.assert_platform_target_support([plat], "template_debug")
    godot_context.assert_platform_target_support(["web"], "template_release")
    # web + editor 是引擎事实（Emscripten 目标构建不出桌面编辑器），必须 resolve 期失败
    captured = expect_fail(
        lambda: godot_context.assert_platform_target_support(["web"], "editor"),
        "web + editor 组合", needle="template_debug")
    assert "template_release" in captured.text, captured.text


def _ctx(contract, baseline, **kw):
    return godot_context.build_context(contract, baseline, kw.pop("chain", "build"),
                                       kw.pop("target", ""), kw.pop("platform", ""),
                                       kw.pop("arch", ""))


@check("context 分语境定值：批 / release 期 platform/target=null；schema 校验 null；cache.key 已废除")
def t_context_null_forms():
    os.environ.pop("CALLER_USES_REF", None)
    os.environ["GITHUB_REPOSITORY"] = "me/proj"
    try:
        batch = _ctx(_contract(), _baseline())
        assert batch["platform"] is None and batch["target"] is None, batch
        assert "key" not in batch["cache"], batch["cache"]
        single = _ctx(_contract(), _baseline(), chain="build",
                      target="template_release", platform="linux")
        assert single["platform"] == "linux" and single["target"] == "template_release"
        schema = load_schema(str(godot_context.CONTRACT_DIR / "context.schema.json"))
        assert not validate(batch, schema), "null platform/target 未通过 context schema"
        assert not validate(single, schema)
        # 旧四类形态已废除：cache.key 回填即未知字段
        stale = json.loads(json.dumps(single))
        stale["cache"]["key"] = "k"
        assert validate(stale, schema), "cache.key 应作为未知字段被拒"
    finally:
        os.environ.pop("GITHUB_REPOSITORY", None)


@check("契约 schema：ci 段可选（缺失合法），ci 段格式非法被拒")
def t_schema():
    schema = load_schema(str(godot_context.CONTRACT_DIR / "contract.schema.json"))
    contract = _contract()
    del contract["ci"]
    apply_defaults(contract, schema)
    errs = validate(contract, schema)
    assert not errs, errs
    bad = _contract(ci={"repository": "not-a-repo-path", "ref": "short"})
    apply_defaults(bad, schema)
    errs = validate(bad, schema)
    assert errs, "非法 ci 段未被拒"


@check("契约 build.modules 条目校验：remote:/local: 前缀 / ref 白名单 / path 段 / 重复")
def t_modules():
    godot_context.assert_modules([], "build.modules")
    godot_context.assert_modules(
        ["remote:godot-spellbook/spellbook", "remote:o/r@v1.2.3",
         "remote:o/r2@main:modules/ai", "remote:o/r3@" + "a" * 40,
         "local:modules/ai"],
        "build.modules")
    bad_cases = [
        (["godot-spellbook/spellbook"], "缺 remote:/local: 前缀"),
        (["local:modules/ai", "local:modules/ai"], "条目重复"),
        (["remote:o/r@--upload-pack=x"], "ref 选项注入"),
        (["remote:o/r@bad..ref"], "ref 含 .."),
        (["remote:o/r@v1.lock"], "ref 以 .lock 结尾"),
        (["remote:o/r@a@{b"], "ref 含 @{"),
        (["remote:o/r:../escape"], "path 含 .. 段"),
        (["local:../escape"], "local path 含 .. 段"),
        (["remote:justname"], "缺 owner 段"),
        (["remote:a/b/c"], "repo 段含斜杠"),
    ]
    for entries, why in bad_cases:
        expect_fail(lambda e=entries: godot_context.assert_modules(e, "build.modules"), why)


@check("合成默认契约：无项目契约时的取值来源（引擎官方仓库 + 基线 + schema default）")
def t_synth_contract():
    """契约是可选的：无契约的外部项目正是「CI 仓库侧构建」要服务的对象。

    合成契约只填 schema 必填四段（engine / build / export / artifacts）；
    platforms / roles / hooks / ci 留空即「沿用基线」，不编造覆写。
    """
    schema = load_schema(str(godot_context.CONTRACT_DIR / "contract.schema.json"))
    contract = godot_context.synth_contract(schema)
    # 必填四段齐备且过校验（synth_contract 内部已 validate_or_fail）
    for seg in ("engine", "build", "export", "artifacts"):
        assert seg in contract, seg
    # 引擎仓库默认官方；ref 不在这里编造（由 resolve_engine 自动解析或 UI 提供）
    assert contract["engine"]["source"] == "godotengine/godot", contract["engine"]
    assert "@" not in contract["engine"]["source"], "ref 不应在合成契约里硬编码"
    # 平台取基线全量；target 取 schema default
    assert set(contract["engine"]["platform"]) == set(godot_context.PLATFORMS)
    assert contract["engine"]["target"] == ["template_release"]
    # artifacts / export.paths 走 schema default，不写死在代码里
    assert contract["export"]["paths"]["project_godot"] == "project.godot"
    assert contract["export"]["paths"]["export_presets"] == "export_presets.cfg"
    assert contract["artifacts"]["build_naming"] == "{prefix}_{platform}_{target}"
    assert contract["artifacts"]["export_naming"] == "{prefix}_{preset}"
    # 覆写段留空（沿用基线），不是编造一份
    assert not contract.get("platforms")
    assert not contract.get("ci")


@check("引擎 ref 自动解析：优先取 override，否则查最新 stable tag")
def t_latest_stable_ref():
    # override 生效（离线测试 / 逃生口），不触网
    os.environ["ENGINE_LATEST_REF"] = "9.9.9-stable"
    try:
        assert godot_context.latest_stable_ref("any/repo") == "9.9.9-stable"
    finally:
        os.environ.pop("ENGINE_LATEST_REF", None)

    # 真实查询走 ls-remote：用不存在的仓库验证 fail-fast（不回落默认版本）
    os.environ.pop("ENGINE_LATEST_REF", None)
    expect_fail(lambda: godot_context.latest_stable_ref("no-such-owner-xyz/no-such-repo-xyz"),
                "仓库不存在时无法解析版本",
                needle="无法解析引擎最新版本")


@check("context.artifacts.retention：已封闭（多余键被拒、四键为整数）")
def t_context_retention_closed():
    contract = _contract()
    baseline = _baseline()
    ctx = godot_context.build_context(contract, baseline, "build", "template_release",
                                      "linux", "x86_64")
    schema = load_schema(str(godot_context.CONTRACT_DIR / "context.schema.json"))
    assert validate(ctx, schema) == [], validate(ctx, schema)
    assert set(ctx["artifacts"]["retention"]) == {"build", "prefetch", "export", "contract"}
    # 多余键：以前该段没有 additionalProperties:false，写错键名静默失效
    bad = json.loads(json.dumps(ctx))
    bad["artifacts"]["retention"]["bogus"] = 1
    errs = validate(bad, schema)
    assert any("bogus" in e for e in errs), errs
    # 类型收敛：保留天数是整数，字符串不再被放过
    bad2 = json.loads(json.dumps(ctx))
    bad2["artifacts"]["retention"]["build"] = "7"
    assert validate(bad2, schema), "字符串 retention 未被拒"


@check("真实平台基线：结构完整，且档案与导出 runner 的 OS 族一致")
def t_real_baseline():
    """校验仓库里那份真实 platform-baseline.json，而不是自造夹具。

    其余用例都用 _baseline() 造夹具（该造时会用固定值），所以基线文件本身写错
    （缺字段、取值与平台约束不符）不会被任何用例发现——这条补上那个缺口。

    只断言**约束**，不断言**当前策略取值**：具体选哪个 runner / 哪份档案是 CI
    维护者按引擎演进调的默认（基线注释：改原生导出就把 export_runner 与
    editor_archive 一起换），锁死这些值会让合法改动误报。
    """
    real = _real_baseline()
    plats = real["platforms"]
    assert set(plats) == set(godot_context.PLATFORMS), sorted(plats)

    for name, cfg in plats.items():
        for field in ("build_runner", "export_runner", "editor_archive"):
            assert cfg.get(field), f"{name} 缺 {field}"
        # runner 标签必须是不含空格的单值（含空格会被 runs-on 当成多个标签）
        for field in ("build_runner", "export_runner"):
            assert " " not in cfg[field], f"{name}.{field} 含空格：{cfg[field]!r}"
        # 档案是下载到 export_runner 上执行的编辑器：OS 族必须一致，
        # 否则改原生导出时漏改另一半（如 export_runner=macos-26 仍配 linux.x86_64）
        runner_os = _runner_os(cfg["export_runner"])
        archive_os = _archive_os(cfg["editor_archive"])
        assert runner_os == archive_os, (
            f"{name}: export_runner {cfg['export_runner']!r}（{runner_os}）与 "
            f"editor_archive {cfg['editor_archive']!r}（{archive_os}）OS 族不一致")

    # Apple 平台的导出器硬依赖 codesign / xcrun，无法在 Linux 上导出
    for apple in ("ios", "visionos"):
        assert _runner_os(plats[apple]["export_runner"]) == "macos", \
            f"{apple} 的 export_runner 必须是 macOS（导出器依赖 codesign/xcrun）"

    # roles：resolve 键是结构性必需（runner_baseline.py 读取），其余键可选
    assert real["roles"].get("resolve"), "roles 缺 resolve 键（runner.yml 仲裁依赖它）"

    # toolchain：满足 Godot 4.7 的下限（emsdk>=4.0.0 / scons>=4.4 / JDK 17）
    tc = real["toolchain"]
    assert int(tc["emsdk"].split(".")[0]) >= 4, tc["emsdk"]
    assert tuple(int(x) for x in tc["scons"].split(".")[:2]) >= (4, 4), tc["scons"]
    assert tc["java"] == "17", tc["java"]


def _runner_os(label: str) -> str:
    """runner 标签 → OS 族（linux / macos / windows）。"""
    if label.startswith("macos-"):
        return "macos"
    if label.startswith("windows-"):
        return "windows"
    if label.startswith("ubuntu-"):
        return "linux"
    raise AssertionError(f"无法识别的 runner 标签：{label!r}")


def _archive_os(archive: str) -> str:
    """编辑器档案后缀 → OS 族；未登记的形态直接报错，避免断言静默失效。"""
    for prefix, os_name in (("linux.", "linux"), ("macos.", "macos"), ("win", "windows")):
        if archive.startswith(prefix):
            return os_name
    raise AssertionError(f"无法识别的编辑器档案：{archive!r}")


@check("真实平台基线：Web 变体只用 wasm32（wasm64 产物不可用）")
def t_real_baseline_web_variants():
    """wasm64 只在编译期被接受、产物不可用（官方已 revert，godot-cpp 硬性拒绝）。"""
    real = _real_baseline()
    variants = real["platforms"]["web"]["variants"]
    assert len(variants) == 2, variants
    names = sorted(v["variant"] for v in variants)
    assert names == ["nothreads", "threads"], names
    for v in variants:
        flags = v["scons_flags"]
        assert "arch=wasm32" in flags, f"{v['variant']} 必须用 wasm32：{flags}"
        assert "wasm64" not in flags, f"{v['variant']} 不得使用 wasm64（产物不可用）：{flags}"
    # threads 维度由 threads= 区分，不是架构
    thread_flags = {v["variant"]: v["scons_flags"] for v in variants}
    assert "threads=yes" in thread_flags["threads"], thread_flags
    assert "threads=no" in thread_flags["nothreads"], thread_flags


# 跨仓形态缺 caller-uses-ref 不再静默跳过
@check("跨仓 + 有 ci 段 + caller-uses-ref 空 → resolve 期 fail-fast")
def t_cross_repo_missing_caller_ref():
    os.environ.pop("CALLER_USES_REF", None)
    os.environ["GITHUB_REPOSITORY"] = "me/proj"
    os.environ["WORKFLOW_REPOSITORY"] = "me/ci"   # 跨仓形态
    try:
        expect_fail(lambda: godot_context.assert_sha_consistency(_contract()),
                    "跨仓缺 caller-uses-ref", needle="caller-uses-ref")
    finally:
        for k in ("GITHUB_REPOSITORY", "WORKFLOW_REPOSITORY", "CALLER_USES_REF"):
            os.environ.pop(k, None)


@check("同仓形态（CI 仓库直触发）caller-uses-ref 空 → 放行")
def t_standalone_empty_caller_ref_ok():
    os.environ.pop("CALLER_USES_REF", None)
    os.environ["GITHUB_REPOSITORY"] = "me/proj"
    os.environ["WORKFLOW_REPOSITORY"] = "me/proj"
    try:
        assert not godot_context.is_cross_repo()
        assert godot_context.assert_sha_consistency(_contract()) == "a" * 40
    finally:
        for k in ("GITHUB_REPOSITORY", "WORKFLOW_REPOSITORY"):
            os.environ.pop(k, None)


@check("跨仓 + caller-uses-ref 与 ci.ref 一致 → 放行")
def t_cross_repo_caller_ref_ok():
    os.environ["CALLER_USES_REF"] = "a" * 40
    os.environ["GITHUB_REPOSITORY"] = "me/proj"
    os.environ["WORKFLOW_REPOSITORY"] = "me/ci"
    try:
        assert godot_context.assert_sha_consistency(_contract()) == "a" * 40
    finally:
        for k in ("GITHUB_REPOSITORY", "WORKFLOW_REPOSITORY", "CALLER_USES_REF"):
            os.environ.pop(k, None)


# resolve_project 统一走 parse_source_entry
@check("remote:o/r@v1 → ('o/r','v1')，不再带 remote: 前缀")
def t_resolve_project_remote():
    os.environ["INPUT_PROJECT"] = "remote:o/r@v1"
    os.environ["GITHUB_REPOSITORY"] = "me/ci"
    try:
        assert godot_context.resolve_project({}) == ("o/r", "v1")
    finally:
        for k in ("INPUT_PROJECT", "GITHUB_REPOSITORY"):
            os.environ.pop(k, None)


@check("remote 缺 ref → 空串（clone_shallow 默认分支），不再伪造 HEAD")
def t_resolve_project_remote_no_ref():
    os.environ["INPUT_PROJECT"] = "remote:o/r"
    os.environ["GITHUB_REPOSITORY"] = "me/ci"
    try:
        assert godot_context.resolve_project({}) == ("o/r", "")
    finally:
        for k in ("INPUT_PROJECT", "GITHUB_REPOSITORY"):
            os.environ.pop(k, None)


@check("local: 保留当前仓库口径 + 非法 ref 经 ref_guard 拒绝")
def t_resolve_project_local_and_guard():
    os.environ["GITHUB_REPOSITORY"] = "me/ci"
    os.environ["GITHUB_SHA"] = "b" * 40
    os.environ["INPUT_PROJECT"] = "local:sub/proj"
    try:
        assert godot_context.resolve_project({}) == ("me/ci", "b" * 40)
    finally:
        for k in ("INPUT_PROJECT", "GITHUB_REPOSITORY", "GITHUB_SHA"):
            os.environ.pop(k, None)
    # 非法 ref 形态（前导 /）由 parse_source_entry → assert_valid_ref 拒绝
    os.environ["INPUT_PROJECT"] = "remote:o/r@/bad"
    os.environ["GITHUB_REPOSITORY"] = "me/ci"
    try:
        expect_fail(lambda: godot_context.resolve_project({}),
                    "remote 非法 ref", needle="ref")
    finally:
        for k in ("INPUT_PROJECT", "GITHUB_REPOSITORY"):
            os.environ.pop(k, None)


@check("hooks_policy 用解析后的 repo 判外部（不再靠 remote: 前缀巧合）")
def t_hooks_policy_parsed_repo():
    os.environ["GITHUB_REPOSITORY"] = "me/ci"
    os.environ["INPUT_PROJECT"] = "remote:o/r@v1"
    try:
        repo, _ = godot_context.resolve_project({})
        assert repo == "o/r", repo
        captured = capture(lambda: godot_context.resolve_hooks_policy(_contract(), repo))
        assert captured.value == "off", captured.raw_stdout
    finally:
        for k in ("GITHUB_REPOSITORY", "INPUT_PROJECT"):
            os.environ.pop(k, None)


def main() -> int:
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print(f"[ok] {name}")
        except Exception as e:  # noqa: BLE001 - 自测捕获任意异常
            failed.append((name, e))
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
