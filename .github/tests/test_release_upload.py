#!/usr/bin/env python3
"""release_upload.py 的零依赖自测：纯函数与 main 的 fail-fast 分支。

gh 编排（ensure_release_and_upload 的网络路径）不在本仓自测范围——行为验证依赖
三条发布链的真实发布；本文件覆盖「传给 gh 的参数组装」与「gh 调用之前的输入校验」。
所有 fail-fast 用例都发生在首个 gh 调用之前，不触网。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "actions" / "release-upload" / "scripts"))

from release_upload import (
    ReleaseUploadError,
    build_draft_args,
    build_notes_args,
    list_asset_files,
    main,
    validate_tag,
)
from testlib import expect_fail

def test_notes_generate_notes_wins():
    create, edit = build_notes_args(True, "文本", "/file")
    assert create == ["--generate-notes"], create
    # generate-notes 模式下已存在 Release 不刷新正文
    assert edit == [], edit

def test_notes_file_beats_text():
    create, edit = build_notes_args(False, "文本", "/file")
    assert create == ["--notes-file", "/file", "--notes", "文本"], create
    # notes 类输入同时参与刷新正文
    assert edit == create, edit

def test_notes_all_empty():
    create, edit = build_notes_args(False, "", "")
    assert create == [], create
    assert edit == [], edit

def test_draft_args():
    assert build_draft_args(True) == ["--draft"]
    assert build_draft_args(False) == []

def test_validate_tag_strips_and_rejects_empty():
    assert validate_tag(" v1.0.0 ") == "v1.0.0"
    try:
        validate_tag("")
        raise AssertionError("空 tag 未拒绝")
    except ReleaseUploadError as exc:
        assert "无法确定目标 Tag" in str(exc), exc
    try:
        validate_tag(None)
        raise AssertionError("None tag 未拒绝")
    except ReleaseUploadError as exc:
        assert "无法确定目标 Tag" in str(exc), exc

def test_list_asset_files(tmp):
    # 纯函数失败路径抛 ReleaseUploadError（main 层才转 ::error:: + SystemExit），
    # 故此处用 try/except 断言而非 expect_fail（其 capture 只捕 SystemExit）
    for tag, why in (("missing", "附件目录不存在"), ("empty", "附件目录为空")):
        target = os.path.join(tmp, tag)
        if why == "附件目录为空":
            os.mkdir(target)
        try:
            list_asset_files(target)
            raise AssertionError(f"{why}：未拒绝")
        except ReleaseUploadError as exc:
            assert why in str(exc), exc
    empty = os.path.join(tmp, "empty")
    Path(empty, "b.7z").write_bytes(b"x")
    Path(empty, "a.7z").write_bytes(b"y")
    files = list_asset_files(empty)
    assert files == [os.path.join(empty, "a.7z"), os.path.join(empty, "b.7z")], files
    # 平铺语义：子目录不算附件
    os.mkdir(os.path.join(empty, "sub"))
    assert len(list_asset_files(empty)) == 2, files

def test_main_fail_fast_before_any_gh_call(tmp):
    # 三个 fail-fast 分支都在首个 gh 调用之前（不触网）；经 main 走 ::error:: + 退出
    expect_fail(lambda: main(["--tag", "", "--assets-dir", tmp, "--repo", "r"]),
                "tag 为空", needle="无法确定目标 Tag")
    expect_fail(lambda: main(["--tag", "t", "--assets-dir", os.path.join(tmp, "missing"),
                              "--repo", "r"]),
                "附件目录缺失", needle="附件目录不存在")
    # 独立空目录：前一个测试已向 tmp/empty 写入附件，不能复用（否则用例穿透到 gh 调用）
    empty_main = os.path.join(tmp, "empty-main")
    os.makedirs(empty_main, exist_ok=True)
    expect_fail(lambda: main(["--tag", "t", "--assets-dir", empty_main, "--repo", "r"]),
                "附件目录为空", needle="附件目录为空")
    real = os.path.join(tmp, "real")
    os.mkdir(real)
    Path(real, "a.7z").write_bytes(b"x")
    expect_fail(lambda: main(["--tag", "t", "--assets-dir", real, "--repo", ""]),
                "repo 缺失", needle="--repo 未提供")

def main_test(tmp):
    test_main_fail_fast_before_any_gh_call(tmp)

cases_done = 0

def run_case(fn, *args):
    global cases_done
    fn(*args)
    cases_done += 1

with tempfile.TemporaryDirectory(prefix="release-upload-test-") as tmp:
    run_case(test_notes_generate_notes_wins)
    run_case(test_notes_file_beats_text)
    run_case(test_notes_all_empty)
    run_case(test_draft_args)
    run_case(test_validate_tag_strips_and_rejects_empty)
    run_case(test_list_asset_files, tmp)
    run_case(main_test, tmp)

print(f"release_upload 自测通过（{cases_done} 组）")
