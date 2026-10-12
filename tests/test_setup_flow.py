"""The setup flow's logic (no Tk): screens, model download, pairing inline, copy."""

from __future__ import annotations

import json
import threading

from shoutabout import setup_flow
from shoutabout.config_edit import update_config
from shoutabout.setup_flow import SetupFlow, download_line, phone_status_line, size_line
from v2_fakes import Downloads, FakeAccess, inline_runner, make_services

PHONE = "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d"


def flow_for(tmp_path, **kw) -> tuple[SetupFlow, object]:
    saved: list = []
    svc = make_services(tmp_path, saved=saved, **kw)
    svc.extra["saved"] = saved
    return SetupFlow(svc, runner=inline_runner), svc


def phone_flow(tmp_path, **kw) -> tuple[SetupFlow, object]:
    """A first-run flow that got to the end and chose "Connect your iPhone"."""
    flow, svc = flow_for(tmp_path, **kw)
    flow.screen = "done"
    flow.connect_phone()
    return flow, svc


# ------------------------------------------------------------------- copy


def test_download_line():
    assert download_line("checking", 0, 0) == "One moment…"
    assert download_line("downloading", 672_000_000, 1_600_000_000) == (
        "Downloading the speech model… 42%"
    )
    assert download_line("downloading", 0, 0) == "Downloading the speech model…"
    assert download_line("ready", 0, 0) == "Ready."
    assert "internet" in download_line("failed", 0, 0)
    assert download_line("idle", 0, 0) == ""
    assert size_line("downloading", 672_000_000, 1_600_000_000) == "672 MB of 1,600 MB"
    assert size_line("downloading", 0, 0) == "" and size_line("ready", 1, 1) == ""


def test_phone_lines():
    assert phone_status_line("waiting", None) == "Waiting for your iPhone…"
    assert (
        phone_status_line("asking", "Sam's iPhone") == "Sam's iPhone wants to dictate to this PC."
    )
    assert phone_status_line("paired", "Sam's iPhone") == "Sam's iPhone is connected."
    assert phone_status_line("bogus", None) == "Waiting for your iPhone…"


def test_no_tours_or_disclaimers_in_the_copy():
    """The setup says what to do, not where icons live or what the app promises."""
    lines = [
        *setup_flow.TITLES.values(),
        setup_flow.NO_MIC_LINE,
        setup_flow.PHONE_HOWTO,
        setup_flow.QR_NOTE,
        *(download_line(p, 1, 2) for p in ("checking", "downloading", "ready", "failed")),
        *setup_flow.ALLOW_LABELS.values(),
        *(why.format(key="Right ⌘ Command") for why in setup_flow.ALLOW_WHY.values()),
        setup_flow.ALLOW_HINT,
        setup_flow.ALMOST_TITLE,
        setup_flow.ACCESS_LINE,
        setup_flow.PASTE_NOTE,
    ]
    for line in lines:
        lowered = line.lower()
        assert "tray" not in lowered and "menu bar" not in lowered and "clock" not in lowered
        assert "yellow mouth" not in lowered and "ollama" not in lowered
        assert len(line) <= 80, line


# ------------------------------------------------- screen 0: allow access (macOS)


def mac_flow(tmp_path, access: FakeAccess, mode: str = "full", **kw) -> SetupFlow:
    svc = make_services(tmp_path, permissions=access, **kw)
    return SetupFlow(svc, mode=mode, runner=inline_runner)


def test_windows_has_no_allow_screen(tmp_path):
    flow, svc = flow_for(tmp_path)
    assert svc.permissions is None and flow.access is None
    assert flow.screen == "check" and flow.can_try() and not flow.needs_access()
    flow.poll_permissions()  # nothing to read
    assert flow.try_note() == "" and flow.skip_allow() is False


def test_a_mac_starts_with_allow_access_while_something_is_missing(tmp_path):
    access = FakeAccess()  # nothing asked yet
    flow = mac_flow(tmp_path, access)
    assert flow.screen == "allow" and flow.title() == "Allow access"
    rows = flow.allow_rows()
    assert [(r.label, r.why, r.granted) for r in rows] == [
        ("Microphone", "to hear you", False),
        ("Accessibility", "for Right Ctrl and pasting", False),
    ]
    flow.start()  # the model is checked (and would download) meanwhile
    assert flow.phase == "ready" and flow.screen == "allow"  # but it waits for the permissions


