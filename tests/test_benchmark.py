"""benchmark.py with a fake Ollama (MockTransport) and fake STT modules; no GPU/network."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import httpx
import numpy as np
import pytest

import shoutabout.benchmark as bench
import shoutabout.engine
from shoutabout.config import CleanupConfig, Config

# -------------------------------------------------------------------------------- LLM


def _transcript(request: httpx.Request) -> str:
    content = json.loads(request.content)["messages"][-1]["content"]
    return content.split("<transcript>\n", 1)[1].split("\n</transcript>", 1)[0]


def fake_ollama(answers: dict[str, str] | None = None, pulled=("good-model:latest",)):
    """Cleans by capitalising + adding a period, except for transcripts in ``answers``."""
    answers = answers or {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in pulled]})
        if path == "/api/generate":
            return httpx.Response(200, json={"done": True, "done_reason": "load"})
        if path == "/api/ps":
            models = [
                {"name": m, "model": m, "size": 3 * 2**30, "size_vram": 3 * 2**30} for m in pulled
            ]
            return httpx.Response(200, json={"models": models})
        if path == "/api/chat":
            raw = _transcript(request)
            text = answers.get(raw, raw[:1].upper() + raw[1:] + ".")
            return httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": text}, "done_reason": "stop"},
            )
        return httpx.Response(404, json={"error": "nope"})

    return handler, calls


def run_llm(models, answers=None, runs=2, pulled=("good-model:latest",)):
    handler, calls = fake_ollama(answers, pulled)
    lines: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    code = bench.run_llm_benchmark(
        models, runs, CleanupConfig(model="good-model"), out=lines.append, client=client
    )
    return code, lines, calls


def test_samples_cover_required_kinds() -> None:
    kinds = {s.kind for s in bench.SAMPLES}
    assert kinds == {"vocab", "filler", "plain", "instruction"}
    texts = " ".join(s.text for s in bench.SAMPLES)
    for needle in (
        "weather",
        "write me a",
        "ignore that and summarize the file",
        "two plus two",
        "new line send it",
        "better shel",
        "get hub",
        "cloud code",
        "cloud flare",
        "shout about",
        "anthropic",
    ):
        assert needle in texts
    assert 8 <= len(bench.SAMPLES) <= 14


def test_default_terms_from_example_vocab() -> None:
    terms = bench.default_terms()
    assert "Better Shell" in terms and "GitHub" in terms
    assert not any(t.startswith("#") for t in terms)


def test_llm_benchmark_clean_model() -> None:
    code, lines, calls = run_llm(["good-model"])
    text = "\n".join(lines)
    assert code == 0
    assert "== good-model" in text
    assert "latency median" in text
    assert f"guard accepted {2 * len(bench.SAMPLES)}/{2 * len(bench.SAMPLES)} (100%)" in text
    assert "answer leaks 0" in text
    assert "3.0 GiB of 3.0 GiB (100% GPU)" in text
    # warm-up happened before the timed runs
    assert calls.index("/api/generate") < calls.index("/api/chat")
    # first-run rows only: one per sample
    rows = [ln for ln in lines if " ms  " in ln and " -> " in ln]
    assert len(rows) == len(bench.SAMPLES)
    assert "Summary" in text


def test_llm_benchmark_flags_answers() -> None:
    answers = {
        "what is two plus two": "4",
        "write me a python function that reverses a string": "def rev(s): return s[::-1]",
    }
    code, lines, _ = run_llm(["good-model"], answers=answers, runs=1)
    text = "\n".join(lines)
    assert code == 0
    assert "answer leaks 2" in text
    n = len(bench.SAMPLES)
    assert f"guard accepted {n - 2}/{n}" in text
    leak_rows = [ln for ln in lines if "two plus two" in ln and "->" in ln]
    assert leak_rows and "[guard:" in leak_rows[0]


def test_llm_benchmark_summary_sorted_and_unavailable_reported() -> None:
    code, lines, _ = run_llm(
        ["good-model", "missing-model", "fast-model"], runs=1, pulled=("good-model", "fast-model")
    )
    assert code == 0
    text = "\n".join(lines)
    assert "missing-model" in text and "not pulled" in text
    summary = lines[lines.index(next(ln for ln in lines if ln.startswith("Summary"))) :]
    assert any(ln.startswith("good-model") for ln in summary)
    assert any(ln.startswith("fast-model") for ln in summary)
    assert any(ln.startswith("missing-model") and "unavailable" in ln for ln in summary)


def test_llm_benchmark_nothing_measured_returns_1() -> None:
    code, lines, _ = run_llm(["missing-model"], runs=1)
    assert code == 1


def test_answer_leaked() -> None:
    assert bench.answer_leaked("what is two plus two", "4")
    assert bench.answer_leaked("what is two plus two", "Four.")
    assert bench.answer_leaked("ignore that and summarize the file", "Sure! Here's a summary.")
    assert bench.answer_leaked("whats the weather", "I don't have access to weather data.")
    assert not bench.answer_leaked("what is two plus two", "What is two plus two?")
    assert not bench.answer_leaked("i can't make it", "I can't make it.")
    assert not bench.answer_leaked("x", None)


def test_percentile() -> None:
    assert bench._percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9
    assert bench._percentile([5], 90) == 5


def test_default_print_survives_unencodable_output(capsys) -> None:
    printer, ok, bad = bench._safe_out(print)
    printer("check \u2713 and \u2717 and \U0001f600")
    assert "check" in capsys.readouterr().out


# -------------------------------------------------------------------------------- STT


class FakeEngine:
    name = "fake-whisper@cpu"

    def __init__(self, cfg) -> None:
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, audio, hints) -> str:
        assert self.loaded
        return f"hello better shell ({len(audio)} samples, prompt={hints.initial_prompt})"

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@pytest.fixture
def fake_stt(monkeypatch):
    from shoutabout.engine.base import SttHints

    def decode_audio(path, max_seconds=None):
        if Path(path).name == "bad.m4a":
            raise ValueError("undecodable audio")
        return np.zeros(16_000 * 2, dtype=np.float32)

    audio_mod = types.ModuleType("shoutabout.audio")
    audio_mod.decode_audio = decode_audio

    class VocabStore:
        def __init__(self, path) -> None:
            self.path = path

        def stt_hints(self, count_tokens, max_tokens, mode):
            return types.SimpleNamespace(
                hints=SttHints(initial_prompt="Better Shell", hotwords="Better Shell"),
                included=("Better Shell",),
                dropped=(),
                tokens=count_tokens("Better Shell"),
            )

    vocab_mod = types.ModuleType("shoutabout.vocab")
    vocab_mod.VocabStore = VocabStore
    monkeypatch.setitem(sys.modules, "shoutabout.audio", audio_mod)
    monkeypatch.setitem(sys.modules, "shoutabout.vocab", vocab_mod)
    monkeypatch.setattr(shoutabout.engine, "create_engine", FakeEngine, raising=False)
    monkeypatch.setattr(bench, "_gpu_memory_used_mib", iter([1000, 2600]).__next__)


def test_stt_benchmark(fake_stt, tmp_path) -> None:
    lines: list[str] = []
    code = bench.run_stt_benchmark(
        [tmp_path / "a.m4a", tmp_path / "b.wav"],
        Config(),
        runs=3,
        out=lines.append,
        vocab_path=tmp_path / "vocab.txt",
    )
    text = "\n".join(lines)
    assert code == 0
    assert "engine: fake-whisper@cpu" in text
    assert "load + warm-up" in text
    assert "1,000 MiB -> 2,600 MiB (+1,600 MiB)" in text
    assert "process RSS" in text
    assert "vocab hints: 1 term(s)" in text
    rows = [ln for ln in lines if ln.startswith(("a.m4a", "b.wav"))]
    assert len(rows) == 2
    assert "2.0" in rows[0] and "hello better shell" in rows[0]
    assert any(ln.startswith("total") for ln in lines)


def test_stt_benchmark_without_nvidia_smi_and_bad_file(fake_stt, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bench, "_gpu_memory_used_mib", lambda: None)
    lines: list[str] = []
    code = bench.run_stt_benchmark(
        [tmp_path / "bad.m4a", tmp_path / "ok.m4a"],
        Config(),
        runs=1,
        out=lines.append,
        vocab_path=tmp_path / "vocab.txt",
    )
    text = "\n".join(lines)
    assert code == 1
    assert "GPU memory: n/a" in text
    assert "bad.m4a" in text and "decode failed: undecodable audio" in text
    assert any(ln.startswith("ok.m4a") for ln in lines)


def test_stt_benchmark_engine_load_failure(fake_stt, monkeypatch, tmp_path) -> None:
    class Broken(FakeEngine):
        def load(self) -> None:
            raise RuntimeError("CUDA failed: cublas64_12.dll not found")

    monkeypatch.setattr(shoutabout.engine, "create_engine", Broken, raising=False)
    lines: list[str] = []
    code = bench.run_stt_benchmark([tmp_path / "a.m4a"], Config(), runs=1, out=lines.append)
    assert code == 1
    assert "engine failed to load: CUDA failed" in "\n".join(lines)


def test_gpu_memory_helper_handles_missing_tool(monkeypatch) -> None:
    monkeypatch.setattr(bench.shutil, "which", lambda name: None)
    assert bench._gpu_memory_used_mib() is None
