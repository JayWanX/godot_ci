#!/usr/bin/env python3
"""prune_release_assets.py 自测：Release 陈旧附件的删除判据。

核心判据（宁可漏删，不可误删）：
- 完整性门控：本轮未覆盖项目全部预设 → 一律不删
- 家族限定：只处理 <prefix>_ 开头的附件
- 期望集合：本次上传的文件名永不删
- 家族判定失效（产物名不以 prefix_ 开头）→ 不删

用法：python .github/tests/test_prune_release_assets.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prune_release_assets import select_stale  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@check("完整导出：删同家族陈旧附件，保留本次产物")
def t_prune_basic():
    existing = [
        "demo_windows.zip",              # 旧命名（11-07 遗留）
        "demo_macos.zip",
        "demo_windows_windows_desktop.zip",   # 旧命名（平台重复）
        "demo_windows_desktop.zip",      # 本次产物
        "demo_macos_embed_python.zip",   # 本次产物
    ]
    expected = ["demo_windows_desktop.zip", "demo_macos_embed_python.zip"]
    stale, reason = select_stale(existing, expected, "demo", True)
    assert reason == "", reason
    assert stale == ["demo_windows.zip", "demo_macos.zip",
                     "demo_windows_windows_desktop.zip"], stale


@check("不完整导出：一律不删（其余附件是上一轮有效产物）")
def t_incomplete_skips():
    existing = ["demo_windows_desktop.zip", "demo_linux_x11.zip"]
    stale, reason = select_stale(existing, ["demo_windows_desktop.zip"], "demo", False)
    assert stale == [], stale
    assert "未覆盖" in reason, reason


@check("家族限定：非 prefix_ 开头的附件永不触碰")
def t_family_only():
    existing = [
        "demo_windows_desktop.zip",   # 本次
        "README.md",                  # 手工说明文件
        "other_project_v1.zip",       # 别的项目
        "demo2_extra.zip",            # 另一家族
        "demo_legacy.zip",            # 本家族旧件
    ]
    stale, reason = select_stale(existing, ["demo_windows_desktop.zip"], "demo", True)
    assert reason == "", reason
    assert stale == ["demo_legacy.zip"], stale


@check("前缀为空：不猜家族，跳过清理")
def t_no_prefix():
    stale, reason = select_stale(["a.zip", "b.zip"], ["a.zip"], "", True)
    assert stale == [], stale
    assert "前缀" in reason, reason


@check("家族判定失效：本次产物不以 prefix_ 开头则不删（export_naming 缺 {prefix}）")
def t_family_mismatch():
    existing = ["demo_old.zip", "windows_desktop.zip"]
    stale, reason = select_stale(existing, ["windows_desktop.zip"], "demo", True)
    assert stale == [], stale
    assert "家族判定失效" in reason, reason


@check("无陈旧附件：返回空且无跳过原因")
def t_nothing_stale():
    existing = ["demo_windows_desktop.zip"]
    stale, reason = select_stale(existing, ["demo_windows_desktop.zip"], "demo", True)
    assert stale == [] and reason == "", (stale, reason)


@check("期望集合为空：绝不删任何家族成员（ 空集守卫，跳过并给原因）")
def t_empty_expected_skips():
    # 旧实现会把「家族全部 − 空集」当陈旧附件全删；空集只可能是上游收集步骤异常
    # （目录名变更 / 失败未阻断），此时清空历史交付物是静默灾难，必须 fail-safe。
    existing = ["demo_windows.zip", "demo_linux.zip", "demo_macos.zip"]
    stale, reason = select_stale(existing, [], "demo", True)
    assert stale == [], stale
    assert "为空" in reason, reason
    assert "拒绝清空" in reason or "无法判定" in reason, reason


@check("期望集合为空 + 不完整导出：仍跳过且原因明确（跳过判据优先）")
def t_empty_expected_incomplete():
    stale, reason = select_stale(["demo_old.zip"], [], "demo", False)
    assert stale == [], stale
    assert reason, reason


@check("CLI --existing-json --dry-run：待删列表可判定且不执行删除")
def t_cli_dry_run():
    script = Path(__file__).resolve().parents[1] / "scripts" / "prune_release_assets.py"
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "demo_windows_desktop.zip").write_text("x", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script), "--repo", "o/r", "--tag", "v1",
             "--prefix", "demo", "--expected", str(d), "--complete", "true",
             "--dry-run", "--existing-json",
             json.dumps(["demo_windows_desktop.zip", "demo_old.zip"])],
            capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"), check=True)
        assert "demo_old.zip" in proc.stdout, proc.stdout
        assert "demo_windows_desktop.zip\n" not in proc.stdout, proc.stdout
        assert "dry-run" in proc.stdout, proc.stdout


@check("CLI 空期望集 + dry-run：打印跳过原因、不产生待删列表、退出 0")
def t_cli_empty_expected():
    script = Path(__file__).resolve().parents[1] / "scripts" / "prune_release_assets.py"
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "assets"     # 期望目录存在但为空（或干脆不存在，见下）
        empty.mkdir()
        proc = subprocess.run(
            [sys.executable, str(script), "--repo", "o/r", "--tag", "v1",
             "--prefix", "demo", "--expected", str(empty), "--complete", "true",
             "--dry-run", "--existing-json",
             json.dumps(["demo_windows.zip", "demo_linux.zip"])],
            capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert proc.returncode == 0, proc.stderr
        assert "跳过清理" in proc.stdout, proc.stdout
        assert "为空" in proc.stdout, proc.stdout
        assert "--- 待清理附件 ---" not in proc.stdout, proc.stdout
        # 期望目录缺失（上游失败的最坏形态）同样不得产生删除列表
        proc2 = subprocess.run(
            [sys.executable, str(script), "--repo", "o/r", "--tag", "v1",
             "--prefix", "demo", "--expected", str(Path(td) / "not-there"),
             "--complete", "true", "--dry-run", "--existing-json",
             json.dumps(["demo_windows.zip", "demo_linux.zip"])],
            capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert proc2.returncode == 0, proc2.stderr
        assert "跳过清理" in proc2.stdout and "为空" in proc2.stdout, proc2.stdout
        assert "--- 待清理附件 ---" not in proc2.stdout, proc2.stdout


@check("CLI 不完整导出：打印跳过原因且退出 0")
def t_cli_incomplete():
    script = Path(__file__).resolve().parents[1] / "scripts" / "prune_release_assets.py"
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [sys.executable, str(script), "--repo", "o/r", "--tag", "v1",
             "--prefix", "demo", "--expected", td, "--complete", "false",
             "--existing-json", '["demo_old.zip"]'],
            capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert proc.returncode == 0, proc.stderr
        assert "跳过清理" in proc.stdout, proc.stdout


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
