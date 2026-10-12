"""The window process on macOS: the shared Tk setup/settings windows and pairing prompts.

Why a separate process: Cocoa and Tk both want the main thread (Tk on macOS owns the
NSApplication and must run there), and the hub's main thread already runs the menu bar,
the overlay and the event loop. So the hub (:mod:`shoutabout.mac.ui_bridge`) starts
``ShoutAbout mac-ui`` when a window is needed; it runs the same
:class:`shoutabout.ui_host.UiHost` as Windows, on *its* main thread, and talks to the hub
over its stdin/stdout (:mod:`shoutabout.mac.uilink`):

* the hub sends ``open`` (setup / settings), ``pair`` and ``pair-done`` events, ``quit``;
* the windows call the hub for what only the hub can do: approve or deny a phone
  (:class:`RemotePairing`), apply saved settings live, re-announce a new name, download
  the speech model (so only one process ever writes it), open the Teach window, read and
  ask for the macOS permissions (:class:`shoutabout.mac.access.RemoteAccess`).

Everything else the windows need (config.toml, devices.json, identity.json, the LaunchAgent)
is files they read and write directly, as on Windows; the hub sees changes by mtime.

When no window has been open for a moment the process says ``idle`` (with the number of
the last hub message it handled) and the hub tells it to quit, unless it has sent
something newer. If the hub goes away, the pipe closes and this process quits too.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from shoutabout.config import Paths
from shoutabout.mac.uilink import Link
from shoutabout.pair_requests import PairPrompt
from shoutabout.ui_host import IDLE_EXIT_MS, PUMP_MS, UiHost

log = logging.getLogger(__name__)

DOWNLOAD_POLL_S = 0.25


class RemotePairing:
    """The :class:`shoutabout.pair_requests.PairingManager` methods the windows use, backed
    by the hub over the link."""

    def __init__(self, link: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._link = link
        self._clock = clock
        self._lock = threading.Lock()
        self._prompts: dict[str, PairPrompt] = {}
        self._on_request: list[Callable[[PairPrompt], None]] = []
        self._on_finished: list[Callable[[str, str], None]] = []
        self.enabled = True

    def subscribe(
        self,
        on_request: Callable[[PairPrompt], None] | None = None,
        on_finished: Callable[[str, str], None] | None = None,
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
                    group.remove(callback)  # type: ignore[arg-type]

    # -- events from the hub

    def add(self, request_id: str, code: str, device_name: str, seconds_left: float) -> PairPrompt:
        prompt = PairPrompt(
            request_id=str(request_id),
            code=str(code),
            device_name=str(device_name),
            deadline=self._clock() + max(0.0, float(seconds_left)),
        )
        with self._lock:
            self._prompts[prompt.request_id] = prompt
            listeners = list(self._on_request)
        for listener in listeners:
            try:
                listener(prompt)
            except Exception:
                log.exception("pairing listener failed")
        return prompt

    def finish(self, request_id: str, status: str) -> None:
        with self._lock:
            self._prompts.pop(request_id, None)
            listeners = list(self._on_finished)
        for listener in listeners:
            try:
                listener(request_id, status)
            except Exception:
                log.exception("pairing listener failed")

    # -- what the windows call

    def pending(self) -> PairPrompt | None:
        now = self._clock()
        with self._lock:
            for prompt in self._prompts.values():
                if prompt.deadline > now:
                    return prompt
        return None

    def seconds_left(self, prompt: PairPrompt) -> float:
        return max(0.0, prompt.deadline - self._clock())

    def approve(self, request_id: str) -> bool:
        return bool(self._link.call("approve", request_id=request_id))

    def deny(self, request_id: str) -> bool:
        try:
            return bool(self._link.call("deny", request_id=request_id))
        except Exception:
            log.warning("couldn't tell the hub about Don't allow", exc_info=True)
            return False


def remote_download(link: Any) -> Callable[..., Any]:
    """``AppServices.download_model`` that lets the hub download (one writer only)."""

    def download(stt_model: str, *, progress: Callable[[int, int], None], cancel: Any) -> None:
        link.call("download", model=stt_model)
        while not cancel.is_set():
            status = link.call("download_status", model=stt_model) or {}
            done, total = int(status.get("done") or 0), int(status.get("total") or 0)
            if done or total:
                progress(done, total)
            state = status.get("state")
            if state == "ready":
                return
            if state == "failed":
                raise RuntimeError(str(status.get("error") or "the download failed"))
            cancel.wait(DOWNLOAD_POLL_S)
        raise RuntimeError("cancelled")

    return download


class ChildUiHost(UiHost):
    """:class:`UiHost` on this process's main thread, fed by the hub's messages."""

    def __init__(self, services: Any, link: Any, pairing: RemotePairing) -> None:
        self.link = link
        self.remote = pairing
        self._last_seq = 0
        self._idle_sent = False
        super().__init__(services)

    # -- from the reader thread

    def on_event(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        seq = int(message.get("seq") or 0)
        if kind == "open":
            what = str(message.get("kind") or "")
            if what == "setup":
                self._send("setup", str(message.get("arg") or "full"))
            elif what:
                self._send(what, message.get("arg"))
        elif kind == "pair":
            self.remote.add(
                str(message.get("request_id")),
                str(message.get("code")),
                str(message.get("device_name")),
                float(message.get("seconds_left") or 0.0),
            )
            self.link.send({"t": "ack", "request_id": message.get("request_id")})
        elif kind == "pair-done":
            self.remote.finish(str(message.get("request_id")), str(message.get("status")))
        elif kind == "quit":
            self._requests.put(("quit", None))
        if seq:
            self._last_seq = max(self._last_seq, seq)  # after queueing (see _pump)

    def on_link_closed(self) -> None:
        self._requests.put(("quit", None))

    # -- UiHost overrides

    def _send(self, kind: str, arg: Any, *, start: bool = True) -> None:
        with self._lock:
            if self._stopping:
                return
            self._requests.put((kind, arg))  # the main thread runs the Tk loop already

    def run(self) -> None:
        """The Tk loop on this (main) thread, until the hub says quit."""
        self._session()

    def _handle(self, kind: str, arg: Any) -> None:
        if kind == "settings":
            self._show_settings()
        elif kind in ("setup", "pair", "pair-done"):
            super()._handle(kind, arg)
        else:
            opener = getattr(self, f"_show_{kind}", None)  # windows added later (history...)
            if callable(opener):
                opener()
            else:
                log.warning("unknown window %r", kind)
                return
        if kind not in ("pair", "pair-done"):
            self._bring_forward(kind)

    def _bring_forward(self, kind: str) -> None:
        window = self._windows.get(kind)
        win = getattr(window, "win", None)
        try:
            from AppKit import NSApplication

            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            log.debug("couldn't activate the window process", exc_info=True)
        if win is not None:
            try:  # float above other apps for a moment, in case macOS kept us behind
                win.attributes("-topmost", True)
                win.after(900, lambda: win.attributes("-topmost", False))
                win.lift()
                win.focus_force()
            except Exception:
                log.debug("couldn't raise the window", exc_info=True)

    def _pump(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                kind, arg = self._requests.get_nowait()
                if kind == "quit":
                    root.quit()
                    return
                self._handle(kind, arg)
        except queue.Empty:
            pass
        except Exception:
            log.exception("window request failed")
        if self._windows:
            self._idle_ms = 0
            self._idle_sent = False
        else:
            self._idle_ms += PUMP_MS
            if self._idle_ms >= IDLE_EXIT_MS and not self._idle_sent:
                seq = self._last_seq  # read before the emptiness check: see on_event
                if self._requests.empty():
                    self.link.send({"t": "idle", "seq": seq})
                    self._idle_sent = True
        root.after(PUMP_MS, self._pump)


# ------------------------------------------------------------------ services


def mac_services(paths: Paths, link: Any, pairing: RemotePairing) -> Any:
    from shoutabout import __version__
    from shoutabout.app_services import AppServices
    from shoutabout.devices import DeviceStore
    from shoutabout.identity import IdentityStore
    from shoutabout.mac import keys
    from shoutabout.mac.access import RemoteAccess
    from shoutabout.mac.startup_mac import MacStartupManager

    def settings_saved(changed: set[str]) -> set[str]:
        return set(link.call("settings_saved", keys=sorted(changed)) or [])

    def identity_changed() -> None:
        link.call("identity_changed")

    def open_teach() -> None:
        link.call("open_teach")

    def open_logs() -> None:
        import subprocess

        paths.log_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["/usr/bin/open", str(paths.log_dir)])

    def copy_hub_token() -> None:
        from shoutabout.config import load_token
        from shoutabout.mac.paste import CONCEALED, CocoaPasteboard

        CocoaPasteboard().write_text(load_token(paths), (CONCEALED,))

    return AppServices(
        paths=paths,
        devices=DeviceStore(paths.devices_file),
        identity=IdentityStore(paths.identity_file),
        pairing=pairing,
        state=None,
        hotkey_choices=lambda: dict(keys.MAC_HOTKEY_CHOICES),
        hint_text=keys.hint_text,
        download_model=remote_download(link),
        on_settings_saved=settings_saved,
        on_identity_changed=identity_changed,
        open_teach=open_teach,
        open_logs=open_logs,
        copy_hub_token=copy_hub_token,
        startup=MacStartupManager(),
        permissions=RemoteAccess(link),  # the hub reads and asks for them
        version=__version__,
    )


def _link_streams() -> tuple[Any, Any]:
    """Our private copies of stdin/stdout for the link; fd 0/1 then point elsewhere, so a
    stray print() from a library can't corrupt the messages."""
    reader = os.fdopen(os.dup(0), "rb", buffering=0)
    writer = os.fdopen(os.dup(1), "wb", buffering=0)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(2, 1)  # stdout -> stderr (the hub sends that to the log folder)
    os.close(devnull)
    sys.stdout = sys.stderr
    return reader, writer


