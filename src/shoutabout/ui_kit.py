"""Brand building blocks for the Tk windows (setup, settings, the pairing prompt).

Shout Yellow ground, black type, Tongue Red accents; Anton for headlines and Archivo for
text when they're installed, otherwise the closest Windows fonts (Bahnschrift Condensed is
a condensed heavy face like Anton and ships with Windows 10+). Everything here runs on the
Tk thread; ``tkinter`` is imported lazily so importing this module works headless.

Pure helpers (font choice, sizes, mouth-frame heights) are kept free of Tk for tests.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import math
import sys
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from typing import Any

from shoutabout import brand

log = logging.getLogger(__name__)

YELLOW = "#FFE600"
BLACK = "#0A0A0A"
RED = "#FF1A1A"
WHITE = "#FFFFFF"
PAPER = "#FFF8C4"  # a lighter yellow for cards
MUTED = "#4A4A4A"  # secondary text (7:1 on yellow)
GREY = "#8C8C84"

TITLE_FONTS = (
    "Anton",
    "Bahnschrift SemiBold Condensed",
    "Bahnschrift Condensed",
    "Impact",
    "Arial Black",
    "Segoe UI Black",
    "Segoe UI",
    "Arial",
)
BODY_FONTS = ("Archivo", "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Arial")
# Faces that are already heavy: asking Tk for "bold" on top would fake-embolden them.
HEAVY_FACES = {"Anton", "Impact", "Arial Black", "Segoe UI Black"}


def pick_font(available: Iterable[str], preferred: Sequence[str]) -> str:
    """The first of ``preferred`` that is installed (case-insensitive), else Tk's default."""
    installed = {name.casefold(): name for name in available}
    for name in preferred:
        if name.casefold() in installed:
            return installed[name.casefold()]
    return "TkDefaultFont"


class Fonts:
    """The type scale, from the installed families."""

    def __init__(self, families: Iterable[str]) -> None:
        families = list(families)
        title = pick_font(families, TITLE_FONTS)
        body = pick_font(families, BODY_FONTS)
        weight = () if title in HEAVY_FACES else ("bold",)
        self.title_family, self.body_family = title, body
        self.display = (title, 34, *weight)
        self.title = (title, 26, *weight)
        self.subtitle = (title, 18, *weight)
        self.big = (body, 15, "bold")
        self.body = (body, 12)
        self.body_bold = (body, 12, "bold")
        self.small = (body, 10)
        self.small_bold = (body, 10, "bold")
        self.code = (title, 40, *weight)
        self.entry = (body, 16)


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        # Per-monitor v2; harmless (fails) if the process already chose an awareness.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # type: ignore[attr-defined]


def cancel_jobs(widget: Any, jobs: dict[str, Any]) -> None:
    """Cancel a page's pending ``after`` jobs as it closes (a job left behind fires on a
    destroyed widget, and Tk prints "invalid command name")."""
    for job in jobs.values():
        with contextlib.suppress(Exception):
            widget.after_cancel(job)
    jobs.clear()


def ui_scale(widget: Any) -> float:
    """96-dpi pixels -> screen pixels (fonts scale by themselves)."""
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


# ------------------------------------------------------------------- images
#
# How the mouth stays sharp at any scale:
#
# * macOS: Tk draws a PhotoImage one pixel per *point*, so on a Retina screen every bitmap is
#   scaled up 2x (soft edges). The canvas there is antialiased and drawn at the screen's
#   backing scale, so the mouth is vector shapes on a canvas (brand.mouth_polygons: the
#   brand's own outlines, curves sampled finely), sharp at 1x, 2x or 3x.
# * Windows: Tk's canvas isn't antialiased (GDI), so the mouth stays a supersampled bitmap,
#   rendered at the real size in screen pixels (callers pass ``N * ui_scale``: the process is
#   DPI aware, so a bitmap pixel is a screen pixel at 100, 125, 150 or 200 %).

VECTOR_MOUTH = sys.platform == "darwin"


def photo(master: Any, image: brand.Image) -> Any:
    import tkinter as tk

    data = brand.png_bytes(image.width, image.height, image.rgba)
    return tk.PhotoImage(master=master, data=base64.b64encode(data))


def talk_heights(n: int = 12) -> list[float]:
    """Mouth heights (px of 130) for one saTalk loop, starting wide open."""
    return brand.talk_frames(n)


def mouth_box(width: int) -> tuple[int, int]:
    """The mouth's box: as wide as asked, as tall as the open mouth (every frame fits it, so
    the art doesn't jump while it talks)."""
    width = max(1, int(width))
    return width, max(1, math.ceil(width * brand.OPEN / brand.REF_W))


def _with_tongue(opening: float, talking: bool) -> bool:
    """Talking frames show the tongue; of the stills only the open mouth does (ajar: none)."""
    return talking or opening >= brand.OPEN


