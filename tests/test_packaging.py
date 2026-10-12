"""Static checks of the Windows installer sources in packaging/ (never builds or installs).

- make_assets.py renders a valid multi-size .ico and the wizard BMPs;
- notices.py collects license texts from installed metadata;
- installer.iss keeps the contract the app and docs rely on (AppId, per-machine install,
  Private-only firewall rule for ShoutAbout.exe, the HKCU Run value name, user data kept by
  default);
- the PowerShell scripts are ASCII and parse (Windows PowerShell 5.1 reads BOM-less files as
  ANSI), and the CI workflow builds, smoke-tests and uploads the installer.
"""

from __future__ import annotations

import ast
import base64
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from shoutabout import frozen

REPO = Path(__file__).resolve().parents[1]
PACKAGING = REPO / "packaging"
ISS = PACKAGING / "installer.iss"
WORKFLOW = REPO / ".github" / "workflows" / "windows-installer.yml"
PS_FILES = sorted(PACKAGING.glob("*.ps1"))
POWERSHELL = (
    (shutil.which("powershell.exe") or shutil.which("powershell"))
    if (sys.platform == "win32")
    else None
)

sys.path.insert(0, str(PACKAGING))
import bundle_rules  # noqa: E402
import make_assets  # noqa: E402
import notices  # noqa: E402

# ------------------------------------------------------------------------ assets


def _ico_entries(data: bytes) -> list[tuple[int, int, int]]:
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    out = []
    for i in range(count):
        w, h, _c, _r, planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        assert planes == 1 and bpp == 32 and offset + size <= len(data)
        out.append((w or 256, h or 256, size))
    return out


def test_ico_has_every_size():
    entries = _ico_entries(make_assets.ico_bytes())
    assert [w for w, _h, _s in entries] == list(make_assets.ICON_SIZES)
    assert all(w == h for w, h, _s in entries)


