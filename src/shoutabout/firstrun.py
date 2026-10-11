"""First run: is setup done, and does the default port need moving?

* The ``setup_complete`` flag (config dir) is written when the setup window is finished or
  skipped. While it's missing the hub opens the setup window at start.
* An install from before v2 never had that window. It counts as set up already (so a
  working PC never suddenly shows a wizard) when, *before* the hub creates any missing
  files, it already has a hub token and has been used: a dictation history, a vocab that
  differs from the example, or the v1 setup script's install-state.json.
* On a fresh install, if the configured port is taken by another program, the next free
  port is picked and saved (phones find the PC by DNS-SD, so nobody types the port).
"""

from __future__ import annotations

import json
import logging
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from shoutabout.config import Paths, example_vocab_text
from shoutabout.fileio import atomic_write_text

log = logging.getLogger(__name__)

PORT_SEARCH_SPAN = 50


@dataclass(frozen=True)
class InstallSnapshot:
    """What was in the config dir before this run created anything."""

    had_token: bool
    had_history: bool
    had_custom_vocab: bool
    had_install_state: bool
    had_flag: bool

    @property
    def existing_install(self) -> bool:
        """A pre-v2 install that's in use: never show it the first-run window."""
        return self.had_token and (
            self.had_history or self.had_custom_vocab or self.had_install_state
        )


def _vocab_is_custom(paths: Paths) -> bool:
    try:
        text = paths.vocab_file.read_text("utf-8")
    except OSError:
        return False
    try:
        example = example_vocab_text()
    except OSError:
        return True
    return text.replace("\r\n", "\n").strip() != example.replace("\r\n", "\n").strip()


def snapshot(paths: Paths) -> InstallSnapshot:
    """Look at the config dir. Call before ``ensure_initialized`` creates missing files."""
    return InstallSnapshot(
        had_token=paths.token_file.exists(),
        had_history=paths.history_file.exists(),
        had_custom_vocab=_vocab_is_custom(paths),
        had_install_state=paths.install_state_file.exists(),
        had_flag=paths.setup_flag_file.exists(),
    )


def is_setup_complete(paths: Paths) -> bool:
    return paths.setup_flag_file.exists()


def mark_setup_complete(paths: Paths, how: str = "finished") -> None:
    """Write the flag (``how``: finished | skipped | existing-install). Never raises."""
    data = {"how": how, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        atomic_write_text(paths.setup_flag_file, json.dumps(data) + "\n")
    except OSError:
        log.exception("couldn't write %s", paths.setup_flag_file)


def needs_setup(paths: Paths, before: InstallSnapshot) -> bool:
    """True if the setup window should open by itself. Marks an existing install done."""
    if before.had_flag or is_setup_complete(paths):
        return False
    if before.existing_install:
        mark_setup_complete(paths, "existing-install")
        log.info("existing install: skipping the first-run setup window")
        return False
    return True


# --------------------------------------------------------------------------- ports


def port_is_free(host: str, port: int) -> bool:
    """True if nothing is listening on ``port`` (checked with an exclusive bind)."""
    bind_host = "" if host in ("0.0.0.0", "::", "") else host
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
            sock.bind((bind_host, port))
    except OSError:
        return False
    return True


def pick_port(
    preferred: int,
    is_free: Callable[[int], bool],
    *,
    span: int = PORT_SEARCH_SPAN,
) -> int | None:
    """``preferred`` if free, else the next free port above it (None if none in ``span``)."""
    for port in range(preferred, min(65535, preferred + span) + 1):
        if is_free(port):
            return port
    return None
