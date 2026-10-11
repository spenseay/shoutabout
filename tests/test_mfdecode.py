"""shoutabout.mfdecode: Windows Media Foundation through ctypes.

The pure parts (GUIDs, HRESULTs, "N edition" messages, what happens when Media Foundation
is missing) run everywhere. Tests that decode with the real Media Foundation are marked
``windows`` (``--run-windows``); the default suite still exercises it on Windows through
tests/test_audio.py, where it is the decoder for every compressed file.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import uuid
from pathlib import Path

import numpy as np
import pytest

from shoutabout import audio, mfdecode, pyavdecode


@pytest.fixture
def fresh(monkeypatch):
    """Forget the loaded DLLs / MFStartup so a test can simulate another PC."""
    monkeypatch.setattr(mfdecode, "_api", None)
    monkeypatch.setattr(mfdecode, "_api_error", None)
    monkeypatch.setattr(mfdecode, "_started", False)


def tone(seconds: float, rate: int, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def make_m4a(path: Path, seconds: float = 2.0, rate: int = 44_100, channels: int = 1) -> Path:
    import av

    layout = "mono" if channels == 1 else "stereo"
    with av.open(str(path), "w", format="ipod") as c:
        s = c.add_stream("aac", rate=rate, layout=layout)
        data = np.tile(tone(seconds, rate), (channels, 1))
        frame = av.AudioFrame.from_ndarray(data, format="fltp", layout=layout)
        frame.sample_rate = rate
        fifo = av.AudioFifo()
        fifo.write(frame)
        while fifo.samples:
            for p in s.encode(fifo.read(min(1024, fifo.samples))):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)
    return path


def peak_hz(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[int(np.argmax(spectrum))])


# ------------------------------------------------------------------- everywhere


@pytest.mark.parametrize(
    "text",
    [
        "279a808d-aec7-40c8-9c6b-a6b492c78a66",
        "00000003-0000-0010-8000-00aa00389b71",
    ],
)
def test_guid_layout(text):
    guid = mfdecode.GUID.of(text)
    assert ctypes.sizeof(guid) == 16
    assert str(guid) == text
    assert bytes(guid) == uuid.UUID(text).bytes_le


def test_well_known_guids_follow_the_audio_subtype_pattern():
    # MFAudioFormat_* = {<format tag>-0000-0010-8000-00AA00389B71}
    assert str(mfdecode.MFAudioFormat_Float).startswith("00000003-")
    assert str(mfdecode.MFAudioFormat_PCM).startswith("00000001-")
    assert str(mfdecode.MFMediaType_Audio) == "73647561-0000-0010-8000-00aa00389b71"  # 'auds'


def test_failed():
    assert mfdecode.failed(0xC00D36C4) and mfdecode.failed(-2147024894)
    assert not mfdecode.failed(0) and not mfdecode.failed(1)


def test_propvariant_is_big_enough():
    assert ctypes.sizeof(mfdecode.PROPVARIANT) >= (
        24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16
    )


def test_missing_message_points_n_editions_to_the_feature_pack(monkeypatch):
    monkeypatch.setattr(mfdecode, "is_n_edition", lambda: False)
    plain = mfdecode._missing_message()
    assert "Media Feature Pack" in plain and "N" in plain
    monkeypatch.setattr(mfdecode, "is_n_edition", lambda: True)
    assert mfdecode.N_EDITION_HINT in mfdecode._missing_message("(x)")


def test_is_n_edition_reads_the_edition(monkeypatch):
    if sys.platform != "win32":
        assert mfdecode.is_n_edition() is False
        return
    import winreg

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    for edition, expected in [
        ("Core", False),
        ("CoreN", True),
        ("ProfessionalN", True),
        ("EducationN", True),
        ("EnterpriseSN", True),
        ("Professional", False),
        ("ServerFoundation", False),
    ]:
        monkeypatch.setattr(winreg, "OpenKey", lambda *a: Key())
        monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name, e=edition: (e, 1))
        assert mfdecode.is_n_edition() is expected, edition


def test_off_windows_media_foundation_is_unavailable(fresh, monkeypatch):
    monkeypatch.setattr(mfdecode.sys, "platform", "linux")
    ok, why = mfdecode.available()
    assert not ok and "only available on Windows" in why
    with pytest.raises(mfdecode.MFUnavailableError):
        mfdecode.decode(b"\x00", hint=".m4a")
    assert mfdecode.probe_seconds(b"\x00") is None


def test_missing_dlls_like_an_n_edition(fresh, monkeypatch, tmp_path):
    """Windows N without the Media Feature Pack: mfplat.dll doesn't exist."""

    def no_dll(name, *args, **kwargs):
        raise FileNotFoundError(f"Could not find module '{name}' (or one of its dependencies).")

    monkeypatch.setattr(mfdecode.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", no_dll, raising=False)
    monkeypatch.setattr(mfdecode, "is_n_edition", lambda: True)
    ok, why = mfdecode.available()
    assert not ok
    assert "Media Feature Pack" in why and "Optional features" in why
    assert mfdecode.N_EDITION_HINT in why

    # The decoder chain turns it into a "this PC can't decode" error (HTTP 503), not 415.
    upload = tmp_path / "upload.m4a"
    upload.write_bytes(b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 64)
    monkeypatch.setattr(pyavdecode, "available", lambda: False)
    with pytest.raises(audio.DecoderUnavailableError, match="Media Feature Pack") as info:
        audio.decode_audio_info(upload, backends=(audio.WAV, audio.MEDIA_FOUNDATION, audio.PYAV))
    assert str(upload.parent) not in str(info.value)  # no local paths in the message


def test_mfstartup_failure_is_reported(fresh, monkeypatch):
    class FakeFn:
        def __init__(self, result):
            self.result = result

        def __call__(self, *args):
            return self.result

    class FakeDll:
        def __getattr__(self, name):
            fn = FakeFn(-1072875856 if name == "MFStartup" else 0)  # MF_E_... failure
            setattr(self, name, fn)
            return fn

    monkeypatch.setattr(mfdecode.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, *a, **k: FakeDll(), raising=False)
    ok, why = mfdecode.available()
    assert not ok and "MFStartup failed" in why


# ---------------------------------------------------------------- real Windows


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
@pytest.mark.parametrize("channels", [1, 2])
def test_decode_file_and_bytes(tmp_path, channels):
    path = make_m4a(tmp_path / "a.m4a", channels=channels, rate=48_000)
    from_file = mfdecode.decode(path, hint=".m4a")
    from_bytes = mfdecode.decode(path.read_bytes(), hint=".m4a")
    for decoded in (from_file, from_bytes):
        assert len(decoded.segments) == 1
        seg = decoded.segments[0]
        assert (seg.rate, seg.channels) == (48_000, channels)
        assert abs(decoded.seconds - 2.0) < 0.1
        assert decoded.declared_seconds == pytest.approx(2.0, abs=0.1)
        mono = seg.samples().reshape(-1, channels)[:, 0]
        assert abs(peak_hz(mono, 48_000) - 440) < 2
    assert np.array_equal(from_file.segments[0].samples(), from_bytes.segments[0].samples())


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_probe_seconds(tmp_path):
    path = make_m4a(tmp_path / "a.m4a", seconds=3.0)
    assert mfdecode.probe_seconds(path, hint=".m4a") == pytest.approx(3.0, abs=0.1)
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 64)
    assert mfdecode.probe_seconds(junk, hint=".m4a") is None
    assert mfdecode.probe_seconds(tmp_path / "missing.m4a") is None


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_errors(tmp_path):
    good = make_m4a(tmp_path / "good.m4a").read_bytes()
    with pytest.raises(mfdecode.MFError):
        mfdecode.decode(good[:300], hint=".m4a")
    with pytest.raises(mfdecode.MFUnsupportedError):
        mfdecode.decode(b"OggS" + b"\x00" * 500, hint=".ogg")
    with pytest.raises(FileNotFoundError):
        mfdecode.decode(tmp_path / "missing.m4a", hint=".m4a")


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_no_audio_stream(tmp_path):
    import av

    path = tmp_path / "video.mp4"
    with av.open(str(path), "w", format="mp4") as c:
        v = c.add_stream("mpeg4", rate=10)
        v.width, v.height, v.pix_fmt = 32, 32, "yuv420p"
        for _ in range(5):
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), np.uint8), format="rgb24")
            for p in v.encode(frame):
                c.mux(p)
        for p in v.encode(None):
            c.mux(p)
    with pytest.raises(mfdecode.MFNoAudioError):
        mfdecode.decode(path, hint=".m4a")


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_limits(tmp_path, monkeypatch):
    path = make_m4a(tmp_path / "a.m4a", seconds=3.0)
    with pytest.raises(mfdecode.MFTooLongError) as info:
        mfdecode.decode(path, hint=".m4a", max_seconds=1.0)
    assert info.value.at_least is False and info.value.seconds == pytest.approx(3.0, abs=0.1)
    monkeypatch.setattr(mfdecode, "_declared_seconds", lambda api, reader: None)
    with pytest.raises(mfdecode.MFTooLongError) as info:
        mfdecode.decode(path, hint=".m4a", max_seconds=1.0)
    assert info.value.at_least is True and 1.0 < info.value.seconds < 1.2  # stopped early


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_file_is_released_right_after_decoding(tmp_path):
    path = make_m4a(tmp_path / "a.m4a")
    mfdecode.decode(path, hint=".m4a")
    path.unlink()  # the hub deletes each upload as soon as it is transcribed
    assert not path.exists()


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_threads_and_apartments(tmp_path):
    files = [make_m4a(tmp_path / f"{i}.m4a", seconds=1.0 + i / 4) for i in range(4)]
    expected = {f: mfdecode.decode(f, hint=".m4a").segments[0].samples() for f in files}
    errors: list[str] = []

    def worker(n: int, sta: bool) -> None:
        ole32 = ctypes.windll.ole32
        if sta:  # like a Tk thread: a single-threaded apartment already exists
            ole32.CoInitializeEx(None, 0x2)
        try:
            for i in range(12):
                f = files[(n + i) % len(files)]
                got = mfdecode.decode(f, hint=".m4a").segments[0].samples()
                if not np.array_equal(got, expected[f]):
                    errors.append(f"thread {n}: {f.name} differs")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(f"thread {n}: {type(exc).__name__}: {exc}")
        finally:
            if sta:
                ole32.CoUninitialize()

    threads = [threading.Thread(target=worker, args=(n, n % 2 == 0)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Media Foundation is Windows-only")
def test_available_here():
    assert mfdecode.available() == (True, "")
