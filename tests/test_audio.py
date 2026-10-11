"""shoutabout.audio: the decoder chain (WAV fast path -> Media Foundation -> PyAV), sniffing,
limits and errors. Runs everywhere: on Windows the compressed files go through Media
Foundation, elsewhere through PyAV. Fake decoders cover the fallback logic on any OS."""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

import av
import numpy as np
import pytest

from shoutabout import audio as audio_mod
from shoutabout import mfdecode, pyavdecode
from shoutabout.audio import (
    MEDIA_FOUNDATION,
    PYAV,
    SAMPLE_RATE,
    WAV,
    AudioError,
    AudioTooLongError,
    DecoderUnavailableError,
    decode_audio,
    decode_audio_info,
    encode_m4a,
    probe_duration,
    sniff,
)

COMPRESSED_BACKEND = MEDIA_FOUNDATION if sys.platform == "win32" else PYAV


def tone(seconds: float, rate: int, freq: float = 440.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray, rate: int) -> Path:
    """samples: (n,) mono or (n, channels) float in [-1, 1]."""
    data = np.asarray(samples, dtype=np.float32)
    channels = 1 if data.ndim == 1 else data.shape[1]
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


def dominant_freq(x: np.ndarray, rate: int = SAMPLE_RATE) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[int(np.argmax(spectrum))])


def make_mp4(path: Path, *, with_audio: bool) -> Path:
    """A tiny mp4 with a video stream and (optionally) an AAC audio stream."""
    with av.open(str(path), "w", format="mp4") as c:
        v = c.add_stream("mpeg4", rate=10)
        v.width, v.height, v.pix_fmt = 32, 32, "yuv420p"
        a = c.add_stream("aac", rate=SAMPLE_RATE, layout="mono") if with_audio else None
        for _ in range(10):
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), np.uint8), format="rgb24")
            for p in v.encode(frame):
                c.mux(p)
        for p in v.encode(None):
            c.mux(p)
        if a is not None:
            af = av.AudioFrame.from_ndarray(
                tone(1.0, SAMPLE_RATE).reshape(1, -1), format="flt", layout="mono"
            )
            af.sample_rate = SAMPLE_RATE
            for p in a.encode(af):
                c.mux(p)
            for p in a.encode(None):
                c.mux(p)
    return path


def make_encoded(path: Path, fmt: str, codec: str, rate: int = 44_100, seconds: float = 2.0):
    """Encode a 440 Hz tone with PyAV (test fixture only; the app never encodes)."""
    with av.open(str(path), "w", format=fmt) as c:
        s = c.add_stream(codec, rate=rate, layout="mono")
        fifo = av.AudioFifo()
        frame = av.AudioFrame.from_ndarray(
            tone(seconds, rate).reshape(1, -1), format="flt", layout="mono"
        )
        frame.sample_rate = rate
        resampler = av.AudioResampler(format=s.format.name, layout="mono", rate=rate)
        for f in resampler.resample(frame) + resampler.resample(None):
            f.pts = None
            fifo.write(f)
        size = s.codec_context.frame_size or 1024
        while fifo.samples:
            for p in s.encode(fifo.read(min(size, fifo.samples))):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)
    return path


# ------------------------------------------------------------------------- decode


def test_decode_wav_mono_16k_is_exact(tmp_path):
    src = tone(1.5, SAMPLE_RATE)
    path = write_wav(tmp_path / "a.wav", src, SAMPLE_RATE)
    info = decode_audio_info(path)
    out = info.samples
    assert info.backend == WAV and (info.source_rate, info.channels) == (SAMPLE_RATE, 1)
    assert out.dtype == np.float32 and out.ndim == 1
    assert len(out) == len(src)
    assert np.max(np.abs(out - src)) < 1e-3
    assert np.max(np.abs(out)) <= 1.0


