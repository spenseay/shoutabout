"""macOS hotkey: key codes, modifier logic, the event tap with a fake Quartz, and the
listener end to end (the shared HotkeyMachine). Nothing here needs a Mac or PyObjC."""

from __future__ import annotations

import queue
import threading

import pytest

from shoutabout import hotkey
from shoutabout.hotkey import Action
from shoutabout.mac import keys
from shoutabout.mac.eventtap import (
    MacEventTap,
    MacHotkeyListener,
    TapPermissionError,
    choose_listen_only,
)

RCMD = keys.KC_RIGHT_COMMAND
LCMD = keys.KC_COMMAND
CMD_DOWN = keys.FLAG_COMMAND | keys.DEV_RCMD
LCMD_DOWN = keys.FLAG_COMMAND | keys.DEV_LCMD
KC_C = 0x08


# ------------------------------------------------------------------- keys


def test_escape_matches_the_shared_machine():
    assert keys.VK_ESCAPE == hotkey.VK_ESCAPE
    assert keys.vk_for_keycode(keys.KC_ESCAPE) == hotkey.VK_ESCAPE
    assert keys.vk_for_keycode(KC_C) == keys.KEY_BASE + KC_C


def test_mac_hotkeys_and_labels():
    assert list(keys.MAC_HOTKEY_CHOICES)[0] == keys.DEFAULT_MAC_HOTKEY == "right_cmd"
    assert set(keys.MAC_HOTKEY_CHOICES) == set(keys.HOTKEY_KEYCODES)
    assert keys.hotkey_label("right_cmd") == "Right ⌘ Command"
    assert keys.hotkey_vk("right_alt") == keys.KEY_BASE + keys.KC_RIGHT_OPTION
    with pytest.raises(ValueError):
        keys.hotkey_vk("f8")
    # Mac virtual keys never collide with the Windows codes the machine knows.
    assert all(keys.hotkey_vk(k) >= keys.KEY_BASE for k in keys.MAC_HOTKEY_CHOICES)


def test_event_mask_has_keys_flags_and_clicks():
    mask = keys.event_mask()
    for kind in (keys.EV_KEY_DOWN, keys.EV_KEY_UP, keys.EV_FLAGS_CHANGED, keys.EV_LEFT_MOUSE_DOWN):
        assert mask & (1 << kind)
    assert not mask & (1 << 5)  # mouse moves: never


@pytest.mark.parametrize(
    ("keycode", "flags", "was_down", "expected"),
    [
        (RCMD, CMD_DOWN, False, True),  # right cmd went down
        (RCMD, 0, True, False),  # all cmd up
        (RCMD, LCMD_DOWN, True, False),  # right released while left is still held
        (LCMD, LCMD_DOWN | keys.DEV_RCMD, False, True),
        (RCMD, keys.FLAG_COMMAND, False, True),  # no device bits: toggles from our record
        (RCMD, keys.FLAG_COMMAND, True, False),
        (keys.KC_FUNCTION, keys.FLAG_SECONDARY_FN, False, True),
        (keys.KC_FUNCTION, 0, True, False),
    ],
)
def test_modifier_down(keycode, flags, was_down, expected):
    assert keys.modifier_down(keycode, flags, was_down) is expected


def test_translator_turns_events_into_virtual_keys():
    t = keys.KeyTranslator()
    rcmd = keys.hotkey_vk("right_cmd")
    assert t.translate(keys.EV_FLAGS_CHANGED, RCMD, CMD_DOWN) == [(rcmd, True)]
    assert t.translate(keys.EV_FLAGS_CHANGED, RCMD, 0) == [(rcmd, False)]
    assert t.translate(keys.EV_KEY_DOWN, KC_C) == [(keys.KEY_BASE + KC_C, True)]
    assert t.translate(keys.EV_KEY_UP, KC_C) == [(keys.KEY_BASE + KC_C, False)]
    caps = keys.vk_for_keycode(keys.KC_CAPS_LOCK)
    assert t.translate(keys.EV_FLAGS_CHANGED, keys.KC_CAPS_LOCK, 0) == [(caps, True), (caps, False)]
    click = t.translate(keys.EV_LEFT_MOUSE_DOWN, 0, 0, button=0)
    assert click == [(keys.MOUSE_BASE, True), (keys.MOUSE_BASE, False)]
    assert t.translate(keys.EV_FLAGS_CHANGED, 0x7F, 0) == []  # not a modifier we know
    assert t.translate(5) == []


