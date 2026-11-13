#!/usr/bin/env python3
"""config-inject 执行器（由 action.yml 各 step 按子命令调用）。

来源声明语法与校验唯一源在 scripts/godot_context.py（parse_source_entry /
assert_safe_relpath / assert_modules），ref 白名单在 scripts/ref_guard.py——
此处不含第二份规则，也不含 shell 解释面（git 一律经列表参数调用）。

子命令：
  place      注入单个文件（profile.py / project.gdbuild）：local: 从项目工作区复制，
             remote: 浅克隆远程仓库后抓取仓库内文件
  modules    检出/复制自定义模块到汇总目录，按 register_types 注册名重命名
  check-key  Godot PCK 加密密钥格式校验（不落盘、不写 GITHUB_ENV）

鉴权：INPUT_REPO_TOKEN 经进程级 GIT_CONFIG_* 注入 insteadOf 重写（不写 ~/.gitconfig、
不落盘），仅对脚本内 git 子进程生效；token 为空 = 仅公开仓库。
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
import godot_context  # noqa: E402
import git_ops  # noqa: E402

MODULE_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
REGISTER_RE = re.compile(r"void\s+initialize_([A-Za-z0-9_]+)_module\s*\(")


def notice(message: str) -> None:
    print(f"::notice::{escape_error(message)}")


def clone_shallow(repo: str, ref: str, dest: Path, with_submodules: bool) -> None:
    """浅克隆唯一实现在 scripts/git_ops.py；本包装保留子命令的调用形状。"""
    git_ops.clone_shallow(repo, ref, dest,
                          os.environ.get("INPUT_REPO_TOKEN", ""), with_submodules)


def set_output(key: str, value: str) -> None:
    """写 $GITHUB_OUTPUT；key 为空则不写。

    空 key 会写出 `=value` 这一行，GitHub 的 file-command 解析器按 ``name=value``
    取键，得到 null 后整条 output 命令失败（"Value cannot be null. (Parameter
    'name')"），整个 step 以非零退出。--output-key 是可选参数，未传时就该静默跳过。
    """
    if not key:
        return
    out = os.environ.get("GITHUB_OUTPUT", "")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def cmd_place(args) -> int:
    source = args.source
    if not source:
        notice(f"{args.label}: 未注入（输入为空）。")
        return 0
    dest = Path(args.dest)
    parsed = godot_context.parse_source_entry(source, args.label)
    if parsed["kind"] == "local":
        if not args.project_root:
            fail(f"{args.label}: local: 来源需要 project-root 输入。")
        src = Path(args.project_root) / parsed["path"]
        if not src.is_file():
            fail(f"{args.label}: local: 来源文件不存在：{src}")
        shutil.copyfile(src, dest)
    else:
        if not parsed["path"]:
            fail(f"{args.label}: remote: 来源须给出仓库内文件路径（remote:<owner/repo>[@<ref>]:<path>）。")
        with tempfile.TemporaryDirectory(prefix="fetch_repo_file_") as td:
            src_root = Path(td) / "src"
            clone_shallow(parsed["repo"], parsed["ref"], src_root, with_submodules=False)
            src = src_root / parsed["path"]
            if not src.is_file():
                fail(f"{args.label}: remote: '{parsed['path']}' 在 "
                     f"{parsed['repo']}@{parsed['ref'] or 'default'} 中不存在。")
            shutil.copyfile(src, dest)
    abs_dest = os.path.abspath(str(dest))
    set_output(args.output_key, abs_dest)
    notice(f"{args.label} 已写入 {abs_dest}")
    return 0


def parse_module_name(module_dir: Path, entry: str) -> str:
    """从 register_types.h / .cpp 解析 initialize_<name>_module（.h 优先）。

    目录名即 scons 模块 id，必须与注册名一致，不一致时由调用方改名。
    """
    for reg in ("register_types.h", "register_types.cpp"):
        p = module_dir / reg
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = REGISTER_RE.search(text)
        if m:
            return m.group(1)
    fail(f"custom_modules: 条目 '{entry}' 无法从 register_types.h / .cpp 解析 "
         f"initialize_<name>_module，不是合法 Godot 模块。")


def cmd_modules(args) -> int:
    entries = [e.strip() for e in args.entries.split(",") if e.strip()]
    if not entries:
        return 0
    # 语法与字面重复先整体 fail-fast（零成本），再逐条执行检出
    godot_context.assert_modules(entries, "custom_modules")
    dest_root = Path(args.dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    project_root = Path(args.project_root) if args.project_root else None
    with tempfile.TemporaryDirectory(prefix="checkout_modules_") as td:
        tmp = Path(td)
        for entry in entries:
            parsed = godot_context.parse_source_entry(entry, "custom_modules")
            if parsed["kind"] == "remote":
                print(f"::group::检出自定义模块 {entry}")
                clone_name = parsed["repo"].rsplit("/", 1)[1]
                src_dir = tmp / clone_name
                clone_shallow(parsed["repo"], parsed["ref"], src_dir, with_submodules=True)
                if parsed["path"]:
                    sub = src_dir / parsed["path"]
                    if not sub.is_dir():
                        fail(f"custom_modules: 条目 '{entry}' 的 path 子目录在仓库中不存在。")
                    src_dir = sub
            else:
                if project_root is None:
                    fail("custom_modules: local: 条目需要 project-root 输入。")
                src_dir = project_root / parsed["path"]
                if not src_dir.is_dir():
                    fail(f"custom_modules: 条目 '{entry}' 的目录 {src_dir} 不存在。")
                clone_name = parsed["path"].rsplit("/", 1)[1]
                print(f"::group::复制自定义模块 {entry}")
            target = dest_root / clone_name
            if target.exists():
                fail(f"custom_modules: 目标目录 {target} 已存在（条目 '{entry}' 与先前条目冲突）。")
            shutil.copytree(src_dir, target)
            name = parse_module_name(target, entry)
            if not MODULE_NAME_RE.fullmatch(name):
                fail(f"custom_modules: 模块名 '{name}' 非法，应为字母/数字/下划线。")
            final_dir = target
            if clone_name != name:
                final_dir = dest_root / name
                if final_dir.exists():
                    fail(f"custom_modules: 目标目录 {final_dir} 已存在（模块名冲突）。")
                target.rename(final_dir)
            notice(f"模块目录 {final_dir}（来源 {entry}）")
            print("::endgroup::")
    set_output(args.output_key, os.path.abspath(str(dest_root)))
    return 0


def cmd_check_key(args) -> int:
    # 敏感值走固定名 env：不经 argv，避免进程参数表短暂暴露密钥
    key = os.environ.get("SCRIPT_AES256_ENCRYPTION_KEY", "")
    if not key:
        notice("encryption_key: 未设置（输入为空）。")
        return 0
    if not re.fullmatch(r"[0-9a-fA-F]+", key):
        fail("encryption_key 必须是十六进制字符串。")
    if len(key) != 64:
        fail(f"encryption_key 必须恰好 64 位十六进制字符（32 字节），当前 {len(key)} 位。")
    notice("encryption_key 格式校验通过（64 个十六进制字符），由编译 step 进程级注入")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("place", help="注入单个文件（local: 复制 / remote: 浅克隆抓取）")
    p.add_argument("--source", default="", help="来源声明（local:<path> / remote:<owner/repo>[@<ref>][:<path>]）")
    p.add_argument("--dest", required=True, help="写入的目标文件名（相对当前工作目录）")
    p.add_argument("--label", required=True, help="报错前缀（profile / build-profile）")
    p.add_argument("--output-key", default="", help="写入 GITHUB_OUTPUT 的字段名（绝对路径）")
    p.add_argument("--project-root", default="", help="local: 来源的解析基准（项目工作区根）")

    m = sub.add_parser("modules", help="检出/复制自定义模块并按注册名重命名")
    m.add_argument("--entries", default="", help="逗号分隔的来源条目")
    m.add_argument("--dest-root", required=True, help="汇总目录名（相对当前工作目录）")
    m.add_argument("--project-root", default="", help="local: 条目的解析基准")
    m.add_argument("--output-key", default="", help="写入 GITHUB_OUTPUT 的字段名（汇总目录绝对路径）")

    k = sub.add_parser("check-key", help="PCK 加密密钥格式校验（从 SCRIPT_AES256_ENCRYPTION_KEY 读）")

    args = parser.parse_args()
    try:
        if args.command == "place":
            return cmd_place(args)
        if args.command == "modules":
            return cmd_modules(args)
        return cmd_check_key(args)
    except subprocess.CalledProcessError as e:
        fail(f"git {' '.join(e.cmd)} 失败（exit {e.returncode}）")


if __name__ == "__main__":
    sys.exit(main())