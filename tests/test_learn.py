"""Vocab suggestions: history mining, project seeding, the suggestions store, the Teach window."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
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
    git_remote_names,
    load_suggestions,
    match_key,
    mine_history,
    package_names,
    pending_suggestions,
    readme_title,
    refresh_suggestions,
    seed_from_projects,
    spoken_form,
)
from shoutabout.vocab import add_term, parse_vocab, read_terms, remove_term

# ------------------------------------------------------------------------- fixtures


def record(text: str, raw: str | None = None, *, ts: str, cleanup: str = "applied") -> dict:
    return {
        "ts": ts,
        "id": "abc",
        "route": "lan",
        "target": "pc",
        "raw_text": text if raw is None else raw,
        "text": text,
        "cleanup": cleanup,
    }


HISTORY = [
    record(
        "I pushed the branch to GitHub and asked Claude Code to review it.",
        ts="2026-10-01T09:00:00-07:00",
    ),
    record("Then Chameleon CV needs a new release on GitHub.", ts="2026-10-02T09:00:00-07:00"),
    record("Tell Priya Raghunathan about Chameleon CV on Monday.", ts="2026-10-03T09:00:00-07:00"),
    record(
        "Yesterday Priya Raghunathan said the iOS build is fine. OK so the iOS app works.",
        ts="2026-10-04T09:00:00-07:00",
    ),
    # Only ever capitalised because it starts a sentence: not a suggestion.
    record("Zorblax shipped it. Zorblax is great. Zorblax again!", ts="2026-10-05T09:00:00-07:00"),
    # Stoplist: months, I, OK, days.
    record("We met in March and I said OK in March on Friday.", ts="2026-10-05T10:00:00-07:00"),
    # (a) the LLM cleanup re-cased a name (plus the usual sentence-start fix).
    record(
        "The ShoutAbout hub runs on my PC.",
        raw="the shoutabout hub runs on my pc",
        ts="2026-09-30T08:00:00-07:00",
    ),
    # (c) letters+digits, also at a sentence start.
    record("Compare GPT5 with the old model. GPT5 is faster.", ts="2026-10-06T09:00:00-07:00"),
    # An everyday word that is mostly lowercase.
    record(
        "we follow the Plan daily, the Plan works, we plan to plan and plan again",
        ts="2026-10-06T10:00:00-07:00",
    ),
    # Cleanup fell back: its text is the raw text, nothing to learn from the diff.
    record(
        "the zentrix thing",
        raw="the zentrix thing",
        ts="2026-10-06T11:00:00-07:00",
        cleanup="fallback",
    ),
    # A 4+ word capitalised run is a title, not a term.
    record(
        "I read The Very Hungry Caterpillar Book and The Very Hungry Caterpillar Book.",
        ts="2026-10-06T12:00:00-07:00",
    ),
]

VOCAB = ["Better Shell", "Claude Code"]


def write_history(path: Path, records=HISTORY, junk: bool = True) -> Path:
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    if junk:
        lines[2:2] = ["{not json", "", "[1, 2]"]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "home")
    p.home.mkdir()
    p.vocab_file.write_text("# vocab\n" + "\n".join(VOCAB) + "\n", "utf-8")
    write_history(p.history_file)
    return p


def by_term(suggestions) -> dict[str, Suggestion]:
    return {s.term: s for s in suggestions}


# -------------------------------------------------------------------- history mining


def test_mine_history_finds_the_right_words(tmp_path):
    found = by_term(mine_history(write_history(tmp_path / "h.jsonl"), VOCAB))
    assert {"GitHub", "Chameleon CV", "Priya Raghunathan", "iOS", "ShoutAbout", "GPT5"} <= set(
        found
    )
    for not_wanted in (
        "Claude Code",  # already in vocab
        "Zorblax",  # only at sentence starts
        "March",  # stoplist (month)
        "Friday",
        "Monday",
        "OK",
        "I",
        "PC",
        "Then",
        "The",
        "Plan",  # mostly lowercase "plan"
        "Zentrix",
        "The Very Hungry Caterpillar Book",
        "Very Hungry Caterpillar Book",
    ):
        assert not_wanted not in found, not_wanted
    assert all(s.source == "history" and s.status == "pending" for s in found.values())


def test_mine_history_counts_details_and_first_seen(tmp_path):
    found = by_term(mine_history(write_history(tmp_path / "h.jsonl"), VOCAB))
    assert found["GitHub"].count == 2
    assert found["GitHub"].detail == "seen 2× in your dictations"
    assert found["Chameleon CV"].count == 2
    assert found["Chameleon CV"].first_seen == "2026-10-02T09:00:00-07:00"
    assert found["iOS"].count == 2
    assert found["GPT5"].count == 2
    shout = found["ShoutAbout"]
    assert shout.count == 1
    assert shout.detail == 'cleanup fixed it 1× (Whisper heard "shoutabout")'


def test_mine_history_vocab_and_exclusions_are_case_insensitive(tmp_path):
    path = write_history(tmp_path / "h.jsonl")
    found = by_term(
        mine_history(path, ["github", "chameleon-cv"], exclude={match_key("Priya Raghunathan")})
    )
    assert "GitHub" not in found
    assert "Chameleon CV" not in found
    assert "Priya Raghunathan" not in found
    assert "iOS" in found


def test_mine_history_single_mention_is_not_enough(tmp_path):
    path = write_history(
        tmp_path / "h.jsonl",
        [record("Ask Nadia about the Kestrel launch.", ts="2026-10-01T00:00:00Z")],
    )
    assert mine_history(path, []) == []


def test_mine_history_phrase_at_sentence_start_is_not_split(tmp_path):
    # "Claude Code is ready": the capital C of "Claude" says nothing, and "Code" must not
    # become a suggestion of its own.
    path = write_history(
        tmp_path / "h.jsonl",
        [
            record("Claude Code is ready. Claude Code works.", ts="2026-10-01T00:00:00Z"),
            record("Claude Code again.", ts="2026-10-01T00:00:01Z"),
        ],
    )
    assert mine_history(path, []) == []


def test_sentence_initial_verb_before_a_name(tmp_path):
    # "Ask Nadia ..." at a sentence start: the tail "Nadia" counts once Nadia has also been
    # seen mid-sentence; without that confirmation it doesn't.
    ask = [record("Ask Nadia about the launch.", ts=f"2026-10-0{i}T00:00:00Z") for i in (1, 2)]
    path = write_history(tmp_path / "h.jsonl", ask)
    assert mine_history(path, []) == []
    path = write_history(
        tmp_path / "h2.jsonl", [*ask, record("I told Nadia already.", ts="2026-10-03T00:00:00Z")]
    )
    found = by_term(mine_history(path, []))
    assert list(found) == ["Nadia"]
    assert found["Nadia"].count == 3
    assert found["Nadia"].first_seen == "2026-10-01T00:00:00Z"


def test_mine_history_missing_or_empty_file(tmp_path):
    assert mine_history(tmp_path / "nope.jsonl", []) == []
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", "utf-8")
    assert mine_history(empty, []) == []


def test_cleanup_fix_of_a_phrase(tmp_path):
    path = write_history(
        tmp_path / "h.jsonl",
        [record("Ship the Kestrel Launch today.", raw="ship the kestrel lunch today", ts="t")],
    )
    found = by_term(mine_history(path, []))
    assert list(found) == ["Kestrel Launch"]
    assert 'heard "kestrel lunch"' in found["Kestrel Launch"].detail


def test_tokens_sentence_starts():
    toks = learn._tokens('Hello there. "Quoted" word, GitHub\'s repo! next')
    assert [(t.text, t.start, t.brk) for t in toks] == [
        ("Hello", True, False),
        ("there", False, True),
        ("Quoted", True, True),
        ("word", False, True),
        ("GitHub", False, False),
        ("repo", False, True),
        ("next", True, False),
    ]


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


def test_refresh_merge_semantics(paths):
    new = refresh_suggestions(paths)
    pending = by_term(pending_suggestions(paths))
    assert new == len(pending) > 0
    assert "GitHub" in pending
    assert refresh_suggestions(paths) == 0  # nothing new the second time
    data = json.loads(paths.suggestions_file.read_text("utf-8"))
    assert data["version"] == 1
    assert {s["status"] for s in data["suggestions"]} == {"pending"}

    # Dismissed: never comes back.
    assert dismiss(paths, ["github"]) == 1
    assert "GitHub" not in by_term(pending_suggestions(paths))
    assert refresh_suggestions(paths) == 0
    assert "GitHub" not in by_term(pending_suggestions(paths))

    # Accepted: goes to the top of vocab.txt, never re-suggested, even after removal.
    results = accept(paths, ["Chameleon CV", "iOS"])
    assert [r.status for r in results] == ["added", "added"]
    assert read_terms(paths.vocab_file)[:3] == ["Chameleon CV", "iOS", "Better Shell"]
    remove_term(paths.vocab_file, "Chameleon CV")
    assert refresh_suggestions(paths) == 0
    assert "Chameleon CV" not in by_term(pending_suggestions(paths))
    statuses = {s.term: s.status for s in load_suggestions(paths)}
    assert statuses["GitHub"] == "dismissed"
    assert statuses["Chameleon CV"] == "accepted"


def test_refresh_counts_new_history(paths):
    refresh_suggestions(paths)
    with paths.history_file.open("a", encoding="utf-8") as fh:
        for i in range(2):
            fh.write(json.dumps(record("We shipped Kestrel Labs today.", ts=f"2026-10-09T0{i}")))
            fh.write("\n")
    assert refresh_suggestions(paths) == 1
    assert "Kestrel Labs" in by_term(pending_suggestions(paths))


def test_term_added_elsewhere_becomes_accepted(paths):
    refresh_suggestions(paths)
    add_term(paths.vocab_file, "GPT5")  # e.g. taught from the phone
    assert "GPT5" not in by_term(pending_suggestions(paths))
    refresh_suggestions(paths)
    assert {s.term: s.status for s in load_suggestions(paths)}["GPT5"] == "accepted"


def test_accept_reports_invalid_and_existing(paths):
    results = accept(paths, ["better shell", "#bad", "Fresh Term"])
    assert [r.status for r in results] == ["exists", "invalid", "added"]
    assert parse_vocab(paths.vocab_file.read_text("utf-8"))[0] == "Fresh Term"


def test_refresh_with_projects(paths, tmp_path):
    root = make_tree(tmp_path / "Documents")
    new = refresh_suggestions(paths, include_projects=True, roots=[root])
    pending = pending_suggestions(paths)
    sources = [s.source for s in pending]
    assert sources == sorted(sources, key=["history", "projects"].index)  # history first
    terms = by_term(pending)
    assert terms["Chameleon CV"].source == "history"  # found in both: first source wins
    assert terms["Rocket Launcher"].source == "projects"
    assert "Better Shell" not in terms  # in vocab
    assert new == len(pending)


def test_projects_only_refresh(paths, tmp_path):
    root = make_tree(tmp_path / "Documents")
    refresh_suggestions(paths, include_projects=True, include_history=False, roots=[root])
    assert {s.source for s in pending_suggestions(paths)} == {"projects"}


def test_corrupt_store_is_replaced(paths):
    paths.suggestions_file.write_text("{this is not json", "utf-8")
    assert load_suggestions(paths) == []
    assert refresh_suggestions(paths) > 0
    assert load_suggestions(paths)


def test_store_tolerates_odd_entries(paths):
    paths.suggestions_file.write_text(
        json.dumps(
            {
                "suggestions": [
                    {"term": "GitHub", "status": "dismissed"},
                    {"term": "GitHub", "status": "pending"},  # duplicate key: first wins
                    {"nope": 1},
                    "junk",
                    {"term": "Odd", "status": "weird", "count": "x"},
                ]
            }
        ),
        "utf-8",
    )
    stored = {s.term: s for s in load_suggestions(paths)}
    assert stored["GitHub"].status == "dismissed"
    assert stored["Odd"].status == "pending" and stored["Odd"].count == 0
    refresh_suggestions(paths)
    assert "GitHub" not in by_term(pending_suggestions(paths))


# ------------------------------------------------------------------- hub job


def test_refresh_and_notify_once(paths):
    notes = FakeNotifier()
    cfg = LearnConfig(seed_projects=False)
    n = refresh_and_notify(paths, cfg, notes.notify)
    assert n > 0
    assert notes.messages == [teach_notification(n)]
    title, message = notes.messages[0]
    assert title == "Teach ShoutAbout"
    assert message.startswith(f"{n} new words to teach")
    assert "Teach ShoutAbout…" in message
    assert refresh_and_notify(paths, cfg, notes.notify) == 0
    assert len(notes.messages) == 1  # nothing new, no second notification


def test_refresh_and_notify_respects_notify_false(paths):
    notes = FakeNotifier()
    assert refresh_and_notify(paths, LearnConfig(seed_projects=False, notify=False), notes.notify)
    assert notes.messages == []


def test_history_is_mined_only_while_history_is_on(paths, tmp_path):
    """[history] enabled = false (the default): the hub's refresh and the Teach window's
    Rescan leave history.jsonl alone, even if one is left from when history was on."""
    notes = FakeNotifier()
    learn_cfg = LearnConfig(seed_projects=False)
    assert refresh_and_notify(paths, learn_cfg, notes.notify, include_history=False) == 0
    assert pending_suggestions(paths) == [] and notes.messages == []
    # The Teach window's Rescan reads config.toml; [history] isn't there: off by default.
    paths.config_file.write_text("[learn]\nseed_projects = false\n", "utf-8")
    assert review_ui.rescan_suggestions(paths) == 0
    assert pending_suggestions(paths) == []
    root = make_tree(tmp_path / "Documents")
    paths.config_file.write_text(f"[learn]\nproject_roots = [{json.dumps(str(root))}]\n", "utf-8")
    assert review_ui.rescan_suggestions(paths) > 0
    assert {s.source for s in pending_suggestions(paths)} == {"projects"}
    # Turned on: dictations are scanned too.
    paths.config_file.write_text(
        f"[history]\nenabled = true\n\n[learn]\nproject_roots = [{json.dumps(str(root))}]\n",
        "utf-8",
    )
    assert review_ui.rescan_suggestions(paths) > 0
    assert "history" in {s.source for s in pending_suggestions(paths)}


def test_teach_notification_singular():
    assert teach_notification(1)[1].startswith("1 new word to teach")


def test_learn_config_defaults():
    cfg = Config().learn
    assert (cfg.enabled, cfg.seed_projects, cfg.notify, cfg.project_roots) == (
        True,
        True,
        True,
        [],
    )


# --------------------------------------------------------------------- review UI


def test_review_ui_import_is_headless():
    code = "import sys, shoutabout.review_ui; print('tkinter' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_pick_font():
    available = ["Segoe UI", "Arial", "archivo"]
    assert review_ui.pick_font(available, review_ui.TITLE_FONTS) == "Segoe UI"
    assert review_ui.pick_font(available, review_ui.BODY_FONTS) == "archivo"
    assert review_ui.pick_font(["Anton", "Segoe UI"], review_ui.TITLE_FONTS) == "Anton"
    assert review_ui.pick_font([], review_ui.BODY_FONTS) == "TkDefaultFont"


def test_group_suggestions():
    s = [
        Suggestion("P1", "projects", 1, "", ""),
        Suggestion("H1", "history", 2, "", ""),
        Suggestion("M1", "manual", 0, "", ""),
        Suggestion("H2", "history", 1, "", ""),
    ]
    groups = review_ui.group_suggestions(s)
    assert [(title, [x.term for x in items]) for title, items in groups] == [
        ("From your dictations", ["H1", "H2"]),
        ("From your projects", ["P1"]),
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


def test_one_window_at_a_time(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    runs: list[tuple] = []

    def fake_run(paths, mode, handle):
        runs.append((mode, handle))
        started.set()
        release.wait(5)

    monkeypatch.setattr(review_ui, "_run_tk", fake_run)
    paths = Paths(tmp_path)
    t1 = review_ui.open_add_word_dialog(paths)
    assert started.wait(5)
    assert review_ui.is_open()
    t2 = review_ui.open_review_window(paths)  # brings the open one forward instead
    assert t2 is t1
    assert len(runs) == 1
    assert runs[0][1].requests.get_nowait() == ("focus", "review")
    release.set()
    t1.join(5)
    assert not review_ui.is_open()
    t3 = review_ui.open_review_window(paths)  # a new one once the first has closed
    t3.join(5)
    assert t3 is not t1 and len(runs) == 2


def test_hub_exposes_tray_callables(monkeypatch, tmp_path):
    from shoutabout import hub

    calls = []
    monkeypatch.setattr(review_ui, "open_review_window", lambda p: calls.append(("review", p)))
    monkeypatch.setattr(review_ui, "open_add_word_dialog", lambda p: calls.append(("add", p)))
    paths = Paths(tmp_path)
    hub.open_teach_window(paths)
    hub.open_add_word(paths)
    assert calls == [("review", paths), ("add", paths)]


@pytest.mark.windows
def test_real_window_opens_and_closes(paths):
    t = review_ui.open_review_window(paths)
    t.join(2.0)  # still running: the window is up
    assert t.is_alive() and review_ui.is_open()
    assert review_ui.open_review_window(paths) is t
    review_ui.close_window(5.0)
    assert not t.is_alive()
    t = review_ui.open_add_word_dialog(paths)
    t.join(1.5)
    review_ui.close_window(5.0)
    assert not t.is_alive()
