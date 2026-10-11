"""Microphone recorder with a fake stream: no real microphone is ever opened."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from shoutabout import mic
from shoutabout.mic import (
    SAMPLE_RATE,
    LevelMeter,
    MicError,
    MicRecorder,
    friendly_error,
    privacy_block,
    resample,
)


class FakeStream:
    """Delivers blocks to the recorder's callback when the test says so."""

    def __init__(self, samplerate, blocksize, callback) -> None:
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.started = self.stopped = self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def feed(self, samples: np.ndarray) -> None:
        for i in range(0, len(samples), self.blocksize):
            block = samples[i : i + self.blocksize].reshape(-1, 1)
            self.callback(block, len(block), None, None)


class Factory:
    def __init__(self, *, refuse_16k: bool = False, device_rate: int = 48_000, error=None):
        self.refuse_16k = refuse_16k
        self.device_rate = device_rate
        self.error = error
        self.streams: list[FakeStream] = []
        self.requested: list[int | None] = []

    def __call__(self, samplerate, blocksize, callback):
        self.requested.append(samplerate)
        if self.error is not None:
            raise self.error
        if samplerate == SAMPLE_RATE and self.refuse_16k:
            raise RuntimeError("Error opening InputStream: Invalid sample rate [PaErrorCode -9997]")
        stream = FakeStream(samplerate or self.device_rate, blocksize, callback)
        self.streams.append(stream)
        return stream


def no_privacy(hive, key):
    return None


