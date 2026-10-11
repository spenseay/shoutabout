"""The "Teach ShoutAbout" window: a small Tk (stdlib) UI to teach words and review suggestions.

* :func:`open_review_window` - the full window: add a word, pending suggestions grouped by
  source (checkboxes; Add selected / Dismiss selected / Rescan) and the current vocab
  (in priority order, with Remove).
* :func:`open_add_word_dialog` - just the "add a word" box (it can grow into the full window).

Threading: both return at once. The Tk root is created and its mainloop run on a dedicated
"teach-ui" thread (never the tray thread), and all Tk objects are created, used and freed
on that thread only; slow work (file locks, rescans) runs on short worker threads that hand
results back through a queue. Only one window exists at a time: opening it again brings the
existing one to the front. ``tkinter`` is imported lazily, so importing this module works
headless.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import queue
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shoutabout.config import Paths
from shoutabout.learn import SOURCES, Suggestion

log = logging.getLogger(__name__)

WINDOW_TITLE = "Teach ShoutAbout"
POLL_MS = 120

# Brand palette.
SHOUT_YELLOW = "#FFE600"  # ground
BLACK = "#0A0A0A"  # text
TONGUE_RED = "#FF1A1A"  # accents
PAPER = "#FFF8C4"  # list backgrounds: a lighter yellow
MUTED = "#4A4A4A"  # secondary text (7:1 on the yellow ground)
WHITE = "#FFFFFF"

TITLE_FONTS = ("Anton", "Archivo Black", "Segoe UI Black", "Arial Black", "Segoe UI", "Arial")
BODY_FONTS = ("Archivo", "Segoe UI", "Helvetica Neue", "Helvetica", "Arial")

SOURCE_TITLES = {"history": "From your dictations", "projects": "From your projects"}

Mode = str  # "review" | "add"


# ------------------------------------------------------------------- pure helpers


def pick_font(available: Iterable[str], preferred: Sequence[str]) -> str:
    """The first of ``preferred`` that is installed (case-insensitive), else Tk's default."""
    installed = {name.casefold(): name for name in available}
    for name in preferred:
        if name.casefold() in installed:
            return installed[name.casefold()]
    return "TkDefaultFont"


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


def rescan_suggestions(paths: Paths) -> int:
    """Rescan: refresh the suggestions with the owner's settings. Dictations are mined only
    while ``[history] enabled`` is on; project folders as ``[learn]`` says."""
    from shoutabout.learn import refresh_suggestions

    include_projects, include_history, roots = True, False, None
    try:
        from shoutabout.config import load_config

        cfg = load_config(paths)
        include_projects = cfg.learn.seed_projects
        include_history = cfg.history.enabled
        roots = [Path(r).expanduser() for r in cfg.learn.project_roots] or None
    except Exception:
        log.warning("couldn't read the settings; rescanning project folders only")
    return refresh_suggestions(
        paths, include_projects=include_projects, include_history=include_history, roots=roots
    )


# ------------------------------------------------------------------- single window


@dataclass
class _Handle:
    requests: queue.Queue = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    hwnd: int = 0  # the window's frame (Windows), to wake its event loop

    def send(self, kind: str, arg: Any = None) -> None:
        """Queue a request for the Tk thread and nudge its event loop (any thread)."""
        self.requests.put((kind, arg))
        if self.hwnd and sys.platform == "win32":
            with contextlib.suppress(Exception):
                import ctypes

                ctypes.windll.user32.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL


_lock = threading.Lock()
_active: _Handle | None = None


def _start(paths: Paths, mode: Mode) -> threading.Thread:
    global _active
    with _lock:
        if _active is not None and _active.thread is not None and _active.thread.is_alive():
            _active.send("focus", mode)
            return _active.thread
        handle = _Handle()
        thread = threading.Thread(
            target=_thread_main, args=(paths, mode, handle), name="teach-ui", daemon=True
        )
        handle.thread = thread
        _active = handle
    thread.start()
    return thread


