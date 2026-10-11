"""Record from the default microphone for "Talk to this PC" (16 kHz mono float32).

:class:`MicRecorder` opens the default input device only while ShoutAbout is listening,
keeps the samples in memory, reports a smoothed loudness level (0..1) for the on-screen
mouth, and stops by itself at ``max_seconds``. (Only a recording whose transcription
*failed* is written to disk, by :mod:`shoutabout.failed_audio`, so it can be tried again.)

Audio goes through ``sounddevice`` (MIT; its Windows wheel bundles PortAudio, MIT). The
default host API on Windows is MME, which converts to 16 kHz for us; if a device refuses
16 kHz anyway, the recorder opens it at its own rate and resamples.

Speed: importing sounddevice starts PortAudio, which lists every audio device (~0.1 s).
:func:`warm_up` does that at start-up on a background thread, so the first press of the
hotkey doesn't pay for it. :func:`has_input_device` asks Windows (``waveInGetNumDevs``,
nothing is opened) whether any microphone is plugged in at all; if that number changes
later (a mic plugged in), PortAudio's device list is refreshed before the next recording.

Problems come back as :class:`MicError` with a plain-words message and a two-word
version for the overlay. The common one on Windows 10/11 is the privacy switch "Let
desktop apps access your microphone": with it off, Windows hands desktop apps silence or
refuses the device, so we read that setting from the registry to say exactly what to fix.

Tests use a fake stream factory: nothing here opens a real microphone unless
:meth:`MicRecorder.start` runs with the default factory.
"""

from __future__ import annotations

import ctypes
import logging
import math
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BLOCK_SECONDS = 0.03
SILENCE_CHECK_S = 0.5  # this much exact digital silence means the mic is blocked or muted

PRIVACY_SETTINGS = "Settings > Privacy & security > Microphone"
NO_MIC_MESSAGE = (
    "No microphone found. Plug one in, or pick one in Settings > System > Sound > Input."
)
BLOCKED_MESSAGE = (
    f"Windows is blocking the microphone. Open {PRIVACY_SETTINGS} and turn on "
    '"Microphone access" and "Let desktop apps access your microphone".'
)
SILENT_MESSAGE = (
    "The microphone only sent silence. Check that it isn't muted, and that it's the one "
    "picked in Settings > System > Sound > Input."
)


class MicError(RuntimeError):
    """The microphone can't be used. ``str()`` is a full sentence; ``short`` fits the
    overlay ("Mic blocked")."""

    def __init__(self, message: str, short: str = "Mic problem") -> None:
        super().__init__(message)
        self.short = short


# ------------------------------------------------------------------------ privacy

_CONSENT = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
)

RegistryReader = Callable[[str, str], str | None]  # (hive "HKCU"/"HKLM", subkey) -> Value


