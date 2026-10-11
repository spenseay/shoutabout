"""faster-whisper (CTranslate2) engine: Whisper large-v3-turbo on CUDA by default.

Decode settings and why:

* ``condition_on_previous_text=False``: dictations are short; conditioning mostly
  propagates errors/hallucinations from one 30 s window to the next.
* ``initial_prompt`` therefore only biases the first 30 s window, while ``hotwords`` is
  added to *every* window's prompt (unless a ``prefix`` is set, which we never do).
  Whisper's decoder context is 448 tokens and the new-token budget per window is
  ``min(224, 448 - prompt_len)``, so the vocab text (hotwords + initial prompt) must stay
  under ~200 tokens in total (``[stt] max_prompt_tokens``; see shoutabout.vocab).
* ``without_timestamps=False``: timestamp mode lets long audio be split at segment
  boundaries instead of mid-word at the 30 s boundary.
* temperature fallback is capped at 0.4: higher temperatures rarely fix anything for
  dictation and each fallback re-decodes the window.
* VAD (silero) drops silence, which is also the main defence against Whisper
  "hallucinating" text (often the vocab hint itself) on silent or noise-only audio.
  Segments that are only an echo of the hint text, or that Whisper itself flags as
  no-speech with low confidence, are dropped as well.
* ``chunk_length`` is never passed: faster-whisper stores it on the shared feature
  extractor, so per-call values race between threads.
"""

from __future__ import annotations

import gc
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from shoutabout.engine.base import SAMPLE_RATE, EngineError, SttHints
from shoutabout.engine.cuda_dlls import add_nvidia_dll_dirs

if TYPE_CHECKING:
    from shoutabout.config import SttConfig

log = logging.getLogger(__name__)

TEMPERATURES = (0.0, 0.2, 0.4)
VAD_SPEECH_PAD_MS = 300
# Standard Whisper "no speech" heuristic (same thresholds faster-whisper uses per window).
NO_SPEECH_PROB = 0.6
LOW_AVG_LOGPROB = -1.0
_REQUIRED_MODEL_FILES = ("model.bin", "config.json")
_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)

FIX_HINT = (
    "Run `python -m shoutabout doctor`. Check the NVIDIA driver and that "
    "`uv sync --extra cuda` installed nvidia-cublas-cu12. To run on the CPU (slow), set "
    '[stt] device = "cpu" or cpu_fallback = true in config.toml.'
)


def heuristic_token_count(text: str) -> int:
    """Whisper-token estimate used before the tokenizer is loaded (errs on the high side)."""
    s = text.strip()
    if not s:
        return 0
    return max(len(s.split()), math.ceil(len(s) / 3.5))


@dataclass(frozen=True)
class _Backend:
    """The faster-whisper entry points (a seam for tests)."""

    whisper_model: Callable[..., Any]
    cuda_device_count: Callable[[], int]
    download_model: Callable[..., str]
    available_models: Callable[[], list[str]]


def _import_backend() -> _Backend:
    # Imported lazily so the CUDA DLL folders are registered first and so importing this
    # module (e.g. in unit tests or the CLI) doesn't load CTranslate2.
    from shoutabout.avshim import ensure_av_importable

    # faster-whisper imports PyAV at import time; the Windows app doesn't ship it (it is
    # only used by faster-whisper's own file decoder, and the engine always gets samples).
    ensure_av_importable()
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
        from faster_whisper.utils import available_models, download_model
    except ImportError as exc:
        raise EngineError(
            f"faster-whisper is not installed ({exc}). Run `uv sync` in the repo."
        ) from exc
    return _Backend(
        whisper_model=WhisperModel,
        cuda_device_count=ctranslate2.get_cuda_device_count,
        download_model=download_model,
        available_models=available_models,
    )


# ------------------------------------------------------------------ hint-echo filter


def _norm_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.sub(" ", text.casefold()).split() if w]


