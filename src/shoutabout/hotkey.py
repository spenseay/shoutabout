"""The "Talk to this PC" hotkey: a pure state machine plus a small Win32 keyboard listener.

Toggle mode (the default): a *clean tap* starts listening and the next clean press stops it.
A clean tap is the hotkey going down and up within :data:`TAP_MAX_S` with no other key
pressed in between; listening starts at the key-up. So Right Ctrl + C, Right Ctrl + V,
Ctrl + Shift + T... never start anything (and nothing is shown for them). While listening,
other keys are just typing and don't stop it, and neither does a shortcut with the hotkey
(Right Ctrl + V); the hotkey pressed and released on its own (any length) stops it.

Hold mode: hold the key for :data:`HOLD_DELAY_S` to start listening, let go to stop. A quick
tap does nothing, and if any other key goes down while the hotkey is held it was a shortcut:
nothing starts, or listening is cancelled silently. In both modes Esc cancels.

The listener **only observes**. It installs a ``WH_KEYBOARD_LL`` hook on its own thread,
copies each key event into a queue and always passes it on with ``CallNextHookEx``: no key
is ever swallowed, changed or injected (the only key ShoutAbout ever sends is the paste
chord, from :mod:`shoutabout.inject`). Injected events (our own paste, remote tools) are
ignored. The hook is removed when the listener stops, and at interpreter exit.

A release the hook never saw (Win+L, Ctrl+Alt+Del: the secure desktop takes the key-up) is
caught by reading the physical key while busy. Windows calls a low-level hook *before* it
updates that physical ("async") key state, so right after a key-down the key can still read
as up: such a reading only counts once :data:`RELEASE_GRACE_S` has passed since the last
hotkey event. (Trusting it at once turned a key-down into a whole tap: the pill popped up
during Right Ctrl + C in toggle mode, and hold mode only started on the key's auto-repeat,
half a second late.)

The state machine (:class:`HotkeyMachine`) has no Win32 in it and is unit-tested event by
event; the Win32 glue (:class:`KeyboardHook`) goes through a tiny facade so it is tested
with fakes too. Tests never install a real hook.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

# id -> label shown in the settings window. Order = order in the picker.
# Not offered on purpose: Right Shift (held while typing capitals, so it would keep starting
# and cancelling) and the Menu key (releasing it opens a context menu that takes the paste).
HOTKEY_CHOICES: dict[str, str] = {
    "right_ctrl": "Right Ctrl",
    "right_alt": "Right Alt",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "scroll_lock": "Scroll Lock",
    "pause": "Pause",
}
DEFAULT_HOTKEY = "right_ctrl"
DEFAULT_MODE = "toggle"

# Virtual-key codes. Low-level hooks report left/right-specific modifier codes.
HOTKEY_VKS: dict[str, int] = {
    "right_ctrl": 0xA3,  # VK_RCONTROL
    "right_alt": 0xA5,  # VK_RMENU
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "scroll_lock": 0x91,
    "pause": 0x13,
}
VK_ESCAPE = 0x1B
VK_LCONTROL = 0xA2
# AltGr (Right Alt on many non-US layouts) arrives as a fake Left Ctrl with this scan code
# right before Right Alt; it's part of the AltGr press, not another key.
ALTGR_FAKE_CTRL_SCAN = 0x21D

HOLD_DELAY_S = 0.2  # hold mode: hold this long before listening starts (a tap does nothing)
TAP_MAX_S = 0.5  # toggle mode: a press longer than this isn't a tap (nothing starts)
HELD_STALE_S = 1.5  # another key "held" longer than this without auto-repeat was missed
RELEASE_GRACE_S = 0.25  # trust a physical "up" reading only this long after a hotkey event
POLL_S = 0.05  # how often the listener re-checks the physical key while busy
# A queue wait with a timeout wakes up to one 15.6 ms timer tick late on Windows; the last
# stretch before a deadline is slept with time.sleep instead, which is precise.
COARSE_TIMER_S = 0.017

Mode = Literal["hold", "toggle"]


class Action(StrEnum):
    START = "start"
    STOP = "stop"
    CANCEL = "cancel"


@dataclass(frozen=True)
class HotkeyEvent:
    action: Action
    # START: "hold" | "press"; STOP: "release" | "press"; CANCEL: "escape" | "shortcut"
    reason: str = ""


def hotkey_label(hotkey: str) -> str:
    return HOTKEY_CHOICES.get(hotkey, hotkey)


# ------------------------------------------------------------------------ the machine


class HotkeyMachine:
    """Turns key events into START / STOP / CANCEL. Pure; not thread-safe (one caller).

    States: ``idle``; ``armed`` (hold mode: down, waiting out the hold delay; toggle mode:
    down, waiting for a clean release within :data:`TAP_MAX_S`); ``listening``;
    ``stopping`` (toggle mode: pressed again while listening, waiting for the release);
    ``blocked`` (ignore everything until the hotkey is released: it was part of a
    shortcut, held too long for a tap, or listening ended by itself).
    """

    def __init__(
        self,
        vk: int,
        mode: Mode = DEFAULT_MODE,
        hold_delay: float = HOLD_DELAY_S,
        tap_max: float = TAP_MAX_S,
    ) -> None:
        if mode not in ("hold", "toggle"):
            raise ValueError(f"unknown hotkey mode {mode!r}")
        self.vk = vk
        self.mode: Mode = mode
        self.hold_delay = hold_delay
        self.tap_max = tap_max
        self.state = "idle"
        self._pressed_at = 0.0
        self._last_hotkey_at = 0.0  # the last hotkey event seen (down, auto-repeat or up)
        self._down = False  # the hotkey is physically down (as far as we've seen)
        self._others: dict[int, float] = {}  # other keys down -> last down/repeat time

    # -- queries

    @property
    def listening(self) -> bool:
        return self.state in ("listening", "stopping")

    def deadline(self) -> float | None:
        """When :meth:`tick` must be called next (hold delay or tap limit), or None."""
        if self.state != "armed":
            return None
        return self._pressed_at + (self.hold_delay if self.mode == "hold" else self.tap_max)

    def busy(self) -> bool:
        """True while anything but idle (the listener then polls the physical key)."""
        return self.state != "idle"

    def _others_held(self, now: float) -> bool:
        stale = [vk for vk, t in self._others.items() if now - t > HELD_STALE_S]
        for vk in stale:
            del self._others[vk]
        return bool(self._others)

    # -- input

    def key(self, vk: int, down: bool, now: float) -> list[HotkeyEvent]:
        if vk == self.vk:
            return self._hotkey(down, now)
        if down:
            self._others[vk] = now
        else:
            self._others.pop(vk, None)
        if not down:
            return []
        return self._other_down(vk)

    def _hotkey(self, down: bool, now: float) -> list[HotkeyEvent]:
        repeat = down and self._down
        self._down = down
        self._last_hotkey_at = now
        if repeat:
            return []  # auto-repeat while held
        s = self.state
        if down:
            if s == "idle":
                if self._others_held(now):
                    self.state = "blocked"  # e.g. Shift is held: part of a shortcut
                else:
                    self.state, self._pressed_at = "armed", now
            elif s == "listening" and self.mode == "toggle" and not self._others_held(now):
                self.state = "stopping"  # (with another key held it's a shortcut: no stop)
            return []
        # released
        if s == "armed":
            if self.mode == "toggle" and now - self._pressed_at <= self.tap_max:
                self.state = "listening"  # a clean tap: listening starts at the key-up
                return [HotkeyEvent(Action.START, "press")]
            self.state = "idle"  # hold mode: a tap does nothing; toggle: held too long
            return []
        if s == "listening" and self.mode == "hold":
            self.state = "idle"
            return [HotkeyEvent(Action.STOP, "release")]
        if s == "stopping":
            self.state = "idle"
            return [HotkeyEvent(Action.STOP, "press")]
        if s == "blocked":
            self.state = "idle"
        return []

    def _other_down(self, vk: int) -> list[HotkeyEvent]:
        s = self.state
        if s == "armed":
            self.state = "blocked"  # hotkey + another key = a shortcut
            return []
        if s == "stopping":
            if vk == VK_ESCAPE:
                self.state = "blocked"
                return [HotkeyEvent(Action.CANCEL, "escape")]
            self.state = "listening"  # toggle: hotkey + key while listening = a shortcut
            return []
        if s == "listening":
            if vk == VK_ESCAPE:
                self.state = "blocked" if self._down else "idle"
                return [HotkeyEvent(Action.CANCEL, "escape")]
            if self.mode == "hold":
                self.state = "blocked"
                return [HotkeyEvent(Action.CANCEL, "shortcut")]
        return []

    def tick(self, now: float, hotkey_down: bool | None = None) -> list[HotkeyEvent]:
        """Time passes. ``hotkey_down`` is the physical key state when known: a release we
        never saw (the secure desktop took it) counts as a release, but only once
        :data:`RELEASE_GRACE_S` has passed since the last hotkey event (Windows updates the
        physical state after the hook has seen the key, so it lags a key-down)."""
        if hotkey_down is False and self._down and now - self._last_hotkey_at >= RELEASE_GRACE_S:
            return self._hotkey(False, now)
        if self.state != "armed":
            return []
        if self.mode == "toggle":
            if now - self._pressed_at >= self.tap_max:
                self.state = "blocked"  # held too long for a tap: not a dictation
            return []
        if now >= self._pressed_at + self.hold_delay:
            if self._others_held(now):
                self.state = "blocked"
                return []
            self.state = "listening"
            return [HotkeyEvent(Action.START, "hold")]
        return []

    def force_stop(self) -> None:
        """Listening ended by itself (time limit, mic error, not ready): go back to idle,
        ignoring the rest of the current press."""
        self.state = "blocked" if self._down else "idle"

    def reset(self) -> None:
        self.state = "idle"
        self._down = False
        self._others.clear()


# ---------------------------------------------------------------------- Win32 glue

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_QUIT = 0x0012
WM_USER = 0x0400
PM_NOREMOVE = 0x0000
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_UP = 0x80


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_uint32),
        ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = (ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
)

KeyCallback = Callable[[int, bool, int, int], None]  # vk, down, scan code, flags


class HookApi(Protocol):
    """The few user32 calls the hook thread makes (a fake in tests)."""

    def thread_id(self) -> int: ...
    def ensure_queue(self) -> None: ...
    def set_hook(self, proc: Any) -> int: ...
    def unhook(self, handle: int) -> bool: ...
    def call_next(self, code: int, wparam: int, lparam: int) -> int: ...
    def get_message(self) -> int: ...
    def post_quit(self, thread_id: int) -> bool: ...
    def read(self, lparam: int) -> tuple[int, int, int]: ...  # vk, scan, flags


class Win32HookApi:
    def __init__(self) -> None:
        from ctypes import wintypes

        self._wt = wintypes
        self._u32 = ctypes.WinDLL("user32", use_last_error=True)
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u = self._u32
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u.UnhookWindowsHookEx.restype = wintypes.BOOL
        u.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.GetMessageW.restype = wintypes.BOOL
        u.PeekMessageW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        u.PeekMessageW.restype = wintypes.BOOL
        u.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        u.PostThreadMessageW.restype = wintypes.BOOL
        self._k32.GetCurrentThreadId.restype = wintypes.DWORD
        self._k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._k32.GetModuleHandleW.restype = wintypes.HMODULE
        self._msg = (ctypes.c_byte * 64)()  # MSG is 48 bytes on x64

    def thread_id(self) -> int:
        return int(self._k32.GetCurrentThreadId())

    def ensure_queue(self) -> None:
        self._u32.PeekMessageW(ctypes.byref(self._msg), None, WM_USER, WM_USER, PM_NOREMOVE)

    def set_hook(self, proc: Any) -> int:
        handle = self._u32.SetWindowsHookExW(
            WH_KEYBOARD_LL, proc, self._k32.GetModuleHandleW(None), 0
        )
        if not handle:
            raise OSError(f"SetWindowsHookExW failed (error {ctypes.get_last_error()})")
        return int(handle)

    def unhook(self, handle: int) -> bool:
        return bool(self._u32.UnhookWindowsHookEx(ctypes.c_void_p(handle)))

    def call_next(self, code: int, wparam: int, lparam: int) -> int:
        return int(self._u32.CallNextHookEx(None, code, wparam, lparam))

    def get_message(self) -> int:
        # Low-level hook callbacks run inside this call; we never expect window messages.
        return int(self._u32.GetMessageW(ctypes.byref(self._msg), None, 0, 0))

    def post_quit(self, thread_id: int) -> bool:
        return bool(self._u32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0))

    def read(self, lparam: int) -> tuple[int, int, int]:
        kb = KBDLLHOOKSTRUCT.from_address(lparam)
        return int(kb.vkCode), int(kb.scanCode), int(kb.flags)


class KeyboardHook:
    """A ``WH_KEYBOARD_LL`` hook on its own thread that reports key events to ``on_key``.

    Observe-only: the hook procedure always returns ``CallNextHookEx`` (so every key
    reaches its window unchanged) and skips injected events. ``on_key`` runs on the hook
    thread inside the hook call, so it must be quick (we just queue the event): Windows
    drops hooks that are slow to answer.
    """

    def __init__(self, on_key: KeyCallback, *, api: HookApi | None = None) -> None:
        self._on_key = on_key
        self._api = api
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._tid = 0
        self._handle = 0
        self._proc_ref: Any = None  # keep the ctypes thunk alive while hooked
        self._lock = threading.Lock()

    @property
    def installed(self) -> bool:
        return bool(self._handle)

    def start(self, timeout: float = 5.0) -> None:
        """Install the hook (blocks until it is in place). Raises OSError on failure."""
        with self._lock:
            if self._thread is not None:
                return
            if self._api is None:
                self._api = Win32HookApi()
            self._ready.clear()
            self._error = None
            self._thread = threading.Thread(target=self._run, name="hotkey-hook", daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise OSError("the keyboard listener didn't start")
        if self._error is not None:
            error = self._error
            self.stop()
            raise OSError(f"couldn't listen for the hotkey: {error}") from error
        atexit.register(self.stop)

    def stop(self, timeout: float = 5.0) -> None:
        """Remove the hook and end the thread. Safe to call more than once."""
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return
        api = self._api
        if api is not None and self._tid:
            api.post_quit(self._tid)
        thread.join(timeout)
        if thread.is_alive():
            log.warning("the keyboard listener thread didn't stop")
        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

    def _proc(self, code: int, wparam: int, lparam: int) -> int:
        api = self._api
        assert api is not None
        try:
            if code == HC_ACTION:
                vk, scan, flags = api.read(lparam)
                if not flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED):
                    self._on_key(vk, not flags & LLKHF_UP, scan, flags)
        except Exception:  # never let an error change what happens to the key
            log.debug("hotkey callback failed", exc_info=True)
        return api.call_next(code, wparam, lparam)

    def _run(self) -> None:
        api = self._api
        assert api is not None
        handle = 0
        try:
            self._tid = api.thread_id()
            api.ensure_queue()
            self._proc_ref = HOOKPROC(self._proc)
            handle = api.set_hook(self._proc_ref)
            self._handle = handle
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        log.info("listening for the dictation hotkey")
        try:
            while True:
                result = api.get_message()
                if result == 0 or result == -1:  # WM_QUIT or an error
                    break
        finally:
            if handle:
                api.unhook(handle)
            self._handle = 0
            log.info("stopped listening for the dictation hotkey")


# ----------------------------------------------------------------- the listener


def is_key_down(vk: int) -> bool | None:
    """The physical state of a key (Windows), or None when it can't be read."""
    if sys.platform != "win32":
        return None
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)  # type: ignore[attr-defined]
    except Exception:
        return None


