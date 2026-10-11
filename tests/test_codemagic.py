"""The Codemagic builds of the Apple apps (codemagic.yaml, docs/release/codemagic.md): the
workflow file, the scripts it runs, the Mach-O lister that orders Developer ID signing, the
Mac entitlements and the self-test's PyObjC callback check.

None of this can run here (it needs a Mac, Codemagic and Apple's services); these tests
pin down the parts that can go wrong without one: structure, names, secrets, shell syntax,
signing order and the decisions documented in the guide."""

from __future__ import annotations

import importlib.util
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CODEMAGIC = REPO / "codemagic.yaml"
GUIDE = REPO / "docs" / "release" / "codemagic.md"
MACOS = REPO / "packaging" / "macos"
SCRIPTS = [
    MACOS / "build.sh",
    MACOS / "release.sh",
    MACOS / "developer-id.sh",
    REPO / "ios" / "scripts" / "install-xcodegen.sh",
    REPO / "ios" / "scripts" / "next-build-number.sh",
    REPO / "ios" / "scripts" / "run-on-iphone.sh",
]
SECRET_NAMES = (
    "APP_STORE_CONNECT_ISSUER_ID",
    "APP_STORE_CONNECT_KEY_IDENTIFIER",
    "APP_STORE_CONNECT_PRIVATE_KEY",
    "CERTIFICATE_PRIVATE_KEY",
)


def load_macho():
    if "mac_macho" in sys.modules:
        return sys.modules["mac_macho"]
    spec = importlib.util.spec_from_file_location("mac_macho", MACOS / "macho.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mac_macho"] = module  # dataclasses look their module up while defining
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def config():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(CODEMAGIC.read_text("utf-8"))


# ------------------------------------------------------------------ codemagic.yaml


def test_workflows_run_on_the_free_mac(config):
    flows = config["workflows"]
    assert set(flows) == {"ios-testflight", "mac-signing-request", "mac-release"}
    for name, flow in flows.items():
        assert flow["instance_type"] == "mac_mini_m2", name  # the free tier's machine
        assert 0 < flow["max_build_duration"] <= 60, name
        env = flow["environment"]
        assert env["xcode"] == "26.5", name  # a string; the 26.6 pool queues for hours
        assert env["groups"] == ["app_store_credentials"], name
        assert flow["scripts"][0]["name"] == "Check the App Store Connect settings"


def test_release_tags_trigger_both_apps_and_nothing_else_is_automatic(config):
    flows = config["workflows"]
    for name in ("ios-testflight", "mac-release"):
        trigger = flows[name]["triggering"]
        assert trigger["events"] == ["tag"]
        assert trigger["tag_patterns"] == [{"pattern": "release-*", "include": True}]
    assert "triggering" not in flows["mac-signing-request"]  # by hand only


def test_ios_workflow(config):
    flow = config["workflows"]["ios-testflight"]
    assert flow["environment"]["vars"]["BUNDLE_ID"] == "fyi.shoutabout.app"
    text = "\n".join(step["script"] for step in flow["scripts"])
    assert "bash ios/scripts/install-xcodegen.sh" in text and "generate" in text
    assert "keychain initialize" in text and "keychain add-certificates" in text
    assert 'fetch-signing-files "$BUNDLE_ID" --type IOS_APP_STORE --create' in text
    assert "--strict-match-identifier" not in text  # the widgets' bundle ID must match too
    assert "xcode-project use-profiles" in text
    assert "next-build-number.sh" in text and "CM_ENV" in text
    assert "CURRENT_PROJECT_VERSION=$IOS_BUILD_NUMBER" in text
    assert "--config Release" in text
    assert "build/ios/ipa/*.ipa" in flow["artifacts"]
    publish = flow["publishing"]["app_store_connect"]
    assert publish["api_key"] == "$APP_STORE_CONNECT_PRIVATE_KEY"
    assert publish["key_id"] == "$APP_STORE_CONNECT_KEY_IDENTIFIER"
    assert publish["issuer_id"] == "$APP_STORE_CONNECT_ISSUER_ID"
    # Internal testers need no beta review; submit_to_testflight would start one.
    assert publish["submit_to_testflight"] is False


