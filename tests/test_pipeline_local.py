"""Pipeline v2: samples from the PC's microphone, the shared busy lock, cleanup model."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import FakeCleaner, FakeEngine, FakeInjector, FakeVocabStore, fake_decode
from shoutabout.config import Config
from shoutabout.engine.base import EngineError
from shoutabout.history import HistoryLog
from shoutabout.models import HubStatus
from shoutabout.pipeline import PAUSED_WARNING, Pipeline
from shoutabout.state import HubState

AUDIO = Path("unused.m4a")


def make(*, engine=None, cleaner="default", config=None, history=None, decode=fake_decode):
    return Pipeline(
        config=config or Config(),
        engine=engine or FakeEngine(text="hello better shell"),
        vocab=FakeVocabStore(),
        cleaner=FakeCleaner() if cleaner == "default" else cleaner,
        injector=FakeInjector(),
        state=HubState(),
        history=history,
        decode=decode,
    )


def test_process_audio_skips_decoding_and_marks_the_route():
    def no_decode(path, max_seconds=None):
        raise AssertionError("samples from the microphone are never decoded")

    p = make(decode=no_decode)
    r = p.process_audio([0.0] * 32_000)
    assert (r.route, r.target, r.pasted) == ("local", "pc", True)
    assert r.audio_seconds == pytest.approx(2.0)
    assert r.timings.decode == 0
    assert p.injector.pasted == ["hello better shell"]
    assert p.state.status == HubStatus.IDLE


def test_process_audio_follows_pause_and_writes_history(tmp_path):
    hist = HistoryLog(tmp_path / "h.jsonl", tmp_path / "audio", keep_audio=True)
    p = make(history=hist)
    p.state.set_paused(True)
    r = p.process_audio([0.0] * 16_000)
    assert r.pasted is False and r.warning == PAUSED_WARNING
    record = json.loads((tmp_path / "h.jsonl").read_text("utf-8"))
    assert record["route"] == "local" and record["audio_file"] is None


def test_process_audio_engine_error_sets_error_status():
    p = make(engine=FakeEngine(fail=EngineError("CUDA died")))
    with pytest.raises(EngineError):
        p.process_audio([0.0] * 16_000)
    assert p.state.status == HubStatus.ERROR


def test_pipeline_uses_the_hub_busy_lock():
    p = make()
    assert p._lock is p.state.busy
    done = threading.Event()

    def run():
        p.process_audio([0.0] * 16_000)
        done.set()

    with p.state.busy:
        threading.Thread(target=run, daemon=True).start()
        assert not done.wait(0.1)  # waits while someone else holds the lock
    assert done.wait(2)


def test_state_without_a_busy_lock_gets_its_own():
    p = Pipeline(
        config=Config(),
        engine=FakeEngine(),
        vocab=FakeVocabStore(),
        cleaner=None,
        injector=FakeInjector(),
        state=SimpleNamespace(paused=False, set_status=lambda *a: None),
        decode=fake_decode,
    )
    assert p.process_file(AUDIO, target="phone", route="lan").text


def test_llm_model_comes_from_the_cleaner_in_use():
    cleaner = FakeCleaner()
    cleaner.cfg = SimpleNamespace(model="granite4:micro")
    r = make(cleaner=cleaner).process_file(AUDIO, target="phone", route="lan")
    assert r.extra["llm_model"] == "granite4:micro"


def test_auto_cleanup_runs_only_when_a_cleaner_exists():
    cfg = Config()
    assert cfg.cleanup.enabled == "auto"
    r = make(config=cfg).process_file(AUDIO, target="phone", route="lan")
    assert r.cleanup.status == "applied"
    r = make(config=cfg, cleaner=None).process_file(AUDIO, target="phone", route="lan")
    assert r.cleanup.status == "disabled"
