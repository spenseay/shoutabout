from __future__ import annotations

import pytest

from shoutabout.config import (
    ConfigError,
    Paths,
    ensure_initialized,
    load_config,
    set_server_port,
)


def test_set_server_port_rewrites_only_server_port(tmp_path):
    paths = Paths(tmp_path)
    ensure_initialized(paths)
    before = paths.config_file.read_text("utf-8")

    assert set_server_port(paths, 8766) is True
    after = paths.config_file.read_text("utf-8")
    assert load_config(paths).server.port == 8766
    # comments and other sections survive
    assert after.count("#") == before.count("#")
    assert load_config(paths).cleanup == load_config(paths).cleanup.model_copy()
    assert set_server_port(paths, 8766) is False


def test_set_server_port_inserts_when_missing(tmp_path):
    paths = Paths(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text('[server]\nhost = "127.0.0.1"\n\n[stt]\nbeam_size = 1\n', "utf-8")
    assert set_server_port(paths, 9000)
    cfg = load_config(paths)
    assert cfg.server.port == 9000
    assert cfg.server.host == "127.0.0.1"
    assert cfg.stt.beam_size == 1


def test_set_server_port_ignores_other_sections(tmp_path):
    paths = Paths(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("[stt]\nbeam_size = 1\n", "utf-8")
    assert set_server_port(paths, 9001)
    assert load_config(paths).server.port == 9001


@pytest.mark.parametrize("port", [0, 70000])
def test_set_server_port_rejects_invalid(tmp_path, port):
    paths = Paths(tmp_path)
    ensure_initialized(paths)
    with pytest.raises(ConfigError):
        set_server_port(paths, port)
