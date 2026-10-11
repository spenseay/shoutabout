"""The "mouth pill": a little yellow pill with the mouth, at the top of the screen while
you talk.

States (see :func:`frame_at`):

* ``listening``: the mouth opens with your voice (mic level). Nothing else: no glow, no
  clock (the time limit is generous and reaching it simply finishes the recording);
* ``thinking``: the brand talk animation (saTalk + saPunch, as in the tray icon);
* ``done``: the text was pasted: the mouth zips shut (a slider with a paddle pull runs
  along it), the zipped line flattens away and the pill flattens and fades (0.73 s);
* ``error``: a red pill with a few words ("No mic"), then a fade;
* ``notice``: a yellow pill with a few words ("Getting ready… 42%");
* ``cancelled``: the mouth shuts and fades quickly.

The window is a per-pixel-alpha layered popup (``WS_EX_LAYERED | WS_EX_TOPMOST |
WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT``) drawn with
``UpdateLayeredWindow``: it never takes focus (the text must land in *your* window), mouse
clicks go through it, and it has no taskbar button. It sits top-centre on the work area of
the monitor with the foreground window (``rcWork``: a taskbar docked at the top pushes it
down), a small gap below the top edge, at that monitor's DPI. It runs its own thread and
message loop, and animates at ~30 fps only while visible.

It has to appear the instant listening starts, so nothing slow happens then: the window is
created when "Talk to this PC" starts, the frames are rendered on a background thread right
after (for every monitor's DPI), and showing the pill is one ``UpdateLayeredWindow`` with a
cached bitmap plus ``ShowWindow(SW_SHOWNOACTIVATE)``, at full opacity (no fade-in).

Everything visual is a pure function of (state, time, mic level, DPI scale): frames are
rendered with :mod:`shoutabout.brand`, cached, and unit-tested without a window. Text uses
GDI (Segoe UI, greyscale anti-aliasing) on Windows; tests pass a fake text renderer.
"""

from __future__ import annotations

import ctypes
import logging
import math
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

from shoutabout import brand

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------ geometry
# Logical pixels at 100 % scaling (96 DPI); everything scales with the monitor's DPI.

PILL_W = 64.0
PILL_H = 32.0
MARGIN = 5.0  # transparent room around the pill for its shadow
BORDER = 1.5  # ink outline: keeps the yellow visible on white windows
MOUTH_W = 33.0
SHUT_H = 3.6  # the closed mouth's line
TEXT_PX = 13.0
TEXT_PAD = 12.0
ICON_W = 18.0  # the small mouth before the text of error / notice pills
ICON_GAP = 6.0
TOP_GAP = 6.0  # between the work area's top edge and the canvas (the pill sits MARGIN lower)

# ----------------------------------------------------------------------- animation

FPS = 30
LEVEL_STEPS = 14
LISTEN_MIN_OPEN = 40.0  # the narrowest talking mouth (px of 130, like saTalk's minimum)
TALK_STEPS = 40  # thinking loop frames per 1.6 s (~25 fps of distinct frames)
ERROR_HOLD_S, ERROR_FADE_S = 2.4, 0.3
NOTICE_HOLD_S, NOTICE_FADE_S = 1.8, 0.3
CANCEL_FADE_S = 0.2

# "done" (pasted): the mouth zips shut. The owner's "Paddle pull" design (zip variations
# v2, 2c), in the mouth's 170 x 130 box (brand.REF_W x brand.OPEN), scaled to MOUTH_W:
# a white slider pops in at the left and zips along the mouth's centre line. Behind it a
# V seam closes the mouth into an ink line with white zipper teeth; a paddle pull hangs
# from the slider and swings with it. Sealed, the line squashes once while the pull
# settles, then it flattens away and the pill fades. Only time is quantised (FPS frames).
ZIP_MID = brand.TOP_RY * brand.OPEN  # 54.6: the mouth's widest line, where it zips
ZIP_SEAM = 100.0  # the V seam reaches the box's top and bottom this far right of its point
ZIP_FROM, ZIP_TO = -ZIP_SEAM - 12.0, 176.0  # the seam's point travels from / to here
ZIP_PARK = (-14.0, 128.0)  # ...and the slider follows it between these
ZIP_LIP = 34.0  # the zipped line's height
ZIP_TOOTH, ZIP_GAP, ZIP_TOOTH_H, ZIP_INSET = 12.0, 10.0, 12.0, 14.0  # its white teeth
ZIP_INK = 6.0  # the slider's and the pull's ink outline
ZIP_SWING_MAX = 40.0  # degrees
# The design swings the pull 0.07 deg per box px/s of its 0.552 s zip; per bit of
# progress instead, so the swing looks the same at our (faster) speed.
ZIP_SWING_K = 0.07 * (ZIP_TO - ZIP_FROM) / 0.552
ZIP_SETTLE = (26.0, 14.0, 40.0)  # sealed, the pull settles: degrees, decay /s, rad/s
# (the design's 26 deg, 7 /s, 20 rad/s, twice as fast)
ZIP_SQUASH = (0.08, 0.15)  # sealed, the line squashes: this much wider, this much flatter
PILL_FLAT = 0.6  # the pill loses this much of its height as it fades
# Timeline (s): the slider pops in, zips, the line squashes, flattens; the pill fades.
DONE_POP = (0.0, 0.1)
DONE_ZIP = (0.03, 0.33)
DONE_SQUASH = (0.33, 0.51)
DONE_FLAT = (0.47, 0.63)
DONE_FADE = (0.6, 0.73)
DONE_S = DONE_FADE[1]

StateName = Literal["hidden", "listening", "thinking", "done", "error", "notice", "cancelled"]
STATES: tuple[str, ...] = ("listening", "thinking", "done", "error", "notice", "cancelled")

SHADOW = (0, 0, 0)


@dataclass(frozen=True)
class ZipPose:
    """One moment of the zip ("done"), in the mouth's 170 x 130 box."""

    z: float  # the V seam's point, ZIP_FROM..ZIP_TO: the mouth is zipped left of it
    slider: float = 1.0  # the slider's (and its pull's) scale: it pops in
    swing: float = 0.0  # the pull's angle, degrees (+ = clockwise: trailing a move right)
    squash: float = 0.0  # the sealed line's squash, 0..1
    flat: float = 0.0  # the zipped line flattening away, 0..1 (1 = gone)
    pill: float = 1.0  # the pill's height factor: it flattens too as it fades


