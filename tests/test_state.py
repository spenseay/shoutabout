"""HubState v2 additions: download progress, phase, model choice, busy lock."""

from __future__ import annotations

from shoutabout.models import HubStatus
from shoutabout.state import HubState, Progress


def test_progress_text_and_percent():
    assert Progress(42, 100).percent == 42
    assert Progress(42, 100).text == "Getting ready… 42%"
    assert Progress(5, 0).percent is None and Progress(5, 0).text == "Getting ready…"
    assert Progress(200, 100).percent == 100


def test_phase_and_listeners_only_hear_whole_percent_changes():
    state = HubState()
    calls = []
    state.subscribe(lambda s: calls.append((s.phase, s.progress)))
    assert state.phase == "starting" and not state.preparing
    for done in (0, 1, 2, 3, 10, 11, 500):
        state.set_progress(Progress(done * 1000, 1_000_000))
    assert state.preparing and state.phase == "preparing"
    assert state.progress == 50 and state.download == Progress(500_000, 1_000_000)
    assert state.progress_text == "Getting ready… 50%"
    assert calls == [("preparing", 0), ("preparing", 1), ("preparing", 50)]
    state.set_status(HubStatus.IDLE)
    assert state.progress is None and state.phase == "idle"
    assert state.download is None and state.progress_text is None
    state.set_progress(None)
    assert calls[-1] == ("idle", None)


def test_choice_and_unsubscribe():
    state = HubState()
    calls = []

    def listener(s):
        calls.append(s.choice)

    state.subscribe(listener)
    state.set_choice("x")  # any object; normally a ModelChoice
    state.unsubscribe(listener)
    state.set_choice(None)
    assert calls == ["x"] and state.choice is None
    state.unsubscribe(listener)  # harmless twice


def test_busy_is_reentrant():
    state = HubState()
    with state.busy, state.busy:
        pass
