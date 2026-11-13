#!/usr/bin/env python3
"""testlib.py 自测：fail-fast 断言辅助的判据。

间接守护两件事：
- **不污染 CI 注解**：预期失败的输出被收进内存，不进 step 日志（否则 GitHub
  把它们抽成界面 annotation，每 step 只显示前 10 条 error，真实失败被挤掉）。
- **断言强度**：只断言「退出了」是弱断言，任何原因都能通过；必须校验拒绝理由。

用法：python .github/tests/test_testlib.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testlib import capture, expect_fail, expect_ok_silent  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def _failer(message: str):
    """模拟生产脚本的 fail()：打印 ::error:: 后 SystemExit(1)。"""
    def fn():
        print(f"::error::{message}", file=sys.stderr)
        sys.exit(1)
    return fn


def _ok_printer(message: str):
    def fn():
        print(f"::notice::{message}")
        return 0
    return fn


@check("capture：捕获 stdout/stderr/退出码/返回值")
def t_capture():
    def three():
        print("out")
        print("err", file=sys.stderr)
        return 42

    captured = capture(three)
    assert captured.code == 0, captured.code
    assert captured.value == 42, captured.value
    assert "out" in captured.raw_stdout and "err" in captured.raw_stderr

    # SystemExit(3) 记为退出码 3
    def raises():
        sys.exit(3)
    assert capture(raises).code == 3


@check("capture：剥掉 GitHub 注解前缀，保留可读文本")
def t_strip():
    captured = capture(_failer("预设文件不存在"))
    assert captured.code == 1
    assert captured.text == "预设文件不存在", captured.text
    assert "::error::" not in captured.text


@check("expect_fail：失败 + 理由匹配 → 通过")
def t_expect_fail_ok():
    captured = expect_fail(_failer("预设文件不存在"), "缺预设", needle="不存在")
    assert captured.code == 1


@check("expect_fail：未失败 → 报错（弱断言反例）")
def t_expect_fail_should_fail():
    try:
        expect_fail(lambda: 0, "本该失败却成功")
    except AssertionError as e:
        assert "未拒绝" in str(e), e
    else:
        raise AssertionError("未失败的场景应抛 AssertionError")


@check("expect_fail：理由不符 → 报错并给出实际输出")
def t_expect_fail_wrong_reason():
    try:
        expect_fail(_failer("完全是别的错"), "缺预设", needle="不存在")
    except AssertionError as e:
        msg = str(e)
        assert "拒绝理由不符" in msg and "完全是别的错" in msg, msg
    else:
        raise AssertionError("理由不符时应抛 AssertionError")


@check("expect_ok_silent：成功且无 ::error:: → 通过")
def t_ok_silent():
    expect_ok_silent(lambda: 0, "正常路径")
    expect_ok_silent(_ok_printer("进度通知"), "notice 不算 error")


@check("expect_ok_silent：正向路径泄漏 ::error:: → 报错")
def t_ok_silent_leak():
    try:
        expect_ok_silent(lambda: print("::error::正向路径不该报错"), "泄漏")
    except AssertionError as e:
        assert "泄漏" in str(e), e
    else:
        raise AssertionError("泄漏 ::error:: 时应抛 AssertionError")


@check("expect_ok_silent：意外失败 → 报错")
def t_ok_silent_fail():
    try:
        expect_ok_silent(_failer("炸了"), "本该成功")
    except AssertionError as e:
        assert "意外失败" in str(e), e
    else:
        raise AssertionError("意外失败时应抛 AssertionError")


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
