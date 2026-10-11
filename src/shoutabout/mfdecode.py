"""Decode compressed audio with Windows Media Foundation, through ctypes.

Why: the installed app must not ship FFmpeg (PyAV's wheels bundle a GPL build of it), and
every Windows 10/11 PC already has Media Foundation with AAC, MP3, FLAC and WAV support.
This module talks to it directly, with plain ctypes COM calls (no comtypes, no pywin32):

    bytes or file -> IMFByteStream -> IMFSourceResolver -> IMFMediaSource
                  -> IMFSourceReader (first audio stream, 32-bit float PCM out)
                  -> interleaved float32 at the file's own rate and channel count.

Downmixing and resampling to 16 kHz are left to :mod:`shoutabout.resample`, so the result
doesn't depend on which Windows resampler a PC happens to have.

Threading: every call initialises COM on the calling thread (multithreaded apartment, or
whatever the thread already has) and undoes exactly what it did; ``MFStartup`` runs once per
process. Each call builds and releases its own objects, so calls from different threads
never share anything.

Windows "N" editions ship without Media Foundation until the Media Feature Pack is
installed; :class:`MFUnavailableError` says so in plain words.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import uuid
from collections.abc import Callable
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_long,
    c_longlong,
    c_ubyte,
    c_uint32,
    c_uint64,
    c_ulong,
    c_ushort,
    c_void_p,
    c_wchar_p,
)
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ------------------------------------------------------------------------- errors


class MFError(Exception):
    """Media Foundation couldn't decode the input (message is user-readable)."""

    def __init__(self, message: str, hresult: int | None = None) -> None:
        super().__init__(message)
        self.hresult = hresult


class MFUnavailableError(MFError):
    """Media Foundation itself is missing or won't start on this PC."""


class MFUnsupportedError(MFError):
    """No Media Foundation handler for this container or codec."""


class MFNoAudioError(MFError):
    """The file has no audio stream."""


class MFTooLongError(MFError):
    """Decoding stopped because the audio is longer than the caller's limit."""

    def __init__(self, seconds: float, *, at_least: bool) -> None:
        super().__init__(f"audio is longer than the limit ({seconds:.1f} s)")
        self.seconds = seconds
        self.at_least = at_least


N_EDITION_HINT = (
    "This looks like a Windows 'N' edition, which comes without Windows media features. "
    "Install the Media Feature Pack (Settings > Apps > Optional features > Add a feature > "
    "Media Feature Pack), restart the PC, and try again."
)
MISSING_MF = (
    "Windows Media Foundation isn't available on this PC, so ShoutAbout can't read audio "
    "recordings. On Windows 'N' editions, install the Media Feature Pack (Settings > Apps > "
    "Optional features > Add a feature > Media Feature Pack) and restart the PC."
)

# --------------------------------------------------------------------- constants

S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = 0x80010106
COINIT_MULTITHREADED = 0x0
MF_VERSION = 0x00020070  # MF_SDK_VERSION 0x0002 << 16 | MF_API_VERSION 0x0070 (Windows 7+)
MFSTARTUP_LITE = 0x1  # no sockets: only local byte streams are ever opened

MF_ACCESSMODE_READ = 1
MF_OPENMODE_FAIL_IF_NOT_EXIST = 0
MF_FILEFLAGS_NONE = 0
MF_RESOLUTION_MEDIASOURCE = 0x1
MF_RESOLUTION_READ = 0x10000

MF_SOURCE_READER_FIRST_AUDIO_STREAM = 0xFFFFFFFD
MF_SOURCE_READER_ALL_STREAMS = 0xFFFFFFFE
MF_SOURCE_READER_MEDIASOURCE = 0xFFFFFFFF
MF_SOURCE_READERF_ERROR = 0x1
MF_SOURCE_READERF_ENDOFSTREAM = 0x2
MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED = 0x20
VT_UI8 = 21

