"""Settings > "Start with Windows".

An installed app (a frozen executable, from the Windows installer) starts at sign-in through
the per-user Run key: ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``, value
``ShoutAbout`` = ``"<that executable>" run``. Nothing needs admin rights.

A source checkout (``python -m shoutabout``) isn't installed: its sign-in start is the v1
setup script's "ShoutAbout Hub" scheduled task. Settings only *shows* that state there,
read-only. This module never creates Run entries for a non-installed copy and never
changes, starts or stops the scheduled task (it only asks ``schtasks /Query`` if it exists).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ShoutAbout"
LEGACY_TASK = "ShoutAbout Hub"
SCHTASKS_TIMEOUT_S = 10.0


class RunKey(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class WinRegRunKey:
    """HKCU\\...\\Run through winreg (Windows only)."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return str(value) if value else None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def legacy_task_exists() -> bool:
    """Read-only: does the v1 "ShoutAbout Hub" logon task exist?"""
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", LEGACY_TASK],
            capture_output=True,
            timeout=SCHTASKS_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def run_command(executable: str) -> str:
    return f'"{executable}" run'


@dataclass(frozen=True)
class StartupState:
    enabled: bool
    editable: bool
    note: str


class StartupManager:
    def __init__(
        self,
        *,
        run_key: RunKey | None = None,
        frozen: bool | None = None,
        executable: str | None = None,
        task_exists: Callable[[], bool] = legacy_task_exists,
    ) -> None:
        self._run_key = run_key
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self.executable = executable or sys.executable
        self._task_exists = task_exists

    def _key(self) -> Any:
        if self._run_key is None:
            if sys.platform != "win32":
                return None
            self._run_key = WinRegRunKey()
        return self._run_key

    def state(self) -> StartupState:
        key = self._key()
        value = key.get(VALUE_NAME) if key is not None else None
        if self.frozen:
            mine = value is not None and value.strip() == run_command(self.executable)
            note = "ShoutAbout starts when you sign in." if mine else "Start it yourself."
            if value is not None and not mine:
                note = "Another copy of ShoutAbout starts when you sign in."
            return StartupState(enabled=mine, editable=True, note=note)
        if value is not None:
            return StartupState(True, False, "Starts when you sign in (Run key).")
        if self._task_exists():
            return StartupState(True, False, "Starts when you sign in (setup script task).")
        return StartupState(False, False, "Run from source: the installer sets this up.")

    def set_enabled(self, on: bool) -> StartupState:
        """Add or remove the Run value. Only for an installed (frozen) app."""
        if not self.frozen:
            raise PermissionError("only the installed app can change this")
        key = self._key()
        if key is None:
            raise OSError("needs Windows")
        if on:
            key.set(VALUE_NAME, run_command(self.executable))
        else:
            key.delete(VALUE_NAME)
        log.info("start with Windows: %s", "on" if on else "off")
        return self.state()
