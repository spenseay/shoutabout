"""The hub's side of the window process on macOS (see :mod:`shoutabout.mac.ui_child`).

:class:`MacUiBridge` has the interface :mod:`shoutabout.hub_v2` and the menu use for
:class:`shoutabout.ui_host.UiHost` (``open_window``, ``open_setup``, ``open_settings``,
``open_recent``, ``open_teach``, ``shutdown``), so the hub wires it in the same place. It
starts ``ShoutAbout mac-ui`` on demand, sends it window requests and pairing events, and
answers its calls on the link's reader thread.

**Recent** lives in the hub's memory only. The window process asks for it (``recent``), and
the hub tells a running window process each time it changes (a ``recent`` event), so the
Recent page shows a new dictation at once. Try again runs here (:class:`Retries`: it needs
the speech engine), polled like a download.

**Pairing prompts.** A phone's request goes to the window process (its setup window
approves inline, otherwise the small branded prompt appears). If that process doesn't
confirm within :data:`ACK_TIMEOUT_S` (it couldn't start, Tk is broken...), the hub shows a
native macOS alert instead (``native_prompt``), so a phone can always be allowed.

**Downloads** asked for by the setup window run here, in the hub process
(:class:`Downloads`), so the hub's own loader and the window share one download
(:func:`shoutabout.modelstore.download` joins concurrent callers in a process).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from shoutabout.config import Paths
from shoutabout.mac.uilink import Link

log = logging.getLogger(__name__)

ACK_TIMEOUT_S = 12.0


def app_command() -> list[str]:
    """How to start ShoutAbout again: the app's executable, or ``python -m shoutabout``."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "shoutabout"]


def child_command(paths: Paths, command: str = "mac-ui") -> list[str]:
    return [*app_command(), "--home", str(paths.home), command]


