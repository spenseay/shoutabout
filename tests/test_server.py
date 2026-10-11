"""HTTP contract tests via httpx.ASGITransport with an explicit TCP peer address."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fakes import FakeCleaner, FakeEngine, FakeInjector, FakeVocabStore, fake_decode
from shoutabout.config import Config
from shoutabout.engine.base import EngineError
from shoutabout.models import PasteResult
from shoutabout.pipeline import Pipeline
from shoutabout.security import AccessJwtValidator
from shoutabout.server import HubContext, create_app
from shoutabout.state import HubState

TOKEN = "T" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LAN_IP = "192.168.1.20"
LOOPBACK = "127.0.0.1"
PUBLIC_IP = "8.8.8.8"
TEAM = "myteam.cloudflareaccess.com"
AUD = "aud-tag-1"
RESPONSE_KEYS = {
    "text",
    "raw_text",
    "target",
    "pasted",
    "warning",
    "status",
    "route",
    "cleanup",
    "timings_ms",
}
TIMING_KEYS = {"decode", "stt", "cleanup", "paste", "total"}
AUDIO = b"\x00\x01fake-m4a-bytes" * 10


class Client:
    """Sync wrapper over httpx.AsyncClient + ASGITransport(client=(ip, port))."""

    def __init__(self, app, ip: str, port: int = 50123) -> None:
        self.app = app
        self.ip = ip
        self.port = port

    def _client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(self.ip, self.port))
        return httpx.AsyncClient(transport=transport, base_url="http://hub")

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        async def go():
            async with self._client() as c:
                return await c.request(method, url, **kw)

        return asyncio.run(go())

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)

    def gather(self, n: int, **kw) -> list[httpx.Response]:
        async def go():
            async with self._client() as c:
                return await asyncio.gather(*(c.post("/transcribe", **kw) for _ in range(n)))

        return asyncio.run(go())


def make_config(**server) -> Config:
    cfg = Config()
    for key, value in server.items():
        setattr(cfg.server, key, value)
    return cfg


class Hub:
    def __init__(
        self,
        *,
        config=None,
        engine=None,
        injector=None,
        cleaner=None,
        access=None,
        pipeline=True,
        startup_error=None,
    ):
        self.config = config or make_config()
        self.state = HubState()
        self.engine = engine or FakeEngine(text="hello better shell")
        self.injector = injector or FakeInjector()
        self.cleaner = cleaner or FakeCleaner()
        self.pipeline = Pipeline(
            config=self.config,
            engine=self.engine,
            vocab=FakeVocabStore(),
            cleaner=self.cleaner,
            injector=self.injector,
            state=self.state,
            decode=fake_decode,
        )
        self.ctx = HubContext(
            config=self.config,
            token=TOKEN,
            state=self.state,
            pipeline=self.pipeline if pipeline else None,
            access=access,
            startup_error=startup_error,
        )
        self.app = create_app(self.ctx)

    def client(self, ip: str = LAN_IP) -> Client:
        return Client(self.app, ip)


def upload(target: str = "pc", filename: str = "rec.m4a", data: bytes = AUDIO):
    return {"files": {"file": (filename, data, "audio/mp4")}, "data": {"target": target}}


# ------------------------------------------------------------------------- health


@pytest.mark.parametrize("ip", [LAN_IP, "10.1.2.3", "172.20.0.5", LOOPBACK, "::1"])
def test_health_allowed_sources(ip):
    r = Hub().client(ip).get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.parametrize("ip", [PUBLIC_IP, "1.1.1.1", "100.64.1.1", "2606:4700::1", "testclient"])
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/health"), ("POST", "/transcribe"), ("GET", "/nope"), ("GET", "/docs")],
)
def test_public_source_forbidden_everywhere(ip, method, path):
    r = Hub().client(ip).request(method, path, headers=AUTH)
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden"}


def test_docs_and_openapi_disabled():
    c = Hub().client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_forwarding_headers_not_trusted():
    hub = Hub()
    spoof = {"X-Forwarded-For": LAN_IP, "Cf-Connecting-IP": LAN_IP, "X-Real-IP": LAN_IP}
    assert hub.client(PUBLIC_IP).get("/health", headers=spoof).status_code == 403
    r = hub.client(LAN_IP).post(
        "/transcribe", headers={**AUTH, "X-Forwarded-For": LOOPBACK}, **upload()
    )
    assert r.json()["route"] == "lan"


# --------------------------------------------------------------------- transcribe


def test_transcribe_lan_contract():
    hub = Hub()
    r = hub.client().post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == RESPONSE_KEYS
    assert set(body["timings_ms"]) == TIMING_KEYS
    assert all(isinstance(v, int) for v in body["timings_ms"].values())
    assert body["route"] == "lan"
    assert body["target"] == "pc"
    assert body["text"] == "hello better shell"
    assert body["raw_text"] == "hello better shell"
    assert body["pasted"] is True
    assert body["warning"] is None
    assert body["cleanup"] == "applied"
    assert hub.injector.pasted == ["hello better shell"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
    ],
)
def test_transcribe_bad_bearer(headers):
    hub = Hub()
    r = hub.client().post("/transcribe", headers=headers, **upload())
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert hub.injector.pasted == []


def test_bad_bearer_from_loopback_also_401():
    r = Hub().client(LOOPBACK).post("/transcribe", **upload())
    assert r.status_code == 401


@pytest.mark.parametrize("target", ["phone", " PHONE ", "Phone"])
def test_target_phone_never_pastes(target):
    hub = Hub()
    r = hub.client().post("/transcribe", headers=AUTH, **upload(target=target))
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "phone"
    assert body["pasted"] is False
    assert body["warning"] is None
    assert hub.injector.pasted == []


@pytest.mark.parametrize("target", [" PC ", "Pc\n"])
def test_target_normalised(target):
    r = Hub().client().post("/transcribe", headers=AUTH, **upload(target=target))
    assert r.status_code == 200
    assert r.json()["target"] == "pc"


@pytest.mark.parametrize("target", ["desktop", "pc phone", "both"])
def test_bad_target_422(target):
    hub = Hub()
    r = hub.client().post("/transcribe", headers=AUTH, **upload(target=target))
    assert r.status_code == 422
    assert hub.injector.pasted == []


@pytest.mark.parametrize("target", ["", "  ", "\n"])
def test_empty_target_defaults_to_pc(target):
    # The Shortcut sends the contents of its target file; a missing file means "pc".
    r = Hub().client().post("/transcribe", headers=AUTH, **upload(target=target))
    assert r.status_code == 200
    assert r.json()["target"] == "pc"


def test_missing_target_defaults_to_pc():
    r = (
        Hub()
        .client()
        .post("/transcribe", headers=AUTH, files={"file": ("a.m4a", AUDIO, "audio/mp4")})
    )
    assert r.status_code == 200
    assert r.json()["target"] == "pc"


def test_missing_file_400():
    r = Hub().client().post("/transcribe", headers=AUTH, data={"target": "pc"})
    assert r.status_code == 400
    assert "file" in r.json()["detail"]


@pytest.mark.parametrize("kw", [{"json": {"target": "pc"}}, {"content": b"hi"}])
def test_non_audio_body_415(kw):
    headers = dict(AUTH)
    if "content" in kw:
        headers["content-type"] = "text/plain"
    hub = Hub()
    r = hub.client().post("/transcribe", headers=headers, **kw)
    assert r.status_code == 415
    assert hub.injector.pasted == []


def test_two_files_400():
    files = [("file", ("a.m4a", AUDIO, "audio/mp4")), ("file2", ("b.m4a", AUDIO, "audio/mp4"))]
    r = Hub().client().post("/transcribe", headers=AUTH, files=files, data={"target": "pc"})
    assert r.status_code == 400


def test_paste_failure_is_200_with_warning():
    injector = FakeInjector(
        PasteResult(pasted=False, warning="Couldn't paste into an admin window")
    )
    r = Hub(injector=injector).client().post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 200
    body = r.json()
    assert body["pasted"] is False
    assert body["warning"] == "Couldn't paste into an admin window"


def test_temp_file_deleted_and_suffix_kept(monkeypatch):
    seen = []

    def decode(path, max_seconds=None):
        seen.append((path, path.exists(), path.read_bytes()))
        return fake_decode(path, max_seconds)

    hub = Hub()
    hub.pipeline._decode = decode
    r = hub.client().post("/transcribe", headers=AUTH, **upload(filename="memo.WAV"))
    assert r.status_code == 200
    path, existed, data = seen[0]
    assert existed and data == AUDIO
    assert path.suffix == ".wav"
    assert not path.exists()


@pytest.mark.parametrize("filename", ["noext", "evil.m4a/../../x", "a.<script>", ""])
def test_odd_filenames_default_suffix(filename):
    seen = []

    def decode(path, max_seconds=None):
        seen.append(path)
        return fake_decode(path, max_seconds)

    hub = Hub()
    hub.pipeline._decode = decode
    r = hub.client().post("/transcribe", headers=AUTH, **upload(filename=filename or "x"))
    assert r.status_code == 200
    assert seen[0].suffix in (".m4a",)


# ---------------------------------------------------------------- tunnel and JWT


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def access(rsa_key):
    from shoutabout.config import AccessConfig

    jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda t: SimpleNamespace(key=rsa_key.public_key())
    )
    return AccessJwtValidator(AccessConfig(team_domain=TEAM, aud=[AUD]), jwks_client=jwks)


def make_jwt(key, **overrides):
    now = int(time.time())
    payload = {
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "iat": now,
        "exp": now + 300,
        "common_name": "cid.access",
    }
    payload.update(overrides)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "k"})


def test_tunnel_requires_jwt_when_configured(access):
    hub = Hub(access=access)
    r = hub.client(LOOPBACK).post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 403
    assert hub.injector.pasted == []


def test_tunnel_invalid_jwt_403(access, rsa_key):
    hub = Hub(access=access)
    for bad in ("garbage", make_jwt(rsa_key, aud=["other"]), make_jwt(rsa_key, exp=1)):
        r = hub.client(LOOPBACK).post(
            "/transcribe", headers={**AUTH, "Cf-Access-Jwt-Assertion": bad}, **upload()
        )
        assert r.status_code == 403, bad
    assert hub.injector.pasted == []


def test_tunnel_valid_jwt_200(access, rsa_key):
    hub = Hub(access=access)
    headers = {**AUTH, "Cf-Access-Jwt-Assertion": make_jwt(rsa_key)}
    r = hub.client(LOOPBACK).post("/transcribe", headers=headers, **upload())
    assert r.status_code == 200, r.text
    assert r.json()["route"] == "tunnel"


def test_tunnel_ipv6_loopback_valid_jwt(access, rsa_key):
    headers = {**AUTH, "Cf-Access-Jwt-Assertion": make_jwt(rsa_key)}
    r = Hub(access=access).client("::1").post("/transcribe", headers=headers, **upload())
    assert r.status_code == 200
    assert r.json()["route"] == "tunnel"


def test_tunnel_jwt_checked_after_bearer(access, rsa_key):
    headers = {"Cf-Access-Jwt-Assertion": make_jwt(rsa_key)}
    r = Hub(access=access).client(LOOPBACK).post("/transcribe", headers=headers, **upload())
    assert r.status_code == 401


def test_tunnel_without_access_config_bearer_only():
    r = Hub(access=None).client(LOOPBACK).post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 200
    assert r.json()["route"] == "tunnel"


def test_lan_does_not_need_jwt(access):
    r = Hub(access=access).client(LAN_IP).post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 200
    assert r.json()["route"] == "lan"


# ----------------------------------------------------------------- limits / errors


def test_pipeline_none_503_starting():
    r = Hub(pipeline=False).client().post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 503
    assert r.json()["detail"] == "hub is starting"


def test_pipeline_none_503_startup_error():
    hub = Hub(pipeline=False, startup_error="CUDA failed: cuDNN missing")
    r = hub.client().post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 503
    assert r.json()["detail"] == "CUDA failed: cuDNN missing"


def test_readiness_checked_after_auth():
    r = Hub(pipeline=False).client().post("/transcribe", **upload())
    assert r.status_code == 401


def test_oversized_upload_content_length_413():
    cfg = make_config(max_upload_mb=0.01)  # ~10 KB
    hub = Hub(config=cfg)
    r = hub.client().post("/transcribe", headers=AUTH, **upload(data=b"x" * 200_000))
    assert r.status_code == 413
    assert hub.injector.pasted == []


def test_oversized_file_part_413_even_within_slack():
    cfg = make_config(max_upload_mb=0.01)  # 10485 bytes; body slack is 64 KB
    hub = Hub(config=cfg)
    r = hub.client().post("/transcribe", headers=AUTH, **upload(data=b"x" * 20_000))
    assert r.status_code == 413


def _multipart(boundary: str, data: bytes, target: str = "pc") -> bytes:
    return (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="target"\r\n\r\n{target}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.m4a"\r\n'
            "Content-Type: audio/mp4\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )


def test_oversized_chunked_upload_without_content_length_413():
    cfg = make_config(max_upload_mb=0.01)
    hub = Hub(config=cfg)
    body = _multipart("XyZ", b"y" * 300_000)

    async def chunks():
        for i in range(0, len(body), 8192):
            yield body[i : i + 8192]

    r = hub.client().post(
        "/transcribe",
        headers={**AUTH, "Content-Type": "multipart/form-data; boundary=XyZ"},
        content=chunks(),
    )
    assert r.status_code == 413
    assert hub.injector.pasted == []


def test_chunked_upload_within_limit_ok():
    hub = Hub()
    body = _multipart("XyZ", AUDIO, target="phone")

    async def chunks():
        for i in range(0, len(body), 7):
            yield body[i : i + 7]

    r = hub.client().post(
        "/transcribe",
        headers={**AUTH, "Content-Type": "multipart/form-data; boundary=XyZ"},
        content=chunks(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["target"] == "phone"


def test_audio_errors_mapped():
    from shoutabout.audio import AudioError, AudioTooLongError

    hub = Hub()
    c = hub.client()
    hub.pipeline._decode = lambda p, m=None: (_ for _ in ()).throw(AudioTooLongError("too long"))
    r = c.post("/transcribe", headers=AUTH, **upload())
    assert (r.status_code, r.json()["detail"]) == (413, "too long")
    hub.pipeline._decode = lambda p, m=None: (_ for _ in ()).throw(
        AudioError(r"Invalid data found when processing input: 'C:\Temp\shoutabout-x.m4a'")
    )
    r = c.post("/transcribe", headers=AUTH, **upload())
    # Generic message: decoder errors can contain local temp paths.
    assert (r.status_code, r.json()["detail"]) == (415, "unsupported or unreadable audio")
    assert "Temp" not in r.text


def test_missing_decoder_is_503_with_the_fix():
    # Windows "N" edition without the Media Feature Pack: no upload can be decoded, and
    # it isn't the upload's fault.
    from shoutabout.audio import DecoderUnavailableError

    message = "Windows Media Foundation isn't available on this PC. Install the Media Feature Pack."
    hub = Hub()
    hub.pipeline._decode = lambda p, m=None: (_ for _ in ()).throw(DecoderUnavailableError(message))
    r = hub.client().post("/transcribe", headers=AUTH, **upload())
    assert (r.status_code, r.json()["detail"]) == (503, message)


def test_engine_error_503():
    hub = Hub(engine=FakeEngine(fail=EngineError("CUDA out of memory")))
    r = hub.client().post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 503
    assert r.json()["detail"] == "CUDA out of memory"


def test_unexpected_error_500_without_details(caplog):
    hub = Hub(engine=FakeEngine(fail=RuntimeError("secret internals")))
    with caplog.at_level(logging.ERROR):
        r = hub.client().post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 500
    assert "secret" not in r.text
    assert "transcription failed" in caplog.text


def test_rate_limit_429():
    hub = Hub(config=make_config(rate_limit_per_minute=3))
    c = hub.client()
    codes = [c.post("/transcribe", headers=AUTH, **upload()).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    # a different source has its own budget
    assert (
        hub.client("192.168.1.21").post("/transcribe", headers=AUTH, **upload()).status_code == 200
    )


def test_rate_limit_applies_before_auth():
    hub = Hub(config=make_config(rate_limit_per_minute=2, max_auth_failures=100))
    c = hub.client()
    codes = [c.post("/transcribe", **upload()).status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_auth_failure_lockout():
    hub = Hub(config=make_config(max_auth_failures=2))
    c = hub.client()
    assert (
        c.post("/transcribe", headers={"Authorization": "Bearer x"}, **upload()).status_code == 401
    )
    assert c.post("/transcribe", **upload()).status_code == 401
    r = c.post("/transcribe", headers=AUTH, **upload())  # correct token, but locked out
    assert r.status_code == 429
    assert hub.injector.pasted == []
    assert (
        hub.client("192.168.1.99").post("/transcribe", headers=AUTH, **upload()).status_code == 200
    )


def test_jwt_failures_count_towards_lockout(access):
    hub = Hub(access=access, config=make_config(max_auth_failures=2))
    c = hub.client(LOOPBACK)
    bad = {**AUTH, "Cf-Access-Jwt-Assertion": "nope"}
    assert [c.post("/transcribe", headers=bad, **upload()).status_code for _ in range(3)] == [
        403,
        403,
        429,
    ]


# ------------------------------------------------- raw body uploads (Shortcuts "File" body)


def raw(target: str | None = "pc", ctype: str = "audio/x-m4a", data: bytes = AUDIO):
    kw: dict = {"content": data, "headers": {**AUTH, "content-type": ctype}}
    if target is not None:
        kw["params"] = {"target": target}
    return kw


def test_status_field_for_the_shortcut():
    ok = Hub().client().post("/transcribe", **raw("pc")).json()
    assert (ok["status"], ok["warning"]) == ("ok", None)
    hub = Hub()
    hub.state.set_paused(True)
    paused = hub.client().post("/transcribe", **raw("pc")).json()
    assert paused["status"] == "warning"
    assert paused["warning"]


def test_raw_upload_ok():
    hub = Hub()
    r = hub.client().post("/transcribe", **raw("pc"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == RESPONSE_KEYS
    assert (body["route"], body["target"], body["pasted"]) == ("lan", "pc", True)
    assert hub.injector.pasted == ["hello better shell"]


@pytest.mark.parametrize("ctype", ["audio/x-m4a", "audio/mp4", "application/octet-stream", ""])
def test_raw_upload_any_audio_content_type(ctype):
    r = Hub().client().post("/transcribe", **raw("phone", ctype=ctype))
    assert r.status_code == 200, r.text
    assert r.json()["target"] == "phone"


@pytest.mark.parametrize("target", [None, "", " PC "])
def test_raw_upload_target_defaults_to_pc(target):
    r = Hub().client().post("/transcribe", **raw(target))
    assert r.status_code == 200
    assert r.json()["target"] == "pc"


def test_raw_upload_bad_target_422():
    hub = Hub()
    r = hub.client().post("/transcribe", **raw("desktop"))
    assert r.status_code == 422
    assert hub.injector.pasted == []


def test_raw_upload_empty_body_400():
    r = Hub().client().post("/transcribe", **raw("pc", data=b""))
    assert r.status_code == 400


def test_raw_upload_too_large_413():
    hub = Hub(config=make_config(max_upload_mb=0.001))
    r = hub.client().post("/transcribe", **raw("pc", data=b"x" * 5000))
    assert r.status_code == 413


def test_raw_upload_needs_bearer():
    kw = raw("pc")
    kw["headers"] = {"content-type": "audio/x-m4a"}
    assert Hub().client().post("/transcribe", **kw).status_code == 401


def test_urlencoded_form_without_file_is_400():
    # What iOS sends when a Form body's file field doesn't resolve: target only.
    r = Hub().client().post("/transcribe", headers=AUTH, data={"target": ""})
    assert r.status_code == 400
    assert "file" in r.json()["detail"]


def test_tunnel_lockout_is_per_remote_client():
    # All tunnel traffic comes from 127.0.0.1; one remote client's failures must not lock
    # out another remote client (Cloudflare sets Cf-Connecting-IP).
    hub = Hub(access=None, config=make_config(max_auth_failures=2))
    c = hub.client(LOOPBACK)
    attacker = {"Authorization": "Bearer wrong", "Cf-Connecting-IP": "203.0.113.7"}
    codes = [c.post("/transcribe", headers=attacker, **upload()).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    owner = {**AUTH, "Cf-Connecting-IP": "198.51.100.20"}
    r = c.post("/transcribe", headers=owner, **upload())
    assert r.status_code == 200
    assert r.json()["route"] == "tunnel"


def test_cf_connecting_ip_never_changes_route():
    hub = Hub(access=None)
    r = hub.client(LOOPBACK).post(
        "/transcribe", headers={**AUTH, "Cf-Connecting-IP": "192.168.1.5"}, **upload()
    )
    assert r.json()["route"] == "tunnel"
    r = hub.client(PUBLIC_IP).post(
        "/transcribe", headers={**AUTH, "Cf-Connecting-IP": "192.168.1.5"}, **upload()
    )
    assert r.status_code == 403


def test_concurrent_requests_are_serialised():
    class SlowEngine(FakeEngine):
        def __init__(self):
            super().__init__(text="concurrent text")
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def transcribe(self, audio, hints):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            return super().transcribe(audio, hints)

    engine = SlowEngine()
    hub = Hub(engine=engine)
    responses = hub.client().gather(4, headers=AUTH, **upload())
    assert [r.status_code for r in responses] == [200] * 4
    assert engine.max_active == 1
    assert hub.injector.pasted == ["concurrent text"] * 4


def test_request_log_line_has_no_text_or_token(caplog):
    hub = Hub(engine=FakeEngine(text="my secret dictation"))
    with caplog.at_level(logging.INFO, logger="shoutabout"):
        r = hub.client().post("/transcribe", headers=AUTH, **upload())
    assert r.status_code == 200
    lines = [rec.getMessage() for rec in caplog.records if "transcribe id=" in rec.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    for part in (
        "route=lan",
        "target=pc",
        "chars=19",
        "pasted=true",
        "cleanup=applied",
        "audio_s=1.0",
        "decode=",
        "stt=",
        "paste=",
        "total=",
    ):
        assert part in line
    assert "secret" not in caplog.text
    assert TOKEN not in caplog.text


def test_rejections_logged_without_token(caplog):
    hub = Hub()
    with caplog.at_level(logging.WARNING, logger="shoutabout"):
        hub.client().post(
            "/transcribe", headers={"Authorization": "Bearer leaked-guess"}, **upload()
        )
        hub.client(PUBLIC_IP).get("/health")
    assert "reason=bad-bearer" in caplog.text
    assert "ip=192.168.1.20" in caplog.text
    assert "ip=8.8.8.8" in caplog.text
    assert "leaked-guess" not in caplog.text
