#!/usr/bin/env python3
"""artifact_name.py 自测：两套命名模板的渲染判据。

覆盖：
- 编译产物名：占位符渲染与分隔符收敛
- 平台默认架构（android=arm64）与显式 arch 覆盖
- 变体追加与模板自带 {arch} 的后缀抑制
- 下载 pattern：多变体通配、单产物精确名
- 编译产物名未知占位符拒绝
- 导出产物名：{preset} 收敛 safe 名、默认不拼平台、未知占位符拒绝

用法：python .github/tests/test_artifact_name.py
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_name  # noqa: E402
from schema import load_schema, apply_defaults, validate  # noqa: E402
from testlib import expect_fail  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@check("前缀为空且模板用到 {prefix} → fail-fast（产物名会失去项目维度）")
def t_empty_prefix():
    naming = "{prefix}_{platform}_{target}"
    expect_fail(lambda: artifact_name.render(naming, "", "linux", "template_release"),
                "前缀为空", needle="产物名前缀为空")
    expect_fail(lambda: artifact_name.render(naming, "   ", "linux", "template_release"),
                "前缀为空白", needle="产物名前缀为空")
    # 模板不用 {prefix} 时前缀可有可无（不影响产物名）
    assert artifact_name.render("{platform}_{target}", "", "linux", "template_release") \
        == "linux_template_release"


@check("编译产物名渲染：占位符 / 后缀 / 未知占位符拒绝")
def t_artifact_name():
    naming = "{prefix}_{platform}_{target}"
    assert artifact_name.render(naming, "repo", "linux", "template_release") == "repo_linux_template_release"
    # android 默认 arm64
    assert artifact_name.render(naming, "repo", "android", "template_release") == "repo_android_template_release_arm64"
    # 显式 arch 覆盖默认
    assert artifact_name.render(naming, "repo", "android", "template_release", arch="x86_64") == "repo_android_template_release_x86_64"
    # 变体追加
    assert artifact_name.render(naming, "repo", "web", "template_release", variant="threads") == "repo_web_template_release_threads"
    # 模板含 {arch} 时以模板为准（取平台默认架构），不再追加后缀
    naming2 = "{prefix}_{platform}_{target}_{arch}"
    assert artifact_name.render(naming2, "repo", "android", "template_release") == "repo_android_template_release_arm64"
    assert artifact_name.render(naming2, "repo", "linux", "template_release") == "repo_linux_template_release"
    # pattern：多变体平台（基线 platform_variants 有两项）用通配
    assert artifact_name.pattern(naming, "repo", "web", "template_release",
                                 variants=["threads", "nothreads"]) == "repo_web_template_release_*"
    # pattern：单产物平台给精确名（不能是 `..._*`——字面下划线会要求产物名带它）
    assert artifact_name.pattern(naming, "repo", "windows", "template_release",
                                 variants=[]) == "repo_windows_template_release"
    assert artifact_name.pattern(naming, "repo", "android", "template_release",
                                 variants=[]) == "repo_android_template_release_arm64"
    # 只有一个变体时也按精确名
    assert artifact_name.pattern(naming, "repo", "web", "template_release",
                                 variants=["threads"]) == "repo_web_template_release_threads"
    # 未知占位符拒绝（含拒绝理由：可用占位符清单）
    expect_fail(lambda: artifact_name.render("{prefix}_{nope}", "repo", "linux",
                                             "template_release"),
                "未知占位符", needle="未知占位符 ['nope']")
    # 分隔符收敛
    assert artifact_name.render("{prefix}//{platform}", "repo", "linux", "t") == "repo_linux"


@check("CLI --variants：单值精确、多值通配、--pattern 与渲染同判据")
def t_cli_variants():
    script = Path(__file__).resolve().parents[1] / "scripts" / "artifact_name.py"

    def run(*extra):
        proc = subprocess.run([sys.executable, str(script),
                               "--build-naming", "{prefix}_{platform}_{target}",
                               "--prefix", "repo", "--platform", "web",
                               "--target", "template_release", *extra],
                              capture_output=True, text=True, encoding="utf-8", check=True)
        return proc.stdout.strip()

    assert run("--variants", "threads") == "repo_web_template_release_threads"
    assert run("--variants", "threads,nothreads") == "repo_web_template_release_*"
    assert run("--variants", "threads,nothreads", "--pattern") == "repo_web_template_release_*"
    assert run() == "repo_web_template_release"


@check("导出产物名：{preset} 收敛 safe、默认模板不拼平台")
def t_render_export():
    naming = "{prefix}_{preset}"
    # 预设名自带平台：默认模板不再拼 {platform}，避免 windows_windows_desktop
    assert artifact_name.render_export(
        naming, "demo", "windows", "Windows Desktop") == "demo_windows_desktop"
    assert artifact_name.render_export(
        naming, "demo", "windows", "Windows Desktop Embed Python"
    ) == "demo_windows_desktop_embed_python"
    assert artifact_name.render_export(
        naming, "demo", "macos", "macOS") == "demo_macos"
    assert artifact_name.render_export(
        naming, "demo", "linux", "Linux/X11") == "demo_linux_x11"
    # 预设名跨平台通用时改用含 {platform} 的模板
    naming2 = "{prefix}_{platform}_{preset}"
    assert artifact_name.render_export(
        naming2, "demo", "windows", "Release") == "demo_windows_release"
    assert artifact_name.render_export(
        naming2, "demo", "linux", "Release") == "demo_linux_release"
    # 分隔符收敛与空 prefix
    assert artifact_name.render_export("{preset}", "", "linux", "Linux X11") == "linux_x11"
    # 未知占位符拒绝（{target}/{arch} 属编译产物模板，不得混用；理由须点名该占位符）
    for bad, token in (("{prefix}_{target}", "target"),
                       ("{prefix}_{arch}", "arch"),
                       ("{prefix}_{nope}", "nope")):
        expect_fail(lambda b=bad: artifact_name.render_export(
            b, "demo", "linux", "Linux/X11"),
            f"导出模板混用 {token}", needle=f"未知占位符 ['{token}']")
    # 空模板拒绝
    expect_fail(lambda: artifact_name.render_export("", "demo", "linux", "Linux/X11"),
                "空模板", needle="模板为空")


@check("CLI --export-naming：渲染导出产物名，缺 --preset 报错")
def t_cli_export():
    script = Path(__file__).resolve().parents[1] / "scripts" / "artifact_name.py"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(script),
                           "--export-naming", "{prefix}_{preset}",
                           "--prefix", "demo", "--platform", "windows",
                           "--preset", "Windows Desktop Embed Python"],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, check=True)
    assert proc.stdout.strip() == "demo_windows_desktop_embed_python", proc.stdout
    missing = subprocess.run([sys.executable, str(script),
                              "--export-naming", "{prefix}_{preset}",
                              "--prefix", "demo", "--platform", "windows"],
                             capture_output=True, text=True, encoding="utf-8", env=env)
    assert missing.returncode != 0, "缺 --preset 未被拒绝"



@check("产物名白名单：shell 元字符 / $() / 反引号一律收敛为 '_'")
def t_whitelist():
    naming = "{prefix}_{platform}_{target}"
    # 恶意前缀：双引号内仍会展开的 $() / 反引号，以及词切分字符
    assert artifact_name.render(naming, "a$(id)b", "linux", "t") == "a_id_b_linux_t", \
        artifact_name.render(naming, "a$(id)b", "linux", "t")
    assert artifact_name.render(naming, "a`id`b", "linux", "t") == "a_id_b_linux_t"
    assert artifact_name.render(naming, "repo;rm -rf /", "linux", "t") == "repo_rm_-rf_linux_t"
    # 导出产物名同样收敛（预设名由 preset_safe 先处理，prefix 仍可能来自外部契约）
    assert artifact_name.render_export("{prefix}_{preset}", "a$(id)b", "linux", "Linux/X11") \
        == "a_id_b_linux_x11"
    # 合法字符全部保留
    assert artifact_name.render(naming, "My.Repo-1_2", "linux", "t") == "My.Repo-1_2_linux_t"


@check("pattern 多变体 '*' 不被白名单误收敛")
def t_whitelist_glob():
    naming = "{prefix}_{platform}_{target}"
    assert artifact_name.pattern(naming, "repo", "web", "template_release",
                                 variants=["threads", "nothreads"]) == "repo_web_template_release_*"


@check("契约 schema：artifacts.prefix 拒绝非法字符（resolve 期 fail-fast）")
def t_prefix_schema():
    schema = load_schema(str(Path(__file__).resolve().parents[1] / "contract" / "contract.schema.json"))
    for bad in ("a$(id)b", "a`id`b", "repo;rm", "a/b", "a b"):
        contract = {"artifacts": {"prefix": bad}}
        apply_defaults(contract, schema)
        errs = validate(contract, schema)
        assert any("prefix" in e for e in errs), (bad, errs)
    ok = {"artifacts": {"prefix": "My.Repo-1_2"}}
    apply_defaults(ok, schema)
    assert not any("prefix" in e for e in validate(ok, schema))


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