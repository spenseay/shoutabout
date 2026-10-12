from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path

import pytest

from shoutabout import brand

REPO = Path(__file__).resolve().parents[1]
BRAND_DIR = REPO / "brand"


def rgb(img: brand.Image, x: int, y: int) -> tuple[int, int, int]:
    return img.pixel(x, y)[:3]


def alpha(img: brand.Image, x: int, y: int) -> int:
    return img.pixel(x, y)[3]


# ------------------------------------------------------------------- PNG decoding


def read_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    assert data[:8] == brand.PNG_SIGNATURE
    chunks, pos = [], 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, f"bad CRC in {kind!r}"
        chunks.append((kind, body))
        pos += 12 + length
    assert pos == len(data)
    assert chunks[0][0] == b"IHDR" and chunks[-1] == (b"IEND", b"")
    return chunks


def unfilter_none(raw: bytes, width: int, height: int, channels: int) -> bytes:
    stride = width * channels
    assert len(raw) == height * (stride + 1)
    out = bytearray()
    for y in range(height):
        row = raw[y * (stride + 1) : (y + 1) * (stride + 1)]
        assert row[0] == 0, "writer uses filter type 0"
        out += row[1:]
    return bytes(out)


def decode_png(data: bytes) -> brand.Image:
    """Decode the PNGs brand.png_bytes writes (8-bit RGB / RGBA, filter 0) to RGBA."""
    chunks = read_chunks(data)
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, comp, filt, interlace) == (8, 0, 0, 0)
    assert ctype in (2, 6)
    idat = b"".join(body for kind, body in chunks if kind == b"IDAT")
    channels = 4 if ctype == 6 else 3
    pixels = unfilter_none(zlib.decompress(idat), width, height, channels)
    if channels == 4:
        return brand.Image(width, height, pixels)
    rgba = bytearray(b"\xff" * (width * height * 4))
    rgba[0::4], rgba[1::4], rgba[2::4] = pixels[0::3], pixels[1::3], pixels[2::3]
    return brand.Image(width, height, bytes(rgba))


# ------------------------------------------------------------------------ palette


def test_palette_matches_the_brand_sheet():
    assert brand.YELLOW == (0xFF, 0xE6, 0x00)
    assert brand.BLACK == (0x0A, 0x0A, 0x0A)
    assert brand.RED == (0xFF, 0x1A, 0x1A)
    assert brand.WHITE == (0xFF, 0xFF, 0xFF)
    assert brand.GREY == (0x8C, 0x8C, 0x84)
    assert brand.hex_rgb("#202020") == brand.TASKBAR_DARK
    with pytest.raises(ValueError):
        brand.hex_rgb("#FFF")


# ------------------------------------------------------------------ mouth geometry


@pytest.fixture(scope="module")
def open_mouth() -> brand.Image:
    return brand.mouth_image(170, brand.OPEN)  # the 170 x 130 reference box


def test_open_mouth_reference_geometry(open_mouth):
    m = open_mouth
    assert (m.width, m.height) == (170, 130)
    assert rgb(m, 85, 8) == brand.WHITE  # teeth band: top 26 px (20 %)
    assert rgb(m, 85, 22) == brand.WHITE
    assert rgb(m, 85, 30) == brand.BLACK  # below the teeth: throat
    assert rgb(m, 85, 65) == brand.BLACK  # centre: throat
    assert rgb(m, 85, 120) == brand.RED  # tongue rises from the bottom centre
    assert rgb(m, 60, 100) == brand.RED  # tongue rx = 22 % of W around the centre line
    assert rgb(m, 40, 100) == brand.BLACK
    assert rgb(m, 2, 54) == brand.BLACK  # widest at 42 % of H
    for x, y in [(0, 0), (169, 0), (0, 129), (169, 129), (10, 5), (5, 110)]:
        assert alpha(m, x, y) == 0  # outside the outline: transparent
    assert all(a == 255 for a in (alpha(m, 85, y) for y in range(1, 129)))


