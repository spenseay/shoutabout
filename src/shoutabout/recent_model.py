"""The Recent page, as plain logic (no Tk): what it lists and what its buttons do.

It lists the last few dictations meant for this computer (:mod:`shoutabout.recent`, kept in
the hub's memory only), newest first: when, where it came from ("This PC" or the phone's
name), and the text, each with **Copy**, so text that landed in the wrong spot is never
lost, and **Fix a word**: "Sophia" should be "Sofia" teaches it for next time (a custom word,
or a replacement when it can't be told from the sound; :func:`shoutabout.vocabfix.teach_fix`)
and corrects the text shown. Above them, recordings the speech engine couldn't write out,
with **Try again** and **Delete**. Nothing is kept on disk, nothing to search or clear.

The page watches :meth:`RecentModel.stamp` (cheap) and reloads when it changes, so a new
dictation shows up by itself. :mod:`recent_ui` only draws this; tests drive it with fakes.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from shoutabout.app_services import AppServices
from shoutabout.recent import FailedItem, RecentItem, items_from

log = logging.getLogger(__name__)

TITLE = "Recent"
EMPTY_TEXT = "Nothing yet."
COPIED = "Copied."
RETRIED = "Done. It's at the top."
RETRY_NEEDS_HUB = "Start ShoutAbout first, then try again."
FAILED_TITLE = "Couldn't write these out"


def fix_message(wrong: str, right: str, status: str, existing: str = "") -> str:
    """Status line after **Fix a word**."""
    from shoutabout.vocab import split_replacement

    pair = split_replacement(existing) if status == "exists" else None
    if pair and pair[1] != right:  # one replacement per spoken phrase
        return f'"{pair[0]}" already turns into "{pair[1]}". Change it on Teach words.'
    return f'Saved. "{wrong}" will come out "{right}" from now on.'


# --------------------------------------------------------------------- pure copy


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.astimezone()


def when_label(ts: Any, now: datetime | None = None) -> str:
    """ "14:03", "Yesterday 9:12", "3 Oct 14:03"."""
    when = _parse(ts)
    if when is None:
        return ""
    now = now or datetime.now().astimezone()
    local = when.astimezone(now.tzinfo)
    clock = f"{local.hour}:{local.minute:02d}"
    if local.date() == now.date():
        return clock
    if local.date() == (now - timedelta(days=1)).date():
        return f"Yesterday {clock}"
    return f"{local.day} {local:%b} {clock}"


def source_label(route: str, source: str | None, platform: str | None = None) -> str:
    """Where a dictation came from: "This PC" ("This Mac"), the phone's name, or "Phone"."""
    if route in ("local", "cli"):
        return "This Mac" if (platform or sys.platform) == "darwin" else "This PC"
    if isinstance(source, str) and source.strip() and source != "phone":
        return source.strip()
    return "Phone"