def test_decode_stereo_48k_to_mono_16k(tmp_path):
    left = tone(2.0, 48_000, 440)
    stereo = np.stack([left, left * 0.5], axis=1)
    path = write_wav(tmp_path / "st.wav", stereo, 48_000)
    out = decode_audio(path)
    assert out.dtype == np.float32 and out.ndim == 1
    assert abs(len(out) - 2 * SAMPLE_RATE) <= 64
    assert abs(dominant_freq(out) - 440) < 5
    assert 0.1 < np.max(np.abs(out)) <= 1.0
    # mono = mean of the channels: 0.75 * 0.3 peak
    assert np.max(np.abs(out[1000:-1000])) == pytest.approx(0.225, abs=0.005)


def test_encode_m4a_round_trip(tmp_path):
    wav = write_wav(tmp_path / "in.wav", tone(3.0, 22_050, 660), 22_050)
    m4a = tmp_path / "out.m4a"
    encode_m4a(wav, m4a)
    with av.open(str(m4a)) as c:
        stream = c.streams.audio[0]
        assert stream.codec_context.name == "aac"
        assert stream.rate in (44_100, 48_000)
        assert stream.channels == 1
    info = decode_audio_info(m4a)
    assert info.backend == COMPRESSED_BACKEND
    out = info.samples
    # AAC adds encoder priming/padding: allow ~0.1 s.
    assert abs(len(out) / SAMPLE_RATE - 3.0) < 0.1
    assert abs(dominant_freq(out) - 660) < 5
    assert abs(probe_duration(m4a) - 3.0) < 0.1


@pytest.mark.parametrize(
    ("name", "fmt", "codec"),
    [
        ("a.mp3", "mp3", "libmp3lame"),
        ("a.flac", "flac", "flac"),
        ("a.aac", "adts", "aac"),
        ("a.m4a", "ipod", "alac"),
    ],
)
def test_common_formats_decode(tmp_path, name, fmt, codec):
    path = make_encoded(tmp_path / name, fmt, codec)
    info = decode_audio_info(path)
    assert info.backend == COMPRESSED_BACKEND
    assert abs(len(info.samples) / SAMPLE_RATE - 2.0) < 0.1
    assert abs(dominant_freq(info.samples) - 440) < 5


def test_compressed_wav_falls_through_to_a_real_decoder(tmp_path):
    # mu-law WAV: not for the numpy fast path, fine for Media Foundation / PyAV.
    path = make_encoded(tmp_path / "mulaw.wav", "wav", "pcm_mulaw", rate=8000)
    info = decode_audio_info(path)
    assert info.backend == COMPRESSED_BACKEND
    assert abs(dominant_freq(info.samples) - 440) < 5


def test_probe_duration(tmp_path):
    path = write_wav(tmp_path / "a.wav", tone(2.5, SAMPLE_RATE), SAMPLE_RATE)
    assert probe_duration(path) == pytest.approx(2.5, abs=0.01)
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00\x01garbage" * 50)
    assert probe_duration(junk) is None
    assert probe_duration(tmp_path / "missing.wav") is None


def test_multi_stream_container_decodes_the_audio_stream(tmp_path):
    out = decode_audio(make_mp4(tmp_path / "va.mp4", with_audio=True))
    assert abs(len(out) / SAMPLE_RATE - 1.0) < 0.15
    assert abs(dominant_freq(out) - 440) < 5


def test_misnamed_file_is_sniffed_by_content(tmp_path):
    m4a = tmp_path / "x.m4a"
    encode_m4a(write_wav(tmp_path / "in.wav", tone(1.0, 16_000), 16_000), m4a)
    misnamed = tmp_path / "upload.wav"
    misnamed.write_bytes(m4a.read_bytes())
    info = decode_audio_info(misnamed)
    assert info.backend == COMPRESSED_BACKEND
    assert abs(dominant_freq(info.samples) - 440) < 5


