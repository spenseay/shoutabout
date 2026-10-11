"""``ShoutAbout.exe --self-test``: prove that a build has everything it needs to run.

CI runs it right after a silent install of the Windows installer; on a user's PC it is a
quick support check. It also works from source: ``python -m shoutabout.selftest``.

It never opens the microphone, never pastes, never binds a port, never starts the hub and
never reads the hub token's value. It only writes into the home folder it is given
(default: a fresh temporary folder, deleted afterwards).

Checks, each PASS / FAIL / SKIP with a detail and its duration:

* ``modules``: every ``shoutabout`` module imports (catches modules the build missed);
* ``dependencies``: every runtime dependency declared in pyproject.toml imports, found
  from the package metadata, so new dependencies are checked without editing this file;
* ``config``: creates (or loads) config.toml, vocab.txt and the hub token in the home;
* ``tk``: a Tcl interpreter and a hidden Tk root window (the bundled Tcl/Tk library);
* ``audio``: decodes the bundled 1 s AAC .m4a tone (``shoutabout/data/selftest.m4a``, the
  iPhone app's format) with Windows Media Foundation (PyAV off Windows) and checks its
  length and pitch;
* ``no-ffmpeg``: the installed app ships no PyAV/FFmpeg (GPL codecs; docs/licenses.md):
  ``import av`` must not find the real PyAV and no FFmpeg DLL may be in the bundle
  (skipped from source, where PyAV is a normal dependency of faster-whisper);
* ``vad``: runs faster-whisper's Silero VAD (onnxruntime + the bundled .onnx asset);
* ``downloads``: huggingface_hub sees hf_xet, so model downloads use the fast Xet transfer
  (required in the installed app);
* ``ctranslate2``: version, CPU compute types and the CUDA device count (0 is fine);
* ``cublas``: the cuBLAS DLLs are found and load by name, the way CTranslate2 loads them
  (required in the installed app, skipped from source without the ``cuda`` extra);
* ``hardware``: hardware detection, when this build has it;
* ``brand``: the tray icon renders, and the installed app has its .ico;
* ``objc-callbacks`` (macOS only): Objective-C and C code call back into Python through
  PyObjC (a method written in Python, a C callback), the way the menu bar, its delegates
  and the hotkey's event tap do. PyObjC makes small executable stubs for that at run time,
  which the Hardened Runtime of a notarized app must allow (packaging/macos/release.sh
  compares this check before and after signing).

The result is one JSON object, written to ``--out FILE`` and printed when there is a
console. Exit code 0 when every required check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import platform
import re
import shutil
import struct
import sys
import tempfile
import time
import traceback
import wave
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from shoutabout import __version__

PACKAGE = "shoutabout"
# Modules that must not be imported by the module check (importing __main__ runs the CLI).
SKIP_MODULES = frozenset({"shoutabout.__main__"})
# Import names for distributions whose metadata can't be mapped (e.g. no RECORD file).
KNOWN_IMPORT_NAMES = {
    "pyjwt": ["jwt"],
    "python-multipart": ["python_multipart", "multipart"],
    "faster-whisper": ["faster_whisper"],
    "nvidia-cublas-cu12": [],  # DLLs only; the cublas check covers them
}
# Extras whose requirements the installed app ships (see packaging/shoutabout.spec).
BUNDLED_EXTRAS = frozenset({"cuda"})
CUBLAS_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
HARDWARE_MODULES = ("shoutabout.hardware", "shoutabout.engine.hardware")
HARDWARE_FUNCTIONS = ("detect", "detect_hardware", "probe_hardware", "probe")
TONE_HZ = 440.0
AUDIO_SAMPLE = "selftest.m4a"  # in shoutabout/data: 1 s, 440 Hz, AAC-LC mono 16 kHz 32 kbps
# Files of PyAV's FFmpeg build that must never be in the installed app.
FFMPEG_FILES = ("avcodec*", "avformat*", "avutil*", "swresample*", "*x264*", "*x265*")
FFMPEG_FILES += ("libavcodec*", "libavformat*", "libavutil*", "libswresample*")  # v2-macos


class SkipCheck(Exception):
    """Raised by a check that doesn't apply here (reported as SKIP, never a failure)."""


