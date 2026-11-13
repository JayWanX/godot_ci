#!/usr/bin/env python3
"""resolve_build.py 自测：build 链入口归一化判据（与 test_resolve_export 对称）。

全部离线：契约 engine.source 用 40 位 SHA（resolve_engine_sha 直采纳分支，不触发
ls-remote）。覆盖：
- 正常通过：platforms JSON / target / repo-safe / engine-sha / upload-artifacts
- 外部项目构建的 project 入参：缺前缀拒绝、local: 解析、契约缺失 fail-fast
- target / platform 白名单拒绝
- repository_dispatch payload 覆写 UI 输入
- artifacts.build_naming 校验前移（未知占位符 resolve 期暴露）

用法：python .github/tests/test_resolve_build.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RESOLVE = Path(__file__).resolve().parents[1] / "actions" / "resolve-build" / "scripts" / "resolve_build.py"
sys.path.insert(0, str(RESOLVE.parents[3] / "scripts"))
import godot_context  # noqa: E402
SHA = "a" * 40

CONTRACT = """{
  "ci": { "repository": "o/ci", "ref": "%s" },
  "engine": { "source": "o/r@%s",
              "target": ["template_release"], "platform": ["windows", "linux"] },
  "build": { "paths": {}, "scons_flags": "", "compat": {"standard_includes": "auto"}, "toolchain": {} },
  "export": { "type": "custom_build", "editor_version": "4.7.2-stable",
              "presets": ["Windows Desktop", "Linux/X11"],
              "paths": { "project_godot": "project.godot", "export_presets": "export_presets.cfg", "dist": "bin" } },
  "artifacts": { "prefix": "", "build_naming": "{prefix}_{platform}_{target}", "retention": {} },
  "hooks": { "manifest": ".github/ci-hooks.json", "policy": "declared" }
}""" % (SHA, SHA)

# 会干扰判定的继承 env 一律剥离
CLEAN = ("INPUT_PROJECT", "INPUT_ENGINE_SOURCE", "INPUT_ENGINE_REF", "INPUT_TARGET",
         "INPUT_PLATFORM", "INPUT_PROFILE", "INPUT_BUILD_PROFILE",
         "INPUT_SCONS_FLAGS", "INPUT_FORCE_UPLOAD", "PAYLOAD", "EVENT_NAME",
         "CALLER_USES_REF", "ENGINE_SHA", "CI_CONTRACT", "PROJECT_ROOT",
         "GITHUB_WORKSPACE", "REPO_SAFE")

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def make_project(root: Path, contract: str) -> Path:
    (root / ".github").mkdir(parents=True)
    (root / ".github" / "ci-contract.json").write_text(contract, encoding="utf-8")
    (root / "project.godot").write_text("", encoding="utf-8")
    return root


def run(env_extra: dict, workspace: str) -> tuple:
    env = {k: v for k, v in os.environ.items() if k not in CLEAN}
    env.update({
        "CURRENT_REPO": "me/proj", "CURRENT_SHA": SHA, "RUNNER_OS": "Linux",
    })
    env.update(env_extra)
    fd, out = tempfile.mkstemp()
    os.close(fd)
    env["GITHUB_OUTPUT"] = out
    try:
        proc = subprocess.run([sys.executable, str(RESOLVE)], capture_output=True,
                              text=True, encoding="utf-8", env=dict(env, PYTHONIOENCODING="utf-8"), cwd=workspace)
        return proc, Path(out).read_text(encoding="utf-8")
    finally:
        os.unlink(out)


@check("产物名前缀：契约 prefix 为空时取 repo-safe（不得渲染成空前缀）")
def t_prefix_falls_back_to_repo_safe():
    """REPO_SAFE 必须在 resolve_outputs 之前就位。

    否则 artifacts.prefix 为空 + build_naming 含 {prefix} 时前缀算成空串，
    产物名渲染期才失败，错误信息指向模板而看不出根因。
    """
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, outputs = run(
            {"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
             "PROJECT_ROOT": str(contract), "CURRENT_REPO": "me/proj"},
            td)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        prefix = next(line.split("=", 1)[1] for line in outputs.splitlines()
                      if line.startswith("artifact-prefix="))
        assert prefix == "proj", f"前缀未回落到 repo-safe：{prefix!r}"


@check("正常通过（无 project 入参）：归一化输出齐全")
def t_normal():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, out = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json")}, td)
        assert proc.returncode == 0, proc.stderr
        for needle in (
            "platforms=[\"windows\", \"linux\"]",
            "target=template_release",
            "repo-safe=proj",
            f"engine-sha={SHA}",
            f"engine-ref={SHA}",
            "upload-artifacts=false",
        ):
            assert needle in out, (needle, out)


@check("project 入参缺 remote:/local: 前缀 → fail-fast")
def t_project_no_prefix():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run({"INPUT_PROJECT": "evil/repo"}, td)
        assert proc.returncode == 1
        assert "remote:/local:" in proc.stderr


@check("local: project：契约与 project-root 定位到检出内相对路径")
def t_project_local():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, out = run({"INPUT_PROJECT": "local:proj",
                         "GITHUB_WORKSPACE": td}, td)
        assert proc.returncode == 0, proc.stderr
        assert f"project-repo=me/proj\n" in out, out
        assert f"project-ref={SHA}" in out, out
        assert "project-root=" in out and "proj" in out


@check("无 project、无契约 → 合成默认契约（在 CI 仓库直接编引擎）")
def t_no_project_no_contract():
    """构建链编译的是**引擎**，不是 Godot 项目——没有任何项目配置也该能编。

    契约是否可选只看契约文件在不在，与 project 填没填无关：project 只决定去哪个
    仓库找契约。此前把「无契约」绑在「填了 project」上，导致在 CI 仓库不填 project
    直接编引擎必然失败（实测暴露）。
    """
    with tempfile.TemporaryDirectory() as td:
        # 显式给 CI_CONTRACT：不给会回落到 godot_context.paths() 的默认值
        # （CI 仓库自身的 .github/ci-contract.json），测试会污染真实仓库
        expected = Path(td) / ".github" / "ci-contract.json"
        proc, out = run({"GITHUB_WORKSPACE": td, "CI_CONTRACT": str(expected),
                         "ENGINE_LATEST_REF": "4.7.2-stable",
                         "INPUT_TARGET": "template_release", "INPUT_PLATFORM": "linux"}, td)
        assert proc.returncode == 0, proc.stderr
        assert "无 ci-contract.json" in proc.stdout, proc.stdout
        assert "engine-repo=godotengine/godot" in out, out
        assert "engine-ref=4.7.2-stable" in out, out
        assert "target=template_release" in out, out
        # 平台来自 UI 覆写而非契约/基线全量
        assert 'platforms=["linux"]' in out, out
        # 合成契约落在该位置（不污染别处）
        assert expected.is_file(), f"合成契约未写到 {expected}"


@check("合成契约写在「契约本该在的路径」：contract-path 指向真实文件")
def t_synthetic_contract_persisted():
    """resolve 之后还有「生成运行时上下文」「钩子 validate」两步会按 contract-path
    重新读契约。合成契约不落盘时它们会再撞一次「缺少 ci-contract.json」（实测踩到）。

    落盘位置就是 CI_CONTRACT 指向处，不是另造的临时路径——contract-path 的语义是
    「本次运行实际使用的契约在哪」，写原位才自洽。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        expected = workspace / ".github" / "ci-contract.json"
        proc, out = run({"GITHUB_WORKSPACE": td, "CI_CONTRACT": str(expected),
                         "ENGINE_LATEST_REF": "4.7.2-stable",
                         "INPUT_TARGET": "template_release", "INPUT_PLATFORM": "linux"}, td)
        assert proc.returncode == 0, proc.stderr
        line = [ln for ln in out.splitlines() if ln.startswith("contract-path=")]
        assert line, out
        path = Path(line[0].split("=", 1)[1].strip())
        # 契约-path 就是契约本该在的位置，且文件真实存在
        assert path == expected, f"应在原位 {expected}，实际 {path}"
        assert path.is_file(), f"contract-path 指向的文件不存在：{path}"
        # 落盘内容能被后续步骤当契约解析
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["engine"]["platform"], data


