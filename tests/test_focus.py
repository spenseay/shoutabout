"""The focus probe: classification rules (Windows and macOS), the look-up thread's deadline,
and the probes with fake facts. The real Windows probe runs only with --run-windows."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from shoutabout.inject import focus as fo
from shoutabout.inject.focus import (
    NONE,
    TEXT,
    UNKNOWN,
    Focus,
    LookWorker,
    UiaFacts,
    WinFacts,
    classify_windows,
    quick_answer,
)
from shoutabout.inject.focus_win import WindowsFocusProbe
from shoutabout.mac.focus_mac import MacFacts, MacFocusProbe, classify_mac

HWND = 0x1234


def win(**kw) -> WinFacts:
    base = {"hwnd": HWND, "window_class": "Chrome_WidgetWin_1", "exe": "chrome.exe"}
    base.update(kw)
    return WinFacts(**base)


def uia(ct: int, **kw) -> UiaFacts:
    return UiaFacts(control_type=ct, has_focus=True, **kw)


# ------------------------------------------------------- Windows: Win32 facts alone


def test_no_foreground_window_is_none():
    assert classify_windows(WinFacts()).kind == NONE
    assert quick_answer(WinFacts()) == Focus(NONE, "no window in front")


@pytest.mark.parametrize(
    "exe",
    [
        "WindowsTerminal.exe",
        "OpenConsole.exe",
        "conhost.exe",
        "mintty.exe",
        "putty.exe",
        "wezterm-gui.exe",
    ],
)
def test_terminals_are_text_without_ui_automation(exe):
    facts = win(exe=exe.lower(), window_class="CASCADIA_HOSTING_WINDOW_CLASS")
    assert quick_answer(facts).kind == TEXT


def test_extra_terminals_count_too():
    facts = win(exe="myterm.exe", window_class="X")
    assert quick_answer(facts) is None
    assert quick_answer(facts, fo.TERMINAL_PROCESSES | {"myterm.exe"}).kind == TEXT


def test_open_menu_is_none():
    assert quick_answer(win(menu=True)).kind == NONE


@pytest.mark.parametrize(
    "focus_class",
    ["Edit", "RichEditD2DPT", "Scintilla", "_WwG", "EXCEL7", "RICHEDIT50W", "ConsoleWindowClass"],
)
def test_text_window_classes(focus_class):
    assert (
        quick_answer(win(window_class="Notepad", exe="notepad.exe", focus_class=focus_class)).kind
        == TEXT
    )


def test_read_only_edit_control_is_none():
    facts = win(focus_class="Edit", focus_read_only=True)
    assert quick_answer(facts) == Focus(NONE, "read-only text box")


@pytest.mark.parametrize(
    "cls,reason", [("Progman", "desktop"), ("WorkerW", "desktop"), ("Shell_TrayWnd", "taskbar")]
)
def test_desktop_and_taskbar_are_none(cls, reason):
    facts = win(window_class=cls, exe="explorer.exe", focus_class="SysListView32")
    assert quick_answer(facts) == Focus(NONE, reason)


def test_renaming_on_the_desktop_is_text():
    # the rename box is an Edit control with a caret
    facts = win(window_class="Progman", exe="explorer.exe", focus_class="Edit", caret=True)
    assert classify_windows(facts).kind == TEXT
    # a caret alone keeps the desktop rule from answering early
    assert quick_answer(win(window_class="Progman", exe="explorer.exe", caret=True)) is None


def test_unclear_win32_facts_ask_ui_automation():
    assert quick_answer(win(focus_class="Chrome_RenderWidgetHostHWND")) is None


# ---------------------------------------------- Windows: UI Automation (measured)
# The UiaFacts below are what this PC's Chrome, Edge, Firefox, Explorer, Cursor and the
# Claude app reported (scripts/focus_probe.py).


@pytest.mark.parametrize(
    "facts,kind",
    [
        # <input>, <textarea>, role=textbox (Gmail, Slack), Monaco/xterm textareas
        (
            uia(
                fo.EDIT,
                value_pattern=True,
                value_read_only=False,
                text_pattern=True,
                text_edit_pattern=True,
            ),
            TEXT,
        ),
        # plain contenteditable and a contenteditable iframe body in Chrome (Google Docs)
        (
            uia(
                fo.GROUP,
                value_pattern=False,
                value_read_only=True,
                text_pattern=True,
                text_edit_pattern=True,
            ),
            TEXT,
        ),
        # <input readonly>
        (
            uia(
                fo.EDIT,
                value_pattern=True,
                value_read_only=True,
                text_pattern=True,
                text_edit_pattern=True,
            ),
            NONE,
        ),
        (uia(fo.BUTTON, value_pattern=False, value_read_only=True), NONE),
        (uia(fo.HYPERLINK, value_pattern=True, value_read_only=False), NONE),  # Firefox's links
        # the page itself (nothing focused in it), Chrome and Firefox
        (
            uia(
                fo.DOCUMENT,
                value_pattern=True,
                value_read_only=True,
                text_pattern=True,
                text_edit_pattern=True,
            ),
            NONE,
        ),
        (uia(fo.DOCUMENT, value_pattern=True, value_read_only=True, text_pattern=True), NONE),
        # Firefox's contenteditable iframe body: a writable document
        (uia(fo.DOCUMENT, value_pattern=True, value_read_only=False, text_pattern=True), TEXT),
        # File Explorer: a file in the list has a writable value (rename), still not typable
        (uia(fo.LISTITEM, value_pattern=True, value_read_only=False), NONE),
        # an Electron app with the focus on a plain pane
        (uia(fo.PANE, value_pattern=False, value_read_only=True), UNKNOWN),
        (uia(fo.DOCUMENT), UNKNOWN),
        (uia(fo.COMBOBOX, value_pattern=True, value_read_only=False), TEXT),
        (uia(fo.COMBOBOX), NONE),
        (uia(fo.DATAITEM, value_pattern=True, value_read_only=False), TEXT),  # a grid cell
        (uia(fo.CUSTOM), UNKNOWN),
        (uia(fo.TABITEM), NONE),
        (uia(fo.TREEITEM), NONE),
        (uia(fo.MENUITEM), NONE),
    ],
)
def test_ui_automation_rules(facts, kind):
    assert classify_windows(win(uia=facts)).kind == kind


def test_caret_counts_as_text_unless_read_only():
    assert classify_windows(win(caret=True, uia=uia(fo.GROUP))).kind == TEXT
    assert classify_windows(win(caret=True, uia=None, uia_error="x")).kind == TEXT
    ro_doc = uia(fo.DOCUMENT, value_pattern=True, value_read_only=True)
    assert classify_windows(win(caret=True, uia=ro_doc)).kind == NONE


def test_text_edit_pattern_counts_unless_read_only():
    assert classify_windows(win(uia=uia(fo.CUSTOM, text_edit_pattern=True))).kind == TEXT


def test_value_read_only_needs_the_value_pattern():
    # UIA answers IsReadOnly=true even without a ValuePattern: that alone means nothing
    facts = uia(fo.CUSTOM, value_pattern=False, value_read_only=True, text_edit_pattern=True)
    assert classify_windows(win(uia=facts)).kind == TEXT


def test_no_ui_automation_answer_is_unknown():
    out = classify_windows(win(uia=None, uia_error="UI Automation was too slow"))
    assert out == Focus(UNKNOWN, "UI Automation was too slow")


def test_reasons_are_short_and_never_text():
    for ct in fo.CONTROL_TYPE_NAMES:
        reason = classify_windows(win(uia=uia(ct))).reason
        assert 0 < len(reason) <= 40


# ------------------------------------------------------------------ look-up thread


class SlowClient:
    def __init__(self, delay: float, answer="facts", fail: Exception | None = None):
        self.delay = delay
        self.answer = answer
        self.fail = fail
        self.calls = 0

    def focused(self):
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return self.answer


def test_worker_answers_in_time():
    client = SlowClient(0.0)
    worker = LookWorker(lambda: client)
    assert worker.ask(1.0) == ("facts", "")
    assert worker.ask(1.0) == ("facts", "")
    assert client.calls == 2


def test_worker_deadline_then_busy_then_recovers():
    client = SlowClient(0.3)
    worker = LookWorker(lambda: client, "UI Automation")
    t0 = time.monotonic()
    facts, error = worker.ask(0.05)
    assert facts is None and error == "UI Automation was too slow"
    assert time.monotonic() - t0 < 0.25
    facts, error = worker.ask(1.0)  # the first look is still stuck: no waiting at all
    assert facts is None and "busy" in error
    time.sleep(0.4)
    client.delay = 0.0
    assert worker.ask(1.0) == ("facts", "")


def test_worker_reports_client_errors():
    worker = LookWorker(
        lambda: SlowClient(0.0, fail=OSError("no focused element")), "Accessibility"
    )
    facts, error = worker.ask(1.0)
    assert facts is None and error == "Accessibility: no focused element"


def test_worker_retries_a_failed_client_factory():
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("COM didn't start")
        return SlowClient(0.0)

    worker = LookWorker(factory)
    assert worker.ask(1.0)[0] is None
    assert worker.ask(1.0) == ("facts", "")


def test_worker_makes_the_client_on_its_own_thread():
    threads = []

    def factory():
        threads.append(threading.current_thread().name)
        return SlowClient(0.0)

    LookWorker(factory).ask(1.0)
    assert threads == ["focus-probe"]


# ------------------------------------------------------------- the Windows probe


class FakeWorker:
    def __init__(self, facts=None, error=""):
        self.facts, self.error = facts, error
        self.timeouts: list[float] = []

    def ask(self, timeout):
        self.timeouts.append(timeout)
        return self.facts, self.error


def test_windows_probe_quick_answers_skip_ui_automation():
    worker = FakeWorker()
    probe = WindowsFocusProbe(facts=lambda: win(exe="windowsterminal.exe"), worker=worker)
    focus = probe.probe()
    assert focus.kind == TEXT and worker.timeouts == []
    assert probe.last_facts.exe == "windowsterminal.exe"


def test_windows_probe_asks_ui_automation_within_the_budget():
    edit = uia(fo.EDIT, value_pattern=True, value_read_only=False)
    worker = FakeWorker(edit)
    probe = WindowsFocusProbe(facts=lambda: win(), worker=worker, timeout_s=0.15)
    focus = probe.probe()
    assert focus.kind == TEXT and focus.reason == "text box"
    assert len(worker.timeouts) == 1 and 0 < worker.timeouts[0] <= 0.15
    assert probe.last_facts.uia == edit
    assert focus.ms >= 0


def test_windows_probe_unknown_when_ui_automation_is_late():
    worker = FakeWorker(None, "UI Automation was too slow")
    focus = WindowsFocusProbe(facts=lambda: win(), worker=worker).probe()
    assert (focus.kind, focus.reason) == (UNKNOWN, "UI Automation was too slow")


def test_windows_probe_never_raises():
    def boom():
        raise OSError("access denied")

    focus = WindowsFocusProbe(facts=boom, worker=FakeWorker()).probe()
    assert focus.kind == UNKNOWN and "OSError" in focus.reason


def test_windows_probe_respects_the_hard_cap_with_a_real_worker():
    worker = LookWorker(lambda: SlowClient(1.0, answer=uia(fo.EDIT)))
    probe = WindowsFocusProbe(facts=lambda: win(), worker=worker, timeout_s=0.1)
    t0 = time.monotonic()
    focus = probe.probe()
    assert focus.kind == UNKNOWN
    assert time.monotonic() - t0 < 0.3


# ------------------------------------------------------------------------ macOS


@pytest.mark.parametrize(
    "facts,kind",
    [
        (MacFacts(app="com.apple.terminal", role="AXTextArea"), TEXT),
        (MacFacts(app="com.googlecode.iterm2", role="AXGroup"), TEXT),
        (MacFacts(app="com.apple.textedit", role="AXTextArea", value_settable=True), TEXT),
        (MacFacts(role="AXTextField", value_settable=True, selected_range=True), TEXT),
        (MacFacts(role="AXTextField", subrole="AXSearchField"), TEXT),
        (MacFacts(role="AXComboBox", value_settable=True), TEXT),
        # a text field macOS says can't be set: maybe read-only, maybe not -> unknown
        (MacFacts(role="AXTextField", value_settable=False), UNKNOWN),
        # contenteditable in Chrome / Safari (Gmail, Google Docs, Slack)
        (MacFacts(app="com.google.chrome", role="AXGroup", editable_ancestor=True), TEXT),
        (MacFacts(app="com.apple.safari", role="AXWebArea", editable_ancestor=False), NONE),
        (MacFacts(app="com.apple.finder", role="AXList"), NONE),
        (MacFacts(app="com.apple.finder", role="AXOutline"), NONE),
        (MacFacts(role="AXButton"), NONE),
        (MacFacts(role="AXLink"), NONE),
        (MacFacts(role="AXGroup", value_settable=True, selected_range=True), TEXT),
        (MacFacts(role="AXGroup"), UNKNOWN),
        (MacFacts(role="AXScrollArea"), UNKNOWN),
        (MacFacts(error="Accessibility isn't allowed"), UNKNOWN),
        (MacFacts(error="nothing has the focus"), UNKNOWN),
    ],
)
def test_mac_rules(facts, kind):
    assert classify_mac(facts).kind == kind


def test_mac_probe_with_a_fake_worker():
    probe = MacFocusProbe(worker=FakeWorker(MacFacts(role="AXTextArea", value_settable=True)))
    assert probe.probe().kind == TEXT
    assert probe.last_facts.role == "AXTextArea"


def test_mac_probe_unknown_when_late_or_broken():
    late = MacFocusProbe(worker=FakeWorker(None, "Accessibility was too slow")).probe()
    assert late.kind == UNKNOWN and late.reason == "Accessibility was too slow"

    class Boom:
        def ask(self, timeout):
            raise RuntimeError("x")

    assert MacFocusProbe(worker=Boom()).probe().kind == UNKNOWN


def test_mac_client_with_a_fake_accessibility_api(monkeypatch):
    """AxClient's calls, with a stand-in for PyObjC's ApplicationServices."""
    from types import SimpleNamespace

    from shoutabout.mac import focus_mac

    element = object()
    values = {
        (element, "AXRole"): (0, "AXTextArea"),
        (element, "AXSubrole"): (-25212, None),
        (element, "AXSelectedTextRange"): (0, "range"),
        (element, "AXEditableAncestor"): (-25205, None),
    }

    def copy(el, attr, _none):
        if attr == "AXFocusedUIElement":
            return 0, element
        return values.get((el, attr), (-25205, None))

    fake_as = SimpleNamespace(
        AXUIElementCreateSystemWide=lambda: "system",
        AXUIElementSetMessagingTimeout=lambda el, t: 0,
        AXUIElementCopyAttributeValue=copy,
        AXUIElementIsAttributeSettable=lambda el, attr, _n: (0, True),
        AXUIElementGetPid=lambda el, _n: (-25204, 0),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake_as)
    client = focus_mac.AxClient()
    monkeypatch.setattr(client, "_frontmost", lambda: "com.apple.textedit")
    facts = client.focused()
    assert facts == MacFacts(
        app="com.apple.textedit",
        role="AXTextArea",
        subrole=None,
        value_settable=True,
        selected_range=True,
        editable_ancestor=None,
    )
    assert classify_mac(facts).kind == TEXT

    fake_as.AXUIElementCopyAttributeValue = lambda el, attr, _n: (-25211, None)
    facts = client.focused()
    assert facts.role is None and facts.error == "Accessibility isn't allowed"


# ------------------------------------------------------- scripts/focus_probe.py


def _load_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "focus_probe.py"
    spec = importlib.util.spec_from_file_location("focus_probe_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_focus_probe_script_prints_one_line_per_look():
    script = _load_script()
    edit = uia(fo.EDIT, value_pattern=True, value_read_only=False)
    probe = WindowsFocusProbe(facts=lambda: win(caret=True), worker=FakeWorker(edit))
    result = script.describe(probe)
    assert result["kind"] == TEXT and result["facts"]["uia"]["control_type"] == fo.EDIT
    text = script.line(result)
    assert text.startswith("text") and "chrome.exe" in text and "uia=50004" in text
    mac = MacFocusProbe(worker=FakeWorker(MacFacts(app="com.apple.finder", role="AXList")))
    assert script.line(script.describe(mac)).startswith("none")


# ------------------------------------------------------------- the real probe


@pytest.mark.windows
def test_real_windows_probe_answers_quickly():
    probe = WindowsFocusProbe()
    probe.probe()  # the first look starts the thread and UI Automation
    t0 = time.monotonic()
    focus = probe.probe()
    assert focus.kind in (TEXT, NONE, UNKNOWN)
    assert time.monotonic() - t0 < 0.5
    assert probe.last_facts is not None