@dataclass
class Context:
    home: Path
    work: Path
    frozen: bool


@dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "skip"
    required: bool
    detail: str
    ms: int

    @property
    def failed(self) -> bool:
        return self.status == "fail" and self.required


CheckFn = Callable[[Context], str]


@dataclass
class Check:
    name: str
    fn: CheckFn
    # Required checks fail the self-test; optional ones only report.
    required: bool | Callable[[Context], bool] = True

    def is_required(self, ctx: Context) -> bool:
        return self.required(ctx) if callable(self.required) else bool(self.required)


@dataclass
class Report:
    ok: bool
    app: str
    version: str
    frozen: bool
    executable: str
    bundle: str | None
    python: str
    platform: str
    home: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if c.failed]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["failed"] = self.failed
        return data


# --------------------------------------------------------------------- dependencies


def normalize_dist(name: str) -> str:
    """PEP 503 normalised distribution name ("PyJWT" -> "pyjwt", "a_b.c" -> "a-b-c")."""
    return re.sub(r"[-_.]+", "-", name).lower()


_REQ_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_MARKER = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")
# v2-macos: platform-only dependencies (PyObjC: sys_platform == 'darwin').
_PLATFORM_MARKER = re.compile(r"""sys_platform\s*(==|!=)\s*['"]([^'"]+)['"]""")


def platform_applies(spec: str, platform: str | None = None) -> bool:
    """False when ``spec``'s ``sys_platform`` marker excludes this platform."""
    platform = sys.platform if platform is None else platform
    marker = spec.split(";", 1)[1] if ";" in spec else ""
    for op, value in _PLATFORM_MARKER.findall(marker):
        if (op == "==") != (platform == value):
            return False
    return True


def parse_requirement(spec: str) -> tuple[str, str | None]:
    """'nvidia-cublas-cu12<13; extra == "cuda"' -> ("nvidia-cublas-cu12", "cuda")."""
    match = _REQ_NAME.match(spec)
    if not match:
        raise ValueError(f"unparseable requirement {spec!r}")
    marker = spec.split(";", 1)[1] if ";" in spec else ""
    extra = _EXTRA_MARKER.search(marker)
    return normalize_dist(match.group(1)), (extra.group(1) if extra else None)


def runtime_requirements(
    requires: Iterable[str] | None = None, extras: Iterable[str] = BUNDLED_EXTRAS
) -> list[str]:
    """Normalised names of ShoutAbout's runtime dependencies (plus the given extras), from
    the package metadata. The PyInstaller spec uses the same list to decide what to
    collect, so a dependency added to pyproject.toml lands in the build by itself."""
    if requires is None:
        from importlib import metadata

        requires = metadata.requires(PACKAGE) or []
    wanted = set(extras)
    names: list[str] = []
    for spec in requires:
        if not platform_applies(spec):  # v2-macos
            continue
        name, extra = parse_requirement(spec)
        if (extra is None or extra in wanted) and name not in names:
            names.append(name)
    return names


def import_names(dist: str, mapping: dict[str, list[str]] | None = None) -> list[str]:
    """Top-level import names of an installed distribution (metadata first, then a small
    built-in table; last resort the distribution name itself)."""
    dist = normalize_dist(dist)
    if mapping is None:
        mapping = _top_levels()
    own = dist.replace("-", "_")
    # The name matching the distribution first (python_multipart, not the deprecated
    # multipart alias), private helpers (_sounddevice_data) last.
    names = sorted(mapping.get(dist, []), key=lambda n: (n.startswith("_"), n != own, n))
    if names:
        return names
    if dist in KNOWN_IMPORT_NAMES:
        return list(KNOWN_IMPORT_NAMES[dist])
    return [dist.replace("-", "_")]


def _top_levels() -> dict[str, list[str]]:
    from importlib import metadata

    out: dict[str, list[str]] = {}
    try:
        for top, dists in metadata.packages_distributions().items():
            if not top.isidentifier():
                continue
            for d in dists:
                out.setdefault(normalize_dist(d), []).append(top)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- checks


