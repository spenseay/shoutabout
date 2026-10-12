"""The brand mouth in the Mac windows, drawn by AppKit at the screen's own resolution.

Tk 8.6 (the Mac build has 8.6.14) can't draw sharp artwork on a Retina screen. A PhotoImage
is drawn one pixel per point (scaled up 2x: soft), and a canvas rounds every polygon corner
to a whole point before Core Graphics draws it, so the mouth's finely sampled curves came
out as staircases of 1-point steps: it looked pixelated. (Tk 9's ``nsimage`` would do; 8.6
has nothing like it.)

So each :class:`shoutabout.ui_kit.Mouth` gets an ``NSImageView`` laid exactly over its canvas
in the window's content view, showing the brand's own rendering (:func:`image`) at the
window's backing scale: one image pixel per screen pixel. While the view shows, the canvas
hides its polygons; if the view can't attach (no window found, an AppKit error), the
polygons are what you see.

The view follows its canvas. Tk reports <Map>, <Unmap> and <Configure> of every widget in a
window to the window's own binding as well, so one binding per window (:class:`_Group`)
re-places every mouth in it when a page is shown or hidden, or the window is resized or
moved (to a screen with another scale, say). <Destroy> of the canvas removes the view.
Everything runs on the main thread (the window process's Tk thread).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from shoutabout import brand

log = logging.getLogger(__name__)

RETRY_MS = 100  # looking again for the NSWindow of a window that isn't on screen yet
RETRIES = 30
TOLERANCE_PT = 3.0  # how close a Tk window and its NSWindow's content must be
SAMPLES = 8  # the rasterizer's sub-scanlines per pixel (brand's default)
IMAGE_SCALE_AXES = 1  # NSImageScaleAxesIndependently: the image fills the view exactly

Rect = tuple[float, float, float, float]  # x, y, width, height


# ------------------------------------------------------------------- pure


def image(width_pt: int, height_pt: int, opening: float, tongue: bool, scale: float) -> brand.Image:
    """The mouth for a canvas of ``width_pt`` x ``height_pt`` points (:func:`ui_kit.mouth_box`)
    at ``scale`` pixels per point, centred like the canvas's polygons, on transparency."""
    w_px = max(1, round(width_pt * scale))
    h_px = max(1, round(height_pt * scale))
    w = width_pt * scale
    h = w * opening / brand.REF_W
    layers = brand.mouth_layers(0, (h_px - h) / 2, w, opening, tongue=brand.RED if tongue else None)
    return brand.rasterize(w_px, h_px, layers, samples=SAMPLES)


def content_frame(x: float, y: float, w: float, h: float, content_h: float, flipped: bool) -> Rect:
    """A widget at (x, y) from the window's top left, in its content view's coordinates."""
    return (x, y if flipped else content_h - y - h, w, h)


def tk_rect(cocoa: Rect, primary_h: float) -> Rect:
    """A Cocoa screen rectangle (origin at the bottom left of the primary screen) in Tk's
    screen coordinates (origin at its top left)."""
    x, y, w, h = cocoa
    return (x, primary_h - y - h, w, h)


def pick_window(target: Rect, title: str, windows: list[tuple[Rect, str]]) -> int | None:
    """Which of ``windows`` (content rectangle in Tk coordinates, title) is the Tk window at
    ``target``: the nearest within TOLERANCE_PT on every side (the title breaks ties), else
    the only one with that title, else None."""
    best, best_key = None, math.inf
    for i, (rect, name) in enumerate(windows):
        d = max(abs(a - b) for a, b in zip(rect, target, strict=True))
        key = d - (TOLERANCE_PT if name == title else 0.0)
        if d <= TOLERANCE_PT and key < best_key:
            best, best_key = i, key
    if best is not None:
        return best
    same = [i for i, (_rect, name) in enumerate(windows) if name == title]
    return same[0] if len(same) == 1 else None


# ------------------------------------------------------------------- AppKit


def _rect(r: Any) -> Rect:
    return (float(r.origin.x), float(r.origin.y), float(r.size.width), float(r.size.height))


def _nsimage(width_pt: int, height_pt: int, opening: float, tongue: bool, scale: float) -> Any:
    from AppKit import NSImage

    from shoutabout.mac.cocoa import cgimage_from_bgra
    from shoutabout.overlay import premultiplied_bgra

    img = image(width_pt, height_pt, opening, tongue, scale)
    cg = cgimage_from_bgra(img.width, img.height, premultiplied_bgra(img))
    return NSImage.alloc().initWithCGImage_size_(cg, (width_pt, height_pt))


def _image_view(frame: Rect) -> Any:
    from AppKit import NSImageView

    x, y, w, h = frame
    view = NSImageView.alloc().initWithFrame_(((x, y), (w, h)))
    view.setImageScaling_(IMAGE_SCALE_AXES)
    view.setImageFrameStyle_(0)  # NSImageFrameNone
    view.setEditable_(False)
    try:  # the window's text says what it needs to; the mouth is decoration
        view.setAccessibilityElement_(False)
    except Exception:
        pass
    return view


