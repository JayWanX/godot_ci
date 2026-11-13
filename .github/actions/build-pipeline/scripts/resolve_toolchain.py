#!/usr/bin/env python3
"""工具链版本分发（build-pipeline「解析工具链版本」执行器）。

输入是 resolve 归一化的 toolchain-json（契约 build.toolchain 覆写平台基线的
归一化终值，单一次来源）；emsdk / java / scons 任一缺失即 fail-fast。输出三行到
GITHUB_OUTPUT。

scons 也必须在缺失时失败：下游是 `pip install "scons==$INPUT_SCONS_VERSION"`，
空版本号会变成 `pip install "scons=="`，报错停在 pip 的参数解析上，看不出根因。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from jsonc import ensure_utf8_stdio, fail  # noqa: E402

# setup-scons 安装所需的全部版本号（缺一即无法装出可用的构建驱动）
REQUIRED_VERSIONS = ("emsdk", "java", "scons")


def set_output(key: str, value: str) -> None:
    """写 $GITHUB_OUTPUT。"""
    out = os.environ.get("GITHUB_OUTPUT", "")
    if out:
        # noqa: output-key —— key 全部为字面量硬编码（下方调用点固定三个键），
        # 取值集封闭恒非空，无需再做防空。
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--toolchain-json", required=True,
                    help="resolve 归一化的工具链版本 JSON")
    args = ap.parse_args()

    if not args.toolchain_json.strip():
        fail("toolchain-json 为空：须由 resolve 归一化提供（契约 build.toolchain 覆写平台基线）")
    try:
        tc = json.loads(args.toolchain_json)
    except ValueError as e:
        fail(f"toolchain-json 非法：{e}")

    missing = [k for k in REQUIRED_VERSIONS if not str(tc.get(k) or "").strip()]
    if missing:
        fail(f"工具链版本缺失：{missing}（toolchain-json 归一化结果不完整，"
             f"契约 build.toolchain 与平台基线 toolchain 均未提供；"
             f"setup-scons 需要 scons 版本号才能安装构建驱动）")

    for key in REQUIRED_VERSIONS:
        set_output(key, str(tc[key]).strip())
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())