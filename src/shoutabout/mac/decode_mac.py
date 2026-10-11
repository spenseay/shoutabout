"""Phone audio on macOS: decoded by Core Audio through the built-in ``afconvert`` tool.

The Mac app ships no FFmpeg/PyAV (GPL codecs; docs/licenses.md). ``afinfo`` reads the
declared duration first (so a too-long upload is refused before decoding), then
``afconvert -f WAVE -d LEF32`` turns the file into 32-bit float WAV at its own sample rate
and channel count; :mod:`shoutabout.wavdecode` reads that and :mod:`shoutabout.resample`
mixes to mono and resamples to 16 kHz, exactly like the Media Foundation path on Windows.
AAC priming and padding are trimmed by Core Audio from the file's own metadata.

Both tools are part of macOS (``/usr/bin``), take the file path as an argument (never a
shell), and run with a timeout. The temporary WAV is deleted before returning.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

AFCONVERT = "/usr/bin/afconvert"
AFINFO = "/usr/bin/afinfo"
TIMEOUT_S = 120.0
INFO_TIMEOUT_S = 15.0

# Sniffed containers (shoutabout.audio.sniff) that Core Audio reads.
CORE_AUDIO_KINDS = frozenset({"mp4", "mp3", "aac", "wav", "flac", "caf", "aiff", "amr"})

_DURATION = re.compile(r"estimated duration:\s*([0-9.]+)\s*sec", re.IGNORECASE)

Runner = Callable[..., subprocess.CompletedProcess]


class CoreAudioError(RuntimeError):
    """afconvert couldn't decode the file."""


class CoreAudioUnavailable(RuntimeError):
    """afconvert isn't there (not macOS)."""


@dataclass(frozen=True)
class Decoded:
    samples: np.ndarray  # float32, interleaved, as read from the WAV
    rate: int
    channels: int


def parse_duration(afinfo_output: str) -> float | None:
    match = _DURATION.search(afinfo_output)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def probe_seconds(path: Path, *, run: Runner = subprocess.run) -> float | None:
    """The duration ``afinfo`` reports, or None."""
    try:
        proc = run([AFINFO, str(path)], capture_output=True, text=True, timeout=INFO_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return parse_duration(proc.stdout or "")


def convert_command(src: Path, dst: Path) -> list[str]:
    return [AFCONVERT, "-f", "WAVE", "-d", "LEF32", str(src), str(dst)]


def decode(path: Path, *, run: Runner = subprocess.run) -> Decoded:
    """Decode ``path`` to float samples at its own rate/channels. Raises CoreAudioError or
    CoreAudioUnavailable."""
    from shoutabout import wavdecode

    if not Path(AFCONVERT).exists() and run is subprocess.run:
        raise CoreAudioUnavailable("afconvert isn't available (macOS only)")
    with tempfile.TemporaryDirectory(prefix="shoutabout-decode-") as tmp:
        out = Path(tmp) / "decoded.wav"
        try:
            proc = run(
                convert_command(path, out), capture_output=True, text=True, timeout=TIMEOUT_S
            )
        except FileNotFoundError as exc:
            raise CoreAudioUnavailable(str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise CoreAudioError("decoding took too long") from exc
        except OSError as exc:
            raise CoreAudioError(str(exc)) from exc
        if proc.returncode != 0 or not out.is_file():
            detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or ["failed"])[-1]
            raise CoreAudioError(detail[:200])
        size = out.stat().st_size
        with out.open("rb") as f:
            try:
                info = wavdecode.read_info(f, size)
                samples = wavdecode.read_samples(f, info)
            except (wavdecode.WavError, wavdecode.WavNotHandled) as exc:
                raise CoreAudioError(f"unexpected afconvert output: {exc}") from exc
    return Decoded(np.asarray(samples, dtype=np.float32), int(info.rate), int(info.channels))
