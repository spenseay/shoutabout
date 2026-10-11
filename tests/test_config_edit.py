"""Comment-preserving config edits (config_edit) and the first-run helpers (firstrun)."""

from __future__ import annotations

import socket

import pytest

from shoutabout import config_edit, firstrun, hub_v2
from shoutabout.config import Config, ConfigError, Paths, example_vocab_text, parse_config
from shoutabout.config_edit import ConfigEditError, apply_changes, read_value, update_config

OWNER_STYLE = """\
# My hub. Hand-edited, keep my comments!

[server]
host = "0.0.0.0"
port = 8766   # 8765 is taken by another app

[stt]
model = "large-v3-turbo"
device = "cuda"            # "cuda" or "cpu"

[cleanup]
enabled = true             # false = skip the LLM
model = "qwen3:4b-instruct-2507-q4_K_M"

[logging]
level = "INFO"
"""


def permissive(_text, _source):
    return None


def test_change_keeps_every_other_byte():
    new, changed = apply_changes(OWNER_STYLE, {"cleanup": {"enabled": False}})
    assert changed
    assert new == OWNER_STYLE.replace(
        "enabled = true             # false", "enabled = false             # false"
    )


def test_same_value_is_not_a_change():
    new, changed = apply_changes(OWNER_STYLE, {"server": {"port": 8766}})
    assert not changed and new == OWNER_STYLE
    # bool vs int are different values even though True == 1
    _new, changed = apply_changes("[a]\nx = 1\n", {"a": {"x": True}})
    assert changed


def test_new_section_and_key_are_appended():
    new, changed = apply_changes(
        OWNER_STYLE, {"dictation": {"hotkey": "right_alt", "enabled": True}}
    )
    assert changed
    assert new.startswith(OWNER_STYLE.rstrip("\n"))
    assert '[dictation]\nhotkey = "right_alt"\nenabled = true\n' in new
    new2, _ = apply_changes(OWNER_STYLE, {"server": {"discovery": False}})
    assert "discovery = false" in new2
    assert parse_config(new2).server.discovery is False


def test_none_removes_a_key():
    new, changed = apply_changes(OWNER_STYLE, {"stt": {"device": None}})
    assert changed and "device" not in new
    _new, changed = apply_changes(OWNER_STYLE, {"stt": {"nope": None}, "zzz": {"a": None}})
    assert not changed


def test_invalid_toml_on_disk_is_an_error():
    with pytest.raises(ConfigEditError):
        apply_changes("[server\nport = 1", {"server": {"port": 2}})
    with pytest.raises(ConfigEditError):
        apply_changes("server = 5\n", {"server": {"port": 2}})


def test_update_config_validates_and_writes_atomically(tmp_path):
    paths = Paths(tmp_path)
    paths.config_file.write_text(OWNER_STYLE, "utf-8")
    assert update_config(paths, {"cleanup": {"enabled": False}})
    assert parse_config(paths.config_file.read_text("utf-8")).cleanup.enabled is False
    assert "# 8765 is taken by another app" in paths.config_file.read_text("utf-8")
    assert not update_config(paths, {"cleanup": {"enabled": False}})
    # An invalid result is refused and nothing is written.
    before = paths.config_file.read_bytes()
    with pytest.raises(ConfigEditError):
        update_config(paths, {"server": {"port": 70000}})
    with pytest.raises(ConfigEditError):
        update_config(paths, {"no_such_section": {"x": 1}})
    assert paths.config_file.read_bytes() == before
    assert not list(tmp_path.glob(".config.toml.*"))  # no temp files left behind


def test_update_config_creates_from_the_example(tmp_path):
    paths = Paths(tmp_path)
    assert update_config(paths, {"server": {"port": 47999}})
    text = paths.config_file.read_text("utf-8")
    assert "port = 47999" in text and "[cleanup]" in text and "#" in text
    assert parse_config(text).server.port == 47999


def test_custom_validator(tmp_path):
    paths = Paths(tmp_path)
    paths.config_file.write_text(OWNER_STYLE, "utf-8")
    assert update_config(paths, {"dictation": {"hotkey": "f8"}}, validate=permissive)
    assert read_value(paths, "dictation", "hotkey") == "f8"

    def refuse(_text, _source):
        raise ConfigError("nope")

    with pytest.raises(ConfigEditError):
        update_config(paths, {"dictation": {"hotkey": "f9"}}, validate=refuse)


