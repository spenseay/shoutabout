from __future__ import annotations

import ctypes
import sys
import threading

import pytest

from fakes import FakeNotifier
from shoutabout.config import PasteConfig
from shoutabout.inject import DISABLED_REASON, NullInjector, create_injector
from shoutabout.inject import clipboard as cb
from shoutabout.inject import win32 as w
from shoutabout.inject.base import ADMIN_WINDOW_WARNING
from shoutabout.inject.windows import (
    ALLOWED_VKS,
    CLIPBOARD_WARNING,
    LOCKED_WARNING,
    MODIFIER_WARNING,
    NO_WINDOW_WARNING,
    SEND_FAILED_WARNING,
    KeyEvent,
    PasteChord,
    WindowsPasteInjector,
    build_inputs,
    chord_events,
    normalize_process_names,
)

MEDIUM = w.SECURITY_MANDATORY_MEDIUM_RID
HIGH = w.SECURITY_MANDATORY_HIGH_RID
VK_RETURN = 0x0D  # Enter: must never be sent (deliberately not defined in production code)

CTRL_V_EVENTS = [
    KeyEvent(w.VK_CONTROL, up=False),
    KeyEvent(w.VK_V, up=False),
    KeyEvent(w.VK_V, up=True),
    KeyEvent(w.VK_CONTROL, up=True),
]
SHIFT_INSERT_EVENTS = [
    KeyEvent(w.VK_SHIFT, up=False),
    KeyEvent(w.VK_INSERT, up=False, extended=True),
    KeyEvent(w.VK_INSERT, up=True, extended=True),
    KeyEvent(w.VK_SHIFT, up=True),
]
ALLOWED_SEQUENCES = (CTRL_V_EVENTS, SHIFT_INSERT_EVENTS)


class FakeApi:
    """Stands in for windows.Win32Api; records everything."""

    def __init__(self) -> None:
        self.fg: int | None = 0x1234
        self.fg_sequence: list[int | None] | None = None  # successive foreground answers
        self.desktop: str | None = "Default"
        self.pids = {0x1234: 42}
        self.rids: dict[int | None, int | None] = {None: MEDIUM, 42: MEDIUM}
        self.exe: dict[int, str | None] = {42: "notepad.exe"}
        self.modifier_answers: list[tuple[int, ...]] = []
        self.send_result: int | None = None
        self.sent: list[list[KeyEvent]] = []
        self.clipboard_text: str | None = "user's original"
        self.seq = 100
        self.set_calls: list[tuple[str, bool]] = []
        self.restores: list[tuple[cb.ClipboardSnapshot, bool]] = []
        self.snapshot_error: Exception | None = None
        self.set_error: Exception | None = None
        self.snapshot_value: cb.ClipboardSnapshot | None = None
        self.sleeps: list[float] = []
        self.now = 0.0
        self.log: list[str] = []
        self.restore_gate: threading.Event | None = None

    def foreground_window(self):
        if self.fg_sequence:
            self.fg = self.fg_sequence.pop(0)
        self.log.append("foreground")
        return self.fg

    def input_desktop_name(self):
        return self.desktop

    def window_pid(self, hwnd):
        return self.pids.get(hwnd, 0)

    def integrity_rid(self, pid):
        return self.rids.get(pid)

    def process_exe_name(self, pid):
        return self.exe.get(pid)

    def held_modifiers(self):
        if self.modifier_answers:
            return self.modifier_answers.pop(0)
        return ()

    def send_chord(self, chord):
        events = list(chord_events(chord))
        self.sent.append(events)
        self.log.append(f"send:{chord.value}")
        # also exercise the real INPUT builder with a fake scan-code mapper
        inputs = build_inputs(chord, lambda vk: vk + 0x100)
        assert [i.ki.wVk for i in inputs] == [e.vk for e in events]
        return len(events) if self.send_result is None else self.send_result

    def clipboard_snapshot(self):
        self.log.append("snapshot")
        if self.snapshot_error:
            raise self.snapshot_error
        if self.snapshot_value is not None:
            return self.snapshot_value
        if self.clipboard_text is None:
            return cb.ClipboardSnapshot()
        data = self.clipboard_text.encode("utf-16-le") + b"\0\0"
        return cb.ClipboardSnapshot(formats=((w.CF_UNICODETEXT, data),), original_format_count=1)

    def clipboard_set_text(self, text, *, exclude_from_history):
        self.log.append("set_text")
        if self.set_error:
            raise self.set_error
        self.set_calls.append((text, exclude_from_history))
        self.clipboard_text = text
        self.seq += 1
        return self.seq

    def clipboard_restore(self, snap, *, exclude_from_history, expected_sequence=None):
        if self.restore_gate is not None:
            self.restore_gate.wait(5)
        if expected_sequence is not None and expected_sequence != self.seq:
            self.log.append("restore-skipped")
            return
        self.log.append("restore")
        self.restores.append((snap, exclude_from_history))
        self.seq += 1
        data = snap.get(w.CF_UNICODETEXT)
        self.clipboard_text = None if data is None else cb._decode_text(data)

    def clipboard_sequence(self):
        return self.seq

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


