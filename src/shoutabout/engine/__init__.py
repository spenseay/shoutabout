"""Speech-to-text engines.

The concrete engine is imported lazily so importing this package never pulls in
faster-whisper / CTranslate2 (and never touches CUDA).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shoutabout.engine.base import SAMPLE_RATE, EngineError, SpeechEngine, SttHints

if TYPE_CHECKING:
    from shoutabout.config import SttConfig

__all__ = ["SAMPLE_RATE", "EngineError", "SpeechEngine", "SttHints", "create_engine"]


def create_engine(cfg: SttConfig, *, cpu_threads: int = 0) -> SpeechEngine:
    """Build the engine named by ``[stt] engine`` (not loaded yet: call ``load()``).

    ``"auto"`` model / device / compute type are picked from the hardware here
    (:func:`shoutabout.modelpick.resolve_stt`), so every caller (hub, CLI, doctor) runs
    the same choice. ``cpu_threads`` is for a config that is already concrete.
    """
    if cfg.engine == "faster-whisper":
        from shoutabout.engine.faster_whisper_engine import FasterWhisperEngine
        from shoutabout.modelpick import resolve_stt, stt_has_auto

        if stt_has_auto(cfg):
            cfg, cpu_threads = resolve_stt(cfg)
        return FasterWhisperEngine(cfg, cpu_threads=cpu_threads)
    raise EngineError(f"Unknown STT engine {cfg.engine!r} in [stt] engine.")