# HRESULTs (mferror.h / winerror.h) that get their own message.
MF_E_INVALIDMEDIATYPE = 0xC00D36B4
MF_E_INVALIDSTREAMNUMBER = 0xC00D36B3
MF_E_INVALID_FILE_FORMAT = 0xC00D36BE
MF_E_UNSUPPORTED_BYTESTREAM_TYPE = 0xC00D36C4
MF_E_INVALID_STREAM_DATA = 0xC00D36CB
MF_E_ATTRIBUTENOTFOUND = 0xC00D36E6
MF_E_TOPO_CODEC_NOT_FOUND = 0xC00D5212
MF_E_NO_MORE_TYPES = 0xC00D36B9
E_FAIL = 0x80004005
E_INVALIDARG = 0x80070057
ERROR_FILE_NOT_FOUND = 0x80070002
ERROR_PATH_NOT_FOUND = 0x80070003
ERROR_HANDLE_EOF = 0x80070026

_UNSUPPORTED = {MF_E_UNSUPPORTED_BYTESTREAM_TYPE, MF_E_INVALID_FILE_FORMAT}
_NO_CODEC = {MF_E_TOPO_CODEC_NOT_FOUND, MF_E_INVALIDMEDIATYPE, MF_E_NO_MORE_TYPES}

# vtable slots (IUnknown 0-2; IMFAttributes 3-32; see mfobjects.h / mfidl.h / mfreadwrite.h)
_RELEASE = 2
_QUERY_INTERFACE = 0
_ATTR_GET_UINT32 = 7
_ATTR_GET_GUID = 10
_ATTR_SET_GUID = 24
_BYTESTREAM_CLOSE = 17
_RESOLVER_FROM_BYTESTREAM = 4
_SOURCE_SHUTDOWN = 12
_READER_SET_STREAM_SELECTION = 4
_READER_GET_CURRENT_MEDIA_TYPE = 6
_READER_SET_CURRENT_MEDIA_TYPE = 7
_READER_READ_SAMPLE = 9
_READER_GET_PRESENTATION_ATTRIBUTE = 12
_SAMPLE_CONVERT_TO_CONTIGUOUS_BUFFER = 41
_BUFFER_LOCK = 3
_BUFFER_UNLOCK = 4


class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]

    @classmethod
    def of(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)

    def __str__(self) -> str:
        return str(uuid.UUID(bytes_le=bytes(self)))


class PROPVARIANT(Structure):
    # vt + 3 reserved words + the 8-byte value; padded to the x64 size (24 bytes).
    _fields_ = [
        ("vt", c_ushort),
        ("reserved1", c_ushort),
        ("reserved2", c_ushort),
        ("reserved3", c_ushort),
        ("value", c_uint64),
        ("padding", c_void_p),
    ]


IID_IMFMediaSource = GUID.of("279a808d-aec7-40c8-9c6b-a6b492c78a66")
MF_MT_MAJOR_TYPE = GUID.of("48eba18e-f8c9-4687-bf11-0a74c9f96a8f")
MF_MT_SUBTYPE = GUID.of("f7e34c9a-42e8-4714-b74b-cb29d72c35e5")
MF_MT_AUDIO_NUM_CHANNELS = GUID.of("37e48bf5-645e-4c5b-89de-ada9e29b696a")
MF_MT_AUDIO_SAMPLES_PER_SECOND = GUID.of("5faeeae7-0290-4c31-9e8a-c534f68d9dba")
MF_MT_AUDIO_BITS_PER_SAMPLE = GUID.of("f2deb57f-40fa-4764-aa33-ed4f2d1ff669")
MF_PD_DURATION = GUID.of("6c990d33-bb8e-477a-8598-0d5d96fcd88a")
MFMediaType_Audio = GUID.of("73647561-0000-0010-8000-00aa00389b71")
MFAudioFormat_Float = GUID.of("00000003-0000-0010-8000-00aa00389b71")
MFAudioFormat_PCM = GUID.of("00000001-0000-0010-8000-00aa00389b71")

