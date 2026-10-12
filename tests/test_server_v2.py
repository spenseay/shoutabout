"""The v2 phone protocol over HTTP (docs/protocol.md), including adversarial cases.

Requests go through httpx.ASGITransport with an explicit TCP peer address, so the route
(LAN / tunnel / rejected) is exactly what the hub would see from a real socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fakes import FakeCleaner, FakeEngine, FakeInjector, FakeVocabStore, fake_decode
from shoutabout import devices as devices_mod
from shoutabout import pair_requests, security
from shoutabout.config import AccessConfig, Config
from shoutabout.devices import DeviceStore
from shoutabout.identity import IdentityStore
from shoutabout.models import PasteResult
from shoutabout.pair_requests import PairingManager
from shoutabout.pipeline import Pipeline
from shoutabout.security import AccessJwtValidator
from shoutabout.server import HubContext, create_app, describe_stt
from shoutabout.state import HubState

HUB_TOKEN = "H" * 43
LAN_IP = "192.168.1.23"
OTHER_LAN = "192.168.1.77"
LOOPBACK = "127.0.0.1"
PUBLIC_IP = "8.8.8.8"
PHONE_ID = "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d"
TEAM = "myteam.cloudflareaccess.com"
AUD = "aud-tag-1"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Client:
    """Sync requests through ASGITransport with a chosen TCP peer (and local address)."""

    def __init__(self, app, ip: str, server: tuple[str, int] | None = None) -> None:
        self.ip = ip
        if server is not None:
            inner = app

            async def with_server(scope, receive, send):
                await inner({**scope, "server": server}, receive, send)

            app = with_server
        self.app = app

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        async def go():
            transport = httpx.ASGITransport(app=self.app, client=(self.ip, 50123))
            async with httpx.AsyncClient(transport=transport, base_url="http://hub") as c:
                return await c.request(method, url, **kw)

        return asyncio.run(go())

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)


@dataclass
class Choice:
    tier: str
    device: str
    speed: str
    cleanup: bool


class FakeRecent:
    def __init__(self) -> None:
        self.records = []

    def add(self, result) -> None:
        self.records.append(result)


class Hub:
    def __init__(self, tmp_path, *, config=None, access=None, model_info=None, pipeline=True):
        self.clock = Clock()
        self.config = config or Config()
        self.state = HubState()
        self.devices = DeviceStore(tmp_path / "devices.json")
        self.pairing = PairingManager(self.devices, clock=self.clock, code_factory=lambda: "4821")
        self.identity = IdentityStore(tmp_path / "identity.json", default_name=lambda: "DESK-1")
        self.injector = FakeInjector()
        self.recent = FakeRecent()
        self.prompts = []
        self.pairing.subscribe(on_request=self.prompts.append)
        pipe = Pipeline(
            config=self.config,
            engine=FakeEngine(text="hello"),
            vocab=FakeVocabStore(),
            cleaner=FakeCleaner(),
            injector=self.injector,
            state=self.state,
            decode=fake_decode,
        )
        self.ctx = HubContext(
            config=self.config,
            token=HUB_TOKEN,
            state=self.state,
            pipeline=pipe if pipeline else None,
            access=access,
            vocab_path=tmp_path / "vocab.txt",
            devices=self.devices,
            pairing=self.pairing,
            identity=self.identity,
            version="9.9.9",
            model_info=model_info,
            injector=self.injector,
            recent=self.recent,
            lan_url=lambda: "http://192.168.1.50:47821",
        )
        self.app = create_app(self.ctx)

    def client(self, ip: str = LAN_IP, server=None) -> Client:
        return Client(self.app, ip, server)

    def pair(self, ip: str = LAN_IP, name: str = "Sam's iPhone") -> str:
        """Run the whole pairing flow; returns the device token."""
        c = self.client(ip)
        r = c.post("/pair/request", json={"device_name": name, "device_id": PHONE_ID})
        assert r.status_code == 202, r.text
        rid = r.json()["request_id"]
        assert self.pairing.approve(rid)
        r = c.get("/pair/status", params={"request_id": rid})
        assert r.json()["status"] == "approved"
        return r.json()["token"]


@pytest.fixture
def hub(tmp_path):
    (tmp_path / "vocab.txt").write_text("Better Shell\n", "utf-8")
    return Hub(tmp_path)


# ------------------------------------------------------------------- /hello


def test_hello_on_lan(hub):
    r = hub.client().get("/hello")
    assert r.status_code == 200
    body = r.json()
    ident = hub.identity.get()
    assert body == {
        "app": "shoutabout",
        "v": 1,
        "id": ident.id,
        "name": "DESK-1",
        "version": "9.9.9",
        "pairing": True,
    }
    uuid.UUID(body["id"])
    assert hub.client(OTHER_LAN).get("/hello").json()["id"] == ident.id  # stable


@pytest.mark.parametrize("ip", [LOOPBACK, "::1", "::ffff:127.0.0.1"])
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/hello"), ("POST", "/pair/request"), ("GET", "/pair/status?request_id=x")],
)
def test_unauthenticated_endpoints_are_lan_only(hub, ip, method, path):
    """Loopback is the tunnel route (cloudflared): pairing is invisible there."""
    spoof = {
        "X-Forwarded-For": LAN_IP,
        "Cf-Connecting-IP": LAN_IP,
        "X-Real-IP": LAN_IP,
        "Forwarded": f"for={LAN_IP}",
    }
    kw = {"json": {"device_name": "x", "device_id": PHONE_ID}} if method == "POST" else {}
    r = hub.client(ip).request(method, path, headers=spoof, **kw)
    assert r.status_code == 404
    assert hub.prompts == []


@pytest.mark.parametrize("ip", [PUBLIC_IP, "100.64.1.1", "2606:4700::1"])
def test_unauthenticated_endpoints_forbidden_from_public(hub, ip):
    for path in ("/hello", "/pair/status?request_id=x"):
        assert hub.client(ip).get(path, headers={"X-Forwarded-For": LAN_IP}).status_code == 403
    r = hub.client(ip).post("/pair/request", json={"device_name": "x", "device_id": PHONE_ID})
    assert r.status_code == 403


def test_hello_rate_limited(hub):
    c = hub.client()
    codes = [c.get("/hello").status_code for _ in range(125)]
    assert codes[:120] == [200] * 120
    assert codes[-1] == 429
    assert hub.client(OTHER_LAN).get("/hello").status_code == 200  # per source address


def test_hello_reports_pairing_off(hub):
    hub.pairing.enabled = False
    assert hub.client().get("/hello").json()["pairing"] is False
    r = hub.client().post("/pair/request", json={"device_name": "x", "device_id": PHONE_ID})
    assert r.status_code == 503


def test_hello_reflects_rename(hub):
    hub.identity.rename("Kitchen PC")
    assert hub.client().get("/hello").json()["name"] == "Kitchen PC"


# -------------------------------------------------------------- pairing flow


def test_pairing_happy_path(hub):
    c = hub.client()
    r = c.post("/pair/request", json={"device_name": "Sam's iPhone", "device_id": PHONE_ID})
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"request_id", "code", "expires_in"}
    assert body["code"] == "4821" and body["expires_in"] == 120
    assert len(body["request_id"]) >= 22  # >= 128 bits of urlsafe base64
    rid = body["request_id"]
    (prompt,) = hub.prompts
    assert (prompt.device_name, prompt.code) == ("Sam's iPhone", "4821")

    assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "pending"}
    assert hub.pairing.approve(rid)
    r = c.get("/pair/status", params={"request_id": rid})
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["status"] == "approved"
    token = body["token"]
    assert len(token) == 43
    assert body["hub"] == {
        "id": hub.identity.get().id,
        "name": "DESK-1",
        "lan_url": "http://192.168.1.50:47821",
    }
    # The token comes back exactly once.
    for _ in range(3):
        assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "claimed"}
    # Only its hash is stored.
    stored = (hub.devices.path).read_text("utf-8")
    assert token not in stored
    assert devices_mod.token_digest(token).hex() in stored
    (device,) = hub.devices.list()
    assert (device.name, device.device_id) == ("Sam's iPhone", PHONE_ID)
    # And it works as a bearer token.
    assert c.get("/info", headers=bearer(token)).status_code == 200


def test_pairing_lan_url_is_the_address_the_phone_used(hub):
    rid = (
        hub.client()
        .post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID})
        .json()["request_id"]
    )
    hub.pairing.approve(rid)
    r = hub.client(server=("192.168.1.9", 47821)).get("/pair/status", params={"request_id": rid})
    assert r.json()["hub"]["lan_url"] == "http://192.168.1.9:47821"


def test_pairing_denied_and_cooldown(hub):
    c = hub.client()
    rid = c.post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID}).json()[
        "request_id"
    ]
    assert hub.pairing.deny(rid)
    assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "denied"}
    assert not hub.pairing.approve(rid)  # too late to change its mind over the network
    r = c.post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID})
    assert r.status_code == 429
    assert hub.devices.list() == []
    hub.clock.now += 61
    r = c.post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID})
    assert r.status_code == 202


def test_pairing_expires(hub):
    c = hub.client()
    rid = c.post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID}).json()[
        "request_id"
    ]
    hub.clock.now += 119
    assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "pending"}
    hub.clock.now += 1
    assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "expired"}
    assert not hub.pairing.approve(rid)
    assert hub.devices.list() == []
    # Long after, the id is simply unknown.
    hub.clock.now += pair_requests.RESULT_TTL_S + 1
    assert c.get("/pair/status", params={"request_id": rid}).status_code == 404


def test_only_one_pending_request(hub):
    c = hub.client()
    assert c.post("/pair/request", json={"device_name": "A", "device_id": PHONE_ID}).status_code
    r = hub.client(OTHER_LAN).post(
        "/pair/request", json={"device_name": "B", "device_id": str(uuid.uuid4())}
    )
    assert r.status_code == 409
    assert len(hub.prompts) == 1


def _ask(hub, ip: str, name: str = "A"):
    return hub.client(ip).post("/pair/request", json={"device_name": name, "device_id": PHONE_ID})


def test_pair_request_rate_limited_per_ip(hub):
    # The first opens a pending request, the next are busy (409) until the per-address
    # limit (5 a minute, busy answers included) answers 429.
    codes = [_ask(hub, LAN_IP).status_code for _ in range(7)]
    assert codes == [202, 409, 409, 409, 409, 429, 429]
    assert _ask(hub, OTHER_LAN).status_code == 409  # another address isn't rate limited
    hub.clock.now += 61
    assert _ask(hub, LAN_IP).status_code == 409  # window passed; still one pending request
    assert len(hub.prompts) == 1


def test_pair_request_rate_limited_overall(hub):
    codes = [_ask(hub, f"10.0.0.{i}").status_code for i in range(1, 32)]
    assert codes[0] == 202
    assert codes[1:30] == [409] * 29
    assert codes[30] == 429  # 30 a minute across all addresses


def test_pair_status_unknown_and_replayed_ids(hub):
    c = hub.client()
    assert c.get("/pair/status").status_code == 404
    assert c.get("/pair/status", params={"request_id": "nope"}).status_code == 404
    assert c.get("/pair/status", params={"request_id": "x" * 5000}).status_code == 404
    token = hub.pair()
    assert token
    # Replaying the claimed id never yields the token again.
    (rid,) = [p.request_id for p in hub.prompts]
    assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "claimed"}
    # A near-miss id (one character changed) is unknown.
    near = rid[:-1] + ("A" if rid[-1] != "A" else "B")
    assert c.get("/pair/status", params={"request_id": near}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00garbage",
        b"[]",
        b"null",
        b'"string"',
        b"[" * 1020 + b"]" * 1020,  # deep nesting, still under the 2 KB cap
        json.dumps({"device_id": PHONE_ID}).encode(),
        json.dumps({"device_name": "", "device_id": PHONE_ID}).encode(),
        json.dumps({"device_name": "   ", "device_id": PHONE_ID}).encode(),
        json.dumps({"device_name": "\u202e\u200b", "device_id": PHONE_ID}).encode(),
        json.dumps({"device_name": "x" * 61, "device_id": PHONE_ID}).encode(),
        json.dumps({"device_name": 7, "device_id": PHONE_ID}).encode(),
        json.dumps({"device_name": "P", "device_id": "not-a-uuid"}).encode(),
        json.dumps({"device_name": "P", "device_id": 12}).encode(),
        json.dumps({"device_name": "P"}).encode(),
    ],
    ids=lambda b: repr(b[:24]),
)
def test_pair_request_bad_input_422(hub, body):
    r = hub.client().post(
        "/pair/request", content=body, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422, r.text
    assert hub.prompts == []


def test_pair_request_oversized_413(hub):
    big = json.dumps({"device_name": "P", "device_id": PHONE_ID, "pad": "x" * 5000})
    r = hub.client().post("/pair/request", content=big)
    assert r.status_code == 413
    r = hub.client().post("/pair/request", content=big, headers={"Content-Length": "999999999999"})
    assert r.status_code in (400, 413)
    assert hub.prompts == []


def test_pair_request_ignores_unknown_fields_and_cleans_the_name(hub):
    r = hub.client().post(
        "/pair/request",
        json={
            "device_name": "  Sam\u202e's\u200b   iPhone\n",
            "device_id": PHONE_ID.upper(),
            "approve": True,
            "token": "x",
            "status": "approved",
        },
    )
    assert r.status_code == 202
    (prompt,) = hub.prompts
    assert prompt.device_name == "Sam's iPhone"
    rid = r.json()["request_id"]
    # A body field can never approve anything.
    assert hub.client().get("/pair/status", params={"request_id": rid}).json() == {
        "status": "pending"
    }


def test_no_network_call_can_approve(hub):
    """There is no approve endpoint, whatever method or path is tried."""
    c = hub.client()
    rid = c.post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID}).json()[
        "request_id"
    ]
    for method, path in [
        ("POST", "/pair/approve"),
        ("POST", f"/pair/status?request_id={rid}&status=approved"),
        ("PUT", f"/pair/status?request_id={rid}"),
        ("POST", "/pair/request/approve"),
    ]:
        c.request(method, path, json={"request_id": rid, "status": "approved"})
    assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "pending"}
    # Not even with the hub token.
    c.post("/pair/approve", headers=bearer(HUB_TOKEN), json={"request_id": rid})
    assert hub.devices.list() == []


def test_uncollected_token_is_revoked(hub):
    c = hub.client()
    rid = c.post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID}).json()[
        "request_id"
    ]
    assert hub.pairing.approve(rid)
    assert len(hub.devices.list()) == 1
    hub.clock.now += pair_requests.CLAIM_TTL_S
    assert c.get("/pair/status", params={"request_id": rid}).json() == {"status": "expired"}
    assert hub.devices.list() == []


def test_pairing_disabled_without_manager(tmp_path):
    hub = Hub(tmp_path)
    hub.ctx.pairing = None
    assert hub.client().get("/hello").json()["pairing"] is False
    r = hub.client().post("/pair/request", json={"device_name": "P", "device_id": PHONE_ID})
    assert r.status_code == 404


# ----------------------------------------------------------- device tokens


def test_device_token_and_legacy_token_both_work(hub):
    token = hub.pair()
    c = hub.client()
    for t in (token, HUB_TOKEN):
        assert c.get("/info", headers=bearer(t)).status_code == 200
        assert c.get("/vocab", headers=bearer(t)).status_code == 200
        r = c.post(
            "/transcribe",
            headers={**bearer(t), "Content-Type": "audio/x-m4a"},
            content=b"audio-bytes",
            params={"target": "phone"},
        )
        assert r.status_code == 200, r.text


def test_removed_device_is_revoked_at_once(hub):
    token = hub.pair()
    c = hub.client()
    assert c.get("/info", headers=bearer(token)).status_code == 200
    (device,) = hub.devices.list()
    assert hub.devices.remove(device.key)
    r = c.get("/info", headers=bearer(token))
    assert r.status_code == 401
    assert c.get("/info", headers=bearer(HUB_TOKEN)).status_code == 200


def test_removal_by_another_process_is_seen(hub, tmp_path):
    token = hub.pair()
    other = DeviceStore(hub.devices.path)  # e.g. `shoutabout devices remove` in a terminal
    time.sleep(0.02)
    other.remove(other.list()[0].key)
    assert hub.client().get("/info", headers=bearer(token)).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic " + HUB_TOKEN,
        "Bearer " + HUB_TOKEN + "x",
        "Bearer " + HUB_TOKEN[:-1],
        "Bearer " + "A" * 5000,
        "Bearer ünïcödé",
    ],
    ids=lambda h: repr(h)[:24],
)
def test_bad_bearer_401(hub, header):
    headers = {} if header is None else {"Authorization": header.encode("latin-1")}
    r = hub.client().get("/info", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_device_token_failures_count_towards_lockout(tmp_path):
    hub = Hub(tmp_path, config=_cfg(max_auth_failures=2))
    token = hub.pair()
    c = hub.client()
    assert c.get("/info", headers=bearer("wrong")).status_code == 401
    assert c.get("/info", headers=bearer(token + "x")).status_code == 401
    assert c.get("/info", headers=bearer(token)).status_code == 429  # locked out


def test_device_token_on_tunnel_still_needs_access_jwt(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key()))
    access = AccessJwtValidator(AccessConfig(team_domain=TEAM, aud=[AUD]), jwks_client=jwks)
    hub = Hub(tmp_path, access=access)
    token = hub.pair()
    c = hub.client(LOOPBACK)
    assert c.get("/info", headers=bearer(token)).status_code == 403
    now = int(time.time())
    assertion = jwt.encode(
        {"aud": [AUD], "iss": f"https://{TEAM}", "iat": now, "exp": now + 300},
        key,
        algorithm="RS256",
        headers={"kid": "k"},
    )
    r = c.get("/info", headers={**bearer(token), "Cf-Access-Jwt-Assertion": assertion})
    assert r.status_code == 200


def test_auth_compares_against_every_credential(hub, monkeypatch):
    """No short-circuit: every stored hash is compared, whichever one matches."""
    token = hub.pair()
    for name in ("Work iPhone", "iPad"):
        hub.devices.add(name, str(uuid.uuid4()))
    calls = []
    real = devices_mod.hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(devices_mod.hmac, "compare_digest", counting)
    monkeypatch.setattr(security.hmac, "compare_digest", counting)
    c = hub.client()
    for t in (token, HUB_TOKEN, "wrong-token"):
        calls.clear()
        c.get("/info", headers=bearer(t))
        assert len(calls) == 1 + 3  # the hub token + all three devices


def test_last_used_is_recorded(hub):
    token = hub.pair()
    assert hub.devices.list()[0].last_seen is None
    hub.client().get("/info", headers=bearer(token))
    assert hub.devices.list()[0].last_seen is not None


# ------------------------------------------------------------------- /info


def test_info_from_model_choice(tmp_path):
    hub = Hub(tmp_path, model_info=lambda: Choice("balanced", "cuda", "ok", True))
    body = hub.client().get("/info", headers=bearer(HUB_TOKEN)).json()
    assert body == {
        "id": hub.identity.get().id,
        "name": "DESK-1",
        "version": "9.9.9",
        "stt": {"tier": "balanced", "device": "cuda", "speed": "ok"},
        "cleanup": True,
        "accepts": ["audio", "text"],
    }


def test_info_light_cpu_is_slow(tmp_path):
    hub = Hub(tmp_path, model_info=lambda: Choice("light", "cpu", "bogus", False))
    body = hub.client().get("/info", headers=bearer(HUB_TOKEN)).json()
    assert body["stt"] == {"tier": "light", "device": "cpu", "speed": "slow"}
    assert body["cleanup"] is False


def test_info_without_model_pick_guesses_from_config(tmp_path):
    def broken():
        raise RuntimeError("no modelpick yet")

    hub = Hub(tmp_path, model_info=broken)
    body = hub.client().get("/info", headers=bearer(HUB_TOKEN)).json()
    # A new config says "auto" everywhere: a neutral guess, not "slow".
    assert body["stt"] == {"tier": "balanced", "device": "cpu", "speed": "ok"}


def test_describe_stt_cuda_config():
    cfg = Config()
    cfg.stt.device = "cuda"
    cfg.cleanup.enabled = True
    assert describe_stt(None, cfg) == ({"tier": "best", "device": "cuda", "speed": "fast"}, True)


def test_describe_stt_cpu_config():
    cfg = Config()
    cfg.stt.device = "cpu"
    cfg.cleanup.enabled = False
    assert describe_stt(None, cfg) == ({"tier": "light", "device": "cpu", "speed": "slow"}, False)


def test_info_needs_auth(hub):
    assert hub.client().get("/info").status_code == 401


# ------------------------------------------------------------------ /paste


def test_paste_pastes_like_a_transcription(hub, caplog):
    caplog.set_level(logging.INFO)
    token = hub.pair()
    secret_text = "Line one\r\n\tline two\u2028 send it\x1b"
    r = hub.client().post("/paste", headers=bearer(token), json={"text": secret_text})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "pasted": True}
    assert hub.injector.pasted == ["Line one line two send it"]  # one paragraph, no Enter
    (record,) = hub.recent.records
    assert record.text == "Line one line two send it"
    assert record.cleanup.status == "disabled"
    assert record.target == "pc"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "line two" not in logged and "send it" not in logged
    assert "chars=25" in logged
    assert token not in logged


def test_paste_trailing_space_setting(tmp_path):
    cfg = Config()
    cfg.paste.trailing_space = True
    hub = Hub(tmp_path, config=cfg)
    hub.client().post("/paste", headers=bearer(HUB_TOKEN), json={"text": "hi"})
    assert hub.injector.pasted == ["hi "]


def test_paste_respects_pause(hub):
    hub.state.set_paused(True)
    r = hub.client().post("/paste", headers=bearer(HUB_TOKEN), json={"text": "hello"})
    assert r.status_code == 409
    assert r.json()["status"] == "paused"
    assert hub.injector.pasted == []


def test_paste_failure_is_a_warning(hub):
    hub.injector.result = PasteResult(pasted=False, warning="Couldn't paste into an admin window.")
    r = hub.client().post("/paste", headers=bearer(HUB_TOKEN), json={"text": "hello"})
    assert r.status_code == 200
    assert r.json() == {
        "status": "warning",
        "pasted": False,
        "warning": "Couldn't paste into an admin window.",
    }


def test_paste_injector_crash_is_contained(hub):
    def boom(_text):
        raise RuntimeError("clipboard locked")

    hub.injector.paste = boom
    r = hub.client().post("/paste", headers=bearer(HUB_TOKEN), json={"text": "hello"})
    assert r.status_code == 200
    assert r.json()["pasted"] is False


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"", 422),
        (b"hello", 422),
        (b"[" * 5000 + b"]" * 5000, 422),
        (json.dumps({"text": 5}).encode(), 422),
        (json.dumps({"txt": "hi"}).encode(), 422),
        (json.dumps({"text": "   \n\t "}).encode(), 422),
        (json.dumps({"text": "x" * 10_001}).encode(), 413),
        (json.dumps({"text": "x" * 200_000}).encode(), 413),
    ],
    ids=["empty", "not-json", "deep", "not-str", "no-text", "blank", "too-long", "huge"],
)
def test_paste_bad_input(hub, body, status):
    r = hub.client().post("/paste", headers=bearer(HUB_TOKEN), content=body)
    assert r.status_code == status, r.text
    assert hub.injector.pasted == []


def test_paste_max_length_accepted(hub):
    r = hub.client().post("/paste", headers=bearer(HUB_TOKEN), json={"text": "😀" * 10_000})
    assert r.status_code == 200
    assert hub.injector.pasted == ["😀" * 10_000]


def test_paste_needs_auth_before_reading_the_body(hub):
    r = hub.client().post("/paste", json={"text": "hello"})
    assert r.status_code == 401
    assert hub.injector.pasted == []


def test_paste_works_while_the_engine_loads(tmp_path):
    hub = Hub(tmp_path, pipeline=False)
    r = hub.client().post("/paste", headers=bearer(HUB_TOKEN), json={"text": "hello"})
    assert r.status_code == 200
    assert hub.injector.pasted == ["hello"]


def test_paste_without_injector_503(hub):
    hub.ctx.injector = None
    r = hub.client().post("/paste", headers=bearer(HUB_TOKEN), json={"text": "hello"})
    assert r.status_code == 503


def test_paste_from_public_ip_forbidden(hub):
    r = hub.client(PUBLIC_IP).post("/paste", headers=bearer(HUB_TOKEN), json={"text": "x"})
    assert r.status_code == 403


def _cfg(**server) -> Config:
    cfg = Config()
    for key, value in server.items():
        setattr(cfg.server, key, value)
    return cfg
