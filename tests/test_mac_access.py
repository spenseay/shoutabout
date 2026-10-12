"""macOS permissions with fakes: what the setup lists, how the hub asks, the window
process's remote handle, the hotkey watcher, the menu lines, the menu bar icon and the
Dock icon."""

from __future__ import annotations

import io
import struct
import zlib
from types import SimpleNamespace

import pytest

from shoutabout import brand
from shoutabout.config import Config, Paths
from shoutabout.local_dictation import DictationState
from shoutabout.mac import access, menubar, permissions, platform, tkmac
from shoutabout.mac.access import HubAccess, RemoteAccess
from shoutabout.mac.permissions import PermissionReport
from shoutabout.state import HubState


def st(mic="granted", ax=True, im=None, hotkey=None) -> dict:
    return {"microphone": mic, "accessibility": ax, "input_monitoring": im, "hotkey": hotkey}


# ------------------------------------------------------------------ pure


def test_status_is_plain_values_for_the_link():
    assert access.status_of(PermissionReport("denied", False, None), True) == st(
        "denied", False, None, True
    )


@pytest.mark.parametrize(
    ("status", "missing", "hear", "talk", "paste"),
    [
        (st("undetermined", False, False), ["microphone", "accessibility"], False, False, False),
        (st("granted", True, False), [], True, True, True),
        (st("denied", True, None), ["microphone"], True, False, True),
        (st("granted", False, True), ["accessibility"], True, True, False),  # managed Mac
        (st("unknown", None, None), [], True, True, True),  # can't tell: never blocks
        (st("granted", None, False), ["input_monitoring"], False, False, True),
        (st("restricted", False, False), ["microphone", "accessibility"], False, False, False),
    ],
)
def test_what_is_missing(status, missing, hear, talk, paste):
    assert access.missing(status) == missing
    assert access.can_hear_hotkey(status) is hear
    assert access.can_talk(status) is talk
    assert access.can_paste(status) is paste


def test_rows_list_input_monitoring_only_without_an_accessibility_answer():
    """Accessibility lets the event tap hear the key (an ordinary tap), so Input Monitoring
    is only the way when Accessibility can't be read."""
    assert access.rows(st(ax=False, im=False)) == ["microphone", "accessibility"]
    assert access.rows(st(ax=None, im=False)) == ["microphone", "accessibility", "input_monitoring"]


def test_notice_words():
    assert access.notice(st()) is None
    assert access.notice(st("undetermined", False, False)) == (
        "ShoutAbout needs access",
        "Click to allow the microphone and Accessibility.",
    )
    assert access.notice(st("granted", False, True))[1] == "Click to allow Accessibility."
    assert access.notice(st("denied", None, False))[1] == (
        "Click to allow the microphone and Input Monitoring."
    )


# ------------------------------------------------------------------ the hub asks


class FakeApi:
    """shoutabout.mac.permissions, recorded."""

    def __init__(self, mic="undetermined", ax=False, im=False, request_ok=True) -> None:
        self.mic, self.ax, self.im = mic, ax, im
        self.request_ok = request_ok
        self.calls: list[tuple] = []

    def report(self) -> PermissionReport:
        return PermissionReport(self.mic, self.ax, self.im)

    def microphone_status(self) -> str:
        return self.mic

    def request_microphone(self) -> bool:
        self.calls.append(("request_microphone",))
        return self.request_ok

    def accessibility_trusted(self, prompt: bool = False):
        if prompt:
            self.calls.append(("prompt_accessibility",))
        return self.ax

    def input_monitoring_granted(self):
        return self.im

    def request_input_monitoring(self):
        self.calls.append(("request_input_monitoring",))
        return False

    def open_settings(self, which: str) -> bool:
        self.calls.append(("open", which))
        return True


def test_microphone_dialog_first_then_system_settings():
    api = FakeApi()
    hub = HubAccess(api=api)
    assert hub.ask("microphone") == "asked"  # macOS's own dialog
    assert api.calls == [("request_microphone",)]
    # Asked already (the dialog may never have shown): System Settings, every time.
    assert hub.ask("microphone") == "opened"
    assert api.calls[-1] == ("open", "microphone")
    api.mic = "denied"
    assert HubAccess(api=api).ask("microphone") == "opened"
    api.mic = "granted"
    assert hub.ask("microphone") == "granted"


def test_microphone_request_that_fails_opens_system_settings():
    api = FakeApi(request_ok=False)
    assert HubAccess(api=api).ask("microphone") == "opened"
    assert api.calls == [("request_microphone",), ("open", "microphone")]


