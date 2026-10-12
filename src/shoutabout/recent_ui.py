"""The Recent page of the ShoutAbout window (Tk): the last few dictations, with Copy.

Draws :class:`~shoutabout.recent_model.RecentModel` in the brand look (:mod:`ui_kit`):
paper-coloured cards on Shout Yellow, newest first; recordings that couldn't be written out
sit above them with **Try again**. The text in a card can be selected (to copy a part).

It keeps itself current: every :data:`WATCH_MS` it compares the hub's Recent stamp (cheap)
and reloads when it changed, so a dictation shows up at the top within a second, even while
the page is open. A reload only adds the new cards and drops the old ones; the cards that
stay are the same widgets, so a selection in them survives, and when the list is scrolled
down, it stays on what the person was reading. Slow work (asking the hub on a Mac, Try
again, the clipboard) runs on worker threads; results come back through a queue.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.recent_model import FAILED_TITLE, Entry, RecentModel, RecentView

log = logging.getLogger(__name__)

POLL_MS = 120
WATCH_MS = 400


class RecentPage:
    def __init__(self, parent: Any, model: RecentModel, *, load_in_background: bool = True) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.win = parent
        self.model = model
        self.closed = False
        self.scale = kit.ui_scale(parent)
        self.fonts = kit.Fonts(tkfont.families(parent))
        self.results: queue.Queue = queue.Queue()
        self._pending: dict[int, Callable[[Any, BaseException | None], None]] = {}
        self._job = 0
        self._loading = False
        self._again = False
        self._seen: Any = model.stamp()
        self._cards: dict[str, Any] = {}  # entry key -> card frame (kept across reloads)
        self._texts: dict[str, Any] = {}  # entry key -> its Text widget
        self._failed_key: Any = None
        self._jobs: dict[str, Any] = {}  # pending after() jobs
        s = self.scale

        outer = tk.Frame(parent, bg=kit.YELLOW)
        outer.pack(fill="both", expand=True, padx=round(28 * s), pady=(round(18 * s), 0))
        self.status = tk.Label(
            outer, text="", font=self.fonts.small_bold, bg=kit.YELLOW, fg=kit.BLACK, anchor="w"
        )
        self.status.pack(side="bottom", fill="x", pady=(round(6 * s), round(12 * s)))
        frame = tk.Frame(outer, bg=kit.YELLOW)
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg=kit.YELLOW, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=kit.YELLOW)
        inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self._fit_scroll())

        def resized(event: Any) -> None:
            self.canvas.itemconfigure(inner_id, width=event.width)
            self._fit_scroll()

        self.canvas.bind("<Configure>", resized)
        self.failed_box = tk.Frame(self.inner, bg=kit.YELLOW)
        self.failed_box.pack(fill="x")
        self.list = tk.Frame(self.inner, bg=kit.YELLOW)
        self.list.pack(fill="x")
        self.empty = tk.Label(self.inner, text="", font=self.fonts.body, bg=kit.YELLOW)
        self.empty.configure(fg=kit.MUTED, anchor="w")

        if load_in_background:
            self.reload()
        else:
            model.load()
            self.render()
        self._jobs["poll"] = self.win.after(POLL_MS, self._poll)
        self._jobs["watch"] = self.win.after(WATCH_MS, self._watch)

    # ------------------------------------------------------------ plumbing

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._pending.clear()
        kit.cancel_jobs(self.win, self._jobs)
        with contextlib.suppress(Exception):
            self.win.destroy()

    def on_shown(self) -> None:
        self.reload()

    def run_async(
        self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        """``work`` on a worker thread; ``done(result, error)`` back on the UI thread. The
        worker never holds a Tk object (freeing one off-thread aborts the process)."""
        self._job += 1
        job = self._job
        self._pending[job] = done
        results = self.results

        def worker() -> None:
            try:
                result, error = work(), None
            except Exception as exc:
                log.exception("Recent action failed")
                result, error = None, exc
            results.put((job, result, error))

        threading.Thread(target=worker, name="recent-worker", daemon=True).start()

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
            log.exception("Recent page update failed")
        if not self.closed:
            self._jobs["poll"] = self.win.after(POLL_MS, self._poll)

    def _watch(self) -> None:
        """A new dictation (or a failed recording) since the last look: reload."""
        if self.closed:
            return
        try:
            stamp = self.model.stamp()
            if stamp != self._seen and not self._loading:
                self.reload()
        except Exception:
            log.exception("watching Recent failed")
        self._jobs["watch"] = self.win.after(WATCH_MS, self._watch)

    def reload(self) -> None:
        if self.closed:
            return
        if self._loading:
            self._again = True  # once this load is in
            return
        self._loading, self._again = True, False
        self._seen = self.model.stamp()  # read first: a change during the load reloads again
        self.run_async(self.model.load, self._loaded)

    def _loaded(self, _result: Any, _error: BaseException | None) -> None:
        self._loading = False
        self.render()
        if self._again:
            self.reload()

    def on_wheel(self, event: Any) -> None:
        if self.scrollbar.winfo_ismapped():
            step = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
            self.canvas.yview_scroll(step, "units")

    def _fit_scroll(self) -> None:
        """Scroll region = the cards; the scrollbar shows only when they don't fit."""
        with contextlib.suppress(Exception):
            canvas = self.canvas
            canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), self._content_height()))
            needed = self._content_height() > canvas.winfo_height() + 1
            packed = bool(self.scrollbar.winfo_manager())
            if needed and not packed:
                self.scrollbar.pack(side="right", fill="y")
            elif not needed and packed:
                self.scrollbar.pack_forget()
                canvas.yview_moveto(0)

    def _content_height(self) -> int:
        return max(1, int(self.inner.winfo_reqheight()))

    def _set_status(self) -> None:
        m = self.model
        bad = m.error and bool(m.message)
        self.status.configure(
            text=m.message,
            bg=kit.RED if bad else kit.YELLOW,
            padx=round(8 * self.scale) if bad else 0,
        )

    # -------------------------------------------------------------- drawing

    def render(self) -> None:
        if self.closed:
            return
        view = self.model.view()
        top = self.canvas.canvasy(0)  # what the person is looking at, in content pixels
        before = self.failed_box.winfo_reqheight()
        self._fill_failed(view)
        added = self._sync_cards(view.entries)
        self.inner.update_idletasks()
        above = self.failed_box.winfo_reqheight() - before
        above += sum(card.winfo_reqheight() + round(10 * self.scale) for card in added)
        if view.loading or view.empty_text:
            self.empty.configure(text="" if view.loading else view.empty_text)
            self.empty.pack(fill="x", pady=(round(4 * self.scale), 0))
        else:
            self.empty.pack_forget()
        self._set_status()
        self._fit_scroll()
        if top > 0:  # scrolled down: stay on the same cards (new ones arrive above)
            total = self._content_height()
            self.canvas.yview_moveto(max(0.0, min(1.0, (top + above) / total)))

    def _sync_cards(self, entries: list[Entry]) -> list[Any]:
        """Make the cards match ``entries`` (newest first): keep the ones still listed,
        add new ones in place, drop the rest. Returns the new cards."""
        keys = {e.key for e in entries}
        for key in [k for k in self._cards if k not in keys]:
            self._texts.pop(key, None)
            self._cards.pop(key).destroy()
        added: list[Any] = []
        older: Any = None  # the card just below, while going from the oldest up
        for entry in reversed(entries):
            card = self._cards.get(entry.key)
            if card is None:
                card = self._card(entry)
                self._cards[entry.key] = card
                pack = {"fill": "x", "pady": (round(10 * self.scale), 0)}
                if older is not None:
                    card.pack(before=older, **pack)
                else:
                    card.pack(**pack)
                added.append(card)
            older = card
        for key in self._texts:
            self._fit_text(self._texts[key])
        return added

    def _card(self, entry: Entry) -> Any:
        import tkinter as tk

        s, f = self.scale, self.fonts
        pad = round(12 * s)
        card = tk.Frame(self.list, bg=kit.WHITE, highlightthickness=1, highlightbackground=kit.GREY)
        top = tk.Frame(card, bg=kit.WHITE)
        top.pack(fill="x", padx=pad, pady=(round(8 * s), 0))
        meta = " · ".join(x for x in (entry.when, entry.source, entry.note) if x)
        tk.Label(top, text=meta, font=f.small_bold, bg=kit.WHITE, fg=kit.MUTED).pack(side="left")
        kit.button(top, "Copy", lambda k=entry.key: self._copy(k), f, "light").pack(side="right")
        text = tk.Text(
            card,
            font=f.body,
            bg=kit.WHITE,
            fg=kit.BLACK,
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            height=1,
            padx=0,
            pady=0,
            cursor="xterm",
            selectbackground=kit.BLACK,
            selectforeground=kit.YELLOW,
            inactiveselectbackground=kit.BLACK,
            takefocus=0,
        )
        text.insert("1.0", entry.text)
        text.configure(state="disabled")  # read-only; it can still be selected and copied
        text.pack(fill="x", padx=pad, pady=(round(4 * s), round(10 * s)))
        text.bind("<Configure>", lambda _e, t=text: self._fit_text(t))
        self._texts[entry.key] = text
        return card

    def _fit_text(self, text: Any) -> None:
        """Make a card's text exactly as tall as its wrapped lines."""
        with contextlib.suppress(Exception):
            counted = text.count("1.0", "end", "displaylines")
            lines = counted[0] if isinstance(counted, tuple) else counted
            lines = max(1, int(lines or 1))
            if int(text.cget("height")) != lines:
                text.configure(height=lines)

    def _fill_failed(self, view: RecentView) -> None:
        import tkinter as tk

        key = (tuple(view.failed), frozenset(self.model.busy), view.can_retry)
        if key == self._failed_key:
            return
        self._failed_key = key
        for child in self.failed_box.winfo_children():
            child.destroy()
        if not view.failed:
            return
        s, f = self.scale, self.fonts
        pad = round(12 * s)
        box = tk.Frame(
            self.failed_box, bg=kit.WHITE, highlightthickness=2, highlightbackground=kit.RED
        )
        box.pack(fill="x", pady=(round(10 * s), 0))
        tk.Label(
            box, text=FAILED_TITLE.upper(), font=f.small_bold, bg=kit.WHITE, fg=kit.BLACK
        ).pack(anchor="w", padx=pad, pady=(round(10 * s), 0))
        for item in view.failed:
            row = tk.Frame(box, bg=kit.WHITE)
            row.pack(fill="x", padx=pad, pady=(round(8 * s), round(4 * s)))
            meta = " · ".join(x for x in (item.when, item.source, item.length) if x)
            tk.Label(row, text=meta, font=f.body_bold, bg=kit.WHITE, fg=kit.BLACK).pack(side="left")
            busy = item.id in self.model.busy
            kit.link(row, "Delete", lambda i=item.id: self._delete_failed(i), f, kit.WHITE).pack(
                side="right", padx=(round(10 * s), 0)
            )
            retry = kit.button(
                row, "Working…" if busy else "Try again", lambda i=item.id: self._retry(i), f
            )
            if busy or not view.can_retry:
                retry.configure(state="disabled")
            retry.pack(side="right")
            if item.error:
                tk.Label(
                    box,
                    text=item.error,
                    font=f.small,
                    bg=kit.WHITE,
                    fg=kit.MUTED,
                    anchor="w",
                    justify="left",
                    wraplength=round(520 * s),
                ).pack(fill="x", padx=pad)
        tk.Frame(box, bg=kit.WHITE, height=round(8 * s)).pack()

    # -------------------------------------------------------------- actions

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
