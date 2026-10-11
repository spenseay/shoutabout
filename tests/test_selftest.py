"""ShoutAbout.exe --self-test (shoutabout.selftest): the runner, its report and the checks."""

from __future__ import annotations

import json
import sys

import pytest

from shoutabout import selftest
from shoutabout.selftest import Check, Context, SkipCheck


def _ctx(tmp_path, frozen=False):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return Context(home=tmp_path / "home", work=work, frozen=frozen)


# ------------------------------------------------------------------ requirements


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("av<19,>=13", ("av", None)),
        ("PyJWT[crypto]>=2.9", ("pyjwt", None)),
        ("python_multipart>=0.0.18", ("python-multipart", None)),
        ("nvidia-cublas-cu12<13,>=12.4; extra == 'cuda'", ("nvidia-cublas-cu12", "cuda")),
        ('sounddevice>=0.5; sys_platform == "win32"', ("sounddevice", None)),
        ('foo; python_version >= "3.12" and extra == "dev"', ("foo", "dev")),
    ],
)
def test_parse_requirement(spec, expected):
    assert selftest.parse_requirement(spec) == expected


def test_runtime_requirements_keeps_bundled_extras_only():
    requires = [
        "fastapi>=0.115",
        "nvidia-cublas-cu12<13; extra == 'cuda'",
        "pytest; extra == 'test'",
        "FastAPI>=1",  # duplicate, other spelling
    ]
    assert selftest.runtime_requirements(requires) == ["fastapi", "nvidia-cublas-cu12"]
    assert selftest.runtime_requirements(requires, extras=()) == ["fastapi"]


def test_runtime_requirements_from_installed_metadata():
    names = selftest.runtime_requirements()
    for dist in ("fastapi", "uvicorn", "faster-whisper", "numpy", "pyjwt"):
        assert dist in names
    # PyAV (FFmpeg) is only an optional extra: the app must not need, bundle or check it.
    assert "av" not in names


def test_import_names():
    mapping = {"pyjwt": ["jwt"], "python-multipart": ["python_multipart", "multipart"]}
    assert selftest.import_names("PyJWT", mapping) == ["jwt"]
    assert selftest.import_names("python-multipart", mapping) == ["python_multipart", "multipart"]
    assert selftest.import_names("faster-whisper", {}) == ["faster_whisper"]  # built-in table
    assert selftest.import_names("nvidia-cublas-cu12", {}) == []  # DLLs only
    assert selftest.import_names("some-new-lib", {}) == ["some_new_lib"]


def test_import_names_from_installed_metadata():
    assert "jwt" in selftest.import_names("pyjwt")
    assert selftest.import_names("faster-whisper") == ["faster_whisper"]


# ------------------------------------------------------------------------ runner


def test_run_check_statuses(tmp_path):
    ctx = _ctx(tmp_path)

    def ok(_ctx):
        return "fine"

    def skip(_ctx):
        raise SkipCheck("not here")

    def boom(_ctx):
        raise ValueError("bad thing")

    assert selftest.run_check(Check("a", ok), ctx).status == "pass"
    result = selftest.run_check(Check("b", skip), ctx)
    assert (result.status, result.detail, result.failed) == ("skip", "not here", False)
    result = selftest.run_check(Check("c", boom), ctx)
    assert result.status == "fail" and result.failed
    assert result.detail.startswith("ValueError: bad thing at test_selftest.py:")
    optional = selftest.run_check(Check("d", boom, required=False), ctx)
    assert optional.status == "fail" and not optional.failed


def test_run_check_turns_system_exit_into_a_failure(tmp_path):
    def exits(_ctx):
        raise SystemExit(3)

    assert selftest.run_check(Check("x", exits), _ctx(tmp_path)).status == "fail"


def test_required_can_depend_on_frozen(tmp_path):
    check = Check("cublas", lambda _c: "", required=lambda c: c.frozen)
    assert check.is_required(_ctx(tmp_path, frozen=True)) is True
    assert check.is_required(_ctx(tmp_path, frozen=False)) is False


