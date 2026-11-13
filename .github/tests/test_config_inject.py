#!/usr/bin/env python3
"""config_inject.py 自测：config-inject 执行器的落点判据。

覆盖：
- place local: 复制 + GITHUB_OUTPUT 绝对路径输出
- place local: 路径逃逸（..）拒绝、缺 project-root 拒绝
- place remote: 非法形态（ref 注入 / 坏 owner / 缺文件路径）在解析层拒绝，不触网
- place remote: 克隆接线（mock clone_shallow：repo/ref/子模块开关透传、文件抓取）
- modules: local 检出 + register_types 注册名改名（.h / .cpp 回退）
- modules: 目标目录冲突、缺前缀、重复条目、ref 注入拒绝
- check-key: 空 / 非 hex / 长度错 / 合法

用法：python .github/tests/test_config_inject.py
"""
import os
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions" / "config-inject" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_inject as ci  # noqa: E402
from testlib import expect_fail  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def expect_exit(fn, why, needle=""):
    """兼容旧调用点：转调 testlib.expect_fail（捕获输出 + 断言理由）。"""
    return expect_fail(fn, why, needle=needle)


def with_github_output(fn):
    """在临时 GITHUB_OUTPUT 下运行 fn，返回 (返回值, 输出文件内容)。"""
    fd, p = tempfile.mkstemp()
    os.close(fd)
    old = os.environ.get("GITHUB_OUTPUT")
    os.environ["GITHUB_OUTPUT"] = p
    try:
        result = fn()
        content = Path(p).read_text(encoding="utf-8")
        return result, content
    finally:
        if old is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = old
        os.unlink(p)


@check("place local: 复制 + 输出绝对路径")
def t_place_local():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "proj" / "sub").mkdir(parents=True)
        (root / "proj" / "sub" / "profile.py").write_text("x = 1\n", encoding="utf-8")
        ns = Namespace(source="local:sub/profile.py", dest=str(root / "profile.py"),
                       label="profile", output_key="scons-profile-path",
                       project_root=str(root / "proj"))
        code, out = with_github_output(lambda: ci.cmd_place(ns))
        assert code == 0
        assert (root / "profile.py").read_text(encoding="utf-8") == "x = 1\n"
        val = out.split("=", 1)[1].strip()
        assert Path(val) == root / "profile.py", val


@check("place local: '..' 逃逸 / 缺 project-root 拒绝")
def t_place_local_reject():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ns = Namespace(source="local:../escape", dest=str(root / "x.py"),
                       label="profile", output_key="", project_root=str(root))
        expect_exit(lambda: ci.cmd_place(ns), ".. 逃逸")
        ns2 = Namespace(source="local:sub/x.py", dest=str(root / "x.py"),
                        label="profile", output_key="", project_root="")
        expect_exit(lambda: ci.cmd_place(ns2), "缺 project-root")


@check("place local: output_key 为空时不写 GITHUB_OUTPUT（空键会让 step 失败）")
def t_place_no_output_key():
    # 空 key 会写出 `=value`，GitHub file-command 解析器取键得到 null，
    # 整条 output 命令失败 → step 非零退出（"Value cannot be null. (Parameter 'name')"）。
    # 这正是 CI Self-check 长期失败的原因，故必须有回归用例钉住。
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "proj").mkdir(parents=True)
        (root / "proj" / "profile.py").write_text("y = 2\n", encoding="utf-8")
        ns = Namespace(source="local:profile.py", dest=str(root / "profile.py"),
                       label="profile", output_key="", project_root=str(root / "proj"))
        code, out = with_github_output(lambda: ci.cmd_place(ns))
        assert code == 0
        assert out == "", f"空 output_key 仍写入了 GITHUB_OUTPUT：{out!r}"
        # 目录汇总路径同样不得写空键（模块需 register_types 才被接受，走成功路径）
        mod = root / "proj" / "mods" / "m1"
        mod.mkdir(parents=True)
        (mod / "register_types.h").write_text(
            "void initialize_m1_module();\n", encoding="utf-8")
        (mod / "SCsub").write_text("", encoding="utf-8")
        ns2 = Namespace(entries="local:mods/m1",
                        dest_root=str(root / "custom_modules"),
                        label="custom_modules", output_key="",
                        project_root=str(root / "proj"))
        code2, out2 = with_github_output(lambda: ci.cmd_modules(ns2))
        assert code2 == 0
        assert out2 == "", f"空 output_key 仍写入了 GITHUB_OUTPUT：{out2!r}"


