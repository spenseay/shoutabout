"""Render the installer's brand assets from shoutabout.brand (pure Python, no image tools).

    .venv\\Scripts\\python.exe packaging\\make_assets.py [--out DIR]

Writes into DIR (default: packaging\\build\\assets, which git ignores):

  ShoutAbout.ico            app icon, 16-256 px (exe icon, Start menu, Apps list, windows)
  wizard-large-<W>x<H>.bmp  the installer's welcome/finish panel, one per DPI step
  wizard-small-<N>.bmp      the square mark in the installer's page header, per DPI step

Small sizes (<= 48 px) use the tray renderer, whose edges snap to whole pixels; larger
ones use the app-icon artwork. Wizard images are 32-bit BMPs with straight alpha
(Inno Setup: WizardImageAlphaFormat=defined), which every Inno Setup 6 release reads.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
try:
    from shoutabout import brand
except ImportError:  # running without the package installed
    sys.path.insert(0, str(REPO / "src"))
    from shoutabout import brand

DEFAULT_OUT = HERE / "build" / "assets"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SNAPPED_MAX = 48
# Inno Setup picks the file closest to its image area at the current DPI. The area is
# 164:314 (pre-6.6 sizes 164x314 ... 416x797; 6.6+ sizes 202x386 ... 534x1022).
WIZARD_WIDTHS = (164, 202, 240, 269, 336, 403, 430, 534)
WIZARD_RATIO = 314 / 164
# Square header image: pre-6.6 58 ... 147 px, 6.6+ 58 ... 159 px.
SMALL_SIZES = (58, 71, 77, 85, 97, 112, 124, 147, 159)
MOUTH_WIDTH = 0.70  # mouth width on the wizard panel, fraction of the panel width
MOUTH_CENTRE_Y = 0.40  # mouth centre, fraction of the panel height
BAND_HEIGHT = 0.035  # black band at the bottom of the panel, fraction of the height


def icon_image(size: int) -> brand.Image:
    """The app icon at ``size`` px: the open mouth with its tongue on a rounded yellow tile."""
    if size <= SNAPPED_MAX:
        return brand.tray_icon("transcribing", size)  # frame 0: wide open, pixel-snapped
    return brand.app_icon("primary", size, rounded=True)


def ico_bytes(sizes: tuple[int, ...] = ICON_SIZES) -> bytes:
    return brand.ico_bytes([icon_image(s) for s in sizes])


def wizard_image(width: int) -> brand.Image:
    """The tall welcome-page panel: Shout Yellow, the talking mouth, an ink band below."""
    height = round(width * WIZARD_RATIO)
    w, h = float(width), float(height)
    band = max(2.0, round(h * BAND_HEIGHT))
    layers = [
        brand.Layer(brand.Rect(0, 0, w, h), brand.YELLOW),
        brand.Layer(brand.Rect(0, h - band, w, h), brand.BLACK),
        *brand.centred_mouth_layers(w / 2, h * MOUTH_CENTRE_Y, MOUTH_WIDTH * w),
    ]
    return brand.rasterize(width, height, layers)


def bmp_bytes(image: brand.Image) -> bytes:
    """A 32-bit bottom-up BMP (BGRA, straight alpha) of a brand image."""
    w, h = image.width, image.height
    bgra = bytearray(image.rgba)
    bgra[0::4], bgra[2::4] = image.rgba[2::4], image.rgba[0::4]
    stride = w * 4
    pixels = b"".join(bytes(bgra[y * stride : (y + 1) * stride]) for y in reversed(range(h)))
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, len(pixels), 2835, 2835, 0, 0)
    offset = 14 + len(info)
    head = struct.pack("<2sIHHI", b"BM", offset + len(pixels), 0, 0, offset)
    return head + info + pixels


def write_assets(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def put(name: str, data: bytes) -> None:
        path = out / name
        path.write_bytes(data)
        written.append(path)

    put("ShoutAbout.ico", ico_bytes())
    for width in WIZARD_WIDTHS:
        img = wizard_image(width)
        put(f"wizard-large-{img.width}x{img.height}.bmp", bmp_bytes(img))
    for size in SMALL_SIZES:
        put(f"wizard-small-{size}.bmp", bmp_bytes(icon_image(size)))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    args = parser.parse_args(argv)
    t0 = time.perf_counter()
    written = write_assets(args.out)
    for path in written:
        print(f"  {path.name:28} {path.stat().st_size:>9,} bytes")
    print(f"{len(written)} files in {args.out} ({time.perf_counter() - t0:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
