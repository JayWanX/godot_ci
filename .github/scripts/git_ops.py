#!/usr/bin/env python3
"""git 检出与私有仓库鉴权的唯一实现（resolve / config-inject 共用）。

来源声明语法唯一实现在 scripts/godot_context.py::parse_source_entry；
本模块只负责把解析结果落成一次浅克隆，以及 token 直接内插的进程级鉴权
（git insteadOf 不做变量展开，故不能用占位符写法）。
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ref_guard import assert_valid_ref  # noqa: E402


def git_auth_env(token: str) -> dict:
    """返回带私有仓库鉴权的子进程 env；token 为空时返回当前 env 的副本。"""
    env = os.environ.copy()
    if token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"url.https://x-access-token:{token}@github.com/.insteadOf"
        env["GIT_CONFIG_VALUE_0"] = "https://github.com/"
    return env


def clone_shallow(repo: str, ref: str, dest, token: str = "", with_submodules: bool = False) -> None:
    """浅克隆远程 owner/repo 到 dest；ref 为空 = 默认分支，token 非空走鉴权。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if ref:
        assert_valid_ref(ref, "project ref")
    env = git_auth_env(token)

    def run(*args):
        subprocess.run(["git", *args], check=True, capture_output=True, env=env)

    run("init", "--quiet", str(dest))
    run("-C", str(dest), "remote", "add", "origin", f"https://github.com/{repo}")
    if ref:
        run("-C", str(dest), "fetch", "--depth=1", "--quiet", "origin", ref)
    else:
        run("-C", str(dest), "fetch", "--depth=1", "--quiet", "origin")
    run("-C", str(dest), "checkout", "--quiet", "FETCH_HEAD")
    if with_submodules:
        run("-C", str(dest), "submodule", "update", "--init", "--recursive", "--depth=1")
