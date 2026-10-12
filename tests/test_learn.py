"""Vocab suggestions: project folders (only when asked), the suggestions store, Teach words."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fakes import FakeNotifier
from shoutabout import learn, review_ui
from shoutabout.config import Config, LearnConfig, Paths
from shoutabout.hub import refresh_and_notify, teach_notification
from shoutabout.learn import (
    Suggestion,
    accept,
    base_slug,
    dismiss,
    find_projects,
    forget_source,
    git_remote_names,
    load_suggestions,
    match_key,
    package_names,
    pending_suggestions,
    readme_title,
    refresh_suggestions,
    seed_from_projects,
    spoken_form,
)
from shoutabout.vocab import add_term, parse_vocab, read_terms, remove_term

# ------------------------------------------------------------------------- fixtures

VOCAB = ["Better Shell", "Claude Code"]


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "home")
    p.home.mkdir()
    p.vocab_file.write_text("# vocab\n" + "\n".join(VOCAB) + "\n", "utf-8")
    return p


@pytest.fixture
def root(tmp_path) -> Path:
    return make_tree(tmp_path / "Documents")


def by_term(suggestions) -> dict[str, Suggestion]:
    return {s.term: s for s in suggestions}


# ------------------------------------------------------------------ project seeding


@pytest.mark.parametrize(
    ("slug", "spoken"),
    [
        ("chameleon-cv", "Chameleon CV"),
        ("better_shell", "Better Shell"),
        ("ShoutAbout", "ShoutAbout"),
        ("shoutabout", "Shoutabout"),
        ("ios-app", "iOS App"),
        ("my-llm-api", "My LLM API"),
        ("gpu.db.cli", "GPU DB CLI"),
        ("ui ux kit", "UI UX Kit"),
        ("id-sdk", "ID SDK"),
        ("ai_tool", "AI Tool"),
        ("Custom Keyboard Project", "Custom Keyboard Project"),
        ("myCoolApp", "myCoolApp"),
        ("the-NASA-thing", "The NASA Thing"),
    ],
)
def test_spoken_form(slug, spoken):
    assert spoken_form(slug) == spoken


@pytest.mark.parametrize(
    ("name", "base"),
    [
        ("paperclip-src", "paperclip"),
        ("old-portfolio-test", "portfolio"),
        ("app-v2", "app"),
        ("ios-app", "ios-app"),
        ("cash_app_wip", "cash-app"),
        ("plot-twist-beta", "plot-twist"),
        ("chameleon-cv", "chameleon-cv"),
        ("Custom Keyboard Project", "Custom-Keyboard"),
        ("src", "src"),
    ],
)
def test_base_slug(name, base):
    assert base_slug(name) == base


def make_tree(root: Path) -> Path:
    def write(rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")

    write(
        "chameleon-cv/.git/config",
        '[core]\n\tbare = false\n[remote "origin"]\n'
        "\turl = https://github.com/someone/chameleon-cv.git\n"
        '\tfetch = +refs/heads/*:refs/remotes/origin/*\n[branch "main"]\n\tremote = origin\n',
    )
    write("chameleon-cv/README.md", "<p>badge</p>\n\n# Chameleon CV — tailor your CV\n")
    # A git worktree of the same repo: .git is a file pointing at the main repo.
    write("chameleon-cv/.git/worktrees/review/commondir", "../..\n")
    git_dir = (root / "chameleon-cv/.git/worktrees/review").as_posix()
    write("chameleon-cv-review/.git", f"gitdir: {git_dir}\n")
    write("better_shell/pyproject.toml", '[project]\nname = "better-shell"\nversion = "1"\n')
    write("webstuff/package.json", json.dumps({"name": "@acme/rocket-launcher"}))
    write("group/nested-ai-tool/Cargo.toml", '[package]\nname = "nested-ai-tool"\n')
    write("group/deeper/too-deep/package.json", json.dumps({"name": "too-deep"}))
    write("node_modules/somepkg/package.json", json.dumps({"name": "somepkg"}))
    write(".hidden/package.json", json.dumps({"name": "hidden-thing"}))
    (root / "ShoutAbout/.git").mkdir(parents=True)
    write("ShoutAbout/README.md", "# ShoutAbout\n")
    write("ios-app/go.mod", "module github.com/me/ios-app\n\ngo 1.22\n")
    write("src/package.json", "{}")
    write("old-portfolio-test/package.json", json.dumps({"name": "portfolio"}))
    write("Tools/Thing.sln", "")
    write("not-a-project/notes.txt", "hello")
    write("emoji-app/README.md", "# 🚀 Rocket [Docs](https://x) ![b](https://img)\n")
    write("emoji-app/package.json", "{broken json")
    return root


def test_find_projects(tmp_path):
    root = make_tree(tmp_path / "Documents")
    found = {p.relative_to(root).as_posix() for p in find_projects([root])}
    assert found == {
        "chameleon-cv",
        "chameleon-cv-review",
        "better_shell",
        "webstuff",
        "group/nested-ai-tool",
        "ShoutAbout",
        "ios-app",
        "src",
        "old-portfolio-test",
        "Tools",
        "emoji-app",
    }


def test_metadata_readers(tmp_path):
    root = make_tree(tmp_path / "Documents")
    assert git_remote_names(root / "chameleon-cv") == ["chameleon-cv"]
    assert git_remote_names(root / "chameleon-cv-review") == ["chameleon-cv"]  # via commondir
    assert git_remote_names(root / "better_shell") == []
    assert package_names(root / "webstuff") == ["rocket-launcher"]
    assert package_names(root / "better_shell") == ["better-shell"]
    assert package_names(root / "group/nested-ai-tool") == ["nested-ai-tool"]
    assert package_names(root / "ios-app") == ["ios-app"]
    assert package_names(root / "emoji-app") == []
    assert readme_title(root / "chameleon-cv") == "Chameleon CV"
    assert readme_title(root / "emoji-app") == "Rocket Docs"
    assert readme_title(root / "webstuff") is None


def test_seed_from_projects(tmp_path):
    root = make_tree(tmp_path / "Documents")
    found = by_term(seed_from_projects([root]))
    assert {
        "Chameleon CV",
        "Better Shell",
        "Rocket Launcher",
        "Nested AI Tool",
        "ShoutAbout",
        "iOS App",
        "Portfolio",
        "Webstuff",
    } <= set(found)
    for not_wanted in (
        "Too Deep",
        "Somepkg",
        "Hidden Thing",
        "Chameleon CV Review",
        "Src",
        "Tools",
        "Not A Project",
        "Old Portfolio Test",
    ):
        assert not_wanted not in found, not_wanted
    cv = found["Chameleon CV"]
    assert cv.count == 2  # the clone and its worktree
    assert "chameleon-cv" in cv.detail and "+1 more folder" in cv.detail
    assert cv.source == "projects"
    assert found["Better Shell"].detail.endswith("better_shell")
    assert next(iter(found)) == "Chameleon CV"  # most folders first


def test_seed_from_projects_excludes(tmp_path):
    root = make_tree(tmp_path / "Documents")
    found = by_term(seed_from_projects([root], exclude={match_key("better shell")}))
    assert "Better Shell" not in found


def test_seed_missing_root(tmp_path):
    assert seed_from_projects([tmp_path / "nope"]) == []


# ------------------------------------------------------------------------ the store


def test_refresh_merge_semantics(paths, root):
    new = refresh_suggestions(paths, roots=[root])
    pending = by_term(pending_suggestions(paths))
    assert new == len(pending) > 0
    assert "Rocket Launcher" in pending
    assert "Better Shell" not in pending  # already in the vocab
    assert refresh_suggestions(paths, roots=[root]) == 0  # nothing new the second time
    data = json.loads(paths.suggestions_file.read_text("utf-8"))
    assert data["version"] == 1
    assert {s["status"] for s in data["suggestions"]} == {"pending"}

    # Dismissed: never comes back.
    assert dismiss(paths, ["rocket launcher"]) == 1
    assert "Rocket Launcher" not in by_term(pending_suggestions(paths))
    assert refresh_suggestions(paths, roots=[root]) == 0
    assert "Rocket Launcher" not in by_term(pending_suggestions(paths))

    # Accepted: goes to the top of vocab.txt, never re-suggested, even after removal.
    results = accept(paths, ["Chameleon CV", "iOS App"])
    assert [r.status for r in results] == ["added", "added"]
    assert read_terms(paths.vocab_file)[:3] == ["Chameleon CV", "iOS App", "Better Shell"]
    remove_term(paths.vocab_file, "Chameleon CV")
    assert refresh_suggestions(paths, roots=[root]) == 0
    assert "Chameleon CV" not in by_term(pending_suggestions(paths))
    statuses = {s.term: s.status for s in load_suggestions(paths)}
    assert statuses["Rocket Launcher"] == "dismissed"
    assert statuses["Chameleon CV"] == "accepted"


def test_refresh_counts_new_projects(paths, root):
    refresh_suggestions(paths, roots=[root])
    (root / "kestrel-labs" / ".git").mkdir(parents=True)
    assert refresh_suggestions(paths, roots=[root]) == 1
    assert "Kestrel Labs" in by_term(pending_suggestions(paths))


def test_without_projects_nothing_is_looked_at(paths, root, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("project folders were scanned")

    monkeypatch.setattr(learn, "seed_from_projects", boom)
    assert refresh_suggestions(paths, include_projects=False, roots=[root]) == 0
    assert pending_suggestions(paths) == []


def test_term_added_elsewhere_becomes_accepted(paths, root):
    refresh_suggestions(paths, roots=[root])
    add_term(paths.vocab_file, "Webstuff")  # e.g. taught from the phone
    assert "Webstuff" not in by_term(pending_suggestions(paths))
    refresh_suggestions(paths, roots=[root])
    assert {s.term: s.status for s in load_suggestions(paths)}["Webstuff"] == "accepted"


def test_accept_reports_invalid_and_existing(paths):
    results = accept(paths, ["better shell", "#bad", "Fresh Term"])
    assert [r.status for r in results] == ["exists", "invalid", "added"]
    assert parse_vocab(paths.vocab_file.read_text("utf-8"))[0] == "Fresh Term"


def test_corrupt_store_is_replaced(paths, root):
    paths.suggestions_file.write_text("{this is not json", "utf-8")
    assert load_suggestions(paths) == []
    assert refresh_suggestions(paths, roots=[root]) > 0
    assert load_suggestions(paths)


def test_store_tolerates_odd_entries(paths, root):
    paths.suggestions_file.write_text(
        json.dumps(
            {
                "suggestions": [
                    {"term": "Webstuff", "status": "dismissed"},
                    {"term": "Webstuff", "status": "pending"},  # duplicate key: first wins
                    {"nope": 1},
                    "junk",
                    {"term": "Odd", "status": "weird", "count": "x"},
                ]
            }
        ),
        "utf-8",
    )
    stored = {s.term: s for s in load_suggestions(paths)}
    assert stored["Webstuff"].status == "dismissed"
    assert stored["Odd"].status == "pending" and stored["Odd"].count == 0
    refresh_suggestions(paths, roots=[root])
    assert "Webstuff" not in by_term(pending_suggestions(paths))


def test_forget_source_drops_waiting_ones_and_where_they_came_from(paths, root):
    refresh_suggestions(paths, roots=[root])
    dismiss(paths, ["Webstuff"])
    accept(paths, ["iOS App"])
    waiting = len(pending_suggestions(paths))
    assert forget_source(paths, "projects") == waiting
    assert pending_suggestions(paths) == []
    left = {s.term: s for s in load_suggestions(paths)}
    assert left["Webstuff"].status == "dismissed" and left["iOS App"].status == "accepted"
    assert all(s.detail == "" for s in left.values())  # no folder names left behind
    assert forget_source(paths, "projects") == 0
    assert forget_source(Paths(paths.home / "nothing-here"), "projects") == 0


def test_default_project_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert learn.default_project_roots("win32") == []
    (tmp_path / "Documents").mkdir()
    (tmp_path / "source" / "repos").mkdir(parents=True)
    found = learn.default_project_roots("win32")
    assert found == [tmp_path / "Documents", tmp_path / "source/repos"]
    # A Mac never looks in Documents (macOS would ask the person out of the blue).
    assert learn.default_project_roots("darwin") == [tmp_path / "source/repos"]
    (tmp_path / "Developer").mkdir()
    found = learn.default_project_roots("darwin")
    assert found == [tmp_path / "Developer", tmp_path / "source/repos"]


# ------------------------------------------------------------------- hub job


def test_refresh_and_notify_once(paths, root):
    notes = FakeNotifier()
    cfg = LearnConfig(seed_projects=True, project_roots=[str(root)])
    n = refresh_and_notify(paths, cfg, notes.notify)
    assert n > 0
    assert notes.messages == [teach_notification(n)]
    title, message = notes.messages[0]
    assert title == "Teach words"  # the tray / menu bar item's name: where to look
    assert message == f"{n} new words to teach."
    assert refresh_and_notify(paths, cfg, notes.notify) == 0
    assert len(notes.messages) == 1  # nothing new, no second notification


def test_refresh_and_notify_respects_notify_false(paths, root):
    notes = FakeNotifier()
    cfg = LearnConfig(seed_projects=True, project_roots=[str(root)], notify=False)
    assert refresh_and_notify(paths, cfg, notes.notify)
    assert notes.messages == []


def test_the_hub_never_scans_by_default(paths, root, monkeypatch):
    """[learn] seed_projects is off: the hub's refresh never looks at the folders."""

    def boom(*_a, **_k):
        raise AssertionError("project folders were scanned")

    monkeypatch.setattr(learn, "seed_from_projects", boom)
    notes = FakeNotifier()
    cfg = LearnConfig(project_roots=[str(root)])
    assert refresh_and_notify(paths, cfg, notes.notify) == 0
    assert pending_suggestions(paths) == [] and notes.messages == []


