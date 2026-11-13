#!/usr/bin/env python3
"""resolve_toolchain.py 自测：toolchain-json 解析、格式校验、缺失 fail-fast。

用法：python .github/tests/test_resolve_toolchain.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "actions" / "build-pipeline" / "scripts" / "resolve_toolchain.py"

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def run(toolchain_json, workdir):
    fd, out = tempfile.mkstemp()
    os.close(fd)
    env = dict(os.environ, GITHUB_OUTPUT=out)
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--toolchain-json", toolchain_json],
            capture_output=True, text=True, encoding="utf-8", env=dict(env, PYTHONIOENCODING="utf-8"), cwd=workdir)
        content = Path(out).read_text(encoding="utf-8")
        return proc, content
    finally:
        os.unlink(out)


@check("合法 toolchain-json：三行输出")
def t_valid_json():
    with tempfile.TemporaryDirectory() as td:
        proc, out = run('{"emsdk": "3.1.50", "java": "17", "scons": "4.0.0"}', td)
        assert proc.returncode == 0, proc.stderr
        assert out == "emsdk=3.1.50\njava=17\nscons=4.0.0\n", out


@check("空输入 → fail-fast（toolchain-json 由 resolve 归一化必供）")
def t_empty_fail():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run("", td)
        assert proc.returncode == 1
        assert "toolchain-json 为空" in proc.stderr


@check("非法 JSON → fail-fast")
def t_invalid_json():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run("not json", td)
        assert proc.returncode == 1
        assert "toolchain-json 非法" in proc.stderr


@check("emsdk/java 缺失 → fail-fast")
def t_missing_keys():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run('{"emsdk": ""}', td)
        assert proc.returncode == 1
        assert "工具链版本缺失" in proc.stderr and "java" in proc.stderr


@check("scons 缺失 → fail-fast（否则 pip install \"scons==\" 报错不指向根因）")
def t_missing_scons():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run('{"emsdk": "4.0.11", "java": "17"}', td)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "scons" in proc.stderr, proc.stderr
        # 空白值同样视为缺失（空串会被拼成 `scons==`）
        proc, _ = run('{"emsdk": "4.0.11", "java": "17", "scons": "  "}', td)
        assert proc.returncode == 1 and "scons" in proc.stderr, proc.stderr


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