"""Deterministic spelling fixes for every transcript: replacements and the custom words.

Whisper only gets the vocabulary as a *hint*, and the optional LLM cleanup isn't always on,
so a name Whisper already knows another spelling of comes out its way ("Sofia" -> "Sophia").
This pass runs on the final text of every dictation (after the cleanup, if any) and makes
the owner's words stick. It is a port of the iPhone app's ``VocabFixer`` (same rules, same
tests), plus replacements:

1. **Replacements** (``say -> write`` lines in vocab.txt): the spoken words, matched as whole
   words ignoring case, become the written text ("omw" -> "on my way", "slash consolidate"
   -> "/consolidate"). A custom word with a symbol gets one for free: "/consolidate" also
   matches "slash consolidate", "C#" "C sharp", "Node.js" "node dot js" (:func:`spoken_rule`).
2. **Same spelling**: case-insensitive matches of a term become its spelling ("better
   shell" -> "Better Shell", "shout-about" -> "ShoutAbout").
3. **Sound-alike**: a run of words that sounds like a term and is spelled almost like it
   becomes the term ("Sophia" -> "Sofia", "Shout Aboot" -> "ShoutAbout"). Built to leave
   ordinary sentences alone:

   - **Shape**: as many words as the term (camelCase counts as words), only spaces or
     hyphens between them, and about the same number of letters (within 30%, at least 2).
   - **Each word** is spelled like the term's word (an "anchor") or sounds like it: both
     3+ letters, the same phonetic key (:func:`sound_key`), at most 2 edits apart and no
     more than 40% of the longer word. Terms under 4 letters are never matched by sound.
   - **Evidence**, for every sound-alike word: Whisper capitalised it (it heard a name), or
     another word of the term matched exactly and neither is common English.
   - **Common English words** ("sofa", "shoot", "cloud") only become a name or brand (a
     term with a capital) when capitalised mid-sentence: "I sat on the sofa" stays.
   - **Longest term wins**, words already spelled like a term are never rewritten into
     another term, and each word is rewritten once.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

# Between the words of a term or a spoken phrase: spaces or hyphens (Whisper writes
# "shout-about", "Better-Shell"). Replacements also allow a comma ("Slash, consolidate").
_TERM_GAP = r"[\s-]*"
_SAY_GAP = r"[\s,-]+"
_NOT_AFTER_WORD = r"(?<![^\W_])"  # not preceded by a letter or digit
_NOT_BEFORE_WORD = r"(?![^\W_])"

# How a symbol in a custom word is said. Only symbols whose spoken word is rarely meant
# literally next to the term ("at", "and" are, so "@" and "&" are left out).
SYMBOL_WORDS: dict[str, tuple[str, ...]] = {
    "/": ("slash", "forward slash"),
    "\\": ("backslash",),
    ".": ("dot",),
    "_": ("underscore",),
    "#": ("hash", "hashtag", "sharp", "pound"),
    "+": ("plus",),
}

MIN_TERM_LETTERS = 4  # the smallest term the sound-alike pass considers
MIN_WORD_LETTERS = 3  # the shortest word that may differ from the term's spelling


# --------------------------------------------------------------------------- public


@dataclass(frozen=True)
class Fixer:
    """The compiled fixes for one vocabulary. Build with :func:`build_fixer` (cached)."""

    terms: tuple[str, ...]
    rules: tuple[tuple[re.Pattern[str], str], ...]
    spellings: tuple[tuple[re.Pattern[str], str], ...]
    candidates: tuple[_Candidate, ...] = field(repr=False)

    def apply(self, text: str) -> str:
        if not text:
            return text
        for regex, written in self.rules:
            text = regex.sub(lambda m, w=written: _match_case(m.group(0), w), text)
        for regex, term in self.spellings:
            text = regex.sub(lambda _m, t=term: t, text)
        return _fix_sound_alikes(text, self.candidates)


def apply_fixes(
    text: str, terms: Sequence[str], replacements: Sequence[tuple[str, str]] = ()
) -> str:
    """``text`` with the replacements and the custom words' spellings applied."""
    return build_fixer(tuple(terms), tuple(replacements)).apply(text)


