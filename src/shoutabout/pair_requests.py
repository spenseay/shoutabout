"""Phone pairing requests: the PC side of ``POST /pair/request`` and ``GET /pair/status``.

The flow (docs/protocol.md):

1. A phone on the Wi-Fi asks to pair (name + its own id). The hub answers with a random
   ``request_id`` (192 bits) and a 4-digit ``code``, and the PC shows an approval prompt:
   "<name> wants to dictate to this PC. Code 4821. [Allow] [Don't allow]". The phone shows
   the same code, so the person can tell their phone's request from anyone else's.
2. The phone polls the status every second. **Only a click on the PC approves**: there is
   no network call that can approve or deny, and nothing is ever approved automatically.
3. On Allow a per-device token is minted (only its hash is stored, :mod:`devices`); the
   next status poll returns it exactly once, after that the request reads "claimed". A
   token that isn't collected within :data:`CLAIM_TTL_S` is revoked again.

Limits: one pending request at a time (409), 5 requests a minute per source address and
30 a minute overall (429), a minute's cool-down for an address after "Don't allow", and
requests expire after :data:`REQUEST_TTL_S` seconds. Finished requests stay answerable for
:data:`RESULT_TTL_S`, then the id is unknown (404).

Listeners (the approval prompt, the setup window) hear about new requests and outcomes;
they run on the caller's thread and must not block.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shoutabout.devices import DEVICE_NAME_MAX, DeviceLimitError, DeviceStore
from shoutabout.names import clean_name
from shoutabout.security import RateLimiter

log = logging.getLogger(__name__)

REQUEST_TTL_S = 120
CLAIM_TTL_S = 120.0
RESULT_TTL_S = 600.0
RATE_PER_IP = 5
RATE_GLOBAL = 30
RATE_WINDOW_S = 60.0
DENY_COOLDOWN_S = 60.0
CODE_DIGITS = 4
REQUEST_ID_BYTES = 24  # 192 bits; token_urlsafe -> 32 characters
MAX_TRACKED = 64


class PairStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CLAIMED = "claimed"


class PairError(Exception):
    """A request the hub refuses: carries the HTTP status and a short public message."""

    def __init__(self, status: int, detail: str, reason: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reason = reason  # for the log line, never user data


@dataclass(frozen=True)
class PairPrompt:
    """What the approval prompt shows. ``deadline`` is on the manager's clock."""

    request_id: str
    code: str
    device_name: str
    deadline: float


@dataclass
class _Request:
    request_id: str
    code: str
    device_name: str
    device_id: str
    source_ip: str
    created: float
    deadline: float
    status: PairStatus = PairStatus.PENDING
    finished: float | None = None
    token: str | None = None  # between approval and the one status poll that collects it
    device_key: str | None = None
    approved_at: float | None = None

    def prompt(self) -> PairPrompt:
        return PairPrompt(self.request_id, self.code, self.device_name, self.deadline)


def validate_request(body: Any) -> tuple[str, str]:
    """``(device_name, device_id)`` from a /pair/request body; :class:`PairError` 422 if bad.

    Unknown fields are ignored. The name is cleaned (no control/format characters) and must
    be 1-60 characters; the id must be a UUID (returned in canonical form).
    """
    if not isinstance(body, dict):
        raise PairError(422, "send a JSON object with device_name and device_id", "bad-body")
    raw_name = body.get("device_name")
    if not isinstance(raw_name, str):
        raise PairError(422, "device_name must be a string", "bad-name")
    name = clean_name(raw_name, max_chars=10_000)
    if not name or len(name) > DEVICE_NAME_MAX:
        raise PairError(422, f"device_name must be 1-{DEVICE_NAME_MAX} characters", "bad-name")
    raw_id = body.get("device_id")
    if not isinstance(raw_id, str) or len(raw_id) > 64:
        raise PairError(422, "device_id must be a UUID string", "bad-id")
    try:
        device_id = str(uuid.UUID(raw_id.strip()))
    except ValueError as exc:
        raise PairError(422, "device_id must be a UUID string", "bad-id") from exc
    return name, device_id


