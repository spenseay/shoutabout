"""The ShoutAbout brand, rendered in pure Python (stdlib only: no Pillow, no fonts).

The mark is an open mouth (black throat, a white strip of top teeth, a red tongue) on a
Shout Yellow tile. This module draws it for the tray icon, the app icons and the docs
(see docs/brand.md), writes PNG / animated PNG files, and converts images to the Windows
icon-resource format :mod:`shoutabout.icons` hands to ``CreateIconFromResourceEx``.

Rendering model: every shape here is convex, so a horizontal line crosses it in at most
one span. The rasterizer takes ``samples`` sub-scanlines per pixel row, resolves the
spans painted on each into non-overlapping coloured intervals (later layers on top; no
seams where shapes touch) and integrates those exactly along x. That is supersampling
with infinite horizontal and ``samples`` vertical resolution, and the work per row is
proportional to the number of shape edges, not to the image width: a 32 px tray icon
takes a few milliseconds, a 1024 px app icon well under a second.

Images are straight-alpha RGBA with top-down rows. All geometry follows the owner's brand
sheet; the numbers are named constants below.
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

RGB = tuple[int, int, int]
Span = tuple[float, float]


def hex_rgb(value: str) -> RGB:
    """'#FFE600' -> (255, 230, 0)."""
    digits = value.strip().lstrip("#")
    if len(digits) != 6:
        raise ValueError(f"expected #RRGGBB, got {value!r}")
    return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


# --------------------------------------------------------------------------- palette

YELLOW = hex_rgb("#FFE600")  # Shout Yellow: the ground
BLACK = hex_rgb("#0A0A0A")  # type, the mouth / throat
RED = hex_rgb("#FF1A1A")  # Tongue Red: the accent
WHITE = hex_rgb("#FFFFFF")  # teeth, highlights
GREY = hex_rgb("#8C8C84")  # the "starting" tray tile
TASKBAR_DARK = hex_rgb("#202020")
TASKBAR_LIGHT = hex_rgb("#F3F3F3")
PAPER = hex_rgb("#FAF9F5")

PALETTE: dict[str, RGB] = {
    "Shout Yellow": YELLOW,
    "Black": BLACK,
    "Tongue Red": RED,
    "Teeth": WHITE,
    "Starting Grey": GREY,
    "Dark taskbar": TASKBAR_DARK,
    "Paper": PAPER,
}

# Alternative "loud" grounds from the sheet, for seasonal / beta variants.
LOUD_GROUNDS: dict[str, RGB] = {
    "blue": hex_rgb("#1A2BFF"),
    "magenta": hex_rgb("#FF2E9A"),
    "orange": hex_rgb("#FF5A00"),
    "green": hex_rgb("#3DFF3D"),
}

# -------------------------------------------------------------------------- geometry
# The mouth lives in a box W x H. Reference box: 170 x 130 (open). CSS outline:
# border-radius 50% / 42% 42% 58% 58% = the top half of an ellipse (rx W/2, ry .42H)
# on the bottom half of another (rx W/2, ry .58H), both centred at (W/2, .42H).

REF_W = 170.0
OPEN = 130.0  # box height (px of the 170 px reference width): wide open, tongue out
AJAR = 72.0  # resting / ready
TOP_RY = 0.42
BOTTOM_RY = 0.58
TEETH_DEPTH = 26.0  # teeth band depth in reference px: 20 % of the open height
TEETH_MAX = 0.55  # ...but never more than this fraction of a nearly-closed mouth
# Tongue: ellipse, left 28 % / width 44 % / height 62 %, bottom 24 % below the box.
TONGUE_CX, TONGUE_CY, TONGUE_RX, TONGUE_RY = 0.50, 0.93, 0.22, 0.31
# Shut mouth: a black pill, fractions of the tile.
SHUT_W, SHUT_H = 0.624, 0.125

TRAY_RADIUS = 0.23  # tray tile corner radius, fraction of its size
TRAY_MOUTH_W = 75 / 96  # mouth width on a tray tile
APP_RADIUS = 0.225  # rounded previews of the (full-bleed) app icon
APP_MOUTH_W = 0.78  # primary / alt: 156 of 200
DARK_MOUTH_W = 0.70
DARK_LIP = 0.035  # yellow lip ring on the dark icon, fraction of the tile
BLEED_MOUTH_W, BLEED_LEFT, BLEED_TOP = 1.30, -0.15, -0.07

# ------------------------------------------------------------------------ animation
# saTalk: mouth box height keyframes (px of 130), 1.6 s, ease-in-out between keyframes.
TALK_DURATION_MS = 1600
TALK_KEYFRAMES: tuple[tuple[float, float], ...] = (
    (0.00, 130),
    (0.10, 58),
    (0.22, 118),
    (0.32, 40),
    (0.44, 130),
    (0.54, 84),
    (0.64, 124),
    (0.74, 46),
    (0.86, 108),
    (1.00, 130),
)
# saPunch: scale keyframes, 1.6 s, ease-out (in step with saTalk: the punch lands on the
# 44 % wide-open peak). The sheet also rotates -2/+1 deg; too small to matter in pixels.
PUNCH_KEYFRAMES: tuple[tuple[float, float], ...] = (
    (0.00, 1.0),
    (0.38, 1.0),
    (0.44, 1.07),
    (0.50, 0.98),
    (0.60, 1.0),
    (1.00, 1.0),
)
EASE_IN_OUT = (0.42, 0.0, 0.58, 1.0)  # CSS ease-in-out
EASE_OUT = (0.0, 0.0, 0.58, 1.0)  # CSS ease-out

TALK_FRAMES = 12  # tray "transcribing" loop: 12 x 133 ms = 1.6 s
HUM_FRAMES = 8  # tray "starting" loop: the shut mouth hums while the model loads
HUM_DURATION_MS = 1600
HUM_SQUASH = 0.28  # the pill narrows by up to this fraction (and thickens to match)

TRAY_STATES = ("starting", "idle", "transcribing", "paused", "error")
APP_ICON_VARIANTS = ("primary", "dark", "alt", "bleed")

# state -> (tile colour, mouth): "shut" pill, "ajar" (no tongue), "open" (tongue out)
TRAY_LOOKS: dict[str, tuple[RGB, str]] = {
    "starting": (GREY, "shut"),
    "idle": (YELLOW, "ajar"),
    "transcribing": (YELLOW, "open"),
    "paused": (YELLOW, "shut"),
    "error": (RED, "ajar"),
}
# The macOS menu bar rests on the mark itself (the app icon's mouth: wide open, tongue out);
# a teeth-only mouth reads oddly up there. Every other state is the tray's.
MENUBAR_LOOKS: dict[str, tuple[RGB, str]] = {**TRAY_LOOKS, "idle": (YELLOW, "open")}


# ---------------------------------------------------------------------------- easing


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """A CSS ``cubic-bezier()`` timing function: progress in [0, 1] -> eased progress."""

    def coord(t: float, p1: float, p2: float) -> float:
        u = 1.0 - t
        return 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t

    def ease(x: float) -> float:
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        lo, hi = 0.0, 1.0
        t = x
        for _ in range(60):  # bisection: x(t) is monotonic for 0 <= x1, x2 <= 1
            if coord(t, x1, x2) < x:
                lo = t
            else:
                hi = t
            t = (lo + hi) / 2
            if hi - lo < 1e-9:
                break
        return coord(t, y1, y2)

    return ease


def keyframe_value(
    keyframes: Sequence[tuple[float, float]], t: float, ease: Callable[[float], float]
) -> float:
    """CSS-style keyframe interpolation at loop position ``t`` (wraps into [0, 1))."""
    t = t % 1.0
    for (k0, v0), (k1, v1) in zip(keyframes, keyframes[1:], strict=False):
        if k0 <= t <= k1:
            p = 0.0 if k1 == k0 else (t - k0) / (k1 - k0)
            return v0 + (v1 - v0) * ease(p)
    return keyframes[-1][1]


_ease_in_out = cubic_bezier(*EASE_IN_OUT)
_ease_out = cubic_bezier(*EASE_OUT)


def talk_height(t: float) -> float:
    """saTalk mouth height (px of 130) at loop position t in [0, 1)."""
    return keyframe_value(TALK_KEYFRAMES, t, _ease_in_out)


def punch_scale(t: float) -> float:
    """saPunch scale at loop position t in [0, 1)."""
    return keyframe_value(PUNCH_KEYFRAMES, t, _ease_out)


def talk_frames(n: int = TALK_FRAMES) -> list[float]:
    """Mouth heights (px of 130, between 40 and 130) for ``n`` evenly spaced frames of
    one saTalk loop, starting wide open."""
    if n < 1:
        raise ValueError("need at least one frame")
    return [talk_height(i / n) for i in range(n)]


def punch_frames(n: int = TALK_FRAMES) -> list[float]:
    if n < 1:
        raise ValueError("need at least one frame")
    return [punch_scale(i / n) for i in range(n)]


def hum_frames(n: int = HUM_FRAMES) -> list[float]:
    """Shut-pill squash factors (1 = full width) for the "starting" loop."""
    if n < 1:
        raise ValueError("need at least one frame")
    return [1.0 - HUM_SQUASH * (1 - math.cos(2 * math.pi * i / n)) / 2 for i in range(n)]


# ---------------------------------------------------------------------------- shapes


class Shape:
    """A convex shape: :meth:`span` gives the [x0, x1) it covers on the line y."""

    __slots__ = ()

    def span(self, y: float) -> Span | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def contains(self, x: float, y: float) -> bool:
        s = self.span(y)
        return s is not None and s[0] <= x < s[1]


class Rect(Shape):
    __slots__ = ("x0", "x1", "y0", "y1")

    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    def span(self, y: float) -> Span | None:
        return (self.x0, self.x1) if self.y0 <= y < self.y1 else None


def above(y: float) -> Rect:
    """The half-plane above the line y."""
    return Rect(-math.inf, -math.inf, math.inf, y)


class RoundRect(Shape):
    """Rounded rectangle; ``r`` = half the height makes a pill."""

    __slots__ = ("r", "x0", "x1", "y0", "y1")

    def __init__(self, x0: float, y0: float, x1: float, y1: float, r: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))

    def span(self, y: float) -> Span | None:
        if not self.y0 <= y < self.y1:
            return None
        r = self.r
        if y < self.y0 + r:
            d = self.y0 + r - y
        elif y > self.y1 - r:
            d = y - (self.y1 - r)
        else:
            return (self.x0, self.x1)
        inset = r - math.sqrt(max(0.0, r * r - d * d))
        return (self.x0 + inset, self.x1 - inset)


class Egg(Shape):
    """The top half of one ellipse on the bottom half of another (same centre and rx):
    the mouth outline. With ``ry_top == ry_bottom`` it's a plain ellipse."""

    __slots__ = ("cx", "cy", "rx", "ry_bottom", "ry_top")

    def __init__(self, cx: float, cy: float, rx: float, ry_top: float, ry_bottom: float) -> None:
        self.cx, self.cy, self.rx = cx, cy, rx
        self.ry_top, self.ry_bottom = ry_top, ry_bottom

    def span(self, y: float) -> Span | None:
        ry = self.ry_top if y < self.cy else self.ry_bottom
        if ry <= 0.0 or self.rx <= 0.0:
            return None
        d = (y - self.cy) / ry
        t = 1.0 - d * d
        if t <= 0.0:
            return None
        h = self.rx * math.sqrt(t)
        return (self.cx - h, self.cx + h)


