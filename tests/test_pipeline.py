from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from fakes import FakeCleaner, FakeEngine, FakeInjector, FakeVocabStore, fake_decode
from shoutabout.config import Config
from shoutabout.engine.base import EngineError, SttHints
from shoutabout.models import CleanupResult, HubStatus, PasteResult
from shoutabout.pipeline import (
    NO_SPEECH_WARNING,
    PASTE_ERROR_WARNING,
    PAUSED_WARNING,
    Pipeline,
    format_log_line,
)
from shoutabout.recent import Recent
from shoutabout.state import HubState

AUDIO = Path("unused.m4a")


def make(
    *,
    text="hello better shell",
    engine=None,
    cleaner="default",
    injector=None,
    config=None,
    recent=None,
    vocab=None,
    decode=fake_decode,
):
    cfg = config or Config()
    state = HubState()
    p = Pipeline(
        config=cfg,
        engine=engine or FakeEngine(text=text),
        vocab=vocab or FakeVocabStore(),
        cleaner=FakeCleaner() if cleaner == "default" else cleaner,
        injector=injector or FakeInjector(),
        state=state,
        recent=recent,
        decode=decode,
    )
    return p


def test_happy_path_pc():
    p = make()
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert r.text == "hello better shell"
    assert r.raw_text == "hello better shell"
    assert r.pasted is True
    assert r.warning is None
    assert r.cleanup.status == "applied"
    assert r.route == "lan"
    assert r.target == "pc"
    assert r.audio_seconds == pytest.approx(1.0)
    assert len(r.request_id) == 12
    assert set(r.timings.as_dict()) == {"decode", "stt", "cleanup", "paste", "total"}
    assert r.timings.total >= r.timings.stt
    assert p.injector.pasted == ["hello better shell"]
    assert p.state.status == HubStatus.IDLE
    assert r.extra["stt_model"] == "fake-engine"
    assert r.extra["llm_model"] == Config().cleanup.model


def test_request_ids_unique():
    p = make()
    ids = {p.process_file(AUDIO, target="phone", route="lan").request_id for _ in range(20)}
    assert len(ids) == 20


def test_vocab_hints_and_terms_flow_through():
    vocab = FakeVocabStore(terms=("Better Shell", "Anthropic"))
    p = make(vocab=vocab)
    p.process_file(AUDIO, target="phone", route="lan")
    _, hints = p.engine.calls[0]
    assert hints == SttHints(
        initial_prompt="Better Shell, Anthropic", hotwords="Better Shell, Anthropic"
    )
    assert p.cleaner.calls == [("hello better shell", ("Better Shell", "Anthropic"))]


def test_decode_gets_max_audio_seconds():
    seen = []

    def decode(path, max_seconds=None):
        seen.append((path, max_seconds))
        return fake_decode(path, max_seconds)

    cfg = Config()
    cfg.server.max_audio_seconds = 42
    make(decode=decode, config=cfg).process_file(AUDIO, target="phone", route="lan")
    assert seen == [(AUDIO, 42)]


def test_trailing_space_only_in_paste():
    cfg = Config()
    cfg.paste.trailing_space = True
    p = make(config=cfg)
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert p.injector.pasted == ["hello better shell "]
    assert r.text == "hello better shell"
    assert r.to_response()["text"] == "hello better shell"


def test_no_trailing_space_by_default():
    p = make()
    p.process_file(AUDIO, target="pc", route="lan")
    assert p.injector.pasted == ["hello better shell"]


def test_paused_does_not_paste():
    p = make()
    p.state.set_paused(True)
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert r.pasted is False
    assert r.warning == PAUSED_WARNING == "Hub is paused — not pasted on the PC."
    assert r.text == "hello better shell"
    assert p.injector.pasted == []


def test_paused_phone_has_no_warning():
    p = make()
    p.state.set_paused(True)
    r = p.process_file(AUDIO, target="phone", route="lan")
    assert (r.pasted, r.warning) == (False, None)


def test_empty_text_no_paste_and_skipped_cleanup():
    p = make(text="   \n ")
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert r.text == "" and r.raw_text == ""
    assert r.pasted is False
    assert r.warning == NO_SPEECH_WARNING == "No speech detected."
    assert r.cleanup.status == "skipped"
    assert p.cleaner.calls == []
    assert p.injector.pasted == []