def test_accessibility_prompts_and_opens_the_pane():
    """The prompt puts ShoutAbout in the Accessibility list (and shows macOS's dialog);
    the pane opens too, so there is always a switch to flip."""
    api = FakeApi()
    hub = HubAccess(api=api)
    assert hub.ask("accessibility") == "opened"
    assert api.calls == [("prompt_accessibility",), ("open", "accessibility")]
    api.ax = True
    assert hub.ask("accessibility") == "granted"
    assert hub.ask("input_monitoring") == "opened"
    assert api.calls[-2:] == [("request_input_monitoring",), ("open", "input_monitoring")]
    with pytest.raises(ValueError):
        hub.ask("camera")


def test_status_says_when_it_changed():
    api = FakeApi()
    changes: list[int] = []
    on = {"value": False}
    hub = HubAccess(api=api, hotkey=lambda: on["value"], on_change=lambda: changes.append(1))
    assert hub.status() == st("undetermined", False, False, False)
    hub.status()
    assert changes == []  # the same answer: nothing to wake
    api.ax = True
    assert hub.status()["accessibility"] is True
    assert changes == [1]  # the hub's watcher wakes up now, not in a few seconds


def test_hub_access_uses_the_permissions_module_by_default(monkeypatch):
    monkeypatch.setattr(permissions, "report", lambda: PermissionReport("granted", True, None))
    assert HubAccess().status() == st()


# ------------------------------------------------------------------ the window's side


class FakeLink:
    def __init__(self, answers=None, fail=False) -> None:
        self.answers = answers or {}
        self.fail = fail
        self.calls: list[tuple] = []

    def call(self, op, timeout=10.0, **args):
        self.calls.append((op, args))
        if self.fail:
            raise ConnectionError("the hub went away")
        return self.answers.get(op)


def test_remote_access_asks_the_hub():
    link = FakeLink({"permissions": st(), "allow": "asked"})
    remote = RemoteAccess(link)
    assert remote.status() == st()
    assert remote.ask("microphone") == "asked"
    assert link.calls[-1] == ("allow", {"which": "microphone"})


def test_remote_access_falls_back_to_system_settings():
    opened: list[str] = []
    remote = RemoteAccess(FakeLink(fail=True), open_pane=opened.append)
    assert remote.status() is None  # unknown: the setup doesn't block on it
    assert remote.ask("accessibility") == "opened" and opened == ["accessibility"]
    assert RemoteAccess(FakeLink({"permissions": "junk"})).status() is None


# ------------------------------------------------------------------ menu lines


def test_menu_lines_ask_for_the_microphone_before_macos_did():
    report = PermissionReport("undetermined", False, False)
    assert [label for _n, label in platform.permission_items(report)] == [
        "Allow the microphone…",
        "Allow pasting (Accessibility)…",
        "Allow the shortcut key (Input Monitoring)…",
    ]
    assert platform.permission_items(PermissionReport("granted", True, None)) == []


# ------------------------------------------------------------------ the hotkey watcher


class FakeDictation:
    def __init__(self, state=DictationState.OFF) -> None:
        self.state = state


class FakeVoice:
    def __init__(self, state=DictationState.OFF) -> None:
        self.cfg = Config()
        self.dictation = FakeDictation(state)
        self.reloads = 0

    def reload_dictation(self, _cfg) -> None:
        self.reloads += 1
        self.dictation.state = DictationState.READY


def watcher(tmp_path, voice) -> platform.MacHub:
    hub = platform.MacHub(Paths(tmp_path), HubState(), lambda t, m: None)
    hub._voice = voice
    return hub


def test_watcher_starts_the_hotkey_once_allowed(tmp_path):
    voice = FakeVoice()
    hub = watcher(tmp_path, voice)
    hub._grants = (False, False)
    assert not hub.check_hotkey(PermissionReport("granted", False, False))
    assert hub.check_hotkey(PermissionReport("granted", True, False))  # Accessibility on
    assert voice.reloads == 1 and voice.dictation.state == DictationState.READY
    assert not hub.check_hotkey(PermissionReport("granted", True, False))  # no news
    assert hub._hotkey_on() is True


def test_watcher_remakes_a_tap_made_before_input_monitoring(tmp_path):
    """A listen-only tap made before Input Monitoring was allowed may hear nothing: when it
    turns on, the listener is made again even though it seemed to run."""
    voice = FakeVoice(DictationState.READY)
    hub = watcher(tmp_path, voice)
    hub._grants = (True, False)
    assert hub.check_hotkey(PermissionReport("granted", True, True))
    assert voice.reloads == 1


