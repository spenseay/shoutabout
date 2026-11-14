"""Text delivery: the injector factory (Windows paste or a no-op)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from shoutabout.config import PasteConfig
from shoutabout.inject.base import (
    ADMIN_WINDOW_WARNING,
    Injector,
    LogNotifier,
    Notifier,
    NullInjector,
)

DISABLED_REASON = "Pasting is disabled in config (paste.enabled = false)."

__all__ = [
    "ADMIN_WINDOW_WARNING",
    "DISABLED_REASON",
    "Injector",
    "LogNotifier",
    "Notifier",
    "NullInjector",
    "create_injector",
]


def create_injector(
    cfg: PasteConfig,
    notifier: Notifier,
    *,
    popup: Callable[[str], Any] | None = None,
    pc_name: Callable[[], str | None] | None = None,
) -> Injector:
    """WindowsPasteInjector on Windows; NullInjector elsewhere or when pasting is disabled.

    ``popup(text)`` shows the "not pasted" pill when no text box had the focus (else a
    notification); ``pc_name()`` is this computer's friendly name for the phone's warning.
    """
    if not cfg.enabled:
        return NullInjector(DISABLED_REASON)
    if sys.platform == "darwin":  # v2-macos: NSPasteboard + Cmd+V
        from shoutabout.mac.paste import MacPasteInjector

        return MacPasteInjector(cfg, notifier, popup=popup, pc_name=pc_name)
    if sys.platform != "win32":
        return NullInjector()
    from shoutabout.inject.windows import WindowsPasteInjector

    return WindowsPasteInjector(cfg, notifier, popup=popup, pc_name=pc_name)
