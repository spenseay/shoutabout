"""Vocab suggestions: words the owner says often, mined locally from their dictation history
and their project folders.

Nothing here changes vocab.txt on its own. Candidates wait in ``<home>/vocab-suggestions.json``
with ``status`` ``pending`` | ``accepted`` | ``dismissed`` until the owner accepts them
(:func:`accept` -> :func:`shoutabout.vocab.add_terms`, so they go to the top of vocab.txt) or
dismisses them; accepted and dismissed words are never suggested again. Everything stays on
this PC, and logs carry counts only (suggestions come from transcripts).

History candidates (:func:`mine_history`):

a. words the LLM cleanup re-cased or corrected (raw vs cleaned text), e.g. "github" -> "GitHub";
b. Capitalised words / 2-3 word Capitalised Phrases seen mid-sentence at least twice;
c. CamelCase / mixed-case / letters+digits tokens (GitHub, iOS, GPT5) seen at least twice;

minus existing vocab, words only ever capitalised at the start of a sentence, a built-in
stoplist of everyday capitalised words (days, months, I, I'm, OK, ...) and anything accepted
or dismissed before.

Project candidates (:func:`seed_from_projects`): folder names, git remote repo names, package
names (package.json, pyproject.toml, Cargo.toml, go.mod) and README ``# Titles`` of project
folders up to two levels below the roots, as spoken forms ("chameleon-cv" -> "Chameleon CV").
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tomllib
from collections import Counter, deque
from collections.abc import Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from shoutabout.config import Paths
from shoutabout.vocab import (
    AddResult,
    VocabTermError,
    add_terms,
    atomic_write_bytes,
    file_lock,
    normalize_term,
    read_terms,
)

log = logging.getLogger(__name__)

STORE_VERSION = 1
SOURCES = ("history", "projects")
STATUSES = ("pending", "accepted", "dismissed")

MIN_MENTIONS = 2  # (b) and (c) need at least this many occurrences
MAX_PHRASE_WORDS = 3
MAX_HISTORY_RECORDS = 20_000  # most recent records mined
MAX_HISTORY_SUGGESTIONS = 40
MAX_PROJECT_SUGGESTIONS = 60
MAX_SCAN_DIRS = 4000  # directories listed per seed_from_projects() call
MAX_DIR_ENTRIES = 3000  # entries looked at per directory
README_MAX_BYTES = 8192

# --------------------------------------------------------------------------- stoplist

_STOP_WORDS = """
i i'm i've i'll i'd im ive ill id ok okay ok'd hey hi hello yes yeah yep no nope oh um uh
uhm hmm ah wow please thanks thank sorry well so and but or nor yet the a an this that these
those it its it's we we're we've you you're your yours he she they them their there here
my mine our ours me us his her what when where why how who whom which whose if then than
also now just maybe sure right actually basically first second third next finally today
tomorrow yesterday tonight morning afternoon evening night let's lets can could would should
will shall do does did don't doesn't didn't is are was were be been being have has had in on
at for to of with from by about as into over after before again all any both each few more
most other some such only own same too very not one two three four five six seven eight nine
ten new old good great nice cool fine dear mr mrs ms dr sr jr st prof
monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri
sat sun january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec
god christmas easter thanksgiving halloween english spanish french german chinese american
america internet mom dad tv am pm us usa uk eu ai api pc ceo faq fyi asap eta usb cpu gpu id
url pdf lol omg wi-fi wifi
google apple microsoft windows amazon youtube facebook instagram twitter netflix siri iphone
ipad mac
"""


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


STOPLIST = _wordset(_STOP_WORDS)

# Acronyms kept upper-case in spoken forms of project slugs ("chameleon-cv" -> "Chameleon CV").
ACRONYMS = _wordset(
    "ai api cv ui ux gpu cpu llm sdk cli db id ml gpt pdf url sql css html js ts vr ar os tts "
    "stt ocr mcp aws gcp io"
)
_SPECIAL_CASE = {"ios": "iOS", "macos": "macOS", "ipados": "iPadOS", "github": "GitHub"}

# --------------------------------------------------------------------------- model


@dataclass
class Suggestion:
    term: str
    source: str  # "history" | "projects"
    count: int
    detail: str  # e.g. "seen 3× in your dictations", "project folder Documents\\chameleon-cv"
    first_seen: str  # ISO 8601 local time
    status: str = "pending"  # "pending" | "accepted" | "dismissed"
    updated: str = ""

    @property
    def key(self) -> str:
        return match_key(self.term)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Suggestion | None:
        if not isinstance(data, dict) or not isinstance(data.get("term"), str):
            return None
        status = data.get("status")
        source = data.get("source")
        try:
            count = int(data.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        return cls(
            term=data["term"],
            source=source if isinstance(source, str) else "history",
            count=count,
            detail=str(data.get("detail") or ""),
            first_seen=str(data.get("first_seen") or ""),
            status=status if status in STATUSES else "pending",
            updated=str(data.get("updated") or ""),
        )


_KEY_SEPARATORS = re.compile(r"[\s\-_.]+")


def match_key(term: str) -> str:
    """Identity used to de-duplicate suggestions against vocab and each other:
    case-insensitive and ignoring spaces, hyphens, underscores and dots, so
    "chameleon-cv", "Chameleon CV" and "ChameleonCV" are the same word."""
    return _KEY_SEPARATORS.sub("", term.casefold())


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _valid(term: str) -> str | None:
    try:
        return normalize_term(term)
    except VocabTermError:
        return None


# ------------------------------------------------------------------ history mining

_OPENERS = "\"'`“‘([{«<"
_CLOSERS = "\"'`”’)]}»>"
_SENTENCE_END = ".!?…"
_TRAILING = ",;:" + _SENTENCE_END + _CLOSERS
_POSSESSIVE = re.compile(r"['’][sS]$")


@dataclass(frozen=True)
class _Tok:
    text: str  # without surrounding punctuation / possessive 's
    start: bool  # first word of a sentence (or of a quote)
    brk: bool  # punctuation right after it: a phrase can't continue past it


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    start = True
    for raw in text.split():
        core = raw.strip(_OPENERS + _CLOSERS + ",;:" + _SENTENCE_END)
        core = _POSSESSIVE.sub("", core)
        tail = raw.rstrip(_CLOSERS)
        ends_sentence = bool(tail) and tail[-1] in _SENTENCE_END
        if core and any(ch.isalnum() for ch in core):
            out.append(_Tok(core, start or raw[0] in _OPENERS, raw[-1] in _TRAILING))
            start = ends_sentence
        elif ends_sentence:
            start = True
    return out


def _stop_key(word: str) -> str:
    return word.replace("’", "'").casefold()


def _is_stop(word: str) -> bool:
    return _stop_key(word) in STOPLIST


def _is_capitalised(word: str) -> bool:
    return len(word) >= 2 and word[0].isupper() and any(ch.isalpha() for ch in word)


def _is_mixed_case(word: str) -> bool:
    """CamelCase / mixed case: GitHub, iOS, ShoutAbout, macOS (not "Hello", not "API")."""
    return any(ch.isupper() for ch in word[1:]) and any(ch.islower() for ch in word)


def _is_code_word(word: str) -> bool:
    """Letters + digits with a capital: GPT5, RTX4080, M2Pro."""
    return (
        len(word) >= 3
        and any(ch.isdigit() for ch in word)
        and any(ch.isalpha() for ch in word)
        and any(ch.isupper() for ch in word)
    )


def _distinctive(word: str) -> bool:
    return _is_mixed_case(word) or _is_code_word(word)


@dataclass
class _Candidate:
    forms: Counter = field(default_factory=Counter)
    occurrences: set = field(default_factory=set)
    mid: int = 0  # (b) mid-sentence capitalised occurrences
    code: int = 0  # (c) distinctive-token occurrences
    fixes: int = 0  # (a) cleanup re-cased / corrected it
    heard: str | None = None  # what Whisper wrote before cleanup fixed it
    first_seen: str = ""
    words: int = 1

    def see(self, form: str, occurrence: tuple, ts: str) -> None:
        self.forms[form] += 1
        self.occurrences.add(occurrence)
        if ts and (not self.first_seen or ts < self.first_seen):
            self.first_seen = ts


def _proper_runs(tokens: Sequence[_Tok]) -> tuple[list[list[int]], list[list[int]]]:
    """Maximal runs (<= 3 words) of capitalised, non-stoplist words: ``(runs, tails)``.

    ``runs`` start mid-sentence. A run that begins with a capitalised sentence-initial word
    is ambiguous: "Tell Priya Raghunathan" should give "Priya Raghunathan", but "Claude Code
    is ready" must not give "Code". Its tail (the run minus that first word) is returned in
    ``tails``; the caller counts a tail only if it was also seen as a run of its own.
    """
    runs: list[list[int]] = []
    tails: list[list[int]] = []
    current: list[int] = []
    tainted = False

    def flush() -> None:
        nonlocal current, tainted
        if current and not tainted and len(current) <= MAX_PHRASE_WORDS:
            runs.append(current)
        elif tainted and 2 <= len(current) <= MAX_PHRASE_WORDS + 1:
            tails.append(current[1:])
        current, tainted = [], False

    for i, tok in enumerate(tokens):
        proper = _is_capitalised(tok.text) and not _is_stop(tok.text)
        if tok.start:
            flush()
            if proper:
                current, tainted = [i], True
        elif proper:
            current.append(i)
        else:
            flush()
        if tok.brk:
            flush()
    flush()
    return runs, tails


def _cleanup_fixes(raw: str, text: str) -> list[tuple[list[int], str]]:
    """Spans of cleaned-text tokens the LLM re-cased or replaced into a proper-looking word.

    Returns ``[(token indices in tokens(text), heard raw words)]``.
    """
    raw_toks, clean_toks = _tokens(raw), _tokens(text)
    a = [t.text.casefold() for t in raw_toks]
    b = [t.text.casefold() for t in clean_toks]
    heard: dict[int, str] = {}
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                if raw_toks[i1 + k].text != clean_toks[j1 + k].text:
                    heard[j1 + k] = raw_toks[i1 + k].text
        elif op == "replace":
            said = " ".join(t.text for t in raw_toks[i1:i2])
            for j in range(j1, j2):
                heard[j] = said

    def proper(j: int) -> bool:
        tok = clean_toks[j]
        word = tok.text
        if _is_stop(word) or not (_is_capitalised(word) or _distinctive(word)):
            return False
        return not tok.start or _distinctive(word)

    spans: list[tuple[list[int], str]] = []
    current: list[int] = []

    def flush() -> None:
        nonlocal current
        if current and len(current) <= MAX_PHRASE_WORDS:
            said = " ".join(dict.fromkeys(heard[j] for j in current))
            spans.append((current, said))
        current = []

    for j in range(len(clean_toks)):
        if j in heard and proper(j):
            if current and (j != current[-1] + 1 or clean_toks[current[-1]].brk):
                flush()
            current.append(j)
        else:
            flush()
    flush()
    return spans


def _read_history(path: Path) -> list[dict[str, Any]]:
    records: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY_RECORDS)
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("couldn't read history for suggestions: %s", exc)
        return []
    return list(records)


def mine_history(
    history_path: Path, vocab_terms: Iterable[str], *, exclude: Collection[str] = ()
) -> list[Suggestion]:
    """Suggestions from ``history.jsonl`` (see the module docstring), best first.

    ``exclude`` holds :func:`match_key` keys never to suggest (accepted/dismissed before).
    """
    blocked = {match_key(t) for t in vocab_terms} | set(exclude)
    candidates: dict[str, _Candidate] = {}
    lower_mid: Counter = Counter()  # mid-sentence lowercase uses of single words
    deferred: list[tuple[list[str], tuple, str]] = []  # tails of sentence-initial runs

    def cand(words: Sequence[str]) -> tuple[str, _Candidate]:
        term = " ".join(words)
        key = match_key(term)
        c = candidates.get(key)
        if c is None:
            c = candidates[key] = _Candidate(words=len(words))
        return term, c

    for n, record in enumerate(_read_history(history_path)):
        text = record.get("text")
        raw = record.get("raw_text")
        ts = record.get("ts") if isinstance(record.get("ts"), str) else ""
        if not isinstance(text, str) or not text.strip():
            continue
        tokens = _tokens(text)
        for tok in tokens:
            if not tok.start and tok.text.islower():
                lower_mid[tok.text.casefold()] += 1
        # (b) capitalised words / phrases mid-sentence
        runs, tails = _proper_runs(tokens)
        for run in runs:
            term, c = cand([tokens[i].text for i in run])
            c.see(term, (n, run[0], len(run)), ts)
            c.mid += 1
        for tail in tails:
            deferred.append(([tokens[i].text for i in tail], (n, tail[0], len(tail)), ts))
        # (c) mixed-case / letters+digits tokens anywhere
        for i, tok in enumerate(tokens):
            if _distinctive(tok.text) and not _is_stop(tok.text):
                term, c = cand([tok.text])
                c.see(term, (n, i, 1), ts)
                c.code += 1
        # (a) what the LLM cleanup re-cased or corrected
        if record.get("cleanup") == "applied" and isinstance(raw, str) and raw != text:
            for span, said in _cleanup_fixes(raw, text):
                term, c = cand([tokens[j].text for j in span])
                c.see(term, (n, span[0], len(span)), ts)
                c.fixes += 1
                c.heard = c.heard or said

    # Tails of sentence-initial runs ("Tell <Priya Raghunathan>") count once the same words
    # were seen as a mid-sentence run somewhere else.
    confirmed = {key for key, c in candidates.items() if c.mid}
    for words, occurrence, ts in deferred:
        term = " ".join(words)
        if match_key(term) in confirmed:
            _, c = cand(words)
            c.see(term, occurrence, ts)
            c.mid += 1

    out: list[Suggestion] = []
    for key, c in candidates.items():
        if key in blocked or not key:
            continue
        if not (c.fixes >= 1 or c.mid >= MIN_MENTIONS or c.code >= MIN_MENTIONS):
            continue
        form = c.forms.most_common(1)[0][0]
        if (
            c.words == 1
            and not c.fixes
            and c.code < MIN_MENTIONS
            and lower_mid[form.casefold()] > c.mid
        ):
            continue  # mostly a lowercase everyday word ("Plan" vs "plan")
        term = _valid(form)
        if term is None:
            continue
        count = len(c.occurrences)
        if c.fixes:
            detail = f'cleanup fixed it {c.fixes}× (Whisper heard "{c.heard}")'
            if count > c.fixes:
                detail += f"; seen {count}× in your dictations"
        else:
            detail = f"seen {count}× in your dictations"
        out.append(Suggestion(term, "history", count, detail, c.first_seen or _now()))
    out.sort(key=lambda s: (-s.count, s.first_seen, s.term.casefold()))
    return out[:MAX_HISTORY_SUGGESTIONS]


# ----------------------------------------------------------------- project seeding

PROJECT_FILES = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")
SKIP_DIRS = _wordset(
    "node_modules .venv venv env __pycache__ site-packages dist build target bin obj out"
) | {"my music", "my pictures", "my videos"}  # Windows junctions inside Documents
GENERIC_NAMES = _wordset(
    "src app apps test tests docs doc demo demos example examples sample samples tmp temp "
    "scratch sandbox playground project projects repo repos code backup backups old new misc "
    "untitled web website site api server client frontend backend lib libs tools scripts utils "
    "packages workspace main master dev github git my-app hello-world test-project notes "
    "documents downloads desktop"
)
# Words that mark a copy of a project rather than its name ("paperclip-src", "app-v2-review").
VARIANT_WORDS = _wordset(
    "src source main master dev old new copy backup bak test tests review hotfix fix "
    "beta alpha wip tmp temp fork clone draft final project worktree worktrees spike"
)
MAX_TERMS_PER_PROJECT = 3
_SLUG_SPLIT = re.compile(r"[\s\-_.]+")
_VERSION_WORD = re.compile(r"^v?\d+$")
_GLUED_DIGITS = re.compile(r"\b[A-Z][a-z]+\d+\b")  # "Erni3" from "coach_erni3": not how it's said
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_HEADING_SPLIT = re.compile(r"\s+[-–—|:]\s+|:\s")
_REMOTE_SECTION = re.compile(r'^\s*\[\s*remote\s+"[^"]*"\s*\]', re.I)
_SECTION = re.compile(r"^\s*\[")
_URL_LINE = re.compile(r"^\s*url\s*=\s*(\S+)", re.I)


def default_project_roots() -> list[Path]:
    home = Path.home()
    names = ("Documents", "source/repos", "repos", "code", "projects")
    return [home / n for n in names if (home / n).is_dir()]


def spoken_form(name: str) -> str:
    """How a project slug is said: "chameleon-cv" -> "Chameleon CV", "better_shell" ->
    "Better Shell", "ios-app" -> "iOS App". Mixed-case names without separators stay as
    they are ("ShoutAbout")."""
    name = name.strip()
    if not _SLUG_SPLIT.search(name) and _is_mixed_case(name):
        return name
    words = []
    for part in _SLUG_SPLIT.split(name):
        if not part:
            continue
        low = part.casefold()
        if low in _SPECIAL_CASE:
            words.append(_SPECIAL_CASE[low])
        elif low in ACRONYMS:
            words.append(part.upper())
        elif _is_mixed_case(part) or part.isupper():
            words.append(part)
        else:
            words.append(part[:1].upper() + part[1:])
    return " ".join(words)


def base_slug(name: str) -> str:
    """Drop variant words at either end of a folder/repo name: "paperclip-src" ->
    "paperclip", "old-portfolio-test" -> "portfolio", "app-v2" -> "app"."""
    parts = [p for p in _SLUG_SPLIT.split(name) if p]

    def variant(part: str) -> bool:
        low = part.casefold()
        return low in VARIANT_WORDS or bool(_VERSION_WORD.match(low))

    stripped = list(parts)
    while len(stripped) > 1 and variant(stripped[-1]):
        stripped.pop()
    while len(stripped) > 1 and variant(stripped[0]):
        stripped.pop(0)
    return name if stripped == parts else "-".join(stripped)


def _scandir(path: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            entries = []
            for i, entry in enumerate(it):
                if i >= MAX_DIR_ENTRIES:
                    break
                entries.append(entry)
            return entries
    except OSError:
        return []


def _is_project(entries: Sequence[os.DirEntry]) -> bool:
    names = {e.name for e in entries}
    if ".git" in names or any(n in names for n in PROJECT_FILES):
        return True
    return any(n.lower().endswith(".sln") for n in names)


def _subdirs(entries: Sequence[os.DirEntry]) -> list[Path]:
    out = []
    for e in entries:
        name = e.name
        if name.startswith(".") or name.casefold() in SKIP_DIRS:
            continue
        try:
            if e.is_symlink() or e.is_junction() or not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        out.append(Path(e.path))
    return sorted(out, key=lambda p: p.name.casefold())


def find_projects(roots: Iterable[Path], max_depth: int = 2) -> list[Path]:
    """Project folders (``.git``, pyproject.toml, package.json, Cargo.toml, go.mod, *.sln)
    at most ``max_depth`` levels below each root. A root that is itself a project is
    included and still searched; other projects, hidden folders, .venv and node_modules
    are not descended into."""
    found: list[Path] = []
    seen: set[str] = set()
    budget = MAX_SCAN_DIRS
    queue: deque[tuple[Path, int]] = deque((Path(r).expanduser(), 0) for r in roots)
    while queue and budget > 0:
        path, depth = queue.popleft()
        try:
            ident = os.path.normcase(str(path.resolve()))
        except OSError:
            continue
        if ident in seen:
            continue
        seen.add(ident)
        budget -= 1
        entries = _scandir(path)
        if _is_project(entries):
            found.append(path)
            if depth > 0:  # a root is always searched (~/Documents may itself be a repo)
                continue
        if depth < max_depth:
            queue.extend((sub, depth + 1) for sub in _subdirs(entries))
    return found


def _read_text(path: Path, limit: int = 65536) -> str | None:
    try:
        with path.open("rb") as fh:
            data = fh.read(limit)
    except OSError:
        return None
    return data.decode("utf-8-sig", errors="replace")


def _git_dir(project: Path) -> Path | None:
    dot_git = project / ".git"
    if dot_git.is_dir():
        return dot_git
    text = _read_text(dot_git, 4096) if dot_git.is_file() else None
    if not text or not text.startswith("gitdir:"):
        return None
    git_dir = Path(text[len("gitdir:") :].strip())
    if not git_dir.is_absolute():
        git_dir = project / git_dir
    common = _read_text(git_dir / "commondir", 4096)  # worktrees point at the main .git
    if common and common.strip():
        common_dir = Path(common.strip())
        git_dir = common_dir if common_dir.is_absolute() else git_dir / common_dir
    return git_dir


def _repo_name_from_url(url: str) -> str | None:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    name = re.split(r"[/:\\]", url)[-1] if url else ""
    return name or None


def git_remote_names(project: Path) -> list[str]:
    """Repo names of the project's git remotes, parsed from .git/config (no git needed)."""
    git_dir = _git_dir(project)
    text = _read_text(git_dir / "config") if git_dir else None
    if not text:
        return []
    names: list[str] = []
    in_remote = False
    for line in text.splitlines():
        if _SECTION.match(line):
            in_remote = bool(_REMOTE_SECTION.match(line))
            continue
        m = _URL_LINE.match(line) if in_remote else None
        if m:
            name = _repo_name_from_url(m.group(1))
            if name and name not in names:
                names.append(name)
    return names