def test_empty_text_phone():
    r = make(text="").process_file(AUDIO, target="phone", route="lan")
    # Phone mode also gets the warning, so the Shortcut doesn't copy "" to the clipboard.
    assert (r.text, r.pasted, r.warning, r.cleanup.status) == (
        "",
        False,
        NO_SPEECH_WARNING,
        "skipped",
    )


def test_cleanup_output_emptied_means_no_paste():
    p = make(cleaner=FakeCleaner(transform=lambda s: " \n "))
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert r.text == ""
    assert r.warning == NO_SPEECH_WARNING
    assert p.injector.pasted == []


def test_phone_target_never_pastes():
    p = make()
    r = p.process_file(AUDIO, target="phone", route="tunnel")
    assert (r.pasted, r.warning) == (False, None)
    assert r.timings.paste == 0
    assert p.injector.pasted == []


def test_deliver_false_never_pastes():
    p = make()
    r = p.process_file(AUDIO, target="pc", route="cli", deliver=False)
    assert (r.pasted, r.warning) == (False, None)
    assert p.injector.pasted == []


@pytest.mark.parametrize(
    "raw",
    [
        "line one\nline two\r\n",
        "line one\r\nline two",
        "line one line\ttwo",
        "line one ​line\x00 two\x1b",
    ],
)
def test_raw_newlines_normalised(raw):
    p = make(text=raw, cleaner=None)
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert "\n" not in r.raw_text and "\r" not in r.raw_text
    assert "\n" not in r.text
    assert p.injector.pasted == [r.text]
    for ch in "\n\r\t  \x00\x1b​":
        assert ch not in p.injector.pasted[0]


def test_cleaner_output_normalised():
    p = make(cleaner=FakeCleaner(transform=lambda s: "Hello,\nBetter Shell.\r\n "))
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert r.text == "Hello, Better Shell."
    assert p.injector.pasted == ["Hello, Better Shell."]


def test_cleanup_disabled_by_config():
    cfg = Config()
    cfg.cleanup.enabled = False
    p = make(config=cfg)
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert r.cleanup.status == "disabled"
    assert p.cleaner.calls == []
    assert r.extra["llm_model"] is None


def test_cleanup_disabled_by_argument():
    p = make()
    r = p.process_file(AUDIO, target="pc", route="lan", cleanup=False)
    assert r.cleanup.status == "disabled"
    assert p.cleaner.calls == []


def test_cleanup_none_cleaner():
    r = make(cleaner=None).process_file(AUDIO, target="pc", route="lan")
    assert r.cleanup.status == "disabled"
    assert r.text == r.raw_text


def test_cleanup_fallback_status_passthrough():
    cleaner = FakeCleaner(transform=lambda s: s, status="fallback")
    r = make(cleaner=cleaner).process_file(AUDIO, target="pc", route="lan")
    assert r.cleanup.status == "fallback"
    assert r.cleanup.fell_back


def test_cleaner_exception_falls_back_to_raw():
    class Boom:
        def clean(self, raw, terms):
            raise RuntimeError("ollama exploded")

    r = make(cleaner=Boom()).process_file(AUDIO, target="pc", route="lan")
    assert r.cleanup.status == "fallback"
    assert r.text == "hello better shell"
    assert r.pasted is True


def test_cleanup_applied_changes_text():
    cleaner = FakeCleaner(transform=lambda s: "Hello Better Shell.")
    r = make(cleaner=cleaner).process_file(AUDIO, target="pc", route="lan")
    assert r.raw_text == "hello better shell"
    assert r.text == "Hello Better Shell."


def test_paste_failure_reported():
    injector = FakeInjector(PasteResult(pasted=False, warning="admin window"))
    r = make(injector=injector).process_file(AUDIO, target="pc", route="lan")
    assert (r.pasted, r.warning) == (False, "admin window")


def test_injector_exception_reported():
    class Broken:
        def paste(self, text):
            raise OSError("SendInput failed")

    p = make(injector=Broken())
    r = p.process_file(AUDIO, target="pc", route="lan")
    assert (r.pasted, r.warning) == (False, PASTE_ERROR_WARNING)
    assert p.state.status == HubStatus.IDLE


