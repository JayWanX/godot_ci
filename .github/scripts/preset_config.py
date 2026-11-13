#!/usr/bin/env python3
"""export_presets.cfg 段落解析与应用（resolve_export / prepare_presets 共用）。

 Godot 的 export_presets.cfg 是 [preset.N] / [preset.N.options] 段 + key=value 行。
逐行状态机假设 name= 先于 platform= 出现，格式稍变即误判；本模块按段解析，
与字段顺序无关。write:presets 能力的声明式覆盖也在此应用（不让钩子原地改 cfg）。
"""
import re

SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_PRESET_SECTION_RE = re.compile(r"^preset\.(\d+)$")

# 平台长名 → 契约平台名（前缀匹配；"Windows Desktop"→windows、"Linux/X11"→linux 等）
PLATFORM_MAPPING = (
    ("Windows", "windows"), ("Linux", "linux"), ("macOS", "macos"),
    ("Android", "android"), ("iOS", "ios"), ("VisionOS", "visionos"), ("Web", "web"),
)


def parse_sections(text):
    """把 cfg 文本解析成 {段名: [(key, raw_line), ...]} 与段顺序列表。

    返回 (sections, order)：sections[名] = {key: value}（同段重复键取最后一个，
    重复段名由调用方按需 fail-fast）；order 保持原始段顺序。
    """
    sections = {}
    order = []
    current = None
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            current = m.group(1)
            if current not in sections:
                sections[current] = {}
                order.append(current)
            continue
        if current is None:
            continue
        kv = line.split("=", 1)
        if len(kv) != 2:
            continue
        key, value = kv[0].strip(), kv[1].strip()
        sections[current][key] = _strip_quotes(value)
    return sections, order


def _strip_quotes(value):
    """剥掉 cfg 值外层的双引号；正确处理转义引号与多值形态。

    Godot cfg 里字符串值写作 `"..."`，内部 `\\"` 是转义引号（如路径里的引号），
    数组 / 多值写作 `"a", "b"`。整体 `[1:-1]` 会把后者剥成 `a", "b`——外层结构
    丢了、还留下半边引号，后续按名比较全部失配；`\\"` 也会被当成结束符。
    """
    if len(value) < 2 or not value.startswith('"'):
        return value
    out = []
    i, n = 1, len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            out.append(value[i + 1])
            i += 2
            continue
        if ch == '"':
            # 首个未转义引号 = 当前值结束；后面若还有内容（`, "b"`）说明是多值，
            # 原样保留剩余部分，让调用方按整体字符串处理（与 Godot 读到的字面值一致）
            rest = value[i + 1:].lstrip()
            return "".join(out) + rest
        out.append(ch)
        i += 1
    # 有前导引号却没有闭合引号：值本身残缺，原样返回让后续校验能看见问题
    return value


def preset_entries(sections, order):
    """按段序返回 [(序号, 段名字典)]；序号取 [preset.N] 的 N。"""
    out = []
    for name in order:
        m = _PRESET_SECTION_RE.match(name)
        if m:
            out.append((int(m.group(1)), sections[name]))
    return out


def read_preset_names(text):
    """全部预设名（按段序）。"""
    sections, order = parse_sections(text)
    return [fields.get("name", "") for _, fields in preset_entries(sections, order)
            if fields.get("name")]


def short_platform(long_name):
    """平台长名 → 契约平台名；无法识别返回 None。"""
    for prefix, short in PLATFORM_MAPPING:
        if long_name.startswith(prefix):
            return short
    return None


def derive_platforms(text, selected_presets):
    """从 cfg 文本推导 (平台排序列表, {预设名: 契约平台名})。

    与字段顺序无关：按 [preset.N] 段取 name/platform。selected_presets 限定
    参与推导的预设集合（空集合 = 全部）。
    """
    sections, order = parse_sections(text)
    platforms = set()
    preset_platform = {}
    for _, fields in preset_entries(sections, order):
        name = fields.get("name", "")
        if not name or (selected_presets and name not in selected_presets):
            continue
        short = short_platform(fields.get("platform", ""))
        if short:
            platforms.add(short)
            preset_platform[name] = short
    return sorted(platforms), preset_platform


def apply_overrides(text, overrides, newline="\n"):
    """把 write:presets 的声明式覆盖应用到 cfg 文本，返回新文本。

    overrides 格式：{"<预设名>": {"<字段>": <值>}}；字段含 '/' 写入该预设的
    [preset.N.options] 段，否则写入 [preset.N] 段。值：bool → true/false，
    其余 → 带引号字符串（与 Godot cfg 的书写一致）。
    预设名不存在 → ValueError（拼错名必须显式失败，不静默丢覆盖）。
    """
    if not isinstance(overrides, dict):
        raise ValueError("presets 覆盖必须是对象 {预设名: {字段: 值}}")
    lines = text.splitlines(keepends=True)
    # 预设名 → (主段名, options 段名)
    sections, order = parse_sections(text)
    name_to_sections = {}
    for name in order:
        m = _PRESET_SECTION_RE.match(name)
        if m and sections[name].get("name"):
            name_to_sections[sections[name]["name"]] = name
    for preset, fields in overrides.items():
        if preset not in name_to_sections:
            raise ValueError(f"presets 覆盖的预设 '{preset}' 不在 cfg 中"
                             f"（实际预设：{sorted(name_to_sections)}）")
        if not isinstance(fields, dict):
            raise ValueError(f"预设 '{preset}' 的覆盖必须是对象")
        base = name_to_sections[preset]
        for key, value in fields.items():
            section = base + ".options" if "/" in key else base
            rendered = _render_value(value)
            lines = _set_key(lines, section, key, rendered, newline)
    return "".join(lines)


def _render_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace('"', '\"') + '"'


def _set_key(lines, section, key, rendered, newline):
    """替换 section 段内 key= 行；不存在则追加到段尾。段不存在则新建段。"""
    out = []
    in_section = False
    replaced = False
    section_seen = False
    for line in lines:
        stripped = line.rstrip("\r\n")
        m = SECTION_RE.match(stripped)
        if m:
            if in_section and not replaced:
                _append_line(out, f"{key}={rendered}", newline)
                replaced = True
            in_section = m.group(1) == section
            if in_section:
                section_seen = True
            out.append(line)
            continue
        if in_section and not replaced and re.match(rf"^{re.escape(key)}=", stripped):
            out.append(f"{key}={rendered}" + newline)
            replaced = True
            continue
        out.append(line)
    if in_section and not replaced:
        _append_line(out, f"{key}={rendered}", newline)
        replaced = True
    if not section_seen:
        if out:
            _append_line(out, "", newline)
        out.append(f"[{section}]" + newline)
        out.append(f"{key}={rendered}" + newline)
    return out


def _append_line(out, text, newline):
    """在已有文本末尾安全追加一行。

    `splitlines(keepends=True)` 下最后一行可能没有行尾换行（cfg 不以换行结尾，
    手写/工具生成都常见）。直接 append 会与最后一个物理行粘连成 `key=valuekey=value`，
    整段后半字段丢失。这里先补换行再追加；text 为空时等同于只补换行。
    """
    if out and not out[-1].endswith(("\n", "\r")):
        out[-1] += newline
    if text:
        out.append(text + newline)
