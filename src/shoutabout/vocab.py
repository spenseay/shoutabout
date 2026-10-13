"""Custom vocabulary: vocab.txt parsing, hot reload, and Whisper prompt hints.

vocab.txt holds two kinds of line: a **word** ("Sofia", "/consolidate"), spelled the way it
should come out, and a **replacement** ("omw -> on my way", "slash consolidate ->
/consolidate"): what you say on the left, what gets written on the right. Words go to
Whisper as hints; both are applied to every transcript by :mod:`shoutabout.vocabfix`.

How the vocabulary reaches Whisper (faster-whisper 1.2.1)
---------------------------------------------------------
Both ``hotwords`` and ``initial_prompt`` end up in the decoder's "previous text" context
(``<|startofprev|> hotwords + previous-text tokens``). ``hotwords`` is added to *every*
30 s window; ``initial_prompt`` is the first window's "previous text" only (we decode
with ``condition_on_previous_text=False``). The decoder context is 448 tokens and the
new-token budget per window is ``min(224, 448 - prompt_len)``, so the vocab text of both
together is kept within ``[stt] max_prompt_tokens`` (default 200).

Hint text format, chosen by experiment on this project's target PC (RTX 4080 SUPER,
large-v3-turbo fp16, beam 5, VAD on; 12 SAPI sentences x 2 voices incl. a 38 s clip,
84 vocab-term occurrences, 14-term vocab with made-up names such as "Zorblax Industries"
and "Priya Raghunathan"): no hints found 43% of the terms; every hint variant 87-96%.
A natural sentence, ``In this dictation I mention A, B, C.``, was as accurate as the best
alternatives (92-95% vs. "A, B, C" 92-94%, "Glossary: ..." 87-96%, "Vocabulary: ..."
88-95%), had the lowest word error rate, and was the only format that never leaked into
the output on silence/noise with VAD off (the label formats produced e.g.
"Glossary. Glossary. ..."). Across three orderings of the 14 terms, "both" found 243/252,
hotwords 239, initial_prompt 233. With a 38-term vocab (~148 tokens), splitting the budget
evenly between the two dropped a third of the terms (77% found), while hotwords alone kept
them all (94%) and the scheme below 92%. Hence:

* every mode uses the sentence format;
* ``"both"`` gives hotwords the budget first (all terms that fit) and repeats the
  highest-priority terms as the initial prompt with whatever budget is left, so the
  total never exceeds ``max_tokens`` and adding the initial prompt never costs terms.

Teaching words (:func:`add_term` / :func:`add_terms` / :func:`remove_term`)
-------------------------------------------------------------------------
Used by ``POST /vocab``, the tray's "Teach ShoutAbout" window and ``shoutabout vocab``.
New terms go right after the leading comment block, i.e. above every existing term, so a
word the owner explicitly taught gets prompt priority. Writes are atomic (temp file +
``os.replace``) under a lock file, keep comments, order, BOM and line endings, and the
running hub's :class:`VocabStore` picks them up through its mtime/size hot reload.
"""

from __future__ import annotations

import codecs
import contextlib
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from shoutabout.engine.base import SttHints
from shoutabout.text import normalize_whitespace

log = logging.getLogger(__name__)

PROMPT_MODES = ("both", "hotwords", "initial_prompt", "none")
HINT_PREFIX = "In this dictation I mention "
HINT_SUFFIX = "."
_SEPARATOR = ", "
_WS = re.compile(r"\s+")
_MAX_LOGGED_TERMS = 20
ARROW = "->"
# "say -> write"; "=>" and "→" work too (a phone may turn "->" into an arrow).
_ARROW = re.compile(r"\s*(?:->|=>|\u2192)\s*")

CountTokens = Callable[[str], int]


# ----------------------------------------------------------------------- parsing


