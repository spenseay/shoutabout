"""Comment-preserving edits to config.toml (tomlkit), for the settings and setup windows.

The owner's config.toml is hand-edited and full of comments, so a settings change must
touch only the values it changes: comments, key order, blank lines and every other value
stay byte-for-byte. Edits are validated against the real schema before anything is written
(an invalid config would stop the hub from starting) and written atomically (temp file in
the same folder + ``os.replace``), so a crash can never leave half a file behind.

::

    update_config(paths, {"dictation": {"hotkey": "right_alt"}, "cleanup": {"enabled": "auto"}})

A value of ``None`` removes the key (the setting goes back to its default); a section of
``None`` removes the whole section (``{"history": None}``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

import tomlkit
from tomlkit.items import Item, Table

from shoutabout.config import ConfigError, Paths, example_config_text, parse_config
from shoutabout.fileio import atomic_write_text

Changes = Mapping[str, Mapping[str, Any] | None]
Validator = Callable[[str, str], Any]

_lock = threading.Lock()  # one writer at a time inside this process


class ConfigEditError(RuntimeError):
    """The change can't be applied (bad TOML on disk, or the result isn't a valid config)."""


def _with_trivia(old: Item | None, value: Any) -> Item:
    """``value`` as a TOML item that keeps ``old``'s inline comment and spacing."""
    new = tomlkit.item(value)
    if old is not None:
        trivia, src = new.trivia, old.trivia
        trivia.indent = src.indent
        trivia.comment_ws = src.comment_ws
        trivia.comment = src.comment
        trivia.trail = src.trail
    return new


_MISSING = object()


def _current(table: Table, key: str) -> Any:
    try:
        item = table.item(key)
    except KeyError:
        return _MISSING
    return item.unwrap() if hasattr(item, "unwrap") else item


def apply_changes(text: str, changes: Changes) -> tuple[str, bool]:
    """``(new_text, changed)`` with ``changes`` applied to the TOML ``text``."""
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:  # tomlkit raises its own ParseError subclasses
        raise ConfigEditError(f"config.toml isn't valid TOML: {exc}") from exc
    changed = False
    for section, values in changes.items():
        if values is None:
            if section in doc:
                del doc[section]
                changed = True
            continue
        if not values:
            continue
        table = doc.get(section)
        if table is not None and not isinstance(table, Table):
            raise ConfigEditError(f"[{section}] in config.toml isn't a table")
        for key, value in values.items():
            if value is None:
                if table is not None and key in table:
                    del table[key]
                    changed = True
                continue
            if table is None:
                table = tomlkit.table()
                doc[section] = table
                table = doc[section]
            current = _current(table, key)
            if current == value and type(current) is type(value):
                continue
            old = table.item(key) if key in table else None
            table[key] = _with_trivia(old, value)
            changed = True
    return tomlkit.dumps(doc), changed


def update_config(
    paths: Paths, changes: Changes, *, validate: Validator | None = parse_config
) -> bool:
    """Apply ``changes`` to config.toml (created from the example if missing).

    Returns True if the file changed. Raises :class:`ConfigEditError` when the result
    wouldn't load (nothing is written then). ``validate(text, source)`` defaults to the
    hub's own schema check.
    """
    path = paths.config_file
    with _lock:
        try:
            text = path.read_text("utf-8")
        except FileNotFoundError:
            text = example_config_text()
        new_text, changed = apply_changes(text, changes)
        if not changed and path.exists():
            return False
        if validate is not None:
            try:
                validate(new_text, str(path))
            except ConfigError as exc:
                raise ConfigEditError(str(exc)) from exc
        atomic_write_text(path, new_text)
        return True


def read_value(paths: Paths, section: str, key: str, default: Any = None) -> Any:
    """One raw value from config.toml as written (``"auto"`` stays a string), or default."""
    try:
        doc = tomlkit.parse(paths.config_file.read_text("utf-8"))
    except Exception:  # missing file or invalid TOML
        return default
    table = doc.get(section)
    if not isinstance(table, Table) or key not in table:
        return default
    value = _current(table, key)
    return default if value is _MISSING else value
