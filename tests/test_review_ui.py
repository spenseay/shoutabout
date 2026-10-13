"""Teach words: add words and replacements, see and remove them."""

from __future__ import annotations

import subprocess
import sys

import pytest

from shoutabout import review_ui
from shoutabout.config import Paths
from shoutabout.vocab import read_entries, read_terms

VOCAB = ["Better Shell", "Claude Code"]


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "home")
    p.home.mkdir()
    p.vocab_file.write_text("# vocab\n" + "\n".join(VOCAB) + "\n", "utf-8")
    return p


def test_review_ui_import_is_headless():
    code = "import sys, shoutabout.review_ui; print('tkinter' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_messages():
    assert review_ui.add_message("added", "Sofia") == (
        'Added "Sofia". ShoutAbout will spell it that way.'
    )
    assert "already" in review_ui.add_message("exists", "GitHub")
    assert "too long" in review_ui.add_message("invalid", "x", "too long")
    assert review_ui.add_message("added", "omw -> on my way") == (
        'Added. Saying "omw" now writes "on my way".'
    )
    assert review_ui.add_message("exists", "omw -> on my way") == (
        '"omw" already has a replacement: omw → on my way.'
    )
    assert "Whisper" not in review_ui.add_message("added", "GitHub")


def test_replacement_entries():
    assert review_ui.compose_entry("/consolidate", "slash consolidate") == (
        "slash consolidate -> /consolidate"
    )
    assert review_ui.compose_entry("Sofia", "  ") == "Sofia"
    assert review_ui.display_entry("omw -> on my way") == "omw → on my way"
    assert review_ui.display_entry("Sofia") == "Sofia"


def test_read_words(paths):
    paths.vocab_file.write_text("# vocab\nomw -> on my way\nSofia\n", "utf-8")
    assert review_ui.read_words(paths) == ["omw -> on my way", "Sofia"]


def _pump(root, seconds: float) -> None:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


@pytest.mark.windows
def test_teach_page_adds_and_removes(paths):
    tk = pytest.importorskip("tkinter")
    try:
        window = tk.Tk()
    except tk.TclError as exc:  # no display, or Tcl not ready for another interpreter
        pytest.skip(f"no Tk: {exc}")
    window.withdraw()
    try:
        page = review_ui.TeachPage(tk.Frame(window), paths)
        _pump(window, 0.4)
        assert page.vocab_terms == VOCAB
        page.entry.insert(0, "Priya Raghunathan")
        page.add_word()
        _pump(window, 0.4)
        assert read_terms(paths.vocab_file)[0] == "Priya Raghunathan"
        assert page.vocab_terms[0] == "Priya Raghunathan"
        page.entry.insert(0, "on my way")
        page.say_entry.insert(0, "omw")
        page.add_word()
        _pump(window, 0.4)
        assert page.vocab_terms[0] == "omw -> on my way"
        assert page.vocab_list.get(0).endswith("omw → on my way")
        assert page.say_entry.get() == ""
        page.vocab_list.selection_set(0)
        page.remove_selected()
        _pump(window, 0.4)
        assert read_entries(paths.vocab_file) == ["Priya Raghunathan", *VOCAB]
        page.close()
        assert page.closed
    finally:
        window.destroy()
