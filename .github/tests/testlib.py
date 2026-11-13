#!/usr/bin/env python3
"""自测公共断言：fail-fast 用例必须「按预期理由」失败，且不污染 CI 注解通道。

两条判据缺一不可：

1. **捕获输出**：被测脚本通过 `fail()` 打印 `::error::<消息>`。GitHub 的日志扫描器
   会把这些标记抽成界面的 annotation——预期失败的用例因此变成界面噪声，且每 step
   只显示前 10 条 error，真实失败会被挤掉。测试用 contextlib.redirect_stderr/
   stdout 把它们收进内存，只在断言失败时才回显。
2. **断言理由**：只断言「退出了」是弱断言——任何原因（拼错路径、KeyError 包装成
   SystemExit、逻辑写歪）都能让它通过。必须校验消息里含预期片段，把「它退出了」
   升级成「它按预期理由退出了」。

用法：
    from testlib import expect_fail, expect_ok_silent

    # 断言失败且消息含 '不存在'
    expect_fail(lambda: resolve(...), "预设文件不存在", needle="不存在")

    # 断言成功且没有 ::error:: 泄漏（正向路径不该报错）
    expect_ok_silent(lambda: collect(src, dist))
"""
import contextlib
import io
import sys


def _strip_annotations(text: str) -> str:
    """剥掉 GitHub 注解前缀，保留人类可读的消息。"""
    out = []
    for line in text.splitlines():
        for prefix in ("::error::", "::warning::", "::notice::", "::debug::"):
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        out.append(line)
    return "\n".join(out)


class Captured:
    """一次捕获的结果：退出码、返回值、原始输出、剥掉注解前缀的可读文本。"""

    def __init__(self, code, stdout: str, stderr: str, value=None):
        self.code = code
        self.value = value
        self.raw_stdout = stdout
        self.raw_stderr = stderr
        self.text = _strip_annotations(stdout + stderr).strip()

    def __bool__(self):  # 兼容 `if captured:` 写法
        return self.code == 0


def capture(fn) -> Captured:
    """运行 fn，捕获 stdout/stderr、返回值与「退出状态」。

    code 的语义：fn 正常返回 → 0（返回的整数当业务返回值，不当退出码）；
    fn 抛 SystemExit → 取其 code。这样 `cmd_xxx(...)` 这类返回 int 的函数
    与 `fail()` 终止的路径都能用同一个 .code 判定成功/失败。
    """
    out, err = io.StringIO(), io.StringIO()
    code = 0
    value = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            value = fn()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return Captured(code, out.getvalue(), err.getvalue(), value)


def expect_fail(fn, why: str, needle: str = "") -> Captured:
    """断言 fn 失败；needle 非空时还要求消息含该片段。

    返回 Captured，便于调用方追加断言。断言失败时把捕获内容一并抛出，
    这样本地/CI 都能直接看到真实原因（而非只看到「未拒绝」）。
    """
    captured = capture(fn)
    if captured.code == 0:
        raise AssertionError(f"未拒绝：{why}（场景本应 fail-fast，实际正常返回）")
    if needle and needle not in captured.text:
        raise AssertionError(
            f"拒绝理由不符：{why}\n  期望消息含：{needle!r}\n  实际输出：{captured.text!r}")
    return captured


def expect_ok_silent(fn, why: str = "") -> Captured:
    """断言 fn 成功，且没有 ::error:: 泄漏到输出（正向路径不应报错）。"""
    captured = capture(fn)
    label = f"（{why}）" if why else ""
    if captured.code != 0:
        raise AssertionError(
            f"意外失败{label}：退出码 {captured.code}\n  输出：{captured.text!r}")
    leaked = [ln for ln in (captured.raw_stdout + captured.raw_stderr).splitlines()
              if ln.startswith("::error::")]
    if leaked:
        raise AssertionError(
            f"正向路径泄漏了 ::error::{label}：\n  " + "\n  ".join(leaked))
    return captured
