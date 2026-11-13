#!/usr/bin/env python3
"""resolve_scons_flags.py 判据矩阵：验证 detect 与 overrides 同源、无分裂。"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

spec = importlib.util.spec_from_file_location("rsf", SCRIPTS / "resolve_scons_flags.py")
rsf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rsf)


def run_cli(platform, profile_text, scons_flags, mode):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "profile.py"
        p.write_text(profile_text, encoding="utf-8")
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "resolve_scons_flags.py"),
             "--platform", platform, "--profile", str(p), "--scons-flags", scons_flags, "--mode", mode],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        return out.stdout.strip()


def sdk(profile_text, scons_flags, platform="windows"):
    """返回 (need-d3d12, overrides 补传片段)。"""
    need = run_cli(platform, profile_text, scons_flags, "detect")
    d3d12 = next(l for l in need.splitlines() if l.startswith("need-d3d12"))
    overrides = run_cli(platform, profile_text, scons_flags, "overrides")
    return d3d12.split("=")[1], overrides


def run_cli_raw(platform, profile, scons_flags, mode="detect"):
    """按给定 profile 路径跑一次（profile 可为不存在路径 / 二进制文件），返回完成结果。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "resolve_scons_flags.py"),
         "--platform", platform, "--profile", profile,
         "--scons-flags", scons_flags, "--mode", mode],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )


CASES = [
    # (名称, profile, scons-flags, platform, 期望 need-d3d12, 期望 overrides 片段)
    ("标准：profile d3d12=no", 'd3d12 = "no"\n', "", "windows", "false", "d3d12=no"),
    ("profile 空", "", "", "windows", "true", ""),
    ("profile d3d12=yes", 'd3d12 = "yes"\n', "", "windows", "true", "d3d12=yes"),
    ("命令行 d3d12=no", 'd3d12 = "no"\n', "d3d12=no", "windows", "false", ""),
    ("命令行 d3d12=true + profile no", 'd3d12 = "no"\n', "d3d12=true", "windows", "true", ""),
    ("命令行 d3d12=1 + profile no", 'd3d12 = "no"\n', "d3d12=1", "windows", "true", ""),
    ("命令行 d3d12=on + profile no", 'd3d12 = "no"\n', "d3d12=on", "windows", "true", ""),
    ("命令行 d3d12=false + profile no", 'd3d12 = "no"\n', "d3d12=false", "windows", "false", ""),
    ("命令行 d3d12=auto + profile no", 'd3d12 = "no"\n', "d3d12=auto", "windows", "true", ""),
    ("单引号", "d3d12 = 'no'\n", "", "windows", "false", "d3d12=no"),
    ("无引号", "d3d12 = no\n", "", "windows", "false", "d3d12=no"),
    ("off", 'd3d12 = "off"\n', "", "windows", "false", "d3d12=no"),
    ("0", 'd3d12 = "0"\n', "", "windows", "false", "d3d12=no"),
    ("false", 'd3d12 = "false"\n', "", "windows", "false", "d3d12=no"),
    ("行尾注释", 'd3d12 = "no"  # 关闭驱动\n', "", "windows", "false", "d3d12=no"),
    ("多次赋值取最后", 'd3d12 = "yes"\nd3d12 = "no"\n', "", "windows", "false", "d3d12=no"),
    ("多次赋值最后是 yes", 'd3d12 = "no"\nd3d12 = "yes"\n', "", "windows", "true", "d3d12=yes"),
    ("大写 key 不被误认", 'D3D12 = "no"\n', "", "windows", "true", ""),
    ("CCFLAGS 含空格", 'd3d12 = "no"\n', 'CCFLAGS="-O2 -g"', "windows", "false", "d3d12=no"),
    ("非 Windows 强制关闭", "", "", "linux", "false", "d3d12=no"),
    ("非 Windows 命令行显式", "", "d3d12=yes", "linux", "true", ""),
    ("macos use_volk 兜底", 'use_volk = "yes"\n', "", "macos", "false", "d3d12=no use_volk=yes"),
    ("macos metal 兜底", 'metal = "no"\n', "", "macos", "false", "d3d12=no metal=no"),
    ("visionos vulkan 兜底", 'vulkan = "yes"\n', "", "visionos", "false", "d3d12=no vulkan=yes"),
    ("web module_raycast 兜底", 'module_raycast_enabled = "yes"\n', "", "web", "false", "d3d12=no module_raycast_enabled=yes"),
]


