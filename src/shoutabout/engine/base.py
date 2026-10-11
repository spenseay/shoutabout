"""Speech-to-text engine interface.

Engines receive already-decoded audio (16 kHz mono float32, see shoutabout.audio) so
decoding is timed separately and the duration limit is enforced before the GPU runs.
A future NVIDIA Parakeet TDT engine implements the same protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

SAMPLE_RATE = 16_000


class EngineError(RuntimeError):
    """Engine failed to load or transcribe (message is shown to the user)."""


@dataclass(frozen=True)
class SttHints:
    """Vocabulary biasing for one transcription."""

    initial_prompt: str | None = None
    hotwords: str | None = None


@runtime_checkable
class SpeechEngine(Protocol):
    name: str  # e.g. "faster-whisper:large-v3-turbo@cuda/float16"

    def load(self) -> None:
        """Load the model and warm it up. Idempotent. Raises EngineError."""

    def transcribe(self, audio: np.ndarray, hints: SttHints) -> str:
        """Transcribe 16 kHz mono float32 samples. Returns plain text (may be empty)."""

    def count_tokens(self, text: str) -> int:
        """Number of prompt tokens `text` uses (for the vocab prompt budget)."""
