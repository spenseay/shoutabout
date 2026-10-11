"""A tiny message link between the hub and its window process (JSON lines over two pipes).

Each message is one JSON object on one line with a type ``t``:

* ``call`` ``{id, op, args}`` asks the other side to do something; it answers with
  ``reply`` ``{id, ok, value | error}``. :meth:`Link.call` blocks until the reply (with a
  timeout).
* anything else is a one-way event, handed to the ``on_event`` callback.

A reader thread reads lines and dispatches them; calls are handled by ``on_call`` on that
thread, so handlers must be quick. Nothing secret ever crosses the link (the pipes are
private to the two processes anyway): pairing codes and device names only, never tokens.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
from collections.abc import Callable
from typing import IO, Any

log = logging.getLogger(__name__)

MAX_LINE = 1 << 20
CALL_TIMEOUT_S = 10.0


class LinkClosed(ConnectionError):
    """The other process went away."""


class RemoteError(RuntimeError):
    """The other side's handler raised; the message is its plain-words error."""


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("t"), str) else None


class Link:
    def __init__(
        self,
        reader: IO[bytes],
        writer: IO[bytes],
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_call: Callable[[str, dict[str, Any]], Any] | None = None,
        on_close: Callable[[], None] | None = None,
        name: str = "ui-link",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_event = on_event
        self._on_call = on_call
        self._on_close = on_close
        self._name = name
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, tuple[threading.Event, list[Any]]] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_loop, name=self._name, daemon=True)
        self._thread.start()

    def send(self, message: dict[str, Any]) -> bool:
        """Send one message; False if the link is closed."""
        if self._closed.is_set():
            return False
        data = encode(message)
        try:
            with self._write_lock:
                self._writer.write(data)
                self._writer.flush()
            return True
        except (OSError, ValueError):
            self._close()
            return False

    def call(self, op: str, timeout: float = CALL_TIMEOUT_S, **args: Any) -> Any:
        """Ask the other side to run ``op``; returns its value or raises RemoteError,
        LinkClosed or TimeoutError."""
        call_id = next(self._ids)
        done = threading.Event()
        box: list[Any] = []
        with self._lock:
            self._waiting[call_id] = (done, box)
        try:
            if not self.send({"t": "call", "id": call_id, "op": op, "args": args}):
                raise LinkClosed("the other ShoutAbout process isn't running")
            while not done.wait(0.1):
                if self._closed.is_set():
                    raise LinkClosed("the other ShoutAbout process went away")
                timeout -= 0.1
                if timeout <= 0:
                    raise TimeoutError(f"no answer to {op!r}")
        finally:
            with self._lock:
                self._waiting.pop(call_id, None)
        if not box:  # woken by the link closing, not by a reply
            raise LinkClosed("the other ShoutAbout process went away")
        reply = box[0]
        if not reply.get("ok"):
            raise RemoteError(str(reply.get("error") or "failed"))
        return reply.get("value")

    def close(self) -> None:
        """Close our end: the other side sees EOF and closes its end, which ends our
        reader thread (it closes the reading pipe itself; closing a pipe another thread
        is reading from can block)."""
        try:
            with self._write_lock:
                self._writer.close()
        except Exception:
            pass
        reader = self._thread
        if reader is None or not reader.is_alive():
            try:
                self._reader.close()
            except Exception:
                pass
        self._close()

    def _close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._lock:
            waiting = list(self._waiting.values())
        for done, _box in waiting:
            done.set()
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                log.exception("link close handler failed")

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._reader.readline(MAX_LINE)
                if not line:
                    break
                message = decode(line)
                if message is None:
                    log.warning("ignored a malformed message on the window link")
                    continue
                self._dispatch(message)
        except (OSError, ValueError):
            log.debug("window link read failed", exc_info=True)
        finally:
            try:
                self._reader.close()
            except Exception:
                pass
            self._close()

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message["t"]
        if kind == "reply":
            with self._lock:
                entry = self._waiting.get(message.get("id"))  # type: ignore[arg-type]
            if entry is not None:
                done, box = entry
                box.append(message)
                done.set()
            return
        if kind == "call":
            self._answer(message)
            return
        if self._on_event is not None:
            try:
                self._on_event(message)
            except Exception:
                log.exception("window link event %r failed", kind)

    def _answer(self, message: dict[str, Any]) -> None:
        call_id = message.get("id")
        op = str(message.get("op") or "")
        args = message.get("args") if isinstance(message.get("args"), dict) else {}
        if self._on_call is None:
            self.send({"t": "reply", "id": call_id, "ok": False, "error": "not supported"})
            return
        try:
            value = self._on_call(op, args)  # type: ignore[arg-type]
        except Exception as exc:
            log.info("window link call %r failed: %s", op, exc)
            self.send(
                {"t": "reply", "id": call_id, "ok": False, "error": str(exc) or type(exc).__name__}
            )
            return
        self.send({"t": "reply", "id": call_id, "ok": True, "value": value})
