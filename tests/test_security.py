from __future__ import annotations

import base64
import json
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shoutabout.config import AccessConfig, ServerConfig
from shoutabout.security import (
    AccessJwtError,
    AccessJwtValidator,
    RateLimiter,
    bearer_ok,
    classify_source,
    parse_ip,
)

LAN = ServerConfig().lan_networks
TEAM = "myteam.cloudflareaccess.com"
AUD = "a" * 64


# ------------------------------------------------------------------ classify_source


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "tunnel"),
        ("127.5.6.7", "tunnel"),
        ("::1", "tunnel"),
        ("::ffff:127.0.0.1", "tunnel"),
        ("[::1]", "tunnel"),
        ("192.168.1.5", "lan"),
        ("192.168.1.50", "lan"),
        ("10.0.0.7", "lan"),
        ("172.16.0.1", "lan"),
        ("172.31.255.254", "lan"),
        ("::ffff:192.168.1.5", "lan"),
        ("8.8.8.8", None),
        ("1.1.1.1", None),
        ("172.32.0.1", None),
        ("100.64.1.1", None),  # CGNAT/Tailscale: not allowed by default
        ("169.254.10.10", None),
        ("0.0.0.0", None),
        ("255.255.255.255", None),
        ("2606:4700:4700::1111", None),
        ("::ffff:8.8.8.8", None),
        ("fe80::1", None),
        ("testclient", None),
        ("", None),
        ("   ", None),
        ("localhost", None),
        ("999.1.1.1", None),
        ("192.168.1.5:1234", None),
        (None, None),
    ],
)
def test_classify_source_default_networks(host, expected):
    assert classify_source(host, LAN) == expected


def test_classify_source_custom_networks():
    nets = ["100.64.0.0/10", "fd00::/8"]
    assert classify_source("100.64.1.1", nets) == "lan"
    assert classify_source("fd12::5", nets) == "lan"
    assert classify_source("192.168.1.5", nets) is None
    assert classify_source("127.0.0.1", nets) == "tunnel"
    assert classify_source("127.0.0.1", []) == "tunnel"
    assert classify_source("192.168.1.5", []) is None


def test_classify_source_ignores_public_networks_even_if_passed():
    # Config validation rejects these; classify_source must not trust them either.
    assert classify_source("8.8.8.8", ["0.0.0.0/0"]) is None
    assert classify_source("8.8.8.8", ["8.8.8.0/24", "garbage"]) is None


def test_parse_ip_non_string():
    assert parse_ip(123) is None
    assert str(parse_ip("::ffff:10.1.2.3")) == "10.1.2.3"


# --------------------------------------------------------------------------- bearer

TOKEN = "t" * 43


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        (f"Bearer {TOKEN}", True),
        (f"bearer {TOKEN}", True),
        (f"BEARER {TOKEN}", True),
        (f"  Bearer   {TOKEN}  ", True),
        (f"Bearer\t{TOKEN}", True),
        (f"Bearer {TOKEN}x", False),
        (f"Bearer {TOKEN[:-1]}", False),
        (f"Basic {TOKEN}", False),
        (TOKEN, False),
        ("Bearer", False),
        ("Bearer ", False),
        ("", False),
        (None, False),
        (f"Bearer {TOKEN} extra", False),
        ("Bearer ünïcödé", False),
    ],
)
def test_bearer_ok(header, ok):
    assert bearer_ok(header, TOKEN) is ok


def test_bearer_empty_token_never_matches():
    assert bearer_ok("Bearer ", "") is False
    assert bearer_ok("Bearer x", "") is False


# ----------------------------------------------------------------------------- JWT


@pytest.fixture(scope="module")
def rsa_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, other


class FakeJwks:
    def __init__(self, public_key):
        self.public_key = public_key
        self.calls = 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        return SimpleNamespace(key=self.public_key)


class FailingJwks:
    def get_signing_key_from_jwt(self, token):
        raise jwt.PyJWKClientConnectionError("network down")


def access_cfg(**kw) -> AccessConfig:
    data = {"team_domain": TEAM, "aud": [AUD]}
    data.update(kw)
    return AccessConfig(**data)


