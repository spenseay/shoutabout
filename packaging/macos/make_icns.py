"""Render the macOS app icon (ShoutAbout.icns) from shoutabout.brand. Pure Python + numpy.

    python packaging/macos/make_icns.py [--out packaging/build/assets/ShoutAbout.icns]

The artwork is the brand's primary app icon (Shout Yellow, the mouth) as a rounded tile on
Apple's icon grid: the tile is 824 of 1024 px, centred, corner radius 22.5 % (the brand's
APP_RADIUS matches Apple's). The .icns holds PNGs for 16-1024 px, including the @2x
variants, in the modern ``ic07``-``ic14``/``icp4``-``icp6`` slots that macOS 10.7+ reads.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
try:
    from shoutabout import brand
except ImportError:  # running without the package installed
    sys.path.insert(0, str(REPO / "src"))
    from shoutabout import brand

DEFAULT_OUT = REPO / "packaging" / "build" / "assets" / "ShoutAbout.icns"
TILE_RATIO = 824 / 1024  # Apple's macOS icon grid (Big Sur and later)

# icns slot -> pixel size (PNG payloads)
SLOTS: tuple[tuple[bytes, int], ...] = (
    (b"icp4", 16),
    (b"icp5", 32),
    (b"icp6", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
    (b"ic11", 32),  # 16@2x
    (b"ic12", 64),  # 32@2x
    (b"ic13", 256),  # 128@2x
    (b"ic14", 512),  # 256@2x
)


def icon_image(size: int) -> brand.Image:
    """The rounded tile centred on a transparent ``size`` x ``size`` canvas."""
    import numpy as np

    tile = max(1, round(size * TILE_RATIO))
    samples = 4 if tile > 256 else 8
    art = brand.app_icon("primary", tile, rounded=True, samples=samples)
    canvas = np.zeros((size, size, 4), dtype=np.uint8)
    off = (size - tile) // 2
    canvas[off : off + tile, off : off + tile] = np.frombuffer(art.rgba, dtype=np.uint8).reshape(
        tile, tile, 4
    )
    return brand.Image(size, size, canvas.tobytes())


def png_for(size: int, cache: dict[int, bytes]) -> bytes:
    if size not in cache:
        img = icon_image(size)
        cache[size] = brand.png_bytes(img.width, img.height, img.rgba, alpha=True)
    return cache[size]


def icns_bytes(entries: list[tuple[bytes, bytes]]) -> bytes:
    """An .icns file: 'icns' + total length, then (type, length incl. header, data)."""
    body = b"".join(kind + struct.pack(">I", len(data) + 8) + data for kind, data in entries)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def build(out: Path, slots: tuple[tuple[bytes, int], ...] = SLOTS) -> Path:
    cache: dict[int, bytes] = {}
    entries = [(kind, png_for(size, cache)) for kind, size in slots]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(icns_bytes(entries))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    path = build(args.out)
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
