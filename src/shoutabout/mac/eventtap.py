"""The "Talk to this Mac" hotkey listener: a Quartz event tap on its own thread.

:class:`MacEventTap` has the interface of :class:`shoutabout.hotkey.KeyboardHook`
(``start``/``stop``/``installed``, reporting ``on_key(vk, down, scan, flags)``), so
:class:`MacHotkeyListener` is the Windows :class:`~shoutabout.hotkey.HotkeyListener` with a
different hook: the same worker thread and the same :class:`~shoutabout.hotkey.HotkeyMachine`
(clean tap starts, the next clean tap stops, Esc cancels, any chord is a shortcut and never
starts anything). Key codes become virtual keys in :mod:`shoutabout.mac.keys`.

**Observe only.** The tap's callback copies the event's type, key code and flags into the
listener's queue and returns the event unchanged; it never swallows, changes or posts a
key. Two kinds of tap, picked by the permissions macOS has granted:

* **listen-only** (``kCGEventTapOptionListenOnly``) when Input Monitoring is allowed: events
  never wait for us at all;
* otherwise an ordinary tap (``kCGEventTapOptionDefault``), which macOS allows with
  Accessibility alone (the permission ShoutAbout needs anyway to paste). Its callback still
  returns every event untouched; it only sits in the event path, so it must be quick.

macOS turns a tap off if its callback is slow (``kCGEventTapDisabledByTimeout``) or on
some user input; the callback turns it straight back on. Events ShoutAbout posts itself
(the Cmd+V) carry a mark in ``kCGEventSourceUserData`` and are ignored, like injected
events on Windows. With Secure Keyboard Entry on (password fields, some terminals) macOS
sends taps no key events at all, so the hotkey can't start dictation there.

The tap runs on a "hotkey-hook" thread with its own CFRunLoop (the pynput pattern: run the
loop in short slices and check a stop flag), never on the main thread, which Cocoa needs
for the menu bar and the overlay.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from shoutabout.hotkey import HOTKEY_VKS, HotkeyListener, KeyCallback, Mode
from shoutabout.mac import keys

log = logging.getLogger(__name__)

RUNLOOP_SLICE_S = 0.25
RUNLOOP_FINISHED = 1  # kCFRunLoopRunFinished


class TapApi(Protocol):
    """The Quartz calls the tap thread makes (a fake in tests)."""

    def create(self, listen_only: bool, mask: int, callback: Callable[..., Any]) -> Any: ...
    def run(self, tap: Any, stop: threading.Event, ready: Callable[[], None]) -> None: ...
    def enable(self, tap: Any) -> None: ...
    def read(self, event: Any) -> tuple[int, int, int, int]: ...  # keycode, flags, button, mark
    def listen_allowed(self) -> bool | None: ...
    def post_allowed(self) -> bool | None: ...


class QuartzTapApi:
    """The real thing (macOS, PyObjC Quartz)."""

    def __init__(self) -> None:
        import Quartz

        self.Q = Quartz

    def create(self, listen_only: bool, mask: int, callback: Callable[..., Any]) -> Any:
        q = self.Q
        option = q.kCGEventTapOptionListenOnly if listen_only else q.kCGEventTapOptionDefault
        return q.CGEventTapCreate(
            q.kCGSessionEventTap, q.kCGHeadInsertEventTap, option, mask, callback, None
        )

    def run(self, tap: Any, stop: threading.Event, ready: Callable[[], None]) -> None:
        q = self.Q
        source = q.CFMachPortCreateRunLoopSource(None, tap, 0)
        loop = q.CFRunLoopGetCurrent()
        q.CFRunLoopAddSource(loop, source, q.kCFRunLoopDefaultMode)
        q.CGEventTapEnable(tap, True)
        ready()
        try:
            while not stop.is_set():
                result = q.CFRunLoopRunInMode(q.kCFRunLoopDefaultMode, RUNLOOP_SLICE_S, False)
                if result == RUNLOOP_FINISHED:  # no source left: the tap is gone
                    log.warning("the hotkey listener's event tap went away")
                    break
        finally:
            q.CGEventTapEnable(tap, False)
            q.CFRunLoopRemoveSource(loop, source, q.kCFRunLoopDefaultMode)
            q.CFMachPortInvalidate(tap)

    def enable(self, tap: Any) -> None:
        self.Q.CGEventTapEnable(tap, True)

    def read(self, event: Any) -> tuple[int, int, int, int]:
        q = self.Q
        keycode = int(q.CGEventGetIntegerValueField(event, q.kCGKeyboardEventKeycode))
        flags = int(q.CGEventGetFlags(event))
        button = int(q.CGEventGetIntegerValueField(event, q.kCGMouseEventButtonNumber))
        mark = int(q.CGEventGetIntegerValueField(event, q.kCGEventSourceUserData))
        return keycode, flags, button, mark

    def listen_allowed(self) -> bool | None:
        from shoutabout.mac.permissions import input_monitoring_granted

        return input_monitoring_granted()

    def post_allowed(self) -> bool | None:
        from shoutabout.mac.permissions import accessibility_trusted

        return accessibility_trusted()


class TapPermissionError(OSError):
    """macOS refused the event tap: a permission is missing (the message says which)."""


def choose_listen_only(listen: bool | None, post: bool | None) -> list[bool]:
    """Which tap kinds to try, in order (True = listen-only).

    Input Monitoring granted: listen-only. Accessibility granted: an ordinary tap, then
    listen-only as a last resort. Both refused: nothing (trying would make macOS ask for
    Input Monitoring too; the app asks for Accessibility and retries once it's on). Both
    unknown (older macOS, no PyObjC answer): try listen-only."""
    if listen is None and post is None:
        return [True]
    order: list[bool] = []
    if listen:
        order.append(True)
    if post:
        order.append(False)
        if True not in order:
            order.append(True)
    return order


class MacEventTap:
    """A keyboard/mouse event tap on its own thread (see the module docstring)."""

    def __init__(self, on_key: KeyCallback, *, api: TapApi | None = None) -> None:
        self._on_key = on_key
        self._api = api
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._tap: Any = None
        self._translator = keys.KeyTranslator()
        self._callback_ref = self._callback  # one bound method, kept alive while tapped
        self.listen_only: bool | None = None
        self._lock = threading.Lock()

    @property
    def installed(self) -> bool:
        return self._tap is not None and self._thread is not None

    def start(self, timeout: float = 5.0) -> None:
        """Create the tap (blocks until it runs). Raises OSError when macOS refuses it."""
        with self._lock:
            if self._thread is not None:
                return
            if self._api is None:
                self._api = QuartzTapApi()
            self._stop.clear()
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
            raise error if isinstance(error, OSError) else OSError(str(error))

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("the keyboard listener thread didn't stop")
        self._tap = None
        self._translator.reset()

    # -- tap thread

    def _callback(self, _proxy: Any, kind: int, event: Any, _refcon: Any) -> Any:
        """Runs inside the event path: copy what we need, return the event untouched."""
        try:
            kind = int(kind)
            if kind in (keys.EV_TAP_DISABLED_BY_TIMEOUT, keys.EV_TAP_DISABLED_BY_USER_INPUT):
                if self._tap is not None and self._api is not None:
                    self._api.enable(self._tap)
                    log.info("the hotkey listener was paused by macOS; turned it back on")
                return event
            api = self._api
            if api is None:
                return event
            keycode, flags, button, mark = api.read(event)
            if mark == keys.OWN_EVENT_MARK:
                return event  # our own Cmd+V
            for vk, down in self._translator.translate(kind, keycode, flags, button):
                self._on_key(vk, down, 0, flags)
        except Exception:  # never let an error change what happens to the event
            log.debug("hotkey callback failed", exc_info=True)
        return event

    def _run(self) -> None:
        api = self._api
        assert api is not None
        tap = None
        try:
            order = choose_listen_only(api.listen_allowed(), api.post_allowed())
            for listen_only in order:
                tap = api.create(listen_only, keys.event_mask(), self._callback_ref)
                if tap is not None:
                    self.listen_only = listen_only
                    break
            if tap is None:
                raise TapPermissionError(
                    "macOS didn't allow ShoutAbout to watch for the shortcut key. Turn on "
                    "ShoutAbout in System Settings > Privacy & Security > Accessibility."
                )
            self._tap = tap
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        log.info(
            "listening for the dictation hotkey (%s tap)",
            "listen-only" if self.listen_only else "Accessibility",
        )
        try:
            api.run(tap, self._stop, self._ready.set)
        except Exception as exc:
            if not self._ready.is_set():
                self._error = exc
            log.exception("the hotkey listener stopped")
        finally:
            self._ready.set()
            self._tap = None
            log.info("stopped listening for the dictation hotkey")


def _key_state_unknown(_vk: int) -> bool | None:
    """The physical key state isn't read on macOS (it would need Input Monitoring too);
    the machine relies on the events alone."""
    return None


class MacHotkeyListener(HotkeyListener):
    """:class:`HotkeyListener` with a Mac event tap and Mac key codes (``right_cmd``, ...).

    It reuses the Windows listener's worker thread and state machine unchanged: only the
    hook and the watched virtual key differ.
    """

    def __init__(
        self,
        hotkey: str,
        mode: Mode,
        on_event: Callable[..., None],
        *,
        tap_factory: Callable[[KeyCallback], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        vk = keys.hotkey_vk(hotkey)  # ValueError for a key a Mac doesn't offer
        super().__init__(
            next(iter(HOTKEY_VKS)),  # any Windows id; the machine's key is replaced below
            mode,
            on_event,
            hook_factory=tap_factory or MacEventTap,
            key_state=_key_state_unknown,
            clock=clock,
        )
        self.hotkey = hotkey
        self.machine.vk = vk
