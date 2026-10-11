"""One-time config upgrades: only lines an older version generated, only once."""

from __future__ import annotations

import json

from shoutabout.config import Paths, ensure_initialized, load_config
from shoutabout.migrations import (
    DICTATION_DEFAULTS,
    OLD_GENERATED_LINES,
    migrate,
    untouched_dictation_defaults,
)

OLD_DICTATION = """[dictation]
# Talk to this PC: hold the hotkey, speak, let go; the text is pasted where you're typing.
enabled = true
# right_ctrl, right_alt, f8, f9, f10, scroll_lock or pause
hotkey = "right_ctrl"
mode = "hold"              # "hold" (hold to talk) or "toggle" (press to start, press to stop)
overlay = true             # the small mouth near the bottom of the screen
max_seconds = 120          # stop listening after this long
"""

# The [history] section earlier versions generated: history on, spelled out.
OLD_HISTORY = """[history]
# Keep what you dictate in history.jsonl on this PC (tray > History...). If writing out a
# recording fails, it is kept too (the last few, in the "failed" folder) so you can try again.
enabled = true
keep_audio = false
"""


def home(tmp_path, config: str | None) -> Paths:
    paths = Paths(tmp_path / "home")
    paths.home.mkdir(parents=True)
    if config is not None:
        paths.config_file.write_text(config, "utf-8")
    return paths


def test_an_untouched_generated_config_gets_the_new_defaults(tmp_path):
    paths = home(
        tmp_path, "[server]\nport = 47821\n\n" + OLD_DICTATION + '\n[logging]\nlevel = "INFO"\n'
    )
    assert migrate(paths) == ["dictation.mode = 'toggle'", "dictation.max_seconds = 600"]
    cfg = load_config(paths)
    assert (cfg.dictation.mode, cfg.dictation.max_seconds) == ("toggle", 600)
    text = paths.config_file.read_text("utf-8")
    # only the two values changed: comments and everything else stay
    assert 'mode = "toggle"              # "hold" (hold to talk)' in text
    assert "max_seconds = 600          # stop listening after this long" in text
    assert "port = 47821" in text and 'level = "INFO"' in text
    done = json.loads(paths.migrations_file.read_text("utf-8"))
    assert DICTATION_DEFAULTS in done


def test_it_runs_only_once(tmp_path):
    paths = home(tmp_path, OLD_DICTATION)
    migrate(paths)
    # the person picks "Hold to talk" again afterwards: it stays
    text = paths.config_file.read_text("utf-8").replace('mode = "toggle"', 'mode = "hold"')
    paths.config_file.write_text(text, "utf-8")
    assert migrate(paths) == []
    assert load_config(paths).dictation.mode == "hold"


def test_real_choices_are_never_changed(tmp_path):
    old_mode, old_max = list(OLD_GENERATED_LINES)
    edited = OLD_DICTATION.replace(old_mode, 'mode = "hold"  # I like holding').replace(
        old_max, "max_seconds = 90"
    )
    paths = home(tmp_path, edited)
    assert migrate(paths) == []
    cfg = load_config(paths)
    assert (cfg.dictation.mode, cfg.dictation.max_seconds) == ("hold", 90)
    assert paths.migrations_file.exists()  # and it won't look again


def test_only_the_dictation_section_counts():
    text = "[other]\nmax_seconds = 120          # stop listening after this long\n"
    assert untouched_dictation_defaults(text) == {}
    assert untouched_dictation_defaults(OLD_DICTATION) == {"mode": "toggle", "max_seconds": 600}


def test_configs_without_the_section_or_file(tmp_path):
    paths = home(tmp_path, "[server]\nport = 8766\n")  # a v1 config: defaults apply anyway
    assert migrate(paths) == []
    assert paths.config_file.read_text("utf-8") == "[server]\nport = 8766\n"
    assert load_config(paths).dictation.mode == "toggle"
    fresh = home(tmp_path / "fresh", None)
    assert migrate(fresh) == [] and fresh.migrations_file.exists()


def test_a_fresh_install_needs_nothing(tmp_path):
    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    before = paths.config_file.read_text("utf-8")
    assert migrate(paths) == []
    assert paths.config_file.read_text("utf-8") == before


def test_a_broken_config_is_left_alone(tmp_path):
    paths = home(tmp_path, OLD_DICTATION + "\n[server]\nport = 'not a number'\n")
    assert migrate(paths) == []
    assert "max_seconds = 120" in paths.config_file.read_text("utf-8")
    assert not paths.migrations_file.exists()  # tried again next time


def test_history_is_off_on_a_fresh_install(tmp_path):
    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)  # what the hub does at start, before reading the config
    migrate(paths)
    assert load_config(paths).history.enabled is False
    text = paths.config_file.read_text("utf-8")
    assert (
        "# Off by default. Turn it on in Settings or in the History window.\nenabled = false\n"
        in text
    )


def test_an_existing_install_keeps_its_history_on(tmp_path):
    """History is off by default now, but nothing turns it off on an existing install: the
    config.toml an earlier version generated says ``enabled = true``, and that stays."""
    paths = home(tmp_path, "[server]\nport = 8766\n\n" + OLD_HISTORY + "\n" + OLD_DICTATION)
    ensure_initialized(paths)  # never overwrites an existing config.toml
    migrate(paths)
    assert load_config(paths).history.enabled is True
    assert OLD_HISTORY in paths.config_file.read_text("utf-8")
    assert migrate(paths) == []  # and later starts leave it alone too
    assert load_config(paths).history.enabled is True