def test_read_value(tmp_path):
    paths = Paths(tmp_path)
    assert read_value(paths, "server", "port", 1) == 1
    paths.config_file.write_text(OWNER_STYLE, "utf-8")
    assert read_value(paths, "server", "port") == 8766
    assert read_value(paths, "server", "nope", "d") == "d"
    assert read_value(paths, "nope", "x") is None
    paths.config_file.write_text("[[[", "utf-8")
    assert read_value(paths, "server", "port", 3) == 3


def test_config_edit_uses_one_lock():
    assert config_edit._lock is not None


# ------------------------------------------------------------------ first run


def _fresh(tmp_path) -> Paths:
    paths = Paths(tmp_path / "home")
    paths.home.mkdir()
    return paths


def test_fresh_install_needs_setup(tmp_path):
    paths = _fresh(tmp_path)
    before = firstrun.snapshot(paths)
    assert not before.existing_install
    assert firstrun.needs_setup(paths, before)
    firstrun.mark_setup_complete(paths, "finished")
    assert firstrun.is_setup_complete(paths)
    assert not firstrun.needs_setup(paths, firstrun.snapshot(paths))


def test_installer_init_without_use_still_needs_setup(tmp_path):
    """token + the example vocab (what `init` makes) isn't an install in use."""
    paths = _fresh(tmp_path)
    paths.token_file.write_text("t" * 43, "utf-8")
    paths.vocab_file.write_text(example_vocab_text(), "utf-8")
    before = firstrun.snapshot(paths)
    assert not before.existing_install
    assert firstrun.needs_setup(paths, before)


@pytest.mark.parametrize("used", ["history", "vocab", "install_state"])
def test_existing_install_never_sees_the_wizard(tmp_path, used):
    paths = _fresh(tmp_path)
    paths.token_file.write_text("t" * 43, "utf-8")
    paths.vocab_file.write_text(example_vocab_text(), "utf-8")
    if used == "history":
        paths.history_file.write_text("{}\n", "utf-8")
    elif used == "vocab":
        paths.vocab_file.write_text("Claude Code\nBetter Shell\n", "utf-8")
    else:
        paths.install_state_file.write_text("{}", "utf-8")
    before = firstrun.snapshot(paths)
    assert before.existing_install
    assert not firstrun.needs_setup(paths, before)
    assert firstrun.is_setup_complete(paths)  # remembered from now on
    assert '"existing-install"' in paths.setup_flag_file.read_text("utf-8")


def test_history_without_token_is_not_an_install(tmp_path):
    paths = _fresh(tmp_path)
    paths.history_file.write_text("{}\n", "utf-8")
    assert not firstrun.snapshot(paths).existing_install


def test_pick_port():
    busy = {47821, 47822}
    assert firstrun.pick_port(47821, lambda p: p not in busy) == 47823
    assert firstrun.pick_port(47821, lambda p: True) == 47821
    assert firstrun.pick_port(65534, lambda p: False) is None


def test_port_is_free_sees_a_listener():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert not firstrun.port_is_free("127.0.0.1", port)
    assert firstrun.port_is_free("127.0.0.1", port) in (True, False)  # just doesn't raise


def test_first_run_port_moves_a_fresh_install(tmp_path, monkeypatch):
    paths = _fresh(tmp_path)
    from shoutabout.config import ensure_initialized, load_config

    before = firstrun.snapshot(paths)
    ensure_initialized(paths)
    cfg = load_config(paths)
    assert cfg.server.port == 47821
    monkeypatch.setattr(hub_v2, "port_is_free", lambda host, port: port >= 47823)
    cfg = hub_v2.first_run_port(paths, cfg, before)
    assert cfg.server.port == 47823
    assert "port = 47823" in paths.config_file.read_text("utf-8")


def test_first_run_port_never_moves_an_existing_install(tmp_path, monkeypatch):
    paths = _fresh(tmp_path)
    paths.token_file.write_text("t" * 43, "utf-8")
    paths.history_file.write_text("{}\n", "utf-8")
    paths.config_file.write_text(OWNER_STYLE, "utf-8")
    before = firstrun.snapshot(paths)
    monkeypatch.setattr(hub_v2, "port_is_free", lambda host, port: False)
    cfg = parse_config(OWNER_STYLE)
    assert hub_v2.first_run_port(paths, cfg, before).server.port == 8766
    assert paths.config_file.read_text("utf-8") == OWNER_STYLE


def test_new_configs_default_to_port_47821_and_discovery_on():
    cfg = Config()
    assert cfg.server.port == 47821
    assert cfg.server.discovery is True
