from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from shoutabout.config import (
    MIN_TOKEN_LENGTH,
    AccessConfig,
    Config,
    ConfigError,
    Paths,
    ServerConfig,
    default_home,
    ensure_initialized,
    example_config_text,
    example_vocab_text,
    generate_token,
    load_config,
    load_token,
    parse_config,
)


def test_example_file_equals_defaults():
    assert parse_config(example_config_text()) == Config()


def test_example_file_keeps_no_history_and_no_word_suggestions():
    text = example_config_text()
    assert "[history]" not in text  # ShoutAbout keeps no history (Recent is in memory)
    assert "[learn]" not in text  # word suggestions are gone (words are fixed from Recent)


def test_privacy_policy_never_mentions_looking_at_folders():
    """site/privacy.html (the live website) must match the app: ShoutAbout never looks at
    your project folders (word suggestions are gone)."""
    page = (Path(__file__).resolve().parents[1] / "site" / "privacy.html").read_text("utf-8")
    assert "project folders" not in page
    assert "from your dictation history" not in page


def test_empty_config_is_defaults():
    assert parse_config("") == Config()


def test_defaults_are_safe():
    cfg = Config()
    assert cfg.server.port == 47821
    assert cfg.server.lan_networks == ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    assert cfg.server.max_upload_bytes == 25 * 1024 * 1024
    assert cfg.access.enabled is False
    assert cfg.paste.trailing_space is False


@pytest.mark.parametrize(
    "net",
    ["8.8.8.0/24", "0.0.0.0/0", "1.1.1.1", "::/0", "2001:db8::/32", "172.0.0.0/8", "192.0.0.0/8"],
)
def test_lan_networks_reject_public(net):
    with pytest.raises(ValidationError):
        ServerConfig(lan_networks=[net])
    with pytest.raises(ConfigError, match="not a private range"):
        parse_config(f'[server]\nlan_networks = ["{net}"]\n')


@pytest.mark.parametrize(
    "net", ["100.64.0.0/10", "192.168.1.0/24", "10.0.0.5", "fd00::/8", "169.254.0.0/16"]
)
def test_lan_networks_accept_private(net):
    assert ServerConfig(lan_networks=[net]).lan_networks == [net]


def test_lan_networks_reject_garbage():
    with pytest.raises(ConfigError, match="invalid network"):
        parse_config('[server]\nlan_networks = ["not-a-net"]\n')


def test_lan_networks_validated_on_assignment():
    cfg = Config()
    with pytest.raises(ValidationError):
        cfg.server.lan_networks = ["8.8.8.0/24"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("myteam.cloudflareaccess.com", "myteam.cloudflareaccess.com"),
        ("https://MyTeam.cloudflareaccess.com/", "myteam.cloudflareaccess.com"),
        ("http://myteam.cloudflareaccess.com/cdn-cgi/access/certs", "myteam.cloudflareaccess.com"),
        ("  myteam  ", "myteam.cloudflareaccess.com"),
        ("", ""),
    ],
)
def test_team_domain_normalised(value, expected):
    assert AccessConfig(team_domain=value).team_domain == expected


def test_aud_string_becomes_list():
    assert AccessConfig(aud="abc").aud == ["abc"]
    assert AccessConfig(aud="  ").aud == []
    assert AccessConfig(aud=["a", "b"]).aud == ["a", "b"]
    cfg = parse_config('[access]\nteam_domain = "t"\naud = "tag"\n')
    assert cfg.access.aud == ["tag"]
    assert cfg.access.enabled is True


def test_access_enabled_needs_both():
    assert AccessConfig(team_domain="t").enabled is False
    assert AccessConfig(aud=["x"]).enabled is False
    assert AccessConfig(team_domain="t", aud=["x"]).enabled is True


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError):
        parse_config("[server]\nprot = 1\n")
    with pytest.raises(ConfigError):
        parse_config("[nonsense]\n")


def test_invalid_toml():
    with pytest.raises(ConfigError, match="invalid TOML"):
        parse_config("[server\nport = ")


@pytest.mark.parametrize(
    "text",
    [
        "[server]\nport = 0\n",
        "[server]\nport = 70000\n",
        "[stt]\ndevice = 'tpu'\n",
        "[stt]\nmax_prompt_tokens = 500\n",
        "[server]\nmax_upload_mb = 0\n",
    ],
)
def test_invalid_values(text):
    with pytest.raises(ConfigError):
        parse_config(text)


def test_default_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SHOUTABOUT_HOME", str(tmp_path / "x"))
    assert default_home() == tmp_path / "x"
    assert Paths.default().home == tmp_path / "x"


def test_paths_layout(tmp_path):
    p = Paths(tmp_path)
    assert p.config_file == tmp_path / "config.toml"
    assert p.token_file == tmp_path / "hub_token.txt"
    assert p.vocab_file == tmp_path / "vocab.txt"
    assert p.log_dir == tmp_path / "logs"
    assert p.history_file == tmp_path / "history.jsonl"
    assert p.audio_dir == tmp_path / "audio"
    assert p.install_state_file == tmp_path / "install-state.json"


def test_load_config_missing_is_defaults(tmp_path):
    assert load_config(Paths(tmp_path / "nothing")) == Config()


def test_ensure_initialized_idempotent(home):
    paths = Paths(home)
    first = ensure_initialized(paths)
    assert set(first.created) == {paths.config_file, paths.vocab_file, paths.token_file}
    assert first.existing == []
    assert paths.log_dir.is_dir()
    assert paths.config_file.read_text("utf-8") == example_config_text()
    assert paths.vocab_file.read_text("utf-8") == example_vocab_text()
    token = load_token(paths)
    assert len(token) >= MIN_TOKEN_LENGTH

    paths.config_file.write_text("[server]\nport = 9999\n", "utf-8")
    second = ensure_initialized(paths)
    assert second.created == []
    assert set(second.existing) == {paths.config_file, paths.vocab_file, paths.token_file}
    assert load_token(paths) == token
    assert load_config(paths).server.port == 9999  # never overwritten


def test_rotate_token(home):
    paths = Paths(home)
    ensure_initialized(paths)
    old = load_token(paths)
    report = ensure_initialized(paths, rotate_token=True)
    assert paths.token_file in report.created
    assert load_token(paths) != old


def test_short_token_regenerated(home):
    paths = Paths(home)
    paths.home.mkdir(parents=True)
    paths.token_file.write_text("short", "utf-8")
    report = ensure_initialized(paths)
    assert paths.token_file in report.created
    assert len(load_token(paths)) >= MIN_TOKEN_LENGTH


def test_missing_token_error(tmp_path):
    with pytest.raises(ConfigError, match="init"):
        load_token(Paths(tmp_path))


def test_short_token_error(tmp_path):
    (tmp_path / "hub_token.txt").write_text("abc\n", "utf-8")
    with pytest.raises(ConfigError, match="too short"):
        load_token(Paths(tmp_path))


def test_token_whitespace_stripped(tmp_path):
    token = generate_token()
    (tmp_path / "hub_token.txt").write_text(f"  {token}\r\n", "utf-8")
    assert load_token(Paths(tmp_path)) == token


def test_generate_token_random_and_long():
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= MIN_TOKEN_LENGTH for t in tokens)