@check("合成契约不覆盖已存在的真实契约")
def t_no_overwrite_real_contract():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td), CONTRACT)
        path = contract / ".github" / "ci-contract.json"
        before = path.read_text(encoding="utf-8")
        proc, out = run({"CI_CONTRACT": str(path), "GITHUB_WORKSPACE": td,
                         "INPUT_TARGET": "template_release"}, td)
        assert proc.returncode == 0, proc.stderr
        assert path.read_text(encoding="utf-8") == before, "真实契约被合成值覆盖了"
        assert "使用契约" in proc.stdout, proc.stdout


@check("有契约时静默使用契约（不受 project 留空影响）")
def t_contract_preferred_over_synth():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td), CONTRACT)
        proc, out = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
                         "GITHUB_WORKSPACE": td,
                         "INPUT_TARGET": "template_release"}, td)
        assert proc.returncode == 0, proc.stderr
        assert "使用契约" in proc.stdout, proc.stdout
        # 关键：用的是契约值而非合成默认值——夹具里 engine.source 是 o/r@<SHA>，
        # 合成契约会给 godotengine/godot + 自动解析的 stable tag
        assert "engine-repo=o/r" in out, out
        assert "engine-repo=godotengine/godot" not in out, out
        # 平台来自契约（windows/linux），不是基线全量七平台
        assert 'platforms=["windows", "linux"]' in out or 'platforms=["linux", "windows"]' in out, out


