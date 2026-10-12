"""The ShoutAbout window (Tk): one window with Home, Teach words, Recent and Settings.

A row of tabs at the top switches pages. Each page is built the first time it's shown and
kept while the window is open (:class:`~shoutabout.review_ui.TeachPage`,
:class:`~shoutabout.recent_ui.RecentPage`, :class:`~shoutabout.settings_ui.SettingsPage`);
coming back to one refreshes it. Home is tiny: the mouth, how to talk, and at most one line
when something needs attention (:func:`home_view`).

The tray / menu bar items, opening ShoutAbout again while it runs, and the CLI all open this
same window on a page (:meth:`shoutabout.ui_host.UiHost.open_window`); there is never a
second copy. The setup window and the small pairing prompt stay separate windows.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shoutabout import ui_kit as kit
from shoutabout.app_services import AppServices, dictation_view, talk_instruction

log = logging.getLogger(__name__)

WINDOW_TITLE = "ShoutAbout"
PAGES = {"home": "Home", "teach": "Teach words", "recent": "Recent", "settings": "Settings"}
ALIASES = {"history": "recent", "vocab": "teach"}  # older names for the same pages
BASE_W, BASE_H = 760, 800
POLL_MS = 120
HOME_REFRESH_MS = 1000

NO_MIC_NOTE = "No microphone found."
# A permission macOS hasn't given (Home shows the line with an "Allow" button: it opens the
# setup's "Allow access" screen).
PERMISSION_NOTES = {
    "microphone": "The microphone isn't allowed.",
    "accessibility": "Pasting isn't allowed (Accessibility).",
    "input_monitoring": "The shortcut key isn't allowed (Input Monitoring).",
}
ALLOW_BUTTON = "Allow"
ERROR_MAX = 90


def page_name(page: str | None) -> str:
    """A known page for ``page`` (Home for anything unknown)."""
    name = str(page or "home").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in PAGES else "home"


# ----------------------------------------------------------------------- Home logic


@dataclass(frozen=True)
class HomeView:
    talk: str  # "Press Right Ctrl, talk, press it again."
    note: str = ""  # at most one line when something needs attention
    urgent: bool = False  # the note is a problem (drawn in red), not just news
    busy: bool = False  # getting ready, listening or writing: the mouth talks
    allow: bool = False  # the note is a missing macOS permission: "Allow" opens Allow access


def device_word(platform: str | None = None) -> str:
    return "Mac" if (platform or sys.platform) == "darwin" else "PC"


def attention(
    status: dict[str, Any] | None,
    *,
    dictation_on: bool,
    mic: bool | None,
    platform: str | None = None,
) -> tuple[str, bool]:
    """``(line, urgent)``: the one thing worth saying on Home, most important first: the
    hub failed, a missing macOS permission, no microphone, talking is off, still getting
    ready, paused. ``("", False)`` when all is well."""
    status = status or {}
    phase = str(status.get("phase") or "")
    error = str(status.get("error") or "").strip()
    if phase == "error" and error:
        first = error.splitlines()[0]
        return (first if len(first) <= ERROR_MAX else first[: ERROR_MAX - 1] + "…"), True
    for missing in status.get("missing") or []:
        if missing in PERMISSION_NOTES:
            return PERMISSION_NOTES[missing], True
    if mic is False:
        return NO_MIC_NOTE, True
    if not dictation_on:
        return f"Talk to this {device_word(platform)} is off.", False
    if phase in ("starting", "preparing"):
        percent = status.get("percent")
        if isinstance(percent, int) and 0 <= percent <= 100:
            return f"Getting ready… {percent}%", False
        return "Getting ready…", False
    if status.get("paused"):
        return "Paused.", False
    return "", False


def home_view(svc: AppServices) -> HomeView:
    """What Home shows now (call off the UI thread: on a Mac it asks the hub)."""
    cfg = svc.config()
    dv = dictation_view(cfg)
    try:
        label = svc.hotkey_choices().get(dv.hotkey, dv.hotkey)
    except Exception:
        label = dv.hotkey
    try:
        status = svc.hub_status()
    except Exception:
        log.debug("couldn't ask the hub how it is", exc_info=True)
        status = None
    try:
        mic = svc.has_microphone()
    except Exception:
        mic = None
    note, urgent = attention(status, dictation_on=dv.enabled, mic=mic)
    phase = str((status or {}).get("phase") or "")
    return HomeView(
        talk=talk_instruction(label, dv.mode),
        note=note,
        urgent=urgent,
        busy=phase in ("starting", "preparing", "listening", "transcribing"),
        allow=urgent and note in PERMISSION_NOTES.values(),
    )


# ------------------------------------------------------------------------ Home page


class HomePage:
    def __init__(
        self,
        parent: Any,
        services: AppServices,
        *,
        load_in_background: bool = True,
        on_allow: Callable[[], None] | None = None,
    ):
        import tkinter as tk
        import tkinter.font as tkfont

        self.win = parent
        self.svc = services
        self.on_allow = on_allow
        self.closed = False
        self.visible = True
        self.scale = kit.ui_scale(parent)
        self.fonts = kit.Fonts(tkfont.families(parent))
        self.results: queue.Queue = queue.Queue()
        self._asking = False
        self._jobs: dict[str, Any] = {}  # pending after() jobs
        self.view: HomeView | None = None
        s, f = self.scale, self.fonts

        middle = tk.Frame(parent, bg=kit.YELLOW)
        middle.place(relx=0.5, rely=0.42, anchor="center")
        self.mouth = kit.Mouth(middle, round(190 * s)).pack(pady=(0, round(26 * s)))
        self.talk = tk.Label(middle, text="", font=f.big, bg=kit.YELLOW, fg=kit.BLACK)
        self.talk.pack()
        line = tk.Frame(middle, bg=kit.YELLOW)
        line.pack(pady=(round(14 * s), 0))
        self.note = tk.Label(line, text="", font=f.small_bold, bg=kit.YELLOW, fg=kit.BLACK)
        self.note.pack(side="left")
        self.note.bind("<Button-1>", self.clicked_line)
        # A missing macOS permission: "Allow" opens the setup's "Allow access" screen.
        self.allow_button = kit.button(line, ALLOW_BUTTON, self.allow, f)
        if load_in_background:
            self.refresh()
        else:
            self.render(home_view(services))
        self._jobs["poll"] = self.win.after(POLL_MS, self._poll)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.mouth.stop()
        kit.cancel_jobs(self.win, self._jobs)
        with contextlib.suppress(Exception):
            self.win.destroy()

    def on_shown(self) -> None:
        self.visible = True
        self.refresh()

    def on_hidden(self) -> None:
        self.visible = False

    def refresh(self) -> None:
        """Ask how things are (off the UI thread); the answer comes back through _poll."""
        if self.closed or self._asking:
            return
        self._asking = True
        svc, results = self.svc, self.results

        def worker() -> None:
            try:
                results.put(home_view(svc))
            except Exception:
                log.exception("couldn't work out Home")
                results.put(None)

        threading.Thread(target=worker, name="home-worker", daemon=True).start()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            view = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._asking = False
            if view is not None:
                self.render(view)
            if self.visible:  # one timer at a time: showing the page again doesn't add one
                if "next" in self._jobs:
                    self.win.after_cancel(self._jobs["next"])
                self._jobs["next"] = self.win.after(HOME_REFRESH_MS, self._tick)
        self._jobs["poll"] = self.win.after(POLL_MS, self._poll)

    def _tick(self) -> None:
        self._jobs.pop("next", None)
        if self.visible:
            self.refresh()

    def _allowing(self) -> bool:
        return self.on_allow is not None and self.view is not None and self.view.allow

    def clicked_line(self, _event: Any = None) -> None:
        if self._allowing():  # a missing permission: the line works like its button
            self.allow()

    def allow(self) -> None:
        """Home's "Allow" (or a click on the line): the setup's "Allow access" screen."""
        if self.on_allow is None:
            return
        try:
            self.on_allow()
        except Exception:
            log.exception("couldn't open Allow access")

    def render(self, view: HomeView) -> None:
        if self.closed:
            return
        if view == self.view:
            return
        self.view = view
        self.talk.configure(text=view.talk)
        pad = round(10 * self.scale) if view.urgent and view.note else 0
        allowing = self._allowing()
        self.note.configure(
            text=view.note,
            bg=kit.RED if view.urgent and view.note else kit.YELLOW,
            fg=kit.BLACK if view.urgent else kit.MUTED,
            padx=pad,
            pady=round(4 * self.scale) if pad else 0,
            cursor="hand2" if allowing else "",
        )
        if allowing:
            self.allow_button.pack(side="left", fill="y", padx=(round(8 * self.scale), 0))
        else:
            self.allow_button.pack_forget()
        if view.busy:
            self.mouth.talk()
        else:
            self.mouth.stop(open_mouth=True)  # the brand mouth, at rest


# ----------------------------------------------------------------------- the window


class AppWindow:
    def __init__(
        self,
        window: Any,
        services: AppServices,
        *,
        page: str = "home",
        on_close: Callable[[], None] | None = None,
        on_add_phone: Callable[[], None] | None = None,
        on_allow_access: Callable[[], None] | None = None,
        load_in_background: bool = True,
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.win = window
        self.svc = services
        self.on_close = on_close
        self.on_add_phone = on_add_phone
        self.on_allow_access = on_allow_access
        self.load_in_background = load_in_background
        self.closed = False
        self.scale = kit.ui_scale(window)
        self.fonts = kit.Fonts(tkfont.families(window))
        self.pages: dict[str, Any] = {}
        self.holders: dict[str, Any] = {}
        self.current: str | None = None
        self._tabs: dict[str, tuple[Any, Any]] = {}
        self._icon: Any = None
        s, f = self.scale, self.fonts

        window.title(WINDOW_TITLE)
        window.configure(bg=kit.YELLOW)
        w = min(round(BASE_W * s), window.winfo_screenwidth() - 40)
        h = min(round(BASE_H * s), window.winfo_screenheight() - 80)
        window.geometry(f"{w}x{h}")
        window.minsize(min(w, round(640 * s)), min(h, round(560 * s)))
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _e: self.close())
        window.bind("<Control-Tab>", lambda _e: self._step(1))
        window.bind("<Control-Shift-Tab>", lambda _e: self._step(-1))
        with contextlib.suppress(tk.TclError):  # X11 spells it ISO_Left_Tab
            window.bind("<Control-ISO_Left_Tab>", lambda _e: self._step(-1))
        window.bind("<MouseWheel>", self._on_wheel)
        with contextlib.suppress(Exception):  # cosmetic only
            self._icon = kit.set_window_icon(window)

        # Just the tabs: the brand mouth is Home's (one mouth per screen).
        nav = tk.Frame(window, bg=kit.YELLOW)
        nav.pack(fill="x", padx=round(22 * s), pady=(round(12 * s), 0))
        tab_font = (f.body_family, 12, "bold")
        for name, label in PAGES.items():
            cell = tk.Frame(nav, bg=kit.YELLOW)
            cell.pack(side="left", padx=(0, round(6 * s)))
            button = tk.Button(
                cell,
                text=label.upper(),
                command=lambda n=name: self.show(n),
                font=tab_font,
                bg=kit.YELLOW,
                fg=kit.MUTED,
                activebackground=kit.YELLOW,
                activeforeground=kit.BLACK,
                relief="flat",
                bd=0,
                padx=round(8 * s),
                pady=round(4 * s),
                cursor="hand2",
                highlightthickness=0,
            )
            button.pack()
            bar = tk.Frame(cell, bg=kit.YELLOW, height=round(4 * s))
            bar.pack(fill="x", padx=round(8 * s))
            self._tabs[name] = (button, bar)
        tk.Frame(window, bg=kit.BLACK, height=max(1, round(2 * s))).pack(fill="x")
        self.content = tk.Frame(window, bg=kit.YELLOW)
        self.content.pack(fill="both", expand=True)
        self.show(page)

    # ------------------------------------------------------------ pages

    def _make(self, name: str, holder: Any) -> Any:
        svc, background = self.svc, self.load_in_background
        if name == "teach":
            from shoutabout.review_ui import TeachPage

            return TeachPage(holder, svc.paths, load_in_background=background)
        if name == "recent":
            from shoutabout.recent_model import RecentModel
            from shoutabout.recent_ui import RecentPage

            return RecentPage(holder, RecentModel(svc), load_in_background=background)
        if name == "settings":
            from shoutabout.settings_model import SettingsModel
            from shoutabout.settings_ui import SettingsPage

            return SettingsPage(
                holder,
                SettingsModel(svc),
                on_add_phone=self.on_add_phone,
                load_in_background=background,
            )
        return HomePage(holder, svc, load_in_background=background, on_allow=self.on_allow_access)

    def show(self, page: str | None) -> None:
        """Bring ``page`` up (built on first use; refreshed when coming back to it)."""
        import tkinter as tk

        if self.closed:
            return
        name = page_name(page)
        if name == self.current:
            return
        previous = self.pages.get(self.current or "")
        if previous is not None:
            self.holders[self.current or ""].pack_forget()
            hidden = getattr(previous, "on_hidden", None)
            if callable(hidden):
                hidden()
        fresh = name not in self.pages
        if fresh:
            holder = tk.Frame(self.content, bg=kit.YELLOW)
            self.holders[name] = holder
            try:
                self.pages[name] = self._make(name, holder)
            except Exception:
                log.exception("couldn't build the %s page", name)
                holder.destroy()
                del self.holders[name]
                if name != "home":
                    self.show("home")
                return
        self.holders[name].pack(fill="both", expand=True)
        self.current = name
        for key, (button, bar) in self._tabs.items():
            on = key == name
            button.configure(fg=kit.BLACK if on else kit.MUTED)
            bar.configure(bg=kit.RED if on else kit.YELLOW)
        if not fresh:
            shown = getattr(self.pages[name], "on_shown", None)
            if callable(shown):
                shown()

    def _step(self, direction: int) -> str:
        names = list(PAGES)
        index = names.index(self.current) if self.current in names else 0
        self.show(names[(index + direction) % len(names)])
        return "break"

    def _on_wheel(self, event: Any) -> None:
        page = self.pages.get(self.current or "")
        handler = getattr(page, "on_wheel", None)
        if callable(handler):
            with contextlib.suppress(Exception):
                handler(event)

    # ------------------------------------------------------------ window

    def bring_to_front(self) -> None:
        with contextlib.suppress(Exception):
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for page in self.pages.values():
            with contextlib.suppress(Exception):
                page.close()
        self.pages.clear()
        with contextlib.suppress(Exception):
            self.win.destroy()
        if self.on_close is not None:
            self.on_close()

    def shutdown(self) -> None:
        """The app is quitting."""
        self.close()
