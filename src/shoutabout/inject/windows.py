"""Paste text into the focused Windows app: clipboard + the paste chord, then restore.

The only keystrokes this module can ever synthesise are the two standard paste chords:
Ctrl+V, or Shift+Insert for processes listed in ``paste.shift_insert_processes`` (mintty,
PuTTY...). Never Enter, never anything else; there is deliberately no API that takes
arbitrary keys (see :data:`CHORD_KEYS` and :meth:`Win32Api.send_chord`).

All Win32 calls go through the small :class:`Win32Api` facade so the decision logic in
:class:`WindowsPasteInjector` (no foreground window, locked PC, elevated target, held
modifiers, chord choice, restore-if-unchanged) is unit-tested on any OS with a fake.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from shoutabout.config import PasteConfig
from shoutabout.inject import clipboard
from shoutabout.inject import win32 as w
from shoutabout.inject.base import ADMIN_WINDOW_WARNING, Notifier
from shoutabout.models import PasteResult

log = logging.getLogger(__name__)

NOTIFY_TITLE = "ShoutAbout"
NO_WINDOW_WARNING = "No focused window on the PC (locked?) — text is on your clipboard."
LOCKED_WARNING = "The PC is locked — text is on your clipboard."
MODIFIER_WARNING = "A modifier key is held on the PC — text is on your clipboard (not pasted)."
SEND_FAILED_WARNING = "Couldn't send the paste keystroke — text is on your clipboard."
CLIPBOARD_WARNING = "Couldn't access the clipboard on the PC — not pasted."
NOT_COPIED_WARNING = "Couldn't paste on the PC, and the clipboard was busy — text not copied."

PRE_PASTE_DELAY_S = 0.04  # let clipboard listeners settle before the chord
MODIFIER_WAIT_S = 1.0
MODIFIER_POLL_S = 0.02
DEFAULT_DESKTOP = "Default"


# ----------------------------------------------------------------------- key chords


class PasteChord(Enum):
    CTRL_V = "ctrl+v"
    SHIFT_INSERT = "shift+insert"


@dataclass(frozen=True)
class KeyEvent:
    vk: int
    up: bool
    extended: bool = False


# (modifier vk, key vk, key is an extended key). The complete list of what can be typed.
CHORD_KEYS: dict[PasteChord, tuple[int, int, bool]] = {
    PasteChord.CTRL_V: (w.VK_CONTROL, w.VK_V, False),
    PasteChord.SHIFT_INSERT: (w.VK_SHIFT, w.VK_INSERT, True),  # Insert needs EXTENDEDKEY
}
ALLOWED_VKS = frozenset(vk for mod, key, _ in CHORD_KEYS.values() for vk in (mod, key))
MODIFIER_VKS = (w.VK_SHIFT, w.VK_CONTROL, w.VK_MENU, w.VK_LWIN, w.VK_RWIN)


def chord_events(chord: PasteChord) -> tuple[KeyEvent, ...]:
    """modifier down, key down, key up, modifier up."""
    mod, key, extended = CHORD_KEYS[PasteChord(chord)]
    return (
        KeyEvent(mod, up=False),
        KeyEvent(key, up=False, extended=extended),
        KeyEvent(key, up=True, extended=extended),
        KeyEvent(mod, up=True),
    )


def build_inputs(chord: PasteChord, scan_code: Any) -> ctypes.Array:
    """The INPUT[4] array for one SendInput call. ``scan_code(vk) -> int``.

    wVk plus wScan (from MapVirtualKeyW) without KEYEVENTF_SCANCODE, so apps that look at
    either field see a consistent key.
    """
    events = chord_events(chord)
    inputs = (w.INPUT * len(events))()
    for item, ev in zip(inputs, events, strict=True):
        if ev.vk not in ALLOWED_VKS:  # defence in depth; CHORD_KEYS is the only source
            raise ValueError(f"refusing to synthesise vk {ev.vk:#x}")
        item.type = w.INPUT_KEYBOARD
        item.ki.wVk = ev.vk
        item.ki.wScan = int(scan_code(ev.vk)) & 0xFFFF
        flags = 0
        if ev.up:
            flags |= w.KEYEVENTF_KEYUP
        if ev.extended:
            flags |= w.KEYEVENTF_EXTENDEDKEY
        item.ki.dwFlags = flags
        item.ki.time = 0
        item.ki.dwExtraInfo = 0
    return inputs


def normalize_process_names(names: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for name in names:
        n = ntpath.basename(name.strip()).lower()
        if n:
            out.add(n if n.endswith(".exe") else n + ".exe")
    return frozenset(out)


# -------------------------------------------------------------------- Win32 facade


class Api(Protocol):
    def foreground_window(self) -> int | None: ...
    def input_desktop_name(self) -> str | None: ...
    def window_pid(self, hwnd: int) -> int: ...
    def integrity_rid(self, pid: int | None) -> int | None: ...
    def process_exe_name(self, pid: int) -> str | None: ...
    def held_modifiers(self) -> tuple[int, ...]: ...
    def send_chord(self, chord: PasteChord) -> int: ...
    def clipboard_snapshot(self) -> clipboard.ClipboardSnapshot: ...
    def clipboard_set_text(self, text: str, *, exclude_from_history: bool) -> int: ...
    def clipboard_restore(
        self,
        snap: clipboard.ClipboardSnapshot,
        *,
        exclude_from_history: bool,
        expected_sequence: int | None = None,
    ) -> None: ...
    def clipboard_sequence(self) -> int: ...
    def sleep(self, seconds: float) -> None: ...
    def monotonic(self) -> float: ...


class Win32Api:
    """The real Win32 calls used by the injector."""

    def foreground_window(self) -> int | None:
        return w.user32.GetForegroundWindow() or None

    def input_desktop_name(self) -> str | None:
        """Name of the desktop receiving input: "Default" normally; None if it can't be
        opened (locked workstation, UAC secure desktop)."""
        hdesk = w.user32.OpenInputDesktop(0, False, w.DESKTOP_READOBJECTS)
        if not hdesk:
            return None
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = w.DWORD(0)
            if not w.user32.GetUserObjectInformationW(
                hdesk, w.UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
            ):
                return None
            return buf.value
        finally:
            w.user32.CloseDesktop(hdesk)

    def window_pid(self, hwnd: int) -> int:
        pid = w.DWORD(0)
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def integrity_rid(self, pid: int | None) -> int | None:
        """Mandatory integrity level RID of a process (None = this process).

        Returns None if the process or its token can't be opened/queried (another account,
        protected process...).
        """
        k32, adv = w.kernel32, w.advapi32
        if pid is None:
            hproc = k32.GetCurrentProcess()  # pseudo-handle, never closed
        else:
            hproc = k32.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not hproc:
                return None
        try:
            htoken = w.HANDLE()
            if not adv.OpenProcessToken(hproc, w.TOKEN_QUERY, ctypes.byref(htoken)):
                return None
            try:
                needed = w.DWORD(0)
                adv.GetTokenInformation(
                    htoken, w.TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed)
                )
                if not needed.value:
                    return None
                buf = ctypes.create_string_buffer(needed.value)
                if not adv.GetTokenInformation(
                    htoken, w.TOKEN_INTEGRITY_LEVEL, buf, needed, ctypes.byref(needed)
                ):
                    return None
                # TOKEN_MANDATORY_LABEL { SID_AND_ATTRIBUTES Label { PSID Sid; DWORD Attr; } }
                sid = ctypes.c_void_p.from_buffer(buf).value
                if not sid:
                    return None
                count = adv.GetSidSubAuthorityCount(sid)[0]
                if count < 1:
                    return None
                return int(adv.GetSidSubAuthority(sid, count - 1)[0])
            finally:
                k32.CloseHandle(htoken)
        finally:
            if pid is not None:
                k32.CloseHandle(hproc)

    def process_exe_name(self, pid: int) -> str | None:
        k32 = w.kernel32
        hproc = k32.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hproc:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = w.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return None
            return ntpath.basename(buf.value[: size.value]).lower() or None
        finally:
            k32.CloseHandle(hproc)

    def held_modifiers(self) -> tuple[int, ...]:
        return tuple(vk for vk in MODIFIER_VKS if w.user32.GetAsyncKeyState(vk) & 0x8000)

    def send_chord(self, chord: PasteChord) -> int:
        """One SendInput call with the 4 events of a paste chord. Returns events inserted."""
        inputs = build_inputs(chord, lambda vk: w.user32.MapVirtualKeyW(vk, w.MAPVK_VK_TO_VSC))
        return int(w.user32.SendInput(len(inputs), inputs, ctypes.sizeof(w.INPUT)))

    def clipboard_snapshot(self) -> clipboard.ClipboardSnapshot:
        return clipboard.snapshot()

    def clipboard_set_text(self, text: str, *, exclude_from_history: bool) -> int:
        return clipboard.set_text(text, exclude_from_history=exclude_from_history)

    def clipboard_restore(
        self,
        snap: clipboard.ClipboardSnapshot,
        *,
        exclude_from_history: bool,
        expected_sequence: int | None = None,
    ) -> None:
        clipboard.restore(
            snap, exclude_from_history=exclude_from_history, expected_sequence=expected_sequence
        )

    def clipboard_sequence(self) -> int:
        return clipboard.sequence_number()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


# ------------------------------------------------------------------------ injector


class WindowsPasteInjector:
    """Implements :class:`shoutabout.inject.base.Injector` on Windows."""

    def __init__(self, cfg: PasteConfig, notifier: Notifier, *, api: Api | None = None) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self._api: Api = api if api is not None else Win32Api()
        self._paste_lock = threading.Lock()
        self._restore_thread: threading.Thread | None = None
        self._own_rid: int | None = None

    # -- public

    def paste(self, text: str) -> PasteResult:
        """Never raises: failures come back as PasteResult(False, warning)."""
        try:
            with self._paste_lock:
                self.wait_idle()
                return self._paste(text)
        except Exception as exc:
            log.exception("paste failed")
            return PasteResult(pasted=False, warning=f"Couldn't paste on the PC: {exc}")

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait for a pending clipboard restore. True if none is pending any more."""
        thread = self._restore_thread
        if thread is None:
            return True
        if timeout is None:
            timeout = self.cfg.restore_delay_ms / 1000 + 5.0
        thread.join(timeout)
        if thread.is_alive():
            log.warning("clipboard restore still pending after %.1f s", timeout)
            return False
        if self._restore_thread is thread:
            self._restore_thread = None
        return True

    # -- steps

    def _paste(self, text: str) -> PasteResult:
        api = self._api
        cfg = self.cfg

        pid, problem = self._find_target()
        if problem:
            return self._not_pasted(text, problem)

        if not self._wait_for_modifiers():
            return self._not_pasted(text, MODIFIER_WARNING)

        snap: clipboard.ClipboardSnapshot | None = None
        if cfg.restore_clipboard:
            try:
                snap = api.clipboard_snapshot()
            except Exception:
                log.warning("couldn't snapshot the clipboard; it won't be restored", exc_info=True)

        try:
            seq = api.clipboard_set_text(text, exclude_from_history=cfg.exclude_from_history)
        except Exception:
            log.warning("couldn't put the text on the clipboard", exc_info=True)
            return PasteResult(pasted=False, warning=CLIPBOARD_WARNING)

        api.sleep(PRE_PASTE_DELAY_S)

        # Focus can move while we were busy: re-check right before the keystroke.
        pid, problem = self._find_target()
        if problem:
            return self._not_pasted(text, problem, on_clipboard=True)
        if api.held_modifiers():
            return self._not_pasted(text, MODIFIER_WARNING, on_clipboard=True)

        chord = self._chord_for(pid)
        sent = api.send_chord(chord)
        expected = len(chord_events(chord))
        if sent != expected:
            log.warning("SendInput inserted %d of %d events", sent, expected)
            return PasteResult(pasted=False, warning=SEND_FAILED_WARNING)
        log.debug("pasted %d chars with %s", len(text), chord.value)

        if snap is not None and (snap.formats or snap.original_format_count == 0):
            self._schedule_restore(snap, seq)
        elif snap is not None:
            log.info("clipboard had only formats we can't save; not restoring %r", snap)
        return PasteResult(pasted=True)

    def _find_target(self) -> tuple[int, None] | tuple[None, str]:
        """(pid of the foreground window's process, None) or (None, warning)."""
        api = self._api
        hwnd = api.foreground_window()
        desktop = api.input_desktop_name()
        if desktop is None or desktop.lower() != DEFAULT_DESKTOP.lower():
            log.info("input desktop is %r (locked / secure desktop); not pasting", desktop)
            return None, LOCKED_WARNING
        if not hwnd:
            log.info("no foreground window; not pasting")
            return None, NO_WINDOW_WARNING
        pid = api.window_pid(hwnd)
        target_rid = api.integrity_rid(pid) if pid else None
        own_rid = self._own_integrity()
        if target_rid is None or target_rid > own_rid:
            log.info(
                "foreground window pid=%s integrity=%s > ours=%#x (or unknown); not pasting",
                pid,
                f"{target_rid:#x}" if target_rid is not None else "unknown",
                own_rid,
            )
            return None, ADMIN_WINDOW_WARNING
        return pid, None

    def _own_integrity(self) -> int:
        if self._own_rid is None:
            rid = self._api.integrity_rid(None)
            self._own_rid = rid if rid is not None else w.SECURITY_MANDATORY_MEDIUM_RID
        return self._own_rid

    def _wait_for_modifiers(self) -> bool:
        api = self._api
        deadline = api.monotonic() + MODIFIER_WAIT_S
        while api.held_modifiers():
            if api.monotonic() >= deadline:
                log.info("modifier keys still held after %.1f s; not pasting", MODIFIER_WAIT_S)
                return False
            api.sleep(MODIFIER_POLL_S)
        return True

    def _chord_for(self, pid: int) -> PasteChord:
        names = normalize_process_names(self.cfg.shift_insert_processes)
        if not names:
            return PasteChord.CTRL_V
        try:
            exe = self._api.process_exe_name(pid)
        except Exception:
            log.debug("couldn't get the target's exe name", exc_info=True)
            exe = None
        if exe and exe.lower() in names:
            return PasteChord.SHIFT_INSERT
        return PasteChord.CTRL_V

    def _not_pasted(self, text: str, warning: str, *, on_clipboard: bool = False) -> PasteResult:
        """Leave the text on the clipboard (no restore) and report why it wasn't pasted."""
        notify = warning == ADMIN_WINDOW_WARNING
        if not on_clipboard:
            try:
                self._api.clipboard_set_text(
                    text, exclude_from_history=self.cfg.exclude_from_history
                )
            except Exception:
                log.warning("couldn't put the text on the clipboard either", exc_info=True)
                warning = NOT_COPIED_WARNING
        if notify:
            try:
                self.notifier.notify(NOTIFY_TITLE, warning)
            except Exception:
                log.exception("notifier failed")
        return PasteResult(pasted=False, warning=warning)

    def _schedule_restore(self, snap: clipboard.ClipboardSnapshot, seq: int) -> None:
        delay = self.cfg.restore_delay_ms / 1000
        api = self._api

        def run() -> None:
            try:
                api.sleep(delay)
                current = api.clipboard_sequence()
                if current != seq:
                    log.info(
                        "clipboard changed since the paste (%d != %d); not restoring", current, seq
                    )
                    return
                # The sequence is checked again inside the clipboard session (race-free).
                api.clipboard_restore(snap, exclude_from_history=True, expected_sequence=seq)
            except Exception:
                log.warning("couldn't restore the clipboard", exc_info=True)

        thread = threading.Thread(target=run, name="shoutabout-clipboard-restore", daemon=True)
        self._restore_thread = thread
        thread.start()
