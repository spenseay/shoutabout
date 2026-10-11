"""macOS overlay (with a fake panel), paste (fake pasteboard and key poster) and the
microphone wrapper (fake stream). Nothing touches AppKit, the clipboard or a microphone."""

from __future__ import annotations

import numpy as np
import pytest

from shoutabout import overlay as ov
from shoutabout.config import PasteConfig
from shoutabout.mac import keys, mic_mac
from shoutabout.mac.overlay_mac import MacOverlay, place_top_centre, prewarm_looks
from shoutabout.mac.paste import (
    ACCESSIBILITY_WARNING,
    AUTO_GENERATED,
    CONCEALED,
    SECURE_INPUT_WARNING,
    TRANSIENT,
    MacPasteInjector,
    VKey,
    find_keycode,
)
from shoutabout.mic import MicError

# ------------------------------------------------------------------ overlay


def test_top_centre_is_below_the_menu_bar():
    visible = (0.0, 0.0, 1512.0, 945.0)  # 982 pt screen minus a 37 pt menu bar
    x, y = place_top_centre(visible, 54.0, 32.0)
    assert x == round((1512 - 54) / 2)
    assert y + 32 <= 945 and y > 900
    # a second screen to the right
    x2, _ = place_top_centre((1512.0, 0.0, 1920.0, 1055.0), 54.0, 32.0)
    assert x2 > 1512


class FakePanel:
    def __init__(self):
        self.shown: list[tuple] = []
        self.hidden = 0
        self.timer = None
        self.closed = False
        self.images = 0

    def screen(self):
        return (0.0, 0.0, 1512.0, 945.0), 2.0

    def show(self, image, frame, scale, alpha):
        self.shown.append((image, frame, scale, alpha))

    def hide(self):
        self.hidden += 1

    def make_image(self, img):
        self.images += 1
        return ("image", img.width, img.height)

    def start_timer(self, interval, tick):
        self.timer = tick

    def stop_timer(self):
        self.timer = None

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make_overlay():
    panel = FakePanel()
    clock = Clock()
    overlay = MacOverlay(
        panel_factory=lambda: panel, main=lambda fn, *a: fn(*a), text_renderer=None, clock=clock
    )
    overlay._prewarm = lambda: None  # type: ignore[method-assign]
    return overlay, panel, clock


def test_overlay_shows_listening_at_retina_scale_and_animates():
    overlay, panel, clock = make_overlay()
    overlay.start()
    overlay.listening()
    assert panel.shown, "the pill appears at once"
    image, frame, scale, alpha = panel.shown[-1]
    assert scale == 2.0
    _x, _y, w, h = frame
    assert (w, h) == (image[1] / 2, image[2] / 2)  # points = pixels / backing scale
    assert panel.timer is not None
    clock.now += 0.5
    overlay.set_level(0.9)
    panel.timer()
    assert len(panel.shown) >= 2


def test_overlay_listening_stays_just_the_mouth_like_windows():
    """No clock or countdown on the Mac either (shared overlay.frame_at): a long recording
    shows the same pill as a short one."""
    overlay, panel, clock = make_overlay()
    overlay.start()
    overlay.listening()
    made = panel.images
    for seconds in (0.4, 0.8, 12.0, 575.0):  # a glow or a clock would be new images
        clock.now += seconds
        panel.timer()
    assert panel.images == made


def test_prewarm_has_every_listening_level_and_no_text():
    from shoutabout.mac.overlay_mac import prewarm_looks

    looks = prewarm_looks()
    levels = {ov.level_opening(i / (ov.LEVEL_STEPS - 1)) for i in range(ov.LEVEL_STEPS)}
    assert levels <= {look.opening for look in looks}
    assert not any(look.text for look in looks)


def test_overlay_caches_images():
    overlay, panel, clock = make_overlay()
    overlay.start()
    overlay.thinking()
    made = panel.images
    clock.now += ov.brand.TALK_DURATION_MS / 1000  # one full loop later: same frames again
    overlay.thinking()
    assert panel.images == made