def test_run_selftest_report(tmp_path):
    checks = [
        Check("good", lambda _c: "ok"),
        Check("optional", lambda _c: 1 / 0, required=False),
        Check("skipped", lambda _c: (_ for _ in ()).throw(SkipCheck("n/a"))),
    ]
    report = selftest.run_selftest(tmp_path / "home", checks=checks, frozen=False)
    assert report.ok and report.failed == []
    data = report.to_dict()
    assert data["version"] and data["frozen"] is False
    assert [c["status"] for c in data["checks"]] == ["pass", "fail", "skip"]

    checks.append(Check("bad", lambda _c: 1 / 0))
    report = selftest.run_selftest(tmp_path / "home", checks=checks, frozen=False)
    assert not report.ok and report.failed == ["bad"]
    assert "FAILED: bad" in selftest.format_summary(report)


def test_main_writes_the_report_and_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(selftest, "DEFAULT_CHECKS", (Check("good", lambda _c: "ok"),))
    out = tmp_path / "r" / "report.json"
    assert selftest.main(["--home", str(tmp_path / "home"), "--out", str(out)]) == 0
    data = json.loads(out.read_text("utf-8"))
    assert data["ok"] is True and data["checks"][0]["name"] == "good"
    assert json.loads(capsys.readouterr().out)["ok"] is True

    monkeypatch.setattr(selftest, "DEFAULT_CHECKS", (Check("bad", lambda _c: 1 / 0),))
    assert selftest.main(["--text"]) == 1  # temp home, readable summary
    assert "[FAIL] bad" in capsys.readouterr().out


def test_main_without_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(selftest, "DEFAULT_CHECKS", (Check("good", lambda _c: "ok"),))
    monkeypatch.setattr(sys, "stdout", None)
    out = tmp_path / "report.json"
    assert selftest.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text("utf-8"))["ok"] is True


def test_main_usage_error():
    assert selftest.main(["--nope"]) == 2


# ------------------------------------------------------------------------ checks


def test_check_config_creates_the_home(tmp_path):
    ctx = _ctx(tmp_path)
    detail = selftest.check_config(ctx)
    assert (ctx.home / "config.toml").is_file()
    assert (ctx.home / "hub_token.txt").is_file()
    token = (ctx.home / "hub_token.txt").read_text("utf-8").strip()
    assert token not in detail  # the token value is never reported
    assert "created config.toml" in detail
    assert "created nothing" in selftest.check_config(ctx)  # second run loads it


def test_check_audio_decodes_the_bundled_sample(tmp_path):
    sample = selftest.audio_sample_path()
    assert sample.is_file() and sample.stat().st_size < 20_000
    detail = selftest.check_audio(_ctx(tmp_path))
    assert "selftest.m4a (AAC, " in detail and "of 440 Hz" in detail
    if sys.platform == "win32":
        assert detail.startswith("Windows Media Foundation:")
    else:
        assert detail.startswith("PyAV:")


def test_check_audio_fails_on_a_wrong_decode(tmp_path, monkeypatch):
    import numpy as np

    from shoutabout import audio

    def wrong(path, max_seconds=None, *, backends=None):
        return audio.DecodedAudio(np.zeros(4000, np.float32), backends[0], 16000, 1)

    monkeypatch.setattr(audio, "decode_audio_info", wrong)
    with pytest.raises(RuntimeError, match="decoded 0.25 s"):
        selftest.check_audio(_ctx(tmp_path))


def test_check_no_ffmpeg_skips_from_source(tmp_path):
    with pytest.raises(SkipCheck, match="from source"):
        selftest.check_no_ffmpeg(_ctx(tmp_path))


def test_check_no_ffmpeg_frozen(tmp_path, monkeypatch):
    import shoutabout.frozen
    from shoutabout import avshim

    bundle = tmp_path / "_internal"
    (bundle / "numpy").mkdir(parents=True)
    (bundle / "numpy" / "core.pyd").write_bytes(b"MZ")
    monkeypatch.setattr(shoutabout.frozen, "bundle_dir", lambda: bundle)
    monkeypatch.setattr(avshim, "real_av_available", lambda: False)
    assert "no PyAV/FFmpeg" in selftest.check_no_ffmpeg(_ctx(tmp_path, frozen=True))

    (bundle / "av.libs").mkdir()
    (bundle / "av.libs" / "avcodec-62-abc.dll").write_bytes(b"MZ")
    with pytest.raises(RuntimeError, match="avcodec-62-abc.dll"):
        selftest.check_no_ffmpeg(_ctx(tmp_path, frozen=True))

    monkeypatch.setattr(avshim, "real_av_available", lambda: True)
    with pytest.raises(RuntimeError, match="the real PyAV imports"):
        selftest.check_no_ffmpeg(_ctx(tmp_path, frozen=True))


