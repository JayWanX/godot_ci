#!/usr/bin/env python3
"""SCons 编译命令装配与执行（compile action 执行器）。

装配顺序即语义：
1. driver flags：resolve_scons_flags（overrides 模式）计算的补传片段——命令行非 auto
   的具体值才能压住 profile 同名赋值与平台 get_flags() 覆盖；
2. 平台名映射：CI 契约平台名 → SCons 平台名（linux → linuxbsd，唯一映射点）；
3. scons-flags：shlex 拆分后原样追加（用户显式值位于最后，SCons 对重复键取后者）；
4. 条件参数：build_profile / profile / modules 仅在提供时传递；
5. 固定尾部：cache_path=<dir> redirect_build_objects=no。

BUILD_NAME：PR 事件为 gh-<number>，否则 gh（经进程级 env 传给 scons）。
加密密钥经调用方注入的进程级 env（SCRIPT_AES256_ENCRYPTION_KEY）随子进程继承，
不进命令行、不落日志。

--dry-run：打印装配后的参数（JSON 数组）后退出，不执行 scons；供测试与调试。
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from jsonc import ensure_utf8_stdio  # noqa: E402
import godot_context  # noqa: E402
import resolve_scons_flags as rsf  # noqa: E402


def build_args(platform, target, scons_flags, scons_cache,
               build_profile_path, profile_path, modules_dir) -> list:
    profile = rsf.parse_profile(profile_path)
    cli = rsf.parse_scons_flags(scons_flags)
    flags = rsf.PLATFORM_FLAGS.get(platform, {})
    overrides, _ = rsf.resolve(platform, profile, cli, flags)

    # 平台名映射唯一源：godot_context.CONTRACT_TO_SCONS_PLATFORM（契约名 → SCons 名）
    scons_platform = godot_context.CONTRACT_TO_SCONS_PLATFORM[platform]
    args = [f"platform={scons_platform}", f"target={target}"]
    args += [f"{k}={'yes' if v else 'no'}" for k, v in sorted(overrides.items())]
    args += shlex.split(scons_flags) if scons_flags else []
    if build_profile_path:
        args.append(f"build_profile={build_profile_path}")
    if profile_path:
        args.append(f"profile={profile_path}")
    if modules_dir:
        args.append(f"custom_modules={modules_dir}")
    args += [f"cache_path={scons_cache}", "redirect_build_objects=no"]
    return args


def build_name() -> str:
    pr = os.environ.get("PR_NUMBER", "")
    return f"gh-{pr}" if pr else "gh"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", required=True, choices=tuple(
        godot_context.CONTRACT_TO_SCONS_PLATFORM),
        help="契约平台名（linux/windows/...）；非法值由 argparse 直接拒绝")
    ap.add_argument("--target", required=True,
                    choices=("editor", "template_debug", "template_release"),
                    help="编译目标档位")
    ap.add_argument("--scons-flags", default="", help="合并钩子后的完整 SCons 标志")
    ap.add_argument("--scons-cache", required=True, help="SCons 缓存路径（cache_path=）")
    ap.add_argument("--build-profile-path", default="", help=".gdbuild 路径（空则省略）")
    ap.add_argument("--profile-path", default="", help="profile.py 路径（空则省略）")
    ap.add_argument("--modules-dir", default="", help="自定义模块目录（空则省略）")
    ap.add_argument("--dry-run", action="store_true", help="打印装配参数（JSON）后退出")
    ap.add_argument("--root", default="", help="引擎源码根（默认当前目录）")
    args = ap.parse_args()

    cmd_args = build_args(args.platform, args.target, args.scons_flags, args.scons_cache,
                          args.build_profile_path, args.profile_path,
                          args.modules_dir)
    print("编译参数：" + " ".join(cmd_args))

    if args.dry_run:
        print(json.dumps(cmd_args, ensure_ascii=False))
        return 0

    # 下面的 rmtree / bin 列举都是相对 CWD 的：必须在引擎源码根执行。CWD 走偏时
    # `scons` 会编译到别的树（或什么都没编），而 bin/ 列举照样打印空清单看不出异常。
    root = Path(args.root).resolve() if args.root else Path.cwd()
    if not (root / "SConstruct").is_file():
        print(f"::error::{root} 不是引擎源码根（缺 SConstruct）；"
              f"请用 --root 指定或用 cwd 切到引擎检出根", file=sys.stderr)
        return 1
    os.chdir(root)

    # 导出模板不含编辑器代码
    if args.target != "editor":
        shutil.rmtree("editor", ignore_errors=True)

    env = dict(os.environ, BUILD_NAME=build_name())
    proc = subprocess.run(["scons", *cmd_args], env=env)
    if proc.returncode != 0:
        return proc.returncode

    bin_dir = Path("bin")
    if not bin_dir.is_dir():
        print(f"::error::编译成功但 {bin_dir} 不存在（产物未落在引擎源码根）；"
              f"请确认在正确的引擎检出目录执行", file=sys.stderr)
        return 1
    for p in sorted(bin_dir.iterdir()):
        print(f"{p}  {p.stat().st_size}")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
