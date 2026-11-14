"""Is there somewhere to type? A quick look at what has the keyboard focus, just before a paste.

The answer is one of three kinds:

* ``text``: an editable text field has the keyboard focus (or a terminal is in front);
* ``none``: there is confidently nowhere to type: the desktop, the taskbar, a menu, or the
  focused thing is clearly not editable (a button, a file in a list, a tab, a read-only
  page) and there is no text caret;
* ``unknown``: it couldn't be told (UI Automation failed or was too slow, an app that
  draws its own controls...).

The injector pastes in every case (some apps move the focus into their text box when
they get Ctrl+V, Slack does), but it only puts the old clipboard back after ``text``: after
``none`` or ``unknown`` the dictation stays on the clipboard, because losing a dictation is
worse than losing what was copied before.

This module is pure (no Win32 calls): the facts are gathered by
:mod:`shoutabout.inject.focus_win` (Windows) or :mod:`shoutabout.mac.focus_mac` (macOS), and
:func:`classify_windows` turns them into an answer, so the rules are unit-tested on any OS.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

log = logging.getLogger(__name__)

FocusKind = Literal["text", "none", "unknown"]
TEXT: FocusKind = "text"
NONE: FocusKind = "none"
UNKNOWN: FocusKind = "unknown"


@dataclass(frozen=True)
class Focus:
    """What has the keyboard focus. ``reason`` is a few words for the log, never content."""

    kind: FocusKind
    reason: str = ""
    ms: float = 0.0  # how long the look took

    def __str__(self) -> str:
        return f"{self.kind} ({self.reason}, {self.ms:.0f} ms)" if self.reason else self.kind


class FocusProbe(Protocol):
    def probe(self) -> Focus:
        """Look at the keyboard focus now. Must never raise or block for long: a probe that
        can't tell in time answers ``unknown``."""


# --------------------------------------------------------------- the look-up thread


@dataclass
class _Job:
    done: threading.Event = field(default_factory=threading.Event)
    facts: Any = None
    error: str = ""


class LookWorker:
    """One long-lived thread that asks the accessibility API (UI Automation, macOS
    Accessibility) what has the focus, so the caller can stop waiting at a deadline.

    ``client_factory()`` is called once, on that thread (COM wants its objects used on the
    thread that made them); ``client.focused()`` is called for every look. While a look is
    stuck (an app that doesn't answer), new looks answer at once: "still busy".
    """

    def __init__(self, client_factory: Callable[[], Any], name: str = "UI Automation") -> None:
        self._factory = client_factory
        self.name = name
        self._jobs: queue.Queue[_Job] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._busy = threading.Event()

    def ask(self, timeout: float) -> tuple[Any, str]:
        """(facts, "") or (None, why not). Waits at most ``timeout`` seconds."""
        job = _Job()
        with self._lock:
            if self._busy.is_set():
                return None, f"{self.name} is still busy with an earlier look"
            self._busy.set()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="focus-probe", daemon=True)
                self._thread.start()
            self._jobs.put(job)
        if not job.done.wait(max(0.0, timeout)):
            return None, f"{self.name} was too slow"
        return job.facts, job.error

    def _run(self) -> None:
        client: Any = None
        while True:
            job = self._jobs.get()
            try:
                if client is None:
                    client = self._factory()
                job.facts = client.focused()
            except Exception as exc:
                job.error = f"{self.name}: {exc}"
                log.debug("focus probe: %s failed", self.name, exc_info=True)
            finally:
                self._busy.clear()
                job.done.set()


# ------------------------------------------------------------------- terminals

# Terminals don't describe their text area to UI Automation in a useful way, but you can
# always paste into them. Exe names, lower-case. paste.shift_insert_processes is added.
TERMINAL_PROCESSES = frozenset(
    {
        "windowsterminal.exe",
        "wt.exe",
        "openconsole.exe",
        "conhost.exe",
        # A classic console window reports the program running in it as its process.
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "wsl.exe",
        "bash.exe",
        "mintty.exe",
        "putty.exe",
        "kitty.exe",
        "alacritty.exe",
        "wezterm-gui.exe",
        "wezterm.exe",
        "conemu.exe",
        "conemu64.exe",
        "cmder.exe",
        "hyper.exe",
        "tabby.exe",
        "mobaxterm.exe",
        "rio.exe",
        "warp.exe",
        "ghostty.exe",
        "fluentterminal.app.exe",
        "terminus.exe",
        "kitty-terminal.exe",
    }
)

# ------------------------------------------------------------------- window classes

