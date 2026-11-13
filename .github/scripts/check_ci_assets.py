"""CI 资产静态校验：YAML 语法、重复键、workflow_call 与 composite action 的 input 交叉一致性、命名残留、锚点接线完整性。

用法：python .github/scripts/check_ci_assets.py
"""
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jsonc import ensure_utf8_stdio, read_json  # noqa: E402
import godot_context  # noqa: E402
from run_hooks import validate_anchor_table  # noqa: E402

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted(ROOT.glob(".github/workflows/*.yml"))
ACTIONS = sorted(ROOT.glob(".github/actions/*/action.yml"))
TEMPLATE_WORKFLOWS = sorted(ROOT.glob(".github/templates/workflows/*.yml"))
SCRIPTS = ROOT / ".github" / "scripts"
ACTION_SCRIPTS = ROOT / ".github" / "actions"

errors = []


class DupLoader(yaml.SafeLoader):
    """同名键后者覆盖前者（YAML 默认行为），这里显式检出以免静默丢声明。"""


def _dup_check(loader, node, deep=False):
    mapping = loader.construct_mapping(node, deep)
    keys = [k.value for k, _ in node.value]
    for key, count in collections.Counter(keys).items():
        if count > 1:
            errors.append(f"重复键 '{key}'（出现 {count} 次，后者覆盖前者）")
    return mapping


DupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _dup_check)


PARSED = {}


def load_all():
    """解析全部 yml 一次并缓存；后续检查一律复用，避免裸 load 走栈回溯。

    解析失败的文件记 error 并**占位空字典**：后续规则大量按 PARSED[path] 索引，
    缺键会抛 KeyError 掩盖真正的「YAML 语法错误」，占位使后续检查自然空转。
    """
    for path in WORKFLOWS + ACTIONS + TEMPLATE_WORKFLOWS:
        try:
            parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=DupLoader)
        except yaml.YAMLError as e:
            errors.append(f"{path.relative_to(ROOT)}: YAML 语法错误 {e}")
            parsed = {}
        PARSED[path] = parsed if isinstance(parsed, dict) else {}


def workflow_call_spec(path):
    """返回 (工作流名, {input 名: spec})；无 workflow_call 段则返回 (名, {})。"""
    data = PARSED[path]
    on = data.get("on") or data.get(True) or {}
    call = on.get("workflow_call") if isinstance(on, dict) else None
    inputs = (call or {}).get("inputs") or {}
    return path.stem, inputs


def caller_inputs(path):
    """返回本工作流调用的其它工作流 → 传入的 input 名集合。"""
    data = PARSED[path]
    jobs = data.get("jobs") or {}
    out = {}
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if not uses:
            continue
        name = Path(uses.split("@")[0].strip()).stem
        with_ = job.get("with") or {}
        out.setdefault(name, set()).update(with_.keys())
    return out


def check_input_contract():
    """workflow_call 双向一致性：传的 input 必须在被调方声明；必填 input 不得漏传。"""
    declared = {}
    required = {}
    for path in WORKFLOWS:
        data = PARSED[path]
        on = data.get("on") or data.get(True) or {}
        if not isinstance(on, dict) or "workflow_call" not in on:
            continue
        name, inputs = workflow_call_spec(path)
        declared.setdefault(name, set()).update(inputs.keys())
        required.setdefault(name, set()).update(
            iname for iname, spec in inputs.items()
            if isinstance(spec, dict) and spec.get("required"))
    for path in WORKFLOWS:
        for name, passed in caller_inputs(path).items():
            if name not in declared:
                errors.append(f"{path.relative_to(ROOT)}: 调用了未声明 workflow_call 输入的 {name}")
                continue
            unknown = passed - declared[name]
            if unknown:
                errors.append(
                    f"{path.relative_to(ROOT)}: 调用 {name} 传了未声明的 input {sorted(unknown)}")
            missing = required.get(name, set()) - passed
            if missing:
                errors.append(
                    f"{path.relative_to(ROOT)}: 调用 {name} 未传必填 input {sorted(missing)}")


def check_outputs_into_number_inputs():
    """outputs.* 恒为字符串，不得直传 type: number 的 workflow_call input。

    平台作业的 retention-build 等由 resolve 输出的字符串喂入；声明成 number 时
    GitHub 在「evaluate reusable workflow inputs」阶段直接拒绝（Unexpected value '7'），
    整条链在 job 分派前就失败。string 交给 upload-artifact 的 retention-days 同样成立。
    """
    number_inputs = {}
    for path in WORKFLOWS:
        name, inputs = workflow_call_spec(path)
        number_inputs[name] = {
            iname for iname, spec in inputs.items()
            if isinstance(spec, dict) and spec.get("type") == "number"
        }
    outputs_re = re.compile(r"\$\{\{\s*[^}]*\.outputs\.")
    for path in WORKFLOWS:
        for jname, job in (PARSED[path].get("jobs") or {}).items():
            if not isinstance(job, dict) or not job.get("uses"):
                continue
            callee = Path(str(job["uses"]).split("@")[0].strip()).stem
            for key, value in (job.get("with") or {}).items():
                if key not in number_inputs.get(callee, set()):
                    continue
                if isinstance(value, str) and outputs_re.search(value):
                    errors.append(
                        f"{path.relative_to(ROOT)}: job '{jname}' 把 outputs 直传给 "
                        f"{callee}.{key}（声明 number）；outputs 恒为字符串，"
                        f"该 input 应声明 string")


def check_input_default_types():
    """input 的 default 字面量必须与声明的 type 一致。

    GitHub 在解析期按 type 期望对应的 YAML token：`type: string` 却写裸数字
    （`default: 7`）会被拒 —— Unexpected type 'NumberToken' encountered while
    reading 'input default'。这条规则此前只覆盖了反方向（outputs 字符串喂
    number input），漏掉了 default 自身类型不符。
    """
    # type -> 允许的 Python 类型（YAML 解析后）
    expect = {
        "string": (str,),
        "choice": (str,),
        "environment": (str,),
        "boolean": (bool,),
        "number": (int, float),
    }
    for path in WORKFLOWS + ACTIONS + TEMPLATE_WORKFLOWS:
        data = PARSED.get(path)
        if not data:
            continue
        blocks = [("inputs", data.get("inputs"))]
        on = data.get("on") or data.get(True) or {}
        if isinstance(on, dict):
            for trig in ("workflow_call", "workflow_dispatch"):
                blk = on.get(trig) or {}
                if isinstance(blk, dict):
                    blocks.append((f"{trig}.inputs", blk.get("inputs")))
        for label, block in blocks:
            if not isinstance(block, dict):
                continue
            for iname, spec in block.items():
                if not isinstance(spec, dict) or "default" not in spec:
                    continue
                declared = spec.get("type")
                allowed = expect.get(declared)
                if allowed is None:
                    continue
                value = spec["default"]
                # bool 是 int 的子类：number 声明给布尔、boolean 声明给数字都要报
                if isinstance(value, bool):
                    ok = declared == "boolean"
                else:
                    ok = isinstance(value, allowed)
                if not ok:
                    errors.append(
                        f"{path.relative_to(ROOT)}: {label}.{iname} 声明 type={declared}，"
                        f"但 default={value!r}（{type(value).__name__}）；"
                        f"default 字面量须与 type 一致，否则 GitHub 解析期拒绝")


def check_no_hardcoded_artifact_names():
    """CI 代码不得拼硬编码产物名（须经 artifact_name 渲染）。

    判据：任何 artifact 相关行里把 `inputs.target` 直接拼进名字。历史拼法是
    `repo-safe_${{ inputs.target }}`，但换个变量名 / 换个键就能绕过——所以按
    「上下文出现 artifact 字样 + 该行引用了 inputs.target」判定，覆盖
    `ARTIFACT=`、`artifact-name:`、`name:` 等写法。
    """
    pattern = re.compile(
        r"(?i)(artifact|repo-safe|build-naming)[^\n]*\{\{\s*inputs\.target\s*\}\}")
    for path in WORKFLOWS:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                errors.append(
                    f"{path.relative_to(ROOT)}:{i}: 硬编码产物名拼法"
                    f"（产物名须由 artifact_name.py 按契约模板渲染）")


def check_no_job_level_relative_workflow_uses():
    """job 级同仓库 reusable workflow 调用必须用 $/（./ 在跨仓库场景失效）。"""
    for path in WORKFLOWS:
        data = PARSED[path]
        for jname, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            uses = job.get("uses")
            if uses and uses.startswith("./.github/workflows/"):
                errors.append(
                    f"{path.relative_to(ROOT)}: job '{jname}' 用 job 级 './' 调 reusable workflow"
                    f"（跨仓库失效，应改 '$/'）")