def test_a_mac_that_allows_everything_never_sees_it(tmp_path):
    flow = mac_flow(tmp_path, FakeAccess("granted", True, None))
    assert flow.screen == "check"
    flow.start()
    assert flow.screen == "try"


def test_allow_asks_the_hub_and_moves_on_by_itself(tmp_path):
    access = FakeAccess(grant_on_ask=True, answer="asked")  # macOS's dialog, then Allow
    flow = mac_flow(tmp_path, access)
    flow.start()
    flow.ask("microphone")
    assert access.asked == ["microphone"]
    assert [r.granted for r in flow.allow_rows()] == [True, False]
    assert flow.screen == "allow" and flow.allow_hint == ""  # a dialog: no hint needed
    assert not flow.take_raise()
    flow.ask("accessibility")
    assert flow.screen == "try"  # everything allowed and the model is ready: Try it
    assert flow.take_raise() and not flow.take_raise()  # to the front, once


def test_system_settings_hint_and_polling_follow_macos(tmp_path):
    access = FakeAccess()
    flow = mac_flow(tmp_path, access)
    flow.start()
    flow.ask("accessibility")
    assert flow.allow_hint == setup_flow.ALLOW_HINT  # System Settings opened: what to do there
    version = flow.version
    flow.poll_permissions()
    assert flow.version == version  # nothing changed, nothing redrawn
    access.grant("microphone", "accessibility")  # the person flips the switches
    flow.poll_permissions()
    assert flow.screen == "try" and flow.allow_hint == ""


def test_allow_then_getting_ready_while_the_model_downloads(tmp_path):
    access = FakeAccess()
    flow = mac_flow(tmp_path, access)  # not started: the download hasn't finished
    access.grant("microphone", "accessibility")
    flow.refresh_permissions()
    assert flow.screen == "check"
    flow.start()
    assert (flow.phase, flow.screen) == ("ready", "try")


def test_skip_never_shows_try_it_without_the_mic_and_the_hotkey(tmp_path):
    access = FakeAccess()
    flow = mac_flow(tmp_path, access)
    flow.start()
    assert flow.skip_allow() is False
    assert flow.screen == "done" and flow.needs_access()
    assert flow.done_line() == setup_flow.ACCESS_LINE
    assert flow.title() == "Almost there"  # not "You're all set" while it can't work
    flow.back_to_allow()  # the "Allow access" link on the last screen
    assert flow.screen == "allow"
    access.grant("microphone", "accessibility")
    flow.poll_permissions()
    assert flow.screen == "try" and not flow.needs_access()


def test_no_accessibility_still_tries_it_with_the_copy(tmp_path):
    """A managed Mac may never allow Accessibility: with Input Monitoring the hotkey works,
    and the words land on the clipboard (⌘V)."""
    flow = mac_flow(tmp_path, FakeAccess("granted", False, True))
    flow.start()
    assert flow.screen == "allow"  # still asked for, before Try it
    flow.skip_allow()
    assert flow.screen == "try" and flow.try_note() == setup_flow.PASTE_NOTE


def test_input_monitoring_is_listed_only_when_accessibility_cant_be_read(tmp_path):
    flow = mac_flow(tmp_path, FakeAccess("granted", None, False))
    assert [r.key for r in flow.allow_rows()] == ["microphone", "accessibility", "input_monitoring"]
    assert flow.allow_rows()[2].why == "for Right Ctrl"
    flow = mac_flow(tmp_path / "b", FakeAccess("granted", False, False))
    assert [r.key for r in flow.allow_rows()] == ["microphone", "accessibility"]


def test_allow_mode_is_just_the_permissions(tmp_path):
    downloads = Downloads()
    access = FakeAccess("denied", True, None)
    flow = mac_flow(tmp_path, access, mode="allow", downloads=downloads)
    assert flow.screen == "allow"
    flow.start()
    assert flow.phase == "idle" and downloads.calls == []  # nothing to download here
    assert flow.skip_allow() is True  # Skip just closes it
    access.grant("microphone")
    flow.poll_permissions()
    assert flow.screen == "done" and flow.done_line() == flow.pc_line()
    flow.finish()
    assert not flow.svc.paths.setup_flag_file.exists()  # not the first-run setup
    everything = mac_flow(tmp_path / "b", FakeAccess("granted", True, None), mode="allow")
    assert everything.screen == "done"