# The focused child window's class says "text box" on its own for these (the classic
# controls and the big editors that don't need UI Automation to be recognised).
TEXT_WINDOW_CLASSES = frozenset(
    {
        "edit",
        "richedit",
        "richedit20a",
        "richedit20w",
        "richedit50w",
        "richeditd2dpt",  # Notepad on Windows 11
        "scintilla",  # Notepad++ and friends
        "_wwg",  # Word's document
        "excel7",  # Excel's grid: typing goes into the selected cell
        "excel6",  # ...a cell being edited
        "excel<",  # Excel's formula bar
        "ppteditwnd",
        "consolewindowclass",
        "cascadia_hosting_window_class",  # Windows Terminal
        "pseudoconsolewindow",
        "virtualconsoleclass",  # ConEmu
        "puttytype",
        "mintty",
    }
)
# Classic edit controls whose read-only style is worth checking.
EDIT_WINDOW_CLASSES = frozenset({"edit", "richedit", "richedit20a", "richedit20w", "richedit50w"})

# The desktop and the taskbar: nowhere to type (unless a caret or a text box says otherwise,
# e.g. while renaming a file on the desktop).
DESKTOP_CLASSES = frozenset({"progman", "workerw"})
TASKBAR_CLASSES = frozenset(
    {
        "shell_traywnd",
        "shell_secondarytraywnd",
        "notifyiconoverflowwindow",
        "toplevelwindowforoverflowxamlisland",
        "xaml_windowedpopupclass",
    }
)

# ------------------------------------------------------------------- UI Automation

# UIA_*ControlTypeId (UIAutomationClient.h)
BUTTON = 50000
CALENDAR = 50001
CHECKBOX = 50002
COMBOBOX = 50003
EDIT = 50004
HYPERLINK = 50005
IMAGE = 50006
LISTITEM = 50007
LIST = 50008
MENU = 50009
MENUBAR = 50010
MENUITEM = 50011
PROGRESSBAR = 50012
RADIOBUTTON = 50013
SCROLLBAR = 50014
SLIDER = 50015
SPINNER = 50016
STATUSBAR = 50017
TAB = 50018
TABITEM = 50019
TEXT_CONTROL = 50020
TOOLBAR = 50021
TOOLTIP = 50022
TREE = 50023
TREEITEM = 50024
CUSTOM = 50025
GROUP = 50026
THUMB = 50027
DATAGRID = 50028
DATAITEM = 50029
DOCUMENT = 50030
SPLITBUTTON = 50031
WINDOW = 50032
PANE = 50033
HEADER = 50034
HEADERITEM = 50035
TABLE = 50036
TITLEBAR = 50037
SEPARATOR = 50038
SEMANTICZOOM = 50039
APPBAR = 50040

CONTROL_TYPE_NAMES = {
    BUTTON: "button",
    CALENDAR: "calendar",
    CHECKBOX: "check box",
    COMBOBOX: "combo box",
    EDIT: "edit",
    HYPERLINK: "link",
    IMAGE: "image",
    LISTITEM: "list item",
    LIST: "list",
    MENU: "menu",
    MENUBAR: "menu bar",
    MENUITEM: "menu item",
    PROGRESSBAR: "progress bar",
    RADIOBUTTON: "radio button",
    SCROLLBAR: "scroll bar",
    SLIDER: "slider",
    SPINNER: "spinner",
    STATUSBAR: "status bar",
    TAB: "tab list",
    TABITEM: "tab",
    TEXT_CONTROL: "label",
    TOOLBAR: "toolbar",
    TOOLTIP: "tooltip",
    TREE: "tree",
    TREEITEM: "tree item",
    CUSTOM: "custom",
    GROUP: "group",
    THUMB: "thumb",
    DATAGRID: "grid",
    DATAITEM: "grid item",
    DOCUMENT: "document",
    SPLITBUTTON: "split button",
    WINDOW: "window",
    PANE: "pane",
    HEADER: "header",
    HEADERITEM: "header item",
    TABLE: "table",
    TITLEBAR: "title bar",
    SEPARATOR: "separator",
    SEMANTICZOOM: "semantic zoom",
    APPBAR: "app bar",
}

# Focused, these are clearly not somewhere to type: a paste does nothing there.
NOT_TYPABLE = frozenset(
    {
        BUTTON,
        CALENDAR,
        CHECKBOX,
        HYPERLINK,
        IMAGE,
        LISTITEM,
        LIST,
        MENU,
        MENUBAR,
        MENUITEM,
        PROGRESSBAR,
        RADIOBUTTON,
        SCROLLBAR,
        SLIDER,
        STATUSBAR,
        TAB,
        TABITEM,
        TEXT_CONTROL,
        TOOLBAR,
        TOOLTIP,
        TREE,
        TREEITEM,
        THUMB,
        SPLITBUTTON,
        HEADER,
        HEADERITEM,
        TITLEBAR,
        SEPARATOR,
        APPBAR,
    }
)