def split_replacement(line: str) -> tuple[str, str] | None:
    """``(say, write)`` for a replacement line ("omw -> on my way"), else None."""
    parts = _ARROW.split(line, maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()


def format_replacement(say: str, write: str) -> str:
    return f"{say} {ARROW} {write}"


def entry_key(entry: str) -> str:
    """Identity of a vocab.txt line: a word by its spelling, a replacement by what you say
    (one replacement per spoken phrase), both case-insensitive."""
    pair = split_replacement(entry)
    return f"{ARROW}{term_key(pair[0])}" if pair else term_key(entry)


def parse_entries(text: str) -> list[str]:
    """Every word and replacement line, in file order (replacements as "say -> write").

    Whitespace is trimmed (and runs of it collapsed); blank lines and lines whose first
    non-space character is ``#`` are skipped (no inline comments, so "C#" is a term);
    duplicates are dropped case-insensitively (see :func:`entry_key`), keeping the first.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for raw in text.lstrip("\ufeff").splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line or line.startswith("#"):
            continue
        pair = split_replacement(line)
        if pair:
            line = format_replacement(*pair)
        key = entry_key(line)
        if key in seen:
            continue
        seen.add(key)
        entries.append(line)
    return entries


def parse_vocab_file(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """``(words, replacements)`` in priority order."""
    terms: list[str] = []
    replacements: list[tuple[str, str]] = []
    for entry in parse_entries(text):
        pair = split_replacement(entry)
        if pair:
            replacements.append(pair)
        else:
            terms.append(entry)
    return terms, replacements


def parse_vocab(text: str) -> list[str]:
    """The words (not the replacements), one per line, in priority order."""
    return parse_vocab_file(text)[0]


def format_hint_text(terms: Sequence[str]) -> str:
    """The prompt text for ``terms`` ("" for none)."""
    if not terms:
        return ""
    return f"{HINT_PREFIX}{_SEPARATOR.join(terms)}{HINT_SUFFIX}"


def split_hint_text(text: str) -> tuple[str, list[str]]:
    """Inverse of :func:`format_hint_text`: ``(label, terms)``.

    Text without the label (e.g. a plain "A, B, C" list) is split on commas.
    """
    s = text.strip()
    label = ""
    prefix = HINT_PREFIX.strip()
    if s.startswith(prefix):
        label, s = prefix, s[len(prefix) :]
        if HINT_SUFFIX and s.endswith(HINT_SUFFIX):
            s = s[: -len(HINT_SUFFIX)]
    return label, [t.strip() for t in s.split(",") if t.strip()]


# ------------------------------------------------------------------------- hints


@dataclass(frozen=True)
class Vocab:
    terms: tuple[str, ...]
    version: int  # increments on every reload
    replacements: tuple[tuple[str, str], ...] = ()  # (say, write)


@dataclass(frozen=True)
class PromptBuild:
    hints: SttHints
    included: tuple[str, ...]
    dropped: tuple[str, ...]
    tokens: int  # tokens used by the vocab text (hotwords + initial prompt)


def _fit(
    terms: Sequence[str], count_tokens: CountTokens, max_tokens: int
) -> tuple[str | None, tuple[str, ...], int]:
    """Longest priority-order prefix of ``terms`` whose hint text fits ``max_tokens``."""
    included: list[str] = []
    text: str | None = None
    used = 0
    for term in terms:
        candidate = format_hint_text([*included, term])
        n = count_tokens(candidate)
        if n > max_tokens:
            break  # strict priority: a later, shorter term never jumps the queue
        included.append(term)
        text, used = candidate, n
    return text, tuple(included), used


def build_stt_hints(
    terms: Sequence[str], count_tokens: CountTokens, max_tokens: int, mode: str
) -> PromptBuild:
    """Vocab hints for one transcription, within ``max_tokens`` in total.

    mode: ``"hotwords"`` / ``"initial_prompt"`` -> that field only; ``"both"`` ->
    hotwords with every term that fits, plus an initial prompt repeating the top terms
    in the remaining budget; ``"none"`` -> empty hints. Empty terms -> empty hints.
    """
    if mode not in PROMPT_MODES:
        raise ValueError(f"unknown prompt mode {mode!r}; expected one of {PROMPT_MODES}")
    terms = tuple(terms)
    if mode == "none" or not terms:
        return PromptBuild(SttHints(), (), (), 0)

    text, included, used = _fit(terms, count_tokens, max_tokens)
    dropped = terms[len(included) :]
    if mode == "hotwords":
        return PromptBuild(SttHints(hotwords=text), included, dropped, used)
    if mode == "initial_prompt":
        return PromptBuild(SttHints(initial_prompt=text), included, dropped, used)
    ip_text, _ip_terms, ip_used = _fit(included, count_tokens, max_tokens - used)
    return PromptBuild(
        SttHints(initial_prompt=ip_text, hotwords=text), included, dropped, used + ip_used
    )


# ------------------------------------------------------------------------- store


def _decode(data: bytes, path: Path) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        log.warning("%s is not UTF-8; reading it as Windows-1252 (save it as UTF-8)", path)
        return data.decode("cp1252", errors="replace")


_UNSET = object()


class VocabStore:
    """vocab.txt with hot reload: ``get()`` re-reads the file when its mtime/size change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._vocab = Vocab(terms=(), version=0)
        self._signature: object = _UNSET
        self._last_error: str | None = None
        self._hints_key: tuple | None = None
        self._hints: PromptBuild | None = None
        self._warned_version = -1

    def _stat(self) -> tuple[int, int] | None:
        st = os.stat(self.path)
        return (st.st_mtime_ns, st.st_size)

    def get(self) -> Vocab:
        with self._lock:
            try:
                signature: tuple[int, int] | None = self._stat()
            except FileNotFoundError:
                signature = None
            except OSError as exc:
                self._log_error(f"cannot stat {self.path}: {exc}")
                return self._vocab
            if signature == self._signature:
                return self._vocab
            replacements: list[tuple[str, str]] = []
            if signature is None:
                terms: list[str] = []
                log.info("vocab file %s not found; using an empty vocabulary", self.path)
            else:
                try:
                    text = _decode(self.path.read_bytes(), self.path)
                    terms, replacements = parse_vocab_file(text)
                except FileNotFoundError:
                    terms, signature = [], None
                except OSError as exc:  # e.g. an editor holds the file mid-save
                    self._log_error(f"cannot read {self.path}: {exc}; keeping the last vocab")
                    return self._vocab
            self._signature = signature
            self._last_error = None
            self._vocab = Vocab(
                terms=tuple(terms),
                version=self._vocab.version + 1,
                replacements=tuple(replacements),
            )
            if signature is not None:
                log.info(
                    "loaded %d vocab term(s) and %d replacement(s) from %s (version %d)",
                    len(terms),
                    len(replacements),
                    self.path,
                    self._vocab.version,
                )
            return self._vocab

    def _log_error(self, message: str) -> None:
        if message != self._last_error:  # don't repeat the same error on every request
            log.warning("%s", message)
            self._last_error = message

    def stt_hints(self, count_tokens: CountTokens, max_tokens: int, mode: str) -> PromptBuild:
        """Hints for the current vocab; cached per (version, max_tokens, mode, counter)."""
        vocab = self.get()
        key = (vocab.version, max_tokens, mode, count_tokens)
        with self._lock:
            if self._hints is not None and self._hints_key == key:
                return self._hints
        build = build_stt_hints(vocab.terms, count_tokens, max_tokens, mode)
        with self._lock:
            self._hints_key, self._hints = key, build
            if build.dropped and self._warned_version != vocab.version:
                self._warned_version = vocab.version
                shown = ", ".join(build.dropped[:_MAX_LOGGED_TERMS])
                more = len(build.dropped) - _MAX_LOGGED_TERMS
                log.warning(
                    "vocab: %d of %d term(s) don't fit Whisper's %d-token prompt budget and "
                    "are ignored: %s%s. Terms at the top of %s win; move important ones up.",
                    len(build.dropped),
                    len(vocab.terms),
                    max_tokens,
                    shown,
                    f" (+{more} more)" if more > 0 else "",
                    self.path.name,
                )
        return build


# ---------------------------------------------------------------------- teaching

MAX_TERM_CHARS = 64
MAX_TERM_WORDS = 6
ALREADY_IN_VOCAB = "already in vocab"
LOCK_TIMEOUT_S = 5.0
LOCK_STALE_S = 30.0
_REPLACE_RETRIES = 40  # x 50 ms: an editor or a reader may hold the file for a moment

# Stripped from both ends of a taught term ("“GitHub”", "'iOS'", "`ruff`").
_QUOTES = "\"'`´“”„‟‘’‚‛«»‹›「」『』"
_TRAILING_PUNCT = ".,;:!?…。，、"
_LEADING_PUNCT = ",;:!?¡¿…"
_BRACKETS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_CLOSERS = {v: k for k, v in _BRACKETS.items()}
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85  ")


class VocabTermError(ValueError):
    """A term that can't go into vocab.txt. ``str(exc)`` is a short user-facing reason."""


@dataclass(frozen=True)
class AddResult:
    added: bool
    term: str  # the normalised term (or the existing spelling when already present)
    reason: str | None = None  # None when added; ALREADY_IN_VOCAB or a validation error
    count: int = 0  # terms in vocab.txt afterwards

    @property
    def status(self) -> str:
        """``"added"`` | ``"exists"`` | ``"invalid"`` (exact strings the iPhone Shortcut checks)."""
        if self.added:
            return "added"
        return "exists" if self.reason == ALREADY_IN_VOCAB else "invalid"


def _strip_wrapping(term: str) -> str:
    """Strip surrounding quotes, unbalanced brackets and sentence punctuation."""
    s = term
    while s:
        before = s
        s = s.strip()
        if len(s) >= 2 and _BRACKETS.get(s[0]) == s[-1]:
            s = s[1:-1]
        s = s.strip(_QUOTES)
        if s and s[-1] in _TRAILING_PUNCT:
            s = s[:-1]
        if s and s[0] in _LEADING_PUNCT:
            s = s[1:]
        if s and s[0] in _BRACKETS and _BRACKETS[s[0]] not in s:
            s = s[1:]
        if s and s[-1] in _CLOSERS and _CLOSERS[s[-1]] not in s:
            s = s[:-1]
        if s == before:
            break
    return s


def normalize_term(raw: str) -> str:
    """Clean up and validate one taught term or replacement; raises :class:`VocabTermError`.

    Trims whitespace (inner runs collapse to one space), surrounding quotes/brackets and
    sentence punctuation ("“Chameleon CV.”" -> "Chameleon CV"). Valid terms are 1-64
    characters, at most 6 words, contain a letter, have no control characters or line
    breaks, and don't start with ``#`` (vocab.txt would read that line as a comment).
    A replacement ("omw -> on my way") is checked side by side and comes back as
    "say -> write".
    """
    if not isinstance(raw, str):
        raise VocabTermError("no term given")
    s = raw.strip()
    if not s:
        raise VocabTermError("no term given")
    if any(ch in _LINE_BREAKS for ch in s):
        raise VocabTermError("one term at a time (no line breaks)")
    if any(unicodedata.category(ch) == "Cc" and ch != "\t" for ch in s):
        raise VocabTermError("contains control characters")
    s = normalize_whitespace(s)
    if _ARROW.search(s):
        return normalize_replacement(*_ARROW.split(s, maxsplit=1))
    return _normalize_one(s)


def normalize_replacement(say: str, write: str) -> str:
    """Validate "what you say" and "what gets written"; returns the "say -> write" line."""
    if not say.strip():
        raise VocabTermError("say what you say, then what to write (omw -> on my way)")
    if not write.strip():
        raise VocabTermError(f'what should "{say.strip()}" turn into?')
    try:
        said = _normalize_one(normalize_whitespace(say))
    except VocabTermError as exc:
        raise VocabTermError(f"what you say: {exc}") from None
    try:
        written = _normalize_one(normalize_whitespace(write))
    except VocabTermError as exc:
        raise VocabTermError(f"what to write: {exc}") from None
    if _ARROW.search(said) or _ARROW.search(written):
        raise VocabTermError("one replacement at a time")
    if said == written:
        raise VocabTermError("what you say and what to write are the same")
    return format_replacement(said, written)


def _normalize_one(s: str) -> str:
    s = _strip_wrapping(s)
    if not s:
        raise VocabTermError("needs at least one letter")
    if len(s) > MAX_TERM_CHARS:
        raise VocabTermError(f"too long (max {MAX_TERM_CHARS} characters)")
    if len(s.split(" ")) > MAX_TERM_WORDS:
        raise VocabTermError(f"too many words (max {MAX_TERM_WORDS})")
    if not any(ch.isalpha() for ch in s):
        raise VocabTermError("needs at least one letter")
    if s.startswith("#"):
        raise VocabTermError("can't start with # (vocab.txt would read it as a comment)")
    return s


def term_key(term: str) -> str:
    """Case-insensitive identity of a term (what de-duplication compares)."""
    return _WS.sub(" ", term).strip().casefold()


# -------------------------------------------------------------------- file locking


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(path))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