def make(api: FakeApi | None = None, **cfg) -> tuple[WindowsPasteInjector, FakeApi, FakeNotifier]:
    api = api or FakeApi()
    notifier = FakeNotifier()
    inj = WindowsPasteInjector(PasteConfig(**cfg), notifier, api=api)
    return inj, api, notifier


def assert_only_paste_chords(api: FakeApi) -> None:
    for events in api.sent:
        assert events in ALLOWED_SEQUENCES
        assert all(e.vk != VK_RETURN for e in events)


# ---------------------------------------------------------------- factory


def test_create_injector_disabled():
    inj = create_injector(PasteConfig(enabled=False), FakeNotifier())
    assert isinstance(inj, NullInjector)
    result = inj.paste("hi")
    assert result.pasted is False
    assert (
        result.warning
        == DISABLED_REASON
        == ("Pasting is disabled in config (paste.enabled = false).")
    )


def test_create_injector_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    inj = create_injector(PasteConfig(), FakeNotifier())
    assert isinstance(inj, NullInjector)
    assert inj.paste("x").pasted is False


def test_create_injector_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    inj = create_injector(PasteConfig(), FakeNotifier())
    assert isinstance(inj, WindowsPasteInjector)


# ---------------------------------------------------------------- key chords


def test_chord_events_are_exactly_the_paste_chords():
    assert list(chord_events(PasteChord.CTRL_V)) == CTRL_V_EVENTS
    assert list(chord_events(PasteChord.SHIFT_INSERT)) == SHIFT_INSERT_EVENTS
    assert {PasteChord.CTRL_V, PasteChord.SHIFT_INSERT} == set(PasteChord)
    assert frozenset({w.VK_CONTROL, w.VK_V, w.VK_SHIFT, w.VK_INSERT}) == ALLOWED_VKS
    assert VK_RETURN not in ALLOWED_VKS


@pytest.mark.parametrize(
    ("chord", "expected"),
    [
        (
            PasteChord.CTRL_V,
            [
                (w.VK_CONTROL, 0),
                (w.VK_V, 0),
                (w.VK_V, w.KEYEVENTF_KEYUP),
                (w.VK_CONTROL, w.KEYEVENTF_KEYUP),
            ],
        ),
        (
            PasteChord.SHIFT_INSERT,
            [
                (w.VK_SHIFT, 0),
                (w.VK_INSERT, w.KEYEVENTF_EXTENDEDKEY),
                (w.VK_INSERT, w.KEYEVENTF_EXTENDEDKEY | w.KEYEVENTF_KEYUP),
                (w.VK_SHIFT, w.KEYEVENTF_KEYUP),
            ],
        ),
    ],
)
def test_build_inputs(chord, expected):
    inputs = build_inputs(chord, lambda vk: 0x1D if vk == w.VK_CONTROL else 0x2F)
    assert len(inputs) == 4
    got = [(i.ki.wVk, i.ki.dwFlags) for i in inputs]
    assert got == expected
    for i in inputs:
        assert i.type == w.INPUT_KEYBOARD
        assert i.ki.wScan in (0x1D, 0x2F)
        assert not i.ki.dwFlags & w.KEYEVENTF_SCANCODE
        assert i.ki.time == 0 and i.ki.dwExtraInfo == 0


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="64-bit layout")
def test_input_struct_layout_x64():
    assert ctypes.sizeof(w.INPUT) == 40
    assert w.INPUT.u.offset == 8
    assert ctypes.sizeof(w.KEYBDINPUT) == 24


