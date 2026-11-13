#!/usr/bin/env python3
"""android_keystore.py 自测：签名配置的四种形态与产物校验。

重点钉住「半套配置必须失败」——Godot 的 editor/build.gradle 在签名配置不完整时
静默回退 debug 签名，产物看起来一样、能安装，只有签名身份是错的。故脚本必须
在 gradle 之前就拒绝，而不是尽力而为。

用法：python .github/tests/test_android_keystore.py
"""
import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "android_keystore.py"

# 允许测试直接 import 被测模块（t_key_password_* 用假 run_tool 打桩）。
sys.path.insert(0, str(SCRIPT.parent))

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def run(args, extra_env, cwd):
    """跑脚本，返回 (proc, GITHUB_ENV 内容)。"""
    fd, env_file = tempfile.mkstemp()
    os.close(fd)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANDROID_KEYSTORE_")}
    env.update({"GITHUB_ENV": env_file, "PYTHONIOENCODING": "utf-8"})
    env.update(extra_env)
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + args,
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd)
        return proc, Path(env_file).read_text(encoding="utf-8")
    finally:
        os.unlink(env_file)


def env_pairs(blob):
    """把 GITHUB_ENV 内容解析成字典。"""
    pairs = {}
    for line in blob.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key] = value
    return pairs


@check("未配 keystore：正常退出并标 signed=false")
def t_no_keystore():
    with tempfile.TemporaryDirectory() as td:
        proc, blob = run(["prepare"], {}, td)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "debug 签名" in proc.stdout, proc.stdout
        pairs = env_pairs(blob)
        assert pairs.get("ANDROID_SIGNED") == "false", pairs
        # 未签名时不得写出 gradle 变量：写了就等于声称配好了
        assert "GODOT_ANDROID_SIGN_KEYSTORE" not in pairs, pairs


@check("只有 keystore 缺 alias/pass：失败而不是回退 debug 签名")
def t_partial_config():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run(["prepare"], {"ANDROID_KEYSTORE_B64": "AAAA"}, td)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "ANDROID_KEYSTORE_ALIAS" in proc.stderr, proc.stderr
        assert "ANDROID_KEYSTORE_PASS" in proc.stderr, proc.stderr
        # 缺 alias 但有 pass 也要失败（不能只查一个）
        proc, _ = run(["prepare"],
                      {"ANDROID_KEYSTORE_B64": "AAAA", "ANDROID_KEYSTORE_PASS": "p"}, td)
        assert proc.returncode == 1 and "ALIAS" in proc.stderr, proc.stderr


@check("非法 base64：点名失败")
def t_bad_base64():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run(["prepare"], {
            "ANDROID_KEYSTORE_B64": "!!!不是base64!!!",
            "ANDROID_KEYSTORE_ALIAS": "k",
            "ANDROID_KEYSTORE_PASS": "p",
        }, td)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "base64" in proc.stderr, proc.stderr


@check("base64 含换行与空白仍可解（用户整段粘贴）")
def t_base64_with_whitespace():
    payload = bytes(range(64))
    wrapped = "\n".join(base64.b64encode(payload).decode().replace("=", "=\n ") for _ in [0])
    with tempfile.TemporaryDirectory() as td:
        proc, blob = run(["prepare"], {
            "ANDROID_KEYSTORE_B64": f"  {wrapped}  ",
            "ANDROID_KEYSTORE_ALIAS": "k",
            "ANDROID_KEYSTORE_PASS": "p",
        }, td)
        # 解出来了，但无 keytool / 假 keystore 会在校验环节失败——关键是失败原因不是 base64
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "base64" not in proc.stderr, proc.stderr


@check("verify 在未签名时跳过而非失败")
def t_verify_skips_when_unsigned():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run(["verify", "--apk-dir", td], {"ANDROID_SIGNED": "false"}, td)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "跳过" in proc.stdout, proc.stdout


@check("verify 声明已签名却缺环境变量：失败")
def t_verify_missing_env():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "a.apk").write_bytes(b"x")
        proc, _ = run(["verify", "--apk-dir", td], {"ANDROID_SIGNED": "true"}, td)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "签名环境变量不完整" in proc.stderr, proc.stderr


@check("verify 目录内一个 APK 都没有：失败（构建真没产出安装包）")
def t_verify_no_apk_at_all():
    with tempfile.TemporaryDirectory() as td:
        proc, _ = run(["verify", "--apk-dir", td], {
            "ANDROID_SIGNED": "true",
            "GODOT_ANDROID_SIGN_KEYSTORE": str(Path(td, "k.keystore")),
            "GODOT_ANDROID_KEYSTORE_ALIAS": "k",
            "GODOT_ANDROID_SIGN_PASSWORD": "p",
        }, td)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "没有任何 APK" in proc.stderr, proc.stderr