def ellipse(cx: float, cy: float, rx: float, ry: float) -> Egg:
    return Egg(cx, cy, rx, ry, ry)


class Polygon(Shape):
    """A convex polygon through ``points`` (in order, either way round). Rotated and
    sheared shapes become polygons (see :func:`rounded_rect_points`)."""

    __slots__ = ("edges", "y0", "y1")

    def __init__(self, points: Sequence[tuple[float, float]]) -> None:
        pts = [(float(x), float(y)) for x, y in points]
        if len(pts) < 3:
            raise ValueError("a polygon needs at least 3 points")
        self.y0 = min(y for _, y in pts)
        self.y1 = max(y for _, y in pts)
        edges = []
        for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1], strict=True):
            if ay == by:
                continue  # horizontal: its ends are on the neighbouring edges
            if ay > by:
                ax, ay, bx, by = bx, by, ax, ay
            edges.append((ay, by, ax, (bx - ax) / (by - ay)))
        self.edges = tuple(edges)

    def span(self, y: float) -> Span | None:
        if not self.y0 <= y < self.y1:
            return None
        lo, hi = math.inf, -math.inf
        for ey0, ey1, x0, slope in self.edges:
            if ey0 <= y <= ey1:
                x = x0 + (y - ey0) * slope
                lo, hi = min(lo, x), max(hi, x)
        return (lo, hi) if hi > lo else None


