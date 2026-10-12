"""The brand mouth in the Tk windows: sharp at any scale (on a Mac an AppKit view over a
canvas of the same shapes, tests/test_mac_native_mouth.py; bitmaps at the real pixel size on
Windows), the same art either way, and the window icon at the screen's icon size. Real Tk
runs with --run-windows."""

from __future__ import annotations

import math
import sys
import time

import pytest

from shoutabout import brand
from shoutabout import ui_kit as kit

# ------------------------------------------------------------------- pure


def test_the_vector_mouth_is_for_macos():
    """The canvas (under its AppKit view) is the Mac's; Windows' canvas (GDI) isn't
    antialiased, so it gets bitmaps."""
    assert kit.VECTOR_MOUTH is (sys.platform == "darwin")


@pytest.mark.parametrize("width", [36, 54, 150, 190, 225, 285, 380])
def test_the_box_fits_the_open_mouth_and_nothing_is_cropped(width):
    """The box is as tall as the open mouth's bitmap (rounding it down cropped the top row
    of the teeth at some widths: a flat top)."""
    w, h = kit.mouth_box(width)
    assert (w, h) == (width, math.ceil(width * brand.OPEN / brand.REF_W))
    assert kit._still_mouth(width, brand.OPEN) == brand.mouth_image(width, samples=4)
    for frame in kit._mouth_images(width, 12):
        assert (frame.width, frame.height) == (w, h)  # the art doesn't jump


def test_the_bitmaps_are_the_size_they_are_asked_for():
    """Windows: callers pass N * ui_scale, so the bitmap is in screen pixels and Tk never
    scales it (1:1 at 100, 125, 150 or 200 %)."""
    for base in (190, 150, 54, 36):
        for scale in (1.0, 1.25, 1.5, 2.0):
            width = round(base * scale)
            img = kit._still_mouth(width, brand.AJAR)
            assert img.width == width


def test_the_vector_stills_and_frames():
    w, h = kit.mouth_box(190)
    open_mouth = kit.mouth_shapes(190, brand.OPEN, True)
    ajar = kit.mouth_shapes(190, brand.AJAR, False)
    assert [color for _c, color in open_mouth] == ["#0A0A0A", "#FF1A1A", "#FFFFFF"]
    assert [color for _c, color in ajar] == ["#0A0A0A", "#FFFFFF"]  # no tongue at rest
    talking = [kit.mouth_shapes(190, height, True) for height in kit.talk_heights(12)]
    for shapes in (open_mouth, ajar, *talking):
        xs = [x for coords, _c in shapes for x in coords[0::2]]
        ys = [y for coords, _c in shapes for y in coords[1::2]]
        assert min(xs) >= 0 and max(xs) <= w and min(ys) >= 0 and max(ys) <= h
        assert (min(ys) + max(ys)) / 2 == pytest.approx(h / 2, abs=0.5)  # centred
        assert all(len(coords) >= 6 for coords, _c in shapes)  # a polygon each
        assert min(len(coords) // 2 for coords, _c in shapes[:1]) >= 48  # smooth curves


def test_the_window_icon_is_drawn_at_the_screens_icon_size(monkeypatch):
    """Tk gives Windows one image for the title bar and the taskbar: the large icon's size
    (32 px at 100 %, 48 at 150 %), never a 32 px one Windows scales up."""
    made: list[int] = []
    monkeypatch.setattr(kit, "app_icon_photo", lambda _m, size: made.append(size) or f"p{size}")

    class Window:
        def __init__(self, dpi: float) -> None:
            self.dpi, self.icons = dpi, []

        def winfo_fpixels(self, _what: str) -> float:
            return self.dpi

        def iconphoto(self, default, *images) -> None:
            self.icons.append((default, images))

    for dpi, size in ((96, 32), (120, 40), (144, 48), (192, 64)):
        window = Window(dpi)
        assert kit.set_window_icon(window) == f"p{size}"
        assert window.icons == [(False, (f"p{size}",))]
    assert made == [32, 40, 48, 64]


# ------------------------------------------------------------------ real Tk


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


def _pump(root, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def _visible_polygons(canvas) -> list[tuple[str, int]]:
    return [
        (canvas.itemcget(i, "fill").upper(), len(canvas.coords(i)) // 2)
        for i in canvas.find_all()
        if canvas.type(i) == "polygon" and canvas.itemcget(i, "state") != "hidden"
    ]


@pytest.mark.windows
def test_the_vector_mouth_on_a_canvas(tk_root):
    """The macOS mouth (the same Tk calls on any OS): a canvas the size of the box, at rest
    ajar, open with its tongue, talking through the frames, and still the Mouth API."""
    import tkinter as tk

    frame = tk.Frame(tk_root)
    mouth = kit.Mouth(frame, 190, vector=True)
    assert mouth.pack() is mouth and mouth.label is mouth.widget
    canvas = mouth.widget
    assert canvas.winfo_class() == "Canvas"
    w, h = kit.mouth_box(190)
    assert (int(canvas["width"]), int(canvas["height"])) == (w, h)
    assert int(canvas["highlightthickness"]) == 0 and int(canvas["bd"]) == 0
    assert [f for f, _n in _visible_polygons(canvas)] == ["#0A0A0A", "#FFFFFF"]  # ajar
    mouth.stop(open_mouth=True)
    assert [f for f, _n in _visible_polygons(canvas)] == ["#0A0A0A", "#FF1A1A", "#FFFFFF"]
    assert all(n >= 48 for _f, n in _visible_polygons(canvas))
    before = [canvas.coords(i) for i in canvas.find_all()]
    mouth.talk()
    _pump(tk_root, 0.4)  # two or three frames
    assert mouth._job is not None and mouth._i >= 2
    assert [canvas.coords(i) for i in canvas.find_all()] != before
    mouth.stop()
    assert mouth._job is None
    assert [f for f, _n in _visible_polygons(canvas)] == ["#0A0A0A", "#FFFFFF"]
    grid = kit.Mouth(tk.Frame(tk_root), 36, vector=True)
    assert grid.grid(row=0, column=0) is grid


@pytest.mark.windows
def test_the_bitmap_mouth_in_a_label(tk_root):
    import tkinter as tk

    mouth = kit.Mouth(tk.Frame(tk_root), 150, vector=False).pack()
    assert mouth.widget.winfo_class() == "Label"
    photo_name = str(mouth.widget.cget("image"))  # the label shows a PhotoImage by name
    size = (tk_root.call("image", "width", photo_name), tk_root.call("image", "height", photo_name))
    assert tuple(int(v) for v in size) == kit.mouth_box(150)
    mouth.talk()
    _pump(tk_root, 0.3)
    assert str(mouth.widget.cget("image")) != photo_name
    mouth.stop(open_mouth=True)
    assert str(mouth.widget.cget("image")) == str(mouth._still)
