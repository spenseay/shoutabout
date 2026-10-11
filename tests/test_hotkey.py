"""The hotkey state machine, event by event, and the Win32 glue with a fake user32.

No test here installs a real keyboard hook.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from shoutabout import hotkey
from shoutabout.hotkey import (
    HOLD_DELAY_S,
    HOTKEY_CHOICES,
    HOTKEY_VKS,
    LLKHF_INJECTED,
    LLKHF_UP,
    RELEASE_GRACE_S,
    TAP_MAX_S,
    Action,
    HotkeyEvent,
    HotkeyListener,
    HotkeyMachine,
    KeyboardHook,
)

RCTRL = HOTKEY_VKS["right_ctrl"]
ESC = hotkey.VK_ESCAPE
KEY_C = 0x43
KEY_V = 0x56
KEY_T = 0x54
LSHIFT = 0xA0

START = HotkeyEvent(Action.START, "hold")
STOP = HotkeyEvent(Action.STOP, "release")


def actions(events):
    return [e.action for e in events]


# --------------------------------------------------------------------- choices


def test_choices_have_labels_and_key_codes():
    assert HOTKEY_CHOICES["right_ctrl"] == "Right Ctrl"
    assert list(HOTKEY_CHOICES)[0] == hotkey.DEFAULT_HOTKEY == "right_ctrl"
    for key in ("right_ctrl", "right_alt", "f8", "f9", "f10", "scroll_lock"):
        assert key in HOTKEY_CHOICES
    assert set(HOTKEY_CHOICES) == set(HOTKEY_VKS)
    assert hotkey.hotkey_label("f9") == "F9"
    assert hotkey.hotkey_label("nope") == "nope"


# ------------------------------------------------------------------- hold mode


def test_hold_starts_after_the_delay_and_stops_on_release():
    m = HotkeyMachine(RCTRL, "hold")
    assert m.key(RCTRL, True, 0.0) == []
    assert m.deadline() == pytest.approx(HOLD_DELAY_S)
    assert m.tick(0.1) == []
    assert m.tick(HOLD_DELAY_S) == [START]
    assert m.listening
    assert m.key(RCTRL, True, 0.5) == []  # auto-repeat
    assert m.key(RCTRL, False, 3.0) == [STOP]
    assert m.state == "idle" and not m.busy()


def test_hold_tap_does_nothing():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(RCTRL, True, 0.0)
    assert m.key(RCTRL, False, 0.1) == []
    assert m.tick(1.0) == []
    assert m.state == "idle"


def test_hold_shortcut_before_listening_is_ignored():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(RCTRL, True, 0.0)
    assert m.key(KEY_C, True, 0.05) == []  # Right Ctrl + C
    assert m.tick(1.0) == []
    assert m.key(KEY_C, False, 1.1) == []
    assert m.key(RCTRL, False, 1.2) == []
    assert m.state == "idle"


def test_hold_shortcut_while_listening_cancels_silently():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(RCTRL, True, 0.0)
    m.tick(0.3)
    assert m.key(KEY_C, True, 0.5) == [HotkeyEvent(Action.CANCEL, "shortcut")]
    assert m.key(RCTRL, False, 0.6) == []  # no STOP after a cancel
    assert m.state == "idle"


def test_escape_cancels_while_listening():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(RCTRL, True, 0.0)
    m.tick(0.3)
    assert m.key(ESC, True, 1.0) == [HotkeyEvent(Action.CANCEL, "escape")]
    assert m.key(ESC, False, 1.1) == []
    assert m.key(RCTRL, False, 1.2) == []
    assert m.state == "idle"


def test_hotkey_pressed_while_another_key_is_held_is_a_shortcut():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(LSHIFT, True, 0.0)
    m.key(RCTRL, True, 0.1)
    assert m.tick(1.0) == []
    m.key(RCTRL, False, 1.1)
    m.key(LSHIFT, False, 1.2)
    # Shift released: Right Ctrl alone works again
    m.key(RCTRL, True, 2.0)
    assert m.tick(2.3) == [START]


def test_stale_held_key_does_not_block_forever():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(KEY_C, True, 0.0)  # its key-up was never seen (e.g. the lock screen took it)
    m.key(RCTRL, True, 10.0)
    assert m.tick(10.3) == [START]


def test_missed_release_is_detected_from_the_physical_key():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(RCTRL, True, 0.0)
    m.tick(0.3, hotkey_down=True)
    assert m.listening
    assert m.tick(5.0, hotkey_down=False) == [STOP]
    assert m.state == "idle"


def test_force_stop_ignores_the_rest_of_the_press():
    m = HotkeyMachine(RCTRL, "hold")
    m.key(RCTRL, True, 0.0)
    m.tick(0.3)
    m.force_stop()  # e.g. the time limit
    assert m.state == "blocked"
    assert m.key(RCTRL, False, 1.0) == []
    assert m.state == "idle"
    m.force_stop()
    assert m.state == "idle"


def test_unknown_mode():
    with pytest.raises(ValueError):
        HotkeyMachine(RCTRL, "double-tap")  # type: ignore[arg-type]


# ----------------------------------------------------------------- toggle mode


def test_toggle_press_to_start_press_to_stop():
    m = HotkeyMachine(RCTRL, "toggle")
    assert m.key(RCTRL, True, 0.0) == []
    assert m.key(RCTRL, False, 0.1) == [HotkeyEvent(Action.START, "press")]
    assert m.listening
    assert m.key(KEY_C, True, 1.0) == []  # typing while listening is fine in toggle mode
    m.key(KEY_C, False, 1.1)
    assert m.key(RCTRL, True, 2.0) == []
    assert m.key(RCTRL, False, 2.1) == [HotkeyEvent(Action.STOP, "press")]
    assert m.state == "idle"


def test_toggle_shortcut_does_not_toggle():
    m = HotkeyMachine(RCTRL, "toggle")
    m.key(RCTRL, True, 0.0)
    m.key(KEY_C, True, 0.1)
    assert m.key(RCTRL, False, 0.2) == []
    m.key(KEY_C, False, 0.3)
    assert m.state == "idle"
    # and a shortcut while listening doesn't stop it
    m.key(RCTRL, True, 1.0)
    m.key(RCTRL, False, 1.1)
    m.key(RCTRL, True, 2.0)
    m.key(KEY_C, True, 2.1)
    assert m.key(RCTRL, False, 2.2) == []
    assert m.listening


def test_toggle_escape_cancels():
    m = HotkeyMachine(RCTRL, "toggle")
    m.key(RCTRL, True, 0.0)
    m.key(RCTRL, False, 0.1)
    assert m.key(ESC, True, 1.0) == [HotkeyEvent(Action.CANCEL, "escape")]
    assert m.state == "idle"
    m.key(ESC, False, 1.1)
    # Esc while the hotkey is pressed a second time also cancels
    m.key(RCTRL, True, 2.0)
    m.key(RCTRL, False, 2.1)
    m.key(RCTRL, True, 3.0)
    assert m.key(ESC, True, 3.1) == [HotkeyEvent(Action.CANCEL, "escape")]
    assert m.key(RCTRL, False, 3.2) == []
    assert m.state == "idle"


def test_toggle_press_while_another_key_is_held_is_ignored():
    m = HotkeyMachine(RCTRL, "toggle")
    m.key(ESC, True, 0.0)
    m.key(RCTRL, True, 0.5)
    assert m.key(RCTRL, False, 0.6) == []
    assert m.state == "idle"


def test_toggle_is_the_default_and_its_deadline_is_the_tap_limit():
    assert HotkeyMachine(RCTRL).mode == "toggle" == hotkey.DEFAULT_MODE
    m = HotkeyMachine(RCTRL, "toggle")
    m.key(RCTRL, True, 0.0)
    assert m.deadline() == pytest.approx(TAP_MAX_S)
    assert m.tick(5.0, hotkey_down=True) == []
    assert m.state == "blocked"  # held too long: not a tap
    assert m.key(RCTRL, False, 5.1) == []
    assert m.state == "idle" and m.deadline() is None


def play(m: HotkeyMachine, steps, *, lag: bool = True) -> list[HotkeyEvent]:
    """Feed (vk, down, t) steps the way the listener does: after each event a tick, with the
    physical key read as UP right after a key-down when ``lag`` (Windows updates the async
    key state only after the low-level hook has seen the key)."""
    out: list[HotkeyEvent] = []
    for vk, down, t in steps:
        out += m.tick(t)
        out += m.key(vk, down, t)
        physical = False if lag else (m._down if vk == m.vk else None)
        out += m.tick(t + 0.001, hotkey_down=physical)
    return out


@pytest.mark.parametrize("mode", ["toggle", "hold"])
@pytest.mark.parametrize(
    "shortcut",
    [
        [(RCTRL, True, 0.0), (KEY_C, True, 0.08), (KEY_C, False, 0.15), (RCTRL, False, 0.2)],
        [(RCTRL, True, 0.0), (KEY_V, True, 0.05), (RCTRL, False, 0.1), (KEY_V, False, 0.12)],
        # Ctrl + Shift + T, typed fast
        [
            (RCTRL, True, 0.0),
            (LSHIFT, True, 0.03),
            (KEY_T, True, 0.06),
            (KEY_T, False, 0.1),
            (LSHIFT, False, 0.12),
            (RCTRL, False, 0.14),
        ],
        # Shift first, then Right Ctrl: a shortcut too
        [(LSHIFT, True, 0.0), (RCTRL, True, 0.05), (RCTRL, False, 0.1), (LSHIFT, False, 0.2)],
    ],
)
def test_shortcuts_never_start_listening(mode, shortcut):
    for lag in (True, False):
        m = HotkeyMachine(RCTRL, mode)
        assert play(m, shortcut, lag=lag) == [], (mode, lag)
        assert m.tick(5.0, hotkey_down=False) == []
        assert m.state == "idle"


def test_toggle_clean_tap_starts_at_the_key_up_even_with_a_lagging_key_state():
    m = HotkeyMachine(RCTRL, "toggle")
    events = play(m, [(RCTRL, True, 0.0)])
    assert events == [] and m.state == "armed"  # nothing at the key-down
    assert play(m, [(RCTRL, False, 0.12)]) == [HotkeyEvent(Action.START, "press")]


def test_toggle_long_press_is_not_a_tap():
    m = HotkeyMachine(RCTRL, "toggle")
    m.key(RCTRL, True, 0.0)
    assert m.key(RCTRL, False, TAP_MAX_S + 0.05) == []  # even without a tick in between
    assert m.state == "idle"
    m.key(RCTRL, True, 1.0)
    assert m.key(RCTRL, False, 1.0 + TAP_MAX_S) == [HotkeyEvent(Action.START, "press")]


def test_toggle_listening_survives_typing_and_shortcuts_then_stops_on_a_clean_press():
    m = HotkeyMachine(RCTRL, "toggle")
    play(m, [(RCTRL, True, 0.0), (RCTRL, False, 0.1)])
    assert m.listening
    typing = [(KEY_C, True, 1.0), (KEY_C, False, 1.05), (LSHIFT, True, 1.1), (KEY_T, True, 1.15)]
    typing += [(KEY_T, False, 1.2), (LSHIFT, False, 1.25)]
    assert play(m, typing) == []
    # Right Ctrl + V while listening pastes and keeps listening
    assert play(m, [(RCTRL, True, 2.0), (KEY_V, True, 2.05), (KEY_V, False, 2.1)]) == []
    assert play(m, [(RCTRL, False, 2.15)]) == [] and m.listening
    # Shift held, then Right Ctrl: a shortcut, not a stop
    assert play(m, [(LSHIFT, True, 3.0), (RCTRL, True, 3.05), (RCTRL, False, 3.1)]) == []
    assert m.listening
    play(m, [(LSHIFT, False, 3.2)])
    # a clean press of any length stops it, at the key-up
    assert play(m, [(RCTRL, True, 4.0)]) == []
    assert m.tick(4.9, hotkey_down=True) == []
    assert play(m, [(RCTRL, False, 5.0)]) == [HotkeyEvent(Action.STOP, "press")]
    assert m.state == "idle"


def test_a_lagging_physical_key_never_counts_as_a_release_at_once():
    m = HotkeyMachine(RCTRL, "toggle")
    m.key(RCTRL, True, 0.0)
    assert m.tick(0.001, hotkey_down=False) == []
    assert m.tick(RELEASE_GRACE_S - 0.01, hotkey_down=False) == []
    assert m.state == "armed"
    m = HotkeyMachine(RCTRL, "hold")
    m.key(RCTRL, True, 0.0)
    assert m.tick(0.001, hotkey_down=False) == []
    assert m.tick(HOLD_DELAY_S, hotkey_down=True) == [START]
    # a release the hook never saw (Win+L) is still caught, after the grace period
    assert m.tick(HOLD_DELAY_S + 0.01, hotkey_down=False) == []
    assert m.tick(RELEASE_GRACE_S + 0.5, hotkey_down=False) == [STOP]


def test_hold_press_with_a_lagging_key_state_starts_on_time():
    """The bug: the hold started only on the key's auto-repeat, ~0.5 s late."""
    m = HotkeyMachine(RCTRL, "hold")
    assert play(m, [(RCTRL, True, 0.0)]) == []
    assert m.tick(HOLD_DELAY_S, hotkey_down=True) == [START]


