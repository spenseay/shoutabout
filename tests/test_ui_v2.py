"""Window routing (ui_host), the CLI commands, and real Tk smoke tests (--run-windows).

The Tk tests open each window briefly (withdrawn where possible), drive it through its
states with ``update()`` calls instead of a main loop, and close it. They never touch the
clipboard, the microphone or the keyboard.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from shoutabout import cli, ui_host
from shoutabout.config import Paths
from shoutabout.devices import DeviceStore
from v2_fakes import make_services

PHONE = "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d"


# --------------------------------------------------------- ui_host routing


class FakeWindow:
    def __init__(self, flow=None) -> None:
        self.flow = flow
        self.closed = 0
        self.fronted = 0

    def close(self) -> None:
        self.closed += 1

    def bring_to_front(self) -> None:
        self.fronted += 1


class FakeFlow:
    def __init__(self, accept: bool) -> None:
        self.accept = accept
        self.offered = []
        self.finished = []

    def offer(self, prompt) -> bool:
        self.offered.append(prompt)
        return self.accept

    def on_pair_finished(self, rid, status) -> None:
        self.finished.append((rid, status))


@pytest.fixture
def host(tmp_path, monkeypatch):
    svc = make_services(tmp_path)
    h = ui_host.UiHost(svc)
    monkeypatch.setattr(h, "_send", lambda kind, arg, start=True: h._handle(kind, arg))
    made = []

    def fake_prompt(window, prompt, **kw):
        w = FakeWindow()
        w.prompt, w.kw = prompt, kw
        made.append(w)
        return w

    import shoutabout.approval_ui as approval_ui

    monkeypatch.setattr(approval_ui, "ApprovalPrompt", fake_prompt)
    monkeypatch.setattr(h, "_toplevel", lambda: object())
    h.made = made
    return h


def test_pair_request_goes_to_the_setup_window_when_it_can_show_it(host):
    flow = FakeFlow(accept=True)
    host._windows["setup"] = FakeWindow(flow)
    prompt = host.svc.pairing.request("Sam's iPhone", PHONE, "192.168.1.5")
    assert flow.offered == [prompt]
    assert host.made == []


def test_pair_request_pops_a_prompt_otherwise(host):
    host._windows["setup"] = FakeWindow(FakeFlow(accept=False))
    prompt = host.svc.pairing.request("Sam's iPhone", PHONE, "192.168.1.5")
    (window,) = host.made
    assert window.prompt == prompt
    assert window.kw["approve"] == host.svc.pairing.approve
    assert host.is_open(f"pair:{prompt.request_id}")
    # Answered elsewhere (or expired): the prompt closes itself.
    host.svc.pairing.deny(prompt.request_id)
    assert window.closed == 1
    assert host._windows["setup"].flow.finished == [(prompt.request_id, "denied")]


def test_same_request_never_gets_two_prompts(host):
    prompt = host.svc.pairing.request("P", PHONE, "192.168.1.5")
    host._handle("pair", prompt)
    assert len(host.made) == 1


def test_open_existing_windows_come_to_the_front(host):
    host._windows["setup"] = setup = FakeWindow(FakeFlow(True))
    host._windows["settings"] = settings = FakeWindow()
    host._handle("setup", "full")
    host._handle("settings", None)
    assert (setup.fronted, settings.fronted) == (1, 1)


def test_finished_events_without_windows_are_ignored(host):
    host._handle("pair-done", ("nope", "expired"))


def test_shutdown_unsubscribes(tmp_path):
    svc = make_services(tmp_path)
    h = ui_host.UiHost(svc)
    assert h._on_pair_request in svc.pairing._on_request
    h.shutdown(0.1)
    assert h._on_pair_request not in svc.pairing._on_request
    h.open_settings()  # after shutdown: ignored, no thread
    assert h._thread is None


# ------------------------------------------------------------------- CLI


@pytest.fixture
def cli_home(home):
    Paths(home).home.mkdir(parents=True, exist_ok=True)
    return home


def test_devices_list_and_remove(cli_home, capsys):
    store = DeviceStore(Paths(cli_home).devices_file)
    assert cli.main(["devices", "list"]) == 0
    assert "No phones are paired" in capsys.readouterr().out
    device, token = store.add("Sam's iPhone", PHONE)
    store.add("Work iPhone", "0d5e9b1a-7c3f-4e2d-8a6b-1f0e9d8c7b6a")
    assert cli.main(["devices", "list", "--json"]) == 0
    out = capsys.readouterr().out
    assert token not in out
    rows = json.loads(out)["devices"]
    assert [r["name"] for r in rows] == ["Sam's iPhone", "Work iPhone"]
    assert "token" not in json.dumps(rows)
    assert cli.main(["devices", "list"]) == 0
    assert device.key[:8] in capsys.readouterr().out
    assert cli.main(["devices", "remove", "Sam's", "iPhone"]) == 0
    assert "stops working now" in capsys.readouterr().out
    assert store.match(token) is None
    other = store.list()[0]
    assert cli.main(["devices", "remove", other.key[:6]]) == 0
    assert store.list() == []
    assert cli.main(["devices", "remove", "ghost"]) == 1
    assert "no paired phone" in capsys.readouterr().err


def test_setup_and_settings_commands_open_windows(cli_home, monkeypatch):
    opened = []

    class FakeHost:
        def __init__(self, services) -> None:
            assert services.pairing is None  # no hub in this process
            assert services.devices.path == Paths(cli_home).devices_file
            self.svc = services

        def open_setup(self, mode="full"):
            opened.append(("setup", mode))

        def open_settings(self):
            opened.append(("settings", None))

        def open_history(self):
            opened.append(("history", None))

        def wait(self):
            opened.append(("wait", None))

    monkeypatch.setattr(ui_host, "UiHost", FakeHost)
    assert cli.main(["setup"]) == 0
    assert cli.main(["setup", "--phone"]) == 0
    assert cli.main(["settings"]) == 0
    assert cli.main(["history"]) == 0
    assert opened == [
        ("setup", "full"),
        ("wait", None),
        ("setup", "phone"),
        ("wait", None),
        ("settings", None),
        ("wait", None),
        ("history", None),
        ("wait", None),
    ]


# ----------------------------------------------------------- real Tk (opt-in)


def _pump(root, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def _widgets(parent):
    for child in parent.winfo_children():
        yield child
        yield from _widgets(child)


def _text(widget) -> str | None:
    try:
        return str(widget.cget("text"))
    except Exception:  # widgets without a text option (frames, canvases)
        return None


def _texts(parent) -> list[str]:
    return [t for t in map(_text, _widgets(parent)) if t]


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.mark.windows
def test_setup_window_renders_every_screen(tmp_path, tk_root):
    import tkinter as tk

    from shoutabout.setup_flow import SetupFlow
    from shoutabout.setup_ui import SetupWindow
    from v2_fakes import inline_runner

    svc = make_services(tmp_path)
    flow = SetupFlow(svc, runner=inline_runner)
    top = tk.Toplevel(tk_root)
    top.withdraw()
    closed = []
    win = SetupWindow(top, flow, on_close=lambda: closed.append(1))
    _pump(tk_root, 0.3)
    assert flow.phase == "ready"
    assert str(win._widgets["next"]["state"]) == "normal"
    flow.next()
    _pump(tk_root, 0.3)
    flow.toggle_phone()  # on
    _pump(tk_root, 0.2)
    flow.toggle_phone()  # off
    _pump(tk_root, 0.2)
    flow.toggle_phone()  # on again
    _pump(tk_root, 0.2)
    prompt = svc.pairing.request("Sam's iPhone", PHONE, "192.168.1.5")
    assert flow.offer(prompt)
    _pump(tk_root, 0.3)
    flow.deny()
    flow.toggle_qr()
    _pump(tk_root, 0.3)
    flow.toggle_qr()
    flow.next()
    _pump(tk_root, 0.3)
    box = win._widgets["box"]
    box.insert("1.0", "This is my first dictation.")
    _pump(tk_root, 0.3)
    assert flow.screen == "done"
    win.close()
    assert closed == [1]
    assert svc.paths.setup_flag_file.exists()


@pytest.mark.windows
def test_settings_window_renders_and_saves(tmp_path, tk_root):
    import tkinter as tk

    from shoutabout.settings_model import SettingsModel
    from shoutabout.settings_ui import SettingsWindow

    saved = []
    svc = make_services(tmp_path, saved=saved)
    svc.devices.add("Sam's iPhone", PHONE)
    top = tk.Toplevel(tk_root)
    top.withdraw()
    added = []
    win = SettingsWindow(top, SettingsModel(svc), on_add_phone=lambda: added.append(1))
    _pump(tk_root, 0.5)
    assert not win.view.loading_quality
    key = svc.devices.list()[0].key
    win._remove(key)  # first click asks "Sure?"
    assert svc.devices.list()
    win._remove(key)
    _pump(tk_root, 0.4)
    assert svc.devices.list() == []
    win.apply(lambda: win.model.set_cleanup("off"))
    _pump(tk_root, 0.3)
    assert saved[-1] == {"cleanup": {"enabled": False}}
    # History: a switch, off by default; clicking its label turns it on.
    from shoutabout.settings_model import HISTORY_LABEL, HISTORY_NOTE

    texts = _texts(win.outer)
    assert "HISTORY" in texts and HISTORY_NOTE in texts and win.view.history_on is False
    (label,) = [w for w in _widgets(win.outer) if _text(w) == HISTORY_LABEL]
    label.event_generate("<Button-1>")
    _pump(tk_root, 0.3)
    assert saved[-1] == {"history": {"enabled": True}}
    win._add_phone()
    assert added == [1]
    win.close()


@pytest.mark.windows
def test_approval_prompt_arms_counts_down_and_expires(tmp_path, tk_root):
    import tkinter as tk

    from shoutabout.approval_ui import ApprovalPrompt, countdown_text, prompt_message

    svc = make_services(tmp_path)
    clock = svc.extra["clock"]
    prompt = svc.pairing.request("Sam's iPhone", PHONE, "192.168.1.5")
    top = tk.Toplevel(tk_root)
    win = ApprovalPrompt(
        top,
        prompt,
        approve=svc.pairing.approve,
        deny=svc.pairing.deny,
        seconds_left=svc.pairing.seconds_left,
        arm_delay_ms=200,
    )
    win.allow()  # not armed yet: ignored
    assert not win.closed and svc.devices.list() == []
    _pump(tk_root, 0.4)
    assert str(win.allow_button["state"]) == "normal"
    assert win.countdown["text"] == "Closes in 2:00"
    clock.now += 200
    _pump(tk_root, 0.7)
    assert win.closed and win.result == "expired"
    assert prompt_message("P") == "P wants to dictate to this PC."
    assert countdown_text(61.2) == "Closes in 1:02"


@pytest.mark.windows
def test_approval_prompt_allow_and_dont_allow(tmp_path, tk_root):
    import tkinter as tk

    from shoutabout.approval_ui import ApprovalPrompt

    svc = make_services(tmp_path)
    clock = svc.extra["clock"]
    for answer in ("allow", "dont_allow"):
        prompt = svc.pairing.request("P", PHONE, f"192.168.1.{5 if answer == 'allow' else 6}")
        win = ApprovalPrompt(
            tk.Toplevel(tk_root),
            prompt,
            approve=svc.pairing.approve,
            deny=svc.pairing.deny,
            seconds_left=svc.pairing.seconds_left,
            arm_delay_ms=0,
        )
        _pump(tk_root, 0.1)
        getattr(win, answer)()
        assert win.closed
        clock.now += 1
    assert len(svc.devices.list()) == 1


@pytest.mark.windows
def test_ui_host_real_thread_opens_and_closes(tmp_path, monkeypatch):
    from shoutabout.settings_ui import SettingsWindow

    monkeypatch.setattr(SettingsWindow, "bring_to_front", lambda self: None)  # keep focus
    svc = make_services(tmp_path)
    svc.detect_hardware = lambda **_k: SimpleNamespace(best_gpu=None, cpu_name="x")
    h = ui_host.UiHost(svc)
    h.open_settings()
    for _ in range(100):
        if h.is_open("settings"):
            break
        time.sleep(0.05)
    assert h.is_open("settings")
    h.shutdown(5.0)
    assert h._thread is None or not h._thread.is_alive()
