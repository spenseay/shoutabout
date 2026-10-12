"""The one ShoutAbout window: page names, Home's lines, the hub status it reads, the tray and
"opened again" wiring, and a real Tk run through every page (--run-windows)."""

from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace

import pytest

from shoutabout import app_window as aw
from shoutabout.models import HubStatus
from shoutabout.state import HubState, Progress
from v2_fakes import make_services

# ------------------------------------------------------------------- pure logic


def test_page_names():
    assert list(aw.PAGES) == ["home", "teach", "recent", "settings"]
    assert aw.page_name(None) == "home"
    assert aw.page_name(" Settings ") == "settings"
    assert aw.page_name("history") == "recent"  # what the tray used to call it
    assert aw.page_name("nonsense") == "home"


@pytest.mark.parametrize(
    ("status", "kw", "expected"),
    [
        (None, {}, ("", False)),
        ({"phase": "idle"}, {}, ("", False)),
        (
            {"phase": "error", "error": "Speech engine failed to load: x\ny"},
            {},
            (
                "Speech engine failed to load: x",
                True,
            ),
        ),
        (
            {"phase": "idle", "missing": ["accessibility"]},
            {},
            (
                aw.PERMISSION_NOTES["accessibility"],
                True,
            ),
        ),
        ({"phase": "idle"}, {"mic": False}, (aw.NO_MIC_NOTE, True)),
        ({"phase": "idle"}, {"dictation_on": False}, ("Talk to this PC is off.", False)),
        (
            {"phase": "idle"},
            {"dictation_on": False, "platform": "darwin"},
            (
                "Talk to this Mac is off.",
                False,
            ),
        ),
        ({"phase": "preparing", "percent": 42}, {}, ("Getting ready… 42%", False)),
        ({"phase": "starting", "percent": None}, {}, ("Getting ready…", False)),
        ({"phase": "idle", "paused": True}, {}, ("Paused.", False)),
    ],
)
def test_home_says_at_most_one_thing(status, kw, expected):
    args = {"dictation_on": True, "mic": True, "platform": "win32", **kw}
    assert aw.attention(status, **args) == expected


def test_the_most_important_thing_wins():
    everything = {
        "phase": "error",
        "error": "Couldn't listen on port 47821",
        "missing": ["microphone"],
        "paused": True,
    }
    line, urgent = aw.attention(everything, dictation_on=False, mic=False)
    assert line.startswith("Couldn't listen") and urgent
    everything["phase"] = "preparing"
    assert (
        aw.attention(everything, dictation_on=False, mic=False)[0]
        == (aw.PERMISSION_NOTES["microphone"])
    )
    long = "x" * 200
    line, _ = aw.attention({"phase": "error", "error": long}, dictation_on=True, mic=True)
    assert len(line) == aw.ERROR_MAX and line.endswith("…")


def test_home_view_reads_the_hub(tmp_path):
    svc = make_services(tmp_path)
    svc.state = state = HubState()
    view = aw.home_view(svc)
    assert view.talk == "Press Right Ctrl, talk, press it again."  # the configured key
    assert view.note == "Getting ready…" and view.busy  # the engine is still loading
    state.set_progress(Progress(50, 100))
    assert aw.home_view(svc).note == "Getting ready… 50%"
    state.set_progress(None)
    state.set_status(HubStatus.IDLE)
    assert aw.home_view(svc) == aw.HomeView(talk="Press Right Ctrl, talk, press it again.")
    svc.has_microphone = lambda: False
    assert aw.home_view(svc).note == aw.NO_MIC_NOTE
    svc.has_microphone = lambda: True
    svc.paths.config_file.write_text('[dictation]\nmode = "hold"\nhotkey = "f8"\n', "utf-8")
    assert aw.home_view(svc).talk == "Hold F8, talk, let go."


