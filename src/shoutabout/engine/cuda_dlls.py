"""Make the pip-installed NVIDIA CUDA libraries loadable on Windows.

``uv sync --extra cuda`` installs cuBLAS (and its NVRTC dependency) as wheels under
``site-packages/nvidia/<lib>/bin``. Those folders are not on the DLL search path, so
CTranslate2 fails on first GPU use with "cublas64_12.dll is not found".

Registering the folders with ``os.add_dll_directory`` is not enough on its own:
CTranslate2 loads cuBLAS with a plain ``LoadLibrary`` call (and cuDNN 9, if present,
loads its sub-libraries the same way), which ignores ``AddDllDirectory`` folders but
does search ``PATH``. So each folder is registered *and* prepended to ``PATH``.

Call :func:`add_nvidia_dll_dirs` before the Whisper model is created (it is safe to call
before or after importing ctranslate2; cuBLAS is loaded lazily on first CUDA use).

The installed app (``ShoutAbout.exe``) keeps the same ``nvidia/<lib>/bin`` layout inside its
bundle folder, so the same lookup finds the DLLs there.
"""

from __future__ import annotations

import logging
import os
import site
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()
_registered: list[str] = []
# os.add_dll_directory() handles: keep them referenced for the life of the process.
_handles: list[object] = []


def _frozen_roots() -> list[Path]:
    """The installed app (a PyInstaller build) has no site-packages: the wheels' DLLs are
    copied into the bundle as ``<bundle>/nvidia/<lib>/bin`` (packaging/shoutabout.spec)."""
    if not getattr(sys, "frozen", False):
        return []
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    return [Path(base) / "nvidia"]


def _candidate_roots() -> list[Path]:
    """Every ``nvidia`` namespace-package folder that might hold CUDA wheels."""
    roots: list[Path] = _frozen_roots()
    try:
        import nvidia  # namespace package created by the nvidia-*-cu12 wheels

        roots.extend(Path(p) for p in getattr(nvidia, "__path__", []))
    except ImportError:
        pass
    search: list[str] = []
    try:
        search.extend(site.getsitepackages())
    except AttributeError:  # some embedded/virtualenv layouts lack it
        pass
    try:
        search.append(site.getusersitepackages())
    except AttributeError:
        pass
    search.extend(p for p in sys.path if p.lower().endswith("site-packages"))
    roots.extend(Path(p) / "nvidia" for p in search)

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def find_nvidia_dll_dirs() -> list[str]:
    """``nvidia/*/bin`` folders (with DLLs in them) of the installed NVIDIA wheels."""
    dirs: list[str] = []
    seen: set[str] = set()
    for root in _candidate_roots():
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            bin_dir = child / "bin"
            try:
                has_dll = bin_dir.is_dir() and any(bin_dir.glob("*.dll"))
            except OSError:
                has_dll = False
            if not has_dll:
                continue
            path = os.path.abspath(bin_dir)
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                dirs.append(path)
    return dirs


def _prepend_to_path(dirs: list[str]) -> None:
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    present = {os.path.normcase(os.path.normpath(p)) for p in parts}
    new = [d for d in dirs if os.path.normcase(os.path.normpath(d)) not in present]
    if new:
        os.environ["PATH"] = os.pathsep.join([*new, *parts])


def add_nvidia_dll_dirs() -> list[str]:
    """Register the NVIDIA wheel DLL folders (Windows only; a no-op elsewhere).

    Idempotent and thread-safe. Returns every folder registered so far in this process
    (empty off Windows or when no NVIDIA wheels are installed).
    """
    if sys.platform != "win32":
        return []
    with _lock:
        known = {os.path.normcase(d) for d in _registered}
        for path in find_nvidia_dll_dirs():
            if os.path.normcase(path) in known:
                continue
            try:
                _handles.append(os.add_dll_directory(path))
            except OSError as exc:
                log.warning("could not add DLL directory %s: %s", path, exc)
                continue
            _registered.append(path)
            known.add(os.path.normcase(path))
            log.debug("added NVIDIA DLL directory %s", path)
        # Re-check PATH on every call: something may have replaced os.environ["PATH"].
        _prepend_to_path(_registered)
        return list(_registered)
