"""Regenerate the brand PNGs in brand/ from shoutabout.brand (pure Python, a few seconds).

    .venv\\Scripts\\python.exe scripts\\export_brand_assets.py [--out DIR]

Writes:
  app-icon/{primary,dark,alt,bleed}-{1024,180}.png   full-bleed, opaque (iOS masks corners)
  tray/{starting,idle,transcribing,paused,error}-{16,32,64}.png
  tray/states-on-taskbars.png                        all states at 32 px, dark + light
  talk/tray-talk-64.png, talk/tray-hum-64.png        animated PNGs of the tray loops
  talk/tray-talk-strip-64.png                        the 12 talk frames side by side
  talk/app-icon-talk-256.png                         animated: saTalk + saPunch
  mouth-512.png                                      the mark alone, on transparency
  palette.png                                        swatches, in brand.PALETTE order

The committed files must match the renderer (tests/test_brand.py checks the tray PNGs):
re-run this after changing shoutabout/brand.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
try:
    from shoutabout import brand
except ImportError:  # running without the package installed
    sys.path.insert(0, str(REPO / "src"))
    from shoutabout import brand

APP_ICON_SIZES = (1024, 180)
TRAY_SIZES = (16, 32, 64)
APP_TALK_FRAMES = 16  # 100 ms each: one 1.6 s loop


def taskbar_preview(size: int = 32, pad: int = 16) -> brand.Image:
    """The five states on a dark and a light taskbar strip, like the brand sheet."""
    states = brand.TRAY_STATES
    row_w = pad + len(states) * (size + pad)
    row_h = size + 2 * pad
    placements: list[tuple[brand.Image, int, int]] = []
    for r, colour in enumerate((brand.TASKBAR_DARK, brand.TASKBAR_LIGHT)):
        solid = brand.Image(row_w, row_h, bytes((*colour, 255)) * (row_w * row_h))
        placements.append((solid, 0, r * row_h))
        for i, state in enumerate(states):
            icon = brand.tray_frames(state, size)[0]
            placements.append((icon, pad + i * (size + pad), r * row_h + pad))
    return brand.compose(row_w, row_h * 2, None, placements)


def palette_swatches(w: int = 96, h: int = 64, gap: int = 12, border: float = 2.0) -> brand.Image:
    """One rounded swatch per palette colour (outlined, so white shows on white)."""
    layers: list[brand.Layer] = []
    for i, colour in enumerate(brand.PALETTE.values()):
        x = i * (w + gap)
        outer = brand.RoundRect(x, 0, x + w, h, 12)
        inner = brand.RoundRect(x + border, border, x + w - border, h - border, 12 - border)
        layers += [brand.Layer(outer, brand.BLACK), brand.Layer(inner, colour)]
    count = len(brand.PALETTE)
    return brand.rasterize(count * w + (count - 1) * gap, h, layers)


def export(out: Path) -> list[Path]:
    written: list[Path] = []

    def png(rel: str, img: brand.Image) -> None:
        written.append(brand.write_png(out / rel, img.width, img.height, img.rgba))

    def apng(rel: str, frames: list[brand.Image] | tuple[brand.Image, ...], delay: int) -> None:
        written.append(brand.write_apng(out / rel, frames, delay))

    for variant in brand.APP_ICON_VARIANTS:
        for size in APP_ICON_SIZES:
            png(f"app-icon/{variant}-{size}.png", brand.app_icon(variant, size))

    for state in brand.TRAY_STATES:
        for size in TRAY_SIZES:
            png(f"tray/{state}-{size}.png", brand.tray_frames(state, size)[0])
    png("tray/states-on-taskbars.png", taskbar_preview())

    talk = brand.tray_frames("transcribing", 64)
    apng("talk/tray-talk-64.png", talk, brand.frame_interval_ms("transcribing"))
    png("talk/tray-talk-strip-64.png", brand.strip(talk, gap=8))
    apng(
        "talk/tray-hum-64.png",
        brand.tray_frames("starting", 64),
        brand.frame_interval_ms("starting"),
    )
    app_talk = brand.talking_app_icon_frames("primary", 256, APP_TALK_FRAMES)
    apng("talk/app-icon-talk-256.png", app_talk, brand.TALK_DURATION_MS // APP_TALK_FRAMES)

    png("mouth-512.png", brand.mouth_image(512))
    png("palette.png", palette_swatches())
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "brand", help="output folder")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    written = export(args.out)
    total = 0
    for path in written:
        size = path.stat().st_size
        total += size
        print(f"{size:>9,}  {path.relative_to(args.out).as_posix()}")
    elapsed = time.perf_counter() - started
    print(f"{total:>9,}  total, {len(written)} files in {args.out} ({elapsed:.1f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
