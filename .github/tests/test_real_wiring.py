#!/usr/bin/env python3
"""真实资产接线断言（真实资产接线断言）。

既有 23 套件偏「函数级 + 合成夹具」，真实 web-build.yml 与真实
platform-baseline.json 之间的缝没人站——threads 变体曾误用 wasm64
正是这样假绿过去的。本套件直接读真实资产，把「声明面 ↔ 执行面」钉死：

1. web-build.yml 的 matrix.include 必须是 fromJSON(inputs.*) 表达式；
   工作流内不得出现 wasm64 / 内联变体 flags（wasm64 / 内联参数回归点）。
2. build.yml 必须把 platforms-json 的 web.variants 透传为 web-variants-json
   （改基线 variants 即改构建矩阵，）。
3. 真实基线 platforms.web.variants 只含 wasm32，且 godot_context 的
   platforms-json 输出携带同一份 variants（构建/导出两处消费同源）。

每条判据都带一个「注入回归」自验：把资产改回旧缺陷形态，断言本套件的
判定函数必须拒绝——防止护栏本身退化成恒真断言。

用法：python .github/tests/test_real_wiring.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REAL = Path(__file__).resolve().parents[1]          # .github
REPO = REAL.parent                                 # 仓库根
sys.path.insert(0, str(REAL / "scripts"))
from jsonc import read_json  # noqa: E402
import godot_context  # noqa: E402

WEB_WF = REAL / "workflows" / "web-build.yml"
BUILD_WF = REPO / ".github" / "workflows" / "build.yml"
BASELINE = REAL / "contract" / "platform-baseline.json"

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# 判定函数（纯函数，便于注入回归自验复用）
# ---------------------------------------------------------------------------

def web_matrix_is_from_json(web_text: str) -> bool:
    """web-build.yml 的 matrix.include 是否为 fromJSON(inputs.*) 字符串。"""
    marker = "include: ${{ fromJSON(inputs."
    return marker in web_text


def no_inline_bad_flags(web_text: str) -> bool:
    """工作流内无 wasm64、无内联 arch=/threads= 字面量。"""
    if "wasm64" in web_text:
        return False
    for line in web_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") or ":" not in stripped:
            continue
        value = stripped.split(":", 1)[1]
        if "arch=" in value or "threads=" in value:
            return False
    return True


def baseline_variants_ok(baseline: dict) -> bool:
    """真实基线 web.variants 每项都是 wasm32、无 wasm64。"""
    variants = ((baseline.get("platforms") or {}).get("web") or {}).get("variants") or []
    if not variants:
        return False
    for v in variants:
        if not isinstance(v, dict) or not v.get("variant"):
            return False
        flags = str(v.get("scons_flags") or "")
        if "wasm64" in flags or "arch=wasm32" not in flags:
            return False
    return True


def build_wiring_ok(build_text: str) -> bool:
    """build.yml 的 web-build job 从 platforms-json 透传 web.variants。"""
    expr = "toJSON(fromJSON(needs.resolve.outputs.platforms-json).web.variants)"
    return "web-variants-json:" in build_text and expr in build_text


# ---------------------------------------------------------------------------
# 真实资产断言
# ---------------------------------------------------------------------------

@check("真实 web-build.yml：matrix 由基线渲染，无 wasm64 / 内联 flags（wasm64 / 内联参数回归点）")
def t_web_matrix():
    text = WEB_WF.read_text(encoding="utf-8")
    assert web_matrix_is_from_json(text), "matrix.include 不是 fromJSON(inputs.*) 表达式"
    assert no_inline_bad_flags(text), "web-build.yml 内仍有 wasm64 或内联编译参数"
    assert "wasm32" not in text or "arch=wasm32" not in text, \
        "wasm32 不该内联在工作流里（须来自基线）"


@check("真实 build.yml：web 变体经 platforms-json 透传（基线唯一源）")
def t_build_wiring():
    text = BUILD_WF.read_text(encoding="utf-8")
    assert build_wiring_ok(text), "web-variants-json 未从 platforms-json.web.variants 透传"


@check("真实基线 + godot_context：platforms-json 携带同一份 web.variants")
def t_variants_single_source():
    """resolve_platforms 是纯函数（无网络）：它产出的 platforms_cfg 正是
    resolve_outputs 写进 platforms-json 的那一份，构建矩阵与导出 pattern 都读它。
    """
    baseline = read_json(str(BASELINE), "platform-baseline.json")
    assert baseline_variants_ok(baseline), "真实基线 web.variants 含非 wasm32 或为空"
    platforms_cfg = godot_context.resolve_platforms({}, baseline)
    real_variants = baseline["platforms"]["web"]["variants"]
    assert platforms_cfg["web"]["variants"] == real_variants, \
        "platforms-json 的 web.variants 与基线不一致（构建/导出将各读一份）"
    # 契约覆写 variants 也必须进同一份（改契约即改构建矩阵）
    overridden = godot_context.resolve_platforms(
        {"platforms": {"web": {"variants": [
            {"variant": "threads", "scons_flags": "threads=yes arch=wasm32"}]}}},
        baseline)
    assert overridden["web"]["variants"] == [
        {"variant": "threads", "scons_flags": "threads=yes arch=wasm32"}], overridden


# ---------------------------------------------------------------------------
# 注入回归自验：把资产改回旧缺陷形态，判定函数必须拒绝
# ---------------------------------------------------------------------------

@check("注入回归：matrix 内联 bogus_flag / wasm64 被拒（内联编译参数阳性样本）")
def t_injection_matrix():
    text = WEB_WF.read_text(encoding="utf-8")
    # 旧缺陷形态：内联 matrix + wasm64
    injected = text.replace(
        "include: ${{ fromJSON(inputs.web-variants-json) }}",
        "include:\n"
        "          - variant: threads\n"
        "            scons-flags: threads=yes arch=wasm64\n"
        "          - variant: nothreads\n"
        "            scons-flags: threads=no arch=wasm32")
    assert not web_matrix_is_from_json(injected), "注入后仍判为 fromJSON（恒真断言）"
    assert not no_inline_bad_flags(injected), "注入 wasm64 后仍判为合法（恒真断言）"


@check("注入回归：基线 threads 变体改回 wasm64 被拒（wasm64 回归）")
def t_injection_baseline():
    baseline = read_json(str(BASELINE), "platform-baseline.json")
    assert baseline_variants_ok(baseline)
    injected = json.loads(json.dumps(baseline))
    injected["platforms"]["web"]["variants"][0]["scons_flags"] = "threads=yes arch=wasm64"
    assert not baseline_variants_ok(injected), "基线改回 wasm64 后仍判合法（恒真断言）"


@check("注入回归：build.yml 删掉 variants 透传被拒（矩阵接线落点）")
def t_injection_wiring():
    text = BUILD_WF.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if "web-variants-json:" not in ln]
    assert not build_wiring_ok("\n".join(lines)), "删掉透传后仍判已接线（恒真断言）"


@check("静态校验对真实资产注入 bogus_flag 报 EXIT!=0（静态校验端到端）")
def t_static_check_end_to_end():
    """复制真实 .github 到临时工程，注入内联 matrix，跑 check_ci_assets.py。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with tempfile.TemporaryDirectory() as td:
        gh = Path(td) / ".github"
        shutil.copytree(REAL, gh, ignore=shutil.ignore_patterns("__pycache__"))
        wf = gh / "workflows" / "web-build.yml"
        text = wf.read_text(encoding="utf-8").replace(
            "include: ${{ fromJSON(inputs.web-variants-json) }}",
            "include:\n"
            "          - variant: threads\n"
            "            scons-flags: threads=yes arch=wasm32 bogus_flag=1")
        wf.write_text(text, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(gh / "scripts" / "check_ci_assets.py")],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)
        assert proc.returncode != 0, \
            f"注入 bogus_flag 后静态校验仍通过（假绿）：{proc.stdout[-600:]}"
        assert "fromJSON" in proc.stdout or "bogus_flag" in proc.stdout, proc.stdout[-600:]


