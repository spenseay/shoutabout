"""Streamed dictations over HTTP (docs/protocol.md, "Streaming"): POST /stream/start,
POST /stream/{id}/audio, POST /stream/{id}/finish, DELETE /stream/{id}.

The status codes the iPhone relies on, auth and limits exactly like /transcribe, the
separate chunk rate limit, and a finish response identical to /transcribe's.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import jwt
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fakes import FakeEngine
from shoutabout import server as server_mod
from shoutabout.config import AccessConfig
from shoutabout.engine.base import EngineError
from shoutabout.pipeline import PAUSED_WARNING
from shoutabout.security import AccessJwtValidator
from test_server_v2 import AUD, HUB_TOKEN, LAN_IP, LOOPBACK, PUBLIC_IP, TEAM, Hub, _cfg, bearer

SR = 16_000
MAX_CHUNK = 262_144


def chunk(seconds: float = 2.0) -> bytes:
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()


@pytest.fixture
def hub(tmp_path):
    (tmp_path / "vocab.txt").write_text("Better Shell\n", "utf-8")
    h = Hub(tmp_path)
    h.ctx.pipeline.recent = h.recent  # as in the real hub: dictations go to Recent
    return h


class Phone:
    """The iPhone's side of the protocol."""

    def __init__(self, hub, token: str = HUB_TOKEN, ip: str = LAN_IP) -> None:
        self.c = hub.client(ip)
        self.headers = bearer(token)
        self.stream_id = ""

    def start(self, **body):
        r = self.c.post("/stream/start", headers=self.headers, json=body or None)
        if r.status_code == 200:
            self.stream_id = r.json()["stream_id"]
        return r

    def send(self, seq: int, data: bytes | None = None, stream_id: str | None = None):
        return self.c.post(
            f"/stream/{stream_id or self.stream_id}/audio",
            headers={**self.headers, "Content-Type": "application/octet-stream"},
            params={"seq": seq},
            content=chunk() if data is None else data,
        )

    def finish(self, chunks: int | None, stream_id: str | None = None):
        body = {} if chunks is None else {"chunks": chunks}
        return self.c.post(
            f"/stream/{stream_id or self.stream_id}/finish", headers=self.headers, json=body
        )

    def cancel(self, stream_id: str | None = None):
        return self.c.request(
            "DELETE", f"/stream/{stream_id or self.stream_id}", headers=self.headers
        )


# ------------------------------------------------------------------ happy path


def test_info_offers_streaming(hub):
    r = hub.client().get("/info", headers=bearer(HUB_TOKEN))
    assert r.json()["accepts"] == ["audio", "text", "stream"]


def test_a_streamed_dictation_end_to_end(hub):
    phone = Phone(hub)
    r = phone.start(target="pc", sample_rate=16000, encoding="s16le")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"stream_id", "max_chunk_bytes"}
    assert len(phone.stream_id) == 32 and r.json()["max_chunk_bytes"] == MAX_CHUNK
    assert r.headers["cache-control"] == "no-store"
    for seq in range(3):
        r = phone.send(seq)
        assert r.status_code == 200, r.text
        assert r.json() == {"next_seq": seq + 1, "received_s": 2.0 * (seq + 1)}
    r = phone.finish(3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["streamed"] is True
    assert (body["text"], body["pasted"], body["status"], body["target"]) == (
        "hello",
        True,
        "ok",
        "pc",
    )
    assert body["route"] == "lan" and body["cleanup"] == "applied"
    assert set(body["timings_ms"]) == {"decode", "stt", "cleanup", "paste", "total"}
    assert hub.injector.pasted == ["hello"]
    (record,) = hub.recent.records
    assert record.extra["streamed"] is True and record.audio_seconds == pytest.approx(6.0)


def test_finish_answers_exactly_like_transcribe(hub):
    """Same delivery, same JSON: only "streamed" is added."""
    c = hub.client()
    upload = c.post(
        "/transcribe",
        headers={**bearer(HUB_TOKEN), "Content-Type": "audio/x-m4a"},
        content=b"audio-bytes",
    ).json()
    phone = Phone(hub)
    phone.start()
    phone.send(0)
    streamed = phone.finish(1).json()
    assert set(streamed) == set(upload) | {"streamed"}
    for key in ("text", "raw_text", "target", "pasted", "warning", "status", "route", "cleanup"):
        assert streamed[key] == upload[key], key
    assert hub.injector.pasted == ["hello", "hello"]
    assert [r.text for r in hub.recent.records] == ["hello", "hello"]


def test_finish_is_idempotent_and_pastes_once(hub, caplog):
    caplog.set_level(logging.INFO)
    phone = Phone(hub)
    phone.start()
    phone.send(0)
    first = phone.finish(1)
    again = phone.finish(1)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert hub.injector.pasted == ["hello"]
    assert len(hub.recent.records) == 1
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("shoutabout"))
    assert logged.count("streamed=true chunks=1 stretches=0") == 1
    assert "stream finish repeated" in logged
    assert "hello" not in logged and phone.stream_id not in logged


