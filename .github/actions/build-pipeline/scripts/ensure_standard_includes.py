#!/usr/bin/env python3
"""为使用新工具链的源码补齐缺失的标准库 include。

新版编译器移除传递包含（如 <thread> 不再带入 <memory>），用到
std::unique_ptr / std::unique_lock / pthread_* 而未包含定义头会编译失败。
按实际使用的符号补齐所需头；已自包含的文件不动。

约束：
- 规则声明适用平台（all/posix/windows），POSIX 专用头（如 <pthread.h>）
  在 Windows 目标下永不注入（MSVC 无 pthread.h），符号在平台条件块内也一样。
- 只按 "\\n" 切分，"\\r" 留在行尾，写回保持原字节布局；UTF-8 BOM 匹配前
  剥离、写回还原。
- #if/#ifdef 块内的包含不算「已包含」：补丁必须落在必然编译的段落。
- 仅 C++ 存在的标准头（<memory>、<mutex>）不能进以 C 编译的翻译单元
  （由同名兄弟 .c 文件识别，如 ufbx.h 旁的 ufbx.c）：此类文件里的
  无条件包含包上 #if defined(__cplusplus) 门控，而非新增。
- 匹配在剥离注释与字符串字面量的副本上进行，仅出现于文档的标识符不误触发。

用法：
    python ensure_standard_includes.py [ROOT] [--dry-run] --platform <契约平台名>

--platform 收 CI 契约平台名（linux/windows/macos/android/ios/visionos/web），
内部映射到规则平台域：windows 目标专用 windows 规则，其余一律 posix 域。
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import godot_context  # noqa: E402

# 契约平台名 → 规则平台域（posix | windows）
CONTRACT_PLATFORM_MAP = {"windows": "windows"}

# (header, platforms, symbol_re)  -- platforms 取 {"all"} | {"posix"} | {"windows"}
HEADER_FAMILIES = (
    ("memory", {"all"}, re.compile(r"\bstd::(?:make_)?(?:unique_ptr|shared_ptr|weak_ptr|enable_shared_from_this|allocator|addressof)\b")),
    ("mutex", {"all"}, re.compile(r"\bstd::(?:unique_lock|scoped_lock|lock_guard)\b")),
    ("pthread.h", {"posix"}, re.compile(r"\bpthread_t\b|\bpthread_[a-z_]+_t\b|\bpthread_[a-z_]+(?=\()")),
    ("sal.h", {"windows"}, re.compile(r"\b_In_count_\b|\b_In_opt_count_\b")),
)

SKIP_DIRS = {".git", "bin", ".scons_cache"}
EXTS = (".cpp", ".h", ".hpp", ".cc", ".cxx", ".mm", ".c")
HEADER_EXTS = (".h", ".hpp")
BOM = b"\xef\xbb\xbf"
# 仅 C++ 存在的标准头；无条件包含需 __cplusplus 门控。
CXX_ONLY_HEADERS = {"memory", "mutex"}

_COND_RE = re.compile(r"^\s*#\s*if(?:def|ndef)?\b")
_ENDIF_RE = re.compile(r"^\s*#\s*endif\b")
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')


def is_applicable(family, rule_domain):
    """规则是否适用于目标规则域。"""
    return "all" in family[1] or rule_domain in family[1]


def parse_args(argv):
    """解析命令行；返回 (root, dry_run, rule_domain)。

    ``rule_domain`` 为规则域（posix|windows），由 --platform 的契约平台名映射而来；
    未显式给定直接 fail-fast（设计稿要求判定数据缺失不静默跳过；宿主 OS
    不能代表交叉编译目标）。
    """
    dry_run = False
    platform = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            dry_run = True
        elif a == "--platform":
            if i + 1 >= len(argv):
                fail("--platform 需要取值（契约平台名）。")
            platform = argv[i + 1]
            i += 1
        # `--platform=linux` 连写：手写调用与脚本拼接都常用，不支持会落到下面的
        # 「未知选项」分支，报错信息指向选项名而不是用法差异，容易误判成参数拼错
        elif a.startswith("--platform="):
            platform = a.split("=", 1)[1]
            if not platform:
                fail("--platform= 后缺少契约平台名。")
        elif a.startswith("-"):
            fail(f"未知选项 {a}")
        else:
            positional.append(a)
        i += 1
    if platform is None:
        fail("缺少 --platform（契约平台名）；编译目标必须由 CI 显式传入，不能用宿主 OS 推断。")
    # 平台名必须是契约平台白名单内的值：表外名字会静默退化成 posix 规则域，
    # 在 Windows 目标上少补一批标准头（编译期才炸，症状与根因相距很远）
    if platform not in godot_context.PLATFORMS:
        fail(f"--platform 取值 '{platform}' 不是契约平台名（可用："
             f"{', '.join(godot_context.PLATFORMS)}）")
    # 契约平台名 → 规则平台域：windows 目标用 windows 规则，其余（含 web/android
    # 等在 Linux runner 上交叉编译的目标）一律 posix 域
    rule_domain = CONTRACT_PLATFORM_MAP.get(platform, "posix")
    root = positional[0] if positional else "."
    return root, dry_run, rule_domain


def scan_includes(lines):
    """扫描预处理结构；返回 [(depth, header, line_index), ...]。

    跟踪 ``#if/#ifdef/#ifndef/#endif`` 嵌套，区分无条件包含（depth 0）与
    条件包含；``#else``/``#elif`` 不改变深度。
    """
    depth = 0
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _COND_RE.match(stripped):
            depth += 1
        elif _ENDIF_RE.match(stripped):
            depth = max(0, depth - 1)
        m = _INCLUDE_RE.match(stripped)
        if m:
            result.append((depth, m.group(1), i))
    return result


def has_include(includes, header):
    """``header`` 是否在 depth 0（无条件段）被包含。

    ``#if``/``#ifdef`` 块内的不算：补丁必须落在必然编译的段落才能对所有目标生效。
    """
    return any(depth == 0 and name == header for depth, name, _ in includes)


def find_include_insert_index(includes):
    """新 ``#include`` 应插入其后的行索引。

    优先取 depth 0 的最后一个 ``#include``，让补丁落在必然编译的公共段而非
    目标可能跳过的平台/特性 ``#ifdef`` 内——如 ``thread_posix.cpp`` 末尾的
    ``<pthread_np.h>`` 位于 ``#ifdef PTHREAD_BSD_SET_NAME`` 下，Linux 上被排除。
    没有无条件包含时回退到文件顶（``-1``，位于任何 ``#if`` 之前，故 depth 0）
    保证补丁实际生效；与 ``has_include`` 保持一致。

    文件无 ``#include`` 时返回 ``None``。
    """
    if not includes:
        return None
    depth0 = [i for d, _, i in includes if d == 0]
    if depth0:
        return max(depth0)
    return -1


def insert_after_last_include(text, headers, guard_headers=frozenset()):
    lines = text.split("\n")
    includes = scan_includes(lines)
    idx = find_include_insert_index(includes)
    if idx is None:
        return None
    # 与文件换行风格一致（插入文件顶时取首行）
    crlf = lines[idx].endswith("\r") if idx >= 0 else lines[0].endswith("\r")
    eol = "\r" if crlf else ""
    for header in headers:
        if header in guard_headers:
            lines.insert(idx + 1, f"#if defined(__cplusplus)" + eol)
            lines.insert(idx + 2, f"#include <{header}>" + eol)
            lines.insert(idx + 3, f"#endif" + eol)
            idx += 3
        else:
            lines.insert(idx + 1, f"#include <{header}>" + eol)
            idx += 1
    return "\n".join(lines)


def guard_cxx_only_includes(text, headers):
    """把对 C++ 专用头的每个无条件（depth-0）包含包上 `#if defined(__cplusplus)`。

    [param] headers 需要门控的 C++ 标准库专用头名集合。
    [return] 有门控时返回 (修改后的文本, 已门控的头名集合)，否则返回 None。
    """
    lines = text.split("\n")
    depth = 0
    result = []
    guarded = set()
    for line in lines:
        stripped = line.strip()
        m = _INCLUDE_RE.match(stripped)
        if m and depth == 0 and m.group(1) in headers:
            indent = line[: len(line) - len(line.lstrip())]
            result.append(f"{indent}#if defined(__cplusplus)")
            result.append(line)
            result.append(f"{indent}#endif")
            guarded.add(m.group(1))
            continue
        if _COND_RE.match(stripped):
            depth += 1
        elif _ENDIF_RE.match(stripped):
            depth = max(0, depth - 1)
        result.append(line)
    return ("\n".join(result), guarded) if guarded else None


def _blank_preserving_newlines(segment):
    """把 segment 中除换行外的字符替换为空格。"""
    return "".join("\n" if ch == "\n" else " " for ch in segment)


def strip_comments_and_strings(text):
    """剥离注释与字符串字面量，保留换行。

    返回的副本中 `//` 与 `/* */` 注释及 `"..."` / `'...'` 字面量替换为空格，
    HEADER_FAMILIES 模式只匹配真实代码，不匹配文档或字符串内容。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = text.find("\n", i)
            if j == -1:
                out.append(_blank_preserving_newlines(text[i:]))
                break
            out.append(_blank_preserving_newlines(text[i:j]))
            i = j
        elif ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                out.append(_blank_preserving_newlines(text[i:]))
                break
            out.append(_blank_preserving_newlines(text[i:j + 2]))
            i = j + 2
        elif ch in ('"', "'"):
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == ch:
                    j += 1
                    break
                j += 1
            out.append(_blank_preserving_newlines(text[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def main():
    root, dry_run, rule_domain = parse_args(sys.argv[1:])
    families = [f for f in HEADER_FAMILIES if is_applicable(f, rule_domain)]

    patched = 0
    guarded = 0
    no_include = 0
    not_utf8 = 0
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(EXTS):
                continue
            path = os.path.join(cur, name)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                print(f"::warning::{path}: 读取失败 ({e})")
                continue
            bom = data.startswith(BOM)
            if bom:
                data = data[len(BOM):]
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                not_utf8 += 1
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            changed = False
            guarded_headers = set()
            # 只门控可能以 C 编译的头：同目录存在同名 .c 兄弟文件（如 ufbx.h 旁的 ufbx.c）
            is_c_header = name.endswith(HEADER_EXTS) and os.path.isfile(
                os.path.join(cur, os.path.splitext(name)[0] + ".c"))
            if is_c_header:
                guard = guard_cxx_only_includes(text, CXX_ONLY_HEADERS)
                if guard is not None:
                    text, guarded_headers = guard
                    print(("DRY-RUN " if dry_run else "") + rel + "  以 __cplusplus 门控 C++ 专用头包含")
                    guarded += 1
                    changed = True
            # 在剥离注释/字符串的副本上匹配，仅出现于文档的标识符不误触发
            code_only = strip_comments_and_strings(text) if families else ""
            needed = [header for header, _, symbol_re in families if symbol_re.search(code_only)]
            # 已门控的头已满足 C++ 编译，跳过重复插入
            needed = [header for header in needed if header not in guarded_headers]
            if needed:
                includes = scan_includes(text.split("\n"))
                needed = [header for header in needed if not has_include(includes, header)]
                if needed:
                    guard_inject = CXX_ONLY_HEADERS if is_c_header else frozenset()
                    updated = insert_after_last_include(text, needed, guard_inject)
                    if updated is not None:
                        text = updated
                        print(("DRY-RUN " if dry_run else "") + rel + "  + " + ", ".join("<" + h + ">" for h in needed))
                        patched += 1
                        changed = True
                    else:
                        no_include += 1
            if changed and not dry_run:
                with open(path, "wb") as fh:
                    fh.write((BOM if bom else b"") + text.encode("utf-8"))
    print(f"补丁文件: {patched}, 门控 C++ 专用头: {guarded}, 跳过（无 include 行）: {no_include}, 跳过（非 UTF-8）: {not_utf8}")


if __name__ == "__main__":
    ensure_utf8_stdio()
    main()