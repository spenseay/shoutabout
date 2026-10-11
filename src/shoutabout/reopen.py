"""Opening ShoutAbout while it already runs (Windows).

Clicking ShoutAbout in the Start menu starts a second ``ShoutAbout.exe``, which finds the
hub's single-instance mutex taken. Instead of exiting silently it sets a named event, and
the running hub answers by opening its settings window, so the click always shows
something. The event is auto-reset and lives in the user's session (``Local\\``).
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

EVENT_NAME = "Local\\ShoutAboutReopen"
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
POLL_MS = 500


def _kernel32():  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenEventW.restype = wintypes.HANDLE
    k.SetEvent.argtypes = [wintypes.HANDLE]
    k.SetEvent.restype = wintypes.BOOL
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def ask_running_hub(name: str = EVENT_NAME) -> bool:
    """Ask the running hub to show itself. True if it was asked."""
    if sys.platform != "win32":
        return False
    try:
        k = _kernel32()
        handle = k.OpenEventW(EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(k.SetEvent(handle))
        finally:
            k.CloseHandle(handle)
    except Exception:
        log.debug("couldn't ask the running hub to show itself", exc_info=True)
        return False


def watch(on_reopen: Callable[[], None], stopping: threading.Event, name: str = EVENT_NAME) -> bool:
    """Call ``on_reopen`` (on a background thread) each time another launch asks. Returns
    False when the event couldn't be created (then a second launch just exits)."""
    if sys.platform != "win32":
        return False
    try:
        k = _kernel32()
        handle = k.CreateEventW(None, False, False, name)
    except Exception:
        log.debug("couldn't create the reopen event", exc_info=True)
        return False
    if not handle:
        return False

    def loop() -> None:
        try:
            while not stopping.is_set():
                if k.WaitForSingleObject(handle, POLL_MS) != WAIT_OBJECT_0:
                    continue
                log.info("ShoutAbout was opened again: showing settings")
                try:
                    on_reopen()
                except Exception:
                    log.exception("showing ShoutAbout after a second launch failed")
        finally:
            k.CloseHandle(handle)

    threading.Thread(target=loop, name="reopen", daemon=True).start()
    return True