@check("verify 只有 debug 档：正常通过并说明签名未被使用")
def t_verify_only_debug_passes():
    """release 档能否产出由上游 SCons 的 store_release 决定。

    没开 store_release 时 .so 只落 libs/tools/debug，gradle 只编 debug 档——这是
    引擎的正常行为，不是缺陷。配了 keystore 却只有 debug 产物时如实说明「签名未
    被使用」并放行，不是报错。线上实测过这个形态（6 个产物全是 -debug）。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "android_editor-android-debug.apk").write_bytes(b"d")
        sub = root / "horizonos"
        sub.mkdir()
        (sub / "android_editor-horizonos-debug.apk").write_bytes(b"d")
        proc, _ = run(["verify", "--apk-dir", td], {
            "ANDROID_SIGNED": "true",
            "GODOT_ANDROID_SIGN_KEYSTORE": str(Path(td, "k.keystore")),
            "GODOT_ANDROID_KEYSTORE_ALIAS": "k",
            "GODOT_ANDROID_SIGN_PASSWORD": "p",
        }, td)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "未产出 release 档" in proc.stdout, proc.stdout
        assert "store_release" in proc.stdout, proc.stdout
        # 必须点名两个 debug 产物，让人看清这次真正产出了什么
        assert "android_editor-android-debug.apk" in proc.stdout, proc.stdout
        assert "android_editor-horizonos-debug.apk" in proc.stdout, proc.stdout


def run_with_fake_keytool(args, extra_env, cwd, certreq_ok):
    """跑 prepare，PATH 里放一个假 keytool（-certreq 按 certreq_ok 决定成败）。

    端到端而非打桩：打桩测的是「函数被调用时行为对不对」，抓不到「调用点被删掉」——
    那正是要防的退化。假 keytool 让真实进程跑完整条 prepare 通路。
    """
    fd, env_file = tempfile.mkstemp()
    os.close(fd)
    bin_dir = Path(cwd, "_fakebin")
    bin_dir.mkdir(exist_ok=True)
    # 用 python 脚本冒充 keytool：-list 恒成功、-certreq 由参数决定。
    # 同时写无扩展名与 .bat 两份：Linux runner 认前者，Windows 上 shutil.which
    # 只按 PATHEXT 找，无扩展名的文件不命中。
    body = (
        "import sys\n"
        "if '-certreq' in sys.argv:\n"
        f"    ok = {certreq_ok!r}\n"
        "    if not ok:\n"
        "        sys.stderr.write('keytool error: java.io.IOException: keystore password was incorrect\\n')\n"
        "        sys.exit(1)\n"
        "    sys.exit(0)\n"
        "if '-list' in sys.argv:\n"
        "    print('SHA256: AB:CD:EF')\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n")
    fake = bin_dir / "keytool"
    fake.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    fake.chmod(0o755)
    # Windows：.bat 包一层 python，内容与无扩展名版相同。
    # **不造假 .exe**：shutil.which 在 Windows 上按 PATHEXT 优先命中 .exe，
    # 假 exe 会被当真正可执行文件启动并以 WinError 216 失败，测试就测不到被测逻辑。
    py = bin_dir / "keytool.py"
    py.write_text(body, encoding="utf-8")
    wrapper = bin_dir / "keytool.bat"
    wrapper.write_text(f'@echo off\r\n"{sys.executable}" "%~dp0keytool.py" %*\r\n',
                       encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if not k.startswith("ANDROID_KEYSTORE_")}
    env.update({"GITHUB_ENV": env_file, "PYTHONIOENCODING": "utf-8"})
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env.update(extra_env)
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)] + args,
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd)
        return proc, Path(env_file).read_text(encoding="utf-8")
    finally:
        os.unlink(env_file)


def keystore_env():
    return {
        "ANDROID_KEYSTORE_B64": base64.b64encode(b"fake-jks-bytes").decode(),
        "ANDROID_KEYSTORE_ALIAS": "jaywan",
        "ANDROID_KEYSTORE_PASS": "testpass",
    }


@check("key 密码与 store 密码不一致：prepare 失败并点名两者须相同")
def t_key_password_differs():
    """Godot 用一个值同时填 storePassword / keyPassword，故两者必须相同。

    `keytool -list` 只验 store 密码，验不出 key 密码不同；这正是「指纹拿到了、
    但 gradle 签不出来」的盲区，故 prepare 必须再验一次 key 密码。
    """
    with tempfile.TemporaryDirectory() as td:
        proc, blob = run_with_fake_keytool(
            ["prepare"], keystore_env(), td, certreq_ok=False)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "不一致" in proc.stderr, proc.stderr
        assert "keyPassword" in proc.stderr or "key 密码" in proc.stderr, proc.stderr
        # 失败时不得声称已就绪
        assert env_pairs(blob).get("ANDROID_SIGNED") != "true", blob


@check("key 密码正确：prepare 成功并写出 gradle 变量")
def t_key_password_ok():
    with tempfile.TemporaryDirectory() as td:
        proc, blob = run_with_fake_keytool(
            ["prepare"], keystore_env(), td, certreq_ok=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        pairs = env_pairs(blob)
        assert pairs.get("ANDROID_SIGNED") == "true", pairs
        assert pairs.get("GODOT_ANDROID_KEYSTORE_ALIAS") == "jaywan", pairs
        assert pairs.get("GODOT_ANDROID_SIGN_PASSWORD") == "testpass", pairs


@check("apksigner 输出解析：只取 SHA-256 那一行，指纹恰为 64 字符")
def t_parse_apksigner_output():
    """真实 apksigner 输出每行形如「Signer #1 certificate SHA-256 digest: ab:cd:…」。

    只断言结果形状：只取到 1 个指纹、长度 64、不含同族 SHA-1 / MD5 行的尾巴。
    不断言「错误切法为什么错」——那是推测，不是这里能证明的事。
    """
    import android_keystore as ak

    sample = (
        "Verifies\n"
        "Verified using v2 scheme (APK Signature Scheme v2): true\n"
        "Signer #1 certificate DN: CN=JayWan\n"
        "Signer #1 certificate SHA-256 digest: "
        "ce:fc:ae:a2:56:de:63:2f:06:e2:32:7f:39:fa:0f:66:"
        "db:d0:bf:a6:fb:1a:84:46:47:13:c2:c4:78:51:66:ef\n"
        "Signer #1 certificate SHA-1 digest: 25:01:e4:e0:80:7f\n"
        "Signer #1 certificate MD5 digest: ab:cd:ef\n")

    def fake_run(command, timeout=120):
        return 0, sample, ""

    original_run, original_which = ak.run_tool, ak.shutil.which
    ak.run_tool = fake_run
    ak.shutil.which = lambda name: "/usr/bin/apksigner" if name == "apksigner" else None
    try:
        with tempfile.TemporaryDirectory() as td:
            apk = Path(td, "a.apk")
            apk.write_bytes(b"x")
            digests, problem = ak.apk_signer_fingerprints(apk)
    finally:
        ak.run_tool, ak.shutil.which = original_run, original_which

    assert problem == "", problem
    assert len(digests) == 1, digests
    # 恰好 64 字符，且不含 SHA-1 的尾巴
    assert len(digests[0]) == 64, f"指纹长度 {len(digests[0])}：{digests[0]}"
    assert digests[0] == (
        "cefcaea256de632f06e2327f39fa0f66dbd0bfa6fb1a84464713c2c4785166ef"), digests[0]
    assert "2501e4e0807f" not in digests[0], digests[0]


@check("apksigner 指纹长度异常：失败并报因，不拿错值去比对")
def t_parse_apksigner_bad_length():
    import android_keystore as ak

    sample = "Signer #1 certificate SHA-256 digest: ab:cd\n"
    original_run, original_which = ak.run_tool, ak.shutil.which
    ak.run_tool = lambda command, timeout=120: (0, sample, "")
    ak.shutil.which = lambda name: "/usr/bin/apksigner" if name == "apksigner" else None
    try:
        with tempfile.TemporaryDirectory() as td:
            apk = Path(td, "a.apk")
            apk.write_bytes(b"x")
            digests, problem = ak.apk_signer_fingerprints(apk)
    finally:
        ak.run_tool, ak.shutil.which = original_run, original_which

    assert digests == [], digests
    assert "长度异常" in problem, problem


@check("is_debug_apk 只认 -debug 结尾，release 档不误判")
def t_is_debug_apk():
    """debug 档恒用 SDK 的 debug 证书签名（editor/build.gradle 的 buildTypes.debug），
    与配置的 keystore 无关，故校验时要挑出来排除。
    """
    from android_keystore import is_debug_apk

    assert is_debug_apk(Path("android_editor-android-debug.apk")) is True
    assert is_debug_apk(Path("android_editor-horizonos-debug.apk")) is True
    assert is_debug_apk(Path("android_editor-android-release.apk")) is False
    assert is_debug_apk(Path("android_editor-picoos-release.apk")) is False
    # 「debug」出现在中间不算 debug 档（避免子串误判）
    assert is_debug_apk(Path("android_editor-debuggable-release.apk")) is False


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