def _thread_main(paths: Paths, mode: Mode, handle: _Handle) -> None:
    global _active
    try:
        _run_tk(paths, mode, handle)
    except Exception:
        log.exception("the Teach ShoutAbout window failed")
    finally:
        with _lock:
            if _active is handle:
                _active = None


def open_review_window(paths: Paths) -> threading.Thread:
    """Show the Teach ShoutAbout window (or bring it to the front). Returns immediately;
    safe to call from any thread (e.g. a tray menu action)."""
    return _start(paths, "review")


def open_add_word_dialog(paths: Paths) -> threading.Thread:
    """Show the small "teach a word" box (or bring the open window to the front)."""
    return _start(paths, "add")


def run_review_window(paths: Paths, mode: Mode = "review") -> None:
    """Blocking variant for the CLI: open the window and wait until it is closed."""
    thread = _start(paths, mode)
    while thread.is_alive():
        thread.join(0.25)  # short joins keep Ctrl+C responsive


def is_open() -> bool:
    with _lock:
        return bool(_active and _active.thread and _active.thread.is_alive())


def close_window(timeout: float = 5.0) -> None:
    """Close the window if it is open (any thread) and wait up to ``timeout`` for it."""
    with _lock:
        handle = _active
    if handle is None or handle.thread is None:
        return
    handle.send("close")
    if handle.thread is not threading.current_thread():
        handle.thread.join(timeout)


# ------------------------------------------------------------------------ the app


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        # Per-monitor v2; harmless (fails) if the process already chose an awareness.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # type: ignore[attr-defined]


def _brand_photo(master: Any, size: int) -> Any:
    """The brand mouth (app icon art) as a Tk PhotoImage."""
    import base64
    import tkinter as tk

    from shoutabout import brand

    image = brand.app_icon("primary", size, rounded=True, samples=4)
    data = brand.png_bytes(image.width, image.height, image.rgba)
    return tk.PhotoImage(master=master, data=base64.b64encode(data))


def _run_tk(paths: Paths, mode: Mode, handle: _Handle) -> None:
    _enable_dpi_awareness()
    import tkinter as tk

    root = tk.Tk()
    app: TeachApp | None = TeachApp(root, paths, mode, handle.requests)
    with contextlib.suppress(Exception):
        handle.hwnd = int(root.wm_frame(), 16)
    try:
        root.mainloop()
    finally:
        if app is not None:
            app.closed = True
        with contextlib.suppress(Exception):
            root.destroy()
        # Free every Tk object on this thread: a Tcl interpreter or variable deleted from
        # another thread aborts the process ("Tcl_AsyncDelete: async handler deleted by the
        # wrong thread").
        app = None
        root = None  # type: ignore[assignment]
        gc.collect()


