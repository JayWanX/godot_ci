#!/usr/bin/env python3
"""hooks-result.json → step outputs 归一化（build-pipeline 三个调用点共用）。

hooks-result.json 是扩展链唯一跨 step 传值通道；文件不存在（或损坏）= 空转，
全部字段取兜底值——空转时调用 step 本身被 if: hooks-bundle 短路，下游用
`outputs.* || inputs.*` 兜底，保证「关闭扩展时执行 step 数与未启用扩展时
完全一致」（docs/hooks.md 两阶段执行器、错误/回滚语义）。

--fields 逗号分隔，字段与映射：
  merged-flags   ← merged_flags    （兜底 --base-flags，即契约归一化值）
  config-digests ← config_digests  （兜底空）
  source-hash    ← source_hash     （兜底空）
  source-active  ← source_active   （兜底 false）
  post-build     ← post_build      （兜底 ok）
  post-export    ← post_export     （兜底 ok；export-platform 归一化 post-export）
值一律剥 CR/LF（GITHUB_OUTPUT 单行协议）后写出。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jsonc import ensure_utf8_stdio  # noqa: E402

FIELDS = {
    "merged-flags": ("merged_flags", None),
    "config-digests": ("config_digests", ""),
    "source-hash": ("source_hash", ""),
    "source-active": ("source_active", None),
    "post-build": ("post_build", "ok"),
    "post-export": ("post_export", "ok"),
}


def load_result(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result", default="hooks-result.json", help="hooks-result.json 路径")
    ap.add_argument("--fields", required=True, help="逗号分隔的输出字段集")
    ap.add_argument("--base-flags", default="", help="merged-flags 的兜底值（契约归一化 flags）")
    args = ap.parse_args()

    data = load_result(args.result)
    out = os.environ.get("GITHUB_OUTPUT", "")
    for name in args.fields.split(","):
        name = name.strip()
        if name not in FIELDS:
            print(f"::error::未知 --fields 值 '{name}'；可用：{', '.join(sorted(FIELDS))}",
                  file=sys.stderr)
            return 1
        json_key, default = FIELDS[name]
        if name == "source-active":
            value = "true" if data.get(json_key) else "false"
        elif name == "merged-flags":
            value = data.get(json_key) or args.base_flags
        else:
            value = data.get(json_key) or default
        value = str(value).replace("\r", "").replace("\n", "")
        if out:
            # noqa: output-key —— name 取自已校验的 FIELDS 白名单（上方 `not in FIELDS`
            # 直接 return 1），取值集封闭恒非空，无需再做防空。
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"{name}={value}\n")
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())