def check_optional_inputs_have_default():
    """workflow_call 的可选 input 必须给 default。

    漏给 default 时，调用方不传即得空值，而空值在语义上往往等同于「未配置」——
    这类静默空值只在运行期才暴露，且难以定位。
    """
    for path in WORKFLOWS:
        data = PARSED[path]
        inputs = ((data.get("on") or data.get(True) or {}).get("workflow_call") or {}).get("inputs") or {}
        for name, spec in inputs.items():
            if not isinstance(spec, dict):
                continue
            if not spec.get("required") and "default" not in spec:
                errors.append(
                    f"{path.relative_to(ROOT)}: workflow_call input '{name}' 可选但无 default"
                    f"（调用方漏传即空值）")


def action_specs():
    """返回 {action 目录名: (声明的 input 集合, 必填 input 集合)}。"""
    declared = {}
    required = {}
    for path in ACTIONS:
        data = PARSED[path]
        inputs = data.get("inputs") or {}
        declared[path.parent.name] = set(inputs.keys())
        required[path.parent.name] = {
            name for name, spec in inputs.items()
            if isinstance(spec, dict) and spec.get("required")
        }
    return declared, required


def check_action_input_contract():
    """step 级 composite action 的 with: 双向一致性。

    传的 input 必须在被调 action 声明；必填 input 不得漏传。
    workflow_call 的一致性由 check_input_contract 负责，本项专管 workflow → action。
    """
    declared, required = action_specs()
    for path in WORKFLOWS:
        data = PARSED[path]
        for jname, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                uses = step.get("uses") or ""
                marker = "/actions/"
                if marker not in uses:
                    continue
                name = uses.split(marker)[-1].strip().rstrip("/")
                if name not in declared:
                    continue
                passed = set((step.get("with") or {}).keys())
                label = f"{path.relative_to(ROOT)}: job '{jname}' step '{step.get('name')}'"
                for key in sorted(passed - declared[name]):
                    errors.append(f"{label} 传了 {name} 未声明的 input '{key}'")
                for key in sorted(required[name] - passed):
                    errors.append(f"{label} 未传 {name} 必填的 input '{key}'")


# 字面量扫描（docs/ci-architecture.md 校验入口）：CI 代码不得内嵌项目事实。
# 只扫 run: 步骤的 shell 文本（剥 # 注释行）；description / 注释 / input default 不扫
# ——说明文字允许举例，代码路径才要求契约化。
# 具体引擎版本的正则要求「前面不是版本号片段」：`4.7.2` 这类值常出现在
# `--editor-version 4.7.2` 之外的形态里（如 `scons==4.11.0` 是工具链版本，
# 不该命中）。用 \b 边界 + 排除紧跟 `==` 的形态会漏，故改为只报「形如
# <数字>.<数字>.<数字>[-stable]」且不是工具链赋值右侧的独立字面量。
LITERAL_PATTERNS = {
    "具体引擎版本": re.compile(r"(?<![.\d])4\.\d+\.\d+(?:-stable)?\b"),
    "secrets 明文 token 形态": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "项目契约文件外的 export_presets.cfg 硬编码": re.compile(r"export_presets\.cfg"),
}
_COMMENT_RE = re.compile(r"^\s*#")


def _run_texts(data):
    """收集 workflow/action 的全部 run 块文本（剥 # 注释行）。"""
    texts = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "run" and isinstance(value, str):
                    texts.append("\n".join(
                        ln for ln in value.splitlines() if not _COMMENT_RE.match(ln)))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return texts


def check_literals():
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        data = PARSED[path]
        for text in _run_texts(data):
            for label, pattern in LITERAL_PATTERNS.items():
                for m in pattern.finditer(text):
                    errors.append(
                        f"{path.relative_to(ROOT)}: 字面量扫描命中 [{label}]：{m.group(0)!r}")


def check_anchor_wiring():
    """锚点接线完整性：hooks-anchors.json 表与 dispatcher 挂点双向对照（(id, phase) 粒度）。

    新增锚点的双人步「表加一行 + 手动插 dispatcher」漏掉任一步都是静默陷阱
    （锚点能过 validate、进 plan，却永无 dispatch）——此处当场红。结构校验
    复用 run_hooks.validate_anchor_table（单一实现）。
    """

    table = read_json(str(ROOT / ".github/contract/hooks-anchors.json"), "hooks-anchors.json")
    validate_anchor_table(table)
    expected = {(a["id"], p) for a in table["anchors"] for p in a["phases"]}

    anchor_re = re.compile(r"^\s*anchor:\s*([A-Za-z0-9_-]+)\s*$")
    phase_re = re.compile(r"^\s*phase:\s*([A-Za-z0-9_-]+)\s*$")
    step_re = re.compile(r"^\s*(?:- name:|runs:|inputs:)")
    wired = set()
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        if path.parent.name == "hooks":  # hooks action 自身的输入定义不是挂点
            continue
        pending = None
        for raw in path.read_text(encoding="utf-8").splitlines():
            m = anchor_re.match(raw)
            if m:
                pending = m.group(1)
                continue
            m = phase_re.match(raw)
            if m and pending:
                wired.add((pending, m.group(1)))
                pending = None
            elif step_re.match(raw):
                pending = None  # 步骤边界：孤 anchor 作废，由缺 phase 挂点检查报出
    # Python 执行器内的 dispatcher 挂点（bash 循环无法在循环内插 step 的锚点，
    # 如 export-preset-each 由 export_steps.py 直接调 run_hooks.py dispatch）：
    # 扫 "--anchor", "<id>", "--phase", "<phase>" 字面量。
    py_anchor_re = re.compile(
        r"\"--anchor\",\s*\"([A-Za-z0-9_-]+)\",\s*\"--phase\",\s*\"([A-Za-z0-9_-]+)\"")
    py_files = sorted(ROOT.glob(".github/actions/*/scripts/*.py")) + \
        sorted(ROOT.glob(".github/scripts/*.py"))
    for py in py_files:
        if py.name == "run_hooks.py":  # 执行器自身不是挂点
            continue
        for m in py_anchor_re.finditer(py.read_text(encoding="utf-8")):
            wired.add((m.group(1), m.group(2)))

    for miss in sorted(expected - wired):
        errors.append(f"锚点接线缺失：{miss[0]}:{miss[1]} 在 hooks-anchors.json 内但无 dispatcher 挂点")
    for extra in sorted(wired - expected):
        errors.append(f"未注册挂点：{extra[0]}:{extra[1]} 不在 hooks-anchors.json（先登记再接线）")


def check_run_block_continuation():
    """run 用普通标量（非 `|` 块）时，行尾反斜杠会被 YAML 折叠成字面参数。

    `run: cmd \\` + 续行 折叠为单行文本，反斜杠成为字面 token；shell 收到的命令被拆散
    （preset_name.py 报 unrecognized arguments: <预设名>）。`run: |` 块里的续行反斜杠是
    正确写法，故判据是「run 行本身不是 `|`/`>`，且该 run 标量含续行反斜杠」。
    """
    run_start = re.compile(r"^(\s*)run:\s*(.*)$")
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            m = run_start.match(line)
            if not m:
                continue
            indent = len(m.group(1))
            first = m.group(2)
            if first.startswith(("|", ">")):
                continue  # 块标量：多行命令的正确写法
            # 收集续行（缩进更深、非空）
            tail = []
            for nxt in lines[idx + 1:]:
                if not nxt.strip():
                    break
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                tail.append(nxt)
            # 首行以反斜杠结尾 = 想靠它续行，但普通标量会被折叠成字面 token。
            # 首行完整、续行另有命令的写法（首行不接反斜杠）不受影响。
            if first.rstrip().endswith("\\"):
                errors.append(
                    f"{path.relative_to(ROOT)}:{idx + 1}: run 用普通标量却以反斜杠续行，"
                    f"YAML 折叠后反斜杠成为字面参数、命令被拆散；改用 `run: |`")


