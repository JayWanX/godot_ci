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


def linux_case(name: str, platform_value: str, template_name: str) -> bool:
    """Linux 预设的 platform 值必须能在 TEMPLATE_PATTERNS 里命中。

    **真实故障**：预设文件写 `platform="Linux/X11"`（Godot 编辑器导出的原值），而
    `TEMPLATE_PATTERNS` 曾只登记 `"Linux"`，于是 find_template 返回空串、verify 报
    「平台 'Linux/X11' 无模板模式」，整个导出作业在「清洗导出配置」步骤就失败。
    引擎内部把 `Linux/X11` 归一成 `Linux` 只是内部行为，预设文件里的原值不会被改写。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        root.mkdir()
        (root / "templates").mkdir()
        (root / "templates" / template_name).write_text("")
        (root / "export_presets.cfg").write_text(
            f'[preset.0]\nname="Linux/X11"\nplatform="{platform_value}"\n'
            f'export_path="bin/game"\n', encoding="utf-8")
        (root / "project.godot").write_text(
            'config_version=5\n\n[rendering]\n\n', encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(PREPARE),
             "--cfg", str(root / "export_presets.cfg"),
             "--export-type", "custom_build",
             "--templates-dir", str(root / "templates"),
             "--project-root", str(root),
             "--presets", "Linux/X11",
             "--platforms", "linux",
             "--dist", "bin",
             "--project-godot", "project.godot"],
            capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        ok = proc.returncode == 0
        print(f"{'OK  ' if ok else 'FAIL'} {name}（platform={platform_value}）")
        if not ok:
            print(proc.stdout, proc.stderr)
            return False
        # 命中还必须真把模板路径写进预设，否则「放行」是空的
        cfg = (root / "export_presets.cfg").read_text(encoding="utf-8")
        wrote = "custom_template/release=" in cfg and template_name in cfg
        print(f"{'OK  ' if wrote else 'FAIL'} {name}：模板路径已写入预设")
        return wrote


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
    # Linux 预设的 platform 原值必须命中模板模式（真实故障回归）
    linux_ok = [
        linux_case("Linux/X11 能命中模板模式", "Linux/X11", "godot.linuxbsd.template_release.x86_64"),
        linux_case("Linux（旧写法）也能命中", "Linux", "godot.linuxbsd.template_release.x86_64"),
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

    passed = (sum(1 for r in results if r) + (1 if dirs_ok else 0)
              + sum(1 for r in etc2_ok if r) + sum(1 for r in linux_ok if r))
    total = len(results) + 1 + len(etc2_ok) + len(linux_ok)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())