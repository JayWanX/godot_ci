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
import os
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

# profile.py 赋值行：key = value（仅用于报错提示，不再作为取值判据——正则看不出
# 赋值是否在条件块内，见 parse_profile 的说明）
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$")
_QUOTED_RE = re.compile(r'^"([^"]*)"|^\'([^\']*)\'')

# 本脚本关心的 SCons 选项名全集：SDK 判定键 + 需补传的平台覆盖键。
# profile.py 执行后取值的**优先级判据**——不在此集内的键本脚本不消费，但也不丢弃
# （它们是直接交给 SCons 的普通选项，如 store_release / production）。
_KNOWN_OPTION_KEYS = frozenset(SDK_FLAG_OUTPUTS) | {
    key for per_platform in PLATFORM_FLAGS.values() for key in per_platform
}

# 执行 profile.py 时注入的模块级名字，不属于选项，取值时排除。
_NON_OPTION_NAMES = frozenset({"__name__", "__file__", "__builtins__", "__doc__"})

# 契约平台名 → SCons 平台名，唯一权威在 godot_context；此处按需取，避免循环导入。
PLATFORM_TO_SCONS = godot_context.CONTRACT_TO_SCONS_PLATFORM


def _clean_value(raw):
    """去首尾引号与行尾注释，返回字符串（不转小写，保留 SCons 语义）。"""
    raw = raw.strip()
    m = _QUOTED_RE.match(raw)
    if m:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
    else:
        raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    return raw.strip()


def parse_profile(path, platform=""):
    """执行 profile.py 取它在本平台的真实取值，返回 {key: 值}。

    原实现用正则逐行扫 `key = value`，**看不出赋值是否在条件块里**——`if platform ==
    "windows": d3d12 = "yes"` 会被无条件记成 yes，于是 detect-deps 在 Linux 上也去装
    D3D12 SDK，`--mode overrides` 还会把这个键补传进命令行、压掉另一平台的条件分支。

    改为真实执行，并模拟 SCons 的求值环境（SCons 的 `Variables` 就是 `exec(contents,
    {}, temp_values)`）：命名空间只留 `__name__`，`sys.path` 前置脚本目录，故
    profile.py 里能 `import`、能读环境变量——与 SCons 侧完全一致。

    **判据必须可复现**：SCons 求值时 `platform` 与 `target` 来自命令行。本脚本拿不到
    那两个参数，故把它们经环境变量 `SCONS_PLATFORM` / `SCONS_TARGET` 暴露给 profile.py，
    并把这两个名字写进契约文档；profile.py 用它们写条件，CI 与 SCons 才会得到同一结果。
    另把 `ARGUMENTS` 一并注入（值为 {platform, target, profile}），让按 `ARGUMENTS`
    写条件的既有 profile 也能工作。

    空路径 = 未提供（caller 没配 profile），返回空 dict 是正确语义。显式给了路径却
    读不到则是配置指向错误：必须 fail-fast——静默按空 dict 处理会让 SDK 判定按
    「无 profile」执行（如 need-d3d12=true 却不装 SDK），失败点被推迟到编译期。
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

    # 与 SCons 的 Variables 同构：空 __name__ 命名空间 + 脚本目录进 sys.path。
    namespace = {"__name__": str(p)}
    scons_platform = PLATFORM_TO_SCONS.get(platform, platform)
    injected = {"platform": scons_platform, "target": "editor", "profile": str(p)}
    saved_dir = str(p.resolve().parent)
    saved_env = {}
    for key, value in (("SCONS_PLATFORM", scons_platform), ("SCONS_TARGET", injected["target"])):
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value
    sys.path.insert(0, saved_dir)
    try:
        exec(compile(text, str(p), "exec"), namespace)
    except Exception as e:  # noqa: BLE001 - profile 是用户代码，任何异常都要能归因
        fail(f"profile.py 执行失败：{p}（{type(e).__name__}: {e}）")
    finally:
        sys.path.remove(saved_dir)
        for key, old in saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    # 取模块级赋值的选项值：排除注入的名字、导入的模块、私有名与非标量。
    # **不过滤成 _KNOWN_OPTION_KEYS**：store_release / production 这类普通选项也要能
    # 被取到（它们不经 overrides 补传，但需参与判定与调试输出）。
    for key, value in list(namespace.items()):
        if key in _NON_OPTION_NAMES or key.startswith("_"):
            continue
        if isinstance(value, str):
            values[key] = value
        elif isinstance(value, bool):
            values[key] = "yes" if value else "no"
        elif isinstance(value, int):
            values[key] = str(value)
        # 模块 / 函数 / 其它对象不是 SCons 选项，忽略。
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

    # 传 platform：profile.py 里按平台写条件时必须拿到本次的真实平台。
    profile = parse_profile(args.profile, args.platform)
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