@check("place remote: 非法形态在解析层拒绝（不触网）")
def t_place_remote_reject():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for source, why in (
            ("remote:o/r@--upload-pack=calc", "ref 选项注入"),
            ("remote:justname", "缺 owner 段"),
            ("remote:godotengine/godot", "缺文件路径"),
        ):
            ns = Namespace(source=source, dest=str(root / "x.py"),
                           label="profile", output_key="", project_root="")
            expect_exit(lambda: ci.cmd_place(ns), why)


@check("place remote: 克隆接线（mock：repo/ref/子模块开关透传、文件抓取）")
def t_place_remote_mock():
    captured = {}

    def fake_clone(repo, ref, dest, with_submodules):
        captured["args"] = (repo, ref, with_submodules)
        (dest / "cfg").mkdir(parents=True)
        (dest / "cfg" / "profile.py").write_text("from remote\n", encoding="utf-8")

    orig = ci.clone_shallow
    ci.clone_shallow = fake_clone
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ns = Namespace(source="remote:me/repo@v1.2.3:cfg/profile.py",
                           dest=str(root / "profile.py"),
                           label="profile", output_key="", project_root="")
            assert ci.cmd_place(ns) == 0
            assert captured["args"] == ("me/repo", "v1.2.3", False), captured
            assert (root / "profile.py").read_text(encoding="utf-8") == "from remote\n"
    finally:
        ci.clone_shallow = orig


@check("modules: local 检出 + register_types.h 注册名改名")
def t_modules_rename_h():
    _run_rename_case(use_cpp=False)


@check("modules: register_types.cpp 回退解析")
def t_modules_rename_cpp():
    _run_rename_case(use_cpp=True)


def _run_rename_case(use_cpp: bool):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mod = root / "proj" / "mods" / "repo_dir"
        mod.mkdir(parents=True)
        reg = mod / ("register_types.cpp" if use_cpp else "register_types.h")
        reg.write_text("// comment\nvoid initialize_coolmod_module();\n", encoding="utf-8")
        (mod / "SCsub").write_text("", encoding="utf-8")
        ns = Namespace(entries="local:mods/repo_dir",
                       dest_root=str(root / "custom_modules"),
                       project_root=str(root / "proj"), output_key="custom-modules-dir")
        code, out = with_github_output(lambda: ci.cmd_modules(ns))
        assert code == 0
        final = root / "custom_modules" / "coolmod"
        assert (final / "SCsub").is_file(), "改名后目录内容缺失"
        assert not (root / "custom_modules" / "repo_dir").exists(), "旧目录残留"
        assert "custom-modules-dir=" in out


@check("modules: 目标目录冲突 / 缺前缀 / 重复 / ref 注入拒绝")
def t_modules_reject():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 注册名=末段名（都不改名）：第二个条目的 clone_name 目标必撞第一个
        for sub in ("mods/a", "mods2/a"):
            d = root / "proj" / sub
            d.mkdir(parents=True)
            (d / "register_types.h").write_text(
                "void initialize_a_module();\n", encoding="utf-8")
        base = dict(dest_root=str(root / "custom_modules"),
                    project_root=str(root / "proj"), output_key="")
        expect_exit(lambda: ci.cmd_modules(Namespace(entries="local:mods/a,local:mods2/a", **base)),
                    "两条目末段同名且不改名（目标目录冲突）")
        expect_exit(lambda: ci.cmd_modules(Namespace(entries="mods/a", **base)),
                    "缺 remote:/local: 前缀")
        expect_exit(lambda: ci.cmd_modules(Namespace(entries="local:mods/a,local:mods/a", **base)),
                    "条目重复")
        expect_exit(lambda: ci.cmd_modules(Namespace(entries="remote:o/r@--upload-pack=calc", **base)),
                    "ref 选项注入")


@check("check-key: 空 / 非 hex / 长度错 / 合法（密钥走 env，不经 argv，）")
def t_check_key():
    old = os.environ.get("SCRIPT_AES256_ENCRYPTION_KEY")
    try:
        os.environ.pop("SCRIPT_AES256_ENCRYPTION_KEY", None)
        assert ci.cmd_check_key(Namespace()) == 0
        for bad in ("xyz", "0" * 63 + "g", "a" * 63, "a" * 65):
            os.environ["SCRIPT_AES256_ENCRYPTION_KEY"] = bad
            expect_exit(lambda: ci.cmd_check_key(Namespace()), f"非法密钥 {bad!r}")
        os.environ["SCRIPT_AES256_ENCRYPTION_KEY"] = "a" * 64
        assert ci.cmd_check_key(Namespace()) == 0
    finally:
        os.environ.pop("SCRIPT_AES256_ENCRYPTION_KEY", None)
        if old is not None:
            os.environ["SCRIPT_AES256_ENCRYPTION_KEY"] = old


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
