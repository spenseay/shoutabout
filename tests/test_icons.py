from __future__ import annotations

import ctypes
import struct

import pytest

from shoutabout import brand, icons
from shoutabout.models import HubStatus


@pytest.mark.parametrize("size", [16, 20, 24, 32])
def test_icon_image_layout(size):
    blob = icons.icon_image("idle", size)
    header = struct.unpack("<IiiHHIIiiII", blob[:40])
    bi_size, width, height, planes, bpp, compression, size_image, *_ = header
    assert bi_size == icons.BITMAPINFOHEADER_SIZE == 40
    assert (width, height) == (size, 2 * size)
    assert (planes, bpp, compression) == (1, 32, 0)
    xor_len = size * size * 4
    and_len = icons.and_mask_stride(size) * size
    assert size_image == xor_len + and_len
    assert len(blob) == 40 + xor_len + and_len


def test_and_mask_stride_is_dword_aligned():
    assert icons.and_mask_stride(16) == 4
    assert icons.and_mask_stride(32) == 4
    assert icons.and_mask_stride(33) == 8
    assert icons.and_mask_stride(48) == 8


def _pixel(blob: bytes, size: int, x: int, y_top: int) -> tuple[int, int, int, int]:
    row = size - 1 - y_top  # bottom-up
    off = 40 + (row * size + x) * 4
    b, g, r, a = blob[off : off + 4]
    return r, g, b, a


def _and_bit(blob: bytes, size: int, x: int, y_top: int) -> int:
    stride = icons.and_mask_stride(size)
    row = size - 1 - y_top
    byte = blob[40 + size * size * 4 + row * stride + x // 8]
    return (byte >> (7 - x % 8)) & 1


TILES = {
    "starting": brand.GREY,
    "idle": brand.YELLOW,
    "transcribing": brand.YELLOW,
    "paused": brand.YELLOW,
    "error": brand.RED,
}


@pytest.mark.parametrize("size", [16, 32])
@pytest.mark.parametrize("key", icons.STATES)
def test_icon_pixels_are_the_brand_tile(key, size):
    blob = icons.icon_image(key, size)
    c = size // 2
    assert _pixel(blob, size, 1, c) == (*TILES[key], 255)  # tile beside the mouth
    assert _pixel(blob, size, c, c - 1)[:3] == brand.BLACK  # the mouth / pill
    for x, y in [(0, 0), (size - 1, size - 1)]:  # rounded corners: transparent
        assert _pixel(blob, size, x, y)[3] == 0
        assert _and_bit(blob, size, x, y) == 1
    assert _and_bit(blob, size, c, c) == 0
    alphas = {blob[40 + i * 4 + 3] for i in range(size * size)}
    assert any(0 < a < 255 for a in alphas)  # anti-aliased corners
    # identical to the renderer, pixel for pixel
    img = brand.tray_frames(key, size)[0]
    for y in range(size):
        for x in range(size):
            assert _pixel(blob, size, x, y) == img.pixel(x, y)


def test_icon_size_and_key_bounds():
    with pytest.raises(ValueError):
        icons.icon_image("idle", 4)
    with pytest.raises(ValueError):
        icons.icon_image("idle", 300)
    with pytest.raises(ValueError):
        icons.icon_image("green", 16)


def test_animated_frames():
    assert icons.frame_count("transcribing", 16) == brand.TALK_FRAMES
    assert icons.frame_count("starting", 16) == brand.HUM_FRAMES
    assert icons.is_animated("transcribing") and icons.is_animated("starting")
    for key in ("idle", "paused", "error"):
        assert icons.frame_count(key, 16) == 1
        assert not icons.is_animated(key)
        assert icons.frame_interval_ms(key) == 0
    assert 120 <= icons.frame_interval_ms("transcribing") <= 160
    frames = icons.icon_frames("transcribing", 24)
    assert icons.icon_frames("transcribing", 24) is frames  # cached
    assert icons.icon_image("transcribing", 24, 0) == frames[0]
    assert icons.icon_image("transcribing", 24, 13) == frames[13 % len(frames)]  # wraps
    assert len(set(frames)) >= len(frames) - 1


def test_ico_file_structure():
    data = icons.ico_file("error", sizes=(16, 32))
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 2)
    offset_expected = 6 + 2 * 16
    for i, size in enumerate((16, 32)):
        w, h, colors, res, planes, bpp, length, offset = struct.unpack(
            "<BBBBHHII", data[6 + i * 16 : 6 + (i + 1) * 16]
        )
        assert (w, h, colors, res, planes, bpp) == (size, size, 0, 0, 1, 32)
        assert offset == offset_expected
        assert data[offset : offset + length] == icons.icon_image("error", size)
        offset_expected += length
    assert len(data) == offset_expected


