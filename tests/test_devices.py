"""Per-device tokens (devices.json), the installation identity and name cleaning."""

from __future__ import annotations

import json
import uuid

import pytest

from shoutabout import devices as devices_mod
from shoutabout.devices import MAX_DEVICES, DeviceLimitError, DeviceStore, token_digest
from shoutabout.identity import IdentityStore, clean_friendly_name, computer_name
from shoutabout.names import clean_name
from shoutabout.security import HUB_TOKEN, authenticate, parse_bearer, sha256_digest

RLO = chr(0x202E)  # right-to-left override
ZWSP = chr(0x200B)  # zero-width space


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path):
    return DeviceStore(tmp_path / "devices.json", clock=Clock())


# ------------------------------------------------------------------ devices


def test_add_stores_only_the_hash(store):
    device, token = store.add("Sam's iPhone", "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d")
    assert len(token) == 43
    raw = store.path.read_text("utf-8")
    assert token not in raw
    data = json.loads(raw)
    assert data["version"] == 1
    (row,) = data["devices"]
    assert row["token_sha256"] == token_digest(token).hex()
    assert set(row) == {"key", "device_id", "name", "token_sha256", "created", "last_seen"}
    assert store.match(token) == device
    assert store.match(token + "x") is None
    assert store.match("") is None


def test_tokens_are_unique_and_random(store):
    tokens = {store.add(f"P{i}", str(uuid.uuid4()))[1] for i in range(10)}
    assert len(tokens) == 10


def test_same_phone_pairing_again_replaces_it(store):
    phone = str(uuid.uuid4())
    _, old = store.add("Old name", phone)
    _, new = store.add("New name", phone)
    assert [d.name for d in store.list()] == ["New name"]
    assert store.match(old) is None
    assert store.match(new) is not None


def test_remove_revokes(store):
    device, token = store.add("P", str(uuid.uuid4()))
    assert store.remove(device.key)
    assert store.match(token) is None
    assert not store.remove(device.key)
    assert store.list() == []


def test_changes_by_another_process_are_seen(tmp_path):
    a = DeviceStore(tmp_path / "devices.json")
    b = DeviceStore(tmp_path / "devices.json")
    device, token = a.add("P", str(uuid.uuid4()))
    assert b.match(token) is not None
    b.remove(device.key)
    assert a.match(token) is None


def test_corrupt_file_fails_closed_and_is_kept_aside(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text("{not json", "utf-8")
    store = DeviceStore(path)
    assert store.list() == []
    assert store.match("anything") is None
    kept = list(tmp_path.glob("devices.json.bad-*"))
    assert len(kept) == 1 and kept[0].read_text("utf-8") == "{not json"
    _, token = store.add("P", str(uuid.uuid4()))  # a fresh file works again
    assert store.match(token) is not None


def test_bad_rows_are_skipped(tmp_path):
    path = tmp_path / "devices.json"
    good = "a" * 64
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "devices": [
                    {"key": "k1", "token_sha256": "zz"},
                    {"key": "", "token_sha256": good},
                    {"token_sha256": good},
                    "nonsense",
                    {"key": "k2", "token_sha256": "ab" * 16},
                    {"key": "k3", "token_sha256": good, "name": f"Evil{RLO}x"},
                ],
            }
        ),
        "utf-8",
    )
    (device,) = DeviceStore(path).list()
    assert device.key == "k3"
    assert device.name == "Evilx"


def test_device_limit(store):
    for i in range(MAX_DEVICES):
        store.add(f"P{i}", str(uuid.uuid4()))
    with pytest.raises(DeviceLimitError):
        store.add("one too many", str(uuid.uuid4()))
    assert len(store.list()) == MAX_DEVICES


def test_failed_write_changes_nothing(store, monkeypatch):
    device, token = store.add("P", str(uuid.uuid4()))

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(devices_mod, "atomic_write_json", broken)
    with pytest.raises(OSError):
        store.add("Q", str(uuid.uuid4()))
    with pytest.raises(OSError):
        store.remove(device.key)
    assert [d.name for d in store.list()] == ["P"]
    assert store.match(token) is not None