# ------------------------------------------------------------------- Win32 glue


class FakeHookApi:
    """Stands in for user32: a message queue per hook thread, a fake hook chain."""

    def __init__(self, fail_hook: bool = False) -> None:
        self.fail_hook = fail_hook
        self.messages: queue.Queue[int] = queue.Queue()
        self.hooked = 0
        self.unhooked: list[int] = []
        self.proc = None
        self.next_calls: list[tuple[int, int, int]] = []
        self.events: dict[int, tuple[int, int, int]] = {}
        self.queue_made = False

    def thread_id(self) -> int:
        return 4242

    def ensure_queue(self) -> None:
        self.queue_made = True

    def set_hook(self, proc) -> int:
        if self.fail_hook:
            raise OSError("SetWindowsHookExW failed (error 5)")
        self.proc = proc
        self.hooked = 77
        return 77

    def unhook(self, handle: int) -> bool:
        self.unhooked.append(handle)
        return True

    def call_next(self, code, wparam, lparam) -> int:
        self.next_calls.append((code, wparam, lparam))
        return 1234  # whatever the next hook says must be returned unchanged

    def get_message(self) -> int:
        return self.messages.get()

    def post_quit(self, thread_id: int) -> bool:
        assert thread_id == 4242
        self.messages.put(0)
        return True

    def read(self, lparam: int):
        return self.events[lparam]

    # test helper: what Windows would do on a key event
    def press(self, lparam: int, vk: int, *, up: bool = False, injected: bool = False) -> int:
        flags = (LLKHF_UP if up else 0) | (LLKHF_INJECTED if injected else 0)
        self.events[lparam] = (vk, 0x1D, flags)
        return self.proc(0, 0x101 if up else 0x100, lparam)


