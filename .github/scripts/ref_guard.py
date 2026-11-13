#!/usr/bin/env python3
"""git ref 形态白名单（resolve 系列脚本与契约归一化共用）。

ref 会作为位置参数拼进 git ls-remote / fetch，以 '-' 开头会被 git 解析为
选项（参数注入面，如 --upload-pack）；其余按 git-check-ref-format 拒绝。
"""
import re
import sys

from jsonc import escape_error


def assert_valid_ref(ref: str, label: str) -> None:
    """非法 ref 打 ::error:: 后直接退出（与调用方 fail 行为一致）。

    判据对齐 git-check-ref-format：除整体形态外，还拒绝任何段以 '.' 开头、
    任何段以 '.lock' 结尾、以及连续 '//'（空段）。这些形态 git 一律拒绝，
    放行到 ls-remote 才炸的话错误信息与配置入口相距很远。
    """
    segments = ref.split("/")
    # 空段（连续 '//'）也要拒：`not s` 为真，故用哨兵而非空串做「未命中」判据
    bad_segment = next((s for s in segments
                        if not s or s.startswith(".") or s.endswith(".lock")), None)
    if (not re.fullmatch(r"[A-Za-z0-9._+/-]+", ref)
            or ref.startswith(("-", ".", "/"))
            or ".." in ref or "@{" in ref
            or ref.endswith(".lock") or ref.endswith("/")
            or bad_segment is not None):
        message = f"{label} 含非法 ref 字符或形态：'{ref}'"
        print(f"::error::{escape_error(message)}", file=sys.stderr)
        sys.exit(1)