def test_target_phone_and_defaults(hub):
    phone = Phone(hub)
    r = hub.client().post("/stream/start", headers=bearer(HUB_TOKEN))  # no body at all
    assert r.status_code == 200
    phone.stream_id = r.json()["stream_id"]
    phone.send(0)
    assert phone.finish(None).json()["target"] == "pc"  # target missing -> pc
    phone.start(target="phone")
    phone.send(0)
    body = phone.finish(1).json()
    assert (body["target"], body["pasted"], body["warning"]) == ("phone", False, None)
    assert hub.injector.pasted == ["hello"]


def test_paused_hub_doesnt_paste(hub):
    hub.state.set_paused(True)
    phone = Phone(hub)
    phone.start()
    phone.send(0)
    body = phone.finish(1).json()
    assert (body["pasted"], body["warning"], body["status"]) == (False, PAUSED_WARNING, "warning")
    assert hub.injector.pasted == []


# ------------------------------------------------------------------ chunk order


def test_retries_are_ignored_and_gaps_are_409(hub):
    phone = Phone(hub)
    phone.start()
    assert phone.send(0).json() == {"next_seq": 1, "received_s": 2.0}
    assert phone.send(0).json() == {"next_seq": 1, "received_s": 2.0}  # a retry
    r = phone.send(2)
    assert r.status_code == 409
    assert r.json() == {"detail": "missing chunk", "next_seq": 1}
    assert phone.send(1).json() == {"next_seq": 2, "received_s": 4.0}
    r = phone.finish(3)
    assert r.status_code == 409
    assert r.json() == {"detail": "missing chunks", "next_seq": 2}
    assert phone.finish(2).status_code == 200


def test_404_for_unknown_finished_cancelled_and_expired_streams(hub):
    phone = Phone(hub)
    assert phone.send(0, stream_id="x" * 32).status_code == 404
    assert phone.finish(0, stream_id="x" * 32).status_code == 404
    phone.start()
    phone.send(0)
    phone.finish(1)
    assert phone.send(1).status_code == 404  # finished
    phone.start()
    assert phone.cancel().status_code == 204
    assert phone.send(0).status_code == 404  # cancelled
    assert phone.finish(0).status_code == 404
    phone.start()
    streams = hub.app.state.streams
    streams.clock = lambda: time.monotonic() + 61  # a minute without a request
    assert phone.send(0).status_code == 404  # expired
    assert hub.injector.pasted == ["hello"]


def test_delete_is_204_even_for_unknown_ids(hub):
    phone = Phone(hub)
    r = phone.cancel(stream_id="nope")
    assert r.status_code == 204 and r.content == b""


# ------------------------------------------------------------------ limits


def test_chunk_too_large_413(hub):
    phone = Phone(hub)
    phone.start()
    assert phone.send(0, b"\0" * (MAX_CHUNK + 2)).status_code == 413
    assert phone.send(0, b"\0" * MAX_CHUNK).status_code == 200


def test_audio_past_max_audio_seconds_413(tmp_path):
    hub = Hub(tmp_path, config=_cfg(max_audio_seconds=3))
    phone = Phone(hub)
    phone.start()
    assert phone.send(0).status_code == 200
    r = phone.send(1)
    assert r.status_code == 413
    assert phone.finish(1).status_code == 200  # what arrived is still fine


@pytest.mark.parametrize(
    ("data", "seq"),
    [(b"", "0"), (b"\x01\x02\x03", "0"), (None, ""), (None, "-1"), (None, "1.5"), (None, "x")],
    ids=["empty", "odd", "no-seq", "negative", "fraction", "garbage"],
)
def test_bad_chunks_422(hub, data, seq):
    phone = Phone(hub)
    phone.start()
    r = phone.c.post(
        f"/stream/{phone.stream_id}/audio",
        headers=phone.headers,
        params={"seq": seq} if seq else None,
        content=chunk() if data is None else data,
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "body",
    [
        {"sample_rate": 44100},
        {"sample_rate": "16000"},
        {"sample_rate": 16000.0},
        {"encoding": "f32le"},
        {"target": "tv"},
        {"target": 5},
        [],
    ],
)
def test_bad_start_422(hub, body):
    r = hub.client().post("/stream/start", headers=bearer(HUB_TOKEN), json=body)
    assert r.status_code == 422, r.text
    assert len(hub.app.state.streams) == 0


def test_bad_start_body_422(hub):
    r = hub.client().post("/stream/start", headers=bearer(HUB_TOKEN), content=b"not json")
    assert r.status_code == 422


@pytest.mark.parametrize("chunks", ["1", -1, 1.0, True, None])
def test_bad_finish_422(hub, chunks):
    phone = Phone(hub)
    phone.start()
    phone.send(0)
    body = {"chunks": chunks}
    r = phone.c.post(f"/stream/{phone.stream_id}/finish", headers=phone.headers, json=body)
    if chunks is None:  # null = not given: no check
        assert r.status_code == 200
    else:
        assert r.status_code == 422, r.text


