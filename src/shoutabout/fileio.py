"""Small, crash-safe file helpers shared by the stores in the config dir.

Writes go to a temp file in the same folder, are flushed to disk, then ``os.replace``d over
the target: readers see the old file or the new one, never half of one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    """The parsed file, or None if it's missing. Raises ValueError on invalid JSON."""
    try:
        text = Path(path).read_text("utf-8-sig")
    except FileNotFoundError:
        return None
    return json.loads(text)
