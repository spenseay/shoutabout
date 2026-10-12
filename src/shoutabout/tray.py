"""Notification-area (tray) icon, menu and balloon toasts in pure ctypes (no pystray).

Threading model: :meth:`TrayIcon.run` owns a hidden top-level window and its message
loop (the hub runs it on the main thread). Everything else is thread-safe and only posts
messages to that window: state changes (``WM_APP+2``), notifications (``WM_APP+3``) and
:meth:`TrayIcon.stop` (``WM_CLOSE``); the icon itself is only touched on the tray thread.

The icon is the brand's mouth (:mod:`shoutabout.icons`). Animated states (the mouth
talks while transcribing, hums while starting) cycle pre-built HICON frames on a
``WM_TIMER`` that only runs while such a state is shown. Every HICON is created once per
state and size, and destroyed when the tray stops (the shell keeps its own copy).

The window is a hidden *top-level* window (not message-only) so it receives the
"TaskbarCreated" broadcast and re-adds the icon after Explorer restarts. If ``NIM_ADD``
fails (Explorer not ready yet at logon) it is retried with backoff.

The menu is short on purpose: a status line ("Ready", "Getting ready… 42%", "Listening",
"Paused"), how to talk ("Press Right Ctrl to talk, and again to stop."), Pause/Resume,
Recent…, Teach words…, Settings…, Set up…, Quit. The first three open the one ShoutAbout
window on that page.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from shoutabout import icons
from shoutabout.inject import win32 as w
from shoutabout.models import HubStatus
from shoutabout.state import HubState

log = logging.getLogger(__name__)

WM_TRAY_CALLBACK = w.WM_APP + 1
WM_TRAY_STATE = w.WM_APP + 2
WM_TRAY_NOTIFY = w.WM_APP + 3

TRAY_ICON_ID = 1
TIMER_ADD_RETRY = 1
TIMER_ANIMATE = 2
ADD_RETRY_DELAYS_MS = (1000, 2000, 4000, 8000, 15000, 30000, 30000, 60000, 60000, 120000)
MAX_QUEUED_NOTES = 20

TIP_MAX = 127  # szTip[128]
INFO_TITLE_MAX = 63  # szInfoTitle[64]
INFO_MAX = 255  # szInfo[256]
MENU_ERROR_MAX = 60

CMD_STATUS = 1
CMD_HINT = 2
CMD_PAUSE = 10
CMD_RECENT = 14
CMD_TEACH = 15
CMD_SETTINGS = 17
CMD_SETUP = 18
CMD_QUIT = 19


@dataclass
class TrayActions:
    toggle_pause: Callable[[], None]
    quit: Callable[[], None]
    # Optional windows; each menu item appears only when its action is set.
    recent: Callable[[], None] | None = None  # Recent… (the last few dictations)
    teach: Callable[[], None] | None = None  # Teach words…
    settings: Callable[[], None] | None = None  # Settings…
    setup: Callable[[], None] | None = None  # Set up…
    # "Press Right Ctrl to talk…" (local_dictation.hint_text), read when the menu opens.
    hint: Callable[[], str | None] | None = None
    # v2-voice: the model download percent while the hub is "preparing" (None = unknown).
    percent: Callable[[], int | None] | None = None


@dataclass(frozen=True)
class MenuItem:
    id: int = 0
    label: str = ""
    enabled: bool = True
    separator: bool = False


SEPARATOR = MenuItem(separator=True)


# --------------------------------------------------------------------- pure helpers


def utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Shorten to at most ``limit`` UTF-16 code units (what the WCHAR buffers hold)."""
    text = text.replace("\0", "")
    if utf16_len(text) <= limit:
        return text
    budget = limit - utf16_len(ellipsis)
    out: list[str] = []
    used = 0
    for ch in text:
        n = 2 if ord(ch) > 0xFFFF else 1
        if used + n > budget:
            break
        out.append(ch)
        used += n
    return "".join(out).rstrip() + ellipsis


def short_error(error: str | None, limit: int = MENU_ERROR_MAX) -> str:
    first = (error or "unknown error").strip().splitlines()
    return truncate(first[0] if first else "unknown error", limit)