def speech(seconds: float, rate: int = SAMPLE_RATE, amp: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_records_16k_mono_and_reports_levels():
    factory = Factory()
    levels = []
    rec = MicRecorder(stream_factory=factory, on_level=levels.append, privacy_reader=no_privacy)
    rec.start()
    assert rec.recording and factory.requested == [SAMPLE_RATE]
    stream = factory.streams[0]
    assert stream.started
    stream.feed(speech(1.0))
    audio = rec.stop()
    assert stream.stopped and stream.closed and not rec.recording
    assert audio.dtype == np.float32 and len(audio) == SAMPLE_RATE
    assert levels and 0.5 < max(levels) <= 1.0
    assert rec.stop().size == 0  # not recording any more


def test_start_twice_is_harmless():
    factory = Factory()
    rec = MicRecorder(stream_factory=factory, privacy_reader=no_privacy)
    rec.start()
    rec.start()
    assert len(factory.streams) == 1
    rec.cancel()
    assert factory.streams[0].closed and not rec.recording


def test_device_without_16k_is_resampled():
    factory = Factory(refuse_16k=True, device_rate=48_000)
    rec = MicRecorder(stream_factory=factory, privacy_reader=no_privacy)
    rec.start()
    assert factory.requested == [SAMPLE_RATE, None]
    factory.streams[0].feed(speech(1.0, rate=48_000))
    audio = rec.stop()
    assert abs(len(audio) - SAMPLE_RATE) <= 1


def test_time_limit_calls_back_once_and_stops_collecting():
    factory = Factory()
    hit = threading.Event()
    calls = []

    def on_limit():
        calls.append(1)
        hit.set()

    rec = MicRecorder(
        max_seconds=0.5, stream_factory=factory, on_limit=on_limit, privacy_reader=no_privacy
    )
    rec.start()
    factory.streams[0].feed(speech(1.0))
    assert hit.wait(2)
    audio = rec.stop()
    assert len(calls) == 1
    assert 0.5 * SAMPLE_RATE <= len(audio) < 0.6 * SAMPLE_RATE


def test_digital_silence_means_blocked_or_muted():
    factory = Factory()
    rec = MicRecorder(stream_factory=factory, privacy_reader=no_privacy)
    rec.start()
    factory.streams[0].feed(np.zeros(SAMPLE_RATE, dtype=np.float32))
    with pytest.raises(MicError) as info:
        rec.stop()
    assert info.value.short == "Mic is silent"

    def denied(hive, key):
        return "Deny" if key.endswith("NonPackaged") else "Allow"

    rec = MicRecorder(stream_factory=factory, privacy_reader=denied)
    rec.start()
    factory.streams[-1].feed(np.zeros(SAMPLE_RATE, dtype=np.float32))
    with pytest.raises(MicError) as info:
        rec.stop()
    assert info.value.short == "Mic blocked"
    assert "Let desktop apps access your microphone" in str(info.value)


def test_quiet_room_noise_is_not_silence():
    factory = Factory()
    rec = MicRecorder(stream_factory=factory, privacy_reader=no_privacy)
    rec.start()
    noise = (np.random.default_rng(1).standard_normal(SAMPLE_RATE) * 1e-4).astype(np.float32)
    factory.streams[0].feed(noise)
    assert len(rec.stop()) == SAMPLE_RATE


def test_open_errors_become_friendly():
    rec = MicRecorder(
        stream_factory=Factory(error=RuntimeError("Error querying device -1")),
        privacy_reader=no_privacy,
    )
    with pytest.raises(MicError) as info:
        rec.start()
    assert info.value.short == "No mic" and "Sound > Input" in str(info.value)
    assert not rec.recording


def test_stream_that_will_not_start_is_closed():
    class Stubborn(FakeStream):
        def start(self):
            raise RuntimeError("Unanticipated host error [PaErrorCode -9999]")

    made = []

    def factory(rate, block, cb):
        made.append(Stubborn(rate, block, cb))
        return made[-1]

    rec = MicRecorder(stream_factory=factory, privacy_reader=no_privacy)
    with pytest.raises(MicError):
        rec.start()
    assert made[0].closed and not rec.recording


def test_open_error_with_privacy_off_says_how_to_fix_it():
    rec = MicRecorder(
        stream_factory=Factory(error=RuntimeError("Unanticipated host error [PaErrorCode -9999]")),
        privacy_reader=lambda hive, key: "Deny" if hive == "HKCU" else None,
    )
    with pytest.raises(MicError) as info:
        rec.start()
    assert info.value.short == "Mic blocked"
    assert "Privacy & security > Microphone" in str(info.value)


def test_friendly_error_mapping():
    assert friendly_error(RuntimeError("No Default Input Device Available"), no_privacy).short == (
        "No mic"
    )
    other = friendly_error(RuntimeError("Device busy [PaErrorCode -9985]"), no_privacy)
    assert other.short == "Mic problem" and "Device busy" in str(other)
    same = MicError("x", "y")
    assert friendly_error(same, no_privacy) is same


def test_privacy_block_reads_the_three_switches():
    def reader(values):
        return lambda hive, key: values.get((hive, key.rsplit("\\", 1)[-1]))

    assert privacy_block(reader({})) is None
    assert privacy_block(reader({("HKLM", "microphone"): "Deny"})) == "device"
    assert privacy_block(reader({("HKCU", "microphone"): "deny"})) == "user"
    assert privacy_block(reader({("HKCU", "NonPackaged"): "Deny"})) == "desktop"
    assert privacy_block(reader({("HKCU", "NonPackaged"): "Allow"})) is None


def test_level_meter():
    meter = LevelMeter()
    assert meter.target(np.zeros(100, np.float32)) == 0.0
    assert meter.target(np.ones(100, np.float32)) == 1.0
    assert meter.target(np.zeros(0, np.float32)) == 0.0
    loud = np.full(480, 0.3, np.float32)
    up = meter.update(loud)
    assert 0.5 < up < 1.0  # fast attack
    down = meter.update(np.zeros(480, np.float32))
    assert 0.0 < down < up and down > up * 0.8  # slow release


def test_resample_keeps_pitch_and_drops_ultrasonics():
    rate = 48_000
    t = np.arange(rate) / rate
    tone = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    out = resample(tone, rate)
    assert len(out) == SAMPLE_RATE
    spectrum = np.abs(np.fft.rfft(out))
    assert abs(np.argmax(spectrum) - 1000) <= 1  # 1 Hz bins over 1 s
    assert 0.6 < np.sqrt(np.mean(out[1000:-1000] ** 2)) / np.sqrt(0.5) < 1.1
    high = np.sin(2 * np.pi * 15_000 * t).astype(np.float32)
    assert np.sqrt(np.mean(resample(high, rate)[1000:-1000] ** 2)) < 0.05
    assert resample(tone, SAMPLE_RATE) is not None
    assert resample(np.zeros(0, np.float32), rate).size == 0


def test_sounddevice_is_imported_lazily():
    """No module-level sounddevice import: CI has no PortAudio, and importing the hub must
    not touch audio devices."""
    import ast

    tree = ast.parse(Path(mic.__file__).read_text("utf-8"))
    top = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    names = {a.name for n in top for a in n.names} | {
        n.module for n in top if isinstance(n, ast.ImportFrom)
    }
    assert "sounddevice" not in names


# ---------------------------------------------------------------- devices


def test_has_input_device_asks_windows_without_opening_anything():
    assert mic.has_input_device(lambda: 0) is False
    assert mic.has_input_device(lambda: 2) is True
    assert mic.has_input_device(lambda: None) is None
    assert mic.no_mic_error().short == "No mic"


def test_portaudio_rescans_when_a_microphone_is_plugged_in(monkeypatch):
    import sys
    import types

    calls: list[str] = []
    fake = types.ModuleType("sounddevice")
    fake._terminate = lambda: calls.append("terminate")  # type: ignore[attr-defined]
    fake._initialize = lambda: calls.append("initialize")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(mic, "_pa_devices", None)
    count = {"n": 0}
    assert mic.portaudio(lambda: count["n"]) is fake
    assert mic.portaudio(lambda: count["n"]) is fake
    assert calls == []  # nothing changed: nothing to refresh
    count["n"] = 1  # a microphone was plugged in
    mic.portaudio(lambda: count["n"])
    assert calls == ["terminate", "initialize"]
    mic.portaudio(lambda: None)  # can't ask Windows: leave it
    assert len(calls) == 2


def test_warm_up_never_raises(monkeypatch):
    def broken(*_a, **_k):
        raise OSError("no PortAudio")

    monkeypatch.setattr(mic, "portaudio", broken)
    mic.warm_up()