def _package_modules() -> list[str]:
    import pkgutil

    import shoutabout

    names = {PACKAGE}
    for info in pkgutil.walk_packages(shoutabout.__path__, f"{PACKAGE}.", onerror=lambda _: None):
        names.add(info.name)
    # In the frozen app the bundle's importer also lists every collected module.
    for finder in sys.meta_path:
        toc = getattr(finder, "toc", None)
        if isinstance(toc, (set, frozenset, dict, list, tuple)):
            names.update(n for n in toc if isinstance(n, str) and n.startswith(f"{PACKAGE}."))
    return sorted(n for n in names if n not in SKIP_MODULES)


def check_modules(ctx: Context) -> str:
    names = _package_modules()
    errors = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    if len(names) < 10:
        raise RuntimeError(f"only {len(names)} modules found; the build is missing some")
    return f"{len(names)} modules import"


def check_dependencies(ctx: Context) -> str:
    from shoutabout.avshim import ensure_av_importable

    ensure_av_importable()  # faster-whisper imports PyAV, which the app doesn't ship
    try:
        dists = runtime_requirements()
    except Exception as exc:
        raise RuntimeError(f"no package metadata for {PACKAGE} ({exc})") from exc
    mapping = _top_levels()
    ok: list[str] = []
    errors: list[str] = []
    for dist in dists:
        names = import_names(dist, mapping)
        if not names:
            continue
        failures = []
        for name in names:
            try:
                importlib.import_module(name)
                ok.append(dist)
                break
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            errors.append(f"{dist} ({'; '.join(failures)})")
    if errors:
        raise RuntimeError("can't import " + ", ".join(errors))
    return f"{len(ok)} dependencies import: {', '.join(ok)}"


def check_config(ctx: Context) -> str:
    from shoutabout.config import Paths, ensure_initialized, load_config, load_token

    paths = Paths(ctx.home)
    report = ensure_initialized(paths)
    cfg = load_config(paths)
    token = load_token(paths)  # validates it; the value is never reported
    created = ", ".join(p.name for p in report.created) or "nothing"
    return (
        f"{paths.home}: created {created}; port {cfg.server.port}, "
        f"stt {cfg.stt.model}@{cfg.stt.device}; token ok ({len(token)} chars)"
    )


def check_tk(ctx: Context) -> str:
    import tkinter

    interp = tkinter.Tcl()
    patch = interp.eval("info patchlevel")
    root = tkinter.Tk()
    try:
        root.withdraw()  # never shown: withdrawn before the first update
        tk_patch = root.tk.eval("set tk_patchLevel")
    finally:
        root.destroy()
    return f"Tcl {patch}, Tk {tk_patch}"


def _write_tone(path: Path, seconds: float = 1.0, rate: int = 16_000) -> None:
    frames = bytearray()
    for i in range(int(seconds * rate)):
        value = int(0.3 * 32767 * math.sin(2 * math.pi * TONE_HZ * i / rate))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def _dominant_hz(samples: Any, rate: int) -> float:
    import numpy as np

    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.fft.rfftfreq(len(samples), 1 / rate)[int(np.argmax(spectrum))])


def audio_sample_path() -> Path:
    from importlib import resources

    return Path(str(resources.files("shoutabout.data").joinpath(AUDIO_SAMPLE)))