def control_type_name(control_type: int | None) -> str:
    if control_type is None:
        return "no control type"
    return CONTROL_TYPE_NAMES.get(control_type, f"control type {control_type}")


@dataclass(frozen=True)
class UiaFacts:
    """What UI Automation says about the focused element (None = didn't say)."""

    control_type: int | None = None
    class_name: str | None = None
    framework: str | None = None
    has_focus: bool | None = None
    value_pattern: bool | None = None  # ValuePattern available
    value_read_only: bool | None = None  # ValuePattern.IsReadOnly
    text_pattern: bool | None = None
    text_edit_pattern: bool | None = None
    password: bool | None = None
    pid: int | None = None

    @property
    def writable_value(self) -> bool:
        return bool(self.value_pattern) and self.value_read_only is False

    @property
    def read_only_value(self) -> bool:
        return bool(self.value_pattern) and self.value_read_only is True


@dataclass(frozen=True)
class WinFacts:
    """Everything the Windows probe looked at (see :mod:`shoutabout.inject.focus_win`)."""

    hwnd: int | None = None  # the foreground window
    window_class: str = ""  # its class
    exe: str = ""  # its process, lower-case
    pid: int | None = None
    focus_class: str = ""  # the focused child window's class (GetGUIThreadInfo)
    focus_read_only: bool = False  # ...a classic edit control with ES_READONLY
    caret: bool = False  # the foreground thread shows a text caret
    menu: bool = False  # a menu is open
    uia: UiaFacts | None = None
    uia_error: str = ""  # why UI Automation gave no answer


def quick_answer(facts: WinFacts, terminals: frozenset[str] = TERMINAL_PROCESSES) -> Focus | None:
    """The answer from the Win32 facts alone, when they settle it (then UI Automation, which
    is slower, isn't asked); else None."""
    f = facts
    focus_class = f.focus_class.lower()
    if not f.hwnd:
        return Focus(NONE, "no window in front")
    if f.exe and f.exe.lower() in terminals:
        return Focus(TEXT, f"terminal {f.exe}")
    if f.menu:
        return Focus(NONE, "a menu is open")
    if focus_class in EDIT_WINDOW_CLASSES and f.focus_read_only:
        return Focus(NONE, "read-only text box")
    if focus_class in TEXT_WINDOW_CLASSES:
        return Focus(TEXT, f"{f.focus_class} window")
    if not f.caret:
        window_class = f.window_class.lower()
        if window_class in DESKTOP_CLASSES:
            return Focus(NONE, "desktop")
        if window_class in TASKBAR_CLASSES:
            return Focus(NONE, "taskbar")
    return None


def classify_windows(facts: WinFacts, terminals: frozenset[str] = TERMINAL_PROCESSES) -> Focus:
    """The rules, most certain first. False "text" only means today's behaviour (paste and
    restore); false "none" would show a wrong pop-up, so "none" needs clear evidence."""
    quick = quick_answer(facts, terminals)
    if quick is not None:
        return quick
    f = facts
    uia = f.uia
    window_class = f.window_class.lower()

    if uia is not None and uia.control_type == EDIT:
        if uia.read_only_value:
            return Focus(NONE, "read-only text box")
        return Focus(TEXT, "text box")
    if uia is not None and uia.text_edit_pattern and not uia.read_only_value:
        return Focus(TEXT, f"editable {control_type_name(uia.control_type)}")
    if f.caret and not (uia is not None and uia.read_only_value):
        return Focus(TEXT, "text caret")

    if window_class in DESKTOP_CLASSES:
        return Focus(NONE, "desktop")
    if window_class in TASKBAR_CLASSES:
        return Focus(NONE, "taskbar")

    if uia is None:
        return Focus(UNKNOWN, f.uia_error or "no UI Automation answer")
    ct = uia.control_type
    name = control_type_name(ct)
    if ct == DOCUMENT:
        if uia.read_only_value:
            return Focus(NONE, "read-only page")
        if uia.writable_value:
            return Focus(TEXT, "editable document")
        return Focus(UNKNOWN, "document")
    if ct == COMBOBOX:
        if uia.writable_value:
            return Focus(TEXT, "editable combo box")
        return Focus(NONE, "drop-down list")
    if ct in NOT_TYPABLE:
        return Focus(NONE, name)
    if uia.writable_value:  # a grid cell, a custom editor...
        return Focus(TEXT, f"editable {name}")
    return Focus(UNKNOWN, name)
