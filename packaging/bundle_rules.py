"""Which collected files never go into the installed app, and the check that enforces it.

Used by packaging/shoutabout.spec (to drop the files) and by packaging/build.ps1
(``python packaging/bundle_rules.py check <onedir>``, which fails the build if a forbidden
file slipped through anyway). Patterns are matched, case-insensitively, against a file's
path inside the bundle (forward slashes) and against its bare name.
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path, PurePosixPath

# Must never ship. The build fails if one of these is in the output.
# * ASIO-enabled PortAudio builds (sounddevice's wheel): made with Steinberg's ASIO SDK,
#   whose license isn't permissive. The plain PortAudio DLL (MIT) is all ShoutAbout needs.
# * PyAV and its FFmpeg build (av.libs): linked with x264/x265 (GPL). The app decodes audio
#   with numpy (WAV) and Windows Media Foundation instead; faster-whisper's `import av` is
#   satisfied by shoutabout.avshim. PyAV's metadata goes too, so nothing claims it's there.
FFMPEG = (
    "av/*",
    "av.libs/*",
    "av-*.dist-info/*",
    "avcodec*",
    "avformat*",
    "avutil*",
    "avdevice*",
    "avfilter*",
    "swresample*",
    "swscale*",
    "postproc*",
    "*x264*",
    "*x265*",
)
FORBIDDEN = ("*asio*", *FFMPEG)

# Not needed, so not shipped (size), on top of FORBIDDEN.
UNNEEDED = (
    "cudnn64_9.dll",  # CTranslate2's wheel carries it; its Windows build doesn't use cuDNN
    "nvblas64_12.dll",  # a BLAS shim nothing loads
    "nvrtc64_120_0.alt.dll",  # the alternative NVRTC build; cuBLASLt loads nvrtc64_120_0.dll
    # sounddevice's wheel has PortAudio for every platform; 64-bit Windows uses
    # libportaudio64bit.dll only.
    "_sounddevice_data/portaudio-binaries/*32bit*",
    "_sounddevice_data/portaudio-binaries/*arm64*",
    "_sounddevice_data/portaudio-binaries/*.dylib",
    "*/.github/*",
)

DROP = FORBIDDEN + UNNEEDED


def _matches(dest: str, patterns: tuple[str, ...]) -> bool:
    path = str(dest).replace("\\", "/").lower()
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def is_dropped(dest: str) -> bool:
    """True if a file at ``dest`` (its path inside the bundle) must be left out."""
    return _matches(dest, DROP)


def is_forbidden(dest: str) -> bool:
    return _matches(dest, FORBIDDEN)


def forbidden_files(root: Path) -> list[Path]:
    """Every file under ``root`` that must never ship."""
    return sorted(
        p
        for p in Path(root).rglob("*")
        if p.is_file() and is_forbidden(p.relative_to(root).as_posix())
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "check":
        print("usage: bundle_rules.py check <folder>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    bad = forbidden_files(root)
    for path in bad:
        print(f"FORBIDDEN in the bundle: {path}")
    if bad:
        return 1
    print(f"OK: no forbidden files ({', '.join(FORBIDDEN)}) in {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
