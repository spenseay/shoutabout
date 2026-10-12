"""One-time config upgrades: only lines an older version generated, only once."""

from __future__ import annotations

import json

from shoutabout.config import Paths, ensure_initialized, load_config
from shoutabout.migrations import (
    DICTATION_DEFAULTS,
    HISTORY_GONE,
    LEARN_PROJECTS_OFF,
    OLD_GENERATED_LINES,
    migrate,
    project_scanning_on,
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


def test_a_fresh_install_has_no_history_at_all(tmp_path):
    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)  # what the hub does at start, before reading the config
    assert migrate(paths) == []
    assert "[history]" not in paths.config_file.read_text("utf-8")
    assert not paths.history_file.exists() and not paths.failed_dir.exists()


def test_the_old_history_is_deleted_once(tmp_path):
    """ShoutAbout keeps no history now: what an earlier version kept on disk goes, once."""
    paths = home(tmp_path, "[server]\nport = 8766\n\n" + OLD_HISTORY + "\n" + OLD_DICTATION)
    paths.history_file.write_text('{"text": "something private"}\n', "utf-8")
    (paths.audio_dir / "nested").mkdir(parents=True)
    (paths.audio_dir / "20260101-a.m4a").write_bytes(b"audio")
    paths.failed_dir.mkdir()
    (paths.failed_dir / "20260101-000000-000000.wav").write_bytes(b"RIFF")
    write_suggestions(
        paths,
        [("Priya Raghunathan", "history", "pending"), ("GitHub", "history", "dismissed")],
    )
    ensure_initialized(paths)  # never overwrites an existing config.toml
    did = migrate(paths)
    assert "deleted history.jsonl" in did and "deleted audio" in did and "deleted failed" in did
    assert "removed [history]" in did and "dropped 1 history suggestion(s)" in did
    assert not paths.history_file.exists()
    assert not paths.audio_dir.exists() and not paths.failed_dir.exists()
    text = paths.config_file.read_text("utf-8")
    assert "[history]" not in text and "keep_audio" not in text
    assert "port = 8766" in text and 'mode = "toggle"' in text  # the rest stays
    from shoutabout.learn import load_suggestions

    (left,) = load_suggestions(paths)  # the decision is remembered, where it came from isn't
    assert (left.term, left.status, left.detail) == ("GitHub", "dismissed", "")
    assert HISTORY_GONE in json.loads(paths.migrations_file.read_text("utf-8"))
    paths.history_file.write_text("{}\n", "utf-8")  # (nothing writes it any more)
    assert migrate(paths) == []  # once
    assert paths.history_file.exists()


def test_an_old_history_section_still_loads(tmp_path):
    """The CLI reads config.toml without upgrading it: [history] must not stop it."""
    paths = home(tmp_path, OLD_HISTORY)
    assert load_config(paths).learn.seed_projects is False


# The [learn] section earlier versions generated: project folders scanned every day.
OLD_LEARN = """[learn]
# Suggest words to teach ShoutAbout (tray > Teach ShoutAbout...). Suggestions come from your
# dictation history (only while history is on) and project folders, stay on this PC, and are
# never added without you.
enabled = true
seed_projects = true       # also suggest project/repo names from your project folders
# Folders scanned 2 levels deep; empty = ~/Documents, ~/source/repos, ~/repos, ~/code, ~/projects
project_roots = []
notify = true              # one tray notification when new suggestions are waiting
"""


def write_suggestions(paths: Paths, rows: list[tuple[str, str, str]]) -> None:
    from shoutabout.learn import Suggestion, _save

    _save(paths, [Suggestion(t, src, 1, "", "2026-01-01T00:00:00", st) for t, src, st in rows])


def test_project_folder_scanning_is_switched_off_once(tmp_path):
    from shoutabout.learn import load_suggestions

    paths = home(tmp_path, OLD_LEARN)
    write_suggestions(
        paths,
        [
            ("Chameleon CV", "projects", "pending"),
            ("Better Shell", "projects", "dismissed"),
            ("Paperclip", "projects", "accepted"),
            ("Priya Raghunathan", "history", "pending"),
        ],
    )
    did = migrate(paths)
    assert did[:2] == ["learn.seed_projects = False", "dropped 1 project suggestion(s)"]
    assert load_config(paths).learn.seed_projects is False
    text = paths.config_file.read_text("utf-8")
    assert "seed_projects = false       # also suggest" in text  # the comment stays
    left = {s.term: s.status for s in load_suggestions(paths)}
    # nothing scanned earlier shows again; what the person decided is remembered (and the
    # words mined from the old history go too: history-gone)
    assert left == {"Better Shell": "dismissed", "Paperclip": "accepted"}
    assert LEARN_PROJECTS_OFF in json.loads(paths.migrations_file.read_text("utf-8"))
    # turned back on later (by hand): it stays on
    paths.config_file.write_text(
        text.replace("seed_projects = false", "seed_projects = true"), "utf-8"
    )
    assert migrate(paths) == []
    assert load_config(paths).learn.seed_projects is True


def test_scanning_is_switched_off_however_it_was_written(tmp_path):
    assert project_scanning_on("[learn]\nseed_projects=true\n")
    assert project_scanning_on("[learn]\n  seed_projects = true  # mine\n")
    assert not project_scanning_on("[learn]\nseed_projects = false\n")
    assert not project_scanning_on("[other]\nseed_projects = true\n")
    paths = home(tmp_path, "[learn]\nseed_projects = true\nproject_roots = ['~/Developer']\n")
    assert migrate(paths) == ["learn.seed_projects = False"]  # (and no old history to delete)
    cfg = load_config(paths)
    assert cfg.learn.seed_projects is False and cfg.learn.project_roots == ["~/Developer"]


def test_new_installs_never_scan_project_folders(tmp_path):
    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    assert migrate(paths) == []
    assert load_config(paths).learn.seed_projects is False
    assert "seed_projects = false" in paths.config_file.read_text("utf-8")
