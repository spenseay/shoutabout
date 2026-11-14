"""This installation's identity: a stable id and the friendly name phones see.

``identity.json`` in the config dir::

    {"id": "6f1c…-uuid", "name": null}

* ``id`` is a random UUID made on first use and never changed. The iPhone remembers it,
  so it can tell "my PC" from any other ShoutAbout PC on the Wi-Fi even after a rename or
  a new IP address. It's not a secret (it's in the DNS-SD TXT record and ``/hello``).
* ``name`` is the user's own name for this PC (Settings > Computer name), or null for the
  default: the Windows computer name, read fresh each time so a renamed PC is picked up.
  A name Windows made up at setup ("DESKTOP-Q0DB401", "LAPTOP-AB12CD3") means nothing on a
  phone, so it shows as "Desktop" / "Laptop" instead (:func:`default_name`).
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shoutabout.fileio import atomic_write_json, read_json
from shoutabout.names import clean_name

log = logging.getLogger(__name__)

# A DNS-SD instance name is one DNS label: at most 63 bytes of UTF-8.
NAME_MAX_BYTES = 63
NAME_MAX_CHARS = 40
FALLBACK_NAME = "My PC"
# Windows names a new PC "DESKTOP-" or "LAPTOP-" plus 7 random letters and digits.
_GENERATED_NAME = re.compile(r"(DESKTOP|LAPTOP)-[A-Z0-9]{7}", re.IGNORECASE)


def computer_name() -> str:
    """The Windows computer name (or the host name elsewhere)."""
    name = os.environ.get("COMPUTERNAME") or ""
    if not name and sys.platform == "darwin":  # v2-macos: "Sam's MacBook Pro"
        from shoutabout.mac.sysinfo import computer_name as mac_name

        name = mac_name() or ""
    if not name:
        try:
            name = socket.gethostname().split(".")[0]
        except OSError:
            name = ""
    return clean_friendly_name(name) or FALLBACK_NAME


def friendly_default(name: str) -> str:
    """``name``, or "Desktop" / "Laptop" for a name Windows generated at setup."""
    match = _GENERATED_NAME.fullmatch(name.strip())
    return match.group(1).capitalize() if match else name


def default_name() -> str:
    """The name phones see when nobody chose one: the computer name, made friendly."""
    return friendly_default(computer_name())


def clean_friendly_name(raw: object) -> str:
    """A name fit for the phone list and a DNS-SD instance label ("" if unusable)."""
    return clean_name(raw, max_chars=NAME_MAX_CHARS, max_utf8_bytes=NAME_MAX_BYTES)


@dataclass(frozen=True)
class Identity:
    id: str
    name: str
    custom_name: bool  # False = the computer name (follows a Windows rename)
    # The DNS-SD instance name when it isn't ``name``: two PCs both shown as "Desktop" must
    # still announce different instance names, so those keep the unique computer name.
    announce_as: str = ""


class IdentityStore:
    """Thread-safe access to identity.json (created on first use)."""

    def __init__(
        self,
        path: Path,
        *,
        default_name: Callable[[], str] = default_name,
        machine_name: Callable[[], str] = computer_name,
    ) -> None:
        self.path = Path(path)
        self._default_name = default_name
        self._machine_name = machine_name
        self._lock = threading.Lock()
        self._id: str | None = None
        self._custom: str | None = None
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        data = None
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            log.warning("identity.json is unreadable; making a new installation id")
        data = data if isinstance(data, dict) else {}
        raw_id = data.get("id")
        try:
            self._id = str(uuid.UUID(str(raw_id))) if raw_id else None
        except ValueError:
            self._id = None
        custom = clean_friendly_name(data.get("name"))
        self._custom = custom or None
        if self._id is None:
            self._id = str(uuid.uuid4())
            self._save()
        self._loaded = True

    def _save(self) -> None:
        try:
            atomic_write_json(self.path, {"id": self._id, "name": self._custom})
        except OSError:
            log.exception("couldn't save %s", self.path)

    def get(self) -> Identity:
        with self._lock:
            self._load()
            assert self._id is not None
            if self._custom:
                return Identity(self._id, self._custom, True)
            name = self._default_name() or FALLBACK_NAME
        machine = self._machine_name()
        friendly = machine != name and friendly_default(machine) == name
        return Identity(self._id, name, False, machine if friendly else "")

    def reload(self) -> None:
        """Forget the cached file contents (another process changed identity.json)."""
        with self._lock:
            self._loaded = False

    def rename(self, name: str | None) -> Identity:
        """Set the friendly name; ``None`` or blank goes back to the computer name.

        Raises ValueError if ``name`` has no printable characters left after cleaning.
        """
        cleaned = clean_friendly_name(name) if name is not None else ""
        if name is not None and name.strip() and not cleaned:
            raise ValueError("Use letters or numbers for the name.")
        with self._lock:
            self._load()
            self._custom = cleaned or None
            self._save()
        return self.get()