def test_hint_text():
    class D:
        enabled, hotkey, mode = True, "right_cmd", "toggle"

    class C:
        dictation = D()

    assert keys.hint_text(C()) == "Press Right ⌘ Command to start talking, and again to stop."
    D.mode = "hold"
    assert keys.hint_text(C()) == "Hold Right ⌘ Command to talk."
    D.enabled = False
    assert keys.hint_text(C()) == "Talk to this Mac is off."


# --------------------------------------------------------------- the tap


@pytest.mark.parametrize(
    ("listen", "post", "order"),
    [
        (True, True, [True, False]),
        (True, False, [True]),
        (False, True, [False, True]),
        (None, True, [False, True]),
        (False, False, []),
        (None, None, [True]),
    ],
)
def test_choose_listen_only(listen, post, order):
    assert choose_listen_only(listen, post) == order


class FakeEvent:
    def __init__(self, keycode=0, flags=0, button=0, mark=0):
        self.keycode, self.flags, self.button, self.mark = keycode, flags, button, mark


class FakeTapApi:
    def __init__(self, listen=True, post=True, refuse=()):
        self.listen, self.post, self.refuse = listen, post, set(refuse)
        self.created: list[bool] = []
        self.callback = None
        self.enabled = 0
        self.running = threading.Event()

    def listen_allowed(self):
        return self.listen

    def post_allowed(self):
        return self.post

    def create(self, listen_only, mask, callback):
        self.created.append(listen_only)
        if listen_only in self.refuse:
            return None
        self.callback = callback
        return object()

    def run(self, tap, stop, ready):
        ready()
        self.running.set()
        stop.wait(5)

    def enable(self, tap):
        self.enabled += 1

    def read(self, event):
        return event.keycode, event.flags, event.button, event.mark


def make_tap(api):
    seen = []
    tap = MacEventTap(lambda vk, down, scan, flags: seen.append((vk, down)), api=api)
    return tap, seen


def test_tap_reports_keys_and_passes_every_event_on():
    api = FakeTapApi()
    tap, seen = make_tap(api)
    tap.start()
    try:
        assert api.created == [True] and tap.listen_only is True
        ev = FakeEvent(RCMD, CMD_DOWN)
        assert api.callback(None, keys.EV_FLAGS_CHANGED, ev, None) is ev
        own = FakeEvent(keys.KC_ANSI_V, keys.FLAG_COMMAND, mark=keys.OWN_EVENT_MARK)
        assert api.callback(None, keys.EV_KEY_DOWN, own, None) is own  # ours: ignored
        assert seen == [(keys.hotkey_vk("right_cmd"), True)]
    finally:
        tap.stop()


def test_tap_turns_itself_back_on_when_macos_pauses_it():
    api = FakeTapApi()
    tap, _ = make_tap(api)
    tap.start()
    try:
        ev = FakeEvent()
        assert api.callback(None, keys.EV_TAP_DISABLED_BY_TIMEOUT, ev, None) is ev
        assert api.enabled == 1
    finally:
        tap.stop()


def test_tap_errors_never_change_the_event():
    api = FakeTapApi()
    tap, _ = make_tap(api)
    tap._on_key = lambda *a: 1 / 0  # noqa: ARG005
    tap.start()
    try:
        ev = FakeEvent(KC_C)
        assert api.callback(None, keys.EV_KEY_DOWN, ev, None) is ev
    finally:
        tap.stop()


def test_tap_uses_accessibility_when_input_monitoring_is_off():
    api = FakeTapApi(listen=False, post=True)
    tap, _ = make_tap(api)
    tap.start()
    try:
        assert api.created == [False] and tap.listen_only is False
    finally:
        tap.stop()