class Downloads:
    """Model downloads the setup window asked for, one thread per model."""

    def __init__(self, download: Callable[..., Any] | None = None) -> None:
        self._download = download
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self.cancel = threading.Event()

    def start(self, model: str) -> None:
        with self._lock:
            job = self._jobs.get(model)
            if job is not None and job["state"] in ("running", "ready"):
                return
            job = {"state": "running", "done": 0, "total": 0, "error": None}
            self._jobs[model] = job
        threading.Thread(
            target=self._run, args=(model, job), name="setup-download", daemon=True
        ).start()

    def _run(self, model: str, job: dict[str, Any]) -> None:
        download = self._download
        if download is None:
            from shoutabout import modelstore

            download = modelstore.download

        def progress(done: int, total: int) -> None:
            with self._lock:
                job["done"], job["total"] = int(done), int(total)

        try:
            download(model, progress=progress, cancel=self.cancel)
        except Exception as exc:
            log.warning("speech model download for the setup window failed: %s", exc)
            with self._lock:
                job["state"], job["error"] = "failed", str(exc)
            return
        with self._lock:
            job["state"] = "ready"

    def status(self, model: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(model)
            return dict(job) if job is not None else {"state": "idle", "done": 0, "total": 0}


class Retries:
    """Recent > Try again asked for by the window process: one thread per recording (it
    can take a while; link calls must stay quick)."""

    def __init__(self, retry: Callable[[str], str]) -> None:
        self._retry = retry
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, item_id: str) -> None:
        with self._lock:
            job = self._jobs.get(item_id)
            if job is not None and job["state"] == "running":
                return
            job = {"state": "running", "text": "", "error": None}
            self._jobs[item_id] = job
        threading.Thread(
            target=self._run, args=(item_id, job), name="recent-retry", daemon=True
        ).start()

    def _run(self, item_id: str, job: dict[str, Any]) -> None:
        try:
            text = self._retry(item_id)
        except Exception as exc:
            with self._lock:
                job["state"], job["error"] = "failed", str(exc) or type(exc).__name__
            return
        with self._lock:
            job["state"], job["text"] = "done", str(text or "")

    def status(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(item_id)
            if job is None:
                return {"state": "idle"}
            if job["state"] != "running":
                self._jobs.pop(item_id, None)  # told once
            return dict(job)


class MacUiBridge:
    def __init__(
        self,
        services: Any,
        *,
        on_identity_changed: Callable[[], None] | None = None,
        native_prompt: Callable[[Any], None] | None = None,
        spawn: Callable[[list[str]], Any] | None = None,
        downloads: Downloads | None = None,
        ack_timeout: float = ACK_TIMEOUT_S,
    ) -> None:
        self.svc = services
        self._identity_changed = on_identity_changed
        self._native_prompt = native_prompt
        self._spawn = spawn or _spawn
        self.downloads = downloads or Downloads()
        self._ack_timeout = ack_timeout
        self._lock = threading.RLock()
        self._proc: Any = None
        self._link: Link | None = None
        self._seq = 0
        self._stopping = False
        self._acks: dict[str, threading.Event] = {}
        self.retries = Retries(self._retry)
        if services.pairing is not None:
            services.pairing.subscribe(self._on_pair_request, self._on_pair_finished)
        self._recent = getattr(services, "recent", None)
        if self._recent is not None:
            self._recent.subscribe(self._on_recent_changed)

    # ------------------------------------------------------------ public (any thread)

    def open_setup(self, mode: str = "full") -> None:
        self._send({"t": "open", "kind": "setup", "arg": mode or "full"})

    def open_window(self, page: str = "home") -> None:
        """The ShoutAbout window on ``page`` (the child's UiHost: ``_show_main``)."""
        self._send({"t": "open", "kind": "main", "arg": page or "home"})

    def open_settings(self) -> None:
        self.open_window("settings")

    def open_recent(self) -> None:
        self.open_window("recent")

    def open_teach(self) -> None:
        self.open_window("teach")

    @property
    def running(self) -> bool:
        with self._lock:
            return self._link is not None and not self._link.closed

    def is_open(self, _name: str) -> bool:
        return self.running

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._stopping = True
            link, proc = self._link, self._proc
            self._link = self._proc = None
        self.downloads.cancel.set()
        if self.svc.pairing is not None:
            self.svc.pairing.unsubscribe(self._on_pair_request)
            self.svc.pairing.unsubscribe(self._on_pair_finished)
        if self._recent is not None:
            self._recent.unsubscribe(self._on_recent_changed)
        if link is not None:
            link.send({"t": "quit"})
            link.close()
        if proc is not None:
            try:
                proc.wait(timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    log.debug("couldn't stop the window process", exc_info=True)

    # ------------------------------------------------------------ child process

    def _ensure_child(self) -> Link | None:
        """The link to a running window process, starting one if needed. Lock held."""
        if self._link is not None and not self._link.closed:
            return self._link
        try:
            proc = self._spawn(child_command(self.svc.paths))
        except OSError:
            log.exception("couldn't start the ShoutAbout window process")
            return None
        link = Link(
            proc.stdout,
            proc.stdin,
            on_event=self._on_child_event,
            on_call=self._on_child_call,
            on_close=lambda: self._on_child_closed(link),
            name="ui-bridge",
        )
        self._proc, self._link = proc, link
        link.start()
        log.info("started the window process")
        return link

    def _send(self, message: dict[str, Any], *, start: bool = True) -> bool:
        with self._lock:
            if self._stopping:
                return False
            if not start and (self._link is None or self._link.closed):
                return False
            link = self._ensure_child()
            if link is None:
                return False
            self._seq += 1
            return link.send({**message, "seq": self._seq})

    def _on_child_closed(self, link: Link) -> None:
        with self._lock:
            if self._link is link:
                self._link = None
                self._proc = None
        log.info("the window process ended")

    def _on_child_event(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "ack":
            event = self._acks.get(str(message.get("request_id")))
            if event is not None:
                event.set()
        elif kind == "idle":
            with self._lock:
                if int(message.get("seq") or 0) == self._seq and self._link is not None:
                    link = self._link
                    self._link = self._proc = None
                    link.send({"t": "quit"})

    def _on_child_call(self, op: str, args: dict[str, Any]) -> Any:
        pairing = self.svc.pairing
        if op == "approve":
            if pairing is None:
                raise RuntimeError("pairing isn't available")
            return bool(pairing.approve(str(args.get("request_id"))))
        if op == "deny":
            return bool(pairing.deny(str(args.get("request_id")))) if pairing else False
        if op == "settings_saved":
            hook = self.svc.on_settings_saved
            keys = {str(k) for k in args.get("keys") or []}
            return sorted(hook(keys)) if hook is not None else sorted(keys)
        if op == "identity_changed":
            reload = getattr(self.svc.identity, "reload", None)
            if callable(reload):
                reload()
            if self._identity_changed is not None:
                self._identity_changed()
            return None
        if op == "download":
            self.downloads.start(str(args.get("model")))
            return None
        if op == "download_status":
            return self.downloads.status(str(args.get("model")))
        if op == "recent":
            return self._recent.snapshot() if self._recent is not None else None
        if op == "remove_failed":
            return bool(self._recent.remove_failed(str(args.get("id")))) if self._recent else False
        if op == "retry":
            self.retries.start(str(args.get("id")))
            return None
        if op == "retry_status":
            return self.retries.status(str(args.get("id")))
        if op == "hub_status":
            return self.svc.hub_status()
        raise RuntimeError(f"unknown request {op!r}")

    def _retry(self, item_id: str) -> str:
        retry = self.svc.retry_failed
        if retry is None:
            raise RuntimeError("ShoutAbout can't write that out right now.")
        return str(retry(item_id) or "")

    def _on_recent_changed(self, version: int) -> None:
        """A dictation or failed recording came or went: tell an open window process."""
        self._send({"t": "recent", "version": int(version)}, start=False)

    # ------------------------------------------------------------ pairing

    def _on_pair_request(self, prompt: Any) -> None:
        pairing = self.svc.pairing
        seconds = pairing.seconds_left(prompt) if pairing is not None else 120.0
        ack = threading.Event()
        self._acks[prompt.request_id] = ack
        sent = self._send(
            {
                "t": "pair",
                "request_id": prompt.request_id,
                "code": prompt.code,
                "device_name": prompt.device_name,
                "seconds_left": round(seconds, 1),
            }
        )
        threading.Thread(
            target=self._await_ack, args=(prompt, ack, sent), name="pair-ack", daemon=True
        ).start()

    def _await_ack(self, prompt: Any, ack: threading.Event, sent: bool) -> None:
        try:
            if sent and ack.wait(self._ack_timeout):
                return
            pairing = self.svc.pairing
            if pairing is None or pairing.pending() is None:
                return
            if pairing.pending().request_id != prompt.request_id:
                return
            log.warning("the window process didn't show the pairing prompt; asking natively")
            if self._native_prompt is not None:
                self._native_prompt(prompt)
        finally:
            self._acks.pop(prompt.request_id, None)

    def _on_pair_finished(self, request_id: str, status: Any) -> None:
        self._send({"t": "pair-done", "request_id": request_id, "status": str(status)}, start=False)


def _log_file(paths: Paths) -> Any:
    from shoutabout.logging_setup import STD_LOG_FILE_NAME

    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        return open(paths.log_dir / STD_LOG_FILE_NAME, "ab")  # noqa: SIM115 - child owns it
    except OSError:
        return subprocess.DEVNULL


def _spawn(argv: list[str]) -> subprocess.Popen:
    from pathlib import Path

    home = Paths(Path(argv[argv.index("--home") + 1]))
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=_log_file(home),
        close_fds=True,
    )
