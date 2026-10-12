"""The first-run setup window (Tk): the view over :class:`~shoutabout.setup_flow.SetupFlow`.

Big type on Shout Yellow, very few words, one thing per screen. All logic, copy and
background work live in :mod:`setup_flow`; this module only draws it and forwards clicks.
It runs on the UI thread (:mod:`ui_host`) as a Toplevel, or as its own root for screenshots.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.setup_flow import PHONE_HOWTO, QR_NOTE, TRY_PHRASE, SetupFlow

log = logging.getLogger(__name__)

WINDOW_TITLE = "Set up ShoutAbout"
POLL_MS = 100
PERMISSION_TICKS = 10  # v2-macos: read the permissions again every 10 polls (~1 s)
BASE_W, BASE_H = 760, 540
ARM_DELAY_MS = 800


def _arm(button: Any) -> None:
    with contextlib.suppress(Exception):  # the screen may have moved on
        button.configure(state="normal")


def check_mark(parent: Any, on: bool, scale: float) -> Any:
    """A round mark for an "Allow access" line: an empty ring, or black with a yellow tick."""
    import tkinter as tk

    size = round(26 * scale)
    pad = max(1, round(2 * scale))
    canvas = tk.Canvas(parent, width=size, height=size, bg=parent["bg"], highlightthickness=0)
    box = (pad, pad, size - pad, size - pad)
    if not on:
        canvas.create_oval(*box, outline=kit.BLACK, width=max(2, round(2 * scale)))
        return canvas
    canvas.create_oval(*box, fill=kit.BLACK, width=0)
    tick = [size * x for x in (0.28, 0.52, 0.44, 0.68, 0.73, 0.35)]
    canvas.create_line(
        *tick, fill=kit.YELLOW, width=max(2, round(3 * scale)), capstyle="round", joinstyle="round"
    )
    return canvas


class SetupWindow:
    def __init__(
        self,
        window: Any,
        flow: SetupFlow,
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.win = window
        self.flow = flow
        self.on_close = on_close
        self.closed = False
        self.scale = kit.ui_scale(window)
        self.fonts = kit.Fonts(tkfont.families(window))
        self._seen_version = -1
        self._ticks = 0
        self._layout_key: Any = None
        self._mouth: kit.Mouth | None = None
        self._qr: Any = None
        self._icon: Any = None
        self._widgets: dict[str, Any] = {}

        window.title(WINDOW_TITLE)
        window.configure(bg=kit.YELLOW)
        w = min(round(BASE_W * self.scale), window.winfo_screenwidth() - 40)
        h = min(round(BASE_H * self.scale), window.winfo_screenheight() - 80)
        window.geometry(f"{w}x{h}")
        window.minsize(min(w, round(620 * self.scale)), min(h, round(480 * self.scale)))
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _e: self.close())
        try:  # cosmetic only
            self._icon = kit.app_icon_photo(window, 32)
            window.iconphoto(False, self._icon)
        except Exception:
            log.debug("couldn't set the window icon", exc_info=True)

        self.outer = tk.Frame(window, bg=kit.YELLOW)
        self.outer.pack(fill="both", expand=True, padx=round(36 * self.scale), pady=0)
        flow.start()
        self._poll()

    # ------------------------------------------------------------ plumbing

    def close(self, remember: bool = True) -> None:
        """Close the window. ``remember``: write the setup flag (False when the app quits
        under the window: setup then shows again next time)."""
        if self.closed:
            return
        self.closed = True
        try:
            self.flow.finish(remember=remember)
        finally:
            if self._mouth is not None:
                self._mouth.stop()
            with contextlib.suppress(Exception):
                self.win.destroy()
            if self.on_close is not None:
                self.on_close()

    def shutdown(self) -> None:
        """The app is quitting: close without marking setup as done."""
        self.close(remember=False)

    def bring_to_front(self) -> None:
        with contextlib.suppress(Exception):
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            if self._ticks % PERMISSION_TICKS == 0:
                self.flow.poll_permissions()  # only while it matters (a Mac, first screens)
            self._ticks += 1
            if self.flow.version != self._seen_version:
                self._seen_version = self.flow.version
                self.render()
        except Exception:
            log.exception("setup window update failed")
        self.win.after(POLL_MS, self._poll)

    # ------------------------------------------------------------- layout

    def _layout_key_now(self) -> tuple:
        f = self.flow
        if f.screen == "allow":
            rows = tuple((r.key, r.granted) for r in f.allow_rows())
            return ("allow", rows, f.allow_hint)
        if f.screen == "check":
            return ("check", f.phase == "failed")
        if f.screen == "try":
            return ("try", f.try_note())
        if f.screen == "done":
            return ("done", f.needs_access())
        if f.screen == "phone":
            return ("phone", f.prompt is not None, f.show_qr, f.pair_state)
        return (f.screen,)

    def render(self) -> None:
        key = self._layout_key_now()
        if key != self._layout_key:
            self._layout_key = key
            self._build()
        self._refresh()
        if self.flow.take_raise():
            self.bring_to_front()

    def _clear(self) -> None:
        if self._mouth is not None:
            self._mouth.stop()
            self._mouth = None
        for child in self.outer.winfo_children():
            child.destroy()
        self._widgets = {}

    def _header(self) -> None:
        import tkinter as tk

        f, fonts = self.flow, self.fonts
        top = tk.Frame(self.outer, bg=kit.YELLOW)
        top.pack(fill="x", pady=(round(22 * self.scale), 0))
        if self._icon is not None:
            tk.Label(top, image=self._icon, bg=kit.YELLOW).pack(side="left", padx=(0, 8))
        tk.Label(top, text="SHOUTABOUT", font=fonts.small_bold, bg=kit.YELLOW, fg=kit.BLACK).pack(
            side="left"
        )
        tk.Label(
            self.outer,
            text=f.title().upper(),
            font=fonts.display,
            bg=kit.YELLOW,
            fg=kit.BLACK,
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(round(14 * self.scale), round(4 * self.scale)))
        tk.Frame(self.outer, bg=kit.RED, height=round(5 * self.scale)).pack(
            fill="x", pady=(0, round(18 * self.scale))
        )

    def _footer(self) -> Any:
        import tkinter as tk

        foot = tk.Frame(self.outer, bg=kit.YELLOW)
        foot.pack(side="bottom", fill="x", pady=(0, round(24 * self.scale)))
        return foot

    def _line(self, parent: Any, text: str, font: Any, *, fg: str = kit.BLACK, **pack: Any) -> Any:
        import tkinter as tk

        label = tk.Label(
            parent,
            text=text,
            font=font,
            bg=parent["bg"],
            fg=fg,
            justify="left",
            anchor="w",
            wraplength=pack.pop("wrap", round(470 * self.scale)),
        )
        label.pack(fill="x", **pack)
        return label

    def _build(self) -> None:
        self._clear()
        self._header()
        getattr(self, f"_build_{self.flow.screen}")()

    # ---------------------------------------------- screen 0: allow access (macOS)

    def _build_allow(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        kit.link(foot, "Skip", self._skip_allow, fonts).pack(side="right")
        for row in f.allow_rows():
            line = tk.Frame(self.outer, bg=kit.YELLOW)
            line.pack(fill="x", pady=(0, round(16 * s)))
            check_mark(line, row.granted, s).pack(side="left", padx=(0, round(14 * s)))
            tk.Label(line, text=row.label, font=fonts.big, bg=kit.YELLOW, fg=kit.BLACK).pack(
                side="left"
            )
            tk.Label(line, text=row.why, font=fonts.body, bg=kit.YELLOW, fg=kit.MUTED).pack(
                side="left", padx=(round(8 * s), 0)
            )
            if not row.granted:
                kit.button(line, "Allow", lambda key=row.key: f.ask(key), fonts).pack(side="right")
            else:  # a blank as tall as the button, so the lines don't move when it goes
                tk.Label(line, text=" ", font=fonts.body_bold, bg=kit.YELLOW, pady=6).pack(
                    side="right"
                )
        if f.allow_hint:
            self._line(self.outer, f.allow_hint, fonts.body, fg=kit.MUTED, pady=(round(4 * s), 0))

    def _skip_allow(self) -> None:
        if self.flow.skip_allow():
            self.close()

    def _refresh_allow(self) -> None:
        return  # every change rebuilds the screen (see _layout_key_now)

    # ------------------------------------------------- screen 1: getting ready

    def _build_check(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        failed = f.phase == "failed"
        if failed:
            foot = self._footer()
            kit.button(foot, "Try again", f.retry, fonts, big=True).pack(side="right")
            kit.link(foot, "Skip for now", f.skip_download, fonts).pack(side="left")
        row = tk.Frame(self.outer, bg=kit.YELLOW)
        row.pack(fill="x")
        self._mouth = kit.Mouth(row, round(150 * s)).pack(side="left", padx=(0, round(24 * s)))
        text = tk.Frame(row, bg=kit.YELLOW)
        text.pack(side="left", fill="both", expand=True)
        self._widgets["status"] = self._line(
            text, "", fonts.big, pady=(round(18 * s), round(14 * s))
        )
        if failed:  # no bar: just what happened and the way out
            return
        bar = kit.ProgressBar(text, round(460 * s), round(16 * s))
        bar.canvas.pack(anchor="w")
        self._widgets["bar"] = bar
        self._widgets["size"] = self._line(
            text, "", fonts.body, fg=kit.MUTED, pady=(round(8 * s), 0)
        )

    def _refresh_check(self) -> None:
        f, w = self.flow, self._widgets
        w["status"].configure(text=f.status_line())
        if "bar" in w:
            w["bar"].set(f.progress_fraction())
            w["size"].configure(text=f.size_line())
        if self._mouth is not None:
            if f.phase in ("checking", "downloading"):
                self._mouth.talk()
            else:
                self._mouth.stop(open_mouth=f.phase == "ready")

    # ------------------------------------------------------- screen 2: try it

    def _build_try(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        kit.link(foot, "Skip", f.skip_try, fonts).pack(side="right")
        tk.Label(
            self.outer, text=f.try_instruction(), font=fonts.big, bg=kit.YELLOW, fg=kit.BLACK
        ).pack(anchor="w")
        tk.Label(
            self.outer,
            text=f"“{TRY_PHRASE}”",
            font=fonts.title,
            bg=kit.YELLOW,
            fg=kit.BLACK,
            anchor="w",
        ).pack(fill="x", pady=(round(6 * s), round(16 * s)))
        box = tk.Text(
            self.outer,
            height=4,
            font=fonts.entry,
            bg=kit.WHITE,
            fg=kit.BLACK,
            insertbackground=kit.BLACK,
            relief="flat",
            wrap="word",
            highlightthickness=round(3 * s),
            highlightbackground=kit.BLACK,
            highlightcolor=kit.RED,
            padx=round(12 * s),
            pady=round(10 * s),
        )
        box.pack(fill="x")
        box.bind("<<Modified>>", self._on_text_modified)
        self._widgets["box"] = box
        if f.try_note():  # v2-macos: pasting isn't allowed; the words are on the clipboard
            self._line(self.outer, f.try_note(), fonts.body, fg=kit.MUTED, pady=(round(10 * s), 0))
        self.win.after(50, lambda: self._focus_box())

    def _focus_box(self) -> None:
        box = self._widgets.get("box")
        if box is not None:
            with contextlib.suppress(Exception):
                box.focus_set()

    def _on_text_modified(self, _event: Any = None) -> None:
        box = self._widgets.get("box")
        if box is None:
            return
        try:
            text = box.get("1.0", "end")
            box.edit_modified(False)
        except Exception:
            return
        self.flow.text_changed(text)

    def _refresh_try(self) -> None:
        return

    # ------------------------------------------------------ screen 3: done

    def _build_done(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        kit.button(foot, "Done", self.close, fonts, big=True).pack(side="right")
        kit.link(foot, "Connect your iPhone", f.connect_phone, fonts).pack(side="left")
        row = tk.Frame(self.outer, bg=kit.YELLOW)
        row.pack(fill="x")
        self._mouth = kit.Mouth(row, round(150 * s)).pack(side="left", padx=(0, round(24 * s)))
        text = tk.Frame(row, bg=kit.YELLOW)
        text.pack(side="left", fill="both", expand=True)
        if f.done_line():
            self._line(text, f.done_line(), fonts.big, pady=(round(18 * s), 0))
        if f.needs_access():  # v2-macos: skipped "Allow access"; the way back
            kit.link(text, "Allow access", f.back_to_allow, fonts).pack(
                anchor="w", pady=(round(10 * s), 0)
            )
        self._mouth.talk()
        self.win.after(1600, lambda: self._mouth and self._mouth.stop(open_mouth=True))

    def _refresh_done(self) -> None:
        return

    # ------------------------------------------------- screen 4: the iPhone

    def _build_phone(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        kit.button(foot, "Done", self.close, fonts, big=True).pack(side="right")
        card = tk.Frame(
            self.outer,
            bg=kit.PAPER,
            highlightthickness=round(3 * s),
            highlightbackground=kit.BLACK,
            padx=round(22 * s),
            pady=round(18 * s),
        )
        card.pack(fill="x", pady=(0, round(20 * s)))
        wrap = round(600 * s)
        if f.prompt is not None:  # inline approval
            self._line(card, f.phone_line(), fonts.big, wrap=wrap, pady=(0, round(8 * s)))
            tk.Label(card, text="CODE", font=fonts.small_bold, bg=kit.PAPER, fg=kit.MUTED).pack(
                anchor="w"
            )
            tk.Label(card, text=f.prompt.code, font=fonts.code, bg=kit.PAPER, fg=kit.BLACK).pack(
                anchor="w"
            )
            self._line(
                card,
                "Allow it if your iPhone shows the same code.",
                fonts.small,
                fg=kit.MUTED,
                wrap=wrap,
                pady=(0, round(10 * s)),
            )
            row = tk.Frame(card, bg=kit.PAPER)
            row.pack(fill="x")
            allow = kit.button(row, "Allow", f.allow, fonts)
            # Armed after a moment, so a click aimed at what was there before can't land on it.
            allow.configure(state="disabled", takefocus=0)
            allow.pack(side="left")
            self.win.after(ARM_DELAY_MS, lambda: _arm(allow))
            kit.button(row, "Don't allow", f.deny, fonts, "light").pack(
                side="left", padx=(round(8 * s), 0)
            )
            return
        if f.show_qr:
            if f.qr_text:
                try:
                    self._qr = kit.qr_photo(self.win, f.qr_text, scale=max(3, round(3 * s)))
                    tk.Label(card, image=self._qr, bg=kit.WHITE).pack(anchor="w")
                except Exception:
                    log.exception("couldn't draw the QR code")
            self._line(card, f.qr_error or QR_NOTE, fonts.small, wrap=wrap, pady=(round(8 * s), 0))
            kit.link(card, "Hide the code", f.toggle_qr, fonts, kit.PAPER).pack(
                anchor="w", pady=(round(6 * s), 0)
            )
            return
        self._line(card, PHONE_HOWTO, fonts.big, wrap=wrap, pady=(0, round(12 * s)))
        row = tk.Frame(card, bg=kit.PAPER)
        row.pack(fill="x")
        if f.pair_state in ("waiting", "denied", "expired"):
            self._mouth = kit.Mouth(row, round(36 * s), bg=kit.PAPER).pack(side="left", padx=(0, 8))
        tk.Label(
            row,
            text=f.phone_line(),
            font=fonts.body_bold,
            bg=kit.PAPER,
            fg=kit.BLACK,
            justify="left",
            anchor="w",
            wraplength=wrap - round(50 * s),
        ).pack(side="left", fill="x")
        if f.pair_state != "paired":
            kit.link(card, "Scan a code instead", f.toggle_qr, fonts, kit.PAPER).pack(
                anchor="w", pady=(round(14 * s), 0)
            )

    def _refresh_phone(self) -> None:
        if self._mouth is not None:
            if self.flow.pair_state == "waiting":
                self._mouth.talk()
            else:
                self._mouth.stop()

    def _refresh(self) -> None:
        getattr(self, f"_refresh_{self.flow.screen}")()