def rounded_rect_points(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    radii: tuple[float, float, float, float],
    steps: int = 6,
) -> list[tuple[float, float]]:
    """The outline of a rectangle with its own radius at each corner (top-left, top-right,
    bottom-right, bottom-left, like CSS ``border-radius``), clockwise on screen, each
    corner a ``steps``-segment arc: feed it to :class:`Polygon`, after any transform."""
    limit = max(0.0, min(x1 - x0, y1 - y0) / 2)
    tl, tr, br, bl = (max(0.0, min(r, limit)) for r in radii)
    corners = (  # arc centre, radius, start angle (degrees, y down: 270 = up)
        (x0 + tl, y0 + tl, tl, 180),
        (x1 - tr, y0 + tr, tr, 270),
        (x1 - br, y1 - br, br, 0),
        (x0 + bl, y1 - bl, bl, 90),
    )
    pts: list[tuple[float, float]] = []
    for cx, cy, r, start in corners:
        if r <= 0:
            pts.append((cx, cy))
            continue
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class Intersect(Shape):
    __slots__ = ("shapes",)

    def __init__(self, *shapes: Shape) -> None:
        self.shapes = shapes

    def span(self, y: float) -> Span | None:
        lo, hi = -math.inf, math.inf
        for shape in self.shapes:
            s = shape.span(y)
            if s is None:
                return None
            lo, hi = max(lo, s[0]), min(hi, s[1])
            if hi <= lo:
                return None
        return (lo, hi)