def child_logging(paths: Paths, name: str = "ui.log") -> None:
    """The window processes log to their own file next to hub.log (two processes rotating
    one file would trip over each other)."""
    from logging.handlers import RotatingFileHandler

    from shoutabout.logging_setup import LOG_FORMAT

    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            paths.log_dir / name, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
    except OSError:
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _open_with_finder(path: Any) -> None:
    """``os.startfile`` for the shared windows (Teach > open vocab.txt) on a Mac."""
    import subprocess

    subprocess.Popen(["/usr/bin/open", "-t", str(path)])


def run_child(paths: Paths) -> int:
    """``shoutabout mac-ui``: the window process (started by the hub, never by hand)."""
    from shoutabout.mac import tkmac

    reader, writer = _link_streams()
    child_logging(paths)
    log.info("window process started (pid %d)", os.getpid())
    tkmac.install()
    hosts: list[ChildUiHost] = []

    def on_event(message: dict[str, Any]) -> None:
        if hosts:
            hosts[0].on_event(message)

    def on_close() -> None:
        if hosts:
            hosts[0].on_link_closed()

    link = Link(reader, writer, on_event=on_event, on_close=on_close, name="ui-link")
    pairing = RemotePairing(link)
    host = ChildUiHost(mac_services(paths, link, pairing), link, pairing)
    hosts.append(host)
    link.start()
    try:
        host.run()
    except Exception:
        log.exception("the window process failed")
        return 1
    finally:
        link.close()
        log.info("window process ended")
    return 0


