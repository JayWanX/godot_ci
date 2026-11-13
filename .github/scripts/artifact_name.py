#!/usr/bin/env python3
"""产物命名渲染器：把契约 artifacts 的两套模板渲染成实际名字。

两套模板按**对象**划线（不是按链）：

- **build_naming**（编译产物）：渲染 {prefix} {platform} {target} {arch}
  {variant}。产物是引擎模板/编辑器，在 Actions Artifacts 里中转。
- **export_naming**（导出产物）：渲染 {prefix} {platform} {preset}。产物是最终
  交付给用户的 zip / Release 附件。

依对象而非依链命名的原因（内部接线，使用契约时无需关心）：build 链产编译产物，
export 链在 custom_build 模式下要按同一模板渲染下载 pattern 取回它，所以模板的
归属是「产物」而不是「生产它的链」。

编译产物名的后缀规则（CI 布局常量，本文件）：
- 模板里写了 {arch} / {variant} → 以模板为准，不再追加后缀（避免两段架构名）。
- 模板没写、但调用方给了维度值 → 追加为末段。
- 模板没写、平台有默认架构（android）→ 追加默认架构。
- 未知占位符直接失败，不静默替换。

导出产物名不追加维度后缀：预设名是它的第二维，同名即冲突，不做序号兜底。

用法：
  artifact_name.py --build-naming "{prefix}_{platform}_{target}" --prefix repo \\
      --platform android --target template_release
  artifact_name.py --build-naming ... --platform web --target template_release \\
      --variants threads,nothreads --pattern
  artifact_name.py --export-naming "{prefix}_{preset}" --prefix repo \\
      --preset "Windows Desktop"
"""
import argparse

from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import re
import sys

import preset_name  # noqa: E402

# 平台默认架构（CI 布局常量，不进契约）：模板未含 {arch} 时追加
PLATFORM_DEFAULT_ARCH = {
    "android": "arm64",
}

PLACEHOLDERS = ("prefix", "platform", "target", "arch", "variant")
EXPORT_PLACEHOLDERS = ("prefix", "platform", "preset")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def normalize(name: str, allow_glob: bool = False) -> str:
    """收敛为安全字符：仅允许 [A-Za-z0-9._-]，其余一律替换为 '_'。

    产物名会进 shell 上下文（package-artifact 的 `rm -rf`/7z 参数、Release 附件名），
    双引号内 `$()` / 反引号仍会展开——白名单收敛是最后一道防线。下载 pattern 需要
    '*' 通配多变体维度，故 allow_glob=True 时额外放行 '*'（只由 pattern() 使用）。
    """
    allowed = r"A-Za-z0-9._\-" + (r"\*" if allow_glob else "")
    name = re.sub(rf"[^{allowed}]+", "_", name)
    name = re.sub(r"_{2,}", "_", name)
    return name.strip("_")


def render(naming: str, prefix: str, platform: str, target: str,
           arch: str = "", variant: str = "", _allow_glob: bool = False) -> str:
    """渲染编译产物名；未知占位符直接失败（不静默替换）。

    _allow_glob 仅由 pattern() 为多变体 '*' 通配设置，外部调用不得传。
    """
    template = (naming or "").strip()
    if not template:
        fail("artifacts.build_naming 模板为空")
    used = set(_PLACEHOLDER_RE.findall(template))
    unknown = sorted(used - set(PLACEHOLDERS))
    if unknown:
        fail(f"artifacts.build_naming 含未知占位符 {unknown}；"
             f"可用占位符 {list(PLACEHOLDERS)}")
    # 模板用到 {prefix} 却没给值：渲染出 "___linux_template_release" 这种无主名前缀，
    # 不同项目的产物在同一个 Artifacts 列表里无法区分。取值链的终点必须显式失败。
    if "prefix" in used and not (prefix or "").strip():
        fail("产物名前缀为空（artifacts.prefix / repo-safe 都未提供）；"
             "产物名会失去项目维度，请检查契约或调用方传参")


    values = {
        "prefix": prefix or "",
        "platform": platform or "",
        "target": target or "",
        "arch": arch or PLATFORM_DEFAULT_ARCH.get(platform, ""),
        "variant": variant or "",
    }
    name = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)

    if "arch" not in used:
        suffix = arch or PLATFORM_DEFAULT_ARCH.get(platform, "")
        if suffix:
            name = f"{name}_{suffix}"
    if "variant" not in used and variant:
        name = f"{name}_{variant}"

    return normalize(name, allow_glob=_allow_glob)