def test_keyboard_hook_observes_and_always_passes_keys_on():
    api = FakeHookApi()
    seen = []
    hook = KeyboardHook(lambda vk, down, scan, flags: seen.append((vk, down)), api=api)
    hook.start()
    try:
        assert hook.installed and api.queue_made
        assert api.press(1, RCTRL) == 1234
        assert api.press(2, RCTRL, up=True) == 1234
        assert api.press(3, 0x56, injected=True) == 1234  # our own Ctrl+V: not reported
        assert seen == [(RCTRL, True), (RCTRL, False)]
        assert [c[2] for c in api.next_calls] == [1, 2, 3]  # every event went down the chain
        assert api.proc(-1, 0, 9) == 1234  # nCode < 0: straight to CallNextHookEx
        assert seen == [(RCTRL, True), (RCTRL, False)]
    finally:
        hook.stop()
    assert api.unhooked == [77]
    assert not hook.installed
    hook.stop()  # twice is fine
    assert api.unhooked == [77]


def test_keyboard_hook_never_blocks_a_key_when_the_callback_fails():
    api = FakeHookApi()

    def broken(*a):
        raise RuntimeError("bug")

    hook = KeyboardHook(broken, api=api)
    hook.start()
    try:
        assert api.press(1, RCTRL) == 1234
    finally:
        hook.stop()


