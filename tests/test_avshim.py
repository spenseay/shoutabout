"""shoutabout.avshim: running without PyAV, as the Windows app does.

The interesting cases run in a child Python where ``import av`` is blocked, so that
faster-whisper is imported for the first time with only the placeholder available.
"""

from __future__ import annotations

import json
import pickle
import subprocess
import sys
import textwrap
import types

import pytest

from shoutabout import avshim

REPO_SRC = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")


def test_real_pyav_is_left_alone():
    assert avshim.ensure_av_importable() is True
    assert avshim.real_av_available() is True
    assert not avshim.is_placeholder(avshim.import_real_av())


def test_placeholder_behaviour(monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)  # makes `import av` fail
    assert avshim.real_av_available() is False
    assert avshim.ensure_av_importable() is False
    placeholder = sys.modules["av"]
    assert avshim.is_placeholder(placeholder)
    assert avshim.ensure_av_importable() is False  # idempotent
    import av

    assert av is placeholder
    with pytest.raises(avshim.PyAVUnavailableError, match="isn't part of this ShoutAbout"):
        av.open("x.m4a")
    assert isinstance(avshim.PyAVUnavailableError("x"), AttributeError)
    # Introspection keeps working (hasattr/getattr defaults, pickle's module scan, ...).
    assert not hasattr(av, "open")
    assert getattr(av, "__file__", None) is None
    assert getattr(av, "__path__", None) is None
    pickle.dumps(types.SimpleNamespace(a=1))
    with pytest.raises(ImportError):
        avshim.import_real_av()


def test_dll_load_failure_counts_as_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "av":
            raise OSError("[WinError 126] The specified module could not be found")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "av", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert avshim.ensure_av_importable() is False
    assert avshim.is_placeholder(sys.modules["av"])


CHILD = textwrap.dedent(
    """
    import json, sys
    sys.modules["av"] = None  # PyAV not installed, as in the Windows app
    from shoutabout import avshim
    report = {"ensure": avshim.ensure_av_importable()}

    import faster_whisper  # imports av at import time
    report["whisper_model"] = hasattr(faster_whisper, "WhisperModel")
    try:
        import av.audio  # the placeholder is not a package
    except ImportError:
        report["submodule"] = "ImportError"
    try:
        faster_whisper.decode_audio("anything.wav")
    except avshim.PyAVUnavailableError as exc:
        report["decode_audio"] = str(exc)

    from shoutabout.engine.faster_whisper_engine import _import_backend
    report["backend"] = _import_backend().whisper_model.__name__

    from shoutabout import audio, pyavdecode
    report["pyav_available"] = pyavdecode.available()
    try:
        audio.encode_m4a("a.wav", "b.m4a")
    except audio.AudioError as exc:
        report["encode"] = str(exc)
    report["backends"] = list(audio.default_backends())

    from pathlib import Path
    sample = Path(sys.argv[1])
    if sys.platform == "win32":
        info = audio.decode_audio_info(sample)
        report["decoded"] = [info.backend, round(len(info.samples) / 16000, 2)]
    else:
        try:
            audio.decode_audio(sample)
        except audio.AudioError as exc:
            report["decoded"] = ["error", str(exc)]

    from shoutabout import selftest
    from pathlib import Path as P
    import tempfile
    ctx = selftest.Context(home=P(tempfile.mkdtemp()), work=P(tempfile.mkdtemp()), frozen=False)
    report["vad"] = selftest.check_vad(ctx)
    report["dependencies"] = selftest.check_dependencies(ctx)
    print(json.dumps(report))
    """
)


@pytest.fixture(scope="module")
def without_pyav():
    from shoutabout.selftest import audio_sample_path

    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(audio_sample_path())],
        capture_output=True,
        text=True,
        timeout=300,
        env={**__import__("os").environ, "PYTHONPATH": REPO_SRC},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_faster_whisper_imports_without_pyav(without_pyav):
    assert without_pyav["ensure"] is False
    assert without_pyav["whisper_model"] is True
    assert without_pyav["backend"] == "WhisperModel"
    assert without_pyav["submodule"] == "ImportError"
    assert "isn't part of this ShoutAbout installation" in without_pyav["decode_audio"]


def test_decoding_without_pyav(without_pyav):
    assert without_pyav["pyav_available"] is False
    assert "needs PyAV" in without_pyav["encode"]
    if sys.platform == "win32":
        assert without_pyav["backends"] == ["wav", "mediafoundation", "pyav"]
        backend, seconds = without_pyav["decoded"]
        assert backend == "mediafoundation" and 0.9 < seconds < 1.2
    else:
        assert without_pyav["decoded"][0] == "error"
        assert "can't decode" in without_pyav["decoded"][1]


def test_selftest_checks_without_pyav(without_pyav):
    assert "Silero VAD ran" in without_pyav["vad"]
    assert "faster-whisper" in without_pyav["dependencies"]
    assert "av," not in without_pyav["dependencies"]