def package_names(project: Path) -> list[str]:
    """package.json "name" (scope dropped), pyproject [project]/[tool.poetry] name,
    Cargo [package] name, go.mod module (last path part)."""
    names: list[str] = []
    text = _read_text(project / "package.json")
    if text:
        with contextlib.suppress(ValueError):
            data = json.loads(text)
            name = data.get("name") if isinstance(data, dict) else None
            if isinstance(name, str) and name.strip():
                names.append(name.strip().rsplit("/", 1)[-1])
    for filename, tables in (
        ("pyproject.toml", (("project",), ("tool", "poetry"))),
        ("Cargo.toml", (("package",),)),
    ):
        text = _read_text(project / filename)
        if not text:
            continue
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue
        for path in tables:
            node: Any = data
            for part in path:
                node = node.get(part) if isinstance(node, dict) else None
            name = node.get("name") if isinstance(node, dict) else None
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
                break
    text = _read_text(project / "go.mod", 4096)
    if text:
        m = re.search(r"^\s*module\s+(\S+)", text, re.M)
        if m:
            names.append(m.group(1).rstrip("/").rsplit("/", 1)[-1])
    return list(dict.fromkeys(names))


def readme_title(project: Path) -> str | None:
    """The first ``# Heading`` of the README, without links/badges/emoji or a subtitle."""
    for name in ("README.md", "readme.md", "Readme.md", "README.markdown"):
        text = _read_text(project / name, README_MAX_BYTES)
        if text is None:
            continue
        for line in text.splitlines():
            m = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
            if not m:
                continue
            title = _MD_IMAGE.sub("", m.group(1))
            title = _MD_LINK.sub(r"\1", title)
            title = _HTML_TAG.sub("", title).replace("`", "").replace("*", "")
            title = "".join(ch for ch in title if ord(ch) < 0x2190 or ch.isalnum())  # emoji
            title = _HEADING_SPLIT.split(title, maxsplit=1)[0].strip()
            return title or None
        return None
    return None