@dataclass(frozen=True)
class Layer:
    shape: Shape
    color: RGB


# ------------------------------------------------------------------------ rasterizer


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    rgba: bytes  # straight alpha, top-down rows

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        i = (y * self.width + x) * 4
        r, g, b, a = self.rgba[i : i + 4]
        return (r, g, b, a)

    @property
    def opaque(self) -> bool:
        return self.rgba[3::4] == b"\xff" * (self.width * self.height)


Interval = tuple[float, float, RGB]
_CLEAR = b"\0\0\0\0"


def _intervals(y: float, layers: Sequence[Layer], clip: Shape | None, width: int) -> list:
    """Painted, non-overlapping (x0, x1, colour) intervals on the line y, left to right."""
    lo, hi = 0.0, float(width)
    if clip is not None:
        s = clip.span(y)
        if s is None:
            return []
        lo, hi = max(lo, s[0]), min(hi, s[1])
        if hi <= lo:
            return []
    painted: list[Interval] = []
    for layer in layers:
        s = layer.shape.span(y)
        if s is not None:
            a, b = max(s[0], lo), min(s[1], hi)
            if b > a:
                painted.append((a, b, layer.color))
    if len(painted) <= 1:
        return painted
    cuts = sorted({p for a, b, _ in painted for p in (a, b)})
    out: list[Interval] = []
    for p, q in zip(cuts, cuts[1:], strict=False):
        m = (p + q) * 0.5
        for a, b, color in reversed(painted):  # the last layer painted is on top
            if a <= m < b:
                if out and out[-1][1] == p and out[-1][2] == color:
                    out[-1] = (out[-1][0], q, color)
                else:
                    out.append((p, q, color))
                break
    return out


def _resolve(acc: list[float], n: int) -> bytes:
    r, g, b, cover = acc
    if cover <= 0.0:
        return _CLEAR
    alpha = round(255 * cover / n)
    if alpha <= 0:
        return _CLEAR
    return bytes((round(r / cover), round(g / cover), round(b / cover), min(alpha, 255)))


def _run_color(subrows: list[list[Interval]], xm: float, n: int) -> bytes:
    acc = [0.0, 0.0, 0.0, 0.0]
    for ivs in subrows:
        for a, b, (cr, cg, cb) in ivs:
            if a <= xm < b:
                acc[0] += cr
                acc[1] += cg
                acc[2] += cb
                acc[3] += 1.0
                break
    return _resolve(acc, n)


def _pixel_color(subrows: list[list[Interval]], x: int, n: int) -> bytes:
    acc = [0.0, 0.0, 0.0, 0.0]
    x1 = x + 1
    for ivs in subrows:
        for a, b, (cr, cg, cb) in ivs:
            if b <= x or a >= x1:
                continue
            c = min(b, x1) - max(a, x)
            acc[0] += cr * c
            acc[1] += cg * c
            acc[2] += cb * c
            acc[3] += c
    return _resolve(acc, n)


