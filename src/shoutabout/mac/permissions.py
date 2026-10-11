"""The macOS privacy permissions ShoutAbout needs, checked and explained in plain words.

* **Microphone** (always asked by macOS the first time; the prompt shows the
  ``NSMicrophoneUsageDescription`` from the app's Info.plist).
* **Accessibility**: needed to paste (posting Cmd+V into another app) and, without Input
  Monitoring, to notice the hotkey. macOS shows its own "would like to control this
  computer" dialog with an *Open System Settings* button when we ask with the prompt flag.
* **Input Monitoring**: only needed if the hotkey listener can't run on Accessibility alone
  (see :mod:`shoutabout.mac.eventtap`).

Every probe returns ``None`` when it can't tell (not macOS, PyObjC missing), never raises.
macOS only lets the person grant these in System Settings; we only open the right pane.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

PANE = "x-apple.systempreferences:com.apple.preference.security?"
SETTINGS_URLS: dict[str, str] = {
    "accessibility": PANE + "Privacy_Accessibility",
    "input_monitoring": PANE + "Privacy_ListenEvent",
    "microphone": PANE + "Privacy_Microphone",
    "local_network": PANE + "Privacy_LocalNetwork",
}
WHERE = "System Settings > Privacy & Security"

# AVAuthorizationStatus
MIC_STATUS = {0: "undetermined", 1: "restricted", 2: "denied", 3: "granted"}

ACCESSIBILITY_HELP = (
    "To paste your words, ShoutAbout needs Accessibility. Open "
    f"{WHERE} > Accessibility and turn on ShoutAbout."
)
INPUT_MONITORING_HELP = (
    "To notice the shortcut key, ShoutAbout needs Input Monitoring. Open "
    f"{WHERE} > Input Monitoring and turn on ShoutAbout, then quit and reopen it."
)
MICROPHONE_HELP = (
    f"macOS is blocking the microphone. Open {WHERE} > Microphone and turn on ShoutAbout."
)


def _on_mac() -> bool:
    return sys.platform == "darwin"


def accessibility_trusted(prompt: bool = False) -> bool | None:
    """Is ShoutAbout allowed to control the computer (Accessibility)? ``prompt`` makes
    macOS show its own dialog when it isn't (once per launch at most)."""
    if not _on_mac():
        return None
    try:
        import ApplicationServices as AS

        if prompt:
            options = {AS.kAXTrustedCheckOptionPrompt: True}
            return bool(AS.AXIsProcessTrustedWithOptions(options))
        return bool(AS.AXIsProcessTrusted())
    except Exception:
        log.debug("couldn't read the Accessibility permission", exc_info=True)
        return None


def input_monitoring_granted() -> bool | None:
    """Input Monitoring (listen-only keyboard event taps), macOS 10.15+."""
    if not _on_mac():
        return None
    try:
        import Quartz

        fn = getattr(Quartz, "CGPreflightListenEventAccess", None)
        return bool(fn()) if fn is not None else None
    except Exception:
        log.debug("couldn't read the Input Monitoring permission", exc_info=True)
        return None


def request_input_monitoring() -> bool | None:
    """Ask for Input Monitoring (macOS shows its dialog the first time)."""
    if not _on_mac():
        return None
    try:
        import Quartz

        fn = getattr(Quartz, "CGRequestListenEventAccess", None)
        return bool(fn()) if fn is not None else None
    except Exception:
        log.debug("couldn't request Input Monitoring", exc_info=True)
        return None


def microphone_status() -> str:
    """ "granted" | "denied" | "restricted" | "undetermined" | "unknown"."""
    if not _on_mac():
        return "unknown"
    try:
        import AVFoundation as AV

        status = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)
        return MIC_STATUS.get(int(status), "unknown")
    except Exception:
        log.debug("couldn't read the microphone permission", exc_info=True)
        return "unknown"


def request_microphone(done: Callable[[bool], None] | None = None) -> bool:
    """Ask macOS for the microphone (its dialog appears; the answer comes later, on another
    thread, to ``done``). Returns False if the request couldn't be made."""
    if not _on_mac():
        return False
    try:
        import AVFoundation as AV

        def handler(granted: bool) -> None:
            log.info("microphone permission: %s", "granted" if granted else "denied")
            if done is not None:
                try:
                    done(bool(granted))
                except Exception:
                    log.exception("microphone permission callback failed")

        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AV.AVMediaTypeAudio, handler
        )
        return True
    except Exception:
        log.exception("couldn't ask for the microphone")
        return False


def open_settings(which: str, *, run: Callable[..., object] = subprocess.Popen) -> bool:
    """Open System Settings at a privacy pane ("accessibility", "microphone", ...)."""
    url = SETTINGS_URLS.get(which)
    if url is None:
        raise ValueError(f"unknown privacy pane {which!r}")
    if not _on_mac():
        return False
    try:
        run(["/usr/bin/open", url])
        return True
    except OSError:
        log.exception("couldn't open System Settings")
        return False


@dataclass(frozen=True)
class PermissionReport:
    microphone: str  # see microphone_status()
    accessibility: bool | None
    input_monitoring: bool | None

    @property
    def can_paste(self) -> bool:
        return self.accessibility is True

    @property
    def can_hear_hotkey(self) -> bool:
        """An event tap works with Input Monitoring (listen-only) or Accessibility."""
        return self.input_monitoring is True or self.accessibility is True

    def missing(self) -> list[str]:
        """What to fix, in the order to ask for it."""
        out = []
        if self.microphone in ("denied", "restricted"):
            out.append("microphone")
        if self.accessibility is False:
            out.append("accessibility")
        if not self.can_hear_hotkey and self.input_monitoring is False:
            out.append("input_monitoring")
        return out


def report() -> PermissionReport:
    return PermissionReport(
        microphone=microphone_status(),
        accessibility=accessibility_trusted(),
        input_monitoring=input_monitoring_granted(),
    )
