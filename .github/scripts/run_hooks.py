#!/usr/bin/env python3
"""CI 扩展钩子两阶段执行器（纯标准库，无第三方依赖）。

规格：docs/hooks.md（两阶段 / 锚点注册表 / 声明式过滤 / 按能力授权）。

子命令：
  validate --manifest <path> --chain <build|export|release> --context <context.json>
           --out <plan.json> --bundle-out <dir> [--anchors <hooks-anchors.json>]
           [--policy <off|declared>]
      离线静态校验 + 按 chain 产确定性 plan.json + 打 bundle + 写
      $GITHUB_OUTPUT 的 hooks-bundle / hooks-sha 两行。
      声明式过滤的求交判定不在这里（留到 dispatch），故 plan 不含运行期字段。

  dispatch --bundle <dir> --anchor <id> --phase <before|after>
           --context <context.json> --result <hooks-result.json>
           [--plan <plan.json>] [--chain <build|export|release>]
      运行期按 (anchor,phase) 取 hooks 并做声明式过滤求交，快照 → 子进程
      → 收集通道输出 → post-diff 未声明写入检查 → 失败回滚 → 写 hooks-result.json。

  bundle --plan <plan.json> --bundle-out <dir> --root <project_root>
         [--anchors <path>] [--policy declared] [--chain build]
      独立重建 bundle（plan + 脚本闭包 + sha256），与 validate 内部同一函数。

  selftest
      无参数自测：校验失败 / 声明式过滤 / flags 合并 / 路径逃逸拒绝 /
      write_paths 项目树写入与回滚 / plan 确定性 / 空转零成本 / ::error:: 转义。

约束：
- 三条写入通道（docs/hooks.md「可写面」）：
  dist 通道 = $GODOT_HOOK_DIST 下的产物（artifacts），会被打包搬走交付；
  directives 通道 = $GODOT_HOOK_DIRECTIVES 下的指令（scons_flags / patches / presets），
  由 workflow 层应用到既有对象后丢弃，本执行器只收集 + 摘要 + 合并 flags 结果写入
  hooks-result.json 的 `merged_flags`（唯一传值通道）；
  项目树通道 = write_paths 声明的项目内路径，本执行器做快照 / 放行 / 失败回滚 /
  build 链摘要，不代项目消费其产出。
- 子进程不继承父 job 全环境；不代为禁网（网络边界由 job 与触发形态治理）。
- 所有 ::error:: 消息经 jsonc.escape_error 转义。
"""
import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jsonc import ensure_utf8_stdio, read_json, read_json_optional, escape_error  # noqa: E402
from schema import load_schema, apply_defaults, validate  # noqa: E402
import path_guard  # noqa: E402

# .github/scripts/run_hooks.py -> parents[1] == .github
CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contract"
DEFAULT_ANCHORS = CONTRACT_DIR / "hooks-anchors.json"
DEFAULT_CAPABILITIES = CONTRACT_DIR / "hooks-capabilities.json"
DEFAULT_HOOKS_SCHEMA = Path(os.environ.get("HOOKS_SCHEMA", str(CONTRACT_DIR / "hooks.schema.json")))
DEFAULT_CONTEXT_SCHEMA = Path(os.environ.get("CONTEXT_SCHEMA", str(CONTRACT_DIR / "context.schema.json")))

PHASES = ("before", "after")
CHAINS = ("build", "export", "release")
POLICIES = ("off", "declared")

# 扩展名 -> 声明的解释器名（validate 期校验用）；dispatch 期再解析为真实可执行文件
EXT_INTERPRETER = {".py": "python3", ".sh": "bash"}

SECRET_CAP_RE = re.compile(r"^secret:([A-Z][A-Z0-9_]*)$")

# 三条写入通道的坐标系原点（执行期注入 build_hook_env）。
# dist 与 directives 是两个独立根：前者是会被打包搬走的产物，后者是作用于既有对象的
# 指令（编译标志 / 补丁 / 预设覆盖），用完即弃。混在一个根下会让「写入许可边界」与
# 「内容性质」互相挤压，也让「哪些文件会被交付」难以从目录结构上看出来。
DIST_ENV = "GODOT_HOOK_DIST"
DIRECTIVES_ENV = "GODOT_HOOK_DIRECTIVES"
PROJECT_ROOT_ENV = "GODOT_HOOK_PROJECT_ROOT"
# 声明 write:project_tree 时脚本必须引用的能力标记（validate 期强制）。
TREE_CAPABILITY = "write:project_tree"
# 触及这些前缀的 write_paths 会改仓库元数据或扩展清单本身：不禁止，但 resolve 期 notice。
SENSITIVE_WRITE_PREFIXES = (".git", ".github")

# 写入通道根：键是通道名，值是钩子工作目录下的相对目录名。
# 每个能力归属且仅归属一个通道（落点坐标系由此确定）。
CHANNEL_DIST = "dist"
CHANNEL_DIRECTIVES = "directives"

# outputs -> 落点声明（post-diff 与收集共用的唯一权威表）。
# 每项 = {"channel": dist|directives, "path": 该通道根下相对路径,
#         "kind": file|dir|glob}。用 kind 显式区分形态，不靠字符串隐式编码。
OUTPUT_TARGETS = {
    # 产物：唯一会被搬走交付的通道
    "artifacts": ({"channel": CHANNEL_DIST, "path": "artifacts", "kind": "dir"},),
    # 指令：作用于既有对象，消费后即丢弃，不进入交付物
    "scons_flags": ({"channel": CHANNEL_DIRECTIVES, "path": "scons_flags", "kind": "file"},),
    "profile_patch": ({"channel": CHANNEL_DIRECTIVES, "path": "patches/profile.*.diff",
                       "kind": "glob"},),
    "source_patch": ({"channel": CHANNEL_DIRECTIVES, "path": "patches/source.*.patch",
                      "kind": "glob"},),
    "presets": ({"channel": CHANNEL_DIRECTIVES, "path": "presets.json", "kind": "file"},),
}

MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_CLOSURE_FILES = 200
MAX_IMPORT_DEPTH = 12


class HookError(Exception):
    """可捕获的失败（CLI 层统一转成转义后的 ::error:: 并退出 1）。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def canonical(obj) -> bytes:
    """确定性序列化：dict 键排序、无空白、UTF-8。用于摘要与文件内容。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def print_error(message: str) -> None:
    print(f"::error::{escape_error(message)}", file=sys.stderr)


def write_github_output(pairs) -> None:
    """写 $GITHUB_OUTPUT（同时回显到 stdout 便于日志查看）。

    值里的 CR/LF 必须去掉，避免伪造后续 output 行。
    """
    lines = [f"{k}={str(v).replace(chr(13), '').replace(chr(10), '')}"
             for k, v in pairs]
    text = "\n".join(lines) + "\n"
    out = os.environ.get("GITHUB_OUTPUT", "")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(text)
    sys.stdout.write(text)


