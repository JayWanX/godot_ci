#!/usr/bin/env python3
"""解析 profile.py 与 scons-flags，统一计算 SDK 需求与需补传的命令行标志。

detect 与 overrides 共用本脚本，避免两处判据分裂。
平台名一律用契约平台名（linux/windows/...），linux 由 compile 执行器
scons_build.py 经 godot_context.CONTRACT_TO_SCONS_PLATFORM 映射为 linuxbsd。
用法：resolve_scons_flags.py --platform P [--profile PATH] [--scons-flags STR] [--mode detect|overrides]
  detect:    输出 need-<flag>=true|false（写入 GITHUB_OUTPUT）
  overrides: 输出需补传给 scons 的 key=yes|no 片段（SCons BoolVariable 值域）
"""
import argparse
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import godot_context  # noqa: E402

# SCons BoolVariable 真值集（与 SCons/Variables/BoolVariable.py 一致）
TRUE_STRINGS = ("y", "yes", "true", "t", "1", "on", "all")
FALSE_STRINGS = ("n", "no", "false", "f", "0", "off", "none")

# 据此决定 SDK 安装：key -> (输出名, 默认值)
SDK_FLAG_OUTPUTS = {
    "d3d12": ("d3d12", True),
    "angle": ("angle", True),
    "winrt": ("winrt", True),
    "accesskit": ("accesskit", True),
    "vulkan": ("vulkan", True),
    "use_mingw": ("mingw", False),
}

# 各平台 get_flags() 中会覆盖 profile 同名 BoolVariable 的键 -> 平台默认值。
# profile 显式写、scons-flags 未显式时补传命令行以压住覆盖。
PLATFORM_FLAGS = {
    "windows": {"d3d12": True},
    "macos": {"use_volk": False, "metal": True},
    "ios": {"use_volk": False, "metal": True, "builtin_pcre2_with_jit": False},
    "visionos": {
        "use_volk": False, "metal": True, "builtin_pcre2_with_jit": False,
        "vulkan": False, "opengl3": False,
    },
    "web": {
        "builtin_pcre2_with_jit": False, "vulkan": False,
        "module_raycast_enabled": False,
    },
    "android": {},
    "linux": {},
}

# profile.py 赋值行：key = value（逐行匹配，避免跨行误判）
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$")
_QUOTED_RE = re.compile(r'^"([^"]*)"|^\'([^\']*)\'')


def _clean_value(raw):
    """去首尾引号与行尾注释，返回字符串（不转小写，保留 SCons 语义）。"""
    raw = raw.strip()
    m = _QUOTED_RE.match(raw)
    if m:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
    else:
        raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    return raw.strip()


def parse_profile(path):
    """读 profile.py，返回 {key: 值}；多次赋值取最后一次。

    空路径 = 未提供（caller 没配 profile），返回空 dict 是正确语义。显式给了路径
    却读不到则是配置指向错误：必须 fail-fast——静默按空 dict 处理会让 SDK 判定
    按「无 profile」执行（如 need-d3d12=true 却不装 SDK），失败点被推迟到编译期。
    """
    values = {}
    if not path:
        return values
    p = Path(path)
    if not p.is_file():
        fail(f"profile.py 不存在：{p}（显式传入的路径读不到，不能按『未提供』处理）")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"profile.py 不是合法 UTF-8 文本：{p}")
    except OSError as e:
        fail(f"profile.py 读取失败：{p}（{e}）")
    for line in text.splitlines():
        m = _ASSIGN_RE.match(line)
        if m:
            values[m.group(1)] = _clean_value(m.group(2))
    return values


def parse_scons_flags(text):
    """解析 scons-flags 命令行串，返回 {key: 值}；多次取最后。键大小写敏感。"""
    values = {}
    if not text:
        return values
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        if "=" not in token:
            continue
        key, _, val = token.partition("=")
        if re.fullmatch(r"[A-Za-z_]\w*", key):
            values[key] = val.strip()
    return values


def to_bool(value):
    """按 BoolVariable 真值集转 bool（大小写不敏感）；auto / 未知返回 None。"""
    v = value.lower()
    if v in TRUE_STRINGS:
        return True
    if v in FALSE_STRINGS:
        return False
    return None


def resolve(platform, profile, cli, flags):
    """返回 (overrides, effective)。overrides 为需补传的 {key: bool}。"""
    auto_keys = {k for k, v in cli.items() if v.lower() == "auto"}
    explicit = {k for k, v in cli.items() if v.lower() != "auto"}

    overrides = {}
    # 非 Windows 强制关闭 D3D12（Windows 专用驱动）。
    if platform != "windows" and "d3d12" not in explicit and "d3d12" not in auto_keys:
        overrides["d3d12"] = False
    # profile 显式写、命令行未显式（非 auto）、该键会被 platform_flag 覆盖 -> 补传。
    for key in flags:
        if key in auto_keys or key in explicit:
            continue
        if key in profile:
            val = to_bool(profile[key])
            if val is not None:
                overrides[key] = val

    def effective(key, default):
        if key in overrides:
            return overrides[key]
        if key in explicit:
            val = to_bool(cli[key])
            if val is not None:
                return val
        if key in auto_keys:
            return flags.get(key, default)
        if key in profile:
            val = to_bool(profile[key])
            if val is not None:
                return val
        return default

    return overrides, effective


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=tuple(
        godot_context.CONTRACT_TO_SCONS_PLATFORM),
        help="契约平台名；非法值由 argparse 直接拒绝（表外名字会让 PLATFORM_FLAGS 空转，"
             "在该平台上静默漏补 SDK 判定）")
    ap.add_argument("--profile", default="")
    ap.add_argument("--scons-flags", default="")
    ap.add_argument("--mode", choices=("detect", "overrides"), default="detect")
    args = ap.parse_args()

    profile = parse_profile(args.profile)
    cli = parse_scons_flags(args.scons_flags)
    flags = PLATFORM_FLAGS.get(args.platform, {})
    overrides, effective = resolve(args.platform, profile, cli, flags)

    if args.mode == "overrides":
        print(" ".join(f"{k}={'yes' if v else 'no'}" for k, v in sorted(overrides.items())))
    else:
        for key, (out, default) in SDK_FLAG_OUTPUTS.items():
            print(f"need-{out}={'true' if effective(key, default) else 'false'}")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
