"""Build the homepage's binary assets (site/): fonts, icons, the social image, screenshots.

The website is plain HTML/CSS with no build step. This script is only re-run when an asset
changes. It needs Pillow, fontTools and Brotli, which are NOT app dependencies: use a
separate tool venv, for example

    uv venv .tmp\\sitetools
    uv pip install --python .tmp\\sitetools\\Scripts\\python.exe pillow fonttools brotli
    .tmp\\sitetools\\Scripts\\python.exe scripts\\site\\make_assets.py ^
        --ios-shots <folder with the iOS screenshot tour PNGs> ^
        --pc-shots <folder with scripts\\capture_ui.py output>

Without --ios-shots / --pc-shots the screenshots already in site/ are kept, and so is any
screenshot whose source file isn't in the folder (``capture_ui.py OUT overlay-top`` alone
is enough to refresh the pill).

What it writes (all generated, don't edit by hand):
  site/assets/fonts/*.woff2       Anton + Archivo, Latin subset, WOFF2 (SIL OFL 1.1: the
                                  fonts declare no Reserved Font Name, so subsetting is OK)
  site/assets/fonts/OFL-*.txt     their licence texts, copied unchanged
  site/favicon.ico, site/favicon-32.png, site/apple-touch-icon.png, site/icon-512.png
  site/assets/img/og.png          1200 x 630 social share image: the wordmark
  site/assets/img/shots/*.webp    screenshots (sample data only), resized and compressed

Only screenshots made with made-up data belong here (the iOS Debug screenshot scenes,
scripts/capture_ui.py). Never the Teach window: it lists real project folder names.

The owner's rules for every mouth in the marketing: only the primary mark (black mouth,
white teeth band, red tongue), never without its tongue and never with a ring or outline
around it (on black it sits on a yellow tile, like the app icon). So the lists below take
only the onboarding screen (its wordmark), the PC setup window and the pill while
listening. The iPhone app's other screens are left out: their big record button and the
Dynamic Island show the mouth with a yellow ring, and the idle and pairing screens show
it ajar. The site draws the iPhone in CSS instead.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from fontTools import subset
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
SITE = REPO / "site"
FONT_SRC = REPO / "ios" / "ShoutAbout" / "Resources" / "Fonts"
sys.path.insert(0, str(REPO / "src"))
from shoutabout import brand  # noqa: E402  (stdlib-only renderer, imported from src)

# Google Fonts' "latin" range: ASCII, Latin-1, typographic punctuation, euro, arrows.
LATIN = (
    "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,"
    "U+0329,U+2000-206F,U+2074,U+20AC,U+2122,U+2190-2193,U+2212,U+2215,U+FEFF,U+FFFD"
)
FONTS = {
    "anton.woff2": "Anton-Regular.ttf",
    "archivo-500.woff2": "Archivo-Medium.ttf",
    "archivo-700.woff2": "Archivo-Bold.ttf",
}

# (source file, output name). iPhone shots are full screens, resized.
IOS_SHOTS = [
    ("light-01-onboarding-choice.png", "iphone-onboarding"),
]
# (source file, output name, kind). kind: "window" = a PC window, kept at full size;
# "alpha" = a transparent image (the pill, from overlay.write_state_pngs at 4x).
PC_SHOTS = [
    ("setup-1-downloading.png", "pc-setup-ready", "window"),
    ("setup-2-try.png", "pc-setup-try", "window"),
    ("setup-4-phone.png", "pc-setup-phone", "window"),
    ("approval-prompt.png", "pc-approval", "window"),
    ("overlay-listening-loud.png", "pc-pill-listening", "alpha"),
]
PHONE_WIDTH = 600  # shown at up to ~300 CSS px, so 2x


def to_pil(img: brand.Image) -> Image.Image:
    return Image.frombytes("RGBA", (img.width, img.height), img.rgba)


def build_fonts() -> None:
    out = SITE / "assets" / "fonts"
    out.mkdir(parents=True, exist_ok=True)
    for name, source in FONTS.items():
        options = subset.Options()
        options.flavor = "woff2"
        options.layout_features = ["*"]
        options.name_IDs = ["*"]  # keep the copyright / licence name records
        options.notdef_outline = True
        font = subset.load_font(str(FONT_SRC / source), options)
        subsetter = subset.Subsetter(options)
        subsetter.populate(unicodes=subset.parse_unicodes(LATIN))
        subsetter.subset(font)
        subset.save_font(font, str(out / name), options)
        print(f"  {name}: {(out / name).stat().st_size // 1024} KB")
    for lic in ("OFL-Anton.txt", "OFL-Archivo.txt"):
        shutil.copyfile(FONT_SRC / lic, out / lic)


def build_icons() -> None:
    sizes = (16, 32, 48)
    tiles = [to_pil(brand.tray_icon("transcribing", s)) for s in sizes]
    tiles[-1].save(SITE / "favicon.ico", sizes=[(s, s) for s in sizes], append_images=tiles)
    tiles[1].save(SITE / "favicon-32.png", optimize=True)
    # iOS home-screen icon: full bleed and opaque (iOS rounds the corners itself).
    to_pil(brand.app_icon("primary", 180)).convert("RGB").save(
        SITE / "apple-touch-icon.png", optimize=True
    )
    to_pil(brand.app_icon("primary", 512, rounded=True)).save(SITE / "icon-512.png", optimize=True)


def wordmark(
    draw: ImageDraw.ImageDraw, img: Image.Image, center_x: int, top: int, size: int
) -> int:
    """SH[mouth]UT over ABOUT, centred, like the site's hero and the app's LockupView:
    Anton, the mouth 0.78 em wide and open (tongue out), rows 0.06 em apart. Returns the
    bottom edge."""
    font = ImageFont.truetype(str(FONT_SRC / "Anton-Regular.ttf"), size)
    cap = round(size * 0.86)  # Anton's capitals are 0.86 em tall
    mouth = to_pil(brand.mouth_image(round(size * 0.78)))
    gap = round(size * 0.02)
    sh, ut = font.getlength("SH"), font.getlength("UT")
    row1 = sh + gap + mouth.width + gap + ut
    x = center_x - row1 / 2
    baseline = top + cap
    draw.text((x, baseline), "SH", font=font, fill=brand.BLACK, anchor="ls")
    mx = round(x + sh + gap)
    img.paste(mouth, (mx, round(top + (cap - mouth.height) / 2)), mouth)
    draw.text((mx + mouth.width + gap, baseline), "UT", font=font, fill=brand.BLACK, anchor="ls")
    baseline2 = baseline + round(size * 0.06) + cap
    draw.text((center_x, baseline2), "ABOUT", font=font, fill=brand.BLACK, anchor="ms")
    return baseline2


def build_og() -> None:
    w, h = 1200, 630
    img = Image.new("RGB", (w, h), brand.YELLOW)
    draw = ImageDraw.Draw(img)
    bottom = wordmark(draw, img, w // 2, 70, 214)
    body = ImageFont.truetype(str(FONT_SRC / "Archivo-Bold.ttf"), 38)
    draw.text(
        (w // 2, bottom + 92),
        "Private voice dictation. Free.",
        font=body,
        fill=brand.BLACK,
        anchor="ms",
    )
    out = SITE / "assets" / "img"
    out.mkdir(parents=True, exist_ok=True)
    img.save(out / "og.png", optimize=True)


def save_webp(im: Image.Image, path: Path, *, lossless: bool = False) -> None:
    if lossless:
        im.save(path, "WEBP", lossless=True, method=6)
    else:
        im.save(path, "WEBP", quality=82, method=6)
    print(f"  {path.name}: {im.width}x{im.height}, {path.stat().st_size // 1024} KB")


def build_shots(ios_dir: Path | None, pc_dir: Path | None) -> None:
    out = SITE / "assets" / "img" / "shots"
    out.mkdir(parents=True, exist_ok=True)
    if ios_dir is not None:
        for src, name in IOS_SHOTS:
            if not (ios_dir / src).is_file():
                print(f"  {name}: kept ({src} not in {ios_dir})")
                continue
            im = Image.open(ios_dir / src).convert("RGB")
            h = round(im.height * PHONE_WIDTH / im.width)
            small = im.resize((PHONE_WIDTH, h), Image.Resampling.LANCZOS)
            save_webp(small, out / f"{name}.webp")
    if pc_dir is not None:
        for src, name, kind in PC_SHOTS:
            if not (pc_dir / src).is_file():
                print(f"  {name}: kept ({src} not in {pc_dir})")
                continue
            im = Image.open(pc_dir / src)
            im = im.convert("RGBA") if kind == "alpha" else im.convert("RGB")
            save_webp(im, out / f"{name}.webp", lossless=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ios-shots", type=Path, help="folder with the iOS screenshot tour")
    parser.add_argument("--pc-shots", type=Path, help="folder with capture_ui.py screenshots")
    args = parser.parse_args()
    print("fonts")
    build_fonts()
    print("icons")
    build_icons()
    print("social image")
    build_og()
    if args.ios_shots or args.pc_shots:
        print("screenshots")
        build_shots(args.ios_shots, args.pc_shots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