def load_json_file(path, description):
    p = Path(path)
    if not p.is_file():
        raise HookError(f"缺少 {description}: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HookError(f"{description} 解析失败 {p}: {e}")


def validate_anchor_table(anchors):
    """hooks-anchors.json 结构校验（validate / bundle 入口共用；check_ci_assets 静态校验复用）。

    字段拼错（chains 写成 chain）会被 .get() 兜底静默吞掉，此处显式拒绝；
    phases 全显式无缺省——缺省"双向都支持"会重新打开"钩子声明了不存在组合"
    的静默陷阱，由接线校验的 (id, phase) 粒度兜底。
    """
    if not isinstance(anchors, dict) or not isinstance(anchors.get("anchors"), list) or not anchors["anchors"]:
        raise HookError("hooks-anchors.json 顶层必须是含非空 anchors 数组的对象")
    seen = set()
    for item in anchors["anchors"]:
        if not isinstance(item, dict):
            raise HookError("hooks-anchors.json 条目必须是对象")
        aid = item.get("id")
        if not isinstance(aid, str) or not aid.strip():
            raise HookError("hooks-anchors.json 条目缺合法 id")
        if aid in seen:
            raise HookError(f"hooks-anchors.json 锚点 id 重复：'{aid}'")
        seen.add(aid)
        chains = item.get("chains")
        if not isinstance(chains, list) or not chains or not all(c in CHAINS for c in chains):
            raise HookError(f"锚点 '{aid}' 的 chains 须为 {'/'.join(CHAINS)} 的非空列表")
        phases = item.get("phases")
        if not isinstance(phases, list) or not phases or not all(p in PHASES for p in phases):
            raise HookError(f"锚点 '{aid}' 的 phases 须为 {'/'.join(PHASES)} 的非空列表（全显式）")


def load_context(path):
    context = load_json_file(path, "context.json")
    errs = validate(context, load_schema(str(DEFAULT_CONTEXT_SCHEMA)))
    if errs:
        raise HookError("context.json 校验失败：" + "; ".join(errs))
    return context


def project_root_of(context) -> Path:
    root = context.get("project", {}).get("root") or os.getcwd()
    return Path(root)


def flatten_context(context, prefix="", out=None):
    """把 context 展平成 GODOT_CONTEXT_* 环境变量值（只保留标量叶子）。"""
    if out is None:
        out = {}
    if isinstance(context, dict):
        for k, v in context.items():
            key = f"{prefix}_{k}" if prefix else k
            flatten_context(v, key, out)
    elif isinstance(context, list):
        out[prefix] = ",".join(str(x) for x in context)
    elif context is None:
        out[prefix] = ""
    else:
        out[prefix] = str(context)
    return out


def env_key(name: str) -> str:
    return "GODOT_CONTEXT_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


# ---------------------------------------------------------------------------
# 路径逃逸 / 解释器 / 依赖闭包
# ---------------------------------------------------------------------------

def validate_rel_path(rel, label, root: Path) -> str:
    """唯一实现在 path_guard.check_rel_path；此处把 PathError 转成 HookError。"""
    try:
        return path_guard.check_rel_path(rel, label, root)
    except path_guard.PathError as e:
        raise HookError(str(e))


def interpreter_for(rel: str) -> str:
    ext = PurePosixPath(rel).suffix.lower()
    if ext not in EXT_INTERPRETER:
        raise HookError(
            f"run '{rel}' 扩展名 '{ext or '(无)'}' 不在解释器白名单 "
            f"{sorted(EXT_INTERPRETER)}（只允许 *.py / *.sh）")
    return EXT_INTERPRETER[ext]


def resolve_interpreter(name: str) -> str:
    if name == "python3":
        return (os.environ.get("GODOT_HOOK_PYTHON")
                or shutil.which("python3") or sys.executable)
    if name == "bash":
        return (os.environ.get("GODOT_HOOK_BASH")
                or shutil.which("bash") or "bash")
    raise HookError(f"未知解释器 '{name}'")


def python_imports(text: str, source_rel: str) -> list:
    """静态解析 Python import，返回候选相对路径（不含扩展名以外的猜测）。"""
    try:
        tree = ast.parse(text, filename=source_rel)
    except SyntaxError as e:
        raise HookError(f"脚本 '{source_rel}' Python 语法错误（第 {e.lineno} 行）：{e.msg}")
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.append((alias.name, 0))
        elif isinstance(node, ast.ImportFrom):
            mods.append((node.module or "", node.level or 0))
    return mods


def import_candidates(mod, level, source_rel, root: Path) -> list:
    """把 import 模块名映射为项目根下可能存在的相对 .py 路径。"""
    base_parts = list(PurePosixPath(source_rel).parts[:-1])
    if level > 0:  # 相对 import：from .x / from ..x
        up = level - 1
        if up > len(base_parts):
            return []
        base_parts = base_parts[:len(base_parts) - up]
    name_parts = [p for p in (mod or "").split(".") if p]
    stem = base_parts + name_parts
    if not stem:
        return []
    rel = "/".join(stem)
    return [rel + ".py", rel + "/__init__.py"]


def closure_references(closure, root: Path, token: str) -> bool:
    """闭包内是否有脚本文本引用了指定标记（用于校验钩子确实使用了接口变量）。"""
    for rel in closure:
        path = root / rel
        try:
            if token in path.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def collect_closure(run: str, inputs: list, root: Path) -> list:
    """显式 with.inputs + Python import 递归扫描，返回排序后的相对路径闭包。

    只纳入项目根下真实存在的本地文件；stdlib / 第三方 import 自动跳过。
    """
    seen = set()
    queue = [(run, 0)] + [(x, 0) for x in inputs]
    total = 0
    while queue:
        rel, depth = queue.pop(0)
        if rel in seen:
            continue
        validate_rel_path(rel, "依赖闭包", root)
        p = root / rel
        if not p.is_file():
            raise HookError(f"依赖闭包: 引用的文件不存在 '{rel}'（相对 {root}）")
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            raise HookError(f"依赖闭包: '{rel}' 超过单文件大小上限 {MAX_FILE_BYTES} 字节")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise HookError(f"依赖闭包: 总大小超过 {MAX_TOTAL_BYTES} 字节")
        seen.add(rel)
        if len(seen) > MAX_CLOSURE_FILES:
            raise HookError(f"依赖闭包: 文件数超过 {MAX_CLOSURE_FILES}")
        if depth >= MAX_IMPORT_DEPTH or PurePosixPath(rel).suffix.lower() != ".py":
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # 非 UTF-8 的 .py 交由解释器自己报错
        for mod, level in python_imports(text, rel):
            for cand in import_candidates(mod, level, rel, root):
                if cand not in seen and (root / cand).is_file():
                    queue.append((cand, depth + 1))
    return sorted(seen)


# ---------------------------------------------------------------------------
# validate：静态校验 + 按 chain 产 plan
# ---------------------------------------------------------------------------

def static_validate_hook(hook, anchors_by_id, capability_table, secret_names, chain, root: Path, seen_ids) -> dict:
    """单个 hook 的静态校验；secret_names 为 {链: [名字]} 分组表。

    NAME 拼错（不在全集）与「本链不注入」（不在本链组）分开判、消息都带链名，
    让 release 链声明 secret:IOS_CERT_* 在 resolve 期就响亮失败。
    """
    hid = hook.get("id", "")
    if not isinstance(hid, str) or not hid:
        raise HookError("hook 缺少合法 id")
    if hid in seen_ids:
        raise HookError(f"hook id 重复: '{hid}'")
    seen_ids.add(hid)

    on = hook["on"]
    anchor_id = on.get("anchor")
    if anchor_id not in anchors_by_id:
        raise HookError(f"hook '{hid}': 锚点 '{anchor_id}' 不在 hooks-anchors.json")
    anchor = anchors_by_id[anchor_id]
    anchor_chains = set(anchor.get("chains", []))
    if on["phase"] not in PHASES:
        raise HookError(f"hook '{hid}': phase 应为 {PHASES}")
    if on["phase"] not in anchor.get("phases", []):
        # 锚点只接了部分 phase（如 export-preset 结构性仅 after）：声明了未接线的
        # 组合会让钩子进 plan 却永无 dispatcher，静默空转——validate 期显式拒绝
        raise HookError(
            f"hook '{hid}': 锚点 '{anchor_id}' 不支持 phase '{on['phase']}'"
            f"（已接线：{anchor.get('phases')}）")

    chains = on.get("chains")
    # 空名单与省略同构（= 订阅锚点支持的全部链）：与 dispatch 期求交判据一致，
    # 否则 `"chains": []` 会让钩子谁也进不去，静默永不执行。
    if chains:
        bad = set(chains) - anchor_chains
        if bad:
            raise HookError(
                f"hook '{hid}': on.chains 含锚点 '{anchor_id}' 不支持的链 {sorted(bad)}；"
                f"该锚点仅支持 {sorted(anchor_chains)}")
        effective_chains = set(chains)
    else:
        effective_chains = anchor_chains

    caps = hook["capabilities"]
    outputs = hook["outputs"]
    for cap in caps:
        if cap in capability_table:
            continue
        m = SECRET_CAP_RE.match(cap)
        if m:
            name = m.group(1)
            # secret_names 按链分组：全集防拼错，本链组防「声明了却拿不到值」。
            all_names = set().union(*(set(v) for v in secret_names.values())) \
                if isinstance(secret_names, dict) else set(secret_names or [])
            if name not in all_names:
                raise HookError(
                    f"hook '{hid}': 能力 '{cap}' 的 secret 名不在 hooks-capabilities.json "
                    f"secret_names 白名单全集 {sorted(all_names)}")
            chain_names = (secret_names.get(chain, []) if isinstance(secret_names, dict)
                           else list(secret_names or []))
            if name not in chain_names:
                raise HookError(
                    f"hook '{hid}': 链 '{chain}' 不允许能力 '{cap}'——该链可用 secret "
                    f"{sorted(chain_names)}（调用方 job 级 env 未注入 '{name}'，声明了也拿不到值）")
            continue
        raise HookError(f"hook '{hid}': 未知能力 '{cap}'")
    producible = set()
    for cap in caps:
        producible |= set(capability_table.get(cap, ()))
    unknown = [o for o in outputs if o not in producible]
    if unknown:
        raise HookError(
            f"hook '{hid}': outputs {unknown} 不是 capabilities 能产出的子集"
            f"（可产出 {sorted(producible)}）")
    allowed_out = []
    for o in outputs:
        for target in OUTPUT_TARGETS.get(o, ()):
            if target not in allowed_out:
                allowed_out.append(target)

    # write_paths（直接写项目树）与 write:project_tree 能力双向互校：
    # 有能力无路径 = 声明了写入授权却没说写哪（不可执行）；有路径无能力 = 绕过注册表授权。
    write_paths = [str(x) for x in (hook.get("write_paths") or [])]
    has_tree_cap = TREE_CAPABILITY in caps
    if write_paths and not has_tree_cap:
        raise HookError(
            f"hook '{hid}': 声明了 write_paths 但缺少能力 'write:project_tree'")
    if has_tree_cap and not write_paths:
        raise HookError(
            f"hook '{hid}': 能力 'write:project_tree' 需要 write_paths 列出写入路径")
    for item in write_paths:
        validate_rel_path(item[:-3] if item.endswith("/**") else item,
                          f"hook '{hid}'.write_paths", root)
    # 敏感路径不禁止（钩子由项目自持、同仓评审），但要可见：这类声明会改仓库元数据
    # 或扩展清单本身，评审时容易被忽略，resolve 期打 notice 留痕。
    for item in write_paths:
        base = item[:-3] if item.endswith("/**") else item
        for sensitive in SENSITIVE_WRITE_PREFIXES:
            if base == sensitive or base.startswith(sensitive + "/"):
                print(f"::notice::hook '{hid}' 的 write_paths 触及仓库元数据 '{base}'"
                      f"（{sensitive}/），请确认这是有意为之")
                break

    run = validate_rel_path(hook["run"], f"hook '{hid}'.run", root)
    interp = interpreter_for(run)
    inputs_raw = (hook.get("with") or {}).get("inputs", []) or []
    inputs = [validate_rel_path(x, f"hook '{hid}'.with.inputs", root) for x in inputs_raw]
    closure = collect_closure(run, inputs, root)

    # write:project_tree 的坐标系原点必须被脚本显式引用：bundle 把脚本复制到 scripts/
    # 下，__file__ 的相对位置语义失效，只有 GODOT_HOOK_PROJECT_ROOT 能定位项目树。
    # 不引用 = 脚本无法得知写哪、或误以为自己仍在项目根——在 resolve 期就拒绝。
    if write_paths and not closure_references(closure, root, PROJECT_ROOT_ENV):
        raise HookError(
            f"hook '{hid}': 声明了 write:project_tree 但脚本闭包未引用 "
            f"{PROJECT_ROOT_ENV}（bundle 会改变脚本位置，__file__ 推算不可靠）")

    # 按 chain 分组：不属于本链的 hook 不进 plan（不是运行期过滤）
    if chain not in effective_chains:
        return None

    # on.presets 的语义只在导出链成立（dispatch 期按 current_preset / 本次导出名单求交）。
    # 订阅到不含 export 的链上，过滤条件在那条链里永远无从求值——钩子照跑但过滤失效，
    # 比直接报错更难发现。
    if on.get("presets") and "export" not in effective_chains:
        raise HookError(
            f"hook '{hid}': on.presets 仅对导出链有意义，但锚点 '{anchor_id}' 的生效链为 "
            f"{sorted(effective_chains)}（不含 export）；过滤永不求值，请改用 on.chains 限定")

    files = {rel: digest_file(root / rel) for rel in closure}
    hook_cfg = {
        "id": hid, "on": on, "run": run,
        "with": {"inputs": inputs},
        "capabilities": caps, "outputs": outputs,
        "write_paths": write_paths,
        "cache_impact": hook["cache_impact"],
        "on_error": hook["on_error"],
        "timeout_minutes": hook["timeout_minutes"],
    }
    hook_sha = digest_bytes(canonical(hook_cfg) + b"\0" + canonical(files))
    return {
        **hook_cfg,
        "interpreter": interp,
        "closure": closure,
        "out_targets": sorted(allowed_out,
                              key=lambda t: (t["channel"], t["kind"], t["path"])),
        "sha256": hook_sha,
    }


def build_plan(manifest, anchors, capability_table, secret_names, chain, root: Path) -> dict:
    schema = load_schema(str(DEFAULT_HOOKS_SCHEMA))
    apply_defaults(manifest, schema)
    errs = validate(manifest, schema)
    if errs:
        raise HookError("清单 schema 校验失败：" + "; ".join(errs))
    anchors_by_id = {a["id"]: a for a in anchors.get("anchors", [])}
    seen_ids = set()
    hooks = []
    for hook in manifest.get("hooks", []):
        entry = static_validate_hook(hook, anchors_by_id, capability_table, secret_names, chain, root, seen_ids)
        if entry is not None:
            hooks.append(entry)
    # 不含 run.id / attempt / 绝对路径 / 时间戳：同一清单+同一链恒产同一 plan
    return {
        "plan": "godot.ci/hook-plan",
        "chain": chain,
        "hooks": hooks,
    }


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------

def write_bundle(bundle_out, plan, project_root: Path, anchors, policy, chain) -> str:
    """写 plan.json / scripts/ 闭包 / files.json / hooks-anchors.json / policy.json / sha256。

    返回 bundle 摘要（plan + 闭包文件摘要的 sha256），hooks-sha 取其前 8 位。
    """
    bo = Path(bundle_out)
    bo.mkdir(parents=True, exist_ok=True)
    scripts = bo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)

    rels = sorted({r for h in plan["hooks"] for r in h["closure"]})
    files = {}
    for rel in rels:
        src = project_root / rel
        dst = scripts / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        files[rel] = digest_file(dst)

    plan_bytes = canonical(plan)
    anchors_bytes = canonical(anchors)
    policy_bytes = canonical({
        "policy": policy, "chain": chain,
        "decision": "declared-manifest" if policy == "declared" else "off",
    })
    (bo / "plan.json").write_bytes(plan_bytes)
    (bo / "hooks-anchors.json").write_bytes(anchors_bytes)
    (bo / "policy.json").write_bytes(policy_bytes)
    (bo / "files.json").write_bytes(canonical(files))
    # 摘要覆盖 plan / 脚本闭包 / 锚点表 / 策略，任何一份被改动消费侧都拒绝
    bundle_sha = digest_bytes(
        plan_bytes + b"\0" + canonical(files) + b"\0" + anchors_bytes + b"\0" + policy_bytes)
    (bo / "sha256").write_text(bundle_sha + "\n", encoding="utf-8")
    return bundle_sha