def test_keyboard_hook_install_failure_raises_and_cleans_up():
    api = FakeHookApi(fail_hook=True)
    hook = KeyboardHook(lambda *a: None, api=api)
    with pytest.raises(OSError, match="couldn't listen for the hotkey"):
        hook.start()
    assert not hook.installed
    assert api.unhooked == []


def test_keyboard_hook_unhooks_when_the_loop_errors():
    api = FakeHookApi()
    hook = KeyboardHook(lambda *a: None, api=api)
    hook.start()
    api.messages.put(-1)  # GetMessage error
    deadline = time.monotonic() + 2
    while hook.installed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert api.unhooked == [77]
    hook.stop()


# ------------------------------------------------------------------- listener


class FakeHook:
    def __init__(self, on_key, fail: bool = False) -> None:
        self.on_key = on_key
        self.fail = fail
        self.started = self.stopped = 0

    def start(self) -> None:
        if self.fail:
            raise OSError("no hook")
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


def make_listener(mode="hold", key_state=lambda vk: None):
    events: queue.Queue = queue.Queue()
    hooks = []

    def factory(on_key):
        hooks.append(FakeHook(on_key))
        return hooks[-1]

    listener = HotkeyListener(
        "right_ctrl", mode, events.put, hook_factory=factory, key_state=key_state
    )
    return listener, hooks, events