class _HintEcho:
    """Detects output that merely repeats the vocabulary hint text.

    On silence/noise Whisper sometimes "transcribes" its prompt, e.g. the output is
    ``"Better Shell, Claude Code, Claude, Anthropic"`` or ``"In this dictation I mention
    Better Shell"``. Output counts as an echo when its words are a contiguous run of a
    hint text's words and that run covers at least three whole terms, or contains the
    hint's whole label ("In this dictation I mention"), or is the entire hint with two or
    more terms. A user dictating one or two of their own vocab terms is never dropped.
    """

    def __init__(self, hints: SttHints) -> None:
        from shoutabout.vocab import split_hint_text

        # (words, tags, label length); the label is always at the start
        self._hints: list[tuple[list[str], list[int], int]] = []
        seen: set[str] = set()
        for text in (hints.hotwords, hints.initial_prompt):
            if not text or not text.strip() or text in seen:
                continue
            seen.add(text)
            label, terms = split_hint_text(text)
            label_words = _norm_words(label)
            words: list[str] = list(label_words)
            tags: list[int] = [-1] * len(label_words)  # -1 = label word, else term index
            for i, term in enumerate(terms):
                for w in _norm_words(term):
                    words.append(w)
                    tags.append(i)
            if words:
                self._hints.append((words, tags, len(label_words)))

    def __bool__(self) -> bool:
        return bool(self._hints)

    def matches(self, text: str) -> bool:
        out = _norm_words(text)
        if not out:
            return False
        n = len(out)
        for words, tags, label_len in self._hints:
            for start in range(len(words) - n + 1):
                if words[start : start + n] != out:
                    continue
                end = start + n
                covered = {
                    t
                    for t in set(tags[start:end])
                    if t >= 0 and all(start <= i < end for i, tag in enumerate(tags) if tag == t)
                }
                whole_label = label_len > 0 and start == 0 and n >= label_len
                whole_hint = start == 0 and n == len(words)
                if len(covered) >= 3 or whole_label or (whole_hint and len(covered) >= 2):
                    return True
        return False


