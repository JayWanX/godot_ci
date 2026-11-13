#!/usr/bin/env python3
"""ensure_standard_includes.py 自测：平台域映射与规则适用判据。

覆盖：
- 缺 --platform → fail-fast（编译目标必须显式，不用宿主 OS 推断）
- 契约平台名映射：windows → windows 规则域，其余（linux 等）→ posix 域
- dry-run 端到端：缺 <memory> 的翻译单元被报告补丁且不落盘

用法：python .github/tests/test_ensure_standard_includes.py
"""
import subprocess
import os
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "actions" / "build-pipeline" / "scripts" / "ensure_standard_includes.py"

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def run(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(root), env=dict(os.environ, PYTHONIOENCODING="utf-8"))


@check("缺 --platform → fail-fast")
def t_missing_platform():
    with tempfile.TemporaryDirectory() as td:
        proc = run(Path(td))
        assert proc.returncode == 1
        # 统一 fail() 输出到 stderr（GitHub 对两个流同样解析 ::error::）
        assert "--platform" in (proc.stdout + proc.stderr)


@check("dry-run：缺 <memory> 被报告补丁且文件不落盘")
def t_memory_dryrun():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = root / "a.cpp"
        # 无 include 行的文件会被跳过（找不到插入点），样例须含 include
        f.write_text('#include "util.h"\nstd::unique_ptr<int> p;\n', encoding="utf-8")
        proc = run(root, "--dry-run", "--platform", "linux")
        assert proc.returncode == 0, proc.stderr
        assert "<memory>" in proc.stdout, proc.stdout
        assert "DRY-RUN" in proc.stdout
        assert f.read_text(encoding="utf-8") == '#include "util.h"\nstd::unique_ptr<int> p;\n', "dry-run 落盘了"


@check("posix 规则不进 windows 目标：pthread 符号仅 posix 域触发")
def t_pthread_domain():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = root / "p.c"
        f.write_text("#include <string.h>\npthread_t t;\n", encoding="utf-8")
        posix_run = run(root, "--dry-run", "--platform", "linux")
        win_run = run(root, "--dry-run", "--platform", "windows")
        assert posix_run.returncode == 0 and win_run.returncode == 0
        assert "<pthread.h>" in posix_run.stdout, posix_run.stdout
        assert "pthread.h" not in win_run.stdout, win_run.stdout


@check("windows 规则不进 posix 目标：SAL 注解仅 windows 域触发")
def t_sal_domain():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = root / "s.cpp"
        f.write_text("#include <windows.h>\nvoid g(_In_opt_count_(n) int *p);\n", encoding="utf-8")
        win_run = run(root, "--dry-run", "--platform", "windows")
        posix_run = run(root, "--dry-run", "--platform", "linux")
        assert win_run.returncode == 0 and posix_run.returncode == 0
        assert "<sal.h>" in win_run.stdout, win_run.stdout
        assert "sal.h" not in posix_run.stdout, posix_run.stdout


@check("--platform= 连写形式与空格形式等价")
def t_platform_equals_form():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "p.c").write_text("#include <string.h>\npthread_t t;\n", encoding="utf-8")
        spaced = run(root, "--dry-run", "--platform", "linux")
        joined = run(root, "--dry-run", "--platform=linux")
        assert joined.returncode == 0, joined.stderr
        assert "<pthread.h>" in joined.stdout, joined.stdout
        assert joined.stdout == spaced.stdout, "两种写法结果不一致"
        # 空值：连写但不给值 → 明确报用法，不静默当成缺平台
        empty = run(root, "--dry-run", "--platform=")
        assert empty.returncode == 1
        assert "--platform" in (empty.stdout + empty.stderr)


@check("契约平台白名单外的取值 → fail-fast（不静默退化成 posix 域）")
def t_platform_whitelist():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "s.cpp").write_text("#include <windows.h>\nvoid g(_In_ int *p);\n",
                                    encoding="utf-8")
        for bad in ("Linux", "linuxbsd", "win32", "osx"):
            proc = run(root, "--dry-run", "--platform", bad)
            assert proc.returncode == 1, (bad, proc.returncode, proc.stdout)
            assert "不是契约平台名" in (proc.stdout + proc.stderr), proc.stdout


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