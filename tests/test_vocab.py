from __future__ import annotations

import codecs
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from fakes import FakeEngine
from shoutabout.engine.base import SttHints
from shoutabout.vocab import (
    HINT_PREFIX,
    FileLockTimeout,
    PromptBuild,
    Vocab,
    VocabStore,
    VocabTermError,
    add_term,
    add_terms,
    build_stt_hints,
    file_lock,
    format_hint_text,
    normalize_term,
    parse_vocab,
    remove_term,
    split_hint_text,
)

count = FakeEngine().count_tokens
TERMS = ["Better Shell", "Claude Code", "Claude", "Anthropic", "GitHub", "Cloudflare", "ShoutAbout"]


class CountingCounter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text: str) -> int:
        self.calls += 1
        return count(text)


# ------------------------------------------------------------------------ parsing


def test_parse_vocab_basics():
    text = (
        "﻿# header comment\n"
        "Better Shell\n"
        "\n"
        "   \t\n"
        "  # indented comment\n"
        "C#\n"
        "  Claude   Code  \n"
        "better shell\n"
        "CLAUDE CODE\n"
        "F# and C++\n"
        "GitHub\r\n"
        "Pull\trequest\n"
    )
    assert parse_vocab(text) == [
        "Better Shell",
        "C#",
        "Claude Code",
        "F# and C++",
        "GitHub",
        "Pull request",
    ]


def test_parse_vocab_empty():
    assert parse_vocab("") == []
    assert parse_vocab("# only comments\n\n#\n") == []


def test_format_and_split_round_trip():
    text = format_hint_text(TERMS)
    assert text.startswith(HINT_PREFIX) and text.endswith(".")
    assert "Better Shell, Claude Code" in text
    label, terms = split_hint_text(text)
    assert label == HINT_PREFIX.strip()
    assert terms == TERMS
    assert format_hint_text([]) == ""
    # Plain comma lists (e.g. FakeVocabStore) are understood too.
    assert split_hint_text("Better Shell, Claude Code") == ("", ["Better Shell", "Claude Code"])


# -------------------------------------------------------------------------- hints


@pytest.mark.parametrize("mode", ["both", "hotwords", "initial_prompt", "none"])
def test_empty_terms_give_empty_hints(mode):
    build = build_stt_hints([], count, 200, mode)
    assert build == PromptBuild(SttHints(), (), (), 0)


def test_mode_none_ignores_terms():
    build = build_stt_hints(TERMS, count, 200, "none")
    assert build.hints == SttHints()
    assert build.included == () and build.dropped == () and build.tokens == 0


def test_unknown_mode():
    with pytest.raises(ValueError):
        build_stt_hints(TERMS, count, 200, "everything")


def test_hotwords_mode():
    build = build_stt_hints(TERMS, count, 200, "hotwords")
    assert build.hints.hotwords == format_hint_text(TERMS)
    assert build.hints.initial_prompt is None
    assert build.included == tuple(TERMS) and build.dropped == ()
    assert build.tokens == count(build.hints.hotwords)


def test_initial_prompt_mode():
    build = build_stt_hints(TERMS, count, 200, "initial_prompt")
    assert build.hints.initial_prompt == format_hint_text(TERMS)
    assert build.hints.hotwords is None
    assert build.tokens == count(build.hints.initial_prompt)


def test_both_mode_with_room_duplicates_everything():
    build = build_stt_hints(TERMS, count, 200, "both")
    assert build.hints.hotwords == build.hints.initial_prompt == format_hint_text(TERMS)
    assert build.included == tuple(TERMS) and build.dropped == ()
    assert build.tokens == 2 * count(format_hint_text(TERMS)) <= 200


@pytest.mark.parametrize("mode", ["hotwords", "initial_prompt"])
def test_budget_keeps_priority_order(mode):
    full = count(format_hint_text(TERMS))
    budget = count(format_hint_text(TERMS[:4]))
    assert budget < full
    build = build_stt_hints(TERMS, count, budget, mode)
    assert build.included == tuple(TERMS[:4])
    assert build.dropped == tuple(TERMS[4:])
    assert build.tokens <= budget


def test_budget_is_strict_priority():
    terms = ["Short", "An extremely long vocabulary phrase that will not fit at all", "Tiny"]
    budget = count(format_hint_text(["Short", "Tiny"]))
    build = build_stt_hints(terms, count, budget, "hotwords")
    assert build.included == ("Short",)
    assert build.dropped == tuple(terms[1:])  # "Tiny" never jumps the queue