@check("local: project 无契约 → 用合成默认契约（契约是可选的）")
def t_project_local_no_contract():
    """无契约的外部项目正是 CI 仓库侧要服务的对象；有契约才该走自己的触发器。"""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "proj").mkdir()
        proc, out = run({"INPUT_PROJECT": "local:proj", "GITHUB_WORKSPACE": td,
                         "ENGINE_LATEST_REF": "4.7.2-stable"}, td)
        assert proc.returncode == 0, proc.stderr
        assert "无 ci-contract.json" in proc.stdout, proc.stdout
        assert "engine-repo=godotengine/godot" in out, out
        # 引擎版本来自自动解析（ENGINE_LATEST_REF 注入）
        assert "engine-ref=4.7.2-stable" in out, out
        # 平台取基线全量
        assert 'platforms=["linux", "windows", "macos", "android", "ios", "visionos", "web"]' in out


@check("local: project 目录不存在 → fail-fast")
def t_project_local_missing_dir():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run({"INPUT_PROJECT": "local:nope", "GITHUB_WORKSPACE": td}, td)
        assert proc.returncode == 1 and "项目目录不存在" in proc.stderr, proc.stderr


@check("无契约且引擎版本无法解析 → fail-fast（不回落隐藏默认值）")
def t_no_contract_engine_ref_required():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "proj").mkdir()
        # ENGINE_LATEST_REF 给空串 → 走真实 ls-remote；用不存在的仓库触发失败
        proc, _ = run({"INPUT_PROJECT": "local:proj", "GITHUB_WORKSPACE": td,
                       "INPUT_ENGINE_SOURCE": "no-such-owner/no-such-repo",
                       "ENGINE_LATEST_REF": ""}, td)
        assert proc.returncode == 1, proc.stdout
        assert "无法解析引擎最新版本" in proc.stderr or "没有任何" in proc.stderr, proc.stderr


@check("target 白名单外 → fail-fast")
def t_bad_target():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, _ = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
                       "INPUT_TARGET": "game"}, td)
        assert proc.returncode == 1 and "unsupported target" in proc.stderr


