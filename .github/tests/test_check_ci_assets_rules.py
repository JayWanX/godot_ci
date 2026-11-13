#!/usr/bin/env python3
"""check_ci_assets.py 新增规则的稳定性与兼容性验证（三向）。

每条规则三类样本：
  阳性（构造违规）→ 必须报错且信息含关键词（不漏报）
  阴性（合法形近）→ 必须不报（无假阳性）
  兼容（块标量 / 表达式 / 无 sparse 等合法写法）→ 不得误伤

隔离方式：把 check_ci_assets.py 及其依赖复制进临时工程——脚本用 __file__ 定位
仓库根，直接改 cwd 无效（会检查真实仓库，阳性样本全部漏报）。

用法：python .github/tests/test_check_ci_assets_rules.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REAL = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")

MIN_CONTRACT = (
    '{"engine":{"source":"o/r","target":["template_release"],"platform":["linux"]},'
    '"build":{},"export":{},"artifacts":{},"hooks":{}}'
)
ANCHORS = ('{"anchors":[{"id":"package","chains":["build","export"],'
           '"phases":["before"]}]}')
CAPABILITIES = ('{"capabilities":{"read:context":[],"write:artifacts":["artifacts"]},'
                '"secret_names":{"build":["GIT_PAT"],"export":["GIT_PAT"],'
                 '"release":["GIT_PAT"]}}')
RUNNER_EXPR = "runs-on: ${{ fromJSON(needs.x.outputs.rj).linux }}"
DEPS = ("check_ci_assets.py", "jsonc.py", "run_hooks.py", "godot_context.py",
        "schema.py", "path_guard.py", "ref_guard.py", "preset_name.py",
        "artifact_name.py", "apply_hook_patches.py", "normalize_hooks_result.py")

# 骨架含 hooks 锚点挂点：锚点接线校验要求表内每个锚点都有 dispatcher，
# 缺失会成为噪声，掩盖目标规则的判定。
# 同时声明样本用到的 inputs（repo-safe / platform / matrix 由 with 传入）：
# 真实工作流的 skeleton 也都会声明它们，「inputs.x 必须有声明」这条规则才不会干扰。
HEAD = (
    "on:\n"
    "  workflow_call:\n"
    "    inputs:\n"
    "      repo-safe:\n"
    '        type: string\n        default: ""\n'
    "      platform:\n"
    '        type: string\n        default: ""\n'
    "jobs:\n"
    "  j:\n"
    f"    {RUNNER_EXPR}\n"
    "    strategy:\n"
    "      matrix:\n"
    "        preset-safe: [a]\n"
    "        package: [p]\n"
    "    steps:\n"
    "      - name: dispatch\n"
    "        uses: ./ci-support/.github/actions/hooks\n"
    "        with:\n"
    "          anchor: package\n"
    "          phase: before\n"
)


def wf(body: str = "") -> str:
    """骨架 + body（body 按 steps 列表项同级书写）。"""
    body = textwrap.dedent(body)
    return HEAD if not body.strip() else HEAD + textwrap.indent(body, "      ")


def callee(input_type: str, default: str) -> str:
    """被调 workflow：声明一个 retention input。"""
    return (
        "on:\n"
        "  workflow_call:\n"
        "    inputs:\n"
        "      retention:\n"
        f"        type: {input_type}\n"
        f"        default: {default}\n"
        "jobs:\n"
        "  j:\n"
        f"    {RUNNER_EXPR}\n"
        "    steps:\n"
        "      - name: dispatch\n"
        "        uses: ./ci-support/.github/actions/hooks\n"
        "        with:\n"
        "          anchor: package\n"
        "          phase: before\n"
    )


def caller(value: str) -> str:
    """调用方：把 value 传给 callee 的 retention。"""
    return (
        "on:\n"
        "  workflow_call: {}\n"
        "jobs:\n"
        "  c:\n"
        "    uses: $/.github/workflows/callee.yml\n"
        "    with:\n"
        f"      retention: {value}\n"
        "  d:\n"
        f"    {RUNNER_EXPR}\n"
        "    steps:\n"
        "      - name: dispatch\n"
        "        uses: ./ci-support/.github/actions/hooks\n"
        "        with:\n"
        "          anchor: package\n"
        "          phase: before\n"
    )


def release_wf(run_body: str, env_lines: str = "") -> str:
    """project-release.yml 骨架：publish job 里含清理脚本的 step。

    env_lines 为该 step 的缩进后 env 行（已含前导空格），用于构造
    完整性门控接线的正反样本。骨架另带锚点 dispatcher，避免锚点接线
    规则报错掩盖目标规则的判定。
    """
    return (
        "on:\n"
        "  workflow_call: {}\n"
        "jobs:\n"
        "  publish:\n"
        f"    {RUNNER_EXPR}\n"
        "    steps:\n"
        "      - name: dispatch\n"
        "        uses: ./ci-support/.github/actions/hooks\n"
        "        with:\n"
        "          anchor: package\n"
        "          phase: before\n"
        "      - name: prune\n"
        + ("        env:\n" + env_lines if env_lines else "")
        + "        run: |\n"
        + textwrap.indent(textwrap.dedent(run_body), "          ")
    )


# ---------------------------------------------------------------------------
# check_platform_job_with_keys / check_compat_scalar_coverage 样本
# ---------------------------------------------------------------------------

# 被调平台 workflow：声明两个 input（repo-safe / profile）
PLATFORM_CALLEE = (
    "on:\n"
    "  workflow_call:\n"
    "    inputs:\n"
    "      repo-safe:\n"
    '        type: string\n        default: ""\n'
    "      profile:\n"
    '        type: string\n        default: ""\n'
    "jobs:\n"
    "  build:\n"
    "    runs-on: ${{ fromJSON(needs.x.outputs.rj).linux }}\n"
    "    steps:\n"
    "      - name: dispatch\n"
    "        uses: ./ci-support/.github/actions/hooks\n"
    "        with:\n"
    "          anchor: package\n"
    "          phase: before\n"
)


def platform_caller(with_lines: str) -> str:
    """调用方：一个平台作业调起 linux-build.yml。"""
    return (
        "on:\n  workflow_call: {}\njobs:\n"
        "  linux-build:\n"
        "    uses: $/.github/workflows/linux-build.yml\n"
        "    with:\n" + with_lines +
        "  ok:\n"
        "    runs-on: ${{ fromJSON(needs.x.outputs.rj).linux }}\n"
        "    steps:\n"
        "      - name: dispatch\n"
        "        uses: ./ci-support/.github/actions/hooks\n"
        "        with:\n"
        "          anchor: package\n"
        "          phase: before\n"
    )


PLATFORM_CALLER_OK = platform_caller(
    "      repo-safe: x\n      profile: y\n")
PLATFORM_CALLER_MISSING = platform_caller(
    "      repo-safe: x\n")
PLATFORM_CALLER_EXTRA = platform_caller(
    "      repo-safe: x\n      profile: y\n      bogus-key: z\n")

# compat 标量引用样本：缺 web 一个平台
COMPAT_CALLER_MISSING = (
    "on:\n  workflow_call: {}\njobs:\n"
    "  node:\n"
    "    runs-on: ${{ fromJSON(needs.x.outputs.rj).linux }}\n"
    "    steps:\n"
    "      - name: dispatch\n"
    "        uses: ./ci-support/.github/actions/hooks\n"
    "        with:\n"
    "          anchor: package\n"
    "          phase: before\n"
    + "".join(
        f"# compat-standard-includes-{p}\n"
        for p in ("linux", "windows", "macos", "android", "ios", "visionos"))
)


# ---------------------------------------------------------------------------
# check_artifact_download_needs 样本：三个作业的最小骨架（上传 / 下载 / 中间传递）
# ---------------------------------------------------------------------------

ARTIFACT_UPLOAD_JOB = (
    "  prefetch:\n"
    f"    {RUNNER_EXPR}\n"
    "    steps:\n"
    "      - name: upload\n"
    "        uses: actions/upload-artifact@v4\n"
    "        with:\n"
    "          name: demo_editor_linux\n"
    "          path: a.txt\n"
)

ANCHOR_STEP = (
    "      - name: dispatch\n"
    "        uses: ./ci-support/.github/actions/hooks\n"
    "        with:\n"
    "          anchor: package\n"
    "          phase: before\n"
)

ARTIFACT_EXPORT_JOB = (
    "  export:\n"
    "    needs: __NEEDS__\n"
    f"    {RUNNER_EXPR}\n"
    "    steps:\n"
    + ANCHOR_STEP +
    "      - name: download\n"
    "        uses: actions/download-artifact@v4\n"
    "        with:\n"
    "          pattern: demo_editor_*\n"
)


def artifact_wf(needs: str, extra_job: str = "") -> str:
    """上传作业 + 下载作业（needs 可指定）+ 可选中间作业。"""
    return (
        "on:\n  workflow_call: {}\njobs:\n"
        + ARTIFACT_UPLOAD_JOB
        + extra_job
        + ARTIFACT_EXPORT_JOB.replace("__NEEDS__", needs)
    )


ARTIFACT_NEEDS_OK = artifact_wf("[prefetch]")
ARTIFACT_NEEDS_MISSING = artifact_wf("[other]").replace(
    "  export:\n", "  other:\n    " + RUNNER_EXPR + "\n"
    "    steps:\n" + ANCHOR_STEP +
    "  export:\n")
# 完全没有 needs：连边都没有（报错消息与「不在链上」不同）
ARTIFACT_NO_NEEDS = (
    "on:\n  workflow_call: {}\njobs:\n"
    + ARTIFACT_UPLOAD_JOB
    + ARTIFACT_EXPORT_JOB.replace("    needs: __NEEDS__\n", "")
)
ARTIFACT_NEEDS_TRANSITIVE = artifact_wf(
    "[middle]",
    "  middle:\n    needs: [prefetch]\n" + f"    {RUNNER_EXPR}\n"
    "    steps:\n" + ANCHOR_STEP)
def inputs_wf(dispatch_inputs: str, call_inputs: str, refs: list) -> str:
    def block(indent: str, names: str) -> str:
        if not names:
            return ""
        out = [f"{indent}inputs:"]
        for n in names.split(","):
            n = n.strip()
            out.append(f"{indent}  {n}:")
            out.append(f"{indent}    type: string")
            out.append(f'{indent}    default: ""')
        return "\n".join(out) + "\n"

    with_lines = "".join(f"          {r}: ${{{{ inputs.{r} }}}}\n" for r in refs)
    return (
        "on:\n"
        "  workflow_dispatch:\n"
        + block("    ", dispatch_inputs)
        + "  workflow_call:\n"
        + block("    ", call_inputs)
        + "jobs:\n"
        "  j:\n"
        f"    {RUNNER_EXPR}\n"
        "    steps:\n"
        "      - name: dispatch\n"
        "        uses: ./ci-support/.github/actions/hooks\n"
        "        with:\n"
        "          anchor: package\n"
        "          phase: before\n"
        "      - name: use\n"
        "        uses: $/.github/workflows/dummy.yml\n"
        "        with:\n" + (with_lines or "          x: y\n")
    )


# 最小合法 release 骨架：project-release.yml 的最小合法形态（只有 workflow_call）
RELEASE_CALL_ONLY = (
    "on:\n"
    "  workflow_call:\n"
    "    inputs: {}\n"
    "jobs:\n"
    "  publish:\n"
    f"    {RUNNER_EXPR}\n"
    "    steps:\n"
    "      - name: dispatch\n"
    "        uses: ./ci-support/.github/actions/hooks\n"
    "        with:\n"
    "          anchor: package\n"
    "          phase: before\n"
)


CASES = [
    # A：outputs 不得直传给 type: number 的 input
    ("A1 number input + outputs 直传 → 报错", False,
     {"callee.yml": callee("number", "7"),
      "caller.yml": caller("${{ needs.r.outputs.retention }}")}, "retention"),
    ("A2 number input + 字面数字 → 不报", True,
     {"callee.yml": callee("number", "7"), "caller.yml": caller("7")}, ""),
    ("A3 string input + outputs 直传 → 不报", True,
     {"callee.yml": callee("string", '"7"'),
      "caller.yml": caller("${{ needs.r.outputs.retention }}")}, ""),
    # A4/A5：default 字面量必须与 type 一致（GitHub 解析期按 type 期望对应 token）
    ("A4 string input + 裸数字 default → 报错", False,
     {"callee.yml": callee("string", "7"), "caller.yml": caller("x")}, "type=string"),
    ("A5 boolean input + 字符串 default → 报错", False,
     {"callee.yml": callee("boolean", '"true"'), "caller.yml": caller("x")},
     "type=boolean"),
    ("A6 number input + 裸数字 default → 不报", True,
     {"callee.yml": callee("number", "7"), "caller.yml": caller("7")}, ""),

    # B：run 普通标量含续行反斜杠会被 YAML 折叠
    ("B1 run 普通标量 + 行尾反斜杠 → 报错", False,
     {"w.yml": wf("""
            - name: bad
              run: python x.py --a \\
                --b "v"
            """)}, "run"),
    ("B2 run: | 块 + 反斜杠续行 → 不报", True,
     {"w.yml": wf("""
            - name: good
              run: |
                python x.py --a \\
                  --b "v"
            """)}, ""),
    ("B3 run 单行 → 不报", True, {"w.yml": wf("""
            - run: python x.py --a
            """)}, ""),
    ("B4 run: >- 折叠块 + 反斜杠 → 不报", True,
     {"w.yml": wf("""
            - name: folded
              run: >-
                python x.py --a \\
                --b v
            """)}, ""),
    ("B5 run 多行无续行反斜杠 → 不报", True,
     {"w.yml": wf("""
            - name: multi
              run: |
                echo one
                echo two
            """)}, ""),

    # C：无 path 的 checkout 会清空工作区根
    ("C1 无 path checkout 在子目录之后 → 报错", False,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
            - uses: actions/checkout@v4
            """)}, "清空工作区根"),
    ("C2 无 path checkout 在前 → 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
            - uses: actions/checkout@v4
              with:
                path: ci-support
            """)}, ""),
    ("C3 两者都带 path → 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: a
            - uses: actions/checkout@v4
              with:
                path: b
            """)}, ""),

    # D：sparse-checkout 必须覆盖 run 引用的脚本目录
    ("D1 sparse 漏 .github/scripts 且 run 引用 → 报错", False,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github/contract
            - run: python ci-support/.github/scripts/tool.py
            """)}, "未覆盖它"),
    ("D2 sparse 含 .github/scripts → 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github/contract
                  .github/scripts
            - run: python ci-support/.github/scripts/tool.py
            """)}, ""),
    ("D3 无 sparse（全量检出）→ 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
            - run: python ci-support/.github/scripts/tool.py
            """)}, ""),

    # D4–D9：本地 action 引用须被同 job 检出覆盖（通用判据，用任意 action 名验证，
    # 不针对任何具体 action）。骨架自带 hooks 引用，故清单样例均含 hooks 以隔离判定。
    ("D4 sparse 漏列某个 action → 报错", False,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github/actions/hooks
                  .github/contract
            - uses: ./ci-support/.github/actions/foo
            """)}, "未覆盖它"),
    ("D5 清单按 action 粒度裁剪但漏列另一个 action → 报错", False,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github/actions/hooks
                  .github/actions/alpha
            - uses: ./ci-support/.github/actions/beta
            """)}, "未覆盖它"),
    ("D6 sparse 精确含该 action → 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github/actions/hooks
                  .github/actions/foo
            - uses: ./ci-support/.github/actions/foo
            """)}, ""),
    ("D7 sparse 用 .github 整目录 → 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github
            - uses: ./ci-support/.github/actions/foo
            """)}, ""),
    ("D8 无 sparse（全量检出）→ 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
            - uses: ./ci-support/.github/actions/foo
            """)}, ""),
    ("D9 多个不同 action 均被覆盖 → 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github/actions/hooks
                  .github/actions/alpha
                  .github/actions/beta
            - uses: ./ci-support/.github/actions/alpha
            - uses: ./ci-support/.github/actions/beta
            """)}, ""),
    ("D10 清单未按 action 粒度裁剪（只列 scripts/contract）→ 不查 action 引用 → 不报", True,
     {"w.yml": wf("""
            - uses: actions/checkout@v4
              with:
                path: ci-support
                sparse-checkout: |
                  .github/contract
                  .github/scripts
            - uses: ./ci-support/.github/actions/foo
            """)}, ""),

    # E：runs-on 不得为字面量
    ("E1 runs-on 字面量 → 报错", False,
     {"w.yml": HEAD.replace(RUNNER_EXPR, "runs-on: ubuntu-latest")}, "字面量"),
    ("E2 runs-on 列表 → 报错", False,
     {"w.yml": HEAD.replace(RUNNER_EXPR, "runs-on: [self-hosted, linux]")}, "字面量"),
    ("E3 runs-on 表达式 → 不报", True, {"w.yml": HEAD}, ""),

    # F：YAML 解析失败应报语法错误，而不是 KeyError 崩溃
    ("F1 YAML 语法错误 → 报语法错误而非崩溃", False,
     {"w.yml": HEAD + '      - name: broken\n        run: echo "a: b\n'
                     "      - bad: [unclosed\n"}, "YAML 语法错误"),

    # G：导出产物名必须来自 resolve 渲染（契约 artifacts.export_naming）
    ("G1 ZIP_NAME 在 workflow 里拼矩阵维度 → 报错", False,
     {"w.yml": wf("""
            - name: pack
              env:
                ZIP_NAME: ${{ inputs.repo-safe }}_${{ matrix.preset-safe }}
              run: zip -r "$ZIP_NAME.zip" .
            """)}, "拼产物名"),
    ("G2 ZIP_NAME 取 matrix.package → 不报", True,
     {"w.yml": wf("""
            - name: pack
              env:
                ZIP_NAME: ${{ matrix.package }}
              run: zip -r "$ZIP_NAME.zip" .
            """)}, ""),
    ("G3 ZIP_NAME 纯字面量 → 不报", True,
     {"w.yml": wf("""
            - name: pack
              env:
                ZIP_NAME: fixed_name
              run: zip -r "$ZIP_NAME.zip" .
            """)}, ""),
    # 平台级附加包按 Q3-a 是 CI 布局常量，用 ZIP_NAME_EXTRA，不受本规则管辖
    ("G4 ZIP_NAME_EXTRA 拼平台常量 → 不报", True,
     {"w.yml": wf("""
            - name: pack-extra
              env:
                ZIP_NAME_EXTRA: ${{ inputs.repo-safe }}_${{ inputs.platform }}_extra
              run: zip -r "$ZIP_NAME_EXTRA.zip" .
            """)}, ""),

    # H：Release 清理步骤必须接完整性门控与产物名前缀
    ("H1 清理步骤缺 EXPORT_COMPLETE → 报错", False,
     {"project-release.yml": release_wf(
         "python ci-support/.github/scripts/prune_release_assets.py "
         "--repo o/r --tag v1 --prefix \"$P\" --complete true\n",
         env_lines='          ARTIFACT_PREFIX: ${{ needs.export.outputs.artifact-prefix }}\n')},
     "EXPORT_COMPLETE"),
    ("H2 未传 --complete → 报错", False,
     {"project-release.yml": release_wf(
         "python ci-support/.github/scripts/prune_release_assets.py "
         "--repo o/r --tag v1 --prefix \"$P\"\n",
         env_lines=('          ARTIFACT_PREFIX: ${{ needs.export.outputs.artifact-prefix }}\n'
                    '          EXPORT_COMPLETE: ${{ needs.export.outputs.export-complete }}\n'))},
     "--complete"),
    ("H3 门控齐全 → 不报", True,
     {"project-release.yml": release_wf(
         "python ci-support/.github/scripts/prune_release_assets.py "
         "--repo o/r --tag v1 --prefix \"$P\" --complete \"$C\"\n",
         env_lines=('          ARTIFACT_PREFIX: ${{ needs.export.outputs.artifact-prefix }}\n'
                    '          EXPORT_COMPLETE: ${{ needs.export.outputs.export-complete }}\n'))},
     ""),
    ("H4 未启用清理 → 不报（能力可选）", True,
     {"project-release.yml": release_wf("echo noop\n")}, ""),
]

# I：写 file-command 未防空键（GitHub 解析失败 → step 非零退出）
OUTPUT_GUARD_SCRIPTS = {
    "guarded.py": (
        "def set_output(key, value):\n"
        "    if not key:\n"
        "        return\n"
        "    with open('x', 'a') as f:\n"
        '        f.write(f"{key}={value}\\n")\n'
    ),
    "unguarded.py": (
        "def set_output(key, value):\n"
        "    with open('x', 'a') as f:\n"
        '        f.write(f"{key}={value}\\n")\n'
    ),
    "declared_closed.py": (
        "def set_output(key, value):\n"
        "    with open('x', 'a') as f:\n"
        "        # noqa: output-key —— key 为字面量封闭集\n"
        '        f.write(f"{key}={value}\\n")\n'
    ),
}

CASES += [
    # 骨架 workflow 仅用于满足锚点接线规则，与本组判定无关
    ("I1 写 $GITHUB_OUTPUT 未防空键 → 报错", False,
     {"w.yml": HEAD,
      "scripts/unguarded.py": OUTPUT_GUARD_SCRIPTS["unguarded.py"]}, "未防空键"),
    ("I2 显式防空键 → 不报", True,
     {"w.yml": HEAD,
      "scripts/guarded.py": OUTPUT_GUARD_SCRIPTS["guarded.py"]}, ""),
    ("I3 封闭取值集已声明 → 不报", True,
     {"w.yml": HEAD,
      "scripts/declared_closed.py": OUTPUT_GUARD_SCRIPTS["declared_closed.py"]}, ""),

    # J：inputs.<name> 引用必须至少在一个触发器段声明过
    ("J1 两段都未声明就引用 → 报错", False,
     {"w.yml": inputs_wf(dispatch_inputs="target", call_inputs="target",
                         refs=["target", "nope"])}, "未声明"),
    ("J2 只在 workflow_call 声明、手动触发取空 → 不报（共用 jobs 的合法模式）", True,
     {"w.yml": inputs_wf(dispatch_inputs="target", call_inputs="target,caller-uses-ref",
                         refs=["target", "caller-uses-ref"])}, ""),
    ("J3 只在 workflow_dispatch 声明 → 不报", True,
     {"w.yml": inputs_wf(dispatch_inputs="target,draft", call_inputs="target",
                         refs=["target", "draft"])}, ""),

    # K：project-release.yml 在 CI 仓库必须只由 workflow_call 触发
    ("K1 project-release.yml 含 workflow_dispatch → 报错", False,
     {"project-release.yml": RELEASE_CALL_ONLY.replace(
         "on:\n  workflow_call:", "on:\n  workflow_dispatch:\n  workflow_call:")},
     "只允许 workflow_call"),
    ("K2 project-release.yml 含 push:tags → 报错", False,
     {"project-release.yml": RELEASE_CALL_ONLY.replace(
         "on:\n  workflow_call:", "on:\n  push:\n    tags: ['v*']\n  workflow_call:")},
     "只允许 workflow_call"),
    ("K3 只有 workflow_call → 不报", True, {"project-release.yml": RELEASE_CALL_ONLY}, ""),
]


# 编译参数内联规则：覆盖内联编译参数注入的阳性样本。
# 每个 case 都附一个 w.yml（HEAD 骨架）满足锚点接线规则，避免噪声掩盖目标判定。
def scons_wf(matrix_block: str, with_scons: str = "", extra_inputs: str = "",
             job_if: str = "    if: inputs.target != 'editor'\n") -> str:
    """最小 workflow 骨架：声明 inputs + 自定义 matrix + build-pipeline 调用点。

    默认带 job 级 target 门控（web 只差 editor 一档）：缺门控本身是一条
    独立规则，不带会让每个样本都额外报一条噪声。要测门控取值时传 job_if="" 自行注入。
    """
    decl = ("      scons-flags:\n        type: string\n        default: \"\"\n"
            "      target:\n        type: string\n        default: template_release\n"
            "      web-variants-json:\n        type: string\n        default: \"[]\"\n"
            + extra_inputs)
    with_lines = ("      - name: build\n"
                  "        uses: ./ci-support/.github/actions/build-pipeline\n"
                  "        with:\n"
                  f"          scons-flags: {with_scons}\n") if with_scons else ""
    return (
        "on:\n  workflow_call:\n    inputs:\n" + decl +
        "jobs:\n  j:\n" + job_if + f"    {RUNNER_EXPR}\n" +
        "    strategy:\n      matrix:\n" + matrix_block +
        "    steps:\n"
        "      - name: dispatch\n"
        "        uses: ./ci-support/.github/actions/hooks\n"
        "        with:\n"
        "          anchor: package\n"
        "          phase: before\n" + with_lines
    )


INLINE_MATRIX_BOGUS = (
    "        include:\n"
    "          - variant: threads\n"
    "            scons-flags: threads=yes arch=wasm32 bogus_flag=1\n"
)
INLINE_MATRIX_WASM64 = (
    "        include:\n"
    "          - variant: threads\n"
    "            scons-flags: threads=yes arch=wasm64\n"
)
FROMJSON_MATRIX = "        include: ${{ fromJSON(inputs.web-variants-json) }}\n"

CASES += [
    ("matrix 内联 bogus_flag=1 → 报错", False,
     {"w.yml": HEAD, "web-build.yml": scons_wf(INLINE_MATRIX_BOGUS)}, "fromJSON"),
    ("matrix 内联 arch=wasm64 → 报错（wasm64 防回潮）", False,
     {"w.yml": HEAD, "web-build.yml": scons_wf(INLINE_MATRIX_WASM64)}, "fromJSON"),
    ("web matrix 用 fromJSON 表达式 → 不报", True,
     {"w.yml": HEAD, "web-build.yml": scons_wf(
         FROMJSON_MATRIX, "${{ inputs.scons-flags }} ${{ matrix.scons_flags }}")}, ""),
    ("非 web workflow 内联 arch=wasm32 → 报错（wasm* 仅 web）", False,
     {"w.yml": HEAD, "linux-build.yml": scons_wf(
         FROMJSON_MATRIX, "${{ inputs.scons-flags }} arch=wasm32")}, "wasm* 仅允许 web"),
    ("web workflow 内联 arch=wasm64（with.scons-flags）→ 报错", False,
     {"w.yml": HEAD, "web-build.yml": scons_wf(
         FROMJSON_MATRIX, "${{ inputs.scons-flags }} arch=wasm64")}, "wasm64"),
    ("web workflow 内联合法 wasm32（with.scons-flags）→ 不报", True,
     {"w.yml": HEAD, "web-build.yml": scons_wf(
         FROMJSON_MATRIX, "${{ inputs.scons-flags }} arch=wasm32")}, ""),
    ("with.scons-flags 含未知键 my_flag → 报错", False,
     {"w.yml": HEAD, "web-build.yml": scons_wf(
         FROMJSON_MATRIX, "${{ inputs.scons-flags }} my_flag=1")}, "未知内联编译参数"),
    ("合法内联 arch=${{ matrix.arch }} + threads=yes → 不报", True,
     {"w.yml": HEAD, "web-build.yml": scons_wf(
         FROMJSON_MATRIX, "${{ inputs.scons-flags }} arch=${{ matrix.arch }} threads=yes")}, ""),
]


# ---------------------------------------------------------------------------
# 此前无正反样本的 14 条静态规则各补阳性/阴性样本。
# 判据：规则回归不再只靠真仓全量跑隐式验证。
# ---------------------------------------------------------------------------
def decl_wf(inputs_text: str, body: str = "") -> str:
    """带 workflow_call inputs 声明的最小 workflow（body 为 steps 同级内容）。"""
    return ("on:\n  workflow_call:\n    inputs:\n" + inputs_text +
            "jobs:\n  j:\n" + f"    {RUNNER_EXPR}\n" +
            "    steps:\n"
            "      - name: dispatch\n"
            "        uses: ./ci-support/.github/actions/hooks\n"
            "        with:\n          anchor: package\n          phase: before\n" + body)


CALLER_USES = ("on:\n  workflow_call: {}\njobs:\n  c:\n"
               "    uses: $/.github/workflows/callee.yml@v1\n"
               "    with:\n      __WITH__\n  d:\n" + f"    {RUNNER_EXPR}\n" +
               "    steps:\n      - name: dispatch\n"
               "        uses: ./ci-support/.github/actions/hooks\n"
               "        with:\n          anchor: package\n          phase: before\n")


def caller_raw(with_line: str) -> str:
    return CALLER_USES.replace("__WITH__", with_line)


def demo_action(run_body: str) -> str:
    """composite action 骨架：一个 run step 里调用自带脚本。"""
    body = textwrap.indent(textwrap.dedent(run_body), "        ")
    return (
        "name: demo\n"
        "description: 演示 action\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - name: step\n"
        "      shell: bash\n"
        "      run: |\n"
        + body
    )

FULL_BASELINE = json.dumps({
    "platforms": {p: {"build_runner": "ubuntu-22.04", "export_runner": "ubuntu-22.04",
                      "editor_archive": "linux.x86_64"}
                  for p in ("linux", "windows", "macos", "android", "web", "ios", "visionos")},
    "roles": {"resolve": "ubuntu-latest", "upload": "ubuntu-22.04"},
    "toolchain": {"emsdk": "4.0.11", "java": "17", "scons": "4.11.0"},
})
PARTIAL_BASELINE = json.dumps({
    "platforms": {"linux": {"build_runner": "ubuntu-22.04",
                            "export_runner": "ubuntu-22.04",
                            "editor_archive": "linux.x86_64"}},
    "roles": {"resolve": "ubuntu-latest"},
})
GC_STUB_OK = ("PLATFORMS = ('linux','windows','macos','android','ios','visionos','web')\n"
              "PLATFORM_TARGET_SUPPORT = {'web': ('template_release',)}\n"
              "def f(cfg):\n    return cfg['x']\n")
GC_STUB_BAD = GC_STUB_OK + "def g(cfg):\n    return cfg.get('x', 7)\n"
ACTION_YML = ("name: foo\ninputs:\n  real:\n    required: false\n    default: ''\n"
              "runs:\n  using: composite\n  steps: []\n")
ANCHORS_TWO = json.dumps({"anchors": [
    {"id": "package", "chains": ["build"], "phases": ["before"]},
    {"id": "extra", "chains": ["build"], "phases": ["before"]},
]})
CONTRACT_V1 = json.dumps({"ci": {"repository": "o/r", "ref": "v1"}, "$schema": "https://raw.githubusercontent.com/o/r/v1/x.json"})
# $schema 的 ref 段与 ci.ref 不同值：该 ref 分支必须真的生效（曾整段死代码）
CONTRACT_SCHEMA_REF_MISMATCH = json.dumps({
    "ci": {"repository": "o/r", "ref": "v1"},
    "$schema": "https://raw.githubusercontent.com/o/r/v2/x.json"})
# blob 形态：ref 位置与 raw 不同，按 raw 解析会得到 "blob"——应显式拒绝
CONTRACT_BLOB_FORM = json.dumps({
    "ci": {"repository": "o/r", "ref": "v1"},
    "$schema": "https://github.com/o/r/blob/v1/x.json"})
# 相对路径：私有 CI 仓库可用，跳过 URL ref 比对但契约本身合法
CONTRACT_RELATIVE_SCHEMA = json.dumps({
    "ci": {"repository": "o/r", "ref": "v1"},
    "$schema": ".github/contract/contract.schema.json"})
TPL_V1 = ("on: {}\njobs:\n  j:\n    uses: o/r/.github/workflows/x.yml@v1\n"
          "    with:\n      caller-uses-ref: v1\n")
TPL_V2 = TPL_V1.replace("caller-uses-ref: v1", "caller-uses-ref: v2")

CASES += [
    # check_input_contract
    ("调用方传被调方未声明的 input → 报错", False,
     {"callee.yml": decl_wf("      retention:\n        type: string\n        default: '7'\n"),
      "caller.yml": caller_raw("bogus: x")}, "未声明的 input"),
    ("调用方传已声明 input → 不报", True,
     {"callee.yml": decl_wf("      retention:\n        type: string\n        default: '7'\n"),
      "caller.yml": caller_raw("retention: '7'")}, ""),
    # check_action_input_contract
    ("step 给 action 传未声明 input → 报错", False,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - name: use\n        uses: ./ci-support/.github/actions/foo\n"
                       "        with:\n          bogus: x\n"),
      "actions/foo/action.yml": ACTION_YML}, "未声明的 input"),
    ("step 传已声明 input → 不报", True,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - name: use\n        uses: ./ci-support/.github/actions/foo\n"
                       "        with:\n          real: x\n"),
      "actions/foo/action.yml": ACTION_YML}, ""),
    # check_no_hardcoded_artifact_names
    ("硬编码产物名拼法 → 报错", False,
     {"w.yml": decl_wf("      target:\n        type: string\n        default: ''\n",
                       "      - run: echo repo-safe_${{ inputs.target }}\n")}, "硬编码产物名"),
    ("无硬编码拼法 → 不报", True,
     {"w.yml": decl_wf("      target:\n        type: string\n        default: ''\n",
                       "      - run: echo ok\n")}, ""),
    # check_no_job_level_relative_workflow_uses
    ("job 级 './' 调 reusable workflow → 报错", False,
     {"w.yml": "on:\n  workflow_call: {}\njobs:\n  j:\n"
               "    uses: ./.github/workflows/other.yml\n" + f"    {RUNNER_EXPR}\n" +
               "    steps:\n      - name: dispatch\n"
               "        uses: ./ci-support/.github/actions/hooks\n"
               "        with:\n          anchor: package\n          phase: before\n",
      "other.yml": decl_wf("")}, "跨仓库失效"),
    ("job 级 '$/' 调 reusable workflow → 不报", True,
     {"w.yml": "on:\n  workflow_call: {}\njobs:\n  j:\n"
               "    uses: $/.github/workflows/other.yml@v1\n" + f"    {RUNNER_EXPR}\n" +
               "    steps:\n      - name: dispatch\n"
               "        uses: ./ci-support/.github/actions/hooks\n"
               "        with:\n          anchor: package\n          phase: before\n",
      "other.yml": decl_wf("")}, ""),
    # check_optional_inputs_have_default
    ("可选 input 无 default → 报错", False,
     {"w.yml": decl_wf("      foo:\n        type: string\n")}, "可选但无 default"),
    ("可选 input 有 default → 不报", True,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n")}, ""),
    # check_literals
    ("run 块内嵌具体引擎版本 → 报错", False,
     {"w.yml": decl_wf("", "      - run: echo 4.7.2-stable\n")}, "字面量扫描命中"),
    ("run 块无字面量 → 不报", True,
     {"w.yml": decl_wf("", "      - run: echo ok\n")}, ""),
    # check_anchor_wiring
    ("锚点表新增锚点但无 dispatcher → 报错", False,
     {"w.yml": HEAD, "contract/hooks-anchors.json": ANCHORS_TWO}, "锚点接线缺失"),
    ("单锚点且已接线 → 不报", True, {"w.yml": HEAD}, ""),
    # check_script_flags
    ("run 传脚本未定义的 --旗标 → 报错", False,
     {"w.yml": decl_wf("", "      - run: python scripts/tool.py --bogus\n"),
      "scripts/tool.py": "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--real')\n"},
     "未定义的"),
    ("run 传已定义 --旗标 → 不报", True,
     {"w.yml": decl_wf("", "      - run: python scripts/tool.py --real\n"),
      "scripts/tool.py": "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--real')\n"}, ""),
    # action 自带脚本（.github/actions/<action>/scripts/<name>）：候选路径必须覆盖，
    # 否则 action 内脚本的旗标交叉检查全部静默落空
    ("action 内脚本传未定义 --旗标 → 报错", False,
     {"w.yml": HEAD,
      "actions/demo/action.yml": demo_action(
          'python "$GITHUB_ACTION_PATH/scripts/step.py" --bogus\n'),
      "actions/demo/scripts/step.py":
          "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--real')\n"},
     "未定义的"),
    ("action 内脚本传已定义 --旗标 → 不报", True,
     {"w.yml": HEAD,
      "actions/demo/action.yml": demo_action(
          'python "$GITHUB_ACTION_PATH/scripts/step.py" --real\n'),
      "actions/demo/scripts/step.py":
          "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--real')\n"}, ""),
    # 同一 run 块内并列命令：窗口必须止于命令边界，不能把后一条命令的旗标算到前一条
    ("同 run 块后一条命令的旗标不算到前一条 → 不报", True,
     {"w.yml": decl_wf("", "      - run: |\n"
                       "          python scripts/tool.py --real\n"
                       "          python scripts/other.py --other\n"),
      "scripts/tool.py": "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--real')\n",
      "scripts/other.py": "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--other')\n"}, ""),
    ("同 run 块用 && 并列的旗标仍属同一条命令 → 报错", False,
     {"w.yml": decl_wf("", "      - run: |\n"
                       "          python scripts/tool.py --real && python scripts/tool.py --bogus\n"),
      "scripts/tool.py": "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--real')\n"},
     "未定义的"),
    ("续行反斜杠内的旗标属同一条命令 → 报错", False,
     {"w.yml": decl_wf("", "      - run: |\n"
                       "          python scripts/tool.py --real \\\n"
                       "            --bogus\n"),
      "scripts/tool.py": "import argparse\nap = argparse.ArgumentParser()\nap.add_argument('--real')\n"},
     "未定义的"),
    # check_artifact_dimensions
    ("prepare-build 调用漏 arch/variant → 报错", False,
     {"w.yml": decl_wf("", "      - uses: ./ci-support/.github/actions/prepare-build\n"
                       "        with:\n          target: editor\n")}, "产物名维度会丢失"),
    ("prepare-build 显式传 arch/variant → 不报", True,
     {"w.yml": decl_wf("", "      - uses: ./ci-support/.github/actions/prepare-build\n"
                       "        with:\n          arch: x86_64\n          variant: ''\n")}, ""),
    # check_input_env_naming
    ("env 透传 inputs.x 且被 run 直接消费（非 INPUT_ 前缀）→ 报错", False,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - name: x\n        env:\n          FOO: ${{ inputs.foo }}\n"
                       "        run: echo $FOO\n")}, "应改名"),
    ("用 INPUT_ 前缀 → 不报", True,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - name: x\n        env:\n          INPUT_FOO: ${{ inputs.foo }}\n"
                       "        run: echo $INPUT_FOO\n")}, ""),
    # `${KEY}` 花括号形态同样算「被 run 直接消费」（脚本里更常见的写法）
    ("env 非 INPUT_ 前缀但用 ${FOO} 消费 → 报错", False,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - name: x\n        env:\n          FOO: ${{ inputs.foo }}\n"
                       "        run: echo \"${FOO}\"\n")}, "应改名"),
    ("env 非 INPUT_ 前缀且 run 不消费该变量 → 不报", True,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - name: x\n        env:\n          FOO: ${{ inputs.foo }}\n"
                       "        run: echo ok\n")}, ""),
    # check_platform_target_gates
    ("web 门控放行集合与支持表不一致 → 报错", False,
     {"w.yml": HEAD,
      "web-build.yml": scons_wf(FROMJSON_MATRIX,
                                job_if="    if: inputs.target == 'editor'\n")},
     "不一致"),
    ("web 门控 != editor → 不报", True,
     {"w.yml": HEAD,
      "web-build.yml": scons_wf(FROMJSON_MATRIX,
                                job_if="    if: inputs.target != 'editor'\n")},
     ""),
    # 门控存在性：支持集不是全集的平台删掉 job 级门控 → 报错
    ("web 删掉 job 级 target 门控 → 报错", False,
     {"w.yml": HEAD, "web-build.yml": scons_wf(FROMJSON_MATRIX, job_if="")},
     "缺 job 级 target 门控"),
    ("支持全集（linux）无门控 → 不报", True,
     {"w.yml": HEAD, "linux-build.yml": scons_wf(FROMJSON_MATRIX, job_if="")}, ""),
    # 除 web 外各平台都支持三档全集：无门控合法，残留门控反而会静默挡掉合法组合
    ("支持全集（windows）无门控 → 不报", True,
     {"w.yml": HEAD, "windows-build.yml": scons_wf(FROMJSON_MATRIX, job_if="")}, ""),
    ("支持全集（macos）无门控 → 不报", True,
     {"w.yml": HEAD, "macos-build.yml": scons_wf(FROMJSON_MATRIX, job_if="")}, ""),
    ("支持全集（ios-visionos）无门控 → 不报", True,
     {"w.yml": HEAD, "ios-visionos-build.yml": scons_wf(FROMJSON_MATRIX, job_if="")}, ""),
    ("支持全集（windows）残留 template_debug 门控 → 报错", False,
     {"w.yml": HEAD,
      "windows-build.yml": scons_wf(FROMJSON_MATRIX,
                                    job_if="    if: inputs.target != 'template_debug'\n")},
     "残留 target 门控"),
    # check_platform_job_with_keys：平台作业的 with 键集须覆盖被调方 inputs
    ("平台作业漏传 with 键 → 报错", False,
     {"build.yml": PLATFORM_CALLER_MISSING,
      "linux-build.yml": PLATFORM_CALLEE}, "漏传 with 键"),
    ("平台作业完整传 with → 不报", True,
     {"build.yml": PLATFORM_CALLER_OK,
      "linux-build.yml": PLATFORM_CALLEE}, ""),
    ("平台作业传未登记的多余键 → 报错", False,
     {"build.yml": PLATFORM_CALLER_EXTRA,
      "linux-build.yml": PLATFORM_CALLEE}, "未声明/未登记的 with 键"),
    # check_compat_scalar_coverage：每平台标量的引用集合须等于 PLATFORMS
    ("build.yml 漏某平台的 compat 标量引用 → 报错", False,
     {"w.yml": HEAD, "build.yml": COMPAT_CALLER_MISSING}, "缺 compat-standard-includes"),
    # check_no_hardcoded_artifact_names
    ("artifact 名里直接拼 inputs.target（另一种写法）→ 报错", False,
     {"w.yml": decl_wf("      target:\n        type: string\n        default: ''\n",
                       "      - name: pack\n        env:\n"
                       "          ARTIFACT: demo_${{ inputs.target }}\n        run: echo $ARTIFACT\n")},
     "硬编码产物名"),
    ("artifact 名取 env.ARTIFACT（渲染结果）→ 不报", True,
     {"w.yml": decl_wf("      target:\n        type: string\n        default: ''\n",
                       "      - name: pack\n        env:\n"
                       "          ARTIFACT: ${{ env.ARTIFACT }}\n        run: echo $ARTIFACT\n")}, ""),
    # check_platform_config_coverage
    ("基线 platforms 缺平台 → 报错", False,
     {"w.yml": HEAD, "contract/platform-baseline.json": PARTIAL_BASELINE}, "缺平台"),
    ("基线 platforms 覆盖七平台 → 不报", True,
     {"w.yml": HEAD, "contract/platform-baseline.json": FULL_BASELINE}, ""),
    # check_no_fallback_constants
    ("godot_context 出现 .get(字符串, 整数字面量) → 报错", False,
     {"w.yml": HEAD, "scripts/godot_context.py": GC_STUB_BAD}, "兜底常量"),
    ("无整数字面量兜底 → 不报", True,
     {"w.yml": HEAD, "scripts/godot_context.py": GC_STUB_OK}, ""),
    # 字面量 || 兜底：默认值必须声明在 input default / schema default / 基线
    ("workflow 用 || '字面量' 兜底 → 报错", False,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - run: echo \"${{ inputs.foo || 'ok' }}\"\n")}, "字面量兜底"),
    ("workflow 用 || '' 空分支 → 不报", True,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - run: echo \"${{ inputs.foo != '' && inputs.foo || '' }}\"\n")}, ""),
    ("带 noqa: fallback 的行 → 不报", True,
     {"w.yml": decl_wf("      foo:\n        type: string\n        default: ''\n",
                       "      - run: echo \"${{ inputs.foo || 'default' }}\"  # noqa: fallback\n")}, ""),
    # check_template_ref_consistency
    ("uses:@v1 与 caller-uses-ref:v2 不一致 → 报错", False,
     {"w.yml": HEAD, "templates/workflows/t.yml": TPL_V2,
      "templates/ci-contract.json": CONTRACT_V1}, "不一致"),
    ("uses:@v1 与 caller-uses-ref:v1 一致 → 不报", True,
     {"w.yml": HEAD, "templates/workflows/t.yml": TPL_V1,
      "templates/ci-contract.json": CONTRACT_V1}, ""),
    # $schema URL 的 ref 段确实参与比对（该分支曾因 endswith 判断永假而整段失效）
    ("$schema 的 ref 段与 ci.ref 不一致 → 报错", False,
     {"w.yml": HEAD, "templates/workflows/t.yml": TPL_V1,
      "templates/ci-contract.json": CONTRACT_SCHEMA_REF_MISMATCH}, "四方不一致"),
    ("$schema 为 blob 形态 → 显式报错（不静默跳过）", False,
     {"w.yml": HEAD, "templates/workflows/t.yml": TPL_V1,
      "templates/ci-contract.json": CONTRACT_BLOB_FORM}, "不是 raw 形态"),
    ("$schema 为相对路径（私有仓库）→ 不报", True,
     {"w.yml": HEAD, "templates/workflows/t.yml": TPL_V1,
      "templates/ci-contract.json": CONTRACT_RELATIVE_SCHEMA}, ""),
    # check_artifact_download_needs：下载 job 必须在上传 job 的 needs 链上
    ("下载 artifact 的 job 缺上传方 needs → 报错", False,
     {"w.yml": ARTIFACT_NEEDS_MISSING}, "缺边会与上传竞态"),
    ("下载 job 在上传 job 的 needs 上 → 不报", True,
     {"w.yml": ARTIFACT_NEEDS_OK}, ""),
    ("下载 job 完全没有 needs → 报错", False,
     {"w.yml": ARTIFACT_NO_NEEDS}, "happens-before"),
    ("need 经中间 job 传递 → 不报", True,
     {"w.yml": ARTIFACT_NEEDS_TRANSITIVE}, ""),
]


def build_project(tmp: Path, workflows: dict) -> None:
    """在 tmp 下搭最小工程，并复制真实脚本使 ROOT 解析到 tmp。

    workflows 的键可带 `scripts/` 前缀（写到 .github/scripts/ 下）或
    `actions/` 前缀（写到 .github/actions/ 下），其余写进 .github/workflows/。
    """
    gh = tmp / ".github"
    for sub in ("workflows", "actions", "contract", "scripts", "templates/workflows"):
        (gh / sub).mkdir(parents=True, exist_ok=True)
    for name in DEPS:
        src = REAL / "scripts" / name
        if src.is_file():
            shutil.copy2(src, gh / "scripts" / name)
    (gh / "contract" / "hooks-anchors.json").write_text(ANCHORS, encoding="utf-8")
    (gh / "contract" / "hooks-capabilities.json").write_text(CAPABILITIES, encoding="utf-8")
    # platforms 段是权威平台清单：每个平台都要齐备三个必需字段
    plats = {p: {"build_runner": "ubuntu-22.04",
                 "export_runner": "ubuntu-22.04",
                 "editor_archive": "linux.x86_64"}
             for p in ("linux", "windows", "macos", "android", "web", "ios", "visionos")}
    (gh / "contract" / "platform-baseline.json").write_text(
        json.dumps({"platforms": plats,
                    "roles": {"resolve": "ubuntu-latest", "upload": "ubuntu-22.04"},
                    "toolchain": {"emsdk": "4.0.11", "java": "17", "scons": "4.11.0"}}),
        encoding="utf-8")
    (tmp / "ci-contract.json").write_text(MIN_CONTRACT, encoding="utf-8")
    for name, body in workflows.items():
        if name.startswith("scripts/"):
            (gh / "scripts" / name[len("scripts/"):]).write_text(body, encoding="utf-8")
        elif name.startswith("actions/"):
            target = gh / "actions" / name[len("actions/"):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        elif name.startswith("contract/"):
            # 覆盖 build_project 预写的基线（用于基线回潮 / 老规则正反样本）
            (gh / "contract" / name[len("contract/"):]).write_text(body, encoding="utf-8")
        elif name.startswith("templates/"):
            target = gh / "templates" / name[len("templates/"):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        else:
            (gh / "workflows" / name).write_text(body, encoding="utf-8")


def run_check(project: Path) -> tuple:
    """在临时工程内执行静态校验，返回 (退出码, 输出)。"""
    script = project / ".github" / "scripts" / "check_ci_assets.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", env=ENV)
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    failed = 0
    for title, expect_ok, workflows, needle in CASES:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            build_project(project, workflows)
            code, output = run_check(project)
            ok = (code == 0) == expect_ok
            detail = ""
            if ok and needle and code != 0 and needle not in output:
                ok = False
                detail = f"（报错未含 {needle!r}）"
            elif not ok:
                detail = f"（exit={code}，期望 {'通过' if expect_ok else '报错'}）"
            print(f"[{'ok' if ok else 'FAIL'}] {title}{detail}")
            if not ok:
                print(textwrap.indent(output[-500:], "    "))
                failed += 1
    print(f"\n{len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