def test_icon_artwork_is_the_open_mouth_on_yellow():
    for size in (16, 32, 64):
        img = make_assets.icon_image(size)
        assert (img.width, img.height) == (size, size)
        corner = img.pixel(size // 2, 1)
        assert corner[:3] == (255, 230, 0)  # Shout Yellow tile


def _bmp_header(data: bytes) -> tuple[int, int, int]:
    magic, file_size, _r1, _r2, offset = struct.unpack_from("<2sIHHI", data, 0)
    assert magic == b"BM" and file_size == len(data)
    _size, width, height, _planes, bpp = struct.unpack_from("<IiiHH", data, 14)
    assert offset + width * abs(height) * 4 == len(data)
    return width, height, bpp


def test_wizard_images(tmp_path):
    written = make_assets.write_assets(tmp_path)
    names = {p.name for p in written}
    assert "ShoutAbout.ico" in names
    large = sorted(tmp_path.glob("wizard-large-*.bmp"))
    small = sorted(tmp_path.glob("wizard-small-*.bmp"))
    assert len(large) == len(make_assets.WIZARD_WIDTHS)
    assert len(small) == len(make_assets.SMALL_SIZES)
    for path in large:
        w, h, bpp = _bmp_header(path.read_bytes())
        assert bpp == 32 and abs(h / w - 314 / 164) < 0.01
    for path in small:
        w, h, bpp = _bmp_header(path.read_bytes())
        assert bpp == 32 and w == h


def test_bmp_rows_are_bottom_up_bgra():
    from shoutabout import brand

    img = brand.Image(1, 2, bytes([1, 2, 3, 255, 4, 5, 6, 128]))  # top: (1,2,3), bottom
    data = make_assets.bmp_bytes(img)
    assert data[-8:] == bytes([6, 5, 4, 128, 3, 2, 1, 255])


# ----------------------------------------------------------------------- notices


def test_notices_cover_bundled_distributions(tmp_path):
    out = tmp_path / "NOTICES.txt"
    covered = notices.write_notices(out, ["numpy", "fastapi", "not_a_module"], "ShoutAbout", "9.9")
    assert {"numpy", "fastapi"} <= {c.lower() for c in covered}
    text = out.read_text("utf-8")
    assert text.startswith("ShoutAbout 9.9: third-party software notices")
    assert "numpy" in text and "fastapi" in text
    assert "Permission is hereby granted" in text or "Redistribution and use" in text
    assert "Python " in text


# ------------------------------------------------------------------- installer.iss


@pytest.fixture(scope="module")
def iss() -> str:
    return ISS.read_text("ascii")  # also proves it's pure ASCII


def _setup_value(iss: str, key: str) -> str:
    match = re.search(rf"(?m)^{key}=(.*)$", iss)
    assert match, f"[Setup] {key} missing"
    return match.group(1).strip()


def test_iss_identity(iss):
    guid = re.search(r'#define AppGuid "(\{[0-9A-F-]{36}\})"', iss)
    assert guid, "AppGuid must be a fixed GUID"
    assert _setup_value(iss, "AppId") == "{{#AppGuid}"
    smoke = (PACKAGING / "smoke-test.ps1").read_text("ascii")
    assert guid.group(1) in smoke  # the smoke test checks the same uninstall key
    assert f'#define AppExe "{frozen.APP_EXE_NAME}"' in iss
    assert f'#define AppUserModelId "{frozen.APP_USER_MODEL_ID}"' in iss
    assert "OutputBaseFilename={#AppName}-Setup-{#AppVersion}" in iss


def test_iss_per_machine_one_uac_prompt(iss):
    assert _setup_value(iss, "PrivilegesRequired") == "admin"
    assert _setup_value(iss, "DefaultDirName") == r"{autopf}\{#AppName}"
    assert _setup_value(iss, "ArchitecturesInstallIn64BitMode") == "x64compatible"
    assert "PrivilegesRequiredOverridesAllowed" not in iss


def test_iss_firewall_rule_is_private_only_for_the_app(iss):
    add = re.search(r"'advfirewall firewall add rule (.*?)'Add firewall rule'", iss, re.S)
    assert add, "AddFirewallRule must add the rule with netsh"
    rule = add.group(1)
    for part in ('name="{#FirewallRule}"', "dir=in", "action=allow", "profile=private"):
        assert part in rule
    assert "{app}\\{#AppExe}" in rule
    assert "profile=public" not in rule and "profile=any" not in rule
    assert 'advfirewall firewall delete rule name="{#FirewallRule}"' in iss  # uninstall


def test_iss_run_value_matches_the_app_setting(iss):
    assert '#define RunValue "ShoutAbout"' in iss
    assert r"RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';" in iss
    assert 'Name: "autostart"; Description: "Start ShoutAbout when I sign in"' in iss
    # Offered (ticked) on the first install only, and written as the user who ran Setup.
    assert "(not IsUpgrade) and WizardIsTaskSelected('autostart')" in iss
    assert "ExecAsOriginalUser(ExpandConstant('{sys}\\reg.exe')" in iss
    assert "DisableStartAtSignIn();" in iss


def test_iss_keeps_user_data_unless_asked(iss):
    assert "Also remove my settings and words" in iss
    assert "Box.Checked := False;" in iss
    assert "RemoveUserData := HasParam('/REMOVEUSERDATA');" in iss
    deletes = re.findall(r"DelTree\(ExpandConstant\('([^']+)'\)", iss)
    assert deletes == [r"{userappdata}\{#DataFolder}", r"{localappdata}\{#DataFolder}"]
    assert "and RemoveUserData then" in iss


def test_iss_stops_the_app_and_never_runs_it_elevated(iss):
    assert "function PrepareToInstall" in iss and "StopApp();" in iss
    assert "taskkill.exe'), '/IM {#AppExe}'" in iss  # polite first
    run = iss.split("[Run]", 1)[1].split("[Code]", 1)[0]
    assert "runascurrentuser" not in run
    for line in (ln for ln in run.splitlines() if ln.startswith("Filename:")):
        assert "postinstall" in line or "runasoriginaluser" in line


def test_iss_signing_is_optional(iss):
    assert re.search(r"#ifdef Sign\s+SignTool=shoutabout", iss)
    assert '#define SignFlag ""' in iss


# ------------------------------------------------------------------- PowerShell


@pytest.mark.parametrize("path", PS_FILES, ids=[p.name for p in PS_FILES])
def test_ps1_is_ascii(path):
    path.read_bytes().decode("ascii")


def test_expected_scripts():
    assert {p.name for p in PS_FILES} >= {"build.ps1", "smoke-test.ps1"}


def test_smoke_test_refuses_to_run_outside_ci():
    text = (PACKAGING / "smoke-test.ps1").read_text("ascii")
    assert "$env:GITHUB_ACTIONS -ne 'true'" in text and "[switch]$Force" in text
    assert "$env:SHOUTABOUT_HOME = $TestHome" in text  # never the real config


@pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
@pytest.mark.parametrize("path", PS_FILES, ids=[p.name for p in PS_FILES])
def test_ps1_parses(path):
    script = (
        "$errors = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', "
        "[ref]$tokens, [ref]$errors); "
        "$errors | ForEach-Object { $_.ToString() }; exit $errors.Count"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------------------------------- spec + workflow


def test_spec_is_valid_python_and_collects_generically():
    text = (PACKAGING / "shoutabout.spec").read_text("utf-8")
    ast.parse(text)
    assert "runtime_requirements()" in text  # dependencies come from the package metadata
    assert 'collect_submodules("shoutabout")' in text
    assert "console=False" in text and "uac_admin=False" in text and "upx=False" in text
    assert '"cublas64_12.dll", "cublasLt64_12.dll"' in text


def test_workflow():
    text = WORKFLOW.read_text("utf-8")
    assert "runs-on: windows-latest" in text
    assert "workflow_dispatch:" in text and 'tags: ["v*"]' in text
    assert 'branches: ["main", "v2-*"]' in text
    for path in ("src/**", "packaging/**", "pyproject.toml", "uv.lock"):
        assert f'"{path}"' in text
    assert "uv sync --locked --extra cuda" in text
    assert "smoke-test.ps1" in text and "upload-artifact" in text
    assert "--draft" in text  # releases are never published automatically


# ------------------------------------------------------------------- bundle rules


@pytest.mark.parametrize(
    "dest",
    [
        "_sounddevice_data/portaudio-binaries/libportaudio64bit-asio.dll",
        "_sounddevice_data/portaudio-binaries/libportaudio32bit-asio.dll",
        "_sounddevice_data\\portaudio-binaries\\LIBPORTAUDIOARM64-ASIO.DLL",
        "some/other/asio-thing.bin",
    ],
)
def test_asio_files_are_forbidden(dest):
    assert bundle_rules.is_forbidden(dest)
    assert bundle_rules.is_dropped(dest)


@pytest.mark.parametrize(
    ("dest", "dropped"),
    [
        ("_sounddevice_data/portaudio-binaries/libportaudio64bit.dll", False),
        ("_sounddevice_data/portaudio-binaries/README.md", False),
        ("_sounddevice_data/portaudio-binaries/libportaudio32bit.dll", True),
        ("_sounddevice_data/portaudio-binaries/libportaudioarm64.dll", True),
        ("_sounddevice_data/portaudio-binaries/libportaudio.dylib", True),
        ("_sounddevice_data/portaudio-binaries/.github/workflows/build-libs.yml", True),
        ("ctranslate2/cudnn64_9.dll", True),
        ("ctranslate2/ctranslate2.dll", False),
        ("nvidia/cublas/bin/nvblas64_12.dll", True),
        ("nvidia/cublas/bin/cublasLt64_12.dll", False),
        ("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.alt.dll", True),
        ("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll", False),
        ("base_library.zip", False),
    ],
)
def test_bundle_drop_rules(dest, dropped):
    assert bundle_rules.is_dropped(dest) is dropped


def test_bundle_check_command(tmp_path, capsys):
    (tmp_path / "_internal" / "x").mkdir(parents=True)
    (tmp_path / "_internal" / "x" / "ok.dll").write_bytes(b"MZ")
    assert bundle_rules.main(["check", str(tmp_path)]) == 0
    bad = tmp_path / "_internal" / "x" / "libportaudio64bit-asio.dll"
    bad.write_bytes(b"MZ")
    assert bundle_rules.main(["check", str(tmp_path)]) == 1
    assert "libportaudio64bit-asio.dll" in capsys.readouterr().out
    assert bundle_rules.main(["nope"]) == 2


@pytest.mark.parametrize(
    "dest",
    [
        "av.libs/avcodec-62-984de33114b7fa384296817dec999c9d.dll",
        "av.libs/libx264-165-f3a909470ddc2d85ed21eda3d0fb7954.dll",
        "av.libs/zlib1-79e0c4f9db71cb511398c504b832d395.dll",
        "av/__init__.py",
        "av/audio/frame.cp312-win_amd64.pyd",
        "av-18.1.0.dist-info/METADATA",
        "AVFORMAT-62.DLL",
        "somewhere/else/libx265.dll",
        "avutil-60.dll",
        "swresample-6.dll",
    ],
)
def test_ffmpeg_files_are_forbidden(dest):
    assert bundle_rules.is_forbidden(dest)
    assert bundle_rules.is_dropped(dest)


@pytest.mark.parametrize(
    "dest",
    [
        "shoutabout/data/selftest.m4a",
        "faster_whisper/assets/silero_vad_v6.onnx",
        "numpy/_core/_multiarray_umath.cp312-win_amd64.pyd",
        "_tcl_data/encoding/ascii.enc",
        "onnxruntime/capi/onnxruntime.dll",
        "faster_whisper-1.2.1.dist-info/METADATA",
    ],
)
def test_ffmpeg_rules_leave_the_rest(dest):
    assert not bundle_rules.is_forbidden(dest)


def test_bundle_check_finds_ffmpeg(tmp_path, capsys):
    libs = tmp_path / "_internal" / "av.libs"
    libs.mkdir(parents=True)
    (libs / "avcodec-62-abc.dll").write_bytes(b"MZ")
    assert bundle_rules.main(["check", str(tmp_path)]) == 1
    assert "avcodec-62-abc.dll" in capsys.readouterr().out


def test_ffmpeg_gates_everywhere():
    spec = (PACKAGING / "shoutabout.spec").read_text("utf-8")
    excludes = spec.split("\nEXCLUDES = [", 1)[1].split("]", 1)[0]
    assert '"av",' in excludes
    assert "PyAV must not be bundled" in spec
    iss = ISS.read_text("ascii")
    assert 'Excludes: "\\{#AppExe},*asio*,{#FFmpegExcludes}"' in iss
    for pattern in ("avcodec*", "avformat*", "*x264*", "*x265*", "\\_internal\\av.libs"):
        assert pattern in iss
    smoke = (PACKAGING / "smoke-test.ps1").read_text("ascii")
    assert "no PyAV/FFmpeg files were installed" in smoke
    assert "'no-ffmpeg'" in smoke and "Media Foundation" in smoke


def test_notices_refuse_pyav(tmp_path):
    with pytest.raises(ValueError, match="must not be bundled"):
        notices.write_notices(tmp_path / "N.txt", ["numpy", "av"], "ShoutAbout", "9.9")
    assert not (tmp_path / "N.txt").exists()
    assert "av" not in notices.EXTRA_NOTES


def test_pyav_is_not_a_runtime_dependency():
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))["project"]
    names = [re.split(r"[<>=\[; ]", d, maxsplit=1)[0].lower() for d in project["dependencies"]]
    assert "av" not in names
    assert any(d.startswith("av") for d in project["optional-dependencies"]["pyav"])


def test_asio_gates_everywhere():
    spec = (PACKAGING / "shoutabout.spec").read_text("utf-8")
    assert "is_dropped(b[0])" in spec and "is_dropped(d[0])" in spec
    assert "bundle_rules.py'), 'check'" in (PACKAGING / "build.ps1").read_text("ascii")
    assert 'Excludes: "\\{#AppExe},*asio*,' in ISS.read_text("ascii")
    assert "-like '*asio*'" in (PACKAGING / "smoke-test.ps1").read_text("ascii")


@pytest.mark.parametrize(
    "path",
    [*PS_FILES, ISS, PACKAGING / "shoutabout.spec", *sorted(PACKAGING.glob("*.py"))],
    ids=lambda p: p.name,
)
def test_no_control_characters(path):
    text = path.read_text("utf-8")
    bad = [hex(ord(c)) for c in text if ord(c) < 32 and c not in "\r\n\t"]
    assert not bad, f"control characters in {path.name}: {bad[:5]}"
