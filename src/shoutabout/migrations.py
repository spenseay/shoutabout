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

``learn-projects-off`` (privacy): project folders are no longer scanned for word
suggestions unless the person asks (Teach words > "Suggest from my project folders").
``[learn] seed_projects = true`` is switched off once, however it is written, and project
suggestions still waiting from earlier scans are dropped. Turning it back on afterwards
sticks.

``history-gone`` (privacy): ShoutAbout keeps no history any more (Recent holds the last few
dictations in memory only). What earlier versions kept on disk is deleted once:
history.jsonl, its ``audio`` folder, the ``failed`` recordings, the ``[history]`` section of
config.toml, and the word suggestions mined from dictations that were still waiting.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from shoutabout.config import ConfigError, Paths, load_config
from shoutabout.fileio import atomic_write_text

log = logging.getLogger(__name__)

DICTATION_DEFAULTS = "dictation-toggle-600"
LEARN_PROJECTS_OFF = "learn-projects-off"
HISTORY_GONE = "history-gone"

# The exact lines earlier versions generated (src/shoutabout/data/config.example.toml).
OLD_GENERATED_LINES: dict[str, tuple[str, Any]] = {
    'mode = "hold"              # "hold" (hold to talk) or "toggle" (press to start, '
    "press to stop)": ("mode", "toggle"),
    "max_seconds = 120          # stop listening after this long": ("max_seconds", 600),
}
_SEED_PROJECTS_ON = re.compile(r"^seed_projects\s*=\s*true\b")


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


def project_scanning_on(text: str) -> bool:
    """Does ``[learn]`` in ``text`` say ``seed_projects = true``?"""
    return any(_SEED_PROJECTS_ON.match(line) for line in _section_lines(text, "learn"))


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


def _learn_projects_off(paths: Paths, text: str) -> list[str] | None:
    did: list[str] = []
    if project_scanning_on(text):
        from shoutabout.config_edit import ConfigEditError, update_config

        try:
            update_config(paths, {"learn": {"seed_projects": False}})
        except (ConfigEditError, OSError):
            log.exception("couldn't turn project folder scanning off")
            return None
        did.append("learn.seed_projects = False")
    try:
        from shoutabout.learn import forget_source

        dropped = forget_source(paths, "projects")
    except OSError:
        log.exception("couldn't drop the waiting project suggestions")
        return None
    if dropped:
        did.append(f"dropped {dropped} project suggestion(s)")
    return did


def _delete(path: Path) -> bool:
    """Delete a file or a folder of ours. True if something was there. Raises OSError."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return True
    if path.exists() or path.is_symlink():
        path.unlink()
        return True
    return False


def _history_gone(paths: Paths, text: str) -> list[str] | None:
    did: list[str] = []
    try:
        for path in (paths.history_file, paths.audio_dir, paths.failed_dir):
            if _delete(path):
                did.append(f"deleted {path.name}")
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
    try:
        from shoutabout.learn import forget_source

        dropped = forget_source(paths, "history")  # words mined from dictations
    except OSError:
        log.exception("couldn't drop the suggestions from the old history")
        return None
    if dropped:
        did.append(f"dropped {dropped} history suggestion(s)")
    return did


# (name, step): a step gets the config text and returns what it changed, or None when it
# failed (it is tried again next start).
MIGRATIONS: tuple[tuple[str, Callable[[Paths, str], list[str] | None]], ...] = (
    (DICTATION_DEFAULTS, _dictation_defaults),
    (LEARN_PROJECTS_OFF, _learn_projects_off),
    (HISTORY_GONE, _history_gone),
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
