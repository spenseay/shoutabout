"""Streamed dictations (shoutabout.streaming): where stretches are cut, the background
work, finish (once), cancel, the lock and status rules, and the registry.

The real Pipeline runs with fake engine / cleaner / injector, so delivery (paste rules,
Recent) is the one /transcribe uses. Audio is synthetic: a tone for speech, zeros for
pauses, with the loudness detector (no Silero model needed).
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from fakes import FakeCleaner, FakeEngine, FakeInjector, FakeVocabStore, fake_decode
from shoutabout import streaming
from shoutabout.config import Config
from shoutabout.engine.base import EngineError, SttHints
from shoutabout.engine.faster_whisper_engine import echoes_context, filter_segments
from shoutabout.models import CleanupResult, HubStatus
from shoutabout.pipeline import NO_SPEECH_WARNING, PAUSED_WARNING, Pipeline, format_log_line
from shoutabout.recent import Recent
from shoutabout.state import HubState
from shoutabout.streaming import (
    FRAME,
    BadChunk,
    MissingChunk,
    StreamGone,
    StreamRegistry,
    StreamTooLong,
    TooManyStreams,
    energy_silence,
    find_cut,
    join_pieces,
    merge_cleanups,
    pcm_to_float,
    tail_context,
)

SR = 16_000


def speech(seconds: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def pause(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def join(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts).astype(np.float32)


def cut_at(audio: np.ndarray) -> float | None:
    cut = find_cut(audio, energy_silence(audio))
    return None if cut is None else cut / SR


# Two background stretches (pauses at 16-17 s and 33-34 s) and a 5 s tail.
TALK = join(speech(16), pause(1), speech(16), pause(1), speech(5))


def blocks(audio: np.ndarray, seconds: float = 2.0):
    step = int(seconds * SR)
    for i in range(0, len(audio), step):
        yield audio[i : i + step]


def pcm(audio: np.ndarray) -> bytes:
    return (audio * 32767).astype("<i2").tobytes()


class StretchEngine(FakeEngine):
    """Says "Sentence k." for its k-th transcription; notes the hub status each time."""

    def __init__(self, state: HubState, *, delay: float = 0.0, fail: int = 0, text=None):
        super().__init__()
        self.state = state
        self.delay = delay
        self.fail_next = fail
        self.reply = text
        self.statuses: list[HubStatus] = []
        self.started = threading.Event()
        self._mutex = threading.Lock()

    def transcribe(self, audio, hints):
        self.started.set()
        with self._mutex:
            if self.fail_next:
                self.fail_next -= 1
                raise EngineError("CUDA hiccup")
            self.calls.append((len(audio), hints))
            self.statuses.append(self.state.status)
            k = len(self.calls)
        if self.delay:
            time.sleep(self.delay)
        return self.reply if self.reply is not None else f"Sentence {k}."


def make(*, delay=0.0, fail=0, text=None, cleaner="default", recent=None, config=None):
    state = HubState()
    state.set_status(HubStatus.IDLE)
    return Pipeline(
        config=config or Config(),
        engine=StretchEngine(state, delay=delay, fail=fail, text=text),
        vocab=FakeVocabStore(),
        cleaner=FakeCleaner() if cleaner == "default" else cleaner,
        injector=FakeInjector(),
        state=state,
        recent=recent,
        decode=fake_decode,
    )


def open_stream(p: Pipeline, **kw):
    kw.setdefault("detector", energy_silence)
    return p.open_stream(**kw)


def talk(session, audio: np.ndarray = TALK) -> None:
    for block in blocks(audio):
        session.feed(block)
    assert session.wait_idle(10)


# ------------------------------------------------------------------ cutting


def test_no_cut_before_the_minimum():
    assert cut_at(join(speech(5), pause(1), speech(8))) is None  # 14 s


def test_cuts_in_the_middle_of_a_good_pause():
    assert cut_at(join(speech(16), pause(1), speech(3))) == pytest.approx(16.5, abs=0.04)


def test_a_pause_before_the_minimum_is_not_used():
    assert cut_at(join(speech(10), pause(1), speech(9))) is None  # 20 s, waits


def test_a_short_pause_only_after_the_lookahead():
    audio = join(speech(17), pause(0.4), speech(3.6))  # 21 s
    assert cut_at(audio) is None
    longer = join(audio, speech(3))  # 24 s >= 15 + 8
    assert cut_at(longer) == pytest.approx(17.2, abs=0.04)


def test_prefers_the_longest_pause():
    audio = join(speech(16), pause(0.7), speech(2), pause(1.5), speech(1))
    assert cut_at(audio) == pytest.approx(16 + 0.7 + 2 + 0.75, abs=0.04)


def test_long_silence_across_the_minimum_cuts_at_the_minimum():
    assert cut_at(join(speech(10), pause(10))) == pytest.approx(15.0, abs=0.04)


def test_no_pause_by_the_maximum_cuts_at_the_quietest_moment():
    quiet = (speech(0.2) * 0.02).astype(np.float32)
    audio = join(speech(20), quiet, speech(8))
    assert cut_at(audio[: int(27 * SR)]) is None
    cut = cut_at(audio)
    assert cut is not None and 20.0 <= cut <= 20.2


def test_never_cuts_in_speech():
    rng = np.random.default_rng(7)
    for _ in range(25):
        parts, kinds = [], []
        while sum(len(p) for p in parts) < 40 * SR:
            parts.append(speech(float(rng.uniform(0.5, 6))))
            kinds.append("s")
            parts.append(pause(float(rng.uniform(0.05, 1.2))))
            kinds.append("p")
        audio = join(*parts)[: int(28 * SR)]
        cut = find_cut(audio, energy_silence(audio))
        if cut is None:
            continue
        around = audio[max(0, cut - 800) : cut + 800]  # 50 ms each side
        if not np.all(around == 0):
            # only allowed when no silence existed at all (the quietest moment)
            assert not np.any(energy_silence(audio)[15 * SR // FRAME : 28 * SR // FRAME])


def test_silero_detector_falls_back_to_loudness(monkeypatch):
    session = make().open_stream(detector=lambda a: (_ for _ in ()).throw(RuntimeError("onnx")))
    out = session._silence(join(speech(1), pause(1)))
    assert out.dtype == bool and out[-5:].all() and not out[:5].any()
    session.cancel()


# ------------------------------------------------------------------ text


@pytest.mark.parametrize(
    ("parts", "joined"),
    [
        (["Hello there.", "And then we left."], "Hello there. And then we left."),
        (["I went to the store,", "And then I left."], "I went to the store, and then I left."),
        (["We met", "The team"], "We met the team"),
        (["We met", "Claude today."], "We met Claude today."),
        (["It was", "I think"], "It was I think"),
        (["", "  one  two ", "", "three"], "one two three"),
        (["He said “hi.”", "So we went"], "He said “hi.” So we went"),
        ([], ""),
    ],
)
def test_join_pieces(parts, joined):
    assert join_pieces(parts) == joined


def test_tail_context():
    assert tail_context("") is None
    assert tail_context("Hi. This is the very last sentence of eight words.") == (
        "This is the very last sentence of eight words."
    )
    assert tail_context("One two. Three four. Five six.") == "One two. Three four. Five six."
    long = " ".join(f"w{i}" for i in range(50))
    assert tail_context(long) == " ".join(f"w{i}" for i in range(20, 50))


def test_merge_cleanups():
    a = CleanupResult("Hello.", "applied", duration_ms=5)
    s = CleanupResult("", "skipped", "empty transcript")
    f = CleanupResult("raw words", "fallback", "timeout after 2 s", 2000)
    d = CleanupResult("plain", "disabled")
    assert merge_cleanups([a, s]).status == "applied"
    merged = merge_cleanups([a, f])
    assert (merged.status, merged.reason, merged.text) == (
        "fallback",
        "timeout after 2 s",
        "Hello. raw words",
    )
    assert merged.duration_ms == 2005
    assert merge_cleanups([d, d]).status == "disabled"
    assert merge_cleanups([s, s]).status == "skipped"


def test_pcm_to_float():
    out = pcm_to_float(np.array([0, 16384, -32768, 32767], "<i2").tobytes())
    assert out.dtype == np.float32
    assert out.tolist() == pytest.approx([0, 0.5, -1.0, 32767 / 32768])
    for bad in (b"", b"\x01", b"\x01\x02\x03"):
        with pytest.raises(BadChunk):
            pcm_to_float(bad)


def test_context_echo_is_dropped_only_at_the_start():
    from types import SimpleNamespace as Seg

    hints = SttHints(context="we shipped the new build to everyone today")
    assert echoes_context("the new build to everyone", hints.context)
    assert not echoes_context("the new build", hints.context)  # under four words
    segs = [Seg(text=" the new build to everyone"), Seg(text=" Then lunch.")]
    assert filter_segments(segs, hints) == "Then lunch."
    segs = [Seg(text=" Then lunch."), Seg(text=" the new build to everyone")]
    assert filter_segments(segs, hints) == "Then lunch. the new build to everyone"


def test_engine_puts_the_context_after_the_vocab_prompt():
    from shoutabout.config import SttConfig
    from shoutabout.engine.faster_whisper_engine import FasterWhisperEngine

    engine = FasterWhisperEngine(SttConfig(device="cpu", model="tiny"))
    opts = engine.decode_options(SttHints(initial_prompt="In this dictation I mention A.",
                                          hotwords="A", context="We left early."))  # fmt: skip
    assert opts["initial_prompt"] == "In this dictation I mention A. We left early."
    assert opts["hotwords"] == "A"
    assert engine.decode_options(SttHints(context="Only this."))["initial_prompt"] == "Only this."
    assert "initial_prompt" not in engine.decode_options(SttHints())


# ------------------------------------------------------------------ the session


def test_stretches_in_the_background_and_only_the_tail_at_finish():
    p = make()
    s = open_stream(p, target="pc", route="local")
    talk(s)
    cuts = [x.end / SR for x in s.stretches]
    assert cuts == [pytest.approx(16.5, abs=0.05), pytest.approx(33.5, abs=0.05)]
    assert p.engine.statuses == [HubStatus.IDLE, HubStatus.IDLE]  # not while talking
    assert len(p.cleaner.calls) == 2  # each stretch cleaned as soon as it's transcribed
    r = s.finish()
    assert r.text == "Sentence 1. Sentence 2. Sentence 3."
    assert r.raw_text == r.text
    assert p.engine.calls[2][0] == len(TALK) - s.stretches[-1].end  # only the tail
    assert p.engine.statuses[2] == HubStatus.TRANSCRIBING
    assert p.state.status == HubStatus.IDLE
    assert p.injector.pasted == [r.text]
    assert (r.target, r.route, r.pasted, r.warning) == ("pc", "local", True, None)
    assert r.cleanup.status == "applied"
    assert r.audio_seconds == pytest.approx(len(TALK) / SR)
    assert r.extra["streamed"] is True and r.extra["stretches"] == 2
    assert r.extra["stt_model"] == "fake-engine"
    assert r.timings.total >= r.timings.stt
    line = format_log_line(r)
    assert "streamed=true chunks=0 stretches=2 bg_stt=" in line and "Sentence" not in line


def test_each_stretch_gets_the_vocab_hints_and_the_previous_words():
    p = make()
    s = open_stream(p)
    talk(s)
    s.finish()
    hints = [h for _n, h in p.engine.calls]
    assert all(h.hotwords == "Better Shell, Claude Code" for h in hints)
    assert [h.context for h in hints] == [None, "Sentence 1.", "Sentence 2."]


def test_without_context(monkeypatch):
    monkeypatch.setattr(streaming, "USE_CONTEXT", False)
    p = make()
    s = open_stream(p)
    talk(s)
    s.finish()
    assert [h.context for _n, h in p.engine.calls] == [None, None, None]


def test_cleanup_runs_per_stretch_and_merges():
    p = make(cleaner=FakeCleaner(transform=str.upper))
    s = open_stream(p)
    talk(s)
    r = s.finish()
    assert r.text == "SENTENCE 1. SENTENCE 2. SENTENCE 3."
    assert r.raw_text == "Sentence 1. Sentence 2. Sentence 3."
    assert [c[0] for c in p.cleaner.calls] == ["Sentence 1.", "Sentence 2.", "Sentence 3."]
    assert r.cleanup.status == "applied"


def test_a_stretch_cleanup_fallback_shows():
    class OneFallback(FakeCleaner):
        def clean(self, raw_text, terms):
            result = super().clean(raw_text, terms)
            if raw_text == "Sentence 2.":
                return CleanupResult(raw_text, "fallback", "timeout after 2 s")
            return result

    p = make(cleaner=OneFallback())
    s = open_stream(p)
    talk(s)
    r = s.finish()
    assert (r.cleanup.status, r.cleanup.reason) == ("fallback", "timeout after 2 s")


def test_short_dictation_is_all_tail():
    p = make()
    s = open_stream(p)
    talk(s, speech(6))
    assert s.stretches == []
    r = s.finish()
    assert r.text == "Sentence 1." and r.extra["stretches"] == 0
    assert p.engine.calls[0][0] == 6 * SR


def test_finish_is_idempotent_and_delivers_once():
    p = make()
    s = open_stream(p)
    talk(s)
    first, repeated = s.finish_detailed()
    assert repeated is False
    again, repeated = s.finish_detailed()
    assert again is first and repeated is True
    assert s.finish() is first
    assert p.injector.pasted == [first.text]
    assert len(p.engine.calls) == 3


def test_concurrent_finishes_deliver_once():
    p = make(delay=0.05)
    s = open_stream(p)
    talk(s)
    results = []
    threads = [threading.Thread(target=lambda: results.append(s.finish())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 3 and all(r is results[0] for r in results)
    assert p.injector.pasted == [results[0].text]


def test_finish_drops_the_audio():
    p = make()
    s = open_stream(p)
    talk(s)
    s.finish()
    assert s.received_s == 0 and s.state == streaming.FINISHED


def test_paused_target_phone_and_silence_follow_the_delivery_rules():
    p = make()
    p.state.set_paused(True)
    s = open_stream(p)
    talk(s, speech(4))
    r = s.finish()
    assert (r.pasted, r.warning) == (False, PAUSED_WARNING)
    p.state.set_paused(False)
    s = open_stream(p, target="phone")
    talk(s, speech(4))
    r = s.finish()
    assert (r.pasted, r.warning, r.target) == (False, None, "phone")
    q = make(text="")
    s = open_stream(q)
    talk(s)
    r = s.finish()
    assert (r.text, r.pasted, r.warning, r.cleanup.status) == ("", False, NO_SPEECH_WARNING,
                                                               "skipped")  # fmt: skip
    assert p.injector.pasted == [] and q.injector.pasted == []


def test_recent_keeps_a_streamed_dictation():
    recent = Recent()
    p = make(recent=recent)
    s = open_stream(p, extra={"source": "Sam's iPhone"})
    talk(s, speech(4))
    s.finish()
    (item,) = recent.items()
    assert (item.text, item.route, item.source) == ("Sentence 1.", "local", "Sam's iPhone")


def test_cancel_discards_everything():
    p = make()
    s = open_stream(p)
    talk(s)
    assert s.cancel() is True
    assert s.state == streaming.CANCELLED and s.received_s == 0 and s.stretches == []
    s.feed(speech(1))
    assert s.received_s == 0
    with pytest.raises(StreamGone):
        s.finish()
    with pytest.raises(StreamGone):
        s.append(0, pcm(speech(1)))
    s._worker.join(2)
    assert not s._worker.is_alive()
    assert p.injector.pasted == []
    assert s.cancel() is False


def test_cancel_while_a_stretch_is_transcribing():
    p = make(delay=0.2)
    s = open_stream(p)
    for block in blocks(TALK):
        s.feed(block)
    assert p.engine.started.wait(2)
    s.cancel()
    s._worker.join(2)
    assert not s._worker.is_alive() and s.stretches == []
    assert p.state.status == HubStatus.IDLE and p.injector.pasted == []


def test_phone_chunks_in_order_retries_and_gaps():
    p = make()
    s = open_stream(p, max_seconds=10)
    one = pcm(speech(2))
    assert s.append(0, one) == (1, 2.0)
    assert s.append(0, one) == (1, 2.0)  # a retry: ignored
    with pytest.raises(MissingChunk) as gap:
        s.append(2, one)
    assert gap.value.next_seq == 1 and gap.value.status == 409
    assert s.append(1, one) == (2, 4.0)
    with pytest.raises(MissingChunk) as missing:
        s.finish(chunks=3)
    assert missing.value.next_seq == 2
    for seq in (2, 3, 4):
        s.append(seq, one)
    with pytest.raises(StreamTooLong):
        s.append(5, pcm(speech(0.1)))  # would pass 10 s
    r = s.finish(chunks=5)
    assert r.extra["chunks"] == 5 and r.audio_seconds == pytest.approx(10.0)
    with pytest.raises(StreamGone):
        s.append(5, one)


def test_background_failure_leaves_everything_for_finish():
    p = make(fail=1)
    s = open_stream(p)
    talk(s)
    assert s.stretches == []  # the first stretch failed; no more background work
    r = s.finish()
    assert r.text == "Sentence 1."
    assert p.engine.calls[0][0] == len(TALK)  # the whole dictation at finish
    assert p.state.status == HubStatus.IDLE


def test_engine_failure_at_finish_is_kept_for_retries():
    p = make()
    s = open_stream(p)
    talk(s, speech(4))
    p.engine.fail_next = 5
    with pytest.raises(EngineError):
        s.finish()
    assert p.state.status == HubStatus.ERROR and p.injector.pasted == []
    with pytest.raises(EngineError):
        s.finish()  # the same outcome, not a second try
    assert s.delivered is False


def test_the_recording_wins_over_what_was_fed():
    p = make()
    s = open_stream(p)
    talk(s, TALK[: 20 * SR])
    assert len(s.stretches) == 1
    r = s.finish(audio=TALK)  # the microphone's whole recording
    assert r.audio_seconds == pytest.approx(len(TALK) / SR)
    assert p.engine.calls[-1][0] == len(TALK) - s.stretches[0].end

    q = make()
    s = open_stream(q)
    talk(s, speech(20, freq=330))  # not what was recorded: start over
    r = s.finish(audio=TALK)
    assert r.extra["stretches"] == 0 and q.engine.calls[-1][0] == len(TALK)


def test_background_gives_way_to_a_waiting_upload():
    """A phone upload arriving while stretches queue up waits for one stretch at most."""
    p = make(delay=0.15)
    s = open_stream(p)
    backlog = join(*(join(speech(16), pause(1)) for _ in range(4)))
    for block in blocks(backlog):
        s.feed(block)
    assert p.engine.started.wait(2)  # stretch 1 is transcribing
    upload = threading.Thread(
        target=p.process_audio, args=(np.zeros(SR, np.float32),), kwargs={"target": "phone"}
    )
    upload.start()
    upload.join(5)
    lengths = [n for n, _h in p.engine.calls]
    assert lengths[1] == SR  # right after the stretch that was running
    assert s.wait_idle(10) and len(s.stretches) >= 3
    s.cancel()


def test_status_never_flips_while_talking():
    p = make()
    seen = []
    p.state.subscribe(lambda st: seen.append(st.status))
    s = open_stream(p)
    talk(s)
    assert len(s.stretches) == 2 and seen == []
    s.finish()
    assert seen == [HubStatus.TRANSCRIBING, HubStatus.IDLE]


def test_microphone_limit():
    p = make()
    s = open_stream(p, max_seconds=3)
    s.feed(speech(2))
    s.feed(speech(2))
    assert s.received_s == pytest.approx(3.0)
    s.cancel()


# ------------------------------------------------------------------ registry


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def registry(**kw) -> tuple[StreamRegistry, Clock]:
    clock = Clock()
    return StreamRegistry(clock=clock, detector=energy_silence, **kw), clock


def test_registry_ids_owner_and_limit():
    reg, _clock = registry()
    p = make()
    a = reg.start(p, target="pc", route="lan", owner="hub")
    b = reg.start(p, target="pc", route="lan", owner="hub")
    assert len(a.id) == 32 and a.id != b.id
    assert all(c.isalnum() or c in "-_" for c in a.id)
    with pytest.raises(TooManyStreams):
        reg.start(p, target="pc", route="lan", owner="hub")
    assert reg.get(a.id, "hub") is a
    with pytest.raises(StreamGone):
        reg.get(a.id, "device:other")
    a.append(0, pcm(speech(1)))
    a.finish()  # finished streams don't count
    c = reg.start(p, target="phone", route="lan", owner="hub")
    assert reg.cancel(b.id, "hub") and reg.cancel(c.id, "hub")
    assert not reg.cancel(b.id, "hub") and not reg.cancel("nope", "hub")
    with pytest.raises(StreamGone):
        reg.get(b.id, "hub")


def test_registry_expires_idle_streams_and_forgets_finished_ones():
    reg, clock = registry()
    p = make()
    idle = reg.start(p, target="pc", route="lan", owner="hub")
    done = reg.start(p, target="pc", route="lan", owner="hub")
    done.append(0, pcm(speech(1)))
    done.finish()
    clock.now += 59
    reg.get(idle.id, "hub")  # a request keeps it alive
    clock.now += 59
    assert reg.get(done.id, "hub").state == streaming.FINISHED  # kept ~2 minutes
    clock.now += 61
    with pytest.raises(StreamGone):
        reg.get(idle.id, "hub")
    assert idle.state == streaming.CANCELLED and idle.received_s == 0
    clock.now += 10
    with pytest.raises(StreamGone):
        reg.get(done.id, "hub")
    assert len(reg) == 0


def test_an_idle_stream_expires_by_itself(monkeypatch):
    monkeypatch.setattr(streaming, "WAIT_S", 0.01)
    reg, clock = registry()
    s = reg.start(make(), target="pc", route="lan", owner="hub")
    clock.now += 61
    s._worker.join(2)
    assert s.state == streaming.CANCELLED


def test_registry_close_cancels_open_streams():
    reg, _clock = registry()
    s = reg.start(make(), target="pc", route="lan", owner="hub")
    reg.close()
    assert s.state == streaming.CANCELLED and len(reg) == 0
