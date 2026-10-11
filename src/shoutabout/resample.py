"""Downmix to mono and resample to 16 kHz in numpy (no FFmpeg, no scipy).

Resampling is rational (``up / down`` after dividing both rates by their gcd, e.g.
44.1 kHz -> 16 kHz is 160/441) with a polyphase FIR filter: a Kaiser-windowed sinc
low-pass that removes everything above the new Nyquist frequency before decimating, so
high frequencies don't fold back into the speech band (which a plain linear interpolation
would do). Each of the ``up`` output phases is one small matrix-vector product over a
strided window view of the input, so the work is ``len(output) * taps_per_phase``
multiply-adds and runs in BLAS.

Filter choice: 16 zero crossings per side of the sinc and Kaiser beta 8.6 (about -86 dB
stop band), cut off at 95 % of the lower Nyquist frequency. That is in the same class as
FFmpeg's default swresample filter and transparent for speech recognition (Whisper's
features stop at 8 kHz).
"""

from __future__ import annotations

from functools import lru_cache
from math import gcd

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

ZERO_CROSSINGS = 16
KAISER_BETA = 8.6
CUTOFF = 0.95
# Rates are reduced by their gcd; a pathological ratio (e.g. 44 101 Hz) would need a huge
# filter bank. Above this many phases the input is first snapped to a nearby simple rate.
MAX_PHASES = 4096


def to_mono(interleaved: np.ndarray, channels: int) -> np.ndarray:
    """Average interleaved channels into one float32 channel (drops a partial last frame)."""
    data = np.asarray(interleaved, dtype=np.float32).reshape(-1)
    if channels <= 1:
        return data
    frames = data.size // channels
    return data[: frames * channels].reshape(frames, channels).mean(axis=1, dtype=np.float32)


@lru_cache(maxsize=16)
def _polyphase_bank(up: int, down: int) -> tuple[np.ndarray, int]:
    """(bank, centre): bank is an (up, taps) float32 matrix whose row p holds the filter
    taps for output phase p, reversed so a forward window of the input can be multiplied
    with it directly; centre is the filter's middle tap (its delay, in upsampled samples)."""
    ratio = max(up, down)
    half = ZERO_CROSSINGS * ratio
    n = np.arange(-half, half + 1, dtype=np.float64)
    cutoff = CUTOFF / ratio  # in cycles per (upsampled) sample, times 2: firwin convention
    h = cutoff * np.sinc(cutoff * n) * np.kaiser(n.size, KAISER_BETA)
    h *= up / h.sum()  # unity DC gain after zero-stuffing by `up`
    taps = -(-h.size // up)  # ceil
    padded = np.zeros(taps * up, dtype=np.float64)
    padded[: h.size] = h
    # bank[p, j] = h[p + j*up]; reversed along j for the forward windows below.
    bank = padded.reshape(taps, up).T[:, ::-1]
    return np.ascontiguousarray(bank, dtype=np.float32), half


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample mono float32 ``audio`` from ``src_rate`` to ``dst_rate`` Hz.

    The output has ``ceil(len * dst / src)`` samples and no delay: sample ``m`` of the
    output is centred on input time ``m / dst_rate``.
    """
    x = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
    src_rate, dst_rate = int(src_rate), int(dst_rate)
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"bad sample rates {src_rate} -> {dst_rate}")
    if src_rate == dst_rate or x.size == 0:
        return x.copy() if x.size else np.zeros(0, np.float32)
    g = gcd(src_rate, dst_rate)
    up, down = dst_rate // g, src_rate // g
    if up > MAX_PHASES:
        # e.g. 44 101 Hz: resample as if it were the nearest rate with a small ratio. The
        # pitch error is under 0.03 %, far below anything audible or relevant to STT.
        snapped = round(src_rate / 100) * 100
        return resample(x, snapped, dst_rate)

    bank, delay = _polyphase_bank(up, down)
    taps = bank.shape[1]
    n_out = -(-x.size * up // down)  # ceil
    # Output m sits at upsampled time t = m*down + delay; its newest input sample is
    # k0 = t // up and its phase p = t % up. With m = q*up + r both depend only on r.
    lead = taps - 1  # zeros before x so that k0 - (taps-1) >= 0
    last_k0 = ((n_out - 1) * down + delay) // up
    tail = max(0, last_k0 + 1 - x.size)
    xp = np.concatenate([np.zeros(lead, np.float32), x, np.zeros(tail, np.float32)])
    windows = sliding_window_view(xp, taps)  # windows[s] = xp[s : s + taps]
    out = np.empty(n_out, dtype=np.float32)
    for r in range(min(up, n_out)):
        t0 = r * down + delay
        k0, p = divmod(t0, up)
        count = -(-(n_out - r) // up)  # outputs r, r+up, r+2up, ... below n_out
        # window start for output q: (k0 + q*down) - (taps-1) + lead = k0 + q*down
        rows = windows[k0 : k0 + (count - 1) * down + 1 : down]
        out[r::up] = rows @ bank[p]
    return out


def to_16k_mono(interleaved: np.ndarray, rate: int, channels: int, dst_rate: int) -> np.ndarray:
    """Interleaved float32 at any rate/channel count -> mono float32 at ``dst_rate``."""
    return resample(to_mono(interleaved, channels), rate, dst_rate)
