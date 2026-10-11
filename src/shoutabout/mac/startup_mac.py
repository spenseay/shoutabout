"""Settings > "Open at login" on macOS: a LaunchAgent in ``~/Library/LaunchAgents``.

``fyi.shoutabout.mac.plist`` runs ``<ShoutAbout.app>/Contents/MacOS/ShoutAbout run`` when
you log in (``RunAtLoad``; no ``KeepAlive``, so Quit means quit). Writing or deleting the
file is all it takes: launchd reads the folder at the next login, so turning it on doesn't
start a second copy now. macOS 13+ lists it under System Settings > General > Login Items
("Allow in the Background"), attributed to ShoutAbout through
``AssociatedBundleIdentifiers``.

Only the built app (``ShoutAbout.app``) offers the switch; a source checkout shows it
read-only, like Windows (:mod:`shoutabout.winstartup`). Same :class:`StartupState` and
``state()`` / ``set_enabled()`` interface, so the settings window needs no changes.
"""

from __future__ import annotations

import logging
import plistlib
import sys
from pathlib import Path
from typing import Any

from shoutabout.mac import BUNDLE_ID
from shoutabout.winstartup import StartupState

log = logging.getLogger(__name__)

LABEL = BUNDLE_ID


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def agent_plist(executable: str) -> dict[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": [executable, "run"],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": ["Aqua"],
        "AssociatedBundleIdentifiers": [BUNDLE_ID],
    }


def app_executable() -> str | None:
    """The app's executable when running from ShoutAbout.app, else None."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False) and exe.parent.name == "MacOS":
        return str(exe)
    return None


class MacStartupManager:
    def __init__(self, *, folder: Path | None = None, executable: str | None = None) -> None:
        self.folder = folder or agents_dir()
        self.executable = executable if executable is not None else app_executable()

    @property
    def path(self) -> Path:
        return self.folder / f"{LABEL}.plist"

    def _read(self) -> dict[str, Any] | None:
        try:
            with self.path.open("rb") as f:
                data = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def state(self) -> StartupState:
        data = self._read()
        args = data.get("ProgramArguments") if data else None
        target = args[0] if isinstance(args, list) and args else None
        if self.executable is None:
            if data is not None:
                return StartupState(True, False, "Opens at login (the built app's setting).")
            return StartupState(False, False, "Run from source: build the app to use this.")
        mine = target == self.executable
        if data is not None and not mine:
            return StartupState(False, True, "Another copy of ShoutAbout opens at login.")
        note = "ShoutAbout opens when you log in." if mine else "Open it yourself."
        return StartupState(enabled=mine, editable=True, note=note)

    def set_enabled(self, on: bool) -> StartupState:
        if self.executable is None:
            raise PermissionError("only the built app can change this")
        if on:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".plist.tmp")
            with tmp.open("wb") as f:
                plistlib.dump(agent_plist(self.executable), f)
            tmp.replace(self.path)
        else:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        log.info("open at login: %s", "on" if on else "off")
        return self.state()