def test_normalize_process_names():
    names = normalize_process_names(["MinTTY.EXE", " putty ", r"C:\tools\Kitty.exe", ""])
    assert names == {"mintty.exe", "putty.exe", "kitty.exe"}


# ---------------------------------------------------------------- paste flow


def test_paste_ctrl_v_and_restore():
    inj, api, notifier = make()
    result = inj.paste("hello world ")
    assert result.pasted is True and result.warning is None
    assert api.set_calls == [("hello world ", True)]  # exact text, nothing added
    assert api.sent == [CTRL_V_EVENTS]
    assert inj.wait_idle(5)
    assert len(api.restores) == 1
    snap, exclude = api.restores[0]
    assert exclude is True
    assert api.clipboard_text == "user's original"
    assert 0.5 in api.sleeps  # restore_delay_ms default 500
    assert notifier.messages == []
    assert api.log.index("snapshot") < api.log.index("set_text") < api.log.index("send:ctrl+v")
    assert_only_paste_chords(api)


def test_exclude_from_history_follows_config():
    inj, api, _ = make(exclude_from_history=False)
    assert inj.paste("x").pasted
    assert api.set_calls == [("x", False)]
    inj.wait_idle(5)
    assert api.restores[0][1] is True  # restored item always excluded


def test_restore_skipped_when_clipboard_changed():
    inj, api, _ = make()
    gate = threading.Event()
    orig_sleep = api.sleep

    def slow_sleep(seconds):
        orig_sleep(seconds)
        if seconds == 0.5:
            api.seq += 1  # the user copied something during the restore delay
            gate.set()

    api.sleep = slow_sleep
    assert inj.paste("x").pasted
    assert inj.wait_idle(5)
    assert gate.is_set()
    assert api.restores == []
    assert api.clipboard_text == "x"


def test_restore_disabled():
    inj, api, _ = make(restore_clipboard=False)
    assert inj.paste("x").pasted
    assert "snapshot" not in api.log
    assert inj.wait_idle(1)
    assert api.restores == []


def test_restore_delay_from_config():
    inj, api, _ = make(restore_delay_ms=1200)
    inj.paste("x")
    inj.wait_idle(5)
    assert 1.2 in api.sleeps


def test_empty_original_clipboard_is_restored_empty():
    api = FakeApi()
    api.clipboard_text = None
    inj, api, _ = make(api)
    assert inj.paste("x").pasted
    inj.wait_idle(5)
    assert len(api.restores) == 1
    assert api.restores[0][0].is_empty
    assert api.clipboard_text is None


def test_unsaveable_clipboard_is_not_restored():
    api = FakeApi()
    # e.g. only an enhanced metafile was on the clipboard: nothing we could copy
    api.snapshot_value = cb.ClipboardSnapshot(formats=(), original_format_count=2, complete=True)
    inj, api, _ = make(api)
    assert inj.paste("x").pasted
    inj.wait_idle(5)
    assert api.restores == []


def test_snapshot_failure_still_pastes_without_restore():
    api = FakeApi()
    api.snapshot_error = cb.ClipboardError("busy")
    inj, api, _ = make(api)
    assert inj.paste("x").pasted
    inj.wait_idle(1)
    assert api.restores == []
    assert api.sent == [CTRL_V_EVENTS]


def test_set_text_failure_does_not_send_keys():
    api = FakeApi()
    api.set_error = cb.ClipboardError("busy")
    inj, api, _ = make(api)
    result = inj.paste("x")
    assert result.pasted is False and result.warning == CLIPBOARD_WARNING
    assert api.sent == []


@pytest.mark.parametrize("exe", ["mintty.exe", "MINTTY.EXE", "putty.exe", "kitty.exe"])
def test_shift_insert_for_configured_processes(exe):
    api = FakeApi()
    api.exe[42] = exe.lower()
    inj, api, _ = make(api)
    assert inj.paste("x").pasted
    assert api.sent == [SHIFT_INSERT_EVENTS]
    assert_only_paste_chords(api)


def test_shift_insert_list_is_configurable():
    api = FakeApi()
    api.exe[42] = "mintty.exe"
    inj, api, _ = make(api, shift_insert_processes=["myterm"])
    inj.paste("x")
    api.exe[42] = "myterm.exe"
    inj.paste("y")
    assert api.sent == [CTRL_V_EVENTS, SHIFT_INSERT_EVENTS]