def check_audio(ctx: Context) -> str:
    from shoutabout.audio import (
        MEDIA_FOUNDATION,
        PYAV,
        SAMPLE_RATE,
        WAV,
        decode_audio_info,
    )

    sample = audio_sample_path()
    if not sample.is_file():
        raise RuntimeError(f"{sample} is missing from the build")
    if sys.platform == "win32":
        backend = MEDIA_FOUNDATION  # what the installed app decodes iPhone audio with
    elif sys.platform == "darwin":  # v2-macos: afconvert
        from shoutabout.audio import COREAUDIO

        backend = COREAUDIO
    else:
        from shoutabout import pyavdecode

        if not pyavdecode.available():
            raise SkipCheck("no compressed-audio decoder here (PyAV isn't installed)")
        backend = PYAV
    decoded = decode_audio_info(sample, backends=(backend,))
    seconds = decoded.samples.size / SAMPLE_RATE
    if not 0.9 <= seconds <= 1.2:
        raise RuntimeError(f"decoded {seconds:.2f} s from the 1 s AAC tone")
    pitch = _dominant_hz(decoded.samples, SAMPLE_RATE)
    if abs(pitch - TONE_HZ) > 5:
        raise RuntimeError(f"the 440 Hz AAC tone decoded at {pitch:.0f} Hz")

    wav = ctx.work / "tone.wav"
    _write_tone(wav, rate=48_000)
    tone = decode_audio_info(wav, backends=(WAV,))
    if (
        abs(tone.samples.size - SAMPLE_RATE) > 2
        or abs(_dominant_hz(tone.samples, SAMPLE_RATE) - TONE_HZ) > 5
    ):
        raise RuntimeError("the 48 kHz WAV tone didn't resample to 1 s of 440 Hz at 16 kHz")
    via = {MEDIA_FOUNDATION: "Windows Media Foundation", "coreaudio": "Core Audio"}.get(
        backend, "PyAV"
    )
    return (
        f"{via}: {AUDIO_SAMPLE} (AAC, {decoded.source_rate or '?'} Hz) -> {seconds:.2f} s "
        f"of {pitch:.0f} Hz at {SAMPLE_RATE} Hz; 48 kHz WAV -> 16 kHz ok"
    )


def check_no_ffmpeg(ctx: Context) -> str:
    """The installed app must not ship PyAV's FFmpeg (GPL codecs; docs/licenses.md)."""
    if not ctx.frozen:
        raise SkipCheck("running from source: PyAV is a normal dependency here, never shipped")
    import fnmatch

    from shoutabout.avshim import real_av_available
    from shoutabout.frozen import bundle_dir

    problems = []
    if real_av_available():
        problems.append("the real PyAV imports")
    root = bundle_dir()
    if root is not None:
        for path in Path(root).rglob("*"):
            name = path.name.lower()
            if path.is_file() and any(fnmatch.fnmatchcase(name, p) for p in FFMPEG_FILES):
                problems.append(str(path.relative_to(root)))
            if path.is_dir() and name in ("av", "av.libs"):
                problems.append(f"{path.relative_to(root)}{os.sep}")
    if problems:
        raise RuntimeError("FFmpeg/PyAV is in the build: " + ", ".join(problems[:10]))
    via = "Core Audio" if sys.platform == "darwin" else "Windows Media Foundation"
    return f"no PyAV/FFmpeg in the build (audio is decoded by {via})"


def check_vad(ctx: Context) -> str:
    import numpy as np
    import onnxruntime

    from shoutabout.avshim import ensure_av_importable

    ensure_av_importable()  # importing faster_whisper imports PyAV, which the app doesn't ship
    from faster_whisper.vad import get_speech_timestamps, get_vad_model

    get_vad_model()  # loads silero_vad_v6.onnx from faster_whisper/assets
    chunks = get_speech_timestamps(np.zeros(16_000, dtype=np.float32))
    return f"onnxruntime {onnxruntime.__version__}: Silero VAD ran ({len(chunks)} speech chunks)"


def check_downloads(ctx: Context) -> str:
    """The speech model downloads use Hugging Face's fast Xet transfer when hf_xet is there.
    huggingface_hub finds hf_xet through its package metadata, which a build can lose."""
    import importlib.util

    try:
        from huggingface_hub.utils._runtime import is_xet_available
    except ImportError as exc:
        raise SkipCheck(f"this huggingface_hub has no Xet support ({exc})") from exc
    if importlib.util.find_spec("hf_xet") is None:
        raise SkipCheck("hf_xet isn't installed")
    if not is_xet_available():
        raise RuntimeError("hf_xet is bundled but huggingface_hub can't find it (no metadata?)")
    return "model downloads use hf_xet"