def test_home_offers_allow_for_a_missing_mac_permission(tmp_path):
    svc = make_services(tmp_path)
    svc.state = HubState()
    svc.state.set_status(HubStatus.IDLE)
    assert not aw.home_view(svc).allow
    svc.missing_permissions = lambda: ["accessibility"]
    view = aw.home_view(svc)
    assert view.note == aw.PERMISSION_NOTES["accessibility"] and view.urgent and view.allow
    svc.missing_permissions = lambda: ["microphone", "accessibility"]
    assert aw.home_view(svc).note == aw.PERMISSION_NOTES["microphone"]  # the first to allow
    # a worse problem: no Allow for it
    svc.state.set_status(HubStatus.ERROR, "Couldn't listen on port 47821")
    view = aw.home_view(svc)
    assert view.note.startswith("Couldn't listen") and view.urgent and not view.allow
    # Never "System Settings" in the line: its button opens "Allow access", which opens them.
    assert not any("System Settings" in note for note in aw.PERMISSION_NOTES.values())


def test_hub_status_here_on_a_mac_and_standalone(tmp_path):
    svc = make_services(tmp_path)
    assert svc.hub_status() is None  # no hub in this process (shoutabout settings)
    svc.state = HubState()
    svc.state.set_paused(True)
    assert svc.hub_status() == {
        "phase": "starting",
        "percent": None,
        "error": None,
        "paused": True,
        "missing": [],
    }
    svc.missing_permissions = lambda: ["input_monitoring"]
    assert svc.hub_status()["missing"] == ["input_monitoring"]

    def broken():
        raise RuntimeError("no AX")

    svc.missing_permissions = broken
    assert svc.hub_status()["missing"] == []
    # The Mac window process asks the hub instead.
    svc.remote_status = lambda: {"phase": "error", "error": "boom"}
    assert svc.hub_status() == {"phase": "error", "error": "boom"}
    assert aw.home_view(svc).note == "boom"


# --------------------------------------------------------------------- wiring


def test_tray_items_open_the_window_on_their_page(tmp_path, monkeypatch):
    from shoutabout import hub, tray

    opened = []
    ui = SimpleNamespace(
        open_recent=lambda: opened.append("recent"),
        open_teach=lambda: opened.append("teach"),
        open_settings=lambda: opened.append("settings"),
        open_setup=lambda: opened.append("setup"),
    )
    v2 = SimpleNamespace(ui=ui, services=make_services(tmp_path))
    made = []
    monkeypatch.setattr(tray, "TrayIcon", lambda state, actions: made.append(actions))
    hub._build_tray(None, None, "t", HubState(), lambda *_a: None, lambda: None, v2)
    (actions,) = made
    for name in ("recent", "teach", "settings", "setup"):
        getattr(actions, name)()
    assert opened == ["recent", "teach", "settings", "setup"]
    labels = [i.label for i in tray.build_menu("S", HubStatus.IDLE, False, None, recent=True)]
    assert "Recent…" in labels and "History…" not in labels


def test_opened_again_on_a_mac_shows_the_window(monkeypatch):
    """Finder / Spotlight / Launchpad open a running app with a reopen event: Home."""
    from shoutabout import reopen

    class FakeObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    app = SimpleNamespace(delegate=None)
    app.setDelegate_ = lambda d: setattr(app, "delegate", d)
    foundation = types.ModuleType("Foundation")
    foundation.NSObject = FakeObject
    appkit = types.ModuleType("AppKit")
    appkit.NSApplication = SimpleNamespace(sharedApplication=lambda: app)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(reopen, "_MAC_DELEGATE", None)
    assert reopen.watch_mac(lambda: None) is None  # not a Mac: nothing installed
    monkeypatch.setattr(reopen.sys, "platform", "darwin")
    shown = []
    delegate = reopen.watch_mac(lambda: shown.append("home"))
    assert app.delegate is delegate
    assert delegate.applicationShouldHandleReopen_hasVisibleWindows_(app, False) is False
    for _ in range(50):
        if shown:
            break
        time.sleep(0.02)
    assert shown == ["home"]


# ------------------------------------------------------------------ real Tk


