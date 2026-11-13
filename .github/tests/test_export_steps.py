#!/usr/bin/env python3
"""export_steps.py 自测：export action 五个 step 子命令的判据。

覆盖：
- validate-inputs：通过时写 4 个 GITHUB_ENV；拒绝矩阵（绝对路径 / 空字段 /
  坏密钥 / 版本非法字符 / 缺 templates-dir / 项目与预设文件不存在）
- resolve-presets：显式透传；空 presets = fail-fast（名单唯一实现在 resolve）
- prepare-templates 版本目录推导：patch=0 / rc / hash 段剥离、互证不等拒绝
- export-presets：全成功、单预设失败汇总跑完其余、custom_build 密钥注入

用法：python .github/tests/test_export_steps.py
"""
import os
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions" / "export" / "scripts"))
import export_steps as es  # noqa: E402
from testlib import expect_fail  # noqa: E402  （import 时自行把 scripts/ 置入 sys.path）

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def expect_exit(fn, why, needle=""):
    """转调 testlib.expect_fail：捕获输出（不污染 CI 注解）+ 断言拒绝理由。"""
    return expect_fail(fn, why, needle=needle)


def with_github_env(fn):
    """在临时 GITHUB_ENV 下运行 fn，返回 (返回值, 文件内容)。"""
    fd, p = tempfile.mkstemp()
    os.close(fd)
    old = os.environ.get("GITHUB_ENV")
    os.environ["GITHUB_ENV"] = p
    try:
        result = fn()
        return result, Path(p).read_text(encoding="utf-8")
    finally:
        if old is None:
            os.environ.pop("GITHUB_ENV", None)
        else:
            os.environ["GITHUB_ENV"] = old
        os.unlink(p)


def make_project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "project.godot").write_text("", encoding="utf-8")
    (root / "export_presets.cfg").write_text(
        '[preset.0]\nname="Windows Desktop"\n\n[preset.1]\nname="Linux/X11"\n',
        encoding="utf-8")
    (root / "tpl").mkdir()  # custom_build 默认形态的 templates-dir
    return root


def valid_ns(root: Path, **over) -> Namespace:
    # 密钥走固定名 env，不再作为 argv/参数传递；cmd_validate_inputs 直接读 env
    os.environ.setdefault("SCRIPT_AES256_ENCRYPTION_KEY", "a" * 64)
    ns = Namespace(project_path=str(root), export_type="custom_build",
                    editor_version="4.7.2-stable", templates_dir=str(root / "tpl"),
                    export_presets_path="export_presets.cfg",
                    project_godot="project.godot", dist_dir="bin")
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


@check("validate-inputs 通过：写 4 个 GITHUB_ENV")
def t_validate_ok():
    with tempfile.TemporaryDirectory() as td:
        root = make_project(Path(td) / "proj")
        code, content = with_github_env(lambda: es.cmd_validate_inputs(valid_ns(root)))
        assert code == 0
        for key in ("SOURCE_PRESETS=", "EXPORT_PRESETS_REL=export_presets.cfg",
                    "PROJECT_GODOT_REL=project.godot", "DIST_REL=bin"):
            assert key in content, content


