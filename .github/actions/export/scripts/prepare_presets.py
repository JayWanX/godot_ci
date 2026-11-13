#!/usr/bin/env python3
"""CI 导出前把 export_presets.cfg 清洗成当前 runner 可用的形态。

把每个预设的 export_path 收敛到 <dist>/<preset-safe>/（每预设独立子目录，
同平台多预设同名产物结构上无法互相覆盖）、按模板来源设置 custom_template
与加密开关。只在内存改写后整体落盘，写前留一份 .ci.bak 备份。
"""
import argparse
import fnmatch
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from preset_name import preset_safe  # noqa: E402
from jsonc import fail  # noqa: E402
import path_guard  # noqa: E402

# 预设 platform 值 -> 自编译模板文件名候选模式。
# 桌面：godot.<platform>.<target>[.double].<arch|universal>[.exe/_console.exe] 随版本变化，
# 用 godot.<platform> 前缀式通配全覆盖；debug/console 变体由 _template_score 降权。
# macOS 导出模板必须是含 macos_template.app 的 zip（官方 macos.zip 布局），只认 macos*.zip。
# Android 导出器读 custom_template/release=*.apk（build.gradle 产出 android_release.apk）。
# Web 导出器读 *.zip（scons 产出 bin/godot*.zip）；iOS/visionOS 导出器读 *.zip。
TEMPLATE_PATTERNS = {
    "Windows Desktop": ("godot.windows*.exe",),
    "Linux": ("godot.linux*",),
    "macOS": ("macos*.zip",),
    "Android": ("android*.apk",),
    "iOS": ("ios*.zip", "godot.ios*.zip"),
    "VisionOS": ("visionos*.zip", "godot.visionos*.zip"),
    "Web": ("godot*.zip",),
}

EXPORT_PATH_KEY = "export_path"
ENCRYPT_KEYS = ("encrypt_pck", "encrypt_directory")
BOM = "\ufeff"
SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")

# 这些平台的导出器要求项目开启 ETC2/ASTC，否则导出校验直接失败。
# 按平台整集开启，不区分预设是否为「for mobile」：Godot 的 web 导出器对所有 Web
# 预设都做这项校验，而多开一个纹理导入目标只增加构建期编码时间、不影响产物行为
# ——按预设细分判定省不下什么，却要维护一张「哪些 Web 预设需要」的映射表。
ETC2_ASTC_PLATFORMS = {"macos", "ios", "visionos", "android", "web"}
ETC2_ASTC_KEY = "textures/vram_compression/import_etc2_astc"
RENDERING_SECTION = "rendering"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--cfg", required=True)
    p.add_argument("--export-type", required=True, choices=("official", "custom_build"))
    p.add_argument("--templates-dir", default="")
    p.add_argument("--project-root", default="")
    # 敏感值走固定名 env，不经 argv
    p.add_argument("--presets", default="", help="本次要导出的预设名，逗号分隔；空=全部")
    p.add_argument("--platforms", default="", help="本次导出的平台名，逗号分隔")
    p.add_argument("--dist", required=True,
                   help="导出产物目录相对项目根的路径（契约 export.paths.dist）；"
                        "各预设 export_path 收敛到 <dist>/<preset-safe>/，实际输出目录以它为准")
    p.add_argument("--project-godot", required=True,
                   help="Godot 项目配置文件相对项目根的路径（契约 export.paths.project_godot）")
    return p.parse_args()


def rel_path(value, label):
    """校验相对项目根的路径；唯一实现在 path_guard.check_rel_path。"""
    try:
        return path_guard.check_rel_path(value, label)
    except path_guard.PathError as e:
        fail(str(e))