def find_window(top: Any) -> tuple[Any, str]:
    """The NSWindow of the Tk toplevel ``top`` (or None) and what was compared."""
    from AppKit import NSApplication, NSScreen

    screens = NSScreen.screens()
    if not screens:
        return None, "no screens"
    primary_h = float(screens[0].frame().size.height)
    target = (
        float(top.winfo_rootx()),
        float(top.winfo_rooty()),
        float(top.winfo_width()),
        float(top.winfo_height()),
    )
    title = str(top.wm_title())
    windows = [w for w in NSApplication.sharedApplication().windows() if w.isVisible()]
    seen = [
        (tk_rect(_rect(w.contentRectForFrameRect_(w.frame())), primary_h), str(w.title() or ""))
        for w in windows
    ]
    i = pick_window(target, title, seen)
    if i is None:
        return None, f"no window at {target} titled {title!r} among {seen}"
    return windows[i], ""


class NativeMouth:
    """One mouth's AppKit view (see the module docstring). :meth:`show` is the Mouth's hook:
    True while the view shows the mouth (the canvas then hides its polygons)."""

    def __init__(self, mouth: Any) -> None:
        self.mouth = mouth
        self.canvas = mouth.widget
        self.look: tuple[float, bool] | None = None
        self.view: Any = None
        self.window: Any = None
        self.scale = 0.0
        self.frame: Rect | None = None
        self.covering = False
        self.closed = False
        self.problem = "not placed yet"
        self._shown: tuple[float, bool, float] | None = None
        self._images: dict[tuple[float, bool, float], Any] = {}
        self._tries = 0
        self.canvas.bind("<Destroy>", self._destroyed, add="+")
        self._group = _Group.join(self)

    # -- the Mouth's side

    def show(self, opening: float, tongue: bool) -> bool:
        self.look = (opening, tongue)
        if self.covering:
            try:
                self._draw()
            except Exception:
                self._fail("drawing failed")
        return self.covering

    # -- following the canvas

    def sync(self) -> None:
        """Place the view over the canvas, or hide it with the canvas's page."""
        if self.closed:
            return
        try:
            covering = self._place()
        except Exception:
            self._fail("placing failed")
            return
        if covering != self.covering:
            self.covering = covering
            self.mouth.redraw()

    def _place(self) -> bool:
        canvas = self.canvas
        if not canvas.winfo_exists():
            self.close()
            return False
        if not canvas.winfo_viewable():
            if self.view is not None:
                self.view.setHidden_(True)
            self.problem = "not on screen"
            return False
        top = canvas.winfo_toplevel()
        if self.window is None:
            self.window, self.problem = find_window(top)
            if self.window is None:
                self._retry()
                return False
        content = self.window.contentView()
        w, h = self.mouth.width, self.mouth.height
        frame = content_frame(
            canvas.winfo_rootx() - top.winfo_rootx(),
            canvas.winfo_rooty() - top.winfo_rooty(),
            w,
            h,
            float(content.frame().size.height),
            bool(content.isFlipped()),
        )
        if self.view is None:
            self.view = _image_view(frame)
        if self.view.superview() is None:
            content.addSubview_(self.view)
        if frame != self.frame:
            x, y, fw, fh = frame
            self.view.setFrame_(((x, y), (fw, fh)))
            self.frame = frame
        self.scale = float(self.window.backingScaleFactor() or 2.0)
        self._draw()
        self.view.setHidden_(False)
        self.problem = ""
        return True

    def _draw(self) -> None:
        if self.view is None or self.look is None or not self.scale:
            return
        key = (*self.look, self.scale)
        if key == self._shown:
            return
        img = self._images.get(key)
        if img is None:
            img = _nsimage(self.mouth.width, self.mouth.height, *self.look, self.scale)
            self._images[key] = img
        self.view.setImage_(img)
        self._shown = key

    def _retry(self) -> None:
        if self._tries < RETRIES:
            self._tries += 1
            self.canvas.after(RETRY_MS, self.sync)
        elif self._tries == RETRIES:
            self._tries += 1
            log.info("the mouth stays a canvas drawing: %s", self.problem)

    def _fail(self, what: str) -> None:
        log.debug("native mouth: %s", what, exc_info=True)
        self.problem = what
        self.close()
        if self.covering:
            self.covering = False
            self.mouth.redraw()

    def _destroyed(self, event: Any) -> None:
        if event.widget is self.canvas:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._group.leave(self)
        if self.view is not None:
            try:
                self.view.removeFromSuperview()
            except Exception:
                log.debug("couldn't remove the mouth's view", exc_info=True)
        self._images.clear()


class _Group:
    """The mouths in one Tk window and that window's one binding (see the module
    docstring). The binding is never removed: tkinter's unbind can drop other handlers of
    the same event, and an empty group's handler does nothing."""

    _groups: dict[str, _Group] = {}

    def __init__(self, top: Any) -> None:
        self.top = top
        self.mouths: list[NativeMouth] = []
        self.pending = False
        for sequence in ("<Map>", "<Unmap>", "<Configure>"):
            top.bind(sequence, self.poke, add="+")

    @classmethod
    def join(cls, mouth: NativeMouth) -> _Group:
        top = mouth.canvas.winfo_toplevel()
        group = cls._groups.get(str(top))
        if group is None or group.top is not top:
            group = cls._groups[str(top)] = cls(top)
        group.mouths.append(mouth)
        group.poke()
        return group

    def leave(self, mouth: NativeMouth) -> None:
        if mouth in self.mouths:
            self.mouths.remove(mouth)

    def poke(self, _event: Any = None) -> None:
        if self.pending or not self.mouths:
            return
        self.pending = True
        try:
            self.top.after_idle(self.run)
        except Exception:  # the window is going away
            self.pending = False

    def run(self) -> None:
        self.pending = False
        for mouth in list(self.mouths):
            mouth.sync()
