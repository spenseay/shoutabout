"""Optional PyAV (FFmpeg) decoder for source installs: Linux CI, developers, odd formats.

The Windows app doesn't ship PyAV (its wheels bundle GPL codec libraries; see
docs/licenses.md) and decodes with WAV + Media Foundation instead (:mod:`shoutabout.audio`).
In a source checkout PyAV is always there, because faster-whisper depends on it, so this
module is the fallback after Media Foundation on Windows and the main decoder elsewhere.
The module imports without PyAV (the self-test imports every module); its functions raise
ImportError when PyAV isn't installed (:func:`available` says beforehand).

This deliberately does not use ``faster_whisper.decode_audio`` (it passes
``metadata_errors=`` to ``av.open``, which PyAV 19 removed).

Uploaded files come from authenticated clients but are still parsed by FFmpeg, so the
demuxers are limited to plain audio/media containers (no playlists such as HLS or the
concat demuxer, which can make FFmpeg open other files or URLs) and only the ``file``
protocol is allowed.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import numpy as np

from shoutabout.audio import (
    _LIMIT_SLACK_S,
    SAMPLE_RATE,
    AudioError,
    _too_long,
)
from shoutabout.avshim import import_real_av, real_av_available

if TYPE_CHECKING:
    import av

log = logging.getLogger(__name__)

# FFmpeg demuxer names accepted for input. iOS records AAC in .m4a (the "mov,mp4,..."
# demuxer) or CAF; the rest covers what people commonly drop on the CLI.
ALLOWED_DEMUXERS = (
    "mov,mp4,m4a,3gp,3g2,mj2",
    "wav",
    "w64",
    "mp3",
    "aac",
    "ogg",
    "flac",
    "caf",
    "matroska,webm",
    "aiff",
    "amr",
)
_INPUT_OPTIONS = {
    "format_whitelist": ",".join(ALLOWED_DEMUXERS),
    "protocol_whitelist": "file",
}


def available() -> bool:
    """True when the real PyAV is installed (False with only the placeholder)."""
    return real_av_available()


def _av() -> ModuleType:
    import_real_av()  # ImportError when PyAV isn't installed (or only the placeholder is)
    import av
    import av.audio.resampler  # noqa: F401

    return av


def version() -> str:
    return str(_av().__version__)


def _open_input(path: Path) -> av.container.InputContainer:
    av = _av()
    try:
        return av.open(str(path), mode="r", container_options=dict(_INPUT_OPTIONS))
    except FileNotFoundError as exc:
        raise AudioError(f"Audio file not found: {path.name}") from exc
    except av.error.ArgumentError as exc:
        raise AudioError(
            f"Unsupported audio container in {path.name} "
            f"(supported: m4a/mp4/mov, wav, mp3, aac, ogg/opus, flac, caf, webm/mkv, aiff, amr)."
        ) from exc
    except (av.error.FFmpegError, ValueError, OSError, UnicodeDecodeError) as exc:
        raise AudioError(f"Could not read {path.name} as audio: {exc}") from exc


def _audio_stream(
    container: av.container.InputContainer, path: Path
) -> av.audio.stream.AudioStream:
    streams = container.streams.audio
    if not streams:
        raise AudioError(f"{path.name} contains no audio stream.")
    stream = streams[0]
    if stream.codec_context is None:
        raise AudioError(f"{path.name}: no decoder for the audio stream.")
    return stream


def _declared_duration(container: av.container.InputContainer, stream) -> float | None:
    av = _av()
    seconds: float | None = None
    if stream.duration is not None and stream.time_base is not None:
        seconds = float(stream.duration * stream.time_base)
    elif container.duration is not None:
        seconds = container.duration / av.time_base
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def probe_duration(path: Path) -> float | None:
    """Duration in seconds declared by the container, or None if unknown/unreadable."""
    av = _av()
    try:
        container = _open_input(Path(path))
    except AudioError:
        return None
    try:
        with container:
            streams = container.streams.audio
            if not streams:
                return None
            return _declared_duration(container, streams[0])
    except (av.error.FFmpegError, ValueError, OSError):
        return None


def decode_audio(path: Path, max_seconds: float | None = None) -> np.ndarray:
    """Decode the first audio stream of ``path`` to float32 mono 16 kHz in [-1, 1] with
    FFmpeg's own resampler (swresample).

    Raises AudioTooLongError if the declared or decoded duration exceeds ``max_seconds``
    (decoding stops as soon as the limit is passed), and AudioError for anything that
    isn't decodable audio or yields no samples.
    """
    av = _av()
    path = Path(path)
    container = _open_input(path)
    with container:
        stream = _audio_stream(container, path)
        limit = None if max_seconds is None else max_seconds + _LIMIT_SLACK_S
        declared = _declared_duration(container, stream)
        if limit is not None and declared is not None and declared > limit:
            raise _too_long(declared, max_seconds)
        max_samples = None if limit is None else int(limit * SAMPLE_RATE)

        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        chunks: list[np.ndarray] = []
        total = 0
        bad_packets = 0

        def take(frames: list[av.AudioFrame]) -> None:
            nonlocal total
            for out in frames:
                arr = out.to_ndarray().reshape(-1)
                if arr.size:
                    chunks.append(arr)
                    total += arr.size

        try:
            for packet in container.demux(stream):
                try:
                    frames = packet.decode()
                except av.error.InvalidDataError:
                    bad_packets += 1  # corrupt packet: skip it, like the ffmpeg CLI does
                    continue
                for frame in frames:
                    frame.pts = None  # don't let timestamp gaps upset the resampler
                    take(resampler.resample(frame))
                if max_samples is not None and total > max_samples:
                    raise _too_long(total / SAMPLE_RATE, max_seconds, at_least=True)
            take(resampler.resample(None))  # flush
        except AudioError:
            raise
        except (av.error.FFmpegError, ValueError, OSError, RuntimeError) as exc:
            raise AudioError(f"Could not decode {path.name}: {exc}") from exc

    if bad_packets:
        log.warning("skipped %d undecodable packet(s) in %s", bad_packets, path.name)
    if total == 0:
        raise AudioError(f"{path.name} contains no audio samples.")
    if max_samples is not None and total > max_samples:
        raise _too_long(total / SAMPLE_RATE, max_seconds)

    pcm = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    if pcm.dtype != np.int16:  # s16 is requested, but be defensive about the frame format
        raise AudioError(f"{path.name}: unexpected sample format {pcm.dtype} after resampling.")
    audio = pcm.astype(np.float32)
    audio *= 1.0 / 32768.0
    return audio


def encode_m4a(src: Path, dst: Path, bitrate: int = 64_000) -> None:
    """Dev/test helper: re-encode any audio file as mono AAC-LC in an .m4a (like an iPhone).

    Uses PyAV's native ``aac`` encoder at 44.1 kHz. Overwrites ``dst``.
    """
    av = _av()
    src, dst = Path(src), Path(dst)
    rate = 44_100
    container = _open_input(src)
    try:
        with container:
            in_stream = _audio_stream(container, src)
            with av.open(str(dst), mode="w", format="mp4") as out:
                ost = out.add_stream("aac", rate=rate, layout="mono")
                ost.bit_rate = int(bitrate)
                resampler = av.audio.resampler.AudioResampler(
                    format=ost.format.name, layout="mono", rate=rate
                )
                wrote = False

                def mux(frames: list[av.AudioFrame]) -> None:
                    nonlocal wrote
                    for frame in frames:
                        for packet in ost.encode(frame):
                            out.mux(packet)
                            wrote = True

                for frame in container.decode(in_stream):
                    frame.pts = None
                    mux(resampler.resample(frame))
                mux(resampler.resample(None))
                for packet in ost.encode(None):
                    out.mux(packet)
                    wrote = True
                if not wrote:
                    raise AudioError(f"{src.name} contains no audio samples.")
    except AudioError:
        dst.unlink(missing_ok=True)
        raise
    except (av.error.FFmpegError, ValueError, OSError, RuntimeError) as exc:
        dst.unlink(missing_ok=True)
        raise AudioError(f"Could not encode {src.name} to m4a: {exc}") from exc
