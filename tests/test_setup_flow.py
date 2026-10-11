"""The setup flow's logic (no Tk): screens, model download, pairing inline, copy."""

from __future__ import annotations

import json
import threading

import pytest

from shoutabout import setup_flow
from shoutabout.config_edit import ConfigEditError
from shoutabout.setup_flow import SetupFlow, download_line, hardware_line, phone_status_line
from v2_fakes import BALANCED, BEST, CPU_HW, GPU_HW, LIGHT, Downloads, inline_runner, make_services

PHONE = "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d"


def flow_for(tmp_path, **kw) -> tuple[SetupFlow, object]:
    saved: list = []
    svc = make_services(tmp_path, saved=saved, **kw)
    svc.extra["saved"] = saved
    return SetupFlow(svc, runner=inline_runner), svc


# ------------------------------------------------------------------- copy


@pytest.mark.parametrize(
    ("hw", "choice", "line"),
    [
        (GPU_HW, BEST, "Your NVIDIA GeForce RTX 4080 SUPER can run our most accurate model."),
        (GPU_HW, BALANCED, "Your NVIDIA GeForce RTX 4080 SUPER runs our balanced model: quick"),
        (CPU_HW, BALANCED, "This PC runs our balanced model"),
        (CPU_HW, LIGHT, "We picked our lightest model, so it stays quick on this PC."),
        (CPU_HW, BEST, "This PC can run our most accurate model."),
        (None, None, "We'll pick the best model for this PC."),
    ],
)
def test_hardware_line(hw, choice, line):
    assert hardware_line(hw, choice).startswith(line)


def test_download_line():
    assert download_line("downloading", 672_000_000, 1_600_000_000) == (
        "Getting the speech model… 42% (672 MB of 1,600 MB)"
    )
    assert download_line("downloading", 0, 0) == "Getting the speech model…"
    assert download_line("ready", 0, 0) == "Speech model ready."
    assert "internet" in download_line("failed", 0, 0)
    assert download_line("idle", 0, 0) == ""


def test_phone_lines():
    assert phone_status_line("waiting", None) == "Waiting for your iPhone…"
    assert (
        phone_status_line("asking", "Sam's iPhone") == "Sam's iPhone wants to dictate to this PC."
    )
    assert phone_status_line("paired", "Sam's iPhone") == "Sam's iPhone is connected."
    assert phone_status_line("bogus", None) == "Waiting for your iPhone…"


# --------------------------------------------------------------- screen 1


def test_model_already_there_skips_the_download(tmp_path):
    flow, svc = flow_for(tmp_path)
    assert flow.screen == "check" and flow.step_label() == "STEP 1 OF 3"
    assert not flow.can_continue()
    flow.start()
    assert flow.phase == "ready"
    assert flow.hardware_text.startswith("Your NVIDIA GeForce RTX 4080 SUPER")
    assert svc.extra["downloads"].calls == []
    assert flow.progress_fraction() == 1.0
    assert flow.can_continue()
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
            seen.append((flow.phase, flow.progress_fraction(), flow.status_line()))

        return real(model, progress=wrapped, cancel=cancel)

    flow.svc.download_model = spy
    flow.start()
    assert downloads.calls == ["large-v3-turbo"]
    assert ("downloading", 0.5, "Getting the speech model… 50% (0 MB of 0 MB)") in seen
    assert flow.phase == "ready" and flow.can_continue()


def test_failed_download_can_retry_or_skip(tmp_path):
    downloads = Downloads(fail=True)
    flow, _svc = flow_for(tmp_path, downloads=downloads)
    flow.start()
    assert flow.phase == "failed" and not flow.can_continue()
    downloads.fail = False
    flow.retry()
    assert flow.phase == "ready"
    downloads2 = Downloads(fail=True)
    flow2, _ = flow_for(tmp_path / "b", downloads=downloads2)
    flow2.start()
    flow2.skip_download()
    assert (flow2.phase, flow2.screen) == ("unknown", "use")


def test_without_the_voice_modules_setup_still_works(tmp_path):
    def missing(*_a, **_k):
        raise ModuleNotFoundError("No module named 'shoutabout.hardware'")

    flow, _svc = flow_for(tmp_path)
    flow.svc.detect_hardware = missing
    flow.svc.resolve_model = missing
    flow.start()
    assert flow.phase == "unknown" and flow.can_continue()
    assert flow.hardware_text == "We'll pick the best model for this PC."