def test_listener_turns_key_events_into_actions():
    listener, hooks, events = make_listener()
    listener.start()
    try:
        hook = hooks[0]
        assert hook.started == 1
        hook.on_key(RCTRL, True, 0x1D, 0)
        assert events.get(timeout=2) == START  # after the hold delay, by the worker's timer
        hook.on_key(RCTRL, False, 0x1D, 0)
        assert events.get(timeout=2) == STOP
    finally:
        listener.stop()
    assert hooks[0].stopped == 1


def test_listener_ignores_the_altgr_fake_ctrl():
    listener, hooks, events = make_listener()
    listener.machine = HotkeyMachine(HOTKEY_VKS["right_alt"], "hold")
    listener.start()
    try:
        hooks[0].on_key(hotkey.VK_LCONTROL, True, hotkey.ALTGR_FAKE_CTRL_SCAN, 0)
        hooks[0].on_key(HOTKEY_VKS["right_alt"], True, 0x38, 0)
        assert events.get(timeout=2).action == Action.START
    finally:
        listener.stop()


def test_listener_polls_the_physical_key_while_busy():
    down = {"value": True}
    listener, hooks, events = make_listener(key_state=lambda vk: down["value"])
    listener.start()
    try:
        hooks[0].on_key(RCTRL, True, 0x1D, 0)
        assert events.get(timeout=2) == START
        down["value"] = False  # the release went to the secure desktop
        assert events.get(timeout=2) == STOP
    finally:
        listener.stop()


def test_listener_force_stop():
    listener, hooks, events = make_listener()
    listener.start()
    try:
        hooks[0].on_key(RCTRL, True, 0x1D, 0)
        assert events.get(timeout=2) == START
        listener.force_stop()
        hooks[0].on_key(RCTRL, False, 0x1D, 0)
        with pytest.raises(queue.Empty):
            events.get(timeout=0.3)
    finally:
        listener.stop()


def test_listener_start_failure_stops_the_worker():
    def factory(on_key):
        return FakeHook(on_key, fail=True)

    listener = HotkeyListener("f9", "toggle", lambda e: None, hook_factory=factory)
    before = threading.active_count()
    with pytest.raises(OSError):
        listener.start()
    time.sleep(0.05)
    assert threading.active_count() <= before


def test_listener_rejects_unknown_hotkeys():
    with pytest.raises(ValueError):
        HotkeyListener("left_pinky", "hold", lambda e: None, hook_factory=FakeHook)