class FileLockTimeout(OSError):
    """Another writer held the lock file for longer than the timeout."""


@contextlib.contextmanager
def file_lock(
    path: Path, timeout: float = LOCK_TIMEOUT_S, stale_after: float = LOCK_STALE_S
) -> Iterator[None]:
    """Serialise writers of ``path`` across threads and processes.

    A ``<name>.lock`` file created with O_EXCL next to ``path``; a lock file older than
    ``stale_after`` seconds (left behind by a crashed writer) is broken.
    """
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    tlock = _thread_lock(path)
    if not tlock.acquire(timeout=timeout):
        raise FileLockTimeout(f"timed out waiting for {lock_path.name}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                # PermissionError: how Windows reports a lock file that is being deleted.
                try:
                    age = time.time() - os.stat(lock_path).st_mtime
                except OSError:
                    age = 0.0
                if age > stale_after:
                    log.warning("breaking stale lock file %s (%.0f s old)", lock_path, age)
                    with contextlib.suppress(OSError):
                        os.unlink(lock_path)
                    continue
                if time.monotonic() > deadline:
                    raise FileLockTimeout(f"timed out waiting for {lock_path.name}") from None
                time.sleep(0.02)
                continue
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            break
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.unlink(lock_path)
    finally:
        tlock.release()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same folder + ``os.replace`` (retried while locked)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: the target is open without FILE_SHARE_DELETE (an editor, a reader).
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.05)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# ------------------------------------------------------------------- vocab.txt edits