def test_mouth_outline_is_the_css_egg(open_mouth):
    m = open_mouth
    # The bottom half (ry 58 %) is deeper than the top (ry 42 %): at equal distances
    # from the widest line, the mouth is wider below than above.
    cy = round(0.42 * 130)

    def width_at(y: int) -> int:
        return sum(1 for x in range(170) if alpha(m, x, y) == 255)

    assert width_at(cy - 40) < width_at(cy + 40)
    assert abs(width_at(cy) - 170) <= 2
    # anti-aliased: partially transparent pixels on the outline
    assert any(0 < m.rgba[i] < 255 for i in range(3, len(m.rgba), 4))
    # mirror symmetric (catches off-by-one rows / columns)
    for y in range(0, 130, 7):
        for x in range(85):
            assert abs(alpha(m, x, y) - alpha(m, 169 - x, y)) <= 1


def test_ajar_mouth_and_hidden_tongue():
    m = brand.mouth_image(170, brand.AJAR, tongue=None)
    assert (m.width, m.height) == (170, 72)
    assert rgb(m, 85, 10) == brand.WHITE  # the same 26 px of teeth
    assert rgb(m, 85, 50) == brand.BLACK
    assert brand.RED not in {m.pixel(x, y)[:3] for y in range(72) for x in range(170)}


def test_mouth_colours_are_configurable():
    m = brand.mouth_image(170, throat=brand.GREY, teeth=brand.YELLOW, tongue=brand.WHITE)
    assert rgb(m, 85, 8) == brand.YELLOW
    assert rgb(m, 85, 65) == brand.GREY
    assert rgb(m, 85, 120) == brand.WHITE


def test_teeth_depth_is_fixed_but_capped():
    w = 170.0
    assert brand.teeth_depth(w, 130) == pytest.approx(26)
    assert brand.teeth_depth(w, 72) == pytest.approx(26)  # as on the sheet's tray icons
    assert brand.teeth_depth(w, 40) == pytest.approx(40 * brand.TEETH_MAX)


# --------------------------------------------------------------------- tray tiles


TILE = {
    "starting": brand.GREY,
    "idle": brand.YELLOW,
    "transcribing": brand.YELLOW,
    "paused": brand.YELLOW,
    "error": brand.RED,
}


