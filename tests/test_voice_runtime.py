"""The hub's voice runtime: choice, first-run download with progress, late cleanup,
dictation start. Hardware, Ollama and the Hub are faked."""

from __future__ import annotations

import pytest

from shoutabout import hardware, modelpick, modelstore
from shoutabout.config import Config, parse_config
from shoutabout.engine.base import EngineError
from shoutabout.hardware import GpuInfo, HardwareInfo
from shoutabout.state import HubState
from shoutabout.voice_runtime import VoiceRuntime

QWEN = "qwen3:4b-instruct-2507-q4_K_M"
BIG_GPU = HardwareInfo(
    (GpuInfo("NVIDIA GeForce RTX 4080 SUPER", 16.0, True, "591.86", (8, 9), ("float16",)),),
    "Intel Core i7",
    20,
    28,
    64.0,
)
LAPTOP = HardwareInfo((), "Intel Core i5-8250U", 4, 8, 8.0)

OWNER = """
[stt]
model = "large-v3-turbo"
device = "cuda"
compute_type = "float16"
[cleanup]
enabled = true
[dictation]
enabled = false
"""


@pytest.fixture
def fakes(monkeypatch):
    env = {"hw": BIG_GPU, "ollama": [QWEN], "ready": True, "downloads": []}
    monkeypatch.setattr(hardware, "detect", lambda **kw: env["hw"])
    monkeypatch.setattr(modelpick, "ollama_models", lambda url, timeout=1.0: env["ollama"])
    monkeypatch.setattr(modelstore, "is_ready", lambda name, cache_dir=None: env["ready"])

    def download(name, *, progress=None, cancel=None, cache_dir=None):
        env["downloads"].append(name)
        for done in (0, 50, 100):
            progress(done, 100)
        if env.get("fail"):
            raise modelstore.ModelStoreError("Couldn't download the speech model.")
        return name

    monkeypatch.setattr(modelstore, "download", download)
    return env


def test_owner_config_behaves_as_before(fakes, monkeypatch):
    monkeypatch.setattr(hardware, "detect", lambda **kw: pytest.fail("explicit: no probing"))
    state = HubState()
    voice = VoiceRuntime(parse_config(OWNER), state)
    choice = voice.choose()
    assert choice.source == "config" and state.choice is choice
    assert voice.cleaner is not None and voice.cleaner.cfg.model == QWEN
    stt, threads = voice.prepare()
    assert (stt.model, stt.device, stt.compute_type, threads) == (
        "large-v3-turbo",
        "cuda",
        "float16",
        0,
    )
    assert fakes["downloads"] == []  # already downloaded: never fetched again
    assert voice.start_dictation(lambda: None) is None  # [dictation] enabled = false


def test_auto_on_a_laptop_downloads_with_progress(fakes):
    fakes["hw"], fakes["ready"] = LAPTOP, False
    state = HubState()
    seen = []
    state.subscribe(lambda s: seen.append(s.phase))
    voice = VoiceRuntime(Config(), state)
    voice.choose()
    assert voice.choice.tier == "light" and voice.cleaner is None
    stt, threads = voice.prepare()
    assert fakes["downloads"] == ["base.en"]
    assert (stt.model, stt.device, threads) == ("base.en", "cpu", 4)
    assert "preparing" in seen
    assert state.progress == 100 and state.download.done == 100
    from shoutabout.models import HubStatus

    state.set_status(HubStatus.IDLE)
    assert state.progress is None


def test_download_failure_becomes_an_engine_error(fakes):
    fakes["ready"], fakes["fail"] = False, True
    voice = VoiceRuntime(Config(), HubState())
    voice.choose()
    with pytest.raises(EngineError, match="Couldn't download"):
        voice.prepare()


def test_create_engine_uses_the_concrete_config(fakes, monkeypatch):
    import shoutabout.engine as engine_pkg

    made = {}
    monkeypatch.setattr(
        engine_pkg,
        "create_engine",
        lambda stt, cpu_threads=0: made.update(stt=stt, t=cpu_threads) or "engine",
    )
    voice = VoiceRuntime(Config(), HubState())
    voice.choose()
    assert voice.create_engine() == "engine"
    assert made["stt"].model == "large-v3-turbo" and made["stt"].cpu_fallback is True


def test_auto_cleanup_turns_on_when_ollama_appears(fakes):
    fakes["ollama"] = None  # Ollama not up yet at logon
    state = HubState()
    voice = VoiceRuntime(Config(), state)
    voice.choose()
    assert voice.cleaner is None and voice.cleanup_pending

    class Pipe:
        cleaner = None

    pipe = Pipe()
    assert voice.poll_cleanup(pipe) is None
    fakes["ollama"] = ["granite4:micro"]
    cleaner = voice.poll_cleanup(pipe)
    assert cleaner is not None and pipe.cleaner is cleaner
    assert cleaner.cfg.model == "granite4:micro"
    assert state.choice.cleanup and state.choice.cleanup_model == "granite4:micro"
    assert voice.poll_cleanup(pipe) is cleaner  # once on, it stays


def test_cleanup_never_pending_when_unaffordable_or_explicit(fakes):
    fakes["hw"], fakes["ollama"] = LAPTOP, None
    voice = VoiceRuntime(Config(), HubState())
    voice.choose()
    assert not voice.cleanup_pending
    off = VoiceRuntime(parse_config("[cleanup]\nenabled = false\n"), HubState())
    off.choose()
    assert off.cleaner is None and not off.cleanup_pending


def test_start_dictation_follows_pause(fakes, monkeypatch):
    import shoutabout.local_dictation as ld

    started = []

    class FakeDictation:
        def __init__(self, cfg, *, state, pipeline, notify, recent=None):
            self.state = state

        def follow_pause(self):
            started.append("follow")

        def start(self):
            started.append("start")

        def stop(self):
            started.append("stop")

    monkeypatch.setattr(ld, "LocalDictation", FakeDictation)
    state = HubState()
    voice = VoiceRuntime(Config(), state)
    assert voice.start_dictation(lambda: None) is not None
    assert started == ["follow", "start"]
    voice.stop()
    assert started[-1] == "stop"
    state.set_paused(True)
    started.clear()
    VoiceRuntime(Config(), state).start_dictation(lambda: None)
    assert started == ["follow"]  # paused: it starts when the hub resumes


def test_reload_dictation_starts_it_when_it_was_off(fakes, monkeypatch):
    import shoutabout.local_dictation as ld

    made = []

    class FakeDictation:
        def __init__(self, cfg, *, state, pipeline, notify, recent=None):
            self.cfg = cfg
            self.reloads = []
            made.append(self)

        def follow_pause(self):
            pass

        def start(self):
            pass

        def reload(self, cfg):
            self.reloads.append(cfg)

        def stop(self):
            pass

    monkeypatch.setattr(ld, "LocalDictation", FakeDictation)
    voice = VoiceRuntime(parse_config("[dictation]\nenabled = false\n"), HubState())
    assert voice.start_dictation(lambda: "pipeline") is None
    voice.reload_dictation(parse_config("[dictation]\nenabled = false\n"))
    assert made == []
    voice.reload_dictation(parse_config('[dictation]\nhotkey = "f9"\n'))
    assert len(made) == 1 and made[0].cfg.dictation.hotkey == "f9"
    voice.reload_dictation(parse_config('[dictation]\nmode = "toggle"\n'))
    assert made[0].reloads[-1].dictation.mode == "toggle"