def test_first_term_too_long():
    build = build_stt_hints(["word " * 50], count, 10, "hotwords")
    assert build.hints == SttHints()
    assert build.included == () and len(build.dropped) == 1 and build.tokens == 0


def test_both_mode_budget_total_and_hotwords_first():
    many = [f"Term{i:02d} Name" for i in range(40)]
    budget = 100
    build = build_stt_hints(many, count, budget, "both")
    hot_only = build_stt_hints(many, count, budget, "hotwords")
    # hotwords gets every term that fits on its own...
    assert build.hints.hotwords == hot_only.hints.hotwords
    assert build.included == hot_only.included and build.dropped == hot_only.dropped
    # ...the initial prompt repeats the top terms in what's left; the total fits the budget.
    hot_tokens = count(build.hints.hotwords)
    ip = build.hints.initial_prompt
    assert build.tokens == hot_tokens + (count(ip) if ip else 0) <= budget
    if ip:
        _, ip_terms = split_hint_text(ip)
        assert ip_terms == many[: len(ip_terms)]


def test_both_mode_without_room_for_initial_prompt():
    many = [f"Term{i:02d}" for i in range(100)]
    build = build_stt_hints(many, count, 60, "both")
    assert build.hints.hotwords is not None
    assert build.tokens <= 60
    assert build.dropped


# -------------------------------------------------------------------------- store


def bump_mtime(path: Path, seconds: int = 10) -> None:
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def test_missing_file_is_empty(tmp_path):
    store = VocabStore(tmp_path / "vocab.txt")
    v = store.get()
    assert v.terms == ()
    assert store.get() is v  # no reload while still missing