def test_pyav_backend_still_works(tmp_path):
    m4a = tmp_path / "x.m4a"
    encode_m4a(write_wav(tmp_path / "in.wav", tone(1.0, 16_000), 16_000), m4a)
    info = decode_audio_info(m4a, backends=(PYAV,))
    assert info.backend == PYAV
    assert abs(dominant_freq(info.samples) - 440) < 5


def test_unknown_backend_name(tmp_path):
    path = write_wav(tmp_path / "a.wav", tone(0.1, SAMPLE_RATE), SAMPLE_RATE)
    with pytest.raises(ValueError, match="unknown audio backend"):
        decode_audio_info(path, backends=("ffmpeg",))


def test_default_backends():
    if sys.platform == "win32":
        assert audio_mod.default_backends() == (WAV, MEDIA_FOUNDATION, PYAV)
    else:
        assert audio_mod.default_backends() == (WAV, PYAV)


# ------------------------------------------------------------------------- limits


def test_too_long_by_declared_duration(tmp_path):
    path = write_wav(tmp_path / "long.wav", tone(3.0, SAMPLE_RATE), SAMPLE_RATE)
    with pytest.raises(AudioTooLongError) as info:
        decode_audio(path, max_seconds=1.0)
    assert isinstance(info.value, AudioError)
    assert info.value.max_seconds == 1.0
    assert info.value.seconds == pytest.approx(3.0, abs=0.01)
    assert "1 s limit" in str(info.value)


def test_too_long_compressed_by_declared_duration(tmp_path):
    m4a = tmp_path / "long.m4a"
    encode_m4a(write_wav(tmp_path / "in.wav", tone(3.0, 16_000), 16_000), m4a)
    with pytest.raises(AudioTooLongError) as info:
        decode_audio(m4a, max_seconds=1.0)
    assert info.value.seconds == pytest.approx(3.0, abs=0.1)


def test_too_long_by_decoded_length_when_duration_unknown_pyav(tmp_path, monkeypatch):
    m4a = tmp_path / "long.m4a"
    encode_m4a(write_wav(tmp_path / "in.wav", tone(3.0, 16_000), 16_000), m4a)
    monkeypatch.setattr(pyavdecode, "_declared_duration", lambda container, stream: None)
    with pytest.raises(AudioTooLongError) as info:
        decode_audio_info(m4a, max_seconds=1.0, backends=(PYAV,))
    assert "more than" in str(info.value)


def test_limit_allows_audio_at_the_limit(tmp_path):
    path = write_wav(tmp_path / "ok.wav", tone(1.0, SAMPLE_RATE), SAMPLE_RATE)
    assert len(decode_audio(path, max_seconds=1.0)) == SAMPLE_RATE
    m4a = tmp_path / "ok.m4a"
    encode_m4a(path, m4a)  # AAC priming makes it a hair longer than 1 s
    assert len(decode_audio(m4a, max_seconds=1.0)) > 0


# ------------------------------------------------------------------------- errors


def test_garbage_bytes(tmp_path):
    path = tmp_path / "upload.m4a"
    path.write_bytes(b"this is definitely not audio " * 100)
    with pytest.raises(AudioError) as info:
        decode_audio(path)
    assert not isinstance(info.value, DecoderUnavailableError)


def test_empty_file(tmp_path):
    path = tmp_path / "empty.m4a"
    path.write_bytes(b"")
    with pytest.raises(AudioError, match="empty"):
        decode_audio(path)


def test_wav_with_zero_samples(tmp_path):
    path = write_wav(tmp_path / "zero.wav", np.zeros(0, np.float32), SAMPLE_RATE)
    with pytest.raises(AudioError, match="no audio"):
        decode_audio(path)


def test_missing_file(tmp_path):
    with pytest.raises(AudioError, match="not found"):
        decode_audio(tmp_path / "nope.wav")