def pattern(naming: str, prefix: str, platform: str, target: str,
            arch: str = "", variants=None) -> str:
    """下载 pattern。

    变体集合来自平台基线 `platform_variants.<platform>`：
    - 有多个变体（len >= 2，如 web 的 threads / nothreads）→ 变体维度用 '*' 通配。
    - 单一产物（其余平台）→ 直接给精确产物名。

    变体集合为空时按单产物处理：glob 的 `*` 虽可匹配空串，但前面的字面 `_`
    仍要求产物名带那个下划线，单产物平台会一个都匹配不上。
    """
    variants = list(variants or [])
    if len(variants) >= 2:
        return render(naming, prefix, platform, target, arch=arch, variant="*",
                      _allow_glob=True)
    variant = variants[0] if variants else ""
    return render(naming, prefix, platform, target, arch=arch, variant=variant)


def render_export(export_naming: str, prefix: str, platform: str,
                  preset: str) -> str:
    """渲染导出产物名（预设 zip / Release 附件）。

    preset 传原始预设名，内部收敛成 safe 形式（preset_name.preset_safe 是唯一实现）。
    不做序号去重：包名冲突会让 Release 附件互相覆盖，必须在 resolve 期 fail-fast，
    由调用方对所有选中预设去重后判定。未知占位符同样直接失败。
    """
    template = (export_naming or "").strip()
    if not template:
        fail("artifacts.export_naming 模板为空")
    used = set(_PLACEHOLDER_RE.findall(template))
    unknown = sorted(used - set(EXPORT_PLACEHOLDERS))
    if unknown:
        fail(f"artifacts.export_naming 含未知占位符 {unknown}；"
             f"可用占位符 {list(EXPORT_PLACEHOLDERS)}")
    values = {
        "prefix": prefix or "",
        "platform": platform or "",
        "preset": preset_name.preset_safe(preset),
    }
    return normalize(_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template))




def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-naming", dest="build_naming", default="",
                    help="编译产物命名模板（契约 artifacts.build_naming）")
    ap.add_argument("--export-naming", dest="export_naming", default="",
                    help="导出产物命名模板（契约 artifacts.export_naming）")
    ap.add_argument("--prefix", default="", help="产物名前缀")
    ap.add_argument("--platform", default="")
    ap.add_argument("--target", default="")
    ap.add_argument("--preset", default="", help="预设原始名（导出产物名用，内部收敛 safe 名）")
    ap.add_argument("--arch", default="")
    ap.add_argument("--variants", default="",
                    help="该平台的变体名集合（逗号分隔，来自平台基线 platform_variants）；"
                         "两项及以上按通配，单值按精确名")
    ap.add_argument("--pattern", action="store_true",
                    help="输出下载 pattern（多变体平台用 '*'，单产物平台给精确名）")
    args = ap.parse_args()
    if args.export_naming:
        if not args.preset:
            ap.error("--export-naming 需要 --preset")
        print(render_export(args.export_naming, args.prefix, args.platform, args.preset))
        return 0
    if not args.build_naming:
        ap.error("需要 --build-naming 或 --export-naming")
    if not args.platform or not args.target:
        ap.error("--build-naming 需要 --platform 与 --target")
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    if args.pattern:
        print(pattern(args.build_naming, args.prefix, args.platform, args.target,
                      arch=args.arch, variants=variants))
    else:
        # 单值渲染：多变体集合时用 '*'（与 pattern 同判据），否则取唯一变体
        multi = len(variants) >= 2
        variant = "*" if multi else (variants[0] if variants else "")
        print(render(args.build_naming, args.prefix, args.platform, args.target,
                     arch=args.arch, variant=variant, _allow_glob=multi))
    return 0



if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())