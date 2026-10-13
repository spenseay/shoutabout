"""One-time upgrades of config.toml when a new version changes a default.

Explicit settings always win, so a changed default only reaches configs that never chose:
a missing key simply gets the new default. But the config.toml that earlier versions
*generated* spells the old defaults out, e.g. ``mode = "hold"`` under ``[dictation]``,
which looks like a choice although nobody made it. Each migration here rewrites such a
value only while its line is still byte-for-byte the line ShoutAbout wrote (value, spacing
and comment), and only once per install: ``migrations.json`` in the config dir remembers
what ran, so a person who picks the old value again later keeps it.

``dictation-toggle-600`` (v2 feedback): tap-to-start/tap-to-stop became the default
(``mode = "toggle"``) and the time limit went from 120 s to 600 s. (Choosing "Hold to talk"
in Settings before upgrading writes the same line as the old default, so that one choice
can't be told apart and is switched to toggle once; Settings switches it back.)

``history-gone`` (privacy): ShoutAbout keeps no history any more (Recent holds the last few
dictations in memory only). What earlier versions kept on disk is deleted once:
history.jsonl, its ``audio`` folder, the ``failed`` recordings and the ``[history]`` section
of config.toml.

``suggestions-gone``: word suggestions (from project folders, and earlier from dictations)
are gone; words are fixed from Recent instead. vocab-suggestions.json and the ``[learn]``
section of config.toml are deleted once. (It replaces ``learn-projects-off``, which switched
the project folder scanning off.)
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from shoutabout.config import ConfigError, Paths, load_config
from shoutabout.fileio import atomic_write_text

log = logging.getLogger(__name__)

DICTATION_DEFAULTS = "dictation-toggle-600"
HISTORY_GONE = "history-gone"
SUGGESTIONS_GONE = "suggestions-gone"

# The exact lines earlier versions generated (src/shoutabout/data/config.example.toml).
OLD_GENERATED_LINES: dict[str, tuple[str, Any]] = {
    'mode = "hold"              # "hold" (hold to talk) or "toggle" (press to start, '
    "press to stop)": ("mode", "toggle"),
    "max_seconds = 120          # stop listening after this long": ("max_seconds", 600),
}
# What earlier versions wrote in "audio" (history: 20261010-143000-<request id>.m4a) and
# "failed" (20261010-143000-123456.wav / .json).
_OLD_RECORDING = re.compile(r"^\d{8}-\d{6}-[0-9a-z]+\.[0-9a-z]+$", re.IGNORECASE)


def _done(paths: Paths) -> dict[str, Any]:
    try:
        data = json.loads(paths.migrations_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mark(paths: Paths, done: dict[str, Any], name: str, detail: Any) -> dict[str, Any]:
    done = dict(done)
    done[name] = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "did": detail}
    try:
        atomic_write_text(paths.migrations_file, json.dumps(done, indent=2) + "\n")
    except OSError:
        log.exception("couldn't remember that %s ran", name)
    return done


def _section_lines(text: str, wanted: str) -> list[str]:
    """The stripped lines of ``[wanted]`` in ``text``."""
    out: list[str] = []
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section == wanted:
            out.append(line)
    return out


def untouched_dictation_defaults(text: str) -> dict[str, Any]:
    """{key: new value} for the [dictation] lines in ``text`` that are still exactly as an
    earlier version generated them."""
    changes: dict[str, Any] = {}
    for line in _section_lines(text, "dictation"):
        if line in OLD_GENERATED_LINES:
            key, value = OLD_GENERATED_LINES[line]
            changes[key] = value
    return changes


def _dictation_defaults(paths: Paths, text: str) -> list[str] | None:
    changes = untouched_dictation_defaults(text)
    if not changes:
        return []
    from shoutabout.config_edit import ConfigEditError, update_config

    try:
        update_config(paths, {"dictation": changes})
    except (ConfigEditError, OSError):
        log.exception("couldn't upgrade the [dictation] defaults")
        return None
    return [f"dictation.{k} = {v!r}" for k, v in changes.items()]


def _delete_ours(folder: Path) -> bool:
    """Delete the files ShoutAbout wrote in ``folder`` (known by their names: a custom home
    folder may hold other things called "audio"), then the folder if that emptied it. True
    if anything went. Raises OSError."""
    if not folder.is_dir() or folder.is_symlink():
        return False
    gone = False
    for path in folder.iterdir():
        if path.is_file() and _OLD_RECORDING.match(path.name):
            path.unlink()
            gone = True
    with contextlib.suppress(OSError):  # not empty: something else lives there
        folder.rmdir()
        gone = True
    return gone


def _history_gone(paths: Paths, text: str) -> list[str] | None:
    did: list[str] = []
    try:
        if paths.history_file.is_file():
            paths.history_file.unlink()
            did.append(f"deleted {paths.history_file.name}")
        for folder in (paths.audio_dir, paths.failed_dir):
            if _delete_ours(folder):
                did.append(f"deleted {folder.name}")
    except OSError:
        log.exception("couldn't delete the old history")
        return None
    if "[history]" in text:
        from shoutabout.config_edit import ConfigEditError, update_config

        try:
            if update_config(paths, {"history": None}):
                did.append("removed [history]")
        except (ConfigEditError, OSError):
            log.exception("couldn't remove [history] from config.toml")
            return None
    return did


def _suggestions_gone(paths: Paths, text: str) -> list[str] | None:
    did: list[str] = []
    lock = paths.suggestions_file.with_name(paths.suggestions_file.name + ".lock")
    try:
        for path in (paths.suggestions_file, lock):
            if path.is_file():
                path.unlink()
                did.append(f"deleted {path.name}")
    except OSError:
        log.exception("couldn't delete the old word suggestions")
        return None
    if "[learn]" in text:
        from shoutabout.config_edit import ConfigEditError, update_config

        try:
            if update_config(paths, {"learn": None}):
                did.append("removed [learn]")
        except (ConfigEditError, OSError):
            log.exception("couldn't remove [learn] from config.toml")
            return None
    return did


# (name, step): a step gets the config text and returns what it changed, or None when it
# failed (it is tried again next start).
MIGRATIONS: tuple[tuple[str, Callable[[Paths, str], list[str] | None]], ...] = (
    (DICTATION_DEFAULTS, _dictation_defaults),
    (HISTORY_GONE, _history_gone),
    (SUGGESTIONS_GONE, _suggestions_gone),
)


def migrate(paths: Paths) -> list[str]:
    """Run the one-time upgrades that haven't run yet. Returns what changed (for the log).
    Never raises: a failed upgrade leaves the config as it was."""
    done = _done(paths)
    todo = [(name, step) for name, step in MIGRATIONS if name not in done]
    if not todo:
        return []
    try:
        text = paths.config_file.read_text("utf-8")
    except FileNotFoundError:
        text = ""  # nothing generated yet: the new defaults apply anyway
    except OSError:
        log.exception("couldn't read config.toml to upgrade it")
        return []
    if text:
        try:
            load_config(paths)
        except ConfigError:
            log.warning("config.toml doesn't load; upgrading it next time")
            return []
    did: list[str] = []
    for name, step in todo:
        try:
            changed = step(paths, text)
        except Exception:
            log.exception("the %s upgrade failed", name)
            changed = None
        if changed is None:
            continue
        if changed:
            log.info("config upgraded (%s): %s", name, ", ".join(changed))
        done = _mark(paths, done, name, changed)
        did += changed
    return did