def test_teach_notification_singular():
    assert teach_notification(1)[1].startswith("1 new word to teach")


def test_learn_config_defaults():
    cfg = Config().learn
    assert (cfg.enabled, cfg.seed_projects, cfg.notify, cfg.project_roots) == (
        True,
        False,
        True,
        [],
    )


# ------------------------------------------------------------------- Teach words


def test_review_ui_import_is_headless():
    code = "import sys, shoutabout.review_ui; print('tkinter' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_suggest_from_my_project_folders(paths, root):
    """The button: one look, at [learn] project_roots (else the usual folders)."""
    paths.config_file.write_text(f"[learn]\nproject_roots = [{json.dumps(str(root))}]\n")
    new = review_ui.suggest_from_projects(paths)
    assert new > 0 and {s.source for s in pending_suggestions(paths)} == {"projects"}
    assert review_ui.scan_message(new).startswith(f"Found {new} new word")
    assert review_ui.suggest_from_projects(paths) == 0
    assert review_ui.scan_message(0) == "Nothing new in your project folders."
    assert review_ui.scan_message(1) == "Found 1 new word."


def test_suggest_uses_the_usual_folders_without_a_setting(paths, root, monkeypatch):
    monkeypatch.setattr(learn, "default_project_roots", lambda platform=None: [root])
    assert review_ui.suggest_from_projects(paths) > 0