class TeachApp:
    """The window's widgets and behaviour. Runs on the Tk thread only."""

    def __init__(self, root: Any, paths: Paths, mode: Mode, requests: queue.Queue) -> None:
        import tkinter.font as tkfont

        self.root = root
        self.paths = paths
        self.mode = mode
        self.requests = requests
        self.results: queue.Queue = queue.Queue()
        self.closed = False
        self.busy = 0
        self.suggestion_vars: list[tuple[Any, Suggestion]] = []
        self.vocab_terms: list[str] = []

        families = tkfont.families(root)
        title_family = pick_font(families, TITLE_FONTS)
        body_family = pick_font(families, BODY_FONTS)
        bold_title = title_family not in ("Anton", "Archivo Black", "Segoe UI Black")
        self.f_title = (title_family, 24, "bold") if bold_title else (title_family, 24)
        self.f_heading = (body_family, 11, "bold")
        self.f_body = (body_family, 10)
        self.f_term = (body_family, 11, "bold")
        self.f_small = (body_family, 9)
        self.f_entry = (body_family, 13)

        self.logo: Any = None
        try:  # cosmetic: never let artwork stop the window from opening
            self.logo = _brand_photo(root, 44)
            root.iconphoto(True, self.logo)
        except Exception:
            log.debug("couldn't render the brand icon", exc_info=True)

        root.title(WINDOW_TITLE)
        root.configure(bg=SHOUT_YELLOW)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _e: self.close())
        self.build(mode)
        root.after(POLL_MS, self._poll)
        self.bring_to_front()

    # -------------------------------------------------------------- plumbing

    def close(self) -> None:
        self.closed = True
        self.root.destroy()

    def bring_to_front(self) -> None:
        root = self.root
        with contextlib.suppress(Exception):
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
            root.focus_force()
            self.entry.focus_set()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            while True:
                kind, arg = self.requests.get_nowait()
                if kind == "close":
                    self.close()
                    return
                if kind == "focus":
                    if arg == "review" and self.mode != "review":
                        self.build("review")
                    self.bring_to_front()
        except queue.Empty:
            pass
        try:
            while True:
                callback, result, error = self.results.get_nowait()
                self.busy = max(0, self.busy - 1)
                callback(result, error)
        except queue.Empty:
            pass
        except Exception:
            log.exception("Teach ShoutAbout window update failed")
        if not self.closed:
            self.root.after(POLL_MS, self._poll)

    def run_async(
        self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        """Run ``work`` off the Tk thread; ``done(result, error)`` runs back on it."""
        self.busy += 1

        def worker() -> None:
            try:
                result, error = work(), None
            except Exception as exc:  # reported in the window
                log.exception("Teach ShoutAbout action failed")
                result, error = None, exc
            self.results.put((done, result, error))

        threading.Thread(target=worker, name="teach-ui-worker", daemon=True).start()

    def set_status(self, text: str, error: bool = False) -> None:
        if error:
            self.status.configure(text=text, bg=TONGUE_RED, fg=BLACK, padx=8)
        else:
            self.status.configure(text=text, bg=SHOUT_YELLOW, fg=BLACK, padx=0)

    # -------------------------------------------------------------- widgets

    def _button(self, parent: Any, text: str, command: Callable[[], None], kind: str = "dark"):
        import tkinter as tk

        colors = {
            "dark": (BLACK, SHOUT_YELLOW, "#333333"),
            "red": (TONGUE_RED, BLACK, "#FF4D4D"),
            "light": (PAPER, BLACK, WHITE),
        }[kind]
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=colors[0],
            fg=colors[1],
            activebackground=colors[2],
            activeforeground=colors[1],
            font=self.f_heading,
            relief="flat",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
            highlightthickness=0,
        )

    def build(self, mode: Mode) -> None:
        import tkinter as tk

        self.mode = mode
        for child in self.root.winfo_children():
            child.destroy()
        self.suggestion_vars = []
        root = self.root
        # Layout sizes are 96-dpi pixels scaled to the screen (fonts scale by themselves).
        scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        outer = tk.Frame(root, bg=SHOUT_YELLOW, padx=22, pady=18)
        outer.pack(fill="both", expand=True)

        head = tk.Frame(outer, bg=SHOUT_YELLOW)
        head.pack(anchor="w")
        if self.logo is not None:
            tk.Label(head, image=self.logo, bg=SHOUT_YELLOW).pack(side="left", padx=(0, 10))
        tk.Label(head, text="TEACH SHOUTABOUT", font=self.f_title, bg=SHOUT_YELLOW, fg=BLACK).pack(
            side="left"
        )
        tk.Label(
            outer,
            text="Words you say often. Taught words go to the top of your vocab, "
            "so Whisper hears them first.",
            font=self.f_body,
            bg=SHOUT_YELLOW,
            fg=BLACK,
            justify="left",
            wraplength=int((420 if mode == "add" else 760) * scale),
        ).pack(anchor="w", pady=(2, 10))
        tk.Frame(outer, bg=TONGUE_RED, height=4).pack(fill="x", pady=(0, 12))

        row = tk.Frame(outer, bg=SHOUT_YELLOW)
        row.pack(fill="x")
        self.entry = tk.Entry(
            row,
            font=self.f_entry,
            bg=WHITE,
            fg=BLACK,
            insertbackground=BLACK,
            relief="flat",
            highlightthickness=2,
            highlightbackground=BLACK,
            highlightcolor=TONGUE_RED,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda _e: self.add_word())
        self.entry.bind("<KP_Enter>", lambda _e: self.add_word())
        self._button(row, "Add word", self.add_word).pack(side="left", padx=(10, 0))

        self.status = tk.Label(
            outer, text="", font=self.f_body, bg=SHOUT_YELLOW, fg=BLACK, anchor="w"
        )
        self.status.pack(fill="x", pady=(6, 8))

        if mode == "add":
            root.minsize(460, 0)
            foot = tk.Frame(outer, bg=SHOUT_YELLOW)
            foot.pack(fill="x")
            self._button(foot, "Review suggestions…", lambda: self.build("review"), "light").pack(
                side="left"
            )
            self._button(foot, "Close", self.close, "light").pack(side="right")
            self.set_status("Type a name or term the way it should be written, then press Enter.")
            self.entry.focus_set()
            return

        # Kept on the screen (768-px laptops).
        width = min(int(920 * scale), root.winfo_screenwidth() - 40)
        height = min(int(780 * scale), root.winfo_screenheight() - 90)
        root.minsize(min(int(760 * scale), width), min(int(600 * scale), height))
        root.geometry(f"{width}x{height}")
        body = tk.Frame(outer, bg=SHOUT_YELLOW)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3, uniform="cols")
        body.columnconfigure(1, weight=2, uniform="cols")
        body.rowconfigure(1, weight=1)

        # Suggestions (left)
        self.sugg_title = tk.Label(
            body, text="SUGGESTIONS", font=self.f_heading, bg=SHOUT_YELLOW, fg=BLACK, anchor="w"
        )
        self.sugg_title.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        frame = tk.Frame(body, bg=PAPER, highlightthickness=2, highlightbackground=BLACK)
        frame.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        self.canvas = tk.Canvas(frame, bg=PAPER, highlightthickness=0, bd=0)
        scroll = tk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sugg_inner = tk.Frame(self.canvas, bg=PAPER)
        window = self.canvas.create_window((0, 0), window=self.sugg_inner, anchor="nw")
        self.sugg_inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        root.bind_all("<MouseWheel>", self._on_wheel)

        buttons = tk.Frame(body, bg=SHOUT_YELLOW)
        buttons.grid(row=2, column=0, sticky="ew", pady=(10, 0), padx=(0, 14))
        self._button(buttons, "Add selected", self.accept_selected).pack(side="left")
        self._button(buttons, "Dismiss selected", self.dismiss_selected, "light").pack(
            side="left", padx=(8, 0)
        )
        self._button(buttons, "Rescan", self.rescan, "light").pack(side="right", padx=(8, 0))

        # Vocab (right)
        self.vocab_title = tk.Label(
            body, text="YOUR VOCAB", font=self.f_heading, bg=SHOUT_YELLOW, fg=BLACK, anchor="w"
        )
        self.vocab_title.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        vframe = tk.Frame(body, bg=PAPER, highlightthickness=2, highlightbackground=BLACK)
        vframe.grid(row=1, column=1, sticky="nsew")
        vscroll = tk.Scrollbar(vframe, orient="vertical")
        self.vocab_list = tk.Listbox(
            vframe,
            font=self.f_body,
            bg=PAPER,
            fg=BLACK,
            selectbackground=BLACK,
            selectforeground=SHOUT_YELLOW,
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
        vbuttons = tk.Frame(body, bg=SHOUT_YELLOW)
        vbuttons.grid(row=2, column=1, sticky="ew", pady=(10, 0))
        self._button(vbuttons, "Remove", self.remove_selected, "red").pack(side="left")
        self._button(vbuttons, "Open vocab.txt", self.open_vocab_file, "light").pack(side="right")

        tk.Label(
            outer,
            text="Suggestions come from your dictation history and project folders. "
            "They never leave this PC, and nothing is added until you say so.",
            font=self.f_small,
            bg=SHOUT_YELLOW,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=int(860 * scale),
        ).pack(fill="x", pady=(12, 0))

        self.set_status("Type a word and press Enter, or pick suggestions below.")
        self.reload()

    def _on_wheel(self, event: Any) -> None:
        if self.mode != "review" or self.closed:
            return
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is self.canvas:
                self.canvas.yview_scroll(
                    int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units"
                )
                return
            widget = getattr(widget, "master", None)

    # -------------------------------------------------------------- data

    def reload(self) -> None:
        """Re-read pending suggestions and vocab (off the Tk thread)."""
        from shoutabout.learn import pending_suggestions
        from shoutabout.vocab import read_terms

        paths = self.paths

        def work() -> tuple[list[Suggestion], list[str]]:
            return pending_suggestions(paths), read_terms(paths.vocab_file)

        self.run_async(work, self._show_data)

    def _show_data(self, result: Any, error: BaseException | None) -> None:
        if self.closed or self.mode != "review":
            return
        if error is not None:
            self.set_status(f"Couldn't read your vocab: {error}", error=True)
            return
        suggestions, terms = result
        self._show_suggestions(suggestions)
        self._show_vocab(terms)

    def _show_suggestions(self, suggestions: list[Suggestion]) -> None:
        import tkinter as tk

        for child in self.sugg_inner.winfo_children():
            child.destroy()
        self.suggestion_vars = []
        self.sugg_title.configure(text=f"SUGGESTIONS ({len(suggestions)})")
        if not suggestions:
            tk.Label(
                self.sugg_inner,
                text="No suggestions right now.\nKeep dictating, or press Rescan.",
                font=self.f_body,
                bg=PAPER,
                fg=MUTED,
                justify="left",
            ).pack(anchor="w", padx=12, pady=12)
            return
        for title, items in group_suggestions(suggestions):
            header = tk.Frame(self.sugg_inner, bg=PAPER)
            header.pack(fill="x", padx=10, pady=(10, 2))
            tk.Frame(header, bg=TONGUE_RED, width=6, height=16).pack(side="left", padx=(0, 6))
            tk.Label(header, text=title.upper(), font=self.f_heading, bg=PAPER, fg=BLACK).pack(
                side="left"
            )
            for s in items:
                var = tk.IntVar(master=self.root, value=0)
                row = tk.Frame(self.sugg_inner, bg=PAPER)
                row.pack(fill="x", padx=10, pady=1)
                tk.Checkbutton(
                    row,
                    text=s.term,
                    variable=var,
                    font=self.f_term,
                    bg=PAPER,
                    fg=BLACK,
                    activebackground=PAPER,
                    selectcolor=WHITE,
                    anchor="w",
                    cursor="hand2",
                ).pack(side="left")
                tk.Label(
                    row, text=s.detail, font=self.f_small, bg=PAPER, fg=MUTED, anchor="w"
                ).pack(side="left", padx=(6, 0))
                self.suggestion_vars.append((var, s))
        self.canvas.yview_moveto(0)

    def _show_vocab(self, terms: list[str]) -> None:
        self.vocab_terms = list(terms)
        self.vocab_title.configure(text=f"YOUR VOCAB ({len(terms)})")
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
            if self.mode == "review":
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

    def rescan(self) -> None:
        paths = self.paths

        def done(new: Any, error: BaseException | None) -> None:
            if error is not None:
                self.set_status(f"Rescan failed: {error}", error=True)
            else:
                self.set_status(
                    f"Rescanned: {new} new suggestion{'s' if new != 1 else ''}."
                    if new
                    else "Rescanned: nothing new."
                )
            self.reload()

        self.set_status("Scanning your dictations and projects…")
        self.run_async(lambda: rescan_suggestions(paths), done)

    def remove_selected(self) -> None:
        from shoutabout.vocab import remove_term

        picked = [self.vocab_terms[i] for i in self.vocab_list.curselection()]
        if not picked:
            self.set_status("Select words in your vocab to remove.")
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
        path = self.paths.vocab_file
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                self.set_status(f"vocab.txt is at {path}")
        except OSError as exc:
            self.set_status(f"Couldn't open vocab.txt: {exc}", error=True)
