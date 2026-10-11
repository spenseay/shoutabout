from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from shoutabout.config import SttConfig
from shoutabout.engine import EngineError, SpeechEngine, SttHints, create_engine, cuda_dlls
from shoutabout.engine import faster_whisper_engine as fwe
from shoutabout.engine.faster_whisper_engine import FasterWhisperEngine, filter_segments
from shoutabout.vocab import build_stt_hints, format_hint_text

TERMS = ["Better Shell", "Claude Code", "Claude", "Anthropic", "GitHub", "Cloudflare", "ShoutAbout"]


# ---------------------------------------------------------------------- cuda_dlls


@pytest.fixture
def fresh_dll_state(monkeypatch):
    monkeypatch.setattr(cuda_dlls, "_registered", [])
    monkeypatch.setattr(cuda_dlls, "_handles", [])


def make_nvidia_tree(root: Path) -> Path:
    nvidia = root / "site-packages" / "nvidia"
    for lib, dll in (("cublas", "cublas64_12.dll"), ("cuda_nvrtc", "nvrtc64_120_0.dll")):
        (nvidia / lib / "bin").mkdir(parents=True)
        (nvidia / lib / "bin" / dll).write_bytes(b"MZ")
    (nvidia / "empty" / "bin").mkdir(parents=True)  # no DLLs -> ignored
    (nvidia / "cublas" / "include").mkdir()
    return nvidia


def test_cuda_dlls_noop_off_windows(monkeypatch, fresh_dll_state):
    monkeypatch.setattr(cuda_dlls.sys, "platform", "linux")
    before = os.environ.get("PATH")
    assert cuda_dlls.add_nvidia_dll_dirs() == []
    assert os.environ.get("PATH") == before


def test_cuda_dlls_registers_and_is_idempotent(tmp_path, monkeypatch, fresh_dll_state):
    nvidia = make_nvidia_tree(tmp_path)
    added: list[str] = []
    monkeypatch.setattr(cuda_dlls.sys, "platform", "win32")
    monkeypatch.setattr(cuda_dlls, "_candidate_roots", lambda: [nvidia, nvidia])
    monkeypatch.setattr(
        os, "add_dll_directory", lambda p: added.append(p) or object(), raising=False
    )
    existing = [str(tmp_path / "existing"), str(tmp_path / "other")]  # no ':' on POSIX
    monkeypatch.setenv("PATH", os.pathsep.join(existing))

    dirs = cuda_dlls.add_nvidia_dll_dirs()
    expected = [str(nvidia / "cublas" / "bin"), str(nvidia / "cuda_nvrtc" / "bin")]
    assert [os.path.normcase(d) for d in dirs] == [os.path.normcase(d) for d in expected]
    assert added == dirs
    path_parts = os.environ["PATH"].split(os.pathsep)
    assert path_parts[: len(dirs)] == dirs
    assert path_parts[len(dirs) :] == existing

    again = cuda_dlls.add_nvidia_dll_dirs()
    assert again == dirs
    assert added == dirs  # not registered twice
    assert os.environ["PATH"].split(os.pathsep) == path_parts  # not prepended twice


def test_cuda_dlls_restores_path_entries_if_path_was_replaced(
    tmp_path, monkeypatch, fresh_dll_state
):
    nvidia = make_nvidia_tree(tmp_path)
    monkeypatch.setattr(cuda_dlls.sys, "platform", "win32")
    monkeypatch.setattr(cuda_dlls, "_candidate_roots", lambda: [nvidia])
    monkeypatch.setattr(os, "add_dll_directory", lambda p: object(), raising=False)
    dirs = cuda_dlls.add_nvidia_dll_dirs()
    fresh = str(tmp_path / "fresh")
    monkeypatch.setenv("PATH", fresh)
    cuda_dlls.add_nvidia_dll_dirs()
    assert os.environ["PATH"].split(os.pathsep) == [*dirs, fresh]


def test_find_nvidia_dll_dirs_ignores_bins_without_dlls(tmp_path, monkeypatch):
    nvidia = make_nvidia_tree(tmp_path)
    monkeypatch.setattr(cuda_dlls, "_candidate_roots", lambda: [nvidia])
    found = [Path(d).parent.name for d in cuda_dlls.find_nvidia_dll_dirs()]
    assert found == ["cublas", "cuda_nvrtc"]


# ------------------------------------------------------------------ fake backend


class FakeTokenizer:
    def encode(self, text: str, add_special_tokens: bool = True):
        assert add_special_tokens is False
        return SimpleNamespace(ids=list(range(len(text.split()) * 2)))