def test_no_audio_stream(tmp_path):
    with pytest.raises(AudioError, match="no audio stream"):
        decode_audio(make_mp4(tmp_path / "video.mp4", with_audio=False))


def test_truncated_m4a(tmp_path):
    m4a = tmp_path / "x.m4a"
    encode_m4a(write_wav(tmp_path / "in.wav", tone(2.0, 16_000), 16_000), m4a)
    cut = tmp_path / "cut.m4a"
    cut.write_bytes(m4a.read_bytes()[:200])
    with pytest.raises(AudioError):
        decode_audio(cut)


def test_playlist_formats_are_refused(tmp_path):
    # FFmpeg's HLS/concat demuxers can open other files/URLs; they're not whitelisted, and
    # Media Foundation only ever gets sniffed, allow-listed containers.
    path = tmp_path / "x.m3u8"
    path.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/a.ts\n")
    with pytest.raises(AudioError):
        decode_audio(path)
    concat = tmp_path / "list.txt"
    concat.write_text("ffconcat version 1.0\nfile 'a.wav'\n")
    with pytest.raises(AudioError):
        decode_audio(concat)


def test_encode_m4a_rejects_garbage_and_leaves_no_output(tmp_path):
    src = tmp_path / "junk.wav"
    src.write_bytes(b"junk" * 100)
    dst = tmp_path / "out.m4a"
    with pytest.raises(AudioError):
        encode_m4a(src, dst)
    assert not dst.exists()


def test_encode_m4a_without_pyav(tmp_path, monkeypatch):
    monkeypatch.setattr(pyavdecode, "available", lambda: False)
    with pytest.raises(AudioError, match="needs PyAV"):
        encode_m4a(tmp_path / "a.wav", tmp_path / "b.m4a")


# ------------------------------------------------------------------------- sniffing


ASF = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
        (b"RF64\xff\xff\xff\xffWAVEds64", "wav"),
        (b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00", "mp4"),
        (b"\x00\x00\x00\x08free\x00\x00", "mp4"),
        (b"fLaC\x00\x00\x00\x22", "flac"),
        (b"OggS\x00\x02", "ogg"),
        (b"caff\x00\x01\x00\x00", "caf"),
        (b"FORM\x00\x00\x10\x00AIFFCOMM", "aiff"),
        (b"#!AMR\n<", "amr"),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81", "matroska"),
        (ASF + b"\x00" * 8, "asf"),
        (b"\xff\xf1\x50\x80\x02\x1f\xfc", "aac"),  # ADTS
        (b"\xff\xfb\x90\x64\x00", "mp3"),  # MPEG-1 layer III, 128 kbps, 44.1 kHz
        (b"\xff\xfb\xf0\x64\x00", None),  # bad bitrate index
        (b"\xff\xfb\x9c\x64\x00", None),  # reserved sample rate
        (b"<html><body>", None),
        (b"#EXTM3U\n", None),
        (b"", None),
    ],
)
def test_sniff(head, kind):
    assert sniff(head) == kind


def test_sniff_id3_then_frame():
    tagged = b"ID3\x04\x00\x00\x00\x00\x00\x05"
    assert sniff(tagged, b"\xff\xfb\x90\x64") == "mp3"
    assert sniff(tagged, b"\xff\xf1\x50\x80") == "aac"
    assert sniff(tagged) == "mp3"


def test_sniff_file_reads_past_an_id3_tag(tmp_path):
    path = tmp_path / "tagged.mp3"
    tag_body = b"\x00" * 300
    size = len(tag_body)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    path.write_bytes(b"ID3\x04\x00\x00" + syncsafe + tag_body + b"\xff\xf1\x50\x80\x02\x1f")
    assert audio_mod.sniff_file(path) == ("aac", path.stat().st_size)


# --------------------------------------------------------------- fallback (fakes)