class PairingManager:
    """Pairing state shared by the server (requests, polls) and the UI (approve/deny)."""

    def __init__(
        self,
        devices: DeviceStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        code_factory: Callable[[], str] | None = None,
    ) -> None:
        self.devices = devices
        self._clock = clock
        self._code = code_factory or (lambda: f"{secrets.randbelow(10**CODE_DIGITS):04d}")
        self._lock = threading.RLock()
        self._requests: list[_Request] = []
        self._per_ip = RateLimiter(RATE_PER_IP, RATE_WINDOW_S, clock=clock)
        self._global = RateLimiter(RATE_GLOBAL, RATE_WINDOW_S, clock=clock)
        self._cooldown: dict[str, float] = {}
        self._on_request: list[Callable[[PairPrompt], None]] = []
        self._on_finished: list[Callable[[str, PairStatus], None]] = []
        self.enabled = True

    # ------------------------------------------------------------- listeners

    def subscribe(
        self,
        on_request: Callable[[PairPrompt], None] | None = None,
        on_finished: Callable[[str, PairStatus], None] | None = None,
    ) -> None:
        with self._lock:
            if on_request is not None:
                self._on_request.append(on_request)
            if on_finished is not None:
                self._on_finished.append(on_finished)

    def unsubscribe(self, callback: Callable[..., None]) -> None:
        with self._lock:
            for group in (self._on_request, self._on_finished):
                while callback in group:
                    group.remove(callback)

    def _emit_request(self, prompt: PairPrompt) -> None:
        with self._lock:
            listeners = list(self._on_request)
        for listener in listeners:
            try:
                listener(prompt)
            except Exception:
                log.exception("pairing listener failed")

    def _emit_finished(self, events: list[tuple[str, PairStatus]]) -> None:
        if not events:
            return
        with self._lock:
            listeners = list(self._on_finished)
        for request_id, status in events:
            for listener in listeners:
                try:
                    listener(request_id, status)
                except Exception:
                    log.exception("pairing listener failed")

    # ---------------------------------------------------------------- upkeep

    def _sweep(self, now: float) -> list[tuple[str, PairStatus]]:
        """Expire due requests, revoke uncollected tokens, forget old ones. Lock held."""
        events: list[tuple[str, PairStatus]] = []
        for req in self._requests:
            if req.status == PairStatus.PENDING and now >= req.deadline:
                req.status, req.finished = PairStatus.EXPIRED, now
                events.append((req.request_id, req.status))
                log.info("pairing request expired")
            elif (
                req.status == PairStatus.APPROVED
                and req.approved_at is not None
                and now - req.approved_at >= CLAIM_TTL_S
            ):
                # The phone never collected its token: don't leave a live credential behind.
                if req.device_key:
                    try:
                        self.devices.remove(req.device_key)
                    except OSError:
                        log.exception("couldn't revoke an uncollected pairing token")
                req.token, req.status, req.finished = None, PairStatus.EXPIRED, now
                events.append((req.request_id, req.status))
                log.info("approved pairing was never collected; revoked")
        # Unfinished requests (pending, or approved and not yet collected) are always kept:
        # dropping an approved one would leave its token alive without the revoke above.
        live = [r for r in self._requests if r.finished is None]
        done = [
            r for r in self._requests if r.finished is not None and now - r.finished < RESULT_TTL_S
        ]
        done.sort(key=lambda r: r.finished or 0.0)  # keep the most recently finished
        self._requests = live + done[-MAX_TRACKED:]
        for ip in [ip for ip, until in self._cooldown.items() if until <= now]:
            del self._cooldown[ip]
        return events

    def _find(self, request_id: str) -> _Request | None:
        """Look up by id with a constant-time comparison against every known request."""
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            return None
        wanted = request_id.encode("utf-8", errors="replace")
        found = None
        for req in self._requests:
            if hmac.compare_digest(wanted, req.request_id.encode("ascii")):
                found = req
        return found

    # ---------------------------------------------------------------- server

    def request(self, device_name: str, device_id: str, source_ip: str) -> PairPrompt:
        """A phone asks to pair. Raises :class:`PairError` (409/429/503) when refused."""
        error: PairError | None = None
        prompt: PairPrompt | None = None
        with self._lock:
            now = self._clock()
            events = self._sweep(now)
            try:
                prompt = self._open(device_name, device_id, source_ip, now)
            except PairError as exc:
                error = exc
        self._emit_finished(events)
        if error is not None:
            log.info("pairing request refused from %s (%s)", source_ip, error.reason)
            raise error
        assert prompt is not None
        log.info("pairing request from %s (name %d chars)", source_ip, len(device_name))
        self._emit_request(prompt)
        return prompt

    def _open(self, device_name: str, device_id: str, source_ip: str, now: float) -> PairPrompt:
        if not self.enabled:
            raise PairError(503, "pairing is turned off on this PC", "disabled")
        if self._cooldown.get(source_ip, 0.0) > now:
            raise PairError(429, "the PC said no; try again in a minute", "cooldown")
        if not self._per_ip.allow(source_ip) or not self._global.allow("*"):
            raise PairError(429, "too many pairing requests; wait a minute", "rate")
        if any(r.status == PairStatus.PENDING for r in self._requests):
            raise PairError(409, "another phone is waiting for an answer", "busy")
        req = _Request(
            request_id=secrets.token_urlsafe(REQUEST_ID_BYTES),
            code=self._code(),
            device_name=device_name,
            device_id=device_id,
            source_ip=source_ip,
            created=now,
            deadline=now + REQUEST_TTL_S,
        )
        self._requests.append(req)
        return req.prompt()

    def status(self, request_id: str) -> dict[str, Any] | None:
        """The status body for a poll, or None if the id is unknown. On the first poll after
        approval it holds the token (key ``token``); the request is then "claimed"."""
        with self._lock:
            now = self._clock()
            events = self._sweep(now)
            req = self._find(request_id)
            body: dict[str, Any] | None = None
            if req is not None:
                if req.status == PairStatus.APPROVED and req.token is not None:
                    body = {"status": "approved", "token": req.token}
                    req.token, req.status, req.finished = None, PairStatus.CLAIMED, now
                    events.append((req.request_id, PairStatus.CLAIMED))
                    log.info("pairing token collected")
                else:
                    body = {"status": str(req.status)}
        self._emit_finished(events)
        return body

    # -------------------------------------------------------------------- UI

    def pending(self) -> PairPrompt | None:
        with self._lock:
            events = self._sweep(self._clock())
            req = next((r for r in self._requests if r.status == PairStatus.PENDING), None)
            prompt = req.prompt() if req is not None else None
        self._emit_finished(events)
        return prompt

    def seconds_left(self, prompt: PairPrompt) -> float:
        return max(0.0, prompt.deadline - self._clock())

    def approve(self, request_id: str) -> bool:
        """The person clicked Allow. Mints the device token. False if it's no longer
        pending (expired, or answered elsewhere). Raises DeviceLimitError / OSError if the
        device can't be saved (the request stays pending so the UI can say why)."""
        ok = False
        error: Exception | None = None
        with self._lock:
            now = self._clock()
            events = self._sweep(now)
            req = self._find(request_id)
            if req is not None and req.status == PairStatus.PENDING:
                try:
                    device, token = self.devices.add(req.device_name, req.device_id)
                except (DeviceLimitError, OSError) as exc:
                    error = exc
                else:
                    req.token, req.device_key = token, device.key
                    req.status, req.approved_at = PairStatus.APPROVED, now
                    events.append((req.request_id, PairStatus.APPROVED))
                    ok = True
        self._emit_finished(events)
        if error is not None:
            raise error
        if ok:
            log.info("pairing approved on the PC")
        return ok

    def deny(self, request_id: str) -> bool:
        """The person clicked Don't allow (or closed the prompt)."""
        with self._lock:
            now = self._clock()
            events = self._sweep(now)
            req = self._find(request_id)
            ok = req is not None and req.status == PairStatus.PENDING
            if ok and req is not None:
                req.status, req.finished = PairStatus.DENIED, now
                self._cooldown[req.source_ip] = now + DENY_COOLDOWN_S
                events.append((req.request_id, PairStatus.DENIED))
        self._emit_finished(events)
        if ok:
            log.info("pairing denied on the PC")
        return ok


__all__ = [
    "CLAIM_TTL_S",
    "DeviceLimitError",
    "PairError",
    "PairPrompt",
    "PairStatus",
    "PairingManager",
    "REQUEST_TTL_S",
    "validate_request",
]
