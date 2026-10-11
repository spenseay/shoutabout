"""Tray icon images: the brand's tray states (:mod:`shoutabout.brand`) as Windows icons.

States: "starting" (grey tile, the shut mouth hums while the model loads), "idle" (mouth
ajar), "transcribing" (mouth open, talking), "paused" (mouth shut) and "error" (red
tile). Animated states have several frames; the tray cycles them on a timer
(:func:`frame_interval_ms`).

:func:`icon_image` returns the "icon resource" bytes Windows' ``CreateIconFromResourceEx``
takes: a BITMAPINFOHEADER (biHeight = 2 x size: XOR image + AND mask), 32 bpp bottom-up
BGRA pixels with straight alpha, then a 1 bpp AND mask with DWORD-aligned rows.
:func:`ico_file` wraps one or more of those in a standard .ico container.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache

from shoutabout import brand
from shoutabout.models import HubStatus

STATES = brand.TRAY_STATES
BITMAPINFOHEADER_SIZE = brand.BITMAPINFOHEADER_SIZE
MIN_SIZE, MAX_SIZE = 8, 256


def icon_key(status: HubStatus | str, paused: bool) -> str:
    """Which tray state to show: error > starting > transcribing > paused > idle."""
    status = HubStatus(status)
    if status == HubStatus.ERROR:
        return "error"
    if status == HubStatus.STARTING:
        return "starting"
    if status == HubStatus.TRANSCRIBING:
        return "transcribing"
    if paused:
        return "paused"
    return "idle"


def and_mask_stride(size: int) -> int:
    """Bytes per row of the 1 bpp AND mask (rows are DWORD-aligned)."""
    return brand.and_mask_stride(size)


def frame_interval_ms(key: str) -> int:
    """Milliseconds per frame while ``key`` is shown; 0 for a still icon."""
    return brand.frame_interval_ms(key)


def is_animated(key: str) -> bool:
    return frame_interval_ms(key) > 0


def _check(key: str, size: int) -> None:
    if key not in STATES:
        raise ValueError(f"unknown icon state {key!r}")
    if not MIN_SIZE <= size <= MAX_SIZE:
        raise ValueError(f"icon size must be {MIN_SIZE}..{MAX_SIZE}, got {size}")


@lru_cache(maxsize=64)
def icon_frames(key: str, size: int = 16) -> tuple[bytes, ...]:
    """Icon-resource bytes for every frame of a state at ``size`` px (cached)."""
    _check(key, size)
    return tuple(brand.icon_resource(img) for img in brand.tray_frames(key, size))


def frame_count(key: str, size: int = 16) -> int:
    return len(icon_frames(key, size))


def icon_image(key: str, size: int = 16, frame: int = 0) -> bytes:
    """Icon-resource bytes (header + XOR + AND) for CreateIconFromResourceEx / .ico."""
    frames = icon_frames(key, size)
    return frames[frame % len(frames)]


def ico_file(key: str = "idle", sizes: Iterable[int] = (16, 20, 24, 32, 48, 64)) -> bytes:
    """A complete .ico file (ICONDIR + entries + images) of a state's first frame."""
    images = []
    for size in sizes:
        _check(key, size)
        images.append(brand.tray_frames(key, size)[0])
    return brand.ico_bytes(images)


# ------------------------------------------------------------------ Windows only


def small_icon_size() -> int:
    """The notification-area icon size (SM_CXSMICON): 16 at 100 % scaling, 24 at 150 %
    (the hub makes the process DPI aware before the tray starts)."""
    from shoutabout.inject import win32 as w

    if not w.IS_WINDOWS:
        return 16
    size = int(w.user32.GetSystemMetrics(w.SM_CXSMICON))
    return size if MIN_SIZE <= size <= MAX_SIZE else 16


def hicon_from_resource(blob: bytes, size: int) -> int:
    """Create an HICON from icon-resource bytes (caller must DestroyIcon it)."""
    from shoutabout.inject import win32 as w

    w.require_windows()
    hicon = w.user32.CreateIconFromResourceEx(
        blob, len(blob), True, w.ICON_RESOURCE_VERSION, size, size, 0
    )
    if not hicon:
        raise OSError(f"CreateIconFromResourceEx failed (error {w.last_error()})")
    return int(hicon)


def make_hicon(key: str, size: int | None = None, frame: int = 0) -> int:
    """Create the HICON for one frame of a state (caller must DestroyIcon it).
    Windows only; raises OSError."""
    size = size or small_icon_size()
    return hicon_from_resource(icon_image(key, size, frame), size)


def make_hicons(key: str, size: int | None = None) -> list[int]:
    """One HICON per frame of a state (caller must destroy them all). All or nothing."""
    size = size or small_icon_size()
    made: list[int] = []
    try:
        for blob in icon_frames(key, size):
            made.append(hicon_from_resource(blob, size))
    except BaseException:
        for hicon in made:
            destroy_hicon(hicon)
        raise
    return made


def destroy_hicon(hicon: int) -> None:
    from shoutabout.inject import win32 as w

    if w.IS_WINDOWS and hicon:
        w.user32.DestroyIcon(hicon)