def length_label(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ""
    s = round(seconds)
    if s < 60:
        return f"{s} s"
    return f"{s // 60} min {s % 60:02d} s"


def note_for(item: RecentItem) -> str:
    if item.recovered:
        return "Written out later"
    if not item.pasted and item.warning:
        return "Not pasted"
    return ""


# --------------------------------------------------------------------- the view


@dataclass(frozen=True)
class Entry:
    key: str  # the item's id: stable while ShoutAbout runs
    when: str
    source: str
    text: str
    note: str = ""


@dataclass(frozen=True)
class FailedEntry:
    id: str
    when: str
    source: str
    length: str
    error: str


@dataclass
class RecentView:
    entries: list[Entry] = field(default_factory=list)
    failed: list[FailedEntry] = field(default_factory=list)
    empty_text: str = ""
    can_retry: bool = False
    loading: bool = True


class RecentModel:
    def __init__(self, services: AppServices) -> None:
        self.svc = services
        self.items: list[RecentItem] = []  # newest first
        self.failed_items: list[FailedItem] = []
        self.loaded = False
        self.message = ""
        self.error = False
        self.busy: set[str] = set()  # failed ids being tried again
        self.fixed: dict[str, str] = {}  # item id -> its text after Fix a word

    # -------------------------------------------------------------- reading

    def stamp(self) -> Any:
        """Changes whenever the hub's Recent does (cheap: call it often, on the UI thread)."""
        recent = self.svc.recent
        if recent is None:
            return None
        try:
            return recent.version
        except Exception:
            return None

    def load(self) -> None:
        """Read the hub's Recent (call off the UI thread: on a Mac it asks the hub)."""
        recent = self.svc.recent
        try:
            snapshot = recent.snapshot() if recent is not None else None
        except Exception:
            log.exception("couldn't read Recent")
            snapshot = None
        self.items, self.failed_items = items_from(snapshot)
        self.loaded = True

    def view(self, now: datetime | None = None) -> RecentView:
        entries = [
            Entry(
                i.id,
                when_label(i.ts, now),
                source_label(i.route, i.source),
                self.fixed.get(i.id, i.text),
                note_for(i),
            )
            for i in self.items
        ]
        failed = [
            FailedEntry(
                f.id,
                when_label(f.ts, now),
                source_label(f.route, f.source),
                length_label(f.seconds),
                f.error,
            )
            for f in self.failed_items
        ]
        return RecentView(
            entries=entries,
            failed=failed,
            empty_text=EMPTY_TEXT if not entries and not failed else "",
            can_retry=self.svc.retry_failed is not None,
            loading=not self.loaded,
        )

    def text_of(self, key: str) -> str | None:
        for item in self.items:
            if item.id == key:
                return self.fixed.get(key, item.text)
        return None

    # -------------------------------------------------------------- actions

    def copy(self, key: str) -> bool:
        text = self.text_of(key)
        if text is None or self.svc.copy_text is None:
            self.message, self.error = "Couldn't copy that.", True
            return False
        try:
            self.svc.copy_text(text)
        except Exception:
            log.exception("couldn't copy to the clipboard")
            self.message, self.error = "Couldn't copy that. Try again.", True
            return False
        self.message, self.error = COPIED, False
        return True

    def fix_word(self, key: str, wrong: str, right: str) -> str | None:
        """**Fix a word**: teach "``wrong`` should be ``right``" (vocab.txt, which the hub
        reloads by itself) and correct this dictation's text. Returns the corrected text,
        or None (``message`` says why). Call off the UI thread (the vocab.txt lock)."""
        from shoutabout.vocab import add_terms
        from shoutabout.vocabfix import teach_fix

        text = self.text_of(key)
        wrong, right = " ".join(wrong.split()), " ".join(right.split())
        if text is None:
            self.message, self.error = "That dictation is gone.", True
            return None
        if not wrong or not right:
            self.message, self.error = "Type the wrong word and how it should be.", True
            return None
        if wrong == right:
            self.message, self.error = "That's the same. Type how it should be.", True
            return None
        teaching = teach_fix(text, wrong, right)
        try:
            (result,) = add_terms(self.svc.paths.vocab_file, [teaching.entry])
        except OSError as exc:
            log.warning("couldn't update vocab.txt: %s", exc)
            self.message, self.error = "Couldn't save that. Try again.", True
            return None
        if result.status == "invalid":
            self.message, self.error = f"Can't save that: {result.reason}.", True
            return None
        self.message = fix_message(wrong, right, result.status, result.term)
        self.error = False
        if not self.message.startswith("Saved"):
            return None
        self.fixed[key] = teaching.fixed_text
        return teaching.fixed_text

    def retry(self, item_id: str) -> bool:
        """Transcribe a kept recording again (slow: call off the UI thread)."""
        if self.svc.retry_failed is None:
            self.message, self.error = RETRY_NEEDS_HUB, True
            return False
        self.busy.add(item_id)
        try:
            text = self.svc.retry_failed(item_id)
        except Exception as exc:
            log.warning("trying a kept recording again failed: %s", exc)
            detail = str(exc).strip() or "something went wrong"
            self.message, self.error = f"Still couldn't write it out: {detail}", True
            return False
        finally:
            self.busy.discard(item_id)
        if not text:
            self.message, self.error = "No words found in that recording.", True
            return False
        self.message, self.error = RETRIED, False
        return True

    def delete_failed(self, item_id: str) -> bool:
        recent = self.svc.recent
        try:
            ok = bool(recent.remove_failed(item_id)) if recent is not None else False
        except Exception:
            log.exception("couldn't delete a kept recording")
            ok = False
        self.message, self.error = ("Deleted.", False) if ok else ("Couldn't delete it.", True)
        return ok
