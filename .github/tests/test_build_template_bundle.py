#!/usr/bin/env python3
"""build_template_bundle.py 自测：触发器模板包的打包判据。

覆盖：
- 包内全部条目都在 `.github/` 下（解压到项目仓库根即落位），且该目录与真实
  项目仓库的 `.github/` 同构
- 包内没有顶层 `README.md`（否则解压会覆盖项目自己的 README）
- 使用说明以 `.github/CI-USAGE.md` 落位且是必需条目
- 维护者视角的 `README.md` 不进包
- 必需文件缺失 → fail-fast（不产出残缺模板包）
- 源目录缺失 → fail-fast
- 可复现：同样输入两次打包字节一致（tag 发布重跑不应产生不同附件）

用法：python .github/tests/test_build_template_bundle.py
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REAL = Path(__file__).resolve().parents[1]
SCRIPT = REAL / "scripts" / "build_template_bundle.py"
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
sys.path.insert(0, str(REAL / "scripts"))
from jsonc import read_json  # noqa: E402
import build_template_bundle as btb  # noqa: E402

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def run(src, out, name):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--src", str(src), "--out", str(out), "--name", name],
        capture_output=True, text=True, encoding="utf-8", env=ENV)


def make_templates(root: Path, required=True) -> Path:
    """造一份最小模板目录（结构与真实 templates/ 同构）。"""
    tpl = root / "templates"
    (tpl / "workflows").mkdir(parents=True)
    if required:
        (tpl / "ci-contract.json").write_text("{}", encoding="utf-8")
        (tpl / "USAGE.md").write_text("使用者说明", encoding="utf-8")
        for wf in ("build", "export", "project-release", "engine-release"):
            (tpl / "workflows" / f"{wf}.yml").write_text(f"name: {wf}\n", encoding="utf-8")
    (tpl / "README.md").write_text("维护者说明", encoding="utf-8")
    return tpl


@check("包内全部条目都在 .github/ 下（无顶层文件，解压不覆盖项目根）")
def t_all_under_github():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tpl = make_templates(root)
        proc = run(tpl, root / "dist", "bundle.zip")
        assert proc.returncode == 0, proc.stderr
        with zipfile.ZipFile(root / "dist" / "bundle.zip") as zf:
            names = sorted(zf.namelist())
        assert names == [
            ".github/CI-USAGE.md",
            ".github/ci-contract.json",
            ".github/workflows/build.yml",
            ".github/workflows/engine-release.yml",
            ".github/workflows/export.yml",
            ".github/workflows/project-release.yml",
        ], names
        # 关键判据：没有任何条目落在包根——解压到项目仓库根时不可能覆盖用户的文件
        assert all(n.startswith(".github/") for n in names), names


@check("使用说明以 .github/CI-USAGE.md 落位，内容是源侧 USAGE.md")
def t_usage_renamed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tpl = make_templates(root)
        assert run(tpl, root / "dist", "b.zip").returncode == 0
        with zipfile.ZipFile(root / "dist" / "b.zip") as zf:
            usage = zf.read(".github/CI-USAGE.md").decode("utf-8")
        assert "使用者说明" in usage, usage
        # 维护者 README 既不进包根，也不在 .github/ 下
        assert "维护者说明" not in usage


@check("必需条目含 CI-USAGE.md：源侧 USAGE.md 缺失 → fail-fast")
def t_usage_required():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tpl = make_templates(root)
        (tpl / "USAGE.md").unlink()
        proc = run(tpl, root / "dist", "b.zip")
        assert proc.returncode == 1, proc.stdout
        assert ".github/CI-USAGE.md" in proc.stderr, proc.stderr
        assert not (root / "dist" / "b.zip").exists()


@check("必需文件缺失 → fail-fast（不产出残缺包）")
def t_missing_required():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 有内容但不含必需文件（只留被排除的 README 会被判「目录为空」，
        # 故额外放一个非必需的工作流文件，走到「缺失必需文件」这一支）
        tpl = make_templates(root, required=False)
        (tpl / "workflows" / "extra.yml").write_text("name: x\n", encoding="utf-8")
        proc = run(tpl, root / "dist", "b.zip")
        assert proc.returncode == 1, proc.stdout
        assert "模板缺失必需文件" in proc.stderr, proc.stderr
        assert not (root / "dist" / "b.zip").exists()


@check("目录只有被排除的文件 → 判为空（fail-fast）")
def t_only_excluded():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tpl = make_templates(root, required=False)
        proc = run(tpl, root / "dist", "b.zip")
        assert proc.returncode == 1, proc.stdout
        assert "模板目录为空" in proc.stderr, proc.stderr


@check("源目录缺失 → fail-fast")
def t_missing_inputs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = run(root / "nope", root / "dist", "b.zip")
        assert proc.returncode == 1 and "模板目录不存在" in proc.stderr, proc.stderr


@check("可复现：同样输入两次打包字节一致")
def t_reproducible():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tpl = make_templates(root)
        out = root / "dist"
        hashes = []
        for _ in range(2):
            assert run(tpl, out, "b.zip").returncode == 0
            hashes.append(hashlib.sha256((out / "b.zip").read_bytes()).hexdigest())
        assert hashes[0] == hashes[1], hashes


@check("可复现：create_system 固定（不随宿主 OS 变）")
def t_create_system_fixed():
    """ZipInfo.create_system 默认取宿主 OS：Windows 打包得 0、Unix 得 3。

    该字段进 zip 头部，字节随打机器的 OS 变——tag 重跑会产出不同附件哈希。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tpl = make_templates(root)
        out = root / "dist"
        assert run(tpl, out, "c.zip").returncode == 0
        with zipfile.ZipFile(out / "c.zip") as zf:
            systems = {info.create_system for info in zf.infolist()}
            names = zf.namelist()
        assert systems == {3}, f"create_system 未固定为 Unix：{systems}"
        assert ".github/CI-USAGE.md" in names, names