def test_unknown_exe_uses_ctrl_v():
    api = FakeApi()
    api.exe[42] = None
    inj, api, _ = make(api)
    assert inj.paste("x").pasted
    assert api.sent == [CTRL_V_EVENTS]


def test_no_foreground_window():
    api = FakeApi()
    api.fg = None
    inj, api, notifier = make(api)
    result = inj.paste("x")
    assert result.pasted is False and result.warning == NO_WINDOW_WARNING
    assert api.set_calls == [("x", True)]
    assert api.sent == [] and api.restores == [] and "snapshot" not in api.log
    assert notifier.messages == []


@pytest.mark.parametrize("desktop", [None, "Winlogon", "Screen-saver"])
def test_locked_or_secure_desktop(desktop):
    api = FakeApi()
    api.desktop = desktop
    inj, api, _ = make(api)
    result = inj.paste("x")
    assert result.pasted is False and result.warning == LOCKED_WARNING
    assert api.set_calls == [("x", True)]
    assert api.sent == []


@pytest.mark.parametrize("target_rid", [HIGH, w.SECURITY_MANDATORY_SYSTEM_RID, None])
def test_elevated_or_unqueryable_window(target_rid):
    api = FakeApi()
    api.rids[42] = target_rid
    inj, api, notifier = make(api)
    result = inj.paste("secret text")
    assert result.pasted is False and result.warning == ADMIN_WINDOW_WARNING
    assert api.set_calls == [("secret text", True)]
    assert api.sent == [] and api.restores == [] and "snapshot" not in api.log
    assert notifier.messages == [("ShoutAbout", ADMIN_WINDOW_WARNING)]


def test_same_or_lower_integrity_pastes():
    api = FakeApi()
    api.rids[42] = w.SECURITY_MANDATORY_LOW_RID
    inj, api, _ = make(api)
    assert inj.paste("x").pasted


def test_elevated_hub_can_paste_into_elevated_window():
    api = FakeApi()
    api.rids = {None: HIGH, 42: HIGH}
    inj, api, _ = make(api)
    assert inj.paste("x").pasted


def test_window_pid_failure_is_treated_as_unpasteable():
    api = FakeApi()
    api.pids = {}
    inj, api, notifier = make(api)
    assert inj.paste("x").warning == ADMIN_WINDOW_WARNING
    assert api.sent == []


def test_focus_moves_to_admin_window_before_keystroke():
    api = FakeApi()
    api.pids[0x9999] = 7
    api.rids[7] = HIGH
    api.fg_sequence = [0x1234, 0x9999]
    inj, api, notifier = make(api)
    result = inj.paste("x")
    assert result.pasted is False and result.warning == ADMIN_WINDOW_WARNING
    assert api.sent == []
    assert api.set_calls == [("x", True)]  # set once, left on the clipboard
    assert api.restores == []
    assert notifier.messages == [("ShoutAbout", ADMIN_WINDOW_WARNING)]


def test_focus_moves_to_another_normal_window_uses_its_chord():
    api = FakeApi()
    api.pids[0x9999] = 7
    api.rids[7] = MEDIUM
    api.exe[7] = "putty.exe"
    api.fg_sequence = [0x1234, 0x9999]
    inj, api, _ = make(api)
    assert inj.paste("x").pasted
    assert api.sent == [SHIFT_INSERT_EVENTS]


def test_modifiers_released_within_wait():
    api = FakeApi()
    api.modifier_answers = [(w.VK_SHIFT,), (w.VK_SHIFT,), ()]
    inj, api, _ = make(api)
    assert inj.paste("x").pasted
    assert api.sent == [CTRL_V_EVENTS]


def test_modifier_held_too_long():
    api = FakeApi()
    api.modifier_answers = [(w.VK_CONTROL,)] * 200
    inj, api, _ = make(api)
    result = inj.paste("x")
    assert result.pasted is False and result.warning == MODIFIER_WARNING
    assert api.sent == []
    assert api.set_calls == [("x", True)]
    assert api.now >= 1.0


