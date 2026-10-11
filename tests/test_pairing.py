from __future__ import annotations

import json

import pytest

from shoutabout import cli, netinfo, pairing
from shoutabout.pairing import (
    CF_ID_ENV,
    CF_SECRET_ENV,
    REDACTED,
    CloudflareCredentials,
    PairingError,
    base_url,
    build_payload,
    cf_from_env,
    parse_cf_text,
    redacted,
    to_json,
)

# Dummy values only: nothing here is a real secret.
TOKEN = "dummy-hub-token-" + "x" * 32
CF_ID = "dummy-client-id.access"
CF_SECRET = "dummy-client-secret-" + "y" * 24
CF = CloudflareCredentials(CF_ID, CF_SECRET)
KEYS = ["shoutabout", "lan_url", "public_url", "token", "cf_client_id", "cf_client_secret", "ssid"]


# ---------------------------------------------------------------------- payload


def test_payload_has_exactly_the_documented_keys():
    payload = build_payload(
        lan_url="http://192.168.1.50:8765",
        token=TOKEN,
        public_url="https://dictate.example.test",
        cloudflare=CF,
        ssid="Home Net",
    )
    assert list(payload) == KEYS
    assert payload == {
        "shoutabout": 1,
        "lan_url": "http://192.168.1.50:8765",
        "public_url": "https://dictate.example.test",
        "token": TOKEN,
        "cf_client_id": CF_ID,
        "cf_client_secret": CF_SECRET,
        "ssid": "Home Net",
    }


def test_payload_without_optional_values():
    payload = build_payload(lan_url="http://192.168.1.50:8765/", token=f" {TOKEN}\n", ssid="  ")
    assert payload == {
        "shoutabout": 1,
        "lan_url": "http://192.168.1.50:8765",
        "public_url": None,
        "token": TOKEN,
        "ssid": None,
    }
    assert "cf_client_id" not in payload


def test_payload_rejects_an_empty_token():
    with pytest.raises(PairingError, match="token"):
        build_payload(lan_url="http://192.168.1.50:8765", token="  ")


@pytest.mark.parametrize(
    ("url", "https_only", "expected"),
    [
        ("http://192.168.1.50:8765", False, "http://192.168.1.50:8765"),
        ("http://192.168.1.50:8765/transcribe", False, "http://192.168.1.50:8765"),
        ("http://192.168.1.50:8765/vocab/", False, "http://192.168.1.50:8765"),
        ("HTTPS://Dictate.example.test/transcribe", True, "https://Dictate.example.test"),
        ("dictate.example.test", True, "https://dictate.example.test"),
        ("https://example.test/hub/transcribe", True, "https://example.test/hub"),
        ("http://[fd00::5]:8765", False, "http://[fd00::5]:8765"),
    ],
)
def test_base_url(url, https_only, expected):
    assert base_url(url, https_only=https_only) == expected


@pytest.mark.parametrize(
    ("url", "https_only"),
    [
        ("192.168.1.50:8765", False),
        ("ftp://192.168.1.50", False),
        ("http://dictate.example.test", True),
        ("http://192.168.1.50:8765/?x=1", False),
        ("https://dictate.example.test/#frag", True),
    ],
)
def test_base_url_rejects(url, https_only):
    with pytest.raises(PairingError):
        base_url(url, https_only=https_only)


def test_redacted_hides_only_the_secrets():
    payload = build_payload(
        lan_url="http://192.168.1.50:8765", token=TOKEN, cloudflare=CF, ssid="Home"
    )
    hidden = redacted(payload)
    assert hidden["token"] == hidden["cf_client_id"] == hidden["cf_client_secret"] == REDACTED
    assert hidden["lan_url"] == payload["lan_url"]
    assert hidden["ssid"] == "Home"
    assert payload["token"] == TOKEN  # not modified in place
    text = to_json(hidden)
    for secret in (TOKEN, CF_ID, CF_SECRET):
        assert secret not in text


def test_to_json_is_compact_and_keeps_unicode():
    payload = build_payload(lan_url="http://192.168.1.50:8765", token=TOKEN, ssid="Café 5 GHz")
    text = to_json(payload)
    assert ", " not in text and ": " not in text
    assert "Café" in text
    assert json.loads(text) == payload


def test_credentials_repr_hides_the_values():
    assert CF_SECRET not in repr(CF)
    assert CF_ID not in repr(CF)


# ------------------------------------------------------------ Cloudflare values


def test_parse_cf_text_dashboard_lines():
    text = f"CF-Access-Client-Id: {CF_ID}\r\nCF-Access-Client-Secret: {CF_SECRET}\r\n"
    assert parse_cf_text(text) == CF


