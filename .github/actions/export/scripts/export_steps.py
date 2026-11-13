#!/usr/bin/env python3
"""export action 执行器（五个 step 子命令，与 config-inject 的 config_inject.py 同构）。

GITHUB_ENV 传值链：
  validate-inputs → SOURCE_PRESETS / EXPORT_PRESETS_REL / PROJECT_GODOT_REL / DIST_REL
  resolve-presets → PRESET_LIST
  prepare-editor  → EDITOR
  export-presets / 清洗导出配置 step 消费上述值。

子命令：
  validate-inputs  入口校验：目录存在、相对路径、版本字符白名单、密钥格式
  prepare-editor   预取命中 → cp，否则 curl 下载；解压并暴露 EDITOR（macOS .app 双形态）
  prepare-templates 官方 tpz 落位：目录根按 runner 系统选，版本目录名按 --version 剥离
                    （GODOT_VERSION_FULL_CONFIG）并与 get_version_info 互证
  resolve-presets  预设名单透传（resolve 归一化必传，空= fail-fast）
  export-presets   逐个导出：单个失败记入汇总跑完其余预设，末尾统一退出非零；
                   每个预设导出成功后 dispatch 逐预设锚点 export-preset-each:after
                   （--hooks-bundle 空时短路，与 build / export / release 三条链的空转零成本一致）
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from jsonc import escape_error, fail  # noqa: E402, fail
import path_guard  # noqa: E402

# 全串白名单；".." 段在 cmd_validate_inputs 里显式拒绝（会拼进下载 URL 与模板目录路径）
EDITOR_VERSION_RE = re.compile(r"[A-Za-z0-9._-]+")
KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
# 敏感值唯一入口：与 compile step / config_hash / config-inject / prepare_presets
# 同名的固定 env，不经 argv（进程参数表会短暂暴露密钥）
ENCRYPTION_KEY_ENV = "SCRIPT_AES256_ENCRYPTION_KEY"


def encryption_key() -> str:
    return os.environ.get(ENCRYPTION_KEY_ENV, "")


def set_github_env(key: str, value: str) -> None:
    """写 $GITHUB_ENV；key 为空则跳过（`=value` 是非法 file-command，会让 step 失败）。"""
    if not key:
        return
    out = os.environ.get("GITHUB_ENV", "")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def cmd_validate_inputs(args) -> int:
    if not os.path.isdir(args.project_path):
        fail(f"project-path 不存在: {args.project_path}")
    if args.export_type == "custom_build":
        if not os.path.isdir(args.templates_dir):
            fail(f"custom_build 模板需提供 templates-dir: {args.templates_dir}")
    else:
        # official 模板由本 action 下载，templates-dir 仅确保存在
        os.makedirs(args.templates_dir, exist_ok=True)
    # 契约 export.paths 三字段（相对项目根）：resolve 归一化必传（默认值在契约
    # schema，这里不兜底），空值或绝对路径直接报错
    for name, value in (("export-presets-path", args.export_presets_path),
                        ("project-godot", args.project_godot),
                        ("dist-dir", args.dist_dir)):
        if not value:
            fail(f"{name} 为空：需由 resolve 归一化传入（契约 export.paths）")
        try:
            path_guard.check_rel_path(value, name)
        except path_guard.PathError as e:
            fail(str(e))
    source_presets = os.path.join(args.project_path, args.export_presets_path)
    if not os.path.isfile(source_presets):
        fail(f"presets 文件不存在: {source_presets}")
    set_github_env("SOURCE_PRESETS", source_presets)
    set_github_env("EXPORT_PRESETS_REL", args.export_presets_path)
    set_github_env("PROJECT_GODOT_REL", args.project_godot)
    set_github_env("DIST_REL", args.dist_dir)
    if not args.editor_version:
        fail("需指定 editor-version（官方编辑器版本）")
    # 与 resolve-export/scripts/resolve_export.py::assert_editor_version 同源（
    # 白名单对齐）：全串白名单 + 拒绝 '..' 段 + 拒绝 'v' 前缀（Godot_vv... 会 404）。
    if not EDITOR_VERSION_RE.fullmatch(args.editor_version) or ".." in args.editor_version:
        fail("editor-version 含非法字符或 '..' 段（仅允许 [A-Za-z0-9._-]）")
    if args.editor_version.startswith("v"):
        fail("editor-version 带了 'v' 前缀（会拼成 Godot_vv... 的 404 URL）；"
             "请写 '<N>.<N>[.<N>]-stable' 形态")
    key = encryption_key()
    if key and not KEY_RE.fullmatch(key):
        fail("encryption-key 必须恰好 64 位十六进制字符（32 字节）")
    return 0


def _download(url: str, dest: str) -> None:
    subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", dest, url], check=True)


def cmd_prepare_editor(args) -> int:
    # 预取目录由 resolve 作业统一下载，省掉各作业跨公网重复拉取
    zip_name = f"editor-{args.editor_platform}.zip"
    if args.prefetched and os.path.isfile(os.path.join(args.prefetched, zip_name)):
        shutil.copyfile(os.path.join(args.prefetched, zip_name), "editor.zip")
    else:
        _download(f"https://github.com/godotengine/godot/releases/download/"
                  f"{args.editor_version}/Godot_v{args.editor_version}_{args.editor_platform}.zip", "editor.zip")
    subprocess.run(["unzip", "-o", "editor.zip"], check=True)
    flat = f"Godot_v{args.editor_version}_{args.editor_platform}"
    if os.environ.get("RUNNER_OS") == "macOS" and os.path.isdir("Godot.app"):
        # macOS 官方编辑器为 .app bundle，可执行文件在 Contents/MacOS 下
        editor = os.path.join("Godot.app", "Contents", "MacOS", "Godot")
    else:
        editor = flat
    os.chmod(editor, 0o755)
    subprocess.run([os.path.abspath(editor), "--version"], check=True)
    set_github_env("EDITOR", os.path.abspath(editor))
    return 0


# 取 get_version_info() 四元组的探针脚本（--headless --script 跑一次 SceneTree 空壳）
VERSION_PROBE_GD = """\
extends SceneTree
func _initialize() -> void:
\tvar vi := Engine.get_version_info()
\tprint("major=%s" % str(vi["major"]))
\tprint("minor=%s" % str(vi["minor"]))
\tprint("patch=%s" % str(vi["patch"]))
\tprint("status=%s" % str(vi["status"]))
\tprint("build=%s" % str(vi["build"]))
\tquit()
"""


def parse_version_probe(stdout: str) -> dict:
    """解析探针脚本输出的 key=value 行，返回字段字典（值均为字符串）。"""
    fields = {}
    for line in stdout.splitlines():
        m = re.fullmatch(r"(major|minor|patch|status|build)=(.*)", line.strip())
        if m:
            fields[m.group(1)] = m.group(2)
    return fields


def probe_editor_version_info(editor: str) -> dict:
    """--headless --script 取 get_version_info() 四元组（互证数据源）；临时脚本进 RUNNER_TEMP。"""
    tmp = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    script = tmp / "godot_ci_version_probe.gd"
    script.write_text(VERSION_PROBE_GD, encoding="utf-8")
    proc = subprocess.run([editor, "--headless", "--script", str(script)],
                          capture_output=True, text=True, encoding="utf-8")
    fields = parse_version_probe(proc.stdout)
    missing = {"major", "minor", "patch", "status", "build"} - fields.keys()
    if proc.returncode != 0 or missing:
        fail(f"读取 get_version_info() 失败（exit={proc.returncode}，缺 {sorted(missing)}）:"
             f"{proc.stderr.strip()}")
    return fields


def derive_template_version_dir(version_output: str, info: dict) -> str:
    """模板版本目录名 = --version 输出剥离值，并与 get_version_info 四元组重建值互证。

    剥离（主值零复刻，编辑器产物事实）：尾段 9 位 hex（构建 hash，main.cpp 的
    get_full_version_string = FULL_BUILD + "." + hash.left(9)）→ 剥；尾段与
    get_version_info().build 相等（official）→ 剥；余串即 GODOT_VERSION_FULL_CONFIG
    （mono / .double 段天然保留，全程不依赖「patch=0 省略」等策略知识）。
    互证（校验器）：按 core/version.h 策略重建 NUMBER.STATUS（NUMBER = major.minor
    [.patch 非零]）——引擎改版本策略时剥离值与重建值双向暴露，不静默。
    """
    parts = version_output.strip().split(".")
    if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{9}", parts[-1]):
        parts = parts[:-1]
    build = str(info.get("build", ""))
    if build and parts and parts[-1] == build:
        parts = parts[:-1]
    stripped = ".".join(parts)
    patch = int(info.get("patch", 0))
    rebuilt = (f"{info['major']}.{info['minor']}"
               + (f".{patch}" if patch else "")
               + f".{info['status']}")
    if stripped != rebuilt:
        fail(f"模板版本目录互证失败：--version 剥离值 '{stripped}' 与 get_version_info 重建值 "
             f"'{rebuilt}' 不等（引擎版本策略可能已变，请核对 core/version.h 与 main.cpp 的输出构成）")
    return stripped


def cmd_prepare_templates(args) -> int:
    # 官方模式：下载官方导出模板整合包，解压到引擎默认模板目录，编辑器按版本自动匹配。
    # 目录根按 runner 系统选（引擎事实：EditorPaths.get_export_templates_dir()
    # = data_dir/export_templates，Linux= ~/.local/share/godot、macOS= ~/Library/Application
    # Support/Godot）；版本目录名 = --version 剥离值（GODOT_VERSION_FULL_CONFIG），
    # 推导与互证见 derive_template_version_dir。
    editor = os.environ.get("EDITOR", "")
    if not editor:
        fail("EDITOR 未设置（prepare-editor 未先运行？）")
    proc = subprocess.run([editor, "--version"], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        fail(f"读取编辑器 --version 失败（exit={proc.returncode}）：{proc.stderr.strip()}")
    base_version = derive_template_version_dir(proc.stdout, probe_editor_version_info(editor))
    print(f"editor --version={proc.stdout.strip()} → templates dir version={base_version}")

    home = Path(os.environ.get("HOME") or Path.home())
    runner_os = os.environ.get("RUNNER_OS", "")
    if runner_os == "Linux":
        templates_root = home / ".local" / "share" / "godot" / "export_templates"
    elif runner_os == "macOS":
        templates_root = home / "Library" / "Application Support" / "Godot" / "export_templates"
    else:
        fail(f"official 导出模板不支持 runner 系统 '{runner_os}'（CI 导出机仅 Linux / macOS）")
    global_dir = templates_root / base_version
    global_dir.mkdir(parents=True, exist_ok=True)
    if args.prefetched and os.path.isfile(os.path.join(args.prefetched, "export_templates.tpz")):
        shutil.copyfile(os.path.join(args.prefetched, "export_templates.tpz"), "export_templates.tpz")
    else:
        _download(f"https://github.com/godotengine/godot/releases/download/"
                  f"{args.editor_version}/Godot_v{args.editor_version}_export_templates.tpz", "export_templates.tpz")
    tmp = Path("tmp_templates")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    subprocess.run(["unzip", "-o", "export_templates.tpz", "-d", str(tmp)], check=True)
    os.remove("export_templates.tpz")

    # tpz 内可能还有一层 templates/ 子目录，先压平到 tmp_templates 根
    nested = tmp / "templates"
    if nested.is_dir():
        for name in os.listdir(nested):
            shutil.move(str(nested / name), str(tmp / name))

    # 按需平台筛选（默认保留全部）；官方模板命名形如 windows_release_x86_64.exe、
    # macos.zip。只按文件名包含平台关键字保留：宁可多留不可漏留，漏掉会把导出
    # 所需的模板删掉导致导出失败（例如 macos.zip 不匹配 *_macos*）。
    needed = [p.strip() for p in (args.official_templates_filter or "").split(",") if p.strip()]
    if not needed:
        for name in os.listdir(tmp):
            shutil.move(str(tmp / name), str(global_dir / name))
    else:
        for cur, _dirs, files in os.walk(tmp):
            for name in files:
                if any(p in name for p in needed):
                    shutil.move(os.path.join(cur, name), str(global_dir / name))
    shutil.rmtree(tmp, ignore_errors=True)

    print("--- 官方模板目录保留 ---")
    for name in sorted(os.listdir(global_dir)):
        print(name)
    return 0


def cmd_resolve_presets(args) -> int:
    # 预设名单唯一实现在 preset_config / resolve_export（resolve 归一化必传）；
    # 空 presets = 直调 composite 未归一化，fail-fast（不做行级 name= 自动解析）
    if not args.presets:
        fail("presets 为空：需由 resolve 归一化传入（契约 export.presets / presets-<平台> 输出）")
    set_github_env("PRESET_LIST", args.presets)
    return 0


def cmd_import_assets(args) -> int:
    """导出前导入资源：--import 失败直接 fail-fast，不再 `|| true` 吞掉。

    坏资源 / 陈旧 .godot 缓存会让后续导出产出错误产物或报远离根因的错；导入
    失败必须在这一步暴露，不能静默走到导出。
    """
    editor = os.environ.get("EDITOR") or fail(
        "EDITOR 未设置（prepare-editor 未先运行？）")
    project_path = args.project_path
    proc = subprocess.run(
        [editor, "--headless", "--path", project_path, "--import"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (proc.stdout + proc.stderr).strip()[-800:]
    if proc.returncode != 0:
        fail(f"导出前资源导入失败（--import 退出码 {proc.returncode}）；"
             f"输出尾部：{escape_error(tail)}")
    if tail:
        print(f"--- --import 输出尾部 ---\n{tail}")
    return 0


def run_editor(editor: str, editor_args: list, encryption_key: str, export_type: str) -> int:
    """单次编辑器调用；密钥仅在自编译模板（custom_build）且非空时经进程级 env
    传入（editor_export_platform.cpp 的 _get_script_encryption_key 读取），
    官方模板无编译期密钥。"""
    env = dict(os.environ)
    if export_type == "custom_build" and encryption_key:
        env["GODOT_SCRIPT_ENCRYPTION_KEY"] = encryption_key
    return subprocess.run([editor, *editor_args], env=env).returncode


def dispatch_each_preset(args, preset: str) -> int:
    """逐预设锚点 export-preset-each:after 的 dispatcher（无 bundle 时零成本短路）。

    bash 循环无法在循环内插 step，故由本执行器在导出循环内直接调 run_hooks.py
    dispatch；--current-preset 把当前预设名注入 context.export.current_preset。
    """
    if not getattr(args, "hooks_bundle", ""):
        return 0
    env = dict(os.environ)
    env["GODOT_HOOK_ROOT"] = os.path.join(
        os.environ.get("GITHUB_WORKSPACE", "."), ".godot-hooks",
        "export-preset-each-after")
    return subprocess.run([
        sys.executable, args.hooks_exec, "dispatch",
        "--bundle", getattr(args, "hooks_bundle_dir", "hooks-bundle"),
        "--anchor", "export-preset-each", "--phase", "after",
        "--context", getattr(args, "hooks_context", "context.json"),
        "--result", getattr(args, "hooks_result", "hooks-result.json"),
        "--chain", "export", "--current-preset", preset,
    ], env=env).returncode


def cmd_export_presets(args) -> int:
    editor = os.environ.get("EDITOR") or fail(
        "EDITOR 未设置（prepare-editor 未先运行？）")
    flag = "--export-debug" if args.target == "template_debug" else "--export-release"
    failed = []
    hook_failed = []
    for preset in (args.preset_list or "").split(","):
        preset = preset.strip()
        if not preset:
            continue
        print(f"==> {flag} 预设：{preset}")
        code = run_editor(editor,
                          ["--headless", "--path", args.project_path, flag, preset],
                          encryption_key=encryption_key(), export_type=args.export_type)
        if code == 0:
            print(f"==> 预设 {preset} 导出成功")
            rc = dispatch_each_preset(args, preset)
            if rc != 0:
                hook_failed.append(preset)
        else:
            print(f"::error::预设 {preset} 导出失败")
            failed.append(preset)
    dist = os.path.join(args.project_path, args.dist_dir)
    print(f"--- 导出产物目录（{args.dist_dir}/）---")
    if os.path.isdir(dist):
        for name in sorted(os.listdir(dist)):
            full = os.path.join(dist, name)
            size = os.path.getsize(full) if os.path.isfile(full) else 0
            print(f"{name}  {size}")
    if failed:
        fail(f"以下预设导出失败：{','.join(failed)}")
    if hook_failed:
        fail(f"以下预设的逐预设钩子（export-preset-each:after）失败：{','.join(hook_failed)}")
    print("全部预设导出成功")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate-inputs", help="入口校验并写 GITHUB_ENV 路径")
    v.add_argument("--project-path", required=True)
    v.add_argument("--export-type", required=True)
    v.add_argument("--editor-version", default="")
    v.add_argument("--templates-dir", required=True)
    v.add_argument("--export-presets-path", default="")
    v.add_argument("--project-godot", default="")
    v.add_argument("--dist-dir", default="")

    e = sub.add_parser("prepare-editor", help="获取并解压编辑器，暴露 EDITOR")
    e.add_argument("--editor-version", required=True, help="官方编辑器版本标签")
    e.add_argument("--editor-platform", required=True, help="官方编辑器下载平台（如 linux.x86_64）")
    e.add_argument("--prefetched", default="", help="预取目录（命中则跳过下载）")

    t = sub.add_parser("prepare-templates", help="官方 tpz 落位到默认模板目录")
    t.add_argument("--editor-version", required=True)
    t.add_argument("--official-templates-filter", default="", help="按需保留的平台关键字，逗号分隔（空=全部）")
    t.add_argument("--prefetched", default="")

    r = sub.add_parser("resolve-presets", help="预设名单透传（resolve 归一化必传）")
    r.add_argument("--presets", help="逗号分隔预设名（空= fail-fast，不做自动解析）")

    i = sub.add_parser("import-assets", help="导出前导入资源（失败即 fail-fast）")
    i.add_argument("--project-path", required=True)

    x = sub.add_parser("export-presets", help="逐个导出预设")
    x.add_argument("--project-path", required=True)
    x.add_argument("--target", required=True, help="template_release / template_debug")
    x.add_argument("--export-type", required=True)
    x.add_argument("--preset-list", default="", help="逗号分隔预设名（GITHUB_ENV PRESET_LIST）")
    x.add_argument("--dist-dir", required=True)
    x.add_argument("--hooks-bundle", default="",
                   help="扩展 bundle artifact 名（空=未启用，逐预设 dispatch 短路）")
    x.add_argument("--hooks-bundle-dir", default="hooks-bundle", help="bundle 解压目录")
    x.add_argument("--hooks-context", default="context.json", help="context.json 路径")
    x.add_argument("--hooks-result", default="hooks-result.json", help="hooks-result.json 路径")
    x.add_argument("--hooks-exec", default="", help="run_hooks.py 路径（逐预设 dispatcher）")

    args = parser.parse_args()
    return {
        "validate-inputs": cmd_validate_inputs,
        "prepare-editor": cmd_prepare_editor,
        "prepare-templates": cmd_prepare_templates,
        "resolve-presets": cmd_resolve_presets,
        "import-assets": cmd_import_assets,
        "export-presets": cmd_export_presets,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())