def cmd_validate(args) -> int:
    if args.policy not in POLICIES:
        raise HookError(f"--policy 应为 {POLICIES}，实际 '{args.policy}'")
    if args.chain not in CHAINS:
        raise HookError(f"validate --chain 应为 {CHAINS}，实际 '{args.chain}'")
    # off 档：不读清单，直接空转
    if args.policy == "off":
        write_github_output([("hooks-bundle", ""), ("hooks-sha", "")])
        print("hooks-policy=off：空转（未读取清单）")
        return 0

    manifest = read_json_optional(args.manifest, "ci-hooks.json")
    if manifest is None:
        write_github_output([("hooks-bundle", ""), ("hooks-sha", "")])
        print("无清单 / 空清单：空转，未生成 bundle")
        return 0
    if not isinstance(manifest, dict):
        raise HookError("ci-hooks.json 顶层必须是对象（含 hooks 数组）")
    if not manifest.get("hooks"):
        write_github_output([("hooks-bundle", ""), ("hooks-sha", "")])
        print("无清单 / 空清单：空转，未生成 bundle")
        return 0

    context = load_context(args.context)
    root = project_root_of(context)
    anchors = read_json(str(args.anchors or DEFAULT_ANCHORS), "hooks-anchors.json")
    validate_anchor_table(anchors)
    capabilities = read_json(str(args.capabilities or DEFAULT_CAPABILITIES), "hooks-capabilities.json")
    # secret_names 现为 {链: [名字]} 分组表（按链分组）；缺省空字典使
    # 任何 secret:* 声明都因「不在全集」被拒，不会静默放行。
    plan = build_plan(manifest, anchors, capabilities.get("capabilities", {}),
                      capabilities.get("secret_names", {}), args.chain, root)
    plan_bytes = canonical(plan)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(plan_bytes)  # 与 bundle/plan.json 字节完全一致

    if not plan["hooks"]:
        # 全部 hook 都不属于本链：plan 照写（确定性），但不产 bundle → 空转零成本
        write_github_output([("hooks-bundle", ""), ("hooks-sha", "")])
        print(f"chain={args.chain} 无适用 hook：plan 已写，未生成 bundle（空转）")
        return 0

    bundle_sha = write_bundle(args.bundle_out, plan, root, anchors, args.policy, args.chain)
    # 产物前缀的取值源唯一：context.artifacts.prefix（build_context 由
    # artifacts.prefix / ARTIFACT_PREFIX / REPO_SAFE 归一化后写入）。缺失即数据不全，
    # 在这里拼兜底会让 bundle 名与其余产物命名分家，故 fail-fast。
    prefix = str((context.get("artifacts") or {}).get("prefix") or "").strip()
    if not prefix:
        raise HookError("context.json 缺 artifacts.prefix：bundle 产物名的前缀没有来源"
                        "（context 应由 godot_context.py 生成）")
    artifact = f"{prefix}_{args.chain}_hooks_{bundle_sha[:8]}"
    write_github_output([("hooks-bundle", artifact), ("hooks-sha", bundle_sha[:8])])
    print(f"plan: {out_path}（{len(plan['hooks'])} 个 hook，chain={args.chain}）")
    print(f"bundle: {args.bundle_out}  artifact={artifact}  sha={bundle_sha[:8]}")
    return 0