@check("platform 白名单外 → fail-fast")
def t_bad_platform():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, _ = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
                       "INPUT_PLATFORM": "atab"}, td)
        assert proc.returncode == 1 and "unsupported platform" in proc.stderr


@check("repository_dispatch payload 覆写 UI 输入")
def t_payload():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, out = run({
            "CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
            "EVENT_NAME": "repository_dispatch",
            "PAYLOAD": json.dumps({"target": "template_debug", "platform": "linux",
                                   "force-upload": True}),
        }, td)
        assert proc.returncode == 0, proc.stderr
        assert "target=template_debug" in out, out
        assert "platforms=[\"linux\"]" in out, out
        assert "upload-artifacts=true" in out, out


@check("平台 × target 组合：web+editor 静默零产物组合 → fail-fast 并列出可用 target")
def t_platform_target_combo():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, _ = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
                       "INPUT_TARGET": "editor", "INPUT_PLATFORM": "web"}, td)
        assert proc.returncode == 1
        assert "不支持构建目标" in proc.stderr and "template_release" in proc.stderr, proc.stderr


@check("平台 × target 组合：linux+template_debug 合法放行")
def t_platform_target_ok():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, out = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
                         "INPUT_TARGET": "template_debug", "INPUT_PLATFORM": "linux,android"}, td)
        assert proc.returncode == 0, proc.stderr
        assert "platforms=[\"linux\", \"android\"]" in out, out


@check("artifacts.build_naming 未知占位符在 resolve 期暴露（校验前移）")
def t_bad_naming():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT.replace(
            '"build_naming": "{prefix}_{platform}_{target}"',
            '"build_naming": "{prefix}_{nope}"'))
        proc, _ = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json")}, td)
        assert proc.returncode != 0, "未知占位符未在 resolve 期失败"


# 多 target 显式失败（不静默截断）
@check("UI 传多 target → resolve 期 fail-fast（不静默截第一个）")
def t_multi_target_rejected():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, _ = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
                       "INPUT_TARGET": "editor,template_release"}, td)
        assert proc.returncode == 1, "多 target 未被拒绝"
        assert "一次只能指定一个档位" in proc.stderr, proc.stderr


@check("契约 engine.target 多值 → resolve 期 fail-fast")
def t_contract_multi_target_rejected():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT.replace(
            '"target": ["template_release"]',
            '"target": ["editor", "template_release"]'))
        proc, _ = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json")}, td)
        assert proc.returncode == 1, "契约多 target 未被拒绝"
        assert "一次只产一个" in proc.stderr, proc.stderr


@check("单 target 合法放行（回归保护）")
def t_single_target_ok():
    with tempfile.TemporaryDirectory() as td:
        contract = make_project(Path(td) / "proj", CONTRACT)
        proc, out = run({"CI_CONTRACT": str(contract / ".github" / "ci-contract.json"),
                         "INPUT_TARGET": "template_debug", "INPUT_PLATFORM": "linux"}, td)
        assert proc.returncode == 0, proc.stderr
        assert "target=template_debug" in out, out


# 导出链下传 remote:<repo>@<ref> 的解析链路
@check("导出链下传的 remote:<repo>@<ref> 能被来源语法正确解析")
def t_remote_project_wiring():
    """export.yml:306 用 format('remote:{0}@{1}', project-repo, project-ref) 下传；
    这里验证该字符串进 parse_source_entry 得到 (repo, ref)，不触发网络克隆。
    """
    parsed = godot_context.parse_source_entry("remote:acme/game@v1.2.3", "project 入参")
    assert parsed == {"kind": "remote", "repo": "acme/game", "ref": "v1.2.3", "path": ""}, parsed
    # 自有项目不克隆自己：external-project == 'true' 时才拼该串（静态接线断言见
    # test_real_wiring.py），此处保证判据所需的 repo/ref 形态合法
    parsed2 = godot_context.parse_source_entry("remote:acme/game", "project 入参")
    assert parsed2["repo"] == "acme/game" and parsed2["ref"] == "", parsed2


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