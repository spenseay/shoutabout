"""Transcribe while you talk: streamed dictations (docs/protocol.md, "Streaming").

While someone talks, their audio arrives in pieces: raw 16 kHz PCM chunks from the phone
(``POST /stream/{id}/audio``) or blocks from the PC's own microphone. A
:class:`StreamSession` keeps it in memory and, on a background thread, transcribes
finished *stretches* of speech:

* once at least :data:`MIN_STRETCH_S` seconds wait, it looks for a pause (Silero VAD from
  faster-whisper, or a loudness fallback) and cuts in the middle of it, never mid-word.
  A pause of :data:`GOOD_PAUSE_S` will do at once; a shorter one (:data:`MIN_PAUSE_S`)
  after :data:`LOOKAHEAD_S` more seconds; with no pause by :data:`MAX_STRETCH_S` it cuts
  at the quietest moment (a stretch then still fits one 30 s Whisper window);
* the stretch is transcribed with the vocabulary hints and the previous stretch's last
  sentence as context, then cleaned up by the LLM on a second thread, so the next stretch
  can start transcribing meanwhile.

On ``finish`` only the tail is left: transcribe and clean it, join everything, and deliver
it exactly like ``POST /transcribe`` (:func:`shoutabout.pipeline.deliver_result`). Its
timings are the wait after "stop"; the background totals go in the log line.

Background stretches take the pipeline lock one at a time and give way to any job that
waits for it (:meth:`Pipeline.background_turn`), and they never set the hub status: the
tray shows "transcribing" only after the person stops talking. A stretch that fails is
simply left for ``finish``, which transcribes everything not done yet.

:class:`StreamRegistry` holds the phone's streams for the HTTP endpoints: random ids, at
most :data:`MAX_OPEN_STREAMS` open, discarded after :data:`IDLE_TIMEOUT_S` without a
request, finished results kept :data:`FINISHED_TTL_S` (``finish`` is idempotent). Audio
lives in memory only and is dropped as soon as a stream finishes or is cancelled. Log
lines never contain text.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from shoutabout.engine.base import SAMPLE_RATE, EngineError
from shoutabout.models import CleanupResult, Timings, TranscribeResult

log = logging.getLogger(__name__)

# --- the phone protocol
MAX_CHUNK_BYTES = 256 * 1024  # 8.2 s of 16 kHz s16le
MAX_OPEN_STREAMS = 2
IDLE_TIMEOUT_S = 60.0  # no request for this long: the stream is discarded
FINISHED_TTL_S = 120.0  # a finished stream's result is kept this long (idempotent finish)

# --- where stretches are cut
FRAME = 512  # samples per analysis frame (32 ms; what Silero VAD takes)
MIN_STRETCH_S = 15.0  # never cut a stretch shorter than this
GOOD_PAUSE_S = 0.6  # a pause this long after MIN_STRETCH_S: cut there at once
LOOKAHEAD_S = 8.0  # none by MIN_STRETCH_S + this: a MIN_PAUSE_S pause will do
MIN_PAUSE_S = 0.3
MAX_STRETCH_S = 28.0  # no pause by then: cut at the quietest moment
CHECK_EVERY_S = 1.0  # look for a pause at most this often (the microphone feeds every 30 ms)
SILENCE_PROB = 0.35  # Silero speech probability under this = silence
SILENCE_DB = -50.0  # loudness fallback: always silence under this (dBFS)

# --- context for the next stretch
USE_CONTEXT = True
CONTEXT_MIN_WORDS = 8
CONTEXT_MAX_WORDS = 30

WAIT_S = 1.0  # the worker wakes this often (idle expiry)

OPEN, FINISHING, FINISHED, CANCELLED = "open", "finishing", "finished", "cancelled"

Detector = Callable[[np.ndarray], np.ndarray]  # samples -> one bool per FRAME (True = silent)


# ------------------------------------------------------------------ errors


class StreamError(Exception):
    """A stream request the server answers with ``status`` (and ``next_seq`` for 409)."""

    status = 400

    def __init__(self, detail: str, *, next_seq: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.next_seq = next_seq


class StreamGone(StreamError):
    status = 404


class MissingChunk(StreamError):
    status = 409


class StreamTooLong(StreamError):
    status = 413


class BadChunk(StreamError):
    status = 422


class TooManyStreams(StreamError):
    status = 429


# ------------------------------------------------------------------ pauses


def _frames(audio: np.ndarray) -> np.ndarray:
    """Whole frames only (a partial last frame would look like silence)."""
    n = (len(audio) // FRAME) * FRAME
    return np.asarray(audio[:n], dtype=np.float32).reshape(-1, FRAME)


def _frame_db(audio: np.ndarray) -> np.ndarray:
    frames = _frames(audio)
    if frames.size == 0:
        return np.zeros(0)
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    return 20.0 * np.log10(rms + 1e-10)


def energy_silence(audio: np.ndarray) -> np.ndarray:
    """Silent frames by loudness (the fallback when Silero VAD can't be used): under
    :data:`SILENCE_DB`, or well below this stretch's speech and near its noise floor."""
    db = _frame_db(audio)
    if db.size == 0:
        return np.zeros(0, dtype=bool)
    loud = float(np.percentile(db, 90))
    quiet = max(float(np.percentile(db, 10)), -80.0)
    threshold = max(min(quiet + 10.0, loud - 15.0), SILENCE_DB)
    return db < threshold


class SileroSilence:
    """Silent frames by Silero VAD (the model faster-whisper ships and the engine already
    uses for its own VAD filter). CPU only, a few milliseconds per second of audio."""

    def __init__(self) -> None:
        from shoutabout.avshim import ensure_av_importable

        ensure_av_importable()  # faster_whisper imports PyAV at import time
        from faster_whisper.vad import get_vad_model

        self._model = get_vad_model()

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        frames = _frames(audio)
        if frames.size == 0:
            return np.zeros(0, dtype=bool)
        # A fresh array: the model writes into its input.
        probs = np.asarray(self._model(frames.reshape(-1).copy())).reshape(-1)
        return probs[: len(frames)] < SILENCE_PROB


_default_lock = threading.Lock()
_default: Detector | None = None


def default_detector() -> Detector:
    """Silero VAD when faster-whisper can load it, else :func:`energy_silence`."""
    global _default
    with _default_lock:
        if _default is None:
            try:
                _default = SileroSilence()
                log.debug("streamed dictations: pauses found with Silero VAD")
            except Exception as exc:
                log.info("streamed dictations: Silero VAD unavailable (%s); using loudness", exc)
                _default = energy_silence
        return _default


def _runs(silent: np.ndarray) -> list[tuple[int, int]]:
    """``[start, end)`` frame ranges of consecutive True values."""
    if silent.size == 0:
        return []
    padded = np.concatenate(([False], silent.astype(bool), [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def find_cut(audio: np.ndarray, silent: np.ndarray) -> int | None:
    """Where to end the next stretch of ``audio`` (samples from its start), or None to
    wait for more. ``silent`` has one value per whole :data:`FRAME`.

    The cut is in the middle of a pause (at least half a pause away from speech), never
    before :data:`MIN_STRETCH_S`: a :data:`GOOD_PAUSE_S` pause at once, a
    :data:`MIN_PAUSE_S` one once :data:`LOOKAHEAD_S` more seconds have arrived; past
    :data:`MAX_STRETCH_S` the longest silence, or else the quietest frame in between.
    """
    n = len(audio)
    lo, hi = int(MIN_STRETCH_S * SAMPLE_RATE), int(MAX_STRETCH_S * SAMPLE_RATE)
    if n < lo:
        return None
    runs = [(a * FRAME, min(b * FRAME, n)) for a, b in _runs(np.asarray(silent))]

    def best(min_len: int) -> int | None:
        found: list[tuple[int, int, int]] = []
        for a, b in runs:
            if b - a < min_len:
                continue
            half = min_len // 2
            first, last = max(a + half, lo), min(b - half, hi, n)
            if first > last:
                continue
            cut = min(max((a + b) // 2, first), last)
            found.append((b - a, -a, cut))  # the longest pause; the earliest of equals
        return max(found)[2] if found else None

    cut = best(int(GOOD_PAUSE_S * SAMPLE_RATE))
    if cut is None and n >= lo + int(LOOKAHEAD_S * SAMPLE_RATE):
        cut = best(int(MIN_PAUSE_S * SAMPLE_RATE))
    if cut is not None or n < hi:
        return cut
    cut = best(FRAME)  # any silence at all
    if cut is not None:
        return cut
    db = _frame_db(audio[:hi])
    first = lo // FRAME
    if db.size <= first:
        return hi
    smooth = np.convolve(db, np.ones(3) / 3.0, mode="same")
    quietest = first + int(np.argmin(smooth[first:]))
    return min(hi, quietest * FRAME + FRAME // 2)


# ------------------------------------------------------------------ text


# Words that start a stretch in mid-sentence ("..., and then") are lower-cased when the
# previous stretch didn't end a sentence; anything else (names, "I") is left alone.
_CONTINUATION_WORDS = frozenset(
    {
        "and", "but", "or", "so", "because", "then", "which", "that", "who", "whom",
        "whose", "where", "when", "while", "if", "than", "to", "of", "in", "on", "at",
        "for", "with", "from", "by", "as", "the", "a", "an", "it", "its", "is", "was",
        "are", "were",
    }
)  # fmt: skip
_SENTENCE_END = re.compile(r"""[.!?…]["'”’»)\]]*$""")
_SENTENCES = re.compile(r"(?<=[.!?…])\s+")


def join_pieces(parts: Sequence[str]) -> str:
    """The stretches' texts as one: single spaces, and a stretch that continues a
    sentence doesn't start with a capital ("so, And then" -> "so, and then")."""
    out = ""
    for part in parts:
        part = " ".join((part or "").split())
        if not part:
            continue
        if out and not _SENTENCE_END.search(out):
            word = part.split(" ", 1)[0]
            bare = word.rstrip(",;:.!?")
            if bare[:1].isupper() and bare[1:].islower() and bare.lower() in _CONTINUATION_WORDS:
                part = part[0].lower() + part[1:]
        out = f"{out} {part}" if out else part
    return out


def tail_context(text: str) -> str | None:
    """The last sentence(s) of ``text``: at least :data:`CONTEXT_MIN_WORDS` words when
    there are, at most :data:`CONTEXT_MAX_WORDS`."""
    text = " ".join((text or "").split())
    if not text:
        return None
    chosen: list[str] = []
    count = 0
    for sentence in reversed(_SENTENCES.split(text)):
        words = len(sentence.split())
        if chosen and count + words > CONTEXT_MAX_WORDS:
            break
        chosen.insert(0, sentence)
        count += words
        if count >= CONTEXT_MIN_WORDS:
            break
    words = " ".join(chosen).split()
    return " ".join(words[-CONTEXT_MAX_WORDS:]) or None


def merge_cleanups(results: Sequence[CleanupResult]) -> CleanupResult:
    """One cleanup result for the whole dictation: ``fallback`` when any stretch fell back
    (with its reason), else ``applied`` when any was cleaned, else disabled / skipped."""
    text = join_pieces([r.text for r in results])
    duration = sum(r.duration_ms for r in results)
    fallback = next((r for r in results if r.status == "fallback"), None)
    if fallback is not None:
        return CleanupResult(text, "fallback", fallback.reason, duration)
    statuses = {r.status for r in results}
    if "applied" in statuses:
        return CleanupResult(text, "applied", None, duration)
    if "disabled" in statuses or not results:
        return CleanupResult(text, "disabled", None, duration)
    return CleanupResult(text, "skipped", "empty transcript", duration)


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """Signed 16-bit little-endian mono PCM as float32 in [-1, 1)."""
    if not pcm or len(pcm) % 2:
        raise BadChunk("send 16-bit PCM: a non-empty, even number of bytes")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) * np.float32(1.0 / 32768.0)


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


# ------------------------------------------------------------------ buffer


class _Samples:
    """A growing float32 buffer (amortised doubling)."""

    def __init__(self) -> None:
        self._data = np.zeros(0, dtype=np.float32)
        self.n = 0

    def append(self, samples: np.ndarray) -> None:
        need = self.n + len(samples)
        if need > len(self._data):
            grown = np.empty(max(need, 2 * len(self._data), 30 * SAMPLE_RATE), dtype=np.float32)
            grown[: self.n] = self._data[: self.n]
            self._data = grown
        self._data[self.n : need] = samples
        self.n = need

    def get(self, start: int, end: int) -> np.ndarray:
        return self._data[start:end].copy()

    def view(self) -> np.ndarray:
        return self._data[: self.n]

    def clear(self) -> None:
        self._data = np.zeros(0, dtype=np.float32)
        self.n = 0


@dataclass
class Stretch:
    """One background-transcribed stretch: samples ``[start, end)`` of the dictation."""

    start: int
    end: int
    raw: str
    stt_ms: float
    cleaned: CleanupResult | None = None
    cleanup_ms: float = 0.0
    future: Future[None] | None = field(default=None, repr=False)

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / SAMPLE_RATE


# ------------------------------------------------------------------ session


class StreamSession:
    """One dictation transcribed while it is recorded. Thread-safe.

    Feed it with :meth:`append` (the phone's numbered PCM chunks) or :meth:`feed` (float
    samples from the microphone), then :meth:`finish` (delivers it, once) or
    :meth:`cancel`."""

    def __init__(
        self,
        pipeline: Any,
        *,
        target: str = "pc",
        route: str = "local",
        extra: dict[str, Any] | None = None,
        owner: str | None = None,
        max_seconds: float | None = None,
        idle_timeout_s: float | None = None,
        detector: Detector | None = None,
        cleanup: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        background: bool = True,
        stream_id: str | None = None,
    ) -> None:
        self.id = stream_id or secrets.token_urlsafe(24)
        self.target = target
        self.route = route
        self.owner = owner
        self._pipeline = pipeline
        self._extra = dict(extra or {})
        self._max = int(max_seconds * SAMPLE_RATE) if max_seconds else None
        self._idle_timeout = idle_timeout_s
        self._detector = detector
        self._cleanup = cleanup
        self._clock = clock
        self._min = int(MIN_STRETCH_S * SAMPLE_RATE)
        self._cond = threading.Condition()
        self._samples = _Samples()
        self._cut = 0  # samples before this are in stretches
        self._stretches: list[Stretch] = []
        self._next_seq = 0
        self._next_check = self._min
        self._state = OPEN
        self._background_ok = True
        self._executor: ThreadPoolExecutor | None = None
        self._waiting = False  # the worker waits for more audio (nothing to do)
        self._done = threading.Event()
        self._result: TranscribeResult | None = None
        self._error: BaseException | None = None
        self.delivered = False  # the result went through delivery (pasted or not)
        self.last_seen = clock()
        self.finished_at: float | None = None
        self._worker: threading.Thread | None = None
        if background:
            self._worker = threading.Thread(target=self._run, name="stream", daemon=True)
            self._worker.start()

    # -------------------------------------------------------------- info

    @property
    def state(self) -> str:
        return self._state

    @property
    def received_s(self) -> float:
        return self._samples.n / SAMPLE_RATE

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def stretches(self) -> list[Stretch]:
        with self._cond:
            return list(self._stretches)

    def idle_for(self, now: float | None = None) -> float:
        return (self._clock() if now is None else now) - self.last_seen

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Wait until the background has done all it can with the audio so far (every
        stretch it could cut is transcribed and cleaned). For tests and benchmarks."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not (self._waiting or self._stopping() or self._worker is None):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.05))
            futures = [s.future for s in self._stretches if s.future is not None]
        for future in futures:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                return False
        return True

    # -------------------------------------------------------------- feeding

    def append(self, seq: int, pcm: bytes) -> tuple[int, float]:
        """Chunk ``seq`` of the phone's PCM. Returns ``(next_seq, received_s)``; a chunk
        that already arrived is ignored. Raises :class:`StreamError`."""
        samples = pcm_to_float(pcm)
        with self._cond:
            if self._state != OPEN:
                raise StreamGone("unknown stream")
            self.last_seen = self._clock()
            if seq < self._next_seq:  # a retry of a chunk that arrived
                return self._next_seq, self.received_s
            if seq > self._next_seq:
                raise MissingChunk("missing chunk", next_seq=self._next_seq)
            if self._max is not None and self._samples.n + len(samples) > self._max:
                raise StreamTooLong(f"audio too long (max {self._max // SAMPLE_RATE} s)")
            self._samples.append(samples)
            self._next_seq += 1
            self._wake()
            return self._next_seq, self.received_s

    def feed(self, samples: np.ndarray) -> None:
        """Samples from the microphone (16 kHz mono float32). Quick; never raises."""
        try:
            block = np.asarray(samples, dtype=np.float32).reshape(-1)
            with self._cond:
                if self._state != OPEN:
                    return
                if self._max is not None:
                    block = block[: max(0, self._max - self._samples.n)]
                self._samples.append(block)
                self._wake()
        except Exception:
            log.debug("streamed dictation: couldn't keep a block", exc_info=True)

    def touch(self) -> None:
        with self._cond:
            self.last_seen = self._clock()

    def _wake(self) -> None:
        if self._samples.n >= self._next_check:
            self._waiting = False  # there may be work now
            self._cond.notify_all()

    # -------------------------------------------------------------- ending

    def finish(
        self, *, chunks: int | None = None, audio: np.ndarray | None = None
    ) -> TranscribeResult:
        """Transcribe what's left, join, deliver (paste for target pc, Recent) and return
        the result. Calling it again returns the same result and never delivers twice.

        ``chunks``: how many chunks the phone sent (:class:`MissingChunk` if that isn't
        what arrived). ``audio``: the whole recording (the microphone's), which wins over
        what was fed if the two differ."""
        return self.finish_detailed(chunks=chunks, audio=audio)[0]

    def finish_detailed(
        self, *, chunks: int | None = None, audio: np.ndarray | None = None
    ) -> tuple[TranscribeResult, bool]:
        """:meth:`finish`, plus whether this was a repeat (the result was already made)."""
        with self._cond:
            self.last_seen = self._clock()
            if self._state == CANCELLED:
                raise StreamGone("unknown stream")
            first = self._state == OPEN
            if first:
                if chunks is not None and chunks != self._next_seq:
                    raise MissingChunk("missing chunks", next_seq=self._next_seq)
                self._state = FINISHING
                self._cond.notify_all()
        if not first:
            self._done.wait()
            if self._error is not None:
                raise self._error
            assert self._result is not None
            return self._result, True
        try:
            result = self._finish(audio)
        except BaseException as exc:
            self._error = exc
            raise
        else:
            self._result = result
        finally:
            with self._cond:
                self._state = FINISHED
                self.finished_at = self._clock()
                self._samples.clear()  # the audio goes as soon as it's done with
                executor, self._executor = self._executor, None
            self._done.set()
            if executor is not None:
                executor.shutdown(wait=False)
        return result, False

    def cancel(self, reason: str = "cancelled") -> bool:
        """Throw the dictation away (nothing is delivered). False if it already finished
        or is finishing."""
        with self._cond:
            if self._state != OPEN:
                return False
            self._state = CANCELLED
            seconds = self.received_s
            self._samples.clear()
            self._stretches = []
            executor, self._executor = self._executor, None
            self._cond.notify_all()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        log.info(
            "stream %s route=%s chunks=%d audio_s=%.1f", reason, self.route, self._next_seq, seconds
        )
        return True

    # -------------------------------------------------------------- background

    def _stopping(self) -> bool:
        return self._state != OPEN or not self._background_ok

    def _run(self) -> None:
        try:
            while True:
                expired = False
                with self._cond:
                    while True:
                        if self._stopping():
                            return
                        if self._idle_timeout is not None and self.idle_for() > self._idle_timeout:
                            expired = True
                            break
                        pending = self._samples.n - self._cut
                        if pending >= self._min and self._samples.n >= self._next_check:
                            break
                        self._waiting = True
                        self._cond.notify_all()  # wait_idle()
                        self._cond.wait(WAIT_S)
                    self._waiting = False
                    if not expired:
                        start = self._cut
                        end = min(self._samples.n, start + int(MAX_STRETCH_S * SAMPLE_RATE))
                        window = self._samples.get(start, end)
                        newest = self._samples.n
                if expired:
                    self.cancel("expired")
                    return
                cut = find_cut(window, self._silence(window))
                if cut is None:
                    with self._cond:
                        self._next_check = newest + int(CHECK_EVERY_S * SAMPLE_RATE)
                    continue
                self._transcribe(start, start + cut, window[:cut])
        except Exception:
            log.exception("streamed dictation: background work failed; finishing does it all")
            with self._cond:
                self._background_ok = False

    def _silence(self, audio: np.ndarray) -> np.ndarray:
        detector = self._detector or default_detector()
        try:
            return detector(audio)
        except Exception:
            log.warning("streamed dictation: pause detection failed; using loudness", exc_info=True)
            return energy_silence(audio)

    def _transcribe(self, start: int, end: int, audio: np.ndarray) -> None:
        """One background stretch: STT under the pipeline lock (after anyone waiting),
        then the cleanup on the cleanup thread."""
        pipeline = self._pipeline
        if not pipeline.background_turn(self._stopping):
            return
        try:
            if self._stopping():
                return
            t = time.perf_counter()
            raw = pipeline.stt(audio, context=self._context())
            stt_ms = _ms(t)
        except EngineError as exc:
            log.warning("streamed dictation: a stretch failed (%s); finishing will retry it", exc)
            with self._cond:
                self._background_ok = False
            return
        finally:
            pipeline.release_turn()
        stretch = Stretch(start, end, raw, stt_ms)
        with self._cond:
            if self._state == CANCELLED:
                return
            self._stretches.append(stretch)
            self._cut = end
            self._next_check = end + self._min
            if self._executor is None:
                self._executor = ThreadPoolExecutor(1, thread_name_prefix="stream-cleanup")
            stretch.future = self._executor.submit(self._clean, stretch)
        log.debug(
            "streamed dictation: stretch %d (%.1f s) stt=%.0f ms",
            len(self._stretches),
            stretch.seconds,
            stt_ms,
        )

    def _clean(self, stretch: Stretch) -> None:
        t = time.perf_counter()
        stretch.cleaned = self._pipeline.clean(stretch.raw, self._cleanup)
        stretch.cleanup_ms = _ms(t)

    def _context(self) -> str | None:
        if not USE_CONTEXT:
            return None
        with self._cond:
            last = self._stretches[-1].raw if self._stretches else ""
        return tail_context(last)

    # -------------------------------------------------------------- finish

    def _reconcile(self, audio: np.ndarray) -> None:
        """The microphone's whole recording: append what wasn't fed; if what was fed
        differs from it, start over with the recording (transcribed as one tail)."""
        full = np.asarray(audio, dtype=np.float32).reshape(-1)
        n = self._samples.n
        if len(full) >= n and np.array_equal(self._samples.view(), full[:n]):
            self._samples.append(full[n:])
            return
        log.warning("streamed dictation: the recording differs from what was fed; redoing it")
        self._samples.clear()
        self._samples.append(full)
        self._cut = 0
        self._stretches = []

    def _finish(self, audio: np.ndarray | None) -> TranscribeResult:
        t0 = time.perf_counter()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()  # lets a stretch that is transcribing right now complete
        with self._cond:
            if audio is not None:
                self._reconcile(audio)
            stretches = list(self._stretches)
            tail = self._samples.get(self._cut, self._samples.n)
            total = self._samples.n
            chunks = self._next_seq
        context = tail_context(stretches[-1].raw) if (USE_CONTEXT and stretches) else None
        pipeline = self._pipeline

        def job() -> TranscribeResult:
            timings = Timings()
            raw_tail = pipeline.stt(tail, context=context) if tail.size else ""
            timings.stt = _ms(t0)  # includes waiting for a background stretch
            t = time.perf_counter()
            cleaned_tail = pipeline.clean(raw_tail, self._cleanup)
            cleaned = [self._cleaned(s) for s in stretches] + [cleaned_tail]
            timings.cleanup = _ms(t)
            raw_text = join_pieces([s.raw for s in stretches] + [raw_tail])
            extra = {
                **self._extra,
                "streamed": True,
                "chunks": chunks,
                "stretches": len(stretches),
                "bg_stt_ms": sum(s.stt_ms for s in stretches),
                "bg_cleanup_ms": sum(s.cleanup_ms for s in stretches),
            }
            result = pipeline.deliver(
                raw_text=raw_text,
                cleaned=merge_cleanups(cleaned),
                target=self.target,
                route=self.route,
                deliver=True,
                timings=timings,
                started=t0,
                audio_seconds=total / SAMPLE_RATE,
                extra=extra,
            )
            self.delivered = True
            return result

        return pipeline.run_job(job)

    @staticmethod
    def _cleaned(stretch: Stretch) -> CleanupResult:
        try:
            if stretch.future is not None:
                stretch.future.result()
        except Exception as exc:
            log.warning("streamed dictation: a stretch's cleanup failed (%s)", type(exc).__name__)
        if stretch.cleaned is None:
            return CleanupResult(stretch.raw, "fallback", "cleanup didn't run")
        return stretch.cleaned


# ------------------------------------------------------------------ registry


class StreamRegistry:
    """The phone's open streams (``/stream/*``): ids, the open-stream limit, expiry."""

    def __init__(
        self,
        *,
        max_open: int = MAX_OPEN_STREAMS,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        finished_ttl_s: float = FINISHED_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        detector: Detector | None = None,
    ) -> None:
        self.max_open = max_open
        self.idle_timeout_s = idle_timeout_s
        self.finished_ttl_s = finished_ttl_s
        self.clock = clock
        self.detector = detector
        self._lock = threading.Lock()
        self._sessions: dict[str, StreamSession] = {}

    def start(
        self,
        pipeline: Any,
        *,
        target: str,
        route: str,
        extra: dict[str, Any] | None = None,
        owner: str | None = None,
        max_seconds: float | None = None,
    ) -> StreamSession:
        """A new stream. Raises :class:`TooManyStreams`."""
        with self._lock:
            self._sweep()
            busy = sum(1 for s in self._sessions.values() if s.state in (OPEN, FINISHING))
            if busy >= self.max_open:
                raise TooManyStreams(f"too many open streams (max {self.max_open})")
            session = pipeline.open_stream(
                target=target,
                route=route,
                extra=extra,
                owner=owner,
                max_seconds=max_seconds,
                idle_timeout_s=self.idle_timeout_s,
                detector=self.detector,
                clock=self.clock,
            )
            self._sessions[session.id] = session
        log.info("stream started route=%s target=%s", route, target)
        return session

    def get(self, stream_id: str, owner: str | None = None) -> StreamSession:
        """The stream (and a request for it keeps it alive). Raises :class:`StreamGone`
        for unknown, expired, cancelled, forgotten or someone else's streams."""
        with self._lock:
            self._sweep()
            session = self._sessions.get(stream_id)
        if session is None or session.owner != owner:
            raise StreamGone("unknown stream")
        session.touch()
        return session

    def cancel(self, stream_id: str, owner: str | None = None) -> bool:
        """Discard a stream and its audio. False if there was none (for this owner)."""
        with self._lock:
            session = self._sessions.get(stream_id)
            if session is None or session.owner != owner:
                return False
            del self._sessions[stream_id]
        session.cancel()
        return True

    def close(self) -> None:
        """Cancel every open stream (the hub is stopping)."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.cancel("stopped")

    def sweep(self) -> None:
        with self._lock:
            self._sweep()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _sweep(self) -> None:
        now = self.clock()
        for stream_id, session in list(self._sessions.items()):
            state = session.state
            if state == CANCELLED:
                del self._sessions[stream_id]
            elif state == OPEN and session.idle_for(now) > self.idle_timeout_s:
                session.cancel("expired")
                del self._sessions[stream_id]
            elif (
                state == FINISHED
                and session.finished_at is not None
                and now - session.finished_at > self.finished_ttl_s
            ):
                del self._sessions[stream_id]
