from __future__ import annotations

import threading
import time

import pytest

from shoutabout import icons, tray
from shoutabout.inject import win32
from shoutabout.models import HubStatus
from shoutabout.state import HubState, Progress


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.event = threading.Event()

    def __getattr__(self, name):
        def fn():
            self.calls.append(name)
            self.event.set()

        return fn

    def actions(self, quit_fn=None, **extra) -> tray.TrayActions:
        return tray.TrayActions(toggle_pause=self.toggle_pause, quit=quit_fn or self.quit, **extra)

    def full_actions(self) -> tray.TrayActions:
        return self.actions(
            teach=self.teach,
            settings=self.settings,
            setup=self.setup,
            hint=lambda: "Hold Right Ctrl to talk",
        )


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("status", "paused", "error", "percent", "header"),
    [
        (HubStatus.STARTING, False, None, None, "Getting ready…"),
        (HubStatus.STARTING, False, None, 42, "Getting ready… 42%"),
        ("preparing", False, None, 7, "Getting ready… 7%"),
        ("preparing", False, None, 900, "Getting ready…"),
        (HubStatus.IDLE, False, None, None, "Ready"),
        ("listening", False, None, None, "Listening"),
        (HubStatus.TRANSCRIBING, False, None, None, "Writing…"),
        (HubStatus.IDLE, True, None, None, "Paused"),
        (HubStatus.TRANSCRIBING, True, None, None, "Writing… (paused)"),
        (HubStatus.ERROR, False, "CUDA failed\nstack", None, "Needs attention: CUDA failed"),
        (HubStatus.ERROR, False, None, None, "Needs attention: unknown error"),
        ("something-new", False, None, None, "Ready"),
    ],
)
def test_status_header(status, paused, error, percent, header):
    assert tray.status_header(status, paused, error, percent) == header
    assert tray.status_line("ShoutAbout", status, paused, error, percent) == (
        f"ShoutAbout — {header}"
    )


def test_long_error_is_shortened():
    line = tray.status_line("ShoutAbout", HubStatus.ERROR, False, "x" * 500)
    assert line.endswith("…")
    assert len(line) < 100
    tip = tray.tooltip_text("ShoutAbout", HubStatus.ERROR, False, "y" * 500)
    assert tray.utf16_len(tip) <= tray.TIP_MAX


def test_truncate_counts_utf16_units():
    assert tray.truncate("abc", 5) == "abc"
    assert tray.truncate("abcdef", 5) == "abcd…"
    emoji = "😀" * 10  # 2 UTF-16 units each
    out = tray.truncate(emoji, 7)
    assert tray.utf16_len(out) <= 7
    assert out.endswith("…")
    assert tray.truncate("a\0b", 5) == "ab"


def _labels(items):
    return [i.label if not i.separator else "-" for i in items]


def test_menu_is_short_and_plain():
    items = tray.build_menu(
        "ShoutAbout",
        HubStatus.IDLE,
        False,
        None,
        hint="Hold Right Ctrl to talk",
        teaching=True,
        settings=True,
        setup=True,
    )
    assert _labels(items) == [
        "Ready",
        "Hold Right Ctrl to talk",
        "-",
        "Pause",
        "Teach words…",
        "Settings…",
        "Set up…",
        "-",
        "Quit",
    ]
    assert items[0].enabled is False and items[1].enabled is False
    assert all(i.enabled for i in items[2:] if not i.separator)
    ids = [i.id for i in items if not i.separator]
    assert len(ids) == len(set(ids))


def test_menu_minimal_without_optional_actions():
    items = tray.build_menu("ShoutAbout", HubStatus.STARTING, False, None, percent=42)
    assert _labels(items) == ["Getting ready… 42%", "-", "Pause", "-", "Quit"]


def test_menu_items_paused_and_error_escape():
    items = tray.build_menu("ShoutAbout", HubStatus.ERROR, True, "R&D GPU gone")
    assert items[2].label == "Resume"
    assert items[0].label == "Needs attention: R&&D GPU gone (paused)"
    hinted = tray.build_menu("ShoutAbout", HubStatus.IDLE, False, None, hint="Hold A&B")
    assert hinted[1].label == "Hold A&&B"


