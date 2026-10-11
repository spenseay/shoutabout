"""Audio decoding: any recording -> 16 kHz mono float32 in [-1, 1], without shipping FFmpeg.

Decoders, tried in this order (:func:`default_backends`):

1. ``wav``: uncompressed WAV read with numpy (:mod:`shoutabout.wavdecode`), no decoder;
2. ``mediafoundation`` (Windows only): Windows Media Foundation through ctypes
   (:mod:`shoutabout.mfdecode`): AAC/M4A (what iPhones send), MP3, FLAC, ADTS AAC, WMA, ...;
3. ``pyav``: PyAV/FFmpeg (:mod:`shoutabout.pyavdecode`), only when it is installed, i.e.
   in source checkouts (Linux CI, developers). The Windows app doesn't ship it: PyAV's
   wheels bundle GPL codec libraries (docs/licenses.md).

On macOS (v2-macos) the second decoder is ``coreaudio`` instead: the built-in
``afconvert`` (:mod:`shoutabout.mac.decode_mac`), and the Mac app doesn't ship PyAV either.

Mono downmixing and resampling to 16 kHz happen here in numpy (:mod:`shoutabout.resample`,
a proper anti-aliasing polyphase filter), except on the PyAV path, which keeps FFmpeg's own
resampler. The container is recognised from the file's first bytes, never from the
client's file name, and Media Foundation is only handed formats on an allow-list (the
counterpart of the FFmpeg demuxer allow-list in :mod:`shoutabout.pyavdecode`).

Nothing here imports faster-whisper, so decoding is testable without the STT stack.
"""

from __future__ import annotations

import logging
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

# Container durations and AAC priming make lengths a little fuzzy; don't reject a
# 600.1 s file against a 600 s limit.
_LIMIT_SLACK_S = 0.5
_HEAD_BYTES = 64

WAV, MEDIA_FOUNDATION, PYAV = "wav", "mediafoundation", "pyav"
COREAUDIO = "coreaudio"  # v2-macos: afconvert (shoutabout.mac.decode_mac)
BACKENDS = (WAV, MEDIA_FOUNDATION, PYAV, COREAUDIO)

# Sniffed container -> the file extension that tells Media Foundation which handler to use.
# Formats missing here (CAF, unknown bytes) are never given to Media Foundation.
# Ogg, AIFF and CAF have no Media Foundation reader on Windows 11 (checked); Matroska/WebM
# and AMR do on current Windows, depending on the codecs installed.
MF_HINTS = {
    "mp4": ".m4a",
    "mp3": ".mp3",
    "aac": ".aac",
    "wav": ".wav",
    "flac": ".flac",
    "asf": ".wma",
    "matroska": ".webm",
    "amr": ".amr",
}
SUPPORTED_TEXT = "m4a/AAC, MP3, WAV, FLAC"
_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot")


class AudioError(ValueError):
    """The file is not decodable audio (corrupt, empty, unsupported or no audio stream)."""


