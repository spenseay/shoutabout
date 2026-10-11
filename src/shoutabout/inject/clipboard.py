"""Windows clipboard access via ctypes (no pywin32).

Ownership: ``SetClipboardData`` fails after ``OpenClipboard(NULL)`` + ``EmptyClipboard``
(no owner), and a clipboard-owner window whose thread doesn't pump messages can stall
other apps (``WM_DESTROYCLIPBOARD`` is *sent* to the owner). So every operation creates a
short-lived hidden message-only window, opens the clipboard with it (retrying while
another app holds the clipboard open), does its work, closes the clipboard and destroys
the window. That makes these functions safe to call from any thread, e.g. a uvicorn
worker or the restore timer.

Clipboard history / cloud sync: when ``exclude_from_history`` is set, the three
documented marker formats are written in the *same* open session as the data, so the
history service (which listens asynchronously) never sees the item without them.

Contents are never logged.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache

from shoutabout.inject import win32 as w

log = logging.getLogger(__name__)

EXCLUDE_FORMAT_NAME = "ExcludeClipboardContentFromMonitorProcessing"
HISTORY_FORMAT_NAME = "CanIncludeInClipboardHistory"
CLOUD_FORMAT_NAME = "CanUploadToCloudClipboard"
EXCLUSION_FORMAT_NAMES = (EXCLUDE_FORMAT_NAME, HISTORY_FORMAT_NAME, CLOUD_FORMAT_NAME)
_DWORD_ZERO = struct.pack("<I", 0)

OPEN_ATTEMPTS = 10
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
# Reading a delay-rendered format makes its owner render it (up to ~30 s each). After this
# budget, only text formats are still read.
SNAPSHOT_TIME_BUDGET_S = 1.5

# Formats whose handle isn't a plain HGLOBAL (GDI objects, embedded metafile handles,
# owner-display NULL handles) are never copied. Windows re-synthesises most of them from
# the formats we do keep (e.g. CF_BITMAP from CF_DIB).
SKIP_FORMATS = frozenset(
    {
        w.CF_BITMAP,
        w.CF_METAFILEPICT,
        w.CF_PALETTE,
        w.CF_ENHMETAFILE,
        w.CF_OWNERDISPLAY,
        w.CF_DSPBITMAP,
        w.CF_DSPMETAFILEPICT,
        w.CF_DSPENHMETAFILE,
    }
)
# OLE's clipboard plumbing: meaningless outside the process that put it there.
SKIP_FORMAT_NAMES = frozenset({"DataObject", "Ole Private Data"})
# Always captured, even when the size/time caps drop everything else.
TEXT_FORMATS = (w.CF_UNICODETEXT, w.CF_LOCALE)

# Serialises our own threads (paste vs. delayed restore); other apps are handled by retries.
_lock = threading.RLock()


class ClipboardError(RuntimeError):
    """The clipboard couldn't be opened, read or written."""


@dataclass(frozen=True, repr=False)
class ClipboardSnapshot:
    """A copy of the clipboard's HGLOBAL formats, in their original (preference) order."""

    formats: tuple[tuple[int, bytes], ...] = ()
    # How many formats were on the clipboard when the snapshot was taken (incl. skipped and
    # synthesised ones). 0 means the clipboard was genuinely empty.
    original_format_count: int = 0
    # False when a wanted format couldn't be captured (size/time cap, read error).
    complete: bool = True

    @property
    def is_empty(self) -> bool:
        return not self.formats

    @property
    def total_bytes(self) -> int:
        return sum(len(data) for _, data in self.formats)

    def format_ids(self) -> tuple[int, ...]:
        return tuple(fmt for fmt, _ in self.formats)

    def get(self, fmt: int) -> bytes | None:
        for f, data in self.formats:
            if f == fmt:
                return data
        return None

    def __repr__(self) -> str:  # never put clipboard contents in logs
        return (
            f"ClipboardSnapshot(formats={list(self.format_ids())}, bytes={self.total_bytes}, "
            f"original_format_count={self.original_format_count}, complete={self.complete})"
        )


# ------------------------------------------------------------------ pure selection


def select_formats(available: Sequence[int], name_of: Callable[[int], str | None]) -> list[int]:
    """Choose which of the enumerated formats to save, keeping enumeration order.

    ``EnumClipboardFormats`` lists the real formats first and the synthesised ones after, so
    keeping the first of each convertible family keeps the original data:
    CF_UNICODETEXT beats CF_TEXT/CF_OEMTEXT (CF_LOCALE is kept for the conversion), and the
    first of CF_DIB/CF_DIBV5 wins.
    """
    present = set(available)
    chosen: list[int] = []
    seen_dib = False
    for fmt in available:
        if fmt in chosen or fmt in SKIP_FORMATS:
            continue
        if w.CF_PRIVATEFIRST <= fmt <= w.CF_GDIOBJLAST:
            # Private (0x200-0x2FF) and app-defined GDI (0x300-0x3FF) formats are never freed
            # by the system; only their owner knows what they mean.
            continue
        if fmt in (w.CF_TEXT, w.CF_OEMTEXT) and w.CF_UNICODETEXT in present:
            continue
        if fmt in (w.CF_DIB, w.CF_DIBV5):
            if seen_dib:
                continue
            seen_dib = True
        if fmt >= w.CF_REGISTERED_FIRST and name_of(fmt) in SKIP_FORMAT_NAMES:
            continue
        chosen.append(fmt)
    return chosen


