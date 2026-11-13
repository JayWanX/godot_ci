#!/usr/bin/env python3
"""Android 签名：把 keystore 落到磁盘（prepare），并校验产物真由它签名（verify）。

Godot 的 Android 编辑器签名由 `platform/android/java/editor/build.gradle` 的
`hasReleaseSigningConfigs()` 判定，读三个环境变量：

    GODOT_ANDROID_SIGN_KEYSTORE     keystore 文件的绝对路径
    GODOT_ANDROID_KEYSTORE_ALIAS    key alias
    GODOT_ANDROID_SIGN_PASSWORD     store 密码（同时用作 key 密码）

三者齐备且文件存在才启用 release 签名；**任一缺失即静默回退 debug 签名**，并把
applicationId 加上 `.release` 后缀——产物看起来正常、能安装，只有签名身份是错的。
故本脚本不做「尽力而为」：密钥解不出来就直接失败，不让下游在不知情中产出错签名的包。

两个子命令：

    prepare   解 base64 keystore → 落盘 → keytool 验 alias/密码 → 写 $GITHUB_ENV
    verify    对照 keystore 的证书指纹核对 APK 的签名者，不一致即失败

prepare 读环境变量（由 workflow 从 secrets 注入，不经 argv）：

    ANDROID_KEYSTORE_B64    base64 编码的 keystore（缺省=不做签名，正常退出）
    ANDROID_KEYSTORE_ALIAS  alias
    ANDROID_KEYSTORE_PASS   密码

verify 需要 prepare 写在 `$GITHUB_ENV` 里的三个 GODOT_ANDROID_* 变量仍在环境中
（后续 step 自动继承），参数为 APK 路径。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KEYSTORE_ENV = "ANDROID_KEYSTORE_B64"
ALIAS_ENV = "ANDROID_KEYSTORE_ALIAS"
PASS_ENV = "ANDROID_KEYSTORE_PASS"

# 与 build.gradle 读取的名字逐字一致，改一处即失效。
GRADLE_KEYSTORE = "GODOT_ANDROID_SIGN_KEYSTORE"
GRADLE_ALIAS = "GODOT_ANDROID_KEYSTORE_ALIAS"
GRADLE_PASS = "GODOT_ANDROID_SIGN_PASSWORD"

# 导出器（`godot --export-release`）读的是另一组名字：它走 EditorExportPreset::get_or_env，
# 环境变量优先于预设里的值，故 CI 只需注入这三个就能给导出签名，密钥不进仓库。
# 名字逐字取自 `platform/android/export/export_plugin.h` 的 ENV_ANDROID_KEYSTORE_RELEASE_*。
EXPORT_KEYSTORE = "GODOT_ANDROID_KEYSTORE_RELEASE_PATH"
EXPORT_ALIAS = "GODOT_ANDROID_KEYSTORE_RELEASE_USER"
EXPORT_PASS = "GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD"

SIGNED_FLAG = "ANDROID_SIGNED"

# keystore 落在工作区之外，避免被打进任何产物或缓存。
KEYSTORE_DIR_PREFIX = "android-keystore-"


def fail(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 1


def note(message: str) -> None:
    print(f"::notice::{message}")


def write_env(pairs: dict[str, str]) -> None:
    target = os.environ.get("GITHUB_ENV")
    if not target:
        # 本地直接跑时只打印，便于排查。
        for key, value in pairs.items():
            print(f"{key}={value}")
        return
    with open(target, "a", encoding="utf-8") as handle:
        for key, value in pairs.items():
            handle.write(f"{key}={value}\n")


def decode_keystore(raw: str) -> bytes:
    """解 base64；容忍换行与空白（用户常从文本编辑器整段粘贴）。"""
    compact = "".join(raw.split())
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{KEYSTORE_ENV} 不是合法 base64：{exc}") from exc


def run_tool(command: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout or "", result.stderr or ""


def verify_key_password(keystore: Path, alias: str, password: str) -> str:
    """验证 key 密码也能解开该 alias 的私钥条目。空串=通过，否则返回原因。

    **为什么必须单独验**：Godot 的 `editor/build.gradle` 把 keyPassword 与
    storePassword 一并取 `getGodotSigningPassword()`。若 keystore 的 key 密码与
    store 密码不同，`keytool -list` 照样通过（它只验 store 密码），而 gradle 会
    签名失败或静默回退 debug 签名。

    判据：`keytool -certreq` 必须解开私钥才能生成 CSR，故它能同时证明
    「alias 是私钥条目」与「key 密码正确」。`-list` 做不到这一点。
    """
    keytool = shutil.which("keytool")
    if keytool is None:
        return "找不到 keytool，无法校验 key 密码（本步要求 JDK 已安装）"
    code, out, err = run_tool([
        keytool, "-certreq",
        "-keystore", str(keystore),
        "-alias", alias,
        "-storepass", password,
        "-keypass", password,
        "-file", os.devnull,
    ])
    if code == 0:
        return ""
    detail = (err or out or "").strip()
    lowered = detail.lower()
    if "password" in lowered or "password verification failed" in lowered:
        return (
            "key 密码与 store 密码不一致。Godot 只用同一个值填 keyPassword 与 "
            "storePassword（editor/build.gradle 的 getGodotSigningPassword），"
            "故本方案要求两者相同；请用 `keytool -keypasswd` 把它们改成一致后重试。"
        )
    tail = detail.splitlines()[-1] if detail else f"exit {code}"
    return f"keytool -certreq 失败（无法解开私钥）：{tail}"


def keystore_fingerprint(keystore: Path, alias: str, password: str) -> tuple[str, str]:
    """取 keystore 中该 alias 的证书 SHA-256 指纹（32 字节十六进制，无分隔符）。

    这是「这份 keystore 的身份」本身，后面拿它跟 APK 的签名者比对。
    """
    keytool = shutil.which("keytool")
    if keytool is None:
        return "", "找不到 keytool，无法校验签名配置（本步要求 JDK 已安装）"
    code, out, err = run_tool([
        keytool, "-list", "-v",
        "-keystore", str(keystore),
        "-alias", alias,
        "-storepass", password,
    ])
    if code != 0:
        detail = (err or out or "").strip().splitlines()
        return "", "keytool 校验失败（alias 不存在或密码不对）：" + (
            detail[-1] if detail else f"exit {code}")
    for line in out.splitlines():
        # 形如「SHA256: AB:CD:...」，取该 alias 证书条目的那个。
        if "SHA256:" in line.upper():
            digest = line.split(":", 1)[1]
            return normalize_fingerprint(digest), ""
    return "", f"keytool 输出里找不到 SHA256 指纹（alias {alias}）"


def normalize_fingerprint(value: str) -> str:
    """去掉所有非十六进制字符并小写，便于跨工具比对。"""
    return "".join(ch for ch in value.lower() if ch in "0123456789abcdef")


def find_apksigner() -> str:
    """在 Android SDK 的 build-tools 里找 apksigner（取版本号最大的一个）。

    先看 `ANDROID_HOME` / `ANDROID_SDK_ROOT`（GitHub 托管 runner 与 setup-android
    都会设），再退到常见安装位置，最后查 PATH。找不到即让调用方据实报因——
    「找不到工具」与「签名不对」是两种完全不同的结论。
    """
    roots: list[Path] = []
    for root_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(root_name)
        if root:
            roots.append(Path(root))
    # GitHub 托管 runner 的预装位置（未显式导出变量时也能命中）。
    roots.extend([
        Path("/usr/local/lib/android/sdk"),
        Path.home() / "Android" / "Sdk",
        Path.home() / "Library" / "Android" / "sdk",
    ])
    for root in roots:
        build_tools = root / "build-tools"
        if not build_tools.is_dir():
            continue
        for version_dir in sorted(build_tools.iterdir(), reverse=True):
            for name in ("apksigner", "apksigner.bat"):
                candidate = version_dir / name
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("apksigner") or ""


def apk_signer_fingerprints(apk: Path) -> tuple[list[str], str]:
    """取 APK 全部签名者的证书 SHA-256 指纹。

    每行形如「Signer #1 certificate SHA-256 digest: ab:cd:…」，指纹值本身含 32 个
    冒号分隔组。切法用 `partition("SHA-256 digest:")`：既能剥掉前缀，又不会动指纹
    内部的冒号。
    """
    apksigner = find_apksigner()
    if not apksigner:
        return [], "找不到 apksigner（需要 ANDROID_HOME/build-tools）"
    code, out, err = run_tool([apksigner, "verify", "--print-certs", str(apk)])
    if code != 0:
        detail = (err or out or "").strip().splitlines()
        return [], "apksigner 校验失败（APK 未签名或签名无效）：" + (
            detail[-1] if detail else f"exit {code}")
    digests: list[str] = []
    for line in out.splitlines():
        # 排除 SHA-1 / SHA-512 等同族行，只留 SHA-256。
        if "SHA-256 digest" not in line:
            continue
        # 按标记切掉前缀，指纹内部的冒号保持不动。
        _, _, tail = line.partition("SHA-256 digest:")
        digest = normalize_fingerprint(tail)
        # 合法的 SHA-256 十六进制串恰为 64 字符。长度不符即解析出错，如实报因而不是
        # 拿一个错值去比对——错值只会得出「签名不对」这种归因错误的结论。
        if len(digest) != 64:
            return [], (
                f"apksigner 输出的 SHA-256 指纹长度异常（{len(digest)} 字符，期望 64）："
                f"{line.strip()}")
        digests.append(digest)
    if not digests:
        return [], "apksigner 输出里找不到签名者 SHA-256 指纹"
    return digests, ""


def collect_all_apks(directory: Path) -> list[Path]:
    """取产物目录下的全部 APK（含 debug；编辑器产物有 android / horizonos / picoos 三套）。"""
    return sorted(path for path in directory.rglob("*.apk") if path.is_file())


def is_debug_apk(path: Path) -> bool:
    """是否为 debug 档产物。

    `generateGodotEditor` 同时产 release 与 debug 两档（`editor/build.gradle` 的
    `buildTypes` 两者都有）。debug 档恒用 SDK 的 debug 证书签名（`buildTypes.debug`
    的 `signingConfig signingConfigs.debug`），与配置的 keystore 无关。
    """
    return path.stem.endswith("-debug")


def cmd_prepare(_args: argparse.Namespace) -> int:
    raw = (os.environ.get(KEYSTORE_ENV) or "").strip()
    alias = (os.environ.get(ALIAS_ENV) or "").strip()
    password = os.environ.get(PASS_ENV) or ""

    if not raw:
        # 不做签名是合法选择：产物用 debug 签名，applicationId 带 .release 后缀。
        # 出声说明，免得使用者以为拿到的是正式签名包。
        note(f"未配置 {KEYSTORE_ENV}：Android 编辑器将使用 debug 签名（applicationId 带 .release 后缀）")
        write_env({SIGNED_FLAG: "false"})
        return 0

    # 给了 keystore 就必须给全：半套配置只会静默回退 debug 签名，比不配更危险。
    missing = [name for name, value in ((ALIAS_ENV, alias), (PASS_ENV, password)) if not value]
    if missing:
        return fail(
            f"已配置 {KEYSTORE_ENV} 但缺少 {', '.join(missing)}；"
            "签名配置不完整会静默回退 debug 签名，故直接失败。要么三项配齐，要么都不配。")

    try:
        payload = decode_keystore(raw)
    except ValueError as exc:
        return fail(str(exc))

    keystore_dir = Path(tempfile.mkdtemp(prefix=KEYSTORE_DIR_PREFIX))
    keystore = keystore_dir / "release.keystore"
    keystore.write_bytes(payload)

    fingerprint, problem = keystore_fingerprint(keystore, alias, password)
    if problem:
        return fail(problem)

    # 指纹对了不等于能用：Godot 用一个密码同时当 storePassword 与 keyPassword，
    # 故须确认 key 密码也是它，否则 gradle 签不出来。
    key_problem = verify_key_password(keystore, alias, password)
    if key_problem:
        return fail(key_problem)

    # 只写路径，不写内容：keystore 留在临时目录，job 结束即随 runner 销毁。
    # **两组名字都写**：gradle 构建（编辑器产物）读 GRADLE_*，导出器（导出 APK）读
    # EXPORT_*；少任一组都会让对应那条路静默不签名。
    write_env({
        SIGNED_FLAG: "true",
        GRADLE_KEYSTORE: str(keystore),
        GRADLE_ALIAS: alias,
        GRADLE_PASS: password,
        EXPORT_KEYSTORE: str(keystore),
        EXPORT_ALIAS: alias,
        EXPORT_PASS: password,
    })
    print(f"Android 签名已就绪：keystore {len(payload)} 字节，"
          f"alias {alias}，证书 SHA-256 {fingerprint}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if os.environ.get(SIGNED_FLAG) != "true":
        note("本次构建未启用 release 签名，跳过签名校验")
        return 0

    keystore_path = os.environ.get(GRADLE_KEYSTORE) or ""
    alias = os.environ.get(GRADLE_ALIAS) or ""
    password = os.environ.get(GRADLE_PASS) or ""
    if not (keystore_path and alias and password):
        return fail("签名环境变量不完整，无法校验产物签名身份")

    target = Path(args.apk_dir)
    # **先看清目录里有什么，再决定要不要校验**：release 档能不能产出由上游决定
    # （SCons 的 store_release 开关决定 .so 落到 libs/tools/release 还是 debug，
    # gradle 只编有 native libs 的那档）。没有 release 产物时如实产出 debug 版本
    # 是正常路径，不该判失败。
    all_apks = collect_all_apks(target)
    release_apks = [p for p in all_apks if not is_debug_apk(p)]

    if not all_apks:
        return fail(f"{target} 下没有任何 APK，构建未产出编辑器安装包")

    if not release_apks:
        # 只产 debug 档：如实说明为什么，不报错。
        note(
            f"未产出 release 档 APK，只有 debug 版本（{len(all_apks)} 个）。"
            "Android 编辑器只有在该档积累了 native libs 时才会构建：SCons 需 "
            "store_release=yes 才会把 .so 落到 lib/libs/tools/release，"
            "否则只落 debug。debug 档按 Android 惯例用 SDK 的 debug 证书签名，"
            "不使用配置的 keystore——本次配置的签名未被使用。")
        for apk in all_apks:
            print(f"  产出：{apk.name}")
        return 0

    expected, problem = keystore_fingerprint(Path(keystore_path), alias, password)
    if problem:
        return fail(f"取 keystore 指纹失败：{problem}")

    for apk in release_apks:
        actual, problem = apk_signer_fingerprints(apk)
        if problem:
            return fail(f"{apk.name}: {problem}")
        # 多签名者的包也允许：只要求其中一个就是我们的证书。
        if expected not in actual:
            return fail(
                f"{apk.name} 的签名者不是配置的 keystore。期望 SHA-256 {expected}，"
                f"实际 {', '.join(actual) or '无'}——gradle 很可能静默回退了 debug 签名")
        print(f"签名校验通过：{apk.name}")

    print(f"共校验 {len(release_apks)} 个 release APK，签名身份一致")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="解 keystore 并写 gradle 需要的环境变量")
    verify = sub.add_parser("verify", help="校验 APK 的签名者是否为该 keystore")
    verify.add_argument("--apk-dir", required=True, help="含 APK 的目录（递归查找）")
    args = parser.parse_args(argv)

    if args.command == "verify":
        return cmd_verify(args)
    return cmd_prepare(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
