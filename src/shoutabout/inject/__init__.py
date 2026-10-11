"""Text delivery: the injector factory (Windows paste or a no-op)."""

from __future__ import annotations

import sys

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


def create_injector(cfg: PasteConfig, notifier: Notifier) -> Injector:
    """WindowsPasteInjector on Windows; NullInjector elsewhere or when pasting is disabled."""
    if not cfg.enabled:
        return NullInjector(DISABLED_REASON)
    if sys.platform == "darwin":  # v2-macos: NSPasteboard + Cmd+V
        from shoutabout.mac.paste import MacPasteInjector

        return MacPasteInjector(cfg, notifier)
    if sys.platform != "win32":
        return NullInjector()
    from shoutabout.inject.windows import WindowsPasteInjector

    return WindowsPasteInjector(cfg, notifier)
