#!/usr/bin/env python3
"""Release 附件收集（project-release.yml「收集产物」执行器）。

把 actions/download-artifact 下载到 src 的导出产物收集到扁平的 dist：
- 布局兼容：多 artifact 时各占一个子目录（<artifact>/<file>），命中单个时内容
  平铺（v5+ 行为）——两种布局统一收集到 dist 根；
- 同名冲突消解：先加平台（artifact 目录名）前缀 → 仍冲突时 base.N.ext 序号
  递增（无扩展名文件为 base.N）；
- 收集数为 0 → fail-fast。

遍历按路径排序，占名顺序确定。

用法：collect_release_assets.py [--src artifacts] [--dist assets]
"""
import argparse

from jsonc import ensure_utf8_stdio, fail  # noqa: E402
import shutil
import sys
from pathlib import Path


def collect(src: Path, dist: Path) -> int:
    if not src.is_dir():
        fail(f"未下载到任何导出产物：{src} 不存在")
    files = sorted(p for p in src.rglob("*") if p.is_file())
    dist.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in files:
        rel = f.relative_to(src)
        plat = rel.parts[0] if len(rel.parts) > 1 else ""
        base = rel.parts[-1]
        name = base
        if (dist / name).exists():
            if plat:
                name = f"{plat}-{base}"
            if (dist / name).exists():
                prefix = f"{plat}-" if plat else ""
                stem, dot, ext = base.rpartition(".")
                n = 1
                while (dist / name).exists():
                    name = f"{prefix}{stem}.{n}.{ext}" if dot and stem else f"{prefix}{base}.{n}"
                    n += 1
        shutil.move(str(f), str(dist / name))
        count += 1
    if count == 0:
        fail("未下载到任何导出产物")
    print(f"共收集 {count} 个产物")
    print("--- 待上传附件 ---")
    for p in sorted(dist.iterdir()):
        size = p.stat().st_size if p.is_file() else 0
        print(f"{p}  {size} bytes")
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="artifacts", help="下载产物根目录")
    ap.add_argument("--dist", default="assets", help="附件扁平收集目录")
    args = ap.parse_args()
    collect(Path(args.src), Path(args.dist))
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    sys.exit(main())