def check_ctranslate2(ctx: Context) -> str:
    from shoutabout.engine.cuda_dlls import add_nvidia_dll_dirs

    add_nvidia_dll_dirs()  # what the engine does before importing ctranslate2
    import ctranslate2

    cpu = sorted(ctranslate2.get_supported_compute_types("cpu"))
    try:
        gpus = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:  # broken driver: report, don't fail (CPU still works)
        gpus = -1
        return f"ctranslate2 {ctranslate2.__version__}; CPU {cpu}; CUDA query failed: {exc}"
    return f"ctranslate2 {ctranslate2.__version__}; CPU compute types {cpu}; CUDA devices {gpus}"


def _module_path(handle: int) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(handle), buf, len(buf))  # type: ignore[attr-defined]
    return buf.value


def check_cublas(ctx: Context) -> str:
    if sys.platform != "win32":
        raise SkipCheck("Windows only")
    from shoutabout.engine.cuda_dlls import add_nvidia_dll_dirs

    dirs = add_nvidia_dll_dirs()
    found = {}
    for d in dirs:
        for dll in CUBLAS_DLLS:
            if dll not in found and (Path(d) / dll).is_file():
                found[dll] = Path(d) / dll
    if not found:
        if ctx.frozen:
            raise RuntimeError("cuBLAS DLLs are not in the bundle")
        raise SkipCheck("no NVIDIA wheels installed (uv sync --extra cuda)")
    missing = [dll for dll in CUBLAS_DLLS if dll not in found]
    if missing:
        raise RuntimeError(f"missing {', '.join(missing)} (found {sorted(found)})")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryW.restype = ctypes.c_void_p
    kernel32.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
    # CTranslate2 loads cuBLAS by bare name with LoadLibrary: do exactly that. Loading needs
    # no GPU or driver (cuBLAS only links cublasLt + kernel32; nvcuda is loaded on first use).
    handle = kernel32.LoadLibraryW(CUBLAS_DLLS[0])
    if not handle:
        raise RuntimeError(
            f"LoadLibrary({CUBLAS_DLLS[0]}) failed (error {ctypes.get_last_error()}); dirs: {dirs}"
        )
    loaded = _module_path(handle)
    expected = os.path.normcase(str(found[CUBLAS_DLLS[0]]))
    if os.path.normcase(loaded) != expected:
        raise RuntimeError(f"loaded {loaded}, expected {found[CUBLAS_DLLS[0]]}")
    return f"loaded {loaded}"


def check_hardware(ctx: Context) -> str:
    for module_name in HARDWARE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
        for fn_name in HARDWARE_FUNCTIONS:
            fn = getattr(module, fn_name, None)
            if callable(fn):
                result = fn()
                if callable(getattr(result, "summary", None)):
                    result = result.summary()
                elif hasattr(result, "to_dict"):
                    result = result.to_dict()
                elif hasattr(result, "__dataclass_fields__"):
                    result = asdict(result)
                text = json.dumps(result, default=str) if not isinstance(result, str) else result
                return f"{module_name}.{fn_name}(): {text[:400]}"
        raise RuntimeError(f"{module_name} has none of {', '.join(HARDWARE_FUNCTIONS)}()")
    raise SkipCheck("this build has no hardware detection")


def check_brand(ctx: Context) -> str:
    from shoutabout import brand
    from shoutabout.frozen import app_icon_path

    frames = brand.tray_frames("idle", 16)
    if not frames or frames[0].width != 16:
        raise RuntimeError("tray icon didn't render")
    icon = app_icon_path()
    if icon is None:
        if ctx.frozen and sys.platform == "darwin":  # v2-macos: the .app has an .icns
            return "menu bar icon renders (the app icon is the bundle's .icns)"
        if ctx.frozen:
            raise RuntimeError("ShoutAbout.ico is missing from the app folder")
        return "tray icon renders (no .ico: running from source)"
    return f"tray icon renders; app icon {icon.name} ({icon.stat().st_size} bytes)"