def seg(text: str, no_speech_prob: float = 0.01, avg_logprob: float = -0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob)


class FakeBackend:
    """Stands in for faster_whisper.WhisperModel / ctranslate2 / download_model."""

    def __init__(
        self,
        *,
        cuda_devices: int = 1,
        fail_create_on: tuple[str, ...] = (),
        fail_warmup_on: tuple[str, ...] = (),
        cached: bool = True,
        download_error: Exception | None = None,
        segments: list | None = None,
    ) -> None:
        self.cuda_devices = cuda_devices
        self.fail_create_on = fail_create_on
        self.fail_warmup_on = fail_warmup_on
        self.cached = cached
        self.download_error = download_error
        self.segments = segments if segments is not None else [seg(" Hello"), seg(" world. ")]
        self.created: list[tuple[str, str, str]] = []
        self.calls: list[dict] = []
        self.downloads: list[dict] = []

    def model_dir(self, tmp: Path) -> Path:
        d = tmp / "model"
        d.mkdir(exist_ok=True)
        for f in ("model.bin", "config.json"):
            (d / f).write_bytes(b"x")
        return d

    def as_backend(self, model_dir: Path) -> fwe._Backend:
        backend = self

        class Model:
            def __init__(self, path, device="auto", compute_type="default", **kw):
                backend.created.append((path, device, compute_type))
                if device in backend.fail_create_on:
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                self.device = device
                self.hf_tokenizer = FakeTokenizer()

            def transcribe(self, audio, **opts):
                warm = opts.get("vad_filter") is False and "hotwords" not in opts
                if warm and self.device in backend.fail_warmup_on:
                    raise RuntimeError("CUDA failed with error out of memory")
                backend.calls.append({"audio": audio, **opts})
                return iter(list(backend.segments)), SimpleNamespace(language="en")

        def download_model(name, local_files_only=False, cache_dir=None, **kw):
            backend.downloads.append(
                {"name": name, "local_files_only": local_files_only, "cache_dir": cache_dir}
            )
            if local_files_only and not backend.cached:
                raise FileNotFoundError("not in cache")
            if not local_files_only and backend.download_error:
                raise backend.download_error
            return str(model_dir)

        return fwe._Backend(
            whisper_model=Model,
            cuda_device_count=lambda: backend.cuda_devices,
            download_model=download_model,
            available_models=lambda: ["large-v3-turbo", "tiny"],
        )


@pytest.fixture
def fake(tmp_path, monkeypatch):
    backend = FakeBackend()
    model_dir = backend.model_dir(tmp_path)
    dll_calls: list[int] = []
    monkeypatch.setattr(fwe, "_import_backend", lambda: backend.as_backend(model_dir))
    monkeypatch.setattr(fwe, "add_nvidia_dll_dirs", lambda: dll_calls.append(1) or [])
    backend.dll_calls = dll_calls
    return backend


def v1_stt(**overrides) -> SttConfig:
    """Explicit [stt] values as in a v1 config.toml (the schema default is now "auto")."""
    values: dict = {"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16"}
    values.update(overrides)
    return SttConfig(**values)


def transcribe_calls(backend: FakeBackend) -> list[dict]:
    """Calls other than the warm-up ones."""
    return [c for c in backend.calls if c.get("temperature") != 0.0 and c.get("beam_size") != 1]


# -------------------------------------------------------------------------- engine


def test_create_engine():
    engine = create_engine(v1_stt())
    assert isinstance(engine, FasterWhisperEngine)
    assert isinstance(engine, SpeechEngine)
    assert engine.name == "faster-whisper:large-v3-turbo@cuda/float16"
    with pytest.raises(EngineError, match="Unknown STT engine"):
        create_engine(SttConfig.model_construct(engine="parakeet"))


def test_load_on_cuda_and_warm_up(fake):
    engine = FasterWhisperEngine(v1_stt())
    engine.load()
    assert fake.dll_calls == [1]
    assert fake.created == [(fake.created[0][0], "cuda", "float16")]
    assert engine.device == "cuda" and engine.loaded
    assert engine.name == "faster-whisper:large-v3-turbo@cuda/float16"
    assert engine.fallback_reason is None
    warm = fake.calls[0]
    assert warm["vad_filter"] is False and len(warm["audio"]) == 16_000
    engine.load()  # idempotent
    assert len(fake.created) == 1
    # model resolved from the local cache without network
    assert fake.downloads == [
        {"name": "large-v3-turbo", "local_files_only": True, "cache_dir": None}
    ]