def _rel(path: Path) -> str:
    """The path relative to the home folder (e.g. Documents/chameleon-cv), else in full."""
    for candidate, home in ((path, Path.home()), (path.resolve(), Path.home().resolve())):
        with contextlib.suppress(ValueError, OSError):
            return str(candidate.relative_to(home))
    return str(path)


def _title_like(term: str) -> bool:
    words = term.split()
    return 1 <= len(words) <= MAX_PHRASE_WORDS and all(
        (w[:1].isupper() or _distinctive(w) or not w[:1].isalpha()) and not _is_stop(w)
        for w in words
    )


def _project_names(project: Path, folder_is_variant: bool) -> list[tuple[str, str]]:
    """(display term, where it came from) for one project, merged by match key."""
    raw: list[tuple[str, str, int]] = []  # (term, label, preference)
    if not folder_is_variant and not (project / ".git").is_file():
        raw.append((spoken_form(base_slug(project.name)), "project folder", 1))
    for name in git_remote_names(project):
        raw.append((spoken_form(base_slug(name)), "git remote of", 2))
    for name in package_names(project):
        raw.append((spoken_form(base_slug(name)), "package name in", 3))
    title = readme_title(project)
    names_so_far = {match_key(t) for t, _, _ in raw}
    if title and (match_key(title) in names_so_far or _title_like(title)):
        raw.append((title, "README title in", 4 if _distinctive(title) else 0))

    best: dict[str, tuple[str, str, int]] = {}
    for term, label, pref in raw:
        valid = _valid(term)
        if valid is None or len(match_key(valid)) < 3:
            continue
        if _SLUG_SPLIT.sub("-", valid.casefold()) in GENERIC_NAMES or _is_stop(valid):
            continue
        if not any(ch.isalpha() for ch in valid):
            continue
        if label != "README title in" and _GLUED_DIGITS.search(valid):
            continue
        key = match_key(valid)
        # Prefer a mixed-case spelling (README "ShoutAbout" over folder "Shoutabout").
        score = (2 if _distinctive(valid) else 0) + pref
        if key not in best or score > best[key][2]:
            best[key] = (valid, label, score)
    ranked = sorted(best.values(), key=lambda item: -item[2])[:MAX_TERMS_PER_PROJECT]
    return [(term, label) for term, label, _ in ranked]