def test_handler_errors_do_not_kill_the_listener():
    calls = []

    def handler(event):
        calls.append(event)
        raise RuntimeError("bug in the handler")

    hooks = []
    listener = HotkeyListener(
        "right_ctrl",
        "toggle",
        handler,
        hook_factory=lambda k: hooks.append(FakeHook(k)) or hooks[-1],
    )
    listener.start()
    try:
        for _ in range(2):
            hooks[0].on_key(RCTRL, True, 0x1D, 0)
            hooks[0].on_key(RCTRL, False, 0x1D, 0)
        deadline = time.monotonic() + 2
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert actions(calls) == [Action.START, Action.STOP]
    finally:
        listener.stop()


def run_backlog(mode: str, keys: list[tuple[int, bool, float]]) -> list[HotkeyEvent]:
    """Events that waited in the queue (the worker was busy) are judged by their own
    timestamps, never by the late clock."""
    seen: list[HotkeyEvent] = []
    listener = HotkeyListener(
        "right_ctrl",
        mode,
        seen.append,
        hook_factory=FakeHook,
        key_state=lambda vk: False,
        clock=lambda: 10.0,  # long after the keys
    )
    for vk, down, at in keys:
        listener._queue.put(("key", (vk, down, at)))
    listener._queue.put(("quit", None))
    listener._run()
    return seen


def test_a_busy_worker_never_reorders_a_tap_or_a_hold():
    tap = [(RCTRL, True, 0.0), (RCTRL, False, 0.1)]
    assert run_backlog("toggle", tap) == [HotkeyEvent(Action.START, "press")]
    assert run_backlog("hold", tap) == []  # a tap in hold mode: nothing
    hold = [(RCTRL, True, 0.0), (RCTRL, False, 0.5)]
    assert run_backlog("hold", hold) == [START, STOP]
    assert run_backlog("toggle", hold) == []  # too long for a tap
    shortcut = [(RCTRL, True, 0.0), (KEY_C, True, 0.3), (KEY_C, False, 0.4)]
    assert run_backlog("hold", [*shortcut, (RCTRL, False, 0.5)]) == [
        START,
        HotkeyEvent(Action.CANCEL, "shortcut"),
    ]


def test_listener_with_a_lagging_key_state_waits_for_the_key_up():
    """GetAsyncKeyState lags the hook: it reads "up" just after a key-down. The pill used to
    pop up at the key-down of Right Ctrl + C because of that."""
    listener, hooks, events = make_listener("toggle", key_state=lambda vk: False)
    listener.start()
    try:
        hook = hooks[0]
        hook.on_key(RCTRL, True, 0x1D, 0)
        hook.on_key(KEY_C, True, 0x2E, 0)
        time.sleep(0.1)
        hook.on_key(KEY_C, False, 0x2E, 0)
        hook.on_key(RCTRL, False, 0x1D, 0)
        with pytest.raises(queue.Empty):
            events.get(timeout=0.4)  # Right Ctrl + C: nothing at all
        hook.on_key(RCTRL, True, 0x1D, 0)
        with pytest.raises(queue.Empty):
            events.get(timeout=0.05)  # nothing at the key-down of a tap either
        hook.on_key(RCTRL, False, 0x1D, 0)
        assert events.get(timeout=1) == HotkeyEvent(Action.START, "press")
    finally:
        listener.stop()


def test_listener_hold_delay_is_precise():
    """The hold timer isn't late by a 15.6 ms Windows timer tick."""
    stamps: list[float] = []
    listener = HotkeyListener(
        "right_ctrl",
        "hold",
        lambda e: stamps.append(time.perf_counter()),
        hook_factory=FakeHook,
        key_state=lambda vk: True,
    )
    listener.start()
    try:
        late = []
        for _ in range(3):
            stamps.clear()
            t0 = time.perf_counter()
            listener._on_key(RCTRL, True, 0x1D, 0)
            deadline = time.monotonic() + 2
            while not stamps and time.monotonic() < deadline:
                time.sleep(0.001)
            late.append(stamps[0] - t0 - HOLD_DELAY_S)
            listener._on_key(RCTRL, False, 0x1D, 0)
            time.sleep(0.05)
        assert min(late) < 0.008, late
    finally:
        listener.stop()