def editor_archive_single_source(baseline: dict, toolchain_props: dict,
                                 template_text: str) -> bool:
    """编辑器档案是否只有 platforms.editor_archive 一个来源（纯判定，便于注入自验）。

    三条：下载矩阵 == 按档案去重的结果；契约 toolchain 键集不含 editor_platform；
    模板契约不教人写该键。
    """
    platforms_cfg = godot_context.resolve_platforms({}, baseline)
    matrix = godot_context._unique_archives(platforms_cfg)
    expected = list(dict.fromkeys(cfg["editor_archive"]
                                  for cfg in platforms_cfg.values()))
    if [m["platform"] for m in matrix] != expected:
        return False
    if "editor_platform" in toolchain_props:
        return False
    return "editor_platform" not in template_text


@check("真实基线与契约：编辑器档案唯一来源是 platforms.editor_archive")
def t_editor_archive_single_source():
    """编辑器下载矩阵只由 platforms.<平台>.editor_archive 推导。

    契约 toolchain 段曾教人加 editor_platform 键（实际零消费：照做静默不生效）。
    这里把「唯一来源」钉死，并确认改 platforms.editor_archive 仍直接改矩阵。
    """
    baseline = read_json(str(BASELINE), "platform-baseline.json")
    schema = godot_context.load_schema(
        str(REAL / "contract" / "contract.schema.json"))
    toolchain_props = schema["properties"]["build"]["properties"]["toolchain"]["properties"]
    template_text = (REAL / "templates" / "ci-contract.json").read_text(encoding="utf-8")
    assert editor_archive_single_source(baseline, toolchain_props, template_text), \
        "编辑器档案存在第二个来源（或模板仍在教 editor_platform）"
    # 改契约 platforms.editor_archive 即改矩阵（唯一入口仍生效）
    overridden = godot_context.resolve_platforms(
        {"platforms": {"web": {"editor_archive": "win64.exe"}}}, baseline)
    assert {"key": "web", "platform": "win64.exe"} in godot_context._unique_archives(
        overridden), "契约覆写 editor_archive 未进入下载矩阵"


@check("注入回归：写回 editor_platform 后唯一来源判定必须为假（防恒真断言）")
def t_injection_editor_platform():
    baseline = read_json(str(BASELINE), "platform-baseline.json")
    schema = godot_context.load_schema(
        str(REAL / "contract" / "contract.schema.json"))
    props = schema["properties"]["build"]["properties"]["toolchain"]["properties"]
    template_text = (REAL / "templates" / "ci-contract.json").read_text(encoding="utf-8")
    assert editor_archive_single_source(baseline, props, template_text)
    injected_props = {**props, "editor_platform": {"type": "object"}}
    assert not editor_archive_single_source(baseline, injected_props, template_text), \
        "schema 写回 editor_platform 后仍判唯一来源（恒真断言）"
    injected_template = template_text + '\n"editor_platform": {"linux": "linux.x86_64"}\n'
    assert not editor_archive_single_source(baseline, props, injected_template), \
        "模板写回 editor_platform 后仍判唯一来源（恒真断言）"


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
