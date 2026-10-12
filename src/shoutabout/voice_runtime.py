"""The hub's voice runtime: model choice, first-run model download, cleanup and
"Talk to this PC", behind one object so ``hub.py`` only needs a few calls.

Lifecycle (see :func:`shoutabout.hub.run_hub`)::

    voice = VoiceRuntime(cfg, state, notify)
    voice.choose()                    # main thread, before the threads start (fast)
    voice.start_dictation(lambda: ctx.pipeline)
    ... engine thread:  engine = voice.create_engine(stopping)  # downloads if needed
    ... warmer thread:  voice.poll_cleanup(ctx.pipeline)        # "auto" cleanup
    voice.stop()                      # on exit

``state.choice`` holds the :class:`~shoutabout.modelpick.ModelChoice` (for ``GET /info``
and the settings window), ``state.progress`` the download percent (``state.download``
has the bytes, ``state.progress_text`` says "Getting ready… 42%"). After the settings
change, call ``voice.reload_dictation(cfg)``: it applies the new [dictation] settings, and
also starts "Talk to this PC" if it was off when the hub started.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from shoutabout import modelpick, modelstore
from shoutabout.config import Config, SttConfig
from shoutabout.engine.base import EngineError, SpeechEngine
from shoutabout.modelpick import ModelChoice
from shoutabout.state import HubState, Progress

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]


class VoiceRuntime:
    def __init__(
        self, cfg: Config, state: HubState, notify: Notify | None = None, *, recent: Any = None
    ) -> None:
        self.cfg = cfg
        self.state = state
        self._notify = notify
        self.recent = recent  # recent.Recent: failed recordings kept for Recent > Try again
        self.choice: ModelChoice | None = None
        self.cleaner: Any = None
        self.dictation: Any = None
        self._pipeline: Callable[[], Any] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ choice

    def choose(self) -> ModelChoice:
        """Decide the model setup (explicit config values win) and build the cleaner."""
        cfg = self.cfg
        choice = modelpick.resolve(cfg)
        if choice.source != "config":
            from shoutabout.hardware import detect

            log.info("hardware: %s", detect().summary())
        log.info("speech model: %s", choice.summary)
        self.choice = choice
        self.state.set_choice(choice)
        self.cleaner = self.make_cleaner(choice)
        return choice

    def make_cleaner(self, choice: ModelChoice) -> Any:
        if not choice.cleanup or not choice.cleanup_model:
            return None
        from shoutabout.cleanup import OllamaCleaner

        cc = self.cfg.cleanup
        if choice.cleanup_model != cc.model:
            cc = cc.model_copy(update={"model": choice.cleanup_model})
        return OllamaCleaner(cc)

    @property
    def cleanup_pending(self) -> bool:
        """True when "auto" cleanup is off only because Ollama (or a model) is missing."""
        choice = self.choice
        if choice is None or self.cleaner is not None:
            return False
        if not modelpick.is_auto(self.cfg.cleanup.enabled):
            return False
        hw_gpu = None
        if choice.device == "cuda":
            from shoutabout.hardware import detect

            hw_gpu = detect().best_gpu
        return modelpick.cleanup_affordable(choice.tier, hw_gpu)

    def poll_cleanup(self, pipeline: Any = None) -> Any:
        """For "auto" cleanup: if Ollama now has a suitable model, turn cleanup on (and hand
        the cleaner to the running pipeline). Returns the cleaner or None."""
        if self.cleaner is not None or not self.cleanup_pending:
            return self.cleaner
        cfg = self.cfg
        pulled = modelpick.ollama_models(cfg.cleanup.ollama_url)
        model = modelpick.pick_cleanup_model(pulled, cfg.cleanup.model)
        if model is None or self.choice is None:
            return None
        choice = modelpick.with_cleanup(
            self.choice, True, model, "Text tidying is on (local Ollama)."
        )
        with self._lock:
            self.choice = choice
            self.cleaner = self.make_cleaner(choice)
            if pipeline is not None:
                pipeline.cleaner = self.cleaner
        self.state.set_choice(choice)
        log.info("Ollama is up: text tidying turned on with %s", model)
        return self.cleaner

    # ------------------------------------------------------------------- model

    def prepare(self, stopping: threading.Event | None = None) -> tuple[SttConfig, int]:
        """The concrete [stt] config, after downloading the model if it isn't here yet.
        Progress goes to ``state.progress`` / ``state.download``. Raises EngineError with
        a plain message."""
        choice = self.choice or self.choose()
        stt = modelpick.concrete_stt(self.cfg.stt, choice)
        cache = stt.download_root or None
        name = stt.model
        if modelstore.repo_id(name) is not None and not modelstore.is_ready(name, cache_dir=cache):
            size = modelstore.download_mb(name)
            log.info(
                "the speech model %s isn't downloaded yet (%s MB); downloading", name, size or "?"
            )
            self.state.set_progress(Progress(0, size * 1_000_000))
            try:
                modelstore.download(
                    name,
                    progress=lambda done, total: self.state.set_progress(Progress(done, total)),
                    cancel=stopping,
                    cache_dir=cache,
                )
            except modelstore.ModelStoreError as exc:
                raise EngineError(str(exc)) from exc
        return stt, choice.cpu_threads

    def create_engine(self, stopping: threading.Event | None = None) -> SpeechEngine:
        from shoutabout.engine import create_engine

        stt, threads = self.prepare(stopping)
        return create_engine(stt, cpu_threads=threads)

    # --------------------------------------------------------------- dictation

    def start_dictation(self, pipeline: Callable[[], Any]) -> Any:
        """Start "Talk to this PC" when [dictation] enabled; follows Pause. Never raises."""
        self._pipeline = pipeline
        if not self.cfg.dictation.enabled:
            log.info("talk to this PC is off ([dictation] enabled = false)")
            return None
        try:
            from shoutabout.local_dictation import LocalDictation
            from shoutabout.mac.platform import (  # v2-macos (no-ops elsewhere)
                dictation_factories,
                logged_pipeline,
            )

            self.dictation = LocalDictation(
                self.cfg,
                state=self.state,
                pipeline=logged_pipeline(pipeline),
                notify=self._notify,
                recent=self.recent,
                **dictation_factories(),
            )
            self.dictation.follow_pause()
            if not self.state.paused:
                self.dictation.start()
        except Exception:
            log.exception("couldn't start talk to this PC")
        return self.dictation

    def reload_dictation(self, cfg: Config) -> None:
        """Apply new [dictation] settings (hotkey, mode, overlay, on/off). Never raises."""
        self.cfg = self.cfg.model_copy(update={"dictation": cfg.dictation})
        if self.dictation is None:
            if cfg.dictation.enabled and self._pipeline is not None:
                self.start_dictation(self._pipeline)
            return
        try:
            self.dictation.reload(self.cfg)
        except Exception:
            log.exception("reloading talk to this PC failed")

    def stop(self) -> None:
        if self.dictation is not None:
            try:
                self.dictation.stop()
            except Exception:
                log.exception("stopping talk to this PC failed")