@pytest.mark.parametrize(
    ("status", "paused", "key"),
    [
        (HubStatus.STARTING, False, "starting"),
        (HubStatus.IDLE, False, "idle"),
        (HubStatus.TRANSCRIBING, False, "transcribing"),
        (HubStatus.IDLE, True, "paused"),
        (HubStatus.TRANSCRIBING, True, "transcribing"),  # still talks while finishing up
        (HubStatus.ERROR, True, "error"),
        (HubStatus.ERROR, False, "error"),
        (HubStatus.STARTING, True, "starting"),
        ("error", False, "error"),
        ("idle", True, "paused"),
    ],
)
def test_icon_key(status, paused, key):
    assert icons.icon_key(status, paused) == key
    assert key in icons.STATES


# ------------------------------------------------------------------- real Windows


@pytest.mark.windows
@pytest.mark.parametrize("size", [16, 24, 32, None])
def test_make_hicon(size):
    hicon = icons.make_hicon("idle", size)
    try:
        assert hicon
    finally:
        icons.destroy_hicon(hicon)


@pytest.mark.windows
def test_make_hicons_one_per_frame():
    handles = icons.make_hicons("transcribing", 24)
    try:
        assert len(handles) == brand.TALK_FRAMES
        assert len(set(handles)) == len(handles) and all(handles)
    finally:
        for h in handles:
            icons.destroy_hicon(h)


@pytest.mark.windows
def test_small_icon_size():
    assert 16 <= icons.small_icon_size() <= 64


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", ctypes.c_int),
        ("xHotspot", ctypes.c_uint32),
        ("yHotspot", ctypes.c_uint32),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _read_hicon_bgra(hicon: int, size: int) -> bytes:
    """The colour bitmap of an HICON as top-down BGRA (GetIconInfo + GetDIBits)."""
    user32 = ctypes.WinDLL("user32")  # private instances: our own prototypes
    gdi32 = ctypes.WinDLL("gdi32")
    user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ICONINFO)]
    user32.GetIconInfo.restype = ctypes.c_int
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFOHEADER),
        ctypes.c_uint,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    info = _ICONINFO()
    assert user32.GetIconInfo(hicon, ctypes.byref(info))
    hdc = user32.GetDC(None)
    try:
        bih = _BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bih.biWidth, bih.biHeight = size, -size  # negative height: top-down rows
        bih.biPlanes, bih.biBitCount, bih.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(size * size * 4)
        lines = gdi32.GetDIBits(hdc, info.hbmColor, 0, size, buf, ctypes.byref(bih), 0)
        assert lines == size
        return buf.raw
    finally:
        user32.ReleaseDC(None, hdc)
        gdi32.DeleteObject(info.hbmColor)  # GetIconInfo hands us copies
        gdi32.DeleteObject(info.hbmMask)


@pytest.mark.windows
@pytest.mark.parametrize(("key", "frame"), [("transcribing", 5), ("error", 0)])
def test_hicon_bits_match_the_renderer(key, frame):
    size = 24
    hicon = icons.make_hicon(key, size, frame)
    try:
        bgra = _read_hicon_bgra(hicon, size)
    finally:
        icons.destroy_hicon(hicon)
    img = brand.tray_frames(key, size)[frame]
    expected = bytearray(img.rgba)
    expected[0::4], expected[2::4] = img.rgba[2::4], img.rgba[0::4]
    assert bgra == bytes(expected)