def test_decode_options_and_hints_pass_through(fake):
    engine = FasterWhisperEngine(v1_stt(vad_min_silence_ms=700, beam_size=3))
    hints = SttHints(initial_prompt="In this dictation I mention X.", hotwords="Better Shell")
    text = engine.transcribe(np.zeros(32_000, np.float32), hints)
    assert text == "Hello world."
    call = transcribe_calls(fake)[-1]
    assert call["hotwords"] == "Better Shell"
    assert call["initial_prompt"] == "In this dictation I mention X."
    assert call["language"] == "en"
    assert call["beam_size"] == 3
    assert call["condition_on_previous_text"] is False
    assert call["without_timestamps"] is False
    assert call["vad_filter"] is True
    assert call["vad_parameters"] == {"min_silence_duration_ms": 700, "speech_pad_ms": 300}
    assert list(call["temperature"]) == [0.0, 0.2, 0.4]
    assert "chunk_length" not in call
    assert call["audio"].dtype == np.float32


def test_empty_hints_are_not_passed(fake):
    engine = FasterWhisperEngine(v1_stt(vad_filter=False, language=None))
    engine.transcribe(np.zeros(16_000, np.float32), SttHints(initial_prompt="  ", hotwords=None))
    call = transcribe_calls(fake)[-1]
    assert "hotwords" not in call and "initial_prompt" not in call
    assert "vad_parameters" not in call and call["vad_filter"] is False
    assert call["language"] is None


def test_empty_audio_returns_empty_without_decoding(fake):
    engine = FasterWhisperEngine(v1_stt())
    assert engine.transcribe(np.zeros(0, np.float32), SttHints()) == ""
    assert fake.calls == []


def test_transcribe_failure_is_engine_error(fake):
    engine = FasterWhisperEngine(v1_stt())
    engine.load()

    def boom(audio, **opts):
        raise RuntimeError("CUDA failed with error unspecified launch failure")

    engine._model.transcribe = boom
    with pytest.raises(EngineError, match="Transcription failed"):
        engine.transcribe(np.zeros(16_000, np.float32), SttHints())


def test_cpu_fallback_when_no_cuda_device(fake, caplog):
    fake.cuda_devices = 0
    engine = FasterWhisperEngine(v1_stt(cpu_fallback=True))
    with caplog.at_level(logging.WARNING):
        engine.load()
    assert engine.device == "cpu" and engine.compute_type == "int8"
    assert engine.name == "faster-whisper:large-v3-turbo@cpu/int8"
    assert "no CUDA GPU" in engine.fallback_reason
    assert [c[1:] for c in fake.created] == [("cpu", "int8")]
    assert any("falling back to the CPU" in r.getMessage() for r in caplog.records)


def test_cpu_fallback_when_cublas_missing(fake):
    fake.fail_create_on = ("cuda",)
    engine = FasterWhisperEngine(v1_stt(cpu_fallback=True, cpu_compute_type="float32"))
    engine.load()
    assert [c[1:] for c in fake.created] == [("cuda", "float16"), ("cpu", "float32")]
    assert engine.device == "cpu"
    assert "cublas64_12.dll" in engine.fallback_reason


def test_cpu_fallback_when_warm_up_fails_on_gpu(fake):
    fake.fail_warmup_on = ("cuda",)
    engine = FasterWhisperEngine(v1_stt(cpu_fallback=True))
    engine.load()
    assert engine.device == "cpu"
    assert "out of memory" in engine.fallback_reason


def test_cuda_error_without_fallback_is_actionable(fake):
    fake.fail_create_on = ("cuda",)
    engine = FasterWhisperEngine(v1_stt())
    with pytest.raises(EngineError) as info:
        engine.load()
    msg = str(info.value)
    assert "CUDA is not available" in msg
    assert "cublas64_12.dll" in msg
    assert "python -m shoutabout doctor" in msg
    assert "nvidia-cublas-cu12" in msg
    assert 'device = "cpu"' in msg and "cpu_fallback = true" in msg
    assert not engine.loaded
    assert [c[1] for c in fake.created] == ["cuda"]  # no silent CPU load


def test_no_cuda_device_without_fallback(fake):
    fake.cuda_devices = 0
    with pytest.raises(EngineError, match="no CUDA GPU"):
        FasterWhisperEngine(v1_stt()).load()
    assert fake.created == []


