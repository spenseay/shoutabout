"""v2 config: "auto" model settings (new configs) and the [dictation] section."""

from __future__ import annotations

import pytest

from shoutabout.config import Config, ConfigError, example_config_text, parse_config


def test_new_configs_are_automatic():
    cfg = parse_config(example_config_text())
    assert (cfg.stt.model, cfg.stt.device, cfg.stt.compute_type) == ("auto", "auto", "auto")
    assert cfg.cleanup.enabled == "auto"


def test_v1_config_with_explicit_values_is_unchanged():
    cfg = parse_config(
        '[stt]\nmodel = "large-v3-turbo"\ndevice = "cuda"\ncompute_type = "float16"\n'
        "[cleanup]\nenabled = true\n"
    )
    assert (cfg.stt.model, cfg.stt.device, cfg.stt.compute_type) == (
        "large-v3-turbo",
        "cuda",
        "float16",
    )
    assert cfg.cleanup.enabled is True
    assert parse_config("[cleanup]\nenabled = false\n").cleanup.enabled is False


@pytest.mark.parametrize(
    "text",
    [
        '[stt]\ndevice = "gpu"\n',
        '[cleanup]\nenabled = "sometimes"\n',
        '[dictation]\nhotkey = "left_pinky"\n',
        '[dictation]\nmode = "double-tap"\n',
        "[dictation]\nmax_seconds = 1\n",
        "[dictation]\nmax_seconds = 9999\n",
        "[dictation]\nunknown = 1\n",
    ],
)
def test_bad_v2_values_are_rejected(text):
    with pytest.raises(ConfigError):
        parse_config(text)


def test_dictation_defaults_and_choices():
    d = Config().dictation
    assert (d.enabled, d.hotkey, d.mode, d.overlay, d.max_seconds) == (
        True,
        "right_ctrl",
        "toggle",  # press to start, press to stop (owner feedback)
        True,
        600,  # the hub's own limit for a phone recording
    )
    example = parse_config(example_config_text()).dictation
    assert (example.mode, example.max_seconds) == ("toggle", 600)
    cfg = parse_config('[dictation]\nhotkey = "F9"\nmode = "toggle"\noverlay = false\n')
    assert (cfg.dictation.hotkey, cfg.dictation.mode, cfg.dictation.overlay) == (
        "f9",
        "toggle",
        False,
    )


def test_example_lists_every_hotkey_choice():
    from shoutabout.hotkey import HOTKEY_CHOICES

    text = example_config_text()
    for key in HOTKEY_CHOICES:
        assert key in text