def rasterize(
    width: int,
    height: int,
    layers: Sequence[Layer],
    *,
    clip: Shape | None = None,
    samples: int = 8,
) -> Image:
    """Paint ``layers`` (first = bottom) into a transparent width x height image,
    optionally clipped to ``clip``, with ``samples`` sub-scanlines per pixel row."""
    if width < 1 or height < 1:
        raise ValueError(f"bad image size {width}x{height}")
    n = max(1, int(samples))
    out = bytearray(width * height * 4)
    last = width - 1
    for j in range(height):
        subrows = [_intervals(j + (s + 0.5) / n, layers, clip, width) for s in range(n)]
        if not any(subrows):
            continue
        # Pixels holding an interval end are integrated exactly; between them every
        # sub-scanline is constant, so each run of pixels is one colour.
        events = sorted({min(int(x), last) for ivs in subrows for a, b, _ in ivs for x in (a, b)})
        base = j * width * 4
        prev = -1
        for e in [*events, width]:
            if e - prev > 1:
                color = _run_color(subrows, prev + 1.5, n)
                if color != _CLEAR:
                    out[base + (prev + 1) * 4 : base + e * 4] = color * (e - prev - 1)
            if e < width:
                out[base + e * 4 : base + e * 4 + 4] = _pixel_color(subrows, e, n)
            prev = e
    return Image(width, height, bytes(out))


# ------------------------------------------------------------------------- the mark


def teeth_depth(w: float, h: float) -> float:
    """Depth of the teeth band: fixed (20 % of the open mouth), capped on a shut-ish one."""
    return min(w * TEETH_DEPTH / REF_W, h * TEETH_MAX)


def mouth_layers(
    x: float,
    y: float,
    w: float,
    opening: float = OPEN,
    *,
    throat: RGB = BLACK,
    teeth: RGB = WHITE,
    tongue: RGB | None = RED,
    lip: RGB | None = None,
    lip_width: float = 0.0,
) -> list[Layer]:
    """The mouth in the box (x, y, w, h), h = w * opening / 170: lip ring (optional),
    throat, tongue (``None`` = hidden) and the teeth band, bottom to top."""
    h = w * opening / REF_W
    cx, cy = x + w / 2, y + TOP_RY * h
    outline = Egg(cx, cy, w / 2, TOP_RY * h, BOTTOM_RY * h)
    layers: list[Layer] = []
    if lip is not None and lip_width > 0:
        # CSS box-shadow spread: the box grows by lip_width, every radius too.
        ring = Egg(cx, cy, w / 2 + lip_width, TOP_RY * h + lip_width, BOTTOM_RY * h + lip_width)
        layers.append(Layer(ring, lip))
    layers.append(Layer(outline, throat))
    if tongue is not None:
        blob = ellipse(x + TONGUE_CX * w, y + TONGUE_CY * h, TONGUE_RX * w, TONGUE_RY * h)
        layers.append(Layer(Intersect(blob, outline), tongue))
    depth = teeth_depth(w, h)
    if depth > 0:
        layers.append(Layer(Intersect(outline, above(y + depth)), teeth))
    return layers


def centred_mouth_layers(
    cx: float,
    cy: float,
    w: float,
    opening: float = OPEN,
    *,
    tongue: RGB | None = RED,
    lip: RGB | None = None,
    lip_width: float = 0.0,
) -> list[Layer]:
    """:func:`mouth_layers` for a mouth box centred on (cx, cy)."""
    h = w * opening / REF_W
    return mouth_layers(
        cx - w / 2, cy - h / 2, w, opening, tongue=tongue, lip=lip, lip_width=lip_width
    )


def shut_layer(cx: float, cy: float, w: float, h: float, color: RGB = BLACK) -> Layer:
    return Layer(RoundRect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, h / 2), color)


def mouth_image(
    width: int,
    opening: float = OPEN,
    *,
    tongue: RGB | None = RED,
    throat: RGB = BLACK,
    teeth: RGB = WHITE,
    samples: int = 8,
) -> Image:
    """Just the mouth on transparency, filling ``width``; height follows the opening."""
    height = max(1, math.ceil(width * opening / REF_W))
    layers = mouth_layers(0, 0, width, opening, throat=throat, teeth=teeth, tongue=tongue)
    return rasterize(width, height, layers, samples=samples)


# ----------------------------------------------------------------------- tray icons