def test_group_suggestions():
    s = [
        Suggestion("P1", "projects", 1, "", ""),
        Suggestion("H1", "history", 2, "", ""),
        Suggestion("M1", "manual", 0, "", ""),
        Suggestion("P2", "projects", 1, "", ""),
    ]
    groups = review_ui.group_suggestions(s)
    assert [(title, [x.term for x in items]) for title, items in groups] == [
        ("From your projects", ["P1", "P2"]),
        ("From your dictations", ["H1"]),
        ("Manual", ["M1"]),
    ]
    assert review_ui.group_suggestions([]) == []


def test_messages():
    assert "first in line" in review_ui.add_message("added", "GitHub")
    assert "already" in review_ui.add_message("exists", "GitHub")
    assert "too long" in review_ui.add_message("invalid", "x", "too long")

    class R:
        def __init__(self, status, reason=None):
            self.status, self.reason = status, reason

    msg = review_ui.accept_message([R("added"), R("added"), R("exists"), R("invalid", "bad")])
    assert msg == "Added 2 words, 1 already in your vocab, 1 not valid (bad)."
    assert review_ui.accept_message([]) == "Nothing to add."


def _pump(root, seconds: float) -> None:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


@pytest.mark.windows
def test_teach_page_adds_scans_and_removes(paths, root, monkeypatch):
    tk = pytest.importorskip("tkinter")
    monkeypatch.setattr(learn, "default_project_roots", lambda platform=None: [root])
    window = tk.Tk()
    window.withdraw()
    try:
        page = review_ui.TeachPage(tk.Frame(window), paths)
        _pump(window, 0.4)
        assert page.vocab_terms == VOCAB and page.suggestion_vars == []
        page.entry.insert(0, "Priya Raghunathan")
        page.add_word()
        _pump(window, 0.4)
        assert read_terms(paths.vocab_file)[0] == "Priya Raghunathan"
        assert page.vocab_terms[0] == "Priya Raghunathan"
        assert page.scan_button["text"] == review_ui.SCAN_LABEL
        page.scan_projects()  # the only thing that looks at the folders
        _pump(window, 0.8)
        assert page.status["text"].startswith("Found ")
        terms = [s.term for _var, s in page.suggestion_vars]
        assert "Rocket Launcher" in terms
        var = next(v for v, s in page.suggestion_vars if s.term == "Rocket Launcher")
        var.set(1)
        page.accept_selected()
        _pump(window, 0.4)
        assert read_terms(paths.vocab_file)[0] == "Rocket Launcher"
        page.vocab_list.selection_set(0)
        page.remove_selected()
        _pump(window, 0.4)
        assert "Rocket Launcher" not in read_terms(paths.vocab_file)
        page.close()
        assert page.closed
    finally:
        window.destroy()