# ------------------------------------------------------------------------ loading


def _hr(value: int) -> int:
    return value & 0xFFFFFFFF


def failed(hr: int) -> bool:
    return _hr(hr) & 0x80000000 != 0


@dataclass
class _Api:
    mfplat: ctypes.WinDLL
    mfreadwrite: ctypes.WinDLL
    ole32: ctypes.WinDLL
    shlwapi: ctypes.WinDLL


_lock = threading.Lock()
_api: _Api | None = None
_api_error: MFUnavailableError | None = None
_started = False


def _load() -> _Api:
    """The DLLs, loaded once. Raises MFUnavailableError (e.g. on N editions)."""
    global _api, _api_error
    if _api is not None:
        return _api
    if _api_error is not None:
        raise _api_error
    if sys.platform != "win32":
        _api_error = MFUnavailableError("Media Foundation is only available on Windows.")
        raise _api_error
    try:
        api = _Api(
            mfplat=ctypes.WinDLL("mfplat"),
            mfreadwrite=ctypes.WinDLL("mfreadwrite"),
            ole32=ctypes.WinDLL("ole32"),
            shlwapi=ctypes.WinDLL("shlwapi"),
        )
    except OSError as exc:
        _api_error = MFUnavailableError(_missing_message(f"({exc})"))
        raise _api_error from exc
    _declare(api)
    _api = api
    return api


def _declare(api: _Api) -> None:
    p = POINTER(c_void_p)
    signatures: list[tuple[Callable, list]] = [
        (api.mfplat.MFStartup, [c_ulong, c_ulong]),
        (api.mfplat.MFCreateFile, [c_uint32, c_uint32, c_uint32, c_wchar_p, p]),
        (api.mfplat.MFCreateMFByteStreamOnStream, [c_void_p, p]),
        (api.mfplat.MFCreateSourceResolver, [p]),
        (api.mfplat.MFCreateMediaType, [p]),
        (api.mfreadwrite.MFCreateSourceReaderFromMediaSource, [c_void_p, c_void_p, p]),
        (api.ole32.CoInitializeEx, [c_void_p, c_ulong]),
        (api.ole32.PropVariantClear, [c_void_p]),
    ]
    for fn, argtypes in signatures:
        fn.argtypes = argtypes
        fn.restype = c_long
    api.ole32.CoUninitialize.argtypes = []
    api.ole32.CoUninitialize.restype = None
    api.shlwapi.SHCreateMemStream.argtypes = [c_void_p, c_uint32]
    api.shlwapi.SHCreateMemStream.restype = c_void_p


def _missing_message(detail: str = "") -> str:
    hint = N_EDITION_HINT if is_n_edition() else ""
    parts = [MISSING_MF, detail, hint]
    return " ".join(p for p in parts if p)


