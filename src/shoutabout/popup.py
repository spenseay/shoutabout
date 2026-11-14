"""A short message at the top of the screen, e.g. "Not pasted · Ctrl+V to paste".

The injector calls :meth:`Popup.show` when a dictation couldn't be pasted (no text box had
the focus) and was left on the clipboard. That happens for phone dictations too, while
the mouth pill (:mod:`shoutabout.overlay`) normally belongs to "Talk to this PC", so:

1. "Talk to this PC" is on and its pill is free: the message goes on that pill;
2. else ShoutAbout's own pill, made the first time it's needed (no pre-rendering: only the
   text frame is drawn), on Windows and macOS when the pill isn't turned off
   (``[dictation] overlay = false``);
3. else (pill turned off, not Windows/macOS, no window, you're recording right now): a
   notification.

The pill never takes the focus, so it can't get in the way of the next paste. Never raises.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

from shoutabout.inject.base import NOT_PASTED_HOLD_S

log = logging.getLogger(__name__)

TITLE = "ShoutAbout"


def default_overlay_factory() -> Any:
    """A pill for messages only: the platform's window, without pre-rendered frames."""
    if sys.platform == "darwin":
        from shoutabout.mac.overlay_mac import create_mac_overlay

        return create_mac_overlay(True)
    if sys.platform == "win32":
        from shoutabout.overlay import OverlayWindow

        return OverlayWindow(prerender_frames=False)
    from shoutabout.overlay import NullOverlay

    return NullOverlay()


class Popup:
    def __init__(
        self,
        *,
        notify: Callable[[str, str], Any],
        dictation: Callable[[], Any] | None = None,
        overlay_enabled: Callable[[], bool] = lambda: True,
        overlay_factory: Callable[[], Any] = default_overlay_factory,
    ) -> None:
        self._notify = notify
        self._dictation = dictation or (lambda: None)
        self._overlay_enabled = overlay_enabled
        self._factory = overlay_factory
        self._lock = threading.Lock()
        self._overlay: Any = None
        self._broken = False  # our own pill couldn't be made: notifications from now on

    def show(self, text: str, hold: float | None = NOT_PASTED_HOLD_S) -> str:
        """Show ``text``. Returns where: "dictation", "overlay" or "notification"."""
        try:
            dictation = self._dictation()
            if dictation is not None and dictation.popup(text, hold=hold):
                return "dictation"
        except Exception:
            log.debug("the dictation pill couldn't show the message", exc_info=True)
        try:
            overlay = self._own_overlay()
            if overlay is not None:
                overlay.notice(text, hold=hold)
                return "overlay"
        except Exception:
            log.exception("the pill couldn't show the message")
        try:
            self._notify(TITLE, text)
        except Exception:
            log.exception("couldn't show the notification")
        return "notification"

    def _own_overlay(self) -> Any:
        try:
            enabled = bool(self._overlay_enabled())
        except Exception:
            enabled = True
        if not enabled:
            return None
        with self._lock:
            if self._overlay is None and not self._broken:
                overlay = self._factory()
                try:
                    overlay.start()
                except Exception:
                    log.exception("couldn't start the pill for messages")
                    self._broken = True
                    return None
                if not getattr(overlay, "shows", True):
                    self._broken = True
                    _safe_stop(overlay)
                    return None
                self._overlay = overlay
            return self._overlay

    def stop(self) -> None:
        with self._lock:
            overlay, self._overlay = self._overlay, None
        if overlay is not None:
            _safe_stop(overlay)


def _safe_stop(overlay: Any) -> None:
    try:
        overlay.stop()
    except Exception:
        log.debug("couldn't stop the pill", exc_info=True)
