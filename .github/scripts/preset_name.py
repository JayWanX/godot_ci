#!/usr/bin/env python3
"""预设名 → safe 名的唯一实现（prepare_presets / resolve_export / export-platform 共用）。

safe 名用作每预设产物子目录名（dist/<safe>/）与 Artifact 名末段。
规则：小写；非 [a-z0-9_]+ 的字符替换为 '_'；连续 '_' 收敛；首尾 '_' 去除。
结果为空 → fail-fast（不静默兜底）。跨预设 safe 名冲突由调用方检测。

用法：
  preset_name.py "Windows Desktop"                    # → windows_desktop
  preset_name.py --validate-packages --preset-names "A,B" \\
      --packages '[{"preset":"A","preset-safe":"a","package":"p_a"}, ...]'
"""
import argparse

from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import json
import re
import sys

_SAFE_RE = re.compile(r"[^a-z0-9_]+")


def preset_safe(name: str) -> str:
    """把预设名收敛成目录/Artifact 安全形式；空结果直接失败。"""
    safe = _SAFE_RE.sub("_", (name or "").lower())
    safe = re.sub(r"_{2,}", "_", safe).strip("_")
    if not safe:
        fail(f"预设名 {name!r} 无法收敛出安全目录名")
    return safe


def validate_packages(preset_names_str: str, packages_json: str) -> int:
    """导出链输入一致性守卫（export-platform「校验打包矩阵」step）。

    packages 由 resolve 一次渲染并下传，本处只做三向校验：
    - 条目集合与 presets 的收敛结果一致（否则上传 matrix 找不到产物子目录）；
    - preset-safe 无重复（同平台内产物子目录不得重叠）；
    - package 无重复（zip 同名会让 Release 附件互相覆盖）。
    resolve 期已拦过 package 冲突，这里是跨 job 传值后的第二道闸。
    """
    presets = [p.strip() for p in preset_names_str.split(",") if p.strip()]
    if not presets:
        fail("presets 不能为空（本工作流按预设粒度导出上传，需显式列出）")
    try:
        declared = json.loads(packages_json)
    except ValueError:
        fail("packages 不是合法 JSON")
    if not isinstance(declared, list):
        fail("packages 必须是 JSON 数组")
    seen_safes, seen_packages = [], []
    for i, entry in enumerate(declared):
        if not isinstance(entry, dict):
            fail(f"packages[{i}] 必须是对象")
        missing = [k for k in ("preset", "preset-safe", "package") if not entry.get(k)]
        if missing:
            fail(f"packages[{i}] 缺字段 {missing}（应为 preset / preset-safe / package）")
        seen_safes.append(entry["preset-safe"])
        seen_packages.append(entry["package"])
    derived = [preset_safe(p) for p in presets]
    if sorted(seen_safes) != sorted(derived):
        fail(f"packages 的 preset-safe {sorted(seen_safes)} 与 presets 收敛结果 "
             f"{sorted(derived)} 不一致")
    if len(set(seen_safes)) != len(seen_safes):
        fail("packages 的 preset-safe 含重复项（预设 safe 名冲突）")
    if len(set(seen_packages)) != len(seen_packages):
        fail(f"packages 的 package 含重复项 {sorted(seen_packages)}"
             f"（同名 zip 会让 Release 附件互相覆盖）")
    print(f"打包矩阵校验通过（{len(declared)} 个预设）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", default="", help="打印该预设名的 safe 形式")
    ap.add_argument("--validate-packages", action="store_true",
                    help="校验 presets 与 packages 矩阵的一致性")
    ap.add_argument("--preset-names", default="", help="逗号分隔的预设名")
    ap.add_argument("--packages", default="",
                    help="resolve 下传的打包矩阵 JSON 数组")
    args = ap.parse_args()
    if args.validate_packages:
        return validate_packages(args.preset_names, args.packages)
    if not args.name:
        ap.error("需要预设名，或 --validate-packages")
    print(preset_safe(args.name))
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
