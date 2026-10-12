"""Recent on iPhone (ios/Shared/Core/HistoryStore.swift): the last 3 dictations, in memory
only, never written to disk, and no "Keep history" switch any more.

Swift can't be compiled here (no Mac); these pin down the owner's privacy decision so a later
change can't quietly start saving dictations again. The XCTests (HistoryStoreTests) cover the
behaviour itself on Codemagic."""

from __future__ import annotations

import re
from pathlib import Path

IOS = Path(__file__).resolve().parents[1] / "ios"


def code_only(text: str) -> str:
    """Swift without string literals and comments."""
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def test_recent_keeps_three_in_memory_only():
    source = (IOS / "Shared" / "Core" / "HistoryStore.swift").read_text("utf-8")
    assert re.search(r"\bstatic let limit = 3\b", source)
    code = code_only(source)
    for banned in (".write(", "JSONEncoder", "createDirectory", "setResourceValues", ".set("):
        assert banned not in code, banned
    assert not (IOS / "ShoutAbout" / "Services" / "HistoryStore.swift").exists()


def test_the_keep_history_switch_is_gone():
    files = sorted((IOS / "ShoutAbout").rglob("*.swift"))
    files.append(IOS / "Shared" / "Core" / "SharedSettings.swift")
    for path in files:
        code = code_only(path.read_text("utf-8"))
        assert not re.search(r"keepHistory|KeepHistory", code), path.name
    settings = (IOS / "ShoutAbout" / "Views" / "SettingsView.swift").read_text("utf-8")
    assert "Keep history" not in settings


def test_launch_deletes_what_older_versions_saved():
    model = code_only((IOS / "ShoutAbout" / "App" / "AppModel.swift").read_text("utf-8"))
    assert "HistoryStore.removeSavedHistory(defaults: settings.defaults)" in model
    store = (IOS / "Shared" / "Core" / "HistoryStore.swift").read_text("utf-8")
    assert '"history.json"' in store  # the old file's name
    assert 'legacyKeepHistoryKey = "keepHistory"' in store  # the old switch's App Group key