def test_mac_workflows(config):
    release = config["workflows"]["mac-release"]
    steps = [step["script"] for step in release["scripts"][1:]]
    assert steps == [
        "bash packaging/macos/developer-id.sh install",  # fails fast without a certificate
        "bash packaging/macos/build.sh --ci",
        "bash packaging/macos/release.sh",
    ]
    assert release["artifacts"] == [
        "packaging/dist-mac/release/ShoutAbout.dmg",
        "packaging/dist-mac/release/ShoutAbout-mac.zip",
        "packaging/dist-mac/release/ShoutAbout-mac-logs.zip",
    ]
    request = config["workflows"]["mac-signing-request"]
    assert request["scripts"][1]["script"] == "bash packaging/macos/developer-id.sh request"
    assert request["artifacts"] == ["packaging/dist-mac/signing-request/*"]


def test_no_secrets_and_no_cache():
    text = CODEMAGIC.read_text("utf-8")
    assert "PRIVATE KEY-----" not in text
    assert not re.search(r"[A-Za-z0-9+/]{60,}", text)  # no pasted key material
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", text)  # no issuer ID
    assert "\ncache:" not in text and "    cache:" not in text  # see the comment in the file
    for name in SECRET_NAMES:
        assert name in text  # documented where they come from


def test_referenced_scripts_exist(config):
    for flow in config["workflows"].values():
        for step in flow["scripts"]:
            for path in re.findall(r"bash ((?:ios|packaging)/\S+\.sh)", step["script"]):
                assert (REPO / path).is_file(), path


# ------------------------------------------------------------------ shell scripts


