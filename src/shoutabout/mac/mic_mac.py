"""The microphone on macOS: :class:`shoutabout.mic.MicRecorder` (sounddevice / PortAudio's
Core Audio backend) with macOS permission handling and macOS wording.

macOS asks once per app ("ShoutAbout would like to access the microphone", with the
reason from the app's ``NSMicrophoneUsageDescription``). While the answer is pending, or
after "Don't Allow", Core Audio hands the app silence instead of an error, so the
permission is checked before the device is opened:

* not asked yet: ask now (macOS shows its dialog) and say "Allow the mic" instead of
  recording silence;
* denied or restricted: say where to turn it on;
* granted: record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from shoutabout.mac import permissions
from shoutabout.mic import MicError, MicRecorder

BLOCKED = permissions.MICROPHONE_HELP
ASKING = "macOS is asking whether ShoutAbout may use the microphone. Click Allow, then try again."
NO_MIC = "No microphone found. Plug one in, or pick one in System Settings > Sound > Input."
SILENT = (
    "The microphone only sent silence. Check that it isn't muted, and that the right one is "
    "picked in System Settings > Sound > Input."
)


def _no_registry(_hive: str, _key: str) -> str | None:
    return None  # the Windows privacy switches don't exist here


def mac_error(exc: MicError, status: str) -> MicError:
    """Reword a :class:`MicError` from the shared recorder for macOS."""
    if status in ("denied", "restricted"):
        return MicError(BLOCKED, "Mic blocked")
    if exc.short == "No microphone":
        return MicError(NO_MIC, "No microphone")
    if exc.short in ("Mic is silent", "Mic blocked"):
        return MicError(SILENT, "Mic is silent")
    return exc


class MacMicRecorder(MicRecorder):
    def __init__(
        self,
        *,
        mic_status: Callable[[], str] = permissions.microphone_status,
        ask: Callable[[], Any] = permissions.request_microphone,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("privacy_reader", _no_registry)
        super().__init__(**kwargs)
        self._status = mic_status
        self._ask = ask

    def start(self) -> None:
        status = self._status()
        if status in ("denied", "restricted"):
            raise MicError(BLOCKED, "Mic blocked")
        if status == "undetermined":
            self._ask()
            raise MicError(ASKING, "Allow the mic")
        try:
            super().start()
        except MicError as exc:
            raise mac_error(exc, self._status()) from exc

    def stop(self) -> np.ndarray:
        try:
            return super().stop()
        except MicError as exc:
            raise mac_error(exc, self._status()) from exc
