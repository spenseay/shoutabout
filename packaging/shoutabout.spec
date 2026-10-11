# PyInstaller spec for ShoutAbout.exe: a windowed "onedir" build of the hub.
#
# Build with packaging\build.ps1 (it renders the icon first and runs Inno Setup after), or:
#   .venv\Scripts\python.exe packaging\make_assets.py
#   .venv\Scripts\pyinstaller.exe packaging\shoutabout.spec --noconfirm --clean
#     --distpath packaging\dist --workpath packaging\build\pyinstaller
#
# Output: packaging\dist\ShoutAbout\ShoutAbout.exe plus ShoutAbout\_internal\ (Python, the
# libraries, Tcl/Tk, the cuBLAS DLLs under _internal\nvidia\<lib>\bin, the brand icon).
#
# What gets collected, and why it needs no edits when the app gains dependencies:
# * every shoutabout module and package data file (fonts, examples, ...);
# * for every runtime dependency declared in pyproject.toml (read from the installed
#   metadata, the same list the self-test checks): its metadata, its data files (minus
#   sources, stubs and tests) and its DLLs, e.g. sounddevice's PortAudio DLL;
# * minus what packaging/bundle_rules.py drops: never the ASIO PortAudio builds nor PyAV and
#   its FFmpeg DLLs (licenses), nor DLLs nothing loads (cuDNN, nvblas, other platforms'
#   PortAudio);
# * PyInstaller's own hooks handle the rest of the import graph (onnxruntime, Tcl/Tk for
#   tkinter, uvicorn's dynamically imported modules, ...);
# * never PyAV (`av`, in EXCLUDES): audio is decoded with numpy and Windows Media
#   Foundation, and faster-whisper's `import av` gets shoutabout.avshim's placeholder;
# * from the `cuda` extra only the DLLs CTranslate2 needs (see CUDA_DLLS).
# pyright: reportUndefinedVariable=false
# ruff: noqa: F821  (Analysis, PYZ, EXE, COLLECT and SPECPATH are injected by PyInstaller)

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