def test_at_most_two_open_streams(hub):
    a, b, c = Phone(hub), Phone(hub), Phone(hub)
    assert a.start().status_code == 200
    assert b.start().status_code == 200
    r = c.start()
    assert r.status_code == 429
    a.send(0)
    a.finish(1)  # finished: no longer open
    assert c.start().status_code == 200
    assert Phone(hub).start().status_code == 429
    b.cancel()
    assert Phone(hub).start().status_code == 200


def test_503_while_the_speech_model_loads(tmp_path):
    hub = Hub(tmp_path, pipeline=False)
    r = Phone(hub).start()
    assert r.status_code == 503
    assert r.json()["detail"] == "hub is starting"


def test_engine_failure_is_503(hub):
    hub.ctx.pipeline.engine = FakeEngine(fail=EngineError("CUDA died"))
    phone = Phone(hub)
    phone.start()
    phone.send(0)
    r = phone.finish(1)
    assert r.status_code == 503 and r.json()["detail"] == "CUDA died"
    assert phone.finish(1).status_code == 503  # the same answer, never a paste
    assert hub.injector.pasted == []


# ------------------------------------------------------------------ auth


def test_every_stream_endpoint_needs_the_token(hub):
    phone = Phone(hub)
    phone.start()
    stranger = Phone(hub, token="wrong")
    stranger.stream_id = phone.stream_id
    for r in (stranger.start(), stranger.send(0), stranger.finish(0), stranger.cancel()):
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
    assert phone.send(0).status_code == 200  # untouched


def test_paired_phone_token_works_and_streams_are_private(hub):
    token = hub.pair()
    mine = Phone(hub, token=token)
    assert mine.start().status_code == 200
    assert mine.send(0).status_code == 200
    other = Phone(hub, token=HUB_TOKEN)  # another valid token
    other.stream_id = mine.stream_id
    assert other.send(1).status_code == 404
    assert other.finish(1).status_code == 404
    assert other.cancel().status_code == 204
    assert mine.send(1).status_code == 200  # still there
    body = mine.finish(2).json()
    assert body["text"] == "hello"
    assert hub.recent.records[-1].extra["source"] == "Sam's iPhone"


def test_tunnel_route_needs_the_access_jwt(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key()))
    access = AccessJwtValidator(AccessConfig(team_domain=TEAM, aud=[AUD]), jwks_client=jwks)
    hub = Hub(tmp_path, access=access)
    assert Phone(hub, ip=LOOPBACK).start().status_code == 403
    now = int(time.time())
    assertion = jwt.encode(
        {"aud": [AUD], "iss": f"https://{TEAM}", "iat": now, "exp": now + 300},
        key,
        algorithm="RS256",
        headers={"kid": "k"},
    )
    phone = Phone(hub, ip=LOOPBACK)
    phone.headers["Cf-Access-Jwt-Assertion"] = assertion
    assert phone.start().status_code == 200
    assert phone.send(0).status_code == 200
    assert phone.finish(1).json()["route"] == "tunnel"


def test_public_addresses_are_forbidden(hub):
    assert Phone(hub, ip=PUBLIC_IP).start().status_code == 403


def test_failed_tokens_on_chunks_count_towards_the_lockout(tmp_path):
    hub = Hub(tmp_path, config=_cfg(max_auth_failures=2))
    bad = Phone(hub, token="wrong")
    bad.stream_id = "x" * 32
    assert bad.send(0).status_code == 401
    assert bad.send(0).status_code == 401
    assert hub.client().get("/info", headers=bearer(HUB_TOKEN)).status_code == 429


# ------------------------------------------------------------------ rate limits


def test_chunks_dont_count_against_the_normal_rate_limit(tmp_path):
    hub = Hub(tmp_path, config=_cfg(rate_limit_per_minute=4))
    phone = Phone(hub)
    assert phone.start().status_code == 200  # 1
    for seq in range(40):  # 80 s of audio, 40 posts
        assert phone.send(seq, chunk(0.1)).status_code == 200
    assert phone.finish(40).status_code == 200  # 2
    c = hub.client()
    assert c.get("/info", headers=bearer(HUB_TOKEN)).status_code == 200  # 3
    assert c.get("/info", headers=bearer(HUB_TOKEN)).status_code == 200  # 4
    assert c.get("/info", headers=bearer(HUB_TOKEN)).status_code == 429


def test_chunks_have_their_own_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "STREAM_CHUNK_RATE_PER_MINUTE", 3)
    hub = Hub(tmp_path)
    phone = Phone(hub)
    phone.start()
    assert [phone.send(seq).status_code for seq in range(4)] == [200, 200, 200, 429]
    assert phone.finish(3).status_code == 200  # other endpoints are unaffected
    assert server_mod.STREAM_CHUNK_RATE_PER_MINUTE == 3
