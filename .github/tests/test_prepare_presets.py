#!/usr/bin/env python3
"""prepare_presets.py 端到端自测：预设子目录化与 safe 名冲突分层。

覆盖：
- 两预设同 basename → 各落子目录，export_path 互不覆盖
- safe 名冲突（均选中）→ fail-fast
- safe 名冲突（仅一个选中）→ 不误伤
- 子目录真实创建

用法：python .github/tests/test_prepare_presets.py
"""
import subprocess
import os
import sys
import tempfile
from pathlib import Path

PREPARE = Path(__file__).resolve().parents[1] / "actions" / "export" / "scripts" / "prepare_presets.py"
CFG_TEMPLATE = """[preset.0]
name="Windows Desktop"
platform="Windows Desktop"
export_path="bin/game.exe"

[preset.1]
name="{other}"
platform="Windows Desktop"
export_path="bin/game.exe"
"""


def run_prepare(root: Path, presets: str, platforms: str = "windows") -> subprocess.CompletedProcess:
    (root / "templates").mkdir(exist_ok=True)
    (root / "templates" / "godot.windows.template_release.exe").write_text("")
    (root / "export_presets.cfg").write_text(
        CFG_TEMPLATE.format(other=root.name), encoding="utf-8")
    (root / "project.godot").write_text(
        'config_version=5\n\n[rendering]\n\n', encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(PREPARE),
         "--cfg", str(root / "export_presets.cfg"),
         "--export-type", "custom_build",
         "--templates-dir", str(root / "templates"),
         "--project-root", str(root),
         "--presets", presets,
         "--platforms", platforms,
         "--dist", "bin",
         "--project-godot", "project.godot"],
        capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def case(name, other, presets, expect_ok, cfg_needles=(), out_needles=()):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / other
        root.mkdir()
        proc = run_prepare(root, presets)
        ok = proc.returncode == 0
        status = "OK  " if ok == expect_ok else "FAIL"
        print(f"{status} {name}")
        if ok != expect_ok:
            print(proc.stdout, proc.stderr)
            return False
        hay = proc.stdout + proc.stderr
        for needle in out_needles:
            if needle not in hay:
                print(f"     输出缺少: {needle!r}\n{hay}")
                return False
        if ok:
            cfg = (root / "export_presets.cfg").read_text(encoding="utf-8")
            for needle in cfg_needles:
                if needle not in cfg:
                    print(f"     cfg 缺少: {needle!r}\n{cfg}")
                    return False
    return True


def etc2_case(name: str, platforms: str) -> bool:
    """ETC2/ASTC 开关：命中平台集合才写入 project.godot。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Web"
        root.mkdir()
        proc = run_prepare(root, "Windows Desktop", platforms=platforms)
        if proc.returncode != 0:
            print(f"FAIL {name}：{proc.stdout}{proc.stderr}")
            return False
        text = (root / "project.godot").read_text(encoding="utf-8")
        expect = any(p in {"macos", "ios", "visionos", "android", "web"}
                     for p in platforms.split(","))
        enabled = "import_etc2_astc=true" in text
        ok = enabled == expect
        print(f"{'OK  ' if ok else 'FAIL'} {name}（platforms={platforms}，写入={enabled}）")
        if not ok:
            print(text)
        return ok


def main() -> int:
    results = [
        case("正常：两预设同 basename 落各自子目录",
             "Windows Server", "Windows Desktop,Windows Server", True,
             cfg_needles=["bin/windows_desktop/game.exe", "bin/windows_server/game.exe"]),
        case("冲突（均选中）→ fail-fast",
             "windows_desktop", "Windows Desktop,windows_desktop", False,
             out_needles=["safe 名冲突"]),
        case("冲突（仅一个选中）→ 不误伤",
             "windows_desktop", "Windows Desktop", True,
             cfg_needles=["bin/windows_desktop/game.exe"]),
    ]
    # ETC2/ASTC：Web 平台无差别开启（不区分预设是否 for mobile），桌面平台不写
    etc2_ok = [
        etc2_case("Web 平台开启 ETC2/ASTC", "web"),
        etc2_case("Android 平台开启 ETC2/ASTC", "android"),
        etc2_case("Windows 平台不写 ETC2/ASTC", "windows"),
        etc2_case("混合平台（含 web）开启", "windows,web"),
    ]

    # 单独核验子目录创建（只查目录，不查输出）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Windows Server"
        root.mkdir()
        run_prepare(root, "Windows Desktop,Windows Server")
        dirs = sorted(p.name for p in (root / "bin").iterdir() if p.is_dir())
        dirs_ok = dirs == ["windows_desktop", "windows_server"]
        print(f"{'OK  ' if dirs_ok else 'FAIL'} 子目录创建：{dirs}")

    passed = sum(1 for r in results if r) + (1 if dirs_ok else 0) + sum(1 for r in etc2_ok if r)
    total = len(results) + 1 + len(etc2_ok)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())