def tray_icon(
    state: str,
    size: int,
    *,
    opening: float | None = None,
    scale: float = 1.0,
    squash: float = 1.0,
    samples: int = 8,
    looks: dict[str, tuple[RGB, str]] = TRAY_LOOKS,
) -> Image:
    """One tray tile (rounded square, radius 23 %) for a hub state.

    ``opening`` overrides the mouth height (px of 130) and ``scale`` its width (talk
    frames); ``squash`` narrows-and-thickens the shut pill (hum frames); ``looks`` maps
    states to tiles and mouths (:data:`MENUBAR_LOOKS` on a Mac)."""
    if state not in looks:
        raise ValueError(f"unknown tray state {state!r}")
    tile_color, mouth = looks[state]
    tile = RoundRect(0, 0, size, size, TRAY_RADIUS * size)
    layers = [Layer(tile, tile_color)]
    c = size / 2
    snap = size <= SNAP_MAX  # small icons: put the key edges on whole pixels
    if mouth == "shut":
        w, h = SHUT_W * size * squash, SHUT_H * size / squash
        if snap:
            w, h = _snap_len(w, size), max(1, round(h))
            y0 = math.floor((size - h) / 2)
            layers.append(
                Layer(RoundRect((size - w) / 2, y0, (size + w) / 2, y0 + h, h / 2), BLACK)
            )
        else:
            layers.append(shut_layer(c, c, w, h))
    else:
        if opening is None:
            opening = OPEN if mouth == "open" else AJAR
        tongue = RED if mouth == "open" else None
        w = TRAY_MOUTH_W * size * scale
        if snap:
            w = _snap_len(w, size)
        h = w * opening / REF_W
        x, y = c - w / 2, c - h / 2
        if snap:  # nudge the mouth (< 1/2 px) so the teeth line is crisp
            line = y + teeth_depth(w, h)
            y += round(line) - line
        layers += mouth_layers(x, y, w, opening, tongue=tongue)
    return rasterize(size, size, layers, clip=tile, samples=samples)


SNAP_MAX = 48


def _snap_len(value: float, size: int) -> int:
    """The whole number nearest ``value`` with the parity of ``size``, so a length
    centred in the tile starts and ends on pixel edges."""
    n = math.floor(value)
    if (size - n) % 2:
        n = n + 1 if value - n >= 0.5 else n - 1
    return max(2 - size % 2, n)


@lru_cache(maxsize=64)
def tray_frames(state: str, size: int) -> tuple[Image, ...]:
    """The frames for a tray state at ``size`` px (cached). Static states have one;
    "transcribing" talks (saTalk + saPunch) and "starting" hums while it loads."""
    if state == "transcribing":
        return tuple(
            tray_icon(state, size, opening=h, scale=s)
            for h, s in zip(talk_frames(TALK_FRAMES), punch_frames(TALK_FRAMES), strict=True)
        )
    if state == "starting":
        return tuple(tray_icon(state, size, squash=q) for q in hum_frames(HUM_FRAMES))
    return (tray_icon(state, size),)


@lru_cache(maxsize=64)
def menubar_frames(state: str, size: int) -> tuple[Image, ...]:
    """:func:`tray_frames` for the macOS menu bar (:data:`MENUBAR_LOOKS`: at rest the mouth
    is open with its red tongue, like the app icon)."""
    if MENUBAR_LOOKS.get(state) == TRAY_LOOKS.get(state):
        return tray_frames(state, size)
    return (tray_icon(state, size, looks=MENUBAR_LOOKS),)


def frame_interval_ms(state: str) -> int:
    """Milliseconds per frame for an animated tray state (0 = static)."""
    if state == "transcribing":
        return round(TALK_DURATION_MS / TALK_FRAMES)
    if state == "starting":
        return round(HUM_DURATION_MS / HUM_FRAMES)
    return 0


# ------------------------------------------------------------------------ app icons


def app_icon(
    variant: str = "primary",
    size: int = 1024,
    *,
    opening: float = OPEN,
    scale: float = 1.0,
    rounded: bool = False,
    samples: int = 8,
) -> Image:
    """App icon artwork. Full-bleed square by default (iOS masks the corners itself);
    ``rounded`` gives a preview tile with transparent corners.

    primary: yellow, mouth 78 % wide - dark: black, mouth 70 % with a yellow lip ring -
    alt: red tile, yellow tongue (beta / seasonal) - bleed: mouth 130 % wide, cropped."""
    s = float(size)
    tile: Shape = RoundRect(0, 0, s, s, APP_RADIUS * s) if rounded else Rect(0, 0, s, s)
    if variant == "primary":
        ground, layers = (
            YELLOW,
            centred_mouth_layers(s / 2, s / 2, APP_MOUTH_W * s * scale, opening),
        )
    elif variant == "alt":
        w = APP_MOUTH_W * s * scale
        ground, layers = RED, centred_mouth_layers(s / 2, s / 2, w, opening, tongue=YELLOW)
    elif variant == "dark":
        w = DARK_MOUTH_W * s * scale
        ground = BLACK
        layers = centred_mouth_layers(s / 2, s / 2, w, opening, lip=YELLOW, lip_width=DARK_LIP * s)
    elif variant == "bleed":
        w = BLEED_MOUTH_W * s * scale
        # Anchored like the sheet (left -15 %, top -7 % when open); talks around that centre.
        cx = BLEED_LEFT * s + BLEED_MOUTH_W * s / 2
        cy = BLEED_TOP * s + BLEED_MOUTH_W * s * OPEN / REF_W / 2
        ground, layers = YELLOW, centred_mouth_layers(cx, cy, w, opening)
    else:
        raise ValueError(f"unknown app icon variant {variant!r}")
    return rasterize(size, size, [Layer(tile, ground), *layers], clip=tile, samples=samples)