def _decode_text(raw: bytes) -> str:
    raw = raw[: len(raw) - (len(raw) % 2)]
    text = raw.decode("utf-16-le", errors="replace")
    nul = text.find("\0")
    return text if nul < 0 else text[:nul]


# --------------------------------------------------------------------- Win32 plumbing


def _require_windows() -> None:
    if not w.IS_WINDOWS:
        raise ClipboardError("clipboard access is only implemented on Windows")


@contextmanager
def _session(attempts: int = OPEN_ATTEMPTS) -> Iterator[int]:
    """Open the clipboard owned by a temporary message-only window."""
    _require_windows()
    with _lock:
        hwnd = w.user32.CreateWindowExW(
            0, "STATIC", None, 0, 0, 0, 0, 0, w.HWND_MESSAGE, None, None, None
        )
        if not hwnd:
            raise ClipboardError(f"couldn't create a clipboard window (error {w.last_error()})")
        try:
            _open(hwnd, attempts)
            try:
                yield hwnd
            finally:
                w.user32.CloseClipboard()
        finally:
            w.user32.DestroyWindow(hwnd)


def _open(hwnd: int, attempts: int) -> None:
    for attempt in range(max(1, attempts)):
        if w.user32.OpenClipboard(hwnd):
            return
        if attempt + 1 < attempts:
            time.sleep(min(0.02 + 0.005 * attempt, 0.05))
    holder = w.user32.GetOpenClipboardWindow()
    raise ClipboardError(
        f"the clipboard is busy (held open by another window{f' {holder:#x}' if holder else ''})"
    )


@cache
def _registered_format(name: str) -> int:
    fmt = w.user32.RegisterClipboardFormatW(name)
    if not fmt:
        raise ClipboardError(f"RegisterClipboardFormatW({name!r}) failed ({w.last_error()})")
    return fmt


def exclusion_format_ids() -> dict[str, int]:
    """{marker name: registered format id} for the three history/cloud marker formats."""
    return {name: _registered_format(name) for name in EXCLUSION_FORMAT_NAMES}


def format_name(fmt: int) -> str | None:
    """Name of a registered format (>= 0xC000), else None. Works without opening."""
    if not w.IS_WINDOWS or fmt < w.CF_REGISTERED_FIRST:
        return None
    buf = ctypes.create_unicode_buffer(256)
    n = w.user32.GetClipboardFormatNameW(fmt, buf, len(buf))
    return buf.value[:n] if n > 0 else None


def _put(fmt: int, data: bytes) -> None:
    """SetClipboardData with a fresh GMEM_MOVEABLE copy of ``data`` (clipboard must be open)."""
    k32 = w.kernel32
    size = max(len(data), 1)
    handle = k32.GlobalAlloc(w.GMEM_MOVEABLE, size)
    if not handle:
        raise ClipboardError(f"GlobalAlloc({size}) failed ({w.last_error()})")
    ptr = k32.GlobalLock(handle)
    if not ptr:
        k32.GlobalFree(handle)
        raise ClipboardError(f"GlobalLock failed ({w.last_error()})")
    try:
        ctypes.memset(ptr, 0, size)
        ctypes.memmove(ptr, data, len(data))
    finally:
        k32.GlobalUnlock(handle)
    if not w.user32.SetClipboardData(fmt, handle):
        err = w.last_error()
        k32.GlobalFree(handle)  # only on failure: on success the system owns it
        raise ClipboardError(f"SetClipboardData({fmt:#x}) failed ({err})")


def _put_exclusion_markers(skip: frozenset[int] = frozenset()) -> None:
    for fmt in exclusion_format_ids().values():
        if fmt not in skip:
            _put(fmt, _DWORD_ZERO)


class _TooBig(Exception):
    pass


def _read(fmt: int, max_bytes: int | None = None) -> bytes | None:
    """Copy one HGLOBAL format (clipboard must be open). Never frees or keeps the handle.

    Returns None if the format can't be read; raises _TooBig if it's over ``max_bytes``.
    """
    k32 = w.kernel32
    handle = w.user32.GetClipboardData(fmt)
    if not handle:
        return None
    size = k32.GlobalSize(handle)
    if size == 0:
        return None
    if max_bytes is not None and size > max_bytes:
        raise _TooBig(size)
    ptr = k32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        k32.GlobalUnlock(handle)