class HotkeyListener:
    """Keyboard hook + :class:`HotkeyMachine` + a worker thread that calls ``on_event``.

    The hook thread only queues raw events (time-stamped with ``perf_counter``: the
    monotonic clock only ticks every 15.6 ms on Windows); this worker feeds them to the
    machine, runs the hold timer, double-checks the physical key while busy, and calls
    ``on_event`` with START / STOP / CANCEL. ``on_event`` runs on the worker thread.
    """

    def __init__(
        self,
        hotkey: str,
        mode: Mode,
        on_event: Callable[[HotkeyEvent], None],
        *,
        hook_factory: Callable[[KeyCallback], Any] = KeyboardHook,
        key_state: Callable[[int], bool | None] = is_key_down,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if hotkey not in HOTKEY_VKS:
            raise ValueError(f"unknown hotkey {hotkey!r}")
        self.hotkey = hotkey
        self.machine = HotkeyMachine(HOTKEY_VKS[hotkey], mode)
        self._on_event = on_event
        self._hook = hook_factory(self._on_key)
        self._key_state = key_state
        self._clock = clock
        self._queue: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._worker: threading.Thread | None = None

    def _on_key(self, vk: int, down: bool, scan: int, flags: int) -> None:
        if vk == VK_LCONTROL and scan == ALTGR_FAKE_CTRL_SCAN:
            return
        self._queue.put(("key", (vk, down, self._clock())))

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._worker.start()
        try:
            self._hook.start()
        except BaseException:
            self._queue.put(("quit", None))
            self._worker.join(2.0)
            self._worker = None
            raise

    def stop(self) -> None:
        try:
            self._hook.stop()
        finally:
            worker, self._worker = self._worker, None
            if worker is not None:
                self._queue.put(("quit", None))
                if worker is not threading.current_thread():
                    worker.join(2.0)

    def force_stop(self) -> None:
        """Listening ended by itself; the rest of this key press is ignored."""
        self._queue.put(("force_stop", None))

    def _emit(self, events: list[HotkeyEvent]) -> None:
        for event in events:
            try:
                self._on_event(event)
            except Exception:
                log.exception("hotkey handler failed")

    def _next(self) -> tuple[str, Any]:
        """The next queued item, or ("tick", None) when it's time to look at the clock."""
        m = self.machine
        if not m.busy():
            return self._queue.get()
        timeout = POLL_S
        deadline = m.deadline()
        if deadline is not None:
            timeout = max(0.0, min(timeout, deadline - self._clock()))
        try:
            if timeout > COARSE_TIMER_S:  # a key event still wakes us at once
                return self._queue.get(timeout=timeout - COARSE_TIMER_S)
            return self._queue.get_nowait()
        except queue.Empty:
            pass
        if timeout <= COARSE_TIMER_S and timeout > 0:
            time.sleep(timeout)  # precise; whatever arrives meanwhile is handled next
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                pass
        return "tick", None

    def _run(self) -> None:
        m = self.machine
        while True:
            kind, payload = self._next()
            if kind == "quit":
                return
            if kind == "key":
                vk, down, at = payload
                # Deadlines that passed before this key went down count first, so a busy
                # moment on this thread never reorders a hold or a tap.
                self._emit(m.tick(at))
                self._emit(m.key(vk, down, at))
            elif kind == "force_stop":
                m.force_stop()
            if m.busy() and self._queue.empty():  # never tick past events still queued
                self._emit(m.tick(self._clock(), self._key_state(m.vk)))
