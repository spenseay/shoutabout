"""The settings window (Tk): one short page over :class:`~shoutabout.settings_model.SettingsModel`.

Sections: Talk to this PC, Phones, Quality, AI cleanup (only when Ollama with a suitable
model is there), History (off by default), This PC (name and start with Windows), and a
footer (History…, Teach words…, version). Changes save as soon as they're made; the line at
the bottom says "Saved." or "Restart ShoutAbout to apply".
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.settings_model import (
    CLEANUP_CHOICES,
    CLEANUP_NOTE,
    HISTORY_LABEL,
    HISTORY_NOTE,
    MODE_CHOICES,
    NO_MIC_NOTE,
    SettingsModel,
    SettingsView,
)

log = logging.getLogger(__name__)

WINDOW_TITLE = "ShoutAbout settings"
BASE_W, BASE_H = 640, 940
POLL_MS = 120
CONFIRM_MS = 3000
QUALITY_LABELS = {"best": "Best", "balanced": "Balanced", "light": "Light"}


class Segmented:
    """A row of buttons where exactly one is selected (black) and the rest are light."""

    def __init__(
        self,
        parent: Any,
        options: dict[str, str],
        value: str,
        on_change: Callable[[str], None],
        fonts: kit.Fonts,
        *,
        enabled: bool = True,
    ) -> None:
        import tkinter as tk

        self.frame = tk.Frame(parent, bg=kit.BLACK, padx=2, pady=2)
        for key, label in options.items():
            selected = key == value
            tk.Button(
                self.frame,
                text=label,
                command=(lambda k=key: on_change(k)) if enabled else None,
                bg=kit.BLACK if selected else kit.PAPER,
                fg=kit.YELLOW if selected else kit.BLACK,
                activebackground="#333333" if selected else kit.WHITE,
                activeforeground=kit.YELLOW if selected else kit.BLACK,
                font=fonts.small_bold,
                relief="flat",
                bd=0,
                padx=12,
                pady=5,
                cursor="hand2" if enabled else "arrow",
                highlightthickness=0,
            ).pack(side="left", padx=(0, 2))


class SettingsWindow:
    def __init__(
        self,
        window: Any,
        model: SettingsModel,
        *,
        on_close: Callable[[], None] | None = None,
        on_add_phone: Callable[[], None] | None = None,
        load_in_background: bool = True,
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.win = window
        self.model = model
        self.on_close = on_close
        self.on_add_phone = on_add_phone
        self.closed = False
        self.scale = kit.ui_scale(window)
        self.fonts = kit.Fonts(tkfont.families(window))
        self.results: queue.Queue = queue.Queue()
        self._pending: dict[int, Callable[[Any, BaseException | None], None]] = {}
        self._job = 0
        self._confirm: str | None = None
        self._icon: Any = None
        window.title(WINDOW_TITLE)
        window.configure(bg=kit.YELLOW)
        w = min(round(BASE_W * self.scale), window.winfo_screenwidth() - 40)
        h = min(round(BASE_H * self.scale), window.winfo_screenheight() - 80)
        window.geometry(f"{w}x{h}")
        window.minsize(min(w, round(560 * self.scale)), min(h, round(600 * self.scale)))
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _e: self.close())
        with contextlib.suppress(Exception):
            self._icon = kit.app_icon_photo(window, 32)
            window.iconphoto(False, self._icon)
        # The page scrolls when the screen is short (a 1080p laptop at 150 % is 720 px high).
        frame = tk.Frame(window, bg=kit.YELLOW)
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg=kit.YELLOW, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        page = tk.Frame(self.canvas, bg=kit.YELLOW)
        page_id = self.canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", lambda _e: self._fit_scroll())
        self.canvas.bind(
            "<Configure>",
            lambda e: (self.canvas.itemconfigure(page_id, width=e.width), self._fit_scroll()),
        )
        window.bind("<MouseWheel>", self._on_wheel)
        self.outer = tk.Frame(page, bg=kit.YELLOW)
        self.outer.pack(fill="both", expand=True, padx=round(28 * self.scale))
        self.view: SettingsView = model.view()
        self.render()
        if load_in_background:
            self.run_async(model.load_models, lambda _r, _e: self.reload())
        self.win.after(POLL_MS, self._poll)

    # ------------------------------------------------------------ plumbing

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._pending.clear()
        with contextlib.suppress(Exception):
            self.win.destroy()
        if self.on_close is not None:
            self.on_close()

    def bring_to_front(self) -> None:
        with contextlib.suppress(Exception):
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()

    def run_async(
        self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        """Run ``work`` on a worker thread; ``done(result, error)`` runs back on the Tk thread.

        The worker holds only ``work`` and the queue, never this window or a widget: if the
        window closes first, no Tk object is ever freed on the worker thread (that aborts
        the process: "Tcl_AsyncDelete: async handler deleted by the wrong thread").
        """
        self._job += 1
        job = self._job
        self._pending[job] = done
        results = self.results

        def worker() -> None:
            try:
                result, error = work(), None
            except Exception as exc:
                log.exception("settings action failed")
                result, error = None, exc
            results.put((job, result, error))

        threading.Thread(target=worker, name="settings-worker", daemon=True).start()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            while True:
                job, result, error = self.results.get_nowait()
                done = self._pending.pop(job, None)
                if done is not None:
                    done(result, error)
        except queue.Empty:
            pass
        except Exception:
            log.exception("settings window update failed")
        if not self.closed:
            self.win.after(POLL_MS, self._poll)

    def _fit_scroll(self) -> None:
        """Scroll region = the page; the scrollbar shows only when the page doesn't fit."""
        with contextlib.suppress(Exception):
            canvas = self.canvas
            canvas.configure(scrollregion=canvas.bbox("all"))
            box = canvas.bbox("all")
            needed = box is not None and box[3] > canvas.winfo_height() + 1
            packed = bool(self.scrollbar.winfo_manager())  # mapped or not (yet)
            if needed and not packed:
                self.scrollbar.pack(side="right", fill="y")
            elif not needed and packed:
                self.scrollbar.pack_forget()
                canvas.yview_moveto(0)

    def _on_wheel(self, event: Any) -> None:
        if self.scrollbar.winfo_ismapped():
            step = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
            self.canvas.yview_scroll(step, "units")

    def reload(self) -> None:
        if self.closed:
            return
        try:
            self.view = self.model.view()
        except Exception:
            log.exception("couldn't read the settings")
        self.render()

    def apply(self, action: Callable[[], Any]) -> None:
        """Run a change off the UI thread, then re-read and redraw the page."""
        self.run_async(action, lambda _r, _e: self.reload())

    # -------------------------------------------------------------- layout

    def _section(self, title: str) -> Any:
        import tkinter as tk

        s = self.scale
        tk.Frame(self.outer, bg=kit.BLACK, height=max(1, round(2 * s))).pack(
            fill="x", pady=(round(14 * s), round(8 * s))
        )
        tk.Label(
            self.outer,
            text=title.upper(),
            font=self.fonts.small_bold,
            bg=kit.YELLOW,
            fg=kit.BLACK,
            anchor="w",
        ).pack(fill="x")
        body = tk.Frame(self.outer, bg=kit.YELLOW)
        body.pack(fill="x", pady=(round(6 * s), 0))
        return body

    def _note(self, parent: Any, text: str, *, side: str | None = None) -> Any:
        import tkinter as tk

        label = tk.Label(
            parent,
            text=text,
            font=self.fonts.small,
            bg=kit.YELLOW,
            fg=kit.MUTED,
            anchor="w",
            justify="left",
            wraplength=round(560 * self.scale),
        )
        if side:
            label.pack(side=side)
        else:
            label.pack(fill="x", pady=(round(4 * self.scale), 0))
        return label

    def _switch_row(
        self, parent: Any, text: str, on: bool, toggle: Callable[[], None], enabled: bool = True
    ) -> Any:
        import tkinter as tk

        row = tk.Frame(parent, bg=kit.YELLOW)
        row.pack(fill="x")
        switch = kit.Switch(row, self.scale * 0.85, kit.YELLOW, on)
        switch.canvas.pack(side="left")
        label = tk.Label(
            row,
            text=text,
            font=self.fonts.body_bold,
            bg=kit.YELLOW,
            fg=kit.BLACK if enabled else kit.MUTED,
        )
        label.pack(side="left", padx=(round(10 * self.scale), 0))
        if enabled:
            for widget in (switch.canvas, label):
                widget.bind("<Button-1>", lambda _e: toggle())
                widget.configure(cursor="hand2")
        return row

    def _dropdown(
        self, parent: Any, options: dict[str, str], value: str, on_change: Callable[[str], None]
    ) -> Any:
        import tkinter as tk

        labels = list(options.values())
        current = options.get(value, labels[0] if labels else "")
        var = tk.StringVar(master=self.win, value=current)
        reverse = {label: key for key, label in options.items()}
        menu = tk.OptionMenu(parent, var, *labels, command=lambda lbl: on_change(reverse[lbl]))
        menu.configure(
            font=self.fonts.body,
            bg=kit.PAPER,
            fg=kit.BLACK,
            activebackground=kit.WHITE,
            relief="flat",
            highlightthickness=2,
            highlightbackground=kit.BLACK,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
        )
        menu["menu"].configure(font=self.fonts.body, bg=kit.WHITE, fg=kit.BLACK)
        menu._var = var  # keep the variable alive with the widget  # type: ignore[attr-defined]
        return menu

    def render(self) -> None:
        import tkinter as tk

        if self.closed:
            return
        top = self.canvas.yview()[0]  # keep the scroll position across a redraw
        for child in self.outer.winfo_children():
            child.destroy()
        self.win.after_idle(lambda: self.canvas.yview_moveto(top))
        v, f, s, m = self.view, self.fonts, self.scale, self.model

        head = tk.Frame(self.outer, bg=kit.YELLOW)
        head.pack(fill="x", pady=(round(18 * s), 0))
        if self._icon is not None:
            tk.Label(head, image=self._icon, bg=kit.YELLOW).pack(side="left", padx=(0, 10))
        tk.Label(head, text="SETTINGS", font=f.title, bg=kit.YELLOW, fg=kit.BLACK).pack(side="left")

        # Talk to this PC
        body = self._section("Talk to this PC")
        self._switch_row(
            body,
            "On" if v.pc_enabled else "Off",
            v.pc_enabled,
            lambda: self.apply(lambda: m.set_pc_enabled(not v.pc_enabled)),
        )
        if v.pc_enabled:
            row = tk.Frame(body, bg=kit.YELLOW)
            row.pack(fill="x", pady=(round(8 * s), 0))
            tk.Label(row, text="Key", font=f.body, bg=kit.YELLOW, fg=kit.BLACK).pack(side="left")
            self._dropdown(
                row,
                v.hotkeys or {v.hotkey: v.hotkey},
                v.hotkey,
                lambda key: self.apply(lambda: m.set_hotkey(key)),
            ).pack(side="left", padx=(round(8 * s), round(14 * s)))
            Segmented(
                row,
                MODE_CHOICES,
                v.mode,
                lambda mode: self.apply(lambda: m.set_mode(mode)),
                f,
            ).frame.pack(side="left")
        if v.mic_missing:
            self._note(body, NO_MIC_NOTE)

        # Phones
        body = self._section("Phones")
        if not v.devices:
            self._note(body, "No phones yet.")
        for device in v.devices:
            row = tk.Frame(body, bg=kit.YELLOW)
            row.pack(fill="x", pady=(0, round(4 * s)))
            tk.Label(row, text=device.name, font=f.body_bold, bg=kit.YELLOW, fg=kit.BLACK).pack(
                side="left"
            )
            tk.Label(row, text=device.last_used, font=f.small, bg=kit.YELLOW, fg=kit.MUTED).pack(
                side="left", padx=(round(10 * s), 0)
            )
            confirming = self._confirm == device.key
            kit.link(
                row,
                "Sure? Remove" if confirming else "Remove",
                lambda key=device.key: self._remove(key),
                f,
                kit.RED if confirming else kit.YELLOW,
            ).pack(side="right")
        kit.button(body, "Add a phone", self._add_phone, f, "light").pack(
            anchor="w", pady=(round(6 * s), 0)
        )

        # Quality
        body = self._section("Quality")
        options = {"auto": v.automatic_label}
        for key in v.quality_options:
            if key != "auto":
                options[key] = QUALITY_LABELS.get(key, key)
        if v.quality == "custom":
            options["custom"] = v.custom_label
        self._dropdown(
            body,
            options,
            v.quality,
            lambda q: None if q == "custom" else self.apply(lambda: m.set_quality(q)),
        ).pack(anchor="w")
        if v.loading_quality:
            self._note(body, "Checking this PC…")
        elif v.quality_note:
            self._note(body, v.quality_note)

        # AI cleanup: only when Ollama with a suitable model is there
        if v.show_cleanup:
            body = self._section("AI cleanup")
            Segmented(
                body, CLEANUP_CHOICES, v.cleanup, lambda c: self.apply(lambda: m.set_cleanup(c)), f
            ).frame.pack(anchor="w")
            self._note(body, CLEANUP_NOTE)

        # History (off by default; the hub applies it at once)
        body = self._section("History")
        self._switch_row(
            body,
            HISTORY_LABEL,
            v.history_on,
            lambda: self.apply(lambda: m.set_history(not v.history_on)),
        )
        self._note(body, HISTORY_NOTE)

        # This PC
        body = self._section("This PC")
        row = tk.Frame(body, bg=kit.YELLOW)
        row.pack(fill="x")
        tk.Label(row, text="Name", font=f.body, bg=kit.YELLOW, fg=kit.BLACK).pack(side="left")
        entry = tk.Entry(
            row,
            font=f.body,
            bg=kit.WHITE,
            fg=kit.BLACK,
            relief="flat",
            highlightthickness=2,
            highlightbackground=kit.BLACK,
            highlightcolor=kit.RED,
            insertbackground=kit.BLACK,
            width=24,
        )
        entry.insert(0, v.name)
        entry.pack(side="left", padx=(round(8 * s), round(8 * s)), ipady=3)
        entry.bind("<Return>", lambda _e: self._rename(entry))
        kit.button(row, "Save", lambda: self._rename(entry), f).pack(side="left")
        tk.Frame(body, bg=kit.YELLOW, height=round(10 * s)).pack()
        self._switch_row(
            body,
            "Start with Windows",
            v.startup_on,
            lambda: self.apply(lambda: m.set_startup(not v.startup_on)),
            enabled=v.startup_editable,
        )
        if v.startup_note:
            self._note(body, v.startup_note)

        # Footer
        tk.Frame(self.outer, bg=kit.BLACK, height=max(1, round(2 * s))).pack(
            fill="x", pady=(round(14 * s), round(10 * s))
        )
        foot = tk.Frame(self.outer, bg=kit.YELLOW)
        foot.pack(fill="x")
        svc = m.svc
        if svc.open_history is not None:
            kit.button(foot, "History…", svc.open_history, f, "light").pack(
                side="left", padx=(0, round(8 * s))
            )
        if svc.open_teach is not None:
            kit.button(foot, "Teach words…", svc.open_teach, f, "light").pack(side="left")
        tk.Label(
            foot,
            text=f"ShoutAbout {v.version}".strip(),
            font=f.small,
            bg=kit.YELLOW,
            fg=kit.MUTED,
        ).pack(side="right")
        extras = tk.Frame(self.outer, bg=kit.YELLOW)
        extras.pack(fill="x", pady=(round(6 * s), 0))
        if svc.open_logs is not None:
            kit.link(extras, "Open log folder", svc.open_logs, f).pack(side="left")
        if svc.copy_hub_token is not None:
            kit.link(extras, "Copy hub token (Shortcuts)", svc.copy_hub_token, f).pack(
                side="left", padx=(round(14 * s), 0)
            )
        self._status = tk.Label(
            self.outer,
            text=m.message,
            font=f.small_bold,
            bg=kit.RED if m.error else kit.YELLOW,
            fg=kit.BLACK,
            anchor="w",
        )
        self._status.pack(fill="x", side="bottom", pady=(0, round(12 * s)))

    # -------------------------------------------------------------- actions

    def _remove(self, key: str) -> None:
        if self._confirm != key:
            self._confirm = key
            self.render()
            self.win.after(CONFIRM_MS, lambda: self._cancel_confirm(key))
            return
        self._confirm = None
        self.apply(lambda: self.model.remove_device(key))

    def _cancel_confirm(self, key: str) -> None:
        if self._confirm == key and not self.closed:
            self._confirm = None
            self.render()

    def _rename(self, entry: Any) -> None:
        name = entry.get()  # read on the Tk thread; the worker only gets the text
        model = self.model
        self.apply(lambda: model.rename(name))

    def _add_phone(self) -> None:
        if self.on_add_phone is not None:
            self.on_add_phone()