def test_tap_without_permissions_says_what_to_do():
    api = FakeTapApi(listen=False, post=False)
    tap, _ = make_tap(api)
    with pytest.raises(TapPermissionError, match="Accessibility") as error:
        tap.start()
    assert api.created == []  # didn't even try (no Input Monitoring prompt)
    assert not tap.installed
    assert error.value.told_elsewhere  # the app's own "Allow access" says so, not a retry


def test_tap_refused_by_macos():
    api = FakeTapApi(listen=None, post=None, refuse={True})
    tap, _ = make_tap(api)
    with pytest.raises(OSError) as error:
        tap.start()
    assert not getattr(error.value, "told_elsewhere", False)  # unexpected: notify about it


# ------------------------------------------------------------ the listener


def make_listener(mode="toggle", api=None):
    api = api or FakeTapApi()
    events: queue.Queue = queue.Queue()
    listener = MacHotkeyListener(
        "right_cmd", mode, events.put, tap_factory=lambda on_key: MacEventTap(on_key, api=api)
    )
    return listener, api, events


def press(api, keycode, flags_down, flags_up=0):
    api.callback(None, keys.EV_FLAGS_CHANGED, FakeEvent(keycode, flags_down), None)
    api.callback(None, keys.EV_FLAGS_CHANGED, FakeEvent(keycode, flags_up), None)


def test_listener_tap_starts_and_tap_stops():
    listener, api, events = make_listener()
    listener.start()
    try:
        press(api, RCMD, CMD_DOWN)
        assert events.get(timeout=2).action == Action.START
        press(api, RCMD, CMD_DOWN)
        assert events.get(timeout=2).action == Action.STOP
    finally:
        listener.stop()


def test_listener_shortcuts_never_start():
    listener, api, events = make_listener()
    listener.start()
    try:
        cb = api.callback
        cb(None, keys.EV_FLAGS_CHANGED, FakeEvent(RCMD, CMD_DOWN), None)
        cb(None, keys.EV_KEY_DOWN, FakeEvent(KC_C, CMD_DOWN), None)  # Right Cmd + C
        cb(None, keys.EV_KEY_UP, FakeEvent(KC_C, CMD_DOWN), None)
        cb(None, keys.EV_FLAGS_CHANGED, FakeEvent(RCMD, 0), None)
        cb(None, keys.EV_FLAGS_CHANGED, FakeEvent(RCMD, CMD_DOWN), None)
        cb(None, keys.EV_LEFT_MOUSE_DOWN, FakeEvent(0, CMD_DOWN), None)  # Cmd-click
        cb(None, keys.EV_FLAGS_CHANGED, FakeEvent(RCMD, 0), None)
        with pytest.raises(queue.Empty):
            events.get(timeout=0.3)
    finally:
        listener.stop()


def test_listener_left_cmd_and_escape():
    listener, api, events = make_listener()
    listener.start()
    try:
        press(api, LCMD, LCMD_DOWN)  # the other Command key: nothing
        with pytest.raises(queue.Empty):
            events.get(timeout=0.2)
        press(api, RCMD, CMD_DOWN)
        assert events.get(timeout=2).action == Action.START
        api.callback(None, keys.EV_KEY_DOWN, FakeEvent(keys.KC_ESCAPE), None)
        event = events.get(timeout=2)
        assert (event.action, event.reason) == (Action.CANCEL, "escape")
    finally:
        listener.stop()


def test_listener_hold_mode():
    listener, api, events = make_listener(mode="hold")
    listener.start()
    try:
        api.callback(None, keys.EV_FLAGS_CHANGED, FakeEvent(RCMD, CMD_DOWN), None)
        assert events.get(timeout=2).action == Action.START  # after the hold delay
        api.callback(None, keys.EV_FLAGS_CHANGED, FakeEvent(RCMD, 0), None)
        assert events.get(timeout=2).action == Action.STOP
    finally:
        listener.stop()


def test_listener_rejects_windows_only_keys():
    with pytest.raises(ValueError):
        MacHotkeyListener("f8", "toggle", lambda e: None, tap_factory=lambda k: None)