@functools.lru_cache(maxsize=8)
def build_fixer(terms: tuple[str, ...], replacements: tuple[tuple[str, str], ...] = ()) -> Fixer:
    cleaned = tuple(t.strip() for t in terms if t and t.strip())
    rules: list[tuple[int, re.Pattern[str], str]] = []
    for say, write in replacements:
        regex = say_pattern(say)
        if regex is not None and write.strip():
            rules.append((len(say), regex, write.strip()))
    for term in cleaned:
        regex = spoken_rule(term)
        if regex is not None:
            rules.append((len(term), regex, term))
    rules.sort(key=lambda r: -r[0])  # longest first: "c sharp code" before "c sharp"
    spellings = []
    for term in sorted(cleaned, key=len, reverse=True):  # "Better Shell Pro" first
        regex = term_pattern(term)
        if regex is not None:
            spellings.append((regex, term))
    candidates = [c for c in map(_Candidate.make, cleaned) if c is not None]
    candidates.sort(key=lambda c: (c.word_count, c.letters), reverse=True)
    return Fixer(
        terms=cleaned,
        rules=tuple((r, w) for _n, r, w in rules),
        spellings=tuple(spellings),
        candidates=tuple(candidates),
    )


@dataclass(frozen=True)
class Teaching:
    """What **Fix a word** saves for "``wrong`` should be ``right``", and the fixed text."""

    entry: str  # the vocab.txt line: the word itself, or "wrong -> right"
    is_word: bool
    fixed_text: str


def teach_fix(text: str, wrong: str, right: str) -> Teaching:
    """Learn from a correction in a dictation: "Sophia" should be "Sofia".

    A plain custom word is preferred: it also catches other spellings, and it never
    rewrites an everyday word on its own. It is used when the word alone already turns at
    least one ``wrong`` in ``text`` into ``right`` (other capitals or spacing, a sound-alike
    name, a symbol said aloud). Anything else becomes a replacement, "wrong -> right".
    """
    wrong, right = " ".join(wrong.split()), " ".join(right.split())
    finder = say_pattern(wrong)
    context = text if finder is not None and finder.search(text) else f"so {wrong}"
    as_word = apply_fixes(context, [right])
    if as_word != context and as_word.count(right) > context.count(right):
        return Teaching(right, True, apply_fixes(text, [right]))
    return Teaching(f"{wrong} -> {right}", False, apply_fixes(text, [], [(wrong, right)]))


def say_pattern(say: str) -> re.Pattern[str] | None:
    """Whole-word, case-insensitive match of a spoken phrase ("omw", "slash consolidate")."""
    words = say.replace("-", " ").split()
    if not words:
        return None
    body = _SAY_GAP.join(map(re.escape, words))
    return re.compile(_NOT_AFTER_WORD + body + _NOT_BEFORE_WORD, re.IGNORECASE)


def spoken_rule(term: str) -> re.Pattern[str] | None:
    """For a custom word with a symbol ("/consolidate", "C#"), a match of it said aloud
    ("slash consolidate", "C sharp"); None for a term without one.

    Each symbol may also come through as itself ("/ consolidate")."""
    tokens = re.findall(r"[^\W_]+|[^\w\s]|_", term)
    if not any(t in SYMBOL_WORDS for t in tokens) or not any(t.isalnum() for t in tokens):
        return None
    parts = []
    for token in tokens:
        if token in SYMBOL_WORDS:
            spoken = [r"\s+".join(map(re.escape, w.split())) for w in SYMBOL_WORDS[token]]
            parts.append("(?:" + "|".join([*spoken, re.escape(token)]) + ")")
        elif token.isalnum():
            parts.append(re.escape(token))
        else:
            parts.append(re.escape(token) + "?")  # other punctuation is optional
    body = _SAY_GAP.join(parts)
    # The symbol said aloud must be a word of its own: "dot" in "dotnet" isn't ".".
    return re.compile(r"(?<![^\W_])" + body + _NOT_BEFORE_WORD, re.IGNORECASE)