@pytest.mark.parametrize("size", [16, 20, 24, 32, 64])
@pytest.mark.parametrize("state", brand.TRAY_STATES)
def test_tray_tile_colour_and_corners(state, size):
    img = brand.tray_icon(state, size)
    assert (img.width, img.height) == (size, size)
    assert rgb(img, 1, size // 2) == TILE[state]  # left edge, beside the mouth
    assert alpha(img, 1, size // 2) == 255
    assert rgb(img, size // 2, 1) == TILE[state]  # top edge
    for x, y in [(0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)]:
        assert alpha(img, x, y) == 0  # rounded (23 %) corners are transparent
    for y in range(size):  # left/right mirror symmetric
        for x in range(size // 2):
            assert abs(alpha(img, x, y) - alpha(img, size - 1 - x, y)) <= 1


@pytest.mark.parametrize("size", [16, 24, 32, 64])
def test_tray_mouths(size):
    c = size // 2
    for state in ("starting", "paused"):  # shut: a black pill through the centre
        img = brand.tray_icon(state, size)
        assert rgb(img, c, c) == brand.BLACK
        assert rgb(img, c, c - size // 4) == TILE[state]  # above the pill: tile
    img = brand.tray_icon("error", size)  # ajar on the red tile: teeth over throat
    assert rgb(img, c, c) == brand.BLACK
    assert brand.WHITE in {img.pixel(x, y)[:3] for y in range(size) for x in range(size)}
    for state in ("idle", "transcribing"):  # open, the mark itself: teeth, throat, tongue
        img = brand.tray_icon(state, size)
        assert rgb(img, c, c - 1) == brand.BLACK
        column = [img.pixel(c, y)[:3] for y in range(size)]
        assert brand.WHITE in column[: c - 1]  # teeth above the centre
        assert brand.RED in column[c:]  # tongue below it
        assert column.index(brand.WHITE) < column.index(brand.BLACK) < column.index(brand.RED)


def test_tray_states_are_distinct():
    # "transcribing" rests on the same mouth as "idle" and tells itself apart by talking.
    still = [s for s in brand.TRAY_STATES if s != "transcribing"]
    images = {state: brand.tray_icon(state, 24).rgba for state in still}
    assert len(set(images.values())) == len(still)
    assert len({f.rgba for f in brand.tray_frames("transcribing", 24)}) > 1


def test_small_tray_icons_are_pixel_snapped():
    # Shut pill at 16 px: exactly two crisp black rows.
    img = brand.tray_icon("paused", 16)
    assert [rgb(img, 8, y) == brand.BLACK for y in range(16)].count(True) == 2
    # Teeth line on a pixel edge: a pure white pixel directly over a pure black one.
    img = brand.tray_icon("idle", 24)
    column = [img.pixel(12, y)[:3] for y in range(24)]
    i = max(y for y, c in enumerate(column) if c == brand.WHITE)
    assert column[i + 1] == brand.BLACK


def test_unknown_tray_state():
    with pytest.raises(ValueError):
        brand.tray_icon("shouting", 16)


# ---------------------------------------------------------------------- animation


def test_talk_frames_follow_the_satalk_keyframes():
    frames = brand.talk_frames(12)
    assert len(frames) == 12
    assert frames[0] == pytest.approx(130)  # starts wide open
    assert all(40 <= h <= 130 for h in frames)
    assert min(frames) < 50 and max(frames) > 120  # really talks
    assert len(brand.talk_frames(1)) == 1
    assert len(brand.talk_frames(40)) == 40
    with pytest.raises(ValueError):
        brand.talk_frames(0)
    for t, h in brand.TALK_KEYFRAMES:
        assert brand.talk_height(t % 1.0) == pytest.approx(h if t < 1 else 130)
    # ease-in-out: halfway between keyframes is halfway in value, a quarter is less
    assert brand.talk_height(0.05) == pytest.approx((130 + 58) / 2, abs=0.01)
    assert brand.talk_height(0.025) > 130 - (130 - 58) / 4


def test_punch_and_hum():
    assert brand.punch_scale(0.44) == pytest.approx(1.07)
    assert brand.punch_scale(0.50) == pytest.approx(0.98)
    assert brand.punch_scale(0.2) == pytest.approx(1.0)
    hum = brand.hum_frames(8)
    assert hum[0] == pytest.approx(1.0)
    assert min(hum) == pytest.approx(1 - brand.HUM_SQUASH)


def test_cubic_bezier_matches_css_ease():
    ease = brand.cubic_bezier(*brand.EASE_IN_OUT)
    assert ease(0) == 0 and ease(1) == 1
    assert ease(0.5) == pytest.approx(0.5, abs=1e-6)
    assert ease(0.25) == pytest.approx(0.1291, abs=1e-3)  # CSS ease-in-out at 25 %
    values = [ease(i / 50) for i in range(51)]
    assert values == sorted(values)


def test_tray_frames():
    talk = brand.tray_frames("transcribing", 24)
    assert len(talk) == brand.TALK_FRAMES
    assert len({f.rgba for f in talk}) >= brand.TALK_FRAMES - 1  # (nearly) all distinct
    assert talk[0].rgba == brand.tray_icon("transcribing", 24).rgba  # frame 0 = open
    assert len(brand.tray_frames("starting", 24)) == brand.HUM_FRAMES
    for state in ("idle", "paused", "error"):
        assert len(brand.tray_frames(state, 24)) == 1
        assert brand.frame_interval_ms(state) == 0
    assert brand.tray_frames("transcribing", 24) is talk  # cached
    assert 120 <= brand.frame_interval_ms("transcribing") <= 160
    assert brand.frame_interval_ms("transcribing") * brand.TALK_FRAMES == pytest.approx(
        brand.TALK_DURATION_MS, abs=brand.TALK_FRAMES
    )
    assert brand.frame_interval_ms("starting") > 0


def test_tray_frames_render_fast():
    start = time.perf_counter()
    for h, s in zip(brand.talk_frames(), brand.punch_frames(), strict=True):
        brand.tray_icon("transcribing", 32, opening=h, scale=s)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"12 frames at 32 px took {elapsed * 1000:.0f} ms"  # ~15 ms here


# ----------------------------------------------------------------------- app icons


def test_app_icon_primary():
    img = brand.app_icon("primary", 180)
    assert img.opaque  # full bleed: iOS masks the corners itself
    assert rgb(img, 0, 0) == brand.YELLOW
    assert rgb(img, 5, 90) == brand.YELLOW  # mouth is 78 % wide: margin of 11 %
    assert rgb(img, 25, 82) == brand.BLACK
    assert rgb(img, 90, 50) == brand.WHITE
    assert rgb(img, 90, 90) == brand.BLACK
    assert rgb(img, 90, 125) == brand.RED


def test_app_icon_dark_has_a_yellow_lip():
    img = brand.app_icon("dark", 180)
    assert rgb(img, 5, 5) == brand.BLACK
    # mouth 126 px wide centred: its widest line is at y ~ 82; the lip ring (6.3 px)
    # sits just outside the outline on both sides.
    assert rgb(img, 23, 82) == brand.YELLOW
    assert rgb(img, 156, 82) == brand.YELLOW
    assert rgb(img, 32, 82) == brand.BLACK
    assert rgb(img, 90, 50) == brand.WHITE
    assert rgb(img, 90, 125) == brand.RED


def test_app_icon_alt_and_bleed():
    alt = brand.app_icon("alt", 180)
    assert rgb(alt, 5, 5) == brand.RED
    assert rgb(alt, 90, 128) == brand.YELLOW  # yellow tongue
    bleed = brand.app_icon("bleed", 180)
    assert rgb(bleed, 0, 0) == brand.YELLOW
    assert rgb(bleed, 90, 0) == brand.WHITE  # teeth cropped by the top edge
    assert rgb(bleed, 90, 160) == brand.RED
    assert rgb(bleed, 90, 176) == brand.YELLOW
    assert rgb(bleed, 0, 70) == brand.BLACK  # mouth wider than the tile
    with pytest.raises(ValueError):
        brand.app_icon("loud", 64)


def test_rounded_app_icon_preview():
    img = brand.app_icon("primary", 64, rounded=True)
    assert alpha(img, 0, 0) == 0 and alpha(img, 32, 0) == 255


def test_talking_app_icon_frames():
    frames = brand.talking_app_icon_frames("primary", 48, 6)
    assert len(frames) == 6 and len({f.rgba for f in frames}) == 6


def test_strip_and_compose():
    a = brand.tray_icon("idle", 16)
    b = brand.tray_icon("error", 16)
    s = brand.strip([a, b], gap=2)
    assert (s.width, s.height) == (34, 16)
    assert s.pixel(1, 8) == a.pixel(1, 8) and s.pixel(19, 8) == b.pixel(1, 8)
    assert s.pixel(16, 8)[3] == 0  # the gap
    c = brand.compose(20, 20, brand.TASKBAR_DARK, [(a, 2, 2)])
    assert c.pixel(0, 0) == (*brand.TASKBAR_DARK, 255)
    assert c.pixel(3, 10) == a.pixel(1, 8)
    assert c.opaque
    with pytest.raises(ValueError):
        brand.strip([a, brand.tray_icon("idle", 24)])


def test_rasterizer_has_no_seams_between_touching_shapes():
    # Two rectangles meeting mid-pixel: the shared pixel is a clean mix, fully opaque.
    left = brand.Layer(brand.Rect(0, 0, 2.5, 4), brand.WHITE)
    right = brand.Layer(brand.Rect(2.5, 0, 5, 4), brand.BLACK)
    img = brand.rasterize(5, 4, [left, right])
    r, g, b, a = img.pixel(2, 1)
    assert a == 255 and r == g == b and abs(r - (255 + 10) / 2) <= 0.5
    assert img.pixel(0, 0) == (255, 255, 255, 255)
    assert img.pixel(4, 3) == (*brand.BLACK, 255)


def test_polygon_spans_a_convex_outline_either_way_round():
    tri = brand.Polygon([(0, 0), (10, 5), (0, 10)])  # a "V" seam's wedge, point right
    assert tri.span(-0.1) is None and tri.span(10) is None
    assert tri.span(5) == pytest.approx((0, 10))
    assert tri.span(2.5) == pytest.approx((0, 5))
    assert brand.Polygon([(0, 10), (10, 5), (0, 0)]).span(2.5) == pytest.approx((0, 5))
    square = brand.Polygon([(1, 1), (3, 1), (3, 3), (1, 3)])  # horizontal edges are fine
    assert square.span(2) == (1, 3) and square.contains(2, 1) and not square.contains(2, 3)
    assert brand.Polygon([(0, 0), (0, 0), (0, 0)]).span(0) is None  # degenerate: empty
    with pytest.raises(ValueError):
        brand.Polygon([(0, 0), (1, 1)])


def test_polygon_rasterizes_like_the_same_rect():
    rect = brand.rasterize(9, 7, [brand.Layer(brand.Rect(1.25, 0.5, 7.5, 6.25), brand.RED)])
    poly = brand.Polygon([(1.25, 0.5), (7.5, 0.5), (7.5, 6.25), (1.25, 6.25)])
    assert brand.rasterize(9, 7, [brand.Layer(poly, brand.RED)]) == rect


def test_rounded_rect_points_follow_each_corner_radius():
    pts = brand.rounded_rect_points(0, 0, 40, 44, (10, 18, 18, 10), steps=8)
    assert len(pts) == 4 * 9
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((0, 40, 0, 44))
    shape = brand.Polygon(pts)
    # the top-left corner is rounder on the right (18) than on the left (10)
    assert shape.span(4)[0] == pytest.approx(10 - (10**2 - 6**2) ** 0.5, abs=0.15)
    assert 40 - shape.span(4)[1] == pytest.approx(18 - (18**2 - 14**2) ** 0.5, abs=0.15)
    assert shape.span(22) == pytest.approx((0, 40))
    # radii beyond half the box are clamped (a pill); a zero radius is a sharp corner
    pill = brand.rounded_rect_points(0, 0, 30, 10, (99, 99, 99, 99))
    assert brand.Polygon(pill).span(5) == pytest.approx((0, 30))
    assert (0, 0) in brand.rounded_rect_points(0, 0, 4, 4, (0, 1, 1, 1))


# ---------------------------------------------------------------------------- PNG


def test_png_roundtrip_rgba():
    img = brand.tray_icon("transcribing", 32)
    data = brand.png_bytes(img.width, img.height, img.rgba)
    chunks = read_chunks(data)
    assert [k for k, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBBBBB", chunks[0][1]) == (32, 32, 8, 6, 0, 0, 0)
    assert decode_png(data) == img


def test_png_opaque_images_are_written_as_rgb():
    img = brand.app_icon("primary", 40)
    data = brand.png_bytes(img.width, img.height, img.rgba)
    assert struct.unpack(">IIBBBBB", read_chunks(data)[0][1])[3] == 2  # RGB, no alpha
    assert decode_png(data) == img
    forced = brand.png_bytes(img.width, img.height, img.rgba, alpha=True)
    assert struct.unpack(">IIBBBBB", read_chunks(forced)[0][1])[3] == 6


def test_write_png(tmp_path):
    img = brand.tray_icon("idle", 16)
    path = brand.write_png(tmp_path / "sub" / "idle.png", 16, 16, img.rgba)
    assert decode_png(path.read_bytes()) == img
    with pytest.raises(ValueError):
        brand.png_bytes(16, 16, img.rgba[:-1])
    with pytest.raises(ValueError):
        brand.png_bytes(0, 16, b"")


def test_apng_structure():
    frames = list(brand.tray_frames("transcribing", 16))[:4]
    data = brand.apng_bytes(frames, 133)
    chunks = read_chunks(data)
    kinds = [k for k, _ in chunks]
    assert kinds[:3] == [b"IHDR", b"acTL", b"fcTL"]
    assert kinds.index(b"acTL") < kinds.index(b"IDAT")
    assert struct.unpack(">II", chunks[1][1]) == (4, 0)  # 4 frames, loop forever
    seqs = []
    decoded = []
    for kind, body in chunks:
        if kind == b"fcTL":
            seq, w, h, x, y, num, den, dispose, blend = struct.unpack(">IIIIIHHBB", body)
            assert (w, h, x, y, num, den, dispose, blend) == (16, 16, 0, 0, 133, 1000, 0, 0)
            seqs.append(seq)
        elif kind == b"fdAT":
            seqs.append(struct.unpack(">I", body[:4])[0])
            decoded.append(unfilter_none(zlib.decompress(body[4:]), 16, 16, 4))
        elif kind == b"IDAT":
            decoded.insert(0, unfilter_none(zlib.decompress(body), 16, 16, 4))
    assert seqs == list(range(len(seqs))) and len(seqs) == 2 * 4 - 1
    assert decoded == [f.rgba for f in frames]
    # A plain PNG decoder sees the first frame.
    assert decode_png(data).rgba == frames[0].rgba


# ---------------------------------------------------------------------- ICO bytes


@pytest.mark.parametrize("size", [16, 24, 33])
def test_icon_resource_layout(size):
    img = brand.tray_icon("error", size)
    blob = brand.icon_resource(img)
    bi_size, width, height, planes, bpp, comp, size_image, *_ = struct.unpack(
        "<IiiHHIIiiII", blob[:40]
    )
    xor_len = size * size * 4
    and_len = brand.and_mask_stride(size) * size
    assert (bi_size, width, height, planes, bpp, comp) == (40, size, 2 * size, 1, 32, 0)
    assert size_image == xor_len + and_len == len(blob) - 40
    # bottom-up BGRA: the first stored row is the image's last row
    for x, y in [(0, size - 1), (1, size // 2), (size // 2, size // 2)]:
        row = size - 1 - y
        b, g, r, a = blob[40 + (row * size + x) * 4 : 40 + (row * size + x) * 4 + 4]
        assert (r, g, b, a) == img.pixel(x, y)
    # AND mask bit set exactly where alpha == 0
    stride = brand.and_mask_stride(size)
    for y in range(size):
        row = size - 1 - y
        for x in range(size):
            bit = (blob[40 + xor_len + row * stride + x // 8] >> (7 - x % 8)) & 1
            assert bit == (img.pixel(x, y)[3] == 0)


def test_and_mask_stride():
    assert [brand.and_mask_stride(n) for n in (1, 16, 32, 33, 48, 64)] == [4, 4, 4, 8, 8, 8]


def test_ico_bytes():
    images = [brand.tray_icon("idle", s) for s in (16, 32)] + [brand.app_icon("primary", 256)]
    data = brand.ico_bytes(images)
    assert struct.unpack("<HHH", data[:6]) == (0, 1, 3)
    offset = 6 + 3 * 16
    for i, img in enumerate(images):
        entry = struct.unpack("<BBBBHHII", data[6 + i * 16 : 6 + (i + 1) * 16])
        dim = img.width % 256  # 256 is stored as 0
        assert entry[:6] == (dim, dim, 0, 0, 1, 32)
        assert entry[7] == offset
        assert data[offset : offset + entry[6]] == brand.icon_resource(img)
        offset += entry[6]
    assert len(data) == offset
    with pytest.raises(ValueError):
        brand.icon_resource(brand.app_icon("primary", 300))


# ------------------------------------------------------------ committed brand assets


@pytest.mark.skipif(not (BRAND_DIR / "tray").is_dir(), reason="brand/ assets not present")
@pytest.mark.parametrize("state", brand.TRAY_STATES)
@pytest.mark.parametrize("size", [16, 32, 64])
def test_committed_tray_pngs_match_the_renderer(state, size):
    """brand/ is generated by scripts/export_brand_assets.py: re-run it after changes."""
    path = BRAND_DIR / "tray" / f"{state}-{size}.png"
    assert decode_png(path.read_bytes()) == brand.tray_frames(state, size)[0]


@pytest.mark.skipif(not BRAND_DIR.is_dir(), reason="brand/ assets not present")
def test_committed_brand_assets_are_small_and_valid():
    files = sorted(BRAND_DIR.rglob("*.png"))
    assert len(files) >= 25
    total = sum(p.stat().st_size for p in files)
    assert total < 1_500_000, f"brand/ PNGs total {total} bytes"
    for path in files:
        read_chunks(path.read_bytes())
    for variant in brand.APP_ICON_VARIANTS:
        for size in (1024, 180):
            img = decode_png((BRAND_DIR / "app-icon" / f"{variant}-{size}.png").read_bytes())
            assert (img.width, img.height) == (size, size) and img.opaque
