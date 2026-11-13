#!/usr/bin/env python3
"""collect_release_assets.py 自测：Release 附件收集与同名冲突消解策略。

覆盖：
- 平铺布局（单 artifact 下载）与子目录布局（多 artifact）统一收集
- 同名冲突：平台（artifact 目录名）前缀消解
- 前缀后仍冲突：base.N.ext 序号递增（占名顺序按路径排序确定）
- 空产物 / src 不存在 → fail-fast
- CLI --dist 指定收集目录

用法：python .github/tests/test_collect_release_assets.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_release_assets import collect  # noqa: E402
from testlib import expect_fail  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def expect_exit(fn, why, needle=""):
    """转调 testlib.expect_fail：捕获输出（不污染 CI 注解）+ 断言拒绝理由。"""
    return expect_fail(fn, why, needle=needle)


def touch(root: Path, rel: str, content="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@check("平铺布局：单 artifact 下载内容直接收集")
def t_flat():
    with tempfile.TemporaryDirectory() as td:
        src, dist = Path(td) / "artifacts", Path(td) / "assets"
        for name in ("a.exe", "b.zip", "c.7z"):
            touch(src, name)
        assert collect(src, dist) == 3
        assert sorted(p.name for p in dist.iterdir()) == ["a.exe", "b.zip", "c.7z"]


@check("子目录布局：artifact 目录名只作平台归属，文件直接落 dist 根")
def t_nested():
    with tempfile.TemporaryDirectory() as td:
        src, dist = Path(td) / "artifacts", Path(td) / "assets"
        touch(src, "export-windows/game.exe")
        touch(src, "export-linux/godot")
        assert collect(src, dist) == 2
        # 无冲突时 base 名直接落 dist 根（plat 只在冲突时用作前缀）
        assert sorted(p.name for p in dist.iterdir()) == ["game.exe", "godot"]


@check("同名冲突：第二份加 artifact 目录名前缀")
def t_conflict_prefix():
    with tempfile.TemporaryDirectory() as td:
        src, dist = Path(td) / "artifacts", Path(td) / "assets"
        touch(src, "p1/game.exe")
        touch(src, "p2/game.exe")
        assert collect(src, dist) == 2
        names = sorted(p.name for p in dist.iterdir())
        assert names == ["game.exe", "p2-game.exe"], names  # 排序后 p1 先占 base 名


@check("前缀后仍冲突：base.N.ext 序号递增（dist 预置占名）")
def t_conflict_numbering():
    with tempfile.TemporaryDirectory() as td:
        src, dist = Path(td) / "artifacts", Path(td) / "assets"
        dist.mkdir()
        (dist / "game.exe").write_text("held", encoding="utf-8")
        (dist / "p2-game.exe").write_text("held", encoding="utf-8")
        touch(src, "p1/game.exe")
        touch(src, "p2/game.exe")
        assert collect(src, dist) == 2
        names = sorted(p.name for p in dist.iterdir())
        # 预置 2 个占位 + 收集 2 个：p1 → 前缀 p1-game.exe；p2 → 前缀已占 → 序号
        assert names == ["game.exe", "p1-game.exe", "p2-game.1.exe", "p2-game.exe"], names


@check("空产物 / src 不存在 → fail-fast")
def t_empty_fail():
    with tempfile.TemporaryDirectory() as td:
        src, dist = Path(td) / "artifacts", Path(td) / "assets"
        src.mkdir()
        expect_exit(lambda: collect(src, dist), "空产物目录",
                    needle="未下载到任何导出产物")
        expect_exit(lambda: collect(Path(td) / "nope", dist), "src 不存在",
                    needle="不存在")


@check("CLI --dist：收集目录由 --dist 指定")
def t_cli_dist():
    with tempfile.TemporaryDirectory() as td:
        src, dist = Path(td) / "artifacts", Path(td) / "out-assets"
        touch(src, "game.exe")
        script = Path(__file__).resolve().parents[1] / "scripts" / "collect_release_assets.py"
        subprocess.run([sys.executable, str(script), "--src", str(src),
                        "--dist", str(dist)], capture_output=True, text=True,
                       encoding="utf-8", check=True,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        assert (dist / "game.exe").is_file(), sorted(p.name for p in dist.iterdir())


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