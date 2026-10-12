"""The Mac windows' mouth: an AppKit view over the canvas, drawn at the screen's backing
scale (shoutabout.mac.native_mouth). The geometry is pure; the view logic runs against
stand-in AppKit objects here (the real thing is checked by the Mac self-test)."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from shoutabout import brand
from shoutabout import ui_kit as kit
from shoutabout.mac import native_mouth as nm

# ------------------------------------------------------------------- pure


def _rgba(img: brand.Image) -> np.ndarray:
    return np.frombuffer(img.rgba, dtype=np.uint8).reshape(img.height, img.width, 4)


@pytest.mark.parametrize("scale", [1.0, 2.0, 3.0])
@pytest.mark.parametrize("width", [36, 54, 150, 190])
def test_the_image_has_a_pixel_per_screen_pixel(width, scale):
    w, h = kit.mouth_box(width)
    img = nm.image(w, h, brand.OPEN, True, scale)
    assert (img.width, img.height) == (round(w * scale), round(h * scale))


def test_the_image_is_the_brand_mouth_centred_like_the_canvas():
    w, h = kit.mouth_box(190)
    a = _rgba(nm.image(w, h, brand.OPEN, True, 2.0))
    assert a[0, 0, 3] == 0 and a[-1, -1, 3] == 0  # transparent around the mouth
    rows = np.nonzero(a[..., 3].max(axis=1))[0]
    cols = np.nonzero(a[..., 3].max(axis=0))[0]
    assert cols[0] == 0 and cols[-1] == a.shape[1] - 1  # as wide as the box
    assert (rows[0] + rows[-1]) / 2 == pytest.approx((a.shape[0] - 1) / 2, abs=1)
    cx = a.shape[1] // 2
    teeth, throat, tongue = (a[round(a.shape[0] * f), cx, :3] for f in (0.06, 0.45, 0.85))
    assert tuple(teeth) == brand.WHITE
    assert tuple(throat) == brand.BLACK
    assert tuple(tongue) == brand.RED
    ajar = _rgba(nm.image(w, h, brand.AJAR, False, 2.0))
    assert not np.any(np.all(ajar[..., :3] == brand.RED, axis=-1) & (ajar[..., 3] == 255))


def test_edges_are_antialiased_at_the_screen_scale():
    """The point of it all: partly covered pixels along the curves at 2x (not 1-point steps)."""
    w, h = kit.mouth_box(150)
    alpha = _rgba(nm.image(w, h, brand.OPEN, True, 2.0))[..., 3]
    soft = np.count_nonzero((alpha > 0) & (alpha < 255))
    assert soft > 2 * (w + h)  # a soft pixel or more on every row and column of the outline


def test_content_frame_and_screen_coordinates():
    assert nm.content_frame(20, 30, 150, 115, 400, flipped=True) == (20, 30, 150, 115)
    assert nm.content_frame(20, 30, 150, 115, 400, flipped=False) == (20, 255, 150, 115)
    assert nm.tk_rect((100, 200, 300, 400), 900) == (100, 300, 300, 400)


def test_pick_window():
    target = (100.0, 50.0, 400.0, 300.0)
    near = ((101.0, 51.0, 400.0, 300.0), "Other")
    exact = ((100.0, 50.0, 400.0, 300.0), "ShoutAbout")
    far = ((500.0, 50.0, 400.0, 300.0), "ShoutAbout")
    assert nm.pick_window(target, "ShoutAbout", [far, near]) == 1  # by position
    assert nm.pick_window(target, "ShoutAbout", [near, exact]) == 1  # the title breaks ties
    assert nm.pick_window(target, "ShoutAbout", [far]) == 0  # the only one with the title
    assert nm.pick_window(target, "ShoutAbout", [far, far]) is None  # can't tell
    assert nm.pick_window(target, "ShoutAbout", []) is None


# ------------------------------------------------------------------- with stand-ins


class FakeView:
    def __init__(self, frame):
        self.frame, self.hidden, self.image, self.superview_ = frame, False, None, None
        self.images_set = 0

    def setImageScaling_(self, v):
        self.scaling = v

    def setImageFrameStyle_(self, v):
        pass

    def setEditable_(self, v):
        pass

    def setAccessibilityElement_(self, v):
        pass

    def superview(self):
        return self.superview_

    def setFrame_(self, rect):
        (x, y), (w, h) = rect
        self.frame = (x, y, w, h)

    def setHidden_(self, hidden):
        self.hidden = hidden

    def setImage_(self, image):
        self.image = image
        self.images_set += 1

    def removeFromSuperview(self):
        self.superview_ = None


class FakeContent:
    def __init__(self, height, flipped):
        self.height, self.flipped, self.subviews = height, flipped, []

    def frame(self):
        return SimpleNamespace(size=SimpleNamespace(width=500, height=self.height))

    def isFlipped(self):
        return self.flipped

    def addSubview_(self, view):
        self.subviews.append(view)
        view.superview_ = self


def _ns_rect(x, y, w, h):
    return SimpleNamespace(
        origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=w, height=h)
    )


class FakeWindow:
    def __init__(self, cocoa_rect, title, scale=2.0, flipped=False):
        self.rect, self.title_, self.scale = cocoa_rect, title, scale
        self.content = FakeContent(cocoa_rect[3], flipped)

    def isVisible(self):
        return True

    def frame(self):
        return "frame"

    def contentRectForFrameRect_(self, _frame):
        return _ns_rect(*self.rect)

    def title(self):
        return self.title_

    def contentView(self):
        return self.content

    def backingScaleFactor(self):
        return self.scale


class FakeWidget:
    """Just enough of a Tk widget: geometry, viewability, bindings, after."""

    def __init__(self, root_xy, size, top=None, title="ShoutAbout"):
        self.root_xy, self.size, self.top = root_xy, size, top or self
        self.viewable, self.exists, self.title = True, True, title
        self.bindings: dict[str, list] = {}
        self.idle: list = []
        self.later: list = []

    def winfo_rootx(self):
        return self.root_xy[0]

    def winfo_rooty(self):
        return self.root_xy[1]

    def winfo_width(self):
        return self.size[0]

    def winfo_height(self):
        return self.size[1]

    def winfo_viewable(self):
        return self.viewable

    def winfo_exists(self):
        return self.exists

    def winfo_toplevel(self):
        return self.top

    def wm_title(self):
        return self.title

    def bind(self, sequence, fn, add=None):
        self.bindings.setdefault(sequence, []).append(fn)

    def after_idle(self, fn):
        self.idle.append(fn)

    def after(self, ms, fn):
        self.later.append(fn)

    def fire(self, sequence, widget=None):
        for fn in self.bindings.get(sequence, []):
            fn(SimpleNamespace(widget=widget or self))

    def run_idle(self):
        while self.idle:
            self.idle.pop(0)()


class FakeMouth:
    def __init__(self, widget, width=150):
        self.widget = widget
        self.width, self.height = kit.mouth_box(width)
        self.redraws = 0

    def redraw(self):
        self.redraws += 1


@pytest.fixture
def appkit(monkeypatch):
    """Stand-in AppKit (one screen 900 pt high) and image making that records its calls."""
    windows: list[FakeWindow] = []
    made: list[tuple] = []
    mod = types.ModuleType("AppKit")
    mod.NSScreen = SimpleNamespace(
        screens=lambda: [SimpleNamespace(frame=lambda: _ns_rect(0, 0, 1440, 900))]
    )
    mod.NSApplication = SimpleNamespace(
        sharedApplication=lambda: SimpleNamespace(windows=lambda: windows)
    )
    mod.NSImageView = SimpleNamespace(
        alloc=lambda: SimpleNamespace(initWithFrame_=lambda r: FakeView(r))
    )
    monkeypatch.setitem(sys.modules, "AppKit", mod)
    monkeypatch.setattr(nm, "_nsimage", lambda *args: made.append(args) or ("image", *args))
    monkeypatch.setattr(nm._Group, "_groups", {})
    return SimpleNamespace(windows=windows, made=made)


def _window_and_canvas(appkit, flipped=False, scale=2.0):
    # A Tk window whose content is at (100, 50) in Tk's coordinates, 500 x 400 points:
    # in Cocoa's, its bottom is at 900 - 50 - 400 = 450.
    top = FakeWidget((100, 50), (500, 400))
    appkit.windows.append(FakeWindow((100, 450, 500, 400), "ShoutAbout", scale, flipped))
    canvas = FakeWidget((130, 90), kit.mouth_box(150), top=top)
    return top, canvas


def test_the_view_goes_over_the_canvas_at_the_screen_scale(appkit):
    top, canvas = _window_and_canvas(appkit)
    mouth = FakeMouth(canvas)
    native = nm.NativeMouth(mouth)
    assert native.show(brand.AJAR, False) is False  # not on screen yet: the polygons show
    top.run_idle()
    assert native.covering and mouth.redraws == 1  # the canvas hides its polygons
    view = native.view
    w, h = mouth.width, mouth.height
    # 30, 40 from the window's top left; Cocoa's y counts from the content's bottom.
    assert view.frame == (30, 400 - 40 - h, w, h)
    assert view.superview() is appkit.windows[0].content and not view.hidden
    assert view.scaling == nm.IMAGE_SCALE_AXES
    assert appkit.made == [(w, h, brand.AJAR, False, 2.0)]
    assert view.image == ("image", w, h, brand.AJAR, False, 2.0)
    assert native.show(brand.OPEN, True) is True
    assert view.image[3:] == (brand.OPEN, True, 2.0)
    native.show(brand.AJAR, False)  # cached: no new rendering
    assert len(appkit.made) == 2


def test_a_flipped_content_view(appkit):
    top, canvas = _window_and_canvas(appkit, flipped=True)
    native = nm.NativeMouth(FakeMouth(canvas))
    top.run_idle()
    assert native.view.frame[:2] == (30, 40)


def test_it_follows_the_page_and_the_window(appkit):
    top, canvas = _window_and_canvas(appkit)
    mouth = FakeMouth(canvas)
    native = nm.NativeMouth(mouth)
    native.show(brand.AJAR, False)
    top.run_idle()
    canvas.viewable = False  # another page: the page's frame is unmapped
    top.fire("<Unmap>")
    top.run_idle()
    assert native.view.hidden and not native.covering and mouth.redraws == 2
    canvas.viewable = True
    canvas.root_xy = (150, 90)  # back, somewhere else
    top.fire("<Map>")
    top.run_idle()
    assert not native.view.hidden and native.covering and native.view.frame[0] == 50
    appkit.windows[0].scale = 1.0  # dragged to a 1x screen
    top.fire("<Configure>")
    top.fire("<Configure>")  # many events, one placement
    assert len(top.idle) == 1
    top.run_idle()
    assert appkit.made[-1][-1] == 1.0
    canvas.fire("<Destroy>", widget=canvas)
    assert native.closed and native.view.superview() is None
    top.fire("<Configure>")
    assert top.idle == []  # nothing left to place


def test_no_window_found_keeps_the_polygons(appkit):
    top = FakeWidget((100, 50), (500, 400), title="Somewhere else")
    canvas = FakeWidget((130, 90), kit.mouth_box(150), top=top)
    mouth = FakeMouth(canvas)
    native = nm.NativeMouth(mouth)
    native.show(brand.AJAR, False)
    top.run_idle()
    assert not native.covering and native.view is None and mouth.redraws == 0
    assert "no window at" in native.problem
    for _ in range(nm.RETRIES + 5):  # it looks again a few times, then gives up quietly
        if canvas.later:
            canvas.later.pop(0)()
    assert canvas.later == [] and native._tries == nm.RETRIES + 1


def test_an_appkit_error_falls_back_to_the_polygons(appkit, monkeypatch):
    top, canvas = _window_and_canvas(appkit)
    mouth = FakeMouth(canvas)
    native = nm.NativeMouth(mouth)
    native.show(brand.AJAR, False)
    top.run_idle()
    assert native.covering

    def broken(*_args):
        raise RuntimeError("no Quartz")

    monkeypatch.setattr(nm, "_nsimage", broken)
    assert native.show(brand.OPEN, True) is False
    assert native.closed and not native.covering and native.view.superview() is None
    assert mouth.redraws == 2


# ------------------------------------------------------------------- the Mouth's hook


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


class FakeNative:
    def __init__(self, mouth):
        self.mouth, self.covering, self.shown = mouth, False, []

    def show(self, opening, tongue):
        self.shown.append((opening, tongue))
        return self.covering


def _polygon_states(canvas):
    return {canvas.itemcget(i, "state") for i in canvas.find_all()}


@pytest.mark.windows
def test_the_mouth_hides_its_polygons_under_the_native_view(tk_root, monkeypatch):
    import tkinter as tk

    monkeypatch.setattr(kit, "native_mouth", FakeNative)
    mouth = kit.Mouth(tk.Frame(tk_root), 150, vector=True).pack()
    native = mouth._native
    assert isinstance(native, FakeNative) and native.mouth is mouth
    assert native.shown == [(brand.AJAR, False)]
    assert _polygon_states(mouth.widget) == {"normal"}
    native.covering = True
    mouth.redraw()
    assert _polygon_states(mouth.widget) == {"hidden"}
    mouth.stop(open_mouth=True)
    assert native.shown[-1] == (brand.OPEN, True) and _polygon_states(mouth.widget) == {"hidden"}
    native.covering = False
    mouth.redraw()
    assert _polygon_states(mouth.widget) == {"normal"}
    bitmap = kit.Mouth(tk.Frame(tk_root), 150, vector=False)
    assert bitmap._native is None  # Windows' bitmap mouth never gets one


@pytest.mark.windows
def test_a_broken_native_view_never_stops_the_mouth(tk_root, monkeypatch):
    import tkinter as tk

    def broken(_mouth):
        raise RuntimeError("no AppKit")

    monkeypatch.setattr(kit, "native_mouth", broken)
    mouth = kit.Mouth(tk.Frame(tk_root), 150, vector=True)
    assert mouth._native is None and _polygon_states(mouth.widget) == {"normal"}