def test_engine_error_sets_error_state():
    p = make(engine=FakeEngine(fail=EngineError("CUDA died")))
    with pytest.raises(EngineError):
        p.process_file(AUDIO, target="pc", route="lan")
    assert p.state.status == HubStatus.ERROR
    assert p.state.error == "CUDA died"
    assert p.injector.pasted == []


def test_engine_recovers_to_idle_after_error():
    engine = FakeEngine(fail=EngineError("transient"))
    p = make(engine=engine)
    with pytest.raises(EngineError):
        p.process_file(AUDIO, target="pc", route="lan")
    engine.fail = None
    p.process_file(AUDIO, target="pc", route="lan")
    assert p.state.status == HubStatus.IDLE


def test_decode_error_propagates_and_state_idle():
    class AudioError(ValueError):
        pass

    def decode(path, max_seconds=None):
        raise AudioError("bad file")

    p = make(decode=decode)
    with pytest.raises(AudioError):
        p.process_file(AUDIO, target="pc", route="lan")
    assert p.state.status == HubStatus.IDLE
    assert p.engine.calls == []


def test_state_transcribing_during_work():
    seen = []

    class Watching(FakeEngine):
        def transcribe(self, audio, hints):
            seen.append(p.state.status)
            return super().transcribe(audio, hints)

    p = make(engine=Watching(text="x"))
    p.process_file(AUDIO, target="phone", route="lan")
    assert seen == [HubStatus.TRANSCRIBING]
    assert p.state.status == HubStatus.IDLE


def test_vocab_failure_does_not_break_transcription():
    class BadVocab:
        def stt_hints(self, *a):
            raise OSError("disk gone")

        def get(self):
            raise OSError("disk gone")

    p = make(vocab=BadVocab())
    r = p.process_file(AUDIO, target="phone", route="lan")
    assert r.text == "hello better shell"
    assert p.engine.calls[0][1] == SttHints()
    assert p.cleaner.calls == [("hello better shell", ())]


def test_serialised_by_lock():
    class Slow(FakeEngine):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def transcribe(self, audio, hints):
            with self.lock:
                Slow.active += 1
                Slow.max_active = max(Slow.max_active, Slow.active)
            time.sleep(0.02)
            with self.lock:
                Slow.active -= 1
            return "x"

    p = make(engine=Slow())
    threads = [
        threading.Thread(
            target=p.process_file, args=(AUDIO,), kwargs={"target": "pc", "route": "lan"}
        )
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Slow.max_active == 1
    assert p.injector.pasted == ["x"] * 5


def test_recent_keeps_the_result():
    recent = Recent()
    p = make(recent=recent)
    r = p.process_file(AUDIO, target="pc", route="lan")
    (item,) = recent.items()
    assert (item.text, item.route) == (r.text, "lan")


def test_recent_not_added_on_error():
    recent = Recent()
    p = make(recent=recent, engine=FakeEngine(fail=EngineError("x")))
    with pytest.raises(EngineError):
        p.process_file(AUDIO, target="pc", route="lan")
    assert recent.items() == []


def test_format_log_line():
    r = make().process_file(AUDIO, target="pc", route="lan")
    line = format_log_line(r)
    assert line.startswith(f"transcribe id={r.request_id} route=lan target=pc chars=18 ")
    assert "pasted=true cleanup=applied audio_s=1.0 decode=" in line
    assert "hello" not in line


def test_default_decode_is_audio_module(monkeypatch):
    import shoutabout.audio as audio

    p = Pipeline(
        config=Config(),
        engine=FakeEngine(),
        vocab=FakeVocabStore(),
        cleaner=None,
        injector=FakeInjector(),
        state=HubState(),
    )
    assert p._decode is audio.decode_audio


def test_cleanup_result_type_preserved():
    class Reasoned:
        def clean(self, raw, terms):
            return CleanupResult(text=raw, status="fallback", reason="guard: too different")

    r = make(cleaner=Reasoned()).process_file(AUDIO, target="phone", route="lan")
    assert r.cleanup.reason == "guard: too different"