def test_load_and_hot_reload(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("# c\nBetter Shell\nClaude Code\n", "utf-8")
    store = VocabStore(path)
    v1 = store.get()
    assert v1.terms == ("Better Shell", "Claude Code")
    assert store.get() is v1  # unchanged file -> cached, same version

    path.write_text("Zorblax Industries\nBetter Shell\n", "utf-8")
    bump_mtime(path)
    v2 = store.get()
    assert v2.terms == ("Zorblax Industries", "Better Shell")
    assert v2.version == v1.version + 1


def test_reload_same_size_new_mtime(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("AAAA\n", "utf-8")
    store = VocabStore(path)
    v1 = store.get()
    path.write_text("BBBB\n", "utf-8")
    bump_mtime(path, 5)
    v2 = store.get()
    assert v2.terms == ("BBBB",) and v2.version > v1.version


def test_file_deleted_then_recreated(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("One\n", "utf-8")
    store = VocabStore(path)
    v1 = store.get()
    path.unlink()
    v2 = store.get()
    assert v2.terms == () and v2.version == v1.version + 1
    path.write_text("Two\n", "utf-8")
    v3 = store.get()
    assert v3.terms == ("Two",) and v3.version == v2.version + 1


def test_read_error_keeps_last_good_vocab(tmp_path, monkeypatch, caplog):
    path = tmp_path / "vocab.txt"
    path.write_text("Good Term\n", "utf-8")
    store = VocabStore(path)
    v1 = store.get()

    path.write_text("New Term\n", "utf-8")
    bump_mtime(path)

    def locked(self):
        raise PermissionError("file is locked by another process")

    monkeypatch.setattr(Path, "read_bytes", locked)
    with caplog.at_level(logging.WARNING, logger="shoutabout.vocab"):
        assert store.get() is v1
        assert store.get() is v1
    assert sum("cannot read" in r.getMessage() for r in caplog.records) == 1

    monkeypatch.undo()
    v2 = store.get()  # retried once the file is readable again
    assert v2.terms == ("New Term",) and v2.version == v1.version + 1


def test_non_utf8_encodings(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_bytes(codecs.BOM_UTF16_LE + "Café Olé\nNaïve\n".encode("utf-16-le"))
    assert VocabStore(path).get().terms == ("Café Olé", "Naïve")
    path2 = tmp_path / "ansi.txt"
    path2.write_bytes("Café\n".encode("cp1252"))
    assert VocabStore(path2).get().terms == ("Café",)


def test_stt_hints_cached_per_version_and_args(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("\n".join(TERMS), "utf-8")
    store = VocabStore(path)
    counter = CountingCounter()

    b1 = store.stt_hints(counter, 200, "both")
    calls = counter.calls
    assert calls > 0
    assert store.stt_hints(counter, 200, "both") is b1
    assert counter.calls == calls  # cached

    b2 = store.stt_hints(counter, 200, "hotwords")
    assert b2 is not b1 and b2.hints.initial_prompt is None

    path.write_text("Only Term\n", "utf-8")
    bump_mtime(path)
    b3 = store.stt_hints(counter, 200, "hotwords")
    assert b3.included == ("Only Term",)


def test_dropped_terms_warn_once_per_reload(tmp_path, caplog):
    path = tmp_path / "vocab.txt"
    many = [f"Term number {i}" for i in range(60)]
    path.write_text("\n".join(many), "utf-8")
    store = VocabStore(path)

    with caplog.at_level(logging.WARNING, logger="shoutabout.vocab"):
        b = store.stt_hints(count, 50, "hotwords")
        store.stt_hints(count, 50, "hotwords")
        store.stt_hints(count, 50, "both")  # different args, same version: still one warning
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert b.dropped
    assert f"{len(b.dropped)} of 60" in warnings[0]
    assert b.dropped[0] in warnings[0]

    caplog.clear()
    path.write_text("\n".join(many[:59]), "utf-8")
    bump_mtime(path)
    with caplog.at_level(logging.WARNING, logger="shoutabout.vocab"):
        store.stt_hints(count, 50, "hotwords")
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_no_warning_when_everything_fits(tmp_path, caplog):
    path = tmp_path / "vocab.txt"
    path.write_text("\n".join(TERMS), "utf-8")
    with caplog.at_level(logging.WARNING, logger="shoutabout.vocab"):
        VocabStore(path).stt_hints(count, 200, "both")
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_concurrent_get_during_rewrites(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("Start\n", "utf-8")
    store = VocabStore(path)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                v = store.get()
                assert isinstance(v, Vocab)
                store.stt_hints(count, 100, "both")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(30):
            path.write_text(f"Term {i}\nOther {i}\n", "utf-8")
            bump_mtime(path, i + 1)
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert not errors
    assert store.get().terms == ("Term 29", "Other 29")


# ---------------------------------------------------------------------- teaching

HEADER = "# ShoutAbout vocabulary\n# order = priority\n\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Chameleon CV", "Chameleon CV"),
        ("  “Chameleon   CV.”  ", "Chameleon CV"),
        ('"iOS"', "iOS"),
        ("'GitHub',", "GitHub"),
        ("(ShoutAbout)", "ShoutAbout"),
        ("Node.js.", "Node.js"),
        ("C#", "C#"),
        ("C++", "C++"),
        (".NET", ".NET"),
        ("F# and C++", "F# and C++"),
        ("GPT5", "GPT5"),
        ("RTX 4080", "RTX 4080"),
        ("Café Olé!", "Café Olé"),
        ("Pull\trequest", "Pull request"),
        ("a" * 64, "a" * 64),
        ("one two three four five six", "one two three four five six"),
    ],
)
def test_normalize_term(raw, expected):
    assert normalize_term(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "no term"),
        ("   ", "no term"),
        ("1234", "letter"),
        ("...", "letter"),
        ("!!!", "letter"),
        ("a" * 65, "too long"),
        ("one two three four five six seven", "too many words"),
        ("first\nsecond", "line breaks"),
        ("first\r\nsecond", "line breaks"),
        ("bad\x1bterm", "control"),
        ("nul\x00x", "control"),
        ("#hashtag", "comment"),
        ("  # spaced", "comment"),
    ],
)
def test_normalize_term_rejects(raw, reason):
    with pytest.raises(VocabTermError, match=reason):
        normalize_term(raw)


def test_add_term_goes_to_top_after_comments(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text(HEADER + "Better Shell\n# section\nClaude Code\n", "utf-8")
    r = add_term(path, "Chameleon CV")
    assert (r.added, r.term, r.reason, r.count, r.status) == (
        True,
        "Chameleon CV",
        None,
        3,
        "added",
    )
    assert path.read_text("utf-8") == (
        HEADER + "Chameleon CV\nBetter Shell\n# section\nClaude Code\n"
    )
    add_term(path, "GitHub")  # the newest teaching wins priority
    assert parse_vocab(path.read_text("utf-8")) == [
        "GitHub",
        "Chameleon CV",
        "Better Shell",
        "Claude Code",
    ]


def test_add_term_dedupes_case_insensitively(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("Better Shell\nGitHub\n", "utf-8")
    before = path.read_bytes()
    r = add_term(path, "  github. ")
    assert (r.added, r.term, r.reason, r.status, r.count) == (
        False,
        "GitHub",
        "already in vocab",
        "exists",
        2,
    )
    assert path.read_bytes() == before  # untouched: an existing term keeps its place


def test_add_term_invalid_raises_and_leaves_file(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("One\n", "utf-8")
    with pytest.raises(VocabTermError):
        add_term(path, "#comment")
    assert path.read_text("utf-8") == "One\n"


def test_add_terms_batch_keeps_order_and_reports_each(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("# c\nOld\n", "utf-8")
    results = add_terms(path, ["Alpha", "old", "1234", "Beta", "alpha"])
    assert [r.status for r in results] == ["added", "exists", "invalid", "added", "exists"]
    assert results[2].reason == "needs at least one letter"
    assert all(r.count == 3 for r in results)
    assert path.read_text("utf-8") == "# c\nAlpha\nBeta\nOld\n"


def test_add_term_preserves_crlf_bom_and_comments(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_bytes(codecs.BOM_UTF8 + b"# head\r\n\r\nOne\r\n  # note: C# is fine\r\nTwo")
    add_term(path, "New Term")
    assert path.read_bytes() == (
        codecs.BOM_UTF8 + b"# head\r\n\r\nNew Term\r\nOne\r\n  # note: C# is fine\r\nTwo"
    )


def test_add_term_utf16_file_stays_utf16(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_bytes(codecs.BOM_UTF16_LE + "Café\r\n".encode("utf-16-le"))
    add_term(path, "Naïve")
    data = path.read_bytes()
    assert data.startswith(codecs.BOM_UTF16_LE)
    assert VocabStore(path).get().terms == ("Naïve", "Café")


def test_add_term_only_comments_appends(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("# only a comment", "utf-8")  # no trailing newline
    add_term(path, "First")
    assert path.read_text("utf-8") == "# only a comment\nFirst\n"


def test_add_term_creates_missing_file_with_header(tmp_path):
    path = tmp_path / "sub" / "vocab.txt"
    r = add_term(path, "Zorblax")
    assert r.added and r.count == 1
    text = path.read_text("utf-8")
    assert text.startswith("# ShoutAbout vocabulary")
    assert parse_vocab(text) == ["Zorblax"]


def test_add_term_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("One\n", "utf-8")
    add_term(path, "Two")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vocab.txt"]


def test_store_hot_reloads_taught_terms(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("Better Shell\n", "utf-8")
    store = VocabStore(path)
    v1 = store.get()
    add_term(path, "Chameleon CV")
    v2 = store.get()
    assert v2.terms == ("Chameleon CV", "Better Shell")
    assert v2.version == v1.version + 1


def test_remove_term(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("# GitHub stays (comment)\nGitHub\nOther\ngithub\n", "utf-8")
    assert remove_term(path, "  GITHUB ")
    assert path.read_text("utf-8") == "# GitHub stays (comment)\nOther\n"
    assert not remove_term(path, "GitHub")
    assert not remove_term(tmp_path / "missing.txt", "x")


def test_remove_term_accepts_lines_that_would_not_validate(tmp_path):
    path = tmp_path / "vocab.txt"
    long_line = "x" * 80
    path.write_text(f"{long_line}\nKeep\n", "utf-8")
    assert remove_term(path, long_line)
    assert path.read_text("utf-8") == "Keep\n"


def test_concurrent_adds_lose_nothing(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("# c\n", "utf-8")
    words = [f"Word{i:02d}x" for i in range(40)]
    errors: list[BaseException] = []

    def teach(chunk):
        try:
            for w in chunk:
                add_term(path, w)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=teach, args=(words[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert sorted(parse_vocab(path.read_text("utf-8"))) == sorted(words)
    assert not path.with_name("vocab.txt.lock").exists()


def test_concurrent_adds_from_two_processes(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "vocab.txt"
    path.write_text("# c\n", "utf-8")
    script = (
        "import sys\n"
        "from shoutabout.vocab import add_term\n"
        "for i in range(15):\n"
        "    add_term(sys.argv[1], f'{sys.argv[2]}{i:02d}x')\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(path), prefix])
        for prefix in ("Alpha", "Beta")
    ]
    assert [p.wait(60) for p in procs] == [0, 0]
    terms = parse_vocab(path.read_text("utf-8"))
    assert len(terms) == 30
    assert set(terms) == {f"{p}{i:02d}x" for p in ("Alpha", "Beta") for i in range(15)}


def test_stale_lock_file_is_broken(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("One\n", "utf-8")
    lock = tmp_path / "vocab.txt.lock"
    lock.write_text("12345\n", "utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert add_term(path, "Two").added
    assert not lock.exists()


def test_fresh_lock_file_times_out(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("One\n", "utf-8")
    (tmp_path / "vocab.txt.lock").write_text("12345\n", "utf-8")
    with pytest.raises(FileLockTimeout), file_lock(path, timeout=0.2):
        pass  # pragma: no cover
    assert path.read_text("utf-8") == "One\n"
