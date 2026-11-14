"""Text delivery + notification interfaces (Windows implementation in inject/windows.py)."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from shoutabout.models import PasteResult

log = logging.getLogger(__name__)

ADMIN_WINDOW_WARNING = "Couldn't paste into an admin window — text is on your clipboard."
# v2-macos: macOS doesn't let ShoutAbout paste (Accessibility is off, maybe for good on a
# managed Mac). The words are on the clipboard every time, the pill says so (COPIED_NOTICE)
# and the injector notifies once, not at every dictation.
NO_PASTE_PERMISSION_WARNING = "ShoutAbout isn't allowed to paste yet — text is on your clipboard."
COPIED_NOTICE = "Copied · ⌘V to paste"

# No text box had the focus (the focus probe, inject/focus.py): the paste keystroke is still
# sent, but the dictation stays on the clipboard and the pill says so (≤ 28 characters).
NOT_PASTED_NOTICE = "Not pasted · Ctrl+V to paste"
NOT_PASTED_NOTICE_MAC = "Not pasted · ⌘V to paste"
NOT_PASTED_HOLD_S = 4.0  # the pill stays this long (you may be looking at your phone)
NO_TEXT_BOX_PREFIX = "No text box was selected on "


def no_text_box_warning(name: str | None, computer: str = "PC") -> str:
    """What the phone is told: where the text is, e.g. "No text box was selected on
    Desktop, so it's on that computer's clipboard." ``name`` is the computer's friendly
    name (None/empty: "the PC")."""
    name = " ".join(str(name or "").split())
    if name:
        return f"{NO_TEXT_BOX_PREFIX}{name}, so it's on that computer's clipboard."
    return f"{NO_TEXT_BOX_PREFIX}the {computer}, so it's on the {computer}'s clipboard."


def is_no_text_box(warning: str | None) -> bool:
    return bool(warning) and str(warning).startswith(NO_TEXT_BOX_PREFIX)


def safe_name(pc_name: object) -> str | None:
    """Call a ``pc_name`` provider without ever failing."""
    if not callable(pc_name):
        return None
    try:
        value = pc_name()
    except Exception:
        log.debug("couldn't read the computer's name", exc_info=True)
        return None
    return str(value) if value else None


@runtime_checkable
class Injector(Protocol):
    def paste(self, text: str) -> PasteResult:
        """Paste plain `text` into the focused window via the clipboard + Ctrl+V.

        Must never send any key other than the paste chord (never Enter). On failure,
        return PasteResult(pasted=False, warning=...) instead of raising. When the text is
        left on the clipboard (not restored), set ``copied``.
        """


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None:
        """Show a desktop notification (toast). Must not raise."""


class NullInjector:
    """Used on non-Windows platforms and by the CLI: never pastes."""

    def __init__(self, reason: str = "Pasting is not supported on this platform.") -> None:
        self.reason = reason

    def paste(self, text: str) -> PasteResult:
        return PasteResult(pasted=False, warning=self.reason)


class LogNotifier:
    """Fallback notifier when no tray icon is available."""

    def notify(self, title: str, message: str) -> None:
        log.warning("notification: %s: %s", title, message)
