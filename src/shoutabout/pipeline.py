"""The transcription pipeline: decode -> STT -> normalise -> cleanup -> deliver -> Recent.

One request at a time (serialised by a lock): the GPU model and the paste target are
shared resources. Errors the server maps to HTTP statuses (AudioError,
AudioTooLongError, EngineError) propagate to the caller.

Every finished dictation reaches the PC through :func:`deliver_result`: phone uploads
(``process_file``), the PC's own microphone (``process_audio``), streamed dictations
(:mod:`shoutabout.streaming`) and phone-made text (``POST /paste``). It holds the paste
rules (target pc only, not while paused, never empty text), the "No speech detected."
warning, the trailing-space setting and Recent.

Streamed dictations transcribe stretches in the background while the person talks. They
take the same lock, one stretch at a time, and always after any job someone is waiting
for (:meth:`Pipeline.foreground` / :meth:`Pipeline.background_turn`), so a phone upload
waits for at most one stretch. They don't change the hub status: the tray shows
"transcribing" only once the person has stopped talking.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
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
    from shoutabout.streaming import StreamSession

log = logging.getLogger(__name__)

PAUSED_WARNING = "Hub is paused — not pasted on the PC."
NO_SPEECH_WARNING = "No speech detected."
PASTE_ERROR_WARNING = "Couldn't paste on the PC — see the hub log."

DecodeFn = Callable[[Path, float | None], np.ndarray]


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


# ------------------------------------------------------------------- delivery


def paste_text(config: Config, injector: Any, text: str) -> PasteResult:
    """Paste ``text`` into the focused window (plus the trailing-space setting). Never
    raises: a broken injector is a warning."""
    payload = text + (" " if config.paste.trailing_space else "")
    try:
        result = injector.paste(payload)
    except Exception:
        log.exception("paste failed")
        return PasteResult(pasted=False, warning=PASTE_ERROR_WARNING)
    if not result.pasted and result.warning:
        log.warning("not pasted: %s", result.warning)
    return result


def deliver_result(
    *,
    config: Config,
    injector: Any,
    state: HubState,
    recent: Recent | None,
    raw_text: str,
    cleaned: CleanupResult,
    target: Target,
    route: str,
    deliver: bool = True,
    timings: Timings,
    started: float,
    audio_seconds: float = 0.0,
    extra: dict[str, Any] | None = None,
    paste: Callable[[str], PasteResult] | None = None,
) -> TranscribeResult:
    """The last step every dictation shares: the final text, the paste rules, Recent.

    ``started`` is the ``time.perf_counter()`` the request started at (``timings.total``
    is measured from it). ``deliver=False`` never pastes (the CLI, Recent > Try again).
    ``paste`` overrides how the text is pasted (the pipeline's own ``_paste``)."""
    text = normalize_whitespace(cleaned.text)
    pasted, warning, copied = False, None, False
    if not text:
        # Either target: lets the Shortcut tell "nothing heard" apart from a real result.
        warning = NO_SPEECH_WARNING
    elif target == "pc" and deliver:
        if state.paused:
            warning = PAUSED_WARNING
        else:
            t = time.perf_counter()
            result = paste(text) if paste is not None else paste_text(config, injector, text)
            timings.paste = _ms(t)
            pasted, warning = bool(result.pasted), result.warning
            # Left on the computer's clipboard (no text box had the focus, ...).
            copied = bool(getattr(result, "copied", False))

    timings.total = _ms(started)
    out = TranscribeResult(
        text=text,
        raw_text=raw_text,
        target=target,
        route=route,
        pasted=pasted,
        warning=warning,
        cleanup=cleaned,
        timings=timings,
        audio_seconds=audio_seconds,
        request_id=uuid.uuid4().hex[:12],
        extra=dict(extra or {}),
        copied=copied,
    )
    if recent is not None:
        recent.add(out)  # never raises; keeps target pc only
    return out


# ------------------------------------------------------------------- pipeline


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
        # Jobs waiting for the lock that someone is waiting for (background stretches of
        # streamed dictations give way to them).
        self._waiting = 0
        self._waiting_cond = threading.Condition()

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
        return self.run_job(
            lambda: self._process(
                path, target=target, route=route, cleanup=cleanup, deliver=deliver, extra=extra
            )
        )

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
        return self.run_job(
            lambda: self._process(
                None,
                target=target,
                route=route,
                cleanup=cleanup,
                deliver=deliver,
                audio=samples,
                extra=extra,
            )
        )

    def open_stream(
        self,
        *,
        target: Target = "pc",
        route: str = "local",
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> StreamSession:
        """A dictation transcribed while it is recorded (:mod:`shoutabout.streaming`):
        feed it samples, then ``finish()`` delivers it like :meth:`process_audio`."""
        from shoutabout.streaming import StreamSession

        return StreamSession(self, target=target, route=route, extra=extra, **kwargs)

    def decode(self, path: Path) -> np.ndarray:
        """An audio file as 16 kHz mono float32 samples (what the pipeline transcribes)."""
        return self._decode(Path(path), self.config.server.max_audio_seconds)

    # ------------------------------------------------------ the lock and status

    @contextlib.contextmanager
    def foreground(self) -> Iterator[None]:
        """Hold the lock for a job someone is waiting for. Background stretches of
        streamed dictations don't take the lock while one of these waits."""
        with self._waiting_cond:
            self._waiting += 1
        try:
            self._lock.acquire()
        finally:
            with self._waiting_cond:
                self._waiting -= 1
                self._waiting_cond.notify_all()
        try:
            yield
        finally:
            self._lock.release()

    def background_turn(self, stop: Callable[[], bool], poll_s: float = 0.1) -> bool:
        """Take the lock for one background stretch, after every job that waits for it.
        True = the caller holds the lock (and must ``release_turn()``); False = ``stop()``
        turned true first."""
        while not stop():
            with self._waiting_cond:
                if self._waiting:
                    self._waiting_cond.wait(poll_s)
                    continue
            if self._lock.acquire(timeout=poll_s):
                return True
        return False

    def release_turn(self) -> None:
        self._lock.release()

    def run_job(self, job: Callable[[], TranscribeResult]) -> TranscribeResult:
        """Run a dictation's foreground work under the lock, with the hub status
        "transcribing" while it runs (ERROR after an engine failure)."""
        with self.foreground():
            self.state.set_status(HubStatus.TRANSCRIBING)
            try:
                result = job()
            except EngineError as exc:
                self.state.set_status(HubStatus.ERROR, str(exc))
                raise
            except BaseException:
                self.state.set_status(HubStatus.IDLE)
                raise
            self.state.set_status(HubStatus.IDLE)
            return result

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

        if audio is None:
            assert path is not None
            t = time.perf_counter()
            audio = self._decode(path, cfg.server.max_audio_seconds)
            timings.decode = _ms(t)
        audio_seconds = float(len(audio)) / SAMPLE_RATE

        t = time.perf_counter()
        raw_text = self.stt(audio)
        timings.stt = _ms(t)

        t = time.perf_counter()
        cleaned = self._cleanup(raw_text, cleanup)
        timings.cleanup = _ms(t)

        return self.deliver(
            raw_text=raw_text,
            cleaned=cleaned,
            target=target,
            route=route,
            deliver=deliver,
            timings=timings,
            started=t_total,
            audio_seconds=audio_seconds,
            extra=extra,
        )

    def deliver(
        self,
        *,
        raw_text: str,
        cleaned: CleanupResult,
        target: Target,
        route: str,
        deliver: bool,
        timings: Timings,
        started: float,
        audio_seconds: float,
        extra: dict[str, Any] | None = None,
    ) -> TranscribeResult:
        """:func:`deliver_result` with this pipeline's injector, state and Recent, plus the
        models that made the text."""
        return deliver_result(
            config=self.config,
            injector=self.injector,
            state=self.state,
            recent=self.recent,
            raw_text=raw_text,
            cleaned=cleaned,
            target=target,
            route=route,
            deliver=deliver,
            timings=timings,
            started=started,
            audio_seconds=audio_seconds,
            extra={
                "stt_model": getattr(self.engine, "name", None),
                "llm_model": self.llm_model(cleaned),
                **(extra or {}),
            },
            paste=self._paste,
        )

    def stt(self, audio: np.ndarray, *, context: str | None = None) -> str:
        """The engine's transcript of ``audio`` with the vocab hints (and the words said
        just before, for a stretch of a streamed dictation), whitespace normalised. The
        caller holds the lock."""
        hints = self._hints()
        if context:
            hints = replace(hints, context=context)
        return normalize_whitespace(self.engine.transcribe(audio, hints) or "")

    def clean(self, raw_text: str, cleanup: bool | None = None) -> CleanupResult:
        """LLM cleanup of one transcript (never raises). Needs no lock."""
        return self._cleanup(raw_text, cleanup)

    def llm_model(self, cleaned: CleanupResult) -> str | None:
        if cleaned.status not in ("applied", "fallback"):
            return None
        cleaner_model = getattr(getattr(self.cleaner, "cfg", None), "model", None)
        return cleaner_model or self.config.cleanup.model

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
        return paste_text(self.config, self.injector, text)


def format_log_line(result: TranscribeResult) -> str:
    """The one-line request summary from PLAN §6 (no text, no secrets). A streamed
    dictation adds how it was cut and the background work (``bg_*``, milliseconds spent
    while the person was still talking); its own timings are the wait after "stop"."""
    t = result.timings.as_dict()
    line = (
        f"transcribe id={result.request_id} route={result.route} target={result.target} "
        f"chars={len(result.text)} pasted={str(result.pasted).lower()} "
        f"cleanup={result.cleanup.status} audio_s={result.audio_seconds:.1f} "
        f"decode={t['decode']} stt={t['stt']} cleanup={t['cleanup']} paste={t['paste']} "
        f"total={t['total']}"
    )
    extra = result.extra or {}
    if extra.get("streamed"):
        line += (
            f" streamed=true chunks={int(extra.get('chunks') or 0)}"
            f" stretches={int(extra.get('stretches') or 0)}"
            f" bg_stt={round(float(extra.get('bg_stt_ms') or 0))}"
            f" bg_cleanup={round(float(extra.get('bg_cleanup_ms') or 0))}"
        )
    return line
