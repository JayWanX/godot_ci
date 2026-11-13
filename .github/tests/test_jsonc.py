#!/usr/bin/env python3
"""jsonc.py 自测：strip_jsonc 是全部契约与清单文件的唯一加载入口，
边界错剥（字符串内的 // 与 ,）是静默值损坏，此处直接锁死关键边界。

用法：python .github/tests/test_jsonc.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jsonc import read_json_optional, strip_jsonc  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def parsed(text):
    return json.loads(strip_jsonc(text))


@check("字符串内 // 不误剥（URL 值）")
def t_url():
    data = parsed('{"u": "http://github.com/o/r"}')
    assert data == {"u": "http://github.com/o/r"}, data


@check("字符串内 /* 与 // 组合不误剥")
def t_comment_marks_in_string():
    data = parsed('{"a": "x/*y//z"}')
    assert data == {"a": "x/*y//z"}, data


@check("转义引号不提前结束字符串")
def t_escape():
    data = parsed('{"a": "say \\"hi//\\""}')
    assert data == {"a": 'say "hi//"'}, data


@check("行注释、块注释跨行、尾逗号组合")
def t_combo():
    text = """{
        // 全行注释
        "a": 1, // 行尾注释
        /* 块注释
           跨两行 */
        "b": [1, 2,],
    }"""
    assert parsed(text) == {"a": 1, "b": [1, 2]}, parsed(text)


@check("块注释先到先得，注释内的 // 被一并吞掉")
def t_block_swallows_line():
    assert parsed('{"a": 1 /* // */ }') == {"a": 1}


@check("read_json_optional：文件缺失返回 None（不 sys.exit）")
def t_optional_missing():
    with tempfile.TemporaryDirectory() as td:
        assert read_json_optional(str(Path(td) / "nope.json"), "测试") is None


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