def test_modifier_pressed_just_before_keystroke():
    api = FakeApi()
    api.modifier_answers = [(), (w.VK_MENU,)]
    inj, api, _ = make(api)
    result = inj.paste("x")
    assert result.pasted is False and result.warning == MODIFIER_WARNING
    assert api.sent == []


@pytest.mark.parametrize("sent", [0, 2])
def test_sendinput_failure(sent):
    api = FakeApi()
    api.send_result = sent
    inj, api, _ = make(api)
    result = inj.paste("x")
    assert result.pasted is False and result.warning == SEND_FAILED_WARNING
    inj.wait_idle(1)
    assert api.restores == []
    assert api.clipboard_text == "x"


def test_paste_never_raises():
    api = FakeApi()

    def boom():
        raise RuntimeError("kaboom")

    api.foreground_window = boom
    inj, api, _ = make(api)
    result = inj.paste("x")
    assert result.pasted is False
    assert "kaboom" in (result.warning or "")


def test_notifier_failure_is_swallowed():
    class BadNotifier:
        def notify(self, title, message):
            raise RuntimeError("no tray")

    api = FakeApi()
    api.rids[42] = HIGH
    inj = WindowsPasteInjector(PasteConfig(), BadNotifier(), api=api)
    assert inj.paste("x").warning == ADMIN_WINDOW_WARNING


def test_next_paste_waits_for_pending_restore():
    api = FakeApi()
    api.restore_gate = threading.Event()
    inj, api, _ = make(api)
    assert inj.paste("first").pasted
    done = threading.Event()

    def second():
        inj.paste("second")
        done.set()

    t = threading.Thread(target=second)
    t.start()
    assert not done.wait(0.2)  # blocked behind the pending restore
    api.restore_gate.set()
    t.join(5)
    assert done.is_set()
    assert api.log.count("restore") >= 1
    first_restore = api.log.index("restore")
    second_set = [i for i, e in enumerate(api.log) if e == "set_text"][1]
    assert first_restore < second_set
    inj.wait_idle(5)
    assert api.clipboard_text == "user's original"
    assert_only_paste_chords(api)


def test_many_pastes_only_ever_send_paste_chords():
    api = FakeApi()
    inj, api, _ = make(api)
    for i, exe in enumerate(["notepad.exe", "mintty.exe", "code.exe", "putty.exe"] * 3):
        api.exe[42] = exe
        inj.paste(f"text {i}\n")
    inj.wait_idle(5)
    assert len(api.sent) == 12
    assert_only_paste_chords(api)


# ---------------------------------------------------------------- clipboard selection


def _names(mapping):
    return lambda fmt: mapping.get(fmt)


def test_select_formats_keeps_text_locale_and_registered():
    html, rtf = 0xC0A1, 0xC0A2
    available = [w.CF_UNICODETEXT, html, rtf, w.CF_LOCALE, w.CF_TEXT, w.CF_OEMTEXT]
    chosen = cb.select_formats(available, _names({html: "HTML Format", rtf: "Rich Text Format"}))
    assert chosen == [w.CF_UNICODETEXT, html, rtf, w.CF_LOCALE]


def test_select_formats_skips_gdi_private_and_ole():
    ole, priv = 0xC010, 0xC011
    available = [
        w.CF_BITMAP,
        w.CF_DIB,
        w.CF_DIBV5,
        w.CF_PALETTE,
        w.CF_METAFILEPICT,
        w.CF_ENHMETAFILE,
        w.CF_OWNERDISPLAY,
        w.CF_DSPBITMAP,
        w.CF_DSPMETAFILEPICT,
        w.CF_DSPENHMETAFILE,
        0x200,
        0x2FF,
        0x300,
        ole,
        priv,
        w.CF_HDROP,
        w.CF_DSPTEXT,
    ]
    names = _names({ole: "DataObject", priv: "Ole Private Data"})
    assert cb.select_formats(available, names) == [w.CF_DIB, w.CF_HDROP, w.CF_DSPTEXT]


def test_select_formats_keeps_first_dib_variant():
    assert cb.select_formats([w.CF_DIBV5, w.CF_BITMAP, w.CF_DIB], _names({})) == [w.CF_DIBV5]


def test_select_formats_text_without_unicode():
    assert cb.select_formats([w.CF_TEXT, w.CF_LOCALE], _names({})) == [w.CF_TEXT, w.CF_LOCALE]


