#!/usr/bin/env python3
"""打包项目仓库侧触发器模板（template-release 工作流「打包模板」执行器）。

把 `.github/templates/` 打成**可直接解压落位**的 zip：
- 包内全部条目都在 `.github/` 下，与该目录在项目仓库中的真实位置一一对应，
  解压到项目仓库根即得 `.github/workflows/*.yml` / `.github/ci-contract.json` /
  `.github/ci-hooks.json` / `.github/CI-USAGE.md`；
- 包内没有顶层 `README.md`：项目仓库根本就该放项目自己的 README，解压覆盖它是
  静默数据损失。使用说明落 `.github/CI-USAGE.md`，与包内其余文件同层；
- `templates/README.md`（维护者视角的目录说明）不进入包内。

用法：
  build_template_bundle.py --src .github/templates --out dist \
      --name godot_ci_trigger_templates_v1.0.0.zip
"""
import argparse

from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import sys
import zipfile
from pathlib import Path

# 包内必须存在的条目（源文件缺失即 fail-fast，不产出残缺模板包）
REQUIRED = (
    ".github/ci-contract.json",
    ".github/CI-USAGE.md",
    ".github/workflows/build.yml",
    ".github/workflows/export.yml",
    ".github/workflows/project-release.yml",
    ".github/workflows/engine-release.yml",
)

# 不进包的文件：README.md 是维护者视角的目录说明，进包会让使用者误读成
# 「包内目录说明」；RELEASE-NOTES.md 是发布页正文源，只给 Release 用。
# USAGE.md 以 .github/CI-USAGE.md 落位，由 collect 直接收进来，不排除。
EXCLUDED_NAMES = {"README.md", "RELEASE-NOTES.md"}


def collect(src: Path):
    """按相对路径收集模板文件（排除维护者 README 与隐藏文件）。"""
    if not src.is_dir():
        fail(f"模板目录不存在：{src}")
    files = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if path.name in EXCLUDED_NAMES:
            continue
        if any(part.startswith(".") for part in path.relative_to(src).parts):
            continue  # .git 之类
        files.append(path)
    if not files:
        fail(f"模板目录为空：{src}")
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=".github/templates", help="模板源目录")
    ap.add_argument("--out", default="dist", help="输出目录")
    ap.add_argument("--name", default="", help="产物 zip 文件名（--check 时可省）")
    ap.add_argument("--check", action="store_true",
                    help="只做落位校验（必需文件 / 说明文档），不写产物（本地预检用）")
    args = ap.parse_args()
    if not args.check and not args.name:
        ap.error("--name 是必填项（除非用 --check 只做校验）")

    src = Path(args.src)
    out = Path(args.out)
    files = collect(src)

    # 与真实仓库布局一致：模板目录镜像 `.github/`，故包内路径 = `.github/<相对路径>`。
    # 唯一改名：源侧的 USAGE.md 以 `.github/CI-USAGE.md` 落位——USAGE.md 在维护者
    # 视角下是「模板目录的使用说明」，落位后必须自带归属，否则在项目仓库里会被
    # 当成项目自身的 usage。
    entries = []
    for p in files:
        rel = p.relative_to(src).as_posix()
        arc = ".github/CI-USAGE.md" if rel == "USAGE.md" else f".github/{rel}"
        entries.append((arc, p))
    present = {name for name, _ in entries}
    missing = [r for r in REQUIRED if r not in present]
    if missing:
        fail(f"模板缺失必需文件：{missing}（实际 {sorted(present)}）")

    if args.check:
        # 与 bundle 同源的校验都已在上面跑过：把将要打包的清单打出来即可，
        # 不写 zip（本地预检不该产生需要清理的产物）
        print(f"模板包校验通过（{len(entries)} 个文件）：")
        for name in sorted(present):
            print(f"  {name}")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    target = out / args.name
    # 固定时间戳、权限与宿主标识：同样的输入产出同样的字节（跨平台可复现）
    stamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, path in sorted(entries):
            info = zipfile.ZipInfo(arcname, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # 恒为 Unix：默认值随宿主 OS 变，产物字节随之变
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())

    print(f"--- {target} ---")
    with zipfile.ZipFile(target) as zf:
        for name in zf.namelist():
            print(f"  {name}  {zf.getinfo(name).file_size} bytes")
    print(f"共 {len(entries)} 个文件")
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())
