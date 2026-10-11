"""THIRD-PARTY-NOTICES.txt for the installed app, built from the build venv's metadata.

The PyInstaller spec calls :func:`write_notices` with the top-level module names and file
destinations it bundles. Every distribution that owns one of them gets a section with its
name, version, declared license and the license / notice files from its ``.dist-info``
(and ``ThirdPartyNotices``-style files inside the package, e.g. onnxruntime's). Python's own
LICENSE.txt (which covers Tcl/Tk, OpenSSL, libffi, ...) and short notes for binaries whose
wheels ship no notice (Intel OpenMP in CTranslate2, PortAudio in sounddevice) are added too.

PyAV/FFmpeg is never bundled (packaging/bundle_rules.py), so it has no section, and
:func:`write_notices` refuses to write one. Audio is decoded by Windows Media Foundation,
which is part of Windows and not redistributed.

This is a compliance aid, not legal advice: docs/licenses.md has the review and its flags.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

LICENSE_FILE = re.compile(r"(licen[cs]e|copying|notice|authors|copyright|eula)", re.IGNORECASE)
TEXT_SUFFIXES = {"", ".txt", ".md", ".rst", ".mit", ".bsd", ".apache", ".apache2", ".psf"}
MAX_FILE_CHARS = 400_000

# Distributions that must never be in the app (the spec excludes them). If one shows up
# among the bundled distributions anyway, write_notices fails the build.
NEVER_SHIPPED = frozenset({"av"})  # PyAV: FFmpeg with x264/x265 (GPL), docs/licenses.md

EXTRA_NOTES = {
    "ctranslate2": (
        "The CTranslate2 wheel bundles libiomp5md.dll (Intel OpenMP runtime, redistributed "
        "under Intel's runtime terms). cudnn64_9.dll from the wheel is not shipped "
        "(CTranslate2 for Windows is built without cuDNN)."
    ),
    "sounddevice": (
        "sounddevice bundles PortAudio "
        "(_sounddevice_data\\portaudio-binaries\\libportaudio64bit.dll), MIT License, "
        "Copyright (c) 1999-2006 Ross Bencina and Phil Burk (http://www.portaudio.com). "
        "The wheel's ASIO-enabled PortAudio builds are not shipped."
    ),
    "nvidia-cublas-cu12": (
        "cublas64_12.dll and cublasLt64_12.dll are redistributed under Attachment A of the "
        "NVIDIA CUDA Toolkit EULA (below). nvblas64_12.dll is not shipped."
    ),
    "nvidia-cuda-nvrtc-cu12": (
        "nvrtc64_120_0.dll and nvrtc-builtins64_*.dll are redistributed under Attachment A of "
        "the NVIDIA CUDA Toolkit EULA (below)."
    ),
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def distributions_for(top_levels: Iterable[str]) -> list[metadata.Distribution]:
    """Installed distributions that provide any of the given top-level names."""
    owners = metadata.packages_distributions()
    names: set[str] = set()
    for top in top_levels:
        for dist in owners.get(top, []):
            names.add(dist)
    found: dict[str, metadata.Distribution] = {}
    for name in names:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        found.setdefault(_norm(dist.metadata["Name"] or name), dist)
    return [found[k] for k in sorted(found)]


def _license_files(dist: metadata.Distribution) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for file in dist.files or []:
        parts = Path(str(file)).parts
        name = parts[-1]
        in_dist_info = any(p.endswith(".dist-info") for p in parts)
        if not LICENSE_FILE.search(name) or Path(name).suffix.lower() not in TEXT_SUFFIXES:
            continue
        if not in_dist_info and len(parts) > 3:  # skip deep test/vendor trees
            continue
        try:
            text = Path(str(dist.locate_file(file))).read_text("utf-8", errors="replace")
        except OSError:
            continue
        out.append((str(file).replace("\\", "/"), text[:MAX_FILE_CHARS]))
    return out


def _declared_license(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    lic = (md.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) < 120:
        return lic
    licenses = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if "License" in c]
    return ", ".join(licenses) or "see the license text below"


def render(dists: Iterable[metadata.Distribution], app_name: str, version: str) -> str:
    rule = "=" * 78
    lines = [
        f"{app_name} {version}: third-party software notices",
        "",
        f"{app_name} includes the open-source and redistributable components listed below.",
        "Each section names the component, its version and license, followed by the license",
        "and notice texts that ship with it.",
        "",
    ]
    dists = list(dists)
    for dist in dists:
        lines.append(f"  - {dist.metadata['Name']} {dist.version} ({_declared_license(dist)})")
    lines += [
        "  - Python " + sys.version.split()[0] + " (PSF-2.0; includes Tcl/Tk, OpenSSL, ...)",
        "",
    ]
    for dist in dists:
        name = dist.metadata["Name"]
        lines += [rule, f"{name} {dist.version}", f"License: {_declared_license(dist)}"]
        note = EXTRA_NOTES.get(_norm(name))
        if note:
            lines += ["", note]
        for path, text in _license_files(dist):
            lines += ["", f"--- {path} ---", "", text.rstrip()]
        lines.append("")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    lines += [rule, f"Python {sys.version.split()[0]}", "License: PSF-2.0 (and the licenses below)"]
    if python_license.is_file():
        lines += ["", python_license.read_text("utf-8", errors="replace").rstrip()]
    lines.append("")
    return "\n".join(lines)


def write_notices(path: Path, top_levels: Iterable[str], app_name: str, version: str) -> list[str]:
    """Write the notices file; returns the distribution names it covers. Raises ValueError
    if a distribution in NEVER_SHIPPED (PyAV) is among the bundled ones."""
    dists = distributions_for(top_levels)
    bad = sorted(d.metadata["Name"] for d in dists if _norm(d.metadata["Name"]) in NEVER_SHIPPED)
    if bad:
        raise ValueError(f"{', '.join(bad)} must not be bundled (FFmpeg; see docs/licenses.md)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(dists, app_name, version), "utf-8")
    return [d.metadata["Name"] for d in dists]