def read_cfg(path):
    """解析配置，返回 (preamble, blocks, newline, has_bom)。

    blocks 为 [(段名, 行列表), ...]，保持原始顺序；段前注释与空行归入下一段，
    避免落盘时被挪到上一段尾部。
    """
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    preamble, blocks = [], []
    for line in text.splitlines(keepends=True):
        m = SECTION_RE.match(line)
        if m:
            tail = []
            if blocks:
                # 段尾的注释/空行属于下一段的段前说明
                while blocks[-1][1] and (
                    blocks[-1][1][-1].lstrip().startswith("#") or not blocks[-1][1][-1].strip()
                ):
                    tail.insert(0, blocks[-1][1].pop())
            blocks.append((m.group(1), tail))
        elif not blocks:
            preamble.append(line)
        else:
            blocks[-1][1].append(line)
    return preamble, blocks, newline, has_bom


def section_index(blocks):
    """返回 {段名: 行列表} 的只读映射；同名段直接 fail-fast（后者不得静默覆盖前者）。"""
    out = {}
    for name, lines in blocks:
        if name in out:
            fail(f"export_presets.cfg 含重复段 [{name}]，后者会静默覆盖前者")
        out[name] = lines
    return out


def field_of(lines, field):
    for line in lines:
        m = re.match(rf'^{re.escape(field)}="([^"]*)"', line.strip())
        if m:
            return m.group(1)
    return ""


def name_of(sections, idx):
    return field_of(sections.get(f"preset.{idx}", []), "name")


def platform_of(sections, idx):
    return field_of(sections.get(f"preset.{idx}", []), "platform")


def preset_indexes(sections):
    """列出全部 preset.N 段序号（不要求连续）。"""
    return sorted(int(m.group(1)) for m in (re.fullmatch(r"preset\.(\d+)", k) for k in sections) if m)


def _template_score(name):
    """console wrapper 与 debug 模板排后，优先正式 release 模板。"""
    lowered = name.lower()
    return (int("console" in lowered), int("debug" in lowered), lowered)


def project_dir(args):
    """项目根目录：显式传入优先，否则取 cfg 所在目录。"""
    root = Path(args.project_root or args.cfg).resolve()
    return root.parent if root.is_file() else root


def find_template(args, files, platform):
    """在 templates-dir 下找该平台模板，返回绝对路径。

    custom_template/* 是 PROPERTY_HINT_GLOBAL_FILE，引擎按绝对路径校验，
    写入相对值会随进程 cwd 变化而失效。
    """
    patterns = TEMPLATE_PATTERNS.get(platform)
    if patterns is None:
        return ""  # 未知平台：留空，由 verify 报错
    for pattern in patterns:
        hits = sorted(
            (f for f in files if fnmatch.fnmatch(f.name.lower(), pattern.lower())),
            key=lambda f: _template_score(f.name),
        )
        if hits:
            return hits[0].resolve().as_posix()
    return ""


def set_value(lines, key, value, quoted=True, newline="\n"):
    """替换 key=... 行；不存在则追加。"""
    rendered = f'{key}="{value}"' if quoted else f"{key}={value}"
    out, done = [], False
    for line in lines:
        if re.match(rf"^{re.escape(key)}=", line):
            if not done:
                out.append(rendered + newline)
                done = True
            continue
        out.append(line)
    if not done:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += newline
        out.append(rendered + newline)
    return out


def redirect_export_path(preset, safe, dist):
    """把 export_path 收敛到 <dist>/<preset-safe>/<basename>（每预设独立子目录）。

    子目录隔离使同平台多预设的同名产物在结构上无法互相覆盖。safe 名由调用方
    传入（preset_name.preset_safe 是唯一实现），须与 resolve 输出的 presets-safe
    数组、上传 matrix 三方一致。dist 相对项目根（契约 export.paths.dist）。
    """
    for line in preset:
        m = re.match(r'^export_path="([^"]*)"', line.strip())
        if not m:
            continue
        raw = m.group(1)
        # res://、UNC、盘符、POSIX 绝对路径统一只取最后一段文件名
        filename = re.split(r"[\\/]", raw.replace("res://", ""))[-1]
        if not filename or filename in (".", ".."):
            filename = "game"
        return set_value(preset, EXPORT_PATH_KEY, f"{dist}/{safe}/{filename}")
    return preset


