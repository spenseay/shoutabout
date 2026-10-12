"""Settings logic (no Tk), AppServices defaults and the hub's v2 wiring (hub_v2)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from shoutabout import app_services, hub_v2
from shoutabout.app_services import (
    DictationView,
    default_hint_text,
    default_hotkey_choices,
    dictation_view,
    talk_instruction,
)
from shoutabout.config import Config, load_config
from shoutabout.config_edit import ConfigEditError, update_config
from shoutabout.server import HubContext
from shoutabout.settings_model import (
    SettingsModel,
    automatic_label,
    cleanup_choice,
    cleanup_value,
    download_note,
    last_used_text,
)
from shoutabout.state import HubState
from shoutabout.winstartup import StartupManager
from v2_fakes import BEST, CPU_HW, GPU_HW, LIGHT, Downloads, make_services

NOW = datetime(2026, 10, 10, 15, 30).astimezone()


# ------------------------------------------------------------------- copy


@pytest.mark.parametrize(
    ("ago", "text"),
    [
        (timedelta(seconds=5), "Used just now"),
        (timedelta(minutes=1), "Used 1 minute ago"),
        (timedelta(minutes=12), "Used 12 minutes ago"),
        (timedelta(hours=3), "Used today at 12:30"),
        (timedelta(days=1), "Used yesterday"),
        (timedelta(days=9), "Used on 1 Oct"),
    ],
)
def test_last_used_text(ago, text):
    assert last_used_text((NOW - ago).isoformat(), NOW) == text


def test_last_used_text_missing_or_bad():
    assert last_used_text(None) == "Not used yet"
    assert last_used_text("yesterday-ish") == "Not used yet"


def test_automatic_label_and_notes():
    assert automatic_label(BEST, GPU_HW) == "Automatic: Best (NVIDIA GeForce RTX 4080 SUPER)"
    assert automatic_label(LIGHT, CPU_HW) == "Automatic: Light (CPU: Intel Core i5-8250U)"
    assert automatic_label(None, None) == "Automatic"
    assert download_note(BEST, False) == (
        "Needs a 1,600 MB download. ShoutAbout gets it next time it starts."
    )
    assert download_note(BEST, True) == "" and download_note(BEST, None) == ""
    assert download_note(SimpleNamespace(download_mb=None), False).startswith("Needs a download")


@pytest.mark.parametrize(("raw", "choice"), [(True, "on"), (False, "off"), ("auto", "auto")])
def test_cleanup_mapping(raw, choice):
    assert cleanup_choice(raw) == choice
    assert cleanup_value(choice) == raw


# ------------------------------------------------------------------ the page


EXPLICIT = {
    "stt": {"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16"},
    "cleanup": {"enabled": True},
}


def model_for(tmp_path, *, config=EXPLICIT, **kw):
    """A settings model over a config with explicit values (not whatever the example has)."""
    saved: list = []
    svc = make_services(tmp_path, saved=saved, **kw)
    update_config(svc.paths, config)
    return SettingsModel(svc), svc, saved


def test_view_reads_everything(tmp_path):
    model, svc, _ = model_for(tmp_path)
    svc.devices.add("Sam's iPhone", "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d")
    svc.startup = StartupManager(
        run_key=SimpleNamespace(get=lambda n: None), frozen=False, task_exists=lambda: True
    )
    view = model.view()
    assert view.startup_note == ""  # not read yet: that happens off the UI thread
    model.load_models()
    view = model.view()
    assert (view.pc_enabled, view.hotkey, view.mode) == (True, "right_ctrl", "toggle")
    assert view.mic_missing is False
    assert view.hotkeys["right_alt"] == "Right Alt"
    assert [(d.name, d.last_used) for d in view.devices] == [("Sam's iPhone", "Not used yet")]
    assert view.name == "DESK-1" and view.version == "0.2.0"
    assert view.cleanup == "on"  # the example config says enabled = true
    assert view.show_cleanup is False  # ...but no Ollama here: the choice stays hidden
    assert (view.startup_on, view.startup_editable) == (True, False)
    assert "setup script" in view.startup_note
    assert view.loading_quality is False


def test_quality_detection(tmp_path):
    model, _svc, _ = model_for(tmp_path)
    model.load_models()
    view = model.view()
    assert not view.loading_quality
    assert view.quality == "best"  # the explicit values match the best tier
    assert view.quality_options == ["auto", "best", "balanced", "light"]
    assert view.automatic_label == "Automatic: Best (NVIDIA GeForce RTX 4080 SUPER)"
    assert view.quality_note == ""


def test_quality_custom_values_are_not_clobbered(tmp_path):
    custom = {"stt": {"model": "medium.en", "device": "cuda", "compute_type": "float16"}}
    model, _svc, _ = model_for(tmp_path, config=custom)
    model.load_models()
    view = model.view()
    assert view.quality == "custom"
    assert view.custom_label == "Custom: medium.en on cuda (config.toml)"


def test_quality_download_note(tmp_path):
    model, _svc, _ = model_for(tmp_path, downloads=Downloads(ready=set()))
    model.load_models()
    assert model.view().quality_note.startswith("Needs a 1,600 MB download")


def test_quality_without_model_pick(tmp_path):
    def missing(*_a, **_k):
        raise ModuleNotFoundError("shoutabout.modelpick")

    model, svc, _ = model_for(tmp_path)
    svc.detect_hardware = missing
    svc.auto_model = missing
    svc.tier_choice = missing
    model.load_models()
    view = model.view()
    assert view.quality_options == ["auto"]
    assert view.automatic_label == "Automatic"


def test_changes_are_saved_with_the_right_values(tmp_path):
    model, _svc, saved = model_for(tmp_path)
    model.load_models()
    assert model.set_pc_enabled(False)
    assert model.set_hotkey("right_alt")
    assert not model.set_hotkey("caps_lock")  # not offered
    assert model.set_mode("toggle")
    assert not model.set_mode("sometimes")
    assert model.set_quality("light")
    assert model.set_quality("auto")
    assert not model.set_quality("ultra")
    assert model.set_cleanup("auto")
    assert model.set_cleanup("off")
    assert not model.set_cleanup("maybe")
    assert saved == [
        {"dictation": {"enabled": False}},
        {"dictation": {"hotkey": "right_alt"}},
        {"dictation": {"mode": "toggle"}},
        {"stt": {"model": "base.en", "device": "cpu", "compute_type": "int8"}},
        {"stt": {"model": "auto", "device": "auto", "compute_type": "auto"}},
        {"cleanup": {"enabled": "auto"}},
        {"cleanup": {"enabled": False}},
    ]
    assert model.message == "Saved." and not model.error


def test_ai_cleanup_shows_only_when_ollama_has_a_model(tmp_path):
    asked = []
    model, svc, saved = model_for(tmp_path)
    svc.cleanup_available = lambda cfg: asked.append(cfg) or False
    assert model.view().show_cleanup is False  # not looked for yet (off the UI thread)
    model.load_models()
    assert model.view().show_cleanup is False and len(asked) == 1
    svc.cleanup_available = lambda _cfg: True
    model.load_models()
    view = model.view()
    assert view.show_cleanup is True and view.cleanup == "on"
    assert model.set_cleanup("auto") and saved[-1] == {"cleanup": {"enabled": "auto"}}

    def broken(_cfg):
        raise RuntimeError("boom")

    svc.cleanup_available = broken
    model.load_models()
    assert model.view().show_cleanup is False


def test_default_cleanup_available_asks_ollama(monkeypatch):
    from shoutabout import modelpick

    cfg = Config()
    urls = []

    def tags(url, timeout=1.0):
        urls.append(url)
        return pulled

    monkeypatch.setattr(modelpick, "ollama_models", tags)
    pulled = None  # Ollama isn't running
    assert app_services.default_cleanup_available(cfg) is False
    assert urls == [cfg.cleanup.ollama_url]
    pulled = ["llama3.2:1b-made-up"]  # running, but no model the cleanup knows
    assert app_services.default_cleanup_available(cfg) is False
    pulled = [cfg.cleanup.model]
    assert app_services.default_cleanup_available(cfg) is True
    assert app_services.default_cleanup_available(SimpleNamespace()) is False  # never raises


def test_restart_message_and_save_errors(tmp_path):
    model, svc, _ = model_for(tmp_path, restart={"stt.model"})
    model.load_models()
    model.set_quality("best")
    assert model.message == "Saved. Restart ShoutAbout to apply."

    def refuse(_changes):
        raise ConfigEditError("bad")

    svc.save_settings = refuse
    assert not model.set_cleanup("on")
    assert model.error and "Couldn't save" in model.message


def test_rename_remove_and_startup(tmp_path):
    model, svc, _ = model_for(tmp_path)
    announced = []
    svc.on_identity_changed = lambda: announced.append(1)
    assert model.rename("Kitchen PC")
    assert svc.identity.get().name == "Kitchen PC" and announced == [1]
    assert not model.rename(chr(0x200B))
    assert model.error
    device, _ = svc.devices.add("P", "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d")
    assert model.remove_device(device.key)
    assert svc.devices.list() == []
    assert not model.remove_device("nope")
    assert not model.set_startup(True)  # no startup manager
    svc.startup = StartupManager(run_key=SimpleNamespace(get=lambda n: None), frozen=False)
    assert not model.set_startup(True)  # source checkout: read-only
    assert model.error


# --------------------------------------------------------------- app services


def test_dictation_view_defaults_without_the_schema():
    assert dictation_view(Config()) == DictationView(True, "right_ctrl", "toggle")
    cfg = SimpleNamespace(dictation=SimpleNamespace(enabled=False, hotkey="f8", mode="weird"))
    assert dictation_view(cfg) == DictationView(False, "f8", "toggle")
    assert dictation_view(SimpleNamespace()) == DictationView(True, "right_ctrl", "toggle")


def test_hint_text_and_hotkeys_fall_back_gracefully(monkeypatch):
    import sys

    import shoutabout

    monkeypatch.setitem(sys.modules, "shoutabout.local_dictation", None)  # as if not merged
    monkeypatch.delattr(shoutabout, "local_dictation", raising=False)  # imported by other tests
    assert default_hotkey_choices()["right_ctrl"] == "Right Ctrl"
    assert default_hint_text(Config()) == "Press Right Ctrl to talk"
    hold = SimpleNamespace(dictation=SimpleNamespace(enabled=True, hotkey="f8", mode="hold"))
    assert default_hint_text(hold) == "Hold F8 to talk"
    toggle = SimpleNamespace(dictation=SimpleNamespace(enabled=True, hotkey="f8", mode="toggle"))
    assert default_hint_text(toggle) == "Press F8 to talk"
    off = SimpleNamespace(dictation=SimpleNamespace(enabled=False, hotkey="f8", mode="hold"))
    assert default_hint_text(off) == "Talk to this PC is off"
    assert talk_instruction("F8", "toggle") == "Press F8, talk, press it again."


def test_voice_defaults_raise_until_merged_or_work_after(monkeypatch):
    """The lazy v2-voice defaults call the real modules once they exist."""
    import sys

    fake = SimpleNamespace(
        detect=lambda refresh=False: "hw",
        resolve=lambda cfg, hw=None: ("resolved", hw),
        choose=lambda hw: ("auto", hw),
        choose_tier=lambda tier, hw: (tier, hw),
        is_ready=lambda m: m == "x",
        download=lambda m, progress, cancel: f"/m/{m}",
        HOTKEY_CHOICES={"f9": "F9"},
        hint_text=lambda cfg: "Hold F9 to talk",
    )
    for name in ("hardware", "modelpick", "modelstore", "local_dictation"):
        monkeypatch.setitem(sys.modules, f"shoutabout.{name}", fake)
        monkeypatch.setattr(app_services, name, fake, raising=False)
    import shoutabout

    for name in ("hardware", "modelpick", "modelstore", "local_dictation"):
        monkeypatch.setattr(shoutabout, name, fake, raising=False)
    assert app_services.default_detect_hardware() == "hw"
    assert app_services.default_resolve_model(Config(), "hw") == ("resolved", "hw")
    assert app_services.default_auto_model("hw") == ("auto", "hw")
    assert app_services.default_tier_choice("light", "hw") == ("light", "hw")
    assert app_services.default_model_ready("x") is True
    assert app_services.default_download_model("x", progress=None, cancel=None) == "/m/x"
    assert app_services.default_hotkey_choices() == {"f9": "F9"}
    assert app_services.default_hint_text(Config()) == "Hold F9 to talk"


def test_save_settings_writes_and_hot_reloads(tmp_path):
    svc = make_services(tmp_path)
    seen = []
    svc.on_settings_saved = lambda keys: (seen.append(keys), {"cleanup.enabled"})[1]
    assert svc.save_settings({"cleanup": {"enabled": False}}) == {"cleanup.enabled"}
    assert seen == [{"cleanup.enabled"}]
    assert load_config(svc.paths).cleanup.enabled is False
    assert svc.save_settings({"cleanup": {"enabled": False}}) == set()  # unchanged
    with pytest.raises(ConfigEditError):
        svc.save_settings({"cleanup": {"enabled": "sometimes"}})
    svc.on_settings_saved = None
    assert svc.save_settings({"cleanup": {"enabled": True}}) == {"cleanup.enabled"}


def test_legacy_qr_text(tmp_path):
    svc = make_services(tmp_path)
    svc.lan_url = lambda: "http://192.168.1.50:47821"
    payload = json.loads(svc.legacy_qr_text())
    assert payload["shoutabout"] == 1 and payload["lan_url"] == "http://192.168.1.50:47821"


# --------------------------------------------------------------------- hub_v2


def _ctx() -> HubContext:
    return HubContext(config=Config(), token="H" * 43, state=HubState())


def test_start_wires_the_context(tmp_path):
    from shoutabout.config import Paths, ensure_initialized

    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    cfg = load_config(paths)
    cfg.server.discovery = False
    ctx = _ctx()
    hub = hub_v2.start(paths, cfg, ctx, HubState(), with_ui=False, notify=lambda *a: None)
    assert ctx.devices is hub.services.devices
    assert ctx.pairing is hub.services.pairing
    assert ctx.pairing.enabled is False  # no window to say yes in: refuse requests
    assert ctx.identity.get().id
    assert hub.advertiser is None and hub.ui is None
    assert hub.services.on_settings_saved == hub.on_settings_saved
    hub.stop()


def test_on_settings_saved_uses_reloaders(tmp_path):
    from shoutabout.config import Paths, ensure_initialized

    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    cfg = load_config(paths)
    cfg.server.discovery = False
    hub = hub_v2.start(paths, cfg, _ctx(), HubState(), with_ui=False, notify=lambda *a: None)
    applied = []
    hub.reloaders["cleanup"] = lambda new_cfg, key: applied.append(key) or True
    hub.reloaders["dictation"] = lambda new_cfg, key: False

    def broken(new_cfg, key):
        raise RuntimeError("boom")

    hub.reloaders["learn"] = broken
    restart = hub.on_settings_saved(
        {"cleanup.enabled", "dictation.hotkey", "stt.model", "learn.enabled"}
    )
    assert applied == ["cleanup.enabled"]
    assert restart == {"dictation.hotkey", "stt.model", "learn.enabled"}


def test_model_info_is_cached(tmp_path):
    from shoutabout.config import Paths, ensure_initialized

    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    cfg = load_config(paths)
    cfg.server.discovery = False
    hub = hub_v2.start(paths, cfg, _ctx(), HubState(), with_ui=False, notify=lambda *a: None)
    calls = []
    hub.services.resolve_model = lambda c, hw=None: calls.append(1) or BEST
    assert hub.model_info() == BEST and hub.model_info() == BEST
    assert len(calls) == 1
    hub.on_settings_saved(set())  # a settings change drops the cache
    hub.model_info()
    assert len(calls) == 2


def test_announce_uses_identity_and_port(tmp_path):
    from shoutabout.config import Paths, ensure_initialized

    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    cfg = load_config(paths)
    cfg.server.discovery = False
    hub = hub_v2.start(paths, cfg, _ctx(), HubState(), with_ui=False, notify=lambda *a: None)
    seen = []
    hub.advertiser = SimpleNamespace(update=seen.append, stop=lambda: seen.append("stop"))
    hub.announce()
    (info,) = seen
    assert info.port == 47821
    assert dict(info.txt)["name"] == hub.services.identity.get().name
    hub.services.identity.rename("Study")
    hub.services.on_identity_changed()
    assert dict(seen[-1].txt)["name"] == "Study"
    hub.stop()
    assert seen[-1] == "stop"


def test_settings_say_when_there_is_no_microphone(tmp_path):
    from shoutabout.settings_model import NO_MIC_NOTE, SettingsModel

    assert SettingsModel(make_services(tmp_path)).view().mic_missing is False
    svc = make_services(tmp_path / "b", has_microphone=lambda: False)
    assert SettingsModel(svc).view().mic_missing is True
    assert "No microphone found" in NO_MIC_NOTE

    def broken():
        raise OSError("winmm")

    model = SettingsModel(make_services(tmp_path / "c", has_microphone=broken))
    assert model.view().mic_missing is False


def test_mode_choices_put_press_to_start_first():
    from shoutabout.settings_model import MODE_CHOICES

    assert list(MODE_CHOICES) == ["toggle", "hold"]


def test_settings_have_no_history_switch(tmp_path):
    """ShoutAbout keeps no history (Recent is in memory): nothing to switch on."""
    model, _svc, _ = model_for(tmp_path)
    assert not hasattr(model, "set_history")
    assert not hasattr(model.view(), "history_on")
