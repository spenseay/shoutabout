""" "Talk to this PC" end to end with fakes: hotkey events in, pasted text and overlay out.

The real Pipeline runs (fake engine, cleaner and injector), so the paste rules, history,
pause and the shared busy lock are the ones the phone uses.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from fakes import FakeCleaner, FakeEngine, FakeInjector, FakeVocabStore, fake_decode
from shoutabout.config import Config, parse_config
from shoutabout.engine.base import EngineError
from shoutabout.hotkey import Action, HotkeyEvent
from shoutabout.inject.base import ADMIN_WINDOW_WARNING
from shoutabout.local_dictation import (
    DictationState,
    LocalDictation,
    hint_text,
    overlay_warning,
    ready_text,
)
from shoutabout.mic import MicError
from shoutabout.models import HubStatus, PasteResult
from shoutabout.overlay import NullOverlay
from shoutabout.pipeline import PAUSED_WARNING, Pipeline
from shoutabout.state import HubState, Progress

START = HotkeyEvent(Action.START, "hold")
STOP = HotkeyEvent(Action.STOP, "release")


class RecordingOverlay(NullOverlay):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.started = self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1
        super().stop()

    def _set(self, state: str, text: str = "") -> None:
        super()._set(state, text)
        self.calls.append(state if not text else f"{state}:{text}")


class FakeListener:
    def __init__(self, hotkey, mode, on_event, fail=False) -> None:
        self.hotkey, self.mode, self.on_event = hotkey, mode, on_event
        self.fail = fail
        self.started = self.stopped = self.forced = 0

    def start(self) -> None:
        if self.fail:
            raise OSError("SetWindowsHookExW failed (error 5)")
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def force_stop(self) -> None:
        self.forced += 1


class FakeRecorder:
    def __init__(
        self, *, max_seconds, on_level, on_limit, audio=None, start_error=None, stop_error=None
    ):
        self.max_seconds = max_seconds
        self.on_level, self.on_limit = on_level, on_limit
        self.audio = audio if audio is not None else np.full(16_000 * 2, 0.1, np.float32)
        self.start_error, self.stop_error = start_error, stop_error
        self.started = self.stopped = self.cancelled = 0

    def start(self) -> None:
        if self.start_error:
            raise self.start_error
        self.started += 1
        self.on_level(0.6)

    def stop(self) -> np.ndarray:
        self.stopped += 1
        if self.stop_error:
            raise self.stop_error
        return self.audio

    def cancel(self) -> None:
        self.cancelled += 1


class Harness:
    def __init__(
        self,
        cfg: Config | None = None,
        *,
        injector=None,
        engine=None,
        ready=True,
        failed=None,
        mic_present=None,
        **rec,
    ):
        self.cfg = cfg or Config()
        self.state = HubState()
        self.injector = injector or FakeInjector()
        self.pipeline = Pipeline(
            config=self.cfg,
            engine=engine or FakeEngine(text="hello from the pc"),
            vocab=FakeVocabStore(),
            cleaner=FakeCleaner(),
            injector=self.injector,
            state=self.state,
            decode=fake_decode,
        )
        self.ready = ready
        self.overlay = RecordingOverlay()
        self.listeners: list[FakeListener] = []
        self.recorders: list[FakeRecorder] = []
        self.notes: list[tuple[str, str]] = []
        self.rec_kw = rec
        self.listener_fail = False
        self.dictation = LocalDictation(
            self.cfg,
            state=self.state,
            pipeline=lambda: self.pipeline if self.ready else None,
            notify=lambda t, m: self.notes.append((t, m)),
            listener_factory=self._listener,
            recorder_factory=self._recorder,
            overlay_factory=lambda enabled: self.overlay,
            failed=failed,
            mic_present=mic_present,
        )

    def _listener(self, hotkey, mode, on_event):
        self.listeners.append(FakeListener(hotkey, mode, on_event, fail=self.listener_fail))
        return self.listeners[-1]

    def _recorder(self, **kw):
        self.recorders.append(FakeRecorder(**kw, **self.rec_kw))
        self.recorders[-1].overlay_at_start = list(self.overlay.calls)
        return self.recorders[-1]

    def send(self, event: HotkeyEvent) -> None:
        self.listeners[-1].on_event(event)

    def wait_state(self, state: DictationState, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while self.dictation.state != state and time.monotonic() < deadline:
            time.sleep(0.005)
        assert self.dictation.state == state

    def wait_overlay(self, prefix: str, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not (self.overlay.calls and self.overlay.calls[-1].startswith(prefix)):
            if time.monotonic() > deadline:
                raise AssertionError(f"overlay calls: {self.overlay.calls}")
            time.sleep(0.005)


@pytest.fixture
def h():
    harness = Harness()
    assert harness.dictation.start()
    yield harness
    harness.dictation.stop()


def test_tap_speak_tap_pastes(h):
    assert h.dictation.state == DictationState.READY
    assert h.listeners[0].hotkey == "right_ctrl" and h.listeners[0].mode == "toggle"
    assert h.overlay.started == 1
    h.send(START)
    assert h.dictation.state == DictationState.LISTENING
    assert h.overlay.calls == ["listening"] and h.overlay.level == 0.6
    assert h.recorders[0].max_seconds == 600  # stops by itself then; the pill shows no clock
    h.send(STOP)
    h.wait_overlay("done")
    assert h.overlay.calls == ["listening", "thinking", "done"]
    assert h.injector.pasted == ["hello from the pc"]
    h.wait_state(DictationState.READY)
    assert h.state.status == HubStatus.IDLE


def test_route_and_history_are_local(tmp_path):
    from shoutabout.history import HistoryLog

    h = Harness()
    h.pipeline.history = HistoryLog(tmp_path / "history.jsonl", tmp_path / "audio", False)
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("done")
    line = (tmp_path / "history.jsonl").read_text("utf-8")
    assert '"route": "local"' in line and '"target": "pc"' in line
    assert '"audio_file": null' in line
    h.dictation.stop()


def test_too_short_is_ignored(h):
    h.recorders.clear()
    h.rec_kw["audio"] = np.full(1000, 0.1, np.float32)
    h.send(START)
    h.send(STOP)
    assert h.overlay.calls[-1] == "cancelled"
    assert h.injector.pasted == []
    assert h.dictation.state == DictationState.READY


def test_escape_and_shortcut_cancel(h):
    h.send(START)
    h.send(HotkeyEvent(Action.CANCEL, "escape"))
    assert h.overlay.calls[-1] == "cancelled" and h.recorders[0].cancelled == 1
    h.send(START)
    h.send(HotkeyEvent(Action.CANCEL, "shortcut"))
    assert h.overlay.calls[-1] == "hidden"
    assert h.injector.pasted == []
    assert h.dictation.state == DictationState.READY
    h.send(HotkeyEvent(Action.CANCEL, "escape"))  # nothing recording: no-op
    h.send(STOP)
    assert h.dictation.state == DictationState.READY


def test_mic_problem_on_start_explains_and_resets_the_key():
    h = Harness(start_error=MicError("Windows is blocking the microphone. Open ...", "Mic blocked"))
    h.dictation.start()
    h.send(START)
    assert h.overlay.calls[-1] == "error:Mic blocked"
    assert h.notes and "blocking the microphone" in h.notes[0][1]
    assert h.listeners[0].forced == 1
    assert h.dictation.state == DictationState.READY
    h.dictation.stop()


def test_unexpected_recorder_error_on_start():
    h = Harness(start_error=RuntimeError("PortAudio not initialized"))
    h.dictation.start()
    h.send(START)
    assert h.overlay.calls[-1] == "error:Mic problem"
    assert h.dictation.state == DictationState.READY
    h.dictation.stop()


def test_silent_mic_on_stop():
    h = Harness(stop_error=MicError("The microphone only sent silence.", "Mic is silent"))
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    assert h.overlay.calls[-1] == "error:Mic is silent"
    assert h.injector.pasted == []
    assert h.dictation.state == DictationState.READY
    h.dictation.stop()


def test_not_ready_shows_download_progress():
    h = Harness(ready=False)
    h.dictation.start()
    h.state.set_progress(Progress(42, 100))
    h.send(START)
    assert h.overlay.calls[-1] == "notice:Getting ready… 42%"
    assert h.recorders == [] and h.listeners[0].forced == 1
    h.state.set_progress(None)
    h.send(START)
    assert h.overlay.calls[-1] == "notice:Getting ready…"
    h.state.set_status(HubStatus.ERROR, "CUDA is not available")
    h.send(START)
    assert h.overlay.calls[-1] == "error:Not working"
    assert ("ShoutAbout", "CUDA is not available") in h.notes
    h.dictation.stop()


def test_no_speech_is_a_gentle_notice():
    h = Harness(engine=FakeEngine(text=""))
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("notice")
    assert h.overlay.calls[-1] == "notice:Didn't catch that"
    assert h.injector.pasted == []
    h.dictation.stop()


def test_admin_window_is_reported_once():
    h = Harness(injector=FakeInjector(PasteResult(False, ADMIN_WINDOW_WARNING)))
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("error")
    assert h.overlay.calls[-1] == "error:Admin window"
    assert h.notes == []  # the injector already showed a toast
    h.dictation.stop()


def test_other_paste_problems_notify():
    h = Harness(
        injector=FakeInjector(PasteResult(False, "The PC is locked — text is on your clipboard."))
    )
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("error")
    assert h.overlay.calls[-1] == "error:PC is locked"
    assert h.notes == [("ShoutAbout", "The PC is locked — text is on your clipboard.")]
    h.dictation.stop()


def test_engine_failure_while_transcribing():
    h = Harness(engine=FakeEngine(fail=EngineError("Transcription failed: CUDA error")))
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("error")
    assert h.overlay.calls[-1] == "error:Not working"
    assert any("CUDA error" in m for _, m in h.notes)
    h.wait_state(DictationState.READY)
    h.dictation.stop()


def test_time_limit_finishes_the_recording(h):
    h.send(START)
    h.recorders[0].on_limit()
    h.wait_overlay("done")
    assert h.listeners[0].forced == 1
    assert h.injector.pasted == ["hello from the pc"]


def test_new_recording_while_the_last_is_transcribing():
    gate = threading.Event()

    class SlowEngine(FakeEngine):
        def transcribe(self, audio, hints):
            gate.wait(3)
            return super().transcribe(audio, hints)

    h = Harness(engine=SlowEngine(text="first"))
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_state(DictationState.THINKING)
    h.send(START)  # a second dictation starts while the first one is still running
    assert h.dictation.state == DictationState.LISTENING
    gate.set()
    deadline = time.monotonic() + 3
    while not h.injector.pasted and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.05)
    assert h.overlay.calls[-1] == "listening"  # the old result didn't paint over it
    assert h.dictation.state == DictationState.LISTENING
    h.send(STOP)
    h.wait_overlay("done")
    assert h.injector.pasted == ["first", "first"]
    h.dictation.stop()


def test_phone_and_pc_share_the_busy_lock(h):
    with h.state.busy:  # e.g. a phone request is being transcribed
        h.send(START)
        h.send(STOP)
        time.sleep(0.1)
        assert h.injector.pasted == []  # waits its turn
    h.wait_overlay("done")
    assert h.injector.pasted == ["hello from the pc"]


def test_pause_stops_listening_and_resume_starts_again():
    h = Harness()
    h.dictation.follow_pause()
    h.dictation.start()
    h.state.set_paused(True)
    h.wait_state(DictationState.OFF)
    assert h.listeners[0].stopped == 1 and h.overlay.stopped == 1
    h.state.set_status(HubStatus.IDLE)  # other state changes don't toggle anything
    h.state.set_paused(False)
    h.wait_state(DictationState.READY)
    assert len(h.listeners) == 2 and h.listeners[1].started == 1
    h.dictation.stop()


def test_paused_hub_never_records(h):
    h.state._paused = True  # paused without the listener noticing yet
    h.send(START)
    assert h.overlay.calls[-1] == "notice:Paused" and h.recorders == []


def test_disabled_does_not_start():
    cfg = parse_config("[dictation]\nenabled = false\n")
    h = Harness(cfg)
    assert h.dictation.start() is False
    assert h.listeners == [] and h.dictation.state == DictationState.OFF


def test_hook_failure_is_reported_and_leaves_it_off():
    h = Harness()
    h.listener_fail = True
    assert h.dictation.start() is False
    assert h.dictation.state == DictationState.OFF
    assert h.notes and "SetWindowsHookExW" in h.notes[0][1]
    assert h.overlay.stopped == 1


def test_reload_applies_new_settings(h):
    cfg = parse_config('[dictation]\nhotkey = "f9"\nmode = "toggle"\nmax_seconds = 30\n')
    h.dictation.reload(cfg)
    assert h.listeners[0].stopped == 1
    assert (h.listeners[1].hotkey, h.listeners[1].mode) == ("f9", "toggle")
    h.send(START)
    assert h.recorders[-1].max_seconds == 30
    h.dictation.reload(parse_config("[dictation]\nenabled = false\n"))
    assert h.dictation.state == DictationState.OFF


def test_stop_drops_a_recording_and_is_idempotent(h):
    h.send(START)
    h.dictation.stop()
    assert h.recorders[0].cancelled == 1
    assert h.listeners[0].stopped == 1
    assert h.dictation.state == DictationState.OFF
    h.dictation.stop()
    assert h.listeners[0].stopped == 1


def test_state_listeners(h):
    seen = []
    h.dictation.subscribe(seen.append)
    h.send(START)
    h.send(STOP)
    h.wait_state(DictationState.READY)
    assert seen[:2] == [DictationState.LISTENING, DictationState.THINKING]
    assert seen[-1] == DictationState.READY


def test_hint_text():
    assert hint_text(Config()) == "Press Right Ctrl to talk, and again to stop."
    toggle = parse_config('[dictation]\nhotkey = "f9"\nmode = "toggle"\n')
    assert hint_text(toggle) == "Press F9 to talk, and again to stop."
    assert hint_text(parse_config('[dictation]\nmode = "hold"\n')) == "Hold Right Ctrl to talk."
    assert hint_text(parse_config("[dictation]\nenabled = false\n")) == "Talk to this PC is off."


def test_greet_says_ready_with_the_hotkey(h):
    assert h.dictation.greet()
    assert h.overlay.calls == ["notice:Ready · Right Ctrl to talk"]


def test_greet_waits_its_turn_and_needs_dictation_on(h):
    h.send(START)
    assert not h.dictation.greet()  # listening: never cover the live pill
    h.dictation.stop()
    assert not h.dictation.greet()


def test_ready_text_fits_the_pill():
    from shoutabout.hotkey import HOTKEY_CHOICES

    assert ready_text(Config()) == "Ready · Right Ctrl to talk"
    for key in HOTKEY_CHOICES:
        text = ready_text(parse_config(f'[dictation]\nhotkey = "{key}"\n'))
        assert len(text) <= 28 and text.startswith("Ready")


def test_overlay_warning_words():
    assert overlay_warning(ADMIN_WINDOW_WARNING) == "Admin window"
    assert overlay_warning(PAUSED_WARNING) == "Paused"
    assert overlay_warning("The PC is locked — text is on your clipboard.") == "PC is locked"
    assert overlay_warning("A modifier key is held on the PC") == "Key still held"
    assert overlay_warning("anything else") == "Not pasted"


# ------------------------------------------------------------- v2 feedback


def test_the_pill_is_shown_before_the_microphone_opens(h):
    h.send(START)
    assert h.recorders[0].overlay_at_start == ["listening"]  # pill first, then the mic


def test_pasted_ends_with_the_closing_mouth_not_a_check_mark(h):
    h.send(START)
    h.send(STOP)
    h.wait_overlay("done")
    assert h.overlay.calls == ["listening", "thinking", "done"]


def test_no_microphone_shows_the_red_pill_every_press_but_notifies_once():
    h = Harness(mic_present=lambda: False)
    h.dictation.start()
    for _ in range(3):
        h.send(START)
        assert h.overlay.calls[-1] == "error:No mic"
        assert h.dictation.state == DictationState.READY
    assert h.recorders == []  # nothing tried to open a microphone
    assert len(h.notes) == 1 and "No microphone found" in h.notes[0][1]
    assert h.listeners[0].forced == 3
    h.dictation.stop()


def test_mic_errors_notify_once_per_session():
    h = Harness(start_error=MicError("Windows is blocking the microphone. Open ...", "Mic blocked"))
    h.dictation.start()
    h.send(START)
    h.send(START)
    assert h.overlay.calls.count("error:Mic blocked") == 2
    assert len(h.notes) == 1
    h.dictation.stop()


def test_unknown_microphone_state_still_records():
    h = Harness(mic_present=lambda: None)  # couldn't ask Windows: just try
    h.dictation.start()
    h.send(START)
    assert h.dictation.state == DictationState.LISTENING
    h.dictation.stop()


def test_failed_transcription_keeps_the_recording_for_history(tmp_path):
    from shoutabout.failed_audio import FailedRecordings

    failed = FailedRecordings(tmp_path / "failed")
    h = Harness(engine=FakeEngine(fail=EngineError("CUDA out of memory")), failed=failed)
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("error")
    (item,) = failed.list()
    assert item.route == "local" and item.seconds == pytest.approx(2.0)
    assert "CUDA out of memory" in item.error
    assert any("open History" in m for _, m in h.notes)
    h.dictation.stop()


def test_failed_transcription_is_not_kept_when_history_is_off(tmp_path):
    from shoutabout.failed_audio import FailedRecordings

    failed = FailedRecordings(tmp_path / "failed", enabled=False)
    h = Harness(engine=FakeEngine(fail=EngineError("CUDA out of memory")), failed=failed)
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("error")
    assert failed.list() == []
    assert not any("History" in m for _, m in h.notes)
    h.dictation.stop()


@pytest.mark.parametrize(
    "warning",
    [
        "The PC is locked — text is on your clipboard.",
        "No focused window on the PC (locked?) — text is on your clipboard.",
        ADMIN_WINDOW_WARNING,
    ],
)
def test_history_keeps_the_text_when_pasting_fails(tmp_path, warning):
    import json

    from shoutabout.history import HistoryLog

    h = Harness(injector=FakeInjector(PasteResult(False, warning)))
    h.pipeline.history = HistoryLog(tmp_path / "history.jsonl", tmp_path / "audio", False)
    h.dictation.start()
    h.send(START)
    h.send(STOP)
    h.wait_overlay("error")
    (record,) = [
        json.loads(x) for x in (tmp_path / "history.jsonl").read_text("utf-8").split("\n") if x
    ]
    assert record["text"] == "hello from the pc" and record["pasted"] is False
    assert record["warning"] == warning and record["route"] == "local"
    h.dictation.stop()


def test_warm_mic_runs_when_listening_starts():
    called = threading.Event()
    state = HubState()
    d = LocalDictation(
        Config(),
        state=state,
        pipeline=lambda: None,
        listener_factory=FakeListener,
        recorder_factory=FakeRecorder,
        overlay_factory=lambda enabled: NullOverlay(),
        warm_mic=called.set,
    )
    assert d.start()
    assert called.wait(2)
    d.stop()


@pytest.mark.parametrize("mode", ["toggle", "hold"])
def test_right_ctrl_shortcuts_never_show_the_pill(mode):
    """Owner feedback: the mouth popped up during Right Ctrl + C / V. Real HotkeyListener
    (fake hook), with the physical key state lagging the hook like Windows does."""
    from shoutabout.hotkey import HOTKEY_VKS, HotkeyListener

    rctrl, key_c, key_v, shift, key_t = HOTKEY_VKS["right_ctrl"], 0x43, 0x56, 0xA0, 0x54
    hooks = []

    class Hook:
        def __init__(self, on_key) -> None:
            self.on_key = on_key
            hooks.append(self)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    def listener_factory(hotkey, mode_, on_event):
        return HotkeyListener(
            hotkey, mode_, on_event, hook_factory=Hook, key_state=lambda vk: False
        )

    cfg = parse_config(f'[dictation]\nmode = "{mode}"\n')
    h = Harness(cfg)
    h.dictation._listener_factory = listener_factory
    assert h.dictation.start()
    key = hooks[0].on_key
    for combo in ([key_c], [key_v], [shift, key_t]):
        key(rctrl, True, 0x1D, 0)
        for vk in combo:
            time.sleep(0.03)
            key(vk, True, 0, 0)
        time.sleep(0.03)
        for vk in reversed(combo):
            key(vk, False, 0, 0)
        key(rctrl, False, 0x1D, 0)
        time.sleep(0.15)
    # held down for a while with C pressed late (after the hold delay in hold mode the
    # listening has started, and the shortcut cancels it quietly: no pill left on screen)
    time.sleep(0.4)
    assert h.overlay.calls == [], h.overlay.calls  # never shown, not even for a moment
    assert h.recorders == []  # and the microphone never opened
    # a clean tap (toggle) / hold (hold mode) still works
    key(rctrl, True, 0x1D, 0)
    time.sleep(0.08 if mode == "toggle" else 0.35)
    key(rctrl, False, 0x1D, 0)
    deadline = time.monotonic() + 3
    while "listening" not in h.overlay.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    assert h.overlay.calls[0] == "listening"
    h.dictation.stop()