def talking_app_icon_frames(
    variant: str = "primary", size: int = 256, n: int = 16, *, rounded: bool = True
) -> list[Image]:
    """saTalk + saPunch on an app icon: ``n`` frames of one 1.6 s loop."""
    return [
        app_icon(variant, size, opening=h, scale=p, rounded=rounded)
        for h, p in zip(talk_frames(n), punch_frames(n), strict=True)
    ]


def strip(frames: Sequence[Image], gap: int = 0) -> Image:
    """Lay equally sized frames out left to right (transparent gaps)."""
    if not frames:
        raise ValueError("no frames")
    fw, fh = frames[0].width, frames[0].height
    if any((f.width, f.height) != (fw, fh) for f in frames):
        raise ValueError("frames differ in size")
    width = fw * len(frames) + gap * (len(frames) - 1)
    out = bytearray(width * fh * 4)
    for i, f in enumerate(frames):
        x = i * (fw + gap)
        for y in range(fh):
            dst = (y * width + x) * 4
            out[dst : dst + fw * 4] = f.rgba[y * fw * 4 : (y + 1) * fw * 4]
    return Image(width, fh, bytes(out))


def compose(
    width: int,
    height: int,
    background: RGB | None,
    placements: Sequence[tuple[Image, int, int]],
) -> Image:
    """Alpha-blend images (top-left at x, y) over a solid or transparent background."""
    out = bytearray(width * height * 4)
    if background is not None:
        out[:] = bytes((*background, 255)) * (width * height)
    for img, ox, oy in placements:
        for y in range(img.height):
            ty = oy + y
            if not 0 <= ty < height:
                continue
            for x in range(img.width):
                tx = ox + x
                if not 0 <= tx < width:
                    continue
                si = (y * img.width + x) * 4
                sa = img.rgba[si + 3]
                if sa == 0:
                    continue
                di = (ty * width + tx) * 4
                if sa == 255:
                    out[di : di + 4] = img.rgba[si : si + 4]
                    continue
                a, da = sa / 255, out[di + 3] / 255
                oa = a + da * (1 - a)
                for k in range(3):
                    sc, dc = img.rgba[si + k], out[di + k]
                    out[di + k] = round((sc * a + dc * da * (1 - a)) / oa)
                out[di + 3] = round(oa * 255)
    return Image(width, height, bytes(out))


# ------------------------------------------------------------------------------ PNG

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _check_buffer(width: int, height: int, rgba: bytes) -> None:
    if width < 1 or height < 1:
        raise ValueError(f"bad image size {width}x{height}")
    if len(rgba) != width * height * 4:
        raise ValueError(f"expected {width * height * 4} RGBA bytes, got {len(rgba)}")


