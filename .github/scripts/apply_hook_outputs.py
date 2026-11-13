#!/usr/bin/env python3
"""把 hooks-result.json 里已声明的输出应用到实际目标（编排层能力应用）。

run_hooks.py dispatch 只收集并校验各通道下的声明输出；本脚本负责把它们落到
CI 认可的位置。按 docs/hooks.md 可写面（按能力授权），调用时机：
  - artifacts  : package:before 之后、打包之前 → 合并进 dist/
  - presets    : preset-clean:after 之后 → 声明式覆盖应用到 export_presets.cfg
                 （经 preset_config.apply_overrides，不让钩子原地覆写 cfg）

用法：
  apply_hook_outputs.py --result hooks-result.json --kind artifacts --dist <dir>
  apply_hook_outputs.py --result hooks-result.json --kind presets --cfg <export_presets.cfg>
"""
import argparse

from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import hashlib
import json
import shutil
import sys
from pathlib import Path

import preset_config  # noqa: E402


# 消费侧重验：hooks-result.json 跨 job 经 artifact 传递（merge-multiple 会平铺
# 混合），被篡改后可把 runner 任意路径文件合并进 dist / 当 patch 应用。每项输出
# re-算 sha256，并约束 path 落在对应通道目录下（docs/hooks.md「消费侧也要重验」）。
# 合法形态恒为 <workspace>/.godot-hooks/<anchor>-<phase>/<channel>/<prefix>。
CHANNEL_EXPECT = {
    "artifacts": ("dist", "artifacts"),
    "presets": ("directives", "presets.json"),
    "profile_patch": ("directives", "patches"),
    "source_patch": ("directives", "patches"),
}


def sha256_of_path(path: Path):
    """按 collect_outputs 同款算法重算：单文件取内容摘要，目录取各文件摘要拼接的摘要。"""
    if path.is_symlink():
        return None
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        return None
    files = sorted(f for f in path.rglob("*") if f.is_file())
    if any(f.is_symlink() for f in files):
        return None
    parts = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def verify_record(rec, kind, label):
    """重验单条输出记录：sha256 一致 + path 落在对应通道目录内。"""
    if not isinstance(rec, dict):
        fail(f"{label} 输出记录不是对象：{rec!r}")
    for field in ("path", "sha256"):
        if field not in rec:
            fail(f"{label} 输出记录缺 {field} 字段：{rec}")
    path = Path(rec["path"])
    channel, prefix = CHANNEL_EXPECT[kind]
    if not path.is_absolute() or ".godot-hooks" not in path.parts:
        fail(f"{label} 输出 path 不在钩子通道根（.godot-hooks）内：{rec['path']}")
    if path.parent.name != channel or path.name != prefix:
        fail(f"{label} 输出 path 不在 {channel}/{prefix} 通道下：{rec['path']}")
    actual = sha256_of_path(path)
    if actual is None:
        fail(f"{label} 输出 path 不存在或含符号链接：{rec['path']}")
    if actual != rec["sha256"]:
        fail(f"{label} 输出 sha256 不匹配（消费侧重验失败，可能已被篡改）："
             f"记录 {rec['sha256']} vs 实际 {actual}；path={rec['path']}")
    return path


def load(result_path):
    p = Path(result_path)
    if not p.is_file():
        return {}  # 空转：无 hooks-result.json 视为无输出
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail(f"读取 {result_path} 失败：{e}")


def apply_artifacts(data, dist):
    """把 dist/artifacts/ 目录树合并进导出产物目录（同名覆盖，不删既有文件）。"""
    outputs = data.get("outputs") or {}
    rec = outputs.get("artifacts")
    if not rec:
        return 0
    src = verify_record(rec, "artifacts", "apply_hook_outputs")
    if not src.is_dir():
        return 0
    dst = Path(dist)
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        count += 1
    return count


def apply_presets(data, cfg_path):
    """把 directives/presets.json 的声明式覆盖应用到 export_presets.cfg（原地覆写）。

    覆盖格式 {预设名: {字段: 值}}；字段含 '/' 落 [preset.N.options] 段。
    预设名不存在 / 覆盖格式非法 → fail-fast（拼错名不得静默丢覆盖）。
    """
    outputs = data.get("outputs") or {}
    rec = outputs.get("presets")
    if not rec:
        return 0
    src = verify_record(rec, "presets", "apply_hook_outputs")
    if not src.is_file():
        fail(f"presets 输出不存在：{src}")
    try:
        overrides = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail(f"presets.json 非法：{e}")
    if not overrides:
        return 0
    cfg = Path(cfg_path)
    if not cfg.is_file():
        fail(f"presets 覆盖需要 export_presets.cfg，但 {cfg} 不存在")
    raw = cfg.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    try:
        updated = preset_config.apply_overrides(text, overrides, newline="\n")
    except ValueError as e:
        fail(f"presets 覆盖应用失败：{e}")
    if newline != "\n":
        updated = updated.replace("\n", newline)
    cfg.write_bytes((b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"")
                    + updated.encode("utf-8"))
    return len(overrides)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", default="hooks-result.json")
    ap.add_argument("--kind", required=True, choices=("artifacts", "presets"))
    ap.add_argument("--dist", default="")
    ap.add_argument("--cfg", default="", help="presets 覆盖的目标 export_presets.cfg")
    args = ap.parse_args()
    data = load(args.result)
    if args.kind == "artifacts":
        if not args.dist:
            fail("--kind artifacts 需要 --dist")
        n = apply_artifacts(data, args.dist)
        print(f"apply artifacts: {n} 个文件合并进 {args.dist}")
    else:
        if not args.cfg:
            fail("--kind presets 需要 --cfg")
        n = apply_presets(data, args.cfg)
        print(f"apply presets: {n} 项覆盖已应用到 {args.cfg}")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())