@check("validate-inputs 拒绝矩阵")
def t_validate_rejects():
    with tempfile.TemporaryDirectory() as td:
        root = make_project(Path(td) / "proj")
        cases = [
            (valid_ns(root, export_presets_path="/abs/cfg"), "presets-path 绝对路径"),
            (valid_ns(root, dist_dir=r"C:\bin"), "dist-dir Windows 盘符"),
            (valid_ns(root, dist_dir=""), "dist-dir 为空"),
            (valid_ns(root, editor_version="4.7 bad"), "版本含非法字符"),
            (valid_ns(root, export_type="custom_build", templates_dir=str(Path(td) / "nope")),
             "custom_build 缺 templates-dir"),
            (valid_ns(Path(td) / "noproj"), "项目目录不存在"),
            (valid_ns(root, export_presets_path="missing.cfg"), "预设文件不存在"),
        ]
        for ns, why in cases:
            expect_exit(lambda: es.cmd_validate_inputs(ns), why)
        # 密钥长度错：走 env，单独设置后校验
        old = os.environ.get("SCRIPT_AES256_ENCRYPTION_KEY")
        try:
            os.environ["SCRIPT_AES256_ENCRYPTION_KEY"] = "a" * 63
            expect_exit(lambda: es.cmd_validate_inputs(valid_ns(root)), "密钥长度错")
        finally:
            if old is None:
                os.environ.pop("SCRIPT_AES256_ENCRYPTION_KEY", None)
            else:
                os.environ["SCRIPT_AES256_ENCRYPTION_KEY"] = old


@check("resolve-presets：显式透传；空 presets = fail-fast（不做自动解析）")
def t_resolve_presets():
    with tempfile.TemporaryDirectory() as td:
        root = make_project(Path(td) / "proj")
        cfg = str(root / "export_presets.cfg")
        code, content = with_github_env(
            lambda: es.cmd_resolve_presets(Namespace(presets="My Preset")))
        assert code == 0 and "PRESET_LIST=My Preset\n" in content, content
        expect_exit(lambda: es.cmd_resolve_presets(Namespace(presets="")),
                    "空 presets 未拒绝（预设名单唯一实现在 resolve，直调传空必须显式失败）")


@check("prepare-templates 版本目录：patch=0 重建 / rc 剥离 / patch≠0 / hash 段剥离")
def t_derive_template_version():
    info_stable0 = {"major": "4", "minor": "8", "patch": "0",
                    "status": "stable", "build": "official"}
    assert es.derive_template_version_dir(
        "4.8.stable.official.123456789", info_stable0) == "4.8.stable", \
        "patch=0（首发）时目录名须为 major.minor.status"
    assert es.derive_template_version_dir(
        "4.8.rc1.official.abcdef123", {**info_stable0, "status": "rc1"}) == "4.8.rc1", \
        "rc 状态段不得被截断"
    assert es.derive_template_version_dir(
        "4.7.2.stable.official.111111111",
        {"major": "4", "minor": "7", "patch": "2", "status": "stable", "build": "official"},
        ) == "4.7.2.stable", "patch≠0 须保留"
    # 无 hash 段（本地构建无 VERSION_HASH）同样可剥
    assert es.derive_template_version_dir(
        "4.8.stable.official", info_stable0) == "4.8.stable"


@check("prepare-templates 版本目录：剥离值与重建值不等 → fail-fast（策略漂移双向暴露）")
def t_derive_template_version_mismatch():
    # 引擎把 patch=0 写进输出（version.h 策略漂移）→ 剥离值带 .0，重建值不带 → 红
    expect_exit(lambda: es.derive_template_version_dir(
        "4.8.0.stable.official.111111111",
        {"major": "4", "minor": "8", "patch": "0", "status": "stable", "build": "official"}),
        "patch 策略漂移未被拒绝")
    # --version 与 get_version_info 版本不符（探针取错编辑器）→ 红
    expect_exit(lambda: es.derive_template_version_dir(
        "4.9.stable.official.111111111",
        {"major": "4", "minor": "8", "patch": "0", "status": "stable", "build": "official"}),
        "版本不符未被拒绝")


def _patch_run_editor(monkey):
    """替换 es.run_editor，按预设名返回退出码并记录调用。"""
    calls = []

    def fake(editor, editor_args, encryption_key, export_type):
        calls.append({"args": editor_args, "key": encryption_key, "type": export_type})
        preset = editor_args[-1]
        return 0 if monkey.get(preset, 0) == 0 else 1

    orig = es.run_editor
    es.run_editor = fake
    return calls, orig