def test_window_actions_dispatch():
    rec = Recorder()
    icon = tray.TrayIcon(HubState(), rec.full_actions())
    for cmd in (tray.CMD_TEACH, tray.CMD_SETTINGS, tray.CMD_SETUP):
        rec.event.clear()
        icon._dispatch(cmd)
        assert rec.event.wait(5)
    for _ in range(50):
        if len(rec.calls) == 3:
            break
        time.sleep(0.01)
    assert sorted(rec.calls) == ["settings", "setup", "teach"]


def test_missing_window_actions_are_ignored():
    rec = Recorder()
    icon = tray.TrayIcon(HubState(), rec.actions())
    icon._dispatch(tray.CMD_SETTINGS)
    icon._dispatch(tray.CMD_SETUP)
    icon._dispatch(tray.CMD_HINT)
    time.sleep(0.05)
    assert rec.calls == []


def test_hint_and_percent_never_raise():
    def boom():
        raise RuntimeError("no")

    icon = tray.TrayIcon(HubState(), Recorder().actions(hint=boom, percent=boom))
    assert icon._hint() is None
    assert icon._percent() is None
    icon = tray.TrayIcon(HubState(), Recorder().actions(hint=lambda: "", percent=lambda: 42.4))
    assert icon._hint() is None
    assert icon._percent() == 42


def test_percent_falls_back_to_hub_state_progress():
    state = HubState()
    state.set_status(HubStatus.STARTING)
    state.set_progress(Progress(done=63, total=100))  # the first-run model download
    icon = tray.TrayIcon(state, Recorder().actions())
    assert icon._percent() == 63


# ---------------------------------------------------------------- thread-safe API


def test_notify_and_stop_before_run_never_raise():
    state = HubState()
    icon = tray.TrayIcon(state, Recorder().actions())
    icon.notify("ShoutAbout", "queued")
    icon.notify("t" * 500, "m" * 5000)
    state.set_status(HubStatus.IDLE)  # listener with no window yet
    icon.stop()
    assert len(icon._notes) == 2


def test_notify_queue_is_bounded():
    icon = tray.TrayIcon(HubState(), Recorder().actions())
    for i in range(100):
        icon.notify("t", str(i))
    assert len(icon._notes) == tray.MAX_QUEUED_NOTES
    assert icon._notes[-1] == ("t", "99")


def test_dispatch_runs_actions_off_thread():
    rec = Recorder()
    icon = tray.TrayIcon(HubState(), rec.full_actions())
    main = threading.get_ident()
    seen = {}

    def toggle():
        seen["thread"] = threading.get_ident()
        rec.event.set()

    icon._actions.toggle_pause = toggle
    icon._dispatch(tray.CMD_PAUSE)
    assert rec.event.wait(5)
    assert seen["thread"] != main
    rec.event.clear()
    icon._dispatch(tray.CMD_SETTINGS)
    assert rec.event.wait(5)
    assert rec.calls[-1] == "settings"
    icon._dispatch(tray.CMD_STATUS)  # disabled item: nothing happens


def test_dispatch_quit_inline_and_stops():
    rec = Recorder()
    icon = tray.TrayIcon(HubState(), rec.actions())
    icon._dispatch(tray.CMD_QUIT)
    assert rec.calls == ["quit"]
    assert icon._stop_requested


def test_dispatch_quit_failure_still_stops():
    def bad_quit():
        raise RuntimeError("boom")

    icon = tray.TrayIcon(HubState(), Recorder().actions(quit_fn=bad_quit))
    icon._dispatch(tray.CMD_QUIT)
    assert icon._stop_requested


def test_failing_action_is_logged_not_raised(caplog):
    done = threading.Event()

    def bad():
        try:
            raise RuntimeError("nope")
        finally:
            done.set()

    icon = tray.TrayIcon(HubState(), Recorder().actions(settings=bad))
    icon._dispatch(tray.CMD_SETTINGS)
    assert done.wait(5)


# --------------------------------------------- icon states & animation (fake Win32)

HWND = 0x5A5A