def selected_names(args):
    return {n.strip() for n in args.presets.split(",") if n.strip()}


def clean(blocks, args, newline):
    # 敏感值走固定名 env：与 compile step / config_hash / config-inject /
    # export_steps 同名，不经 argv
    key = os.environ.get("SCRIPT_AES256_ENCRYPTION_KEY", "")
    encrypt = "true" if args.export_type == "custom_build" and key else "false"
    selected = selected_names(args)
    sections = section_index(blocks)
    files = []
    if args.export_type == "custom_build" and args.templates_dir and Path(args.templates_dir).is_dir():
        files = [f for f in Path(args.templates_dir).rglob("*") if f.is_file()]

    used_safes = {}
    active_safes = []
    for idx in preset_indexes(sections):
        preset = sections[f"preset.{idx}"]
        opts_key = f"preset.{idx}.options"
        opts = sections.get(opts_key, [])
        name = name_of(sections, idx) or f"preset-{idx}"
        safe = preset_safe(name)
        active = not selected or name_of(sections, idx) in selected

        # 仅本次导出的预设指向真实模板、并按其来源设置加密开关；其余预设清空本机路径
        template = find_template(args, files, platform_of(sections, idx)) if active else ""
        for key in ("custom_template/debug", "custom_template/release"):
            opts = set_value(opts, key, template, newline=newline)

        if active:
            # 冲突只在其产生 Artifact 的集合（本次导出的预设）内才有意义；resolve 期
            # 已做权威检测，这里兜底直调 prepare_presets 的路径。跨平台 safe 名同名、
            # 未选中预设不产 Artifact，都不构成冲突，不误伤。
            if safe in used_safes and used_safes[safe] != name:
                fail(f"预设 safe 名冲突：'{name}' 与 '{used_safes[safe]}' 都收敛为 "
                     f"'{safe}'，请改用可区分的预设名")
            used_safes[safe] = name
            for key in ENCRYPT_KEYS:
                preset = set_value(preset, key, encrypt, quoted=False, newline=newline)
            # 预设产物子目录先建好（引擎写 export_path 前目录必须存在）
            active_safes.append(safe)

        preset = redirect_export_path(preset, safe, args.dist)
        sections[f"preset.{idx}"] = preset
        if opts_key in sections or opts:
            sections[opts_key] = opts
    # 回写到 blocks（就地替换，保住段序与段前注释）
    for i, (name, _) in enumerate(blocks):
        if name in sections:
            blocks[i] = (name, sections[name])
    existing = {name for name, _ in blocks}
    for name, lines in sections.items():
        if name not in existing:
            blocks.append((name, lines))
    return blocks, active_safes


def verify(blocks, args):
    """只校验本次导出预设的模板指向，避免全文扫描误伤 uid:// 等合法内容。"""
    selected = selected_names(args)
    sections = section_index(blocks)
    for key, lines in sections.items():
        if not key.endswith(".options"):
            continue
        idx = key.split(".")[1]
        if selected and name_of(sections, int(idx)) not in selected:
            continue
        platform = platform_of(sections, int(idx))
        patterns = TEMPLATE_PATTERNS.get(platform)
        for field in ("custom_template/debug", "custom_template/release"):
            m = re.search(rf'^{re.escape(field)}="([^"]*)"', "".join(lines), re.M)
            if not m:
                continue
            value = m.group(1)
            if args.export_type == "official" and value:
                print(f"::error::{key} 的 {field} 未被清空", file=sys.stderr)
                return False
            if args.export_type == "custom_build" and (not value or not Path(value).is_file()):
                expect = "、".join(patterns) if patterns else f"平台 {platform!r} 无模板模式"
                print(f"::error::{key} 的 {field} 未指向有效模板文件（{platform} 期望 {expect}）", file=sys.stderr)
                return False
    return True


