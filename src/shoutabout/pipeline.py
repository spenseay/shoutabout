"""The transcription pipeline: decode -> STT -> normalise -> cleanup -> deliver -> Recent.

One request at a time (serialised by a lock): the GPU model and the paste target are
shared resources. Errors the server maps to HTTP statuses (AudioError,
AudioTooLongError, EngineError) propagate to the caller.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from shoutabout.config import Config
from shoutabout.engine.base import SAMPLE_RATE, EngineError, SpeechEngine, SttHints
from shoutabout.inject.base import Injector
from shoutabout.models import (
    CleanupResult,
    HubStatus,
    PasteResult,
    Target,
    Timings,
    TranscribeResult,
)
from shoutabout.state import HubState
from shoutabout.text import normalize_whitespace

if TYPE_CHECKING:
    from shoutabout.recent import Recent

log = logging.getLogger(__name__)

PAUSED_WARNING = "Hub is paused — not pasted on the PC."
NO_SPEECH_WARNING = "No speech detected."
PASTE_ERROR_WARNING = "Couldn't paste on the PC — see the hub log."

DecodeFn = Callable[[Path, float | None], np.ndarray]


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


class Pipeline:
    def __init__(
        self,
        *,
        config: Config,
        engine: SpeechEngine,
        vocab: Any,
        cleaner: Any | None,
        injector: Injector,
        state: HubState,
        recent: Recent | None = None,
        decode: DecodeFn | None = None,
    ) -> None:
        if decode is None:
            from shoutabout.audio import decode_audio

            decode = decode_audio
        self.config = config
        self.engine = engine
        self.vocab = vocab
        self.cleaner = cleaner
        self.injector = injector
        self.state = state
        self.recent = recent  # the last few results, in memory only (the Recent page)
        self._decode = decode
        # One transcription at a time across the whole hub (phone requests, "Talk to this
        # PC", text pastes): HubState owns the lock when it has one.
        self._lock = getattr(state, "busy", None) or threading.RLock()

    # ----------------------------------------------------------------- public

    def process_file(
        self,
        path: Path,
        *,
        target: Target,
        route: str,
        cleanup: bool | None = None,
        deliver: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> TranscribeResult:
        """Transcribe an audio file. ``extra`` is added to the result's extras (and so to
        Recent), e.g. ``{"source": "<phone name>"}``."""
        path = Path(path)
        with self._lock:
            self.state.set_status(HubStatus.TRANSCRIBING)
            try:
                result = self._process(
                    path, target=target, route=route, cleanup=cleanup, deliver=deliver, extra=extra
                )
            except EngineError as exc:
                self.state.set_status(HubStatus.ERROR, str(exc))
                raise
            except BaseException:
                self.state.set_status(HubStatus.IDLE)
                raise
            if self.recent is not None:
                self.recent.add(result)
            self.state.set_status(HubStatus.IDLE)
            return result

    def process_audio(
        self,
        audio: np.ndarray,
        *,
        target: Target = "pc",
        route: str = "local",
        cleanup: bool | None = None,
        deliver: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> TranscribeResult:
        """Like :meth:`process_file` for samples that are already 16 kHz mono float32
        (the PC's own microphone): same STT, cleanup, paste rules, Recent and lock."""
        samples = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        with self._lock:
            self.state.set_status(HubStatus.TRANSCRIBING)
            try:
                result = self._process(
                    None,
                    target=target,
                    route=route,
                    cleanup=cleanup,
                    deliver=deliver,
                    audio=samples,
                    extra=extra,
                )
            except EngineError as exc:
                self.state.set_status(HubStatus.ERROR, str(exc))
                raise
            except BaseException:
                self.state.set_status(HubStatus.IDLE)
                raise
            if self.recent is not None:
                self.recent.add(result)
            self.state.set_status(HubStatus.IDLE)
            return result

    def decode(self, path: Path) -> np.ndarray:
        """An audio file as 16 kHz mono float32 samples (what the pipeline transcribes)."""
        return self._decode(Path(path), self.config.server.max_audio_seconds)

    # ---------------------------------------------------------------- stages

    def _process(
        self,
        path: Path | None,
        *,
        target: Target,
        route: str,
        cleanup: bool | None,
        deliver: bool,
        audio: np.ndarray | None = None,
        extra: dict[str, Any] | None = None,
    ) -> TranscribeResult:
        cfg = self.config
        timings = Timings()
        t_total = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]

        if audio is None:
            assert path is not None
            t = time.perf_counter()
            audio = self._decode(path, cfg.server.max_audio_seconds)
            timings.decode = _ms(t)
        audio_seconds = float(len(audio)) / SAMPLE_RATE

        t = time.perf_counter()
        hints = self._hints()
        raw_text = normalize_whitespace(self.engine.transcribe(audio, hints) or "")
        timings.stt = _ms(t)

        t = time.perf_counter()
        cleaned = self._cleanup(raw_text, cleanup)
        timings.cleanup = _ms(t)
        text = normalize_whitespace(cleaned.text)

        pasted, warning = False, None
        if not text:
            # Either target: lets the Shortcut tell "nothing heard" apart from a real result.
            warning = NO_SPEECH_WARNING
        elif target == "pc" and deliver:
            if self.state.paused:
                warning = PAUSED_WARNING
            else:
                t = time.perf_counter()
                paste = self._paste(text)
                timings.paste = _ms(t)
                pasted, warning = bool(paste.pasted), paste.warning

        timings.total = _ms(t_total)
        cleaner_model = getattr(getattr(self.cleaner, "cfg", None), "model", None)
        llm_model = (
            (cleaner_model or cfg.cleanup.model)
            if cleaned.status in ("applied", "fallback")
            else None
        )
        return TranscribeResult(
            text=text,
            raw_text=raw_text,
            target=target,
            route=route,
            pasted=pasted,
            warning=warning,
            cleanup=cleaned,
            timings=timings,
            audio_seconds=audio_seconds,
            request_id=request_id,
            extra={
                "stt_model": getattr(self.engine, "name", None),
                "llm_model": llm_model,
                **(extra or {}),
            },
        )

    def _hints(self) -> SttHints:
        stt = self.config.stt
        try:
            build = self.vocab.stt_hints(
                self.engine.count_tokens, stt.max_prompt_tokens, stt.prompt_mode
            )
            return build.hints
        except Exception:
            log.exception("couldn't build vocab hints; transcribing without them")
            return SttHints()

    def _terms(self) -> tuple[str, ...]:
        try:
            return tuple(self.vocab.get().terms)
        except Exception:
            log.exception("couldn't read vocab terms")
            return ()

    def _cleanup(self, raw_text: str, cleanup: bool | None) -> CleanupResult:
        # enabled may be "auto": then the hub only builds a cleaner when it can run.
        if cleanup is False or self.cleaner is None or self.config.cleanup.enabled is False:
            return CleanupResult(text=raw_text, status="disabled")
        if not raw_text:
            return CleanupResult(text=raw_text, status="skipped", reason="empty transcript")
        try:
            return self.cleaner.clean(raw_text, self._terms())
        except Exception as exc:  # Cleaner.clean must not raise; be defensive anyway
            log.exception("cleanup failed unexpectedly; using the raw transcript")
            return CleanupResult(
                text=raw_text, status="fallback", reason=f"cleanup error: {type(exc).__name__}"
            )

    def _paste(self, text: str) -> PasteResult:
        payload = text + (" " if self.config.paste.trailing_space else "")
        try:
            result = self.injector.paste(payload)
        except Exception:
            log.exception("paste failed")
            return PasteResult(pasted=False, warning=PASTE_ERROR_WARNING)
        if not result.pasted and result.warning:
            log.warning("not pasted: %s", result.warning)
        return result


def format_log_line(result: TranscribeResult) -> str:
    """The one-line request summary from PLAN §6 (no text, no secrets)."""
    t = result.timings.as_dict()
    return (
        f"transcribe id={result.request_id} route={result.route} target={result.target} "
        f"chars={len(result.text)} pasted={str(result.pasted).lower()} "
        f"cleanup={result.cleanup.status} audio_s={result.audio_seconds:.1f} "
        f"decode={t['decode']} stt={t['stt']} cleanup={t['cleanup']} paste={t['paste']} "
        f"total={t['total']}"
    )
