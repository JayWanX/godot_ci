#!/usr/bin/env python3
"""Chain Runner Bootstrap：读 platform-baseline.json 的 roles 段，输出 runner-json=<紧凑 JSON>。

resolve 作业 / CI Self-check 的 runner 分配先于契约可读（GitHub 的 runs-on 求值不依赖
任何 step 输出，见 workflows/runner.yml），故此处只读基线的 roles 段（链级作业 runner，
与平台无关、不可覆写）；平台级 build_runner 由 resolve 归一化后随 platforms-json 下发。

用法：python runner_baseline.py   # 在 GITHUB_OUTPUT 存在时写 runner-json 行，否则打印
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jsonc import ensure_utf8_stdio, fail, read_json  # noqa: E402

BASELINE_PATH = Path(__file__).resolve().parents[1] / "contract" / "platform-baseline.json"


def main() -> int:
    path = os.environ.get("PLATFORM_BASELINE", str(BASELINE_PATH))
    baseline = read_json(path, "platform-baseline.json")
    roles = baseline.get("roles")
    if not isinstance(roles, dict) or not roles:
        fail("平台基线 roles 段缺失或为空，无法读取链级作业 runner")
    if "resolve" not in roles:
        fail("平台基线 roles 缺 resolve 键（resolve 作业的 runner 必须由基线唯一决定）")
    out_path = os.environ.get("GITHUB_OUTPUT", "")
    line = "runner-json=" + json.dumps(roles, ensure_ascii=False, separators=(",", ":"))
    if out_path:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line)
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