def test_parse_cf_text_curl_lines_and_case():
    text = (
        "curl https://dictate.example.test/health \\\n"
        f'  -H "cf-access-client-id: {CF_ID}" \\\n'
        f"  -H 'CF-ACCESS-CLIENT-SECRET:{CF_SECRET}'\n"
    )
    assert parse_cf_text(text) == CF


def test_parse_cf_text_repeated_identical_lines_are_fine():
    line = f"CF-Access-Client-Id: {CF_ID}\nCF-Access-Client-Secret: {CF_SECRET}\n"
    assert parse_cf_text(line + line) == CF


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"CF-Access-Client-Id: {CF_ID}\n", "CF-Access-Client-Secret"),
        (f"CF-Access-Client-Secret: {CF_SECRET}\n", "CF-Access-Client-Id"),
        ("nothing useful here\n", "CF-Access-Client-Id"),
        (
            f"CF-Access-Client-Id: {CF_ID}\nCF-Access-Client-Id: other.access\n"
            f"CF-Access-Client-Secret: {CF_SECRET}\n",
            "more than one",
        ),
    ],
)
def test_parse_cf_text_errors(text, message):
    with pytest.raises(PairingError, match=message) as err:
        parse_cf_text(text, source="cf.txt")
    assert CF_SECRET not in str(err.value)
    assert "cf.txt" in str(err.value)


def test_cf_from_env():
    assert cf_from_env({}) is None
    assert cf_from_env({CF_ID_ENV: f" {CF_ID} ", CF_SECRET_ENV: CF_SECRET}) == CF
    with pytest.raises(PairingError, match="Client Secret"):
        cf_from_env({CF_ID_ENV: CF_ID})
    with pytest.raises(PairingError, match="Client ID"):
        cf_from_env({CF_SECRET_ENV: CF_SECRET, CF_ID_ENV: "  "})


# ------------------------------------------------------------------- pair (CLI)


@pytest.fixture
def paired_home(home, monkeypatch):
    """A temp config dir with a dummy token; LAN 192.168.1.50, Wi-Fi "Home Net"."""
    home.mkdir(parents=True)
    (home / "hub_token.txt").write_text(TOKEN + "\n", "utf-8")
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: "192.168.1.50")
    monkeypatch.setattr(netinfo, "wifi_ssid", lambda: "Home Net")
    monkeypatch.delenv(CF_ID_ENV, raising=False)
    monkeypatch.delenv(CF_SECRET_ENV, raising=False)
    return home


