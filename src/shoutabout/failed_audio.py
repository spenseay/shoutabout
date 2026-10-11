"""Recordings whose transcription failed, kept so they can be tried again (History).

A long dictation must never be lost because the speech engine hiccuped (CUDA out of memory,
a driver problem, the model still loading). When transcribing fails *after* the recording
was made, the audio is kept in ``<config dir>\\failed\\``: ``<id>.wav`` (16 kHz mono 16-bit:
what "Talk to this PC" recorded) or the phone's own upload (``<id>.m4a``...), with
``<id>.json`` beside it (when, where it came from, how long, what went wrong; never text).
The History window lists them with **Try again** and **Delete**.

Only the few most recent are kept (:data:`MAX_ITEMS`, :data:`MAX_BYTES`); older ones are
deleted when a new one arrives, and a successful "Try again" deletes the recording.
Nothing is kept while history is off (``[history] enabled = false``): the person asked
ShoutAbout not to keep what they say. The folder never leaves the PC.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from shoutabout.fileio import atomic_write_text

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
MAX_ITEMS = 5
MAX_BYTES = 120 * 1024 * 1024  # 10 minutes from this PC is ~19 MB; phone uploads <= 25 MB
_ID = re.compile(r"^\d{8}-\d{6}-\d{6}$")  # time to the microsecond: sorts oldest first
_AUDIO_SUFFIXES = (".wav", ".m4a", ".mp4", ".aac", ".mp3", ".caf", ".ogg", ".opus", ".flac")


@dataclass(frozen=True)
class FailedItem:
    id: str
    ts: str  # ISO-8601 local time with offset
    route: str  # "local" (this PC) | "lan" | "tunnel"
    source: str | None  # the phone's name, when it came from a paired phone
    seconds: float | None
    error: str
    audio: Path
    size: int


def _safe_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if suffix in _AUDIO_SUFFIXES else ".bin"


class FailedRecordings:
    """The ``failed`` folder. Thread-safe; methods never raise on disk errors (logged)."""

    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        max_items: int = MAX_ITEMS,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.dir = Path(directory)
        self.enabled = enabled
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- keeping

    def _new_id(self, now: datetime) -> str:
        """A new id from the time (lock held): ids sort by age, and never collide."""
        stamp = now
        while True:
            item_id = f"{stamp:%Y%m%d-%H%M%S}-{stamp.microsecond:06d}"
            if not any(self.dir.glob(f"{item_id}.*")):
                return item_id
            stamp += timedelta(microseconds=1)

    def _write_meta(self, item_id: str, meta: dict[str, Any]) -> None:
        atomic_write_text(self.dir / f"{item_id}.json", json.dumps(meta, ensure_ascii=False) + "\n")

    def keep_samples(
        self, audio: np.ndarray, *, route: str = "local", source: str | None = None, error: str = ""
    ) -> FailedItem | None:
        """Keep 16 kHz mono float32 samples as a WAV. None when off or it couldn't."""
        if not self.enabled:
            return None
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return None
        now = datetime.now().astimezone()
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                item_id = self._new_id(now)
                path = self.dir / f"{item_id}.wav"
                pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2")
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(SAMPLE_RATE)
                    wav.writeframes(pcm.tobytes())
                seconds = round(samples.size / SAMPLE_RATE, 2)
                self._write_meta(item_id, self._meta(now, route, source, seconds, error, path))
                self._prune()
        except Exception:
            log.exception("couldn't keep the failed recording")
            return None
        log.info("kept a recording that couldn't be transcribed (%s, %.0f s)", item_id, seconds)
        return self.get(item_id)

    def keep_file(
        self,
        src: Path,
        *,
        route: str,
        source: str | None = None,
        error: str = "",
        seconds: float | None = None,
    ) -> FailedItem | None:
        """Keep a copy of an uploaded audio file. None when off or it couldn't."""
        if not self.enabled:
            return None
        src = Path(src)
        now = datetime.now().astimezone()
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                item_id = self._new_id(now)
                path = self.dir / f"{item_id}{_safe_suffix(src)}"
                shutil.copyfile(src, path)
                self._write_meta(item_id, self._meta(now, route, source, seconds, error, path))
                self._prune()
        except Exception:
            log.exception("couldn't keep the failed upload")
            return None
        log.info("kept an upload that couldn't be transcribed (%s)", item_id)
        return self.get(item_id)

    @staticmethod
    def _meta(
        now: datetime,
        route: str,
        source: str | None,
        seconds: float | None,
        error: str,
        path: Path,
    ) -> dict[str, Any]:
        return {
            "ts": now.isoformat(timespec="seconds"),
            "route": route,
            "source": source,
            "seconds": seconds,
            "error": " ".join(str(error).split())[:300],
            "audio": path.name,
        }

    # ----------------------------------------------------------------- reading

    def _load(self, meta_path: Path) -> FailedItem | None:
        item_id = meta_path.stem
        if not _ID.match(item_id):
            return None
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            audio = self.dir / str(meta["audio"])
            if audio.parent != self.dir or not audio.name.startswith(item_id):
                return None
            size = audio.stat().st_size
        except (OSError, ValueError, KeyError, TypeError):
            return None
        seconds = meta.get("seconds")
        return FailedItem(
            id=item_id,
            ts=str(meta.get("ts") or ""),
            route=str(meta.get("route") or "local"),
            source=meta.get("source") if isinstance(meta.get("source"), str) else None,
            seconds=float(seconds) if isinstance(seconds, int | float) else None,
            error=str(meta.get("error") or ""),
            audio=audio,
            size=size,
        )

    def list(self) -> list[FailedItem]:
        """Newest first."""
        try:
            metas = sorted(self.dir.glob("*.json"), reverse=True)
        except OSError:
            return []
        return [item for item in (self._load(p) for p in metas) if item is not None]

    def get(self, item_id: str) -> FailedItem | None:
        if not _ID.match(str(item_id)):
            return None
        return self._load(self.dir / f"{item_id}.json")

    # ---------------------------------------------------------------- removing

    def _delete(self, item_id: str) -> bool:
        removed = False
        for path in self.dir.glob(f"{item_id}.*"):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("couldn't delete %s", path.name)
        return removed

    def remove(self, item_id: str) -> bool:
        if not _ID.match(str(item_id)):
            return False
        with self._lock:
            return self._delete(item_id)

    def clear(self) -> int:
        """Delete every kept recording. Returns how many."""
        with self._lock:
            ids = {p.stem for p in self.dir.glob("*.json") if _ID.match(p.stem)}
            ids |= {p.stem for p in self.dir.glob("*.*") if _ID.match(p.stem)}
            return sum(self._delete(i) for i in sorted(ids))

    def _prune(self) -> None:
        """Keep the newest :attr:`max_items`, and at most :attr:`max_bytes` (lock held)."""
        items = sorted((p.stem for p in self.dir.glob("*.json") if _ID.match(p.stem)), reverse=True)
        total = 0
        for i, item_id in enumerate(items):
            size = sum(p.stat().st_size for p in self.dir.glob(f"{item_id}.*") if p.is_file())
            total += size
            if i >= self.max_items or (i > 0 and total > self.max_bytes):
                self._delete(item_id)
        # Audio without its .json (a crash between the two writes) is just clutter.
        for path in self.dir.glob("*.*"):
            orphan = path.suffix != ".json" and _ID.match(path.stem)
            if orphan and not (self.dir / f"{path.stem}.json").exists():
                try:
                    path.unlink()
                except OSError:
                    pass
