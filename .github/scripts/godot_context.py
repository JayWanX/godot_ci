#!/usr/bin/env python3
"""契约归一化器：读 ci-contract.json + platform-baseline.json → 产 context.json。

resolve 系列脚本（build / export）共用本模块，保证「项目可配值只有一个来源」。
context.json 只服务钩子；CI 本体的项目可配值从本模块 resolve_outputs() 取。

用法：
  godot_context.py --resolve --chain build --out context.json   # 生成并写文件
  godot_context.py --check                                      # 只校验不写
  godot_context.py --context context.json --chain export            # 消费侧重验
  godot_context.py --outputs                                     # 人查归一化结果（调试用；
                                                             #   workflow 内由 resolve 脚本 import 调用）

环境变量（由 resolve action 的 env: 提供）：
  CI_CONTRACT / PLATFORM_BASELINE / CONTRACT_DIR / CONTEXT_SCHEMA 路径
  GITHUB_REPOSITORY / GITHUB_SHA / GITHUB_REF_NAME / GITHUB_RUN_ID
  GITHUB_RUN_ATTEMPT / GITHUB_EVENT_NAME / GITHUB_WORKSPACE / RUNNER_OS
  INPUT_PROJECT / INPUT_ENGINE_SOURCE / INPUT_ENGINE_REF / INPUT_TARGET / INPUT_PLATFORM / INPUT_SCONS_FLAGS
  INPUT_REPO_TOKEN / INPUT_PROFILE / INPUT_BUILD_PROFILE / INPUT_CUSTOM_MODULES
  CALLER_USES_REF / ENGINE_SHA / REPO_SAFE / ARTIFACT_PREFIX / TARGET / PLATFORM / ARCH / CHAIN
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jsonc import ensure_utf8_stdio, read_json, escape_error  # noqa: E402
from ref_guard import assert_valid_ref  # noqa: E402
from schema import load_schema, validate_or_fail, apply_defaults, read_json_validated  # noqa: E402
import path_guard  # noqa: E402

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contract"

# 无契约时的默认引擎仓库（合成契约与文档指针共用同一常量，不重复字面量）
DEFAULT_ENGINE_REPO = "godotengine/godot"

PLATFORMS = ("linux", "windows", "macos", "android", "ios", "visionos", "web")

# 平台 → 允许构建的 target 集合（CI 固有能力面，与各平台 workflow 的 target 门控一致，
# 一致性由 check_ci_assets.py 校验）。
#
# 判据是这个 target 在该平台上**能否被 SCons 编出来**，不是 CI 能否在 runner 上跑起来验证。
# 后者取决于宿主：wasm / iOS / visionOS 产物无法在 runner 执行，但编译本身完全成立，
# 产物照常上传，验证由使用者在本机完成——CI 没有验收手段不构成「不支持」。
# 唯一的真实限制是 web + editor：Emscripten 目标无法构建出桌面编辑器，属引擎事实。
PLATFORM_TARGET_SUPPORT = {
    "linux": ("editor", "template_debug", "template_release"),
    "windows": ("editor", "template_debug", "template_release"),
    "macos": ("editor", "template_debug", "template_release"),
    "android": ("editor", "template_debug", "template_release"),
    "ios": ("editor", "template_debug", "template_release"),
    "visionos": ("editor", "template_debug", "template_release"),
    "web": ("template_debug", "template_release"),
}

# 契约平台名 → SCons 平台名。CI 内部（工作流 / action / context）一律用契约平台名
# linux；唯一消费点是 compile 执行器 scons_build.py 调 scons 时。
CONTRACT_TO_SCONS_PLATFORM = {
    "linux": "linuxbsd",
    "windows": "windows",
    "macos": "macos",
    "android": "android",
    "ios": "ios",
    "visionos": "visionos",
    "web": "web",
}


def fail(message: str) -> None:
    print(f"::error::{escape_error(message)}", file=sys.stderr)
    sys.exit(1)


def env(name, default=""):
    return os.environ.get(name, default)


def paths():
    """返回 (契约, 基线, 上下文 schema) 三路径，可用环境变量覆写（供离线测试）。"""
    return (
        env("CI_CONTRACT", str(CONTRACT_DIR.parent / "ci-contract.json")),
        env("PLATFORM_BASELINE", str(CONTRACT_DIR / "platform-baseline.json")),
        env("CONTEXT_SCHEMA", str(CONTRACT_DIR / "context.schema.json")),
    )


def load_contract(allow_missing=False):
    """读并校验契约。

    allow_missing=True 用于「CI 仓库构建无契约的外部项目」：项目仓库没有
    ci-contract.json 时改用合成默认契约（见 synth_contract），不直接失败。
    有契约时一律静默使用项目那份——外部项目构建的配置来源唯一。
    """
    contract_path, baseline_path, _ = paths()
    schema = load_schema(str(CONTRACT_DIR / "contract.schema.json"))
    if allow_missing and not Path(contract_path).is_file():
        contract = synth_contract(schema)
    else:
        contract = read_json_validated(contract_path, "ci-contract.json", schema, "契约")
    baseline = read_json(baseline_path, "platform-baseline.json")
    return contract, baseline, schema


def synth_contract(schema):
    """合成默认契约：无项目契约时的取值来源。

    只填 schema 必填的四段（engine / build / export / artifacts）；其余段
    （platforms / roles / hooks / ci）留空 —— 它们本就是「覆写基线」语义，
    留空即沿用基线或 schema default。

    engine.source 的 ref 由调用方经 INPUT_ENGINE_SOURCE / INPUT_ENGINE_REF 提供
    （resolve 期用 UI 值覆写，见 resolve_engine）；此处给默认仓库作为兜底，ref 留空串
    由 resolve_engine 判定：未指定时自动解析该仓库最新的 stable tag（可追溯，且日志打
    notice），解析失败才 fail-fast——不静默落到任何固定版本。
    """
    contract = {
        "engine": {
            "source": env("ENGINE_DEFAULT_REPO", DEFAULT_ENGINE_REPO),
            "target": ["template_release"],
            "platform": list(PLATFORMS),
        },
        "build": {},
        "export": {},
        "artifacts": {},
    }
    apply_defaults(contract, schema)
    return validate_or_fail(contract, schema, "合成契约")



def is_cross_repo():
    """本次运行是否为跨仓形态：CI 资产住另一仓库、由薄触发器调用。

    `job.workflow_repository` 是「正在运行的这份工作流所属仓库」，跨仓 called workflow
    下即 CI 资产仓库；它与触发仓库不同即为跨仓形态。单仓自用（全套 .github 在项目仓库内）
    两者相同，返回 False。缺值（本地/文档场景）按同仓处理。
    """
    workflow_repo = env("WORKFLOW_REPOSITORY")
    return bool(workflow_repo) and workflow_repo != env("GITHUB_REPOSITORY")


def resolve_hooks_policy(contract, project_repo):
    """扩展策略终值：外部项目构建强制 off，清单不进 bundle、不在 CI runner 执行。

    清单来自待构建项目仓库；外部项目构建的清单会在 CI 仓库 runner 上执行，
    而钩子沙箱只做 post-diff（覆盖通道根与脚本闭包），不构成文件系统隔离。
    外部项目判定：项目仓库 != 触发仓库（`github.repository`）。
    """
    policy = contract["hooks"]["policy"]
    current_repo = env("GITHUB_REPOSITORY")
    if project_repo and current_repo and project_repo != current_repo and policy != "off":
        print(f"::notice::外部项目构建（{project_repo}）强制 hooks-policy=off："
              f"不读取/执行外部仓库的扩展清单")
        return "off"
    return policy


def parse_schema_url_ref(url):
    """从 `$schema` URL 解析出 ref 段；非 raw 形态返回 None。

    只认 `https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>`。
    blob 形态（`github.com/<owner>/<repo>/blob/<ref>/...`）里 ref 的位置不同，
    按 raw 的索引取会取到 "blob"——那正是「解析出来一个假 ref 然后报误导错误」
    的来源。与静态校验共用本函数，两处判据不再各写一套。
    """
    parts = [p for p in url.split("/") if p]
    if len(parts) < 5:
        return None
    if parts[0] != "https:" or parts[1] != "raw.githubusercontent.com":
        return None
    return parts[4]


def assert_sha_consistency(contract):
    """$schema URL 的 ref 段 = ci.ref = 调用方 uses:@ref（后者经 caller-uses-ref 入参告知）。

    ci.ref 是分支/tag（默认 main，跟随更新）或 40 位 SHA（锁版本），断言对两者同样成立。
    `ci` 段只承担这组一致性断言，不决定检出路径——检出由 job.workflow_* 自描述。
    跨仓形态必填（否则跳过一个本就必需的防漂移断言），单仓自用可省略。
    """
    ci = contract.get("ci")
    if not ci:
        if is_cross_repo():
            fail("跨仓形态（CI 资产住另一仓库）必须在契约 ci 段声明 repository + ref："
                 "该段用于断言 $schema URL 的 ref 段 / ci.ref / 调用方 uses:@ref 三者一致")
        return ""
    ci_ref = ci["ref"]
    schema_url = contract.get("$schema", "")
    # 私有 CI 资产仓库可用相对路径，此时跳过 URL ref 比对（契约注释已注明）
    if schema_url.startswith("https://"):
        url_ref = parse_schema_url_ref(schema_url)
        if url_ref is None:
            fail(f"$schema 不是 raw 形态的 GitHub URL：'{schema_url}'。"
                 f"只接受 https://raw.githubusercontent.com/<owner>/<repo>/<ref>/.github/..."
                 f"（blob 形态的 URL 里 <ref> 位置不是 ref，解析出来的值毫无意义）")
        if url_ref != ci_ref:
            fail(f"$schema 的 ref「{url_ref}」与 ci.ref「{ci_ref}」不一致")
    caller_ref = env("CALLER_USES_REF")
    # 跨仓形态下薄触发器必须显式传 caller-uses-ref（三方 pin 断言的一环）。
    # 空串会让「uses:@ref vs ci.ref」这段断言永久静默跳过——手写薄触发器漏传时
    # 跨仓防漂移形同虚设，必须在 resolve 期响亮失败。CI 仓库直触发（同仓形态）
    # 没有「调用方 uses:@ref」这个事实，空值是正确表达，放行。
    if not caller_ref and is_cross_repo():
        fail("跨仓形态（CI 资产住另一仓库）缺 caller-uses-ref：调用方 uses:@ref "
             "未透传，三方 pin 一致性断言会静默跳过；请在薄触发器传 "
             "caller-uses-ref（与 uses: …@<ref> 同值）")
    if caller_ref and caller_ref != ci_ref:
        fail(f"调用方 uses:@ref「{caller_ref}」与 ci.ref「{ci_ref}」不一致")
    return ci_ref


def resolve_project(contract):
    """自有项目构建恒为自己；外部项目构建的 project 仅由 CI 仓库运行时输入（INPUT_PROJECT）传入。

    来源声明语法的唯一实现是 parse_source_entry：这里不再自造
    partition("@") 的第二套解析——前缀剥除、ref 白名单、path 校验全交给它。
    """
    ui_project = env("INPUT_PROJECT")
    current_repo = env("GITHUB_REPOSITORY")
    current_sha = env("GITHUB_SHA")
    if not ui_project:
        return current_repo, current_sha or env("GITHUB_REF_NAME")
    parsed = parse_source_entry(ui_project, "project 入参")
    if parsed["kind"] == "remote":
        # ref 为空 = 默认分支（与 git_ops.clone_shallow 口径一致），不再伪造 "HEAD"
        return parsed["repo"], parsed["ref"]
    # local: 场景：项目树仍在当前检出内，project.repo 保持当前仓库口径
    return current_repo, current_sha or env("GITHUB_REF_NAME")


def resolve_engine(contract, baseline):
    """引擎来源/版本：INPUT_ENGINE_SOURCE 优先于契约。单形态字段，裸写 owner/repo[@ref]，无来源前缀。

    引擎源码即整个仓库，仓库根即引擎根，故无 path 段。

    ref 缺失时（无契约 + 未给 engine-ref）自动解析该仓库最新的 stable tag，用于
    「临时构建别人的项目」这类一次性场景。解析失败即 fail-fast，不回落任何默认版本
    ——隐藏默认会让构建产物来源不可追溯。
    """
    ui_source = env("INPUT_ENGINE_SOURCE")
    source = ui_source or contract["engine"]["source"]
    if source.startswith(("remote:", "local:")):
        fail("engine.source 恒为远程仓库，直接写 owner/repo[@ref]，无需 remote:/local: 来源前缀")
    if ":" in source:
        # 引擎源码即整个仓库，仓库根即引擎根：写 :path 只会在 CI 里被静默忽略，
        # 用户却以为换成了子目录，故显式拒绝而不是接受一个不生效的语法。
        fail(f"engine.source 不接受 path 段（引擎源码即整个仓库）：'{source}'；"
             f"应写 owner/repo[@ref]")
    repo, _, ref = source.partition("@")
    owner, _, name = repo.partition("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        fail(f"engine.source 的仓库段非法，应为 owner/repo[@ref]：'{source}'")
    ui_ref = env("INPUT_ENGINE_REF")
    if ui_ref:
        ref = ui_ref
    if not ref:
        ref = latest_stable_ref(repo)
    return repo, ref, env("ENGINE_SHA"), ""


def latest_stable_ref(repo):
    """解析 repo 最新的 `<N>.<N>[.<N>]-stable` tag；失败直接 fail。

    唯一用途是「无契约 + 未指定版本」时给出一个可追溯的起点（日志里会打 notice）。
    只认 stable tag，不取 -rc / -beta：CI 构建显然不该默认落到预发布版。
    """
    override = env("ENGINE_LATEST_REF")  # 离线测试/逃生口：显式给值即不查网络
    if override:
        return override
    proc = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", f"https://github.com/{repo}.git"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        fail(f"无法解析引擎最新版本（git ls-remote {repo} 失败："
             f"{proc.stderr.strip()}）；请显式填写 engine-ref")
    tags = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        tag = parts[1].rsplit("/", 1)[-1]
        m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?-stable", tag)
        if m:
            tags.append(((int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)), tag))
    if not tags:
        fail(f"引擎 {repo} 没有任何 <N>.<N>[.<N>]-stable tag；请在 engine-source 显式带上 @ref")
    latest = max(tags)[1]
    print(f"::notice::未指定引擎版本，自动解析 {repo} 最新 stable tag：{latest}")
    return latest



# ---------------------------------------------------------------------------
# compat.standard_includes：auto 按 engine_ref + toolchain + platform 分档
# ---------------------------------------------------------------------------

def lookup_toolchain(toolchain, dotted_key):
    """按点号路径取工具链字段；缺失返回 None。"""
    node = toolchain
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def resolve_standard_includes(contract, baseline, eng_ref, toolchain, platform):
    """返回 on / off。

    契约显式 on/off 直接采纳。auto = 跟随基线：底值取基线 compat.default
    （CI 维护者按引擎版本演进全局调整），命中 skip_rules 恒 off（规则是单向
    关闭）。规则按 engine_ref（通配）+ toolchain（字段全等）+ platform（可选）
    判定；规则引用的工具链字段缺失 → fail-fast（数据缺失不静默跳过）。
    """
    value = contract["build"]["compat"]["standard_includes"]
    if value in ("on", "off"):
        return value
    compat = baseline.get("compat", {}).get("standard_includes", {})
    rules = compat.get("skip_rules")
    if not isinstance(rules, list):
        fail("平台基线 compat.standard_includes.skip_rules 缺失，无法判定 auto；"
             "请补齐基线或显式 on/off")
    # 基线是 CI 自有数据：缺 compat.default 属数据不完整，直接报错，不用代码兜底
    base_default = compat.get("default")
    if base_default not in ("on", "off"):
        fail(f"平台基线 compat.standard_includes.default 须为 on/off，实际 {base_default!r}")
    for rule in rules:
        if not isinstance(rule, dict):
            fail(f"平台基线 skip_rules 条目必须是对象，实际 {type(rule).__name__}")
        ref = rule.get("engine_ref")
        if not isinstance(ref, str) or not ref:
            fail("平台基线 skip_rules 条目缺 engine_ref")
        if not fnmatch.fnmatch(eng_ref, ref):
            continue
        rule_platform = rule.get("platform")
        if rule_platform and rule_platform != platform:
            continue
        wanted = rule.get("toolchain") or {}
        if not isinstance(wanted, dict):
            fail(f"平台基线 skip_rules 条目（engine_ref={ref}）的 toolchain 必须是对象")
        matched = True
        for key, expect in wanted.items():
            actual = lookup_toolchain(toolchain, key)
            if actual is None:
                fail(f"平台基线 skip_rules 规则（engine_ref={ref}, platform={platform}）"
                     f"引用了缺失的工具链字段 '{key}'；请补齐基线 toolchain 或契约 build.toolchain")
            if str(actual) != str(expect):
                matched = False
                break
        if matched:
            return "off"
    return base_default


def resolve_compat_map(contract, baseline, eng_ref, toolchain):
    """按平台逐个判定 standard_includes，返回 {platform: on|off}。"""
    return {
        platform: resolve_standard_includes(contract, baseline, eng_ref,
                                            toolchain, platform)
        for platform in PLATFORMS
    }


# ---------------------------------------------------------------------------
# 运行时上下文
# ---------------------------------------------------------------------------

def build_context(contract, baseline, chain, target, platform, arch):
    """组装 context.json（只服务钩子，不含 layout / toolchain 绝对路径 / secrets / release）。

    platform / target 分语境定值：多平台批的 resolve 期（两个 resolve）与 release 链
    未传即 null（= 不过滤，hook_matches 与 on.platform 省略同构）；单平台 job 期
    （build-pipeline / export-platform）传单值。空串形态已废除。
    """
    proj_repo, proj_ref = resolve_project(contract)
    eng_repo, eng_ref, eng_sha, _ = resolve_engine(contract, baseline)
    engine = contract["engine"]
    build = contract["build"]
    export = contract["export"]
    artifacts = contract["artifacts"]

    prefix = artifacts.get("prefix") or env("ARTIFACT_PREFIX") or env("REPO_SAFE")
    context = {
        "schema": "godot.ci/hook-context",
        "chain": chain,
        "platform": platform or None,
        "target": target or None,
        "arch": arch,
        "engine": {"repo": eng_repo, "ref": eng_ref, "sha": eng_sha},
        "project": {"repo": proj_repo, "ref": proj_ref, "root": env("PROJECT_ROOT", env("GITHUB_WORKSPACE"))},
        "export": {
            "type": export["type"],
            "editor_version": export.get("editor_version", ""),
            # 本次实际导出的预设名单（resolve 归一化后经 env 传入）；
            # 缺省回落契约白名单（可能为空 = 项目全部预设）。
            "presets": [p for p in env("EXPORT_PRESETS_ACTUAL").split(",") if p]
                       or export.get("presets", []),
        },
        "paths": {
            "engine_root": env("ENGINE_ROOT", ""),
            "profile": env("PROFILE_PATH") or None,
            "build_profile": env("BUILD_PROFILE_PATH") or None,
            "modules_dir": env("MODULES_DIR") or None,
            "dist": env("DIST_PATH") or None,
            "export_presets": str(Path(env("PROJECT_ROOT", env("GITHUB_WORKSPACE"))) / export["paths"]["export_presets"]),
        },
        "cache": {
            "config_hash": env("CONFIG_HASH", ""),
            "source_hash": env("SOURCE_HASH", ""),
            "scons_flags_base": build.get("scons_flags", ""),
        },
        "artifacts": {"prefix": prefix,
                      "build_naming": artifacts.get("build_naming", ""),
                      "export_naming": artifacts.get("export_naming", ""),
                      "retention": artifacts.get("retention", {})},
        "runner": {"os": env("RUNNER_OS"), "workspace": env("GITHUB_WORKSPACE")},
        "run": {
            "id": env("GITHUB_RUN_ID"),
            "attempt": int(env("GITHUB_RUN_ATTEMPT", "1") or 1),
            "event": env("GITHUB_EVENT_NAME"),
        },
    }
    return context


def validate_context(context):
    """用 context.schema.json 硬校验。"""
    _, _, context_schema_path = paths()
    schema = load_schema(context_schema_path)
    return validate_or_fail(context, schema, "context.json")


def assert_safe_relpath(path, label):
    """来源声明 path 段安全校验；唯一实现在 path_guard.check_rel_path。"""
    try:
        path_guard.check_rel_path(path, label)
    except path_guard.PathError as e:
        fail(str(e))


def parse_source_entry(entry, label):
    """来源声明条目解析（语法见 docs/workflows.md「来源声明语法」）。

    remote:<owner/repo>[@<ref>][:<path>] → kind=remote
    local:<path>                          → kind=local
    """
    if entry.startswith("remote:"):
        spec = entry[len("remote:"):]
        path = ""
        if ":" in spec:  # owner/repo 不含冒号，第一个冒号起为 path 段
            spec, path = spec.split(":", 1)
        repo, _, ref = spec.partition("@")
        owner, _, name = repo.partition("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            fail(f"{label} 条目 '{entry}' 的仓库段非法，应为 owner/repo")
        if ref:
            assert_valid_ref(ref, f"{label} 条目 '{entry}' 的 ref")
        if path:
            assert_safe_relpath(path, f"{label} 条目 '{entry}' 的 path")
        return {"kind": "remote", "repo": repo, "ref": ref, "path": path}
    if entry.startswith("local:"):
        path = entry[len("local:"):]
        assert_safe_relpath(path, f"{label} 条目 '{entry}' 的 path")
        return {"kind": "local", "path": path}
    fail(f"{label} 条目 '{entry}' 缺少 remote:/local: 前缀（语法见 docs/workflows.md「来源声明语法」）")


def assert_modules(entries, label):
    """契约 build.modules 条目 resolve 期 fail-fast（零成本本地校验）。

    仓库存在性、模块注册名解析与最终目录名冲突留构建期（config-inject 的
    config_inject.py 兜底）；这里只做语法与字面重复。
    """
    seen = set()
    for entry in entries:
        parse_source_entry(entry, label)
        if entry in seen:
            fail(f"{label} 条目重复：'{entry}'")
        seen.add(entry)


def assert_platform_target_support(platforms, target):
    """resolve 期逐组合校验所选平台 × 所选 target（PLATFORM_TARGET_SUPPORT）。

    不支持即 fail-fast 并打印该平台可用 target——静默零产物（如 web+editor 被
    job 级 if 整块跳过）必须在 resolve 期暴露。
    """
    for plat in platforms:
        supported = PLATFORM_TARGET_SUPPORT.get(plat)
        if supported is None:
            continue  # 平台白名单由调用方先校验（PLATFORMS）
        if target not in supported:
            fail(f"平台 '{plat}' 不支持构建目标 '{target}'；"
                 f"该平台可用 target：{', '.join(supported)}")


def resolve_platforms(contract, baseline):
    """平台配置 = 基线 platforms + 契约 platforms 逐平台逐字段深合并。

    基线 platforms 是权威平台清单（每个平台的全部平台级配置集中一处）；
    契约可按同名键覆写任意字段，未声明的字段沿用基线值。
    """
    base = baseline.get("platforms") or {}
    if not base:
        fail("平台基线缺 platforms 段（权威平台清单）")
    overrides = contract.get("platforms") or {}
    merged = {}
    for plat in PLATFORMS:
        if plat not in base:
            fail(f"平台基线 platforms 缺平台 '{plat}'")
        fields = dict(base[plat])
        for key, value in (overrides.get(plat) or {}).items():
            if key not in fields and key != "variants":
                fail(f"契约 platforms.{plat} 含未知字段 '{key}'；"
                     f"可用字段 {sorted(set(fields) | {'variants'})}")
            fields[key] = value
        merged[plat] = fields
    unknown = sorted(set(overrides) - set(PLATFORMS))
    if unknown:
        fail(f"契约 platforms 含未知平台 {unknown}；可用平台 {list(PLATFORMS)}")
    return merged


def resolve_runner(contract, baseline, platforms):
    """runner 映射（renderer 用）：平台键 → 构建 runner，角色键 → 链级 runner。

    roles 段 = 基线 roles + 契约 roles 覆写（契约 schema 只允许 publish / upload）。
    resolve 键结构性不可覆写：resolve 作业与 CI Self-check 的 runner 分配先于契约可读
    （runs-on 求值不依赖任何 step 输出，见 workflows/runner.yml），由基线唯一决定；
    契约 schema 的 roles 段不含该键，写出来即未知字段被拒。

    build_runner 缺失或为空即数据不完整：空串会被 fromJSON 取到并当作 runs-on 标签，
    job 在分派期报「no matching runner」这类远离根因的错。
    """
    mapping = {}
    for plat, cfg in platforms.items():
        runner = cfg.get("build_runner")
        if not runner:
            fail(f"平台 '{plat}' 缺 build_runner（平台基线 platforms.{plat} 与契约覆写均未提供）")
        mapping[plat] = runner
    mapping.update(baseline.get("roles") or {})
    mapping.update(contract.get("roles") or {})
    return mapping


def _unique_archives(platforms_cfg):
    """按 editor_archive 去重，返回 [{key, platform}]（key = 首个使用该档案的平台名）。

    多个平台常共用同一档案（交叉导出全用 linux.x86_64），逐平台展开会让 prefetch
    重复下载同一份；artifact 名只需区分档案本身即可。
    """
    seen = {}
    for plat, cfg in platforms_cfg.items():
        archive = cfg.get("editor_archive", "")
        if archive and archive not in seen:
            seen[archive] = {"key": plat, "platform": archive}
    return list(seen.values())


def resolve_outputs(contract, baseline, chain):
    """CI 本体消费的归一化结果（不写进 context 的散字段，避免 CI 绕 context 读布局）。

    resolve_build.py / resolve_export.py 从这里取项目可配值，写入 GITHUB_OUTPUT。
    """
    eng_repo, eng_ref, eng_sha, _ = resolve_engine(contract, baseline)
    engine = contract["engine"]
    build = contract["build"]
    export = contract["export"]
    artifacts = contract["artifacts"]
    toolchain = {**baseline.get("toolchain", {}), **build.get("toolchain", {})}
    # retention 四键由契约 schema default 补齐（load_contract 走 apply_defaults），
    # 下标直取：schema default 是唯一源，CI 代码不写兜底常量
    retention = artifacts.get("retention", {})
    platforms_cfg = resolve_platforms(contract, baseline)
    runner_map = resolve_runner(contract, baseline, platforms_cfg)
    compat_map = resolve_compat_map(contract, baseline, eng_ref, toolchain)
    prefix = artifacts.get("prefix") or env("REPO_SAFE")
    modules = build.get("modules", [])
    assert_modules(modules, "契约 build.modules")
    outputs = {
        "engine-repo": eng_repo,
        "engine-ref": eng_ref,
        "engine-sha": eng_sha,
        "scons-flags": build.get("scons_flags", ""),
        "profile": build["paths"]["profile"],
        "build-profile": build["paths"]["build_profile"],
        "custom-modules": ",".join(modules),
        "targets": ",".join(engine["target"]),
        "platforms": ",".join(engine["platform"]),
        "export-type": export["type"],
        "editor-version": export.get("editor_version", ""),
        "presets": ",".join(export.get("presets", [])),
        "export-project-godot": export["paths"]["project_godot"],
        "export-presets-path": export["paths"]["export_presets"],
        "export-dist-dir": export["paths"]["dist"],
        "artifact-prefix": prefix,
        "artifact-build-naming": artifacts.get("build_naming", ""),
        "artifact-export-naming": artifacts.get("export_naming", ""),
        "retention-build": str(retention["build"]),
        "retention-prefetch": str(retention["prefetch"]),
        "retention-export": str(retention["export"]),
        "retention-contract": str(retention["contract"]),
        "compat-standard-includes": json.dumps(compat_map, ensure_ascii=False,
                                               separators=(",", ":")),
        "toolchain-json": json.dumps(toolchain, ensure_ascii=False, separators=(",", ":")),
        "runner-json": json.dumps(runner_map, ensure_ascii=False, separators=(",", ":")),
        # 平台配置（已合并契约覆写）：平台 → {build_runner, export_runner,
        # editor_archive, variants}。构建/导出/预取三处消费同一份，避免多段各自取值。
        "platforms-json": json.dumps(platforms_cfg, ensure_ascii=False,
                                     separators=(",", ":")),
        # 预取 job 的 matrix-include 形态：[{key, platform}]，按**档案后缀去重**——
        # 交叉导出时多个平台共用同一档案（如全部用 linux.x86_64），重复列会重复下载。
        # key 取首个使用该档案的平台名，仅用于 artifact 命名与日志。
        "editor-archives-matrix": json.dumps(
            _unique_archives(platforms_cfg), ensure_ascii=False, separators=(",", ":")),
        "hooks-manifest": contract["hooks"]["manifest"],
        # --outputs 路径（release 链 resolve-hooks）同样适用外部项目强制 off
        "hooks-policy": resolve_hooks_policy(contract, resolve_project(contract)[0]),
    }
    # 按平台各出一个 on/off 标量：各平台作业直接取自己那个，
    # 免得把 JSON 映射传下去后与 'off' 比较永不成立（分档静默失效）。
    for plat, value in compat_map.items():
        outputs[f"compat-standard-includes-{plat}"] = value
    return outputs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve", action="store_true", help="生成 context.json")
    ap.add_argument("--check", action="store_true", help="只校验不写")
    ap.add_argument("--outputs", action="store_true", help="输出 CI 本体归一化结果到 GITHUB_OUTPUT")
    ap.add_argument("--context", default="", help="消费侧重验既有 context.json 路径")
    ap.add_argument("--chain", default="build")
    ap.add_argument("--target", default="")
    ap.add_argument("--platform", default="")
    ap.add_argument("--arch", default="")
    ap.add_argument("--out", default="context.json")
    args = ap.parse_args()

    contract, baseline, _ = load_contract()
    assert_sha_consistency(contract)

    if args.context:  # 消费侧重验
        data = json.loads(Path(args.context).read_text(encoding="utf-8"))
        validate_context(data)
        print(f"context.json 校验通过：{args.context}")
        return 0

    outputs = resolve_outputs(contract, baseline, args.chain)
    if args.outputs:
        out_path = env("GITHUB_OUTPUT")
        lines = [f"{k}={str(v).replace(chr(13), '').replace(chr(10), '')}" for k, v in outputs.items()]
        if out_path:
            with open(out_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        for line in lines:
            print(line)
        return 0

    context = build_context(contract, baseline, args.chain, args.target, args.platform, args.arch)
    validate_context(context)
    if args.check:
        print("契约与 context.json 校验通过（--check 未写文件）")
        return 0
    if args.resolve:
        Path(args.out).write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已生成 {args.out}")
        return 0
    print("未指定动作；用 --resolve / --check / --outputs / --context")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