@dataclass(frozen=True)
class Look:
    """What one frame shows (hashable: frames are cached by it)."""

    kind: Literal["mouth", "text", "zip"]
    opening: float | None = None  # mouth height in px of 130; None = shut
    scale: float = 1.0  # mouth width factor (saPunch)
    tongue: bool = True
    tone: Literal["yellow", "red"] = "yellow"
    text: str = ""
    zip: ZipPose | None = None  # kind "zip": the open mouth, zipping shut


@dataclass(frozen=True)
class Frame:
    look: Look | None  # None = nothing to show
    alpha: int  # 0..255, applied to the whole window
    finished: bool = False  # the state has run its course: hide the window


@dataclass(frozen=True)
class Bitmap:
    """A frame ready for ``UpdateLayeredWindow``: premultiplied BGRA, top-down."""

    width: int
    height: int
    bgra: bytes


# ---------------------------------------------------------------------- text masks


@dataclass(frozen=True)
class TextMask:
    width: int
    height: int
    alpha: bytes  # coverage 0..255, row-major


TextRenderer = Callable[[str, int], TextMask | None]


def gdi_text_mask(text: str, px: int) -> TextMask | None:
    """Render ``text`` (bold Segoe UI, ``px`` high) as a coverage mask with GDI.

    White text on black in a 32-bit DIB with greyscale anti-aliasing: any channel is the
    coverage. Returns None off Windows or if GDI fails.
    """
    if sys.platform != "win32" or not text:
        return None
    try:
        return _gdi_text_mask(text, px)
    except Exception:
        log.debug("text rendering failed", exc_info=True)
        return None


@lru_cache(maxsize=128)
def _gdi_text_mask(text: str, px: int) -> TextMask | None:
    from ctypes import wintypes

    gdi = _Gdi.get()
    hdc = gdi.CreateCompatibleDC(None)
    if not hdc:
        return None
    font = bmp = None
    try:
        font = gdi.CreateFontW(-px, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI")
        old_font = gdi.SelectObject(hdc, font)
        size = _SIZE()
        gdi.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size))
        w, h = int(size.cx) + 2, int(size.cy) + 2
        bits = ctypes.c_void_p()
        bmi = _bitmap_info(w, h)
        bmp = gdi.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not bmp or not bits.value:
            return None
        old_bmp = gdi.SelectObject(hdc, bmp)
        gdi.SetTextColor(hdc, 0x00FFFFFF)
        gdi.SetBkColor(hdc, 0)
        gdi.SetBkMode(hdc, 2)  # OPAQUE
        rect = wintypes.RECT(0, 0, w, h)
        gdi.ExtTextOutW(hdc, 1, 1, 2, ctypes.byref(rect), text, len(text), None)  # ETO_OPAQUE
        gdi.GdiFlush()
        raw = ctypes.string_at(bits.value, w * h * 4)
        gdi.SelectObject(hdc, old_bmp)
        gdi.SelectObject(hdc, old_font)
        alpha = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)[:, :, 1].tobytes()
        return TextMask(w, h, alpha)
    finally:
        if bmp:
            gdi.DeleteObject(bmp)
        if font:
            gdi.DeleteObject(font)
        gdi.DeleteDC(hdc)


# ------------------------------------------------------------------------ shapes


def capsule(cx: float, cy: float, w: float, h: float) -> brand.RoundRect:
    return brand.RoundRect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, h / 2)


# The slider and its paddle pull, in the slider's own box (40 x 44), from the design's
# CSS: (x0, y0, x1, y1, corner radii TL TR BR BL). Each white part has a ZIP_INK outline
# (a box-shadow spread: box and radii grow by it). Painted: pull, then slider on top.
_SLIDER_W, _SLIDER_H = 40.0, 44.0
_Box = tuple[float, float, float, float, tuple[float, float, float, float]]
_SLIDER_BODY: _Box = (0.0, 0.0, 40.0, 44.0, (10.0, 18.0, 18.0, 10.0))
_PULL_BODY: _Box = (0.0, 38.0, 40.0, 84.0, (8.0, 8.0, 20.0, 20.0))  # hangs below it
_PULL_SLOT: _Box = (12.0, 58.0, 28.0, 66.0, (4.0, 4.0, 4.0, 4.0))  # an ink slot in it


def _spread(box: _Box, d: float) -> _Box:
    x0, y0, x1, y1, radii = box
    tl, tr, br, bl = radii
    return (x0 - d, y0 - d, x1 + d, y1 + d, (tl + d, tr + d, br + d, bl + d))


_PULL = (
    (_spread(_PULL_BODY, ZIP_INK), brand.BLACK),
    (_PULL_BODY, brand.WHITE),
    (_PULL_SLOT, brand.BLACK),
)
_SLIDER = ((_spread(_SLIDER_BODY, ZIP_INK), brand.BLACK), (_SLIDER_BODY, brand.WHITE))


