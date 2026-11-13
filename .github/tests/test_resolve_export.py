#!/usr/bin/env python3
"""resolve_export.py fail-fast 判据自测：入口归一化与安全面。

覆盖：
- 名单与 cfg 全部匹配的正常通过
- 预设不存在 / cfg 路径错 / project_godot 路径错 → fail-fast
- editor_version 与引擎 major.minor 错配 → fail-fast
- ref 参数注入（--upload-pack）→ fail-fast
- project_godot 报错含具体路径

用法：python .github/tests/test_resolve_export.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RESOLVE = Path(__file__).resolve().parents[1] / "actions" / "resolve-export" / "scripts" / "resolve_export.py"
SHA = "a" * 40

CONTRACT = """{
  "ci": { "repository": "o/ci", "ref": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" },
  "engine": { "source": "godotengine/godot@4.7.2-stable",
              "target": ["template_release"], "platform": ["windows", "linux"] },
  "build": { "paths": {}, "scons_flags": "", "compat": {"standard_includes": "auto"}, "toolchain": {} },
  "export": { "type": "custom_build", "editor_version": "4.7.2-stable",
              "presets": ["Windows Desktop", "Linux/X11"],
              "paths": { "project_godot": "{project_godot}", "export_presets": "{export_presets}", "dist": "bin" } },
  "artifacts": { "prefix": "", "build_naming": "{prefix}_{platform}_{target}", "retention": {} },
  "hooks": { "manifest": ".github/ci-hooks.json", "policy": "declared" }
}"""

CFG = """[preset.0]
name="Windows Desktop"
platform="Windows Desktop"
export_path="bin/game.exe"

[preset.1]
name="Linux/X11"
platform="Linux/X11"
export_path="bin/game.exe"
"""


def make_project(tmp: Path, contract: str) -> Path:
    (tmp / ".github").mkdir()
    (tmp / ".github" / "ci-contract.json").write_text(contract, encoding="utf-8")
    (tmp / "export_presets.cfg").write_text(CFG, encoding="utf-8")
    (tmp / "project.godot").write_text("", encoding="utf-8")
    return tmp


DEFAULT_CONTRACT = (CONTRACT.replace("{export_presets}", "export_presets.cfg")
                    .replace("{project_godot}", "project.godot"))


def run(env_extra: dict) -> subprocess.CompletedProcess:
    # 清理清单必须列全 resolve_export 读取的每一个输入变量：宿主环境里若恰好存在
    # 同名变量（本地 shell 导出的 INPUT_*），不清理就会渗入用例，造成「本机能过、
    # 干净 runner 上挂」这类最难查的假绿/假红。
    controlled = (
        "GITHUB_OUTPUT", "GITHUB_ENV", "CALLER_USES_REF", "ENGINE_SHA",
        "INPUT_PROJECT", "INPUT_PRESETS", "INPUT_EXPORT_TYPE", "INPUT_EDITOR_VERSION",
        "INPUT_SCONS_FLAGS", "INPUT_PROFILE", "INPUT_BUILD_PROFILE", "INPUT_REPO_TOKEN",
        "REPO_SAFE", "ARTIFACT_PREFIX", "EXPORT_PRESETS_ACTUAL",
        "CURRENT_REPO", "CURRENT_SHA", "RUNNER_OS", "PROJECT_ROOT", "CI_CONTRACT",
    )
    env = {k: v for k, v in os.environ.items() if k not in controlled}
    env.update({
        "INPUT_PROJECT": "", "INPUT_PRESETS": "", "INPUT_EXPORT_TYPE": "",
        "INPUT_EDITOR_VERSION": "4.7.2-stable",
        "INPUT_SCONS_FLAGS": "", "INPUT_PROFILE": "", "INPUT_BUILD_PROFILE": "",
        "INPUT_REPO_TOKEN": "",
        "CURRENT_REPO": "me/proj", "CURRENT_SHA": SHA, "RUNNER_OS": "Linux",
    })
    env.update(env_extra)
    return subprocess.run([sys.executable, str(RESOLVE)], capture_output=True,
                          text=True, encoding="utf-8", env=dict(env, PYTHONIOENCODING="utf-8"), cwd=tempfile.gettempdir())


def case(name, expect_ok, stderr_needle=(), stdout_needle=(), setup=None, env_extra=None):
    with tempfile.TemporaryDirectory() as td:
        contract = setup(Path(td)) if setup else None
        project = make_project(Path(td), contract or DEFAULT_CONTRACT)
        env_extra = dict(env_extra or {})
        env_extra.setdefault("CI_CONTRACT", str(project / ".github" / "ci-contract.json"))
        env_extra.setdefault("PROJECT_ROOT", str(project))
        proc = run(env_extra)
        ok = proc.returncode == 0
        status = "OK  " if ok == expect_ok else "FAIL"
        print(f"{status} {name}")
        if ok != expect_ok:
            print(proc.stdout, proc.stderr)
            return False
        for needle in stderr_needle:
            if needle not in proc.stderr:
                print(f"     stderr 缺少: {needle!r}\n{proc.stderr}")
                return False
        for needle in stdout_needle:
            if needle not in proc.stdout:
                print(f"     stdout 缺少: {needle!r}\n{proc.stdout}")
                return False
    return True


def main() -> int:
    results = [
        case("通过：名单与 cfg 全部匹配且自有项目 external-project=false", True,
             stdout_needle=["packages-windows=", "external-project=false",
                            # 平台配置（含契约覆写）：导出矩阵据此取 export_runner /
                            # editor_archive。此处只断言形状，不锁具体档案值——
                            # 档案是基线里的策略取值（改原生导出时会换），
                            # 逐字断言会让合法的基线改动误报。去重正确性由
                            # test_godot_context 的夹具用例单独覆盖。
                            "platforms-json={",
                            "editor-archives-matrix=[{",
                            # 完整导出（选中 2 个 = cfg 全部 2 个）→ 允许清理陈旧附件
                            "export-complete=true"]),
        case("预设名不存在（自有项目构建）→ fail-fast", False,
             stderr_needle=["不存在于", "NoSuchPreset"],
             env_extra={"INPUT_PRESETS": "Windows Desktop,NoSuchPreset"}),
        case("export_presets 路径写错 → fail-fast", False,
             stderr_needle=["预设文件不存在"],
             setup=lambda t: CONTRACT.replace("export_presets.cfg", "wrong/presets.cfg")),
        case("版本错配（4.7 vs 4.6）→ fail-fast", False,
             stderr_needle=["major.minor 不一致"],
             env_extra={"INPUT_EDITOR_VERSION": "4.6.1-stable"}),
        case("ref 参数注入（--upload-pack）→ fail-fast", False,
             stderr_needle=["非法 ref"],
             env_extra={"INPUT_PROJECT": "remote:evil/repo@--upload-pack=calc"}),
        case("project 入参缺 remote:/local: 前缀 → fail-fast", False,
             stderr_needle=["remote:/local:"],
             env_extra={"INPUT_PROJECT": "evil/repo@main"}),
        case("project_godot 路径写错 → fail-fast", False,
             stderr_needle=["project_godot"],
             setup=lambda t: (CONTRACT.replace('{export_presets}', 'export_presets.cfg')
                              .replace('{project_godot}', 'missing.godot'))),
        # 编辑器版本白名单在 resolve 期（prefetch / 官方模板下载之前）生效
        case("editor-version 带 'v' 前缀 → resolve 期 fail-fast", False,
             stderr_needle=["'v' 前缀", "Godot_vv"],
             env_extra={"INPUT_EDITOR_VERSION": "v4.7.2-stable"}),
        case("editor-version 含非法字符（shell 元字符）→ resolve 期 fail-fast", False,
             stderr_needle=["非法字符"],
             env_extra={"INPUT_EDITOR_VERSION": "4.7.2-stable;rm -rf /"}),
        case("editor-version 含 '..' 段 → resolve 期 fail-fast", False,
             stderr_needle=["'..' 段"],
             env_extra={"INPUT_EDITOR_VERSION": "4.7.2..stable"}),
        case("合法 editor-version → 放行（回归保护）", True,
             stdout_needle=["editor-version=4.7.2-stable"]),
        # 自有项目不克隆自己（external-project=false）。外部项目分支会触发
        # git_ops.clone_shallow（联网），不在本离线套件跑；其下传判据由
        # test_real_wiring.py 静态断言 export.yml:306 覆盖。
        case("自有项目：external-project=false（不克隆自己）", True,
             stdout_needle=["external-project=false"]),
    ]

    # project_godot 的 fail 在导出路径段（cfg 校验之后），单独核对其报错文本
    with tempfile.TemporaryDirectory() as td:
        bad_contract = CONTRACT.replace('{export_presets}', 'export_presets.cfg').replace(
            '{project_godot}', 'missing.godot')
        project = make_project(Path(td), bad_contract)
        proc = run({"CI_CONTRACT": str(project / ".github" / "ci-contract.json"),
                    "PROJECT_ROOT": str(project)})
        detail_ok = proc.returncode != 0 and "missing.godot" in proc.stderr
        print(f"{'OK  ' if detail_ok else 'FAIL'} project_godot 报错含具体路径")

    extra = []

    # 导出产物名默认模板不拼平台：预设名自带平台，再拼会重复成 windows_windows_desktop
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td), DEFAULT_CONTRACT)
        proc = run({"CI_CONTRACT": str(project / ".github" / "ci-contract.json"),
                    "PROJECT_ROOT": str(project)})
        ok = (proc.returncode == 0
              and '"package":"proj_windows_desktop"' in proc.stdout
              and '"package":"proj_linux_x11"' in proc.stdout
              and "windows_windows_desktop" not in proc.stdout
              and "linux_linux_x11" not in proc.stdout)
        print(f"{'OK  ' if ok else 'FAIL'} 导出产物名不重复平台段")
        if not ok:
            print(proc.stdout, proc.stderr)
        extra.append(ok)

    # 导出模板编译参数可被 UI 覆写（与 build.yml 同名入参同义）；留空回落到契约值
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td), DEFAULT_CONTRACT)
        base_env = {"CI_CONTRACT": str(project / ".github" / "ci-contract.json"),
                    "PROJECT_ROOT": str(project)}
        # 基线：三个参数取契约归一化值
        proc = run(dict(base_env))
        ok_base = (proc.returncode == 0
                   and "scons-flags=\n" in proc.stdout
                   and "profile=local:build/profile.py" in proc.stdout
                   and "build-profile=local:build/project.gdbuild" in proc.stdout)
        print(f"{'OK  ' if ok_base else 'FAIL'} 编译参数默认取契约值")
        if not ok_base:
            print(proc.stdout, proc.stderr)
        extra.append(ok_base)

        # UI 覆写：三个参数全部生效（否则 export 链的 custom_build 无法换编译选项）
        over = dict(base_env)
        over["INPUT_SCONS_FLAGS"] = "warnings=extra"
        over["INPUT_PROFILE"] = "remote:me/p@main:build/profile.py"
        over["INPUT_BUILD_PROFILE"] = "local:build/alt.gdbuild"
        proc = run(over)
        ok_over = (proc.returncode == 0
                   and "scons-flags=warnings=extra" in proc.stdout
                   and "profile=remote:me/p@main:build/profile.py" in proc.stdout
                   and "build-profile=local:build/alt.gdbuild" in proc.stdout)
        print(f"{'OK  ' if ok_over else 'FAIL'} 编译参数可被 UI 覆写并透传给 build-templates")
        if not ok_over:
            print(proc.stdout, proc.stderr)
        extra.append(ok_over)

    # 部分导出 → export-complete=false（不清理 Release 陈旧附件）
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td), DEFAULT_CONTRACT)
        proc = run({"CI_CONTRACT": str(project / ".github" / "ci-contract.json"),
                    "PROJECT_ROOT": str(project),
                    "INPUT_PRESETS": "Windows Desktop"})
        ok = proc.returncode == 0 and "export-complete=false" in proc.stdout
        print(f"{'OK  ' if ok else 'FAIL'} 部分导出时 export-complete=false")
        if not ok:
            print(proc.stdout, proc.stderr)
        extra.append(ok)

    # 导出产物名冲突（模板未含 {platform}，跨平台两个预设收敛同名）→ fail-fast
    # 真实场景：预设名不同但 safe 形式相同（'Shared/A' 与 'Shared-A' 都收敛为
    # shared_a）。同平台会被 safe 名冲突挡住，跨平台只有产物名判据能拦住。
    with tempfile.TemporaryDirectory() as td:
        clash_contract = (DEFAULT_CONTRACT
                          .replace('"presets": ["Windows Desktop", "Linux/X11"]',
                                   '"presets": ["Shared/A", "Shared-A"]'))
        project = make_project(Path(td), clash_contract)
        (project / "export_presets.cfg").write_text(
            '[preset.0]\nname="Shared/A"\nplatform="Windows Desktop"\n'
            '[preset.1]\nname="Shared-A"\nplatform="Linux/X11"\n', encoding="utf-8")
        proc = run({"CI_CONTRACT": str(project / ".github" / "ci-contract.json"),
                    "PROJECT_ROOT": str(project)})
        ok = proc.returncode != 0 and "导出产物名冲突" in proc.stderr
        print(f"{'OK  ' if ok else 'FAIL'} 跨平台同名导出产物 → fail-fast")
        if not ok:
            print(proc.stdout, proc.stderr)
        extra.append(ok)

    passed = sum(1 for r in results if r) + (1 if detail_ok else 0) + sum(1 for r in extra if r)
    total = len(results) + 1 + len(extra)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())