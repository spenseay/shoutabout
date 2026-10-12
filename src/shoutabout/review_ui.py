"""The Teach words page of the ShoutAbout window (Tk): teach a word, review suggestions.

* a box to add a word (Enter adds it, at the top of vocab.txt);
* suggestions waiting for a yes or no (Add selected / Dismiss selected), and **Suggest from
  my project folders**, the only thing that ever looks at the project folders: once, when
  pressed (:func:`suggest_from_projects`);
* your words in priority order (Remove, Open vocab.txt).

:class:`TeachPage` draws into a frame of :class:`shoutabout.app_window.AppWindow`, on the
UI thread; slow work (file locks, the folder scan) runs on short worker threads that hand
results back through a queue. ``tkinter`` is imported lazily, so importing this module
works headless.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.config import Paths
from shoutabout.learn import SOURCES, Suggestion

log = logging.getLogger(__name__)

POLL_MS = 120
SCAN_LABEL = "Suggest from my project folders"
SOURCE_TITLES = {"history": "From your dictations", "projects": "From your projects"}


# ------------------------------------------------------------------- pure helpers


def group_suggestions(suggestions: Iterable[Suggestion]) -> list[tuple[str, list[Suggestion]]]:
    """``[(section title, suggestions)]`` in source order, empty sections left out."""
    groups: dict[str, list[Suggestion]] = {}
    for s in suggestions:
        groups.setdefault(s.source, []).append(s)
    order = [*SOURCES, *sorted(set(groups) - set(SOURCES))]
    return [
        (SOURCE_TITLES.get(src, src.capitalize()), groups[src]) for src in order if src in groups
    ]


def add_message(status: str, term: str, reason: str | None = None) -> str:
    """Status line after teaching one word."""
    if status == "added":
        return f'Added "{term}" - it\'s now first in line for Whisper.'
    if status == "exists":
        return f'"{term}" is already in your vocab.'
    return f"Can't add that: {reason or 'invalid term'}."


def accept_message(results: Sequence[Any]) -> str:
    added = sum(r.status == "added" for r in results)
    exists = sum(r.status == "exists" for r in results)
    bad = [r for r in results if r.status == "invalid"]
    parts = []
    if added:
        parts.append(f"Added {added} word{'s' if added != 1 else ''}")
    if exists:
        parts.append(f"{exists} already in your vocab")
    if bad:
        parts.append(f"{len(bad)} not valid ({bad[0].reason})")
    return (", ".join(parts) or "Nothing to add") + "."


def scan_message(new: int) -> str:
    if not new:
        return "Nothing new in your project folders."
    return f"Found {new} new word{'s' if new != 1 else ''}."


def suggest_from_projects(paths: Paths) -> int:
    """**Suggest from my project folders**: one look through ``[learn] project_roots``, or
    the usual project folders (on a Mac only ones macOS doesn't guard). Returns how many
    new suggestions it found."""
    from shoutabout.learn import refresh_suggestions

    roots = None
    try:
        from shoutabout.config import load_config

        configured = load_config(paths).learn.project_roots
        roots = [Path(r).expanduser() for r in configured] or None
    except Exception:
        log.warning("couldn't read the settings; looking in the usual project folders")
    return refresh_suggestions(paths, include_projects=True, roots=roots)


def open_file(path: Path) -> None:
    """Open a text file in the person's editor."""
    if hasattr(os, "startfile"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/open", "-t", str(path)])
    else:
        raise OSError(f"vocab.txt is at {path}")


# ------------------------------------------------------------------------ the page


class TeachPage:
    """The page's widgets and behaviour. Runs on the UI thread only."""

    def __init__(self, parent: Any, paths: Paths, *, load_in_background: bool = True) -> None:
        import tkinter.font as tkfont

        self.win = parent
        self.paths = paths
        self.closed = False
        self.scale = kit.ui_scale(parent)
        self.fonts = kit.Fonts(tkfont.families(parent))
        self.results: queue.Queue = queue.Queue()
        self.busy = 0
        self.scanning = False
        self.suggestion_vars: list[tuple[Any, Suggestion]] = []
        self.vocab_terms: list[str] = []
        self.build()
        if load_in_background:
            self.reload()
        self.win.after(POLL_MS, self._poll)

    # -------------------------------------------------------------- plumbing

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            self.win.destroy()

    def on_shown(self) -> None:
        """The page came to the front: read the words again (the phone may have taught one)."""
        self.reload()
        with contextlib.suppress(Exception):
            self.entry.focus_set()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            while True:
                callback, result, error = self.results.get_nowait()
                self.busy = max(0, self.busy - 1)
                callback(result, error)
        except queue.Empty:
            pass
        except Exception:
            log.exception("Teach words page update failed")
        if not self.closed:
            self.win.after(POLL_MS, self._poll)

    def run_async(
        self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        """Run ``work`` off the UI thread; ``done(result, error)`` runs back on it. The
        worker never holds a Tk object (freeing one off-thread aborts the process)."""
        self.busy += 1
        results = self.results

        def worker() -> None:
            try:
                result, error = work(), None
            except Exception as exc:  # reported on the page
                log.exception("Teach words action failed")
                result, error = None, exc
            results.put((done, result, error))

        threading.Thread(target=worker, name="teach-worker", daemon=True).start()

    def set_status(self, text: str, error: bool = False) -> None:
        pad = round(8 * self.scale) if error else 0
        self.status.configure(text=text, bg=kit.RED if error else kit.YELLOW, padx=pad)

    # -------------------------------------------------------------- widgets

    def build(self) -> None:
        import tkinter as tk

        s, f = self.scale, self.fonts
        outer = tk.Frame(self.win, bg=kit.YELLOW)
        outer.pack(fill="both", expand=True, padx=round(28 * s), pady=(round(18 * s), 0))

        row = tk.Frame(outer, bg=kit.YELLOW)
        row.pack(fill="x")
        self.entry = tk.Entry(
            row,
            font=f.entry,
            bg=kit.WHITE,
            fg=kit.BLACK,
            insertbackground=kit.BLACK,
            relief="flat",
            highlightthickness=2,
            highlightbackground=kit.BLACK,
            highlightcolor=kit.RED,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=round(4 * s))
        self.entry.bind("<Return>", lambda _e: self.add_word())
        self.entry.bind("<KP_Enter>", lambda _e: self.add_word())
        kit.button(row, "Add word", self.add_word, f).pack(side="left", padx=(round(10 * s), 0))

        self.status = tk.Label(outer, text="", font=f.small_bold, bg=kit.YELLOW, fg=kit.BLACK)
        self.status.configure(anchor="w")
        self.status.pack(fill="x", pady=(round(6 * s), round(10 * s)))

        body = tk.Frame(outer, bg=kit.YELLOW)
        body.pack(fill="both", expand=True, pady=(0, round(18 * s)))
        body.columnconfigure(0, weight=3, uniform="cols")
        body.columnconfigure(1, weight=2, uniform="cols")
        body.rowconfigure(1, weight=1)
        gap = round(14 * s)

        # Suggestions (left)
        head = tk.Frame(body, bg=kit.YELLOW)
        head.grid(row=0, column=0, sticky="ew", padx=(0, gap), pady=(0, round(4 * s)))
        self.sugg_title = tk.Label(
            head, text="SUGGESTIONS", font=f.small_bold, bg=kit.YELLOW, fg=kit.BLACK
        )
        self.sugg_title.pack(side="left", anchor="s")
        self.scan_button = kit.button(head, SCAN_LABEL, self.scan_projects, f, "light")
        self.scan_button.configure(font=f.small_bold, padx=round(10 * s), pady=round(3 * s))
        self.scan_button.pack(side="right")
        frame = tk.Frame(body, bg=kit.PAPER, highlightthickness=2, highlightbackground=kit.BLACK)
        frame.grid(row=1, column=0, sticky="nsew", padx=(0, gap))
        self.canvas = tk.Canvas(frame, bg=kit.PAPER, highlightthickness=0, bd=0)
        scroll = tk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sugg_inner = tk.Frame(self.canvas, bg=kit.PAPER)
        window = self.canvas.create_window((0, 0), window=self.sugg_inner, anchor="nw")
        self.sugg_inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))

        buttons = tk.Frame(body, bg=kit.YELLOW)
        buttons.grid(row=2, column=0, sticky="ew", pady=(round(10 * s), 0), padx=(0, gap))
        kit.button(buttons, "Add selected", self.accept_selected, f).pack(side="left")
        kit.button(buttons, "Dismiss", self.dismiss_selected, f, "light").pack(
            side="left", padx=(round(8 * s), 0)
        )

        # Your words (right)
        self.vocab_title = tk.Label(
            body, text="YOUR WORDS", font=f.small_bold, bg=kit.YELLOW, fg=kit.BLACK, anchor="w"
        )
        self.vocab_title.grid(row=0, column=1, sticky="ew", pady=(0, round(4 * s)))
        vframe = tk.Frame(body, bg=kit.PAPER, highlightthickness=2, highlightbackground=kit.BLACK)
        vframe.grid(row=1, column=1, sticky="nsew")
        vscroll = tk.Scrollbar(vframe, orient="vertical")
        self.vocab_list = tk.Listbox(
            vframe,
            font=f.body,
            bg=kit.PAPER,
            fg=kit.BLACK,
            selectbackground=kit.BLACK,
            selectforeground=kit.YELLOW,
            relief="flat",
            highlightthickness=0,
            activestyle="none",
            selectmode="extended",
            yscrollcommand=vscroll.set,
        )
        vscroll.configure(command=self.vocab_list.yview)
        vscroll.pack(side="right", fill="y")
        self.vocab_list.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.vocab_list.bind("<Delete>", lambda _e: self.remove_selected())
        vbuttons = tk.Frame(body, bg=kit.YELLOW)
        vbuttons.grid(row=2, column=1, sticky="ew", pady=(round(10 * s), 0))
        kit.button(vbuttons, "Remove", self.remove_selected, f, "light").pack(side="left")
        kit.link(vbuttons, "Open vocab.txt", self.open_vocab_file, f).pack(side="right")

        self.set_status("Type a word, then press Enter.")

    def on_wheel(self, event: Any) -> None:
        """Scroll the suggestions while the pointer is over them."""
        if self.closed:
            return
        widget = self.win.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is self.canvas:
                step = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
                self.canvas.yview_scroll(step, "units")
                return
            widget = getattr(widget, "master", None)

    # -------------------------------------------------------------- data

    def reload(self) -> None:
        """Re-read pending suggestions and vocab (off the UI thread)."""
        from shoutabout.learn import pending_suggestions
        from shoutabout.vocab import read_terms

        paths = self.paths

        def work() -> tuple[list[Suggestion], list[str]]:
            return pending_suggestions(paths), read_terms(paths.vocab_file)

        self.run_async(work, self._show_data)

    def _show_data(self, result: Any, error: BaseException | None) -> None:
        if self.closed:
            return
        if error is not None:
            self.set_status(f"Couldn't read your vocab: {error}", error=True)
            return
        suggestions, terms = result
        self._show_suggestions(suggestions)
        self._show_vocab(terms)

    def _show_suggestions(self, suggestions: list[Suggestion]) -> None:
        import tkinter as tk

        s, f = self.scale, self.fonts
        for child in self.sugg_inner.winfo_children():
            child.destroy()
        self.suggestion_vars = []
        self.sugg_title.configure(
            text=f"SUGGESTIONS ({len(suggestions)})" if suggestions else "SUGGESTIONS"
        )
        pad = round(10 * s)
        if not suggestions:
            tk.Label(
                self.sugg_inner, text="None right now.", font=f.body, bg=kit.PAPER, fg=kit.MUTED
            ).pack(anchor="w", padx=pad, pady=pad)
            return
        for item in suggestions:
            var = tk.IntVar(master=self.win, value=0)
            row = tk.Frame(self.sugg_inner, bg=kit.PAPER)
            row.pack(fill="x", padx=pad, pady=(round(4 * s), 0))
            tk.Checkbutton(
                row,
                text=item.term,
                variable=var,
                font=f.body_bold,
                bg=kit.PAPER,
                fg=kit.BLACK,
                activebackground=kit.PAPER,
                selectcolor=kit.WHITE,
                anchor="w",
                cursor="hand2",
            ).pack(side="left")
            tk.Label(
                row, text=item.detail, font=f.small, bg=kit.PAPER, fg=kit.MUTED, anchor="w"
            ).pack(side="left", padx=(round(6 * s), 0))
            self.suggestion_vars.append((var, item))
        tk.Frame(self.sugg_inner, bg=kit.PAPER, height=pad).pack()
        self.canvas.yview_moveto(0)

    def _show_vocab(self, terms: list[str]) -> None:
        self.vocab_terms = list(terms)
        self.vocab_title.configure(text=f"YOUR WORDS ({len(terms)})" if terms else "YOUR WORDS")
        self.vocab_list.delete(0, "end")
        for i, term in enumerate(terms, 1):
            self.vocab_list.insert("end", f"{i:>3}. {term}")

    def _selected_suggestions(self) -> list[str]:
        return [s.term for var, s in self.suggestion_vars if var.get()]

    # -------------------------------------------------------------- actions

    def add_word(self) -> None:
        from shoutabout.learn import accept
        from shoutabout.vocab import VocabTermError, normalize_term

        raw = self.entry.get()
        try:
            term = normalize_term(raw)
        except VocabTermError as exc:
            self.set_status(add_message("invalid", raw, str(exc)), error=True)
            return
        paths = self.paths

        def done(results: Any, error: BaseException | None) -> None:
            if self.closed:
                return
            if error is not None:
                self.set_status(f"Couldn't update vocab.txt: {error}", error=True)
                return
            result = results[0]
            self.set_status(
                add_message(result.status, result.term, result.reason),
                error=result.status == "invalid",
            )
            if result.status != "invalid":
                self.entry.delete(0, "end")
            self.reload()

        self.run_async(lambda: accept(paths, [term]), done)

    def accept_selected(self) -> None:
        from shoutabout.learn import accept

        terms = self._selected_suggestions()
        if not terms:
            self.set_status("Tick the words to add first.")
            return
        paths = self.paths

        def done(results: Any, error: BaseException | None) -> None:
            if error is not None:
                self.set_status(f"Couldn't update vocab.txt: {error}", error=True)
            else:
                self.set_status(accept_message(results))
            self.reload()

        self.run_async(lambda: accept(paths, terms), done)

    def dismiss_selected(self) -> None:
        from shoutabout.learn import dismiss

        terms = self._selected_suggestions()
        if not terms:
            self.set_status("Tick the words to dismiss first.")
            return
        paths = self.paths

        def done(_result: Any, error: BaseException | None) -> None:
            if error is not None:
                self.set_status(f"Couldn't save: {error}", error=True)
            else:
                n = len(terms)
                self.set_status(f"Dismissed {n} word{'s' if n != 1 else ''}; they won't come back.")
            self.reload()

        self.run_async(lambda: dismiss(paths, terms), done)

    def scan_projects(self) -> None:
        """Look through the project folders, once, now."""
        if self.scanning:
            return
        paths = self.paths
        self.scanning = True
        self.scan_button.configure(state="disabled")

        def done(new: Any, error: BaseException | None) -> None:
            self.scanning = False
            if self.closed:
                return
            self.scan_button.configure(state="normal")
            if error is not None:
                self.set_status(f"Couldn't look through your folders: {error}", error=True)
            else:
                self.set_status(scan_message(int(new or 0)))
            self.reload()

        self.set_status("Looking through your project folders…")
        self.run_async(lambda: suggest_from_projects(paths), done)

    def remove_selected(self) -> None:
        from shoutabout.vocab import remove_term

        picked = [self.vocab_terms[i] for i in self.vocab_list.curselection()]
        if not picked:
            self.set_status("Select words to remove.")
            return
        paths = self.paths

        def done(_result: Any, error: BaseException | None) -> None:
            if error is not None:
                self.set_status(f"Couldn't update vocab.txt: {error}", error=True)
            elif len(picked) == 1:
                self.set_status(f'Removed "{picked[0]}".')
            else:
                self.set_status(f"Removed {len(picked)} words.")
            self.reload()

        self.run_async(lambda: [remove_term(paths.vocab_file, t) for t in picked], done)

    def open_vocab_file(self) -> None:
        try:
            open_file(self.paths.vocab_file)
        except OSError as exc:
            self.set_status(str(exc) or "Couldn't open vocab.txt.", error=True)
