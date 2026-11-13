#!/usr/bin/env python3
"""preset_config.py 自测：段落解析、平台推导与 write:presets 覆盖应用。

覆盖（核心闸：错了预设覆盖会静默丢或写坏 cfg）：
- 按段解析与字段顺序无关（platform= 在 name= 之前也能推导）
- derive_platforms：selected 限定、未知平台返回 None
- apply_overrides：主段/options 段分流、bool/字符串渲染、追加新键、
  预设名不存在 fail、保持 CRLF

用法：python .github/tests/test_preset_config.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_hook_outputs as aho  # noqa: E402
import preset_config as pc  # noqa: E402

CFG = """[preset.0]

name="Windows Desktop"
platform="Windows Desktop"
runnable=true
export_path="bin/game.exe"

[preset.0.options]

custom_template/debug=""
texture_format/s3tc_bptc=true
"""

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@check("解析与字段顺序无关：platform= 在 name= 之前也能推导")
def t_order_independent():
    reordered = """[preset.0]
platform="Windows Desktop"
name="Win"

[preset.1]
platform="Linux/X11"
name="Nux"
"""
    platforms, mapping = pc.derive_platforms(reordered, set())
    assert platforms == ["linux", "windows"], platforms
    assert mapping == {"Win": "windows", "Nux": "linux"}, mapping


@check("derive_platforms：selected 限定与未知平台")
def t_derive():
    platforms, mapping = pc.derive_platforms(CFG, {"Windows Desktop"})
    assert platforms == ["windows"], platforms
    assert mapping == {"Windows Desktop": "windows"}, mapping
    assert pc.short_platform("WeirdOS") is None


@check("read_preset_names 按段序返回")
def t_names():
    assert pc.read_preset_names(CFG) == ["Windows Desktop"]


@check("apply_overrides：主段/options 分流与值渲染")
def t_apply_overrides():
    out = pc.apply_overrides(CFG, {
        "Windows Desktop": {
            "export_path": "bin/renamed.exe",   # 主段已有键 → 替换
            "runnable": False,                  # bool → false
            "new_key": "v",                     # 主段新键 → 段尾追加
            "texture_format/s3tc_bptc": False,  # 含 '/' → options 段替换
        },
    })
    assert 'export_path="bin/renamed.exe"' in out
    assert "runnable=false" in out
    assert 'new_key="v"' in out
    assert "texture_format/s3tc_bptc=false" in out
    # options 段的键不得混入主段
    main = out.split("[preset.0.options]")[0]
    assert "texture_format" not in main


@check("apply_overrides：预设名不存在 → ValueError；保持 CRLF")
def t_apply_fail_and_crlf():
    try:
        pc.apply_overrides(CFG, {"NoSuchPreset": {"a": "b"}})
    except ValueError:
        pass
    else:
        raise AssertionError("不存在的预设名未拒绝")
    crlf_cfg = CFG.replace("\n", "\r\n")
    out = pc.apply_overrides(crlf_cfg, {"Windows Desktop": {"runnable": False}}, newline="\r\n")
    assert "runnable=false\r\n" in out


@check("值引号剥离：转义引号与多值形态")
def t_strip_quotes():
    cfg = (
        '[preset.0]\n'
        'name="Windows Desktop"\n'
        'platform="Windows Desktop"\n'
        'escaped="a\\"b"\n'          # 内部转义引号：不得提前结束
        'multi="a", "b"\n'           # 多值：整体保留，不剥成 a", "b
        'bare=/usr/bin/x\n'
        'only_prefix="unterminated\n'
    )
    sections, _ = pc.parse_sections(cfg)
    fields = sections["preset.0"]
    assert fields["name"] == "Windows Desktop", fields["name"]
    assert fields["escaped"] == 'a"b', repr(fields["escaped"])
    assert fields["multi"] == 'a, "b"', repr(fields["multi"])
    assert fields["bare"] == "/usr/bin/x", fields["bare"]
    # 只有一个前导引号（残缺值）不享受剥离，保持原样以便被后续校验发现
    assert fields["only_prefix"] == '"unterminated', repr(fields["only_prefix"])


@check("apply_overrides：cfg 无结尾换行时段内追加不粘连")
def t_apply_no_trailing_newline():
    """cfg 不以换行结尾是常见形态（手写 / 工具生成）。

    `splitlines(keepends=True)` 下最后一行没有行尾换行；段内追加若直接 append，
    新键会与最后一个物理行拼成 `key=valuekey=value`——cfg 损坏、后半字段丢失。
    """
    no_tail = CFG.rstrip("\r\n")  # 最后一个物理行是 texture_format/s3tc_bptc=true
    out = pc.apply_overrides(no_tail, {"Windows Desktop": {"appended/x": "v"}})    # 结构性判据：追加的键必须独占一行，且原末行未被污染
    lines = out.splitlines()
    assert "texture_format/s3tc_bptc=true" in lines, lines[-4:]
    assert 'appended/x="v"' in lines, lines[-4:]
    # 语义判据：重新解析后两个字段都在（粘连会丢后者）
    sections, _ = pc.parse_sections(out)
    assert sections["preset.0.options"].get("texture_format/s3tc_bptc") == "true", \
        sections["preset.0.options"]
    assert sections["preset.0.options"].get("appended/x") == "v", sections["preset.0.options"]

    # 新建段路径（options 段尚不存在）同样不得粘连
    main_only = '[preset.0]\n\nname="Windows Desktop"\nplatform="Windows Desktop"'
    created = pc.apply_overrides(main_only, {"Windows Desktop": {"a/b": "1"}})
    sections2, _ = pc.parse_sections(created)
    assert sections2["preset.0.options"].get("a/b") == "1", sections2["preset.0.options"]
    assert "export_path" not in created  # 只加了目标段，没吞掉既有段


@check("apply_hook_outputs --kind presets 端到端：覆盖落盘 cfg")
def t_apply_hook_outputs_e2e():
    root = Path(tempfile.mkdtemp(prefix="preset_cfg_e2e_"))
    try:
        cfg = root / "export_presets.cfg"
        cfg.write_text(CFG, encoding="utf-8")
        result = root / "hooks-result.json"
        # 消费侧重验：path 必须落在 .godot-hooks/<anchor-phase>/directives/
        # 通道下，且带正确 sha256
        directives = root / ".godot-hooks" / "preset-clean-after" / "directives"
        directives.mkdir(parents=True)
        presets_out = directives / "presets.json"
        presets_out.write_text(json.dumps({"Windows Desktop": {"runnable": False}}),
                               encoding="utf-8")
        sha = hashlib.sha256(presets_out.read_bytes()).hexdigest()
        result.write_text(json.dumps({"outputs": {"presets": {
            "path": str(presets_out), "sha256": sha}}}), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "apply_hook_outputs.py"
        proc = subprocess.run([sys.executable, str(script),
                               "--result", str(result), "--kind", "presets",
                               "--cfg", str(cfg)],
                              capture_output=True, text=True, encoding="utf-8",
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert proc.returncode == 0, proc.stderr
        assert "runnable=false" in cfg.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@check("apply_hook_outputs --kind artifacts：篡改 sha256 / 越界 path 被拒")
def t_apply_artifacts_tamper():
    root = Path(tempfile.mkdtemp(prefix="aho_tamper_"))
    try:
        channel = root / ".godot-hooks" / "package-before" / "dist" / "artifacts"
        channel.mkdir(parents=True)
        (channel / "a.txt").write_text("hello", encoding="utf-8")
        sha = hashlib.sha256("".join(
            hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p for p in channel.rglob("*") if p.is_file())
        ).encode("utf-8")).hexdigest()
        good = {"outputs": {"artifacts": {"path": str(channel), "sha256": sha}}}
        dist = root / "dist"
        assert aho.apply_artifacts(good, str(dist)) == 1
        assert (dist / "a.txt").read_text(encoding="utf-8") == "hello"
        # 篡改 sha256 → 拒绝
        bad_sha = {"outputs": {"artifacts": {"path": str(channel), "sha256": "deadbeef"}}}
        try:
            aho.apply_artifacts(bad_sha, str(root / "dist2"))
        except SystemExit:
            pass
        else:
            raise AssertionError("篡改 sha256 未被消费侧拒绝")
        # 越界 path（runner 任意目录）→ 拒绝
        evil = root / "secrets"
        evil.mkdir()
        (evil / "token.txt").write_text("secret", encoding="utf-8")
        bad_path = {"outputs": {"artifacts": {
            "path": str(evil),
            "sha256": hashlib.sha256("".join(
                hashlib.sha256(f.read_bytes()).hexdigest()
                for f in sorted(p for p in evil.rglob("*") if p.is_file())
            ).encode("utf-8")).hexdigest()}}}
        try:
            aho.apply_artifacts(bad_path, str(root / "dist3"))
        except SystemExit:
            pass
        else:
            raise AssertionError("越界 path 未被消费侧拒绝")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print(f"[ok] {name}")
        except Exception as e:  # noqa: BLE001 - 自测捕获任意异常
            failed.append((name, e))
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
