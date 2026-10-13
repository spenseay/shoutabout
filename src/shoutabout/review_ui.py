"""The Teach words page of the ShoutAbout window (Tk): your words and replacements.

* a box to add a word (Enter adds it, at the top of vocab.txt), and an optional "When I
  say" box that makes it a replacement instead ("omw" -> "on my way", "slash
  consolidate" -> "/consolidate");
* your words and replacements in priority order (Remove, Open vocab.txt).

A word that came out wrong is usually fixed from Recent instead (**Fix a word** on the
dictation, :mod:`shoutabout.recent_ui`).

:class:`TeachPage` draws into a frame of :class:`shoutabout.app_window.AppWindow`, on the
UI thread; slow work (the vocab.txt lock) runs on short worker threads that hand results
back through a queue. ``tkinter`` is imported lazily, so importing this module
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.config import Paths
from shoutabout.vocab import ARROW, split_replacement

log = logging.getLogger(__name__)

POLL_MS = 120


# ------------------------------------------------------------------- pure helpers


def add_message(status: str, term: str, reason: str | None = None) -> str:
    """Status line after teaching one word or replacement."""
    pair = split_replacement(term) if status != "invalid" else None
    if pair:
        say, write = pair
        if status == "added":
            return f'Added. Saying "{say}" now writes "{write}".'
        return f'"{say}" already has a replacement: {display_entry(term)}.'
    if status == "added":
        return f'Added "{term}". ShoutAbout will spell it that way.'
    if status == "exists":
        return f'"{term}" is already in your words.'
    return f"Can't add that: {reason or 'invalid term'}."


def display_entry(entry: str) -> str:
    """How a vocab.txt line shows in the list: "omw → on my way" for a replacement."""
    pair = split_replacement(entry)
    return f"{pair[0]} \u2192 {pair[1]}" if pair else entry


def compose_entry(word: str, say: str) -> str:
    """What the two boxes add: the word, or "say -> word" when "When I say" is filled in."""
    return f"{say.strip()} {ARROW} {word.strip()}" if say.strip() else word


def read_words(paths: Paths) -> list[str]:
    """Vocab words and replacements in priority order."""
    from shoutabout.vocab import read_entries

    return read_entries(paths.vocab_file)


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
        self._pending: dict[int, Callable[[Any, BaseException | None], None]] = {}
        self._job = 0
        self._jobs: dict[str, Any] = {}  # pending after() jobs
        self.vocab_terms: list[str] = []
        self.build()
        if load_in_background:
            self.reload()
        else:
            self._show_data(read_words(paths), None)
        self._jobs["poll"] = self.win.after(POLL_MS, self._poll)

    # -------------------------------------------------------------- plumbing

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._pending.clear()
        kit.cancel_jobs(self.win, self._jobs)
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
                job, result, error = self.results.get_nowait()
                done = self._pending.pop(job, None)
                if done is not None:
                    done(result, error)
        except queue.Empty:
            pass
        except Exception:
            log.exception("Teach words page update failed")
        if not self.closed:
            self._jobs["poll"] = self.win.after(POLL_MS, self._poll)

    def run_async(
        self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        """Run ``work`` off the UI thread; ``done(result, error)`` runs back on it. The
        worker holds only ``work`` and the queue, never this page or a widget: freeing a
        Tk object off its thread aborts the process."""
        self._job += 1
        job = self._job
        self._pending[job] = done
        results = self.results

        def worker() -> None:
            try:
                result, error = work(), None
            except Exception as exc:  # reported on the page
                log.exception("Teach words action failed")
                result, error = None, exc
            results.put((job, result, error))

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

        def entry_box(parent: Any, font: Any) -> Any:
            box = tk.Entry(
                parent,
                font=font,
                bg=kit.WHITE,
                fg=kit.BLACK,
                insertbackground=kit.BLACK,
                relief="flat",
                highlightthickness=2,
                highlightbackground=kit.BLACK,
                highlightcolor=kit.RED,
            )
            box.bind("<Return>", lambda _e: self.add_word())
            box.bind("<KP_Enter>", lambda _e: self.add_word())
            return box

        def box_label(parent: Any, text: str) -> None:
            label = tk.Label(parent, text=text, font=f.small_bold, bg=kit.YELLOW, fg=kit.BLACK)
            label.configure(width=11, anchor="w")
            label.pack(side="left")

        row = tk.Frame(outer, bg=kit.YELLOW)
        row.pack(fill="x")
        box_label(row, "WORD")
        self.entry = entry_box(row, f.entry)
        self.entry.pack(side="left", fill="x", expand=True, ipady=round(4 * s))
        kit.button(row, "Add", self.add_word, f).pack(side="left", padx=(round(10 * s), 0))

        # Optional: what you say, when it should turn into the word above.
        say_row = tk.Frame(outer, bg=kit.YELLOW)
        say_row.pack(fill="x", pady=(round(6 * s), 0))
        box_label(say_row, "WHEN I SAY")
        self.say_entry = entry_box(say_row, f.body)
        self.say_entry.configure(width=22)
        self.say_entry.pack(side="left", ipady=round(2 * s))
        tk.Label(
            say_row,
            text="optional: write the word instead, like \u201comw\u201d \u2192 on my way",
            font=f.small,
            bg=kit.YELLOW,
            fg=kit.MUTED,
        ).pack(side="left", padx=(round(8 * s), 0))

        self.status = tk.Label(outer, text="", font=f.small_bold, bg=kit.YELLOW, fg=kit.BLACK)
        self.status.configure(anchor="w")
        self.status.pack(fill="x", pady=(round(6 * s), round(10 * s)))

        self.vocab_title = tk.Label(
            outer, text="YOUR WORDS", font=f.small_bold, bg=kit.YELLOW, fg=kit.BLACK, anchor="w"
        )
        self.vocab_title.pack(fill="x", pady=(0, round(4 * s)))
        vbuttons = tk.Frame(outer, bg=kit.YELLOW)
        vbuttons.pack(side="bottom", fill="x", pady=(round(10 * s), round(18 * s)))
        kit.button(vbuttons, "Remove", self.remove_selected, f, "light").pack(side="left")
        kit.link(vbuttons, "Open vocab.txt", self.open_vocab_file, f).pack(side="right")
        vframe = tk.Frame(outer, bg=kit.PAPER, highlightthickness=2, highlightbackground=kit.BLACK)
        vframe.pack(fill="both", expand=True)
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

        self.set_status("Type a word the way it should be spelled, then press Enter.")

    # -------------------------------------------------------------- data

    def reload(self) -> None:
        """Re-read vocab.txt (off the UI thread; the worker gets only the paths, never this
        page)."""
        paths = self.paths
        self.run_async(lambda: read_words(paths), self._show_data)

    def _show_data(self, result: Any, error: BaseException | None) -> None:
        if self.closed:
            return
        if error is not None:
            self.set_status(f"Couldn't read your words: {error}", error=True)
            return
        self._show_vocab(result)

    def _show_vocab(self, terms: list[str]) -> None:
        self.vocab_terms = list(terms)
        self.vocab_title.configure(text=f"YOUR WORDS ({len(terms)})" if terms else "YOUR WORDS")
        self.vocab_list.delete(0, "end")
        for i, term in enumerate(terms, 1):
            self.vocab_list.insert("end", f"{i:>3}. {display_entry(term)}")

    # -------------------------------------------------------------- actions

    def add_word(self) -> None:
        from shoutabout.vocab import VocabTermError, add_terms, normalize_term

        word, say = self.entry.get(), self.say_entry.get()
        if say.strip() and not word.strip():
            self.set_status("Type what it should write in the top box.", error=True)
            return
        raw = compose_entry(word, say)
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
                self.say_entry.delete(0, "end")
            self.reload()

        self.run_async(lambda: add_terms(paths.vocab_file, [term]), done)

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
                self.set_status(f'Removed "{display_entry(picked[0])}".')
            else:
                self.set_status(f"Removed {len(picked)} words.")
            self.reload()

        self.run_async(lambda: [remove_term(paths.vocab_file, t) for t in picked], done)

    def open_vocab_file(self) -> None:
        try:
            open_file(self.paths.vocab_file)
        except OSError as exc:
            self.set_status(str(exc) or "Couldn't open vocab.txt.", error=True)