def status_header(
    status: HubStatus | str, paused: bool, error: str | None = None, percent: int | None = None
) -> str:
    """The plain-English status: "Ready", "Getting ready… 42%", "Listening", "Paused"...

    Takes the status as a string, so states the hub gains later ("preparing" while the
    model downloads, "listening" while the hotkey is held) read well here too.
    """
    value = str(getattr(status, "value", status)).lower()
    if value == "error":
        phrase = f"Needs attention: {short_error(error)}"
    elif value in ("starting", "preparing"):
        phrase = "Getting ready…"
        if isinstance(percent, int) and 0 <= percent <= 100:
            phrase = f"Getting ready… {percent}%"
    elif value == "listening":
        phrase = "Listening"
    elif value == "transcribing":
        phrase = "Writing…"
    elif paused:
        return "Paused"
    else:
        phrase = "Ready"
    return f"{phrase} (paused)" if paused else phrase


def status_line(
    title: str,
    status: HubStatus | str,
    paused: bool,
    error: str | None,
    percent: int | None = None,
) -> str:
    return f"{title} — {status_header(status, paused, error, percent)}"


def tooltip_text(
    title: str,
    status: HubStatus | str,
    paused: bool,
    error: str | None,
    percent: int | None = None,
) -> str:
    return truncate(status_line(title, status, paused, error, percent), TIP_MAX)


def _menu_label(text: str) -> str:
    return text.replace("&", "&&")  # '&' marks a mnemonic in menu text


def build_menu(
    title: str,
    status: HubStatus | str,
    paused: bool,
    error: str | None,
    *,
    hint: str | None = None,
    percent: int | None = None,
    teaching: bool = False,
    settings: bool = False,
    setup: bool = False,
    recent: bool = False,
) -> list[MenuItem]:
    """Status, how to talk, Pause/Resume, Recent…, Teach words…, Settings…, Set up…, Quit."""
    header = status_header(status, paused, error, percent)
    items = [MenuItem(CMD_STATUS, _menu_label(truncate(header, 64)), False)]
    if hint:
        items.append(MenuItem(CMD_HINT, _menu_label(truncate(hint, 64)), False))
    items += [SEPARATOR, MenuItem(CMD_PAUSE, "Resume" if paused else "Pause")]
    if recent:
        items.append(MenuItem(CMD_RECENT, "Recent…"))
    if teaching:
        items.append(MenuItem(CMD_TEACH, "Teach words…"))
    if settings:
        items.append(MenuItem(CMD_SETTINGS, "Settings…"))
    if setup:
        items.append(MenuItem(CMD_SETUP, "Set up…"))
    return [*items, SEPARATOR, MenuItem(CMD_QUIT, "Quit")]


def _signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


# ----------------------------------------------------------------------- tray icon