HERE = Path(SPECPATH).resolve()
ROOT = HERE.parent
for p in (ROOT / "src", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bundle_rules import is_dropped  # noqa: E402  (packaging/bundle_rules.py)
from notices import write_notices  # noqa: E402  (packaging/notices.py)

from shoutabout import __version__  # noqa: E402
from shoutabout.selftest import import_names, runtime_requirements  # noqa: E402

APP = "ShoutAbout"
ASSETS = Path(os.environ.get("SHOUTABOUT_ASSETS", HERE / "build" / "assets")).resolve()
ICON = ASSETS / "ShoutAbout.ico"
NOTICES = ASSETS / "THIRD-PARTY-NOTICES.txt"
if not ICON.is_file():
    raise SystemExit(f"{ICON} is missing: run packaging\\make_assets.py first")

# Data files never needed at runtime.
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

# Development, build and heavy optional packages that must never end up in the app.
EXCLUDES = [
    # PyAV bundles FFmpeg built with x264/x265 (GPL). faster-whisper imports it, but only
    # for its own file decoder, which ShoutAbout never calls (docs/licenses.md, flag 1).
    "av",
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

# cuBLAS for CTranslate2 on NVIDIA GPUs (from the `cuda` extra). CTranslate2 loads
# cublas64_12.dll by name, which links cublasLt64_12.dll; cublasLt loads NVRTC on demand
# for kernels it compiles at run time. nvblas (a BLAS shim) and the ".alt" NVRTC build
# are never loaded, so they are left out. Collected as data (not binaries) so PyInstaller
# doesn't analyse them and copy a second 600 MB cublasLt into the top folder.
CUDA_DLLS = {
    "cublas": ["cublas64_12.dll", "cublasLt64_12.dll"],
    "cuda_nvrtc": ["nvrtc64_120_0.dll", "nvrtc-builtins64_*.dll"],
}


def _nvidia_root():
    for base in sys.path:
        candidate = Path(base) / "nvidia"
        if candidate.is_dir() and base.lower().endswith("site-packages"):
            return candidate
    return None


def _cuda_datas():
    root = _nvidia_root()
    if root is None:
        print("shoutabout.spec: no NVIDIA wheels (uv sync --extra cuda): CPU-only build")
        return []
    out = []
    for lib, patterns in CUDA_DLLS.items():
        bin_dir = root / lib / "bin"
        for pattern in patterns:
            matches = sorted(bin_dir.glob(pattern))
            if not matches:
                raise SystemExit(f"{bin_dir}\\{pattern} not found; run uv sync --extra cuda")
            out += [(str(m), f"nvidia/{lib}/bin") for m in matches]
    return out


def _try(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # not a package, no metadata, ...
        print(f"shoutabout.spec: {fn.__name__}{args}: {exc}")
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
]
# Metadata of shoutabout and every runtime dependency, recursively: libraries look each
# other up with importlib.metadata (huggingface_hub only uses hf_xet's fast downloads when it
# finds hf_xet's metadata), and the self-test reads the dependency list from it.
datas = collect_data_files("shoutabout") + copy_metadata("shoutabout", recursive=True)
binaries = []

dependencies = runtime_requirements()
for dist in dependencies:
    if dist.startswith("nvidia-"):
        continue  # DLLs only, chosen in CUDA_DLLS
    datas += _try(copy_metadata, dist)
    for top in import_names(dist):
        hiddenimports.append(top)
        datas += _try(collect_data_files, top, excludes=DATA_EXCLUDES)
        binaries += _try(collect_dynamic_libs, top)
datas += _cuda_datas()
datas.append((str(ICON), "."))  # for Tk windows (shoutabout.frozen.app_icon_path)

a = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,  # keep docstrings and asserts: some libraries read their own docstrings
)

# Files that must not ship (licensing) or aren't needed: packaging/bundle_rules.py.
a.binaries = [b for b in a.binaries if not is_dropped(b[0])]
a.datas = [d for d in a.datas if not is_dropped(d[0])]
_pyav = sorted(name for name, *_ in a.pure if name == "av" or name.startswith("av."))
if _pyav:
    raise SystemExit(f"shoutabout.spec: PyAV must not be bundled, found {_pyav[:5]}")

# Third-party notices for everything that made it into the bundle.
_tops = {name.split(".")[0] for name, *_ in a.pure}
_tops |= {Path(dest).parts[0] for dest, *_ in a.binaries + a.datas if Path(dest).parts}
_tops = {t.removesuffix(".libs") for t in _tops}
_tops.discard("shoutabout")
covered = write_notices(NOTICES, sorted(_tops), APP, __version__)
print(f"shoutabout.spec: notices for {len(covered)} distributions -> {NOTICES}")

_parts = [int(x) for x in __version__.split("+")[0].split("-")[0].split(".")[:3] if x.isdigit()]
_v = tuple((_parts + [0, 0, 0, 0])[:4])
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_v, prodvers=_v, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", APP),
                        StringStruct("FileDescription", APP),
                        StringStruct("FileVersion", __version__),
                        StringStruct("InternalName", APP),
                        StringStruct("OriginalFilename", f"{APP}.exe"),
                        StringStruct("ProductName", APP),
                        StringStruct("ProductVersion", __version__),
                        StringStruct("LegalCopyright", "Copyright (c) 2026 Spencer Seay. MIT License."),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    icon=str(ICON),
    version=version_info,
    console=False,  # windowed: no console window (frozen.py attaches one for CLI commands)
    disable_windowed_traceback=False,
    upx=False,  # UPX breaks CUDA DLLs and upsets antivirus scanners
    strip=False,
    debug=False,
    bootloader_ignore_signals=False,
    uac_admin=False,  # the hub must run as the signed-in user, never elevated
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name=APP,
    upx=False,
    strip=False,
)