def seed_from_projects(
    roots: Iterable[Path] | None = None, *, exclude: Collection[str] = ()
) -> list[Suggestion]:
    """Suggestions from project folders below ``roots`` (default: :func:`default_project_roots`).

    Read-only: lists folders and reads small metadata files (.git/config, package.json,
    pyproject.toml, Cargo.toml, go.mod, README). Clones/worktrees of the same repo count
    once per folder; folders named like another project plus a suffix ("app-v2-review")
    are treated as variants of it.
    """
    roots = default_project_roots() if roots is None else [Path(r) for r in roots]
    projects = find_projects(roots)
    slugs = {p.name.casefold() for p in projects}

    def is_variant(project: Path) -> bool:
        name = project.name.casefold()
        return any(
            name != other and name.startswith(other) and name[len(other)] in "-_. "
            for other in slugs
        )

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    blocked = set(exclude)
    found: dict[str, dict[str, Any]] = {}
    now = _now()
    for project in sorted(projects, key=mtime, reverse=True):
        for term, label in _project_names(project, is_variant(project)):
            key = match_key(term)
            if key in blocked:
                continue
            entry = found.get(key)
            if entry is None:
                found[key] = {"term": term, "label": label, "where": [project], "order": len(found)}
            elif project not in entry["where"]:
                entry["where"].append(project)
    out = []
    for entry in found.values():
        where: list[Path] = entry["where"]
        detail = f"{entry['label']} {_rel(where[0])}"
        if len(where) > 1:
            detail += f" (+{len(where) - 1} more folder{'s' if len(where) > 2 else ''})"
        out.append(Suggestion(entry["term"], "projects", len(where), detail, now))
    order = {match_key(e["term"]): e["order"] for e in found.values()}
    out.sort(key=lambda s: (-s.count, order[s.key]))
    return out[:MAX_PROJECT_SUGGESTIONS]


