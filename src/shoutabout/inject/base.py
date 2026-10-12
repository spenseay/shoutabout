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


@runtime_checkable
class Injector(Protocol):
    def paste(self, text: str) -> PasteResult:
        """Paste plain `text` into the focused window via the clipboard + Ctrl+V.

        Must never send any key other than the paste chord (never Enter). On failure,
        return PasteResult(pasted=False, warning=...) instead of raising.
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
