#!/usr/bin/env python3
"""run_hooks 自测（由内嵌 selftest 拆出；import 执行器内部函数）。

用法：python .github/tests/test_run_hooks.py
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_hooks as rh  # noqa: E402
from run_hooks import *  # noqa: E402,F403
from path_guard import check_rel_path, PathError  # noqa: E402
from testlib import capture  # noqa: E402

# ---------------------------------------------------------------------------

def _ns(**kw):
    kw.setdefault("scons_flags", "")
    kw.setdefault("current_preset", "")
    kw.setdefault("capabilities", "")
    return argparse.Namespace(**kw)


def _make_context(root: Path, chain="build", platform="windows",
              target="template_release", run_id="1", attempt=1):
    return {
        "schema": "godot.ci/hook-context",
        "chain": chain, "platform": platform, "target": target,
        "arch": "x86_64",
        "engine": {"repo": "x/y", "ref": "main", "sha": "abc"},
        "project": {"repo": "x/y", "ref": "main", "root": str(root)},
        "export": {"type": "custom_build", "editor_version": "<version>-stable",
                   "presets": ["Windows Desktop"], "current_preset": "Windows Desktop"},
        "paths": {
            "engine_root": str(root),
            "profile": None, "build_profile": None, "modules_dir": None,
            "dist": None, "export_presets": str(root / "export_presets.cfg"),
        },
        "cache": {"config_hash": "h", "source_hash": "s",
                  "scons_flags_base": "d3d12=no"},
        "artifacts": {"prefix": "demo", "build_naming": "{prefix}_{platform}_{target}",
                      "retention": {}},
        "runner": {"os": "Linux", "workspace": str(root)},
        "run": {"id": run_id, "attempt": attempt, "event": "push"},
    }


def _write_manifest(root: Path, hooks, name="ci-hooks.json"):
    p = root / name
    p.write_text(json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def _base_hook(**over):
    hook = {
        "id": "gen-profile",
        "on": {"anchor": "inject-config", "phase": "after"},
        "run": "ci-hooks/gen_profile.py",
        "with": {"inputs": []},
        "capabilities": ["write:scons_flags"],
        "outputs": ["scons_flags"],
        "cache_impact": "config",
        "on_error": "fail",
        "timeout_minutes": 5,
    }
    hook.update(over)
    return hook


def selftest() -> int:
    cases = []

    def check(name, fn):
        try:
            fn()
            cases.append((name, True, ""))
        except Exception as e:  # noqa: BLE001 - 自测需捕获任意断言/异常
            cases.append((name, False, f"{type(e).__name__}: {e}"))

    with tempfile.TemporaryDirectory(prefix="run_hooks_selftest_") as td:
        base = Path(td)
        project = base / "project"
        (project / "ci-hooks").mkdir(parents=True)
        (project / "ci-hooks" / "gen_profile.py").write_text(
            "import json, os\n"
            "dist = os.environ['GODOT_HOOK_DIST']\n"
            "directives = os.environ['GODOT_HOOK_DIRECTIVES']\n"
            "os.makedirs(directives, exist_ok=True)\n"
            "with open(os.path.join(directives, 'scons_flags'), 'w', encoding='utf-8') as f:\n"
            "    json.dump({'flags': [{'key': 'vulkan', 'value': 'yes'},"
            " {'key': 'd3d12', 'value': 'yes'}]}, f)\n", encoding="utf-8")
        (project / "ci-hooks" / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
        context_path = base / "context.json"
        context_path.write_text(json.dumps(_make_context(project), ensure_ascii=False), encoding="utf-8")
        anchors_path = DEFAULT_ANCHORS
        output_file = base / "github_output.txt"
        os.environ["GITHUB_OUTPUT"] = str(output_file)
        # GITHUB_STEP_SUMMARY 必须一并重定向：run_hooks 在 warn 档会往它写 job summary
        # （docs/hooks.md 承诺），测试若用 runner 的真实文件就会把自己的用例结果写进
        # 本次运行的 Job summary，界面上出现「⚠️ hook evil 失败已回滚」这类噪声。
        summary_file = base / "github_step_summary.md"
        summary_file.write_text("", encoding="utf-8")
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary_file)
        os.environ["GODOT_HOOK_ROOT"] = str(base / "hook_root")

        def run_validate(manifest, chain="build", policy="declared", out=None, bundle_out=None):
            output_file.write_text("", encoding="utf-8")
            return cmd_validate(_ns(
                manifest=str(manifest), chain=chain, context=str(context_path),
                out=str(out or base / "plan.json"),
                bundle_out=str(bundle_out or base / "bundle"),
                anchors=str(anchors_path), policy=policy))

        # 1) 校验失败：未知字段 / 未知能力 / outputs 越权 / 未知锚点 / 坏扩展名
        def t_validation_failures():
            # 每条都带预期理由片段：只断言「抛了 HookError」会把 KeyError 包装、路径拼错等
            # 无关失败一并算通过，等于没有断言。
            bad = [
                ({"hooks": [{**_base_hook(), "bogus": 1}]}, "未知字段", "bogus"),
                ({"hooks": [_base_hook(capabilities=["write:unknown"])]},
                 "未知能力", "write:unknown"),
                ({"hooks": [_base_hook(outputs=["artifacts"])]},
                 "outputs 越权", "不是 capabilities 能产出的子集"),
                ({"hooks": [_base_hook(on={"anchor": "nope", "phase": "after"})]},
                 "未知锚点", "不在 hooks-anchors.json"),
                ({"hooks": [_base_hook(run="ci-hooks/x.exe")]},
                 "坏扩展名", ".exe"),
                ({"hooks": [_base_hook(), _base_hook()]}, "id 重复", "id 重复"),
                ({"hooks": [_base_hook(capabilities=["secret:NO_SUCH_NAME"], outputs=[])]},
                 "secret 名不在白名单", "secret_names 白名单"),
            ]
            # secret 名在白名单内（固定 secret 名清单）应通过 validate
            ok_manifest = _write_manifest(
                project, [_base_hook(capabilities=["secret:GIT_PAT"], outputs=[])], "secret_ok.json")
            if run_validate(ok_manifest) != 0:
                raise AssertionError("白名单内 secret:GIT_PAT 未被接受")
            for i, (obj, why, needle) in enumerate(bad):
                m = _write_manifest(project, obj["hooks"], f"bad{i}.json")
                try:
                    run_validate(m)
                except HookError as e:
                    if needle not in str(e):
                        raise AssertionError(
                            f"第 {i} 组（{why}）拒绝理由不符：期望含 {needle!r}，实际 {str(e)!r}")
                    continue
                raise AssertionError(f"第 {i} 组（{why}）非法清单未被拒绝")
            # 缺必填字段：schema 校验要指出缺哪个字段
            m = _write_manifest(project, [{"id": "x"}], "missing.json")
            try:
                run_validate(m)
            except HookError as e:
                assert "schema" in str(e) or "必填" in str(e), str(e)
            else:
                raise AssertionError("缺必填字段未被拒绝")

        check("校验失败（schema/能力/锚点/解释器/id）", t_validation_failures)

        # 1b) secret_names 按链拆组 + validate 期按链拒绝 + dispatch 期缺 env 直接失败
        def t_secret_per_chain():
            def validate_on(chain, secret_cap, anchor, phase, name):
                m = _write_manifest(project, [_base_hook(
                    id="sec", capabilities=[secret_cap], outputs=[],
                    on={"anchor": anchor, "phase": phase}, cache_impact="none")], name)
                try:
                    rc = run_validate(m, chain=chain, out=base / f"plan_{name}.json",
                                      bundle_out=str(base / f"bundle_{name}"))
                except HookError as e:
                    return str(e)  # 拒绝理由
                return "" if rc == 0 else f"异常退出码 {rc}"

            # 阳性：本链组内的名字放行（返回空串 = 通过）
            assert validate_on("build", "secret:GIT_PAT",
                               "inject-config", "after", "sec_build_ok.json") == ""
            assert validate_on("export", "secret:IOS_CERT_B64",
                               "package", "before", "sec_export_ok.json") == ""

            # 阴性：release 链声明 IOS_CERT_* → 拒绝，且消息带链名
            msg_release = validate_on("release", "secret:IOS_CERT_B64",
                                      "publish", "before", "sec_release_bad.json")
            assert msg_release, "release 链声明 IOS_CERT_B64 未被拒绝"
            assert "release" in msg_release and "IOS_CERT_B64" in msg_release, msg_release
            # 阴性：build 链声明 IOS_CERT_*（不在 build 组）同样按链拒绝
            msg_build = validate_on("build", "secret:IOS_CERT_PASS",
                                    "inject-config", "after", "sec_build_bad.json")
            assert msg_build and "build" in msg_build, msg_build
            # 阴性：拼错名字（不在全集）仍按「白名单全集」拒绝，不被按链逻辑吞掉
            msg_typo = validate_on("export", "secret:NO_SUCH_NAME",
                                   "package", "before", "sec_typo.json")
            assert msg_typo and "全集" in msg_typo, msg_typo

            # dispatch 期取不到声明的 secret → 响亮失败（build_hook_env 单元）
            hook = _base_hook(id="needs-secret", capabilities=["secret:GIT_PAT"],
                              outputs=[], cache_impact="none")
            ctx = _make_context(project)
            hr = Path(os.environ["GODOT_HOOK_ROOT"])
            saved = os.environ.pop("GIT_PAT", None)
            try:
                try:
                    build_hook_env(hook, ctx, hr, base / "bundle",
                                   "inject-config", "after")
                except HookError as e:
                    msg = str(e)
                    assert "GIT_PAT" in msg and "env" in msg, msg
                else:
                    raise AssertionError("声明 secret 但 env 缺失时未失败")
                os.environ["GIT_PAT"] = "dummy-token"
                present = build_hook_env(hook, ctx, hr, base / "bundle",
                                         "inject-config", "after")
                assert present["GIT_PAT"] == "dummy-token", "已注入的 secret 未透传"
            finally:
                os.environ.pop("GIT_PAT", None)
                if saved is not None:
                    os.environ["GIT_PAT"] = saved

        check(" secret 按链拆组 + validate 拒绝（消息含链名）+ env 缺失 dispatch fail",
              t_secret_per_chain)

        # 2) 声明式过滤求交（dispatch 期）
        def t_declarative_filter():
            context = _make_context(project)
            on_all = {"anchor": "inject-config", "phase": "after"}
            assert hook_matches(on_all, context)
            assert hook_matches({"platform": ["windows", "linux"], **on_all}, context)
            assert not hook_matches({"platform": ["linux"], **on_all}, context)
            assert hook_matches({"target": ["template_release"], **on_all}, context)
            assert not hook_matches({"target": ["editor"], **on_all}, context)
            assert hook_matches({"chains": ["build"], **on_all}, context)
            assert not hook_matches({"chains": ["export"], **on_all}, context)
            assert hook_matches({"presets": ["Windows Desktop"], **on_all}, context)
            assert not hook_matches({"presets": ["Linux/X11"], **on_all}, context)
            # 空名单与省略同构（docs/hooks.md：省略或空名单 = 全部）。四个过滤键
            # 必须同一口径：`[]` 若被判成「永不匹配」，写空数组的钩子会静默不执行。
            for key in ("platform", "target", "chains", "presets"):
                assert hook_matches({key: [], **on_all}, context), \
                    f"on.{key}=[] 未按『不过滤』放行"
                assert hook_matches({key: [], **on_all}, _make_context(project, chain="release")), \
                    f"on.{key}=[] 在 release 链未按『不过滤』放行"
            context2 = _make_context(project)
            context2["export"].pop("current_preset")
            assert hook_matches({"presets": ["Windows Desktop"], **on_all}, context2)
            # context 的 platform/target=null（多平台批 / release 链）= 不过滤（双向）：
            # 带过滤清单的钩子放行；单值 context 照常拦截
            context_null = _make_context(project)
            context_null["platform"] = None
            context_null["target"] = None
            assert hook_matches({"platform": ["linux"], **on_all}, context_null), \
                "null platform 未按不过滤放行"
            assert hook_matches({"target": ["editor"], **on_all}, context_null), \
                "null target 未按不过滤放行"
            assert not hook_matches({"platform": ["linux"], **on_all}, context), \
                "单值 context 与 null 同流（过滤失效）"
            # validate 期按 chain 分组（不是运行期过滤）：export 链的 plan 不含 build-only 锚点
            m = _write_manifest(project, [_base_hook()], "group.json")
            p = base / "plan_export.json"
            run_validate(m, chain="export", out=p, bundle_out=str(base / "bundle_export"))
            plan = json.loads(p.read_text(encoding="utf-8"))
            assert plan["chain"] == "export" and plan["hooks"] == [], \
                "inject-config(build-only) 不应进 export plan"

        check("声明式过滤求交 + 按 chain 分组", t_declarative_filter)

        # 2b) on.presets 的跨链约束：订阅到不含 export 的链 = 过滤永不求值 → 拒绝
        def t_presets_requires_export_chain():
            m = _write_manifest(project, [_base_hook(
                on={"anchor": "inject-config", "phase": "after",
                    "presets": ["Windows Desktop"]})], "presets_bad.json")
            err = None
            try:
                run_validate(m, out=base / "plan_presets_bad.json",
                             bundle_out=str(base / "bundle_presets_bad"))
            except HookError as e:
                err = str(e)
            assert err and "on.presets" in err and "export" in err, err
            # export 链上同样合法（plan 按 chain 分组，不因过滤键被误拒）
            m2 = _write_manifest(project, [{
                **_base_hook(id="preset-hook", run="ci-hooks/gen_profile.py"),
                "on": {"anchor": "preset-clean", "phase": "after",
                       "presets": ["Windows Desktop"]},
            }], "presets_ok.json")
            bundle_ok = base / "bundle_presets_ok"
            run_validate(m2, chain="export", out=base / "plan_presets_ok.json",
                         bundle_out=str(bundle_ok))
            plan = json.loads((base / "plan_presets_ok.json").read_text(encoding="utf-8"))
            assert [h["id"] for h in plan["hooks"]] == ["preset-hook"], plan["hooks"]

        check("on.presets 仅导出链有效（build 链锚点声明即拒绝）",
              t_presets_requires_export_chain)

        # 3) flags 合并（单元 + 端到端 dispatch）
        def t_flags_merge():
            merged = merge_scons_flags("d3d12=no CCFLAGS='-O2 -g'",
                                       {"flags": [{"key": "vulkan", "value": "yes"},
                                                  {"key": "d3d12", "value": "yes"}]})
            assert shlex.split(merged) == ["d3d12=yes", "CCFLAGS=-O2 -g", "vulkan=yes"], merged
            m = _write_manifest(project, [_base_hook()], "flags.json")
            plan_p, bundle_d = base / "plan_flags.json", base / "bundle_flags"
            run_validate(m, out=plan_p, bundle_out=str(bundle_d))
            result_p = base / "result_flags.json"
            rc = cmd_dispatch(_ns(bundle=str(bundle_d), anchor="inject-config",
                                  phase="after", context=str(context_path),
                                  result=str(result_p), plan="", chain="build"))
            assert rc == 0
            result = json.loads(result_p.read_text(encoding="utf-8"))
            assert result["ok"] and result["post_build"] == "ok"
            assert result["merged_flags"] == "d3d12=yes vulkan=yes", \
                result["merged_flags"]
            assert result["config_digests"], "config_digests 未记录"
            assert result["source_active"] is False and result["source_hash"] == ""
            assert result["hook_output_digests"]["config"], "config 层摘要未记录"
            # outputs 以输出名为键（apply_hook_outputs.py / apply_hook_patches.py 按输出名取 path）
            assert "scons_flags" in result["outputs"], result["outputs"]
            assert "scons_flags" in result["hooks_outputs"]["gen-profile"]

        check("flags 合并（单元 + 端到端 dispatch）", t_flags_merge)

        # 3b) write_paths：项目树写入放行 + build 链摘要 + 未声明拒绝 + 失败回滚
        def t_write_paths():
            tree_hook = _base_hook(
                id="gen-tree", capabilities=["write:project_tree"], outputs=[],
                write_paths=["gen/**"],
                run="ci-hooks/gen_tree.py", cache_impact="source")
            (project / "ci-hooks" / "gen_tree.py").write_text(
                "import os\n"
                "root = os.environ['GODOT_HOOK_PROJECT_ROOT']\n"
                "d = os.path.join(root, 'gen')\n"
                "os.makedirs(d, exist_ok=True)\n"
                "open(os.path.join(d, 'a.txt'), 'w', encoding='utf-8').write('hello')\n",
                encoding="utf-8")
            # 守卫脚本：越界写入（写 write_paths 之外的路径）
            (project / "ci-hooks" / "gen_escape.py").write_text(
                "import os\n"
                "root = os.environ['GODOT_HOOK_PROJECT_ROOT']\n"
                "open(os.path.join(root, 'forbidden.txt'), 'w', encoding='utf-8').write('x')\n",
                encoding="utf-8")
            # 失败脚本：先写后失败，验证回滚
            (project / "ci-hooks" / "gen_fail.py").write_text(
                "import os, sys\n"
                "root = os.environ['GODOT_HOOK_PROJECT_ROOT']\n"
                "d = os.path.join(root, 'gen')\n"
                "os.makedirs(d, exist_ok=True)\n"
                "open(os.path.join(d, 'bomb.txt'), 'w', encoding='utf-8').write('x')\n"
                "sys.exit(1)\n",
                encoding="utf-8")

            # 放行：写入落在声明写面内
            m = _write_manifest(project, [tree_hook], "tree.json")
            plan_p, bundle_d = base / "plan_tree.json", base / "bundle_tree"
            run_validate(m, out=plan_p, bundle_out=str(bundle_d))
            rc = cmd_dispatch(_ns(bundle=str(bundle_d), anchor="inject-config",
                                  phase="after", context=str(context_path),
                                  result=str(base / "result_tree.json"), plan="", chain="build"))
            assert rc == 0, "write_paths 内合法写入被判未声明"
            assert (project / "gen" / "a.txt").is_file(), "写入未落盘"

            # 未声明拒绝：越界写入 → 非零退出且文件被回滚删除
            esc = {**tree_hook, "id": "gen-escape", "run": "ci-hooks/gen_escape.py",
                   "with": {"inputs": []}}
            m2 = _write_manifest(project, [esc], "tree_escape.json")
            b2 = base / "bundle_tree2"
            run_validate(m2, out=base / "plan_tree2.json", bundle_out=str(b2))
            rc2 = capture(lambda: cmd_dispatch(
                _ns(bundle=str(b2), anchor="inject-config", phase="after",
                    context=str(context_path),
                    result=str(base / "result_tree2.json"), plan="", chain="build"))).value
            assert rc2 != 0, "write_paths 之外的写入未被拒绝"
            assert not (project / "forbidden.txt").exists(), "越界写入未被回滚"

            # 失败回滚：钩子先写后失败 → 写入被撤销
            fail = {**tree_hook, "id": "gen-fail", "run": "ci-hooks/gen_fail.py",
                    "with": {"inputs": []}, "on_error": "fail"}
            m3 = _write_manifest(project, [fail], "tree_fail.json")
            b3 = base / "bundle_tree3"
            run_validate(m3, out=base / "plan_tree3.json", bundle_out=str(b3))
            captured_fail = capture(lambda: cmd_dispatch(
                _ns(bundle=str(b3), anchor="inject-config", phase="after",
                    context=str(context_path),
                    result=str(base / "result_tree3.json"), plan="", chain="build")))
            assert captured_fail.value != 0, "on_error=fail 的钩子失败应使 dispatch 失败"
            assert "退出码 1" in captured_fail.text, captured_fail.text
            assert not (project / "gen" / "bomb.txt").exists(), "失败钩子的写入未被回滚"

            # 相对路径场景（CI 的真实调用形态）：bundle / result 都传相对路径，
            # 子进程 cwd 会是 hook_root。历史上 bundle 未绝对化导致脚本路径被拼成
            # <hook_root>/<bundle>/scripts/...，钩子一律「找不到脚本」。
            # 用会成功的 gen-tree（gen-fail 故意失败，不适合做成功断言）。
            m_rel = _write_manifest(project, [tree_hook], "tree_rel.json")
            b_rel = base / "bundle_rel"
            run_validate(m_rel, out=base / "plan_rel.json", bundle_out=str(b_rel))
            cwd = os.getcwd()
            try:
                os.chdir(base)  # 相对基准 = base（validate 生成 bundle 的位置）
                rc_rel = cmd_dispatch(_ns(
                    bundle="bundle_rel", anchor="inject-config", phase="after",
                    context=str(context_path),
                    result="rel-result.json", plan="", chain="build"))
            finally:
                os.chdir(cwd)
            assert rc_rel == 0, "相对 bundle 路径下钩子执行失败（路径未绝对化）"
            assert (base / "rel-result.json").is_file(), "相对 result 路径未落在 cwd 下"

            # 坐标系原点注入：脚本能通过 GODOT_HOOK_PROJECT_ROOT 拿到项目根绝对路径
            probe = {**tree_hook, "id": "gen-probe", "run": "ci-hooks/gen_probe.py",
                     "with": {"inputs": []}}
            (project / "ci-hooks" / "gen_probe.py").write_text(
                "import os\n"
                "root = os.environ['GODOT_HOOK_PROJECT_ROOT']\n"
                "assert os.path.isabs(root), root\n"
                "d = os.path.join(root, 'gen')\n"
                "os.makedirs(d, exist_ok=True)\n"
                "open(os.path.join(d, 'probe.txt'), 'w', encoding='utf-8').write(root)\n",
                encoding="utf-8")
            m_probe = _write_manifest(project, [probe], "tree_probe.json")
            b_probe = base / "bundle_tree_probe"
            run_validate(m_probe, out=base / "plan_tree_probe.json",
                         bundle_out=str(b_probe))
            rc_probe = cmd_dispatch(_ns(bundle=str(b_probe), anchor="inject-config",
                                        phase="after", context=str(context_path),
                                        result=str(base / "result_tree_probe.json"),
                                        plan="", chain="build"))
            assert rc_probe == 0, "注入 GODOT_HOOK_PROJECT_ROOT 后钩子执行失败"
            recorded = (project / "gen" / "probe.txt").read_text(encoding="utf-8")
            assert recorded == str(project.resolve()) or os.path.isabs(recorded), \
                f"注入的项目根不是绝对路径：{recorded!r}"

            # 未引用坐标系原点 → validate 期拒绝（不依赖文档纪律）
            no_ref = {**tree_hook, "id": "gen-no-ref", "run": "ci-hooks/gen_no_ref.py",
                      "with": {"inputs": []}}
            (project / "ci-hooks" / "gen_no_ref.py").write_text(
                "import os\n"
                "root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
                "open(os.path.join(root, 'x.txt'), 'w').write('x')\n",
                encoding="utf-8")
            m_no_ref = _write_manifest(project, [no_ref], "tree_no_ref.json")
            try:
                run_validate(m_no_ref, out=base / "plan_no_ref.json",
                             bundle_out=str(base / "bundle_no_ref"))
            except Exception as exc:
                assert "GODOT_HOOK_PROJECT_ROOT" in str(exc), \
                    f"拒绝理由未指明缺失的接口变量：{exc}"
            else:
                raise AssertionError("未引用 GODOT_HOOK_PROJECT_ROOT 的 write:project_tree 钩子未被拒绝")

            # 能力与 write_paths 双向互校
            for bad_hook, why in (
                (_base_hook(id="no-cap", write_paths=["gen/**"]),
                 "有 write_paths 无能力"),
                (_base_hook(id="no-path", capabilities=["write:project_tree"]),
                 "有能力无 write_paths"),
            ):
                mb = _write_manifest(project, [bad_hook], f"bad_{bad_hook['id']}.json")
                try:
                    run_validate(mb, out=base / f"p_{bad_hook['id']}.json",
                                 bundle_out=str(base / f"b_{bad_hook['id']}"))
                except Exception:
                    continue
                raise AssertionError(f"未被拒绝：{why}")

            # 路径逃逸拒绝
            try:
                m4 = _write_manifest(project, [_base_hook(
                    id="escape", capabilities=["write:project_tree"],
                    write_paths=["../outside/**"])], "tree_escape_path.json")
                run_validate(m4, out=base / "p4.json", bundle_out=str(base / "b4"))
            except Exception:
                pass
            else:
                raise AssertionError("write_paths 路径逃逸未被拒绝")

        check("write_paths（项目树写入 + 回滚 + 能力互校 + 逃逸）", t_write_paths)

        # 3c) restore_write_paths 必须用原始声明，不得按 before 顶层目录放宽
        # 旧实现 _declared_from(before) 把单文件声明 build/profile.py 放宽成 build/**，
        # 回滚时把 build/ 下钩子未触碰的预存文件当「新增」删掉（静默丢用户文件）。
        def t_restore_write_paths_narrow():
            for declared, label in (
                (["build/profile.py"], "单文件声明"),
                (["build/nested/profile.py"], "嵌套声明"),
            ):
                case_root = base / ("restore_" + label)
                case_root.mkdir(parents=True, exist_ok=True)
                # 声明写面内的预存文件（会被备份/还原）
                target = case_root / declared[0]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("original", encoding="utf-8")
                # 同一顶层目录 / 嵌套目录下的其它预存文件：钩子从未触碰，回滚不得删
                sibling = case_root / "build" / "other.py"
                sibling.parent.mkdir(parents=True, exist_ok=True)
                sibling.write_text("keep-me", encoding="utf-8")
                nested_sibling = case_root / "build" / "nested" / "extra.py"
                nested_sibling.parent.mkdir(parents=True, exist_ok=True)
                nested_sibling.write_text("keep-me-too", encoding="utf-8")
                top_level = case_root / "top_level.py"
                top_level.write_text("keep-me", encoding="utf-8")
                # 钩子写入：改声明文件 + 新增一个声明写面内文件，然后模拟失败回滚
                backup_root = base / ("backup_" + label)
                before = list_write_paths(case_root, declared)
                backup_write_paths(case_root, before, backup_root)
                target.write_text("modified", encoding="utf-8")
                # 反向验证注入点：restore 必须删新增、还原被改，不碰其它预存文件
                restore_write_paths(case_root, before, backup_root, declared)
                assert target.read_text(encoding="utf-8") == "original", \
                    f"{label}：被改写的声明文件未还原"
                assert sibling.read_text(encoding="utf-8") == "keep-me", \
                    f"{label}：回滚误删了同顶层目录的预存文件（旧缺陷）"
                assert nested_sibling.read_text(encoding="utf-8") == "keep-me-too", \
                    f"{label}：回滚误删了嵌套目录下的预存文件（旧缺陷）"
                assert top_level.read_text(encoding="utf-8") == "keep-me", \
                    f"{label}：回滚误删了顶层预存文件"

        check(" 回滚写面收窄（单文件/嵌套声明不误删预存文件）",
              t_restore_write_paths_narrow)

        # 3d)  /  固定用例：越界「改写预存文件」只删新增、无法还原
        # remove_outside_writes 只有元数据快照（rel->(kind,size)），没有内容备份：
        # 声明写面之外的预存文件被改写后 rel 仍在 before/after 里，既不会被删、
        # 也不会被还原——这是「失败即回滚」的真实边界，文档必须与之一致。
        def t_outside_write_boundary():
            case_root = base / "outside_boundary"
            case_root.mkdir(parents=True, exist_ok=True)
            (case_root / "build").mkdir()
            declared = ["build/profile.py"]
            # 越界预存文件：钩子会改写它（内容无备份，回滚还原不了）
            outside_pre = case_root / "README.md"
            outside_pre.write_text("original", encoding="utf-8")
            # 越界新增文件：回滚应删除
            outside_new = case_root / "injected.txt"
            before = list_project_tree(case_root, declared)
            outside_pre.write_text("MODIFIED-BY-HOOK", encoding="utf-8")
            outside_new.write_text("injected", encoding="utf-8")
            removed = remove_outside_writes(case_root, before, declared)
            assert "injected.txt" in removed, removed
            assert not outside_new.exists(), "越界新增文件未被删除"
            assert outside_pre.read_text(encoding="utf-8") == "MODIFIED-BY-HOOK", \
                "越界预存文件被静默还原了——实现没有内容备份，此断言锁住真实边界"

        check(" 越界改写预存文件只删新增、不还原（回滚边界）",
              t_outside_write_boundary)

        # 4) 路径逃逸拒绝
        def t_path_escape():
            bad = ["/etc/passwd", "../outside.py", "ci-hooks/../x.py", "./x.py",
                   "C:\\Windows\\x.py", "ci-hooks\\x.py", "", "a//b.py"]
            for rel in bad:
                try:
                    validate_rel_path(rel, "selftest", project)
                except HookError as e:
                    # 理由须指向路径本身（而不是某个无关的 OSError 被包装）
                    assert "selftest" in str(e) or "路径" in str(e), (rel, str(e))
                    continue
                raise AssertionError(f"非法路径未被拒绝: {rel!r}")
            assert validate_rel_path("ci-hooks/gen_profile.py", "selftest", project)
            # 符号链接
            link = project / "ci-hooks" / "link.py"
            try:
                link.symlink_to(project / "ci-hooks" / "gen_profile.py")
            except (OSError, NotImplementedError):
                pass
            else:
                try:
                    validate_rel_path("ci-hooks/link.py", "selftest", project)
                except HookError as e:
                    assert "符号链接" in str(e), str(e)
                else:
                    raise AssertionError("符号链接未被拒绝")

        check("路径逃逸拒绝（绝对/../反斜杠/符号链接）", t_path_escape)

        def t_gate_fixes():
            try:
                check_rel_path("C:/evil", "selftest")
            except PathError:
                pass
            else:
                raise AssertionError("盘符路径未被拒绝")
            scons = [{"channel": "directives", "path": "scons_flags", "kind": "file"}]
            assert classify_write("directives/scons_flags.bak", scons) == "undeclared"
            presets = [{"channel": "directives", "path": "presets.json", "kind": "file"}]
            assert classify_write("directives/presets.json.orig", presets) == "undeclared"
            arts = [{"channel": "dist", "path": "artifacts", "kind": "dir"}]
            assert classify_write("dist/artifacts", arts) == "ok"
            assert classify_write("dist/artifacts/a.exe", arts) == "ok"
            assert classify_write("dist/artifactsX/a.exe", arts) == "undeclared"
            src = [{"channel": "directives", "path": "patches/source.*.patch", "kind": "glob"}]
            assert classify_write("directives/patches/source.a.patch", src) == "ok"
            assert classify_write("directives/patches/profile.a.diff", src) == "undeclared"
            assert classify_write("directives/patches", src) == "ok"
            # 声明表是唯一权威源：每种输出的落点形态必须自洽
            for name, targets in OUTPUT_TARGETS.items():
                for t in targets:
                    assert set(t) == {"channel", "path", "kind"}, (name, t)
                    assert t["kind"] in ("file", "dir", "glob"), (name, t)
                    assert not t["path"].endswith("/"), (name, t)
            merged = merge_scons_flags("d3d12=no arch=arm64 verbose",
                                       {"flags": [{"key": "vulkan", "value": "yes"}]})
            assert shlex.split(merged) == ["verbose", "d3d12=no", "arch=arm64", "vulkan=yes"], merged

        check("门禁修复：精确路径 / glob 排除 / 合并基准", t_gate_fixes)

        # 5) plan 确定性（不含 run.id / attempt）
        def t_plan_determinism():
            m = _write_manifest(project, [_base_hook()], "det.json")
            p1, b1 = base / "p1.json", base / "b1"
            p2, b2 = base / "p2.json", base / "b2"
            run_validate(m, out=p1, bundle_out=str(b1))
            sha1 = (b1 / "sha256").read_text(encoding="utf-8").strip()[:8]
            # 改运行期字段（run.id / attempt / event）不应影响 plan 与 hooks-sha
            context2 = _make_context(project, run_id="999", attempt=7)
            context2["run"]["event"] = "workflow_dispatch"
            saved = context_path.read_text(encoding="utf-8")
            context_path.write_text(json.dumps(context2, ensure_ascii=False), encoding="utf-8")
            try:
                run_validate(m, out=p2, bundle_out=str(b2))
            finally:
                context_path.write_text(saved, encoding="utf-8")
            sha2 = (b2 / "sha256").read_text(encoding="utf-8").strip()[:8]
            assert p1.read_bytes() == p2.read_bytes(), "plan.json 不确定"
            assert sha1 == sha2, f"hooks-sha 随运行期字段漂移: {sha1} != {sha2}"
            plan = json.loads(p1.read_bytes())
            text = p1.read_text(encoding="utf-8")
            for forbidden in ("run_id", "attempt", "GITHUB_RUN", str(project)):
                assert forbidden not in text, f"plan 泄漏运行期/绝对字段 {forbidden}"
            assert plan["hooks"][0]["interpreter"] == "python3"

        check("plan 确定性（不含 run.id/attempt/绝对路径）", t_plan_determinism)

        # 6) 空转零成本：缺清单 + policy=off
        def t_zero_cost():
            missing = project / "does_not_exist.json"
            bundle_d = base / "bundle_zero"
            run_validate(missing, bundle_out=bundle_d)
            lines = output_file.read_text(encoding="utf-8").strip().splitlines()
            assert lines == ["hooks-bundle=", "hooks-sha="], lines
            assert not bundle_d.exists(), "空转仍创建了 bundle 目录"
            # 空清单
            empty = _write_manifest(project, [], "empty.json")
            run_validate(empty, bundle_out=base / "bundle_empty")
            assert output_file.read_text(encoding="utf-8").strip().splitlines() == [
                "hooks-bundle=", "hooks-sha="]
            # policy=off 不读清单（用一个非法清单也不会报错）
            bad = _write_manifest(project, [_base_hook(run="ci-hooks/x.exe")], "off.json")
            bundle_off = base / "bundle_off"
            run_validate(bad, policy="off", bundle_out=bundle_off)
            lines = output_file.read_text(encoding="utf-8").strip().splitlines()
            assert lines == ["hooks-bundle=", "hooks-sha="], lines
            assert not bundle_off.exists(), "policy=off 仍创建了 bundle 目录"

        check("空转零成本（缺清单/空清单/policy=off）", t_zero_cost)

        # 7) post-diff 未声明写入 + 回滚
        def t_post_diff_rollback():
            (project / "ci-hooks" / "evil.py").write_text(
                "import os\n"
                "root = os.environ['GODOT_HOOK_ROOT']\n"
                "with open(os.path.join(root, 'context.json'), 'a', encoding='utf-8') as f:\n"
                "    f.write('TAMPERED')\n"
                "dist = os.environ['GODOT_HOOK_DIST']\n"
            "directives = os.environ['GODOT_HOOK_DIRECTIVES']\n"
                "os.makedirs(dist, exist_ok=True)\n"
                "with open(os.path.join(dist, 'sneaky.txt'), 'w', encoding='utf-8') as f:\n"
                "    f.write('x')\n"
                "with open(os.path.join(directives, 'scons_flags'), 'w', encoding='utf-8') as f:\n"
                "    f.write('{\"flags\": []}')\n", encoding="utf-8")
            m = _write_manifest(project, [
                _base_hook(id="evil", run="ci-hooks/evil.py",
                           outputs=["scons_flags"]),
            ], "evil.json")
            bp, bd = base / "plan_evil.json", base / "bundle_evil"
            run_validate(m, out=bp, bundle_out=str(bd))
            hr = Path(os.environ["GODOT_HOOK_ROOT"])
            rp = base / "result_evil.json"
            # cmd_dispatch 失败会打印 ::error::（通道根外改动）——捕获以免污染 CI 注解
            captured = capture(lambda: cmd_dispatch(
                _ns(bundle=str(bd), anchor="inject-config",
                    phase="after", context=str(context_path),
                    result=str(rp), plan="", chain="build")))
            rc = captured.value
            assert rc == 1, f"未声明写入应失败；输出：{captured.text}"
            assert "未声明写入" in captured.text, captured.text
            restored = (hr / "context.json").read_text(encoding="utf-8")
            assert "TAMPERED" not in restored, "回滚未恢复被篡改的 context.json"
            assert json.loads(restored)["schema"] == "godot.ci/hook-context"
            # 恶意脚本实际写 GODOT_HOOK_DIST（= hr/dist），旧断言指向已重构掉
            # 的 out/ 恒真，回滚是否真删了 dist/sneaky.txt 从未被验证。改用执行器常量。
            assert not (hr / rh.CHANNEL_DIST / "sneaky.txt").exists(), \
                "回滚未删除未声明输出（dist/sneaky.txt）"
            result = json.loads(rp.read_text(encoding="utf-8"))
            assert result["post_build"] == "fail" and result["merged_flags"] == "d3d12=no"
            # warn 档：回滚后继续，链不失败
            m2 = _write_manifest(project, [
                _base_hook(id="evil", run="ci-hooks/evil.py",
                           outputs=["scons_flags"], on_error="warn"),
            ], "warn.json")
            run_validate(m2, out=base / "plan_warn.json",
                     bundle_out=str(base / "bundle_warn"))
            rp2 = base / "result_warn.json"
            captured_warn = capture(lambda: cmd_dispatch(
                _ns(bundle=str(base / "bundle_warn"),
                    anchor="inject-config", phase="after",
                    context=str(context_path), result=str(rp2),
                    plan="", chain="build")))
            rc2 = captured_warn.value
            assert rc2 == 0, captured_warn.text
            # warn 档仍会打印 ::error::（已捕获），但链不失败
            assert "未声明写入" in captured_warn.text, captured_warn.text
            r2 = json.loads(rp2.read_text(encoding="utf-8"))
            assert r2["warnings"] and r2["post_build"] == "ok"
            # warn 必须写进 job summary（docs/hooks.md 承诺）——写的是重定向后的
            # 临时文件，不会污染本次运行的 Job summary
            summary = summary_file.read_text(encoding="utf-8")
            assert "evil" in summary and "回滚" in summary, summary
            assert "未声明写入" in summary, summary

        check("post-diff 未声明写入检测 + 失败回滚 + warn 继续", t_post_diff_rollback)

        # 7a) post_build / post_export 是两个独立状态，不跨链传染
        def t_post_status_per_chain():
            """build 链失败不得把 post_export 标成 fail（反之亦然）。

            两者的消费者不同：post_build 门控保存缓存（build 链），post_export 门控
            上传与发布（export 链）。共用一个失败标志会让「本次从未在 export 链执行过」
            的状态被写坏，下游按另一条链的门控误判。
            """
            (project / "ci-hooks" / "boom.py").write_text(
                "import sys\nsys.exit(3)\n", encoding="utf-8")
            m = _write_manifest(project, [_base_hook(
                id="boom", run="ci-hooks/boom.py", outputs=[], cache_impact="none")],
                "boom.json")
            bp, bd = base / "plan_boom.json", base / "bundle_boom"
            run_validate(m, out=bp, bundle_out=str(bd))
            rp = base / "result_chain.json"
            capture(lambda: cmd_dispatch(
                _ns(bundle=str(bd), anchor="inject-config", phase="after",
                    context=str(context_path), result=str(rp), plan="", chain="build")))
            res = json.loads(rp.read_text(encoding="utf-8"))
            assert res["chain"] == "build", res["chain"]
            assert res["post_build"] == "fail", res["post_build"]
            assert res["post_export"] == "", \
                f"build 链失败污染了 post_export：{res['post_export']!r}"

        check("post_build / post_export 按链独立（不跨链传染）",
              t_post_status_per_chain)

        # 7b) 通道根快照：大产物不进内存，且被改写的大文件仍能完整回滚
        def t_snapshot_large_artifact():
            root = base / "snap_probe"
            root.mkdir(exist_ok=True)
            payload = b"A" * (3 * 1024 * 1024)  # 3 MB：超过任何合理的内存驻留阈值
            (root / "dist").mkdir(exist_ok=True)
            (root / "dist" / "big.bin").write_bytes(payload)
            (root / "dist" / "small.txt").write_text("v1", encoding="utf-8")
            backup = base / "snap_backup"
            snap = TreeSnapshot(root, backup, "通道根")
            # 内存态只留元数据：条目值与内容大小无关（kind/size/digest）
            assert set(snap.entries) == {"dist", "dist/big.bin", "dist/small.txt"}, snap.entries
            assert snap.entries["dist/big.bin"] == (
                "file", len(payload), hashlib.sha256(payload).hexdigest()), \
                snap.entries["dist/big.bin"]
            assert not hasattr(snap, "contents"), "快照不应在内存里保存文件内容"
            # 内容落盘：备份副本与原件字节一致
            assert (backup / "dist" / "big.bin").stat().st_size == len(payload)
            # 改写同尺寸内容 + 新增文件 + 删除预存文件 → 三项都必须被差异检出
            (root / "dist" / "big.bin").write_bytes(b"B" * len(payload))
            (root / "dist" / "new.txt").write_text("x", encoding="utf-8")
            (root / "dist" / "small.txt").unlink()
            after = TreeSnapshot(root, None, "通道根")
            assert snapshot_changes(snap, after) == [
                "dist/big.bin", "dist/new.txt", "dist/small.txt"], \
                snapshot_changes(snap, after)
            # 回滚：被改写的大文件、被删的小文件都还原，新增文件删除
            snap.restore()
            assert (root / "dist" / "big.bin").read_bytes() == payload, \
                "回滚未还原被改写的大文件（内容备份失效）"
            assert (root / "dist" / "small.txt").read_text(encoding="utf-8") == "v1"
            assert not (root / "dist" / "new.txt").exists(), "回滚未删除新增文件"
            # 无内容备份的快照只用于差异比对，回滚必须显式拒绝（不静默半还原）
            try:
                after.restore()
            except HookError as e:
                assert "不能回滚" in str(e), str(e)
            else:
                raise AssertionError("无内容备份的快照被允许回滚")

        check("通道根快照：大产物不进内存 + 改写/新增/删除全部可回滚",
              t_snapshot_large_artifact)

        # 8) ::error:: 转义
        def t_escape():
            msg = "line1\nline2%0A::error::fake\r"
            escaped = escape_error(msg)
            assert "\\n" not in escaped.replace("%0A", "")
            assert "\\r" not in escaped.replace("%0D", "")
            assert "%25" in escaped and "%0A" in escaped and "%0D" in escaped
            assert "::error::fake" in escaped  # 内容保留，但换行已转义，无法伪造新行

        check("::error:: 转义（% / CR / LF）", t_escape)

        # 9) 无匹配 hook 时仍写 ok 结果（空转分支的事务占位）
        def t_no_match():
            m = _write_manifest(project, [_base_hook(
                on={"anchor": "inject-config", "phase": "after",
                    "platform": ["linux"]})], "nomatch.json")
            bp, bd = base / "plan_nomatch.json", base / "bundle_nomatch"
            run_validate(m, out=bp, bundle_out=str(bd))
            rp = base / "result_nomatch.json"
            rc = cmd_dispatch(_ns(bundle=str(bd), anchor="inject-config",
                                  phase="after", context=str(context_path),
                                  result=str(rp), plan="", chain="build"))
            assert rc == 0
            r = json.loads(rp.read_text(encoding="utf-8"))
            assert r["ok"] and r["executed"] == [] and r["post_build"] == "ok"
            # 冻结键的兜底值（无匹配 hook 时也必须齐备）
            assert r["merged_flags"] == "d3d12=no", r["merged_flags"]
            assert r["config_digests"] == ""
            assert r["source_hash"] == "" and r["source_active"] is False

        check("无匹配 hook 仍写兜底结果（merged_flags/config/source/post_build）", t_no_match)

        # 10) bundle 摘要消费侧重验（篡改即失败）
        def t_bundle_integrity():
            m = _write_manifest(project, [_base_hook()], "integrity.json")
            bp, bd = base / "plan_int.json", base / "bundle_int"
            run_validate(m, out=bp, bundle_out=str(bd))
            pf = bd / "plan.json"
            obj = json.loads(pf.read_text(encoding="utf-8"))
            obj["hooks"][0]["id"] = "tampered"
            pf.write_text(json.dumps(obj), encoding="utf-8")
            try:
                cmd_dispatch(_ns(bundle=str(bd), anchor="inject-config",
                                 phase="after", context=str(context_path),
                                 result=str(base / "r_int.json"), plan="", chain="build"))
            except HookError:
                pass
            else:
                raise AssertionError("bundle 摘要被篡改后未拒绝")

        check("bundle 摘要/文件摘要消费侧重验", t_bundle_integrity)

        # 11) source 层钩子 -> source_active / source_hash（驱动 restore-keys 条件化）
        def t_source_layer():
            (project / "ci-hooks" / "patch.py").write_text(
                "import os\n"
                "dist = os.environ['GODOT_HOOK_DIST']\n"
            "directives = os.environ['GODOT_HOOK_DIRECTIVES']\n"
                "os.makedirs(os.path.join(directives, 'patches'), exist_ok=True)\n"
                "with open(os.path.join(directives, 'patches', 'source.x.patch'), 'w', encoding='utf-8') as f:\n"
                "    f.write('--- a/x.cpp\\n+++ b/x.cpp\\n')\n", encoding="utf-8")
            m = _write_manifest(project, [_base_hook(
                id="patch", run="ci-hooks/patch.py",
                capabilities=["write:source_patch"], outputs=["source_patch"],
                cache_impact="source")], "source.json")
            bp, bd = base / "plan_src.json", base / "bundle_src"
            run_validate(m, out=bp, bundle_out=str(bd))
            rp = base / "result_src.json"
            rc = cmd_dispatch(_ns(bundle=str(bd), anchor="inject-config",
                                  phase="after", context=str(context_path),
                                  result=str(rp), plan="", chain="build"))
            assert rc == 0
            r = json.loads(rp.read_text(encoding="utf-8"))
            assert r["source_active"] is True and len(r["source_hash"]) == 8, r["source_hash"]
            assert r["hook_output_digests"]["source"], "source 层摘要未记录"
            assert "source_patch" in r["outputs"], r["outputs"]
            assert "source_patch" in r["hooks_outputs"]["patch"]
            # 同一 plan 两次 dispatch 的 source_hash 必须一致（确定性）
            rp2 = base / "result_src2.json"
            cmd_dispatch(_ns(bundle=str(bd), anchor="inject-config",
                             phase="after", context=str(context_path),
                             result=str(rp2), plan="", chain="build"))
            r2 = json.loads(rp2.read_text(encoding="utf-8"))
            assert r2["source_hash"] == r["source_hash"]

        check("source 层钩子产 source_hash（激活条件化 restore-keys）", t_source_layer)

        # 12) source_patch 补丁**内容**并入 source_hash（docs/hooks.md 分层缓存键 source_patch_digests）
        def t_source_patch_content_hash():
            """补丁文件内容变了 -> source_hash 必须变；补丁内容摘要须落进结果文件。

            补丁内容由脚本从 context.project.root 下读取（钩子只读项目根是既有语义），
            两次 dispatch 之间改这个文件即可改变补丁内容。
            """
            body_file = project / "ci-hooks" / "body.txt"
            (project / "ci-hooks" / "patch2.py").write_text(
                "import os\n"
                "dist = os.environ['GODOT_HOOK_DIST']\n"
            "directives = os.environ['GODOT_HOOK_DIRECTIVES']\n"
                "os.makedirs(os.path.join(directives, 'patches'), exist_ok=True)\n"
                "root = os.environ['GODOT_HOOK_PROJECT_ROOT']\n"
                "with open(os.path.join(root, 'ci-hooks', 'body.txt'), encoding='utf-8') as f:\n"
                "    body = f.read()\n"
                "with open(os.path.join(directives, 'patches', 'source.x.patch'), 'w', encoding='utf-8') as f:\n"
                "    f.write('--- a/x.cpp\\n+++ b/x.cpp\\n' + body + '\\n')\n",
                encoding="utf-8")
            m = _write_manifest(project, [_base_hook(
                id="patch", run="ci-hooks/patch2.py",
                capabilities=["write:source_patch"], outputs=["source_patch"],
                cache_impact="source")], "source2.json")
            bp, bd = base / "plan_src2.json", base / "bundle_src2"
            run_validate(m, out=bp, bundle_out=str(bd))

            def dispatch_with(body, out_name):
                body_file.write_text(body, encoding="utf-8")
                rp = base / out_name
                rc = cmd_dispatch(_ns(bundle=str(bd), anchor="inject-config",
                                      phase="after", context=str(context_path),
                                      result=str(rp), plan="", chain="build"))
                assert rc == 0
                return json.loads(rp.read_text(encoding="utf-8"))

            r1 = dispatch_with("A", "result_src_a.json")
            r2 = dispatch_with("B", "result_src_b.json")
            # 补丁内容摘要（补丁文件自身的 sha256）必须落进结果文件
            assert r1["source_patch_digests"], "补丁内容摘要未记录"
            assert len(r1["source_patch_digests"][0]) == 64, r1["source_patch_digests"]
            # 补丁内容不同 -> source_hash 必须不同（否则改了源码缓存键不变）
            assert r1["source_hash"] != r2["source_hash"], \
                "补丁内容变化未反映到 source_hash"
            assert r1["source_active"] is True

        check("source_patch 补丁内容并入 source_hash", t_source_patch_content_hash)

        # 13) release 链：validate --chain release 只收 publish 锚点钩子，
        #     dispatch（publish:before/after）与 write:artifacts 收集正常
        def t_release_chain():
            (project / "ci-hooks" / "notarize.py").write_text(
                "import json, os\n"
                "dist = os.environ['GODOT_HOOK_DIST']\n"
            "directives = os.environ['GODOT_HOOK_DIRECTIVES']\n"
                "os.makedirs(os.path.join(dist, 'artifacts'), exist_ok=True)\n"
                "with open(os.path.join(dist, 'artifacts', 'CHECKSUMS.txt'), 'w', encoding='utf-8') as f:\n"
                "    f.write('chain=' + os.environ.get('GODOT_HOOK_CHAIN', ''))\n", encoding="utf-8")
            m = _write_manifest(project, [_base_hook(
                id="notarize", run="ci-hooks/notarize.py",
                on={"anchor": "publish", "phase": "before"},
                capabilities=["write:artifacts"], outputs=["artifacts"],
                cache_impact="none")], "release.json")
            bp, bd = base / "plan_rel.json", base / "bundle_rel"
            run_validate(m, chain="release", out=bp, bundle_out=str(bd))
            plan = json.loads(bp.read_text(encoding="utf-8"))
            assert plan["chain"] == "release" and len(plan["hooks"]) == 1, plan
            # 按 chain 分组：publish 锚点只属 release 链，build/export 的 plan 不含它
            for other in ("build", "export"):
                po = base / f"plan_{other}_rel.json"
                run_validate(m, chain=other, out=po,
                         bundle_out=str(base / f"bundle_{other}_rel"))
                assert json.loads(po.read_text(encoding="utf-8"))["hooks"] == [], \
                    f"publish 锚点钩子不应进 {other} 链 plan"
            # dispatch：context.chain 必须为 release（与 --chain 一致）
            context_rel_path = base / "context_release.json"
            context_rel_path.write_text(
                json.dumps(_make_context(project, chain="release"), ensure_ascii=False),
                encoding="utf-8")
            rp = base / "result_rel.json"
            rc = cmd_dispatch(_ns(bundle=str(bd), anchor="publish",
                                  phase="before", context=str(context_rel_path),
                                  result=str(rp), plan="", chain="release"))
            assert rc == 0
            r = json.loads(rp.read_text(encoding="utf-8"))
            assert r["ok"] and r["chain"] == "release", r
            assert "artifacts" in r["outputs"], r["outputs"]
            assert "artifacts" in r["hooks_outputs"]["notarize"], r["hooks_outputs"]
            # chain 不一致时拒绝，且消息要点明两侧取值
            try:
                cmd_dispatch(_ns(bundle=str(bd), anchor="publish",
                                  phase="before", context=str(context_path),
                                  result=str(base / "result_rel_bad.json"),
                                  plan="", chain="release"))
            except HookError as e:
                assert "chain" in str(e), str(e)
            else:
                raise AssertionError("context.chain 与 --chain 不一致未被拒绝")

        check("release 链 validate/dispatch（publish 锚点 + chain 一致性）", t_release_chain)

        # 14) 逐预设锚点 export-preset-each：--current-preset 注入 context，
        #     on.presets 按当前预设过滤；整批锚点语义不变
        def t_export_preset_each():
            # 钩子只读断言 env：current_preset 经 flatten_context 展平为
            # GODOT_CONTEXT_EXPORT_CURRENT_PRESET
            (project / "ci-hooks" / "per_preset.py").write_text(
                "import os\n"
                "assert os.environ.get('GODOT_CONTEXT_EXPORT_CURRENT_PRESET') == 'Windows Desktop'\n",
                encoding="utf-8")
            m = _write_manifest(project, [_base_hook(
                id="per-preset", run="ci-hooks/per_preset.py",
                on={"anchor": "export-preset-each", "phase": "after"},
                capabilities=["read:context"], outputs=[],
                cache_impact="none")], "each.json")
            bp, bd = base / "plan_each.json", base / "bundle_each"
            run_validate(m, chain="export", out=bp, bundle_out=str(bd))
            plan = json.loads(bp.read_text(encoding="utf-8"))
            assert len(plan["hooks"]) == 1, plan
            context_exp = base / "context_export_each.json"
            ctx = _make_context(project, chain="export")
            ctx["export"].pop("current_preset")
            context_exp.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
            rp = base / "result_each.json"
            rc = cmd_dispatch(_ns(bundle=str(bd), anchor="export-preset-each",
                                  phase="after", context=str(context_exp),
                                  result=str(rp), plan="", chain="export",
                                  current_preset="Windows Desktop"))
            assert rc == 0, (rp.read_text(encoding="utf-8") if rp.exists() else "")
            r = json.loads(rp.read_text(encoding="utf-8"))
            assert r["ok"] and len(r["executed"]) == 1, r
            # on.presets 过滤按注入的 current_preset 判定
            m2 = _write_manifest(project, [_base_hook(
                id="per-preset", run="ci-hooks/per_preset.py",
                on={"anchor": "export-preset-each", "phase": "after",
                    "presets": ["Linux/X11"]},
                capabilities=["read:context"], outputs=[],
                cache_impact="none")], "each_filter.json")
            run_validate(m2, chain="export", out=base / "plan_each2.json",
                     bundle_out=str(base / "bundle_each2"))
            rp2 = base / "result_each2.json"
            rc2 = cmd_dispatch(_ns(bundle=str(base / "bundle_each2"),
                                   anchor="export-preset-each", phase="after",
                                   context=str(context_exp), result=str(rp2),
                                   plan="", chain="export",
                                   current_preset="Windows Desktop"))
            assert rc2 == 0
            r2 = json.loads(rp2.read_text(encoding="utf-8"))
            assert r2["executed"] == [], "on.presets 不含当前预设时不应执行"
            # on.presets 空名单 = 不过滤（新语义）
            assert hook_matches({"anchor": "x", "phase": "after", "presets": []}, ctx)

        check("逐预设锚点 export-preset-each（current_preset 注入 + on.presets 过滤）", t_export_preset_each)

        # 锚点表：声明了未接线 phase 的钩子在 validate 期显式拒绝（不再静默空转）
        def t_anchor_phase_gate():
            m = _write_manifest(project, [_base_hook(on={"anchor": "export-preset", "phase": "before"})], "phase_gate.json")
            try:
                run_validate(m)
            except HookError as e:
                assert "不支持 phase 'before'" in str(e), e
            else:
                raise AssertionError("export-preset:before（未接线的 phase）未被拒绝")
            m = _write_manifest(project, [_base_hook(on={"anchor": "compile", "phase": "after"})], "phase_ok.json")
            run_validate(m)  # 已接线组合放行

        check("锚点 phase 门禁（未接线组合拒绝 / 已接线放行）", t_anchor_phase_gate)

        # hooks-anchors.json 结构校验：字段拼错/缺字段/重复 id 显式拒绝，真表放行
        def t_anchor_table_structure():
            # 每条带预期理由：只断言「抛了 HookError」会把任何无关异常算成通过
            bad_tables = [
                ({"anchors": [{"id": "x", "chains": ["buildd"], "phases": ["after"]}]},
                 "链拼错", "chains"),
                ({"anchors": [{"id": "x", "chains": ["build"], "phases": ["after", "nope"]}]},
                 "phase 拼错", "phases"),
                ({"anchors": [{"id": "", "chains": ["build"], "phases": ["after"]}]},
                 "空 id", "id"),
                ({"anchors": [{"id": "x", "chains": ["build"]}]},
                 "缺 phases", "phases"),
                ({"anchors": [{"id": "x", "chains": ["build"], "phases": ["after"]},
                              {"id": "x", "chains": ["build"], "phases": ["before"]}]},
                 "id 重复", "重复"),
                ({"anchors": [{"id": "x", "chain": ["build"], "phases": ["after"]}]},
                 "字段名拼错", "chains"),
            ]
            for bad, why, needle in bad_tables:
                try:
                    validate_anchor_table(bad)
                except HookError as e:
                    if needle not in str(e):
                        raise AssertionError(
                            f"锚点表（{why}）拒绝理由不符：期望含 {needle!r}，实际 {str(e)!r}")
                else:
                    raise AssertionError(f"锚点表结构校验未拒绝：{bad}（{why}）")
            validate_anchor_table(read_json(str(DEFAULT_ANCHORS), "hooks-anchors.json"))  # 真表放行

        check("hooks-anchors.json 结构校验（拼错/缺字段/重复 id 拒绝，真表放行）", t_anchor_table_structure)

        # Windows 超时终止必须连子孙进程一起杀（不能只 kill 直接子进程）
        def t_windows_kill_tree():
            class FakeProc:
                pid = 4321

                def __init__(self):
                    self.killed = False

                def kill(self):
                    self.killed = True

            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd)
                return type("R", (), {"returncode": 0})()

            proc = FakeProc()
            with mock.patch.object(rh.os, "name", "nt"), \
                 mock.patch.object(rh.subprocess, "run", fake_run):
                rh.kill_process(proc)
            assert calls and calls[0][:4] == ["taskkill", "/T", "/F", "/PID"], calls
            assert calls[0][4] == "4321", calls
            assert not proc.killed, "taskkill 成功时不应再 fallback 到 proc.kill()"

            # taskkill 不可用（非零退出）→ 回退 proc.kill()，不至于留活进程
            def fail_run(cmd, **kw):
                return type("R", (), {"returncode": 1})()

            proc2 = FakeProc()
            with mock.patch.object(rh.os, "name", "nt"), \
                 mock.patch.object(rh.subprocess, "run", fail_run):
                rh.kill_process(proc2)
            assert proc2.killed, "taskkill 失败时未回退 proc.kill()"

        check("Windows 超时杀进程组（taskkill /T /F + 失败回退）", t_windows_kill_tree)

    width = max(len(n) for n, _, _ in cases)
    failed = 0
    for name, ok, detail in cases:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name.ljust(width)}" + (f"  {detail}" if detail else ""))
        if not ok:
            failed += 1
    print(f"\nselftest: {len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


# ---------------------------------------------------------------------------


if __name__ == "__main__":
    sys.exit(selftest())