def test_touch_is_written_at_most_every_few_minutes(tmp_path, monkeypatch):
    clock = Clock()
    store = DeviceStore(tmp_path / "devices.json", clock=clock)
    device, _ = store.add("P", str(uuid.uuid4()))
    writes = []
    real = devices_mod.atomic_write_json

    def counting(path, data):
        writes.append(1)
        real(path, data)

    monkeypatch.setattr(devices_mod, "atomic_write_json", counting)
    store.touch(device.key)
    store.touch(device.key)
    clock.now += 10
    store.touch(device.key)
    assert len(writes) == 1
    clock.now += devices_mod.LAST_SEEN_WRITE_INTERVAL_S
    store.touch(device.key)
    assert len(writes) == 2
    store.touch("unknown")
    assert store.list()[0].last_seen is not None
    assert json.loads(store.path.read_text("utf-8"))["devices"][0]["last_seen"]


def test_match_compares_every_hash(store, monkeypatch):
    tokens = [store.add(f"P{i}", str(uuid.uuid4()))[1] for i in range(4)]
    calls = []
    real = devices_mod.hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(devices_mod.hmac, "compare_digest", counting)
    for token in (tokens[0], tokens[-1], "nope"):
        calls.clear()
        store.match(token)
        assert len(calls) == 4


# ------------------------------------------------------------- bearer helpers


@pytest.mark.parametrize(
    ("header", "token"),
    [
        ("Bearer abc", "abc"),
        ("  bearer   abc  ", "abc"),
        ("BEARER\tabc", "abc"),
        ("Bearer", None),
        ("Bearer ", None),
        ("Basic abc", None),
        ("abc", None),
        ("Bearer a b", "a b"),  # never matches a real token: tokens have no spaces
        ("Bearer " + "x" * 513, None),
        (None, None),
        ("", None),
    ],
)
def test_parse_bearer(header, token):
    assert parse_bearer(header) == token


def test_authenticate_hub_device_and_nothing(store):
    hub = "H" * 43
    device, token = store.add("P", str(uuid.uuid4()))
    hub_digest = sha256_digest(hub)
    assert authenticate(f"Bearer {hub}", hub_digest, store) == HUB_TOKEN
    assert authenticate(f"Bearer {token}", hub_digest, store) == device
    assert authenticate("Bearer nope", hub_digest, store) is None
    assert authenticate(None, hub_digest, store) is None
    assert authenticate(f"Bearer {token}", None, store) == device
    assert authenticate(f"Bearer {hub}", hub_digest, None) == HUB_TOKEN
    assert authenticate(f"Bearer {hub}", None, None) is None


# ------------------------------------------------------------------- identity


def test_identity_is_stable_and_default_name_follows_the_pc(tmp_path):
    names = iter(["DESK-1", "DESK-2"])
    store = IdentityStore(tmp_path / "identity.json", default_name=lambda: next(names))
    first = store.get()
    uuid.UUID(first.id)
    assert (first.name, first.custom_name) == ("DESK-1", False)
    second = IdentityStore(tmp_path / "identity.json", default_name=lambda: "X").get()
    assert second.id == first.id
    assert store.get().name == "DESK-2"  # a Windows rename is picked up
    assert json.loads((tmp_path / "identity.json").read_text("utf-8")) == {
        "id": first.id,
        "name": None,
    }


def test_identity_rename_and_reset(tmp_path):
    store = IdentityStore(tmp_path / "identity.json", default_name=lambda: "DESK-1")
    ident = store.rename(f"  Kitchen{ZWSP}   PC\n")
    assert (ident.name, ident.custom_name) == ("Kitchen PC", True)
    assert IdentityStore(tmp_path / "identity.json").get().name == "Kitchen PC"
    assert store.rename("").name == "DESK-1"
    assert store.rename(None).custom_name is False
    with pytest.raises(ValueError):
        store.rename(f"{RLO}{ZWSP}")


def test_identity_bad_file_gets_a_new_id(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text('{"id": "not-a-uuid", "name": 5}', "utf-8")
    ident = IdentityStore(path, default_name=lambda: "D").get()
    uuid.UUID(ident.id)
    assert ident.name == "D"
    path.write_text("garbage", "utf-8")
    uuid.UUID(IdentityStore(path, default_name=lambda: "D").get().id)


def test_friendly_names_fit_a_dns_label():
    assert clean_friendly_name("é" * 40).encode("utf-8") == ("é" * 31).encode("utf-8")
    assert len(clean_friendly_name("x" * 100)) == 40
    assert clean_friendly_name(7) == ""
    assert computer_name()


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Sam's iPhone", "Sam's iPhone"),
        (f"  a{RLO}b{ZWSP}c \t\n d ", "abc d"),
        (chr(0) + chr(0x1B) + "[31mred", "[31mred"),
        (None, ""),
        (b"bytes", ""),
        ("x" * 100, "x" * 60),
    ],
)
def test_clean_name(raw, clean):
    assert clean_name(raw) == clean
