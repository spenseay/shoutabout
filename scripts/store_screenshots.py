"""App Store screenshots: each simulator shot under an Anton caption on the brand yellow, at the
6.9-inch size Apple requires (1320 x 2868, RGB, no alpha).

Usage: python scripts/store_screenshots.py <shots dir> ios/ShoutAbout/Resources/Fonts <out dir>
(the shots: the ios-screenshots artifact of the iOS workflow run with store_size=true;
docs/launch/store-listing.md lists the set and the owner's mouth rule).
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1320, 2868
YELLOW = (0xFF, 0xE6, 0x00)
BLACK = (0x0A, 0x0A, 0x0A)

SHOTS = [
    ("light-01-onboarding-choice.png", ["PRIVATE VOICE", "DICTATION."]),
    ("dark-07-recording.png", ["PRESS. TALK.", "PRESS."]),
    ("keyboard-31-typing-light.png", ["A KEYBOARD", "THAT LISTENS."]),
    ("keyboard-33-listening-light.png", ["TAP THE MOUTH.", "TALK."]),
]

shots_dir, fonts_dir, out_dir = (Path(a) for a in sys.argv[1:4])
out_dir.mkdir(parents=True, exist_ok=True)
font = ImageFont.truetype(str(fonts_dir / "Anton-Regular.ttf"), 168)

SHOT_W = 1010
SHOT_H = round(SHOT_W * H / W)  # the simulator shot's own 1320:2868 shape
SHOT_Y = H - SHOT_H - 110
RADIUS = 64
STROKE = 7

for index, (name, lines) in enumerate(SHOTS, start=1):
    canvas = Image.new("RGB", (W, H), YELLOW)
    draw = ImageDraw.Draw(canvas)

    # Caption: Anton, upper case, line height ~0.86, centred in the space above the phone.
    line_height = round(168 * 0.98)
    block = line_height * len(lines)
    top = (SHOT_Y - block) // 2 - 10
    for row, text in enumerate(lines):
        width = draw.textlength(text, font=font)
        draw.text(((W - width) / 2, top + row * line_height), text, font=font, fill=BLACK)

    # The screen, with rounded corners and a black outline (like the brand sheet's tiles).
    shot = Image.open(shots_dir / name).convert("RGB").resize((SHOT_W, SHOT_H), Image.LANCZOS)
    mask = Image.new("L", (SHOT_W, SHOT_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SHOT_W - 1, SHOT_H - 1), radius=RADIUS, fill=255)
    x = (W - SHOT_W) // 2
    draw.rounded_rectangle(
        (x - STROKE, SHOT_Y - STROKE, x + SHOT_W + STROKE - 1, SHOT_Y + SHOT_H + STROKE - 1),
        radius=RADIUS + STROKE,
        fill=BLACK,
    )
    canvas.paste(shot, (x, SHOT_Y), mask)

    out = out_dir / f"store-{index}-{Path(name).stem}.png"
    canvas.save(out, optimize=True)
    print(out.name, canvas.size, canvas.mode)