def _fake_decoded(seconds: float = 1.0, rate: int = 44_100, channels: int = 2):
    data = np.repeat(tone(seconds, rate), channels)
    seg = mfdecode.Segment(rate, channels, [data], len(data) // channels)
    return mfdecode.Decoded(segments=[seg], declared_seconds=seconds)


@pytest.fixture
def m4a_bytes(tmp_path):
    path = tmp_path / "fake.m4a"
    path.write_bytes(b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 100)  # sniffs as mp4
    return path


@pytest.fixture
def chain(monkeypatch):
    """Windows' decoder order on any OS, with fake Media Foundation / PyAV."""
    monkeypatch.setattr(audio_mod, "default_backends", lambda: (WAV, MEDIA_FOUNDATION, PYAV))
    calls: list[str] = []

    def use(mf=None, pyav=None, pyav_available=True):
        def fake_mf(source, *, hint, max_seconds):
            calls.append(f"mf{hint}")
            if isinstance(mf, BaseException):
                raise mf
            return mf

        def fake_pyav(path, max_seconds=None):
            calls.append("pyav")
            if isinstance(pyav, BaseException):
                raise pyav
            return pyav

        monkeypatch.setattr(mfdecode, "decode", fake_mf)
        monkeypatch.setattr(pyavdecode, "decode_audio", fake_pyav)
        monkeypatch.setattr(pyavdecode, "available", lambda: pyav_available)
        return calls

    return use


def test_media_foundation_result_is_downmixed_and_resampled(chain, m4a_bytes):
    calls = chain(mf=_fake_decoded(1.0, 44_100, 2))
    info = decode_audio_info(m4a_bytes)
    assert calls == ["mf.m4a"]
    assert (info.backend, info.source_rate, info.channels) == (MEDIA_FOUNDATION, 44_100, 2)
    assert len(info.samples) == SAMPLE_RATE
    assert abs(dominant_freq(info.samples) - 440) < 5


def test_media_foundation_rate_change_mid_stream(chain, m4a_bytes):
    first = mfdecode.Segment(22_050, 1, [tone(0.5, 22_050)], 11_025)
    second = mfdecode.Segment(44_100, 1, [tone(0.5, 44_100)], 22_050)
    chain(mf=mfdecode.Decoded([first, second], None))
    out = decode_audio(m4a_bytes)
    assert abs(len(out) - SAMPLE_RATE) <= 2
    assert abs(dominant_freq(out) - 440) < 5


def test_falls_back_to_pyav_when_media_foundation_fails(chain, m4a_bytes):
    calls = chain(
        mf=mfdecode.MFUnsupportedError("no reader"),
        pyav=np.zeros(SAMPLE_RATE, np.float32) + 0.1,
    )
    info = decode_audio_info(m4a_bytes)
    assert calls == ["mf.m4a", "pyav"] and info.backend == PYAV


def test_first_decoder_error_wins_when_all_fail(chain, m4a_bytes):
    chain(mf=mfdecode.MFError("damaged (0xC00D36E5)"), pyav=AudioError("pyav says no"))
    with pytest.raises(AudioError, match="damaged") as info:
        decode_audio(m4a_bytes)
    assert not isinstance(info.value, DecoderUnavailableError)


def test_too_long_is_final(chain, m4a_bytes):
    calls = chain(mf=mfdecode.MFTooLongError(700.0, at_least=False), pyav=np.zeros(10))
    with pytest.raises(AudioTooLongError) as info:
        decode_audio(m4a_bytes, max_seconds=600)
    assert calls == ["mf.m4a"]  # no second opinion on the length
    assert info.value.max_seconds == 600 and info.value.seconds == 700.0


def test_too_long_after_decoding(chain, m4a_bytes):
    chain(mf=_fake_decoded(3.0, 16_000, 1))
    with pytest.raises(AudioTooLongError):
        decode_audio(m4a_bytes, max_seconds=1.0)


def test_media_foundation_missing_is_a_decoder_problem(chain, m4a_bytes):
    # Windows "N" edition without the Media Feature Pack, and no PyAV (the installed app).
    chain(mf=mfdecode.MFUnavailableError("Install the Media Feature Pack"), pyav_available=False)
    with pytest.raises(DecoderUnavailableError, match="Media Feature Pack"):
        decode_audio(m4a_bytes)


def test_media_foundation_missing_with_pyav(chain, m4a_bytes):
    chain(mf=mfdecode.MFUnavailableError("missing"), pyav=np.full(1600, 0.1, np.float32))
    assert decode_audio_info(m4a_bytes).backend == PYAV


def test_unknown_format_without_pyav_is_unsupported_not_unavailable(chain, tmp_path):
    calls = chain(mf=mfdecode.MFUnavailableError("missing"), pyav_available=False)
    path = tmp_path / "a.ogg"
    path.write_bytes(b"OggS" + b"\x00" * 100)
    with pytest.raises(AudioError, match="OGG audio, which this PC can't decode") as info:
        decode_audio(path)
    assert not isinstance(info.value, DecoderUnavailableError)
    assert calls == []  # Ogg is never handed to Media Foundation


def test_no_audio_stream_from_media_foundation(chain, m4a_bytes):
    chain(mf=mfdecode.MFNoAudioError("none"), pyav_available=False)
    with pytest.raises(AudioError, match="no audio stream"):
        decode_audio(m4a_bytes)


def test_silence_only_result(chain, m4a_bytes):
    chain(mf=mfdecode.Decoded([], None), pyav_available=False)
    with pytest.raises(AudioError, match="no audio samples"):
        decode_audio(m4a_bytes)


def test_output_is_clipped(chain, m4a_bytes):
    loud = mfdecode.Segment(16_000, 1, [np.full(1600, 1.7, np.float32)], 1600)
    chain(mf=mfdecode.Decoded([loud], None))
    out = decode_audio(m4a_bytes)
    assert out.max() <= 1.0 and out.min() >= -1.0


# ------------------------------------------------------------------- WAV variants


def _riff(chunks: list[tuple[bytes, bytes]], kind: bytes = b"RIFF", size: int | None = None):
    body = b"WAVE" + b"".join(
        cid + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")
        for cid, data in chunks
    )
    return kind + struct.pack("<I", len(body) if size is None else size) + body


def _fmt(tag=1, channels=1, rate=16_000, bits=16, extensible_tag=None) -> bytes:
    align = channels * bits // 8
    base = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    if extensible_tag is None:
        return base
    guid = struct.pack("<H", extensible_tag) + bytes.fromhex("000000001000800000aa00389b71")
    return base + struct.pack("<HHI", 22, bits, 0) + guid


@pytest.mark.parametrize(
    ("bits", "tag", "dtype", "scale"),
    [
        (16, 1, "<i2", 32767),
        (32, 1, "<i4", 2**31 - 1),
        (32, 3, "<f4", 1.0),
        (64, 3, "<f8", 1.0),
    ],
)
def test_wav_sample_formats(tmp_path, bits, tag, dtype, scale):
    src = tone(0.5, 16_000)
    data = (src * scale).astype(dtype).tobytes()
    path = tmp_path / "x.wav"
    path.write_bytes(_riff([(b"fmt ", _fmt(tag, bits=bits)), (b"data", data)]))
    info = decode_audio_info(path)
    assert info.backend == WAV
    assert np.max(np.abs(info.samples - src)) < 1e-3


def test_wav_24_bit_and_8_bit(tmp_path):
    src = tone(0.5, 16_000)
    ints = np.round(src * (2**23 - 1)).astype("<i4")
    packed = np.frombuffer(ints.tobytes(), np.uint8).reshape(-1, 4)[:, :3].tobytes()
    p24 = tmp_path / "24.wav"
    p24.write_bytes(_riff([(b"fmt ", _fmt(bits=24)), (b"data", packed)]))
    assert np.max(np.abs(decode_audio(p24) - src)) < 1e-4
    u8 = (np.round(src * 127) + 128).astype(np.uint8).tobytes()
    p8 = tmp_path / "8.wav"
    p8.write_bytes(_riff([(b"fmt ", _fmt(bits=8)), (b"data", u8)]))
    assert np.max(np.abs(decode_audio(p8) - src)) < 1e-2


def test_wav_extensible_float_with_extra_chunks(tmp_path):
    src = tone(0.5, 16_000)
    path = tmp_path / "x.wav"
    path.write_bytes(
        _riff(
            [
                (b"LIST", b"INFOISFT\x05\x00\x00\x00test\x00"),  # odd size: padded
                (b"fmt ", _fmt(0xFFFE, bits=32, extensible_tag=3)),
                (b"fact", struct.pack("<I", len(src))),
                (b"data", src.astype("<f4").tobytes()),
            ]
        )
    )
    info = decode_audio_info(path)
    assert info.backend == WAV
    assert np.max(np.abs(info.samples - src)) < 1e-6


def test_streaming_wav_without_sizes(tmp_path):
    src = tone(0.5, 16_000)
    data = (src * 32767).astype("<i2").tobytes()
    raw = bytearray(_riff([(b"fmt ", _fmt()), (b"data", data)], size=0xFFFFFFFF))
    data_size_at = raw.index(b"data") + 4
    raw[data_size_at : data_size_at + 4] = b"\xff\xff\xff\xff"
    path = tmp_path / "stream.wav"
    path.write_bytes(bytes(raw))
    assert len(decode_audio(path)) == len(src)


def test_rf64_wav(tmp_path):
    src = tone(0.25, 16_000)
    data = (src * 32767).astype("<i2").tobytes()
    ds64 = struct.pack("<QQQI", 0, len(data), len(src), 0)
    raw = bytearray(
        _riff([(b"ds64", ds64), (b"fmt ", _fmt()), (b"data", data)], kind=b"RF64", size=0xFFFFFFFF)
    )
    at = raw.index(b"data") + 4
    raw[at : at + 4] = b"\xff\xff\xff\xff"
    path = tmp_path / "rf64.wav"
    path.write_bytes(bytes(raw))
    assert len(decode_audio(path)) == len(src)


def test_truncated_wav_data_uses_what_is_there(tmp_path):
    src = tone(1.0, 16_000)
    full = _riff([(b"fmt ", _fmt()), (b"data", (src * 32767).astype("<i2").tobytes())])
    path = tmp_path / "cut.wav"
    path.write_bytes(full[: len(full) // 2])
    out = decode_audio(path)
    assert 0.4 < len(out) / SAMPLE_RATE < 0.6


@pytest.mark.parametrize(
    "chunks",
    [
        [(b"data", b"\x00\x00" * 10)],  # data before fmt
        [(b"fmt ", _fmt(channels=0)), (b"data", b"\x00" * 20)],  # no channels
        [(b"fmt ", b"\x01\x00")],  # short fmt, no data
        [(b"fmt ", _fmt())],  # no data chunk
    ],
)
def test_broken_wav_headers(tmp_path, chunks, monkeypatch):
    monkeypatch.setattr(audio_mod, "default_backends", lambda: (WAV,))
    path = tmp_path / "bad.wav"
    path.write_bytes(_riff(chunks))
    with pytest.raises(AudioError, match="WAV"):
        decode_audio(path)


def test_wav_header_limit_check_happens_before_reading(tmp_path, monkeypatch):
    from shoutabout import wavdecode

    path = write_wav(tmp_path / "long.wav", tone(5.0, 8000), 8000)
    monkeypatch.setattr(
        wavdecode, "read_samples", lambda f, info: pytest.fail("read the samples anyway")
    )
    with pytest.raises(AudioTooLongError):
        decode_audio(path, max_seconds=2.0)
