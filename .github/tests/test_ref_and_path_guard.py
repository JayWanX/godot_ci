#!/usr/bin/env python3
"""ref_guard / path_guard 自测：两个入口守卫的判据边界。

两者都是「纵深防线」：ref 会拼进 git 命令行，相对路径会决定钩子与补丁的落点。
判据错松的代价是参数注入或越界写入，错紧的代价是合法项目无法构建——两个方向都要钉。

覆盖：
- ref：前导/尾 '/'、首字符 '.'、任何段以 '.' 开头、段尾 '.lock'、连续 '//'、'@{'、'..'
- path：绝对路径、反斜杠、'.'/'..'/空段、Windows 保留设备名、段尾点与空格
- root：规范化逃逸、符号链接、junction（Windows）

用法：python .github/tests/test_ref_and_path_guard.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from path_guard import PathError, _is_link, check_rel_path  # noqa: E402
from ref_guard import assert_valid_ref  # noqa: E402
from testlib import capture  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def ref_ok(ref) -> bool:
    """ref 是否被放行（assert_valid_ref 非法时走 sys.exit，故取退出码判定）。"""
    captured = capture(lambda: assert_valid_ref(ref, "demo"))
    return captured.code == 0


@check("ref：合法形态全部放行")
def t_ref_valid():
    for ref in ("main", "v1.0.0", "4.7.2-stable", "release/1.2", "feature/x_y",
                "a" * 40, "refs/heads/main", "v1.0.0-rc1"):
        assert ref_ok(ref), f"合法 ref 被拒：{ref}"


@check("ref：非法形态全部拒绝")
def t_ref_invalid():
    cases = [
        ("-upload", "以 '-' 开头（git 会当选项）"),
        ("--upload-pack=x", "长选项注入"),
        (".hidden", "整体以 '.' 开头"),
        ("/main", "以 '/' 开头"),
        ("main/", "以 '/' 结尾"),
        ("a//b", "连续 '//'（空段）"),
        ("a/.hidden", "中间段以 '.' 开头"),
        ("a/b.lock", "中间段以 '.lock' 结尾"),
        ("main.lock", "整体以 '.lock' 结尾"),
        ("a..b", "含 '..'"),
        ("main@{1}", "含 '@{'"),
        ("main branch", "含空格"),
        ("main~1", "含 '~'"),
        ("main^", "含 '^'"),
        ("main:ref", "含 ':'"),
        ("main?", "含 '?'"),
        ("main*", "含 '*'"),
        ("main[0]", "含 '['"),
    ]
    for ref, why in cases:
        assert not ref_ok(ref), f"非法 ref 被放行：{ref}（{why}）"


@check("path：合法相对路径放行")
def t_path_valid():
    for rel in ("build/profile.py", "a/b/c.txt", "gen/**".rstrip("*").rstrip("/"),
                "project.godot", "a.b/c.d"):
        assert check_rel_path(rel, "demo") == rel, rel


@check("path：非法形态全部拒绝")
def t_path_invalid():
    cases = [
        ("", "空"),
        ("   ", "空白"),
        ("/abs/x", "绝对路径"),
        ("C:/x", "Windows 盘符"),
        ("a\\b", "反斜杠"),
        ("a/../b", "含 '..'"),
        ("./a", "含 '.' 段"),
        ("a//b", "连续 '/'"),
        ("a/", "尾 '/'"),
        ("/a", "首 '/'"),
        # Windows 落点陷阱
        ("CON", "保留设备名 CON"),
        ("nul.txt", "保留设备名 NUL.txt"),
        ("a/com1/b", "中间段是保留名 COM1"),
        ("a/LPT9", "保留名 LPT9"),
        ("a/b. /c", "段尾空格"),
        ("a/b./c", "段尾点"),
    ]
    for rel, why in cases:
        try:
            check_rel_path(rel, "demo")
        except PathError as e:
            assert "demo" in str(e), e
        else:
            raise AssertionError(f"非法路径被放行：{rel!r}（{why}）")


@check("path + root：逃逸 / 符号链接拒绝")
def t_path_root():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "ok.txt").write_text("x", encoding="utf-8")
        # 合法：存在且不越界
        assert check_rel_path("sub/ok.txt", "demo", root) == "sub/ok.txt"
        # 合法：尚不存在的目标（部署期才创建）也放行——只做形态与父链检查
        assert check_rel_path("sub/new.txt", "demo", root) == "sub/new.txt"
        # 越界：符号链接目录把落点挪到根外
        outside = Path(td) / "outside"
        outside.mkdir()
        try:
            os.symlink(str(outside), str(root / "link"), target_is_directory=True)
        except (OSError, NotImplementedError, TypeError):
            return  # 平台不允许创建符号链接（Windows 无权限时）：跳过其余断言
        try:
            check_rel_path("link/x.txt", "demo", root)
        except PathError as e:
            assert "符号链接" in str(e) or "逃逸" in str(e) or "盘符" in str(e), e
        else:
            raise AssertionError("经符号链接目录的路径未拒绝")


@check("path：junction（Windows 重解析点）同样视为链接")
def t_path_junction():
    """junction 的 is_symlink() 为假，但落点一样会被重定向。"""
    class FakeJunctionPath:
        def is_symlink(self):
            return False

        def is_junction(self):
            return True

    fake = FakeJunctionPath()
    real_is_symlink = FakeJunctionPath.is_symlink
    real_is_junction = FakeJunctionPath.is_junction
    try:
        # 直接验证判定函数本身：不依赖平台能否创建 junction。
        # path_guard._is_link 接受一个「像 Path 的对象」，故用鸭子类型替身。
        assert _is_link(fake) is True, "is_junction() 为真的路径未被识别为链接"
        assert _is_link(Path(".")) is False, "普通路径被误判为链接"
    finally:
        FakeJunctionPath.is_symlink = real_is_symlink
        FakeJunctionPath.is_junction = real_is_junction


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
