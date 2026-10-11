"""PairingManager: the pairing state machine without HTTP."""

from __future__ import annotations

import threading
import uuid

import pytest

from shoutabout import pair_requests as pr
from shoutabout.devices import DeviceStore
from shoutabout.pair_requests import PairError, PairingManager, PairStatus, validate_request

PHONE = "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d"


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    devices = DeviceStore(tmp_path / "devices.json")
    manager = PairingManager(devices, clock=clock)
    events: list = []
    manager.subscribe(
        on_request=lambda p: events.append(("request", p.device_name)),
        on_finished=lambda rid, st: events.append(("finished", str(st))),
    )
    return manager, devices, clock, events


def test_codes_are_four_digits_and_ids_long():
    manager = PairingManager(DeviceStore.__new__(DeviceStore))
    codes = {manager._code() for _ in range(200)}
    assert all(len(c) == 4 and c.isdigit() for c in codes)
    assert len(codes) > 50


def test_full_flow_and_events(env):
    manager, devices, _clock, events = env
    prompt = manager.request("Sam's iPhone", PHONE, "192.168.1.5")
    assert len(prompt.request_id) == 32
    assert manager.pending() == prompt
    assert manager.status(prompt.request_id) == {"status": "pending"}
    assert manager.approve(prompt.request_id)
    assert manager.pending() is None
    body = manager.status(prompt.request_id)
    assert body["status"] == "approved"
    assert devices.match(body["token"]).name == "Sam's iPhone"
    assert manager.status(prompt.request_id) == {"status": "claimed"}
    assert events == [
        ("request", "Sam's iPhone"),
        ("finished", "approved"),
        ("finished", "claimed"),
    ]


def test_answering_twice_does_nothing(env):
    manager, devices, _clock, _events = env
    prompt = manager.request("P", PHONE, "10.0.0.2")
    assert manager.deny(prompt.request_id)
    assert not manager.deny(prompt.request_id)
    assert not manager.approve(prompt.request_id)
    assert devices.list() == []
    assert not manager.approve("unknown")
    assert not manager.deny("unknown")


def test_expiry_fires_an_event(env):
    manager, _devices, clock, events = env
    prompt = manager.request("P", PHONE, "10.0.0.2")
    assert manager.seconds_left(prompt) == pr.REQUEST_TTL_S
    clock.now += pr.REQUEST_TTL_S
    assert manager.seconds_left(prompt) == 0
    assert manager.pending() is None
    assert ("finished", "expired") in events
    assert manager.status(prompt.request_id) == {"status": "expired"}


def test_device_save_failure_keeps_the_request_pending(env, monkeypatch):
    manager, devices, _clock, _events = env
    prompt = manager.request("P", PHONE, "10.0.0.2")

    def broken(*_a):
        raise OSError("disk full")

    monkeypatch.setattr(devices, "add", broken)
    with pytest.raises(OSError):
        manager.approve(prompt.request_id)
    assert manager.pending() == prompt


def test_disabled_manager_refuses(env):
    manager, *_ = env
    manager.enabled = False
    with pytest.raises(PairError) as err:
        manager.request("P", PHONE, "10.0.0.2")
    assert err.value.status == 503


def test_listener_errors_are_contained(env):
    manager, *_ = env

    def boom(*_a):
        raise RuntimeError("ui gone")

    manager.subscribe(on_request=boom, on_finished=boom)
    prompt = manager.request("P", PHONE, "10.0.0.2")
    assert manager.approve(prompt.request_id)
    manager.unsubscribe(boom)
    assert boom not in manager._on_request


def test_lookup_is_constant_time_over_all_requests(env, monkeypatch):
    manager, _devices, clock, _events = env
    ids = []
    for i in range(3):
        prompt = manager.request("P", PHONE, f"10.0.0.{i + 2}")
        manager.deny(prompt.request_id)
        clock.now += pr.DENY_COOLDOWN_S
        ids.append(prompt.request_id)
    calls = []
    real = pr.hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(pr.hmac, "compare_digest", counting)
    for rid in (ids[0], ids[-1], "x" * 32):
        calls.clear()
        manager.status(rid)
        assert len(calls) == 3


def test_concurrent_requests_open_only_one(env):
    manager, *_ = env
    results = []
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        try:
            manager.request("P", PHONE, f"10.1.0.{i}")
            results.append(202)
        except PairError as exc:
            results.append(exc.status)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [202] + [409] * 7


def test_an_uncollected_approval_is_never_forgotten_early(env):
    manager, devices, clock, _events = env
    first = manager.request("P", PHONE, "10.9.0.1")
    assert manager.approve(first.request_id)
    for i in range(pr.MAX_TRACKED + 5):  # lots of finished requests after it
        p = manager.request("Q", PHONE, f"10.8.{i // 200}.{i % 200 + 1}")
        manager.deny(p.request_id)
        clock.now += 0.01
        manager._per_ip.reset(f"10.8.{i // 200}.{i % 200 + 1}")
        manager._global.reset("*")
    clock.now += pr.CLAIM_TTL_S
    assert manager.status(first.request_id) == {"status": "expired"}
    assert devices.list() == []  # the uncollected token was revoked


def test_old_requests_are_forgotten(env):
    manager, _devices, clock, _events = env
    prompt = manager.request("P", PHONE, "10.0.0.2")
    manager.deny(prompt.request_id)
    clock.now += pr.RESULT_TTL_S
    assert manager.status(prompt.request_id) is None


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ({"device_name": "Sam", "device_id": PHONE}, ("Sam", PHONE)),
        ({"device_name": "Sam", "device_id": PHONE.upper(), "x": 1}, ("Sam", PHONE)),
        ({"device_name": "x" * 60, "device_id": PHONE}, ("x" * 60, PHONE)),
        ({"device_name": "x" * 61, "device_id": PHONE}, None),
        ({"device_name": "", "device_id": PHONE}, None),
        ({"device_name": "Sam", "device_id": "{" + PHONE + "}"}, ("Sam", PHONE)),
        ({"device_name": "Sam", "device_id": "x" * 65}, None),
        ({"device_name": ["Sam"], "device_id": PHONE}, None),
        ([], None),
        ("text", None),
    ],
)
def test_validate_request(body, ok):
    if ok is None:
        with pytest.raises(PairError) as err:
            validate_request(body)
        assert err.value.status == 422
    else:
        assert validate_request(body) == ok


def test_status_values_are_the_contract():
    assert {s.value for s in PairStatus} == {"pending", "approved", "denied", "expired", "claimed"}
    uuid.UUID(PHONE)