def zip_layers(cx: float, cy: float, w: float, pose: ZipPose) -> list[brand.Layer]:
    """The mouth zipping shut, its box ``w`` wide and centred on (cx, cy): what's left of
    the open mouth (right of the V seam), the zipped line with its teeth, the pull and the
    slider, bottom to top."""
    s = w / brand.REF_W
    sx = s * (1 + ZIP_SQUASH[0] * pose.squash)
    sy = s * (1 - ZIP_SQUASH[1] * pose.squash) * (1 - pose.flat)
    mid_y = cy + (ZIP_MID - brand.OPEN / 2) * s  # squash and flatten keep this line

    def at(bx: float, by: float) -> tuple[float, float]:
        return cx + (bx - brand.REF_W / 2) * sx, mid_y + (by - ZIP_MID) * sy

    layers: list[brand.Layer] = []
    z = pose.z
    if z < brand.REF_W and pose.squash == 0 and pose.flat == 0:  # still (partly) open
        far = 2 * brand.REF_W
        wedge = brand.Polygon(
            [
                at(z, ZIP_MID),
                at(z + ZIP_SEAM, -14),
                at(far, -14),
                at(far, brand.OPEN + 20),
                at(z + ZIP_SEAM, brand.OPEN + 20),
            ]
        )
        for layer in brand.centred_mouth_layers(cx, cy, w, brand.OPEN):
            layers.append(brand.Layer(brand.Intersect(layer.shape, wedge), layer.color))
    if sy <= 0:
        return layers

    right = min(max(z + ZIP_INSET, 0.0), brand.REF_W)  # the zipped line's right end
    if right > 0:
        (x0, y0), (x1, y1) = at(0, ZIP_MID - ZIP_LIP / 2), at(right, ZIP_MID + ZIP_LIP / 2)
        layers.append(brand.Layer(brand.RoundRect(x0, y0, x1, y1, (y1 - y0) / 2), brand.BLACK))
        tx = ZIP_INSET
        while tx < right - ZIP_INSET:
            (x0, y0) = at(tx, ZIP_MID - ZIP_TOOTH_H / 2)
            (x1, y1) = at(min(tx + ZIP_TOOTH, right - ZIP_INSET), ZIP_MID + ZIP_TOOTH_H / 2)
            layers.append(brand.Layer(brand.Rect(x0, y0, x1, y1), brand.WHITE))
            tx += ZIP_TOOTH + ZIP_GAP

    k = pose.slider
    if k > 0.01:
        left = min(max(z, ZIP_PARK[0]), ZIP_PARK[1]) - 4  # the slider's box, in the mouth's
        top = ZIP_MID - _SLIDER_H / 2
        px, py = _SLIDER_W / 2, _SLIDER_H / 2  # its centre: it scales and the pull swings here
        a = math.radians(pose.swing)
        cos_a, sin_a = math.cos(a), math.sin(a)
        for parts, turn in ((_PULL, True), (_SLIDER, False)):
            for (x0, y0, x1, y1, radii), color in parts:
                points = []
                for x, y in brand.rounded_rect_points(x0, y0, x1, y1, radii):
                    dx, dy = x - px, y - py
                    if turn:
                        dx, dy = dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a
                    points.append(at(left + px + k * dx, top + py + k * dy))
                layers.append(brand.Layer(brand.Polygon(points), color))
    return layers


# ------------------------------------------------------------------------ rendering
# Layers are kept as premultiplied RGBA uint8 arrays (read-only, cached); composing two of
# them and a bit of text costs well under a millisecond.


def _rgba_array(img: brand.Image) -> np.ndarray:
    return (
        np.frombuffer(img.rgba, dtype=np.uint8).reshape(img.height, img.width, 4).astype(np.float32)
        / 255.0
    )


