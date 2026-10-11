"""Read uncompressed WAV files with numpy: the decoder-free fast path.

Handles RIFF/RF64 WAVE with integer PCM (8/16/24/32-bit) or IEEE float (32/64-bit), also in
WAVE_FORMAT_EXTENSIBLE form. That covers what recorders, Windows text-to-speech and test
tools write. The stdlib ``wave`` module isn't used because it can't read float WAV files
(format 3). Compressed WAV variants (ADPCM, mu-law, GSM, ...) raise :class:`WavNotHandled`
so the caller can hand the file to a real decoder.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import numpy as np

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# KSDATAFORMAT_SUBTYPE_* GUIDs end with this; the first two bytes are the format tag.
_SUBTYPE_TAIL = bytes.fromhex("000000001000800000aa00389b71")
_MAX_CHUNKS = 1000  # don't walk a hostile file's chunk list forever


class WavError(ValueError):
    """The file says it's WAV but is broken (message is user-readable)."""


class WavNotHandled(Exception):
    """A valid WAV this reader doesn't decode (compressed); try another decoder."""


@dataclass(frozen=True)
class WavInfo:
    rate: int
    channels: int
    bits: int
    is_float: bool
    data_offset: int
    frames: int

    @property
    def seconds(self) -> float:
        return self.frames / self.rate

    @property
    def block_align(self) -> int:
        return self.channels * self.bits // 8


def is_wav(head: bytes) -> bool:
    return len(head) >= 12 and head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE"


def _format(chunk: bytes) -> tuple[int, int, int, int]:
    if len(chunk) < 16:
        raise WavError("the WAV format chunk is too short")
    tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", chunk, 0)
    if tag == WAVE_FORMAT_EXTENSIBLE:
        if len(chunk) < 40:
            raise WavError("the WAV extensible format chunk is too short")
        subformat = chunk[24:40]
        if subformat[2:] != _SUBTYPE_TAIL:
            raise WavNotHandled("WAV with a non-standard sub-format")
        tag = struct.unpack_from("<H", subformat, 0)[0]
    return tag, channels, rate, bits


def read_info(f: BinaryIO, file_size: int) -> WavInfo:
    """Parse the header of an open WAV file (positioned anywhere)."""
    f.seek(0)
    head = f.read(12)
    if not is_wav(head):
        raise WavNotHandled("not a WAV file")
    rf64 = head[:4] == b"RF64"
    ds64_data_size: int | None = None
    fmt: tuple[int, int, int, int] | None = None
    pos = 12
    for _ in range(_MAX_CHUNKS):
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            break
        chunk_id, size = header[:4], struct.unpack("<I", header[4:])[0]
        body = pos + 8
        if chunk_id == b"ds64" and rf64:
            ds64 = f.read(min(size, 28))
            if len(ds64) >= 16:
                ds64_data_size = struct.unpack_from("<Q", ds64, 8)[0]
        elif chunk_id == b"fmt ":
            fmt = _format(f.read(min(size, 64)))
        elif chunk_id == b"data":
            if fmt is None:
                raise WavError("the WAV data comes before its format")
            if rf64 and size == 0xFFFFFFFF and ds64_data_size is not None:
                size = ds64_data_size
            available = max(0, file_size - body)
            # Streaming writers leave 0 or 0xFFFFFFFF here: then the data runs to the end.
            if size in (0, 0xFFFFFFFF) or size > available:
                size = available
            return _info(fmt, body, size)
        pos = body + size + (size & 1)  # chunks are word aligned
        if pos > file_size:
            break
    raise WavError("the WAV file has no audio data" if fmt else "the WAV file has no format")


def _info(fmt: tuple[int, int, int, int], offset: int, size: int) -> WavInfo:
    tag, channels, rate, bits = fmt
    if tag == WAVE_FORMAT_PCM and bits in (8, 16, 24, 32):
        is_float = False
    elif tag == WAVE_FORMAT_IEEE_FLOAT and bits in (32, 64):
        is_float = True
    else:
        raise WavNotHandled(f"WAV format 0x{tag:04X} with {bits}-bit samples")
    if channels < 1 or channels > 32 or rate < 1000 or rate > 768_000:
        raise WavError(f"the WAV header is invalid ({channels} channels, {rate} Hz)")
    block = channels * bits // 8
    return WavInfo(rate, channels, bits, is_float, offset, size // block)


def read_samples(f: BinaryIO, info: WavInfo) -> np.ndarray:
    """All frames as interleaved float32 in [-1, 1]."""
    f.seek(info.data_offset)
    count = info.frames * info.channels
    width = info.bits // 8
    raw = f.read(count * width)
    count = len(raw) // width
    if info.is_float:
        out = np.frombuffer(raw, dtype="<f4" if width == 4 else "<f8", count=count)
        return out.astype(np.float32)  # always a copy, so never a read-only view
    if width == 1:
        out = np.frombuffer(raw, dtype=np.uint8, count=count).astype(np.float32)
        out -= 128.0
        out *= 1.0 / 128.0
        return out
    if width == 3:
        b = np.frombuffer(raw, dtype=np.uint8, count=count * 3).reshape(-1, 3)
        wide = np.zeros((b.shape[0], 4), dtype=np.uint8)
        wide[:, 1:] = b  # little endian: the low byte stays 0, so >> 8 sign-extends
        out = (wide.view("<i4").reshape(-1) >> 8).astype(np.float32)
        out *= 1.0 / 8388608.0
        return out
    dtype = "<i2" if width == 2 else "<i4"
    out = np.frombuffer(raw, dtype=dtype, count=count).astype(np.float32)
    out *= 1.0 / (32768.0 if width == 2 else 2147483648.0)
    return out


def probe(path: Path) -> WavInfo:
    path = Path(path)
    with path.open("rb") as f:
        return read_info(f, path.stat().st_size)