def claims(**overrides):
    now = int(time.time())
    data = {
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 300,
        "sub": "",
        "common_name": "client-id-1.access",
        "type": "app",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def sign(private_key, payload, alg="RS256", kid="kid-1"):
    return jwt.encode(payload, private_key, algorithm=alg, headers={"kid": kid})


def b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def validator(rsa_keys):
    key, _ = rsa_keys
    return AccessJwtValidator(access_cfg(), jwks_client=FakeJwks(key.public_key()))


def test_jwt_valid(rsa_keys, validator):
    key, _ = rsa_keys
    result = validator.validate(sign(key, claims()))
    assert result["common_name"] == "client-id-1.access"


def test_jwt_urls_and_issuer():
    v = AccessJwtValidator(access_cfg(team_domain="https://MyTeam.cloudflareaccess.com/"))
    assert v.issuer == "https://myteam.cloudflareaccess.com"
    assert v.certs_url == "https://myteam.cloudflareaccess.com/cdn-cgi/access/certs"
    assert isinstance(v._jwks, jwt.PyJWKClient)


def test_jwt_validator_requires_config():
    with pytest.raises(ValueError):
        AccessJwtValidator(AccessConfig())


def test_jwt_aud_as_string_claim(rsa_keys, validator):
    key, _ = rsa_keys
    assert validator.validate(sign(key, claims(aud=AUD)))["aud"] == AUD


def test_jwt_one_of_multiple_configured_auds(rsa_keys):
    key, _ = rsa_keys
    v = AccessJwtValidator(access_cfg(aud=["other", AUD]), jwks_client=FakeJwks(key.public_key()))
    v.validate(sign(key, claims()))


def test_jwt_within_leeway_accepted(rsa_keys, validator):
    key, _ = rsa_keys
    now = int(time.time())
    validator.validate(sign(key, claims(exp=now - 30, iat=now - 600)))


@pytest.mark.parametrize(
    ("payload_kw", "message"),
    [
        ({"exp": int(time.time()) - 600}, "expired"),
        ({"aud": ["wrong-aud"]}, "audience"),
        ({"iss": "https://evil.cloudflareaccess.com"}, "issuer"),
        ({"iss": f"http://{TEAM}"}, "issuer"),
        ({"exp": None}, "missing claim 'exp'"),
        ({"iat": None}, "missing claim 'iat'"),
        ({"aud": None}, "missing claim 'aud'"),
        ({"iss": None}, "missing claim 'iss'"),
        ({"iat": int(time.time()) + 3600}, "not yet valid"),
    ],
)
def test_jwt_rejected_claims(rsa_keys, validator, payload_kw, message):
    key, _ = rsa_keys
    with pytest.raises(AccessJwtError, match=message):
        validator.validate(sign(key, claims(**payload_kw)))


def test_jwt_wrong_key(rsa_keys, validator):
    _, other = rsa_keys
    with pytest.raises(AccessJwtError, match="signature"):
        validator.validate(sign(other, claims()))


def test_jwt_hs256_rejected(validator):
    token = jwt.encode(claims(), "s" * 64, algorithm="HS256", headers={"kid": "kid-1"})
    with pytest.raises(AccessJwtError, match="algorithm"):
        validator.validate(token)


def test_jwt_alg_none_rejected(validator):
    token = f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': 'kid-1'})}.{b64(claims())}."
    with pytest.raises(AccessJwtError, match="algorithm"):
        validator.validate(token)


def test_jwt_rs512_rejected(rsa_keys, validator):
    key, _ = rsa_keys
    with pytest.raises(AccessJwtError, match="algorithm"):
        validator.validate(sign(key, claims(), alg="RS512"))


@pytest.mark.parametrize("token", ["", "   ", "garbage", "a.b.c", "x.y"])
def test_jwt_malformed(validator, token):
    with pytest.raises(AccessJwtError):
        validator.validate(token)


def test_jwt_jwks_failure_is_access_error(rsa_keys):
    key, _ = rsa_keys
    v = AccessJwtValidator(access_cfg(), jwks_client=FailingJwks())
    with pytest.raises(AccessJwtError, match="signing keys"):
        v.validate(sign(key, claims()))


def test_jwt_common_name_allow_list(rsa_keys):
    key, _ = rsa_keys
    v = AccessJwtValidator(
        access_cfg(allowed_client_ids=["client-id-1.access"]),
        jwks_client=FakeJwks(key.public_key()),
    )
    v.validate(sign(key, claims()))
    with pytest.raises(AccessJwtError, match="service token"):
        v.validate(sign(key, claims(common_name="someone-else.access")))
    with pytest.raises(AccessJwtError, match="service token"):
        v.validate(sign(key, claims(common_name=None)))  # e.g. a user (email) login


def test_jwt_error_messages_do_not_leak_token(rsa_keys, validator):
    key, _ = rsa_keys
    token = sign(key, claims(aud=["nope"]))
    with pytest.raises(AccessJwtError) as info:
        validator.validate(token)
    assert token not in str(info.value)
    assert len(str(info.value)) < 80


# ---------------------------------------------------------------------- RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_sliding_window():
    clock = Clock()
    rl = RateLimiter(3, 60, clock=clock)
    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
    assert rl.count("a") == 3
    assert rl.allow("b") is True  # keys are independent
    clock.now += 30
    assert rl.allow("a") is False
    clock.now += 30.001  # first three hits leave the window
    assert rl.count("a") == 0
    assert rl.allow("a") is True
    assert rl.count("a") == 1


def test_rate_limiter_partial_expiry():
    clock = Clock()
    rl = RateLimiter(2, 10, clock=clock)
    assert rl.allow("k")
    clock.now += 6
    assert rl.allow("k")
    assert not rl.allow("k")
    clock.now += 5  # first hit expired, second still inside
    assert rl.count("k") == 1
    assert rl.allow("k")
    assert not rl.allow("k")


def test_rate_limiter_reset_and_validation():
    rl = RateLimiter(1, 10)
    assert rl.allow("x")
    assert not rl.allow("x")
    rl.reset("x")
    assert rl.allow("x")
    with pytest.raises(ValueError):
        RateLimiter(0, 10)
    with pytest.raises(ValueError):
        RateLimiter(1, 0)


def test_rate_limiter_thread_safe():
    rl = RateLimiter(500, 60)
    allowed = []
    lock = threading.Lock()

    def worker():
        ok = sum(rl.allow("shared") for _ in range(100))
        with lock:
            allowed.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == 500
    assert rl.count("shared") == 500


def test_rate_limiter_sweeps_stale_keys():
    clock = Clock()
    rl = RateLimiter(1, 1, clock=clock)
    for i in range(1500):
        rl.allow(f"k{i}")
    clock.now += 5
    for _ in range(1024):
        rl.allow("fresh")
    assert len(rl._hits) <= 2
