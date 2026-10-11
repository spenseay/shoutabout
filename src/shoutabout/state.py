"""Thread-safe hub state shared by the server, pipeline, tray and "Talk to this PC"."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shoutabout.models import HubStatus

if TYPE_CHECKING:
    from shoutabout.modelpick import ModelChoice

log = logging.getLogger(__name__)

Listener = Callable[["HubState"], None]


@dataclass(frozen=True)
class Progress:
    """A first-run download (the speech model) while the hub is starting."""

    done: int  # bytes
    total: int  # bytes (0 = unknown)
    what: str = "speech model"

    @property
    def percent(self) -> int | None:
        if self.total <= 0:
            return None
        return max(0, min(100, int(self.done * 100 / self.total)))

    @property
    def text(self) -> str:
        """ "Getting ready… 42%" for the tray, setup window and overlay."""
        pct = self.percent
        return "Getting ready…" if pct is None else f"Getting ready… {pct}%"


class HubState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused = False
        self._status = HubStatus.STARTING
        self._error: str | None = None
        self._progress: Progress | None = None
        self._choice: ModelChoice | None = None
        self._listeners: list[Listener] = []
        # Held while a transcription or paste runs: phone requests, the PC's own
        # microphone and text pastes take turns (the GPU model and the focused window are
        # shared). Re-entrant so one owner can call helpers that take it again.
        self.busy = threading.RLock()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def status(self) -> HubStatus:
        with self._lock:
            return self._status

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def download(self) -> Progress | None:
        """The speech-model download (bytes) while it runs, else None."""
        with self._lock:
            return self._progress

    @property
    def progress(self) -> int | None:
        """Download percent (0-100) while preparing, else None: what the tray and setup
        window show as "Getting ready… 42%". Also None while the size isn't known yet."""
        with self._lock:
            return self._progress.percent if self._progress is not None else None

    @property
    def progress_text(self) -> str | None:
        """ "Getting ready… 42%" while preparing, else None."""
        with self._lock:
            return self._progress.text if self._progress is not None else None

    @property
    def preparing(self) -> bool:
        """True while the hub downloads what it needs before it can start."""
        with self._lock:
            return self._progress is not None and self._status == HubStatus.STARTING

    @property
    def phase(self) -> str:
        """``"preparing"`` while downloading, else the status value (``"idle"``...)."""
        with self._lock:
            if self._progress is not None and self._status == HubStatus.STARTING:
                return "preparing"
            return self._status.value

    @property
    def choice(self) -> ModelChoice | None:
        """The model setup the hub runs (shoutabout.modelpick), once decided."""
        with self._lock:
            return self._choice

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused
        log.info("hub %s", "paused" if paused else "resumed")
        self._emit()

    def toggle_paused(self) -> bool:
        with self._lock:
            self._paused = not self._paused
            paused = self._paused
        log.info("hub %s", "paused" if paused else "resumed")
        self._emit()
        return paused

    def set_status(self, status: HubStatus, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._error = error if status == HubStatus.ERROR else None
            if status != HubStatus.STARTING:
                self._progress = None
        self._emit()

    def set_progress(self, progress: Progress | None) -> None:
        """Report download progress; listeners hear about whole-percent changes only."""
        with self._lock:
            old = self._progress
            self._progress = progress
            changed = (old is None) != (progress is None) or (
                old is not None and progress is not None and old.percent != progress.percent
            )
        if changed:
            self._emit()

    def set_choice(self, choice: ModelChoice | None) -> None:
        with self._lock:
            self._choice = choice
        self._emit()

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(self)
            except Exception:  # a broken listener must never break the pipeline
                log.exception("state listener failed")
