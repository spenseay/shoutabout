"""Smoke tests of the Cocoa-facing code with stand-in PyObjC modules.

These can't prove the Apple APIs behave as expected (only a Mac can), but they run every
line of the menu bar, overlay panel, tap, pasteboard and permission glue, so Python-level
mistakes (wrong names of our own, bad arguments, unhandled None) show up on any OS.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from shoutabout.state import HubState
from shoutabout.tray import CMD_PAUSE, CMD_QUIT, CMD_SETTINGS, TrayActions


class FakeNSObject:
    def __init_subclass__(cls, protocols=None, **kwargs):
        super().__init_subclass__(**kwargs)

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


def stub_module(name: str, **attrs):
    mod = types.ModuleType(name)
    mocks: dict[str, MagicMock] = {}

    def getattr_(attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return mocks.setdefault(attr, MagicMock(name=f"{name}.{attr}"))

    mod.__getattr__ = getattr_  # type: ignore[attr-defined]
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture
def cocoa(monkeypatch):
    calls: dict[str, list] = {"after": [], "stop": []}

    def call_after(fn, *args, **kwargs):
        calls["after"].append(fn)
        fn(*args, **kwargs)

    apphelper = stub_module(
        "PyObjCTools.AppHelper",
        callAfter=call_after,
        runEventLoop=lambda **kw: calls.setdefault("loop", []).append(kw),
        stopEventLoop=lambda: calls["stop"].append(1),
    )
    pyobjctools = stub_module("PyObjCTools", AppHelper=apphelper)
    foundation = stub_module("Foundation", NSObject=FakeNSObject)
    objc = stub_module("objc", protocolNamed=lambda name: name)
    modules = {
        "objc": objc,
        "Foundation": foundation,
        "AppKit": stub_module("AppKit"),
        "Quartz": stub_module("Quartz"),
        "ApplicationServices": stub_module("ApplicationServices"),
        "AVFoundation": stub_module("AVFoundation"),
        "PyObjCTools": pyobjctools,
        "PyObjCTools.AppHelper": apphelper,
    }
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)
    from shoutabout.mac import menubar

    monkeypatch.setattr(menubar, "_CONTROLLER", None)
    return modules, calls


def test_menu_bar_runs_builds_its_menu_and_dispatches(cocoa):
    from shoutabout.mac.menubar import MacMenuBar

    modules, calls = cocoa
    state = HubState()
    done: list[str] = []
    actions = TrayActions(
        toggle_pause=lambda: done.append("pause"),
        quit=lambda: done.append("quit"),
        settings=lambda: done.append("settings"),
        hint=lambda: "Press Right ⌘ Command to talk.",
    )
    opened: list[int] = []
    bar = MacMenuBar(
        state, actions, extra_items=lambda: [(0, "Allow pasting…")], on_extra=opened.append
    )
    bar.run()
    assert calls["loop"], "the Cocoa event loop ran"
    menu = MagicMock()
    bar._rebuild(menu)
    titles = [
        c.args[0]
        for c in modules["AppKit"]
        .NSMenuItem.alloc()
        .initWithTitle_action_keyEquivalent_.call_args_list
    ]
    assert "Allow pasting…" in titles and "Quit" in titles
    state.set_paused(True)  # the listener refreshes the icon on the main thread
    bar.notify("Title", "Message")
    import time

    for cmd in (CMD_PAUSE, CMD_SETTINGS, 100):
        bar._dispatch(cmd)
    time.sleep(0.2)
    assert {"pause", "settings"} <= set(done) and opened == [0]
    bar._dispatch(CMD_QUIT)
    assert "quit" in done and calls["stop"]


def test_overlay_panel_and_text(cocoa):
    from shoutabout import overlay as ov
    from shoutabout.mac import overlay_mac

    panel = overlay_mac.CocoaPanel()
    rect, scale = panel.screen()
    img = ov.render(ov.frame_at("listening", 1.0).look, 2.0)
    image = panel.make_image(img)
    panel.show(image, (10, 10, img.width / 2, img.height / 2), 2.0, 0.5)
    panel.start_timer(0.03, lambda: None)
    panel.stop_timer()
    panel.hide()
    panel.close()
    assert rect and scale
    # Off macOS the text renderer declines (the pill then shows no words).
    assert overlay_mac.mac_text_mask("Mic blocked", 30) is None


def test_tap_api_paste_and_permissions_glue(cocoa, monkeypatch):
    from shoutabout.mac import eventtap, paste, permissions

    api = eventtap.QuartzTapApi()
    tap = api.create(True, 1024, lambda *a: None)
    assert tap is not None
    api.enable(tap)
    api.read(object())
    paste.QuartzKeyPoster().post_paste(9)
    board = paste.CocoaPasteboard()
    board.snapshot()
    board.write_text("hello", (paste.CONCEALED,))
    board.restore([[("public.utf8-plain-text", b"x")]], (paste.TRANSIENT,))
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    assert permissions.accessibility_trusted() in (True, False)
    assert permissions.accessibility_trusted(prompt=True) in (True, False)
    assert permissions.input_monitoring_granted() in (True, False, None)
    assert permissions.microphone_status() in (*permissions.MIC_STATUS.values(), "unknown")
    assert permissions.request_microphone() is True
    seen = []
    assert permissions.open_settings("accessibility", run=seen.append) is True
    assert seen[0][1].endswith("Privacy_Accessibility")


def test_native_prompt_and_permission_menu(cocoa, monkeypatch, tmp_path):
    from shoutabout.config import Paths
    from shoutabout.mac import permissions
    from shoutabout.mac.platform import MacHub

    modules, _calls = cocoa
    alert = modules["AppKit"].NSAlert.alloc().init()
    alert.runModal.return_value = 1000
    hub = MacHub(Paths(tmp_path), HubState(), lambda t, m: None)
    pairing = MagicMock()
    prompt = types.SimpleNamespace(request_id="r", code="1234", device_name="Phone")
    hub.native_prompt(prompt, pairing)
    pairing.approve.assert_called_once_with("r")
    alert.runModal.return_value = 1001
    hub.native_prompt(prompt, pairing)
    pairing.deny.assert_called_once_with("r")
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    opened = []
    monkeypatch.setattr(permissions, "open_settings", lambda key, **kw: opened.append(key))
    monkeypatch.setattr(permissions, "microphone_status", lambda: "denied")
    for i in range(3):
        hub.open_permission(i)
    assert opened == ["accessibility", "microphone", "input_monitoring"]
    hub.request_permissions()


def test_hub_v2_wires_the_mac_parts(monkeypatch, tmp_path):
    from shoutabout import hub_v2
    from shoutabout.config import Config, Paths
    from shoutabout.mac.discovery_mac import MacAdvertiser
    from shoutabout.mac.startup_mac import MacStartupManager
    from shoutabout.mac.ui_bridge import MacUiBridge

    # Import what start() imports first: sys.platform is process-wide, and the standard
    # library reacts to "darwin" at import time (urllib wants _scproxy).
    for name in ("app_services", "devices", "discovery", "identity", "netinfo"):
        __import__(f"shoutabout.{name}")
    __import__("shoutabout.pair_requests")
    __import__("shoutabout.winstartup")
    __import__("shoutabout.mac.platform")
    monkeypatch.setattr(hub_v2.sys, "platform", "darwin")
    ctx = types.SimpleNamespace()
    from shoutabout.recent import Recent

    recent = Recent()
    v2 = hub_v2.start(
        Paths(tmp_path),
        Config(),
        ctx,
        HubState(),
        with_ui=True,
        notify=lambda t, m: None,
        recent=recent,
    )
    try:
        assert isinstance(v2.ui, MacUiBridge)
        assert isinstance(v2.advertiser, MacAdvertiser)
        assert isinstance(v2.services.startup, MacStartupManager)
        assert "right_cmd" in v2.services.hotkey_choices()
        assert callable(v2.services.missing_permissions)  # Home's status line
        # Recent lives in the hub; the window process hears when it changes.
        assert v2.services.recent is recent and v2.ui._on_recent_changed in recent._listeners
        assert ctx.pairing is v2.services.pairing and ctx.pairing.enabled
    finally:
        v2.advertiser = None  # nothing was announced
        v2.stop()
