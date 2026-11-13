#!/usr/bin/env python3
"""JSONC 配置读取工具，供 .github/actions/ 下的 resolve 系列脚本共用。

配置（ci-contract.json / ci-hooks.json）支持 // 与 /* */ 注释及尾逗号（JSONC）；
先剥注释与尾逗号，再按严格 JSON 解析。schema 校验见同目录 schema.py。
"""
import json
import re
import sys
from pathlib import Path

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def escape_error(message: str) -> str:
    """转义 GitHub workflow command 消息里的 % / CR / LF，防伪造后续命令。"""
    return str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def fail(message: str) -> None:
    """打印 GitHub Actions 的错误标记并终止脚本。"""
    print(f"::error::{escape_error(message)}", file=sys.stderr)
    sys.exit(1)


def ensure_utf8_stdio() -> None:
    """入口统一 UTF-8 输出：Windows 控制台默认 GBK，中文消息会 UnicodeEncodeError。

    这些脚本在本地（尤其是 Windows + GBK 代码页）也会被直接运行：编码错误会让
    真正的业务错被一层编码回溯盖掉。CI 上本就有 PYTHONIOENCODING=utf8，此处幂等。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if (getattr(stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # 已被重定向为不支持 reconfigure 的对象（如测试里的 StringIO）


def _strip_trailing_commas(text: str) -> str:
    """去掉对象/数组末尾的逗号；逐字符跟踪字符串，字符串内的逗号原样保留。"""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == ",":
            # peek：跳过后续空白，若下一个字符是 } 或 ] 则丢弃该逗号
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # 丢弃逗号，保留后续空白由主循环处理
                continue
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def strip_jsonc(text: str) -> str:
    """剥除 JSONC 注释与尾逗号，字符串字面量内的 / 与 , 原样保留。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # 转义序列原样保留（如 \" 不提前结束字符串）
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and text[i + 1 : i + 2] == "/":
            while i < n and text[i] != "\n":  # 行注释：吞到行尾（换行符保留）
                i += 1
        elif c == "/" and text[i + 1 : i + 2] == "*":
            end = text.find("*/", i + 2)  # 块注释：吞到 */（未闭合则吞到文件尾）
            i = n if end == -1 else end + 2
        else:
            out.append(c)
            i += 1
    return _strip_trailing_commas("".join(out))


def _read_text(path: Path, file_path: str, description: str):
    """读文本；非 UTF-8 / 读取失败都转成 ::error::（不裸崩栈）。

    `read_text` 遇到非法字节会抛 UnicodeDecodeError，堆栈信息里看不到「是哪个配置」，
    而且不会被 exit 1 以外的方式识别为可读错误。
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        fail(f"{description} 不是合法 UTF-8 文本 {file_path}：{e}")
    except OSError as e:
        fail(f"{description} 读取失败 {file_path}：{e}")


def _loads(text, file_path, description):
    try:
        return json.loads(strip_jsonc(text))
    except json.JSONDecodeError as e:
        fail(f"{description} 解析失败 {file_path}: {e}")


def read_json(file_path: str, description: str):
    """读取 JSONC 配置并解析；文件缺失或格式错误则报错退出。"""
    path = Path(file_path)
    if not path.is_file():
        fail(f"缺少 {description}: {file_path}")
    return _loads(_read_text(path, file_path, description), file_path, description)


def read_json_optional(file_path: str, description: str):
    """读取可选 JSONC 配置：文件不存在返回 None，格式错误才报错退出。

    清单类配置（ci-hooks.json / 平台基线覆写）「不存在」是合法状态，
    不可用 read_json + try/except（它在缺失时直接 sys.exit）。
    """
    path = Path(file_path)
    if not path.is_file():
        return None
    return _loads(_read_text(path, file_path, description), file_path, description)