def test_watcher_never_interrupts_a_dictation(tmp_path):
    voice = FakeVoice(DictationState.LISTENING)
    hub = watcher(tmp_path, voice)
    hub._grants = (False, False)
    assert not hub.check_hotkey(PermissionReport("granted", True, False))
    voice.dictation.state = DictationState.READY
    assert hub.check_hotkey(PermissionReport("granted", True, False))  # the next round
    assert voice.reloads == 1


def test_watcher_retries_an_off_listener_once_and_follows_pause(tmp_path):
    voice = FakeVoice()
    voice.reload_dictation = lambda _cfg: setattr(voice, "reloads", voice.reloads + 1)
    hub = watcher(tmp_path, voice)
    hub._grants = (True, False)
    allowed = PermissionReport("granted", True, False)
    assert hub.check_hotkey(allowed)  # allowed but off (it failed for another reason)
    assert not hub.check_hotkey(allowed)  # only once
    hub.state.set_paused(True)
    hub._grants = (False, False)
    assert not hub.check_hotkey(allowed)  # paused: Resume starts it


def test_watcher_wakes_at_once(tmp_path, monkeypatch):
    """The setup window's poll wakes the watcher, so Try it works right after Allow."""
    import time

    voice = FakeVoice()
    hub = watcher(tmp_path, voice)
    now = {"report": PermissionReport("granted", False, False)}
    monkeypatch.setattr(permissions, "report", lambda: now["report"])
    monkeypatch.setattr(platform, "PERMISSION_POLL_S", 30.0)
    hub.access.status()  # at start (start_permissions)
    hub.watch_permissions(voice)
    try:
        now["report"] = PermissionReport("granted", True, False)
        hub.access.status()  # what the setup window's poll does: a change wakes it
        for _ in range(200):
            if voice.reloads:
                break
            time.sleep(0.01)
        assert voice.reloads == 1
    finally:
        hub.stop()


# ------------------------------------------------------------------ the menu bar icon


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == brand.PNG_SIGNATURE
    return struct.unpack(">II", data[16:24])


def png_rgba(data: bytes) -> bytes:
    """Decode our own PNG writer's output (8-bit RGBA, filter 0 rows)."""
    width, height = png_size(data)
    pos, idat = 8, b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = io.BytesIO(zlib.decompress(idat))
    rows = []
    for _ in range(height):
        assert raw.read(1) == b"\0"
        rows.append(raw.read(width * 4))
    return b"".join(rows)


def red_pixels(img: brand.Image) -> int:
    px = img.rgba
    return sum(
        1
        for i in range(0, len(px), 4)
        if px[i] > 200 and px[i + 1] < 80 and px[i + 2] < 80 and px[i + 3] > 200
    )


def test_menu_bar_rests_on_the_mouth_with_its_tongue():
    idle = brand.menubar_frames("idle", 36)[0]
    assert red_pixels(idle) > 20  # the tongue
    assert idle.pixel(18, 2)[:3] == brand.YELLOW  # on the yellow tile (light and dark bars)
    for state in brand.TRAY_STATES:  # the same icons as the Windows tray
        assert brand.menubar_frames(state, 36) == brand.tray_frames(state, 36)


def test_menu_bar_icon_has_a_bitmap_per_screen_scale():
    frames = menubar.icon_pngs("idle")
    assert len(frames) == 1
    assert [png_size(p) for p in frames[0]] == [(18, 18), (36, 36), (54, 54)]
    assert png_rgba(frames[0][1]) == brand.menubar_frames("idle", 36)[0].rgba
    talking = menubar.icon_pngs("transcribing")
    assert len(talking) == brand.TALK_FRAMES and all(len(f) == 3 for f in talking)


# ------------------------------------------------------------------ the Dock icon


def test_tk_never_replaces_the_dock_icon_with_a_small_photo(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(tkmac, "_dock_done", False)
    import sys
    import types

    bundle = SimpleNamespace(objectForInfoDictionaryKey_=lambda key: "ShoutAbout.icns")
    foundation = types.ModuleType("Foundation")
    foundation.NSBundle = SimpleNamespace(mainBundle=lambda: seen.append(1) or bundle)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    window = SimpleNamespace()  # no Tk call is made on it
    assert tkmac.keep_dock_icon(window, False, object()) is None
    assert tkmac.keep_dock_icon(window, True, object()) is None
    assert seen == [1]  # looked once; the app's .icns stays in the Dock


def test_dock_icon_from_source_is_large_and_on_the_icon_grid():
    png = tkmac.dock_icon_png(64)
    assert png_size(png) == (64, 64)
    rgba = png_rgba(png)
    assert rgba[3] == 0  # transparent margin (Apple's grid)
    centre = (32 * 64 + 32) * 4
    assert rgba[centre + 3] == 255
    assert tkmac.DOCK_PX >= 512
