"""What the macOS build leaves out on top of packaging/bundle_rules.py's FORBIDDEN list.

The Windows list (UNNEEDED there) drops PortAudio's .dylib, which the Mac app needs, so the
Mac spec uses FORBIDDEN (licensing: ASIO PortAudio, PyAV/FFmpeg) plus this list instead.
``python packaging/macos/mac_rules.py check <ShoutAbout.app>`` fails if a forbidden or
Windows-only file got in anyway (build.sh runs it).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from bundle_rules import FORBIDDEN, _matches  # noqa: E402

# Not needed on a Mac (size): Windows DLLs and executables, CUDA, and the Windows/32-bit
# PortAudio builds that sounddevice's universal wheel may carry.
MAC_UNNEEDED = (
    "*.dll",
    "*.exe",
    "*.pyd",
    "nvidia/*",
    "_sounddevice_data/portaudio-binaries/*32bit*",
    "_sounddevice_data/portaudio-binaries/*64bit*",
    "*/.github/*",
)

# FFmpeg's libraries are named lib*.dylib on macOS (PyAV's wheel: av/.dylibs/), which the
# Windows-style patterns in FORBIDDEN (avcodec*) don't match by name.
MAC_FFMPEG = (
    "libavcodec*",
    "libavformat*",
    "libavutil*",
    "libavdevice*",
    "libavfilter*",
    "libswresample*",
    "libswscale*",
    "libpostproc*",
)
MAC_FORBIDDEN = FORBIDDEN + MAC_FFMPEG
MUST_NOT_SHIP = MAC_FORBIDDEN + ("*.dll", "*.exe")


def bad_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in Path(root).rglob("*")
        if p.is_file() and _matches(p.relative_to(root).as_posix(), MUST_NOT_SHIP)
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "check":
        print("usage: mac_rules.py check <ShoutAbout.app>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    bad = bad_files(root)
    for path in bad:
        print(f"MUST NOT SHIP: {path}")
    if bad:
        return 1
    print(f"OK: no PyAV/FFmpeg, ASIO or Windows files in {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