# Bitmaps (Windows)


def _boxed(width: int, opening: float, tongue: bool) -> brand.Image:
    w, full_h = mouth_box(width)
    img = brand.mouth_image(w, opening, tongue=brand.RED if tongue else None, samples=4)
    return brand.compose(w, full_h, None, [(img, 0, (full_h - img.height) // 2)])


@lru_cache(maxsize=8)
def _mouth_images(width: int, n: int) -> tuple[brand.Image, ...]:
    """Talking-mouth frames, all padded to the open height so the art doesn't jump."""
    return tuple(_boxed(width, h, True) for h in talk_heights(n))


@lru_cache(maxsize=8)
def _still_mouth(width: int, opening: float) -> brand.Image:
    return _boxed(width, opening, _with_tongue(opening, False))


# Vectors (macOS)

Shapes = tuple[tuple[tuple[float, ...], str], ...]  # ((flat x, y coordinates), Tk colour)


def tk_color(rgb: brand.RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


@lru_cache(maxsize=128)
def mouth_shapes(width: int, opening: float, tongue: bool) -> Shapes:
    """One still of the mouth for a canvas the size of :func:`mouth_box`: filled polygons,
    bottom to top, centred in the box like the bitmaps."""
    w, full_h = mouth_box(width)
    h = w * opening / brand.REF_W
    polygons = brand.mouth_polygons(
        0, (full_h - h) / 2, w, opening, tongue=brand.RED if tongue else None
    )
    return tuple(
        (tuple(round(c, 3) for point in points for c in point), tk_color(color))
        for points, color in polygons
    )


def _mouth_vectors(width: int, n: int) -> tuple[Shapes, ...]:
    return tuple(mouth_shapes(width, h, True) for h in talk_heights(n))


class Mouth:
    """The brand mouth: still (ajar, or open with its tongue) or talking (saTalk, 12 frames x
    133 ms). Vector shapes on a canvas on macOS, a bitmap label on Windows (see above);
    ``widget`` (alias ``label``) is the Tk widget either way."""

    def __init__(
        self,
        parent: Any,
        width: int,
        bg: str = YELLOW,
        frames: int = 12,
        *,
        vector: bool | None = None,
    ) -> None:
        import tkinter as tk

        self.vector = VECTOR_MOUTH if vector is None else bool(vector)
        self.width, self.height = mouth_box(width)
        self._frames: list[Any] = []
        self._still: Any = None
        self._ajar: Any = None
        self._items: list[Any] = []
        self._job: Any = None
        self._i = 0
        self._interval = round(brand.TALK_DURATION_MS / frames)
        if self.vector:
            self.widget = tk.Canvas(
                parent,
                width=self.width,
                height=self.height,
                bg=bg,
                bd=0,
                highlightthickness=0,
                takefocus=0,
            )
        else:
            self.widget = tk.Label(parent, bg=bg, bd=0)
        self.label = self.widget
        try:  # cosmetic: never let artwork stop a window from opening
            if self.vector:
                self._frames = list(_mouth_vectors(self.width, frames))
                self._still = mouth_shapes(self.width, brand.OPEN, True)
                self._ajar = mouth_shapes(self.width, brand.AJAR, False)
            else:
                self._frames = [photo(parent, img) for img in _mouth_images(self.width, frames)]
                self._still = photo(parent, _still_mouth(self.width, brand.OPEN))
                self._ajar = photo(parent, _still_mouth(self.width, brand.AJAR))
            self._show(self._ajar)
        except Exception:
            log.debug("couldn't render the mouth", exc_info=True)

    def pack(self, **kw: Any) -> Mouth:
        self.widget.pack(**kw)
        return self

    def grid(self, **kw: Any) -> Mouth:
        self.widget.grid(**kw)
        return self

    def _show(self, frame: Any) -> None:
        if not self.vector:
            self.widget.configure(image=frame)
            return
        canvas = self.widget
        while len(self._items) < len(frame):
            self._items.append(canvas.create_polygon(0, 0, 0, 0, 0, 0, outline="", width=0))
        for i, item in enumerate(self._items):
            if i < len(frame):
                coords, color = frame[i]
                canvas.coords(item, *coords)
                canvas.itemconfigure(item, fill=color, state="normal")
            else:
                canvas.itemconfigure(item, state="hidden")

    def talk(self) -> None:
        if self._job is None and self._frames:
            self._tick()

    def _tick(self) -> None:
        try:
            self._show(self._frames[self._i % len(self._frames)])
            self._i += 1
            self._job = self.widget.after(self._interval, self._tick)
        except Exception:  # the window closed under us
            self._job = None

    def stop(self, open_mouth: bool = False) -> None:
        if self._job is not None:
            with contextlib.suppress(Exception):
                self.widget.after_cancel(self._job)
            self._job = None
        frame = self._still if open_mouth else self._ajar
        if frame is not None:
            with contextlib.suppress(Exception):
                self._show(frame)


def app_icon_photo(master: Any, size: int = 44) -> Any:
    return photo(master, brand.app_icon("primary", size, rounded=True, samples=4))


WINDOW_ICON_PX = 32  # Windows' large icon at 100 % (SM_CXICON)


def set_window_icon(window: Any) -> Any:
    """The window's icon (title bar and taskbar on Windows), at the screen's own icon size:
    Tk hands Windows one image for both, so it's the large one (32 px at 100 %, 48 at 150 %)
    and the title bar's is an exact half of it. On macOS the window process keeps the Dock
    icon instead (:mod:`shoutabout.mac.tkmac`). Returns the photo, to keep alive."""
    size = max(16, round(WINDOW_ICON_PX * ui_scale(window)))
    icon = app_icon_photo(window, size)
    window.iconphoto(False, icon)
    return icon


def qr_photo(master: Any, text: str, scale: int = 5) -> Any:
    """A QR code (black on white) as a Tk PhotoImage, drawn by segno (no Pillow)."""
    import io
    import tkinter as tk

    import segno

    buf = io.BytesIO()
    segno.make(text, error="m", micro=False).save(buf, kind="png", scale=scale, border=3)
    return tk.PhotoImage(master=master, data=base64.b64encode(buf.getvalue()))


# ------------------------------------------------------------------ widgets


def button(
    parent: Any,
    text: str,
    command: Callable[[], None],
    fonts: Fonts,
    kind: str = "dark",
    *,
    big: bool = False,
) -> Any:
    """A flat brand button: dark (black on yellow text), red, or light."""
    import tkinter as tk

    colors = {
        "dark": (BLACK, YELLOW, "#333333"),
        "red": (RED, BLACK, "#FF4D4D"),
        "light": (PAPER, BLACK, WHITE),
    }[kind]
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=colors[0],
        fg=colors[1],
        activebackground=colors[2],
        activeforeground=colors[1],
        disabledforeground=GREY,
        font=fonts.big if big else fonts.body_bold,
        relief="flat",
        bd=0,
        padx=22 if big else 14,
        pady=10 if big else 6,
        cursor="hand2",
        highlightthickness=0,
    )


def link(parent: Any, text: str, command: Callable[[], None], fonts: Fonts, bg: str = YELLOW):
    """Small underlined text that acts like a button."""
    import tkinter as tk

    label = tk.Label(
        parent,
        text=text,
        font=(fonts.body_family, fonts.small[1], "underline"),
        bg=bg,
        fg=BLACK,
        cursor="hand2",
    )
    label.bind("<Button-1>", lambda _e: command())
    return label


class ProgressBar:
    """A flat black-on-paper bar (ttk's themed bar can't take the brand colours on Windows)."""

    def __init__(self, parent: Any, width: int, height: int = 14, bg: str = YELLOW) -> None:
        import tkinter as tk

        self.width, self.height = width, height
        self.canvas = tk.Canvas(
            parent, width=width, height=height, bg=PAPER, highlightthickness=2, bd=0
        )
        self.canvas.configure(highlightbackground=BLACK)
        self._bar = self.canvas.create_rectangle(0, 0, 0, height, fill=BLACK, width=0)

    def set(self, fraction: float | None) -> None:
        f = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
        # Canvas coordinates start under the 2 px border: cover it at 100 %.
        self.canvas.coords(self._bar, 0, 0, round((self.width + 4) * f), self.height + 4)


class Switch:
    """An on/off pill drawn on a canvas (black when on, with a yellow knob)."""

    def __init__(self, parent: Any, scale: float, bg: str, on: bool = False) -> None:
        import tkinter as tk

        self.w, self.h = round(52 * scale), round(28 * scale)
        self.canvas = tk.Canvas(
            parent, width=self.w, height=self.h, bg=bg, highlightthickness=0, bd=0
        )
        self.canvas.configure(cursor="hand2")
        self.on = on
        self.draw()

    def draw(self) -> None:
        c, w, h = self.canvas, self.w, self.h
        c.delete("all")
        fill = BLACK if self.on else GREY
        r = h / 2
        c.create_oval(0, 0, h, h, fill=fill, width=0)
        c.create_oval(w - h, 0, w, h, fill=fill, width=0)
        c.create_rectangle(r, 0, w - r, h, fill=fill, width=0)
        pad = max(3, round(h * 0.14))
        x0 = w - h + pad if self.on else pad
        knob = YELLOW if self.on else WHITE
        c.create_oval(x0, pad, x0 + h - 2 * pad, h - pad, fill=knob, width=0)

    def set(self, on: bool) -> None:
        self.on = bool(on)
        self.draw()