def test_overlay_hides_when_a_state_has_run_its_course():
    overlay, panel, clock = make_overlay()
    overlay.start()
    overlay.cancelled()
    clock.now += 5.0
    panel.timer()
    assert panel.hidden == 1 and panel.timer is None
    overlay.hide()
    assert overlay.state == "hidden"


def test_overlay_stop_closes_the_panel():
    overlay, panel, _ = make_overlay()
    overlay.start()
    overlay.listening()
    overlay.stop()
    assert panel.closed
    overlay.listening()  # after stop: nothing to draw on, no error
    assert len(panel.shown) == 1


def test_prewarm_looks_cover_listening_and_thinking():
    looks = prewarm_looks()
    assert len(looks) == len(set(looks)) > 50
    assert all(not look.text for look in looks)


def test_prewarm_looks_cover_the_zip_so_done_never_waits():
    looks = prewarm_looks()
    zipping = [f.look for f in ov.done_frames()]
    assert set(zipping) <= set(looks)
    assert looks.index(zipping[0]) > looks.index(ov.first_listening_look())


def test_overlay_zips_then_hides_when_done_like_windows():
    overlay, panel, clock = make_overlay()
    overlay.start()
    overlay.done()
    image, _frame, scale, alpha = panel.shown[-1]
    listening = ov.render(ov.first_listening_look(), scale)
    assert alpha == 1.0 and image[1:] == (listening.width, listening.height)  # same pill size
    for _ in range(round(ov.DONE_S * ov.FPS) - 1):
        clock.now += 1 / ov.FPS
        panel.timer()
    assert len(panel.shown) > 10 and panel.hidden == 0  # every 1/30 s a new zip frame
    clock.now += 0.1
    panel.timer()
    assert panel.hidden == 1 and panel.timer is None


# -------------------------------------------------------------------- paste


class FakeBoard:
    def __init__(self):
        self.count = 1
        self.items = [[("public.utf8-plain-text", b"before")]]
        self.writes: list[tuple[str, tuple]] = []
        self.restored: list = []

    def change_count(self):
        return self.count

    def snapshot(self):
        return [list(i) for i in self.items]

    def write_text(self, text, markers):
        self.count += 1
        self.writes.append((text, markers))
        self.items = [[("public.utf8-plain-text", text.encode())]]
        return self.count

    def restore(self, items, markers):
        self.count += 1
        self.restored.append((items, markers))
        self.items = items
        return self.count


class FakePoster:
    def __init__(self, fail=False):
        self.posted: list[int] = []
        self.fail = fail

    def post_paste(self, keycode):
        if self.fail:
            raise OSError("nope")
        self.posted.append(keycode)


class Timers:
    def __init__(self):
        self.pending = []

    def __call__(self, delay, fn):
        self.pending.append((delay, fn))
        return None

    def run(self):
        for _delay, fn in self.pending:
            fn()
        self.pending.clear()


class Notes:
    def __init__(self):
        self.notes = []

    def notify(self, title, message):
        self.notes.append((title, message))


def make_injector(trusted=True, secure=False, **cfg):
    board, poster, timers, notes = FakeBoard(), FakePoster(), Timers(), Notes()
    vkey = VKey(lookup=lambda: None)
    inj = MacPasteInjector(
        PasteConfig(**cfg),
        notes,
        pasteboard=board,
        poster=poster,
        trusted=lambda: trusted,
        secure_input=lambda: secure,
        vkey=vkey,
        timer=timers,
    )
    return inj, board, poster, timers, notes


def test_paste_posts_cmd_v_and_restores_the_clipboard():
    inj, board, poster, timers, _ = make_injector()
    result = inj.paste("hello there")
    assert result.pasted and result.warning is None
    assert poster.posted == [keys.KC_ANSI_V]
    text, markers = board.writes[0]
    assert text == "hello there"
    assert TRANSIENT in markers and CONCEALED in markers and AUTO_GENERATED in markers
    assert timers.pending and timers.pending[0][0] == pytest.approx(0.5)
    timers.run()
    assert board.items == [[("public.utf8-plain-text", b"before")]]
    assert board.restored[0][1] == (TRANSIENT,)


