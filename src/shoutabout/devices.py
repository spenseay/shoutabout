"""Paired phones and their per-device tokens (``devices.json`` in the config dir).

Each phone that pairs gets its own token (32 random bytes, URL-safe base64). Only the
token's SHA-256 is stored, with the phone's name and id and when it was paired and last
used, so devices.json is no key to the hub even if it leaks::

    {"version": 1,
     "devices": [{"key": "3f9a…", "device_id": "<phone uuid>", "name": "Sam's iPhone",
                  "token_sha256": "<hex>", "created": "2026-10-10T09:41:00+01:00",
                  "last_seen": "2026-10-10T10:02:13+01:00"}]}

Removing a device (Settings > Phones > Remove) revokes it at once: the next request with
its token gets 401. The file is re-read whenever it changes on disk, so a removal made by
another process (the CLI) takes effect immediately too.

Token checks hash the presented token once and compare it with *every* stored hash using
``hmac.compare_digest``, without stopping at the first match, so the time taken says
nothing about which device (or whether any) matched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from shoutabout.fileio import atomic_write_json, read_json
from shoutabout.names import clean_name

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
TOKEN_BYTES = 32
DEVICE_NAME_MAX = 60
MAX_DEVICES = 32
# last_seen is kept in memory on every request but written to disk at most this often.
LAST_SEEN_WRITE_INTERVAL_S = 300.0


def new_token() -> str:
    """32 random bytes, URL-safe base64 without padding (43 characters)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


def token_digest(token: str) -> bytes:
    """SHA-256 of a token's UTF-8 bytes (the form tokens are compared and stored in)."""
    return hashlib.sha256(token.encode("utf-8", errors="surrogatepass")).digest()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Device:
    key: str  # our own random id for this record (what Settings removes by)
    device_id: str  # the phone's own id, from its pairing request
    name: str
    token_sha256: str  # hex
    created: str
    last_seen: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "key": self.key,
            "device_id": self.device_id,
            "name": self.name,
            "token_sha256": self.token_sha256,
            "created": self.created,
            "last_seen": self.last_seen,
        }


class DeviceLimitError(RuntimeError):
    """Too many paired phones; remove one first."""


def _parse_device(item: object) -> Device | None:
    if not isinstance(item, dict):
        return None
    try:
        digest = bytes.fromhex(str(item["token_sha256"]))
        key = str(item["key"])
    except (KeyError, ValueError):
        return None
    if len(digest) != hashlib.sha256().digest_size or not key:
        return None
    last_seen = item.get("last_seen")
    return Device(
        key=key,
        device_id=str(item.get("device_id") or ""),
        name=clean_name(item.get("name"), max_chars=DEVICE_NAME_MAX) or "Phone",
        token_sha256=digest.hex(),
        created=str(item.get("created") or ""),
        last_seen=str(last_seen) if last_seen else None,
    )


class DeviceStore:
    """Thread-safe view of devices.json."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        now_iso: Callable[[], str] = _now_iso,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._now_iso = now_iso
        self._lock = threading.Lock()
        self._devices: list[Device] = []
        self._digests: list[tuple[str, bytes]] = []
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size) of the file we read
        self._loaded = False
        self._written_seen: dict[str, float] = {}  # key -> clock() of the last disk write

    # ------------------------------------------------------------------ loading

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _refresh(self) -> None:
        """(Re)load the file if it changed since we last read it. Lock held."""
        stamp = self._file_stamp()
        if self._loaded and stamp == self._stamp:
            return
        devices: list[Device] = []
        if stamp is not None:
            try:
                data = read_json(self.path)
            except (OSError, ValueError):
                log.error("devices.json is unreadable; no phone tokens are accepted until fixed")
                data = None
                self._quarantine()
            items = data.get("devices") if isinstance(data, dict) else None
            for item in items if isinstance(items, list) else []:
                device = _parse_device(item)
                if device is not None:
                    devices.append(device)
        self._set(devices)
        self._stamp = self._file_stamp()
        self._loaded = True

    def _quarantine(self) -> None:
        """Keep a corrupt devices.json aside instead of overwriting it silently."""
        try:
            bad = self.path.with_name(f"{self.path.name}.bad-{int(time.time())}")
            self.path.replace(bad)
            log.error("moved the unreadable devices.json to %s", bad.name)
        except OSError:
            log.exception("couldn't move the unreadable devices.json aside")

    def _set(self, devices: list[Device]) -> None:
        self._devices = devices
        self._digests = [(d.key, bytes.fromhex(d.token_sha256)) for d in devices]

    def _write(self) -> None:
        data = {"version": FORMAT_VERSION, "devices": [d.to_json() for d in self._devices]}
        atomic_write_json(self.path, data)
        self._stamp = self._file_stamp()

    # ------------------------------------------------------------------- public

    def list(self) -> list[Device]:
        with self._lock:
            self._refresh()
            return list(self._devices)

    def add(self, name: str, device_id: str) -> tuple[Device, str]:
        """Pair a phone: returns (record, token). The token exists only in the return value.

        Pairing the same phone (same ``device_id``) again replaces its old record, which
        revokes its old token. Raises :class:`DeviceLimitError` at :data:`MAX_DEVICES`.
        """
        token = new_token()
        with self._lock:
            self._refresh()
            kept = [d for d in self._devices if not (device_id and d.device_id == device_id)]
            if len(kept) >= MAX_DEVICES:
                raise DeviceLimitError(f"{MAX_DEVICES} phones are paired; remove one first")
            device = Device(
                key=uuid.uuid4().hex,
                device_id=device_id,
                name=clean_name(name, max_chars=DEVICE_NAME_MAX) or "Phone",
                token_sha256=token_digest(token).hex(),
                created=self._now_iso(),
            )
            previous = self._devices
            self._set([*kept, device])
            try:
                self._write()
            except BaseException:
                self._set(previous)
                raise
        log.info("paired a new phone (key %s, %d paired)", device.key[:8], len(kept) + 1)
        return device, token

    def remove(self, key: str) -> bool:
        """Revoke a device. True if it existed."""
        with self._lock:
            self._refresh()
            kept = [d for d in self._devices if d.key != key]
            if len(kept) == len(self._devices):
                return False
            previous = self._devices
            self._set(kept)
            try:
                self._write()
            except BaseException:
                self._set(previous)
                raise
            self._written_seen.pop(key, None)
        log.info("removed paired phone (key %s)", key[:8])
        return True

    def match_digest(self, digest: bytes) -> Device | None:
        """The device whose token hashes to ``digest``, checked in constant time."""
        with self._lock:
            self._refresh()
            found: str | None = None
            for key, stored in self._digests:
                if hmac.compare_digest(digest, stored):  # no break: same work either way
                    found = key
            if found is None:
                return None
            return next((d for d in self._devices if d.key == found), None)

    def match(self, token: str) -> Device | None:
        return self.match_digest(token_digest(token))

    def touch(self, key: str) -> None:
        """Note that a device was just used; written to disk at most every 5 minutes."""
        with self._lock:
            self._refresh()
            index = next((i for i, d in enumerate(self._devices) if d.key == key), None)
            if index is None:
                return
            self._devices[index] = replace(self._devices[index], last_seen=self._now_iso())
            now = self._clock()
            last = self._written_seen.get(key)
            if last is not None and now - last < LAST_SEEN_WRITE_INTERVAL_S:
                return
            self._written_seen[key] = now
            try:
                self._write()
            except OSError:
                log.warning("couldn't save last-used time to devices.json")
