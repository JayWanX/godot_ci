#!/usr/bin/env python3
"""计算构建配置摘要，供 SCons 缓存键使用。

只输出 8 位十六进制：缓存键会明文出现在日志与 cache API 中，
密钥等敏感输入必须先摘要再入键，不能拼接原值。

分层（见 docs/hooks.md 分层缓存键）：
  config-hash = sha256(profile, build_profile, modules_tree, base_scons_flags,
                       encryption_key, plan_json, hook_output_digests[config])[:8]
  source-hash = sha256(engine-sha, source_patch_digests,
                       hook_output_digests[source])[:8]
本脚本只产 config-hash；source-hash 由 run_hooks.py dispatch 处理 source_patch
应用后写入 hooks-result.json，再由 build-pipeline 归一化后传给 cache action。
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from jsonc import ensure_utf8_stdio, fail  # noqa: E402


def read_text(path):
    """读单个输入文件内容参与摘要。

    空路径 = 该输入未提供，返回空串（合法的「未配置」语义）。显式给了路径却读不到，
    属于配置指向错误：必须 fail-fast，不能按空串参与摘要——那会与「未提供」同键，
    让本次运行命中无配置那次的缓存并复用与其不符的旧产物。
    """
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        fail(f"config-hash 输入不存在：{p}（显式传入的路径读不到，不能按『未提供』参与摘要）")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 二进制内容按字节参与摘要：文本解码失败不代表输入无效（profile 旁挂的
        # 二进制资产也会进来），但它的任何变化都必须让摘要变化。
        return p.read_bytes().hex()
    except OSError as e:
        fail(f"config-hash 输入读取失败：{p}（{e}）")


def read_tree(root):
    """目录下全部文件的相对路径与内容，按路径排序保证稳定。

    内容按字节读入摘要：模块树里二进制文件（预编译产物 / 资源）很常见，按文本读
    会在解码失败时退化成空串，改动完全不进摘要，缓存就会静默命中旧产物。
    """
    if not root:
        return b""
    base = Path(root)
    if not base.is_dir():
        fail(f"config-hash 模块树不存在：{base}（显式传入的目录读不到）")
    parts = []
    for f in sorted(p for p in base.rglob("*") if p.is_file()):
        parts.append(f.relative_to(base).as_posix().encode("utf-8"))
        try:
            parts.append(f.read_bytes())
        except OSError as e:
            fail(f"config-hash 模块文件读取失败：{f}（{e}）")
    return b"\0".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="", help="注入的 profile.py 路径")
    ap.add_argument("--build-profile", default="", help="注入的 .gdbuild（build profile）路径")
    ap.add_argument("--modules", default="", help="自定义模块目录")
    ap.add_argument("--scons-flags", default="", help="附加 SCons 标志（含钩子合并后的值）")
    ap.add_argument("--plan-json", default="", help="validate 产出的 plan.json 路径（空转时为空常量）")
    ap.add_argument("--hook-output-digests", default="", help="cache_impact=config 的钩子输出摘要，逗号分隔")
    args = ap.parse_args()
    # 敏感值走固定名 env：不进 argv，仅参与摘要、不出现在键里
    encryption_key = os.environ.get("SCRIPT_AES256_ENCRYPTION_KEY", "")

    parts = [
        read_text(args.profile).encode("utf-8"),
        read_text(args.build_profile).encode("utf-8"),
        read_tree(args.modules),
        args.scons_flags.encode("utf-8"),
        encryption_key.encode("utf-8"),
        read_text(args.plan_json).encode("utf-8"),
        args.hook_output_digests.encode("utf-8"),
    ]
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
        h.update(b"\0")
    print(h.hexdigest()[:8])
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
