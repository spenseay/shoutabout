"""Logging to ``<home>/logs/hub.log`` (rotating) and std-stream safety under pythonw."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import TextIO

from shoutabout.config import Paths

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_NAME = "hub.log"
STD_LOG_FILE_NAME = "stdout.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

_MARK = "_shoutabout_handler"

# Third-party loggers that are noisy at INFO.
_QUIET = ("httpx", "httpcore", "huggingface_hub", "urllib3", "multipart", "python_multipart")


def setup_logging(paths: Paths, level: str = "INFO", console: bool = False) -> None:
    """Configure the root logger. Idempotent (replaces handlers it installed before)."""
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(
        paths.log_dir / LOG_FILE_NAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setFormatter(formatter)
    setattr(file_handler, _MARK, True)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        setattr(stream_handler, _MARK, True)
        root.addHandler(stream_handler)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)


def redirect_std_streams(paths: Paths) -> TextIO | None:
    """Under pythonw ``sys.stdout``/``sys.stderr`` are None and any library ``print`` (tqdm,
    Hugging Face downloads) would crash. Point the missing ones at ``<logs>/stdout.log``.

    Returns the opened file, or None when both streams already exist.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    stream = open(  # noqa: SIM115 - lives for the whole process
        paths.log_dir / STD_LOG_FILE_NAME,
        "a",
        encoding="utf-8",
        errors="backslashreplace",
        buffering=1,
    )
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return stream