def _scanlines(width: int, height: int, rgba: bytes, alpha: bool) -> bytes:
    """Filter type 0 (None) rows: flat-colour artwork compresses very well without one."""
    if alpha:
        data, stride = bytes(rgba), width * 4
    else:
        rgb = bytearray(width * height * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = rgba[0::4], rgba[1::4], rgba[2::4]
        data, stride = bytes(rgb), width * 3
    return b"".join(b"\0" + data[y * stride : (y + 1) * stride] for y in range(height))


def png_bytes(width: int, height: int, rgba: bytes, *, alpha: bool | None = None) -> bytes:
    """A PNG of straight-alpha RGBA pixels (top-down). ``alpha=None`` writes RGB when
    every pixel is opaque (what App Store icons need), RGBA otherwise."""
    _check_buffer(width, height, rgba)
    if alpha is None:
        alpha = rgba[3::4] != b"\xff" * (width * height)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    idat = zlib.compress(_scanlines(width, height, rgba, alpha), 9)
    return (
        PNG_SIGNATURE
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", idat)
        + png_chunk(b"IEND", b"")
    )


def write_png(
    path: str | Path, width: int, height: int, rgba: bytes, *, alpha: bool | None = None
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(width, height, rgba, alpha=alpha))
    return path


def apng_bytes(frames: Sequence[Image], delay_ms: int, *, loops: int = 0) -> bytes:
    """An animated PNG (plays in browsers and on GitHub; other viewers show frame 1)."""
    if not frames:
        raise ValueError("no frames")
    width, height = frames[0].width, frames[0].height
    for f in frames:
        if (f.width, f.height) != (width, height):
            raise ValueError("frames differ in size")
        _check_buffer(f.width, f.height, f.rgba)
    parts = [
        PNG_SIGNATURE,
        png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        png_chunk(b"acTL", struct.pack(">II", len(frames), loops)),
    ]
    seq = 0
    for i, f in enumerate(frames):
        # fcTL: seq, size, offset, delay num/den, dispose NONE, blend SOURCE (full frames)
        fctl = struct.pack(">IIIIIHHBB", seq, width, height, 0, 0, delay_ms, 1000, 0, 0)
        parts.append(png_chunk(b"fcTL", fctl))
        seq += 1
        data = zlib.compress(_scanlines(width, height, f.rgba, True), 9)
        if i == 0:
            parts.append(png_chunk(b"IDAT", data))
        else:
            parts.append(png_chunk(b"fdAT", struct.pack(">I", seq) + data))
            seq += 1
    parts.append(png_chunk(b"IEND", b""))
    return b"".join(parts)


def write_apng(path: str | Path, frames: Sequence[Image], delay_ms: int) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(apng_bytes(frames, delay_ms))
    return path


# ------------------------------------------------------------- Windows icon resources

BITMAPINFOHEADER_SIZE = 40


def and_mask_stride(width: int) -> int:
    """Bytes per row of the 1 bpp AND mask (rows are DWORD-aligned)."""
    return ((width + 31) // 32) * 4


def icon_resource(image: Image) -> bytes:
    """Icon-resource bytes for ``CreateIconFromResourceEx`` and .ico files: a
    BITMAPINFOHEADER (biHeight = 2 x height: XOR image + AND mask), 32 bpp bottom-up BGRA
    with straight alpha, then a 1 bpp AND mask (bit set = transparent, alpha 0)."""
    w, h = image.width, image.height
    if not (1 <= w <= 256 and 1 <= h <= 256):
        raise ValueError(f"icon images must be 1..256 px, got {w}x{h}")
    _check_buffer(w, h, image.rgba)
    bgra = bytearray(image.rgba)
    bgra[0::4], bgra[2::4] = image.rgba[2::4], image.rgba[0::4]
    stride = w * 4
    xor = b"".join(bytes(bgra[y * stride : (y + 1) * stride]) for y in reversed(range(h)))
    mstride = and_mask_stride(w)
    mask = bytearray(mstride * h)
    alpha = image.rgba[3::4]
    for row, y in enumerate(reversed(range(h))):  # bottom-up, like the pixels
        for x in range(w):
            if alpha[y * w + x] == 0:
                mask[row * mstride + x // 8] |= 0x80 >> (x % 8)
    header = struct.pack(
        "<IiiHHIIiiII",
        BITMAPINFOHEADER_SIZE,  # biSize
        w,  # biWidth
        h * 2,  # biHeight: XOR + AND
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        len(xor) + len(mask),  # biSizeImage
        0,  # biXPelsPerMeter
        0,  # biYPelsPerMeter
        0,  # biClrUsed
        0,  # biClrImportant
    )
    return header + xor + bytes(mask)


def ico_bytes(images: Sequence[Image]) -> bytes:
    """A complete .ico file (ICONDIR + entries + images), one entry per image."""
    blobs = [(img, icon_resource(img)) for img in images]
    head = struct.pack("<HHH", 0, 1, len(blobs))
    offset = len(head) + 16 * len(blobs)
    entries = b""
    for img, blob in blobs:
        bw = 0 if img.width >= 256 else img.width
        bh = 0 if img.height >= 256 else img.height
        entries += struct.pack("<BBBBHHII", bw, bh, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    return head + entries + b"".join(blob for _, blob in blobs)