def test_paste_doesnt_restore_over_a_newer_copy():
    inj, board, _poster, timers, _ = make_injector()
    inj.paste("dictated")
    board.count += 1  # the person copied something else in the meantime
    timers.run()
    assert board.restored == []


def test_paste_without_accessibility_leaves_the_text_and_says_why_once():
    inj, board, poster, timers, notes = make_injector(trusted=False)
    result = inj.paste("words")
    assert not result.pasted and result.warning == ACCESSIBILITY_WARNING
    assert poster.posted == [] and timers.pending == []
    text, markers = board.writes[0]
    assert text == "words" and TRANSIENT not in markers  # it stays: not transient
    inj.paste("more")
    assert len(notes.notes) == 1


def test_paste_into_a_password_field_is_refused():
    inj, _board, poster, _timers, _ = make_injector(secure=True)
    result = inj.paste("secret?")
    assert not result.pasted and result.warning == SECURE_INPUT_WARNING
    assert poster.posted == []


def test_paste_without_restore_and_without_concealing():
    inj, board, _poster, timers, _ = make_injector(
        restore_clipboard=False, exclude_from_history=False
    )
    assert inj.paste("x").pasted
    assert board.writes[0][1] == (AUTO_GENERATED,)
    assert timers.pending == []


def test_paste_keystroke_failure_is_reported():
    inj, *_ = make_injector()
    inj._poster = FakePoster(fail=True)
    result = inj.paste("x")
    assert not result.pasted and "keystroke" in result.warning


def test_find_keycode_prefers_ansi_v_and_follows_the_layout():
    qwerty = {keys.KC_ANSI_V: "v", 0x2F: "."}
    assert find_keycode(lambda c: qwerty.get(c)) == keys.KC_ANSI_V
    dvorak = {keys.KC_ANSI_V: ".", 0x2F: "v"}
    assert find_keycode(lambda c: dvorak.get(c)) == 0x2F
    assert find_keycode(lambda c: None) is None
    vkey = VKey(lookup=lambda: None)
    assert vkey.refresh() == keys.KC_ANSI_V
    vkey = VKey(lookup=lambda: 0x2F)
    assert vkey.refresh() == 0x2F


# ---------------------------------------------------------------------- mic


class FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.samplerate = 16_000

    def start(self):
        self.callback(np.full((480, 1), 0.1, dtype=np.float32), 480, None, None)

    def stop(self):
        pass

    def close(self):
        pass


def make_recorder(status, asked=None):
    return mic_mac.MacMicRecorder(
        mic_status=lambda: status,
        ask=(lambda: asked.append(1)) if asked is not None else (lambda: None),
        stream_factory=lambda rate, block, cb: FakeStream(cb),
    )


def test_mic_denied_says_where_to_allow_it():
    with pytest.raises(MicError) as info:
        make_recorder("denied").start()
    assert info.value.short == "Mic blocked"
    assert "Privacy & Security > Microphone" in str(info.value)


def test_mic_not_asked_yet_asks_instead_of_recording_silence():
    asked: list = []
    with pytest.raises(MicError) as info:
        make_recorder("undetermined", asked).start()
    assert asked == [1] and info.value.short == "Allow the mic"


def test_mic_granted_records():
    rec = make_recorder("granted")
    rec.start()
    audio = rec.stop()
    assert audio.size == 480


def test_mic_errors_get_mac_words():
    windows = MicError("Plug one in... Settings > System > Sound > Input.", "No microphone")
    assert "System Settings > Sound" in str(mic_mac.mac_error(windows, "granted"))
    silent = MicError("x", "Mic is silent")
    assert mic_mac.mac_error(silent, "granted").short == "Mic is silent"
    assert mic_mac.mac_error(silent, "denied").short == "Mic blocked"
    other = MicError("odd", "Mic problem")
    assert mic_mac.mac_error(other, "granted") is other
