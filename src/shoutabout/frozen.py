"""The installed app: ``ShoutAbout.exe``, a frozen (PyInstaller "onedir"), windowed build.

The Windows installer ships ShoutAbout without Python, a console or a repo (see
docs/installer.md). This module is the frozen program's entry point
(``packaging/entry.py`` calls :func:`main`) plus the small helpers the rest of the package
needs to behave the same frozen and unfrozen.

What ``ShoutAbout.exe`` does with its arguments:

* none, or ``run``: runs the hub with the tray, exactly like ``python -m shoutabout run``,
  with no console (the Start menu shortcut and the installer's "Start ShoutAbout now" use
  no arguments; the sign-in Run value is ``"<exe>" run``);
* ``--self-test [--home DIR] [--out FILE]``: checks the bundle and exits 0/1
  (:mod:`shoutabout.selftest`; CI runs it after a silent install);
* anything else: the normal CLI (``ShoutAbout.exe doctor``, ``ShoutAbout.exe info``, ...).

A windowed program has no console, so ``sys.stdout``/``sys.stderr`` are None unless the
caller redirected them. For CLI commands the process attaches to the console of the shell
that started it, so ``doctor`` output shows up there; with no console at all the output is
discarded instead of crashing on ``print``. The hub sends stray output to
``<home>/logs/stdout.log`` itself (:func:`shoutabout.logging_setup.redirect_std_streams`).
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

APP_EXE_NAME = "ShoutAbout.exe"
ICON_NAME = "ShoutAbout.ico"
# Matches the AppUserModelID on the installer's Start menu shortcut (packaging/installer.iss),
# so the app's windows group under that shortcut's icon on the taskbar.
APP_USER_MODEL_ID = "ShoutAbout.App"
SELF_TEST_FLAG = "--self-test"
ATTACH_PARENT_PROCESS = -1


def is_frozen() -> bool:
    """True inside the installed app (a PyInstaller build), False when run from source."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    """The folder holding the bundled libraries and data (``<app>\\_internal``), or None
    when not frozen."""
    if not is_frozen():
        return None
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(sys.executable).resolve().parent


def app_dir() -> Path | None:
    """The install folder (where ``ShoutAbout.exe`` lives), or None when not frozen."""
    return Path(sys.executable).resolve().parent if is_frozen() else None


def app_icon_path() -> Path | None:
    """The multi-size brand icon shipped with the app (for Tk windows' ``iconbitmap``), or
    None when it isn't there (e.g. running from source)."""
    for folder in (bundle_dir(), app_dir()):
        if folder is not None and (folder / ICON_NAME).is_file():
            return folder / ICON_NAME
    return None


def app_command() -> list[str]:
    """The command line that starts the hub the way the user expects it to run: the
    installed ``ShoutAbout.exe``, or ``pythonw -m shoutabout run`` from a source checkout.
    (For the sign-in Run value and "restart" actions.)"""
    if is_frozen():
        return [str(Path(sys.executable).resolve())]
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if sys.platform == "win32" and pythonw.is_file():
        exe = pythonw
    return [str(exe), "-m", "shoutabout", "run"]


# --------------------------------------------------------------------- std streams


def _console_stream(name: str, mode: str) -> Any:
    # "CONOUT$"/"CONIN$" open the attached console directly; Python writes them with
    # WriteConsoleW, so Unicode (QR blocks, dashes) shows correctly.
    return open(name, mode, encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115


def attach_parent_console() -> bool:
    """Give a windowed process the console of the shell that started it (Windows).

    Only fills in streams that are None: output redirected to a file or pipe stays there.
    Returns True if a console was attached."""
    if sys.platform != "win32":
        return False
    if sys.stdout is not None and sys.stderr is not None:
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False
        out = _console_stream("CONOUT$", "w")
    except Exception:
        return False
    if sys.stdout is None:
        sys.stdout = out
    if sys.stderr is None:
        sys.stderr = out
    if sys.stdin is None:
        with contextlib.suppress(Exception):
            sys.stdin = _console_stream("CONIN$", "r")
    return True


def ensure_std_streams() -> None:
    """Point any stream that is still None at the null device, so a ``print`` from the CLI
    or a library never raises ``AttributeError: 'NoneType' object has no attribute
    'write'``."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    sink = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives for the process
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def set_app_user_model_id() -> None:
    """Group the app's windows with its Start menu shortcut (Windows; best effort)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``ShoutAbout.exe`` (also works unfrozen, for tests)."""
    args = list(sys.argv[1:] if argv is None else argv)
    # v2-macos: old macOS versions pass a process serial number to apps started by Finder.
    args = [a for a in args if not a.startswith("-psn_")]
    if args and args[0] == SELF_TEST_FLAG:
        attach_parent_console()
        ensure_std_streams()
        from shoutabout.selftest import main as self_test_main

        return self_test_main(args[1:])

    from shoutabout.cli import main as cli_main

    if is_hub_launch(args):
        # The normal launch (Start menu, sign-in Run value, after install): the hub with the
        # tray, no console. run_hub sends stray output to <home>/logs/stdout.log. No
        # arguments means the user opened it (sign-in passes "run"): greet them.
        set_app_user_model_id()
        return cli_main(args or ["run", "--greet"])

    attach_parent_console()
    if command_name(args) != "run":  # run --no-tray: run_hub handles missing streams
        ensure_std_streams()
    return cli_main(args)


def is_hub_launch(args: Sequence[str]) -> bool:
    """True for launches that must behave exactly like double-clicking ShoutAbout.exe: no
    arguments, or ``run`` (the sign-in Run value, written by the installer and by the
    settings window, is ``"<exe>" run``), with or without ``--home``. Only
    ``run --no-tray`` is a console session."""
    return not args or (command_name(args) == "run" and "--no-tray" not in args)


def command_name(args: Sequence[str]) -> str | None:
    """The CLI subcommand in ``args`` (skipping the global ``--home PATH`` option)."""
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--home":
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None
