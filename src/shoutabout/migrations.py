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
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from shoutabout.config import Paths
from shoutabout.fileio import atomic_write_text

log = logging.getLogger(__name__)

DICTATION_DEFAULTS = "dictation-toggle-600"

# The exact lines earlier versions generated (src/shoutabout/data/config.example.toml).
OLD_GENERATED_LINES: dict[str, tuple[str, Any]] = {
    'mode = "hold"              # "hold" (hold to talk) or "toggle" (press to start, '
    "press to stop)": ("mode", "toggle"),
    "max_seconds = 120          # stop listening after this long": ("max_seconds", 600),
}


def _done(paths: Paths) -> dict[str, Any]:
    try:
        data = json.loads(paths.migrations_file.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mark(paths: Paths, done: dict[str, Any], name: str, detail: Any) -> None:
    done = dict(done)
    done[name] = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "did": detail}
    try:
        atomic_write_text(paths.migrations_file, json.dumps(done, indent=2) + "\n")
    except OSError:
        log.exception("couldn't remember that %s ran", name)


def untouched_dictation_defaults(text: str) -> dict[str, Any]:
    """{key: new value} for the [dictation] lines in ``text`` that are still exactly as an
    earlier version generated them."""
    changes: dict[str, Any] = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section == "dictation" and line in OLD_GENERATED_LINES:
            key, value = OLD_GENERATED_LINES[line]
            changes[key] = value
    return changes


def migrate(paths: Paths) -> list[str]:
    """Run the one-time upgrades that haven't run yet. Returns what changed (for the log).
    Never raises: a failed upgrade leaves the config as it was."""
    done = _done(paths)
    if DICTATION_DEFAULTS in done:
        return []
    try:
        text = paths.config_file.read_text("utf-8")
    except FileNotFoundError:
        _mark(paths, done, DICTATION_DEFAULTS, [])
        return []
    except OSError:
        log.exception("couldn't read config.toml to upgrade it")
        return []
    changes = untouched_dictation_defaults(text)
    did: list[str] = []
    if changes:
        from shoutabout.config_edit import ConfigEditError, update_config

        try:
            update_config(paths, {"dictation": changes})
        except (ConfigEditError, OSError):
            log.exception("couldn't upgrade the [dictation] defaults")
            return []
        did = [f"dictation.{k} = {v!r}" for k, v in changes.items()]
        log.info("config upgraded to the new defaults: %s", ", ".join(did))
    _mark(paths, done, DICTATION_DEFAULTS, did)
    return did
