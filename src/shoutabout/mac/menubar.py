"""The menu bar icon and menu (macOS): the Mac counterpart of :mod:`shoutabout.tray`.

Same interface as :class:`shoutabout.tray.TrayIcon`, so the hub uses it unchanged:
``run()`` (on the main thread: runs the Cocoa event loop until ``stop()``), ``stop()``
(any thread) and ``notify(title, message)`` (any thread; a macOS notification).

The app is an "accessory" (``LSUIElement``): no Dock icon, no app menu, just the brand
mouth in the menu bar. The menu is :func:`shoutabout.tray.build_menu` (the same items and
words as Windows: status, how to talk, Pause/Resume, Recent…, Teach words…, Settings…, Set up…,
Quit), plus, while a permission is missing, one line that opens the right System Settings
pane. It is rebuilt each time it opens. Menu commands other than Quit run on a short-lived
thread, so a slow action never freezes the menu bar.

The icon is the tray tile of :mod:`shoutabout.brand` at 36 px shown at 18 points (sharp on
Retina), with the same states and animations as the Windows tray.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from shoutabout import brand, icons
from shoutabout.state import HubState
from shoutabout.tray import (
    CMD_PAUSE,
    CMD_QUIT,
    CMD_RECENT,
    CMD_SETTINGS,
    CMD_SETUP,
    CMD_TEACH,
    TrayActions,
    build_menu,
    tooltip_text,
)

log = logging.getLogger(__name__)

ICON_PX = 36
ICON_PT = 18.0
ACCESSORY_POLICY = 1  # NSApplicationActivationPolicyAccessory
VARIABLE_LENGTH = -1.0  # NSVariableStatusItemLength
SIGNAL_POLL_S = 0.5
EXTRA_BASE = 100

ExtraItems = Callable[[], list[tuple[int, str]]]

_CONTROLLER: Any = None


def _controller_class() -> Any:
    """The Objective-C target for menu actions (defined once per process)."""
    global _CONTROLLER
    if _CONTROLLER is None:
        import objc
        from Foundation import NSObject

        try:
            protocols = [objc.protocolNamed("NSMenuDelegate")]
        except Exception:
            protocols = []

        class ShoutAboutMenuController(NSObject, protocols=protocols):  # type: ignore[call-arg]
            def menuNeedsUpdate_(self, menu: Any) -> None:
                owner = getattr(self, "owner", None)
                if owner is not None:
                    owner._rebuild(menu)

            def menuAction_(self, sender: Any) -> None:
                owner = getattr(self, "owner", None)
                if owner is not None:
                    owner._dispatch(int(sender.tag()))

        _CONTROLLER = ShoutAboutMenuController
    return _CONTROLLER


def menu_entries(
    state: HubState,
    actions: TrayActions,
    hint: str | None,
    extra: list[tuple[int, str]],
    title: str = "ShoutAbout",
) -> list[tuple[int, str, bool, bool]]:
    """(id, label, enabled, separator) rows: the tray menu, plus permission lines after the
    status lines. Pure, for tests."""
    items = build_menu(
        title,
        state.status,
        state.paused,
        state.error,
        hint=hint,
        percent=state.progress,
        recent=actions.recent is not None,
        teaching=actions.teach is not None,
        settings=actions.settings is not None,
        setup=actions.setup is not None,
    )
    rows = [(i.id, i.label.replace("&&", "&"), i.enabled, i.separator) for i in items]
    if extra:
        info = 0
        while info < len(rows) and not rows[info][3] and not rows[info][2]:
            info += 1  # after the greyed status/hint lines
        rows[info:info] = [(EXTRA_BASE + n, label, True, False) for n, label in extra]
    return rows


class MacMenuBar:
    def __init__(
        self,
        state: HubState,
        actions: TrayActions,
        *,
        extra_items: ExtraItems | None = None,
        on_extra: Callable[[int], None] | None = None,
        title: str = "ShoutAbout",
    ) -> None:
        from AppKit import NSApplication

        self._state = state
        self._actions = actions
        self._extra_items = extra_items
        self._on_extra = on_extra
        self._title = title
        self.app = NSApplication.sharedApplication()
        self.app.setActivationPolicy_(ACCESSORY_POLICY)
        self._item: Any = None
        self._controller: Any = None
        self._images: dict[str, list[Any]] = {}
        self._key = ""
        self._frame = 0
        self._timer: Any = None
        self._pump: Any = None
        self._stopped = threading.Event()
        state.subscribe(self._on_state_change)

    # ---------------------------------------------------------------- any thread

    def notify(self, title: str, message: str) -> None:
        log.info("notification: %s: %s", title, message)
        from shoutabout.mac.cocoa import call_on_main

        call_on_main(self._post_notification, title, message)

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        from shoutabout.mac.cocoa import call_on_main

        def stop_loop() -> None:
            from PyObjCTools import AppHelper

            AppHelper.stopEventLoop()

        call_on_main(stop_loop)

    def _on_state_change(self, _state: HubState) -> None:
        from shoutabout.mac.cocoa import call_on_main

        call_on_main(self._refresh)

    # ---------------------------------------------------------------- main thread

    def run(self) -> None:
        from PyObjCTools import AppHelper

        # Neither may stop the event loop from running: without it nothing on the main
        # thread works (the overlay, notifications), and dictation is still useful.
        try:
            self._create_item()
        except Exception:
            log.exception("couldn't create the menu bar icon")
        try:
            self._start_signal_pump()
        except Exception:
            log.exception("couldn't start the signal timer")

        def keep_going() -> bool:
            log.exception("unexpected error in the Cocoa event loop; continuing")
            return True

        try:
            AppHelper.runEventLoop(
                installInterrupt=bool(sys.stdin is not None and sys.stdin.isatty()),
                unexpectedErrorAlert=keep_going,
            )
        finally:
            self._stop_timer()
            if self._pump is not None:
                self._pump.invalidate()
            if self._item is not None:
                from AppKit import NSStatusBar

                NSStatusBar.systemStatusBar().removeStatusItem_(self._item)
                self._item = None

    def _start_signal_pump(self) -> None:
        """Python runs signal handlers (SIGTERM at logout) only between bytecodes on the main
        thread; a light repeating timer gives it the chance while Cocoa waits for events."""
        from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

        timer = NSTimer.timerWithTimeInterval_repeats_block_(SIGNAL_POLL_S, True, lambda _t: None)
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self._pump = timer

    def _create_item(self) -> None:
        from AppKit import NSMenu, NSStatusBar

        controller = _controller_class().alloc().init()
        controller.owner = self
        self._controller = controller
        item = NSStatusBar.systemStatusBar().statusItemWithLength_(VARIABLE_LENGTH)
        menu = NSMenu.alloc().initWithTitle_(self._title)
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(controller)
        item.setMenu_(menu)
        self._item = item
        self._rebuild(menu)
        self._refresh()

    def _frames(self, key: str) -> list[Any]:
        frames = self._images.get(key)
        if frames is None:
            from shoutabout.mac.cocoa import nsimage_from_png

            frames = []
            for img in brand.tray_frames(key, ICON_PX):
                png = brand.png_bytes(img.width, img.height, img.rgba, alpha=True)
                frames.append(nsimage_from_png(png, (ICON_PT, ICON_PT)))
            self._images[key] = frames
        return frames

    def _refresh(self) -> None:
        item = self._item
        if item is None:
            return
        state = self._state
        key = icons.icon_key(state.status, state.paused)
        button = item.button()
        if key != self._key:
            self._key, self._frame = key, 0
            frames = self._frames(key)
            if frames and button is not None:
                button.setImage_(frames[0])
            self._sync_animation()
        if button is not None:
            tip = tooltip_text(self._title, state.status, state.paused, state.error, state.progress)
            button.setToolTip_(tip)

    def _sync_animation(self) -> None:
        interval = icons.frame_interval_ms(self._key)
        self._stop_timer()
        if interval <= 0:
            return
        from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

        def next_frame(_timer: Any) -> None:
            try:
                frames = self._frames(self._key)
                if not frames or self._item is None:
                    return
                self._frame = (self._frame + 1) % len(frames)
                button = self._item.button()
                if button is not None:
                    button.setImage_(frames[self._frame])
            except Exception:
                log.exception("menu bar animation failed")

        timer = NSTimer.timerWithTimeInterval_repeats_block_(interval / 1000.0, True, next_frame)
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self._timer = timer

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _hint(self) -> str | None:
        if self._actions.hint is None:
            return None
        try:
            hint = self._actions.hint()
        except Exception:
            log.exception("couldn't read the dictation hint")
            return None
        return str(hint) if hint else None

    def _extra(self) -> list[tuple[int, str]]:
        if self._extra_items is None:
            return []
        try:
            return list(self._extra_items())
        except Exception:
            log.exception("couldn't read the permission menu items")
            return []

    def _rebuild(self, menu: Any) -> None:
        from AppKit import NSMenuItem

        try:
            rows = menu_entries(self._state, self._actions, self._hint(), self._extra())
        except Exception:
            log.exception("couldn't build the menu")
            rows = [(CMD_QUIT, "Quit", True, False)]
        menu.removeAllItems()
        for cmd, label, enabled, separator in rows:
            if separator:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, "menuAction:", "")
            entry.setTarget_(self._controller)
            entry.setTag_(cmd)
            entry.setEnabled_(bool(enabled))
            menu.addItem_(entry)

    def _dispatch(self, cmd: int) -> None:
        if cmd == CMD_QUIT:
            try:
                self._actions.quit()
            except Exception:
                log.exception("menu action 'quit' failed")
            finally:
                self.stop()
            return
        if cmd >= EXTRA_BASE:
            fn: Callable[[], None] | None = None
            if self._on_extra is not None:
                on_extra = self._on_extra
                fn = lambda: on_extra(cmd - EXTRA_BASE)  # noqa: E731
            name = "permission"
        else:
            table = {
                CMD_PAUSE: ("toggle_pause", self._actions.toggle_pause),
                CMD_RECENT: ("recent", self._actions.recent),
                CMD_TEACH: ("teach", self._actions.teach),
                CMD_SETTINGS: ("settings", self._actions.settings),
                CMD_SETUP: ("setup", self._actions.setup),
            }
            name, fn = table.get(cmd, ("", None))
        if fn is None:
            return
        action = fn

        def call() -> None:
            try:
                action()
            except Exception:
                log.exception("menu action %r failed", name)

        threading.Thread(target=call, name=f"menu-{name}", daemon=True).start()

    def _post_notification(self, title: str, message: str) -> None:
        try:
            from Foundation import NSUserNotification, NSUserNotificationCenter

            center = NSUserNotificationCenter.defaultUserNotificationCenter()
        except Exception:
            center = None
        if center is not None:
            note = NSUserNotification.alloc().init()
            note.setTitle_(title)
            note.setInformativeText_(message)
            center.deliverNotification_(note)
            return
        _osascript_notification(title, message)


def applescript_string(text: str) -> str:
    """``text`` as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript_notification(title: str, message: str) -> None:
    script = (
        f"display notification {applescript_string(message)} with title {applescript_string(title)}"
    )
    try:
        subprocess.Popen(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        log.debug("couldn't show a notification", exc_info=True)