def test_no_ffmpeg_check_is_required_when_frozen(tmp_path):
    check = next(c for c in selftest.DEFAULT_CHECKS if c.name == "no-ffmpeg")
    assert check.is_required(_ctx(tmp_path, frozen=True))
    assert not check.is_required(_ctx(tmp_path, frozen=False))


def test_check_modules_and_dependencies(tmp_path):
    assert "modules import" in selftest.check_modules(_ctx(tmp_path))
    assert "dependencies import" in selftest.check_dependencies(_ctx(tmp_path))


def test_package_modules_skip_main():
    names = selftest._package_modules()
    assert "shoutabout.cli" in names and "shoutabout.selftest" in names
    assert "shoutabout.__main__" not in names


def test_check_brand_from_source(tmp_path):
    assert "tray icon renders" in selftest.check_brand(_ctx(tmp_path))


def test_check_brand_frozen_needs_the_icon(tmp_path, monkeypatch):
    import shoutabout.frozen

    monkeypatch.setattr(shoutabout.frozen, "app_icon_path", lambda: None)
    with pytest.raises(RuntimeError, match="ShoutAbout.ico"):
        selftest.check_brand(_ctx(tmp_path, frozen=True))


def test_check_hardware_skips_without_a_module(tmp_path, monkeypatch):
    monkeypatch.setattr(selftest, "HARDWARE_MODULES", ("shoutabout.no_such_module",))
    with pytest.raises(SkipCheck):
        selftest.check_hardware(_ctx(tmp_path))


def test_check_hardware_prefers_a_summary(tmp_path, monkeypatch):
    import types

    class Info:
        def summary(self):
            return "RTX test, 16 GB; 8 cores; 32 GB RAM"

    module = types.ModuleType("shoutabout.hardware")
    module.detect = lambda: Info()
    monkeypatch.setitem(sys.modules, "shoutabout.hardware", module)
    detail = selftest.check_hardware(_ctx(tmp_path))
    assert detail == "shoutabout.hardware.detect(): RTX test, 16 GB; 8 cores; 32 GB RAM"


def test_check_hardware_uses_a_detect_function(tmp_path, monkeypatch):
    import types

    module = types.ModuleType("shoutabout.hardware")
    module.detect_hardware = lambda: {"gpu": "Test GPU", "vram_gb": 8}
    monkeypatch.setitem(sys.modules, "shoutabout.hardware", module)
    detail = selftest.check_hardware(_ctx(tmp_path))
    assert detail.startswith("shoutabout.hardware.detect_hardware():")
    assert "Test GPU" in detail


@pytest.mark.skipif(sys.platform == "win32", reason="Windows loads the real DLLs")
def test_check_cublas_skips_off_windows(tmp_path):
    with pytest.raises(SkipCheck):
        selftest.check_cublas(_ctx(tmp_path))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_check_cublas_frozen_without_dlls_fails(tmp_path, monkeypatch):
    import shoutabout.engine.cuda_dlls as cuda_dlls

    monkeypatch.setattr(cuda_dlls, "add_nvidia_dll_dirs", lambda: [])
    with pytest.raises(RuntimeError, match="not in the bundle"):
        selftest.check_cublas(_ctx(tmp_path, frozen=True))
    with pytest.raises(SkipCheck):
        selftest.check_cublas(_ctx(tmp_path, frozen=False))


def test_check_downloads_from_source(tmp_path):
    assert selftest.check_downloads(_ctx(tmp_path)) == "model downloads use hf_xet"


def test_check_downloads_fails_without_metadata(tmp_path, monkeypatch):
    import huggingface_hub.utils._runtime as runtime

    monkeypatch.setattr(runtime, "is_xet_available", lambda: False)
    with pytest.raises(RuntimeError, match="can't find it"):
        selftest.check_downloads(_ctx(tmp_path, frozen=True))


@pytest.mark.skipif(sys.platform != "win32", reason="the pill's text uses GDI (Windows)")
def test_overlay_check_passes_for_the_current_pill():
    assert "pill frames render" in selftest.check_overlay(None)  # type: ignore[arg-type]