def term_pattern(term: str) -> re.Pattern[str] | None:
    """The term's words (camelCase split too, so "ShoutAbout" matches "shout about")
    separated by optional spaces/hyphens, bounded by non-alphanumerics."""
    words = [w for part in re.split(r"[\s-]+", term) if part for w in split_camel_case(part)]
    if not words:
        return None
    body = _TERM_GAP.join(map(re.escape, words))
    return re.compile(_NOT_AFTER_WORD + body + _NOT_BEFORE_WORD, re.IGNORECASE)


def split_camel_case(word: str) -> list[str]:
    """ "ShoutAbout" -> ["Shout", "About"]; "GPU" and "hello" stay whole."""
    parts: list[str] = []
    current = ""
    previous = ""
    for ch in word:
        if previous.islower() and ch.isupper() and current:
            parts.append(current)
            current = ""
        current += ch
        previous = ch
    if current:
        parts.append(current)
    return parts


def _match_case(spoken: str, written: str) -> str:
    """A lowercase replacement that starts a sentence keeps the capital ("Omw" -> "On my
    way"); one with a capital of its own ("iPhone", "Sofia") is written as given."""
    if spoken[:1].isupper() and written[:1].islower() and not any(ch.isupper() for ch in written):
        return written[0].upper() + written[1:]
    return written


# ---------------------------------------------------------------------- sound-alike


def fold(text: str) -> str:
    """Lowercased with diacritics and width folded: how spellings are compared."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def sound_key(word: str) -> str:
    """A simplified Metaphone key (after Lawrence Philips, 1990). Words that sound alike in
    English usually share it: "Sophia"/"Sofia" -> "SF", "Better"/"Bettor" -> "BTR". Only
    Latin letters count, so the key is "" for a number or a word in another script. The
    rules are the iPhone app's ``SoundAlike.key``, letter for letter."""
    w = [ch for ch in fold(word).upper() if "A" <= ch <= "Z"]
    if not w:
        return ""
    start = "".join(w[:2])
    if start in ("KN", "GN", "PN", "WR", "AE"):
        w.pop(0)
    elif w[0] == "X":
        w[0] = "S"
    elif start == "WH":
        w.pop(1)

    def at(i: int) -> str:
        return w[i] if 0 <= i < len(w) else ""

    def vowel(ch: str) -> bool:
        return ch != "" and ch in "AEIOU"

    out: list[str] = []
    i = 0
    n = len(w)
    while i < n:
        c, prev, nxt = w[i], at(i - 1), at(i + 1)
        if c != "C" and c == prev:  # doubled letters sound once, except C ("accent")
            i += 1
            continue
        if c in "AEIOU":
            if i == 0:
                out.append("A")
        elif c == "B":
            if not (prev == "M" and i == n - 1):
                out.append("B")
        elif c == "C":
            if nxt == "I" and at(i + 2) == "A":
                out.append("X")
            elif nxt == "H":
                out.append("K" if prev == "S" else "X")
                i += 1
            elif nxt in ("E", "I", "Y"):
                if prev != "S":
                    out.append("S")
            elif nxt == "K":
                out.append("K")
                i += 1
            else:
                out.append("K")
        elif c == "D":
            if nxt == "G" and at(i + 2) in ("E", "I", "Y"):
                out.append("J")
                i += 2
            else:
                out.append("T")
        elif c == "G":
            if nxt == "H":
                if i == 0 or vowel(at(i + 2)):
                    out.append("K")
                i += 1
            elif nxt == "N" and (
                i + 2 == n or (i + 4 == n and at(i + 2) == "E" and at(i + 3) == "D")
            ):
                pass  # silent: "sign", "signed"
            elif nxt in ("E", "I", "Y"):
                out.append("J")
            else:
                out.append("K")
        elif c == "H":
            if not (vowel(prev) and not vowel(nxt)):
                out.append("H")
        elif c == "K":
            if prev != "C":
                out.append("K")
        elif c == "P":
            if nxt == "H":
                out.append("F")
                i += 1
            else:
                out.append("P")
        elif c == "Q":
            out.append("K")
        elif c == "S":
            if nxt == "H":
                out.append("X")
                i += 1
            elif nxt == "I" and at(i + 2) in ("O", "A"):
                out.append("X")
            else:
                out.append("S")
        elif c == "T":
            if nxt == "I" and at(i + 2) in ("O", "A"):
                out.append("X")
            elif nxt == "H":
                out.append("0")
                i += 1
            elif not (nxt == "C" and at(i + 2) == "H"):
                out.append("T")
        elif c == "V":
            out.append("F")
        elif c in "WY":
            if vowel(nxt):
                out.append(c)
        elif c == "X":
            out.append("KS")
        elif c == "Z":
            out.append("S")
        else:
            out.append(c)  # F, J, L, M, N, R
        i += 1
    return "".join(out)