class FakeUser32:
    """Just enough user32 for the tray's icon / timer logic, recording every call."""

    def __init__(self) -> None:
        self.next_handle = 0x1000
        self.created: dict[int, bytes] = {}  # HICON -> icon-resource bytes
        self.destroyed: list[int] = []
        self.timers: dict[int, int] = {}  # timer id -> interval ms
        self.set_timer_calls: list[tuple[int, int]] = []
        self.posted: list[int] = []
        self.fail_create = False

    def CreateIconFromResourceEx(self, blob, length, is_icon, version, cx, cy, flags):
        assert len(blob) == length and is_icon and (cx, cy) == (24, 24)
        if self.fail_create:
            return None
        self.next_handle += 1
        self.created[self.next_handle] = bytes(blob)
        return self.next_handle

    def DestroyIcon(self, hicon):
        assert hicon in self.created and hicon not in self.destroyed
        self.destroyed.append(hicon)
        return 1

    def SetTimer(self, hwnd, timer_id, ms, proc):
        assert hwnd == HWND
        self.timers[timer_id] = ms
        self.set_timer_calls.append((timer_id, ms))
        return timer_id

    def KillTimer(self, hwnd, timer_id):
        return 1 if self.timers.pop(timer_id, None) is not None else 0

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.posted.append(msg)
        return 1

    def GetSystemMetrics(self, index):
        return 24

    def IsWindow(self, hwnd):
        return 1

    def DestroyWindow(self, hwnd):
        return 1

    def PostQuitMessage(self, code):
        return None

    def DefWindowProcW(self, hwnd, msg, wparam, lparam):
        return 0

    @property
    def live(self) -> set[int]:
        return set(self.created) - set(self.destroyed)


class FakeShell32:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int | None, str]] = []  # (message, flags, hIcon, tip)
        self.fail_modify = False

    def Shell_NotifyIconW(self, message, pnid):
        nid = pnid._obj
        self.calls.append((message, nid.uFlags, nid.hIcon, nid.szTip))
        return 0 if (self.fail_modify and message == 1) else 1

    def icons(self, message: int = 1) -> list[int | None]:
        return [h for m, _, h, _ in self.calls if m == message]


@pytest.fixture
def fake_win(monkeypatch):
    user32, shell32 = FakeUser32(), FakeShell32()
    monkeypatch.setattr(win32, "IS_WINDOWS", True)
    monkeypatch.setattr(win32, "last_error", lambda: 0)  # ctypes.get_last_error is Windows-only
    monkeypatch.setattr(win32, "user32", user32)
    monkeypatch.setattr(win32, "shell32", shell32)
    return user32, shell32


def _fake_tray(state: HubState) -> tray.TrayIcon:
    icon = tray.TrayIcon(state, Recorder().actions())
    icon._hwnd = HWND
    icon._icon_size = 24
    icon._add_icon()
    return icon


def _pump(icon: tray.TrayIcon, msg: int, wparam: int = 0) -> None:
    icon._wndproc(HWND, msg, wparam, 0)


def _frame_resource(user32: FakeUser32, hicon: int | None) -> bytes:
    assert hicon is not None
    return user32.created[hicon]


def test_fake_tray_shows_brand_states_and_animates(fake_win):
    user32, shell32 = fake_win
    state = HubState()
    icon = _fake_tray(state)
    assert shell32.calls[0][0] == 0  # NIM_ADD, with the "starting" (hum) frame 0
    assert _frame_resource(user32, shell32.calls[0][2]) == icons.icon_image("starting", 24, 0)
    assert user32.timers == {tray.TIMER_ANIMATE: icons.frame_interval_ms("starting")}

    def show(status=None, paused=None):
        if status is not None:
            state.set_status(status)
        if paused is not None:
            state.set_paused(paused)
        _pump(icon, tray.WM_TRAY_STATE)
        return _frame_resource(user32, shell32.icons()[-1])

    assert show(HubStatus.IDLE) == icons.icon_image("idle", 24)
    assert tray.TIMER_ANIMATE not in user32.timers  # still icon: no timer

    assert show(HubStatus.TRANSCRIBING) == icons.icon_image("transcribing", 24, 0)
    talk_ms = icons.frame_interval_ms("transcribing")
    assert user32.timers == {tray.TIMER_ANIMATE: talk_ms}
    n = icons.frame_count("transcribing", 24)
    for i in range(1, 2 * n + 1):  # two full loops of the talking mouth
        _pump(icon, win32.WM_TIMER, tray.TIMER_ANIMATE)
        assert _frame_resource(user32, shell32.icons()[-1]) == icons.icon_image(
            "transcribing", 24, i % n
        )
    # every NIM_MODIFY keeps the tooltip and callback (NIF_SHOWTIP etc.)
    assert all(flags & 0x80 and flags & 0x04 for m, flags, _, _ in shell32.calls if m == 1)

    # paused while transcribing: keeps talking; paused when idle: shut mouth, still
    assert show(paused=True) == icons.icon_image("transcribing", 24, 0)
    assert user32.timers == {tray.TIMER_ANIMATE: talk_ms}
    assert show(HubStatus.IDLE) == icons.icon_image("paused", 24)
    assert tray.TIMER_ANIMATE not in user32.timers
    assert show(HubStatus.ERROR, paused=False) == icons.icon_image("error", 24)
    assert tray.TIMER_ANIMATE not in user32.timers

    # the frames were made once per state, not per tick
    created_once = len(user32.created)
    assert created_once == sum(icons.frame_count(k, 24) for k in icons.STATES)
    assert show(HubStatus.TRANSCRIBING) == icons.icon_image("transcribing", 24, 0)  # restarts
    _pump(icon, win32.WM_TIMER, tray.TIMER_ANIMATE)
    assert len(user32.created) == created_once

    _pump(icon, win32.WM_CLOSE)
    assert user32.timers == {}  # the animation timer dies with the icon
    icon._cleanup()
    assert user32.live == set()  # every HICON destroyed
    assert icon._icons == {}


