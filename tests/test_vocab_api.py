"""GET/POST /vocab: body formats, the Shortcut's status field, and auth parity with /transcribe."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shoutabout.config import AccessConfig
from shoutabout.security import AccessJwtValidator
from shoutabout.server import VOCAB_MAX_BODY_BYTES
from shoutabout.vocab import parse_vocab
from test_server import (
    AUD,
    AUTH,
    LAN_IP,
    LOOPBACK,
    PUBLIC_IP,
    TEAM,
    TOKEN,
    Hub,
    make_config,
    upload,
)

HEADER = "# ShoutAbout vocabulary\n# order = priority\n"
ADD_KEYS = {"added", "status", "term", "count", "reason", "message"}


@pytest.fixture
def vocab_file(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text(HEADER + "Better Shell\nClaude Code\n", "utf-8")
    return path


def make_hub(vocab_file, **kw) -> Hub:
    hub = Hub(**kw)
    hub.ctx.vocab_path = vocab_file
    return hub


def terms(path) -> list[str]:
    return parse_vocab(path.read_text("utf-8"))


# --------------------------------------------------------------------------- GET


def test_get_vocab(vocab_file):
    r = make_hub(vocab_file).client().get("/vocab", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["terms"] == ["Better Shell", "Claude Code"]
    assert body["replacements"] == []
    assert body["count"] == 2
    assert isinstance(body["version"], int) and body["version"] >= 1


def test_get_vocab_lists_replacements_apart(vocab_file):
    c = make_hub(vocab_file).client()
    assert c.post("/vocab", headers=AUTH, json={"term": "omw -> on my way"}).status_code == 200
    body = c.get("/vocab", headers=AUTH).json()
    assert body["terms"] == ["Better Shell", "Claude Code"]
    assert body["replacements"] == [{"say": "omw", "write": "on my way"}]


def test_get_vocab_sees_new_terms_and_bumps_version(vocab_file):
    c = make_hub(vocab_file).client()
    v1 = c.get("/vocab", headers=AUTH).json()["version"]
    assert c.post("/vocab", headers=AUTH, json={"term": "GitHub"}).status_code == 200
    body = c.get("/vocab", headers=AUTH).json()
    assert body["terms"][0] == "GitHub"
    assert body["version"] == v1 + 1


def test_vocab_unavailable_without_path():
    hub = Hub()  # no vocab_path in the context
    assert hub.client().get("/vocab", headers=AUTH).status_code == 503
    assert hub.client().post("/vocab", headers=AUTH, json={"term": "x"}).status_code == 503


# -------------------------------------------------------------------------- POST


def test_post_json_like_the_shortcut(vocab_file):
    hub = make_hub(vocab_file)
    r = hub.client().post("/vocab", headers=AUTH, json={"term": "  “Chameleon CV.” "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == ADD_KEYS
    assert (body["added"], body["status"], body["term"], body["count"], body["reason"]) == (
        True,
        "added",
        "Chameleon CV",
        3,
        None,
    )
    assert "Chameleon CV" in body["message"]
    assert terms(vocab_file) == ["Chameleon CV", "Better Shell", "Claude Code"]
    assert vocab_file.read_text("utf-8").startswith(HEADER)
    assert hub.injector.pasted == []  # /vocab never types anything


def test_post_existing_term(vocab_file):
    r = make_hub(vocab_file).client().post("/vocab", headers=AUTH, json={"term": "better shell"})
    assert r.status_code == 200
    body = r.json()
    assert (body["added"], body["status"], body["term"], body["reason"]) == (
        False,
        "exists",
        "Better Shell",
        "already in vocab",
    )
    assert "already" in body["message"]
    assert terms(vocab_file) == ["Better Shell", "Claude Code"]


@pytest.mark.parametrize(
    "kw",
    [
        {"content": "GitHub", "headers": {"content-type": "text/plain"}},
        {"content": b"GitHub\n", "headers": {"content-type": "text/plain; charset=utf-8"}},
        {"content": b"\xef\xbb\xbfGitHub", "headers": {"content-type": "text/plain"}},
        {"content": b"GitHub", "headers": {"content-type": "application/octet-stream"}},
        {"content": b"GitHub"},  # no content type at all
        {"data": {"term": "GitHub"}},  # application/x-www-form-urlencoded
        {"files": {"term": (None, "GitHub")}},  # multipart text field
        {"files": {"term": ("term.txt", b"GitHub", "text/plain")}},  # multipart "File" field
        {"params": {"term": "GitHub"}},  # query string, empty body
        {"params": {"term": "GitHub"}, "json": {}},  # JSON without "term" -> query
        {"content": b'"GitHub"', "headers": {"content-type": "application/json"}},
        {
            "content": b'{"term":"GitHub"}',
            "headers": {"content-type": "application/json; charset=UTF-8"},
        },
    ],
)
def test_post_body_formats(vocab_file, kw):
    kw = dict(kw)
    kw["headers"] = {**AUTH, **kw.get("headers", {})}
    r = make_hub(vocab_file).client().post("/vocab", **kw)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["term"]) == ("added", "GitHub")
    assert terms(vocab_file)[0] == "GitHub"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"json": {"term": "#hashtag"}}, "comment"),
        ({"json": {"term": "1234"}}, "letter"),
        ({"json": {"term": "x" * 65}}, "too long"),
        ({"json": {"term": "a b c d e f g"}}, "too many words"),
        ({"json": {"term": "two\nlines"}}, "line breaks"),
        ({"json": {"term": ""}}, "no term"),
        ({"json": {}}, "no term"),
        ({"json": {"term": 42}}, "must be a string"),
        ({"content": b"{nope", "headers": {"content-type": "application/json"}}, "invalid JSON"),
        ({"content": b"\xff\xfe\xfa", "headers": {"content-type": "text/plain"}}, "UTF-8"),
        ({}, "no term"),
    ],
)
def test_post_invalid_term_400(vocab_file, kw, reason):
    before = vocab_file.read_bytes()
    kw = dict(kw)
    kw["headers"] = {**AUTH, **kw.get("headers", {})}
    r = make_hub(vocab_file).client().post("/vocab", **kw)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["status"] == "invalid"
    assert reason in body["reason"]
    assert body["detail"].startswith("invalid term: ")
    assert vocab_file.read_bytes() == before


def test_post_audio_content_type_415(vocab_file):
    r = (
        make_hub(vocab_file)
        .client()
        .post("/vocab", headers={**AUTH, "content-type": "audio/x-m4a"}, content=b"\x00\x01")
    )
    assert r.status_code == 415
    assert r.json()["status"] == "invalid"


def test_post_body_cap(vocab_file):
    c = make_hub(vocab_file).client()
    big = b"x" * (VOCAB_MAX_BODY_BYTES + 1)
    r = c.post("/vocab", headers={**AUTH, "content-type": "text/plain"}, content=big)
    assert r.status_code == 413

    async def chunks():  # no Content-Length: the streaming cap still applies
        for _ in range(10):
            yield b"y" * 1024

    r = c.post("/vocab", headers={**AUTH, "content-type": "text/plain"}, content=chunks())
    assert r.status_code == 413
    assert terms(vocab_file) == ["Better Shell", "Claude Code"]


def test_vocab_log_has_no_term_text(vocab_file, caplog):
    hub = make_hub(vocab_file)
    with caplog.at_level(logging.INFO, logger="shoutabout"):
        hub.client().post("/vocab", headers=AUTH, json={"term": "Zorblax Industries"})
        hub.client().post("/vocab", headers=AUTH, json={"term": "#Secretword"})
        hub.client().get("/vocab", headers=AUTH)
    lines = [rec.getMessage() for rec in caplog.records]
    add = [line for line in lines if line.startswith("vocab add ")]
    assert add == ["vocab add route=lan added=true status=added chars=18 count=3"]
    assert any("reason=invalid-term" in line for line in lines)
    assert "Zorblax" not in caplog.text and "Secretword" not in caplog.text
    assert "Better Shell" not in caplog.text  # GET doesn't log terms either
    assert TOKEN not in caplog.text


# ---------------------------------------------------------------- auth parity


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}]
)
def test_vocab_needs_bearer(vocab_file, method, headers):
    before = vocab_file.read_bytes()
    r = (
        make_hub(vocab_file)
        .client()
        .request(method, "/vocab", headers=headers, json={"term": "X1"})
    )
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert vocab_file.read_bytes() == before


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("ip", [PUBLIC_IP, "100.64.1.1", "2606:4700::1"])
def test_vocab_public_source_403(vocab_file, method, ip):
    before = vocab_file.read_bytes()
    r = make_hub(vocab_file).client(ip).request(method, "/vocab", headers=AUTH, json={"term": "X"})
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden"}
    assert vocab_file.read_bytes() == before


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def access(rsa_key):
    jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda t: SimpleNamespace(key=rsa_key.public_key())
    )
    return AccessJwtValidator(AccessConfig(team_domain=TEAM, aud=[AUD]), jwks_client=jwks)


def make_jwt(key, **overrides):
    now = int(time.time())
    payload = {"aud": [AUD], "iss": f"https://{TEAM}", "iat": now, "exp": now + 300}
    payload.update(overrides)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "k"})


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_vocab_tunnel_requires_jwt_when_configured(vocab_file, access, rsa_key, method):
    hub = make_hub(vocab_file, access=access)
    c = hub.client(LOOPBACK)
    assert c.request(method, "/vocab", headers=AUTH, json={"term": "Tunnel"}).status_code == 403
    bad = {**AUTH, "Cf-Access-Jwt-Assertion": make_jwt(rsa_key, aud=["other"])}
    assert c.request(method, "/vocab", headers=bad, json={"term": "Tunnel"}).status_code == 403
    assert "Tunnel" not in terms(vocab_file)
    good = {**AUTH, "Cf-Access-Jwt-Assertion": make_jwt(rsa_key)}
    assert c.request(method, "/vocab", headers=good, json={"term": "Tunnel"}).status_code == 200


def test_vocab_tunnel_jwt_checked_after_bearer(vocab_file, access, rsa_key):
    headers = {"Cf-Access-Jwt-Assertion": make_jwt(rsa_key)}
    r = make_hub(vocab_file, access=access).client(LOOPBACK).post("/vocab", headers=headers)
    assert r.status_code == 401


def test_vocab_lan_does_not_need_jwt(vocab_file, access):
    r = (
        make_hub(vocab_file, access=access)
        .client(LAN_IP)
        .post("/vocab", headers=AUTH, json={"term": "Lanword"})
    )
    assert r.status_code == 200
    assert terms(vocab_file)[0] == "Lanword"


def test_vocab_tunnel_without_access_config_bearer_only(vocab_file):
    r = make_hub(vocab_file).client(LOOPBACK).post("/vocab", headers=AUTH, json={"term": "Tun"})
    assert r.status_code == 200


def test_rate_limit_shared_with_transcribe(vocab_file):
    hub = make_hub(vocab_file, config=make_config(rate_limit_per_minute=3))
    c = hub.client()
    assert c.post("/transcribe", headers=AUTH, **upload()).status_code == 200
    assert c.get("/vocab", headers=AUTH).status_code == 200
    assert c.post("/vocab", headers=AUTH, json={"term": "One"}).status_code == 200
    r = c.post("/vocab", headers=AUTH, json={"term": "Two"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "60"
    assert c.post("/transcribe", headers=AUTH, **upload()).status_code == 429
    assert hub.client("192.168.1.21").get("/vocab", headers=AUTH).status_code == 200


def test_rate_limit_applies_before_auth(vocab_file):
    hub = make_hub(vocab_file, config=make_config(rate_limit_per_minute=2, max_auth_failures=100))
    c = hub.client()
    assert [c.get("/vocab").status_code for _ in range(3)] == [401, 401, 429]


def test_auth_failure_lockout_shared_across_endpoints(vocab_file):
    hub = make_hub(vocab_file, config=make_config(max_auth_failures=2))
    c = hub.client()
    assert (
        c.post("/vocab", headers={"Authorization": "Bearer x"}, json={"term": "A"}).status_code
        == 401
    )
    assert c.get("/vocab").status_code == 401
    # The correct token is locked out on both endpoints now.
    assert c.post("/vocab", headers=AUTH, json={"term": "A"}).status_code == 429
    assert c.post("/transcribe", headers=AUTH, **upload()).status_code == 429
    assert "A" not in terms(vocab_file)
    assert (
        hub.client("192.168.1.99").post("/vocab", headers=AUTH, json={"term": "Ok"}).status_code
        == 200
    )


def test_jwt_failures_on_vocab_count_towards_lockout(vocab_file, access):
    hub = make_hub(vocab_file, access=access, config=make_config(max_auth_failures=2))
    c = hub.client(LOOPBACK)
    bad = {**AUTH, "Cf-Access-Jwt-Assertion": "nope"}
    codes = [c.post("/vocab", headers=bad, json={"term": "Q"}).status_code for _ in range(3)]
    assert codes == [403, 403, 429]


def test_vocab_rejection_logged_without_token(vocab_file, caplog):
    with caplog.at_level(logging.WARNING, logger="shoutabout"):
        make_hub(vocab_file).client().post(
            "/vocab", headers={"Authorization": "Bearer leaked-guess"}, json={"term": "Nope"}
        )
    assert "reason=bad-bearer" in caplog.text
    assert "leaked-guess" not in caplog.text
    assert "Nope" not in caplog.text
