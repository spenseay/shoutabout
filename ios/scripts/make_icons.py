#!/usr/bin/env python3
"""Render the ShoutAbout app icons as 1024x1024 PNGs with the standard library only.

No Pillow: shapes are evaluated analytically per pixel (anti-aliased with an approximate
signed distance) and written with zlib + struct. The geometry follows the brand sheet's
"open mouth" mark (see docs/ios-app.md, "Branding"):

  outline  top half-ellipse   rx = W/2, ry = 0.42H, centre (W/2, 0.42H)
           bottom half-ellipse rx = W/2, ry = 0.58H, same centre      -> throat (black)
  teeth    band from the top edge down to 0.20H, clipped to the outline (white)
  tongue   ellipse centre (0.50W, 0.93H), rx = 0.22W, ry = 0.31H, clipped (red)
  OPEN     H = 130/170 W

Variants written into the asset catalog:
  AppIcon.appiconset/AppIcon.png          Primary: yellow tile, mouth 78% of the tile, centred
  AppIcon.appiconset/AppIcon-Dark.png     Dark: the primary icon too (the app uses only the
                                          plain mark on yellow, never the yellow lip ring)
  AppIcon.appiconset/AppIcon-Tinted.png   Tinted: greyscale of the primary icon (iOS tints it)
  AppIcon-Alt.appiconset/AppIcon-Alt.png  Alt: red tile, black mouth, yellow tongue

iOS masks the corners itself, so the artwork is full-bleed and fully opaque (App Store
icons must not have an alpha channel, so the PNGs are 8-bit RGB).

Usage:
  python ios/scripts/make_icons.py [--size 1024]
      [--out ios/ShoutAbout/Resources/Assets.xcassets]
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

YELLOW = (0xFF, 0xE6, 0x00)
BLACK = (0x0A, 0x0A, 0x0A)
RED = (0xFF, 0x1A, 0x1A)
WHITE = (0xFF, 0xFF, 0xFF)

OPEN_RATIO = 130.0 / 170.0  # mouth height / width when OPEN


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


class Mouth:
    """The mouth mark inside a W x H box whose top-left corner is (x0, y0), in pixels."""

    def __init__(self, cx: float, cy: float, width: float, grow: float = 0.0) -> None:
        self.w = width
        self.h = width * OPEN_RATIO
        self.x0 = cx - width / 2.0
        self.y0 = cy - self.h / 2.0
        self.cx = cx
        self.cy = self.y0 + 0.42 * self.h  # where the two half-ellipses meet
        # `grow` offsets every radius (used for the lip ring), keeping the same centre.
        self.rx = width / 2.0 + grow
        self.ry_top = 0.42 * self.h + grow
        self.ry_bot = 0.58 * self.h + grow
        self.top = self.cy - self.ry_top
        self.bottom = self.cy + self.ry_bot

    def coverage(self, x: float, y: float) -> float:
        """Anti-aliased coverage (0..1) of the outline at pixel centre (x, y)."""
        ry = self.ry_top if y < self.cy else self.ry_bot
        return _ellipse_coverage(x, y, self.cx, self.cy, self.rx, ry)

    def teeth_coverage(self, x: float, y: float) -> float:
        edge = self.y0 + 0.20 * self.h
        band = _clamp01(0.5 - (y - edge))
        if band <= 0.0:
            return 0.0
        return band * self.coverage(x, y)

    def tongue_coverage(self, x: float, y: float) -> float:
        t = _ellipse_coverage(x, y, self.cx, self.y0 + 0.93 * self.h, 0.22 * self.w, 0.31 * self.h)
        if t <= 0.0:
            return 0.0
        return t * self.coverage(x, y)


def _ellipse_coverage(x: float, y: float, cx: float, cy: float, rx: float, ry: float) -> float:
    dx = (x - cx) / rx
    dy = (y - cy) / ry
    f = dx * dx + dy * dy - 1.0
    gx = 2.0 * dx / rx
    gy = 2.0 * dy / ry
    g = math.hypot(gx, gy)
    if g < 1e-9:
        return 1.0 if f < 0 else 0.0
    dist = f / g  # approximate signed distance in pixels (negative inside)
    return _clamp01(0.5 - dist)


def _mix(base: tuple[float, float, float], top: tuple[int, int, int], a: float):
    if a <= 0.0:
        return base
    if a >= 1.0:
        return (float(top[0]), float(top[1]), float(top[2]))
    return (
        base[0] + (top[0] - base[0]) * a,
        base[1] + (top[1] - base[1]) * a,
        base[2] + (top[2] - base[2]) * a,
    )


def render(
    size: int,
    tile: tuple[int, int, int],
    mouth_frac: float,
    throat: tuple[int, int, int] = BLACK,
    teeth: tuple[int, int, int] = WHITE,
    tongue: tuple[int, int, int] = RED,
    lip: tuple[int, int, int] | None = None,
    lip_frac: float = 0.035,
) -> list[bytearray]:
    centre = size / 2.0
    mouth = Mouth(centre, centre, size * mouth_frac)
    ring = Mouth(centre, centre, size * mouth_frac, grow=size * lip_frac) if lip else None
    outer = ring or mouth
    y_lo = int(math.floor(outer.top)) - 2
    y_hi = int(math.ceil(outer.bottom)) + 2
    x_lo = int(math.floor(outer.cx - outer.rx)) - 2
    x_hi = int(math.ceil(outer.cx + outer.rx)) + 2
    bg = bytes(tile) * size
    rows: list[bytearray] = []
    for py in range(size):
        row = bytearray(bg)
        if y_lo <= py <= y_hi:
            y = py + 0.5
            for px in range(max(0, x_lo), min(size, x_hi + 1)):
                x = px + 0.5
                c: tuple[float, float, float] = (float(tile[0]), float(tile[1]), float(tile[2]))
                if ring is not None and lip is not None:
                    c = _mix(c, lip, ring.coverage(x, y))
                cov = mouth.coverage(x, y)
                if cov <= 0.0:
                    if ring is None:
                        continue
                else:
                    c = _mix(c, throat, cov)
                    c = _mix(c, teeth, mouth.teeth_coverage(x, y))
                    c = _mix(c, tongue, mouth.tongue_coverage(x, y))
                i = px * 3
                row[i] = int(round(c[0]))
                row[i + 1] = int(round(c[1]))
                row[i + 2] = int(round(c[2]))
        rows.append(row)
    return rows


def greyscale(rows: list[bytearray]) -> list[bytearray]:
    out = []
    for row in rows:
        g = bytearray(len(row))
        for i in range(0, len(row), 3):
            v = int(round(0.299 * row[i] + 0.587 * row[i + 1] + 0.114 * row[i + 2]))
            g[i] = g[i + 1] = g[i + 2] = v
        out.append(g)
    return out


def write_png(path: Path, rows: list[bytearray]) -> None:
    height = len(rows)
    width = len(rows[0]) // 3
    raw = b"".join(b"\x00" + bytes(r) for r in rows)  # filter type 0 per scanline

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + body + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB, no alpha
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    print(f"wrote {path} ({len(png)} bytes)")


def main() -> None:
    here = Path(__file__).resolve().parent
    default_out = here.parent / "ShoutAbout" / "Resources" / "Assets.xcassets"
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--out", type=Path, default=default_out)
    args = ap.parse_args()
    size: int = args.size
    out: Path = args.out

    primary = render(size, YELLOW, 0.78)
    alt = render(size, RED, 0.78, tongue=YELLOW)
    write_png(out / "AppIcon.appiconset" / "AppIcon.png", primary)
    write_png(out / "AppIcon.appiconset" / "AppIcon-Dark.png", primary)
    write_png(out / "AppIcon.appiconset" / "AppIcon-Tinted.png", greyscale(primary))
    write_png(out / "AppIcon-Alt.appiconset" / "AppIcon-Alt.png", alt)


if __name__ == "__main__":
    main()