def test_fake_tray_stale_tick_shows_the_new_state(fake_win):
    user32, shell32 = fake_win
    state = HubState()
    state.set_status(HubStatus.TRANSCRIBING)
    icon = _fake_tray(state)
    assert user32.timers.get(tray.TIMER_ANIMATE)
    state.set_status(HubStatus.IDLE)  # the WM_TRAY_STATE message is still queued...
    _pump(icon, win32.WM_TIMER, tray.TIMER_ANIMATE)  # ...when a frame tick arrives
    assert _frame_resource(user32, shell32.icons()[-1]) == icons.icon_image("idle", 24)
    assert tray.TIMER_ANIMATE not in user32.timers
    icon._cleanup()
    assert user32.live == set()


def test_fake_tray_icon_failures_are_contained(fake_win, caplog):
    user32, shell32 = fake_win
    user32.fail_create = True
    state = HubState()
    state.set_status(HubStatus.TRANSCRIBING)
    icon = _fake_tray(state)  # logs, adds the icon without an image, no timer
    assert shell32.calls[0][2] is None
    assert tray.TIMER_ANIMATE not in user32.timers
    user32.fail_create = False
    _pump(icon, tray.WM_TRAY_STATE)  # recovers on the next refresh
    assert shell32.icons()[-1] is not None
    assert user32.timers.get(tray.TIMER_ANIMATE)
    shell32.fail_modify = True
    caplog.clear()
    for _ in range(10):
        _pump(icon, win32.WM_TIMER, tray.TIMER_ANIMATE)
    assert sum("tray icon update failed" in r.message for r in caplog.records) == 1
    icon._cleanup()
    assert user32.live == set()


def test_fake_tray_taskbar_recreated_with_a_new_icon_size(fake_win, monkeypatch):
    user32, shell32 = fake_win
    icon = _fake_tray(HubState())
    icon._taskbar_created_msg = 0xC123
    old = set(user32.created)
    monkeypatch.setattr(icons, "small_icon_size", lambda: 24)
    _pump(icon, 0xC123)  # same size: icons are reused
    assert set(user32.created) == old and shell32.calls[-1][0] in (0, 4)
    monkeypatch.setattr(icons, "small_icon_size", lambda: 32)
    calls = []

    def make(key, size=None):
        calls.append((key, size))
        return [0xBEEF0 + i for i in range(icons.frame_count(key, size))]

    monkeypatch.setattr(icons, "make_hicons", make)
    _pump(icon, 0xC123)
    assert calls == [("starting", 32)]
    assert old <= set(user32.destroyed)  # the old-size icons are gone


# ---------------------------------------------------------------- real Windows


def _gui_resources() -> tuple[int, int]:
    import ctypes

    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    user32.GetGuiResources.restype = ctypes.c_uint32
    process = kernel32.GetCurrentProcess()
    return user32.GetGuiResources(process, 0), user32.GetGuiResources(process, 1)  # GDI, USER


