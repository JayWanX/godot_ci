#!/usr/bin/env python3
"""把钩子产出的声明式 patch 落到真实目标（编排层能力应用）。

run_hooks.py dispatch 只收集并校验各通道下的声明输出；本脚本负责应用：
  - profile_patch : unified diff → 注入的 profile.py（配置层，config-hash:before）
  - source_patch  : git-apply 兼容 diff 树 → 引擎源码根（源码层，cache-restore:before）

补丁格式：标准 git unified diff（`--- a/<path>` / `+++ b/<path>`），按 -p1 剥前缀。
  - profile_patch 以**目标文件所在目录**为基准（`a/profile.py` / `b/profile.py`）。
  - source_patch 以**引擎源码根**为基准。

安全：先对每份补丁做 `git apply --check` 干跑，全部通过才真正应用；中途失败
按逆序 `git apply -R` 回滚已应用的部分，不留半改状态。

用法：
  apply_hook_patches.py --result hooks-result.json --kind profile_patch \\
      --target <profile.py>
  apply_hook_patches.py --result hooks-result.json --kind source_patch \\
      --engine-root <dir>

profile_patch 的 --target 允许为空（本次构建未注入 profile.py）：无产出时
空操作返回 0；有产出时明确报错「补丁无处可落」，由调用方直接调用，无需在外层
预判有无产出。
"""
import argparse

from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_hook_outputs import verify_record  # noqa: E402


def load(result_path):
    p = Path(result_path)
    if not p.is_file():
        return {}
    # 跨 job 传递的文件：编码错误与 JSON 错误同样要转成可读的 ::error::
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except UnicodeDecodeError as e:
        fail(f"读取 {result_path} 失败（非 UTF-8）：{e}")
    except (OSError, ValueError) as e:
        fail(f"读取 {result_path} 失败：{e}")


def patch_files(data, kind):
    """取该 kind 的补丁文件列表（绝对路径）。"""
    outputs = data.get("outputs") or {}
    rec = outputs.get(kind)
    if not rec:
        return []
    # 消费侧重验：摘要一致 + path 必须落在 directives/patches 通道目录下
    src = verify_record(rec, kind, "apply_hook_patches")
    if src.is_file():
        return [src]
    return sorted(p for p in src.rglob("*") if p.is_file())


def git_apply(base: Path, patch: Path, reverse: bool = False, check: bool = False):
    """对 base 目录应用补丁；返回 (ok, 输出)。

    `git apply` 不要求 base 是 git 仓库（只读工作树模式即可），但要求
    base 目录存在且补丁路径以其为基准（-p1 剥 a/ b/ 前缀）。
    check=True 时只干跑（--check）不落盘。

    统一 `errors="replace"`：git 的输出可能混入目标文件里的非 UTF-8 字节，
    严格解码会让回滚路径自己抛 UnicodeDecodeError，掩盖真正的补丁错误。
    """
    cmd = ["git", "apply", "--whitespace=nowarn", "-p1"]
    if reverse:
        cmd.append("-R")
    if check:
        cmd.append("--check")
    cmd.append(str(patch))
    proc = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def apply_patch_set(base: Path, patches, label: str) -> int:
    """先干跑全部（不落盘）、再依次应用；失败逆序回滚已应用的部分。

    回滚失败必须一并报出来：声称「已回滚」而实际留了半改状态，比直接失败更危险
    ——调用方会以为项目树是干净的，继续用被改坏的文件编译。
    """
    for p in patches:
        ok, detail = git_apply(base, p, check=True)
        if not ok:
            fail(f"{label} 补丁 {p.name} 不适用（干跑失败）：{detail}")
    applied = []
    for p in patches:
        ok, detail = git_apply(base, p)
        if not ok:
            rollback_failed = []
            for done in reversed(applied):
                rolled, rollback_detail = git_apply(base, done, reverse=True)
                if not rolled:
                    rollback_failed.append(
                        f"{done.name}（{rollback_detail or '逆序应用失败'}）")
            hint = ""
            if "patch does not apply" in detail:
                hint = ("；若目标文件与补丁行尾不一致（CRLF vs LF）也会报这个错——"
                        "git apply 逐字节比对，Windows 上写补丁请显式用 LF")
            if rollback_failed:
                fail(f"{label} 补丁 {p.name} 应用失败，且逆序回滚未完全成功："
                     f"残留 {rollback_failed}——请人工检查 {base} 下的半改状态。"
                     f"原始错误：{detail}{hint}")
            fail(f"{label} 补丁 {p.name} 应用失败，已回滚：{detail}{hint}")
        applied.append(p)
    return len(applied)


def apply_profile_patch(data, target: str) -> int:
    patches = patch_files(data, "profile_patch")
    if not patches:
        return 0
    if not target:
        fail("hook 产出了 profile_patch，但本次构建未注入 profile.py，补丁无处可落")
    tgt = Path(target)
    if not tgt.is_file():
        fail(f"profile_patch 需要已注入的配置文件，但 {tgt} 不存在")
    base = tgt.parent
    backup = Path(tempfile.mkdtemp(prefix="hook_profile_backup_")) / tgt.name
    shutil.copy2(tgt, backup)
    try:
        n = apply_patch_set(base, patches, "profile_patch")
    except SystemExit:
        shutil.copy2(backup, tgt)
        raise
    print(f"apply profile_patch: {n} 份补丁已应用到 {tgt}")
    return n


def apply_source_patch(data, engine_root: str) -> int:
    patches = patch_files(data, "source_patch")
    if not patches:
        return 0
    root = Path(engine_root)
    if not root.is_dir():
        fail(f"source_patch 需要引擎源码目录，但 {root} 不存在")
    n = apply_patch_set(root, patches, "source_patch")
    print(f"apply source_patch: {n} 份补丁已应用到 {root}")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", default="hooks-result.json")
    ap.add_argument("--kind", required=True, choices=("profile_patch", "source_patch"))
    ap.add_argument("--target", default="", help="profile_patch 的目标文件")
    ap.add_argument("--engine-root", default="", help="source_patch 的引擎源码根")
    args = ap.parse_args()
    data = load(args.result)
    if args.kind == "profile_patch":
        apply_profile_patch(data, args.target)
    else:
        if not args.engine_root:
            fail("--kind source_patch 需要 --engine-root")
        apply_source_patch(data, args.engine_root)
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())