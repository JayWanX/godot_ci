#!/usr/bin/env python3
"""preset_name.py 自测：预设名 → safe 名的唯一实现 + 打包矩阵一致性守卫。

规则：小写；非 [a-z0-9_] 替换 '_'；连续 '_' 收敛；首尾 '_' 去除；
空结果 fail-fast（不静默兜底）。

用法：python .github/tests/test_preset_name.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from preset_name import preset_safe, validate_packages  # noqa: E402
from testlib import expect_fail  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@check("常规映射：空格与斜杠 → 下划线")
def t_basic():
    assert preset_safe("Windows Desktop") == "windows_desktop"
    assert preset_safe("Linux/X11") == "linux_x11"


@check("连续分隔符收敛、首尾下划线去除")
def t_collapse_and_strip():
    assert preset_safe("A--B") == "a_b"
    assert preset_safe("_x_") == "x"
    assert preset_safe("  Web  ") == "web"


@check("点与数字保留语义：v1.2 → v1_2")
def t_dots_and_digits():
    assert preset_safe("v1.2") == "v1_2"
    assert preset_safe("Android_arm64") == "android_arm64"


@check("空 / 全符号无法收敛 → fail-fast（不静默兜底）")
def t_empty_fails():
    for bad in ("", "///", "___"):
        expect_fail(lambda b=bad: preset_safe(b), f"无法收敛 {bad!r}",
                    needle="无法收敛出安全目录名")


PACKAGES_OK = ('[{"preset":"Windows Desktop","preset-safe":"windows_desktop",'
               '"package":"demo_windows_desktop"},'
               '{"preset":"Linux/X11","preset-safe":"linux_x11",'
               '"package":"demo_linux_x11"}]')


@check("validate-packages：一致（顺序无关）通过")
def t_validate_ok():
    assert validate_packages("Windows Desktop,Linux/X11", PACKAGES_OK) == 0


@check("validate-packages 拒绝矩阵（逐项核对拒绝理由）")
def t_validate_rejects():
    cases = [
        ("", PACKAGES_OK, "preset-names 为空", "presets 不能为空"),
        ("A,B", '{"a": 1}', "packages 非数组", "必须是 JSON 数组"),
        ("A,B", "not json", "packages 非法 JSON", "不是合法 JSON"),
        ("Windows Desktop", PACKAGES_OK, "条目集合与 presets 不一致", "不一致"),
        # preset-safe 重复（同平台产物子目录重叠）
        ("A,A", '[{"preset":"A","preset-safe":"a","package":"p1"},'
                '{"preset":"A","preset-safe":"a","package":"p2"}]',
         "preset-safe 重复", "preset-safe 含重复项"),
        # package 重复（Release 附件同名互相覆盖）
        ("A,B", '[{"preset":"A","preset-safe":"a","package":"same"},'
                '{"preset":"B","preset-safe":"b","package":"same"}]',
         "package 重复", "package 含重复项"),
        # 缺字段
        ("A", '[{"preset":"A"}]', "缺字段", "缺字段"),
    ]
    for presets, packages, why, needle in cases:
        expect_fail(lambda p=presets, k=packages: validate_packages(p, k), why,
                    needle=needle)


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
