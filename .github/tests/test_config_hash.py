#!/usr/bin/env python3
"""config_hash.py 自测：config-hash 是 SCons 缓存键的明文摘要源。

核心判据：
- 确定性：同输入必同输出（缓存键稳定性）
- 感知变化：输入变 → 键变（缓存不得误命中），含模块树里的二进制文件
- 树序无关：模块目录文件创建顺序不影响键（rglob 顺序无保证，代码内排序）
- 未提供 = 空串；显式传入却读不到 = fail-fast（不得与「未提供」同键）
- encryption-key 只参与摘要：换 key 必 miss（输出恒为 8 位 hex，原值不可能外泄）

用法：python .github/tests/test_config_hash.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG_HASH = Path(__file__).resolve().parents[1] / "actions" / "build-pipeline" / "scripts" / "config_hash.py"

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def run_hash(**kw) -> str:
    cmd = [sys.executable, str(CONFIG_HASH)]
    # 密钥走固定名 env，不经 argv
    env = dict(os.environ)
    key = kw.pop("encryption_key", None)
    if key is not None:
        env["SCRIPT_AES256_ENCRYPTION_KEY"] = str(key)
    for k, v in kw.items():
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         check=True, env=env)
    return out.stdout.strip()


def run_hash_expect_fail(**kw) -> str:
    """跑一次并断言失败；返回合并输出（供断言失败理由）。"""
    cmd = [sys.executable, str(CONFIG_HASH)]
    for k, v in kw.items():
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode != 0, f"本应 fail-fast，实际成功：{out.stdout!r}"
    return out.stdout + out.stderr


@check("确定性：同输入两次运行同输出，且为 8 位十六进制")
def t_deterministic():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "profile.py").write_text('d3d12 = "no"\n', encoding="utf-8")
        h1 = run_hash(profile=str(root / "profile.py"), scons_flags="a=b")
        h2 = run_hash(profile=str(root / "profile.py"), scons_flags="a=b")
        assert h1 == h2, (h1, h2)
        assert re.fullmatch(r"[0-9a-f]{8}", h1), h1


@check("树序无关：同内容不同创建顺序（含嵌套）→ 同键")
def t_tree_order_independent():
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a", Path(td) / "b"
        for root, order in ((a, ["z.txt", "m/n.bin", "m/a.txt", "b.txt"]),
                            (b, ["b.txt", "m/a.txt", "z.txt", "m/n.bin"])):
            (root / "m").mkdir(parents=True)
            for rel in order:
                (root / rel).write_text(rel, encoding="utf-8")
        assert run_hash(modules=str(a)) == run_hash(modules=str(b))


@check("感知变化：profile 内容变 / 模块树加文件 → 键变")
def t_senses_changes():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "profile.py").write_text('d3d12 = "no"\n', encoding="utf-8")
        mods = root / "mods"
        mods.mkdir()
        (mods / "x.cpp").write_text("int x;\n", encoding="utf-8")
        base = run_hash(profile=str(root / "profile.py"), modules=str(mods))
        (root / "profile.py").write_text('d3d12 = "yes"\n', encoding="utf-8")
        after_profile = run_hash(profile=str(root / "profile.py"), modules=str(mods))
        assert base != after_profile, "profile 内容变化未反映到键"
        (mods / "y.cpp").write_text("int y;\n", encoding="utf-8")
        after_tree = run_hash(profile=str(root / "profile.py"), modules=str(mods))
        assert after_profile != after_tree, "模块树新增文件未反映到键"


@check("二进制文件变化进摘要：模块树里的 .bin 改写 → 键变")
def t_binary_tree_change():
    with tempfile.TemporaryDirectory() as td:
        mods = Path(td) / "mods"
        mods.mkdir()
        (mods / "mod.cpp").write_text("int x;\n", encoding="utf-8")
        (mods / "blob.bin").write_bytes(bytes(range(256)) * 4)
        base_hash = run_hash(modules=str(mods))
        # 同长度、非 UTF-8 的字节改写：旧实现解码失败退化成空串，键纹丝不动
        (mods / "blob.bin").write_bytes(bytes(reversed(range(256))) * 4)
        assert base_hash != run_hash(modules=str(mods)), \
            "二进制文件内容变化未反映到键（解码失败被当成空内容）"


@check("显式路径读不到 → fail-fast（不得与『未提供』同键）")
def t_missing_explicit_fails():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ghost_file = str(root / "nope.py")
        ghost_tree = str(root / "no_mods")
        # 未提供（空串）是合法的「未配置」语义：不崩，且等价于全空输入
        assert run_hash() == run_hash(profile="", modules="")
        # 显式传入却读不到 = 配置指向错误：必须失败，否则与上面同键
        out = run_hash_expect_fail(profile=ghost_file)
        assert "不存在" in out, out
        out = run_hash_expect_fail(modules=ghost_tree)
        assert "不存在" in out, out
        # 目录当文件传也是读不到
        (root / "adir").mkdir()
        out = run_hash_expect_fail(profile=str(root / "adir"))
        assert "不存在" in out, out


@check("encryption-key 参与摘要：换 key → 键变")
def t_key_participates():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "profile.py").write_text("x = 1\n", encoding="utf-8")
        kw = dict(profile=str(root / "profile.py"))
        h1 = run_hash(**kw, encryption_key="secret-a")
        h2 = run_hash(**kw, encryption_key="secret-b")
        assert h1 != h2, "换 encryption-key 未反映到键"


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