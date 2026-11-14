"""The "not pasted" pop-up's router: the dictation pill, else its own pill, else a toast."""

from __future__ import annotations

from shoutabout.inject.base import NOT_PASTED_HOLD_S, NOT_PASTED_NOTICE
from shoutabout.overlay import NullOverlay
from shoutabout.popup import Popup


class Pill(NullOverlay):
    shows = True

    def __init__(self) -> None:
        super().__init__()
        self.started = self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class Dictation:
    def __init__(self, free: bool) -> None:
        self.free = free
        self.shown: list[tuple[str, float | None]] = []

    def popup(self, text, hold=None):
        if self.free:
            self.shown.append((text, hold))
        return self.free


def make(dictation=None, enabled=True, factory=None):
    notes: list[tuple[str, str]] = []
    made: list[Pill] = []

    def default_factory():
        made.append(Pill())
        return made[-1]

    popup = Popup(
        notify=lambda t, m: notes.append((t, m)),
        dictation=lambda: dictation,
        overlay_enabled=lambda: enabled,
        overlay_factory=factory or default_factory,
    )
    return popup, notes, made


def test_the_dictation_pill_comes_first():
    d = Dictation(free=True)
    popup, notes, made = make(d)
    assert popup.show(NOT_PASTED_NOTICE) == "dictation"
    assert d.shown == [(NOT_PASTED_NOTICE, NOT_PASTED_HOLD_S)]
    assert made == [] and notes == []


def test_own_pill_when_talk_to_this_pc_is_off_or_busy():
    popup, notes, made = make(Dictation(free=False))
    assert popup.show("one") == "overlay"
    assert popup.show("two") == "overlay"
    assert len(made) == 1 and made[0].started == 1  # made once, on first use
    assert made[0].state == "notice" and made[0].text == "two"
    assert made[0].hold == NOT_PASTED_HOLD_S
    assert notes == []
    popup.stop()
    assert made[0].stopped == 1


def test_no_dictation_at_all():
    popup, notes, made = make(None)
    assert popup.show("x") == "overlay" and len(made) == 1


def test_pill_turned_off_means_a_notification():
    popup, notes, made = make(None, enabled=False)
    assert popup.show(NOT_PASTED_NOTICE) == "notification"
    assert notes == [("ShoutAbout", NOT_PASTED_NOTICE)] and made == []


def test_no_window_possible_means_notifications_from_then_on():
    made = []

    def factory():
        made.append(NullOverlay())  # not Windows / macOS: nothing would appear
        return made[-1]

    popup, notes, _ = make(None, factory=factory)
    assert popup.show("x") == "notification"
    assert popup.show("y") == "notification"
    assert len(made) == 1 and [m for _, m in notes] == ["x", "y"]


def test_failures_fall_through_and_never_raise():
    class BadDictation:
        def popup(self, text, hold=None):
            raise RuntimeError("gone")

    def bad_factory():
        raise RuntimeError("no GDI")

    popup = Popup(
        notify=lambda t, m: (_ for _ in ()).throw(RuntimeError("no tray")),
        dictation=lambda: BadDictation(),
        overlay_factory=bad_factory,
    )
    assert popup.show("x") == "notification"  # and nothing raised


def test_a_pill_that_fails_to_start_is_not_retried():
    class Broken(Pill):
        def start(self):
            raise OSError("RegisterClassExW failed")

    made = []

    def factory():
        made.append(Broken())
        return made[-1]

    popup, notes, _ = make(None, factory=factory)
    assert popup.show("x") == "notification"
    assert popup.show("y") == "notification"
    assert len(made) == 1


def test_dictation_lookup_before_it_exists():
    def not_yet():
        raise NameError("voice")

    popup = Popup(notify=lambda t, m: None, dictation=not_yet, overlay_factory=Pill)
    assert popup.show("x") == "overlay"