class TrayIcon:
    """Implements :class:`shoutabout.inject.base.Notifier`."""

    def __init__(self, state: HubState, actions: TrayActions, title: str = "ShoutAbout") -> None:
        self._state = state
        self._actions = actions
        self._title = title
        self._lock = threading.Lock()
        self._hwnd: int | None = None
        self._notes: deque[tuple[str, str]] = deque(maxlen=MAX_QUEUED_NOTES)
        self._stop_requested = False
        self._running = False
        # Tray-thread-only state:
        self._icon_added = False
        self._v4 = False
        self._add_attempts = 0
        self._menu_open = False
        self._post_quit = True
        self._icons: dict[str, list[int]] = {}  # state -> one HICON per frame
        self._key: str | None = None  # the state shown
        self._frame = 0
        self._timer_ms = 0  # animation timer interval while it runs, else 0
        self._modify_failing = False
        self._icon_size = 16
        self._taskbar_created_msg = 0
        self._wndproc_ref = None  # the WNDPROC thunk must outlive the window
        self._class_name = f"ShoutAboutTray.{os.getpid()}.{id(self):x}"
        state.subscribe(self._on_state_change)

    # ------------------------------------------------------------- thread-safe API

    def notify(self, title: str, message: str) -> None:
        """Show a balloon / toast. Thread-safe; queued until the tray is running. Never raises."""
        try:
            log.info("notification: %s: %s", title, message)
            with self._lock:
                self._notes.append((str(title), str(message)))
                hwnd = self._hwnd
            if hwnd:
                w.user32.PostMessageW(hwnd, WM_TRAY_NOTIFY, 0, 0)
        except Exception:
            log.exception("tray notify failed")

    def stop(self) -> None:
        """Remove the icon and make run() return. Thread-safe; safe before/after run()."""
        try:
            with self._lock:
                self._stop_requested = True
                hwnd = self._hwnd
            if hwnd:
                w.user32.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
        except Exception:
            log.exception("tray stop failed")

    def _on_state_change(self, _state: HubState) -> None:
        with self._lock:
            hwnd = self._hwnd
        if hwnd:
            w.user32.PostMessageW(hwnd, WM_TRAY_STATE, 0, 0)

    # ------------------------------------------------------------------- run loop

    def run(self) -> None:
        """Create the icon and pump messages until stop() / Quit. Blocking."""
        w.require_windows()
        with self._lock:
            if self._running:
                raise RuntimeError("TrayIcon.run() is already running")
            self._running = True
        self._post_quit = True
        u32 = w.user32
        hinst = w.kernel32.GetModuleHandleW(None)
        self._wndproc_ref = w.WNDPROC(self._wndproc)
        wc = w.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        registered = False
        try:
            if not u32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(f"RegisterClassExW failed (error {w.last_error()})")
            registered = True
            self._taskbar_created_msg = u32.RegisterWindowMessageW("TaskbarCreated")
            self._icon_size = icons.small_icon_size()
            hwnd = u32.CreateWindowExW(
                0,
                self._class_name,
                self._title,
                w.WS_OVERLAPPED,
                0,
                0,
                0,
                0,
                None,
                None,
                hinst,
                None,
            )
            if not hwnd:
                raise OSError(f"CreateWindowExW failed (error {w.last_error()})")
            with self._lock:
                self._hwnd = hwnd
                stop_now = self._stop_requested
            if stop_now:  # stop() was called before the window existed
                u32.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
            else:
                self._add_icon()
            self._message_loop()
        finally:
            self._cleanup()
            if registered:
                u32.UnregisterClassW(self._class_name, hinst)
            with self._lock:
                self._hwnd = None
                self._running = False

    def _message_loop(self) -> None:
        u32 = w.user32
        msg = w.MSG()
        while True:
            result = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0:  # WM_QUIT
                return
            if result == -1:
                log.error("GetMessageW failed (error %d)", w.last_error())
                return
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _cleanup(self) -> None:
        hwnd = self._hwnd
        try:
            self._stop_animation()
            if self._icon_added and hwnd:
                self._shell_notify(w.NIM_DELETE, self._nid(hwnd))
            self._icon_added = False
            if hwnd and w.user32.IsWindow(hwnd):
                # The loop already ended: don't leave a stray WM_QUIT in this thread's queue.
                self._post_quit = False
                w.user32.DestroyWindow(hwnd)
        except Exception:
            log.exception("tray cleanup failed")
        self._destroy_icons()
        self._key = None
        self._frame = 0

    def _destroy_icons(self) -> None:
        for frames in self._icons.values():
            for hicon in frames:
                icons.destroy_hicon(hicon)
        self._icons.clear()

    # --------------------------------------------------------------- window proc

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_TRAY_CALLBACK:
                self._on_tray_event(lparam, wparam)
                return 0
            if msg == WM_TRAY_STATE:
                self._refresh()
                return 0
            if msg == WM_TRAY_NOTIFY:
                self._flush_notes()
                return 0
            if msg == w.WM_TIMER and wparam == TIMER_ANIMATE:
                self._next_frame()
                return 0
            if msg == w.WM_TIMER and wparam == TIMER_ADD_RETRY:
                w.user32.KillTimer(hwnd, TIMER_ADD_RETRY)
                if not self._icon_added:
                    self._add_icon()
                return 0
            if self._taskbar_created_msg and msg == self._taskbar_created_msg:
                log.info("taskbar (re)created; re-adding the tray icon")
                self._icon_added = False
                self._add_attempts = 0
                size = icons.small_icon_size()
                if size != self._icon_size:  # e.g. display scaling changed
                    self._destroy_icons()
                    self._icon_size = size
                self._add_icon()
                return 0
            if msg == w.WM_CLOSE:
                self._stop_animation()
                if self._icon_added:
                    self._shell_notify(w.NIM_DELETE, self._nid(hwnd))
                    self._icon_added = False
                w.user32.DestroyWindow(hwnd)
                return 0
            if msg == w.WM_DESTROY:
                if self._post_quit:
                    w.user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("tray window procedure failed (msg %#x)", msg)
        return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_tray_event(self, lparam: int, wparam: int) -> None:
        event = lparam & 0xFFFF
        if self._v4:
            if event in (w.WM_CONTEXTMENU, w.NIN_SELECT, w.NIN_KEYSELECT):
                # v4: wParam carries the anchor point (cursor or keyboard focus).
                self._show_menu(_signed16(wparam), _signed16(wparam >> 16))
        elif event in (w.WM_LBUTTONUP, w.WM_RBUTTONUP):
            self._show_menu(None, None)

    # --------------------------------------------------------------- icon & toasts

    def _nid(self, hwnd: int) -> w.NOTIFYICONDATAW:
        nid = w.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(w.NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = TRAY_ICON_ID
        return nid

    def _current_nid(self, hwnd: int) -> w.NOTIFYICONDATAW:
        state = self._state
        status, paused, error = state.status, state.paused, state.error
        key = icons.icon_key(status, paused)
        if key != self._key:  # a new state starts at its first frame
            self._key, self._frame = key, 0
        nid = self._nid(hwnd)
        nid.uFlags = w.NIF_MESSAGE | w.NIF_ICON | w.NIF_TIP | w.NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        frames = self._frames(key)
        nid.hIcon = frames[self._frame % len(frames)] if frames else None
        nid.szTip = tooltip_text(self._title, status, paused, error, self._percent())
        return nid

    def _percent(self) -> int | None:
        """Download percent while preparing (v2-voice: HubState progress), else None."""
        try:
            if self._actions.percent is not None:
                value = self._actions.percent()
            else:
                value = getattr(self._state, "progress", None)  # v2-voice: HubState
            return int(value) if isinstance(value, int | float) else None
        except Exception:
            return None

    def _hint(self) -> str | None:
        if self._actions.hint is None:
            return None
        try:
            hint = self._actions.hint()
        except Exception:
            log.exception("couldn't read the dictation hint")
            return None
        return str(hint) if hint else None

    def _frames(self, key: str) -> list[int] | None:
        """The HICONs for a state, created on first use (tray thread only)."""
        frames = self._icons.get(key)
        if frames is None:
            try:
                frames = icons.make_hicons(key, self._icon_size)
            except (OSError, ValueError):
                log.exception("couldn't create the %s tray icon", key)
                return None
            self._icons[key] = frames
        return frames

    # ---------------------------------------------------------------- animation

    def _sync_animation(self) -> None:
        """Run the frame timer exactly while an animated state is on show."""
        hwnd = self._hwnd
        key = self._key
        interval = 0
        if hwnd and self._icon_added and key and len(self._icons.get(key) or ()) > 1:
            interval = icons.frame_interval_ms(key)
        if interval == self._timer_ms:
            return
        if not interval:
            self._stop_animation()
        elif w.user32.SetTimer(hwnd, TIMER_ANIMATE, interval, None):  # replaces a running one
            self._timer_ms = interval
        else:
            log.warning("SetTimer failed (error %d); the tray icon won't animate", w.last_error())
            self._timer_ms = 0

    def _stop_animation(self) -> None:
        if self._timer_ms and self._hwnd:
            w.user32.KillTimer(self._hwnd, TIMER_ANIMATE)
        self._timer_ms = 0

    def _next_frame(self) -> None:
        frames = self._icons.get(self._key) if self._key else None
        self._frame = (self._frame + 1) % (len(frames) if frames else 1)
        self._refresh()  # re-reads the state too: a stale tick can't show the wrong state

    def _shell_notify(self, message: int, nid: w.NOTIFYICONDATAW) -> bool:
        return bool(w.shell32.Shell_NotifyIconW(message, ctypes.byref(nid)))

    def _add_icon(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        nid = self._current_nid(hwnd)
        # NIM_ADD can report failure (e.g. a timeout) although the icon was added, or the
        # icon may already exist: NIM_MODIFY tells us whether it is there.
        ok = self._shell_notify(w.NIM_ADD, nid) or self._shell_notify(w.NIM_MODIFY, nid)
        if not ok:
            self._icon_added = False
            delay = ADD_RETRY_DELAYS_MS[min(self._add_attempts, len(ADD_RETRY_DELAYS_MS) - 1)]
            self._add_attempts += 1
            log.warning(
                "couldn't add the tray icon (attempt %d, error %d); retrying in %d ms",
                self._add_attempts,
                w.last_error(),
                delay,
            )
            w.user32.SetTimer(hwnd, TIMER_ADD_RETRY, delay, None)
            self._sync_animation()
            return
        self._icon_added = True
        self._add_attempts = 0
        self._modify_failing = False
        nid.uVersion = w.NOTIFYICON_VERSION_4
        self._v4 = self._shell_notify(w.NIM_SETVERSION, nid)
        if not self._v4:
            log.info("NOTIFYICON_VERSION_4 not accepted; using legacy click messages")
        self._sync_animation()
        self._flush_notes()

    def _refresh(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            return
        nid = self._current_nid(hwnd)
        if self._icon_added:
            if self._shell_notify(w.NIM_MODIFY, nid):
                self._modify_failing = False
            elif not self._modify_failing:  # once per failure streak: frames tick ~7x/s
                self._modify_failing = True
                log.warning("tray icon update failed (error %d)", w.last_error())
        self._sync_animation()

    def _flush_notes(self) -> None:
        hwnd = self._hwnd
        if not hwnd or not self._icon_added:
            return  # stays queued until the icon exists
        while True:
            with self._lock:
                if not self._notes:
                    return
                title, message = self._notes.popleft()
            nid = self._current_nid(hwnd)
            nid.uFlags |= w.NIF_INFO
            nid.szInfoTitle = truncate(title, INFO_TITLE_MAX)
            nid.szInfo = truncate(message, INFO_MAX) or " "  # empty szInfo hides the balloon
            nid.dwInfoFlags = w.NIIF_INFO
            if not self._shell_notify(w.NIM_MODIFY, nid):
                log.warning("couldn't show a notification (error %d)", w.last_error())

    # --------------------------------------------------------------------- menu

    def _show_menu(self, x: int | None, y: int | None) -> None:
        hwnd = self._hwnd
        if not hwnd or self._menu_open:
            return
        u32 = w.user32
        state = self._state
        actions = self._actions
        items = build_menu(
            self._title,
            state.status,
            state.paused,
            state.error,
            hint=self._hint(),
            percent=self._percent(),
            teaching=actions.teach is not None,
            settings=actions.settings is not None,
            setup=actions.setup is not None,
            recent=actions.recent is not None,
        )
        hmenu = u32.CreatePopupMenu()
        if not hmenu:
            log.warning("CreatePopupMenu failed (error %d)", w.last_error())
            return
        self._menu_open = True
        try:
            for item in items:
                if item.separator:
                    u32.AppendMenuW(hmenu, w.MF_SEPARATOR, 0, None)
                else:
                    flags = w.MF_STRING | (0 if item.enabled else w.MF_GRAYED)
                    u32.AppendMenuW(hmenu, flags, item.id, item.label)
            if x is None or y is None:
                pt = w.POINT()
                u32.GetCursorPos(ctypes.byref(pt))
                x, y = pt.x, pt.y
            # Without this the menu doesn't close when the user clicks elsewhere.
            u32.SetForegroundWindow(hwnd)
            flags = w.TPM_RIGHTBUTTON | w.TPM_RETURNCMD | w.TPM_NONOTIFY
            flags |= w.TPM_RIGHTALIGN | w.TPM_BOTTOMALIGN
            cmd = u32.TrackPopupMenu(hmenu, flags, x, y, 0, hwnd, None)
            u32.PostMessageW(hwnd, w.WM_NULL, 0, 0)
        finally:
            u32.DestroyMenu(hmenu)
            self._menu_open = False
        if cmd:
            self._dispatch(int(cmd))

    def _dispatch(self, cmd: int) -> None:
        """Run a menu command. Quit runs inline (and always stops the tray); the others run
        on a short-lived thread so a slow action never freezes the tray."""
        if cmd == CMD_QUIT:
            try:
                self._actions.quit()
            except Exception:
                log.exception("tray action 'quit' failed")
            finally:
                self.stop()
            return
        actions: dict[int, tuple[str, Callable[[], None]]] = {
            CMD_PAUSE: ("toggle_pause", self._actions.toggle_pause),
        }
        for command, name in (
            (CMD_RECENT, "recent"),
            (CMD_TEACH, "teach"),
            (CMD_SETTINGS, "settings"),
            (CMD_SETUP, "setup"),
        ):
            fn = getattr(self._actions, name)
            if fn is not None:
                actions[command] = (name, fn)
        entry = actions.get(cmd)
        if entry is None:
            return
        name, fn = entry

        def call() -> None:
            try:
                fn()
            except Exception:
                log.exception("tray action %r failed", name)

        threading.Thread(target=call, name=f"tray-{name}", daemon=True).start()