def enable_etc2_astc(project_root, project_godot="project.godot"):
    """按需开启 ETC2/ASTC 导入，返回是否改写。

    macOS/iOS/visionOS/Android 的导出器会硬校验该开关；Linux runner 的偏好
    纹理格式不是 ETC2/ASTC，不显式开启时导出必然失败。必须在 --import 之前改，
    否则已导入资源不含 ETC2/ASTC 平台变体。

    project_godot 相对项目根（契约 export.paths.project_godot）。
    """
    root = Path(project_root)
    cfg = root / project_godot
    # 没有 project.godot 就不是 Godot 项目，交给后续步骤报错
    if not cfg.is_file():
        return False
    raw = cfg.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    # 先统一成 LF 再处理，避免 splitlines 保留 \r 造成重复换行
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    lines = text.splitlines(keepends=True)
    want = f"{ETC2_ASTC_KEY}=true"
    target = f"[{RENDERING_SECTION}]"
    section_at, key_at = None, None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == want:
            return False  # 已是目标值，无需改写
        if stripped.startswith(f"{ETC2_ASTC_KEY}="):
            key_at = i
        elif stripped == target:
            section_at = i

    if key_at is not None:
        lines[key_at] = want + "\n"
    elif section_at is not None:
        # 插到 [rendering] 段末尾（下一个段头之前），避开段内注释与空行
        insert_at = section_at + 1
        for i in range(section_at + 1, len(lines)):
            if SECTION_RE.match(lines[i]):
                break
            if lines[i].strip():
                insert_at = i + 1
        lines.insert(insert_at, want + "\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["\n", target + "\n", want + "\n"])

    out = "".join(lines)
    if newline != "\n":
        out = out.replace("\n", newline)
    cfg.write_bytes((b"\xef\xbb\xbf" if has_bom else b"") + out.encode("utf-8"))
    return True


def main():
    args = parse_args()
    cfg = Path(args.cfg)
    if not cfg.is_file():
        print(f"::error::未找到 {cfg}", file=sys.stderr)
        sys.exit(1)
    args.dist = rel_path(args.dist, "--dist")
    args.project_godot = rel_path(args.project_godot, "--project-godot")
    preamble, blocks, newline, has_bom = read_cfg(cfg)
    sections = section_index(blocks)
    if not preset_indexes(sections):
        print(f"::error::{cfg} 未解析到任何 preset", file=sys.stderr)
        sys.exit(1)
    blocks, active_safes = clean(blocks, args, newline)
    if not verify(blocks, args):
        sys.exit(1)
    # 本次导出的预设产物子目录先建好（引擎写 export_path 前目录必须存在）
    project_root = Path(args.project_root or cfg.parent)
    for safe in active_safes:
        (project_root / args.dist / safe).mkdir(parents=True, exist_ok=True)
    cfg.with_suffix(cfg.suffix + ".ci.bak").write_bytes(cfg.read_bytes())
    text = "".join(preamble) + "".join(f"[{k}]\n" + "".join(v) for k, v in blocks)
    if newline != "\n":
        text = text.replace("\n", newline)
    cfg.write_bytes((b"\xef\xbb\xbf" if has_bom else b"") + text.encode("utf-8"))
    print(f"已清洗 {cfg}（export-type={args.export_type}，presets={len(preset_indexes(section_index(blocks)))}）")

    platforms = {p.strip() for p in args.platforms.split(",") if p.strip()}
    if platforms & ETC2_ASTC_PLATFORMS:
        changed = enable_etc2_astc(args.project_root or cfg.parent, args.project_godot)
        state = "已开启" if changed else "已开启（无需改写）"
        print(f"ETC2/ASTC 导入：{state}（平台 {','.join(sorted(platforms))}）")


if __name__ == "__main__":
    main()