def filter_segments(segments: Sequence[Any], hints: SttHints) -> str:
    """Join segment texts, dropping no-speech segments and echoes of the hint text."""
    echo = _HintEcho(hints)
    kept: list[str] = []
    for seg in segments:
        text = (getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        no_speech = getattr(seg, "no_speech_prob", 0.0) or 0.0
        avg_logprob = getattr(seg, "avg_logprob", 0.0) or 0.0
        if no_speech > NO_SPEECH_PROB and avg_logprob < LOW_AVG_LOGPROB:
            log.debug("dropped no-speech segment (p=%.2f, logprob=%.2f)", no_speech, avg_logprob)
            continue
        if echo and echo.matches(text):
            log.info(
                "dropped a segment that only repeats the vocabulary hint (%d chars)", len(text)
            )
            continue
        kept.append(text)
    joined = " ".join(kept)
    if echo and len(kept) > 1 and echo.matches(joined):
        log.info("dropped output that only repeats the vocabulary hint (%d chars)", len(joined))
        return ""
    return joined


# --------------------------------------------------------------------------- engine


class FasterWhisperEngine:
    """SpeechEngine backed by faster-whisper. Thread-safe (one transcription at a time)."""

    def __init__(self, cfg: SttConfig, *, cpu_threads: int = 0) -> None:
        self.cfg = cfg
        # CPU threads for CTranslate2 (0 = its default of 4). The automatic model choice sets
        # this from the number of physical cores (see shoutabout.modelpick).
        self.cpu_threads = max(0, int(cpu_threads))
        self.device: str = cfg.device
        self.compute_type: str = cfg.compute_type if cfg.device == "cuda" else cfg.cpu_compute_type
        self.name: str = self._make_name()
        self.fallback_reason: str | None = None  # why CUDA was abandoned (CPU fallback)
        self.load_seconds: float | None = None
        self.model_path: str | None = None
        self._model: Any = None
        self._tokenizer: Any = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ properties

    def _make_name(self) -> str:
        return f"faster-whisper:{self.cfg.model}@{self.device}/{self.compute_type}"

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------ model files

    def resolve_model_path(self, *, download: bool = True) -> str:
        """Local folder of the model; downloads it into the cache first if needed."""
        return self._resolve_model(_import_backend(), download=download)

    def _resolve_model(self, backend: _Backend, *, download: bool = True) -> str:
        name = self.cfg.model.strip()
        if os.path.isdir(name):
            return name
        if "/" not in name and name not in backend.available_models():
            raise EngineError(
                f"Unknown Whisper model {name!r} in [stt] model. Use a size such as "
                f'"large-v3-turbo", a Hugging Face repo id of a CTranslate2 model, or a '
                f"local model folder."
            )
        cache_dir = self.cfg.download_root or None
        try:
            path = backend.download_model(name, local_files_only=True, cache_dir=cache_dir)
            if all(os.path.isfile(os.path.join(path, f)) for f in _REQUIRED_MODEL_FILES):
                return path
            log.warning("cached Whisper model at %s is incomplete; re-downloading", path)
        except Exception as exc:  # not cached yet (LocalEntryNotFoundError) or bad cache
            log.debug("model %s not in the local cache: %s", name, exc)
        if not download:
            raise EngineError(
                f"Whisper model {name!r} is not downloaded yet. "
                "Run `python -m shoutabout download-model`."
            )
        where = cache_dir or "the Hugging Face cache"
        log.info("downloading Whisper model %s into %s (large-v3-turbo is ~1.6 GB)...", name, where)
        try:
            path = backend.download_model(name, cache_dir=cache_dir)
        except Exception as exc:
            raise EngineError(
                f"Whisper model {name!r} is not in the local cache and could not be "
                f"downloaded ({type(exc).__name__}: {exc}). Check the internet connection, "
                "then run `python -m shoutabout download-model`."
            ) from exc
        log.info("model downloaded to %s", path)
        return path

    # ------------------------------------------------------------------------ load

    def load(self) -> None:
        """Load + warm the model. Idempotent. Raises EngineError with a fix hint."""
        with self._lock:
            if self._model is not None:
                return
            t0 = time.perf_counter()
            if self.cfg.device == "cuda":
                dirs = add_nvidia_dll_dirs()
                log.debug("NVIDIA DLL directories: %s", dirs or "none found")
            backend = _import_backend()
            path = self._resolve_model(backend)
            if self.cfg.device == "cuda":
                model, device, compute = self._load_cuda(backend, path)
            else:
                compute = self.cfg.cpu_compute_type
                model, device = self._create(backend, path, "cpu", compute), "cpu"
            self._model = model
            self._tokenizer = getattr(model, "hf_tokenizer", None)
            self.model_path = path
            self.device, self.compute_type = device, compute
            self.name = self._make_name()
            self.load_seconds = time.perf_counter() - t0
            log.info("loaded %s in %.1f s", self.name, self.load_seconds)

    def _load_cuda(self, backend: _Backend, path: str) -> tuple[Any, str, str]:
        try:
            count = int(backend.cuda_device_count())
            reason = None if count > 0 else "CTranslate2 found no CUDA GPU"
        except Exception as exc:  # broken driver install
            reason = f"CUDA device query failed: {exc}"
        if reason is None:
            model = None
            try:
                model = backend.whisper_model(
                    path, device="cuda", compute_type=self.cfg.compute_type
                )
                self._warm_up(model)
                return model, "cuda", self.cfg.compute_type
            except Exception as exc:  # cuBLAS missing, out of memory, unsupported type...
                del model
                gc.collect()
                return self._cuda_failed(backend, path, f"{type(exc).__name__}: {exc}", exc)
        return self._cuda_failed(backend, path, reason, None)

    def _cuda_failed(
        self, backend: _Backend, path: str, reason: str, exc: BaseException | None
    ) -> tuple[Any, str, str]:
        if not self.cfg.cpu_fallback:
            raise EngineError(f"CUDA is not available ({reason}). {FIX_HINT}") from exc
        compute = self.cfg.cpu_compute_type
        log.warning(
            "CUDA is not available (%s); falling back to the CPU (%s). Transcription will be "
            "much slower. Run `python -m shoutabout doctor` to diagnose.",
            reason,
            compute,
        )
        self.fallback_reason = reason
        return self._create(backend, path, "cpu", compute), "cpu", compute

    def _create(self, backend: _Backend, path: str, device: str, compute: str) -> Any:
        try:
            kwargs: dict[str, Any] = {"device": device, "compute_type": compute}
            if device == "cpu" and self.cpu_threads:
                kwargs["cpu_threads"] = self.cpu_threads
            model = backend.whisper_model(path, **kwargs)
            self._warm_up(model)
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(
                f"Could not load Whisper model {self.cfg.model!r} on the {device.upper()} "
                f"({compute}): {type(exc).__name__}: {exc}"
            ) from exc
        return model

    def _warm_up(self, model: Any) -> None:
        """Run one decode so CUDA/cuBLAS errors surface at startup, not on first dictation."""
        noise = (np.random.default_rng(0).standard_normal(SAMPLE_RATE) * 0.003).astype(np.float32)
        t0 = time.perf_counter()
        segments, _ = model.transcribe(
            noise,
            language=self.cfg.language or None,
            beam_size=self.cfg.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,  # make the encoder + decoder actually run
        )
        for _ in segments:
            pass
        if self.cfg.vad_filter:  # also load the silero VAD model now
            segments, _ = model.transcribe(
                noise, language=self.cfg.language or None, beam_size=1, vad_filter=True
            )
            for _ in segments:
                pass
        log.debug("warm-up took %.0f ms", (time.perf_counter() - t0) * 1000)

    # ------------------------------------------------------------------ transcribe

    def decode_options(self, hints: SttHints) -> dict[str, Any]:
        cfg = self.cfg
        opts: dict[str, Any] = {
            "language": cfg.language or None,
            "beam_size": cfg.beam_size,
            "temperature": list(TEMPERATURES),
            "condition_on_previous_text": False,
            "without_timestamps": False,
            "vad_filter": cfg.vad_filter,
        }
        if cfg.vad_filter:
            opts["vad_parameters"] = {
                "min_silence_duration_ms": cfg.vad_min_silence_ms,
                "speech_pad_ms": VAD_SPEECH_PAD_MS,
            }
        hotwords = (hints.hotwords or "").strip()
        initial_prompt = (hints.initial_prompt or "").strip()
        if hotwords:
            opts["hotwords"] = hotwords
        if initial_prompt:
            opts["initial_prompt"] = initial_prompt
        return opts

    def transcribe(self, audio: np.ndarray, hints: SttHints) -> str:
        samples = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        if samples.size == 0:
            return ""
        opts = self.decode_options(hints)
        with self._lock:
            if self._model is None:
                self.load()
            try:
                segments, _info = self._model.transcribe(samples, **opts)
                segs = list(segments)  # the generator does the actual decoding
            except Exception as exc:
                raise EngineError(f"Transcription failed: {type(exc).__name__}: {exc}") from exc
        return filter_segments(segs, hints)

    def count_tokens(self, text: str) -> int:
        """Prompt tokens for ``text`` (as faster-whisper encodes it: leading space)."""
        s = text.strip()
        if not s:
            return 0
        tokenizer = self._tokenizer
        if tokenizer is None:
            return heuristic_token_count(s)
        return len(tokenizer.encode(" " + s, add_special_tokens=False).ids)