@check("export-presets：全成功退出 0，trim 与 flag 正确")
def t_export_all_ok():
    with tempfile.TemporaryDirectory() as td:
        root = make_project(Path(td) / "proj")
        (root / "bin").mkdir()
        calls, orig = _patch_run_editor({})
        old_editor = os.environ.get("EDITOR")
        os.environ["EDITOR"] = "fake-godot"
        try:
            code = es.cmd_export_presets(Namespace(
                project_path=str(root), target="template_release", export_type="official",
                encryption_key="", preset_list="Windows Desktop, Linux/X11 ,", dist_dir="bin"))
            assert code == 0
            assert [c["args"][-1] for c in calls] == ["Windows Desktop", "Linux/X11"], calls
            assert all("--export-release" in c["args"] for c in calls)
        finally:
            es.run_editor = orig
            if old_editor is None:
                os.environ.pop("EDITOR", None)
            else:
                os.environ["EDITOR"] = old_editor


@check("export-presets：单预设失败跑完其余，末尾退出非零")
def t_export_partial_fail():
    with tempfile.TemporaryDirectory() as td:
        root = make_project(Path(td) / "proj")
        (root / "bin").mkdir()
        calls, orig = _patch_run_editor({"Linux/X11": 1})
        old_editor = os.environ.get("EDITOR")
        os.environ["EDITOR"] = "fake-godot"
        try:
            expect_exit(lambda: es.cmd_export_presets(Namespace(
                project_path=str(root), target="template_debug", export_type="official",
                encryption_key="", preset_list="Windows Desktop,Linux/X11", dist_dir="bin")),
                "预设失败未导致非零退出")
            assert len(calls) == 2, "失败后未继续跑其余预设"
            assert all("--export-debug" in c["args"] for c in calls)
        finally:
            es.run_editor = orig
            if old_editor is None:
                os.environ.pop("EDITOR", None)
            else:
                os.environ["EDITOR"] = old_editor


@check("run_editor：custom_build + 密钥 → 注入进程级 env；official 不注入")
def t_run_editor_env():
    captured = {}
    real_run = es.subprocess.run

    def fake_run(cmd, env=None, **kw):
        captured["cmd"] = cmd
        captured["env"] = env
        return type("R", (), {"returncode": 0})()

    es.subprocess.run = fake_run
    try:
        es.run_editor("ed", ["--headless"], encryption_key="k" * 64, export_type="custom_build")
        assert captured["env"].get("GODOT_SCRIPT_ENCRYPTION_KEY") == "k" * 64
        es.run_editor("ed", ["--headless"], encryption_key="k" * 64, export_type="official")
        assert "GODOT_SCRIPT_ENCRYPTION_KEY" not in captured["env"]
    finally:
        es.subprocess.run = real_run


@check("import-assets 成功 → 退出 0；--import 命令形态正确")
def t_import_assets_ok():
    real_run = es.subprocess.run
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    es.subprocess.run = fake_run
    old = os.environ.get("EDITOR")
    os.environ["EDITOR"] = "fake-godot"
    try:
        assert es.cmd_import_assets(Namespace(project_path="proj")) == 0
        assert captured["cmd"][1:] == ["--headless", "--path", "proj", "--import"], captured
    finally:
        es.subprocess.run = real_run
        if old is None:
            os.environ.pop("EDITOR", None)
        else:
            os.environ["EDITOR"] = old


@check("import-assets 失败 → fail-fast（不再 || true 吞掉）")
def t_import_assets_fail():
    real_run = es.subprocess.run

    def fake_run(cmd, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "bad resource"})()

    es.subprocess.run = fake_run
    old = os.environ.get("EDITOR")
    os.environ["EDITOR"] = "fake-godot"
    try:
        expect_exit(lambda: es.cmd_import_assets(Namespace(project_path="proj")),
                    "--import 失败未导致非零退出")
    finally:
        es.subprocess.run = real_run
        if old is None:
            os.environ.pop("EDITOR", None)
        else:
            os.environ["EDITOR"] = old


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