def test_device_cpu_skips_cuda(fake):
    fake.cuda_devices = 0
    engine = FasterWhisperEngine(v1_stt(device="cpu"))
    assert engine.name.endswith("@cpu/int8")
    engine.load()
    assert fake.dll_calls == []
    assert [c[1:] for c in fake.created] == [("cpu", "int8")]
    assert engine.fallback_reason is None


def test_cpu_load_failure_is_engine_error(fake):
    fake.fail_create_on = ("cpu",)
    with pytest.raises(EngineError, match="Could not load Whisper model"):
        FasterWhisperEngine(v1_stt(device="cpu")).load()


def test_model_download_when_not_cached(fake):
    fake.cached = False
    engine = FasterWhisperEngine(v1_stt(download_root="D:/models"))
    engine.load()
    assert [d["local_files_only"] for d in fake.downloads] == [True, False]
    assert all(d["cache_dir"] == "D:/models" for d in fake.downloads)


def test_model_not_cached_and_offline(fake):
    fake.cached = False
    fake.download_error = OSError("Connection refused")
    with pytest.raises(EngineError) as info:
        FasterWhisperEngine(v1_stt()).load()
    msg = str(info.value)
    assert "not in the local cache" in msg and "could not be downloaded" in msg
    assert "Connection refused" in msg and "download-model" in msg
    assert fake.created == []


def test_resolve_without_download(fake):
    fake.cached = False
    with pytest.raises(EngineError, match="not downloaded yet"):
        FasterWhisperEngine(v1_stt()).resolve_model_path(download=False)


def test_unknown_model_name(fake):
    with pytest.raises(EngineError, match="Unknown Whisper model"):
        FasterWhisperEngine(v1_stt(model="huge-v9")).load()


def test_local_model_folder(fake, tmp_path):
    folder = tmp_path / "my-model"
    folder.mkdir()
    engine = FasterWhisperEngine(v1_stt(model=str(folder)))
    engine.load()
    assert fake.created[0][0] == str(folder)
    assert fake.downloads == []


def test_transcribe_loads_lazily(fake):
    engine = FasterWhisperEngine(v1_stt())
    assert not engine.loaded
    assert engine.transcribe(np.ones(1600, np.float32) * 0.1, SttHints()) == "Hello world."
    assert engine.loaded


def test_count_tokens_heuristic_then_tokenizer(fake):
    engine = FasterWhisperEngine(v1_stt())
    assert engine.count_tokens("") == 0
    assert engine.count_tokens("   ") == 0
    assert engine.count_tokens("Better Shell") == fwe.heuristic_token_count("Better Shell")
    assert fwe.heuristic_token_count("a b c d e") == 5  # at least one per word
    assert fwe.heuristic_token_count("x" * 35) == 10
    engine.load()
    assert engine.count_tokens("Better Shell, Claude Code") == 8  # FakeTokenizer: 2 per word


# ------------------------------------------------------------------- segment filter


def test_segments_joined_and_stripped():
    out = filter_segments([seg(" Hello there."), seg("   "), seg(" How are you? ")], SttHints())
    assert out == "Hello there. How are you?"


def test_no_speech_segments_dropped():
    segs = [seg(" Thank you.", no_speech_prob=0.9, avg_logprob=-1.3), seg(" Real words.")]
    assert filter_segments(segs, SttHints()) == "Real words."
    # high no-speech prob but confident text is kept (same rule as Whisper's)
    assert filter_segments([seg(" Kept.", 0.9, -0.3)], SttHints()) == "Kept."


@pytest.mark.parametrize("mode", ["both", "hotwords", "initial_prompt"])
def test_hint_echo_dropped(mode):
    hints = build_stt_hints(TERMS, len, 10_000, mode).hints
    full = format_hint_text(TERMS)
    assert filter_segments([seg(" " + full)], hints) == ""
    assert filter_segments([seg(" Better Shell, Claude Code, Claude.")], hints) == ""
    assert filter_segments([seg(" Claude, Anthropic, GitHub, Cloudflare")], hints) == ""
    assert filter_segments([seg(" In this dictation I mention Better Shell")], hints) == ""
    # echo spread over two segments
    assert (
        filter_segments([seg(" Better Shell, Claude Code,"), seg(" Claude, Anthropic.")], hints)
        == ""
    )


