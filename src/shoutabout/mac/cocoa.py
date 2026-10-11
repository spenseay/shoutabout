"""Small Cocoa helpers: run code on the main thread, and turn brand images into NSImage /
CGImage. AppKit objects (windows, the status item, the overlay) may only be touched on the
main thread, which runs the NSApplication event loop (:mod:`shoutabout.mac.menubar`)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def is_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def call_on_main(fn: Callable[..., Any], *args: Any) -> None:
    """Run ``fn(*args)`` on the main thread soon (right away if this is the main thread
    and no event loop is needed). Errors are logged, never raised to the caller."""

    def guarded() -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("main-thread call %s failed", getattr(fn, "__name__", fn))

    from PyObjCTools import AppHelper

    AppHelper.callAfter(guarded)


def run_on_main(fn: Callable[[], Any], timeout: float = 2.0, default: Any = None) -> Any:
    """Run ``fn`` on the main thread and wait (up to ``timeout``) for its result."""
    if is_main_thread():
        return fn()
    done = threading.Event()
    box: list[Any] = [default]

    def work() -> None:
        try:
            box[0] = fn()
        finally:
            done.set()

    call_on_main(work)
    if not done.wait(timeout):
        log.debug("main-thread call timed out after %.1f s", timeout)
        return default
    return box[0]


def nsdata(data: bytes) -> Any:
    from Foundation import NSData

    return NSData.dataWithBytes_length_(data, len(data))


def nsimage_from_png(png: bytes, points: tuple[float, float] | None = None) -> Any:
    """An NSImage from PNG bytes; ``points`` sets its size in points (a 36 px PNG at
    18 x 18 points is sharp on Retina)."""
    from AppKit import NSImage

    image = NSImage.alloc().initWithData_(nsdata(png))
    if image is not None and points is not None:
        image.setSize_(points)
    return image


def cgimage_from_bgra(width: int, height: int, bgra: bytes) -> Any:
    """A CGImage from premultiplied BGRA bytes (what overlay.premultiplied_bgra makes)."""
    import Quartz

    provider = Quartz.CGDataProviderCreateWithCFData(nsdata(bgra))
    space = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    info = Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little
    return Quartz.CGImageCreate(
        width,
        height,
        8,
        32,
        width * 4,
        space,
        info,
        provider,
        None,
        False,
        Quartz.kCGRenderingIntentDefault,
    )
