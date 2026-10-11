"""Opening ShoutAbout says so: the "Ready" pill once the engine is ready, and a second
launch shows the running copy instead of exiting silently."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from shoutabout import reopen
from shoutabout.hub import greet_when_ready
from shoutabout.local_dictation import DictationState
from shoutabout.models import HubStatus
from shoutabout.state import HubState


class FakeDictation:
    def __init__(self, state=DictationState.READY, greets=True):
        self.state = state
        self.greets = greets
        self.greeted = 0

    def greet(self):
        self.greeted += 1
        return self.greets


class FakeVoice:
    def __init__(self, dictation=None):
        self.dictation = dictation


def run(state, voice, *, wait_s=5.0):
    notes: list[tuple[str, str]] = []
    result = greet_when_ready(
        state,
        voice,
        lambda t, m: notes.append((t, m)),
        threading.Event(),
        poll_s=0.01,
        wait_s=wait_s,
    )
    return result, notes


def test_greets_with_the_pill_once_the_engine_is_ready():
    state, dictation = HubState(), FakeDictation()
    threading.Timer(0.05, state.set_status, args=(HubStatus.IDLE,)).start()
    t0 = time.monotonic()
    result, notes = run(state, FakeVoice(dictation))
    assert result == "pill" and dictation.greeted == 1 and notes == []
    assert time.monotonic() - t0 >= 0.04  # waited for the engine


def test_talk_to_this_pc_off_gets_a_notification_instead():
    state = HubState()
    state.set_status(HubStatus.IDLE)
    result, notes = run(state, FakeVoice(None))
    assert result == "notified"
    assert notes and notes[0][0] == "ShoutAbout is running" and "yellow mouth" in notes[0][1]
    result, _ = run(state, FakeVoice(FakeDictation(DictationState.OFF)))
    assert result == "notified"


def test_never_covers_a_dictation_in_progress():
    state = HubState()
    state.set_status(HubStatus.IDLE)
    result, notes = run(state, FakeVoice(FakeDictation(DictationState.LISTENING, greets=False)))
    assert result == "busy" and notes == []


def test_an_engine_error_is_left_to_its_own_notification():
    state = HubState()
    state.set_status(HubStatus.ERROR, "no model")
    assert run(state, FakeVoice(FakeDictation()))[0] == "error"


def test_gives_up_quietly():
    assert run(HubState(), FakeVoice(FakeDictation()), wait_s=0.05)[0] == "gave up"


@pytest.mark.skipif(sys.platform != "win32", reason="named events are Windows-only")
def test_a_second_launch_reaches_the_running_hub():
    name = f"Local\\ShoutAboutReopenTest{time.monotonic_ns()}"
    assert not reopen.ask_running_hub(name)  # nothing running yet
    opened = threading.Event()
    stopping = threading.Event()
    assert reopen.watch(opened.set, stopping, name)
    try:
        assert reopen.ask_running_hub(name)
        assert opened.wait(3.0)
    finally:
        stopping.set()


@pytest.mark.skipif(sys.platform == "win32", reason="other platforms only")
def test_reopen_is_a_no_op_elsewhere():
    assert not reopen.ask_running_hub()
    assert not reopen.watch(lambda: None, threading.Event())
