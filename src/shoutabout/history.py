"""Append-only transcription history (JSONL) with optional audio retention.

One JSON object per line, UTF-8, non-ASCII kept as-is. Record keys:
``ts`` (ISO-8601 local time with offset), ``id``, ``route``, ``target``, ``raw_text``,
``text``, ``cleanup`` (status), ``cleanup_fallback``, ``cleanup_reason``, ``pasted``,
``warning``, ``audio_seconds``, ``audio_file`` (name inside the audio dir, or null),
``stt_model``, ``llm_model``, ``timings_ms``, ``source`` (the paired phone's name, or
null), and ``recovered`` (only present, true, for a failed recording that "Try again" in the
History window transcribed later).

The History window (:mod:`history_model`) reads it back with :func:`read_history`; its
"Clear history" button uses :meth:`HistoryLog.clear` (or :func:`clear_history_file`).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from shoutabout.models import TranscribeResult

log = logging.getLogger(__name__)

RECORD_KEYS = (
    "ts",
    "id",
    "route",
    "target",
    "raw_text",
    "text",
    "cleanup",
    "cleanup_fallback",
    "cleanup_reason",
    "pasted",
    "warning",
    "audio_seconds",
    "audio_file",
    "stt_model",
    "llm_model",
    "timings_ms",
    "source",
)


def build_record(
    result: TranscribeResult, *, ts: datetime, audio_file: str | None = None
) -> dict[str, Any]:
    source = result.extra.get("source")
    record: dict[str, Any] = {
        "ts": ts.isoformat(timespec="seconds"),
        "id": result.request_id,
        "route": result.route,
        "target": result.target,
        "raw_text": result.raw_text,
        "text": result.text,
        "cleanup": result.cleanup.status,
        "cleanup_fallback": result.cleanup.fell_back,
        "cleanup_reason": result.cleanup.reason,
        "pasted": result.pasted,
        "warning": result.warning,
        "audio_seconds": round(float(result.audio_seconds), 2),
        "audio_file": audio_file,
        "stt_model": result.extra.get("stt_model"),
        "llm_model": result.extra.get("llm_model"),
        "timings_ms": result.timings.as_dict(),
        "source": str(source) if source else None,
    }
    if result.extra.get("recovered"):
        record["recovered"] = True
    return record


def read_history(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """The records in ``path``, newest first (the last ``limit`` lines only, if given).
    Lines that aren't JSON objects are skipped; a missing file is an empty history."""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            filled = (line for line in fh if line.strip())
            lines = deque(filled, maxlen=limit) if limit else list(filled)
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def clear_history_file(path: Path, attempts: int = 5) -> None:
    """Delete the history file (a hub in another process may have it open for a moment)."""
    for i in range(attempts):
        try:
            Path(path).unlink(missing_ok=True)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.1)


class HistoryLog:
    def __init__(self, path: Path, audio_dir: Path, keep_audio: bool) -> None:
        self.path = Path(path)
        self.audio_dir = Path(audio_dir)
        self.keep_audio = keep_audio
        self._lock = threading.Lock()

    def clear(self) -> None:
        """Delete every record (the History window's "Clear history"). Raises OSError."""
        with self._lock:
            clear_history_file(self.path)

    def append(self, result: TranscribeResult, audio_path: Path | None = None) -> None:
        """Append one record. Thread-safe; never raises (errors are logged)."""
        try:
            now = datetime.now().astimezone()
            with self._lock:
                audio_file = None
                if self.keep_audio and audio_path is not None:
                    audio_file = self._keep_audio(result, Path(audio_path), now)
                record = build_record(result, ts=now, audio_file=audio_file)
                line = json.dumps(record, ensure_ascii=False) + "\n"
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
                    fh.flush()
        except Exception:
            log.exception("couldn't append to history file %s", self.path)

    def _keep_audio(self, result: TranscribeResult, src: Path, now: datetime) -> str | None:
        try:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            suffix = src.suffix.lower() if src.suffix else ".m4a"
            rid = result.request_id or "noid"
            name = f"{now:%Y%m%d-%H%M%S}-{rid}{suffix}"
            shutil.copyfile(src, self.audio_dir / name)
            return name
        except Exception:
            log.exception("couldn't keep audio file for history")
            return None
