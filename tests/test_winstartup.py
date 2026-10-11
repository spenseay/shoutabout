"""Settings > Start with Windows: Run key for the installed app, read-only otherwise."""

from __future__ import annotations

import pytest

from shoutabout.winstartup import VALUE_NAME, StartupManager, run_command

EXE = r"C:\Program Files\ShoutAbout\ShoutAbout.exe"


class FakeRunKey:
    def __init__(self, values=None) -> None:
        self.values = dict(values or {})
        self.writes: list[tuple[str, str | None]] = []

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value
        self.writes.append((name, value))

    def delete(self, name):
        self.values.pop(name, None)
        self.writes.append((name, None))


def manager(key, *, frozen, task=False):
    asked = []

    def task_exists():
        asked.append(1)
        return task

    m = StartupManager(run_key=key, frozen=frozen, executable=EXE, task_exists=task_exists)
    return m, asked


def test_installed_app_turns_start_with_windows_on_and_off():
    key = FakeRunKey()
    m, asked = manager(key, frozen=True)
    assert m.state().enabled is False and m.state().editable is True
    state = m.set_enabled(True)
    assert state.enabled and key.values[VALUE_NAME] == f'"{EXE}" run'
    state = m.set_enabled(False)
    assert not state.enabled and VALUE_NAME not in key.values
    assert asked == []  # the scheduled task is never even looked at


def test_installed_app_spots_another_copy():
    key = FakeRunKey({VALUE_NAME: '"D:\\old\\ShoutAbout.exe" run'})
    m, _ = manager(key, frozen=True)
    state = m.state()
    assert not state.enabled and "Another copy" in state.note


@pytest.mark.parametrize(
    ("values", "task", "enabled"),
    [({}, True, True), ({}, False, False), ({VALUE_NAME: "x"}, False, True)],
)
def test_source_checkout_is_read_only(values, task, enabled):
    key = FakeRunKey(values)
    m, _ = manager(key, frozen=False, task=task)
    state = m.state()
    assert state.enabled is enabled and state.editable is False and state.note
    with pytest.raises(PermissionError):
        m.set_enabled(True)
    with pytest.raises(PermissionError):
        m.set_enabled(False)
    assert key.writes == []


def test_run_command_quotes_the_path():
    assert run_command(EXE) == f'"{EXE}" run'