def test_model_check_error_is_unknown(tmp_path):
    flow, _svc = flow_for(tmp_path)

    def broken(_model):
        raise RuntimeError("cache unreadable")

    flow.svc.model_ready = broken
    flow.start()
    assert flow.phase == "unknown"


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


# ---------------------------------------------------------- navigation


def test_navigation(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.start()
    flow.next()
    assert flow.screen == "use" and flow.title() == "How will you use ShoutAbout?"
    flow.back()
    assert flow.screen == "check"
    flow.next()
    flow.next()
    assert flow.screen == "try" and flow.step_label() == "STEP 3 OF 3"
    flow.next()
    assert flow.screen == "done" and flow.step_label() == ""
    assert flow.title() == "You're all set!"


def test_neither_way_on_skips_the_try_screen(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.screen = "use"
    flow.pc_on = False
    flow.phone_on = False
    flow.next()
    assert flow.screen == "done"


# ------------------------------------------------------------ screen 2


def test_toggle_pc_saves_the_setting(tmp_path):
    flow, svc = flow_for(tmp_path)
    assert flow.pc_on is True
    assert flow.pc_line() == "Press Right Ctrl, talk, press it again."
    assert flow.pc_card_text() == flow.pc_line()
    assert flow.pc_card_note() == "The words appear wherever you're typing."
    flow.toggle_pc()
    assert flow.pc_on is False
    assert svc.extra["saved"] == [{"dictation": {"enabled": False}}]


def test_toggle_pc_failure_keeps_the_card_and_says_so(tmp_path):
    flow, svc = flow_for(tmp_path)

    def refuse(_changes):
        raise ConfigEditError("schema")

    svc.save_settings = refuse
    flow.toggle_pc()
    assert flow.pc_on is True
    assert flow.save_error == "Couldn't save that setting."


def test_phone_pairing_inline(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen = "use"
    flow.toggle_phone()
    assert flow.phone_on and flow.phone_line() == "Waiting for your iPhone…"
    prompt = svc.pairing.request("Sam's iPhone", PHONE, "192.168.1.5")
    assert flow.offer(prompt)
    assert flow.pair_state == "asking" and not flow.can_continue()
    assert flow.phone_line() == "Sam's iPhone wants to dictate to this PC."
    flow.allow()
    assert flow.pair_state == "connecting" and flow.can_continue()
    flow.on_pair_finished(prompt.request_id, "approved")
    assert flow.pair_state == "connecting"
    body = svc.pairing.status(prompt.request_id)  # the phone collects its token
    flow.on_pair_finished(prompt.request_id, "claimed")
    assert body["status"] == "approved"
    assert flow.pair_state == "paired"
    assert flow.phone_line() == "Sam's iPhone is connected."
    assert [d.name for d in svc.devices.list()] == ["Sam's iPhone"]


def test_phone_pairing_denied_inline(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen, flow.phone_on = "use", True
    prompt = svc.pairing.request("Stranger", PHONE, "192.168.1.66")
    flow.offer(prompt)
    flow.deny()
    assert flow.pair_state == "denied" and flow.prompt is None
    assert svc.pairing.status(prompt.request_id) == {"status": "denied"}
    assert svc.devices.list() == []


def test_request_expiring_on_screen(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen, flow.phone_on = "use", True
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.offer(prompt)
    flow.on_pair_finished(prompt.request_id, "expired")
    assert flow.prompt is None and flow.pair_state == "expired"


def test_offer_refused_when_not_showing_the_phone_card(tmp_path):
    flow, svc = flow_for(tmp_path)
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    assert not flow.offer(prompt)  # screen 1
    flow.screen = "use"
    assert not flow.offer(prompt)  # phone card off
    flow.phone_on = True
    flow.closed = True
    assert not flow.offer(prompt)


def test_turning_the_phone_on_picks_up_a_waiting_request(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen = "use"
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.toggle_phone()
    assert flow.prompt == prompt


def test_allow_failure(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen, flow.phone_on = "use", True
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.offer(prompt)

    def broken(*_a):
        raise OSError("disk full")

    svc.devices.add = broken
    flow.allow()
    assert flow.pair_state == "error" and flow.prompt is None


def test_no_hub_means_pairing_unavailable(tmp_path):
    flow, _ = flow_for(tmp_path, with_pairing=False)
    assert flow.pair_state == "unavailable"
    flow.screen = "use"
    flow.toggle_phone()
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


def test_qr_fallback_without_a_lan_address(tmp_path):
    flow, svc = flow_for(tmp_path)
    svc.lan_url = lambda: None
    flow.toggle_qr()
    assert flow.qr_text is None and flow.qr_error


# ------------------------------------------------------------ screen 3/4


def test_try_screen_finishes_when_words_arrive(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.screen = "try"
    assert flow.try_instruction() == "Press Right Ctrl, say this, then press it again:"
    flow.text_changed("   \n")
    assert flow.screen == "try"
    flow.text_changed("This is my first dictation.\n")
    assert flow.screen == "done"


def test_try_instruction_variants(tmp_path):
    flow, _ = flow_for(tmp_path)
    flow.talk_mode = "hold"
    assert flow.try_instruction() == "Hold Right Ctrl and say:"
    flow.pc_on = False
    assert flow.try_instruction() == "Tap Computer on your iPhone, then say:"


def test_finish_writes_the_flag_and_says_no_to_a_waiting_phone(tmp_path):
    flow, svc = flow_for(tmp_path)
    flow.screen, flow.phone_on = "use", True
    prompt = svc.pairing.request("P", PHONE, "192.168.1.5")
    flow.offer(prompt)
    flow.finish()
    assert svc.pairing.status(prompt.request_id) == {"status": "denied"}
    assert '"skipped"' in svc.paths.setup_flag_file.read_text("utf-8")
    flow.finish()  # twice is harmless


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
    assert flow.screen == "use" and flow.phone_on and not flow.pc_on
    assert flow.title() == "Add your iPhone" and flow.step_label() == ""
    flow.next()
    assert flow.screen == "done"
    flow.finish()
    assert not svc.paths.setup_flag_file.exists()  # adding a phone isn't the first-run setup


def test_titles_cover_every_screen():
    assert set(setup_flow.TITLES) >= set(setup_flow.SCREENS)


# ------------------------------------------------------- no microphone at all


def test_no_microphone_turns_the_pc_card_off_and_says_why(tmp_path):
    flow, svc = flow_for(tmp_path, has_microphone=lambda: False)
    assert flow.no_mic and flow.pc_on is False
    assert flow.pc_card_text() == setup_flow.NO_MIC_LINE
    assert setup_flow.NO_MIC_LINE == (
        "No microphone found on this PC. Use your iPhone, or plug in a mic."
    )
    assert flow.pc_card_note() == ""
    flow.screen = "use"
    flow.next()  # leaving the screen saves what the card showed
    assert svc.extra["saved"] == [{"dictation": {"enabled": False}}]
    flow.back()
    flow.next()
    assert len(svc.extra["saved"]) == 1  # once


def test_no_microphone_but_switched_on_anyway(tmp_path):
    flow, svc = flow_for(tmp_path, has_microphone=lambda: False)
    flow.screen = "use"
    flow.toggle_pc()  # the person wants it (a mic is on its way)
    assert flow.pc_on is True
    assert flow.pc_card_note() == "No microphone found yet: plug one in first."
    flow.next()
    assert svc.extra["saved"] == [{"dictation": {"enabled": True}}]


def test_unknown_microphone_state_changes_nothing(tmp_path):
    flow, svc = flow_for(tmp_path, has_microphone=lambda: None)
    assert flow.pc_on is True and not flow.no_mic
    assert flow.pc_card_text() == flow.pc_line()
    flow.screen = "use"
    flow.next()
    assert svc.extra["saved"] == []


def test_phone_mode_never_asks_about_microphones(tmp_path):
    def boom():
        raise AssertionError("asked")

    svc = make_services(tmp_path, has_microphone=boom)
    flow = SetupFlow(svc, mode="phone", runner=inline_runner)
    assert not flow.no_mic and flow.pc_on is False