def check_overlay(ctx: Context) -> str:
    """The mouth pill's frames render (numpy + the brand rasterizer + GDI text): the first
    listening frame, a frame of the zip ("done"), the "Ready" pill and the "No mic" pill.
    No window is shown. The words come from GDI on Windows and AppKit on a Mac."""
    from shoutabout import overlay

    text_mask = overlay.gdi_text_mask
    if sys.platform == "darwin":
        from shoutabout.mac.overlay_mac import mac_text_mask as text_mask

    t0 = time.perf_counter()
    first = overlay.bitmap(overlay.first_listening_look(), 1.0, text_mask)
    zipping = overlay.frame_at("done", 0.2).look
    if zipping is None or zipping.kind != "zip":
        raise RuntimeError("the pasted pill doesn't zip")
    zipped = overlay.bitmap(zipping, 1.0, text_mask)
    ready = overlay.render(overlay.Look("text", text="Ready · Right Ctrl to talk"), 1.0, text_mask)
    no_mic = overlay.render(overlay.Look("text", tone="red", text="No mic"), 1.0, text_mask)
    pill_w = math.ceil(overlay.PILL_W + 2 * overlay.MARGIN)
    if (
        first.width != pill_w
        or (zipped.width, zipped.height) != (first.width, first.height)
        or ready.width <= no_mic.width
        or no_mic.width < first.width
    ):
        raise RuntimeError("the pill's frames have the wrong size")
    ms = (time.perf_counter() - t0) * 1000
    return f"pill frames render ({first.width}x{first.height} px at 100 %, {ms:.0f} ms)"


def check_history(ctx: Context) -> str:
    """History and failed recordings round-trip in a scratch folder (never the real one)."""
    import numpy as np

    from shoutabout.failed_audio import FailedRecordings
    from shoutabout.history import HistoryLog, read_history
    from shoutabout.models import CleanupResult, Timings, TranscribeResult

    folder = ctx.work / "history"
    log_ = HistoryLog(folder / "history.jsonl", folder / "audio", keep_audio=False)
    text = "Self-test dictation."
    log_.append(
        TranscribeResult(
            text=text,
            raw_text=text,
            target="pc",
            route="local",
            pasted=False,
            warning=None,
            cleanup=CleanupResult(text=text, status="disabled"),
            timings=Timings(),
        )
    )
    records = read_history(log_.path)
    if not records or records[0].get("text") != text:
        raise RuntimeError("history didn't read back")
    failed = FailedRecordings(folder / "failed")
    item = failed.keep_samples(np.zeros(16_000, dtype=np.float32))
    if item is None or not item.audio.exists():
        raise RuntimeError("couldn't keep a failed recording")
    log_.clear()
    failed.clear()
    if log_.path.exists() or failed.list():
        raise RuntimeError("Clear history left something behind")
    return "history writes, reads back and clears; failed recordings kept and cleared"


OBJC_SELFTEST_CLASS = "ShoutAboutSelfTestTarget"


def _objc_compare(a: Any, b: Any, context: Any) -> int:
    """NSComparator-style C callback for the objc-callbacks check."""
    return (a > b) - (a < b)


def check_objc_callbacks(ctx: Context) -> str:
    """v2-codemagic: PyObjC's calls back into Python work in this process. A notarized app
    runs with the Hardened Runtime, which forbids the executable memory PyObjC's stubs
    need unless the signature's entitlements allow it (com.apple.security.cs.allow-jit)."""
    if sys.platform != "darwin":
        raise SkipCheck("macOS only")
    import objc
    from Foundation import NSArray, NSObject

    try:
        target_class = objc.lookUpClass(OBJC_SELFTEST_CLASS)
    except objc.nosuchclass_error:

        class ShoutAboutSelfTestTarget(NSObject):  # an Objective-C class, written in Python
            def answer(self):  # Cocoa reaches it the way it reaches the menu's delegates
                return 42

        target_class = ShoutAboutSelfTestTarget
    target = target_class.alloc().init()
    answer = target.performSelector_("answer")
    if int(answer) != 42:
        raise RuntimeError(f"an Objective-C call into Python returned {answer!r}")
    # A C function pointer that runs Python (like the event tap's callback).
    ordered = NSArray.arrayWithArray_([3, 1, 2]).sortedArrayUsingFunction_context_(
        _objc_compare, None
    )
    if [int(x) for x in ordered] != [1, 2, 3]:
        raise RuntimeError(f"a C callback into Python sorted wrongly: {list(ordered)!r}")
    return "Objective-C and C call back into Python (PyObjC stubs are allowed)"