def _run_real_tray(state: HubState, script) -> tuple[tray.TrayIcon, list[int]]:
    """Run a real TrayIcon while ``script(icon)`` drives it from another thread; returns
    the icon and the hIcon of every NIM_MODIFY."""
    icon = tray.TrayIcon(state, Recorder().actions(), title="ShoutAbout test")
    shown: list[int] = []
    real_notify = icon._shell_notify

    def recording_notify(message, nid):
        if message == win32.NIM_MODIFY:
            shown.append(nid.hIcon)
        return real_notify(message, nid)

    icon._shell_notify = recording_notify
    errors: list[BaseException] = []

    def driver():
        try:
            deadline = time.monotonic() + 5
            while icon._hwnd is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert icon._hwnd
            script(icon)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            icon.stop()

    t = threading.Thread(target=driver)
    t.start()
    started = time.monotonic()
    icon.run()
    t.join(5)
    assert not errors
    assert time.monotonic() - started < 15
    assert icon._hwnd is None
    assert icon._icons == {}  # icons destroyed
    assert icon._timer_ms == 0
    return icon, shown


@pytest.mark.windows
def test_real_tray_runs_updates_animates_and_stops(monkeypatch):
    made: list[tuple[str, int | None]] = []
    destroyed: list[int] = []
    created: list[int] = []
    real_make, real_destroy = icons.make_hicons, icons.destroy_hicon

    def recording_make(key, size=None):
        made.append((key, size))
        handles = real_make(key, size)
        created.extend(handles)
        return handles

    def recording_destroy(hicon):
        destroyed.append(hicon)
        real_destroy(hicon)

    monkeypatch.setattr(icons, "make_hicons", recording_make)
    monkeypatch.setattr(icons, "destroy_hicon", recording_destroy)
    state = HubState()
    talk: list[int] = []

    def script(icon):
        for status in (HubStatus.IDLE, HubStatus.TRANSCRIBING):
            state.set_status(status)
            time.sleep(0.1)
        time.sleep(1.2)  # ~9 frames of talking
        talk.extend(icon._icons["transcribing"])
        state.set_status(HubStatus.IDLE)
        time.sleep(0.1)
        state.set_paused(True)
        time.sleep(0.1)
        state.set_status(HubStatus.ERROR, "test error")
        time.sleep(0.2)
        state.set_paused(False)

    _, shown = _run_real_tray(state, script)
    assert {key for key, _ in made} == set(icons.STATES)
    assert len(made) == len(icons.STATES)  # each state's frames made once
    assert sorted(created) == sorted(destroyed)  # ...and all destroyed
    assert len(set(shown) & set(talk)) >= 6  # the mouth really talked


@pytest.mark.windows
def test_real_tray_does_not_leak_handles():
    def cycles(icon):
        state = icon._state
        for _ in range(4):
            state.set_status(HubStatus.TRANSCRIBING)
            time.sleep(0.4)
            state.set_status(HubStatus.IDLE)
            state.set_paused(True)
            time.sleep(0.05)
            state.set_paused(False)
            state.set_status(HubStatus.ERROR, "x")
            time.sleep(0.05)

    _run_real_tray(HubState(), cycles)  # warm-up: lazily loaded system resources
    before = _gui_resources()
    _run_real_tray(HubState(), cycles)
    _run_real_tray(HubState(), cycles)
    after = _gui_resources()
    assert after[0] <= before[0] + 2, f"GDI objects {before[0]} -> {after[0]}"
    assert after[1] <= before[1] + 2, f"USER objects {before[1]} -> {after[1]}"


@pytest.mark.windows
def test_real_tray_stop_before_run():
    icon = tray.TrayIcon(HubState(), Recorder().actions())
    icon.stop()
    icon.run()  # returns promptly
    assert icon._hwnd is None


def test_recent_item_sits_after_pause_and_opens_the_window():
    items = tray.build_menu(
        "ShoutAbout", HubStatus.IDLE, False, None, recent=True, teaching=True, settings=True
    )
    assert _labels(items) == [
        "Ready",
        "-",
        "Pause",
        "Recent…",
        "Teach words…",
        "Settings…",
        "-",
        "Quit",
    ]
    assert "Recent…" not in _labels(tray.build_menu("ShoutAbout", HubStatus.IDLE, False, None))
    rec = Recorder()
    icon = tray.TrayIcon(HubState(), rec.actions(recent=rec.recent))
    icon._dispatch(tray.CMD_RECENT)
    assert rec.event.wait(5) and rec.calls == ["recent"]