def main():
    failed = []
    for name, profile, flags, platform, want_need, want_overrides in CASES:
        need, overrides = sdk(profile, flags, platform)
        ok = need == want_need and overrides == want_overrides
        status = "OK  " if ok else "FAIL"
        print(f"{status} {name:32s} need={need:5s}({want_need:5s}) overrides='{overrides}'(exp '{want_overrides}')")
        if not ok:
            failed.append(name)

    # 单一来源断言：同输入下，detect 的 d3d12 与 overrides 片段必须一致（关=no 片段）。
    print("\n--- detect/overrides 一致性（同源断言）---")
    inconsistent = []
    for name, profile, flags, platform, _, _ in CASES:
        need_full = run_cli(platform, profile, flags, "detect")
        d3 = dict(l.split("=", 1) for l in need_full.splitlines())["need-d3d12"]
        overrides = run_cli(platform, profile, flags, "overrides")
        # overrides 含 d3d12=no 当且仅当 detect 判 false（非 Windows / profile 关且命令行未显式）
        has = "d3d12=no" in overrides
        consistent = (d3 == "false") == has or (flags and "d3d12=" in flags)
        if not consistent:
            inconsistent.append(f"{name}: detect={d3} overrides={overrides}")
    if inconsistent:
        # 同源分裂是真缺陷（detect 与 overrides 各判一套会让 SDK 装不上而编译失败），
        # 不能只打 WARN 混在通过输出里
        for item in inconsistent:
            print(f"FAIL 不一致：{item}")
        sys.exit(1)
    print(f"{len(CASES)} 组输入 detect/overrides 判定同源")

    if failed:
        print(f"\n{len(failed)} FAILED: {failed}")
        sys.exit(1)

    # 显式 profile 读不到 → fail-fast（不得静默按空 dict 判定 SDK 需求）
    failures = []
    with tempfile.TemporaryDirectory() as d:
        missing = str(Path(d) / "nope.py")
        proc = run_cli_raw("windows", missing, "")
        if proc.returncode == 0 or "不存在" not in proc.stdout + proc.stderr:
            failures.append(f"不存在的 profile 未 fail-fast：exit={proc.returncode} "
                            f"{proc.stdout + proc.stderr!r}")
        binary = Path(d) / "profile_bin.py"
        binary.write_bytes(b"\xff\xfe\x00\x01")
        proc = run_cli_raw("windows", str(binary), "")
        if proc.returncode == 0 or "UTF-8" not in proc.stdout + proc.stderr:
            failures.append(f"二进制 profile 未 fail-fast：exit={proc.returncode} "
                            f"{proc.stdout + proc.stderr!r}")
        # 未提供（空路径）仍是合法语义：按默认判定，不报错
        proc = run_cli_raw("windows", "", "")
        if proc.returncode != 0:
            failures.append(f"未提供 profile 被误判为错误：{proc.stdout + proc.stderr!r}")
    if failures:
        for f in failures:
            print("FAIL", f)
        sys.exit(1)
    print("显式 profile 读不到 / 非 UTF-8 → fail-fast；未提供仍合法")

    # --platform 必须在契约平台白名单内：表外名字会让 PLATFORM_FLAGS 空转
    bad = run_cli_raw("Linux", "", "")
    if bad.returncode != 2 or "invalid choice" not in bad.stdout + bad.stderr:
        print(f"FAIL 非法 --platform 未被拒绝：exit={bad.returncode} "
              f"{bad.stdout + bad.stderr!r}")
        sys.exit(1)
    print("非法 --platform 由 argparse 拒绝（EXIT=2）")

    print(f"\n全部 {len(CASES)} 组通过")
    return 0


if __name__ == "__main__":
    main()