def cmd_bundle(args) -> int:
    plan = load_json_file(args.plan, "plan.json")
    if plan.get("plan") != "godot.ci/hook-plan":
        raise HookError("--plan 不是 run_hooks validate 产出的 plan.json")
    root = Path(args.root)
    anchors = read_json(str(args.anchors or DEFAULT_ANCHORS), "hooks-anchors.json")
    validate_anchor_table(anchors)
    chain = args.chain or plan.get("chain", "build")
    sha = write_bundle(args.bundle_out, plan, root, anchors, args.policy, chain)
    print(f"bundle: {args.bundle_out}  sha={sha[:8]}")
    return 0


# ---------------------------------------------------------------------------
# dispatch：声明式过滤 + 快照 + 受限子进程 + post-diff + 回滚
# ---------------------------------------------------------------------------

def hook_matches(on, context) -> bool:
    """声明式过滤求交；省略或空名单 = 全部。判定只在 dispatch 期做。

    四个过滤键一律按「名单非空才过滤」判定：`[]` 与省略等价（docs/hooks.md 声明式
    过滤）。用 `"key" in on` 判定会让空数组变成「永不匹配」，写 `"platform": []`
    的钩子静默永不执行——文档承诺与实现相反，且与 presets 的既有语义不一致。

    context.platform / target 为 null（多平台批的 resolve 期与 release 链）= 不过滤，
    与 on.platform 省略同构——否则 release 链「声明了 platform 过滤」的钩子会静默漏跑。
    """
    ctx_platform = context.get("platform")
    if on.get("platform") and ctx_platform is not None and ctx_platform not in on["platform"]:
        return False
    ctx_target = context.get("target")
    if on.get("target") and ctx_target is not None and ctx_target not in on["target"]:
        return False
    if on.get("chains") and context.get("chain") not in on["chains"]:
        return False
    if on.get("presets"):
        export = context.get("export", {}) or {}
        current = export.get("current_preset", "")
        if current:
            # 逐预设锚点（export-preset-each）：按当前预设判定
            if current not in on["presets"]:
                return False
        else:
            # 整批语义：context.export.presets 是本次实际导出名单（resolve 注入），
            # 名单为空 = 项目全部预设（resolve 未限定），此时不预设过滤
            actual = export.get("presets") or []
            if actual and not any(p in on["presets"] for p in actual):
                return False
    return True