def distance(a: str, b: str) -> int:
    """Levenshtein distance (insertions, deletions, substitutions)."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def is_common_word(word: str) -> bool:
    """True for a very common English word ("sofa", "shoot", "cloud")."""
    return fold(word) in COMMON_WORDS


@dataclass
class _Word:
    start: int
    end: int
    folded: str
    key: str
    capitalised: bool
    letters_only: bool
    sentence_start: bool  # first word, or the first after . ! ? or a line break
    joins_previous: bool  # only spaces or hyphens since the previous word
    locked: bool = False


_WORD_RUN = re.compile(r"[^\W_]+")


def _split_words(text: str) -> list[_Word]:
    words: list[_Word] = []
    gap_start = 0
    for m in _WORD_RUN.finditer(text):
        raw, gap = m.group(0), text[gap_start : m.start()]
        words.append(
            _Word(
                start=m.start(),
                end=m.end(),
                folded=fold(raw),
                key=sound_key(raw),
                capitalised=raw[0].isupper(),
                letters_only=raw.isalpha(),
                sentence_start=not words or any(ch in ".!?\n\r" for ch in gap),
                joins_previous=bool(words)
                and all(ch == "-" or (ch.isspace() and ch not in "\n\r") for ch in gap),
            )
        )
        gap_start = m.end()
    return words


@dataclass(frozen=True)
class _Part:
    folded: str
    key: str
    letters_only: bool


@dataclass(frozen=True)
class _Candidate:
    term: str
    shapes: tuple[tuple[_Part, ...], ...]
    letters: int
    name_like: bool  # has a capital letter: a name or a brand

    @property
    def word_count(self) -> int:
        return max(len(s) for s in self.shapes)

    @staticmethod
    def make(term: str) -> _Candidate | None:
        written = _WORD_RUN.findall(term)
        if not written:
            return None
        spoken = [p for w in written for p in split_camel_case(w)]

        def parts(words: list[str]) -> tuple[_Part, ...]:
            return tuple(_Part(fold(w), sound_key(w), w.isalpha()) for w in words)

        shapes = (parts(written),)
        if len(spoken) != len(written):  # camelCase: as spoken first, then as written
            shapes = (parts(spoken), parts(written))
        return _Candidate(
            term=term,
            shapes=shapes,
            letters=sum(len(fold(w)) for w in written),
            name_like=any(ch.isupper() for ch in term),
        )


def _windows(count: int, words: list[_Word]):
    """Starts of runs of ``count`` unlocked words joined only by spaces or hyphens; the
    caller ``send``s True after a match so the scan continues after the run."""
    start = 0
    while start + count <= len(words):
        run = range(start, start + count)
        free = all(not words[i].locked for i in run)
        joined = all(words[i].joins_previous for i in run[1:])
        matched = (yield start) if free and joined else False
        start += count if matched else 1


def _scan(count: int, words: list[_Word], match) -> None:
    gen = _windows(count, words)
    try:
        start = next(gen)
        while True:
            start = gen.send(bool(match(start)))
    except StopIteration:
        pass


def _sounds_alike(word: _Word, part: _Part) -> bool:
    if not (
        word.letters_only
        and part.letters_only
        and len(word.folded) >= MIN_WORD_LETTERS
        and len(part.folded) >= MIN_WORD_LETTERS
        and part.key
        and word.key == part.key
    ):
        return False
    edits = distance(word.folded, part.folded)
    return edits <= 2 and edits * 5 <= max(len(word.folded), len(part.folded)) * 2


def _matches(window: list[_Word], shape: tuple[_Part, ...], cand: _Candidate) -> bool:
    anchors: list[_Word] = []
    alikes: list[_Word] = []
    for word, part in zip(window, shape, strict=True):
        if word.folded == part.folded:
            anchors.append(word)
        elif _sounds_alike(word, part):
            alikes.append(word)
        else:
            return False
    if not alikes:
        return False  # spelled right already (the same-spelling pass)
    letters = sum(len(w.folded) for w in window)
    if abs(letters - cand.letters) > max(2, cand.letters * 3 // 10):
        return False
    distinctive_anchor = any(not is_common_word(w.folded) for w in anchors)
    for word in alikes:
        if is_common_word(word.folded):
            if not (cand.name_like and word.capitalised and not word.sentence_start):
                return False
        elif not (word.capitalised or distinctive_anchor):
            return False
    return True


def _fix_sound_alikes(text: str, candidates: Sequence[_Candidate]) -> str:
    words = _split_words(text)
    if not words or not candidates:
        return text

    # Words already spelled like a term are locked first, so "Sophia" stays "Sophia" when
    # both "Sophia" and "Sofia" are custom words.
    for cand in candidates:
        for shape in cand.shapes:

            def lock(start: int, shape=shape) -> bool:
                window = words[start : start + len(shape)]
                if any(w.folded != p.folded for w, p in zip(window, shape, strict=True)):
                    return False
                for w in window:
                    w.locked = True
                return True

            _scan(len(shape), words, lock)

    replacements: list[tuple[int, int, str]] = []
    for cand in candidates:
        if cand.letters < MIN_TERM_LETTERS:
            continue
        for shape in cand.shapes:

            def replace(start: int, shape=shape, cand=cand) -> bool:
                window = words[start : start + len(shape)]
                if not _matches(window, shape, cand):
                    return False
                for w in window:
                    w.locked = True
                first = window[0]
                spelling = cand.term
                # A lowercase term ("faster-whisper") that starts a sentence keeps the capital.
                if not cand.name_like and first.sentence_start and first.capitalised:
                    spelling = spelling[:1].upper() + spelling[1:]
                replacements.append((first.start, window[-1].end, spelling))
                return True

            _scan(len(shape), words, replace)
    if not replacements:
        return text
    out, cursor = [], 0
    for start, end, spelling in sorted(replacements):
        out += [text[cursor:start], spelling]
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


# Everyday English: function words, the most frequent verbs, nouns, adjectives and adverbs,
# plus household words that sound like popular names ("sofa" ~ Sofia, "cloud" ~ Claude).
# The same list as the iPhone app's SoundAlike.commonWords.
_COMMON_WORD_LIST = """
a about above across act actually add after afternoon again against age ago agree ahead
air all allow almost alone along already also always am among amount an and angry animal
another answer any anyone anything anyway anywhere apple appear are area arm around
arrive art as ask at attack aunt autumn away baby back bad bag ball bank bar base bath
be beach bear beat beautiful became because become bed bedroom been beer before began
begin behind being believe bell belong below belt bench best bet better between big bike
bill bird birthday bit black blank block blood blow blue board boat body bone book born
borrow boss both bottle bottom bought bowl box boy brain bread break breakfast bridge
bright bring broke brother brought brown build building burn bus business busy but
butter buy by cake call came camera can car card care careful carry case cat catch cause
cell center chair chance change charge cheap check cheese chicken child children choose
church city class clean clear climb clock close cloth clothes cloud club coat code
coffee cold collect college color come common company complete computer cook cool copy
corner cost could count country couple course court cousin cover cow crazy cream cross
cry cup cut dad dance dark data date daughter day dead deal dear decide deep desk did
die different dinner direction dirty do doctor does dog doing dollar done door double
down draw dream dress drink drive drop dry during each ear early earth easy eat edge egg
eight either else email empty end enough enter even evening event ever every everyone
everything exactly example except excuse expect eye face fact fall family far farm fast
father favorite fear feel feet fell felt few field fight file fill final find fine
finger finish fire first fish fit five fix floor flower fly follow food foot for forget
fork form forward four free fresh friend from front fruit full fun funny game garden gas
gave get gift girl give glad glass go god going gold gone good got grade great green
grew ground group grow guess guy had hair half hall hand hang happen happy hard has hat
hate have he head hear heard heart heat heavy hello help her here hers high hill him his
history hit hold hole holiday home hope horse hospital hot hotel hour house how however
huge human hundred hungry hurry hurt husband i ice idea if important in inside instead
interest into iron is island it its job join joke juice jump just keep key kick kid kill
kind king kitchen knew knife know lady lake land large last late later laugh law lay
lead learn least leave left leg less lesson let letter level lie life light like line
list listen little live long look lose loss lost lot loud love low luck lunch machine
made mail main make man many map mark market matter may maybe me meal mean meat meet
meeting member men message met middle might mile milk mind mine minute miss mister mom
moment money month moon more morning most mother mountain mouth move movie much music
must my name near neck need never new news next nice night nine no noise none noon nor
north nose not note nothing notice now number of off offer office often oh oil ok okay
old on once one only open or orange order other our out outside over own page paid pain
paint pair paper parent park part party pass past pay pen people perfect perhaps person
phone photo pick picture piece pink place plan plane plant plate play please plus pocket
point police poor pop post pound power present pretty price print problem project pull
push put question quick quiet quite race radio rain raise ran rather reach read ready
real really reason red remember rent rest return rice rich ride right ring rise river
road rock room round row rule run sad safe said sale salt same sat save saw say school
sea season seat second see seem seen sell send sense sent set seven several shall shape
share she sheet shirt shoe shoot shop short should shout show shower shut sick side sign
silly simple since sing sister sit six size skin sky sleep slow small smell smile snow
so soap sofa soft some someone something sometimes son song soon sorry sort sound soup
south space speak special spend sport spring square stage stand star start station stay
step still stone stop store story street strong student study stuff such sugar suit
summer sun supper sure surprise sweet swim table take talk tall taste taxi tea teach
team tell ten test text than thank thanks that the their them then there these they
thing think third this those though thought three through throw ticket tie time tired to
today together told tomorrow tonight too took tooth top total touch toward town toy
train travel tree trip trouble truck true try turn twelve twenty two type uncle under
understand until up upon us use used usual very view visit voice wait wake walk wall
want war warm was wash watch water way we wear weather wedding week weekend weight
welcome well went were west wet what wheel when where which while white who whole why
wide wife will win window wine winter wish with without woman women won wonder wood word
work world worry would write wrong yard yeah year yellow yes yesterday yet you young
your
"""
COMMON_WORDS: frozenset[str] = frozenset(_COMMON_WORD_LIST.split())