def _pump(root, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


@pytest.mark.windows
@pytest.mark.parametrize("vector", [False, True], ids=["bitmap-mouth", "vector-mouth"])
def test_the_window_has_every_page(tmp_path, monkeypatch, vector):
    """Every page, and the brand mouth once at most: Home's (no small one in the tabs).
    The macOS vector mouth runs here too (Tk's canvas is the same API, just not smooth)."""
    tk = pytest.importorskip("tkinter")
    from shoutabout.recent import Recent
    from v2_fakes import shown_marks

    monkeypatch.setattr(aw.AppWindow, "bring_to_front", lambda self: None)  # keep focus
    monkeypatch.setattr(aw.kit, "VECTOR_MOUTH", vector)
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display, or Tcl not ready for another interpreter
        pytest.skip(f"no Tk: {exc}")
    root.withdraw()
    try:
        svc = make_services(tmp_path)
        svc.detect_hardware = lambda **_k: SimpleNamespace(best_gpu=None, cpu_name="x")
        svc.state = HubState()
        svc.recent = Recent()
        closed, phones = [], []
        top = tk.Toplevel(root)
        win = aw.AppWindow(
            top,
            svc,
            page="recent",
            on_close=lambda: closed.append(1),
            on_add_phone=lambda: phones.append(1),
        )
        _pump(root, 0.3)
        assert top.title() == "ShoutAbout"
        assert win.current == "recent" and list(win.pages) == ["recent"]  # built when shown
        for name in ("home", "teach", "settings", "recent"):
            win.show(name)
            _pump(root, 0.2)
            assert win.current == name
            assert shown_marks(top) == (1 if name == "home" else 0), name
            button, bar = win._tabs[name]
            assert bar["bg"] == aw.kit.RED and button["fg"] == aw.kit.BLACK
            others = [b for key, (_b, b) in win._tabs.items() if key != name]
            assert all(b["bg"] == aw.kit.YELLOW for b in others)
            assert win.holders[name].winfo_manager() == "pack"
            hidden = [h for key, h in win.holders.items() if key != name]
            assert not any(h.winfo_manager() for h in hidden)
        assert set(win.pages) == set(aw.PAGES)
        win.show("history")  # an old name: the same page
        assert win.current == "recent"
        assert win._step(1) == "break" and win.current == "settings"
        win._step(1)
        assert win.current == "home"
        _pump(root, 1.3)  # Home asks the hub again by itself
        assert win.pages["home"].view.note == "Getting ready…"
        win.pages["settings"]._add_phone()
        assert phones == [1]
        win.close()
        assert closed == [1] and win.pages == {}
        assert not top.winfo_exists()
    finally:
        root.destroy()


@pytest.mark.windows
def test_homes_allow_opens_allow_access(tmp_path, monkeypatch):
    """A missing macOS permission on Home: the red line and an "Allow" button (the line is
    clickable too) that open the setup's "Allow access" screen."""
    tk = pytest.importorskip("tkinter")
    monkeypatch.setattr(aw.AppWindow, "bring_to_front", lambda self: None)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no Tk: {exc}")
    root.withdraw()
    try:
        svc = make_services(tmp_path)
        svc.state = HubState()
        svc.state.set_status(HubStatus.IDLE)
        svc.missing_permissions = lambda: ["accessibility"]
        asked: list[int] = []
        top = tk.Toplevel(root)
        win = aw.AppWindow(
            top, svc, on_allow_access=lambda: asked.append(1), load_in_background=False
        )
        home = win.pages["home"]
        assert home.view.allow and home.note["text"] == aw.PERMISSION_NOTES["accessibility"]
        assert home.allow_button.winfo_manager() == "pack"
        assert home.allow_button["text"] == aw.ALLOW_BUTTON
        home.allow_button.invoke()
        assert home.note.bind("<Button-1>") and home.note["cursor"] == "hand2"
        home.clicked_line()  # (a withdrawn window gets no real clicks)
        assert asked == [1, 1]
        svc.missing_permissions = lambda: []  # allowed: the line and the button go
        home.render(aw.home_view(svc))
        assert not home.allow_button.winfo_manager() and home.note["text"] == ""
        home.clicked_line()
        assert asked == [1, 1] and home.note["cursor"] == ""
        win.close()
        # No way to open "Allow access" (no handler): no button either.
        svc.missing_permissions = lambda: ["microphone"]
        win = aw.AppWindow(tk.Toplevel(root), svc, load_in_background=False)
        assert not win.pages["home"].allow_button.winfo_manager()
        win.close()
    finally:
        root.destroy()