class MainThreadUiHost(UiHost):
    """:class:`UiHost` run on the calling (main) thread: ``shoutabout setup`` / ``settings``
    typed in a Terminal on a Mac, without a running hub (like Windows' standalone mode)."""

    def _send(self, kind: str, arg: Any, *, start: bool = True) -> None:
        with self._lock:
            if not self._stopping:
                self._requests.put((kind, arg))

    def run(self) -> None:
        self._session()  # ends when the last window closes (UiHost's idle exit)


def run_standalone(paths: Paths, kind: str, arg: Any) -> int:
    from shoutabout.cli import standalone_services
    from shoutabout.mac import keys, tkmac
    from shoutabout.mac.startup_mac import MacStartupManager

    tkmac.install()
    services = standalone_services(paths)
    services.hotkey_choices = lambda: dict(keys.MAC_HOTKEY_CHOICES)
    services.hint_text = keys.hint_text
    services.startup = MacStartupManager()
    host = MainThreadUiHost(services)
    if kind == "setup":
        host.open_setup(arg or "full")
    else:
        host.open_settings()
    host.run()
    return 0


def run_teach(paths: Paths) -> int:
    """``shoutabout mac-teach``: the Teach window in its own process (Tk needs a main thread)."""
    from shoutabout.mac import tkmac
    from shoutabout.review_ui import run_review_window

    child_logging(paths, "teach.log")
    if not hasattr(os, "startfile"):
        os.startfile = _open_with_finder  # type: ignore[attr-defined]
    tkmac.install()
    run_review_window(paths)
    return 0
