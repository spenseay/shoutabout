""" "Talk to this PC" with streaming: the microphone feeds a streamed dictation while you
talk, so at "stop" only the tail is left; Escape / stop / a slip of the finger throw it
away; any streaming failure that isn't the engine's falls back to the whole recording.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from fakes import FakeEngine
from shoutabout import streaming
from shoutabout.hotkey import Action, HotkeyEvent
from shoutabout.local_dictation import DictationState
from shoutabout.mic import SAMPLE_RATE, MicRecorder
from shoutabout.recent import Recent
from shoutabout.streaming import energy_silence
from test_local_dictation import START, STOP, Harness
from test_mic import Factory, no_privacy
from test_mic import speech as mic_speech

SR = 16_000


def speech(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


TALK = np.concatenate([speech(16), np.zeros(SR), speech(16), np.zeros(SR), speech(5)])


class CountingEngine(FakeEngine):
    """ "Sentence k." for its k-th transcription."""

    def __init__(self) -> None:
        super().__init__()
        self._mutex = threading.Lock()

    def transcribe(self, audio, hints):
        with self._mutex:
            self.calls.append((len(audio), hints))
            return f"Sentence {len(self.calls)}."


@pytest.fixture(autouse=True)
def loudness_vad(monkeypatch):
    monkeypatch.setattr(streaming, "_default", energy_silence)  # no Silero model in tests


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.005)


def harness(audio: np.ndarray = TALK) -> Harness:
    h = Harness(engine=CountingEngine(), audio=audio)
    h.pipeline.recent = Recent()
    assert h.dictation.start()
    return h


def speak(h: Harness, audio: np.ndarray) -> None:
    """What the microphone does while recording: hand over each block."""
    recorder = h.recorders[-1]
    assert recorder.on_audio is not None
    for i in range(0, len(audio), 480):  # 30 ms blocks
        recorder.on_audio(audio[i : i + 480])


def test_long_dictation_is_transcribed_while_talking():
    h = harness()
    h.send(START)
    session = h.dictation._session
    speak(h, TALK)
    assert session.wait_idle(5)
    assert len(h.pipeline.engine.calls) == 2  # two stretches done before "stop"
    h.send(STOP)
    h.wait_overlay("done")
    assert h.injector.pasted == ["Sentence 1. Sentence 2. Sentence 3."]
    tail = h.pipeline.engine.calls[2][0]
    assert tail == len(TALK) - session.stretches[-1].end  # only the tail at "stop"
    (item,) = h.pipeline.recent.items()
    assert (item.route, item.pasted) == ("local", True)
    h.wait_state(DictationState.READY)
    h.dictation.stop()


def test_the_recording_is_what_counts():
    """Blocks the stream missed (or a recorder that never fed it) are added at "stop"."""
    h = harness()
    h.send(START)
    session = h.dictation._session
    speak(h, TALK[: 20 * SR])  # the rest never reached the stream
    assert session.wait_idle(5) and len(session.stretches) == 1
    h.send(STOP)
    h.wait_overlay("done")
    assert h.injector.pasted == ["Sentence 1. Sentence 2."]
    assert sum(n for n, _h in h.pipeline.engine.calls) == len(TALK)
    h.dictation.stop()


def test_escape_throws_the_stream_away():
    h = harness()
    h.send(START)
    session = h.dictation._session
    speak(h, TALK[: 20 * SR])
    assert session.wait_idle(5)
    h.send(HotkeyEvent(Action.CANCEL, "escape"))
    assert session.state == streaming.CANCELLED and session.received_s == 0
    assert h.dictation._session is None
    time.sleep(0.05)
    assert h.injector.pasted == [] and h.pipeline.recent.items() == []
    assert h.dictation.state == DictationState.READY
    h.dictation.stop()


def test_stopping_or_a_slip_of_the_finger_throws_it_away():
    h = harness(audio=speech(0.1))
    h.send(START)
    session = h.dictation._session
    h.send(STOP)  # too short
    assert session.state == streaming.CANCELLED
    h.send(START)
    session = h.dictation._session
    h.dictation.stop()  # Pause / Quit while recording
    assert session.state == streaming.CANCELLED
    assert h.injector.pasted == []


def test_streaming_failure_falls_back_to_the_whole_recording(monkeypatch):
    def broken(self, **_kw):
        raise RuntimeError("stream bug")

    monkeypatch.setattr(streaming.StreamSession, "finish", broken)
    h = harness()
    h.send(START)
    speak(h, TALK)
    h.send(STOP)
    h.wait_overlay("done")
    (pasted,) = h.injector.pasted  # pasted exactly once
    assert h.pipeline.engine.calls[-1][0] == len(TALK)  # the whole recording again
    assert pasted.startswith("Sentence")
    h.dictation.stop()


def test_without_streaming_support_it_transcribes_at_the_end():
    h = harness()
    h.dictation._pipeline = lambda: _NoStreams(h.pipeline)
    h.send(START)
    assert h.recorders[-1].on_audio is None
    h.send(STOP)
    h.wait_overlay("done")
    assert h.injector.pasted == ["Sentence 1."]
    h.dictation.stop()


class _NoStreams:
    """A pipeline without open_stream (an older wrapper): process_audio only."""

    def __init__(self, pipeline) -> None:
        self.process_audio = pipeline.process_audio


def test_microphone_hands_over_16k_blocks():
    factory = Factory()
    got: list[np.ndarray] = []
    rec = MicRecorder(stream_factory=factory, on_audio=got.append, privacy_reader=no_privacy)
    rec.start()
    factory.streams[0].feed(mic_speech(1.0))
    audio = rec.stop()
    assert len(got) > 10 and np.array_equal(np.concatenate(got), audio)


def test_microphone_at_another_rate_feeds_nothing():
    factory = Factory(refuse_16k=True, device_rate=48_000)
    got: list[np.ndarray] = []
    rec = MicRecorder(stream_factory=factory, on_audio=got.append, privacy_reader=no_privacy)
    rec.start()
    factory.streams[0].feed(mic_speech(1.0, rate=48_000))
    assert len(rec.stop()) > 0 and got == []  # resampled at stop; the finish adds it all


def test_a_broken_audio_callback_never_breaks_recording():
    factory = Factory()

    def boom(_block):
        raise RuntimeError("full")

    rec = MicRecorder(stream_factory=factory, on_audio=boom, privacy_reader=no_privacy)
    rec.start()
    factory.streams[0].feed(mic_speech(0.5))
    assert len(rec.stop()) == SAMPLE_RATE // 2