@dataclass
class _VocabFile:
    lines: list[str]  # each with its own line ending
    newline: str
    encoding: str  # "utf-8", "utf-8-sig" or "utf-16"

    @classmethod
    def read(cls, path: Path) -> _VocabFile:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return cls(lines=_default_header(), newline=os.linesep, encoding="utf-8")
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        elif data.startswith(codecs.BOM_UTF8):
            encoding = "utf-8-sig"
        else:
            encoding = "utf-8"  # a non-UTF-8 (ANSI) file is rewritten as UTF-8
        text = _decode(data, path).lstrip("﻿")
        crlf = text.count("\r\n")
        newline = "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"
        return cls(lines=text.splitlines(keepends=True), newline=newline, encoding=encoding)

    def encode(self) -> bytes:
        return "".join(self.lines).encode(self.encoding)

    @staticmethod
    def is_term(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and not stripped.startswith("#")

    def entries(self) -> list[str]:
        return parse_entries("".join(self.lines))

    def insert_top(self, terms: Sequence[str]) -> None:
        """Insert before the first term line (after the leading comments / blank lines)."""
        index = next((i for i, line in enumerate(self.lines) if self.is_term(line)), None)
        new = [term + self.newline for term in terms]
        if index is None:  # only comments / blank lines: append
            if self.lines and not self.lines[-1].endswith(("\n", "\r")):
                self.lines[-1] += self.newline
            self.lines.extend(new)
        else:
            self.lines[index:index] = new


def _default_header() -> list[str]:
    """Comment header for a vocab.txt created by teaching (the example file's comments)."""
    try:
        from shoutabout.config import example_vocab_text

        text = example_vocab_text()
    except Exception:
        return []
    header = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        header.append(line + os.linesep)
    return header


def read_entries(path: Path) -> list[str]:
    """Every word and replacement in ``path``, in file order ([] if it doesn't exist)."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return parse_entries(_decode(data, path))


def read_terms(path: Path) -> list[str]:
    """The words (not the replacements) in ``path`` in priority order."""
    return [e for e in read_entries(path) if split_replacement(e) is None]


def add_terms(path: Path, terms: Iterable[str]) -> list[AddResult]:
    """Teach several terms at once; they keep their given order at the top of the file.

    Never raises for a bad term: invalid ones come back with ``status == "invalid"`` and
    the reason. Raises OSError (incl. :class:`FileLockTimeout`) if the file can't be written.
    """
    path = Path(path)
    checked: list[tuple[str, str | None]] = []  # (normalised term, or raw text + error)
    for raw in terms:
        try:
            checked.append((normalize_term(raw), None))
        except VocabTermError as exc:
            checked.append((normalize_whitespace(str(raw))[:MAX_TERM_CHARS], str(exc)))
    with file_lock(path):
        vf = _VocabFile.read(path)
        existing = {entry_key(t): t for t in vf.entries()}
        to_add: list[str] = []
        outcome: list[tuple[str, bool, str | None]] = []
        for term, error in checked:
            if error is not None:
                outcome.append((term, False, error))
                continue
            key = entry_key(term)
            if key in existing:
                outcome.append((existing[key], False, ALREADY_IN_VOCAB))
                continue
            existing[key] = term
            to_add.append(term)
            outcome.append((term, True, None))
        if to_add:
            vf.insert_top(to_add)
            atomic_write_bytes(path, vf.encode())
        count = len(vf.entries())
    return [AddResult(added, term, reason, count) for term, added, reason in outcome]


def add_term(path: Path, term: str) -> AddResult:
    """Teach one term (see :func:`normalize_term`); raises :class:`VocabTermError` if invalid.

    A term already in the file (case-insensitively) stays where it is:
    ``AddResult(added=False, reason="already in vocab")``.
    """
    result = add_terms(path, [term])[0]
    if result.status == "invalid":
        raise VocabTermError(result.reason or "invalid term")
    return result


def remove_term(path: Path, term: str) -> bool:
    """Remove every line holding ``term`` (case-insensitive; a replacement by what you
    say). True if anything was removed. Comment lines are never touched.
    """
    path = Path(path)
    keys = {entry_key(_WS.sub(" ", term).strip())}
    with contextlib.suppress(VocabTermError):
        keys.add(entry_key(normalize_term(term)))
    keys.discard("")
    if not keys or not path.exists():
        return False
    with file_lock(path):
        vf = _VocabFile.read(path)
        kept = [
            line
            for line in vf.lines
            if not (vf.is_term(line) and entry_key(_WS.sub(" ", line).strip()) in keys)
        ]
        if len(kept) == len(vf.lines):
            return False
        vf.lines = kept
        atomic_write_bytes(path, vf.encode())
    return True
