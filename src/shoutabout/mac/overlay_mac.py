"""The mouth pill on macOS: a borderless, non-activating NSPanel at the top centre.

The frames are exactly the Windows overlay's (:func:`shoutabout.overlay.frame_at` and
:func:`shoutabout.overlay.render`, rendered at the screen's backing scale, so they are
sharp on Retina), turned into CGImages once and cached, so a state change shows on the
next frame without rendering.

The panel:

* is borderless, transparent and **non-activating** (``NSWindowStyleMaskNonactivatingPanel``)
  and never becomes key, so the text lands in the window you were typing in;
* ignores the mouse (clicks go through it);
* floats at the status-window level, joins every Space and full-screen apps, and stays
  visible while ShoutAbout isn't the active app (``hidesOnDeactivate`` off);
* sits at the top centre of the screen with the active window (``NSScreen.mainScreen``),
  just below the menu bar (``visibleFrame``).

All AppKit work happens on the main thread: the public methods only record the new state
under a lock and ask the main thread to draw (:func:`shoutabout.mac.cocoa.call_on_main`).
A ~30 fps timer runs only while the pill is visible. The drawing logic goes through a tiny
window facade, so it is unit-tested with a fake on any OS.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Protocol

from shoutabout import overlay as ov

log = logging.getLogger(__name__)

TOP_GAP_PT = 8.0  # between the menu bar and the pill's canvas
FPS = 30
IMAGE_CACHE = 256

# NSWindow constants (AppKit ABI values).
STYLE_BORDERLESS = 0
STYLE_NONACTIVATING_PANEL = 1 << 7
BACKING_BUFFERED = 2
STATUS_WINDOW_LEVEL = 25
JOIN_ALL_SPACES = 1 << 0
STATIONARY = 1 << 4
IGNORES_CYCLE = 1 << 6
FULLSCREEN_AUXILIARY = 1 << 8

Rect = tuple[float, float, float, float]  # x, y, width, height (Cocoa: origin bottom-left)


def place_top_centre(visible: Rect, width_pt: float, height_pt: float) -> tuple[float, float]:
    """Bottom-left corner (Cocoa coordinates) of a canvas centred at the top of
    ``visible`` (the screen minus the menu bar and the Dock), TOP_GAP_PT below its top."""
    x, y, w, h = visible
    left = x + (w - width_pt) / 2
    bottom = y + h - height_pt - TOP_GAP_PT
    return round(left), round(max(y, bottom))


def prewarm_looks(limit: int = 400) -> list[Any]:
    """The distinct looks of "listening" (every mic level), "thinking", "done" (the zip)
    and "cancelled", most needed first, without text (text needs AppKit)."""
    looks: list[Any] = []
    seen: set[Any] = set()

    def add(frame: ov.Frame) -> None:
        if frame.look is not None and frame.look not in seen and not frame.look.text:
            seen.add(frame.look)
            looks.append(frame.look)

    add(ov.frame_at("listening", 0.0, 0.0))
    steps = ov.LEVEL_STEPS
    for i in range(steps):
        add(ov.frame_at("listening", 0.0, i / max(1, steps - 1)))
    for i in range(48):
        add(ov.frame_at("thinking", i * 0.034))
    for frame in ov.done_frames():
        add(frame)
    add(ov.frame_at("cancelled", 0.0))
    return looks[:limit]


class PanelLike(Protocol):
    """What the overlay needs from the window (main thread only)."""

    def screen(self) -> tuple[Rect, float]: ...  # visible frame, backing scale
    def show(self, image: Any, frame: Rect, scale: float, alpha: float) -> None: ...
    def hide(self) -> None: ...
    def make_image(self, img: Any) -> Any: ...  # brand.Image -> platform image
    def start_timer(self, interval: float, tick: Callable[[], None]) -> None: ...
    def stop_timer(self) -> None: ...
    def close(self) -> None: ...


class MacOverlay:
    """:class:`shoutabout.overlay.OverlayLike` for macOS. Thread-safe API."""

    def __init__(
        self,
        *,
        panel_factory: Callable[[], PanelLike] | None = None,
        main: Callable[..., None] | None = None,
        text_renderer: ov.TextRenderer | None = None,
        clock: Callable[[], float] = time.monotonic,
        fps: int = FPS,
    ) -> None:
        if main is None:
            from shoutabout.mac.cocoa import call_on_main as main
        self._main = main
        self._panel_factory = panel_factory or CocoaPanel
        self._text = text_renderer if text_renderer is not None else mac_text_mask
        self._clock = clock
        self._interval = 1.0 / max(1, fps)
        self._lock = threading.Lock()
        self._state = "hidden"
        self._message = ""
        self._started = 0.0
        self._level = 0.0
        self._fresh = False
        # main thread only:
        self._panel: PanelLike | None = None
        self._visible = False
        self._timer = False
        self._frame: Rect | None = None
        self._scale = 2.0
        self._visible_frame: Rect = (0.0, 0.0, 1440.0, 875.0)
        self._images: OrderedDict[tuple[Any, float], Any] = OrderedDict()
        self._last: tuple[Any, float, int] | None = None

    # -- thread-safe API

    def start(self) -> None:
        self._main(self._create)

    def stop(self) -> None:
        with self._lock:
            self._state = "hidden"
        self._main(self._destroy)

    def _set(self, state: str, text: str = "") -> None:
        with self._lock:
            self._state = state
            self._message = ov.short_text(text) if text else ""
            self._started = self._clock()
            self._fresh = True
        self._main(self._tick)

    def listening(self) -> None:
        self._level = 0.0
        self._set("listening")

    def set_level(self, level: float) -> None:
        self._level = float(level)

    def thinking(self) -> None:
        self._set("thinking")

    def done(self) -> None:
        self._set("done")

    def error(self, text: str) -> None:
        self._set("error", text)

    def notice(self, text: str) -> None:
        self._set("notice", text)

    def cancelled(self) -> None:
        self._set("cancelled")

    def hide(self) -> None:
        self._set("hidden")

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    # -- main thread

    def _create(self) -> None:
        if self._panel is None:
            self._panel = self._panel_factory()
            # Render the first listening frames now, so the pill appears instantly later.
            threading.Thread(target=self._prewarm, name="overlay-prewarm", daemon=True).start()

    def _prewarm(self) -> None:
        """Render the frames people see first (rendering is pure Python; the cache makes
        the pill appear and animate without waiting). Runs once, in the background."""
        try:
            for look in prewarm_looks():
                if self._panel is None:
                    return
                ov.render(look, self._scale, self._text)
        except Exception:
            log.debug("overlay prewarm failed", exc_info=True)

    def _destroy(self) -> None:
        panel, self._panel = self._panel, None
        if panel is None:
            return
        panel.stop_timer()
        panel.hide()
        panel.close()
        self._timer = self._visible = False
        self._images.clear()
        self._last = None

    def _image(self, look: Any, scale: float) -> tuple[Any, int, int]:
        key = (look, scale)
        cached = self._images.get(key)
        if cached is None:
            img = ov.render(look, scale, self._text)
            assert self._panel is not None
            cached = (self._panel.make_image(img), img.width, img.height)
            self._images[key] = cached
            if len(self._images) > IMAGE_CACHE:
                self._images.popitem(last=False)
        else:
            self._images.move_to_end(key)
        return cached

    def _tick(self) -> None:
        panel = self._panel
        if panel is None:
            return
        with self._lock:
            state, started, message, fresh = self._state, self._started, self._message, self._fresh
            self._fresh = False
        frame = ov.frame_at(state, self._clock() - started, self._level, message)
        if frame.look is None or frame.finished:
            if self._visible:
                panel.hide()
                self._visible = False
            if self._timer:
                panel.stop_timer()
                self._timer = False
            self._last = None
            return
        if fresh or not self._visible:
            visible, self._scale = panel.screen()
            self._visible_frame = visible
        scale = self._scale
        image, w_px, h_px = self._image(frame.look, scale)
        w_pt, h_pt = w_px / scale, h_px / scale
        x, y = place_top_centre(self._visible_frame, w_pt, h_pt)
        rect = (x, y, w_pt, h_pt)
        key = (frame.look, scale, frame.alpha)
        if key != self._last or rect != self._frame or not self._visible:
            panel.show(image, rect, scale, frame.alpha / 255.0)
            self._last, self._frame = key, rect
            self._visible = True
        if not self._timer:
            panel.start_timer(self._interval, self._tick)
            self._timer = True


# ------------------------------------------------------------------- the real panel


class CocoaPanel:
    """The NSPanel (create on the main thread)."""

    def __init__(self) -> None:
        from AppKit import NSColor, NSPanel, NSView

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (10, 10)),
            STYLE_BORDERLESS | STYLE_NONACTIVATING_PANEL,
            BACKING_BUFFERED,
            False,
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)  # the frames draw their own soft shadow
        panel.setIgnoresMouseEvents_(True)
        panel.setLevel_(STATUS_WINDOW_LEVEL)
        panel.setCollectionBehavior_(
            JOIN_ALL_SPACES | STATIONARY | IGNORES_CYCLE | FULLSCREEN_AUXILIARY
        )
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setReleasedWhenClosed_(False)
        view = NSView.alloc().initWithFrame_(((0, 0), (10, 10)))
        try:  # a layer-hosting view: our own layer exists at once (no lazy backing layer)
            from Quartz import CALayer

            view.setLayer_(CALayer.layer())
        except Exception:
            log.debug("couldn't make the overlay's layer; AppKit will", exc_info=True)
        view.setWantsLayer_(True)
        panel.setContentView_(view)
        self._panel = panel
        self._view = view
        self._timer: Any = None

    def screen(self) -> tuple[Rect, float]:
        from AppKit import NSScreen

        screen = NSScreen.mainScreen() or (NSScreen.screens() or [None])[0]
        if screen is None:
            return (0.0, 0.0, 1440.0, 875.0), 2.0
        frame = screen.visibleFrame()
        rect = (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height)
        return rect, float(screen.backingScaleFactor() or 2.0)

    def make_image(self, img: Any) -> Any:
        from shoutabout.mac.cocoa import cgimage_from_bgra

        return cgimage_from_bgra(img.width, img.height, ov.premultiplied_bgra(img))

    def show(self, image: Any, frame: Rect, scale: float, alpha: float) -> None:
        x, y, w, h = frame
        self._panel.setFrame_display_(((x, y), (w, h)), False)
        layer = self._view.layer()
        if layer is not None:
            layer.setContentsScale_(scale)
            layer.setContents_(image)
        self._panel.setAlphaValue_(max(0.0, min(1.0, alpha)))
        if not self._panel.isVisible():
            self._panel.orderFrontRegardless()

    def hide(self) -> None:
        self._panel.orderOut_(None)

    def start_timer(self, interval: float, tick: Callable[[], None]) -> None:
        from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

        self.stop_timer()

        def fire(_timer: Any) -> None:
            try:
                tick()
            except Exception:
                log.exception("overlay frame failed")

        timer = NSTimer.timerWithTimeInterval_repeats_block_(interval, True, fire)
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self._timer = timer

    def stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def close(self) -> None:
        self.stop_timer()
        self._panel.orderOut_(None)
        self._panel.close()


# ----------------------------------------------------------------------- text


@lru_cache(maxsize=64)
def mac_text_mask(text: str, px: int) -> ov.TextMask | None:
    """``text`` in the bold system font, ``px`` high, as a coverage mask (white on black
    in a grey bitmap). None off macOS or if drawing fails (the pill then has no words)."""
    import sys

    if sys.platform != "darwin" or not text:
        return None
    try:
        return _draw_text_mask(text, px)
    except Exception:
        log.debug("overlay text rendering failed", exc_info=True)
        return None


def _draw_text_mask(text: str, px: int) -> ov.TextMask | None:
    import math

    import Quartz
    from AppKit import (
        NSAttributedString,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSGraphicsContext,
    )

    font = NSFont.boldSystemFontOfSize_(px)
    attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.whiteColor()}
    string = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    size = string.size()
    w, h = int(math.ceil(size.width)) + 2, int(math.ceil(size.height)) + 2
    space = Quartz.CGColorSpaceCreateDeviceGray()
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, 0, space, Quartz.kCGImageAlphaNone)
    if ctx is None:
        return None
    Quartz.CGContextSetGrayFillColor(ctx, 0.0, 1.0)
    Quartz.CGContextFillRect(ctx, ((0, 0), (w, h)))
    gc = NSGraphicsContext.graphicsContextWithCGContext_flipped_(ctx, False)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(gc)
        string.drawAtPoint_((1, 1))
        gc.flushGraphics()
    finally:
        NSGraphicsContext.restoreGraphicsState()
    image = Quartz.CGBitmapContextCreateImage(ctx)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image)))
    stride = int(Quartz.CGImageGetBytesPerRow(image))
    rows = [data[r * stride : r * stride + w] for r in range(h)]
    return ov.TextMask(w, h, b"".join(rows))


def create_mac_overlay(enabled: bool = True) -> ov.OverlayLike:
    """The real pill on macOS when enabled, else :class:`shoutabout.overlay.NullOverlay`."""
    import sys

    if enabled and sys.platform == "darwin":
        return MacOverlay()
    return ov.NullOverlay()