def check_checkout_order():
    """无 path 的 checkout 会清空工作区根，必须排在任何子目录检出（ci-support 等）之前。

    actions/checkout 不带 path 时对工作区根执行 git clean -ffdx，把先前检出的
    ci-support/ 一并删掉——后续 `uses: ./ci-support/...` 会报「Can't find action.yml」，
    而日志里那个检出步骤显示成功，极易误判。带 path 的检出只清理该子目录，故安全。
    """
    for path in WORKFLOWS:
        for jname, job in (PARSED[path].get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            seen_subdir_checkout = False
            for step in steps:
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses") or "")
                if not uses.startswith("actions/checkout"):
                    continue
                with_ = step.get("with") or {}
                has_path = bool(str(with_.get("path") or "").strip())
                if has_path:
                    seen_subdir_checkout = True
                elif seen_subdir_checkout:
                    errors.append(
                        f"{path.relative_to(ROOT)}: job '{jname}' 的 step "
                        f"'{step.get('name')}' 是无 path 的 checkout，排在了子目录检出之后"
                        f"（会清空工作区根，删掉先前检出的子目录）")


def check_sparse_checkout_covers_refs():
    """sparse-checkout 清单须覆盖同一 job 引用的仓库内资产目录。

    只针对带 sparse-checkout 的检出 step 判覆盖（全量检出天然覆盖，不在此判）。
    引用分两类，均按「去掉工作区 path 前缀后的仓库相对目录」判定：
      - run 块的 `<path>/.github/scripts/<脚本>.py` → 需 `.github/scripts` 目录；
      - `uses: ./<path>/.github/actions/<名>` → 当清单按 action 粒度裁剪时，需覆盖
        该 action 目录（精确条目或其父目录）。
    漏列只在 CI 运行时炸：前者「No such file」、后者「Can't find action.yml」，本地路径
    齐全看不出来。判据按类别通用，不点名任何具体脚本 / action。清单用目录前缀覆盖即可
    （如 `.github` 覆盖全部）；未按 action 粒度裁剪的清单（只列 scripts / contract 等）
    不检查 action 引用，避免与「该 job 的 action 由别处检出」的合法形态混淆。
    """
    script_ref = re.compile(r"([A-Za-z0-9_.-]+)/\.github/scripts/[A-Za-z0-9_]+\.py")
    action_ref = re.compile(r"^\./([A-Za-z0-9_.-]+)/\.github/actions/([A-Za-z0-9_.-]+)$")
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        data = PARSED[path]
        for jname, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps") or []
            if not isinstance(steps, list):
                continue
            # 检出 step：工作区前缀 path -> sparse-checkout 声明的仓库相对目录集合
            checkouts = {}
            for step in steps:
                if not isinstance(step, dict):
                    continue
                with_ = step.get("with") or {}
                sc = with_.get("sparse-checkout")
                if not isinstance(sc, str):
                    continue
                prefix = str(with_.get("path") or "").strip("/")
                entries = {ln.strip().strip("'\"") for ln in sc.splitlines() if ln.strip()}
                checkouts[prefix] = entries
            if not checkouts:
                continue  # 无 sparse-checkout = 全量检出，不构成缺口
            # 清单是否按 action 粒度裁剪（存在 `.github/actions/<名>` 条目且不是父目录本身）
            action_granular = {
                prefix for prefix, entries in checkouts.items()
                if any(e.startswith(".github/actions/") and e.rstrip("/") != ".github/actions"
                       for e in entries)
            }
            for step in steps:
                if not isinstance(step, dict):
                    continue
                needed_refs = []
                run = step.get("run")
                if isinstance(run, str):
                    for m in script_ref.finditer(run):
                        needed_refs.append((m.group(1), ".github/scripts"))
                m = action_ref.match(str(step.get("uses") or "").strip())
                if m and m.group(1) in action_granular:
                    needed_refs.append((m.group(1), f".github/actions/{m.group(2)}"))
                for prefix, needed in needed_refs:
                    if prefix not in checkouts:
                        continue  # 引用的不是本 job 的检出目录（如 actions 自身路径）
                    if not any(needed == e or needed.startswith(e.rstrip("/") + "/")
                               for e in checkouts[prefix]):
                        errors.append(
                            f"{path.relative_to(ROOT)}: job '{jname}' 引用了 {prefix}/{needed}，"
                            f"但该检出 step 的 sparse-checkout 未覆盖它（运行期会找不到文件）")


def check_script_flags():
    """run 块里的 `--旗标` 与目标脚本 add_argument 交叉比对（传错旗标运行期才炸）。

    只扫 `python <path>/<script>.py` 形式的调用；脚本不存在时跳过（第三方 / 动态路径）。
    """
    py_re = re.compile(r"python(?:3)?\s+[^\n]*?([A-Za-z0-9_./$*{}-]+\.py)")
    flag_re = re.compile(r"(?<!\w)--([A-Za-z][A-Za-z0-9-]*)")
    arg_re = re.compile(r"add_argument\(\s*[\"'](--[A-Za-z][A-Za-z0-9-]*)[\"']")
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        for text in _run_texts(PARSED[path]):
            for m in py_re.finditer(text):
                raw = m.group(1)
                script = _locate_script(raw)
                if script is None:
                    continue
                declared = set(arg_re.findall(script.read_text(encoding="utf-8")))
                if not declared:
                    continue
                # 只取该调用的命令边界内出现的旗标：固定字符窗口会跨到下一条命令，
                # 把别的调用的旗标算到本脚本头上（非独立行的多段命令尤其明显）。
                command = _command_tail(text, m.end())
                for f in flag_re.findall(command):
                    if "--" + f not in declared:
                        errors.append(
                            f"{path.relative_to(ROOT)}: 调用 {script.name} 传了未定义的 '--{f}'"
                            f"（已定义 {sorted(declared)}）")


def _locate_script(raw):
    """脚本名 → 仓库内实际文件路径（找不到返回 None）。

    `.github/scripts/` 与 `.github/actions/<action>/scripts/` 是两处真实布局：
    前者是跨 action 共用的执行器，后者是各 action 自带的步骤脚本。只试
    `.github/scripts/<name>` 会让 action 内的脚本全部落空，旗标交叉检查静默失效。
    `$GITHUB_ACTION_PATH` 等变量写法按文件名在四处布局中查。
    """
    name = Path(raw).name
    candidates = [SCRIPTS / name]
    candidates += sorted(ROOT.glob(f".github/actions/*/scripts/{name}"))
    candidates += sorted(ROOT.glob(f".github/actions/*/{name}"))
    return next((p for p in candidates if p.is_file()), None)


def _command_tail(text, start):
    """从 start 起截出「同一条命令」的文本，到命令边界为止。

    边界取三种 shell 事实：换行（下一条命令）、行尾反斜杠续行（同一条命令，继续读）、
    以及未加引号的分隔符 `;` / `&&` / `||` / `|`（同一条 run 块里的并列命令）。
    """
    out = []
    quote = ""
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and text[i + 1 : i + 2] in ("\n", "\r"):
            # 续行：反斜杠与换行都吃掉，继续读同一条命令
            i += 2
            if text[i - 1] == "\r" and text[i : i + 1] == "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "\n":
            break
        if ch in (";", "|", "&"):
            break
        out.append(ch)
        i += 1
    return "".join(out)


# 工作流内联编译参数的允许键：其余参数必须经契约 build.scons_flags /
# inputs.scons-flags 走 resolve，不能内联进 with: 或 matrix（否则静态校验管不到）。
# 变体集合以平台基线为唯一源，工作流只负责消费，不自行声明编译参数。
_INLINE_SCONS_KEYS = {"arch", "threads"}
_KV_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")
_WASM_RE = re.compile(r"^wasm\d+$")


def _scons_param_texts(data):
    """收集 workflow/action 内所有 with.scons-flags 与 matrix.include 的编译参数文本。

    覆盖两种漏检面：`with:` 里的 scons-flags 字面量、strategy.matrix.include
    条目里的 scons-flags / scons_flags。返回 (来源标签, 文本) 列表。
    """
    out = []

    def walk(node):
        if isinstance(node, dict):
            # with.scons-flags
            with_ = node.get("with")
            if isinstance(with_, dict):
                for key in ("scons-flags", "scons_flags"):
                    value = with_.get(key)
                    if isinstance(value, str):
                        out.append((f"with.{key}", value))
            matrix = node.get("matrix")
            if isinstance(matrix, dict):
                include = matrix.get("include")
                if isinstance(include, list):
                    for i, entry in enumerate(include):
                        if not isinstance(entry, dict):
                            continue
                        for key in ("scons-flags", "scons_flags"):
                            value = entry.get(key)
                            if isinstance(value, str):
                                out.append((f"matrix.include[{i}].{key}", value))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def check_workflow_scons_params():
    """with:/matrix 内联编译参数防回潮（编译参数以平台基线为唯一源）。

    1. web-build.yml 的 strategy.matrix.include 必须是 fromJSON(inputs.*) 表达式：
       变体集合唯一来源是基线 platforms.web.variants（经 platforms-json 下发），
       工作流内不得内联任何变体或 scons 参数——内联 bogus_flag / arch=wasm64
       都在此当场报错。
    2. 全仓内联编译参数抽 key=value：未知键（如 bogus_flag）拒绝；arch=wasm* 仅允许
       web 且必须 wasm32，wasm64 一律拒绝（产物不可用，且官方已 revert）。
    3. 真实基线若有 web.variants，逐变体断言只含 wasm32、不含 wasm64（防基线回潮）。
    """

    for path in WORKFLOWS:
        data = PARSED.get(path) or {}
        is_web = path.name == "web-build.yml"
        if is_web:
            jobs = data.get("jobs") or {}
            for jname, job in jobs.items():
                if not isinstance(job, dict):
                    continue
                matrix = (job.get("strategy") or {}).get("matrix") or {}
                include = matrix.get("include") if isinstance(matrix, dict) else None
                # fromJSON 表达式在 YAML 里是字符串；内联条目是 list
                if not isinstance(include, str) or "fromJSON(" not in include:
                    errors.append(
                        f"{path.relative_to(ROOT)}: job '{jname}' 的 matrix.include 不是 "
                        f"fromJSON(inputs.*) 表达式（变体矩阵必须由基线 "
                        f"platforms.web.variants 渲染，工作流内不得内联变体 / scons 参数）")
        for label, text in _scons_param_texts(data):
            for key, value in _KV_RE.findall(text):
                if key not in _INLINE_SCONS_KEYS:
                    errors.append(
                        f"{path.relative_to(ROOT)}: {label} 含未知内联编译参数 "
                        f"'{key}={value}'（允许键 {sorted(_INLINE_SCONS_KEYS)}；其余须走契约 "
                        f"build.scons_flags / inputs.scons-flags，不得内联）")
                if key == "arch" and _WASM_RE.match(value):
                    if value != "wasm32" or not is_web:
                        errors.append(
                            f"{path.relative_to(ROOT)}: {label} 内联 'arch={value}'——"
                            f"wasm64 产物不可用（官方已 revert），wasm* 仅允许 web 的 wasm32")

    baseline_path = ROOT / ".github" / "contract" / "platform-baseline.json"
    if baseline_path.is_file():
        baseline = read_json(str(baseline_path), "platform-baseline.json")
        variants = ((baseline.get("platforms") or {}).get("web") or {}).get("variants") or []
        for v in variants:
            if not isinstance(v, dict):
                continue
            flags = str(v.get("scons_flags") or "")
            for key, value in _KV_RE.findall(flags):
                if key == "arch" and _WASM_RE.match(value) and value != "wasm32":
                    errors.append(
                        f".github/contract/platform-baseline.json: web 变体 "
                        f"'{v.get('variant')}' 用 arch={value}（wasm64 产物不可用，须 wasm32）")


def check_artifact_dimensions():
    """产物渲染链路：prepare-build 调用点必须显式传 arch / variant（漏传则产物名丢维度）。"""
    for path in WORKFLOWS:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "uses: ./ci-support/.github/actions/prepare-build" not in line:
                continue
            block = "\n".join(lines[i:i + 20])
            missing = [d for d in ("arch", "variant") if not re.search(rf"^\s*{d}:", block, re.M)]
            if missing:
                errors.append(
                    f"{path.relative_to(ROOT)}:{i + 1}: prepare-build 调用未显式传 "
                    f"{', '.join(missing)}（产物名维度会丢失）")



def check_input_env_naming():
    """命名约定防回潮：run 块直接消费的 inputs.* 透传必须用 INPUT_* 前缀。

    只查「env 键的值是纯 ${{ inputs.<name> }}」且「同 step 的 run 文本里出现 $键」
    的组合——这类变量是透传给 shell 消费的，按 naming.md 必须 INPUT_*。
    喂给脚本当上下文的变量（脚本按固定名读 env）与跨 step 的 GITHUB_ENV 不查。

    消费形态三种都要认：`$KEY`、`${KEY}`、`"${KEY}"`。只匹配 `$KEY` 会漏掉
    shellcheck 推荐的 `${KEY}` 写法，而那恰是脚本里更常见的形态。
    """
    pure = re.compile(r"^\s*\$\{\{\s*inputs\.([a-z0-9-]+)\s*\}\}\s*$")
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        data = PARSED.get(path)
        if not data:
            continue

        def walk(node):
            if isinstance(node, dict):
                env = node.get("env")
                run = node.get("run")
                if isinstance(env, dict) and isinstance(run, str):
                    for key, value in env.items():
                        mm = pure.match(str(value)) if isinstance(value, str) else None
                        if not mm or key.startswith("INPUT_"):
                            continue
                        if re.search(r"\$\{?" + re.escape(key) + r"\b", run):
                            errors.append(
                                f"{path.relative_to(ROOT)}: env '{key}' 透传 inputs.{mm.group(1)} "
                                f"且被 run 块直接消费，按 naming.md 应改名 'INPUT_{key}'")
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)


