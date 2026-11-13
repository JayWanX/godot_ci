"""apply_hook_patches.py 端到端自测：profile_patch 与 source_patch 真的落到目标上。

覆盖：
- profile_patch 应用到注入的 profile.py（内容确实变了）
- source_patch 应用到引擎源码根（文件确实变了）
- 不适用的补丁 → 明确失败，不留半改状态
- 缺输出 → 空操作

用法：python .github/tests/test_apply_hook_patches.py
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply_hook_patches as ahp  # noqa: E402
from testlib import expect_fail  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def make_patch(base: Path, rel: str, old: str, new: str) -> Path:
    """生成一份 unified diff（a/<rel> / b/<rel>）。

    注意：git apply 逐字节比对，补丁的原文行必须与目标文件完全一致
    （含行尾换行），否则干跑与应用都会失败。
    """
    patch = base / "x.patch"
    patch.write_text(
        f"--- a/{rel}\n+++ b/{rel}\n@@ -1,1 +1,1 @@\n-{old}\n+{new}\n",
        encoding="utf-8", newline="\n")
    return patch


def channel_dir(tmp: Path) -> Path:
    """模拟真实通道根：<workspace>/.godot-hooks/<anchor-phase>/directives/patches。"""
    d = tmp / ".godot-hooks" / "cache-restore-before" / "directives" / "patches"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dir_digest(d: Path) -> str:
    """与 collect_outputs 同款：目录摘要 = 各文件摘要拼接后的 sha256。"""
    parts = [hashlib.sha256(f.read_bytes()).hexdigest()
             for f in sorted(p for p in d.rglob("*") if p.is_file())]
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def result_file(tmp: Path, kind: str, patch: Path) -> Path:
    """把补丁放进真实形态的通道目录，并写带正确 sha256 的结果记录（消费侧重验）。"""
    d = channel_dir(tmp)
    dst = d / patch.name
    shutil.copy2(patch, dst)
    r = tmp / "hooks-result.json"
    r.write_text(json.dumps({
        "outputs": {kind: {"path": str(d), "sha256": dir_digest(d)}},
    }), encoding="utf-8")
    return r


@check("profile_patch 真的改到目标文件")
def t_profile():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        profile = tmp / "profile.py"
        # 目标与补丁都必须是 LF：Windows 上 write_text 默认把 \n 转成 CRLF，
        # 而 git apply 逐字节比对，行尾不一致就 "patch does not apply"。
        profile.write_text("optimize = 'speed'\n", encoding="utf-8", newline="\n")
        patch = make_patch(tmp, "profile.py", "optimize = 'speed'", "optimize = 'size_extra'")
        res = result_file(tmp, "profile_patch", patch)
        n = ahp.apply_profile_patch(json.loads(res.read_text(encoding="utf-8")), str(profile))
        assert n == 1, n
        assert "size_extra" in profile.read_text(encoding="utf-8"), profile.read_text()


@check("source_patch 真的改到引擎源码树")
def t_source():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        root = tmp / "engine"
        (root / "modules").mkdir(parents=True)
        target = root / "modules" / "demo" / "x.cpp"
        target.parent.mkdir(parents=True)
        target.write_text("int old_value = 1;\n", encoding="utf-8", newline="\n")
        patch = make_patch(tmp, "modules/demo/x.cpp", "int old_value = 1;", "int new_value = 2;")
        res = result_file(tmp, "source_patch", patch)
        n = ahp.apply_source_patch(json.loads(res.read_text(encoding="utf-8")), str(root))
        assert n == 1, n
        assert "new_value" in target.read_text(encoding="utf-8"), target.read_text()


@check("不适用的补丁 → 失败且不改动目标")
def t_mismatch():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        profile = tmp / "profile.py"
        profile.write_text("optimize = 'speed'\n", encoding="utf-8", newline="\n")
        before = profile.read_text(encoding="utf-8")
        patch = make_patch(tmp, "other.py", "a", "b")
        res = result_file(tmp, "profile_patch", patch)
        expect_fail(
            lambda: ahp.apply_profile_patch(json.loads(res.read_text(encoding="utf-8")),
                                            str(profile)),
            "不适用补丁", needle="不适用")
        assert profile.read_text(encoding="utf-8") == before, "失败后目标被改动"


@check("缺输出 → 空操作")
def t_no_output():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        profile = tmp / "profile.py"
        profile.write_text("x = 1\n", encoding="utf-8", newline="\n")
        assert ahp.apply_profile_patch({}, str(profile)) == 0
        assert ahp.apply_source_patch({}, str(tmp)) == 0


@check("目标不存在 → 明确失败")
def t_missing_target():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        patch = make_patch(tmp, "profile.py", "a", "b")
        res = result_file(tmp, "profile_patch", patch)
        expect_fail(
            lambda: ahp.apply_profile_patch(json.loads(res.read_text(encoding="utf-8")),
                                            str(tmp / "nope.py")),
            "目标不存在", needle="不存在")


@check("target 空 + 无产出 → 空操作（未注入 profile 的空转）")
def t_target_empty_no_output():
    assert ahp.apply_profile_patch({}, "") == 0


@check("target 空 + 有产出 → 明确失败（补丁无处可落）")
def t_target_empty_with_output():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        patch = make_patch(tmp, "profile.py", "a", "b")
        res = result_file(tmp, "profile_patch", patch)
        expect_fail(
            lambda: ahp.apply_profile_patch(json.loads(res.read_text(encoding="utf-8")), ""),
            "有产出但 target 为空", needle="无处可落")


@check("消费侧重验：篡改 sha256 → 拒绝")
def t_tamper_sha():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        profile = tmp / "profile.py"
        profile.write_text("optimize = 'speed'\n", encoding="utf-8", newline="\n")
        patch = make_patch(tmp, "profile.py", "optimize = 'speed'", "optimize = 'size_extra'")
        res = result_file(tmp, "profile_patch", patch)
        data = json.loads(res.read_text(encoding="utf-8"))
        data["outputs"]["profile_patch"]["sha256"] = "deadbeef"
        expect_fail(lambda: ahp.apply_profile_patch(data, str(profile)),
                    "篡改 sha256 未被消费侧拒绝", needle="sha256 不匹配")


@check("消费侧重验：path 指向通道根之外的任意路径 → 拒绝")
def t_tamper_path():
    with tempfile.TemporaryDirectory(prefix="ahp_test_") as td:
        tmp = Path(td)
        profile = tmp / "profile.py"
        profile.write_text("optimize = 'speed'\n", encoding="utf-8", newline="\n")
        patch = make_patch(tmp, "profile.py", "optimize = 'speed'", "optimize = 'size_extra'")
        res = result_file(tmp, "profile_patch", patch)
        data = json.loads(res.read_text(encoding="utf-8"))
        # 伪造一个 runner 上的敏感路径：即便 sha256 重算过，也不在通道根内
        evil = tmp / "secrets"
        evil.mkdir()
        (evil / "token.txt").write_text("secret", encoding="utf-8")
        rec = data["outputs"]["profile_patch"]
        rec["path"] = str(evil)
        rec["sha256"] = dir_digest(evil)
        expect_fail(lambda: ahp.apply_profile_patch(data, str(profile)),
                    "越界 path 未被消费侧拒绝", needle=".godot-hooks")


def main() -> int:
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print(f"[ok] {name}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, e))
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
