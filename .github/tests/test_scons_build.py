#!/usr/bin/env python3
"""scons_build.py 自测：编译命令装配判据。

覆盖：
- 平台名映射（linux → linuxbsd，唯一映射点）
- driver flags 展开（resolve_scons_flags overrides 模式的补传片段）
- scons-flags shlex 拆分（引号保护）
- 条件参数（空路径不传）与固定尾部
- BUILD_NAME（PR 事件）
- --dry-run：输出 JSON 参数列表且不执行 scons

用法：python .github/tests/test_scons_build.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions" / "compile" / "scripts"))
from scons_build import build_args, build_name  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "actions" / "compile" / "scripts" / "scons_build.py"

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def keys(args):
    return [a.split("=", 1)[0] for a in args]


@check("平台名映射：linux → linuxbsd，windows 原样")
def t_platform_map():
    a = build_args("linux", "editor", "", "cache", "", "", "")
    assert "platform=linuxbsd" in a, a
    b = build_args("windows", "editor", "", "cache", "", "", "")
    assert "platform=windows" in b, b


@check("driver flags：非 windows 强制 d3d12=no，位于用户 flags 之前")
def t_driver_flags():
    a = build_args("linux", "editor", "", "cache", "", "", "")
    assert "d3d12=no" in a, a
    # web 平台 get_flags() 会覆盖 profile 同名键：resolve 补传 profile 显式写的值
    # （用户要 yes 就把 yes 补到命令行压住平台覆盖），而非平台默认值
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profile.py"
        p.write_text('builtin_pcre2_with_jit = "yes"\n', encoding="utf-8")
        b = build_args("web", "template_release", "", "cache", "", str(p), "")
        assert "builtin_pcre2_with_jit=yes" in b, b


@check("scons-flags 经 shlex 拆分（引号保护），位于 driver 片段之后")
def t_scons_flags_split():
    a = build_args("linux", "editor", 'x=1 use_volk="yes space"', "cache", "", "", "")
    assert "use_volk=yes space" in a, a
    assert a.index("d3d12=no") < a.index("x=1"), a


@check("条件参数：空路径不传，尾部固定 cache_path + redirect_build_objects")
def t_conditional_and_tail():
    a = build_args("linux", "editor", "", "mycache", "", "", "")
    assert keys(a)[-2:] == ["cache_path", "redirect_build_objects"], a
    assert "cache_path=mycache" in a and a[-1] == "redirect_build_objects=no"
    assert "build_profile=" not in " ".join(a) and "profile=" not in " ".join(a)
    # 显式传入的 profile 读不到即 fail-fast，故此处用真实文件
    with tempfile.TemporaryDirectory() as td:
        b_profile = Path(td) / "b.gdbuild"
        b_profile.write_text("", encoding="utf-8")
        p = Path(td) / "p.py"
        p.write_text("d3d12 = \"no\"\n", encoding="utf-8")
        b = build_args("linux", "editor", "", "c", str(b_profile), str(p), "mods")
        for token in (f"build_profile={b_profile}", f"profile={p}", "custom_modules=mods"):
            assert token in b, b


@check("BUILD_NAME：PR 事件 gh-<n>，否则 gh")
def t_build_name():
    old = os.environ.pop("PR_NUMBER", None)
    try:
        assert build_name() == "gh"
        os.environ["PR_NUMBER"] = "42"
        assert build_name() == "gh-42"
    finally:
        if old is None:
            os.environ.pop("PR_NUMBER", None)
        else:
            os.environ["PR_NUMBER"] = old


@check("非法 --platform / --target 由 argparse 拒绝（不抛裸 KeyError）")
def t_invalid_platform():
    with tempfile.TemporaryDirectory() as td:
        for bad in ("plan9", "Linux", "linuxbsd"):
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--platform", bad, "--target", "editor",
                 "--scons-cache", "c", "--dry-run"],
                capture_output=True, text=True, encoding="utf-8", cwd=td,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            assert proc.returncode == 2, (bad, proc.returncode)
            assert "invalid choice" in (proc.stdout + proc.stderr), proc.stderr
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--platform", "linux", "--target", "bogus",
             "--scons-cache", "c", "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", cwd=td,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert proc.returncode == 2 and "invalid choice" in proc.stderr, proc.stderr


@check("CWD 不是引擎源码根 → 显式失败（不静默编译到别处）")
def t_wrong_root():
    with tempfile.TemporaryDirectory() as td:
        # 非 dry-run：先做根目录断言，缺 SConstruct 即失败（不会真的调 scons）
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--platform", "linux", "--target", "editor",
             "--scons-cache", "c"],
            capture_output=True, text=True, encoding="utf-8", cwd=td,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
        assert "不是引擎源码根" in proc.stderr or "SConstruct" in proc.stderr, proc.stderr


@check("--dry-run：输出 JSON 参数且不执行 scons")
def t_dry_run():
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--platform", "linux", "--target", "editor",
             "--scons-cache", "c", "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", cwd=td, check=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        # 末行是 JSON 参数数组；执行到 dry-run 即退出（无 scons 可执行也不报错）
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload[0] == "platform=linuxbsd", payload
        assert payload[-2:] == ["cache_path=c", "redirect_build_objects=no"], payload


@check("CLI 旗标：--profile-path / --modules-dir 装配为 profile= / custom_modules=")
def t_cli_path_flags():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        profile = root / "p.py"
        profile.write_text("d3d12 = \"no\"\n", encoding="utf-8")
        gdbuild = root / "b.gdbuild"
        gdbuild.write_text("", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--platform", "linux", "--target", "editor",
             "--scons-cache", "c", "--build-profile-path", str(gdbuild),
             "--profile-path", str(profile), "--modules-dir", "mods", "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", cwd=td, check=True,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        for token in (f"build_profile={gdbuild}", f"profile={profile}",
                      "custom_modules=mods"):
            assert token in payload, payload


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