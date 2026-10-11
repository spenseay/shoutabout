"""A stand-in for PyAV (``av``) when it isn't installed, as in the Windows app.

The installed app ships without FFmpeg (PyAV's wheels bundle GPL codec libraries; see
docs/licenses.md). faster-whisper still runs ``import av`` when it is imported, for its own
file decoder (``faster_whisper.decode_audio``), which ShoutAbout never uses: the engine is
always handed 16 kHz float32 samples decoded by :mod:`shoutabout.audio`.

:func:`ensure_av_importable` must run before ``faster_whisper`` is imported. It leaves a
working PyAV alone and only installs the placeholder module when ``import av`` fails. The
placeholder raises :class:`PyAVUnavailableError` (an ``AttributeError``, so ``hasattr`` and
``getattr(av, name, default)`` keep working) with a plain explanation as soon as anything
actually uses it.
"""

from __future__ import annotations

import sys
import threading
import types

MODULE = "av"
_MARKER = "__shoutabout_placeholder__"
_lock = threading.Lock()


class PyAVUnavailableError(AttributeError):
    """Something tried to use PyAV, which this installation doesn't include."""


class _Placeholder(types.ModuleType):
    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise PyAVUnavailableError(
            f"PyAV (FFmpeg) isn't part of this ShoutAbout installation, so av.{name} can't "
            "be used. ShoutAbout decodes audio itself (shoutabout.audio); pass the speech "
            "engine 16 kHz float32 samples, never a file."
        )


def is_placeholder(module: object) -> bool:
    return isinstance(module, _Placeholder) or bool(getattr(module, _MARKER, False))


def _placeholder() -> types.ModuleType:
    module = _Placeholder(MODULE, "Placeholder for PyAV, which this installation doesn't ship.")
    setattr(module, _MARKER, True)
    return module


def ensure_av_importable() -> bool:
    """Make ``import av`` succeed. True when it is the real PyAV, False when the placeholder
    had to be installed (or already was)."""
    with _lock:
        current = sys.modules.get(MODULE)
        if current is not None:
            return not is_placeholder(current)
        try:
            import av  # noqa: F401

            return True
        except (ImportError, OSError):
            # ImportError: not installed (or blocked); OSError: its FFmpeg DLLs won't load.
            sys.modules[MODULE] = _placeholder()
            return False


def real_av_available() -> bool:
    """True when the real PyAV can be imported (never imports it if a placeholder is in)."""
    current = sys.modules.get(MODULE)
    if current is not None:
        return not is_placeholder(current)
    try:
        import av  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def import_real_av() -> types.ModuleType:
    """The real ``av`` module; ImportError if it isn't installed (or only the placeholder)."""
    current = sys.modules.get(MODULE)
    if current is not None and is_placeholder(current):
        raise ImportError("PyAV isn't installed (only ShoutAbout's placeholder is)")
    try:
        import av
    except OSError as exc:
        raise ImportError(f"PyAV won't load: {exc}") from exc
    return av