def check_no_literal_runner_labels():
    """workflow 内 runs-on 不得为字面量 label（取值来源 = 平台基线的 platforms / roles 段）。

    唯一例外：workflows/runner.yml（Chain Runner Bootstrap）——其分配先于基线可读，
    ubuntu-latest 是引导常量。块式（列表）runs-on 同样拒绝：表达式只能是行内形态。
    用解析后的 YAML 判定（line 扫描会误命中 workflow_call 的 runs-on input 声明）；
    模板薄触发器无 runs-on，不在扫描面。
    """
    for path in WORKFLOWS:
        if path.name == "runner.yml":
            continue
        for jname, job in (PARSED[path].get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            runs_on = job.get("runs-on")
            literal = str(runs_on) if isinstance(runs_on, list) else (
                runs_on if isinstance(runs_on, str) and "${{" not in runs_on else None)
            if literal:
                errors.append(f"{path.relative_to(ROOT)}: job '{jname}' 的 runs-on 字面量 "
                              f"'{literal}'（runner 取值须经基线 platforms / roles 段引用）")


def check_artifact_download_needs():
    """下载 artifact 的作业必须在 needs 链上覆盖上传它的作业（happens-before）。

    缺边的后果是竞态：`actions/download-artifact` 与上传作业并行时可能读到「正在上传」
    的中间态或直接找不到（pattern 不匹配时默认静默空转），失败信息远离根因。

    判定边界（如实声明，不冒充全覆盖）：
    - 上传 / 下载名字统一归一化：`${{ ... }}` 表达式折叠为同一个占位符，尾部通配与
      下划线剥掉后互为前缀即视为同一家族（如上传 `${{ env.ARTIFACT }}` 与下载
      `${{ env.ARTIFACT }}_*`）；
    - 下载面既包括本 job 自己的 download step，也包括它调用的**本仓可复用工作流**
      （`$/.github/workflows/*.yml`）内的 download step——这是导出链真实形态：
      export.yml 的 prefetch 上传、export-platform.yml 里下载；
    - 上传方只认本文件内的作业（跨仓 / 第三方 action 内的上传无法静态反查）。
    """
    expr_re = re.compile(r"\$\{\{[^}]*\}\}")
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        data = PARSED.get(path)
        if not data:
            continue
        jobs = data.get("jobs") or {}
        if not isinstance(jobs, dict):
            continue
        uploaders = _upload_names(jobs, expr_re)
        if not uploaders:
            continue
        for jname, job in jobs.items():
            if not isinstance(job, dict):
                continue
            downloads = [_norm_artifact(x, expr_re) for x in _download_targets(job)]
            callee = _local_callee_workflow(job)
            if callee:
                downloads += [_norm_artifact(x, expr_re)
                              for x in _callee_download_targets(callee)]
            downloads = [d for d in downloads if d]
            if not downloads:
                continue
            needs = job.get("needs")
            need_list = ([needs] if isinstance(needs, str) else list(needs or []))
            reachable = _transitive_needs(jobs, need_list) | set(need_list)
            related = {up: sorted(n for n in names
                                  if any(_artifact_family_match(d, n) for d in downloads))
                       for up, names in uploaders.items() if up != jname}
            related = {k: v for k, v in related.items() if v}
            if not related:
                continue
            for up_job, names in related.items():
                if up_job in reachable:
                    continue
                errors.append(
                    f"{path.relative_to(ROOT)}: job '{jname}' 下载 {names}（上传方 job "
                    f"'{up_job}'）却不在其 needs 链上（当前 needs={need_list or '[]'}）；"
                    f"缺边会与上传竞态")
            if not need_list:
                errors.append(
                    f"{path.relative_to(ROOT)}: job '{jname}' 下载 artifact 但没有 needs"
                    f"（与上传作业无 happens-before 边，可能读到上传中间态）")


def _upload_names(jobs, expr_re):
    """本文件内各作业上传的 artifact 名（归一化）。

    上传可以发生在作业自己的 step 里，也可以发生在本仓 composite action 的 step 里
    （如 prefetch-editor-templates 内部的 upload-artifact）——后者按 `uses:` 归属到
    调用它的作业，否则「预取作业」在上传方拓扑里缺席，缺边就查不出来。
    """
    action_uploads = _action_upload_names(expr_re)
    out = {}
    for jname, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            names = set()
            if any(m in uses for m in ("upload-artifact", "artifact-upload",
                                       "package-artifact")):
                with_ = step.get("with") or {}
                name = str(with_.get("artifact-name") or with_.get("name") or "").strip()
                if name:
                    names.add(_norm_artifact(name, expr_re))
            local_action = re.search(r"\.github/actions/([A-Za-z0-9_.-]+)", uses)
            if local_action:
                names |= action_uploads.get(local_action.group(1), set())
            if names:
                out.setdefault(jname, set()).update(names)
    return out


def _action_upload_names(expr_re):
    """本仓 composite action 内的上传名：{action 目录名: {归一化名}}。"""
    out = {}
    for path in ACTIONS:
        data = PARSED.get(path)
        if not data:
            continue
        for step in ((data.get("runs") or {}).get("steps") or []):
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            if "upload-artifact" not in uses:
                continue
            with_ = step.get("with") or {}
            name = str(with_.get("name") or with_.get("artifact-name") or "").strip()
            if name:
                out.setdefault(path.parent.name, set()).add(_norm_artifact(name, expr_re))
    return out


def _download_targets(job):
    """单个作业内全部 download step 的 name / pattern（原样文本）。"""
    out = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if "actions/download-artifact" not in str(step.get("uses") or ""):
            continue
        with_ = step.get("with") or {}
        out.append(str(with_.get("pattern") or with_.get("name") or "").strip())
    return out


def _local_callee_workflow(job):
    """job 调用的本仓可复用工作流路径（`$/.github/workflows/x.yml`），否则 None。"""
    uses = str(job.get("uses") or "")
    if not uses.startswith("$/") or ".github/workflows/" not in uses:
        return None
    name = uses.split("@")[0].strip().rsplit("/", 1)[-1]
    return next((p for p in WORKFLOWS if p.name == name), None)


def _callee_download_targets(callee_path):
    """被调可复用工作流内全部 download step 的 name / pattern（原样文本）。"""
    out = []
    for job in (PARSED.get(callee_path) or {}).get("jobs", {}).values():
        if isinstance(job, dict):
            out.extend(_download_targets(job))
    return out


def _norm_artifact(text, expr_re=None):
    """artifact 名归一化：折叠 `${{ ... }}` 表达式、压缩空白、剥尾部通配与下划线。"""
    if expr_re is not None:
        text = expr_re.sub("<expr>", text)
    text = re.sub(r"\s+", " ", text.strip())
    return text.rstrip("*").rstrip("_")


def _transitive_needs(jobs, seeds):
    """按 needs 图求闭包（含直接依赖本身）。"""
    seen = set()
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        job = jobs.get(cur)
        if not isinstance(job, dict):
            continue
        needs = job.get("needs")
        stack.extend([needs] if isinstance(needs, str) else list(needs or []))
    return seen


def _artifact_family_match(download, upload):
    """下载 pattern / 名称与上传名是否同一家族（互为前缀，已归一化）。"""
    if not download or not upload:
        return False
    return (download == upload or download.startswith(upload)
            or upload.startswith(download))


def check_platform_job_with_keys():
    """平台作业的 `with:` 键集合必须与平台 workflow 的 inputs 声明对齐（防漏传）。

    7 个平台作业各自写一遍 ~22 行 `with:`，是同一份声明的 7 份副本——删掉一行
    （例如漏传 `profile`）不会让 YAML 报错，只会在运行期以「默认值」静默降级。
    判据：每个平台作业的 `with:` 键集合 == 被调平台 workflow 的 `workflow_call`
    inputs 声明键集 ∪ 该作业的平台专属键。

    平台专属键是**显式白名单**：新增平台专属键时必须在此登记一次——这正是
    「不让漂移静默发生」想要的，而不是让规则去猜。
    """
    # 平台专属键：不属于平台 workflow inputs，由 build.yml 侧单独提供
    #   platform          ——ios / visionos 共用一个 workflow，平台由调用方指名
    #   web-variants-json ——web 变体矩阵由基线的 variants 渲染后透传
    PLATFORM_EXTRA_KEYS = {
        "ios-build": {"platform"},
        "visionos-build": {"platform"},
        "web-build": {"web-variants-json"},
    }
    path = ROOT / ".github" / "workflows" / "build.yml"
    data = PARSED.get(path)
    if not data:
        return
    # 平台 workflow 的 workflow_call inputs 声明（被调方视角的「应有键集」）
    declared = {}
    for wf in WORKFLOWS:
        on = PARSED[wf].get("on") or PARSED[wf].get(True) or {}
        call = on.get("workflow_call") if isinstance(on, dict) else None
        if isinstance(call, dict):
            declared[wf.stem] = set((call.get("inputs") or {}).keys())
    for jname, job in (data.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        uses = str(job.get("uses") or "")
        if not uses.startswith("$/"):
            continue
        callee = uses.split("@")[0].strip().rsplit("/", 1)[-1].removesuffix(".yml")
        if callee == path.stem:
            continue  # 调自己的作业（无）跳过，避免自指
        expected = declared.get(callee)
        if expected is None:
            continue
        got = set((job.get("with") or {}).keys())
        allowed = expected | PLATFORM_EXTRA_KEYS.get(jname, set())
        missing = sorted(allowed - got)
        extra = sorted(got - allowed)
        if missing:
            errors.append(
                f"{path.relative_to(ROOT)}: job '{jname}' 调 {callee} 漏传 with 键 "
                f"{missing}（被调方会取默认值，静默降级）")
        if extra:
            errors.append(
                f"{path.relative_to(ROOT)}: job '{jname}' 调 {callee} 传了未声明/未登记的 "
                f"with 键 {extra}（新增平台专属键须在本规则的 PLATFORM_EXTRA_KEYS 登记）")


def check_compat_scalar_coverage():
    """`compat-standard-includes-<平台>` 的声明与引用集合必须覆盖 PLATFORMS。

    该标量按平台各出一份（`{platform: on|off}` 的 JSON 映射会让 `== 'off'` 永不
    成立，故拆成每平台一个标量）。新增平台时忘了加标量，构建链会在 `fromJSON` 取到
    空串 → `!= 'off'` 恒真 → 该平台的标准头补齐静默失效。判据：resolve 产出侧与
    build.yml 的引用侧，两边的平台集合都等于 PLATFORMS。
    """
    expected = set(godot_context.PLATFORMS)
    path = ROOT / ".github" / "workflows" / "build.yml"
    if not path.is_file():
        return
    refs = set()
    for m in re.finditer(r"compat-standard-includes-([a-z]+)",
                         path.read_text(encoding="utf-8")):
        refs.add(m.group(1))
    if not refs:
        return  # 未引用任何标量：该文件不承担这份接线（样本夹具 / 精简工程），规则不适用
    # 产出侧：真实脚本里应有按平台产出的循环。只在「确实引用标量」的工程里查，
    # 否则精简夹具（自带的 godot_context 桩）会被误判为失锚。
    text = (ROOT / ".github" / "scripts" / "godot_context.py").read_text(encoding="utf-8")
    if 'outputs[f"compat-standard-includes-{plat}"]' not in text:
        errors.append(".github/scripts/godot_context.py: 未见按平台产出 "
                      "compat-standard-includes-<平台> 标量的循环（规则失锚，请更新判据）")
    missing_ref = sorted(expected - refs)
    if missing_ref:
        errors.append(
            f"{path.relative_to(ROOT)}: 缺 compat-standard-includes 的引用平台 "
            f"{missing_ref}（该平台的标准头补齐会静默失效）")
    extra_ref = sorted(refs - expected)
    if extra_ref:
        errors.append(
            f"{path.relative_to(ROOT)}: 引用了 PLATFORMS 之外的 "
            f"compat-standard-includes-{extra_ref[0]}")


def check_platform_target_gates():
    """平台 workflow 的 target 门控：job 级存在性 + 放行集合与支持表一致。

    门控是防御性的（不合法组合已在 resolve 期 fail-fast），两条判据缺一不可：

    (a) 存在性：`PLATFORM_TARGET_SUPPORT` 里支持集合**不等于全集**的平台，其平台
        workflow 必须有 job 级 target 门控。少了它，不支持的组合会在该 workflow 里
        一路跑到底（编译出零产物或报远离根因的错）。原先的 `if not m: continue`
        让「删掉门控」静默通过，是本规则此前最大的盲区。
    (b) 一致性：门控的放行集合必须与支持表完全相同，防止两处口径漂移。

    支持全集（不需要门控）的平台跳过 (a)，但仍须过 (b'')：支持全集时残留 target 门控
    会静默挡掉合法组合（如 windows 已支持 template_debug，却留着
    `if: inputs.target != 'template_debug'`，该档位永远拉不起 job 也不报错）。
    step 级门控不参与判定——它挡不住 job 拉起，而这里要防的正是「job 白跑」。
    """
    target_re = re.compile(r"inputs\.target\s*(==|!=)\s*'([a-z_]+)'")
    gate_platforms = {
        "linux-build": ("linux",),
        "windows-build": ("windows",),
        "macos-build": ("macos",),
        "android-build": ("android",),
        "web-build": ("web",),
        "ios-visionos-build": ("ios", "visionos"),
    }
    all_targets = set().union(*godot_context.PLATFORM_TARGET_SUPPORT.values())
    for path in WORKFLOWS:
        plats = gate_platforms.get(path.stem)
        if not plats:
            continue
        for plat in plats:
            supported = set(godot_context.PLATFORM_TARGET_SUPPORT[plat])
            if supported == all_targets:
                # 支持全集：不应存在 target 门控，残留即静默挡掉合法组合
                for jname, job in (PARSED[path].get("jobs") or {}).items():
                    if not isinstance(job, dict):
                        continue
                    m = target_re.search(str(job.get("if") or ""))
                    if m:
                        errors.append(
                            f"{path.relative_to(ROOT)}: job '{jname}' 残留 target 门控"
                            f"（{m.group(0)}），但 {plat} 的支持集已是全集 "
                            f"{sorted(supported)}；该门控会静默挡掉合法组合，须删除")
                continue
            has_gate = False
            for jname, job in (PARSED[path].get("jobs") or {}).items():
                if not isinstance(job, dict):
                    continue
                m = target_re.search(str(job.get("if") or ""))
                if not m:
                    continue
                has_gate = True
                op, value = m.groups()
                allowed = ({value} if op == "==" else all_targets - {value})
                if allowed != supported:
                    errors.append(
                        f"{path.relative_to(ROOT)}: job '{jname}' 的 target 门控（{op} "
                        f"'{value}'）与 {plat} 的 PLATFORM_TARGET_SUPPORT "
                        f"{sorted(supported)} 不一致")
            if not has_gate:
                errors.append(
                    f"{path.relative_to(ROOT)}: 缺 job 级 target 门控（{plat} 的支持集为 "
                    f"{sorted(supported)}，不是全集）；不支持的目标会拉整个 job 跑到底，"
                    f"须加 `if: inputs.target == '<允许值>'`")


def check_no_fallback_constants():
    """兜底常量防回潮：默认值的唯一源是 schema default / 平台基线 / action input default。

    两条判据：
    1. `godot_context.py` 内不得出现 `.get(字符串, 整数字面量)` 兜底——整数字面量
       兜底不可达且会与 default 静默漂移；
    2. workflow / action 的 `key:` 里不得用 `|| '字面量'` 给表达式兜底——那是把
       默认值从「声明处」抄进「使用处」，两处一旦漂移就是静默错值。

    合法例外用行尾 `# noqa: fallback` 声明：空串兜底（`|| ''`）本身不算违规——它是
    「该维度取空」的显式表达；需要声明的是「非空占位字面量」里确实无声明处可取的情形
    （如并发组键里「手动触发没有该维度」的占位段）。
    """
    pattern = re.compile(r"\.get\(\s*[\"'][A-Za-z0-9_-]+[\"']\s*,\s*\d+\s*\)")
    text = (ROOT / ".github" / "scripts" / "godot_context.py").read_text(encoding="utf-8")
    for m in pattern.finditer(text):
        errors.append(f".github/scripts/godot_context.py: 兜底常量形态 {m.group(0)!r}"
                      f"（默认值唯一源是 schema default / 平台基线，改下标直取）")

    # `|| 'xxx'`：表达式转义后的字面量兜底（`|| ''` 是合法空值，不算）
    fallback_re = re.compile(r"\|\|\s*'([^']+)'")
    noqa_re = re.compile(r"noqa:\s*fallback")
    for path in tuple(WORKFLOWS) + tuple(ACTIONS):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#") or noqa_re.search(line):
                continue
            m = fallback_re.search(line)
            if m:
                errors.append(
                    f"{path.relative_to(ROOT)}:{i}: 字面量兜底 '|| \"{m.group(1)}\"'"
                    f"（默认值应声明在 action input default / schema default / 平台基线；"
                    f"确属合法空分支请加 `# noqa: fallback`）")


def check_platform_config_coverage():
    """基线 platforms 段必须覆盖全部平台，且每平台字段齐备、取值非空。

    导出/构建矩阵都从 platforms-json 一级取字段（export_runner / build_runner /
    editor_archive）。漏字段会让 fromJSON 取到 null，job 在分派期报错且信息晦涩。
    """
    baseline = read_json(str(ROOT / ".github/contract/platform-baseline.json"),
                         "platform-baseline.json")
    platforms = baseline.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        errors.append("平台基线缺 platforms 段（权威平台清单）")
        return
    required = ("build_runner", "export_runner", "editor_archive")
    for plat in godot_context.PLATFORMS:
        cfg = platforms.get(plat)
        if not isinstance(cfg, dict):
            errors.append(f"平台基线 platforms 缺平台 '{plat}'")
            continue
        for field in required:
            if not cfg.get(field):
                errors.append(f"platforms.{plat} 缺 '{field}' 或为空")
    extra = sorted(set(platforms) - set(godot_context.PLATFORMS))
    if extra:
        errors.append(f"平台基线 platforms 含未知平台 {extra}")

    roles = baseline.get("roles")
    if not isinstance(roles, dict) or "resolve" not in roles:
        errors.append("平台基线 roles 段缺 resolve 键（链级作业 runner）")


def check_template_ref_consistency():
    """跨仓坐标一致：模板触发器 uses:@ref / caller-uses-ref / 契约 ci.ref /
    两处 $schema URL 的 ref 段同值；ci.repository 与 URL 的 owner/repo 一致。

    $schema URL 的 ref 段解析与 `godot_context.parse_schema_url_ref` 同源：
    只认 raw 形态，blob 形态显式报错（不是静默跳过）。
    """
    templates = ROOT / ".github" / "templates"
    contract_path = templates / "ci-contract.json"
    hooks_path = templates / "ci-hooks.json"
    refs = {}
    ci_ref = None
    ci_repo = None
    if contract_path.is_file():
        text = contract_path.read_text(encoding="utf-8")
        m = re.search(r'"ci"\s*:\s*\{[^}]*"repository"\s*:\s*"([^"]+)"[^}]*"ref"\s*:\s*"([^"]+)"', text)
        if m:
            ci_repo, ci_ref = m.group(1), m.group(2)
    for tp in TEMPLATE_WORKFLOWS:
        text = tp.read_text(encoding="utf-8")
        m = re.search(r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/\.github/workflows/[^@\s]+@([^\s#]+)", text)
        if m:
            refs[tp.name] = ("uses", m.group(2), m.group(1))
        m = re.search(r"caller-uses-ref:\s*([^\s#]+)", text)
        if m:
            refs[tp.name + ":caller"] = ("caller", m.group(1), None)
    for jp in (contract_path, hooks_path):
        if not jp.is_file():
            continue
        for m in re.finditer(r'"\$schema"\s*:\s*"([^"]+)"', jp.read_text(encoding="utf-8")):
            url = m.group(1)
            if not url.startswith("https://"):
                continue  # 相对路径形态：私有 CI 仓库可用，跳过 URL ref 比对
            url_ref = godot_context.parse_schema_url_ref(url)
            if url_ref is None:
                errors.append(
                    f"{jp.relative_to(ROOT)}: $schema 不是 raw 形态的 GitHub URL：'{url}'"
                    f"（只接受 https://raw.githubusercontent.com/<owner>/<repo>/<ref>/...；"
                    f"blob 形态的 ref 位置不同，解析结果无意义）")
                continue
            parts = [x for x in url.split("/") if x]
            refs[jp.name] = ("schema", url_ref, parts[2] + "/" + parts[3])
    values = {}
    for key, (kind, ref, repo) in refs.items():
        values.setdefault(ref, []).append(key)
        if kind in ("uses", "schema") and repo and ci_repo and repo != ci_repo:
            errors.append(f"跨仓坐标不一致：{key} 指向 {repo}，契约 ci.repository 为 {ci_repo}")
    if ci_ref:
        values.setdefault(ci_ref, []).append("ci-contract.json:ci.ref")
    if len(values) > 1:
        detail = "; ".join(f"{ref} ← {', '.join(sorted(keys))}" for ref, keys in sorted(values.items()))
        errors.append(f"跨仓 ref 四方不一致：{detail}")


def check_export_package_naming_centralized():
    """导出产物名只能来自 resolve 渲染，run 块不得自行拼预设维度的 zip 名。

    产物名一旦在 workflow 里手拼，契约 artifacts.export_naming 就管不住交付物，
    「只改契约即可改产物名」的判据失效（历史回潮点：此前是
    `ZIP_NAME: ${{ inputs.repo-safe }}_${{ inputs.platform }}_${{ matrix.preset-safe }}`）。
    合法用法是从 packages matrix 直接取 `matrix.package`。

    例外：平台级附加包（钩子 write:artifacts 散文件）不属于任何预设，模板里
    没有 {preset} 可填，按 Q3-a 定为 CI 布局常量 `<repo-safe>_<平台>_extra`。
    它的 env 名是 ZIP_NAME_EXTRA，与本规则管辖的 ZIP_NAME 分开。
    """
    for path in WORKFLOWS:
        data = PARSED.get(path)
        if not data:
            continue

        def walk(node, job_name=""):
            if isinstance(node, dict):
                env = node.get("env")
                if isinstance(env, dict):
                    for key, value in env.items():
                        if not isinstance(value, str) or key != "ZIP_NAME":
                            continue
                        # 唯一合法表达式：直接取 resolve 渲染好的 matrix.package
                        if value.strip() == "${{ matrix.package }}":
                            continue
                        if "${{" in value:
                            errors.append(
                                f"{path.relative_to(ROOT)}: job '{job_name}' 的 {key} "
                                f"在 workflow 里拼产物名（应取 packages matrix 的 "
                                f"matrix.package，由契约 artifacts.export_naming 渲染）")
                for name, sub in node.items():
                    walk(sub, job_name or (name if isinstance(sub, dict) and "runs-on" in sub else ""))
            elif isinstance(node, list):
                for item in node:
                    walk(item, job_name)

        walk(data.get("jobs") or {})


def check_template_caller_contract():
    """模板薄触发器的 `with:` / `secrets:` 必须与它调用的工作流声明对齐。

    模板是跨仓形态的唯一入口：`with:` 键漏一个，调用方 project 的产物名/配置
    就静默降级为默认值；`secrets:` 键漏一个，对应能力在 CI 侧 validate 通过、
    运行时却取不到值。两者都只在 runner 上暴露，模板本身没有人跑过。

    判据：模板里的 `with:` 键 ⊆ 被调工作流的 workflow_call inputs（且不含未声明键）；
    `secrets:` 的每个键 ⊆ 被调工作流的 workflow_call secrets 声明（`inherit` 不查）。
    """
    contract = ROOT / ".github" / "templates" / "ci-contract.json"
    ci_repo = None
    if contract.is_file():
        m = re.search(r'"ci"\s*:\s*\{[^}]*"repository"\s*:\s*"([^"]+)"',
                      contract.read_text(encoding="utf-8"))
        if m:
            ci_repo = m.group(1)
    # 本仓 workflow_call 声明（被调方）
    declared_inputs, declared_secrets = {}, {}
    for wf in WORKFLOWS:
        on = PARSED[wf].get("on") or PARSED[wf].get(True) or {}
        call = on.get("workflow_call") if isinstance(on, dict) else None
        if isinstance(call, dict):
            declared_inputs[wf.stem] = set((call.get("inputs") or {}).keys())
            declared_secrets[wf.stem] = set((call.get("secrets") or {}).keys())
    for tpl in TEMPLATE_WORKFLOWS:
        data = PARSED.get(tpl)
        if not data:
            continue
        for jname, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            uses = str(job.get("uses") or "")
            if ".github/workflows/" not in uses:
                continue
            callee = uses.split("@")[0].strip().rsplit("/", 1)[-1].removesuffix(".yml")
            # 只校验「本仓确有该 workflow_call 声明」的被调方：外部坐标 / 样本夹具
            # 读不到声明，凭名字猜会误报。
            if callee not in declared_inputs:
                continue
            if ci_repo and ci_repo not in uses:
                continue
            with_keys = set((job.get("with") or {}).keys())
            unknown = sorted(with_keys - declared_inputs.get(callee, set()))
            if unknown:
                errors.append(
                    f"{tpl.relative_to(ROOT)}: job '{jname}' 给 {callee} 传了未声明的 with "
                    f"键 {unknown}")
            secrets_block = job.get("secrets")
            if isinstance(secrets_block, dict):
                unknown_s = sorted(set(secrets_block) - declared_secrets.get(callee, set()))
                if unknown_s:
                    errors.append(
                        f"{tpl.relative_to(ROOT)}: job '{jname}' 给 {callee} 传了未声明的 "
                        f"secrets 键 {unknown_s}（被调方无法接收）")


def check_retention_defaults_match_schema():
    """retention 的直调兜底默认值必须与契约 schema default 一致。

    `retention-<类>` 由 resolve 恒产出并逐层下传，action / workflow 里的
    `default:` 只是「绕过 resolve 直调」时的兜底。兜底与权威值一旦漂移，
    直调路径会静默用另一套保留策略——这里把两处钉在一起，谁改都得同时改。
    """
    schema_path = ROOT / ".github" / "contract" / "contract.schema.json"
    if not schema_path.is_file():
        return
    schema = read_json(str(schema_path), "contract.schema.json")
    try:
        retention = schema["properties"]["artifacts"]["properties"]["retention"]["properties"]
    except (KeyError, TypeError):
        errors.append(".github/contract/contract.schema.json: 未找到 artifacts.retention 的 "
                      "properties（规则失锚，请更新判据）")
        return
    authoritative = {k: str(v.get("default", "")) for k, v in retention.items()}
    # 兜底声明点：(路径, 该文件的 retention input 名 → schema 键)
    fallbacks = (
        (ROOT / ".github" / "actions" / "artifact-upload" / "action.yml",
         {"retention-days": "build"}),
        (ROOT / ".github" / "actions" / "package-artifact" / "action.yml",
         {"retention-days": "build"}),
        (ROOT / ".github" / "workflows" / "export-platform.yml",
         {"retention-export": "export"}),
    )
    for path, mapping in fallbacks:
        data = PARSED.get(path)
        if not data:
            continue
        inputs = data.get("inputs") or {}
        for input_name, schema_key in mapping.items():
            spec = inputs.get(input_name)
            if not isinstance(spec, dict) or "default" not in spec:
                continue
            declared = str(spec["default"])
            expected = authoritative.get(schema_key, "")
            if declared != expected:
                errors.append(
                    f"{path.relative_to(ROOT)}: input '{input_name}' 的直调兜底 "
                    f"default={declared!r} 与契约 schema artifacts.retention.{schema_key} "
                    f"的 default={expected!r} 不一致")
    # 平台 workflow 的 retention-build 兜底同样对齐
    for wf in WORKFLOWS:
        data = PARSED.get(wf)
        if not data:
            continue
        on = data.get("on") or data.get(True) or {}
        call = on.get("workflow_call") if isinstance(on, dict) else None
        if not isinstance(call, dict):
            continue
        spec = (call.get("inputs") or {}).get("retention-build")
        if not isinstance(spec, dict) or "default" not in spec:
            continue
        declared = str(spec["default"])
        expected = authoritative.get("build", "")
        if declared != expected:
            errors.append(
                f"{wf.relative_to(ROOT)}: input 'retention-build' 的直调兜底 "
                f"default={declared!r} 与契约 schema artifacts.retention.build 的 "
                f"default={expected!r} 不一致")


def check_dependabot_covers_actions():
    """dependabot 的更新条目必须覆盖每一个 composite action 目录。

    dependabot 只扫 `directory` 指向目录里的 action 文件，不递归——composite
    action 里的第三方 pin 只有逐目录列条目才会被更新。新增 action 目录时漏加
    条目，该目录的 pin 会永远停在旧 SHA，而没有任何信号。
    """
    path = ROOT / ".github" / "dependabot.yml"
    if not path.is_file():
        return
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        errors.append(f".github/dependabot.yml: YAML 语法错误 {e}")
        return
    updates = (data or {}).get("updates") or []
    covered = set()
    for entry in updates:
        if not isinstance(entry, dict):
            continue
        if entry.get("package-ecosystem") != "github-actions":
            continue
        for key in ("directory",):
            value = entry.get(key)
            if isinstance(value, str):
                m = re.fullmatch(r"/\.github/actions/([A-Za-z0-9_.-]+)/?", value)
                if m:
                    covered.add(m.group(1))
        dirs = entry.get("directories")
        if isinstance(dirs, list):
            for value in dirs:
                m = re.fullmatch(r"/?\.github/actions/([A-Za-z0-9_.-]+)/?", str(value))
                if m:
                    # 通配目录（`*`）不算覆盖：dependabot 不支持 glob
                    if "*" not in value:
                        covered.add(m.group(1))
    actual = {p.parent.name for p in ACTIONS}
    missing = sorted(actual - covered)
    extra = sorted(covered - actual)
    if missing:
        errors.append(
            f".github/dependabot.yml: 缺 composite action 目录条目 {missing}"
            f"（这些目录里的第三方 pin 不会被更新）")
    if extra:
        errors.append(
            f".github/dependabot.yml: 引用了不存在的 action 目录 {extra}")


def check_cache_key_parity():
    """cache-restore 与 cache-save 的精确键表达式必须逐字相同。

    两侧各写一遍 5 段键，任一侧加段 / 改条件式都会导致「永不精确命中」——
    现象是缓存命中率悄悄归零，日志里没有任何报错。restore-keys 是回退路径，
    不参与这条判据（它本就按 source-active 条件化）。
    """
    restore = ROOT / ".github" / "actions" / "cache-restore" / "action.yml"
    save = ROOT / ".github" / "actions" / "cache-save" / "action.yml"
    key_re = re.compile(r"^\s*key:\s*(.+?)\s*$", re.M)

    def keys(path):
        if not path.is_file():
            return []
        return [m.group(1) for m in key_re.finditer(path.read_text(encoding="utf-8"))]

    restore_keys = keys(restore)
    save_keys = keys(save)
    if not restore.is_file() or not save.is_file():
        return  # 该工程没有缓存 action（精简夹具），规则不适用
    if not restore_keys or not save_keys:
        errors.append("cache-restore / cache-save 的精确键表达式缺失（规则失锚，请更新判据）")
        return
    for expr in restore_keys:
        if expr not in save_keys:
            errors.append(
                f"{restore.relative_to(ROOT)}: 精确键 '{expr}' 在 cache-save 中找不到同款表达式"
                f"（两侧拼法必须逐字相同，否则永不精确命中）")
    for expr in save_keys:
        if expr not in restore_keys:
            errors.append(
                f"{save.relative_to(ROOT)}: 精确键 '{expr}' 在 cache-restore 中找不到同款表达式"
                f"（两侧拼法必须逐字相同，否则永不精确命中）")


def check_release_prune_wiring():
    """Release 清理步骤必须接完整性门控与产物名前缀。

    漏接 export-complete 会让清理在「只导出部分预设」的运行时删掉上一轮的
    有效附件（静默丢交付物）；漏接 artifact-prefix 则家族判定失效、
    脚本按空前缀跳过（清理静默失效）。两者都是静默故障，必须在静态校验里挡住。
    """
    path = ROOT / ".github" / "workflows" / "project-release.yml"
    data = PARSED.get(path)
    if not data:
        return
    prune = None
    for job_name, job in (data.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str) and "prune_release_assets.py" in run:
                prune = (job_name, step)
    if prune is None:
        return  # 未启用清理不算错（该能力是可选的）
    job_name, step = prune
    env = step.get("env") or {}
    run = step.get("run") or ""
    for required in ("ARTIFACT_PREFIX", "EXPORT_COMPLETE"):
        if required not in env:
            errors.append(f".github/workflows/project-release.yml: job '{job_name}' 的清理步骤缺 "
                          f"env.{required}（漏接会让清理静默失效或静默误删）")
    if "--complete" not in run:
        errors.append(f".github/workflows/project-release.yml: job '{job_name}' 的清理步骤未传 "
                      f"--complete（完整性门控缺失）")
    if "--prefix" not in run:
        errors.append(f".github/workflows/project-release.yml: job '{job_name}' 的清理步骤未传 "
                      f"--prefix（家族判定缺失）")


def check_github_output_key_guard():
    """写 $GITHUB_OUTPUT / $GITHUB_ENV 的函数必须防空键。

    键为空时写出的行形如「等号后直接是值」，GitHub 的 file-command 解析器取键
    得到 null，整条命令失败（"Value cannot be null. (Parameter 'name')"），step
    以非零退出——症状是 CI Self-check 的「单元测试」step 恒失败，日志里却只有一行
    看不出源头的 error。

    判定口径：key 形参的取值点必须显式防空（`if not key: return`），或由**封闭
    取值集**保证非空（字面量硬编码、或已通过白名单校验）。后者在本规则里用
    注释标记 `# noqa: output-key` 声明，避免规则退化成只能靠肉眼判断。
    """
    write_re = re.compile(r'f\.write\(\s*f"\{(\w+)\}=')
    guard_re = re.compile(r"if\s+not\s+(?P<key>\w+)\s*:\s*\n\s+return")
    noqa_re = re.compile(r"noqa:\s*output-key")
    for path in sorted(SCRIPTS.glob("*.py")) + sorted(ACTION_SCRIPTS.glob("**/*.py")):
        text = path.read_text(encoding="utf-8")
        guarded = {m.group("key") for m in guard_re.finditer(text)}
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            m = write_re.search(line)
            if not m:
                continue
            key = m.group(1)
            if key in guarded:
                continue
            # 函数定义行到写入行之间（含 docstring 前）允许 noqa 声明
            window = "\n".join(lines[max(0, i - 12):i + 1])
            if noqa_re.search(window):
                continue
            errors.append(
                f"{path.relative_to(ROOT)}:{i}: 写 file-command 未防空键（'{key}' 为空时"
                f"写出 '=value'，GitHub 解析失败会让 step 非零退出）")


def check_inputs_reference_declared():
    """`inputs.<name>` 的引用必须在**至少一个触发器段**里声明过。

    GitHub 对未声明 input 的 `inputs.x` 引用不报错、静默求值为空串。若某 name 在
    两段都没声明，那就是拼写错误或漏声明，行为会悄悄降级（如 caller-uses-ref 恒空
    使防漂移断言永远跳过），必须拦下。actionlint 会报这类错，但本仓库的静态校验
    此前只看 workflow_call 段（check_input_contract 只收集那一段的 inputs）。

    为什么是「至少一个」而不是「每个」：`jobs` 是两个触发器**共用**的执行体，
    `inputs.x` 是运行期按当前事件求值的。只在 workflow_call 声明的 input
    （如 caller-uses-ref——手动触发没有 uses:@ref 这个事实）在手动触发取空串
    是**正确表达**，不该判错。
    """
    ref_re = re.compile(r"inputs\.([A-Za-z_][A-Za-z0-9_-]*)")
    for path in WORKFLOWS:
        data = PARSED.get(path)
        if not data:
            continue
        on = data.get("on") or data.get(True) or {}
        if not isinstance(on, dict):
            continue
        triggers = [t for t in ("workflow_dispatch", "workflow_call") if t in on]
        if not triggers:
            continue
        declared = set()
        for t in triggers:
            declared |= set(((on.get(t) or {}).get("inputs") or {}).keys())
        text = path.read_text(encoding="utf-8")
        jobs_text = text.split("\njobs:", 1)[-1]
        for ref in sorted(set(ref_re.findall(jobs_text))):
            if ref in declared:
                continue
            errors.append(
                f"{path.relative_to(ROOT)}: 引用了未声明的 input '{ref}'"
                f"（{'/'.join(triggers)} 段都没有，运行期静默求值为空串；"
                f"已声明 {sorted(declared)}）")


def check_release_is_call_only():
    """project-release.yml 在 CI 资产仓库必须只由 workflow_call 触发。

    docs/ci-architecture.md「Release 不是可配项 / 职责分离」：外部项目构建只产
    Artifact 到 CI 仓库，**不创建任何 Release**；Release 落点恒为调用方仓库
    （github.repository 在跨仓 called workflow 下指向调用方）。

    若本文件自带 push:v* 或 workflow_dispatch，在 CI 仓库触发时 github.repository
    就是 CI 仓库自己——会导出 CI 仓库并给它发 Release，既无意义又破坏职责分离。
    手动入口属于项目仓库那一侧的薄触发器（它 workflow_dispatch 后 workflow_call 进来）。
    """
    path = ROOT / ".github" / "workflows" / "project-release.yml"
    data = PARSED.get(path)
    if not data:
        return
    on = data.get("on") or data.get(True) or {}
    if not isinstance(on, dict):
        return
    for trigger in sorted(set(on) - {"workflow_call"}):
        errors.append(
            f".github/workflows/project-release.yml: 含 '{trigger}' 触发（只允许 workflow_call）——"
            f"在 CI 仓库触发会导出 CI 仓库并给它发 Release，违反「外部项目构建不创建 Release」")


def main() -> int:
    load_all()
    check_input_contract()
    check_outputs_into_number_inputs()
    check_input_default_types()
    check_action_input_contract()
    check_no_hardcoded_artifact_names()
    check_no_job_level_relative_workflow_uses()
    check_optional_inputs_have_default()
    check_literals()
    check_anchor_wiring()
    check_workflow_scons_params()
    check_script_flags()
    check_sparse_checkout_covers_refs()
    check_checkout_order()
    check_run_block_continuation()
    check_artifact_dimensions()
    check_input_env_naming()
    check_no_literal_runner_labels()
    check_artifact_download_needs()
    check_platform_job_with_keys()
    check_compat_scalar_coverage()
    check_platform_target_gates()
    check_platform_config_coverage()
    check_no_fallback_constants()
    check_export_package_naming_centralized()
    check_cache_key_parity()
    check_dependabot_covers_actions()
    check_release_prune_wiring()
    check_retention_defaults_match_schema()
    check_template_caller_contract()
    check_release_is_call_only()
    check_github_output_key_guard()
    check_inputs_reference_declared()
    check_template_ref_consistency()
    if errors:
        for e in errors:
            print(f"[FAIL] {e}")
        print(f"\n{len(errors)} 项失败")
        return 1
    print(f"CI 资产静态校验通过（{len(WORKFLOWS)} workflow + {len(ACTIONS)} action）")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())