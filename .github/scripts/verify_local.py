#!/usr/bin/env python3
"""本地一键预检：静态校验 + 全部自测 + 模板打包 dry-run（零第三方依赖、不联网）。

用途：在干净环境里一条命令得到与 CI Self-check 等价的结论，不必逐个跑套件、
也不必推提交等 CI。

不跑「契约校验」（`godot_context.py --check`）：它要求当前目录有
`.github/ci-contract.json`，而 CI 资产仓库本身没有契约（契约在项目仓库）。需要校验
契约时在**项目仓库根**单独执行那条命令。

用法：
  python .github/scripts/verify_local.py            # 全量
  python .github/scripts/verify_local.py --quick    # 只跑静态校验（秒级）
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / ".github" / "scripts"
TESTS = REPO / ".github" / "tests"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jsonc import ensure_utf8_stdio  # noqa: E402


def _run(label: str, cmd, cwd=None) -> bool:
    """跑一条命令，成功只打一行摘要；失败回显完整输出。"""
    proc = subprocess.run(cmd, cwd=str(cwd or REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    ok = proc.returncode == 0
    if ok:
        tail = (proc.stdout.strip().splitlines() or [""])[-1]
        print(f"✓ {label} — {tail}")
    else:
        print(f"✗ {label}（exit={proc.returncode}）")
        print((proc.stdout + proc.stderr).rstrip())
    return ok


def check_pyyaml() -> bool:
    """check_ci_assets.py 依赖 PyYAML；缺了给明确提示而不是让静态校验报 ImportError。"""
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("✗ 缺 PyYAML：静态校验需要它（其余自测零依赖）")
        print("   安装：python -m pip install pyyaml")
        return False
    return True


def static_check() -> bool:
    return _run("静态校验", [sys.executable, str(SCRIPTS / "check_ci_assets.py")])


def suites() -> bool:
    """逐套件跑；任一失败即整体失败，但会把所有失败都列全。"""
    test_files = sorted(TESTS.glob("test_*.py"))
    if not test_files:
        print("✗ 未找到任何测试套件")
        return False
    failed = []
    for f in test_files:
        proc = subprocess.run([sys.executable, str(f)], cwd=str(REPO),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace",
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        if proc.returncode != 0:
            failed.append(f.name)
            print(f"✗ {f.name}（exit={proc.returncode}）")
            print((proc.stdout + proc.stderr).rstrip())
    total = len(test_files)
    if failed:
        print(f"✗ 自测：{total - len(failed)}/{total} 通过，失败 {failed}")
        return False
    print(f"✓ 自测 — {total}/{total} 套件通过")
    return True


def template_dryrun() -> bool:
    """模板打包 dry-run：只校验必需文件与占位符，不写产物、不联网。"""
    return _run("模板打包校验", [sys.executable, str(SCRIPTS / "build_template_bundle.py"),
                                 "--check"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="只跑静态校验")
    args = ap.parse_args()

    print(f"仓库根：{REPO}")
    if not check_pyyaml():
        return 1
    results = [static_check()]
    if not args.quick:
        results.append(suites())
        results.append(template_dryrun())

    ok = all(results)
    print("\n" + ("全部通过" if ok else "存在失败项（详见上方输出）"))
    return 0 if ok else 1


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