@check("真实模板包落位最小项目后 --check 通过（扩展默认关闭，样例清单不阻断）")
def t_default_copy_works():
    """把真实 templates/ 打包 → 解压到临时项目根 → 直接跑 --check，
    不删样例清单也不填脚本即通过（契约 hooks.policy=off）。"""
    templates = REAL / "templates"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dist = root / "dist"
        proc = run(templates, dist, "bundle.zip")
        assert proc.returncode == 0, proc.stderr
        project = root / "project"
        project.mkdir()
        with zipfile.ZipFile(dist / "bundle.zip") as zf:
            zf.extractall(project)
        contract_path = project / ".github" / "ci-contract.json"
        assert contract_path.is_file(), "包内缺契约"
        # 默认关闭：样例清单留在包里也不会在 resolve 期被读取（契约是 JSONC）
        contract = read_json(str(contract_path), "ci-contract.json")
        assert contract["hooks"]["policy"] == "off", contract["hooks"]
        env = dict(ENV)
        env["CI_CONTRACT"] = str(contract_path)
        env["PROJECT_ROOT"] = str(project)
        env.pop("CALLER_USES_REF", None)
        env.pop("GITHUB_REPOSITORY", None)
        env.pop("WORKFLOW_REPOSITORY", None)
        check = subprocess.run(
            [sys.executable, str(REAL / "scripts" / "godot_context.py"), "--check"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(project))
        assert check.returncode == 0, f"--check 失败：{check.stdout}\n{check.stderr}"


@check("真实模板落位后：说明文档在 .github/ 下且不含断链")
def t_real_usage_selfcontained():
    """使用者文档必须能独立读懂。

    断链判据：包内不存在 `docs/` 与 `contract/` 目录，故说明正文不得要求读者
    去那两处取接入所需信息；也不得出现「见本目录 README」这类指向包内不存在
    文件的指引。
    """
    templates = REAL / "templates"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        assert run(templates, root / "dist", "bundle.zip").returncode == 0
        project = root / "project"
        project.mkdir()
        with zipfile.ZipFile(root / "dist" / "bundle.zip") as zf:
            zf.extractall(project)
            names = set(zf.namelist())
        # 包内确实没有 docs/ 与 contract/
        assert not any(n.startswith(".github/docs/") for n in names), names
        assert not any(n.startswith(".github/contract/") for n in names), names
        usage = (project / ".github" / "CI-USAGE.md").read_text(encoding="utf-8")
        # 指向包内不存在文件的指引
        assert "见本目录 README" not in usage, "说明文档指向包内不存在的 README"
        # 接入所需的关键信息必须在包内文档本体里（不能只给外部链接）
        for must in ("来源声明", "remote:", "local:", "锁版本", "secrets", "落位"):
            assert must in usage, f"使用说明缺「{must}」"
        # 触发器里对说明文档的指引必须落在包内真实存在的路径上
        for wf in ("build", "export", "project-release", "engine-release"):
            text = (project / ".github" / "workflows" / f"{wf}.yml").read_text(encoding="utf-8")
            for line in text.splitlines():
                if "本目录 README" in line:
                    raise AssertionError(f"{wf}.yml 指向包内不存在的 README：{line}")
                if ".github/CI-USAGE.md" in line:
                    assert ".github/CI-USAGE.md" in names, f"{wf}.yml 指向的说明文档不在包内"


@check("REQUIRED / EXCLUDED 清单与真实模板一致（含必填标注）")
def t_required_excluded():
    # 必需条目在真实模板里都存在（USAGE.md 源侧以 CI-USAGE.md 落位，单独映射）
    for rel in btb.REQUIRED:
        src = "USAGE.md" if rel == ".github/CI-USAGE.md" else rel[len(".github/"):]
        assert (REAL / "templates" / src).is_file(), rel
    # 维护者 README 不进包
    assert "README.md" in btb.EXCLUDED_NAMES
    # 发布页正文源不进包（它是 Release notes，不是模板内容）
    assert "RELEASE-NOTES.md" in btb.EXCLUDED_NAMES
    assert (REAL / "templates" / "RELEASE-NOTES.md").is_file(), "缺发布页正文源"
    # 契约里的 <version> 占位符是业务必填（不是「零必改」的一部分）
    text = (REAL / "templates" / "ci-contract.json").read_text(encoding="utf-8")
    assert "<version>" in text, "契约缺业务必填占位符标注"


@check("发布页正文源：$TAG 占位符存在，渲染后无残留")
def t_release_notes():
    """RELEASE-NOTES.md 由 template-release 用 sed 替换 $TAG 后作为 Release 正文。

    若无 $TAG 占位符，发布页标题会与 tag 脱节；若替换后仍有残留，正文里会出现字面量。
    """
    text = (REAL / "templates" / "RELEASE-NOTES.md").read_text(encoding="utf-8")
    assert "$TAG" in text, "发布页正文缺 $TAG 占位符"
    rendered = text.replace("$TAG", "v9.9.9")
    assert "$TAG" not in rendered, "渲染后仍有 $TAG 残留"
    assert "v9.9.9" in rendered
    # 解压命令必须与包名同构（模板包名为 godot_ci_trigger_templates_<tag>.zip）
    assert "godot_ci_trigger_templates_v9.9.9.zip" in rendered, "解压命令与包名不同构"


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