class AudioTooLongError(AudioError):
    """The audio is longer than the allowed maximum."""

    def __init__(
        self,
        message: str,
        *,
        seconds: float | None = None,
        max_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.seconds = seconds
        self.max_seconds = max_seconds


class DecoderUnavailableError(AudioError):
    """No audio decoder works on this PC (e.g. Media Foundation missing on Windows 'N'
    editions). Not the file's fault: the server answers 503, not 415."""


@dataclass(frozen=True)
class DecodedAudio:
    samples: np.ndarray  # float32, mono, SAMPLE_RATE
    backend: str  # "wav" | "mediafoundation" | "pyav"
    source_rate: int | None = None
    channels: int | None = None


def _too_long(
    seconds: float | None, max_seconds: float, *, at_least: bool = False
) -> AudioTooLongError:
    if seconds is None:
        detail = ""
    elif at_least:
        detail = f" (more than {seconds:.1f} s)"
    else:
        detail = f" ({seconds:.1f} s)"
    return AudioTooLongError(
        f"Audio is longer than the {max_seconds:g} s limit{detail}.",
        seconds=seconds,
        max_seconds=max_seconds,
    )


class _NotApplicable(Exception):
    """This decoder doesn't handle the file (or isn't installed): try the next one."""


class _MFUnavailable(_NotApplicable):
    """Media Foundation is missing or broken on this PC (Windows 'N' editions)."""


# --------------------------------------------------------------------- sniffing


def sniff(head: bytes, after_id3: bytes = b"") -> str | None:
    """The container in ``head`` (the first bytes of a file): "mp4", "wav", "mp3", "aac"
    (ADTS), "flac", "ogg", "caf", "aiff", "amr", "matroska", "asf", or None if unknown.
    ``after_id3`` is the first bytes after a leading ID3 tag, when there is one."""
    if len(head) >= 12 and head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
        return "wav"
    if len(head) >= 8 and head[4:8] in _MP4_BOXES:
        return "mp4"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"caff":
        return "caf"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if head[:16] == _ASF_GUID:
        return "asf"
    if head[:3] == b"ID3":
        return "aac" if _frame_sync(after_id3) == "aac" else "mp3"
    return _frame_sync(head)


def _frame_sync(data: bytes) -> str | None:
    """ "aac" for an ADTS header, "mp3" for an MPEG audio frame header, else None."""
    if len(data) < 3 or data[0] != 0xFF:
        return None
    b1, b2 = data[1], data[2]
    if b1 & 0xF6 == 0xF0:  # 12-bit sync, layer 00: ADTS
        return "aac"
    version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
    if b1 & 0xE0 == 0xE0 and version != 1 and layer != 0 and b2 >> 4 != 15 and (b2 >> 2) & 3 != 3:
        return "mp3"
    return None


def sniff_file(path: Path) -> tuple[str | None, int]:
    """(container, file size). Raises AudioError for a missing or empty file."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(_HEAD_BYTES)
            after = b""
            if head[:3] == b"ID3" and len(head) >= 10:
                tag = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                f.seek(10 + tag + (10 if head[5] & 0x10 else 0))
                after = f.read(4)
    except FileNotFoundError as exc:
        raise AudioError(f"Audio file not found: {path.name}") from exc
    except OSError as exc:
        raise AudioError(f"Could not read {path.name}: {exc.strerror or exc}") from exc
    if size == 0:
        raise AudioError(f"{path.name} is empty: no audio samples.")
    return sniff(head, after), size


# --------------------------------------------------------------------- backends


def default_backends() -> tuple[str, ...]:
    if sys.platform == "win32":
        return (WAV, MEDIA_FOUNDATION, PYAV)
    if sys.platform == "darwin":  # v2-macos
        return (WAV, COREAUDIO, PYAV)
    return (WAV, PYAV)


def _limit(max_seconds: float | None) -> float | None:
    return None if max_seconds is None else max_seconds + _LIMIT_SLACK_S


def _finish(samples: np.ndarray, path: Path, max_seconds: float | None) -> np.ndarray:
    if samples.size == 0:
        raise AudioError(f"{path.name} contains no audio samples.")
    limit = _limit(max_seconds)
    if limit is not None and samples.size > int(limit * SAMPLE_RATE):
        raise _too_long(samples.size / SAMPLE_RATE, max_seconds)
    out = np.ascontiguousarray(samples, dtype=np.float32)
    np.clip(out, -1.0, 1.0, out=out)
    return out


def _decode_wav(path: Path, size: int, max_seconds: float | None) -> DecodedAudio:
    from shoutabout import wavdecode
    from shoutabout.resample import to_16k_mono

    try:
        with path.open("rb") as f:
            info = wavdecode.read_info(f, size)
            limit = _limit(max_seconds)
            if limit is not None and info.seconds > limit:
                raise _too_long(info.seconds, max_seconds)
            interleaved = wavdecode.read_samples(f, info)
    except wavdecode.WavNotHandled as exc:
        raise _NotApplicable(str(exc)) from exc
    except wavdecode.WavError as exc:
        raise AudioError(f"Could not read {path.name} as WAV: {exc}.") from exc
    except (OSError, struct.error) as exc:
        raise AudioError(f"Could not read {path.name} as WAV: {exc}") from exc
    samples = to_16k_mono(interleaved, info.rate, info.channels, SAMPLE_RATE)
    return DecodedAudio(_finish(samples, path, max_seconds), WAV, info.rate, info.channels)


def _decode_mf(path: Path, kind: str | None, max_seconds: float | None) -> DecodedAudio:
    if kind not in MF_HINTS:
        raise _NotApplicable(f"{kind or 'unknown'} format isn't handed to Media Foundation")
    from shoutabout import mfdecode
    from shoutabout.resample import to_16k_mono

    limit = _limit(max_seconds)
    try:
        decoded = mfdecode.decode(path, hint=MF_HINTS[kind], max_seconds=limit)
    except mfdecode.MFUnavailableError as exc:
        raise _MFUnavailable(str(exc)) from exc
    except mfdecode.MFTooLongError as exc:
        raise _too_long(exc.seconds, max_seconds or 0.0, at_least=exc.at_least) from exc
    except mfdecode.MFNoAudioError as exc:
        raise AudioError(f"{path.name} contains no audio stream.") from exc
    except mfdecode.MFError as exc:
        raise AudioError(f"Could not decode {path.name}: {exc}") from exc
    except FileNotFoundError as exc:
        raise AudioError(f"Audio file not found: {path.name}") from exc
    except OSError as exc:  # a ctypes/COM failure: report it as undecodable, not a crash
        raise AudioError(f"Could not decode {path.name}: {exc}") from exc
    parts = [to_16k_mono(s.samples(), s.rate, s.channels, SAMPLE_RATE) for s in decoded.segments]
    samples = np.concatenate(parts) if len(parts) > 1 else (parts[0] if parts else np.zeros(0))
    first = decoded.segments[0] if decoded.segments else None
    return DecodedAudio(
        _finish(samples, path, max_seconds),
        MEDIA_FOUNDATION,
        first.rate if first else None,
        first.channels if first else None,
    )


# --- v2-macos: Core Audio through afconvert
class _CoreAudioUnavailable(_NotApplicable):
    """afconvert is missing (not macOS)."""


def _decode_coreaudio(path: Path, kind: str | None, max_seconds: float | None) -> DecodedAudio:
    from shoutabout.mac import decode_mac
    from shoutabout.resample import to_16k_mono

    if kind not in decode_mac.CORE_AUDIO_KINDS:
        raise _NotApplicable(f"{kind or 'unknown'} format isn't handed to Core Audio")
    limit = _limit(max_seconds)
    declared = decode_mac.probe_seconds(path)
    if limit is not None and declared is not None and declared > limit:
        raise _too_long(declared, max_seconds or 0.0)
    try:
        decoded = decode_mac.decode(path)
    except decode_mac.CoreAudioUnavailable as exc:
        raise _CoreAudioUnavailable(str(exc)) from exc
    except decode_mac.CoreAudioError as exc:
        raise AudioError(f"Could not decode {path.name}: {exc}") from exc
    samples = to_16k_mono(decoded.samples, decoded.rate, decoded.channels, SAMPLE_RATE)
    return DecodedAudio(
        _finish(samples, path, max_seconds), COREAUDIO, decoded.rate, decoded.channels
    )


# --- end v2-macos


def _decode_pyav(path: Path, max_seconds: float | None) -> DecodedAudio:
    from shoutabout import pyavdecode

    if not pyavdecode.available():
        raise _NotApplicable("PyAV isn't installed")
    samples = pyavdecode.decode_audio(path, max_seconds)
    return DecodedAudio(_finish(samples, path, max_seconds), PYAV)


def decode_audio_info(
    path: Path,
    max_seconds: float | None = None,
    *,
    backends: Sequence[str] | None = None,
) -> DecodedAudio:
    """Like :func:`decode_audio`, and also says which decoder did it. ``backends``
    restricts or reorders the decoders (tests, benchmarks, the self-test)."""
    path = Path(path)
    order = tuple(default_backends() if backends is None else backends)
    unknown = [b for b in order if b not in BACKENDS]
    if unknown:
        raise ValueError(f"unknown audio backend(s) {unknown}; choose from {BACKENDS}")
    kind, size = sniff_file(path)

    first_error: AudioError | None = None
    mf_missing: str | None = None
    for backend in order:
        try:
            if backend == WAV:
                if kind != "wav":
                    continue
                result = _decode_wav(path, size, max_seconds)
            elif backend == MEDIA_FOUNDATION:
                result = _decode_mf(path, kind, max_seconds)
            elif backend == COREAUDIO:  # v2-macos
                result = _decode_coreaudio(path, kind, max_seconds)
            else:
                result = _decode_pyav(path, max_seconds)
        except AudioTooLongError:
            raise  # a length verdict doesn't change with another decoder
        except _NotApplicable as exc:
            if isinstance(exc, _MFUnavailable):
                mf_missing = str(exc)
            log.debug("%s: %s skipped (%s)", path.name, backend, exc)
            continue
        except AudioError as exc:
            log.info("%s: %s couldn't decode it: %s", path.name, backend, exc)
            if first_error is None:
                first_error = exc
            continue
        if first_error is not None:
            log.info("%s: decoded by %s after %s", path.name, backend, first_error)
        return result

    if first_error is not None:
        raise first_error
    if mf_missing is not None and kind in MF_HINTS:
        # Not the file's fault: Media Foundation itself is missing (Windows 'N' editions).
        raise DecoderUnavailableError(mf_missing)
    what = "isn't a recognised audio format" if kind is None else f"is {kind.upper()} audio"
    raise AudioError(
        f"{path.name} {what}, which this PC can't decode (supported: {SUPPORTED_TEXT})."
    )


def decode_audio(path: Path, max_seconds: float | None = None) -> np.ndarray:
    """Decode the first audio stream of ``path`` to float32 mono 16 kHz in [-1, 1].

    Raises AudioTooLongError if the declared or decoded duration exceeds ``max_seconds``
    (decoding stops as soon as the limit is passed), DecoderUnavailableError when no
    decoder works on this PC, and AudioError for anything that isn't decodable audio or
    yields no samples.
    """
    return decode_audio_info(path, max_seconds).samples


def probe_duration(path: Path) -> float | None:
    """Duration in seconds declared by the container, or None if unknown/unreadable."""
    path = Path(path)
    try:
        kind, size = sniff_file(path)
    except AudioError:
        return None
    if kind == "wav":
        from shoutabout import wavdecode

        try:
            with path.open("rb") as f:
                return wavdecode.read_info(f, size).seconds
        except (wavdecode.WavError, wavdecode.WavNotHandled, OSError, struct.error):
            pass
    if sys.platform == "win32" and kind in MF_HINTS:
        from shoutabout import mfdecode

        seconds = mfdecode.probe_seconds(path, hint=MF_HINTS[kind])
        if seconds is not None:
            return seconds
    if sys.platform == "darwin":  # v2-macos
        from shoutabout.mac import decode_mac

        seconds = decode_mac.probe_seconds(path)
        if seconds is not None:
            return seconds
    from shoutabout import pyavdecode

    return pyavdecode.probe_duration(path) if pyavdecode.available() else None


def encode_m4a(src: Path, dst: Path, bitrate: int = 64_000) -> None:
    """Dev/test helper: re-encode any audio file as mono AAC-LC in an .m4a (like an iPhone).

    Needs PyAV (source checkouts have it; the Windows app doesn't). Overwrites ``dst``.
    """
    from shoutabout import pyavdecode

    if not pyavdecode.available():
        raise AudioError(
            "encode-m4a needs PyAV (FFmpeg), which this installation doesn't include. "
            "Use a source checkout (uv sync) to make test files."
        )
    pyavdecode.encode_m4a(src, dst, bitrate=bitrate)
