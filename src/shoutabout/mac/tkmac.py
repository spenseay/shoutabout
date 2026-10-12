"""Make the shared Tk windows (the ShoutAbout window, setup, pairing prompt) right on macOS.

Installed only in ShoutAbout's own window process on a Mac (:mod:`shoutabout.mac.ui_child`),
never on Windows, so the shared window code stays unchanged:

* **Words**: the copy says "PC" and "Start with Windows"; :func:`mac_copy` rewrites every
  widget text and window title on its way to Tk ("Talk to this PC" -> "Talk to this Mac",
  "Start with Windows" -> "Open at login", "system tray" -> "menu bar", "sign in" ->
  "log in").
* **Size**: Tk on macOS counts 72 points per inch, Windows 96, so the same font sizes look a
  quarter smaller. ``tk scaling`` is set to 96/72 so the windows match the Windows design.
* **Buttons**: Aqua draws ``tk.Button`` natively and ignores its colours, which turned the
  brand's yellow-on-black buttons into yellow-on-white. :class:`LabelButton` is a flat,
  clickable label with the same options (``command``, ``state``, colours).
* **Dock icon**: Tk shows the window process in the Dock, and on macOS ``wm iconphoto``
  replaces the *Dock* icon with the photo it gets (the windows pass a 32 px one: a blurry
  tile). It is ignored here, so the Dock shows the app's own ShoutAbout.icns (16 to
  1024 px); run from source (no bundle icon) it shows the brand icon rendered at Dock size.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

TK_SCALING = 96.0 / 72.0

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bStart with Windows\b"), "Open at login"),
    (re.compile(r"\bstart with Windows\b"), "open at login"),
    (re.compile(r"\bsystem tray\b"), "menu bar"),
    (re.compile(r"\btray icon\b"), "menu bar icon"),
    (re.compile(r"\bthe tray\b"), "the menu bar"),
    (re.compile(r"\bsign in\b"), "log in"),
    (re.compile(r"\bPCs\b"), "Macs"),
    (re.compile(r"\bPC\b"), "Mac"),
    (re.compile(r"\bCtrl\+V\b"), "⌘V"),
)


def mac_copy(text: str) -> str:
    """The Mac wording of a piece of UI copy."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def _rewrite_options(options: tuple[Any, ...]) -> tuple[Any, ...]:
    out = list(options)
    for i in range(0, len(out) - 1, 2):
        if out[i] in ("-text", "-title") and isinstance(out[i + 1], str):
            out[i + 1] = mac_copy(out[i + 1])
    return tuple(out)


_installed = False


def install() -> None:
    """Patch tkinter for this process (idempotent)."""
    global _installed
    if _installed:
        return
    import tkinter as tk

    original_options = tk.Misc._options

    def _options(self: Any, cnf: Any, kw: Any = None) -> tuple[Any, ...]:
        return _rewrite_options(original_options(self, cnf, kw))

    tk.Misc._options = _options  # type: ignore[method-assign]

    original_title = tk.Wm.wm_title

    def wm_title(self: Any, string: Any = None) -> Any:
        if isinstance(string, str):
            string = mac_copy(string)
        return original_title(self, string)

    tk.Wm.wm_title = wm_title  # type: ignore[method-assign]
    tk.Wm.title = wm_title  # type: ignore[method-assign]

    original_init = tk.Tk.__init__

    def tk_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        try:
            self.tk.call("tk", "scaling", TK_SCALING)
        except Exception:
            log.debug("couldn't set tk scaling", exc_info=True)

    tk.Tk.__init__ = tk_init  # type: ignore[method-assign]
    tk.Wm.wm_iconphoto = keep_dock_icon  # type: ignore[method-assign]
    tk.Wm.iconphoto = keep_dock_icon  # type: ignore[method-assign]
    tk.Button = make_label_button(tk)  # type: ignore[misc]
    _installed = True


# ------------------------------------------------------------------ the Dock icon

DOCK_PX = 512
DOCK_TILE = 824 / 1024  # Apple's icon grid, as in packaging/macos/make_icns.py
_dock_done = False


def keep_dock_icon(_window: Any, _default: Any = False, *_images: Any) -> None:
    """``wm iconphoto`` on macOS: never the small photo (see the module docstring)."""
    global _dock_done
    if _dock_done:
        return
    _dock_done = True
    try:
        from Foundation import NSBundle

        if NSBundle.mainBundle().objectForInfoDictionaryKey_("CFBundleIconFile"):
            return  # the app: macOS already shows its .icns, sharp at every size
        from AppKit import NSApplication

        from shoutabout.mac.cocoa import nsimage_from_png

        png = dock_icon_png()
        image = nsimage_from_png(png, (DOCK_PX / 2, DOCK_PX / 2))
        NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        log.debug("couldn't set the Dock icon", exc_info=True)


def dock_icon_png(size: int = DOCK_PX) -> bytes:
    """The app icon (the rounded yellow tile on Apple's grid) as a ``size`` px PNG."""
    import numpy as np

    from shoutabout import brand

    tile = max(1, round(size * DOCK_TILE))
    art = brand.app_icon("primary", tile, rounded=True, samples=4)
    canvas = np.zeros((size, size, 4), dtype=np.uint8)
    off = (size - tile) // 2
    canvas[off : off + tile, off : off + tile] = np.frombuffer(art.rgba, dtype=np.uint8).reshape(
        tile, tile, 4
    )
    return brand.png_bytes(size, size, canvas.tobytes(), alpha=True)


def make_label_button(tk: Any) -> type:
    """A ``tk.Button`` look-alike built on ``tk.Label`` (Aqua ignores button colours)."""

    dropped = ("default", "overrelief", "repeatdelay", "repeatinterval", "compound_")

    class LabelButton(tk.Label):  # type: ignore[misc, name-defined]
        def __init__(self, master: Any = None, cnf: Any = None, **kw: Any) -> None:
            options = dict(cnf or {})
            options.update(kw)
            self._command: Callable[[], Any] | None = options.pop("command", None)
            for key in dropped:
                options.pop(key, None)
            super().__init__(master, options)
            self.bind("<ButtonRelease-1>", self._click, add="+")
            self.bind("<Enter>", lambda _e: self._hover(True), add="+")
            self.bind("<Leave>", lambda _e: self._hover(False), add="+")

        def _enabled(self) -> bool:
            return str(self.cget("state")) != "disabled"

        def _hover(self, inside: bool) -> None:
            if self._enabled():
                super().configure(state="active" if inside else "normal")

        def _click(self, _event: Any = None) -> None:
            if self._enabled() and self._command is not None:
                self._command()

        def invoke(self) -> Any:
            if self._enabled() and self._command is not None:
                return self._command()
            return None

        def configure(self, cnf: Any = None, **kw: Any) -> Any:
            options = dict(cnf or {})
            options.update(kw)
            if "command" in options:
                self._command = options.pop("command")
            for key in dropped:
                options.pop(key, None)
            return super().configure(options) if options else super().configure()

        config = configure

        def cget(self, key: str) -> Any:
            if key == "command":
                return self._command
            return super().cget(key)

        __getitem__ = cget

    LabelButton.__name__ = "Button"
    return LabelButton