def _over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Straight-alpha "source over" of two float RGBA arrays of the same size."""
    sa, da = src[..., 3:4], dst[..., 3:4]
    out_a = sa + da * (1 - sa)
    rgb = src[..., :3] * sa + dst[..., :3] * da * (1 - sa)
    safe = np.where(out_a > 0, out_a, 1)
    return np.concatenate([rgb / safe, out_a], axis=-1)


def _solid(
    w: int, h: int, shape: brand.Shape, color: tuple[int, int, int], alpha: float
) -> np.ndarray:
    img = brand.rasterize(w, h, [brand.Layer(shape, color)], samples=6)
    arr = _rgba_array(img)
    arr[..., 3] *= alpha
    return arr


def _premultiply(arr: np.ndarray) -> np.ndarray:
    """Float straight RGBA (0..1) -> premultiplied RGBA uint8 (read-only)."""
    out = np.empty(arr.shape, dtype=np.uint8)
    a = arr[..., 3:4]
    out[..., :3] = np.clip(np.rint(arr[..., :3] * a * 255), 0, 255)
    out[..., 3] = np.clip(np.rint(arr[..., 3] * 255), 0, 255)
    out.flags.writeable = False
    return out


def pill_width(look: Look, text_w: float = 0.0) -> float:
    """Logical width of the pill (text pills grow with their text)."""
    if look.kind == "text":
        return max(PILL_W, TEXT_PAD + ICON_W + ICON_GAP + text_w + TEXT_PAD)
    return PILL_W


def _canvas(pw: float, scale: float) -> tuple[int, int]:
    m = MARGIN * scale
    return math.ceil(pw * scale + 2 * m), math.ceil(PILL_H * scale + 2 * m)


def _text_mask(look: Look, scale: float, text_renderer: TextRenderer | None) -> TextMask | None:
    if look.kind != "text" or not look.text or text_renderer is None:
        return None
    return text_renderer(look.text, max(8, round(TEXT_PX * scale)))


@lru_cache(maxsize=320)  # every warm look (~70) at a few DPI scales, plus text pills
def _body(look: Look, scale: float, text_renderer: TextRenderer | None) -> np.ndarray:
    """The pill, its shadow and what's inside (mouth, or icon + words). Premultiplied
    RGBA uint8."""
    mask = _text_mask(look, scale, text_renderer)
    text_w = mask.width / scale if mask is not None else 0.0
    pw_logical = pill_width(look, text_w)
    pw, ph = pw_logical * scale, PILL_H * scale
    if look.zip is not None:
        ph *= look.zip.pill
    w, h = _canvas(pw_logical, scale)
    cx, cy = w / 2, h / 2
    canvas = np.zeros((h, w, 4), dtype=np.float32)
    for spread, alpha in ((3.0, 0.07), (2.0, 0.08), (1.0, 0.1)):  # soft shadow
        s = spread * scale
        shadow = _solid(w, h, capsule(cx, cy + 1.5 * scale, pw + 2 * s, ph + 2 * s), SHADOW, alpha)
        canvas = _over(canvas, shadow)

    fill = brand.RED if look.tone == "red" else brand.YELLOW
    layers = [
        brand.Layer(capsule(cx, cy, pw, ph), brand.BLACK),
        brand.Layer(capsule(cx, cy, pw - 2 * BORDER * scale, ph - 2 * BORDER * scale), fill),
    ]
    if look.kind == "zip":
        assert look.zip is not None
        layers += zip_layers(cx, cy, MOUTH_W * scale, look.zip)
    elif look.kind == "mouth":
        mw = MOUTH_W * scale * look.scale
        if look.opening is None:
            layers.append(brand.shut_layer(cx, cy, mw * 0.8, max(2.0, SHUT_H * scale)))
        else:
            layers += brand.centred_mouth_layers(
                cx, cy, mw, look.opening, tongue=brand.RED if look.tongue else None
            )
    else:  # text pill: a small mouth, then the words
        left = cx - pw / 2 + TEXT_PAD * scale
        icon_w = ICON_W * scale
        icon_h = icon_w * brand.AJAR / brand.REF_W
        layers += brand.mouth_layers(left, cy - icon_h / 2, icon_w, brand.AJAR)
    canvas = _over(canvas, _rgba_array(brand.rasterize(w, h, layers, samples=6)))

    if mask is not None:
        color = np.array(brand.WHITE if look.tone == "red" else brand.BLACK, dtype=np.float32) / 255
        tx = round(cx - pw / 2 + (TEXT_PAD + ICON_W + ICON_GAP) * scale) - 1
        ty = round(cy - mask.height / 2)
        cov = np.frombuffer(mask.alpha, dtype=np.uint8).reshape(mask.height, mask.width) / 255.0
        x0, y0 = max(0, tx), max(0, ty)
        x1, y1 = min(w, tx + mask.width), min(h, ty + mask.height)
        if x1 > x0 and y1 > y0:
            region = cov[y0 - ty : y1 - ty, x0 - tx : x1 - tx].astype(np.float32)
            text = np.zeros((y1 - y0, x1 - x0, 4), dtype=np.float32)
            text[..., :3] = color
            text[..., 3] = region
            canvas[y0:y1, x0:x1] = _over(canvas[y0:y1, x0:x1], text)
    return _premultiply(canvas)


def compose(
    look: Look, scale: float = 1.0, text_renderer: TextRenderer | None = None
) -> np.ndarray:
    """One frame as premultiplied RGBA uint8 (canvas = pill + margin), at ``scale``."""
    return _body(look, round(scale, 3), text_renderer)


@lru_cache(maxsize=128)
def render(
    look: Look, scale: float = 1.0, text_renderer: TextRenderer | None = None
) -> brand.Image:
    """One overlay frame as a straight-alpha image (screenshots, tests)."""
    arr = compose(look, scale, text_renderer).astype(np.float32)
    a = arr[..., 3:4]
    rgb = np.where(a > 0, arr[..., :3] * 255 / np.where(a > 0, a, 1), 0)
    out = np.concatenate([np.clip(np.rint(rgb), 0, 255), a], axis=-1).astype(np.uint8)
    out[out[..., 3] == 0] = 0
    h, w = out.shape[:2]
    return brand.Image(w, h, out.tobytes())


@lru_cache(maxsize=96)
def bitmap(look: Look, scale: float = 1.0, text_renderer: TextRenderer | None = None) -> Bitmap:
    """One frame as the window wants it (premultiplied BGRA), cached."""
    arr = compose(look, scale, text_renderer)
    h, w = arr.shape[:2]
    return Bitmap(w, h, np.ascontiguousarray(arr[..., [2, 1, 0, 3]]).tobytes())


def premultiplied_bgra(img: brand.Image) -> bytes:
    """What ``UpdateLayeredWindow`` wants: premultiplied alpha, BGRA byte order."""
    arr = np.frombuffer(img.rgba, dtype=np.uint8).reshape(-1, 4).astype(np.uint16)
    a = arr[:, 3:4]
    rgb = (arr[:, :3] * a + 127) // 255
    out = np.empty((arr.shape[0], 4), dtype=np.uint8)
    out[:, 0] = rgb[:, 2]
    out[:, 1] = rgb[:, 1]
    out[:, 2] = rgb[:, 0]
    out[:, 3] = arr[:, 3]
    return out.tobytes()


# ----------------------------------------------------------------------- timeline


def level_opening(level: float) -> float:
    """Mouth opening (px of 130) for a mic level 0..1, quantised so frames are cached."""
    q = round(min(1.0, max(0.0, level)) * (LEVEL_STEPS - 1)) / (LEVEL_STEPS - 1)
    return LISTEN_MIN_OPEN + (brand.OPEN - LISTEN_MIN_OPEN) * q**0.8


def _fade(t: float, hold: float, fade: float) -> tuple[int, bool]:
    if t < hold:
        return 255, False
    if t >= hold + fade:
        return 0, True
    return round(255 * (1 - (t - hold) / fade)), False


def notice_hold(text: str) -> float:
    """How long a notice stays: longer words get time to be read ("Ready · Right Ctrl to
    talk" ~3 s, "Paused" the minimum)."""
    return max(NOTICE_HOLD_S, 0.5 + 0.1 * len(text))


def _progress(t: float, span: tuple[float, float]) -> float:
    a, b = span
    return min(1.0, max(0.0, (t - a) / (b - a)))


def _ease_in_out_quad(u: float) -> float:
    return 2 * u * u if u < 0.5 else -1 + (4 - 2 * u) * u


def _ease_out_back(u: float) -> float:
    c1 = 1.70158
    return 1 + (c1 + 1) * (u - 1) ** 3 + c1 * (u - 1) ** 2


def zip_pose(t: float) -> ZipPose:
    """Where the zip is ``t`` seconds after "done" (the design's easings: the slider pops
    in with ease-out-back and zips with ease-in-out-quad; the pull swings with the
    slider's speed, then settles with a damped sine)."""
    u = _progress(t, DONE_ZIP)
    z = ZIP_FROM + (ZIP_TO - ZIP_FROM) * _ease_in_out_quad(u)
    slider = _ease_out_back(_progress(t, DONE_POP)) if t > DONE_POP[0] else 0.0
    speed = 4 * u if u < 0.5 else 4 * (1 - u)  # d(ease)/du: 0 at both ends
    swing = min(ZIP_SWING_MAX, ZIP_SWING_K * speed)
    ts = t - DONE_ZIP[1]
    if ts > 0:  # sealed: the pull swings on and settles
        amp, decay, rate = ZIP_SETTLE
        swing += amp * math.exp(-decay * ts) * math.sin(rate * ts)
    squash = 0.0
    if DONE_SQUASH[0] < t < DONE_SQUASH[1]:
        squash = math.sin(math.pi * _progress(t, DONE_SQUASH))
    flat = _progress(t, DONE_FLAT) ** 2  # ease-in: it gets sucked flat
    if flat >= 1.0:  # gone: only the pill is left, and it flattens as it fades
        pill = 1 - PILL_FLAT * (1 - (1 - _progress(t, DONE_FADE)) ** 2)  # ease-out
        return ZipPose(ZIP_TO, slider=0.0, flat=1.0, pill=round(pill, 3))
    return ZipPose(
        round(z, 1),
        slider=round(slider, 3),
        swing=round(swing, 1),
        squash=round(squash, 3),
        flat=round(flat, 3),
    )


def done_alpha(t: float) -> int:
    return round(255 * (1 - _progress(t, DONE_FADE)))


@lru_cache(maxsize=1)
def done_frames() -> tuple[Frame, ...]:
    """The zip, one frame per 1/FPS s (the times are quantised so frames are cached)."""
    n = math.ceil(DONE_S * FPS - 1e-9)
    return tuple(Frame(Look("zip", zip=zip_pose(i / FPS)), done_alpha(i / FPS)) for i in range(n))


def first_listening_look() -> Look:
    """What the pill shows the instant listening starts (pre-rendered at start-up)."""
    look = frame_at("listening", 0.0).look
    assert look is not None
    return look


def frame_at(state: str, t: float, level: float = 0.0, text: str = "") -> Frame:
    """The frame for ``state``, ``t`` seconds after it began. Pure."""
    t = max(0.0, t)
    if state == "listening":  # full opacity at once: it must be visible the moment it starts
        return Frame(Look("mouth", level_opening(level)), 255)
    if state == "thinking":
        i = math.floor(t / (brand.TALK_DURATION_MS / 1000) * TALK_STEPS) % TALK_STEPS
        look = Look(
            "mouth",
            round(brand.talk_height(i / TALK_STEPS), 1),
            scale=round(brand.punch_scale(i / TALK_STEPS), 3),
        )
        return Frame(look, 255)
    if state == "done":  # pasted: the mouth zips shut, flattens away, the pill fades
        frames = done_frames()
        if t >= DONE_S:
            return Frame(frames[-1].look, 0, True)
        return frames[min(len(frames) - 1, math.floor(t * FPS + 1e-6))]
    if state == "error":
        alpha, finished = _fade(t, ERROR_HOLD_S, ERROR_FADE_S)
        return Frame(Look("text", tone="red", text=text), alpha, finished)
    if state == "notice":
        alpha, finished = _fade(t, notice_hold(text), NOTICE_FADE_S)
        return Frame(Look("text", text=text), alpha, finished)
    if state == "cancelled":
        alpha, finished = _fade(t, 0.0, CANCEL_FADE_S)
        return Frame(Look("mouth", None), alpha, finished)
    return Frame(None, 0, True)


def place(
    work: tuple[int, int, int, int], width: int, height: int, scale: float
) -> tuple[int, int]:
    """Top-left of the canvas: top-centre of the work area (which excludes a taskbar docked
    at the top), TOP_GAP below its top edge."""
    left, top, right, bottom = work
    x = left + (right - left - width) // 2
    y = top + round(TOP_GAP * scale)
    return x, max(top, min(y, bottom - height))


def short_text(text: str, limit: int = 28) -> str:
    """Overlay text is a few words; anything longer is cut with an ellipsis."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def warm_looks() -> list[Look]:
    """Every frame worth rendering ahead of time, most urgent first: the first listening
    frame, the "No mic" pill, the rest of listening, thinking, then the zip ("done") and
    the shut mouth ("cancelled")."""
    looks = [first_listening_look(), Look("text", tone="red", text="No mic")]
    looks += [Look("mouth", level_opening(i / (LEVEL_STEPS - 1))) for i in range(LEVEL_STEPS)]
    for i in range(TALK_STEPS):
        frame = frame_at("thinking", i * brand.TALK_DURATION_MS / 1000 / TALK_STEPS)
        assert frame.look is not None
        looks.append(frame.look)
    looks += [frame.look for frame in done_frames() if frame.look is not None]
    looks.append(Look("mouth", None))
    return list(dict.fromkeys(looks))  # in order, each once


def prerender(scales: list[float], text_renderer: TextRenderer | None = gdi_text_mask) -> int:
    """Render the frames of :func:`warm_looks` for each DPI scale into the caches (the
    first listening frame for every scale before anything else). Returns the count."""
    scales = sorted({round(s, 3) for s in scales}) or [1.0]
    looks = warm_looks()
    n = 0
    for look in looks[:1]:
        for s in scales:
            bitmap(look, s, text_renderer)
            n += 1
    for s in scales:
        for look in looks[1:]:
            compose(look, s, text_renderer)
            n += 1
    return n


# ------------------------------------------------------------------- the interface


class OverlayLike(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def listening(self) -> None: ...
    def set_level(self, level: float) -> None: ...
    def thinking(self) -> None: ...
    def done(self) -> None: ...
    def error(self, text: str) -> None: ...
    def notice(self, text: str) -> None: ...
    def cancelled(self) -> None: ...
    def hide(self) -> None: ...


class NullOverlay:
    """No window (overlay turned off, or not Windows). Remembers the last state for tests."""

    def __init__(self) -> None:
        self.state: str = "hidden"
        self.text = ""
        self.level = 0.0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.state = "hidden"

    def _set(self, state: str, text: str = "") -> None:
        self.state, self.text = state, text

    def listening(self) -> None:
        self._set("listening")

    def set_level(self, level: float) -> None:
        self.level = level

    def thinking(self) -> None:
        self._set("thinking")

    def done(self) -> None:
        self._set("done")

    def error(self, text: str) -> None:
        self._set("error", short_text(text))

    def notice(self, text: str) -> None:
        self._set("notice", short_text(text))

    def cancelled(self) -> None:
        self._set("cancelled")

    def hide(self) -> None:
        self._set("hidden")


# -------------------------------------------------------------------- Win32 window

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
OVERLAY_EX_STYLE = (
    WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
)
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 0x02
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
WM_APP_WAKE = 0x8000 + 21
WM_APP_PREPARE = 0x8000 + 22
TIMER_ID = 1
MONITOR_DEFAULTTOPRIMARY = 1
MONITOR_DEFAULTTONEAREST = 2
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
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


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", ctypes.c_long * 4),
        ("rcWork", ctypes.c_long * 4),
        ("dwFlags", ctypes.c_uint32),
    ]


def _bitmap_info(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down rows
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    return bmi


_MONITORENUMPROC = (ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE)(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t
)


class _Gdi:
    """Private user32/gdi32/shcore handles with our own prototypes (Windows only)."""

    _instance: _Gdi | None = None

    @classmethod
    def get(cls) -> Any:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        from ctypes import wintypes as wt

        g = ctypes.WinDLL("gdi32", use_last_error=True)
        u = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32, self.user32 = g, u
        vp = ctypes.c_void_p

        def proto(dll: Any, name: str, restype: Any, *args: Any) -> Any:
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = restype, list(args)
            return fn

        self.CreateCompatibleDC = proto(g, "CreateCompatibleDC", vp, vp)
        self.DeleteDC = proto(g, "DeleteDC", wt.BOOL, vp)
        self.CreateDIBSection = proto(
            g,
            "CreateDIBSection",
            vp,
            vp,
            ctypes.POINTER(_BITMAPINFO),
            wt.UINT,
            ctypes.POINTER(vp),
            vp,
            wt.DWORD,
        )
        self.SelectObject = proto(g, "SelectObject", vp, vp, vp)
        self.DeleteObject = proto(g, "DeleteObject", wt.BOOL, vp)
        self.CreateFontW = proto(
            g,
            "CreateFontW",
            vp,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.DWORD,
            wt.LPCWSTR,
        )
        self.GetTextExtentPoint32W = proto(
            g, "GetTextExtentPoint32W", wt.BOOL, vp, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(_SIZE)
        )
        self.SetTextColor = proto(g, "SetTextColor", wt.DWORD, vp, wt.DWORD)
        self.SetBkColor = proto(g, "SetBkColor", wt.DWORD, vp, wt.DWORD)
        self.SetBkMode = proto(g, "SetBkMode", ctypes.c_int, vp, ctypes.c_int)
        self.ExtTextOutW = proto(
            g,
            "ExtTextOutW",
            wt.BOOL,
            vp,
            ctypes.c_int,
            ctypes.c_int,
            wt.UINT,
            ctypes.POINTER(wt.RECT),
            wt.LPCWSTR,
            wt.UINT,
            vp,
        )
        self.GdiFlush = proto(g, "GdiFlush", wt.BOOL)
        self.GetDC = proto(u, "GetDC", vp, vp)
        self.ReleaseDC = proto(u, "ReleaseDC", ctypes.c_int, vp, vp)
        self.UpdateLayeredWindow = proto(
            u,
            "UpdateLayeredWindow",
            wt.BOOL,
            vp,
            vp,
            ctypes.POINTER(wt.POINT),
            ctypes.POINTER(_SIZE),
            vp,
            ctypes.POINTER(wt.POINT),
            wt.DWORD,
            ctypes.POINTER(_BLENDFUNCTION),
            wt.DWORD,
        )
        self.ShowWindow = proto(u, "ShowWindow", wt.BOOL, vp, ctypes.c_int)
        self.SetWindowPos = proto(
            u,
            "SetWindowPos",
            wt.BOOL,
            vp,
            vp,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wt.UINT,
        )
        self.MonitorFromWindow = proto(u, "MonitorFromWindow", vp, vp, wt.DWORD)
        self.MonitorFromPoint = proto(u, "MonitorFromPoint", vp, wt.POINT, wt.DWORD)
        self.GetMonitorInfoW = proto(
            u, "GetMonitorInfoW", wt.BOOL, vp, ctypes.POINTER(_MONITORINFO)
        )
        self.EnumDisplayMonitors = proto(
            u, "EnumDisplayMonitors", wt.BOOL, vp, vp, _MONITORENUMPROC, ctypes.c_ssize_t
        )
        self.GetForegroundWindow = proto(u, "GetForegroundWindow", vp)
        self.SetThreadDpiAwarenessContext = getattr(u, "SetThreadDpiAwarenessContext", None)
        if self.SetThreadDpiAwarenessContext is not None:
            self.SetThreadDpiAwarenessContext.restype = vp
            self.SetThreadDpiAwarenessContext.argtypes = [vp]
        try:
            shcore = ctypes.WinDLL("shcore")
            self.GetDpiForMonitor = proto(
                shcore,
                "GetDpiForMonitor",
                ctypes.c_long,
                vp,
                ctypes.c_int,
                ctypes.POINTER(wt.UINT),
                ctypes.POINTER(wt.UINT),
            )
        except (OSError, AttributeError):
            self.GetDpiForMonitor = None

    def per_monitor_dpi(self) -> None:
        """Make the calling thread per-monitor DPI aware (real pixels on every monitor)."""
        if self.SetThreadDpiAwarenessContext is not None:
            self.SetThreadDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            )

    def monitor_scale(self, mon: Any) -> float:
        from ctypes import wintypes as wt

        if mon and self.GetDpiForMonitor is not None:
            dx, dy = wt.UINT(96), wt.UINT(96)
            if self.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
                return dx.value / 96.0
        return 1.0

    def monitor_scales(self) -> list[float]:
        """The DPI scale of every monitor (call on a per-monitor-aware thread)."""
        found: list[Any] = []

        def collect(mon: Any, _hdc: Any, _rect: Any, _data: Any) -> int:
            found.append(mon)
            return 1

        try:
            self.EnumDisplayMonitors(None, None, _MONITORENUMPROC(collect), 0)
        except Exception:
            log.debug("couldn't list the monitors", exc_info=True)
        return sorted({self.monitor_scale(m) for m in found}) or [1.0]


class OverlayWindow:
    """The real overlay (Windows). Thread-safe API; the window lives on its own thread."""

    def __init__(
        self,
        *,
        text_renderer: TextRenderer | None = gdi_text_mask,
        fps: int = FPS,
        prerender_frames: bool = True,
    ) -> None:
        self._text = text_renderer
        self._interval_ms = max(10, round(1000 / fps))
        self._prerender = prerender_frames
        self._lock = threading.Lock()
        self._state = "hidden"
        self._started = 0.0
        self._message = ""
        self._level = 0.0
        self._fresh = False  # the state changed and the window should re-place itself
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._timer = False
        self._visible = False
        self._place: tuple[int, int, float] | None = None  # top-centre anchor + scale
        self._dib: tuple[int, int, Any, Any, Any] | None = None  # w, h, hdc, bitmap, old
        self._bits: int = 0
        self._in_dib: Bitmap | None = None  # what the DIB holds now
        self._wndproc_ref: Any = None
        self._class_name = f"ShoutAboutOverlay.{id(self):x}"
        self._last_frame: tuple[Look, float, int, int, int] | None = None
        # Called (on the overlay thread, with time.perf_counter()) each time the pill
        # becomes visible: for latency measurements and tests.
        self.on_shown: Callable[[float], None] | None = None

    # -- thread-safe API

    def start(self) -> None:
        if self._thread is not None or sys.platform != "win32":
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        if self._prerender:
            threading.Thread(target=self._warm, name="overlay-warm", daemon=True).start()

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        hwnd = self._hwnd
        if hwnd:
            from shoutabout.inject import win32 as w

            w.user32.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
        if thread is not None and thread is not threading.current_thread():
            thread.join(3.0)

    def _set(self, state: str, text: str = "") -> None:
        with self._lock:
            self._state, self._message = state, short_text(text) if text else ""
            self._started = time.monotonic()
            self._fresh = True
            hwnd = self._hwnd
        if hwnd:
            from shoutabout.inject import win32 as w

            w.user32.PostMessageW(hwnd, WM_APP_WAKE, 0, 0)

    def listening(self) -> None:
        self._level = 0.0
        self._set("listening")

    def set_level(self, level: float) -> None:
        self._level = float(level)

    def thinking(self) -> None:
        self._set("thinking")

    def done(self) -> None:
        self._set("done")

    def error(self, text: str) -> None:
        self._set("error", text)

    def notice(self, text: str) -> None:
        self._set("notice", text)

    def cancelled(self) -> None:
        self._set("cancelled")

    def hide(self) -> None:
        self._set("hidden")

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    # -- warm-up (its own thread: rendering is slow-ish pure Python)

    def _warm(self) -> None:
        try:
            gdi = _Gdi.get()
            gdi.per_monitor_dpi()
            scales = gdi.monitor_scales()
            t0 = time.perf_counter()
            bitmap(first_listening_look(), scales[0], self._text)
            hwnd = self._hwnd
            if hwnd:  # put the first frame in the window's bitmap now
                from shoutabout.inject import win32 as w

                w.user32.PostMessageW(hwnd, WM_APP_PREPARE, 0, 0)
            n = prerender(scales, self._text)
            log.debug(
                "overlay: %d frames ready for scale(s) %s in %.0f ms",
                n,
                ", ".join(f"{s:g}" for s in scales),
                (time.perf_counter() - t0) * 1000,
            )
        except Exception:
            log.exception("couldn't prepare the overlay frames")

    # -- window thread

    def _run(self) -> None:
        from shoutabout.inject import win32 as w

        gdi = _Gdi.get()
        gdi.per_monitor_dpi()
        hinst = w.kernel32.GetModuleHandleW(None)
        self._wndproc_ref = w.WNDPROC(self._wndproc)
        wc = w.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        registered = bool(w.user32.RegisterClassExW(ctypes.byref(wc)))
        try:
            if not registered:
                log.warning("couldn't register the overlay window class (error %d)", w.last_error())
                return
            hwnd = w.user32.CreateWindowExW(
                OVERLAY_EX_STYLE,
                self._class_name,
                "ShoutAbout",
                WS_POPUP,
                0,
                0,
                0,
                0,
                None,
                None,
                hinst,
                None,
            )
            if not hwnd:
                log.warning("couldn't create the overlay window (error %d)", w.last_error())
                return
            with self._lock:
                self._hwnd = int(hwnd)
            self._ready.set()
            msg = w.MSG()
            while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                w.user32.TranslateMessage(ctypes.byref(msg))
                w.user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("overlay window failed")
        finally:
            self._free_dib()
            with self._lock:
                self._hwnd = None
            if registered:
                w.user32.UnregisterClassW(self._class_name, hinst)
            self._ready.set()

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        from shoutabout.inject import win32 as w

        try:
            if msg == WM_APP_WAKE or (msg == w.WM_TIMER and wparam == TIMER_ID):
                self._tick(hwnd)
                return 0
            if msg == WM_APP_PREPARE:
                self._prepare()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == w.WM_CLOSE:
                self._stop_timer(hwnd)
                w.user32.DestroyWindow(hwnd)
                return 0
            if msg == w.WM_DESTROY:
                w.user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("overlay window procedure failed (msg %#x)", msg)
        return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _stop_timer(self, hwnd: int) -> None:
        from shoutabout.inject import win32 as w

        if self._timer:
            w.user32.KillTimer(hwnd, TIMER_ID)
            self._timer = False

    def _prepare(self) -> None:
        """Load the first listening frame into the bitmap while the pill is hidden."""
        if self._visible:
            return
        anchor = self._anchor()
        self._place = anchor
        self._load(bitmap(first_listening_look(), anchor[2], self._text))

    def _tick(self, hwnd: int) -> None:
        from shoutabout.inject import win32 as w

        gdi = _Gdi.get()
        with self._lock:
            state, started, message, fresh = self._state, self._started, self._message, self._fresh
            self._fresh = False
        frame = frame_at(state, time.monotonic() - started, self._level, message)
        if frame.look is None or frame.finished:
            if self._visible:
                gdi.ShowWindow(hwnd, SW_HIDE)
                self._visible = False
            self._last_frame = None
            self._stop_timer(hwnd)
            return
        if fresh or self._place is None or not self._visible:
            self._place = self._anchor()
        x_mid, top, scale = self._place
        bmp = bitmap(frame.look, scale, self._text)
        x, y = x_mid - bmp.width // 2, top
        key = (frame.look, scale, frame.alpha, x, y)
        if key != self._last_frame:
            self._load(bmp)
            self._update(hwnd, bmp, x, y, frame.alpha)
            self._last_frame = key
        if not self._visible:
            gdi.SetWindowPos(
                hwnd,
                ctypes.c_void_p(HWND_TOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            gdi.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            self._visible = True
            if self.on_shown is not None:
                try:
                    self.on_shown(time.perf_counter())
                except Exception:
                    log.debug("on_shown failed", exc_info=True)
        if not self._timer:
            self._timer = bool(w.user32.SetTimer(hwnd, TIMER_ID, self._interval_ms, None))

    def _anchor(self) -> tuple[int, int, float]:
        """(x centre, canvas top, scale) on the foreground window's monitor."""
        from ctypes import wintypes as wt

        gdi = _Gdi.get()
        fg = gdi.GetForegroundWindow()
        mon = gdi.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST) if fg else None
        if not mon:
            mon = gdi.MonitorFromPoint(wt.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        work = (0, 0, 1920, 1080)
        if mon and gdi.GetMonitorInfoW(mon, ctypes.byref(info)):
            work = tuple(info.rcWork)  # type: ignore[assignment]
        scale = gdi.monitor_scale(mon)
        x, y = place(work, 0, 0, scale)
        return x, y, scale

    def _free_dib(self) -> None:
        if self._dib is None:
            return
        gdi = _Gdi.get()
        _w, _h, hdc, bmp, old = self._dib
        gdi.SelectObject(hdc, old)
        gdi.DeleteObject(bmp)
        gdi.DeleteDC(hdc)
        self._dib = None
        self._in_dib = None

    def _load(self, bmp: Bitmap) -> bool:
        """Copy a frame into the window's bitmap (made once per frame size)."""
        if self._in_dib is bmp:
            return True
        gdi = _Gdi.get()
        if self._dib is None or self._dib[:2] != (bmp.width, bmp.height):
            self._free_dib()
            hdc = gdi.CreateCompatibleDC(None)
            bits = ctypes.c_void_p()
            bmi = _bitmap_info(bmp.width, bmp.height)
            handle = gdi.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not handle or not bits.value:
                gdi.DeleteDC(hdc)
                log.warning("couldn't create the overlay bitmap")
                return False
            old = gdi.SelectObject(hdc, handle)
            self._dib = (bmp.width, bmp.height, hdc, handle, old)
            self._bits = bits.value
        ctypes.memmove(self._bits, bmp.bgra, len(bmp.bgra))
        self._in_dib = bmp
        return True

    def _update(self, hwnd: int, bmp: Bitmap, x: int, y: int, alpha: int) -> None:
        from ctypes import wintypes as wt

        if self._dib is None or self._in_dib is not bmp:
            return
        gdi = _Gdi.get()
        hdc = self._dib[2]
        screen = gdi.GetDC(None)
        try:
            blend = _BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, alpha)), AC_SRC_ALPHA)
            ok = gdi.UpdateLayeredWindow(
                hwnd,
                screen,
                ctypes.byref(wt.POINT(x, y)),
                ctypes.byref(_SIZE(bmp.width, bmp.height)),
                hdc,
                ctypes.byref(wt.POINT(0, 0)),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
            if not ok:
                log.debug("UpdateLayeredWindow failed (error %d)", ctypes.get_last_error())
        finally:
            gdi.ReleaseDC(None, screen)


def create_overlay(enabled: bool = True) -> OverlayLike:
    """The real window on Windows when enabled, else a :class:`NullOverlay`."""
    if enabled and sys.platform == "win32":
        return OverlayWindow()
    return NullOverlay()


# ------------------------------------------------------------------- screenshots


def state_frames(level: float = 0.7) -> dict[str, Frame]:
    """A representative frame of every state (docs and screenshots)."""
    return {
        "listening-quiet": frame_at("listening", 0.3, 0.05),
        "listening-loud": frame_at("listening", 0.9, level),
        "thinking": frame_at("thinking", 0.28),
        "done": frame_at("done", 0.2),  # half zipped
        "error": frame_at("error", 0.5, text="No mic"),
        "notice": frame_at("notice", 0.5, text="Getting ready… 42%"),
        "cancelled": frame_at("cancelled", 0.0),
    }


def write_state_pngs(
    out_dir: Path, scale: float = 1.5, text_renderer: TextRenderer | None = gdi_text_mask
) -> list[Path]:
    """Render every state to ``out_dir/overlay-<state>.png`` (transparent background) and
    the thinking loop and the zip ("done") to animated PNGs. Returns the paths."""
    out_dir = Path(out_dir)
    paths = []
    for name, frame in state_frames().items():
        assert frame.look is not None
        img = render(frame.look, scale, text_renderer)
        paths.append(
            brand.write_png(
                out_dir / f"overlay-{name}.png", img.width, img.height, img.rgba, alpha=True
            )
        )
    talk = [
        render(frame_at("thinking", i * 1.6 / TALK_STEPS).look, scale, text_renderer)
        for i in range(TALK_STEPS)
    ]  # type: ignore[arg-type]
    paths.append(
        brand.write_apng(out_dir / "overlay-thinking.apng.png", talk, round(1600 / TALK_STEPS))
    )
    zipping = [
        _faded(render(f.look, scale, text_renderer), f.alpha)
        for f in done_frames()
        if f.look is not None
    ]
    paths.append(brand.write_apng(out_dir / "overlay-done.apng.png", zipping, round(1000 / FPS)))
    return paths


def _faded(img: brand.Image, alpha: int) -> brand.Image:
    """``img`` with the window's opacity baked in (for animated PNGs)."""
    if alpha >= 255:
        return img
    px = np.frombuffer(img.rgba, dtype=np.uint8).reshape(-1, 4).copy()
    px[:, 3] = (px[:, 3].astype(np.uint16) * alpha + 127) // 255
    return brand.Image(img.width, img.height, px.tobytes())
