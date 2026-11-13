#!/usr/bin/env python3
"""normalize_hooks_result.py 自测：hooks-result.json → outputs 的字段映射与兜底。

覆盖：
- 全字段映射（merged-flags / config-digests / source-hash / source-active / post-build / post-export）
- 文件缺失 = 空转兜底（--base-flags、source-active=false、post-build=ok、post-export=ok）
- 值内 CR/LF 剥除（GITHUB_OUTPUT 单行协议）
- --fields 子集输出

用法：python .github/tests/test_normalize_hooks_result.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "normalize_hooks_result.py"

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def run(fields, base_flags="", result=None):
    with tempfile.TemporaryDirectory() as td:
        if result is not None:
            Path(td, "hooks-result.json").write_text(
                json.dumps(result), encoding="utf-8")
        fd, out = tempfile.mkstemp()
        os.close(fd)
        env = dict(os.environ, GITHUB_OUTPUT=out)
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--fields", fields,
                 "--base-flags", base_flags],
                capture_output=True, text=True, encoding="utf-8", env=dict(env, PYTHONIOENCODING="utf-8"), cwd=td, check=True)
            return Path(out).read_text(encoding="utf-8")
        finally:
            os.unlink(out)


@check("全字段映射")
def t_full():
    out = run("merged-flags,config-digests,source-hash,source-active,post-build,post-export",
              base_flags="base=1",
              result={"merged_flags": "a=b", "config_digests": "d1,d2",
                      "source_hash": "deadbeef", "source_active": True,
                      "post_build": "fail", "post_export": "fail"})
    assert out == ("merged-flags=a=b\nconfig-digests=d1,d2\nsource-hash=deadbeef\n"
                   "source-active=true\npost-build=fail\npost-export=fail\n"), out


@check("文件缺失 = 空转兜底")
def t_missing_file():
    out = run("merged-flags,source-hash,source-active,post-build,post-export", base_flags="base=1")
    assert out == ("merged-flags=base=1\nsource-hash=\n"
                   "source-active=false\npost-build=ok\npost-export=ok\n"), out


@check("值内 CR/LF 剥除")
def t_crlf():
    out = run("merged-flags", base_flags="",
              result={"merged_flags": "a\r\nb"})
    assert out == "merged-flags=ab\n", out


@check("--fields 子集只输出指定字段")
def t_subset():
    out = run("source-hash,source-active", result={"source_hash": "x"})
    assert out == "source-hash=x\nsource-active=false\n", out


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