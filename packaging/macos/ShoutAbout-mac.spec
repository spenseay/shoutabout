# PyInstaller spec for ShoutAbout.app (macOS, Apple Silicon): a windowed menu bar app.
#
# Build with packaging/macos/build.sh (it renders the icon first, signs and installs after):
#   python packaging/macos/make_icns.py
#   pyinstaller packaging/macos/ShoutAbout-mac.spec --noconfirm --clean \
#     --distpath packaging/dist-mac --workpath packaging/build/pyinstaller-mac
#
# Output: packaging/dist-mac/ShoutAbout.app (Contents/MacOS/ShoutAbout + the Python runtime,
# the libraries, Tcl/Tk, PortAudio, the brand icon).
#
# Collected exactly like the Windows spec (packaging/shoutabout.spec): every shoutabout
# module and data file, and for every runtime dependency that applies to macOS (read from
# the installed metadata; PyObjC comes in through its sys_platform marker) its metadata,
# data files and dynamic libraries. Never PyAV/FFmpeg (GPL codecs; audio is decoded by
# macOS's afconvert), never ASIO PortAudio builds (packaging/bundle_rules.py FORBIDDEN),
# never Windows DLLs or CUDA.
# pyright: reportUndefinedVariable=false
# ruff: noqa: F821  (Analysis, PYZ, EXE, COLLECT, BUNDLE and SPECPATH come from PyInstaller)

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

HERE = Path(SPECPATH).resolve()
PACKAGING = HERE.parent
ROOT = PACKAGING.parent
for p in (ROOT / "src", PACKAGING, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bundle_rules import _matches  # noqa: E402  (packaging/bundle_rules.py)
from mac_rules import MAC_FORBIDDEN, MAC_UNNEEDED  # noqa: E402  (packaging/macos/mac_rules.py)
from notices import write_notices  # noqa: E402  (packaging/notices.py)

from shoutabout import __version__  # noqa: E402
from shoutabout.mac import BUNDLE_ID  # noqa: E402
from shoutabout.selftest import import_names, runtime_requirements  # noqa: E402

APP = "ShoutAbout"
ASSETS = Path(os.environ.get("SHOUTABOUT_ASSETS", PACKAGING / "build" / "assets")).resolve()
ICNS = ASSETS / "ShoutAbout.icns"
ICO = ASSETS / "ShoutAbout.ico"
NOTICES = ASSETS / "THIRD-PARTY-NOTICES.txt"
if not ICNS.is_file():
    raise SystemExit(f"{ICNS} is missing: run packaging/macos/make_icns.py first")

DATA_EXCLUDES = [
    "**/tests/**",
    "**/test/**",
    "**/__pycache__/**",
    "**/*.pyi",
    "**/*.pxd",
    "**/*.pyx",
    "**/*.h",
    "**/*.hpp",
    "**/*.c",
    "**/*.cpp",
    "**/*.lib",
    "**/*.a",
    "**/*.pc",
    "**/*.cmake",
    "**/py.typed",
]

EXCLUDES = [
    "av",  # PyAV/FFmpeg (GPL codecs): never shipped; see docs/licenses.md
    "pytest",
    "_pytest",
    "pluggy",
    "iniconfig",
    "pygments",
    "ruff",
    "PyInstaller",
    "_pyinstaller_hooks_contrib",
    "pip",
    "setuptools",
    "pkg_resources",
    "wheel",
    "IPython",
    "matplotlib",
    "pandas",
    "scipy",
    "torch",
    "tensorflow",
    "transformers",
    "onnxruntime.transformers",
    "onnxruntime.tools",
    "onnxruntime.quantization",
    "ctranslate2.converters",
    "ctranslate2.specs",
    "tkinter.test",
    "idlelib",
    "turtledemo",
    "lib2to3",
]


def _dropped(dest):
    return _matches(dest, MAC_FORBIDDEN + MAC_UNNEEDED)


def _try(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        print(f"ShoutAbout-mac.spec: {fn.__name__}{args}: {exc}")
        return []


hiddenimports = collect_submodules("shoutabout")
hiddenimports += [
    "tkinter",
    "tkinter.font",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.simpledialog",
    "tkinter.filedialog",
    "tkinter.scrolledtext",
    # PyObjC frameworks are imported lazily inside functions; name them for the analysis.
    "objc",
    "Foundation",
    "AppKit",
    "CoreFoundation",
    "Quartz",
    "ApplicationServices",
    "AVFoundation",
    "PyObjCTools",
    "PyObjCTools.AppHelper",
]
# PyObjC loads framework wrappers lazily; take every submodule of the ones we use.
for _top in (
    "objc",
    "PyObjCTools",
    "Foundation",
    "AppKit",
    "CoreFoundation",
    "Quartz",
    "ApplicationServices",
    "HIServices",
    "AVFoundation",
):
    hiddenimports += _try(collect_submodules, _top)
datas = collect_data_files("shoutabout") + copy_metadata("shoutabout", recursive=True)
binaries = []

for dist in runtime_requirements():  # already filtered by sys_platform markers
    if dist.startswith("nvidia-"):
        continue
    datas += _try(copy_metadata, dist)
    for top in import_names(dist):
        hiddenimports.append(top)
        datas += _try(collect_data_files, top, excludes=DATA_EXCLUDES)
        binaries += _try(collect_dynamic_libs, top)
if ICO.is_file():
    datas.append((str(ICO), "."))  # the self-test's brand check looks for it

a = Analysis(
    [str(PACKAGING / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

a.binaries = [b for b in a.binaries if not _dropped(b[0])]
a.datas = [d for d in a.datas if not _dropped(d[0])]
_pyav = sorted(name for name, *_ in a.pure if name == "av" or name.startswith("av."))
if _pyav:
    raise SystemExit(f"ShoutAbout-mac.spec: PyAV must not be bundled, found {_pyav[:5]}")

_tops = {name.split(".")[0] for name, *_ in a.pure}
_tops |= {Path(dest).parts[0] for dest, *_ in a.binaries + a.datas if Path(dest).parts}
_tops = {t.removesuffix(".libs") for t in _tops}
_tops.discard("shoutabout")
covered = write_notices(NOTICES, sorted(_tops), APP, __version__)
print(f"ShoutAbout-mac.spec: notices for {len(covered)} distributions -> {NOTICES}")
# After Analysis, TOC entries are (name in the bundle, source path, type).
datas_notices = [(NOTICES.name, str(NOTICES), "DATA")]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,  # never turn Apple Events into arguments
    # No target_arch: thinning universal2 libraries would need `lipo`, which only comes with
    # Apple's developer tools. Python and the wheels are arm64 (or universal2) already.
    target_arch=None,
    codesign_identity=None,  # build.sh signs the finished bundle (ad hoc)
    entitlements_file=None,
    upx=False,
    strip=False,
    debug=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas + datas_notices,
    name=APP,
    upx=False,
    strip=False,
)

app = BUNDLE(
    coll,
    name=f"{APP}.app",
    icon=str(ICNS),
    bundle_identifier=BUNDLE_ID,
    version=__version__,
    info_plist={
        "CFBundleName": APP,
        "CFBundleDisplayName": APP,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,  # a menu bar app: no Dock icon
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": (
            "ShoutAbout listens only while you dictate. Your voice never leaves this Mac."
        ),
        "NSLocalNetworkUsageDescription": (
            "ShoutAbout lets your iPhone find this Mac on your home network, so you can "
            "dictate from the phone."
        ),
        "NSBonjourServices": ["_shoutabout._tcp"],
        "NSHumanReadableCopyright": "Copyright (c) 2026 Spencer Seay. MIT License.",
    },
)