def is_n_edition() -> bool:
    """True on Windows 'N' / 'KN' editions (no media features until the pack is added)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            edition = str(winreg.QueryValueEx(key, "EditionID")[0]).upper()
    except (ImportError, OSError):
        return False
    # CoreN, ProfessionalN, EducationN, EnterpriseSN, ...; not e.g. ServerFoundation.
    return edition.endswith("N") and not edition.endswith("ION")


def _startup(api: _Api) -> None:
    """MFStartup once per process (it is reference counted; we keep our one reference)."""
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        hr = api.mfplat.MFStartup(MF_VERSION, MFSTARTUP_LITE)
        if failed(hr):
            raise MFUnavailableError(_missing_message(f"(MFStartup failed: 0x{_hr(hr):08X})"), hr)
        _started = True


def available() -> tuple[bool, str]:
    """(True, "") when Media Foundation loads and starts here, else (False, why)."""
    try:
        _startup(_load())
    except MFUnavailableError as exc:
        return False, str(exc)
    return True, ""


# ---------------------------------------------------------------------- COM calls


class _Com:
    """A raw COM interface pointer with explicit vtable calls and Release()."""

    __slots__ = ("ptr",)

    def __init__(self, ptr: int | None = None) -> None:
        self.ptr = c_void_p(ptr)

    def __bool__(self) -> bool:
        return bool(self.ptr.value)

    def call(self, index: int, argtypes: tuple, *args) -> int:
        vtbl = ctypes.cast(self.ptr, POINTER(POINTER(c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])
        return fn(self.ptr, *args)

    def release(self) -> None:
        if self.ptr.value:
            self.call(_RELEASE, ())
            self.ptr = c_void_p()


def _check(hr: int, what: str) -> None:
    if failed(hr):
        raise MFError(f"{what} failed (0x{_hr(hr):08X})", _hr(hr))


def _get_uint32(attrs: _Com, key: GUID) -> int | None:
    value = c_uint32()
    hr = attrs.call(_ATTR_GET_UINT32, (POINTER(GUID), POINTER(c_uint32)), byref(key), byref(value))
    return None if failed(hr) else int(value.value)


def _get_guid(attrs: _Com, key: GUID) -> GUID | None:
    value = GUID()
    hr = attrs.call(_ATTR_GET_GUID, (POINTER(GUID), POINTER(GUID)), byref(key), byref(value))
    return None if failed(hr) else value


# ------------------------------------------------------------------------- decode


@dataclass
class Segment:
    """A run of decoded audio with one format (the format can change mid-stream, e.g. when
    an AAC decoder discovers SBR and doubles its output rate)."""

    rate: int
    channels: int
    chunks: list[np.ndarray] = field(default_factory=list)
    frames: int = 0

    def samples(self) -> np.ndarray:
        if not self.chunks:
            return np.zeros(0, np.float32)
        return np.concatenate(self.chunks) if len(self.chunks) > 1 else self.chunks[0]


@dataclass
class Decoded:
    segments: list[Segment]
    declared_seconds: float | None

    @property
    def seconds(self) -> float:
        return sum(s.frames / s.rate for s in self.segments if s.rate)


@dataclass
class _Format:
    rate: int
    channels: int
    is_float: bool
    bits: int


class _Session:
    """COM initialised on this thread for the duration of one decode (balanced)."""

    def __init__(self, api: _Api) -> None:
        self.api = api
        self.owns_com = False

    def __enter__(self) -> _Session:
        hr = _hr(self.api.ole32.CoInitializeEx(None, COINIT_MULTITHREADED))
        # S_OK / S_FALSE must be balanced; RPC_E_CHANGED_MODE means the thread already has a
        # single-threaded apartment, which Media Foundation works in as well.
        if hr in (S_OK, S_FALSE):
            self.owns_com = True
        elif hr != RPC_E_CHANGED_MODE:
            raise MFUnavailableError(f"COM didn't initialise (0x{hr:08X}).", hr)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.owns_com:
            self.api.ole32.CoUninitialize()


def _byte_stream(api: _Api, source: Path | bytes) -> _Com:
    out = c_void_p()
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        buf = ctypes.create_string_buffer(data, len(data))
        stream = _Com(api.shlwapi.SHCreateMemStream(buf, len(data)))
        if not stream:
            raise MFError("couldn't create a memory stream for the audio")
        try:
            _check(api.mfplat.MFCreateMFByteStreamOnStream(stream.ptr, byref(out)), "byte stream")
        finally:
            stream.release()  # the byte stream holds its own reference (and a copy of the data)
        return _Com(out.value)
    hr = api.mfplat.MFCreateFile(
        MF_ACCESSMODE_READ,
        MF_OPENMODE_FAIL_IF_NOT_EXIST,
        MF_FILEFLAGS_NONE,
        str(source),
        byref(out),
    )
    if _hr(hr) in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        raise FileNotFoundError(str(source))
    _check(hr, "opening the file")
    return _Com(out.value)


def _media_source(api: _Api, stream: _Com, hint_url: str) -> _Com:
    resolver = _Com()
    _check(api.mfplat.MFCreateSourceResolver(byref(resolver.ptr)), "MFCreateSourceResolver")
    try:
        obj_type = c_uint32()
        unknown = _Com()
        hr = resolver.call(
            _RESOLVER_FROM_BYTESTREAM,
            (c_void_p, c_wchar_p, c_uint32, c_void_p, POINTER(c_uint32), POINTER(c_void_p)),
            stream.ptr,
            hint_url,
            MF_RESOLUTION_MEDIASOURCE | MF_RESOLUTION_READ,
            None,
            byref(obj_type),
            byref(unknown.ptr),
        )
        if failed(hr):
            kind = hint_url.rpartition(".")[2]
            if _hr(hr) in _UNSUPPORTED:
                # The container was sniffed from the content, so "unsupported" here means
                # Windows has no handler for it, or the file is too damaged to recognise.
                raise MFUnsupportedError(
                    f"Windows couldn't open it as {kind}: it is damaged, or Windows has no "
                    f"reader for it (0x{_hr(hr):08X}).",
                    _hr(hr),
                )
            raise MFError(
                f"Windows couldn't open it as {kind}: the file looks damaged or incomplete "
                f"(0x{_hr(hr):08X}).",
                _hr(hr),
            )
        try:
            source = _Com()
            _check(
                unknown.call(
                    _QUERY_INTERFACE,
                    (POINTER(GUID), POINTER(c_void_p)),
                    byref(IID_IMFMediaSource),
                    byref(source.ptr),
                ),
                "IMFMediaSource",
            )
            return source
        finally:
            unknown.release()
    finally:
        resolver.release()


def _set_output(api: _Api, reader: _Com, subtype: GUID) -> int:
    media_type = _Com()
    _check(api.mfplat.MFCreateMediaType(byref(media_type.ptr)), "MFCreateMediaType")
    try:
        args = (POINTER(GUID), POINTER(GUID))
        _check(
            media_type.call(
                _ATTR_SET_GUID, args, byref(MF_MT_MAJOR_TYPE), byref(MFMediaType_Audio)
            ),
            "SetGUID",
        )
        _check(
            media_type.call(_ATTR_SET_GUID, args, byref(MF_MT_SUBTYPE), byref(subtype)), "SetGUID"
        )
        return _hr(
            reader.call(
                _READER_SET_CURRENT_MEDIA_TYPE,
                (c_uint32, c_void_p, c_void_p),
                MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                None,
                media_type.ptr,
            )
        )
    finally:
        media_type.release()


def _current_format(reader: _Com) -> _Format:
    media_type = _Com()
    _check(
        reader.call(
            _READER_GET_CURRENT_MEDIA_TYPE,
            (c_uint32, POINTER(c_void_p)),
            MF_SOURCE_READER_FIRST_AUDIO_STREAM,
            byref(media_type.ptr),
        ),
        "GetCurrentMediaType",
    )
    try:
        rate = _get_uint32(media_type, MF_MT_AUDIO_SAMPLES_PER_SECOND) or 0
        channels = _get_uint32(media_type, MF_MT_AUDIO_NUM_CHANNELS) or 0
        bits = _get_uint32(media_type, MF_MT_AUDIO_BITS_PER_SAMPLE) or 0
        subtype = _get_guid(media_type, MF_MT_SUBTYPE)
    finally:
        media_type.release()
    is_float = subtype is not None and bytes(subtype) == bytes(MFAudioFormat_Float)
    if rate <= 0 or channels <= 0:
        raise MFError(f"the decoder reported no sample rate/channels ({rate} Hz, {channels} ch)")
    if not ((is_float and bits == 32) or (not is_float and bits in (16, 32))):
        raise MFError(f"unexpected decoder output ({'float' if is_float else 'PCM'} {bits}-bit)")
    return _Format(rate=rate, channels=channels, is_float=is_float, bits=bits)


def _declared_seconds(api: _Api, reader: _Com) -> float | None:
    value = PROPVARIANT()
    hr = reader.call(
        _READER_GET_PRESENTATION_ATTRIBUTE,
        (c_uint32, POINTER(GUID), POINTER(PROPVARIANT)),
        MF_SOURCE_READER_MEDIASOURCE,
        byref(MF_PD_DURATION),
        byref(value),
    )
    if failed(hr):
        return None
    try:
        if value.vt != VT_UI8 or value.value == 0:
            return None
        return value.value / 10_000_000  # 100 ns units
    finally:
        api.ole32.PropVariantClear(byref(value))


def _sample_data(sample: _Com, fmt: _Format) -> np.ndarray:
    buffer = _Com()
    _check(
        sample.call(_SAMPLE_CONVERT_TO_CONTIGUOUS_BUFFER, (POINTER(c_void_p),), byref(buffer.ptr)),
        "ConvertToContiguousBuffer",
    )
    try:
        data = c_void_p()
        max_len = c_uint32()
        cur_len = c_uint32()
        _check(
            buffer.call(
                _BUFFER_LOCK,
                (POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32)),
                byref(data),
                byref(max_len),
                byref(cur_len),
            ),
            "IMFMediaBuffer::Lock",
        )
        try:
            raw = ctypes.string_at(data.value, cur_len.value) if cur_len.value else b""
        finally:
            buffer.call(_BUFFER_UNLOCK, ())
    finally:
        buffer.release()
    if fmt.is_float:
        usable = len(raw) - len(raw) % 4
        return np.frombuffer(raw[:usable], dtype="<f4")
    if fmt.bits == 16:
        usable = len(raw) - len(raw) % 2
        out = np.frombuffer(raw[:usable], dtype="<i2").astype(np.float32)
        out *= 1.0 / 32768.0
        return out
    usable = len(raw) - len(raw) % 4
    out = np.frombuffer(raw[:usable], dtype="<i4").astype(np.float32)
    out *= 1.0 / 2147483648.0
    return out


def _codec_message(hint_url: str, hr: int) -> str:
    kind = hint_url.rpartition(".")[2]
    text = (
        f"Windows has no decoder for the audio in this {kind} file (0x{hr:08X}). "
        "Send AAC (.m4a), MP3, FLAC or WAV."
    )
    if is_n_edition():
        text += " " + N_EDITION_HINT
    return text


def decode(
    source: Path | bytes,
    *,
    hint: str = ".m4a",
    max_seconds: float | None = None,
) -> Decoded:
    """Decode the first audio stream of a file path or an in-memory file.

    ``hint`` is a file extension (".m4a", ".mp3", ...) that tells Media Foundation which
    container handler to use; pass the format sniffed from the content, not a client's
    file name. ``max_seconds`` (already including any slack) stops decoding early with
    :class:`MFTooLongError`, before decoding when the container declares a longer duration.
    """
    api = _load()
    _startup(api)
    hint_url = "audio" + (hint if hint.startswith(".") else "." + hint)
    with _Session(api):
        stream = _byte_stream(api, source)
        source_obj = _Com()
        reader = _Com()
        try:
            source_obj = _media_source(api, stream, hint_url)
            _check(
                api.mfreadwrite.MFCreateSourceReaderFromMediaSource(
                    source_obj.ptr, None, byref(reader.ptr)
                ),
                "MFCreateSourceReaderFromMediaSource",
            )
            return _read_all(api, reader, hint_url, max_seconds)
        finally:
            reader.release()
            if source_obj:
                source_obj.call(_SOURCE_SHUTDOWN, ())
                source_obj.release()
            if stream:
                stream.call(_BYTESTREAM_CLOSE, ())
                stream.release()


def _read_all(api: _Api, reader: _Com, hint_url: str, max_seconds: float | None) -> Decoded:
    sel = (c_uint32, c_long)
    reader.call(_READER_SET_STREAM_SELECTION, sel, MF_SOURCE_READER_ALL_STREAMS, 0)
    hr = _hr(reader.call(_READER_SET_STREAM_SELECTION, sel, MF_SOURCE_READER_FIRST_AUDIO_STREAM, 1))
    if hr == MF_E_INVALIDSTREAMNUMBER:
        raise MFNoAudioError("the file contains no audio stream", hr)
    _check(hr, "selecting the audio stream")

    declared = _declared_seconds(api, reader)
    if max_seconds is not None and declared is not None and declared > max_seconds:
        raise MFTooLongError(declared, at_least=False)

    hr = _set_output(api, reader, MFAudioFormat_Float)
    if failed(hr):
        hr = _set_output(api, reader, MFAudioFormat_PCM)
    if failed(hr):
        if hr in _NO_CODEC:
            raise MFUnsupportedError(_codec_message(hint_url, hr), hr)
        raise MFError(f"Windows couldn't set up a decoder (0x{hr:08X}).", hr)

    fmt = _current_format(reader)
    segments = [Segment(fmt.rate, fmt.channels)]
    seconds = 0.0
    read_args = (
        c_uint32,
        c_uint32,
        POINTER(c_uint32),
        POINTER(c_uint32),
        POINTER(c_longlong),
        POINTER(c_void_p),
    )
    while True:
        actual = c_uint32()
        flags = c_uint32()
        timestamp = c_longlong()
        sample = _Com()
        hr = _hr(
            reader.call(
                _READER_READ_SAMPLE,
                read_args,
                MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                0,
                byref(actual),
                byref(flags),
                byref(timestamp),
                byref(sample.ptr),
            )
        )
        try:
            if failed(hr):
                if hr in _NO_CODEC:
                    raise MFUnsupportedError(_codec_message(hint_url, hr), hr)
                raise MFError(f"the audio data is damaged (0x{hr:08X})", hr)
            if flags.value & MF_SOURCE_READERF_ERROR:
                raise MFError("the audio data is damaged (reader error)")
            if flags.value & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED:
                fmt = _current_format(reader)
                current = segments[-1]
                if current.frames == 0:
                    current.rate, current.channels = fmt.rate, fmt.channels
                elif (current.rate, current.channels) != (fmt.rate, fmt.channels):
                    segments.append(Segment(fmt.rate, fmt.channels))
            if sample:
                data = _sample_data(sample, fmt)
                current = segments[-1]
                frames = data.size // current.channels
                if frames:
                    current.chunks.append(data[: frames * current.channels])
                    current.frames += frames
                    seconds += frames / current.rate
                    if max_seconds is not None and seconds > max_seconds:
                        raise MFTooLongError(seconds, at_least=True)
        finally:
            sample.release()
        if flags.value & MF_SOURCE_READERF_ENDOFSTREAM:
            break
    return Decoded(segments=[s for s in segments if s.frames], declared_seconds=declared)


def probe_seconds(source: Path | bytes, *, hint: str = ".m4a") -> float | None:
    """The duration the container declares, or None (unknown, unreadable, no MF)."""
    try:
        api = _load()
        _startup(api)
    except MFUnavailableError:
        return None
    hint_url = "audio" + (hint if hint.startswith(".") else "." + hint)
    with _Session(api):
        try:
            stream = _byte_stream(api, source)
        except (MFError, OSError):
            return None
        source_obj = _Com()
        reader = _Com()
        try:
            source_obj = _media_source(api, stream, hint_url)
            hr = api.mfreadwrite.MFCreateSourceReaderFromMediaSource(
                source_obj.ptr, None, byref(reader.ptr)
            )
            if failed(hr):
                return None
            return _declared_seconds(api, reader)
        except MFError:
            return None
        finally:
            reader.release()
            if source_obj:
                source_obj.call(_SOURCE_SHUTDOWN, ())
                source_obj.release()
            if stream:
                stream.call(_BYTESTREAM_CLOSE, ())
                stream.release()