def _read_consent(hive: str, subkey: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        with winreg.OpenKey(root, subkey) as key:
            value, _ = winreg.QueryValueEx(key, "Value")
            return str(value)
    except OSError:
        return None


def privacy_block(read: RegistryReader = _read_consent) -> str | None:
    """Which Windows privacy switch blocks the microphone for this app, or None.

    "device": Microphone access is off for the whole PC; "user": off for this account;
    "desktop": "Let desktop apps access your microphone" is off.
    """
    checks = (
        ("HKLM", _CONSENT, "device"),
        ("HKCU", _CONSENT, "user"),
        ("HKCU", _CONSENT + r"\NonPackaged", "desktop"),
    )
    for hive, key, which in checks:
        value = read(hive, key)
        if value is not None and value.strip().lower() == "deny":
            return which
    return None


def blocked_error(read: RegistryReader = _read_consent) -> MicError | None:
    return MicError(BLOCKED_MESSAGE, "Mic blocked") if privacy_block(read) else None


def no_mic_error() -> MicError:
    return MicError(NO_MIC_MESSAGE, "No mic")


# ---------------------------------------------------------------------- devices


def input_device_count() -> int | None:
    """How many microphones Windows has (``waveInGetNumDevs``: active capture devices;
    nothing is opened). None off Windows or when it can't be asked."""
    if sys.platform != "win32":
        return None
    try:
        return int(ctypes.windll.winmm.waveInGetNumDevs())  # type: ignore[attr-defined]
    except Exception:
        return None


def has_input_device(count: Callable[[], int | None] = input_device_count) -> bool | None:
    """True / False when Windows says whether a microphone is plugged in, else None."""
    n = count()
    return None if n is None else n > 0


_pa_lock = threading.Lock()
_pa_devices: int | None = None  # input_device_count() when PortAudio last listed devices


def portaudio(count: Callable[[], int | None] = input_device_count) -> Any:
    """The sounddevice module, with PortAudio started and its device list current.

    PortAudio lists the devices once, when it starts; a microphone plugged in later only
    shows up after a restart of PortAudio, which happens here when Windows' count changed.
    """
    global _pa_devices
    with _pa_lock:
        import sounddevice as sd

        n = count()
        if _pa_devices is None:
            _pa_devices = n
        elif n is not None and n != _pa_devices:
            log.info("audio input devices changed (%s -> %s); refreshing", _pa_devices, n)
            try:
                sd._terminate()  # noqa: SLF001  (sounddevice's documented way to rescan)
                sd._initialize()  # noqa: SLF001
            except Exception:
                log.debug("couldn't refresh the audio device list", exc_info=True)
            _pa_devices = n
        return sd


def warm_up() -> None:
    """Start PortAudio now (off the hotkey's path). Opens no device. Never raises."""
    try:
        t0 = time.perf_counter()
        portaudio()
        log.debug("audio ready in %.0f ms", (time.perf_counter() - t0) * 1000)
    except Exception:
        log.debug("couldn't start PortAudio ahead of time", exc_info=True)


def friendly_error(exc: BaseException, read: RegistryReader = _read_consent) -> MicError:
    """Turn a sounddevice / PortAudio error into something a person can act on."""
    if isinstance(exc, MicError):
        return exc
    blocked = blocked_error(read)
    if blocked is not None:
        return blocked
    text = str(exc)
    lowered = text.lower()
    if any(k in lowered for k in ("no default input", "device unavailable", "no input device")):
        return no_mic_error()
    if "-9996" in text or "invalid device" in lowered or "querying device -1" in lowered:
        return no_mic_error()
    detail = text.splitlines()[0][:120] if text else type(exc).__name__
    return MicError(f"Couldn't use the microphone ({detail}).", "Mic problem")


# -------------------------------------------------------------------------- level


class LevelMeter:
    """Loudness for the overlay: block RMS in dBFS mapped to 0..1, fast up, slow down."""

    def __init__(
        self,
        floor_db: float = -55.0,
        ceil_db: float = -14.0,
        attack: float = 0.6,
        release: float = 0.15,
    ) -> None:
        self.floor_db, self.ceil_db = floor_db, ceil_db
        self.attack, self.release = attack, release
        self.level = 0.0

    def target(self, block: np.ndarray) -> float:
        if block.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
        db = 20.0 * math.log10(rms + 1e-10)
        return min(1.0, max(0.0, (db - self.floor_db) / (self.ceil_db - self.floor_db)))

    def update(self, block: np.ndarray) -> float:
        t = self.target(block)
        k = self.attack if t > self.level else self.release
        self.level += (t - self.level) * k
        return self.level


# ----------------------------------------------------------------------- resample


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Resample mono float32 audio (windowed-sinc low-pass, then linear interpolation).
    Plenty for speech recognition; only used when a device refuses 16 kHz."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if src_rate == dst_rate or audio.size == 0:
        return audio
    if src_rate > dst_rate:
        cutoff = 0.45 * dst_rate / src_rate  # cycles per input sample, below the new Nyquist
        taps = 63
        n = np.arange(taps) - (taps - 1) / 2
        kernel = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        kernel /= kernel.sum()
        audio = np.convolve(audio, kernel, mode="same").astype(np.float32)
    duration = audio.size / src_rate
    count = max(1, int(round(duration * dst_rate)))
    positions = np.arange(count) * (src_rate / dst_rate)
    return np.interp(positions, np.arange(audio.size), audio).astype(np.float32)


# ------------------------------------------------------------------------- stream


class Stream(Protocol):
    samplerate: float

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


# (samplerate or None = the device's own, blocksize, callback(indata, frames, time, status))
StreamFactory = Callable[[int | None, int, Callable[..., None]], Stream]


def sounddevice_stream(
    samplerate: int | None, blocksize: int, callback: Callable[..., None]
) -> Stream:
    """An input stream on the default device (starts PortAudio if :func:`warm_up` didn't)."""
    sd = portaudio()
    if samplerate is None:
        info = sd.query_devices(kind="input")
        samplerate = int(info["default_samplerate"])
        blocksize = int(samplerate * BLOCK_SECONDS)
    return sd.InputStream(
        samplerate=samplerate,
        blocksize=blocksize,
        channels=1,
        dtype="float32",
        callback=callback,
    )


def _invalid_rate(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "-9997" in text or "invalid sample rate" in text


class MicRecorder:
    """One recording at a time from the default microphone. Thread-safe."""

    def __init__(
        self,
        *,
        max_seconds: float = 120.0,
        on_level: Callable[[float], None] | None = None,
        on_limit: Callable[[], None] | None = None,
        stream_factory: StreamFactory | None = None,
        privacy_reader: RegistryReader = _read_consent,
    ) -> None:
        self.max_seconds = float(max_seconds)
        self._on_level = on_level
        self._on_limit = on_limit
        self._factory: StreamFactory = stream_factory or sounddevice_stream
        self._read = privacy_reader
        self._lock = threading.Lock()
        self._stream: Stream | None = None
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._rate = SAMPLE_RATE
        self._limit_hit = False
        self._meter = LevelMeter()
        self._started_at = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def seconds(self) -> float:
        return self._frames / self._rate if self._rate else 0.0

    def start(self) -> None:
        """Open the default microphone and start recording. Raises :class:`MicError`."""
        with self._lock:
            if self._stream is not None:
                return
            self._chunks, self._frames, self._limit_hit = [], 0, False
            self._meter = LevelMeter()
            stream: Stream | None = None
            try:
                stream = self._open()
                stream.start()
            except Exception as exc:
                log.warning("couldn't open the microphone: %s", exc)
                if stream is not None:  # opened but wouldn't start: release the device
                    try:
                        stream.close()
                    except Exception:
                        log.debug("closing the microphone failed", exc_info=True)
                raise friendly_error(exc, self._read) from exc
            self._stream = stream
            self._started_at = time.monotonic()
            log.debug("microphone open at %d Hz", self._rate)

    def _open(self) -> Stream:
        blocksize = int(SAMPLE_RATE * BLOCK_SECONDS)
        try:
            stream = self._factory(SAMPLE_RATE, blocksize, self._callback)
            self._rate = SAMPLE_RATE
            return stream
        except Exception as exc:
            if not _invalid_rate(exc):
                raise
        stream = self._factory(None, blocksize, self._callback)
        self._rate = int(getattr(stream, "samplerate", SAMPLE_RATE) or SAMPLE_RATE)
        log.info("the microphone doesn't do 16 kHz; recording at %d Hz and resampling", self._rate)
        return stream

    def _callback(self, indata: Any, frames: int, _time: Any, status: Any) -> None:
        """PortAudio's thread: keep a copy of the block, update the level. Must be quick."""
        if status:
            log.debug("microphone status: %s", status)
        if self._limit_hit:
            return
        block = np.asarray(indata, dtype=np.float32)
        if block.ndim > 1:
            block = block[:, 0]
        block = block.copy()
        self._chunks.append(block)
        self._frames += block.shape[0]
        if self._on_level is not None:
            try:
                self._on_level(self._meter.update(block))
            except Exception:
                log.debug("level callback failed", exc_info=True)
        if self._frames >= self.max_seconds * self._rate:
            self._limit_hit = True
            if self._on_limit is not None:
                # Never stop the stream from inside its own callback.
                threading.Thread(target=self._on_limit, name="mic-limit", daemon=True).start()

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for step in (stream.stop, stream.close):
            try:
                step()
            except Exception:
                log.debug("closing the microphone failed", exc_info=True)

    def stop(self) -> np.ndarray:
        """Stop and return the recording as 16 kHz mono float32 (empty if not recording).

        Raises :class:`MicError` when the device only delivered exact digital silence,
        which is what Windows does when its privacy settings block the microphone.
        """
        with self._lock:
            if self._stream is None:
                return np.zeros(0, dtype=np.float32)
            self._close()
            chunks, self._chunks = self._chunks, []
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        audio = resample(audio, self._rate, SAMPLE_RATE)
        if audio.size >= SILENCE_CHECK_S * SAMPLE_RATE and not np.any(audio):
            raise blocked_error(self._read) or MicError(SILENT_MESSAGE, "Mic is silent")
        return audio

    def cancel(self) -> None:
        """Stop and throw the recording away."""
        with self._lock:
            self._close()
            self._chunks = []
