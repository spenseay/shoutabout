"""The History window (Tk): recent dictations with Copy, kept recordings with Try again.

Draws :class:`~shoutabout.history_model.HistoryModel` in the brand look of the other
windows (:mod:`ui_kit`): Shout Yellow ground, the mouth icon, an Anton headline over a red
rule, paper-coloured cards. Slow work (reading the file, transcribing again, the clipboard)
runs on worker threads; results come back through a queue polled on the Tk thread.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.history_model import (
    FAILED_TITLE,
    OFF_TEXT,
    OFF_TITLE,
    SUBTITLE,
    HistoryModel,
    HistoryView,
)

log = logging.getLogger(__name__)

WINDOW_TITLE = "ShoutAbout history"
BASE_W, BASE_H = 640, 760
POLL_MS = 120
SEARCH_DELAY_MS = 180
CONFIRM_MS = 3000


class HistoryWindow:
    def __init__(
        self,
        window: Any,
        model: HistoryModel,
        *,
        on_close: Callable[[], None] | None = None,
        load_in_background: bool = True,
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.win = window
        self.model = model
        self.on_close = on_close
        self.closed = False
        self.scale = kit.ui_scale(window)
        self.fonts = kit.Fonts(tkfont.families(window))
        self.results: queue.Queue = queue.Queue()
        self._pending: dict[int, Callable[[Any, BaseException | None], None]] = {}
        self._job = 0
        self._icon: Any = None
        self._confirm_clear = False
        self._search_job: Any = None
        self._texts: list[Any] = []  # text labels, re-wrapped when the window resizes
        self._query = ""
        self._layout: str | None = None
        s = self.scale

        window.title(WINDOW_TITLE)
        window.configure(bg=kit.YELLOW)
        w = min(round(BASE_W * s), window.winfo_screenwidth() - 40)
        h = min(round(BASE_H * s), window.winfo_screenheight() - 80)
        window.geometry(f"{w}x{h}")
        window.minsize(min(w, round(480 * s)), min(h, round(420 * s)))
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _e: self.close())
        window.bind("<MouseWheel>", self._on_wheel)
        with contextlib.suppress(Exception):
            self._icon = kit.app_icon_photo(window, 32)
            window.iconphoto(False, self._icon)

        self.outer = tk.Frame(window, bg=kit.YELLOW)
        self.outer.pack(fill="both", expand=True, padx=round(28 * s))
        head = tk.Frame(self.outer, bg=kit.YELLOW)
        head.pack(fill="x", pady=(round(18 * s), 0))
        if self._icon is not None:
            tk.Label(head, image=self._icon, bg=kit.YELLOW).pack(side="left", padx=(0, 10))
        tk.Label(head, text="HISTORY", font=self.fonts.title, bg=kit.YELLOW, fg=kit.BLACK).pack(
            side="left"
        )
        tk.Label(
            self.outer,
            text=SUBTITLE,
            font=self.fonts.body,
            bg=kit.YELLOW,
            fg=kit.BLACK,
            anchor="w",
        ).pack(fill="x", pady=(round(4 * s), round(8 * s)))
        tk.Frame(self.outer, bg=kit.RED, height=round(5 * s)).pack(
            fill="x", pady=(0, round(14 * s))
        )
        self.status = tk.Label(
            self.outer, text="", font=self.fonts.small_bold, bg=kit.YELLOW, fg=kit.BLACK, anchor="w"
        )
        self.status.pack(side="bottom", fill="x", pady=(round(6 * s), round(12 * s)))
        self.body = tk.Frame(self.outer, bg=kit.YELLOW)
        self.body.pack(fill="both", expand=True)

        if load_in_background:
            self.run_async(model.load, lambda _r, _e: self.render())
        else:
            model.load()
        self.render()
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
        """``work`` on a worker thread; ``done(result, error)`` back on the Tk thread. The
        worker never holds a Tk object (freeing one off-thread aborts the process)."""
        self._job += 1
        job = self._job
        self._pending[job] = done
        results = self.results

        def worker() -> None:
            try:
                result, error = work(), None
            except Exception as exc:
                log.exception("history action failed")
                result, error = None, exc
            results.put((job, result, error))

        threading.Thread(target=worker, name="history-worker", daemon=True).start()

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
            log.exception("history window update failed")
        if not self.closed:
            self.win.after(POLL_MS, self._poll)

    def _on_wheel(self, event: Any) -> None:
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        with contextlib.suppress(Exception):
            step = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
            canvas.yview_scroll(step, "units")

    def _set_status(self) -> None:
        m = self.model
        self.status.configure(
            text=m.message,
            bg=kit.RED if m.error and m.message else kit.YELLOW,
            padx=round(8 * self.scale) if m.error and m.message else 0,
        )

    def reload(self) -> None:
        if not self.closed:
            self.run_async(self.model.load, lambda _r, _e: self.render())

    # -------------------------------------------------------------- drawing

    def render(self) -> None:
        if self.closed:
            return
        view = self.model.view(self._query)
        layout = "list" if view.enabled else "off"
        if layout != self._layout:
            for child in self.body.winfo_children():
                child.destroy()
            self._layout = layout
            if layout == "off":
                self._build_off()
            else:
                self._build_list_frame()
        if layout == "list":
            self._fill_list(view)
        self._set_status()

    def _build_off(self) -> None:
        import tkinter as tk

        s, f = self.scale, self.fonts
        tk.Label(
            self.body, text=OFF_TITLE, font=f.big, bg=kit.YELLOW, fg=kit.BLACK, anchor="w"
        ).pack(fill="x", pady=(round(10 * s), round(6 * s)))
        tk.Label(
            self.body,
            text=OFF_TEXT,
            font=f.body,
            bg=kit.YELLOW,
            fg=kit.BLACK,
            anchor="w",
            justify="left",
            wraplength=round(540 * s),
        ).pack(fill="x")
        kit.button(self.body, "Turn history on", self._turn_on, f).pack(
            anchor="w", pady=(round(14 * s), 0)
        )

    def _build_list_frame(self) -> None:
        import tkinter as tk

        s, f = self.scale, self.fonts
        row = tk.Frame(self.body, bg=kit.YELLOW)
        row.pack(fill="x", pady=(0, round(10 * s)))
        tk.Label(row, text="Search", font=f.body_bold, bg=kit.YELLOW, fg=kit.BLACK).pack(
            side="left", padx=(0, round(8 * s))
        )
        self.search = tk.Entry(
            row,
            font=f.body,
            bg=kit.WHITE,
            fg=kit.BLACK,
            relief="flat",
            highlightthickness=2,
            highlightbackground=kit.BLACK,
            highlightcolor=kit.RED,
            insertbackground=kit.BLACK,
        )
        self.search.pack(side="left", fill="x", expand=True, ipady=3)
        self.search.insert(0, self._query)
        self.search.bind("<KeyRelease>", lambda _e: self._search_soon())

        foot = tk.Frame(self.body, bg=kit.YELLOW)
        foot.pack(side="bottom", fill="x", pady=(round(10 * s), 0))
        self.clear_button = kit.button(foot, "Clear history", self._clear, f, "light")
        self.clear_button.pack(side="left")
        self.count = tk.Label(foot, text="", font=f.small, bg=kit.YELLOW, fg=kit.MUTED)
        self.count.pack(side="right")

        frame = tk.Frame(
            self.body, bg=kit.PAPER, highlightthickness=2, highlightbackground=kit.BLACK
        )
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg=kit.PAPER, highlightthickness=0, bd=0)
        scroll = tk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=kit.PAPER)
        inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        def resized(event: Any) -> None:
            self.canvas.itemconfigure(inner_id, width=event.width)
            self._rewrap(event.width)

        self.canvas.bind("<Configure>", resized)

    def _wrap_width(self, width: int | None = None) -> int:
        if width is None:
            width = self.canvas.winfo_width()
        if width <= 1:  # not laid out yet
            width = round((BASE_W - 60) * self.scale)
        return max(120, width - round(48 * self.scale))

    def _rewrap(self, width: int) -> None:
        wrap = self._wrap_width(width)
        for label in self._texts:
            with contextlib.suppress(Exception):
                label.configure(wraplength=wrap)

    def _fill_list(self, view: HistoryView) -> None:
        import tkinter as tk

        s, f = self.scale, self.fonts
        for child in self.inner.winfo_children():
            child.destroy()
        self._texts = []
        wrap = self._wrap_width()
        pad = round(12 * s)

        if view.failed:
            box = tk.Frame(
                self.inner, bg=kit.WHITE, highlightthickness=2, highlightbackground=kit.RED
            )
            box.pack(fill="x", padx=pad, pady=(pad, 0))
            tk.Label(
                box,
                text=FAILED_TITLE.upper(),
                font=f.small_bold,
                bg=kit.WHITE,
                fg=kit.BLACK,
                anchor="w",
            ).pack(fill="x", padx=pad, pady=(round(10 * s), 0))
            for item in view.failed:
                row = tk.Frame(box, bg=kit.WHITE)
                row.pack(fill="x", padx=pad, pady=(round(8 * s), round(4 * s)))
                meta = " · ".join(x for x in (item.when, item.source, item.length) if x)
                tk.Label(row, text=meta, font=f.body_bold, bg=kit.WHITE, fg=kit.BLACK).pack(
                    side="left"
                )
                busy = item.id in self.model.busy
                kit.link(
                    row, "Delete", lambda i=item.id: self._delete_failed(i), f, kit.WHITE
                ).pack(side="right", padx=(round(10 * s), 0))
                retry = kit.button(
                    row,
                    "Working…" if busy else "Try again",
                    lambda i=item.id: self._retry(i),
                    f,
                )
                if busy or not view.can_retry:
                    retry.configure(state="disabled")
                retry.pack(side="right")
                if item.error:
                    err = tk.Label(
                        box,
                        text=item.error,
                        font=f.small,
                        bg=kit.WHITE,
                        fg=kit.MUTED,
                        anchor="w",
                        justify="left",
                        wraplength=wrap - pad,
                    )
                    err.pack(fill="x", padx=pad)
            tk.Frame(box, bg=kit.WHITE, height=round(8 * s)).pack()

        if view.loading:
            tk.Label(
                self.inner, text="Reading your history…", font=f.body, bg=kit.PAPER, fg=kit.MUTED
            ).pack(anchor="w", padx=pad, pady=pad)
        elif view.empty_text:
            tk.Label(
                self.inner, text=view.empty_text, font=f.body, bg=kit.PAPER, fg=kit.MUTED
            ).pack(anchor="w", padx=pad, pady=pad)

        for entry in view.entries:
            card = tk.Frame(
                self.inner, bg=kit.WHITE, highlightthickness=1, highlightbackground=kit.GREY
            )
            card.pack(fill="x", padx=pad, pady=(round(10 * s), 0))
            top = tk.Frame(card, bg=kit.WHITE)
            top.pack(fill="x", padx=pad, pady=(round(8 * s), 0))
            meta = " · ".join(x for x in (entry.when, entry.source, entry.note) if x)
            tk.Label(top, text=meta, font=f.small_bold, bg=kit.WHITE, fg=kit.MUTED).pack(
                side="left"
            )
            kit.button(top, "Copy", lambda k=entry.key: self._copy(k), f, "light").pack(
                side="right"
            )
            text = tk.Label(
                card,
                text=entry.text,
                font=f.body,
                bg=kit.WHITE,
                fg=kit.BLACK,
                anchor="w",
                justify="left",
                wraplength=wrap - pad,
            )
            text.pack(fill="x", padx=pad, pady=(round(4 * s), round(10 * s)))
            self._texts.append(text)
        tk.Frame(self.inner, bg=kit.PAPER, height=pad).pack()

        if view.total:
            shown = len(view.entries)
            words = "dictation" if view.total == 1 else "dictations"
            count = f"{view.total} {words}"
            if view.limited:
                count = f"Newest {shown} of {view.total}. Search to find older ones."
        else:
            count = ""
        self.count.configure(text=count)
        self.clear_button.configure(
            text="Sure? Clear everything" if self._confirm_clear else "Clear history",
            bg=kit.RED if self._confirm_clear else kit.PAPER,
        )
        self.canvas.yview_moveto(0)

    # -------------------------------------------------------------- actions

    def _search_soon(self) -> None:
        if self._search_job is not None:
            with contextlib.suppress(Exception):
                self.win.after_cancel(self._search_job)
        self._search_job = self.win.after(SEARCH_DELAY_MS, self._search_now)

    def _search_now(self) -> None:
        self._search_job = None
        with contextlib.suppress(Exception):
            self._query = self.search.get()
        self.render()

    def _copy(self, key: str) -> None:
        model = self.model
        self.run_async(lambda: model.copy(key), lambda _r, _e: self._set_status())

    def _retry(self, item_id: str) -> None:
        model = self.model
        model.busy.add(item_id)
        model.message, model.error = "Writing it out…", False
        self.render()
        self.run_async(lambda: model.retry(item_id), lambda _r, _e: self.reload())

    def _delete_failed(self, item_id: str) -> None:
        model = self.model
        self.run_async(lambda: model.delete_failed(item_id), lambda _r, _e: self.reload())

    def _clear(self) -> None:
        if not self._confirm_clear:
            self._confirm_clear = True
            self.render()
            self.win.after(CONFIRM_MS, self._cancel_clear)
            return
        self._confirm_clear = False
        model = self.model
        self.run_async(model.clear, lambda _r, _e: self.reload())

    def _cancel_clear(self) -> None:
        if self._confirm_clear and not self.closed:
            self._confirm_clear = False
            self.render()

    def _turn_on(self) -> None:
        model = self.model
        self.run_async(model.turn_on, lambda _r, _e: self.reload())
