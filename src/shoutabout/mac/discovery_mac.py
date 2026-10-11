"""Bonjour announcement on macOS with the built-in ``dns-sd`` tool.

Same contract as Windows (:mod:`shoutabout.discovery`): service ``_shoutabout._tcp`` in
``local.``, instance = the Mac's friendly name, TXT ``v``, ``id``, ``name``, ``ver``; no
secrets. ``dns-sd -R`` registers with macOS's own mDNSResponder and keeps the service up
for as long as it runs.

``dns-sd`` is started through a tiny ``/bin/sh`` watchdog that reads our pipe: when
ShoutAbout quits *or crashes*, the pipe closes, ``read`` returns and the watchdog stops
``dns-sd``, so a dead hub is never left announced. The arguments reach ``dns-sd`` as an
argument list (``"$@"``), never through a shell string.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from shoutabout.discovery import DOMAIN, SERVICE_TYPE, ServiceInfo, instance_label

log = logging.getLogger(__name__)

DNS_SD = "/usr/bin/dns-sd"
WATCHDOG = 'trap "" INT; "$@" >/dev/null 2>&1 & child=$!; read _; kill "$child" 2>/dev/null'


def dns_sd_command(info: ServiceInfo, exe: str = DNS_SD) -> list[str]:
    """``dns-sd -R <name> _shoutabout._tcp local <port> key=value...``"""
    txt = [f"{k}={v}" for k, v in info.txt]
    return [
        exe,
        "-R",
        instance_label(info.instance),
        SERVICE_TYPE,
        DOMAIN.rstrip("."),
        str(int(info.port)),
        *txt,
    ]


def watchdog_command(info: ServiceInfo, exe: str = DNS_SD) -> list[str]:
    return ["/bin/sh", "-c", WATCHDOG, "shoutabout-dns-sd", *dns_sd_command(info, exe)]


class MacAdvertiser:
    """Drop-in for :class:`shoutabout.discovery.Advertiser`. Thread-safe; never raises."""

    def __init__(self, *, spawn: Callable[[list[str]], Any] | None = None) -> None:
        self._spawn = spawn or _spawn
        self._lock = threading.Lock()
        self._proc: Any = None
        self._info: ServiceInfo | None = None
        self.available = True

    @property
    def info(self) -> ServiceInfo | None:
        with self._lock:
            return self._info

    def update(self, info: ServiceInfo) -> bool:
        with self._lock:
            if self._proc is not None and self._info == info and _running(self._proc):
                return True
            self._stop_locked()
            try:
                self._proc = self._spawn(watchdog_command(info))
            except OSError as exc:
                self.available = False
                log.warning("network discovery is unavailable: %s (pair with the QR code)", exc)
                return False
            self._info = info
        log.info(
            "announced on the network as %r (%s, port %d)",
            instance_label(info.instance),
            SERVICE_TYPE,
            info.port,
        )
        return True

    start = update

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._info = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()  # the watchdog stops dns-sd
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                log.debug("couldn't stop dns-sd", exc_info=True)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


def _running(proc: Any) -> bool:
    poll = getattr(proc, "poll", None)
    return poll is None or poll() is None


def _spawn(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
