"""The pairing approval prompt: "<device> wants to dictate to this PC. Code 4821."

A small branded window, always on top, in the bottom-right corner where notifications
appear. It closes by itself when the request expires or is answered somewhere else.

It is careful not to be clicked or typed "yes" by accident, because anyone on the Wi-Fi can
make it appear:

* it doesn't take the keyboard focus (``WS_EX_NOACTIVATE``), so typing elsewhere can't
  answer it, and no key approves (there's no default button);
* Allow is disabled for the first :data:`ARM_DELAY_MS`, so a click that was meant for the
  window underneath can't land on it;
* closing it (X) means Don't allow.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable
from typing import Any

from shoutabout import ui_kit as kit

log = logging.getLogger(__name__)

WINDOW_TITLE = "ShoutAbout"
ARM_DELAY_MS = 1000
TICK_MS = 500
BASE_W, BASE_H = 440, 250


def countdown_text(seconds_left: float) -> str:
    s = max(0, int(seconds_left + 0.999))
    return f"Closes in {s // 60}:{s % 60:02d}"


def prompt_message(device_name: str) -> str:
    return f"{device_name} wants to dictate to this PC."


def _no_activate(window: Any) -> None:
    """Show without stealing focus (Windows: WS_EX_NOACTIVATE on the frame)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        hwnd = int(window.wm_frame(), 16)
        user32 = ctypes.windll.user32
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOPMOST = -20, 0x08000000, 0x00000008
        get = user32.GetWindowLongPtrW
        get.restype = ctypes.c_ssize_t
        get.argtypes = [ctypes.c_void_p, ctypes.c_int]
        put = user32.SetWindowLongPtrW
        put.restype = ctypes.c_ssize_t
        put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        put(hwnd, GWL_EXSTYLE, get(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOPMOST)


class ApprovalPrompt:
    def __init__(
        self,
        window: Any,
        prompt: Any,
        *,
        approve: Callable[[str], bool],
        deny: Callable[[str], bool],
        seconds_left: Callable[[Any], float],
        on_close: Callable[[], None] | None = None,
        arm_delay_ms: int = ARM_DELAY_MS,
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.win = window
        self.prompt = prompt
        self._approve, self._deny, self._left = approve, deny, seconds_left
        self.on_close = on_close
        self.closed = False
        self.result: str | None = None  # "allowed" | "denied" | "expired" | "error"
        s = kit.ui_scale(window)
        fonts = self.fonts = kit.Fonts(tkfont.families(window))

        window.title(WINDOW_TITLE)
        window.configure(bg=kit.YELLOW)
        window.resizable(False, False)
        window.attributes("-topmost", True)
        window.protocol("WM_DELETE_WINDOW", self.dont_allow)
        w, h = round(BASE_W * s), round(BASE_H * s)
        x = max(0, window.winfo_screenwidth() - w - round(24 * s))
        y = max(0, window.winfo_screenheight() - h - round(72 * s))
        window.geometry(f"{w}x{h}+{x}+{y}")
        self._icon: Any = None
        with contextlib.suppress(Exception):
            self._icon = kit.set_window_icon(window)

        frame = tk.Frame(window, bg=kit.YELLOW, padx=round(20 * s), pady=round(16 * s))
        frame.pack(fill="both", expand=True)
        top = tk.Frame(frame, bg=kit.YELLOW)
        top.pack(fill="x")
        self.mouth = kit.Mouth(top, round(54 * s)).pack(side="left", padx=(0, round(12 * s)))
        tk.Label(
            top,
            text=prompt_message(prompt.device_name),
            font=fonts.big,
            bg=kit.YELLOW,
            fg=kit.BLACK,
            justify="left",
            anchor="w",
            wraplength=round(320 * s),
        ).pack(side="left", fill="x", expand=True)
        code_row = tk.Frame(frame, bg=kit.YELLOW)
        code_row.pack(fill="x", pady=(round(10 * s), 0))
        tk.Label(code_row, text="CODE", font=fonts.small_bold, bg=kit.YELLOW, fg=kit.MUTED).pack(
            side="left", anchor="s", pady=(0, round(10 * s))
        )
        tk.Label(code_row, text=prompt.code, font=fonts.code, bg=kit.YELLOW, fg=kit.BLACK).pack(
            side="left", padx=(round(10 * s), 0)
        )
        tk.Label(
            code_row,
            text="Same code\non your iPhone?",
            font=fonts.small,
            bg=kit.YELLOW,
            fg=kit.MUTED,
            justify="right",
        ).pack(side="right")
        buttons = tk.Frame(frame, bg=kit.YELLOW)
        buttons.pack(fill="x", side="bottom")
        self.allow_button = kit.button(buttons, "Allow", self.allow, fonts)
        self.allow_button.configure(state="disabled", takefocus=0)
        self.allow_button.pack(side="left")
        deny_button = kit.button(buttons, "Don't allow", self.dont_allow, fonts, "light")
        deny_button.configure(takefocus=0)
        deny_button.pack(side="left", padx=(round(8 * s), 0))
        self.countdown = tk.Label(
            buttons, text="", font=fonts.small, bg=kit.YELLOW, fg=kit.MUTED, anchor="e"
        )
        self.countdown.pack(side="right")

        _no_activate(window)
        self.mouth.talk()
        window.after(arm_delay_ms, self._arm)
        self._tick()

    def _arm(self) -> None:
        if not self.closed:
            with contextlib.suppress(Exception):
                self.allow_button.configure(state="normal")

    def _tick(self) -> None:
        if self.closed:
            return
        left = self._left(self.prompt)
        if left <= 0:
            self.result = "expired"
            self.close()
            return
        self.countdown.configure(text=countdown_text(left))
        self.win.after(TICK_MS, self._tick)

    def allow(self) -> None:
        if self.closed or str(self.allow_button["state"]) == "disabled":
            return
        try:
            ok = self._approve(self.prompt.request_id)
            self.result = "allowed" if ok else "expired"
        except Exception:
            log.exception("couldn't approve the phone")
            self.result = "error"
        self.close()

    def dont_allow(self) -> None:
        if self.closed:
            return
        try:
            self._deny(self.prompt.request_id)
        except Exception:
            log.exception("couldn't deny the phone")
        self.result = "denied"
        self.close()

    def close(self) -> None:
        """Close without answering (expired, or answered elsewhere)."""
        if self.closed:
            return
        self.closed = True
        self.mouth.stop()
        with contextlib.suppress(Exception):
            self.win.destroy()
        if self.on_close is not None:
            self.on_close()
