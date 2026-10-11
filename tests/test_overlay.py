"""The mouth pill: pure rendering, timeline and placement (no window needed), plus one
real-window test (--run-windows)."""

from __future__ import annotations

import math
import sys
import threading
import time

import numpy as np
import pytest

from shoutabout import brand, overlay
from shoutabout.overlay import (
    MARGIN,
    PILL_H,
    PILL_W,
    Frame,
    Look,
    NullOverlay,
    TextMask,
    bitmap,
    first_listening_look,
    frame_at,
    level_opening,
    notice_hold,
    place,
    premultiplied_bgra,
    render,
    short_text,
)


def fake_text(text: str, px: int) -> TextMask:
    """A solid block per character: 0.6 em wide, px high."""
    w, h = max(1, round(len(text) * px * 0.6)), px
    return TextMask(w, h, b"\xff" * (w * h))


def pixels(img: brand.Image) -> np.ndarray:
    return np.frombuffer(img.rgba, dtype=np.uint8).reshape(img.height, img.width, 4)


def test_canvas_size_follows_the_dpi_scale():
    for scale in (1.0, 1.5, 2.0):
        img = render(Look("mouth", 80.0), scale)
        assert img.width == pytest.approx((PILL_W + 2 * MARGIN) * scale, abs=1)
        assert img.height == pytest.approx((PILL_H + 2 * MARGIN) * scale, abs=1)


