"""shoutabout.resample: mono downmix and the polyphase anti-aliasing resampler."""

from __future__ import annotations

import numpy as np
import pytest

from shoutabout.resample import _polyphase_bank, resample, to_16k_mono, to_mono

RATES = (8_000, 11_025, 22_050, 24_000, 32_000, 44_100, 48_000, 96_000)


def sine(freq: float, rate: int, seconds: float = 1.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12))


def test_to_mono_averages_channels_and_drops_a_partial_frame():
    interleaved = np.array([1.0, 0.0, 0.5, 0.5, -1.0, 1.0, 0.25], np.float32)
    assert to_mono(interleaved, 2).tolist() == [0.5, 0.5, 0.0]
    assert to_mono(interleaved, 1) is not None and len(to_mono(interleaved, 1)) == 7
    assert to_mono(np.zeros(9, np.float32), 3).shape == (3,)


def test_same_rate_and_empty():
    x = sine(440, 16_000, 0.1)
    out = resample(x, 16_000, 16_000)
    assert np.array_equal(out, x) and out is not x
    assert resample(np.zeros(0, np.float32), 44_100, 16_000).size == 0


@pytest.mark.parametrize("rate", RATES)
def test_tone_survives_with_no_delay(rate):
    x = sine(1000, rate, 1.0)
    y = resample(x, rate, 16_000)
    assert y.dtype == np.float32
    assert len(y) == -(-len(x) * 16_000 // rate)
    ref = sine(1000, 16_000, len(y) / 16_000)[: len(y)]
    mid = slice(800, len(y) - 800)  # away from the edges
    assert np.max(np.abs(y[mid] - ref[mid])) < 1e-3  # same amplitude AND phase: no delay


@pytest.mark.parametrize("rate", (22_050, 44_100, 48_000))
def test_passband_is_flat(rate):
    for freq in (100, 1000, 3000, 6000):
        y = resample(sine(freq, rate), rate, 16_000)[2000:-2000]
        assert abs(rms_db(y) - rms_db(sine(freq, 16_000)[2000:-2000])) < 0.1


@pytest.mark.parametrize("rate", (22_050, 44_100, 48_000))
def test_no_aliasing_from_above_the_new_nyquist(rate):
    # A plain linear interpolation (np.interp) folds these back into the speech band.
    for freq in (9_000, 10_000, 11_000):
        if freq >= rate / 2:
            continue
        y = resample(sine(freq, rate), rate, 16_000)[2000:-2000]
        assert rms_db(y) < -70, f"{freq} Hz at {rate} Hz leaks {rms_db(y):.0f} dB"


def test_upsampling_8k():
    y = resample(sine(440, 8000, 1.0), 8000, 16_000)
    assert len(y) == 16_000
    spectrum = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    assert abs(np.fft.rfftfreq(len(y), 1 / 16_000)[np.argmax(spectrum)] - 440) < 2
    # 3 kHz keeps its level, and its mirror image at 8 - 3 = 5 kHz is filtered out
    y = resample(sine(3000, 8000), 8000, 16_000)[2000:-2000]
    assert abs(rms_db(y) - rms_db(sine(3000, 16_000)[2000:-2000])) < 0.1
    spectrum = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), 1 / 16_000)
    image = spectrum[np.abs(freqs - 5000) < 50].max()
    assert 20 * np.log10(image / spectrum.max()) < -70


def test_impulse_stays_in_place():
    x = np.zeros(48_000, np.float32)
    x[24_000] = 1.0  # t = 0.5 s
    y = resample(x, 48_000, 16_000)
    assert int(np.argmax(np.abs(y))) == 8_000


def test_odd_rate_is_snapped_not_exploded():
    y = resample(sine(440, 44_101, 0.5), 44_101, 16_000)
    assert abs(len(y) - 8000) <= 2


def test_dc_gain_is_one_for_every_phase():
    bank, _centre = _polyphase_bank(160, 441)
    assert bank.shape[0] == 160
    np.testing.assert_allclose(bank.sum(axis=1), 1.0, atol=1e-3)


def test_bad_rates():
    with pytest.raises(ValueError):
        resample(np.zeros(10, np.float32), 0, 16_000)


def test_to_16k_mono():
    stereo = np.stack([sine(440, 44_100), sine(440, 44_100) * 0], axis=1).reshape(-1)
    y = to_16k_mono(stereo, 44_100, 2, 16_000)
    assert len(y) == 16_000
    assert np.max(np.abs(y[1000:-1000])) == pytest.approx(0.25, abs=0.01)


def test_long_input_is_fast_enough():
    import time

    x = np.random.default_rng(0).standard_normal(44_100 * 60).astype(np.float32) * 0.1
    start = time.perf_counter()
    resample(x, 44_100, 16_000)
    assert time.perf_counter() - start < 2.0  # ~15 ms on a desktop; generous for CI