def _hash_file(path: Path, chunk: int = 1 << 20) -> str:
    """流式计算文件内容摘要（大产物不进内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


class TreeSnapshot:
    """目录树快照：内存只留元数据，文件内容落盘到备份目录。

    通道根（`dist/` / `directives/`）会装上钩子产出的大产物，把全部内容读进内存
    会让内存峰值随产物大小翻倍（docs/hooks.md「运行时树可达上百 MB」）。这里改为：

    - 内存态：`rel -> (kind, detail, digest)`，与树的文件数成正比、与文件大小无关；
      file 的 detail 是字节数，link 的 detail 是链接目标串；
    - 内容态：backup_dir 非空时在备份目录留一份副本（回滚从这里拷回）。只做差异
      比对时传 backup_dir=None：显式声明「不需要还原能力」，不产生多余副本。

    变更检测按元组全等判定（size 先行体现差异，digest 兜住同尺寸改写）；回滚语义
    与内容快照完全一致——删除新增条目、从备份还原被改/被删的预存文件。
    """

    def __init__(self, root: Path, backup_dir, label: str):
        self.root = root
        self.backup_dir = Path(backup_dir) if backup_dir is not None else None
        self.label = label
        self.entries = {}
        self._capture()

    def _capture(self) -> None:
        if self.backup_dir is not None:
            # 每次快照从空备份开始：残留旧副本会在「同尺寸但内容不同」时被误当成
            # 当前内容（copy 只在尺寸不同时触发），回滚就会还原成上一轮的文件。
            if self.backup_dir.exists():
                shutil.rmtree(self.backup_dir)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        if not self.root.exists():
            return
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            try:
                if p.is_symlink():
                    # 符号链接只记目标串（内容即链接本身），回滚时重建
                    self.entries[rel] = ("link", os.readlink(str(p)), None)
                elif p.is_file():
                    size = p.stat().st_size
                    self.entries[rel] = ("file", size, _hash_file(p))
                    if self.backup_dir is not None:
                        dst = self.backup_dir / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if not dst.is_file() or dst.stat().st_size != size:
                            shutil.copyfile(p, dst)
                elif p.is_dir():
                    self.entries[rel] = ("dir", 0, None)
            except OSError as e:
                raise HookError(f"快照失败（{self.label}）：无法读取 '{rel}'（{e}）")

    def restore(self) -> None:
        """把目录树恢复为快照状态：删除新增、从备份还原被改/被删的预存文件。"""
        if self.backup_dir is None:
            raise HookError(f"快照（{self.label}）未建内容备份，不能回滚")
        if self.root.exists():
            for p in sorted(self.root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                rel = p.relative_to(self.root).as_posix()
                if rel in self.entries:
                    continue
                try:
                    if p.is_symlink() or p.is_file():
                        p.unlink()
                    elif p.is_dir():
                        p.rmdir()
                except OSError:
                    pass
        self.root.mkdir(parents=True, exist_ok=True)
        for rel, (kind, detail, digest) in self.entries.items():
            dst = self.root / rel
            if kind == "dir":
                dst.mkdir(parents=True, exist_ok=True)
            elif kind == "link":
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.is_symlink() or dst.exists():
                    dst.unlink()
                os.symlink(detail, str(dst))
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.is_symlink():
                    dst.unlink()
                if (not dst.is_file() or dst.stat().st_size != detail
                        or _hash_file(dst) != digest):
                    shutil.copyfile(self.backup_dir / rel, dst)


def changed_rels(before: dict, after: dict) -> list:
    """元数据表（rel -> 元组）的差异集。"""
    return sorted(rel for rel in set(before) | set(after)
                  if before.get(rel) != after.get(rel))


def snapshot_changes(before: TreeSnapshot, after: TreeSnapshot) -> list:
    """两次目录树快照的差异集（rel 列表）。"""
    return changed_rels(before.entries, after.entries)


def classify_write(rel: str, allowed_out: list) -> str:
    """返回 'ok' / 'outside' / 'undeclared'。rel 相对 hook_root。

    allowed_out 是 {"channel","path","kind"} 声明数组。两个通道根（dist / directives）
    各有许可边界：落在任一声明内即 ok，落在两通道之外为 outside，落在通道内但
    无对应声明为 undeclared。
    """
    channel, sep, inner = rel.partition("/")
    if channel not in (CHANNEL_DIST, CHANNEL_DIRECTIVES) or not sep:
        return "outside"
    if not inner:
        return "ok"  # 通道根目录本身
    matched_channel = False
    for target in allowed_out:
        if target["channel"] != channel:
            continue
        matched_channel = True
        path = target["path"]
        kind = target["kind"]
        if kind == "glob":
            parent = PurePosixPath(path).parent.as_posix()
            if fnmatch.fnmatch(inner, path) or (parent != "." and inner == parent):
                return "ok"
        elif kind == "dir":
            if inner == path or inner.startswith(path + "/"):
                return "ok"
        elif inner == path:
            return "ok"
    # 该通道有声明但路径不匹配，或该通道根本无声明 → 一律未声明
    return "undeclared"


def list_project_tree(root: Path, skip: list) -> dict:
    """遍历项目根全树，返回 rel -> ("file"|"link"|"dir", size)，跳过 skip 中的路径。

    只取 stat 元数据（不读内容），用于发现 write_paths 之外的越界写入。
    skip 是声明写面的顶层名（那些路径的变化由 list_write_paths 单独判定）。
    """
    skip_tops = {item[:-3] if item.endswith("/**") else item for item in skip}
    out = {}

    def walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for p in entries:
            rel = p.relative_to(root).as_posix()
            if rel in skip_tops or any(rel.startswith(t + "/") for t in skip_tops):
                continue
            try:
                if p.is_symlink():
                    out[rel] = ("link", 0)
                elif p.is_dir():
                    out[rel] = ("dir", 0)
                    walk(p)
                elif p.is_file():
                    out[rel] = ("file", p.stat().st_size)
            except OSError:
                continue

    walk(root)
    return out


def remove_outside_writes(root: Path, tree_before: dict, declared: list) -> list:
    """删除项目树中声明写面之外的新增条目，返回被删除的路径列表。

    越界写入的原始内容无从还原（未备份），只能删除新增；被改写的预存文件无法恢复，
    故调用方在报错信息里列出路径供人工排查。
    """
    removed = []
    after = list_project_tree(root, declared)
    for rel in sorted(set(after) - set(tree_before), key=lambda r: r.count("/"), reverse=True):
        p = root / rel
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
                removed.append(rel)
            elif p.is_dir():
                p.rmdir()
        except OSError:
            pass
    return removed


def write_path_matches(rel: str, declared: list) -> bool:
    """rel（项目根相对 POSIX 路径）是否落在某条声明写面内。

    声明项语义：`dir/**` = 该目录整棵子树；`path` = 该文件（或目录本身）。
    """
    for item in declared:
        if item.endswith("/**"):
            prefix = item[:-3]
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
        elif rel == item or rel.startswith(item + "/"):
            return True
    return False


def list_write_paths(root: Path, declared: list) -> dict:
    """列出声明写面内当前存在的条目：rel -> ("file"|"link"|"dir", size)。

    只做元数据遍历，不读内容——运行时树可达上百 MB，内容快照会撑爆内存。
    """
    out = {}
    for item in declared:
        base_rel = item[:-3] if item.endswith("/**") else item
        base = root / base_rel
        if base.is_symlink() or base.is_file():
            entry = _stat_entry(base)
            if entry:
                out[base_rel] = entry
        elif base.is_dir():
            for p in sorted(base.rglob("*")):
                entry = _stat_entry(p)
                if entry:
                    out[p.relative_to(root).as_posix()] = entry
    return out


def _stat_entry(path: Path):
    try:
        if path.is_symlink():
            return ("link", len(os.readlink(str(path))))
        if path.is_file():
            return ("file", path.stat().st_size)
        if path.is_dir():
            return ("dir", 0)
    except OSError:
        return None
    return None


def backup_write_paths(root: Path, before: dict, backup_root: Path) -> None:
    """把写面内**预存在**的普通文件复制到备份目录；新增文件无前态，回滚时删除即可。

    before 为 list_write_paths 的产物。备份根随 RUNNER_TEMP 走（不落在工作区，
    故不污染 post-diff；也避免与项目自身文件混淆）。
    """
    if backup_root.exists():
        shutil.rmtree(backup_root)
    for rel, (kind, _size) in before.items():
        if kind != "file":
            continue
        src = root / rel
        dst = backup_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def restore_write_paths(root: Path, before: dict, backup_root: Path, declared: list) -> None:
    """把声明写面恢复到钩子执行前：删除新增条目，从备份还原被改/被删的预存文件。

    declared 必须是钩子的原始 write_paths 声明（而非从 before 反推）——若按 before
    的顶层目录放宽成 `<顶层>/**`，声明单文件（如 build/profile.py）时会把该目录下
    所有钩子未触碰的预存文件当成「新增」删掉，回滚反而破坏项目树。
    """
    current = list_write_paths(root, declared)
    # 1) 删除新增条目（当前有、before 无），目录由深到浅
    for rel in sorted(current, key=lambda r: r.count("/"), reverse=True):
        if rel in before:
            continue
        p = root / rel
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        except OSError:
            pass
    # 2) 还原预存文件（被改或被删）
    for rel, (kind, _size) in before.items():
        dst = root / rel
        if kind == "dir":
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if kind == "link":
            continue  # 符号链接不参与内容变化，跳过
        src = backup_root / rel
        if not src.is_file():
            continue
        if dst.is_file() and digest_file(dst) == digest_file(src):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        shutil.copy2(src, dst)


def write_path_digests(root: Path, before: dict, declared: list) -> dict:
    """写面内文件的 sha256 摘要（仅 build 链用，进缓存键）；已删除文件记 'deleted'。"""
    after = list_write_paths(root, declared)
    digests = {}
    for rel in sorted(set(before) | set(after)):
        kind = (after.get(rel) or before.get(rel))[0]
        if kind != "file":
            continue
        p = root / rel
        if p.is_file() and rel in after:
            digests[rel] = digest_file(p)
        else:
            digests[rel] = "deleted"
    return digests


def build_hook_env(hook, context, hook_root: Path, bundle: Path, anchor, phase) -> dict:
    """受限 env：不继承父 job 全环境，只注入白名单 + 声明 secret + GODOT_*。"""
    base_allow = (
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
        "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE", "PYTHONIOENCODING",
    )
    env = {k: os.environ[k] for k in base_allow if k in os.environ}

    for k, v in flatten_context(context).items():
        env[env_key(k)] = v
    env["GODOT_CONTEXT_FILE"] = str(hook_root / "context.json")
    # 三个写面根：产物通道 DIST、指令通道 DIRECTIVES、项目树通道 PROJECT_ROOT。
    # bundle 会把脚本复制到 scripts/ 下，__file__ 的相对位置语义随之失效，故各根的
    # 绝对原点必须由执行器显式给出（validate 期强制 write:project_tree 钩子引用它）。
    # 解析为绝对路径：子进程 cwd 是 hook_root，相对原点会被按错误的基准解释。
    raw_project_root = (context.get("project") or {}).get("root") or ""
    project_root = str(Path(raw_project_root).resolve()) if raw_project_root else ""
    env.update({
        "GODOT_HOOK_ROOT": str(hook_root),
        "GODOT_HOOK_DIST": str(hook_root / CHANNEL_DIST),
        "GODOT_HOOK_DIRECTIVES": str(hook_root / CHANNEL_DIRECTIVES),
        "GODOT_HOOK_PROJECT_ROOT": project_root,
        "GODOT_HOOK_ID": hook["id"],
        "GODOT_HOOK_ANCHOR": anchor,
        "GODOT_HOOK_PHASE": phase,
        "GODOT_HOOK_PLATFORM": str(context.get("platform", "")),
        "GODOT_HOOK_TARGET": str(context.get("target", "")),
        "GODOT_HOOK_CHAIN": str(context.get("chain", "")),
        "GODOT_HOOKS_DIR": str(bundle / "scripts"),
    })
    # secret:<NAME>：从执行器自身 env 挑出声明名注入（值由调用方 job 级 env 提供；
    # NAME 白名单与适用链已在 validate 期校验，这里只按声明透传，不落日志）。
    # 声明了 secret 却取不到值 = 接线缺 env，必须响亮失败——
    # 旧实现静默跳过，钩子拿到空值却以为成功，失败原因离根因极远。
    for cap in hook["capabilities"]:
        m = SECRET_CAP_RE.match(cap)
        if not m:
            continue
        name = m.group(1)
        if name not in os.environ:
            raise HookError(
                f"hook '{hook['id']}' 声明了能力 '{cap}'，但执行器 env 中没有 '{name}'"
                f"（调用方 job 级 env 未注入该 secret，声明的凭证取不到值）")
        env[name] = os.environ[name]
    return env


def tail_of(path: Path, limit=800) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].replace("\r", " ").strip()


def kill_process(proc) -> None:
    """超时终止钩子进程树。

    posix 用进程组一次性杀干净；Windows 没有 killpg，单靠 proc.kill() 只杀直接
    子进程，钩子拉起的 pip/git 等子孙进程会残留、继续持文件锁写快照面。故 Windows
    用 taskkill /T /F /PID 连子孙一起终止，taskkill 不可用时回退 proc.kill()。
    """
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            done = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if done.returncode != 0:
                proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def parse_scons_flags(path: Path) -> dict:
    """校验 directives/scons_flags 的 {flags:[{key,value}]} 输出格式。"""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HookError(f"directives/scons_flags 不是合法 JSON：{e}")
    if not isinstance(obj, dict) or not isinstance(obj.get("flags"), list):
        raise HookError("directives/scons_flags 必须形如 {\"flags\":[{\"key\":...,\"value\":...}]}")
    for item in obj["flags"]:
        if (not isinstance(item, dict) or "key" not in item or "value" not in item
                or not isinstance(item["key"], str)):
            raise HookError("directives/scons_flags 的每项须含字符串 key 与 value")
    return obj


def merge_scons_flags(base: str, flags_obj: dict) -> str:
    """把 {flags:[...]} 合并进 base；同名键后者生效；输出可经 shlex.split 还原。"""
    try:
        tokens = shlex.split(base or "")
    except ValueError:
        tokens = (base or "").split()
    pairs = {}
    bare = []
    for token in tokens:
        if "=" in token:
            k, _, v = token.partition("=")
            pairs[k] = v
        else:
            bare.append(token)
    for item in flags_obj.get("flags", []):
        pairs[str(item["key"])] = str(item["value"])
    return " ".join([*bare, *(f"{k}={shlex.quote(v)}" for k, v in pairs.items())])


def source_patch_digest(outputs: dict) -> str:
    """source_patch 补丁文件**自身内容**的摘要（docs/hooks.md 分层缓存键的 source_patch_digests）。

    取的是补丁文件本身的 sha256，不是补丁应用后被改文件的摘要——后者会随
    应用顺序变化，且无法区分「同一补丁」与「不同补丁改出同样结果」。
    """
    rec = (outputs or {}).get("source_patch") or {}
    src = rec.get("path")
    if not src:
        return ""
    base = Path(src)
    if base.is_file():
        files, names = [base], [base.name]
    elif base.is_dir():
        files = sorted(f for f in base.rglob("*") if f.is_file())
        names = [f.relative_to(base).as_posix() for f in files]
    else:
        return ""
    if not files:
        return ""
    parts = []
    for name, f in zip(names, files):
        parts.append(name)
        parts.append(digest_file(f))
    return digest_bytes("\0".join(parts).encode("utf-8"))


def collect_outputs(hook, dist_dir: Path, result_outputs: dict) -> dict:
    """收集各通道根下已声明的输出，返回 {output: sha256}。"""
    digests = {}
    for name in hook["outputs"]:
        for target_spec in OUTPUT_TARGETS.get(name, ()):
            prefix = target_spec["path"]
            kind = target_spec["kind"]
            channel_dir = dist_dir.parent / target_spec["channel"]
            if kind == "glob":
                pattern = prefix
                matches = sorted(p for p in channel_dir.glob(pattern) if p.is_file())
                if not matches:
                    continue
                # glob 的落点目录 = 模式所在目录（取第一个通配符之前的目录部分），
                # 不能按 split('*')[0] 截断——patches/source.*.patch 会截成 patches/source.
                target = channel_dir / PurePosixPath(pattern).parent
                if any(m.is_symlink() for m in matches):
                    raise HookError(f"输出 '{name}' 含符号链接，拒绝收集")
                parts = [digest_file(f) for f in matches]
                digests[name] = digest_bytes("".join(parts).encode("utf-8"))
                result_outputs[name] = {
                    "path": target.as_posix(), "sha256": digests[name],
                    "files": len(parts),
                }
                continue
            target = channel_dir / prefix
            if target.is_symlink():
                raise HookError(f"输出 '{name}' 的 '{prefix}' 是符号链接，拒绝收集")
            if target.is_file():
                digests[name] = digest_file(target)
                result_outputs[name] = {
                    "path": target.as_posix(),
                    "sha256": digests[name],
                }
            elif target.is_dir():
                files = sorted(f for f in target.rglob("*") if f.is_file())
                if any(f.is_symlink() for f in files):
                    raise HookError(f"输出 '{name}' 的 '{prefix}' 含符号链接，拒绝收集")
                parts = [digest_file(f) for f in files]
                digests[name] = digest_bytes("".join(parts).encode("utf-8"))
                result_outputs[name] = {
                    "path": target.as_posix(), "sha256": digests[name],
                    "files": len(parts),
                }
    return digests


def run_one_hook(hook, context, bundle: Path, hook_root: Path, logs: Path,
                 anchor, phase) -> tuple:
    """执行单个 hook；返回 (ok, record)。异常以 HookError 抛出，由调用方回滚。"""
    run_rel = hook["run"]
    script = bundle / "scripts" / run_rel
    validate_rel_path(run_rel, f"hook '{hook['id']}'.run（消费侧重验）",
                      bundle / "scripts")
    for rel in hook["closure"]:
        validate_rel_path(rel, "依赖闭包（消费侧重验）", bundle / "scripts")
    if not script.is_file():
        raise HookError(f"脚本闭包缺失：{run_rel}")

    directives_dir = hook_root / CHANNEL_DIRECTIVES
    dist_dir = hook_root / CHANNEL_DIST
    for channel_dir in (dist_dir, directives_dir):
        channel_dir.mkdir(parents=True, exist_ok=True)
    env = build_hook_env(hook, context, hook_root, bundle, anchor, phase)
    logs.mkdir(parents=True, exist_ok=True)
    stdout_path = logs / f"{hook['id']}.out"
    stderr_path = logs / f"{hook['id']}.err"
    cmd = [resolve_interpreter(hook["interpreter"]), str(script)]
    popen_kw = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    with open(stdout_path, "wb") as fo, open(stderr_path, "wb") as fe:
        proc = subprocess.Popen(cmd, cwd=str(hook_root), env=env,
                                stdout=fo, stderr=fe, **popen_kw)
        try:
            proc.wait(timeout=hook["timeout_minutes"] * 60)
        except subprocess.TimeoutExpired:
            kill_process(proc)
            proc.wait()
            raise HookError(
                f"hook '{hook['id']}' 超时（{hook['timeout_minutes']} 分钟）")
    rc = proc.returncode
    if rc != 0:
        raise HookError(
            f"hook '{hook['id']}' 脚本 '{run_rel}' 退出码 {rc}；"
            f"stdout tail: {tail_of(stdout_path)}；stderr tail: {tail_of(stderr_path)}")

    # 输出 schema 校验（scons_flags 属指令通道）
    if "scons_flags" in hook["outputs"]:
        f = directives_dir / "scons_flags"
        if not f.is_file():
            raise HookError(
                f"hook '{hook['id']}' 声明 outputs=scons_flags 但未写 "
                f"{CHANNEL_DIRECTIVES}/scons_flags")
        parse_scons_flags(f)

    record = {
        "id": hook["id"], "ok": True, "script": run_rel,
        "exit": rc, "stdout_tail": tail_of(stdout_path),
        "cache_impact": hook["cache_impact"],
        "outputs": {},
    }
    return True, record


def cmd_dispatch(args) -> int:
    if args.phase not in PHASES:
        raise HookError(f"--phase 应为 {PHASES}，实际 '{args.phase}'")
    # 全部路径入口处解析为绝对路径：子进程 cwd 会切到 hook_root，相对路径在切换后
    # 指向别处（bundle/scripts/... 曾因此拼成 <hook_root>/<bundle>/scripts/...）。
    bundle = Path(args.bundle).resolve()
    plan_path = Path(args.plan).resolve() if args.plan else bundle / "plan.json"
    # 结果文件也须绝对化：它在多个 job 间传递，且会被下游 apply_* 脚本按 cwd 解析
    result_path = Path(args.result).resolve()
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    if plan.get("plan") != "godot.ci/hook-plan":
        raise HookError("plan.json 不是 run_hooks validate 的输出")
    files = load_json_file(bundle / "files.json", "bundle/files.json")
    anchors_bytes = (bundle / "hooks-anchors.json").read_bytes()
    policy_bytes = (bundle / "policy.json").read_bytes()
    expected = (bundle / "sha256").read_text(encoding="utf-8").strip()
    actual = digest_bytes(
        plan_bytes + b"\0" + canonical(files) + b"\0" + anchors_bytes + b"\0" + policy_bytes)
    if expected != actual:
        raise HookError(
            "bundle 摘要不匹配（消费侧重验失败）：plan.json / files.json / "
            "hooks-anchors.json / policy.json 有改动")
    scripts_root = bundle / "scripts"
    for rel, dig in files.items():
        validate_rel_path(rel, "bundle 文件（消费侧重验）", scripts_root)
        p = scripts_root / rel
        if not p.is_file() or digest_file(p) != dig:
            raise HookError(f"bundle 脚本摘要不匹配：{rel}")
    anchors = load_json_file(bundle / "hooks-anchors.json", "bundle/hooks-anchors.json")
    if args.anchor not in {a["id"] for a in anchors.get("anchors", [])}:
        raise HookError(f"锚点 '{args.anchor}' 不在 bundle/hooks-anchors.json")

    context = load_context(args.context)
    if args.chain and context.get("chain") != args.chain:
        raise HookError(
            f"--chain '{args.chain}' 与 context.json 的 chain '{context.get('chain')}' 不一致")
    if args.current_preset:
        # 逐预设锚点：当前预设名注入 context（钩子经 GODOT_CONTEXT_EXPORT_CURRENT_PRESET 读取）
        context.setdefault("export", {})["current_preset"] = args.current_preset
    plan_context = context
    selected = [h for h in plan["hooks"]
                if h["on"]["anchor"] == args.anchor
                and h["on"]["phase"] == args.phase
                and hook_matches(h["on"], plan_context)]

    hook_root = Path(os.environ.get("GODOT_HOOK_ROOT",
                                    str(bundle / "hook_root"))).resolve()
    hook_root.mkdir(parents=True, exist_ok=True)
    (hook_root / "context.json").write_bytes(canonical(context))
    # 每次 dispatch 从空通道根开始：避免上一锚点残留的声明输出被误收集
    for channel in (CHANNEL_DIST, CHANNEL_DIRECTIVES):
        channel_dir = hook_root / channel
        if channel_dir.exists():
            shutil.rmtree(channel_dir)
        channel_dir.mkdir(parents=True, exist_ok=True)
    logs = bundle / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    # 多锚点 dispatch 写同一份 hooks-result.json：读既有冻结键作为初值并累积，
    # 否则后调用的锚点会把前面 write:scons_flags / post_build 结果覆盖掉。
    prior = {}
    if result_path.is_file():
        try:
            prior = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # 上一锚点的结果读不出来就按空累积：后面的 write:scons_flags /
            # post_build 会按「首次」重算，缓存键与未损坏时不同。必须留痕，
            # 否则表现为莫名其妙的缓存不命中或状态回洗，无从定位。
            print(f"::warning::上一锚点的 {result_path} 无法读取（{e}），"
                  f"本次按空结果累积；缓存键可能与上一锚点不一致", file=sys.stderr)
            prior = {}
    executed, warnings, errors = [], [], []
    all_outputs = dict(prior.get("outputs") or {})  # 跨锚点累积已收集的输出（按输出名索引）
    hook_outputs = dict(prior.get("hooks_outputs") or {})  # 按 hook id 索引的明细
    hook_digests = {"config": [], "source": []}
    for d in prior.get("hook_output_digests", {}).get("config", []):
        hook_digests["config"].append(d)
    for d in prior.get("hook_output_digests", {}).get("source", []):
        hook_digests["source"].append(d)
    # 补丁内容摘要跨锚点累积：补丁在哪个锚点产出，摘要都进 source-hash
    patch_digests = list(prior.get("source_patch_digests") or [])
    merged_flags = prior.get("merged_flags") or args.scons_flags or context.get("cache", {}).get("scons_flags_base", "") or ""
    failed = prior.get("post_build") == "fail" or prior.get("post_export") == "fail"

    # 项目根与备份根都解析为绝对路径：前者要注入钩子进程（子进程 cwd 已切换），
    # 后者用于回滚，任何相对基准都会在 cwd 变化后指向别处。
    project_root = Path(context.get("project", {}).get("root") or ".").resolve()
    backup_base = (Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
                   / "godot-hook-backup").resolve()
    # 通道根与脚本闭包的内容备份另起一棵子树：write_paths 的备份目录由
    # backup_write_paths 整体重建（先 rmtree），两者混放会被连带清掉。
    channels_backup_base = backup_base / "channels"

    for hook in selected:  # 数组书写顺序即执行顺序
        # 快照只把元数据留在内存，文件内容落盘（通道根会装上大产物，不读进内存）
        snap_base = channels_backup_base / f"{args.anchor}-{args.phase}" / hook["id"]
        hook_snapshot = TreeSnapshot(hook_root, snap_base / "hook_root", "通道根")
        scripts_snapshot = TreeSnapshot(scripts_root, snap_base / "scripts", "脚本闭包")
        # write_paths：只记录元数据 + 预存文件磁盘备份（运行时树可达上百 MB，不读内容）
        declared_writes = hook.get("write_paths") or []
        writes_before = list_write_paths(project_root, declared_writes) if declared_writes else {}
        # 越界检测对所有钩子生效：未声明 write_paths 的钩子不得改动项目树任何文件
        tree_before = list_project_tree(project_root, declared_writes)
        hook_backup = backup_base / f"{args.anchor}-{args.phase}" / hook["id"]
        if declared_writes:
            backup_write_paths(project_root, writes_before, hook_backup)
        try:
            ok, record = run_one_hook(hook, context, bundle, hook_root, logs,
                                      args.anchor, args.phase)
            # post-diff：脚本闭包不得被改；两个通道根内除声明目标外不得有写入；
            # 项目树写入必须落在 write_paths 内（全树比对，未声明的改动一律拒绝）
            script_changes = snapshot_changes(
                scripts_snapshot, TreeSnapshot(scripts_root, None, "脚本闭包"))
            if script_changes:
                raise HookError(f"未声明写入：脚本闭包被修改 {script_changes[:5]}")
            for rel in snapshot_changes(
                    hook_snapshot, TreeSnapshot(hook_root, None, "通道根")):
                verdict = classify_write(rel, hook["out_targets"])
                if verdict == "outside":
                    raise HookError(f"未声明写入：通道根之外出现改动 '{rel}'")
                if verdict == "undeclared":
                    raise HookError(
                        f"未声明写入：'{rel}' 不在该通道声明 outputs 的目标 "
                        f"{hook['out_targets']} 内")
            if declared_writes:
                writes_after = list_write_paths(project_root, declared_writes)
                for rel in changed_rels(writes_before, writes_after):
                    if not write_path_matches(rel, declared_writes):
                        raise HookError(
                            f"未声明写入：'{rel}' 不在 write_paths {declared_writes} 内")
                # 越界检测：项目根全树中声明写面之外的改动一律拒绝
                tree_after = list_project_tree(project_root, declared_writes)
                outside = changed_rels(tree_before, tree_after)
                if outside:
                    raise HookError(
                        f"未声明写入：项目树 '{outside[:5]}' 不在 write_paths 内")

            digests = collect_outputs(hook, hook_root / CHANNEL_DIST, record["outputs"])
            # outputs 以**输出名**为键（apply_hook_outputs.py / apply_hook_patches.py
            # 按输出名取 path），每个 hook 的明细另存 hooks_outputs。
            # 同名输出的合并语义：**后执行者覆盖前者**（数组书写顺序即执行顺序，
            # 与 shell 里「后者覆盖前者」一致），明细仍按 hook id 各自留存可追溯。
            all_outputs.update(record["outputs"])
            hook_outputs[hook["id"]] = record["outputs"]
            impact = hook["cache_impact"]
            if impact in hook_digests and digests:
                hook_digests[impact].append({
                    "id": hook["id"],
                    "sha256": digest_bytes(canonical(digests)),
                })
            # build 链：write_paths 的写入也是构建输入（如 profile.py），必须并入缓存摘要，
            # 否则命中旧键产出与源码不符的包。export/release 链没有缓存键，不计算。
            if declared_writes and impact in hook_digests and context.get("chain") == "build":
                file_digests = write_path_digests(project_root, writes_before, declared_writes)
                if file_digests:
                    hook_digests[impact].append({
                        "id": hook["id"],
                        "sha256": digest_bytes(canonical(file_digests)),
                    })
            # 补丁内容摘要并入 source 层（docs/hooks.md 分层缓存键 source_patch_digests）：
            # 取补丁文件自身的 sha256，不是补丁应用后被改文件的摘要。
            patch_digest = source_patch_digest(record["outputs"])
            if patch_digest:
                hook_digests["source"].append({
                    "id": hook["id"], "sha256": patch_digest,
                })
                patch_digests.append(patch_digest)
            if "scons_flags" in hook["outputs"]:
                merged_flags = merge_scons_flags(
                    merged_flags, parse_scons_flags(hook_root / CHANNEL_DIRECTIVES / "scons_flags"))
            executed.append(record)
        except HookError as e:
            hook_snapshot.restore()
            scripts_snapshot.restore()
            # write_paths：删除新增条目、从磁盘备份还原预存文件
            if declared_writes:
                restore_write_paths(project_root, writes_before, hook_backup,
                                    declared_writes)
            # 越界写入：声明写面之外的新增条目一律删除（预存文件被改写无法还原，报错已列出）
            remove_outside_writes(project_root, tree_before, declared_writes)
            msg = (f"[{args.anchor}:{args.phase}] {e}")
            print_error(msg)
            if hook["on_error"] == "warn":
                # 记录形状与 errors 对齐（id / error），另加 anchor+phase+script
                # 便于在 summary 之外按锚点定位；warned 标记与 executed 条目同名同义。
                warnings.append({
                    "id": hook["id"], "error": str(e),
                    "anchor": args.anchor, "phase": args.phase,
                    "script": hook.get("run", ""),
                })
                # warn 必须进 job summary（docs/hooks.md 承诺），否则失败只留在结果文件里
                summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
                if summary:
                    with open(summary, "a", encoding="utf-8") as sf:
                        sf.write(f"- ⚠️ hook `{hook['id']}` 失败已回滚（warn）：{str(e)}\n")
                executed.append({"id": hook["id"], "ok": False,
                                 "script": hook["run"], "warned": True})
                continue
            errors.append({"id": hook["id"], "error": str(e),
                           "anchor": args.anchor, "phase": args.phase})
            failed = True
            break

    hooks_sha = expected[:8]
    # 冻结接口：hooks-result.json 是唯一传值通道。无匹配 hook 时也必须写兜底值，
    # 供调用方（build-pipeline）的归一化 step 读文件兜底。
    config_digests = [d["sha256"] for d in hook_digests["config"]]
    source_digests = [d["sha256"] for d in hook_digests["source"]]
    source_active = bool(source_digests)
    # docs/hooks.md 分层缓存键：source-hash = sha256(engine-sha, source_patch_digests,
    #                                          hook_output_digests[source])[:8]
    # source_patch_digests 取补丁文件**自身**的 sha256（source_patch_digests），
    # 与 source 层钩子输出摘要一并进入摘要；无 source 层钩子时为空串
    # （restore-keys 保持原前缀，不做条件化收紧）。
    source_hash = (digest_bytes(canonical({
        "engine_sha": context.get("engine", {}).get("sha", ""),
        "source_patch_digests": patch_digests,
        "digests": source_digests,
    }))[:8] if source_active else prior.get("source_hash", ""))
    status = "fail" if failed else "ok"
    # post_build / post_export 是两个独立状态，各有自己的消费者：前者门控保存缓存
    # （build 链），后者门控上传与发布（export 链）。用同一个失败标志同时写两者，
    # 会让 build 链的失败把 post_export 也标成 fail（反之亦然）——本次从未在
    # export 链上执行过的状态被写坏，下游按另一个链的门控会误判。
    current_chain = context.get("chain", "")

    def sticky(key: str, chain: str) -> str:
        if current_chain != chain:
            return prior.get(key, "")  # 不属本链：原样沿用上一锚点的值，不发明状态
        return "fail" if (failed or prior.get(key) == "fail") else status
    result = {
        "ok": not failed,
        "chain": context.get("chain", ""),
        "anchor": args.anchor,
        "phase": args.phase,
        "hooks_sha": hooks_sha,
        "plan_sha256": expected,
        "executed": executed,
        "warnings": warnings,
        "errors": errors,
        "outputs": all_outputs,
        "hooks_outputs": hook_outputs,
        "hook_output_digests": hook_digests,
        "source_patch_digests": patch_digests,
        # --- 冻结键（调用方读文件兜底，文件不存在=空转全部兜底）---
        "merged_flags": merged_flags,
        "config_digests": ",".join(config_digests),
        "source_hash": source_hash,
        "source_active": source_active,
        "post_build": sticky("post_build", "build"),
        "post_export": sticky("post_export", "export"),
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    # hooks-result.json 是唯一传值通道：不写 GITHUB_OUTPUT，避免与 composite
    # 的 outputs 形成第二条耦合路径。
    print(f"dispatch {args.anchor}:{args.phase} → {len(executed)} 个 hook，"
          f"ok={not failed}，结果 {result_path}")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# selftest
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(description="Godot CI 扩展钩子两阶段执行器")
    sub = ap.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="离线静态校验 + 产 plan + 打 bundle")
    v.add_argument("--manifest", required=True)
    v.add_argument("--chain", required=True, choices=CHAINS)
    v.add_argument("--context", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--bundle-out", required=True)
    v.add_argument("--anchors", default="")
    v.add_argument("--capabilities", default="",
                   help="能力注册表路径（默认 contract/hooks-capabilities.json）")
    v.add_argument("--policy", default="declared", choices=POLICIES)

    d = sub.add_parser("dispatch", help="运行期按 (anchor,phase) 执行钩子")
    d.add_argument("--bundle", required=True)
    d.add_argument("--anchor", required=True)
    d.add_argument("--phase", required=True, choices=PHASES)
    d.add_argument("--context", required=True)
    d.add_argument("--result", required=True)
    d.add_argument("--plan", default="")
    d.add_argument("--chain", default="", choices=CHAINS)
    d.add_argument("--scons-flags", default="",
                   help="平台 workflow 追加后的完整 SCons 标志（合并基准优先于契约值）")
    d.add_argument("--current-preset", default="",
                   help="逐预设锚点（export-preset-each）的当前预设名，注入 context.export.current_preset")

    b = sub.add_parser("bundle", help="独立重建 bundle（与 validate 同一实现）")
    b.add_argument("--plan", required=True)
    b.add_argument("--bundle-out", required=True)
    b.add_argument("--root", required=True)
    b.add_argument("--anchors", default="")
    b.add_argument("--policy", default="declared", choices=POLICIES)
    b.add_argument("--chain", default="build", choices=CHAINS)

    sub.add_parser("selftest", help="无参数自测")
    return ap


def selftest() -> int:
    """自测已拆到 .github/tests/test_run_hooks.py；此入口委托过去。"""
    test = Path(__file__).resolve().parents[1] / "tests" / "test_run_hooks.py"
    return subprocess.run([sys.executable, str(test)]).returncode


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "dispatch":
            return cmd_dispatch(args)
        if args.command == "bundle":
            return cmd_bundle(args)
        if args.command == "selftest":
            return selftest()
        raise HookError(f"未知子命令 {args.command}")
    except HookError as e:
        print_error(str(e))
        return 1
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print_error(f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