def _usable_bash() -> str | None:
    bash = shutil.which("bash")
    if bash is None:
        return None
    if os.name == "nt" and "system32" in bash.lower():
        return None  # WSL's launcher, maybe without a distribution
    return bash


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_parse(path):
    text = path.read_text("utf-8")
    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    bash = _usable_bash()
    if bash is None:
        pytest.skip("no bash here")
    result = subprocess.run(
        [bash, "-n"], input=text.encode("utf-8"), capture_output=True, timeout=60
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_xcodegen_pin_matches_run_on_iphone():
    def pins(path):
        text = path.read_text("utf-8")
        return (
            re.search(r'XCODEGEN_VERSION="([^"]+)"', text).group(1),
            re.search(r'XCODEGEN_SHA256="([0-9a-f]{64})"', text).group(1),
        )

    scripts = REPO / "ios" / "scripts"
    assert pins(scripts / "install-xcodegen.sh") == pins(scripts / "run-on-iphone.sh")


def test_build_script_ci_mode():
    text = (MACOS / "build.sh").read_text("utf-8")
    assert "--ci) CI_MODE=1; INSTALL_APP=0; OPEN_APP=0 ;;" in text
    ci = text.index('if [ "$CI_MODE" = 1 ]; then')
    # CI mode stops after PyInstaller and the bundle rules: before the ad-hoc re-signing,
    # the self-test, the packing, the install and `open`.
    assert text.index("mac_rules.py check") < ci < text.index("codesign --force --deep --sign -")
    assert ci < text.index("--self-test") < text.index('open "$DEST')
    header_end = text.splitlines().index("set -euo pipefail")  # = its last line number
    assert f"sed -n '2,{header_end}p'" in text  # --help prints the whole header


def test_release_script():
    text = (MACOS / "release.sh").read_text("utf-8")
    header_end = text.splitlines().index("set -euo pipefail")
    assert f"sed -n '2,{header_end}p'" in text
    assert "--deep --sign" not in text  # Apple: never sign with --deep
    assert "--options runtime --entitlements" in text  # the app: Hardened Runtime
    assert 'xargs -0 -n 16 -P 4 codesign --force --timestamp --sign "$IDENTITY"' in text
    assert "xcrun notarytool submit" in text and "--wait" in text
    assert '"Accepted"' in text and "notarytool log" in text
    assert len(re.findall(r'^\s*staple "\$', text, re.M)) == 2  # the app, then the dmg
    assert "mac_rules.py check" in text
    assert "allow-unsigned-executable-memory" in text  # the documented fallback only
    # The notary key is written to a private temporary file and never echoed.
    assert "umask 077" in text and 'echo "$APP_STORE_CONNECT_PRIVATE_KEY"' not in text
    assert 'SIGNING_CHECKS="modules dependencies vad ctranslate2 objc-callbacks"' in text


def test_developer_id_script():
    text = (MACOS / "developer-id.sh").read_text("utf-8")
    assert "--profile-type MAC_APP_DIRECT" in text  # DEVELOPER_ID_APPLICATION and its G2
    assert '--certificate-key "@file:$KEY" --save' in text
    assert "count_p12" in text  # the JSON lists every team certificate; the .p12s match ours
    assert "certificates create" not in text  # Apple: only the Account Holder, on the web
    assert "openssl req -new -sha256" in text
    assert "MAC_SIGN_IDENTITY=" in text and "CM_ENV" in text
    assert 'echo "$CERTIFICATE_PRIVATE_KEY"' not in text


def test_next_build_number_script():
    text = (REPO / "ios" / "scripts" / "next-build-number.sh").read_text("utf-8")
    assert "get-latest-testflight-build-number" in text
    assert "apps list --bundle-id-identifier" in text
    assert "echo $(( ${latest:-0} + 1 ))" in text


FAKE_CLI = """#!/bin/bash
case "$1 $2" in
  "--version "*) echo "app-store-connect ${FAKE_CLI_VERSION:-0.70.0}" ;;
  "certificates list")
    echo "$*" >> "$FAKE_LOG"
    while [ $# -gt 0 ]; do
      [ "$1" = "--certificates-dir" ] && dir="$2"
      shift
    done
    mkdir -p "$dir"
    if [ -n "${FAKE_HAVE_CERT:-}" ]; then echo p12 > "$dir/DEVELOPER_ID_APPLICATION_G2_X.p12"; fi
    echo '[{"id": "OTHER", "attributes": {"certificateType": "DEVELOPER_ID_APPLICATION_G2"}}]'
    ;;
  "apps list")
    if [ -n "${FAKE_NO_APP:-}" ]; then echo "[]"; exit 0; fi
    echo '[{"id": "1", "attributes": {"bundleId": "fyi.shoutabout.app.widgets"}},'
    echo ' {"id": "6741234567", "type": "apps", "attributes": {"bundleId": "fyi.shoutabout.app"}}]'
    ;;
  "get-latest-testflight-build-number 6741234567")
    echo "Found latest build" >&2
    printf '%s' "${FAKE_LATEST-41}"
    ;;
  *) echo "unexpected call: $*" >&2; exit 9 ;;
esac
"""


@pytest.fixture
def fake_cli(tmp_path):
    """A bin folder with a stand-in `app-store-connect` and a `python3` that is this
    interpreter (except `python3 -m pip`, which only records the call), put first on PATH
    for the scripts under test."""
    if _usable_bash() is None:
        pytest.skip("no bash here")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "app-store-connect").write_bytes(FAKE_CLI.encode())
    python = Path(sys.executable).as_posix()
    (bindir / "python3").write_bytes(
        (
            "#!/bin/bash\n"
            'if [ "$1 $2" = "-m pip" ]; then echo "python3 $*" >> "$FAKE_LOG"; exit 0; fi\n'
            f'exec "{python}" "$@"\n'
        ).encode()
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    return dict(
        os.environ,
        PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        FAKE_LOG=str(tmp_path / "cli.log"),
    )


@pytest.mark.parametrize(
    ("latest", "expected"), [("41", "42"), ("", "1"), ("12.1", "13"), ("7\n", "8")]
)
def test_next_build_number_dry_run(fake_cli, latest, expected):
    script = (REPO / "ios" / "scripts" / "next-build-number.sh").read_text("utf-8")
    env = dict(fake_cli, FAKE_LATEST=latest)
    result = subprocess.run(
        [_usable_bash(), "-s", "fyi.shoutabout.app"],
        input=script.encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode().strip() == expected  # only the number on stdout
    assert "6741234567" in result.stderr.decode()  # found by bundle ID, not the widgets'


def test_next_build_number_without_an_app_record(fake_cli):
    script = (REPO / "ios" / "scripts" / "next-build-number.sh").read_text("utf-8")
    result = subprocess.run(
        [_usable_bash(), "-s", "fyi.shoutabout.app"],
        input=script.encode(),
        capture_output=True,
        env=dict(fake_cli, FAKE_NO_APP="1"),
        timeout=60,
    )
    assert result.returncode == 1 and result.stdout == b""
    assert "New App" in result.stderr.decode()


@pytest.fixture
def signing_env(fake_cli, tmp_path):
    """developer-id.sh copied into a scratch repo layout, the four variables (a fresh RSA
    key in the PKCS#1 PEM that `ssh-keygen -m PEM` writes; dummy API values) and the fake
    CLI. Returns (script path, env, repo root)."""
    if shutil.which("openssl", path=fake_cli["PATH"]) is None:
        pytest.skip("no openssl here")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    assert pem.startswith("-----BEGIN RSA PRIVATE KEY-----")
    root = tmp_path / "repo"
    script = root / "packaging" / "macos" / "developer-id.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes((MACOS / "developer-id.sh").read_text("utf-8").encode())
    env = dict(
        fake_cli,
        APP_STORE_CONNECT_ISSUER_ID="00000000-test",
        APP_STORE_CONNECT_KEY_IDENTIFIER="TESTKEY000",
        APP_STORE_CONNECT_PRIVATE_KEY="unused here",  # only release.sh's notarytool reads it
        CERTIFICATE_PRIVATE_KEY=pem.replace("\n", "\r\n"),  # pasted from Windows
        # Git Bash on Windows converts arguments that look like POSIX paths for native
        # programs such as its openssl.exe: fine for -key, wrong for -subj "/CN=...".
        MSYS2_ARG_CONV_EXCL="/CN=",
    )
    return script, env, root


def test_signing_request_dry_run(signing_env, tmp_path):
    script, env, root = signing_env
    result = subprocess.run(
        [_usable_bash(), str(script), "request"], capture_output=True, env=env, timeout=120
    )
    output = result.stdout.decode() + result.stderr.decode()
    assert result.returncode == 0, output
    out = root / "packaging" / "dist-mac" / "signing-request"
    csr = (out / "ShoutAbout-DeveloperID.certSigningRequest").read_text()
    assert csr.startswith("-----BEGIN CERTIFICATE REQUEST-----")
    assert "Developer ID Application" in (out / "README.txt").read_text()
    assert "PRIVATE KEY" not in output and "PRIVATE KEY" not in csr  # never the secret
    calls = (tmp_path / "cli.log").read_text()
    assert "--profile-type MAC_APP_DIRECT" in calls and "--save" in calls
    assert "pip" not in calls and "Codemagic CLI tools 0.70.0" in output  # new enough


def test_signing_request_upgrades_old_cli_tools(signing_env, tmp_path):
    script, env, _root = signing_env
    result = subprocess.run(
        [_usable_bash(), str(script), "request"],
        capture_output=True,
        env=dict(env, FAKE_CLI_VERSION="0.60.0"),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert "predate 0.68.0: upgrading" in result.stdout.decode()
    calls = (tmp_path / "cli.log").read_text()
    assert "python3 -m pip install --quiet --upgrade codemagic-cli-tools>=0.68.0" in calls


def test_signing_request_when_the_certificate_exists(signing_env):
    script, env, root = signing_env
    result = subprocess.run(
        [_usable_bash(), str(script), "request"],
        capture_output=True,
        env=dict(env, FAKE_HAVE_CERT="1"),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    out = root / "packaging" / "dist-mac" / "signing-request"
    assert not (out / "ShoutAbout-DeveloperID.certSigningRequest").exists()
    assert "Nothing to do" in (out / "README.txt").read_text()


def test_signing_request_needs_the_four_variables(signing_env):
    script, env, _root = signing_env
    env = {k: v for k, v in env.items() if k != "CERTIFICATE_PRIVATE_KEY"}
    result = subprocess.run(
        [_usable_bash(), str(script), "request"], capture_output=True, env=env, timeout=60
    )
    assert result.returncode == 1
    assert (
        "missing in the Codemagic environment group app_store_credentials: "
        "CERTIFICATE_PRIVATE_KEY" in result.stderr.decode()
    )


def _heredoc(text: str, opener: str) -> str:
    """The Python program of the heredoc that starts on the line containing ``opener``."""
    start = text.index(opener)
    body_start = text.index("\n", start) + 1
    return text[body_start : text.index("\nEOF\n", body_start) + 1]


def test_release_selftest_comparison_dry_run(tmp_path):
    """release.sh's `regressions`: a watched check that passed unsigned and fails signed."""
    program = _heredoc(
        (MACOS / "release.sh").read_text("utf-8"),
        '"$PY" - "$1" "$2" "$SIGNING_CHECKS" <<\'EOF\'',
    )
    watched = "modules dependencies vad ctranslate2 objc-callbacks"

    def report(name, **statuses):
        path = tmp_path / f"{name}.json"
        checks = [{"name": k.replace("_", "-"), "status": v} for k, v in statuses.items()]
        path.write_text(__import__("json").dumps({"ok": True, "checks": checks}))
        return path

    def run(before, after):
        out = subprocess.run(
            [sys.executable, "-c", program, str(before), str(after), watched],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    unsigned = report("u", modules="pass", vad="pass", objc_callbacks="pass", tk="fail")
    same = report("s", modules="pass", vad="pass", objc_callbacks="pass", tk="fail")
    broken = report("b", modules="pass", vad="fail", objc_callbacks="fail", tk="pass")
    assert run(unsigned, same) == ""  # nothing that worked before broke
    assert run(unsigned, broken) == "vad objc-callbacks"
    assert run(unsigned, tmp_path / "missing.json") == "crashed"
    assert run(tmp_path / "missing.json", same) == "unknown"
    # A check that already failed unsigned (tk here) is never blamed on signing.
    assert run(broken, unsigned) == ""


def test_release_notary_output_parsing_dry_run(tmp_path):
    """release.sh's `notary_field`: notarytool may print more than one JSON object."""
    program = _heredoc(
        (MACOS / "release.sh").read_text("utf-8"), '"$PY" - "$1" "$2" <<\'EOF\' 2>/dev/null'
    )
    out = tmp_path / "notary.json"
    out.write_text(
        'Conducting pre-submission checks...\n{"id": "2efe2717-0000", "path": "x.zip"}\n'
        '{"id": "2efe2717-0000", "status": "Invalid", "message": "Processing complete"}\n'
    )

    def field(name):
        return subprocess.run(
            [sys.executable, "-c", program, str(out), name],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()

    assert field("id") == "2efe2717-0000"
    assert field("status") == "Invalid"
    out.write_text("Error: HTTP status code: 401. Unable to authenticate.\n")
    assert field("status") == ""  # no answer: release.sh fails with the text above


def test_dist_folder_is_ignored():
    assert "packaging/dist-mac/" in (REPO / ".gitignore").read_text("utf-8")


# ------------------------------------------------------------------ entitlements


def test_entitlements_are_the_justified_minimum():
    raw = (MACOS / "ShoutAbout.entitlements").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # Apple: no byte-order mark
    assert b"<!--" not in raw  # Apple: no comments
    data = plistlib.loads(raw)
    assert data == {
        "com.apple.security.device.audio-input": True,  # the microphone
        "com.apple.security.cs.allow-jit": True,  # PyObjC's callback stubs
    }
    # Never the broad exceptions, and nothing that notarization rejects.
    for key in (
        "com.apple.security.cs.disable-library-validation",
        "com.apple.security.cs.allow-unsigned-executable-memory",
        "com.apple.security.cs.disable-executable-page-protection",
        "com.apple.security.get-task-allow",
        "com.apple.security.app-sandbox",
    ):
        assert key not in data


def test_guide_justifies_each_entitlement():
    text = GUIDE.read_text("utf-8")
    for key in (
        "com.apple.security.device.audio-input",
        "com.apple.security.cs.allow-jit",
        "com.apple.security.cs.allow-unsigned-executable-memory",
        "com.apple.security.cs.disable-library-validation",
    ):
        assert key in text
    for name in SECRET_NAMES:
        assert name in text
    assert "## Sources" in text and "## Troubleshooting" in text


# ------------------------------------------------------------------ Mach-O lister


def thin(filetype: int) -> bytes:
    # mach_header_64, little endian: magic, cputype (arm64), cpusubtype, filetype, ...
    return struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, filetype, 0, 0, 0, 0) + b"\0" * 64


def fat(filetype: int) -> bytes:
    header = struct.pack(">II", 0xCAFEBABE, 2)
    arch = struct.pack(">iiIII", 0x0100000C, 0, 4096, 96, 12)
    arch2 = struct.pack(">iiIII", 0x01000007, 3, 8192, 96, 12)
    body = header + arch + arch2
    return body + b"\0" * (4096 - len(body)) + thin(filetype) + b"\0" * 4000 + thin(filetype)


def make_app(root: Path) -> Path:
    app = root / "ShoutAbout.app"
    contents = app / "Contents"
    for d in (
        "MacOS",
        "Frameworks/numpy/__dot__dylibs",
        "Resources",
        "Frameworks/Foo.framework/Versions/A",
    ):
        (contents / d).mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "ShoutAbout"}))
    (contents / "MacOS" / "ShoutAbout").write_bytes(thin(0x2))
    (contents / "MacOS" / "helper").write_bytes(thin(0x2))
    (contents / "Frameworks" / "libpython3.12.dylib").write_bytes(thin(0x6))
    (contents / "Frameworks" / "_ssl.cpython-312-darwin.so").write_bytes(thin(0x8))
    (contents / "Frameworks" / "numpy" / "__dot__dylibs" / "libopenblas.dylib").write_bytes(
        fat(0x6)
    )
    (contents / "Frameworks" / "Foo.framework" / "Versions" / "A" / "Foo").write_bytes(thin(0x6))
    (contents / "Frameworks" / "thing.o").write_bytes(thin(0x1))  # MH_OBJECT: never signed
    (contents / "Resources" / "Notes.class").write_bytes(struct.pack(">II", 0xCAFEBABE, 52))  # Java
    (contents / "Resources" / "base_library.zip").write_bytes(b"PK\3\4" + b"\0" * 40)
    (contents / "Resources" / "tiny").write_bytes(b"\xcf\xfa")  # truncated, not code
    if hasattr(os, "symlink"):
        try:
            os.symlink("../Frameworks/libpython3.12.dylib", contents / "Resources" / "link.dylib")
        except OSError:
            pass  # no symlink rights on this Windows account: the skip is untested here
    return app


def test_macho_filetypes(tmp_path):
    macho = load_macho()
    cases = {
        "exe": (thin(0x2), 0x2),
        "dylib": (thin(0x6), 0x6),
        "bundle": (thin(0x8), 0x8),
        "fat": (fat(0x6), 0x6),
        "java": (struct.pack(">II", 0xCAFEBABE, 52) + b"\0" * 32, None),
        "text": (b"#!/bin/sh\necho hi\n", None),
        "empty": (b"", None),
        "big-endian": (struct.pack(">IiiI", 0xFEEDFACF, 7, 3, 0x6) + b"\0" * 16, 0x6),
    }
    for name, (data, expected) in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        assert macho.macho_filetype(path) == expected, name


def test_macho_signing_order(tmp_path, capsys):
    macho = load_macho()
    app = make_app(tmp_path)
    warnings: list[str] = []
    items = macho.signing_order(list(macho.walk(app, warn=warnings.append)))
    rel = [(i.kind, i.path.relative_to(app).as_posix()) for i in items]
    assert rel == [
        ("lib", "Contents/Frameworks/Foo.framework/Versions/A/Foo"),
        ("lib", "Contents/Frameworks/numpy/__dot__dylibs/libopenblas.dylib"),
        ("lib", "Contents/Frameworks/_ssl.cpython-312-darwin.so"),
        ("lib", "Contents/Frameworks/libpython3.12.dylib"),
        ("exe", "Contents/MacOS/helper"),
        ("bundle", "Contents/Frameworks/Foo.framework"),
    ]  # no main executable, no object file, no Java class, no symlink, no data
    assert len(warnings) == 1 and "thing.o" in warnings[0]

    assert macho.main(["list", "--kind", "lib", "-0", str(app)]) == 0
    out = capsys.readouterr()
    paths = [p for p in out.out.split("\0") if p]
    assert len(paths) == 4 and all(Path(p).is_file() for p in paths)
    assert "4 code items (4 libraries" in out.err

    assert macho.main(["list", str(app)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split("\t")[0] == "lib" and lines[-1].startswith("bundle\t")


def test_macho_rejects_a_non_bundle(tmp_path, capsys):
    macho = load_macho()
    assert macho.main(["list", str(tmp_path)]) == 2
    assert "not an app bundle" in capsys.readouterr().err


def test_macho_main_executable_name(tmp_path):
    macho = load_macho()
    app = tmp_path / "Other.app"
    (app / "Contents").mkdir(parents=True)
    assert macho.main_executable(app) == app / "Contents" / "MacOS" / "Other"  # no Info.plist
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "X"}))
    assert macho.main_executable(app) == app / "Contents" / "MacOS" / "X"


# ------------------------------------------------------------------ self-test check


def test_objc_callbacks_check_skips_off_macos(tmp_path, monkeypatch):
    from shoutabout import selftest

    monkeypatch.setattr(selftest.sys, "platform", "win32")
    result = selftest.run_check(
        next(c for c in selftest.DEFAULT_CHECKS if c.name == "objc-callbacks"),
        selftest.Context(home=tmp_path, work=tmp_path, frozen=True),
    )
    assert result.status == "skip"


class _FakeNSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def performSelector_(self, name):
        return getattr(self, name)()


class _FakeNSArray(list):
    @classmethod
    def arrayWithArray_(cls, items):
        return cls(items)

    def sortedArrayUsingFunction_context_(self, fn, context):
        import functools

        return sorted(self, key=functools.cmp_to_key(lambda a, b: fn(a, b, context)))


def _fake_pyobjc(monkeypatch, registered=None):
    class NoSuchClass(Exception):
        pass

    def look_up(name):
        if registered is None:
            raise NoSuchClass(name)
        return registered

    objc = types.ModuleType("objc")
    objc.lookUpClass = look_up
    objc.nosuchclass_error = NoSuchClass
    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _FakeNSObject
    foundation.NSArray = _FakeNSArray
    monkeypatch.setitem(sys.modules, "objc", objc)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)


def test_objc_callbacks_check_with_stand_in_pyobjc(tmp_path, monkeypatch):
    from shoutabout import selftest

    monkeypatch.setattr(selftest.sys, "platform", "darwin")
    _fake_pyobjc(monkeypatch)
    ctx = selftest.Context(home=tmp_path, work=tmp_path, frozen=True)
    assert "call back into Python" in selftest.check_objc_callbacks(ctx)


def test_objc_callbacks_check_reuses_the_registered_class(tmp_path, monkeypatch):
    from shoutabout import selftest

    class Registered(_FakeNSObject):
        def answer(self):
            return 41  # a wrong answer must fail the check

    monkeypatch.setattr(selftest.sys, "platform", "darwin")
    _fake_pyobjc(monkeypatch, registered=Registered)
    ctx = selftest.Context(home=tmp_path, work=tmp_path, frozen=True)
    with pytest.raises(RuntimeError, match="returned 41"):
        selftest.check_objc_callbacks(ctx)


def test_objc_class_name_matches_the_constant():
    from shoutabout import selftest

    source = Path(selftest.__file__).read_text("utf-8")
    assert f"class {selftest.OBJC_SELFTEST_CLASS}(NSObject)" in source