def _required_when_frozen(ctx: Context) -> bool:
    return ctx.frozen


DEFAULT_CHECKS: tuple[Check, ...] = (
    Check("modules", check_modules),
    Check("dependencies", check_dependencies),
    Check("config", check_config),
    Check("tk", check_tk),
    Check("audio", check_audio),
    Check("no-ffmpeg", check_no_ffmpeg, required=_required_when_frozen),
    Check("vad", check_vad),
    Check("downloads", check_downloads, required=_required_when_frozen),
    Check("ctranslate2", check_ctranslate2),
    Check("cublas", check_cublas, required=_required_when_frozen),
    Check("hardware", check_hardware),
    Check("brand", check_brand),
    Check("overlay", check_overlay),
    Check("history", check_history),
    Check("objc-callbacks", check_objc_callbacks),  # v2-codemagic
)


# ------------------------------------------------------------------------- runner


def run_check(check: Check, ctx: Context) -> CheckResult:
    required = check.is_required(ctx)
    t0 = time.perf_counter()
    try:
        detail = check.fn(ctx)
        status = "pass"
    except SkipCheck as exc:
        detail, status = str(exc), "skip"
    except BaseException as exc:  # SystemExit from a library is a failure too
        if isinstance(exc, KeyboardInterrupt):
            raise
        tb = traceback.extract_tb(exc.__traceback__)
        where = f" at {Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else ""
        detail, status = f"{type(exc).__name__}: {exc}{where}", "fail"
    ms = round((time.perf_counter() - t0) * 1000)
    return CheckResult(check.name, status, required, str(detail), ms)


def run_selftest(
    home: Path,
    *,
    checks: Sequence[Check] | None = None,
    frozen: bool | None = None,
    work: Path | None = None,
) -> Report:
    from shoutabout.frozen import bundle_dir, is_frozen

    frozen = is_frozen() if frozen is None else frozen
    bundle = bundle_dir()
    own_work = work is None
    work = Path(tempfile.mkdtemp(prefix="shoutabout-selftest-")) if work is None else work
    ctx = Context(home=Path(home), work=work, frozen=frozen)
    try:
        results = [
            run_check(check, ctx) for check in (DEFAULT_CHECKS if checks is None else checks)
        ]
    finally:
        if own_work:
            shutil.rmtree(work, ignore_errors=True)
    report = Report(
        ok=False,
        app=PACKAGE,
        version=__version__,
        frozen=frozen,
        executable=sys.executable,
        bundle=str(bundle) if bundle else None,
        python=platform.python_version(),
        platform=platform.platform(),
        home=str(home),
        checks=results,
    )
    report.ok = not report.failed
    return report


def format_summary(report: Report) -> str:
    lines = [f"ShoutAbout {report.version} self-test ({'frozen' if report.frozen else 'source'})"]
    for c in report.checks:
        tag = {"pass": "PASS", "fail": "FAIL" if c.required else "WARN", "skip": "SKIP"}
        lines.append(f"  [{tag[c.status]}] {c.name} ({c.ms} ms): {c.detail}")
    lines.append("OK" if report.ok else f"FAILED: {', '.join(report.failed)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ShoutAbout.exe --self-test",
        description="Check that this ShoutAbout build has everything it needs.",
    )
    parser.add_argument(
        "--home", metavar="DIR", help="config folder to create/load (default: a temp folder)"
    )
    parser.add_argument("--out", metavar="FILE", help="also write the JSON report here")
    parser.add_argument("--text", action="store_true", help="print a readable summary, not JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    temp_home = None
    if args.home:
        home = Path(args.home).expanduser()
    else:
        temp_home = Path(tempfile.mkdtemp(prefix="shoutabout-selftest-home-"))
        home = temp_home
    try:
        report = run_selftest(home)
    finally:
        if temp_home is not None:
            shutil.rmtree(temp_home, ignore_errors=True)

    data = json.dumps(report.to_dict(), indent=2)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(data + "\n", "utf-8")
    if sys.stdout is not None:
        try:
            print(format_summary(report) if args.text else data)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass  # no usable console: the --out file has the report
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