def test_unreadable_permissions_never_block(tmp_path):
    access = FakeAccess()
    access.broken = True  # the hub doesn't answer
    flow = mac_flow(tmp_path, access)
    assert flow.screen == "check" and flow.can_try()
    flow.start()
    assert flow.screen == "try"


def test_permissions_are_read_only_while_they_matter(tmp_path):
    access = FakeAccess("granted", True, None)
    flow = mac_flow(tmp_path, access)
    flow.start()
    assert flow.screen == "try"
    reads = access.reads
    flow.poll_permissions()
    assert access.reads == reads
    phone = mac_flow(tmp_path / "b", FakeAccess(), mode="phone")
    assert phone.access is None and phone.screen == "phone"


def test_a_closed_flow_ignores_late_answers(tmp_path):
    access = FakeAccess()
    flow = mac_flow(tmp_path, access)
    flow.finish()
    access.grant("microphone", "accessibility")
    flow.refresh_permissions()
    assert flow.screen == "allow"
    assert '"skipped"' in flow.svc.paths.setup_flag_file.read_text("utf-8")


# --------------------------------------------------------- screen 1: getting ready


def test_model_already_there_moves_straight_on(tmp_path):
    flow, svc = flow_for(tmp_path)
    assert flow.screen == "check" and flow.title() == "Getting ready"
    flow.start()
    assert flow.phase == "ready"
    assert svc.extra["downloads"].calls == []
    assert flow.progress_fraction() == 1.0
    assert flow.screen == "try"  # no Next button: it moved on by itself
    flow.start()  # once only
    assert svc.extra["downloads"].calls == []


def test_model_download_with_progress(tmp_path):
    downloads = Downloads()
    flow, _svc = flow_for(tmp_path, downloads=downloads)
    seen = []
    real = downloads.download

    def spy(model, *, progress, cancel):
        def wrapped(done, total):
            progress(done, total)
            seen.append((flow.screen, flow.progress_fraction(), flow.status_line()))

        return real(model, progress=wrapped, cancel=cancel)

    flow.svc.download_model = spy
    flow.start()
    assert downloads.calls == ["large-v3-turbo"]
    assert ("check", 0.5, "Downloading the speech model… 50%") in seen
    assert flow.phase == "ready" and flow.screen == "try"


def test_failed_download_can_retry_or_skip(tmp_path):
    downloads = Downloads(fail=True)
    flow, _svc = flow_for(tmp_path, downloads=downloads)
    flow.start()
    assert (flow.phase, flow.screen) == ("failed", "check")  # waits for Try again / Skip
    downloads.fail = False
    flow.retry()
    assert (flow.phase, flow.screen) == ("ready", "try")
    downloads2 = Downloads(fail=True)
    flow2, _ = flow_for(tmp_path / "b", downloads=downloads2)
    flow2.start()
    flow2.skip_download()
    # no model yet, so nothing to try: straight to the end (the hub downloads it later)
    assert (flow2.phase, flow2.screen) == ("unknown", "done")


def test_without_the_voice_modules_setup_still_works(tmp_path):
    def missing(*_a, **_k):
        raise ModuleNotFoundError("No module named 'shoutabout.hardware'")

    flow, _svc = flow_for(tmp_path)
    flow.svc.detect_hardware = missing
    flow.svc.resolve_model = missing
    flow.start()
    assert flow.phase == "unknown" and flow.screen == "try"


def test_model_check_error_is_unknown(tmp_path):
    flow, _svc = flow_for(tmp_path)

    def broken(_model):
        raise RuntimeError("cache unreadable")

    flow.svc.model_ready = broken
    flow.start()
    assert flow.phase == "unknown" and flow.screen == "try"


def test_closing_cancels_the_download(tmp_path):
    downloads = Downloads()
    downloads.gate = threading.Event()
    svc = make_services(tmp_path, downloads=downloads)
    flow = SetupFlow(svc)  # real background thread
    flow.start()
    for _ in range(200):
        if downloads.calls:
            break
        threading.Event().wait(0.01)
    flow.finish()
    downloads.gate.set()
    assert flow.phase in ("downloading", "checking")
    assert flow._cancel.is_set()