@pytest.fixture
def shown_qr(monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(cli, "print_qr", lambda text, **kwargs: shown.append(text))
    return shown


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_pair_json_is_redacted_by_default(paired_home, capsys):
    assert cli.main(["pair", "--json"]) == 0
    out = _json_out(capsys)
    assert out == {
        "shoutabout": 1,
        "lan_url": "http://192.168.1.50:47821",
        "public_url": None,
        "token": REDACTED,
        "ssid": "Home Net",
    }


def test_pair_json_show_secrets(paired_home, capsys, monkeypatch):
    (paired_home / "install-state.json").write_text(
        json.dumps({"public_hostname": "dictate.example.test"}), "utf-8"
    )
    monkeypatch.setenv(CF_ID_ENV, CF_ID)
    monkeypatch.setenv(CF_SECRET_ENV, CF_SECRET)
    assert cli.main(["pair", "--json", "--show-secrets"]) == 0
    out = _json_out(capsys)
    assert list(out) == KEYS
    assert out["token"] == TOKEN
    assert out["public_url"] == "https://dictate.example.test"
    assert (out["cf_client_id"], out["cf_client_secret"]) == (CF_ID, CF_SECRET)


def test_pair_show_secrets_needs_json(paired_home, capsys, shown_qr):
    assert cli.main(["pair", "--show-secrets"]) == 1
    assert "--json" in capsys.readouterr().err
    assert shown_qr == []


def test_pair_shows_the_payload_as_a_qr_code(paired_home, capsys, shown_qr):
    assert cli.main(["pair", "--no-public", "--ssid", "Other Net"]) == 0
    (text,) = shown_qr
    assert json.loads(text) == {
        "shoutabout": 1,
        "lan_url": "http://192.168.1.50:47821",
        "public_url": None,
        "token": TOKEN,
        "ssid": "Other Net",
    }
    printed = capsys.readouterr().out
    assert TOKEN not in printed  # only inside the QR code
    assert "rotate-token" in printed
    assert "'Other Net'" in printed


def test_pair_real_terminal_qr_hides_the_text(paired_home, capsys):
    assert cli.main(["pair"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert len(out.splitlines()) > 20


def test_pair_open_uses_a_temporary_image(paired_home, capsys, monkeypatch, shown_qr):
    opened: list[str] = []
    monkeypatch.setattr(cli, "_open_qr_svg", lambda text: opened.append(text) or 0)
    assert cli.main(["pair", "--open"]) == 0
    assert shown_qr == []
    assert json.loads(opened[0])["token"] == TOKEN


def test_pair_cf_file(paired_home, tmp_path, capsys):
    cf_file = tmp_path / "cf.txt"
    cf_file.write_text(f"CF-Access-Client-Id: {CF_ID}\nCF-Access-Client-Secret: {CF_SECRET}\n")
    rc = cli.main(
        ["pair", "--json", "--show-secrets", "--cf-file", str(cf_file)]
        + ["--public-url", "https://dictate.example.test/transcribe"]
    )
    assert rc == 0
    out = _json_out(capsys)
    assert (out["cf_client_id"], out["cf_client_secret"]) == (CF_ID, CF_SECRET)
    assert out["public_url"] == "https://dictate.example.test"


def test_pair_cf_file_problems(paired_home, tmp_path, capsys):
    assert cli.main(["pair", "--json", "--cf-file", str(tmp_path / "missing.txt")]) == 1
    assert "can't read" in capsys.readouterr().err
    bad = tmp_path / "bad.txt"
    bad.write_text(f"CF-Access-Client-Id: {CF_ID}\n")
    assert cli.main(["pair", "--json", "--cf-file", str(bad)]) == 1
    assert "CF-Access-Client-Secret" in capsys.readouterr().err


def test_pair_cloudflare_prompt(paired_home, capsys, monkeypatch, shown_qr):
    answers = iter([CF_ID, CF_SECRET])
    prompts: list[str] = []

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    assert cli.main(["pair", "--cloudflare", "--public-url", "dictate.example.test"]) == 0
    assert len(prompts) == 2 and all("hidden" in p for p in prompts)
    payload = json.loads(shown_qr[0])
    assert (payload["cf_client_id"], payload["cf_client_secret"]) == (CF_ID, CF_SECRET)
    printed = capsys.readouterr()
    assert CF_SECRET not in printed.out + printed.err
    assert "service token included" in printed.out


def test_pair_cloudflare_prompt_left_empty(paired_home, capsys, monkeypatch, shown_qr):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    assert cli.main(["pair", "--cloudflare"]) == 1
    assert "no Cloudflare values" in capsys.readouterr().err
    assert shown_qr == []


def test_pair_incomplete_env_is_an_error(paired_home, capsys, monkeypatch, shown_qr):
    monkeypatch.setenv(CF_ID_ENV, CF_ID)
    assert cli.main(["pair"]) == 1
    assert CF_SECRET_ENV in capsys.readouterr().err
    assert shown_qr == []


def test_pair_without_wifi_name(paired_home, capsys, monkeypatch, shown_qr):
    monkeypatch.setattr(netinfo, "wifi_ssid", lambda: None)
    assert cli.main(["pair"]) == 0
    assert json.loads(shown_qr[0])["ssid"] is None
    assert "--ssid" in capsys.readouterr().out


def test_pair_needs_the_hub_token(home, capsys, monkeypatch, shown_qr):
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: "192.168.1.50")
    assert cli.main(["pair", "--json"]) == 1
    assert "init" in capsys.readouterr().err
    assert shown_qr == []


def test_pair_needs_a_lan_url(paired_home, capsys, monkeypatch):
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: None)
    assert cli.main(["pair", "--json"]) == 1
    assert "--lan-url" in capsys.readouterr().err
    assert cli.main(["pair", "--json", "--lan-url", "http://10.0.0.5:9000/transcribe"]) == 0
    assert _json_out(capsys)["lan_url"] == "http://10.0.0.5:9000"


def test_pair_rejects_a_plain_http_public_url(paired_home, capsys):
    assert cli.main(["pair", "--json", "--public-url", "http://dictate.example.test"]) == 1
    assert "https://" in capsys.readouterr().err


def test_pairing_module_never_writes_files(paired_home, tmp_path, monkeypatch, shown_qr):
    before = sorted(p.name for p in paired_home.iterdir())
    assert cli.main(["pair"]) == 0
    assert sorted(p.name for p in paired_home.iterdir()) == before
    assert pairing.PAYLOAD_VERSION == 1