def test_snapshot_repr_hides_contents():
    snap = cb.ClipboardSnapshot(formats=((13, "top secret".encode("utf-16-le")),))
    assert "secret" not in repr(snap)
    assert snap.format_ids() == (13,)
    assert snap.total_bytes == 20


def test_decode_text():
    assert cb._decode_text("abc\0junk".encode("utf-16-le")) == "abc"
    assert cb._decode_text("✓ ok".encode("utf-16-le") + b"\0\0\0") == "✓ ok"


# ---------------------------------------------------------------- real Windows


@pytest.fixture
def saved_clipboard():
    """Snapshot the user's clipboard and put it back afterwards."""
    snap = cb.snapshot()
    try:
        yield snap
    finally:
        cb.restore(snap)


@pytest.mark.windows
def test_real_struct_sizes():
    assert ctypes.sizeof(w.INPUT) == 40
    assert ctypes.sizeof(w.NOTIFYICONDATAW) == 976


@pytest.mark.windows
def test_real_clipboard_roundtrip(saved_clipboard):
    seq = cb.set_text("ShoutAbout test ✓ 123 ", exclude_from_history=True)
    assert seq == cb.sequence_number()
    assert cb.get_text() == "ShoutAbout test ✓ 123 "
    names = {name for _, name in cb.list_formats() if name}
    assert set(cb.EXCLUSION_FORMAT_NAMES) <= names
    snap = cb.snapshot()
    ids = cb.exclusion_format_ids()
    assert snap.get(ids[cb.HISTORY_FORMAT_NAME]) == b"\0\0\0\0"
    assert snap.get(ids[cb.CLOUD_FORMAT_NAME]) == b"\0\0\0\0"

    cb.set_text("without markers", exclude_from_history=False)
    names = {name for _, name in cb.list_formats() if name}
    assert not set(cb.EXCLUSION_FORMAT_NAMES) & names

    cb.restore(snap)
    assert cb.get_text() == "ShoutAbout test ✓ 123 "
    assert cb.sequence_number() > seq


@pytest.mark.windows
def test_real_restore_adds_markers_and_empty(saved_clipboard):
    cb.set_text("marker", exclude_from_history=False)
    snap = cb.snapshot()
    cb.set_text("other")
    cb.restore(snap, exclude_from_history=True)
    assert cb.get_text() == "marker"
    names = {name for _, name in cb.list_formats() if name}
    assert set(cb.EXCLUSION_FORMAT_NAMES) <= names
    cb.restore(cb.ClipboardSnapshot())
    assert cb.list_formats() == []
    assert cb.get_text() is None


@pytest.mark.windows
def test_real_restore_skipped_if_clipboard_changed(saved_clipboard):
    cb.set_text("original", exclude_from_history=True)
    snap = cb.snapshot()
    seq = cb.set_text("transcript", exclude_from_history=True)
    cb.set_text("user copied this meanwhile", exclude_from_history=True)
    assert cb.restore(snap, expected_sequence=seq) is False
    assert cb.get_text() == "user copied this meanwhile"
    seq = cb.sequence_number()
    assert cb.restore(snap, expected_sequence=seq) is True
    assert cb.get_text() == "original"


@pytest.mark.windows
def test_real_clipboard_from_worker_thread(saved_clipboard):
    out = {}

    def work():
        cb.set_text("from a thread")
        out["text"] = cb.get_text()

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert out["text"] == "from a thread"


@pytest.mark.windows
def test_real_win32_queries():
    from shoutabout.inject.windows import Win32Api

    api = Win32Api()
    own = api.integrity_rid(None)
    assert own is not None and own >= w.SECURITY_MANDATORY_MEDIUM_RID
    pid = w.kernel32.GetCurrentProcessId()
    assert api.integrity_rid(pid) == own
    assert api.process_exe_name(pid) in ("python.exe", "pythonw.exe")
    assert api.integrity_rid(4) is None or api.integrity_rid(4) >= own  # System process
    assert api.input_desktop_name() in ("Default", None)
    assert isinstance(api.held_modifiers(), tuple)


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_clipboard_raises_clipboard_error_off_windows():
    with pytest.raises(cb.ClipboardError):
        cb.set_text("x")
    with pytest.raises(cb.ClipboardError):
        cb.sequence_number()