def _enum_formats() -> list[int]:
    formats: list[int] = []
    fmt = 0
    while True:
        fmt = w.user32.EnumClipboardFormats(fmt)
        if not fmt or len(formats) > 1024:
            break
        formats.append(fmt)
    return formats


# ------------------------------------------------------------------------ public API


def set_text(text: str, *, exclude_from_history: bool = True) -> int:
    """Replace the clipboard with ``text`` (CF_UNICODETEXT). Returns the new sequence number."""
    data = text.encode("utf-16-le") + b"\0\0"
    with _session():
        if not w.user32.EmptyClipboard():
            raise ClipboardError(f"EmptyClipboard failed ({w.last_error()})")
        _put(w.CF_UNICODETEXT, data)
        if exclude_from_history:
            _put_exclusion_markers()
    return sequence_number()


def get_text() -> str | None:
    """The clipboard's text (CF_UNICODETEXT), or None if there is none."""
    with _session():
        if not w.user32.IsClipboardFormatAvailable(w.CF_UNICODETEXT):
            return None
        raw = _read(w.CF_UNICODETEXT)
    return None if raw is None else _decode_text(raw)


def sequence_number() -> int:
    _require_windows()
    return int(w.user32.GetClipboardSequenceNumber())


def list_formats() -> list[tuple[int, str | None]]:
    """(format id, registered name or None) for everything currently on the clipboard."""
    with _session():
        formats = _enum_formats()
    return [(fmt, format_name(fmt)) for fmt in formats]


def snapshot(
    *,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
    time_budget_s: float = SNAPSHOT_TIME_BUDGET_S,
) -> ClipboardSnapshot:
    """Copy the clipboard's HGLOBAL formats so they can be put back with :func:`restore`.

    Text (CF_UNICODETEXT + CF_LOCALE) is read first and always kept. Other formats are read
    in order until ``time_budget_s`` passes; if their total size would exceed ``max_bytes``
    the snapshot falls back to text only.
    """
    with _session():
        available = _enum_formats()
        selected = select_formats(available, format_name)
        ordered = [f for f in selected if f in TEXT_FORMATS]
        ordered += [f for f in selected if f not in TEXT_FORMATS]
        captured: dict[int, bytes] = {}
        complete = True
        text_only = False
        other_bytes = 0
        started = time.monotonic()
        for fmt in ordered:
            is_text = fmt in TEXT_FORMATS
            if not is_text:
                if text_only:
                    complete = False
                    continue
                if time.monotonic() - started > time_budget_s:
                    log.info("clipboard snapshot: time budget exceeded; skipping %#x", fmt)
                    complete = False
                    continue
            try:
                data = _read(fmt, None if is_text else max_bytes - other_bytes)
            except _TooBig:
                log.info(
                    "clipboard snapshot: over %d bytes at %#x; keeping text only", max_bytes, fmt
                )
                text_only = True
                complete = False
                for f in [f for f in captured if f not in TEXT_FORMATS]:
                    del captured[f]
                continue
            except OSError:  # access violation reading a bogus handle
                log.info("clipboard snapshot: couldn't read %#x", fmt, exc_info=True)
                data = None
            if data is None:
                complete = False
                continue
            captured[fmt] = data
            if not is_text:
                other_bytes += len(data)
    snap = ClipboardSnapshot(
        formats=tuple((f, captured[f]) for f in selected if f in captured),
        original_format_count=len(available),
        complete=complete,
    )
    log.debug("clipboard snapshot: %r", snap)
    return snap


def restore(
    snap: ClipboardSnapshot,
    *,
    exclude_from_history: bool = True,
    expected_sequence: int | None = None,
) -> bool:
    """Put a snapshot back. An empty snapshot empties the clipboard. Returns True if restored.

    With ``exclude_from_history`` the restored item gets the history/cloud markers too (the
    original is already in clipboard history; this avoids a duplicate entry), unless the
    snapshot already carries them. With ``expected_sequence``, nothing is restored if the
    clipboard changed since then (checked while the clipboard is open, so a copy made in
    the meantime can't be overwritten).
    """
    with _session():
        if expected_sequence is not None and sequence_number() != expected_sequence:
            log.info("clipboard changed since the paste; not restoring")
            return False
        if not w.user32.EmptyClipboard():
            raise ClipboardError(f"EmptyClipboard failed ({w.last_error()})")
        if not snap.formats:
            return True
        failures = 0
        for fmt, data in snap.formats:
            try:
                _put(fmt, data)
            except ClipboardError as exc:
                failures += 1
                log.warning("clipboard restore: %s", exc)
        if failures == len(snap.formats):
            raise ClipboardError("couldn't restore any clipboard format")
        if exclude_from_history:
            _put_exclusion_markers(skip=frozenset(snap.format_ids()))
        return True
