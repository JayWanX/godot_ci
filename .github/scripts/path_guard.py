#!/usr/bin/env python3
"""相对路径校验的唯一实现（resolve / hooks / export 共用）。

规则：非空、纯 POSIX 分隔符、禁绝对路径（含 Windows 盘符）、禁 .. / . / 空段。
root 给定时额外检查规范化后逃逸与符号链接；root 省略时只做形态校验。
"""
import os
import re
from pathlib import Path, PurePosixPath


class PathError(ValueError):
    """路径非法（调用方转成自己的 ::error:: / HookError）。"""


# Windows 保留设备名（COM0 与 LPT0 亦保留，CONIN$/CONOUT$ 属变体，按前缀族判断）
_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.IGNORECASE)


def check_rel_path(rel, label, root=None):
    """校验并返回 strip 后的 rel；非法抛 PathError。root 给定时检查逃逸/符号链接。"""
    if not isinstance(rel, str) or not rel.strip():
        raise PathError(f"{label}: 路径为空")
    rel = rel.strip()
    if "\\" in rel:
        raise PathError(f"{label}: 路径 '{rel}' 含 Windows 反斜杠，请用 '/'")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise PathError(f"{label}: 禁止绝对路径 '{rel}'")
    raw_parts = rel.split("/")
    if any(p == ".." for p in raw_parts):
        raise PathError(f"{label}: 路径 '{rel}' 含 '..' 段")
    if any(p == "" for p in raw_parts):
        raise PathError(f"{label}: 路径 '{rel}' 含空段（重复 '/' 或首尾 '/'）")
    if any(p == "." for p in raw_parts):
        raise PathError(f"{label}: 路径 '{rel}' 含 '.' 段")
    # Windows 侧的落点陷阱：这些路径在 checkout / rmtree / open 时行为与 POSIX 不同，
    # 且多数会被静默规范化到别处（NS 设备名）或直接不可创建（段尾点/空格）。
    for part in raw_parts:
        if part != part.rstrip(". "):
            raise PathError(f"{label}: 路径 '{rel}' 的段 '{part}' 以点或空格结尾"
                            f"（Windows 上会被静默裁掉，落点与声明不一致）")
        if _WINDOWS_RESERVED.fullmatch(part.split(".")[0]):
            raise PathError(f"{label}: 路径 '{rel}' 的段 '{part}' 是 Windows 保留设备名")
    parts = PurePosixPath(rel).parts
    if not parts:
        raise PathError(f"{label}: 路径 '{rel}' 为空")
    if root is not None:
        root_resolved = Path(root).resolve()
        target = (root_resolved / rel).resolve()
        try:
            if os.path.commonpath([str(root_resolved), str(target)]) != str(root_resolved):
                raise PathError(f"{label}: 路径 '{rel}' 规范化后逃逸出 {root_resolved}")
        except ValueError:
            raise PathError(f"{label}: 路径 '{rel}' 与根目录不在同一盘符")
        cur = root_resolved
        for part in parts:
            cur = cur / part
            if _is_link(cur):
                raise PathError(f"{label}: 路径 '{rel}' 的 '{cur.name}' 是符号链接"
                                f"（或 junction / 重解析点）")
    return rel


def _is_link(path: Path) -> bool:
    """符号链接或 Windows junction：后者 is_symlink() 为假但仍会改变落点。"""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())
