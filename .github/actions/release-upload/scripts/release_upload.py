#!/usr/bin/env python3
"""Release 附件发布（release-upload action 的实现）：确保 Release 存在并上传附件目录。

逻辑分层：
- 纯函数（build_notes_args / build_draft_args / validate_tag / list_asset_files）——
  tests/test_release_upload.py 零依赖自测覆盖；
- gh 编排（ensure_release_and_upload）——subprocess 调 gh，参数由纯函数组装；
  gh 的网络 I/O 不在本仓自测范围，行为验证依赖三条发布链的真实发布。

幂等语义：Release 不存在 → 按 create 参数创建；已存在且有 notes 类输入 → 刷新正文
（--generate-notes 仅创建时有效，已存在不刷新）；附件无条件 --clobber 上传。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


class ReleaseUploadError(Exception):
    """配置/输入错误：由 main 转成 ::error:: + 非零退出。"""


def fail(message: str) -> None:
    print(f"::error::{message}", flush=True)
    sys.exit(1)


def build_notes_args(generate_notes: bool, create_notes: str, create_notes_file: str):
    """创建与刷新正文参数组装。

    优先级：generate-notes > notes-file > notes；全空返回两组空数组（创建不带正文
    参数，gh 自动生成默认说明）。edit 组仅 notes 类输入参与——gh release edit
    不支持 --generate-notes，generate-notes 模式下已存在 Release 不刷新正文。
    """
    create: list[str] = []
    edit: list[str] = []
    if generate_notes:
        create.append("--generate-notes")
        return create, edit
    if create_notes_file:
        create += ["--notes-file", create_notes_file]
        edit += ["--notes-file", create_notes_file]
    if create_notes:
        create += ["--notes", create_notes]
        edit += ["--notes", create_notes]
    return create, edit


def build_draft_args(draft: bool) -> list[str]:
    return ["--draft"] if draft else []


def validate_tag(tag: str | None) -> str:
    tag = (tag or "").strip()
    if not tag:
        raise ReleaseUploadError("无法确定目标 Tag（调用方解析结果为空）")
    return tag


def list_asset_files(assets_dir: str) -> list[str]:
    """平铺目录的附件文件（非递归、排序稳定）；目录缺失或无文件即报错。"""
    if not os.path.isdir(assets_dir):
        raise ReleaseUploadError(f"附件目录不存在：{assets_dir}")
    names = sorted(
        n for n in os.listdir(assets_dir)
        if os.path.isfile(os.path.join(assets_dir, n))
    )
    if not names:
        raise ReleaseUploadError(f"附件目录为空：{assets_dir}")
    return [os.path.join(assets_dir, n) for n in names]


def _gh(args: list[str], repo: str) -> None:
    proc = subprocess.run(["gh", *args, "--repo", repo])
    if proc.returncode != 0:
        raise ReleaseUploadError(f"gh {' '.join(args[:2])} 退出码 {proc.returncode}")


def ensure_release_and_upload(
    tag: str,
    files: list[str],
    repo: str,
    create_args: list[str],
    edit_args: list[str],
    draft_args: list[str],
) -> None:
    exists = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not exists:
        print(f"Release {tag} 不存在，创建中...", flush=True)
        _gh(["release", "create", tag, "--title", tag, *create_args, *draft_args], repo)
    elif edit_args:
        print(f"Release {tag} 已存在，刷新正文", flush=True)
        _gh(["release", "edit", tag, *edit_args], repo)
    else:
        print(f"Release {tag} 已存在，跳过创建", flush=True)
    for f in files:
        print(f"上传：{os.path.basename(f)}", flush=True)
        _gh(["release", "upload", tag, f, "--clobber"], repo)
    _gh(["release", "view", tag, "--json", "assets",
         "--jq", '.assets[] | "\\(.name)  \\(.size) bytes"'], repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确保 Release 存在并上传附件目录")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--assets-dir", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--create-notes", default="")
    parser.add_argument("--create-notes-file", default="")
    parser.add_argument("--generate-notes", action="store_true")
    parser.add_argument("--draft", action="store_true")
    args = parser.parse_args(argv)
    try:
        tag = validate_tag(args.tag)
        files = list_asset_files(args.assets_dir)
        if not args.repo:
            raise ReleaseUploadError("--repo 未提供且 GITHUB_REPOSITORY 为空")
        create_args, edit_args = build_notes_args(
            args.generate_notes, args.create_notes, args.create_notes_file)
        ensure_release_and_upload(
            tag, files, args.repo, create_args, edit_args, build_draft_args(args.draft))
    except ReleaseUploadError as exc:
        fail(str(exc))
    print("发布完成", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