# ------------------------------------------------------------ screen 2: try it


def test_try_screen_finishes_when_words_arrive(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.start()
    assert flow.screen == "try" and flow.title() == "Try it"
    assert flow.try_instruction() == "Press Right Ctrl, say this, then press it again:"
    flow.text_changed("   \n")
    assert flow.screen == "try"
    flow.text_changed("This is my first dictation.\n")
    assert flow.screen == "done" and flow.title() == "You're all set"


def test_try_instruction_hold_mode(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.talk_mode = "hold"
    assert flow.try_instruction() == "Hold Right Ctrl and say:"


def test_skip_try(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.start()
    flow.skip_try()
    assert flow.screen == "done"
    flow.skip_try()  # only from the try screen
    assert flow.screen == "done"


def test_talk_to_this_pc_off_skips_the_try_screen(tmp_path):
    svc = make_services(tmp_path)
    update_config(svc.paths, {"dictation": {"enabled": False}})
    flow = SetupFlow(svc, runner=inline_runner)
    assert flow.pc_on is False
    flow.start()
    assert flow.screen == "done" and flow.done_line() == ""


# --------------------------------------------------------------- screen 3: done


def test_done_says_how_to_talk(tmp_path):
    flow, _ = flow_for(tmp_path)
    assert flow.pc_line() == "Press Right Ctrl, talk, press it again."
    assert flow.done_line() == flow.pc_line()
    flow.talk_mode = "hold"
    assert flow.done_line() == "Hold Right Ctrl, talk, let go."


def test_setup_never_changes_a_setting(tmp_path):
    """Setup only reads [dictation]; switching things on and off lives in Settings."""
    flow, svc = flow_for(tmp_path, has_microphone=lambda: False)
    flow.start()
    flow.connect_phone()
    flow.finish()
    assert svc.extra["saved"] == []


# ------------------------------------------------------- no microphone at all


def test_no_microphone_skips_the_try_screen_and_says_why(tmp_path):
    flow, _svc = flow_for(tmp_path, has_microphone=lambda: False)
    assert flow.no_mic and flow.pc_on is False
    flow.start()
    assert flow.screen == "done"
    assert flow.done_line() == setup_flow.NO_MIC_LINE
    assert setup_flow.NO_MIC_LINE == "No microphone found. Plug one in, or connect your iPhone."


def test_unknown_microphone_state_changes_nothing(tmp_path):
    flow, _svc = flow_for(tmp_path, has_microphone=lambda: None)
    assert flow.pc_on is True and not flow.no_mic
    assert flow.done_line() == flow.pc_line()


def test_phone_mode_never_asks_about_microphones(tmp_path):
    def boom():
        raise AssertionError("asked")

    svc = make_services(tmp_path, has_microphone=boom)
    flow = SetupFlow(svc, mode="phone", runner=inline_runner)
    assert not flow.no_mic and flow.pc_on is False


# ------------------------------------------------------- screen 4: the iPhone


def test_connect_your_iphone(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.start()
    flow.skip_try()
    flow.connect_phone()
    assert flow.screen == "phone" and flow.title() == "Connect your iPhone"
    assert flow.phone_line() == "Waiting for your iPhone…"


def test_phone_pairing_inline(tmp_path):
    flow, svc = phone_flow(tmp_path)
    prompt = svc.pairing.request("Sam's iPhone", PHONE, "192.168.1.5")
    assert flow.offer(prompt)
    assert flow.pair_state == "asking"
    assert flow.phone_line() == "Sam's iPhone wants to dictate to this PC."
    flow.allow()
    assert flow.pair_state == "connecting" and flow.prompt is None
    flow.on_pair_finished(prompt.request_id, "approved")
    assert flow.pair_state == "connecting"
    body = svc.pairing.status(prompt.request_id)  # the phone collects its token
    flow.on_pair_finished(prompt.request_id, "claimed")
    assert body["status"] == "approved"
    assert flow.pair_state == "paired"
    assert flow.phone_line() == "Sam's iPhone is connected."
    assert [d.name for d in svc.devices.list()] == ["Sam's iPhone"]


def test_phone_pairing_denied_inline(tmp_path):
    flow, svc = phone_flow(tmp_path)
    prompt = svc.pairing.request("Stranger", PHONE, "192.168.1.66")
    flow.offer(prompt)
    flow.deny()
    assert flow.pair_state == "denied" and flow.prompt is None
    assert svc.pairing.status(prompt.request_id) == {"status": "denied"}
    assert svc.devices.list() == []


def test_request_expiring_on_screen(tmp_path):
    flow, svc = phone_flow(tmp_path)
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.offer(prompt)
    flow.on_pair_finished(prompt.request_id, "expired")
    assert flow.prompt is None and flow.pair_state == "expired"


def test_offer_refused_when_not_showing_the_phone_screen(tmp_path):
    flow, svc = flow_for(tmp_path)
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    assert not flow.offer(prompt)  # getting ready
    flow.screen = "try"
    assert not flow.offer(prompt)
    flow.screen = "done"
    assert not flow.offer(prompt)
    flow.screen = "phone"
    flow.closed = True
    assert not flow.offer(prompt)


def test_opening_the_phone_screen_picks_up_a_waiting_request(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen = "done"
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.connect_phone()
    assert flow.prompt == prompt


def test_allow_failure(tmp_path):
    flow, svc = phone_flow(tmp_path)
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.offer(prompt)

    def broken(*_a):
        raise OSError("disk full")

    svc.devices.add = broken
    flow.allow()
    assert flow.pair_state == "error" and flow.prompt is None


def test_no_hub_means_pairing_unavailable(tmp_path):
    flow, _ = phone_flow(tmp_path, with_pairing=False)
    assert flow.pair_state == "unavailable"
    assert flow.phone_line() == "Start ShoutAbout first, then open this again."
    flow.allow()
    flow.deny()


def test_qr_fallback(tmp_path):
    flow, svc = flow_for(tmp_path)
    svc.lan_url = lambda: "http://192.168.1.50:47821"
    flow.toggle_qr()
    assert flow.show_qr and flow.qr_error is None
    payload = json.loads(flow.qr_text)
    assert payload["lan_url"] == "http://192.168.1.50:47821"
    assert payload["token"] == svc.paths.token_file.read_text("utf-8").strip()
    flow.toggle_qr()
    assert not flow.show_qr
    assert "only to your own phone" in setup_flow.QR_NOTE  # it holds the hub token


def test_qr_fallback_without_a_lan_address(tmp_path):
    flow, svc = flow_for(tmp_path)
    svc.lan_url = lambda: None
    flow.toggle_qr()
    assert flow.qr_text is None and flow.qr_error


# ------------------------------------------------------------------ the end


def test_finish_writes_the_flag_and_says_no_to_a_waiting_phone(tmp_path):
    flow, svc = phone_flow(tmp_path)
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.offer(prompt)
    flow.finish()
    assert svc.pairing.status(prompt.request_id) == {"status": "denied"}
    assert '"finished"' in svc.paths.setup_flag_file.read_text("utf-8")
    flow.finish()  # twice is harmless


def test_closing_early_marks_it_skipped(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.finish()
    assert '"skipped"' in svc.paths.setup_flag_file.read_text("utf-8")


def test_quitting_the_app_mid_setup_does_not_mark_it_done(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.finish(remember=False)
    assert not svc.paths.setup_flag_file.exists()
    assert flow._cancel.is_set()


def test_finish_after_done(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen = "done"
    flow.finish()
    assert '"finished"' in svc.paths.setup_flag_file.read_text("utf-8")


def test_add_phone_mode(tmp_path):
    svc = make_services(tmp_path)
    flow = SetupFlow(svc, mode="phone", runner=inline_runner)
    assert flow.screen == "phone" and not flow.pc_on
    assert flow.title() == "Connect your iPhone"
    flow.start()  # nothing to download in this mode
    assert flow.phase == "idle" and flow.screen == "phone"
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    assert flow.offer(prompt)
    flow.finish()
    assert not svc.paths.setup_flag_file.exists()  # adding a phone isn't the first-run setup


def test_titles_cover_every_screen():
    assert set(setup_flow.TITLES) == set(setup_flow.SCREENS)