def test_pill_is_yellow_with_an_ink_border_and_transparent_corners():
    img = render(Look("mouth", None), 2.0)
    px = pixels(img)
    h, w = px.shape[:2]
    assert tuple(px[0, 0]) == (0, 0, 0, 0)  # transparent corner: click-through look
    m = int(MARGIN * 2)
    inside = px[h // 2, m + 12]
    assert tuple(inside[:3]) == brand.YELLOW and inside[3] == 255
    border = px[h // 2, m + 1]
    assert border[3] > 200 and max(border[:3]) < 60  # ink outline


def test_mouth_opens_with_the_level():
    quiet = pixels(render(Look("mouth", level_opening(0.0)), 1.0))
    loud = pixels(render(Look("mouth", level_opening(1.0)), 1.0))

    def dark(px):
        return int(((px[..., :3].max(axis=-1) < 40) & (px[..., 3] > 200)).sum())

    assert dark(loud) > dark(quiet) * 2


def test_level_opening_is_quantised_and_bounded():
    assert level_opening(0.0) == overlay.LISTEN_MIN_OPEN
    assert level_opening(1.0) == pytest.approx(brand.OPEN)
    assert level_opening(5.0) == level_opening(1.0)
    assert level_opening(-1.0) == level_opening(0.0)
    values = {level_opening(i / 1000) for i in range(1001)}
    assert len(values) == overlay.LEVEL_STEPS


def test_the_pill_is_little():
    """Owner feedback: a little mouth at the top (64 x 32 at 100 %, ~70 % of the first one)."""
    assert (PILL_W, PILL_H) == (64.0, 32.0)
    assert pytest.approx(0.52, abs=0.01) == overlay.MOUTH_W / PILL_W


def test_error_pill_is_red_with_white_text_and_grows_with_it():
    short = render(Look("text", tone="red", text="Paused"), 1.0, fake_text)
    long = render(Look("text", tone="red", text="Mic blocked, sorry!"), 1.0, fake_text)
    assert long.width > short.width >= int(PILL_W + 2 * MARGIN)
    px = pixels(long)
    y = px.shape[0] // 2
    row = px[y]
    assert any(tuple(p[:3]) == brand.RED for p in row)
    assert any(tuple(p[:3]) == (255, 255, 255) and p[3] == 255 for p in row)


def test_notice_pill_is_yellow_with_ink_text():
    px = pixels(render(Look("text", text="Getting ready… 42%"), 1.0, fake_text))
    row = px[px.shape[0] // 2]
    assert any(tuple(p[:3]) == brand.YELLOW for p in row)
    assert any(tuple(p[:3]) == brand.BLACK and p[3] == 255 for p in row)


def test_text_pill_without_a_text_renderer_still_renders():
    img = render(Look("text", tone="red", text="No mic"), 1.0, None)
    assert img.width == int(PILL_W + 2 * MARGIN)


def test_frames_are_cached():
    look = Look("mouth", 77.0)
    assert render(look, 1.25) is render(look, 1.25)
    assert bitmap(look, 1.25) is bitmap(look, 1.25)


def test_bitmap_is_the_premultiplied_bgra_of_the_frame():
    look = Look("mouth", 70.0)
    bmp = bitmap(look, 1.5)
    img = render(look, 1.5)
    assert (bmp.width, bmp.height) == (img.width, img.height)
    a = np.frombuffer(bmp.bgra, dtype=np.uint8).astype(int)
    b = np.frombuffer(premultiplied_bgra(img), dtype=np.uint8).astype(int)
    assert np.abs(a - b).max() <= 1  # straight <-> premultiplied rounding only


def test_premultiplied_bgra():
    img = brand.Image(2, 1, bytes([255, 0, 0, 128, 10, 20, 30, 255]))
    out = premultiplied_bgra(img)
    assert out == bytes([0, 0, 128, 128, 30, 20, 10, 255])


# ------------------------------------------------------------------- timeline


def test_listening_is_just_the_mouth_fully_visible_at_once():
    first = frame_at("listening", 0.0, 0.5)
    assert first.alpha == 255 and not first.finished  # no fade-in: it must show instantly
    assert first.look == Look("mouth", level_opening(0.5))
    assert first_listening_look() == frame_at("listening", 0.0).look
    # no glow and no clock, however long you talk: only the mouth follows your voice
    assert {frame_at("listening", t, 0.5).look for t in (0.3, 0.9, 42.0, 599.0)} == {first.look}
    assert (
        frame_at("listening", 2.0, 1.0).look.opening > frame_at("listening", 2.0, 0.0).look.opening
    )


def test_thinking_loops_the_talk_animation():
    looks = [frame_at("thinking", t / 30).look for t in range(48)]
    assert len({look.opening for look in looks}) > 10
    assert frame_at("thinking", 0.0).look == frame_at("thinking", 1.6).look
    assert all(not frame_at("thinking", t).finished for t in (0, 5, 60))


def done_timeline(step: float = 0.005) -> list[tuple[float, Frame]]:
    """(t, frame) every ``step`` s until "done" has run its course."""
    out = []
    for i in range(1000):
        t = i * step
        out.append((t, frame_at("done", t)))
        if out[-1][1].finished:
            return out
    raise AssertionError("done never finishes")


def zip_point(img: brand.Image, scale: float, bx: float, by: float) -> tuple[int, int]:
    """The pixel at (bx, by) of the mouth's 170 x 130 box (unsquashed), in a frame."""
    s = overlay.MOUTH_W * scale / brand.REF_W
    x = img.width / 2 + (bx - brand.REF_W / 2) * s
    y = img.height / 2 + (by - brand.OPEN / 2) * s
    return int(x), int(y)


def any_red(px: np.ndarray) -> bool:
    r, g, b, a = (px[..., i].astype(int) for i in range(4))
    return bool(((r > 200) & (g < 90) & (b < 90) & (a > 200)).any())


def test_done_zips_the_mouth_shut_quickly_without_a_check_mark():
    timeline = done_timeline()
    t_end, last = timeline[-1]
    assert 0.6 <= overlay.DONE_S <= 0.8  # quick: the owner's "pretty quick"
    assert t_end == pytest.approx(overlay.DONE_S, abs=0.01) and last.alpha == 0
    shown = [(t, f) for t, f in timeline if not f.finished]
    assert all(f.look.kind == "zip" and not f.look.text for _t, f in shown)  # no check mark
    first = shown[0][1]
    assert first.alpha == 255  # full opacity at once, like every other state
    assert first.look.zip.z == overlay.ZIP_FROM and first.look.zip.slider == 0  # wide open
    # opaque until the very end, then a quick fade that never comes back
    alphas = [f.alpha for _t, f in shown]
    assert all(f.alpha == 255 for t, f in shown if t < overlay.DONE_FADE[0])
    assert alphas == sorted(alphas, reverse=True)
    # the slider runs left to right, once, and the mouth is sealed by mid-way
    zs = [f.look.zip.z for _t, f in shown]
    assert zs == sorted(zs) and zs[-1] == overlay.ZIP_TO
    sealed = min(t for t, f in shown if f.look.zip.z >= overlay.ZIP_TO)
    assert 0.25 <= sealed <= 0.4
    assert frame_at("done", overlay.DONE_S - 0.01).finished is False
    assert frame_at("done", overlay.DONE_S).finished and frame_at("done", 9.0).finished


def test_zip_pose_follows_the_paddle_pull_design():
    poses = [overlay.zip_pose(i / 200) for i in range(round(overlay.DONE_S * 200))]
    assert max(p.slider for p in poses) > 1.05  # the slider pops in (ease-out-back)
    moving = [p for p in poses if overlay.ZIP_FROM < p.z < overlay.ZIP_TO]
    assert all(0 <= p.swing <= overlay.ZIP_SWING_MAX for p in moving)  # the pull trails
    assert max(p.swing for p in moving) == overlay.ZIP_SWING_MAX
    settling = [p.swing for p in poses if p.z == overlay.ZIP_TO and p.flat < 1]
    assert max(settling) > 8 and min(settling) < -2  # sealed: it swings past, then rests
    assert all(p.squash == 0 for p in poses if p.z < overlay.ZIP_TO)  # squash once sealed
    assert max(p.squash for p in poses) > 0.9
    flats = [p.flat for p in poses]
    assert flats == sorted(flats) and flats[0] == 0 and flats[-1] == 1  # then it flattens
    assert all(p.pill == 1 for p in poses if p.flat < 1)  # ...before the pill does
    assert min(p.pill for p in poses) < 0.6


def test_done_draws_the_zipped_line_with_white_teeth_where_the_slider_passed():
    scale = 3.0
    look = next(f.look for f in overlay.done_frames() if 100 <= f.look.zip.z <= 140)
    z = look.zip.z
    px = pixels(img := render(look, scale))
    # left of the slider: the ink line, white teeth (12 of 22 box px) on it
    teeth, gaps = 0, 0
    for k in range(3):  # teeth at 14 + 22k .. 26 + 22k, well left of the slider
        x, y = zip_point(img, scale, overlay.ZIP_INSET + 6 + 22 * k, overlay.ZIP_MID)
        teeth += tuple(px[y, x][:3]) == brand.WHITE
        x, y = zip_point(img, scale, overlay.ZIP_INSET + 17 + 22 * k, overlay.ZIP_MID)
        gaps += tuple(px[y, x][:3]) == brand.BLACK
    assert (teeth, gaps) == (3, 3)
    x, y = zip_point(img, scale, 40, overlay.ZIP_MID - overlay.ZIP_LIP / 2 + 3)
    assert tuple(px[y, x][:3]) == brand.BLACK  # the line's ink edge
    # where the open mouth's teeth and tongue were, behind the slider: yellow pill
    for bx, by in ((30, 12), (60, 120), (40, 100)):
        x, y = zip_point(img, scale, bx, by)
        assert tuple(px[y, x][:3]) == brand.YELLOW, (bx, by)
    # ahead of the slider the mouth is still open: its throat
    x, y = zip_point(img, scale, z + 48, overlay.ZIP_MID)
    assert z + 48 < brand.REF_W and tuple(px[y, x][:3]) == brand.BLACK
    # the slider is white with an ink outline, on the line
    x, y = zip_point(img, scale, min(z, overlay.ZIP_PARK[1]) + 16, overlay.ZIP_MID - 8)
    assert tuple(px[y, x][:3]) == brand.WHITE


def test_the_tongue_shows_until_the_zip_covers_it_and_is_gone_once_sealed():
    for scale in (1.0, 2.0):
        for f in overlay.done_frames():
            red = any_red(pixels(render(f.look, scale)))
            if f.look.zip.z <= 0:  # the open mouth always has its tongue
                assert red, f.look
            if f.look.zip.z >= brand.REF_W:  # sealed: no tongue, no red at all
                assert not red, f.look


def test_the_zip_stays_inside_the_pill():
    """The slider and the swinging pull never leave the pill (it doesn't take up more
    space): outside the pill's yellow, every zip frame is the plain pill."""
    for scale in (1.0, 1.5, 2.0):
        empty = pixels(render(Look("zip", zip=overlay.ZipPose(overlay.ZIP_TO, flat=1.0)), scale))
        h, w = empty.shape[:2]
        inner = overlay.capsule(
            w / 2,
            h / 2,
            (PILL_W - 2 * overlay.BORDER) * scale,
            (PILL_H - 2 * overlay.BORDER) * scale,
        )
        outside = np.array(
            [[not inner.contains(x + 0.5, y + 0.5) for x in range(w)] for y in range(h)]
        )
        listening = render(first_listening_look(), scale)
        for f in overlay.done_frames():
            img = render(f.look, scale)
            assert (img.width, img.height) == (listening.width, listening.height)
            if f.look.zip.pill < 1:
                continue  # the pill itself flattening away
            diff = np.abs(pixels(img).astype(int) - empty.astype(int)).max(axis=-1)
            assert diff[outside].max() <= 2, (scale, f.look)


def test_done_frames_are_cached_and_warmed():
    frames = overlay.done_frames()
    assert frames is overlay.done_frames()
    assert len(frames) == math.ceil(overlay.DONE_S * overlay.FPS)  # ~30 fps, a fixed set
    looks = {frame_at("done", i / 1000).look for i in range(round(overlay.DONE_S * 1000))}
    assert looks == {f.look for f in frames}
    assert frame_at("done", 0.21).look is frame_at("done", 0.22).look  # one 1/30 s frame
    warm = overlay.warm_looks()
    assert {f.look for f in frames} <= set(warm)
    assert warm.index(frames[0].look) > warm.index(first_listening_look())
    look = frames[8].look
    assert render(look, 1.5) is render(look, 1.5) and bitmap(look, 1.5) is bitmap(look, 1.5)


def test_error_and_notice_show_their_text():
    err = frame_at("error", 0.5, text="No mic")
    assert err.look == Look("text", tone="red", text="No mic") and err.alpha == 255
    assert frame_at("error", overlay.ERROR_HOLD_S + overlay.ERROR_FADE_S + 0.01).finished
    note = frame_at("notice", 0.5, text="Paused")
    assert note.look.tone == "yellow"
    assert frame_at("notice", 10.0).finished


def test_cancelled_shuts_the_mouth_and_fades_fast():
    f = frame_at("cancelled", 0.0)
    assert f.look.opening is None and f.alpha == 255
    assert frame_at("cancelled", overlay.CANCEL_FADE_S).finished


def test_hidden_and_unknown_states():
    assert frame_at("hidden", 0.0) == Frame(None, 0, True)
    assert frame_at("whatever", 1.0).finished


def test_every_state_renders():
    for name, frame in overlay.state_frames().items():
        img = render(frame.look, 1.0, fake_text)
        assert img.width > 0 and img.height > 0, name


def test_warm_looks_start_with_the_first_listening_frame():
    looks = overlay.warm_looks()
    assert looks[0] == first_listening_look()
    assert Look("text", tone="red", text="No mic") in looks
    assert len(set(looks)) == len(looks)
    assert overlay.prerender([1.0, 1.0], fake_text) == len(looks)
    hits = bitmap.cache_info().hits
    bitmap(first_listening_look(), 1.0, fake_text)
    assert bitmap.cache_info().hits == hits + 1  # showing the pill renders nothing


# ------------------------------------------------------------------ placement


def test_place_is_top_centre_of_the_work_area():
    work = (0, 0, 1920, 1032)  # a 48 px taskbar at the bottom
    x, y = place(work, 108, 64, 1.0)
    assert x == (1920 - 108) // 2
    assert y == overlay.TOP_GAP
    # a taskbar docked at the top: the work area starts below it
    x, y = place((0, 48, 1920, 1080), 108, 64, 1.0)
    assert y == 48 + overlay.TOP_GAP
    # a second monitor to the left, at 150 %
    x, y = place((-2560, 0, 0, 1392), 162, 96, 1.5)
    assert x == -2560 + (2560 - 162) // 2 and y == round(overlay.TOP_GAP * 1.5)
    # never outside a (tiny) work area
    assert place((0, 100, 200, 120), 50, 50, 1.0)[1] == 100


def test_longer_notices_stay_long_enough_to_read():
    assert notice_hold("Paused") == overlay.NOTICE_HOLD_S
    assert notice_hold("Ready · Right Ctrl to talk") == pytest.approx(3.1)
    f = frame_at("notice", 2.5, text="Ready · Right Ctrl to talk")
    assert f.alpha == 255 and not f.finished  # a short notice would be fading by now
    assert frame_at("notice", 2.5, text="Paused").finished


def test_short_text():
    assert short_text("Mic   blocked") == "Mic blocked"
    long = short_text("This is far too long for a little pill at the top")
    assert len(long) <= 28 and long.endswith("…")


# --------------------------------------------------------------- null overlay


def test_null_overlay_records_states():
    o = NullOverlay()
    o.start()
    o.listening()
    o.set_level(0.4)
    assert (o.state, o.level) == ("listening", 0.4)
    o.error("Windows is blocking the microphone right now, sorry")
    assert o.state == "error" and len(o.text) <= 28
    o.cancelled()
    o.hide()
    assert o.state == "hidden"
    o.stop()


def test_create_overlay():
    assert isinstance(overlay.create_overlay(False), NullOverlay)
    if sys.platform != "win32":
        assert isinstance(overlay.create_overlay(True), NullOverlay)
    else:
        assert isinstance(overlay.create_overlay(True), overlay.OverlayWindow)


@pytest.mark.skipif(sys.platform != "win32", reason="GDI text is Windows-only")
def test_gdi_text_mask_renders_antialiased_text():
    mask = overlay.gdi_text_mask("Mic blocked", 20)
    assert mask is not None and mask.width > 60 and 18 <= mask.height <= 40
    cov = np.frombuffer(mask.alpha, dtype=np.uint8)
    assert cov.max() == 255 and 0 < (cov > 0).mean() < 0.6
    assert len(set(cov.tolist())) > 10  # greyscale anti-aliasing, not 1-bit
    assert overlay.gdi_text_mask("", 20) is None


def test_write_state_pngs(tmp_path):
    paths = overlay.write_state_pngs(tmp_path, scale=1.0, text_renderer=fake_text)
    names = {p.name for p in paths}
    assert "overlay-listening-loud.png" in names and "overlay-error.png" in names
    assert "overlay-listening-quiet.png" in names and "overlay-done.png" in names
    assert "overlay-thinking.apng.png" in names and "overlay-done.apng.png" in names
    for p in paths:
        assert p.read_bytes().startswith(brand.PNG_SIGNATURE)


# ---------------------------------------------------------------- real window


@pytest.mark.windows
def test_real_overlay_shows_at_the_top_without_focus():
    """Shows the pill for well under a second at the top of the foreground monitor."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    window = overlay.OverlayWindow()
    shown = threading.Event()
    window.on_shown = lambda _t: shown.set()
    window.start()
    try:
        time.sleep(1.0)  # pre-rendering
        focus_before = user32.GetForegroundWindow()
        t0 = time.perf_counter()
        window.listening()
        assert shown.wait(2.0)
        latency = time.perf_counter() - t0
        rect = wintypes.RECT()
        user32.GetWindowRect(window.hwnd, ctypes.byref(rect))
        mon = user32.MonitorFromWindow(window.hwnd, 2)
        info = overlay._MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(mon, ctypes.byref(info))
        work_top = info.rcWork[1]
        assert work_top <= rect.top <= work_top + 40  # at the top, not the bottom
        assert user32.GetForegroundWindow() == focus_before  # never takes the focus
        assert latency < 0.1
        window.done()  # the zip plays, then the pill hides by itself
        time.sleep(0.3)
        assert user32.IsWindowVisible(window.hwnd)
        assert window._last_frame is not None and window._last_frame[0].kind == "zip"
        time.sleep(overlay.DONE_S)
        assert not user32.IsWindowVisible(window.hwnd)
        shown.clear()
        window.listening()
        assert shown.wait(2.0)
        window.hide()
        time.sleep(0.2)
        assert not user32.IsWindowVisible(window.hwnd)
    finally:
        window.stop()
