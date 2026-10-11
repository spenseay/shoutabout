"""What this Mac is: the Apple chip's name and its performance cores (``sysctl``).

``machdep.cpu.brand_string`` is e.g. "Apple M2 Pro"; ``hw.perflevel0.physicalcpu`` counts
the performance cores (macOS 12+; the efficiency cores are ``perflevel1``). Whisper runs on
the performance cores: the efficiency cores are much slower and only add waiting.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

SYSCTL = "/usr/sbin/sysctl"
TIMEOUT_S = 5.0

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class AppleChip:
    name: str  # "Apple M2 Pro"
    performance_cores: int
    efficiency_cores: int

    @property
    def generation(self) -> int | None:
        """1 for M1, 2 for M2, ... (None if the name doesn't say)."""
        parts = self.name.replace("Apple", "").split()
        if parts and parts[0].upper().startswith("M") and parts[0][1:].isdigit():
            return int(parts[0][1:])
        return None

    @property
    def variant(self) -> str:
        """ "", "Pro", "Max" or "Ultra"."""
        parts = self.name.split()
        for word in ("Ultra", "Max", "Pro"):
            if word in parts:
                return word
        return ""


def sysctl(name: str, *, run: Runner = subprocess.run) -> str | None:
    try:
        proc = run([SYSCTL, "-n", name], capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _int(value: str | None) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def probe_apple_chip(
    *, run: Runner = subprocess.run, machine: str | None = None
) -> AppleChip | None:
    """The Apple chip, or None on anything that isn't an Apple Silicon Mac."""
    if machine is None:
        if sys.platform != "darwin":
            return None
        machine = platform.machine()
    if machine != "arm64":
        return None
    name = sysctl("machdep.cpu.brand_string", run=run) or "Apple Silicon"
    perf = _int(sysctl("hw.perflevel0.physicalcpu", run=run))
    eff = _int(sysctl("hw.perflevel1.physicalcpu", run=run))
    if perf <= 0:  # older macOS: no perf levels; assume half the cores are fast ones
        total = _int(sysctl("hw.physicalcpu", run=run))
        perf, eff = max(1, total // 2), max(0, total - total // 2)
    return AppleChip(" ".join(name.split()), perf, eff)


def computer_name(*, run: Runner = subprocess.run) -> str | None:
    """The Mac's name from System Settings > General > Sharing ("Sam's MacBook Pro")."""
    if sys.platform != "darwin":
        return None
    try:
        proc = run(
            ["/usr/sbin/scutil", "--get", "ComputerName"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None