# --------------------------------------------------------------------------- store


def load_suggestions(paths: Paths) -> list[Suggestion]:
    """Every stored suggestion (any status). A missing or corrupt file means none."""
    try:
        data = json.loads(paths.suggestions_file.read_text("utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", paths.suggestions_file.name, exc)
        return []
    items = data.get("suggestions") if isinstance(data, dict) else None
    out: list[Suggestion] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        s = Suggestion.from_dict(item)
        if s is not None and s.key and s.key not in seen:
            seen.add(s.key)
            out.append(s)
    return out


def _save(paths: Paths, suggestions: Sequence[Suggestion]) -> None:
    payload = {
        "version": STORE_VERSION,
        "updated": _now(),
        "suggestions": [s.to_dict() for s in suggestions],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    atomic_write_bytes(paths.suggestions_file, text.encode("utf-8"))


def pending_suggestions(paths: Paths) -> list[Suggestion]:
    """Pending suggestions not already in vocab.txt: history first, then projects, best first."""
    vocab = {match_key(t) for t in read_terms(paths.vocab_file)}
    pending = [s for s in load_suggestions(paths) if s.status == "pending" and s.key not in vocab]
    rank = {src: i for i, src in enumerate(SOURCES)}
    pending.sort(key=lambda s: (rank.get(s.source, len(rank)), -s.count))
    return pending


def refresh_suggestions(
    paths: Paths,
    include_projects: bool = False,
    *,
    include_history: bool = True,
    roots: Iterable[Path] | None = None,
) -> int:
    """Mine history (and projects) and merge new candidates into the store.

    Pending suggestions get fresh counts; accepted/dismissed ones are never re-added;
    pending ones that meanwhile appeared in vocab.txt become accepted. Returns how many
    NEW pending suggestions were added.
    """
    with file_lock(paths.suggestions_file):
        stored = load_suggestions(paths)
        by_key = {s.key: s for s in stored}
        vocab_terms = read_terms(paths.vocab_file)
        vocab_keys = {match_key(t) for t in vocab_terms}
        now = _now()
        for s in stored:
            if s.status == "pending" and s.key in vocab_keys:
                s.status, s.updated = "accepted", now
        blocked = {k for k, s in by_key.items() if s.status != "pending"}
        found: list[Suggestion] = []
        if include_history:
            found += mine_history(paths.history_file, vocab_terms, exclude=blocked)
        if include_projects:
            found += seed_from_projects(roots, exclude=blocked | vocab_keys)
        new = 0
        for s in found:
            key = s.key
            if key in vocab_keys or key in blocked:
                continue
            old = by_key.get(key)
            if old is None:
                s.updated = now
                stored.append(s)
                by_key[key] = s
                new += 1
            elif old.status == "pending" and old.source == s.source:
                old.count, old.detail, old.updated = s.count, s.detail, now
        _save(paths, stored)
    log.info(
        "vocab suggestions refreshed: %d new, %d pending",
        new,
        sum(s.status == "pending" for s in stored),
    )
    return new


def _set_status(
    stored: list[Suggestion], term: str, status: str, now: str, *, create: bool = True
) -> None:
    key = match_key(term)
    for s in stored:
        if s.key == key:
            s.status, s.updated = status, now
            return
    if create and key:
        stored.append(Suggestion(term, "manual", 0, "", now, status, now))


def accept(paths: Paths, terms: Sequence[str]) -> list[AddResult]:
    """Add ``terms`` to the top of vocab.txt (in this order) and mark them accepted, so
    they're never suggested again (even if later removed from vocab.txt)."""
    with file_lock(paths.suggestions_file):
        results = add_terms(paths.vocab_file, terms)
        stored = load_suggestions(paths)
        now = _now()
        for raw, result in zip(terms, results, strict=True):
            if result.status != "invalid":
                _set_status(stored, result.term, "accepted", now)
                # A suggestion stored under the raw spelling (rare: it was normalised).
                _set_status(stored, raw, "accepted", now, create=False)
        _save(paths, stored)
    return results


def dismiss(paths: Paths, terms: Sequence[str]) -> int:
    """Never suggest ``terms`` again. Returns how many were recorded."""
    done = 0
    with file_lock(paths.suggestions_file):
        stored = load_suggestions(paths)
        now = _now()
        for term in terms:
            cleaned = " ".join(term.split())
            if match_key(cleaned):
                _set_status(stored, cleaned, "dismissed", now)
                done += 1
        _save(paths, stored)
    return done
