#!/usr/bin/env python3
"""Release 陈旧附件清理（project-release.yml「清理陈旧附件」执行器）。

上传用 `gh release upload --clobber`，只覆盖同名文件，**不删除已改名的旧文件**：
产物命名一旦变化（改名、增删预设、模板调整），上一轮的旧附件会永久滞留在
Release 上，与本次产物混在一起无法分辨哪个是当前版本。

判据设计——宁可漏删，不可误删：

1. **完整性门控**（`--complete`）：只有本轮导出的预设覆盖了项目 export_presets.cfg
   的全部预设，才认为「本轮产物 = 该项目当前应交付的完整集合」，其余同家族附件
   才是陈旧残留。只导出部分预设时，其他附件是上一轮的有效交付物，清理会静默
   丢产物，因此直接跳过。
2. **家族限定**（`--prefix`）：只处理 `<prefix>_` 开头的附件。手工上传的说明文件、
   与产物命名无关的附件一律不碰。
3. **期望集合**（`--expected`）：本次实际上传的文件名永不删除。**期望集为空即跳过**
   ——空集意味上游收集步骤异常，此时按「家族全部 − 空集」删会清空上一轮全部有效
   交付物，必须 fail-safe 并打印原因，绝不产生删除列表。
4. 四条任一不成立即跳过并打印原因（不静默通过，也不误删）。

用法：
  prune_release_assets.py --repo owner/repo --tag v1.0.0 --prefix demo \\
      --expected assets --complete true [--dry-run]

退出码恒为 0（清理失败不应让已成功的发布会话失败）：仅在完全没有可判定信息时
以 1 退出，提示配置问题。
"""
import argparse

from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import json
import subprocess
import sys
from pathlib import Path

GH = "gh"


def _run_gh(args: list) -> subprocess.CompletedProcess:
    """调用 gh CLI；未安装时给出明确报错（不是 traceback）。"""
    try:
        return subprocess.run([GH] + args, capture_output=True, text=True,
                              encoding="utf-8", check=False)
    except FileNotFoundError:
        fail("未找到 gh 命令；本步骤需要 GitHub CLI（runner 镜像自带，"
             "本地验证请改用 --existing-json）")


def list_assets(repo: str, tag: str) -> list:
    """Release 现有附件名（调用 gh CLI）。"""
    proc = _run_gh(["release", "view", tag, "--repo", repo, "--json", "assets"])
    if proc.returncode != 0:
        fail(f"读取 Release {tag} 附件失败：{proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        fail(f"gh 输出不是合法 JSON：{proc.stdout[:200]}")
    return [a.get("name", "") for a in data.get("assets") or [] if a.get("name")]


def delete_asset(repo: str, tag: str, name: str) -> bool:
    """删除单个附件；返回是否成功（失败只告警，不中断发布）。"""
    proc = _run_gh(["release", "delete-asset", tag, name, "--repo", repo, "--yes"])
    if proc.returncode != 0:
        print(f"::warning::删除附件 {name} 失败：{proc.stderr.strip()}", file=sys.stderr)
        return False
    return True


def select_stale(existing, expected, prefix: str, complete: bool):
    """挑出应删除的附件名（纯函数，便于自测）。

    返回 (待删列表, 跳过原因)；跳过原因为空串表示判据成立、待删列表可信。
    """
    if not complete:
        return [], "本轮未覆盖项目全部预设，无法判定哪些附件属于陈旧残留"
    if not prefix:
        return [], "未提供产物名前缀，无法界定附件家族（不猜、不误删）"
    family_prefix = f"{prefix}_"
    expected = set(expected)
    # 期望集合为空 = 上游收集步骤异常（目录名变更 / 失败未阻断），此时「家族全部 − 空集」
    # 会把上一轮所有有效附件删光，与「宁可漏删，不可误删」直接冲突。空集一律跳过清理，
    # 并把原因显式打印出来供 dry-run 观察——绝不产生删除列表。
    if not expected:
        return [], ("本次上传附件集合为空，无法判定哪些是陈旧残留"
                    "（上游收集步骤可能失败或目录名变更）；拒绝清空家族附件")
    family = [n for n in existing if n.startswith(family_prefix)]
    # 期望集合与家族前缀对不上（例如 export_naming 未含 {prefix}）：家族判定失效，
    # 此时按家族删会误删，直接跳过。
    if not any(n.startswith(family_prefix) for n in expected):
        return [], (f"本次产物名不以 '{family_prefix}' 开头，家族判定失效"
                    "（export_naming 需含 {prefix}）")
    return [n for n in family if n not in expected], ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/repo（Release 所在仓库）")
    ap.add_argument("--tag", required=True, help="Release Tag")
    ap.add_argument("--prefix", required=True,
                    help="产物名前缀（契约 artifacts.prefix / repo-safe），界定附件家族")
    ap.add_argument("--expected", default="assets",
                    help="本次已上传附件的目录（目录内文件名即期望集合）")
    ap.add_argument("--complete", default="false",
                    choices=("true", "false"),
                    help="本轮是否覆盖项目全部预设（false 时不清理）")
    ap.add_argument("--existing-json", default="",
                    help="现有附件名 JSON 数组（自测用，提供则不调用 gh）")
    ap.add_argument("--dry-run", action="store_true", help="只打印待删列表，不执行")
    args = ap.parse_args()

    if args.existing_json:
        try:
            existing = json.loads(args.existing_json)
        except ValueError:
            fail("--existing-json 不是合法 JSON")
    else:
        existing = list_assets(args.repo, args.tag)

    expected_dir = Path(args.expected)
    expected = sorted(p.name for p in expected_dir.iterdir() if p.is_file()) \
        if expected_dir.is_dir() else []

    complete = args.complete == "true"
    stale, skip_reason = select_stale(existing, expected, args.prefix, complete)

    print(f"现有附件 {len(existing)} 个；本次上传 {len(expected)} 个")
    if skip_reason:
        print(f"跳过清理：{skip_reason}")
        return 0
    if not stale:
        print("无陈旧附件，跳过清理")
        return 0

    print("--- 待清理附件 ---")
    for name in stale:
        print(f"  {name}")
    if args.dry_run:
        print("（dry-run：不执行删除）")
        return 0

    removed = [n for n in stale if delete_asset(args.repo, args.tag, n)]
    print(f"已清理 {len(removed)}/{len(stale)} 个陈旧附件")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