def test_hint_echo_keeps_real_speech():
    hints = build_stt_hints(TERMS, len, 10_000, "both").hints
    for said in ("Claude Code.", "Better Shell, Claude Code", "GitHub", "Claude"):
        assert filter_segments([seg(" " + said)], hints) == said
    assert (
        filter_segments([seg(" Push Better Shell to GitHub and ask Claude.")], hints)
        == "Push Better Shell to GitHub and ask Claude."
    )
    # only the echo segment is dropped
    segs = [seg(" Ship it today."), seg(" Better Shell, Claude Code, Claude, Anthropic.")]
    assert filter_segments(segs, hints) == "Ship it today."


def test_hint_echo_with_plain_list_hints():
    # FakeVocabStore-style hints: plain comma list, no label
    hints = SttHints(
        hotwords="Better Shell, Claude Code", initial_prompt="Better Shell, Claude Code"
    )
    assert filter_segments([seg(" Better Shell, Claude Code.")], hints) == ""
    assert filter_segments([seg(" Better Shell")], hints) == "Better Shell"


# ------------------------------------------------------------------------ GPU tests

gpu = pytest.mark.gpu


@pytest.fixture(scope="module")
def gpu_engine():
    engine = FasterWhisperEngine(v1_stt())
    engine.load()
    return engine


def synthesize(text: str, out: Path, voice: str = "Zira") -> Path:
    """Windows SAPI text-to-speech -> 16 kHz mono WAV (GPU tests only)."""
    if sys.platform != "win32":
        pytest.skip("SAPI text-to-speech needs Windows")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SelectVoice('Microsoft {voice} Desktop');"
        f"$s.SetOutputToWaveFile('{out}',$f);"
        f"$s.Speak('{text}');$s.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"SAPI text-to-speech unavailable: {exc}")
    return out


@gpu
def test_gpu_loads_on_cuda(gpu_engine):
    assert gpu_engine.device == "cuda"
    assert gpu_engine.fallback_reason is None
    assert gpu_engine.load_seconds is not None
    # real Whisper tokenizer is used once loaded
    n = gpu_engine.count_tokens(format_hint_text(TERMS))
    ids = gpu_engine._tokenizer.encode(" " + format_hint_text(TERMS), add_special_tokens=False).ids
    assert n == len(ids) and 10 < n < 60


@gpu
def test_gpu_transcribes_m4a_with_vocab(gpu_engine, tmp_path):
    from shoutabout.audio import decode_audio, encode_m4a

    sentence = (
        "I am working on Better Shell with Claude Code today, "
        "and I pushed the ShoutAbout repo to GitHub."
    )
    wav = synthesize(sentence, tmp_path / "speech.wav")
    m4a = tmp_path / "speech.m4a"
    encode_m4a(wav, m4a)
    audio = decode_audio(m4a, max_seconds=60)
    hints = build_stt_hints(TERMS, gpu_engine.count_tokens, 200, "both").hints

    gpu_engine.transcribe(audio, hints)  # first real call (any lazy init)
    t0 = time.perf_counter()
    text = gpu_engine.transcribe(audio, hints)
    ms = (time.perf_counter() - t0) * 1000
    print(f"\n{len(audio) / 16000:.1f} s audio -> {ms:.0f} ms: {text!r}")
    for term in ("Better Shell", "Claude Code", "ShoutAbout", "GitHub"):
        assert term in text
    assert ms < 2000


@gpu
def test_gpu_generated_tone_m4a_does_not_crash(gpu_engine, tmp_path):
    from shoutabout.audio import decode_audio, encode_m4a

    t = np.arange(16_000 * 2) / 16_000
    src = tmp_path / "tone.wav"
    import wave

    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(((0.2 * np.sin(2 * np.pi * 300 * t)) * 32767).astype("<i2").tobytes())
    m4a = tmp_path / "tone.m4a"
    encode_m4a(src, m4a)
    out = gpu_engine.transcribe(decode_audio(m4a), SttHints())
    assert isinstance(out, str)


@gpu
@pytest.mark.parametrize("kind", ["silence", "low_noise", "white_noise"])
@pytest.mark.parametrize("mode", ["both", "hotwords", "initial_prompt"])
def test_gpu_silence_and_noise_produce_no_text(gpu_engine, kind, mode):
    rng = np.random.default_rng(7)
    audio = {
        "silence": np.zeros(16_000 * 3, np.float32),
        "low_noise": (rng.standard_normal(16_000 * 3) * 0.003).astype(np.float32),
        "white_noise": (rng.standard_normal(16_000 * 3) * 0.08).astype(np.float32),
    }[kind]
    hints = build_stt_hints(TERMS, gpu_engine.count_tokens, 200, mode).hints
    assert gpu_engine.transcribe(audio, hints) == ""
