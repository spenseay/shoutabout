"""The last few dictations, in memory only: Recent, in the ShoutAbout window.

ShoutAbout keeps no history. Its only memory of what you said is here, so text that landed
in the wrong spot can be copied again: the last :data:`KEEP` dictations meant for this
computer (newest first), and the last :data:`KEEP` recordings the speech engine couldn't
write out (a long dictation must never be lost to a hiccup), with **Try again**. Nothing
is ever written to disk, and it's all gone when ShoutAbout quits.

The hub owns one :class:`Recent`; the pipeline adds results, "Talk to this PC" and the
server add failed recordings. :attr:`Recent.version` changes with every change, so the
Recent page can watch it cheaply; on a Mac the window process gets a :meth:`snapshot`
over its link (:mod:`shoutabout.mac.ui_bridge`).
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np

from shoutabout.models import TranscribeResult

log = logging.getLogger(__name__)

KEEP = 3
SAMPLE_RATE = 16_000

Listener = Callable[[int], None]


@dataclass(frozen=True)
class RecentItem:
    id: str
    ts: str  # ISO-8601 local time with offset
    text: str
    route: str  # "local" (this computer) | "lan" | "tunnel" | "cli"
    source: str | None = None  # the paired phone's name
    pasted: bool = True
    warning: str | None = None
    recovered: bool = False  # written out later, from a failed recording


@dataclass(frozen=True)
class FailedItem:
    id: str
    ts: str
    route: str
    source: str | None
    seconds: float
    error: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Recent:
    """Thread-safe; nothing here raises (a dictation must never fail because of it)."""

    def __init__(self, keep: int = KEEP) -> None:
        self.keep = keep
        self._lock = threading.Lock()
        self._items: deque[RecentItem] = deque(maxlen=keep)
        self._failed: deque[tuple[FailedItem, np.ndarray]] = deque(maxlen=keep)
        self._ids = itertools.count(1)
        self._version = 0
        self._listeners: list[Listener] = []

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            while listener in self._listeners:
                self._listeners.remove(listener)

    def _bump(self) -> tuple[int, list[Listener]]:
        """A new version (lock held); the caller tells the listeners after releasing it."""
        self._version += 1
        return self._version, list(self._listeners)

    @staticmethod
    def _tell(version: int, listeners: list[Listener]) -> None:
        for listener in listeners:
            try:
                listener(version)
            except Exception:
                log.exception("recent listener failed")

    # ------------------------------------------------------------------ dictations

    def add(self, result: TranscribeResult) -> RecentItem | None:
        """Remember a result meant for this computer (text a phone only got back isn't
        kept: it never landed here). None when there's nothing to keep."""
        text = " ".join(str(result.text or "").split())
        if not text or result.target != "pc":
            return None
        source = result.extra.get("source")
        with self._lock:
            item = RecentItem(
                id=f"r{next(self._ids)}",
                ts=_now(),
                text=text,
                route=str(result.route),
                source=str(source) if source else None,
                pasted=bool(result.pasted),
                warning=result.warning,
                recovered=bool(result.extra.get("recovered")),
            )
            self._items.appendleft(item)
            changed = self._bump()
        self._tell(*changed)
        return item

    def items(self) -> list[RecentItem]:
        """Newest first."""
        with self._lock:
            return list(self._items)

    # --------------------------------------------------------- failed recordings

    def keep_failed(
        self, audio: np.ndarray, *, route: str = "local", source: str | None = None, error: str = ""
    ) -> FailedItem | None:
        """Keep 16 kHz mono float32 samples the speech engine couldn't write out (as 16-bit,
        half the memory). None when there's nothing to keep."""
        try:
            samples = np.asarray(audio, dtype=np.float32).reshape(-1)
            if samples.size == 0:
                return None
            pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype(np.int16)
            item = FailedItem(
                id=f"f{next(self._ids)}",
                ts=_now(),
                route=route,
                source=source,
                seconds=round(samples.size / SAMPLE_RATE, 2),
                error=" ".join(str(error).split())[:300],
            )
        except Exception:
            log.exception("couldn't keep the failed recording")
            return None
        with self._lock:
            self._failed.appendleft((item, pcm))
            changed = self._bump()
        self._tell(*changed)
        log.info("kept a recording that couldn't be written out (%.0f s, in memory)", item.seconds)
        return item

    def failed(self) -> list[FailedItem]:
        """Newest first."""
        with self._lock:
            return [item for item, _pcm in self._failed]

    def get_failed(self, item_id: str) -> FailedItem | None:
        with self._lock:
            for item, _pcm in self._failed:
                if item.id == item_id:
                    return item
        return None

    def samples(self, item_id: str) -> np.ndarray | None:
        """A failed recording's audio as 16 kHz mono float32 (for Try again)."""
        with self._lock:
            for item, pcm in self._failed:
                if item.id == item_id:
                    return pcm.astype(np.float32) / 32767.0
        return None

    def remove_failed(self, item_id: str) -> bool:
        with self._lock:
            entry = next((e for e in self._failed if e[0].id == item_id), None)
            if entry is None:
                return False
            self._failed.remove(entry)
            changed = self._bump()
        self._tell(*changed)
        return True

    # ------------------------------------------------------------------ sharing

    def snapshot(self) -> dict[str, Any]:
        """Everything the Recent page shows, as plain JSON values (no audio)."""
        with self._lock:
            return {
                "version": self._version,
                "items": [asdict(item) for item in self._items],
                "failed": [asdict(item) for item, _pcm in self._failed],
            }


def items_from(snapshot: dict[str, Any] | None) -> tuple[list[RecentItem], list[FailedItem]]:
    """The other side of :meth:`Recent.snapshot` (bad entries are skipped)."""
    items: list[RecentItem] = []
    failed: list[FailedItem] = []
    data = snapshot if isinstance(snapshot, dict) else {}
    for raw in data.get("items") or []:
        try:
            items.append(RecentItem(**raw))
        except TypeError:
            continue
    for raw in data.get("failed") or []:
        try:
            failed.append(FailedItem(**raw))
        except TypeError:
            continue
    return items, failed
