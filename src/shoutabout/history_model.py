"""The History window, as plain logic (no Tk): what it lists and what its buttons do.

It lists recent dictations from ``history.jsonl``, newest first: when, where it came from
("This PC" or the phone's name), and the text, each with **Copy**. Above them, recordings
that couldn't be written out (:mod:`failed_audio`) with **Try again** and **Delete**. A
search box filters by words. **Clear history** deletes every record and kept recording
(after a confirmation in the window). History is off by default (``[history] enabled =
false``): then the window says so in one line and offers to turn it on (Settings has the
same switch). Everything stays on this PC.

:mod:`history_ui` only draws this; tests drive it with fake services.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from shoutabout.app_services import AppServices

log = logging.getLogger(__name__)

READ_LIMIT = 2000  # history lines read (newest); older ones stay in the file
SHOWN_LIMIT = 150  # cards drawn at once; searching reaches the rest of READ_LIMIT

TITLE = "History"
SUBTITLE = "Saved only on this PC."
OFF_TITLE = "History is off."
# Turning it on also keeps recordings that fail (for Try again): say so, briefly.
OFF_TEXT = "Turn it on to keep what you dictate, and recordings that fail."
EMPTY_TEXT = "Nothing yet."
NO_MATCH_TEXT = "Nothing matches that search."
CLEARED = "History cleared."
COPIED = "Copied."
RETRIED = "Done. It's at the top of the list."
RETRY_NEEDS_HUB = "Start ShoutAbout first, then try again."
FAILED_TITLE = "Couldn't write these out"


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
    """ "Today 14:03", "Yesterday 9:12", "3 Oct 14:03", "3 Oct 2025"."""
    when = _parse(ts)
    if when is None:
        return ""
    now = now or datetime.now().astimezone()
    local = when.astimezone(now.tzinfo)
    clock = f"{local.hour}:{local.minute:02d}"
    if local.date() == now.date():
        return f"Today {clock}"
    if local.date() == (now - timedelta(days=1)).date():
        return f"Yesterday {clock}"
    if local.year == now.year:
        return f"{local.day} {local:%b} {clock}"
    return f"{local.day} {local:%b} {local.year}"


def source_label(record: dict[str, Any]) -> str:
    """Where a dictation came from: "This PC", the phone's name, or "Phone"."""
    route = str(record.get("route") or "")
    if route in ("local", "cli"):
        return "This PC"
    name = record.get("source")
    if isinstance(name, str) and name.strip() and name != "phone":
        return name.strip()
    return "Phone"


def length_label(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ""
    s = round(seconds)
    if s < 60:
        return f"{s} s"
    return f"{s // 60} min {s % 60:02d} s"


def note_for(record: dict[str, Any]) -> str:
    if record.get("recovered"):
        return "Written out later"
    if record.get("target") == "pc" and not record.get("pasted") and record.get("warning"):
        return "Not pasted"
    return ""


def record_text(record: dict[str, Any]) -> str:
    text = record.get("text") or record.get("raw_text") or ""
    return " ".join(str(text).split())


def matches(text: str, where: str, query: str) -> bool:
    """Every word of the search appears in the text or the source (any case)."""
    words = query.casefold().split()
    hay = f"{text} {where}".casefold()
    return all(w in hay for w in words)


# --------------------------------------------------------------------- the view


@dataclass(frozen=True)
class Entry:
    key: str  # stable for this read: the record id (or its position)
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
class HistoryView:
    enabled: bool = True
    entries: list[Entry] = field(default_factory=list)
    failed: list[FailedEntry] = field(default_factory=list)
    total: int = 0  # entries matching the search (may be more than shown)
    limited: bool = False  # more matched than are shown
    query: str = ""
    empty_text: str = ""
    can_retry: bool = False
    loading: bool = True


def build_entries(records: list[dict[str, Any]]) -> list[Entry]:
    """Records (newest first) -> entries; ones with no text ("nothing heard") are skipped."""
    out = []
    for i, record in enumerate(records):
        text = record_text(record)
        if not text:
            continue
        key = str(record.get("id") or f"#{i}")
        out.append(
            Entry(key, str(record.get("ts") or ""), source_label(record), text, note_for(record))
        )
    return out


class HistoryModel:
    def __init__(self, services: AppServices) -> None:
        self.svc = services
        self.enabled = self._read_enabled()  # config.toml: cheap, so the first draw is right
        self.records: list[Entry] = []  # all read entries, newest first (when = raw ts)
        self.failed_items: list[Any] = []
        self.loaded = False
        self.message = ""
        self.error = False
        self.busy: set[str] = set()  # failed ids being tried again

    # -------------------------------------------------------------- reading

    def _read_enabled(self) -> bool:
        try:
            return bool(self.svc.history_enabled())
        except Exception:
            log.exception("couldn't read [history] enabled")
            return True  # show whatever the file holds rather than hide it

    def load(self) -> None:
        """Read the history file and the kept recordings (call off the UI thread)."""
        self.enabled = self._read_enabled()
        try:
            self.records = build_entries(self.svc.read_history(READ_LIMIT))
        except Exception:
            log.exception("couldn't read the history")
            self.records = []
            self.message, self.error = "Couldn't read your history.", True
        try:
            self.failed_items = list(self.svc.failed_recordings().list())
        except Exception:
            log.exception("couldn't list the kept recordings")
            self.failed_items = []
        self.loaded = True

    def view(self, query: str = "", now: datetime | None = None) -> HistoryView:
        query = " ".join(query.split())
        matching = [e for e in self.records if matches(e.text, e.source, query)]
        shown = matching[:SHOWN_LIMIT]
        entries = [Entry(e.key, when_label(e.when, now), e.source, e.text, e.note) for e in shown]
        failed = [
            FailedEntry(
                item.id,
                when_label(item.ts, now),
                source_label({"route": item.route, "source": item.source}),
                length_label(item.seconds),
                item.error,
            )
            for item in self.failed_items
        ]
        if not self.records:
            empty = EMPTY_TEXT
        elif not matching:
            empty = NO_MATCH_TEXT
        else:
            empty = ""
        return HistoryView(
            enabled=self.enabled,
            entries=entries,
            failed=failed,
            total=len(matching),
            limited=len(matching) > len(shown),
            query=query,
            empty_text=empty,
            can_retry=self.svc.retry_failed is not None,
            loading=not self.loaded,
        )

    def text_of(self, key: str) -> str | None:
        for e in self.records:
            if e.key == key:
                return e.text
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
        try:
            ok = bool(self.svc.failed_recordings().remove(item_id))
        except Exception:
            log.exception("couldn't delete a kept recording")
            ok = False
        self.message, self.error = ("Deleted.", False) if ok else ("Couldn't delete it.", True)
        return ok

    def clear(self) -> bool:
        try:
            self.svc.clear_history()
        except Exception:
            log.exception("couldn't clear the history")
            self.message, self.error = "Couldn't clear it. Is the file open somewhere?", True
            return False
        self.records, self.failed_items = [], []
        self.message, self.error = CLEARED, False
        return True

    def turn_on(self) -> bool:
        from shoutabout.config_edit import ConfigEditError

        try:
            restart = self.svc.save_settings({"history": {"enabled": True}})
        except (ConfigEditError, OSError) as exc:
            log.warning("couldn't turn history on: %s", exc)
            self.message, self.error = "Couldn't save that. Is config.toml valid?", True
            return False
        self.enabled = True
        if restart:
            self.message = "History is on. Restart ShoutAbout to start keeping it."
        else:
            self.message = "History is on."
        self.error = False
        return True
