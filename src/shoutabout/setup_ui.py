"""The first-run setup window (Tk): the view over :class:`~shoutabout.setup_flow.SetupFlow`.

Big friendly type on Shout Yellow, very few words, one decision per screen. All logic,
copy and background work live in :mod:`setup_flow`; this module only draws it and forwards
clicks. It runs on the UI thread (:mod:`ui_host`) as a Toplevel, or as its own root for
screenshots.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.setup_flow import TRY_PHRASE, SetupFlow

log = logging.getLogger(__name__)

WINDOW_TITLE = "Set up ShoutAbout"
POLL_MS = 100
BASE_W, BASE_H = 780, 600
ARM_DELAY_MS = 800


def _arm(button: Any) -> None:
    with contextlib.suppress(Exception):  # the screen may have moved on
        button.configure(state="normal")


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
        window.minsize(min(w, round(640 * self.scale)), min(h, round(520 * self.scale)))
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
            if self.flow.version != self._seen_version:
                self._seen_version = self.flow.version
                self.render()
        except Exception:
            log.exception("setup window update failed")
        self.win.after(POLL_MS, self._poll)

    # ------------------------------------------------------------- layout

    def _layout_key_now(self) -> tuple:
        f = self.flow
        if f.screen == "check":
            return ("check", f.phase in ("ready", "unknown", "failed"), f.phase == "failed")
        if f.screen == "use":
            return ("use", f.pc_on, f.phone_on, f.prompt is not None, f.show_qr, f.pair_state)
        return (f.screen,)

    def render(self) -> None:
        key = self._layout_key_now()
        if key != self._layout_key:
            self._layout_key = key
            self._build()
        self._refresh()

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
        tk.Label(top, text=f.step_label(), font=fonts.small_bold, bg=kit.YELLOW, fg=kit.MUTED).pack(
            side="right"
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

    def _build(self) -> None:
        self._clear()
        self._header()
        getattr(self, f"_build_{self.flow.screen}")()

    # --------------------------------------------------------- screen 1

    def _build_check(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        body = tk.Frame(self.outer, bg=kit.YELLOW)
        body.pack(fill="both", expand=True)
        row = tk.Frame(body, bg=kit.YELLOW)
        row.pack(fill="x", expand=True)
        self._mouth = kit.Mouth(row, round(150 * s)).pack(side="left", padx=(0, round(24 * s)))
        text = tk.Frame(row, bg=kit.YELLOW)
        text.pack(side="left", fill="both", expand=True)
        self._widgets["hw"] = tk.Label(
            text,
            text="",
            font=fonts.big,
            bg=kit.YELLOW,
            fg=kit.BLACK,
            justify="left",
            anchor="w",
            wraplength=round(470 * s),
        )
        self._widgets["hw"].pack(fill="x", pady=(round(8 * s), round(18 * s)))
        bar = kit.ProgressBar(text, round(460 * s), round(16 * s))
        bar.canvas.pack(anchor="w")
        self._widgets["bar"] = bar
        self._widgets["status"] = tk.Label(
            text, text="", font=fonts.body, bg=kit.YELLOW, fg=kit.BLACK, anchor="w"
        )
        self._widgets["status"].pack(fill="x", pady=(round(8 * s), 0))

        nxt = kit.button(foot, "Next", f.next, fonts, big=True)
        nxt.pack(side="right")
        self._widgets["next"] = nxt
        if f.phase == "failed":
            kit.button(foot, "Try again", f.retry, fonts, "light", big=True).pack(
                side="right", padx=(0, round(10 * s))
            )
            kit.link(foot, "Skip for now", f.skip_download, fonts).pack(side="left")
        tk.Label(
            body,
            text="Everything runs on this PC. Your voice never leaves it.",
            font=fonts.small,
            bg=kit.YELLOW,
            fg=kit.MUTED,
            anchor="w",
        ).pack(side="bottom", fill="x", pady=(0, round(12 * s)))

    def _refresh_check(self) -> None:
        f = self.flow
        w = self._widgets
        w["hw"].configure(text=f.hardware_text or "Checking this PC…")
        w["bar"].set(f.progress_fraction())
        w["status"].configure(text=f.status_line())
        w["next"].configure(state="normal" if f.can_continue() else "disabled")
        if self._mouth is not None:
            if f.phase in ("checking", "downloading"):
                self._mouth.talk()
            else:
                self._mouth.stop(open_mouth=f.phase == "ready")

    # --------------------------------------------------------- screen 2

    def _card(self, parent: Any, title: str, on: bool, toggle: Callable[[], None]) -> Any:
        import tkinter as tk

        s, fonts = self.scale, self.fonts
        bg = kit.PAPER if on else kit.CARD_OFF
        card = tk.Frame(
            parent,
            bg=bg,
            highlightthickness=round(3 * s) if on else round(2 * s),
            highlightbackground=kit.BLACK if on else kit.GREY,
            padx=round(18 * s),
            pady=round(16 * s),
            cursor="hand2",
        )
        head = tk.Frame(card, bg=bg)
        head.pack(fill="x")
        label = tk.Label(head, text=title, font=fonts.subtitle, bg=bg, fg=kit.BLACK, anchor="w")
        label.pack(side="left")
        switch = kit.Switch(head, s, bg, on)
        switch.canvas.pack(side="right")
        for widget in (card, head, label, switch.canvas):
            widget.bind("<Button-1>", lambda _e: toggle())
        return card

    def _build_use(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        if f.mode == "phone":
            kit.button(foot, "Done", self.close, fonts, big=True).pack(side="right")
        else:
            nxt = kit.button(foot, "Next", f.next, fonts, big=True)
            nxt.pack(side="right")
            self._widgets["next"] = nxt
            kit.link(foot, "Back", f.back, fonts).pack(side="left")
        if f.save_error:
            tk.Label(foot, text=f.save_error, font=fonts.small_bold, bg=kit.RED, fg=kit.BLACK).pack(
                side="left", padx=round(12 * s)
            )

        cards = tk.Frame(self.outer, bg=kit.YELLOW)
        cards.pack(fill="both", expand=True, pady=(0, round(20 * s)))
        cards.columnconfigure(0, weight=1, uniform="cards")
        cards.columnconfigure(1, weight=1, uniform="cards")
        cards.rowconfigure(0, weight=1)
        wrap = round(270 * s)

        if f.mode != "phone":
            pc = self._card(cards, "Talk to this PC", f.pc_on, f.toggle_pc)
            pc.grid(row=0, column=0, sticky="nsew", padx=(0, round(10 * s)))
            bg = pc["bg"]
            tk.Label(
                pc,
                text=f.pc_card_text(),
                font=fonts.big if f.pc_on else fonts.body,
                bg=bg,
                fg=kit.BLACK if f.pc_on or f.no_mic else kit.MUTED,
                justify="left",
                anchor="w",
                wraplength=wrap,
            ).pack(fill="x", pady=(round(14 * s), 0))
            if f.pc_card_note():
                tk.Label(
                    pc,
                    text=f.pc_card_note(),
                    font=fonts.body,
                    bg=bg,
                    fg=kit.MUTED,
                    justify="left",
                    anchor="w",
                    wraplength=wrap,
                ).pack(fill="x", pady=(round(8 * s), 0))

        phone = self._card(cards, "Talk from my iPhone", f.phone_on, f.toggle_phone)
        col = 1 if f.mode != "phone" else 0
        span = 1 if f.mode != "phone" else 2
        phone.grid(row=0, column=col, columnspan=span, sticky="nsew")
        self._build_phone_card(phone, wrap if f.mode != "phone" else round(560 * s))

    def _build_phone_card(self, card: Any, wrap: int) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        bg = card["bg"]
        if not f.phone_on:
            tk.Label(
                card,
                text="Off. Turn it on to use your iPhone as the microphone.",
                font=fonts.body,
                bg=bg,
                fg=kit.MUTED,
                justify="left",
                anchor="w",
                wraplength=wrap,
            ).pack(fill="x", pady=(round(14 * s), 0))
            return
        if f.prompt is not None:  # inline approval
            tk.Label(
                card,
                text=f.phone_line(),
                font=fonts.big,
                bg=bg,
                fg=kit.BLACK,
                justify="left",
                anchor="w",
                wraplength=wrap,
            ).pack(fill="x", pady=(round(14 * s), round(6 * s)))
            tk.Label(card, text="CODE", font=fonts.small_bold, bg=bg, fg=kit.MUTED).pack(anchor="w")
            tk.Label(card, text=f.prompt.code, font=fonts.code, bg=bg, fg=kit.BLACK).pack(
                anchor="w"
            )
            tk.Label(
                card,
                text="Allow it if your iPhone shows the same code.",
                font=fonts.small,
                bg=bg,
                fg=kit.MUTED,
                anchor="w",
            ).pack(fill="x", pady=(0, round(10 * s)))
            row = tk.Frame(card, bg=bg)
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
                    tk.Label(card, image=self._qr, bg=kit.WHITE).pack(pady=(round(10 * s), 0))
                except Exception:
                    log.exception("couldn't draw the QR code")
            tk.Label(
                card,
                text=f.qr_error
                or "Scan it in the ShoutAbout app. It's a key to this PC: "
                "show it only to your own phone.",
                font=fonts.small,
                bg=bg,
                fg=kit.BLACK,
                justify="left",
                anchor="w",
                wraplength=wrap,
            ).pack(fill="x", pady=(round(6 * s), 0))
            kit.link(card, "Hide the code", f.toggle_qr, fonts, bg).pack(
                anchor="w", pady=(round(6 * s), 0)
            )
            return
        tk.Label(
            card,
            text="Open ShoutAbout on your iPhone. It finds this PC by itself.",
            font=fonts.body,
            bg=bg,
            fg=kit.BLACK,
            justify="left",
            anchor="w",
            wraplength=wrap,
        ).pack(fill="x", pady=(round(14 * s), round(12 * s)))
        row = tk.Frame(card, bg=bg)
        row.pack(fill="x")
        waiting = f.pair_state in ("waiting", "denied", "expired")
        if waiting:
            self._mouth = kit.Mouth(row, round(36 * s), bg=bg).pack(side="left", padx=(0, 8))
        tk.Label(
            row,
            text=f.phone_line(),
            font=fonts.body_bold,
            bg=bg,
            fg=kit.BLACK,
            justify="left",
            anchor="w",
            wraplength=wrap - round(50 * s),
        ).pack(side="left", fill="x")
        kit.link(card, "Scan a code instead", f.toggle_qr, fonts, bg).pack(
            anchor="w", pady=(round(14 * s), 0)
        )

    def _refresh_use(self) -> None:
        f = self.flow
        nxt = self._widgets.get("next")
        if nxt is not None:
            nxt.configure(state="normal" if f.can_continue() else "disabled")
        if self._mouth is not None:
            if f.pair_state == "waiting":
                self._mouth.talk()
            else:
                self._mouth.stop()

    # --------------------------------------------------------- screen 3

    def _build_try(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        kit.link(foot, "Back", f.back, fonts).pack(side="left")
        kit.link(foot, "Skip", f.next, fonts).pack(side="right")
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
        tk.Label(
            self.outer,
            text="Your words land here, as if you'd typed them.",
            font=fonts.small,
            bg=kit.YELLOW,
            fg=kit.MUTED,
            anchor="w",
        ).pack(fill="x", pady=(round(8 * s), 0))
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

    # --------------------------------------------------------- screen 4

    def _build_done(self) -> None:
        import tkinter as tk

        f, fonts, s = self.flow, self.fonts, self.scale
        foot = self._footer()
        kit.button(foot, "Done", self.close, fonts, big=True).pack(side="right")
        row = tk.Frame(self.outer, bg=kit.YELLOW)
        row.pack(fill="x")
        self._mouth = kit.Mouth(row, round(150 * s)).pack(side="left", padx=(0, round(24 * s)))
        text = tk.Frame(row, bg=kit.YELLOW)
        text.pack(side="left", fill="both", expand=True)
        lines = []
        if f.mode == "phone":
            lines.append(f.phone_line() if f.pair_state == "paired" else "You can add it any time.")
        else:
            lines.append("ShoutAbout lives in your system tray.")
            if f.pc_on:
                lines.append(f.pc_line())
            if f.phone_on:
                lines.append("On your iPhone, tap Computer and talk.")
        for i, line in enumerate(lines):
            tk.Label(
                text,
                text=line,
                font=fonts.big if i == 0 else fonts.body,
                bg=kit.YELLOW,
                fg=kit.BLACK,
                justify="left",
                anchor="w",
                wraplength=round(470 * s),
            ).pack(fill="x", pady=(round(8 * s) if i == 0 else round(6 * s), 0))
        self._mouth.talk()
        self.win.after(1600, lambda: self._mouth and self._mouth.stop(open_mouth=True))

    def _refresh_done(self) -> None:
        return

    def _refresh(self) -> None:
        getattr(self, f"_refresh_{self.flow.screen}")()
