"""Test doubles for the engine, cleaner, injector and notifier."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from shoutabout.engine.base import SttHints
from shoutabout.models import CleanupResult, PasteResult


class FakeEngine:
    name = "fake-engine"

    def __init__(self, text: str = "hello better shel", fail: Exception | None = None) -> None:
        self.text = text
        self.fail = fail
        self.loaded = False
        self.calls: list[tuple[int, SttHints]] = []

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, audio: np.ndarray, hints: SttHints) -> str:
        if self.fail:
            raise self.fail
        self.calls.append((len(audio), hints))
        return self.text

    def count_tokens(self, text: str) -> int:
        # Roughly Whisper-like: ~1 token per 4 chars, at least one per word.
        return max(len(text.split()), (len(text) + 3) // 4)


class FakeVocabStore:
    """Duck-types vocab.VocabStore for pipeline/server tests."""

    def __init__(self, terms: Sequence[str] = ("Better Shell", "Claude Code")) -> None:
        self.terms = tuple(terms)

    def get(self):
        from types import SimpleNamespace

        return SimpleNamespace(terms=self.terms, version=1)

    def stt_hints(self, count_tokens, max_tokens: int, mode: str):
        from types import SimpleNamespace

        text = ", ".join(self.terms) if self.terms else None
        hints = SttHints(initial_prompt=text, hotwords=text)
        return SimpleNamespace(hints=hints, included=self.terms, dropped=(), tokens=0)


def fake_decode(path, max_seconds=None) -> np.ndarray:
    """Stand-in for audio.decode_audio: 1 s of silence, ignores the file."""
    return np.zeros(16_000, dtype=np.float32)


class FakeCleaner:
    def __init__(self, transform=None, status: str = "applied") -> None:
        self.transform = transform or (lambda s: s)
        self.status = status
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def clean(self, raw_text: str, terms: Sequence[str]) -> CleanupResult:
        self.calls.append((raw_text, tuple(terms)))
        return CleanupResult(text=self.transform(raw_text), status=self.status, duration_ms=1.0)

    def warm(self) -> bool:
        return True


class FakeInjector:
    def __init__(self, result: PasteResult | None = None) -> None:
        self.result = result or PasteResult(pasted=True)
        self.pasted: list[str] = []

    def paste(self, text: str) -> PasteResult:
        self.pasted.append(text)
        return self.result


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))
