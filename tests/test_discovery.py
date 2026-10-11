"""DNS-SD announcement: the ctypes structures and the Advertiser lifecycle, with a fake API."""

from __future__ import annotations

import ctypes
import threading
from types import SimpleNamespace

import pytest

from shoutabout import discovery as d
from shoutabout.discovery import (
    DNS_REQUEST_PENDING,
    Advertiser,
    ServiceInfo,
    build_registration,
    instance_label,
    read_instance,
    service_fqdn,
    service_info,
)

IDENT = SimpleNamespace(id="6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d", name="Kitchen PC")


class FakeApi:
    """Records calls; completes registrations synchronously like Windows does later."""

    def __init__(self, result: int = DNS_REQUEST_PENDING, complete: bool = True) -> None:
        self.result = result
        self.complete = complete
        self.registered: list[dict] = []
        self.deregistered: list[dict] = []
        self.freed: list[int] = []

    def register(self, request) -> int:
        inst = request.pServiceInstance.contents
        self.registered.append(read_instance(inst))
        if self.complete and self.result == DNS_REQUEST_PENDING:
            # Windows passes its own copy of the instance to the callback.
            request.pRegisterCompletionCallback(0, None, 0xDEAD0)
        return self.result

    def deregister(self, request) -> int:
        self.deregistered.append(read_instance(request.pServiceInstance.contents))
        if self.complete:
            request.pRegisterCompletionCallback(0, None, None)
        return DNS_REQUEST_PENDING

    def free_instance(self, ptr) -> None:
        self.freed.append(ptr)


def test_structure_layout_matches_windns():
    ptr = ctypes.sizeof(ctypes.c_void_p)
    inst = d.DNS_SERVICE_INSTANCE
    req = d.DNS_SERVICE_REGISTER_REQUEST
    assert inst.wPort.offset == 4 * ptr
    assert inst.dwPropertyCount.offset == 4 * ptr + 8
    if ptr == 8:
        assert (inst.keys.offset, inst.values.offset, inst.dwInterfaceIndex.offset) == (48, 56, 64)
        assert ctypes.sizeof(inst) == 72
        assert (req.pServiceInstance.offset, req.pRegisterCompletionCallback.offset) == (8, 16)
        assert (req.unicastEnabled.offset, ctypes.sizeof(req)) == (40, 48)


def test_build_registration_fills_everything():
    calls = []
    cb = d.REGISTER_COMPLETE(lambda status, ctx, inst: calls.append(status))
    txt = (("v", "1"), ("id", IDENT.id), ("name", "Kitchen PC"), ("ver", "0.2.0"))
    built = build_registration("Kitchen PC._shoutabout._tcp.local", "DESK.local", 47821, txt, cb)
    assert read_instance(built.instance) == {
        "instance": "Kitchen PC._shoutabout._tcp.local",
        "host": "DESK.local",
        "port": 47821,
        "txt": list(txt),
        "interface": 0,
    }
    req = built.request
    assert req.Version == d.DNS_QUERY_REQUEST_VERSION1
    assert req.InterfaceIndex == 0 and req.unicastEnabled == 0
    assert ctypes.addressof(req.pServiceInstance.contents) == ctypes.addressof(built.instance)
    assert not req.pQueryContext and not req.hCredentials
    assert not built.instance.ip4Address and not built.instance.ip6Address
    req.pRegisterCompletionCallback(0, None, None)
    assert calls == [0]


def test_build_registration_without_txt_and_bad_port():
    cb = d.REGISTER_COMPLETE(lambda *a: None)
    built = build_registration("x._shoutabout._tcp.local", "h.local", 1, (), cb)
    assert read_instance(built.instance)["txt"] == []
    for port in (0, 70000):
        with pytest.raises(ValueError):
            build_registration("x", "h", port, (), cb)


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Kitchen PC", "Kitchen PC"),
        ("my.pc", "my pc"),
        ("back\\slash", "back slash"),
        ("  lots   of   space  ", "lots of space"),
        ("", "ShoutAbout PC"),
        ("...", "ShoutAbout PC"),
    ],
)
def test_instance_label(name, label):
    assert instance_label(name) == label


def test_instance_label_fits_one_dns_label():
    label = instance_label("ü" * 100)
    assert len(label.encode("utf-8")) <= 63
    assert service_fqdn("A.B") == "A B._shoutabout._tcp.local"


def test_txt_record_has_exactly_the_contract_keys_and_no_secrets():
    info = service_info(IDENT, 47821, "0.2.0")
    assert dict(info.txt) == {"v": "1", "id": IDENT.id, "name": "Kitchen PC", "ver": "0.2.0"}
    assert info.instance == "Kitchen PC" and info.port == 47821
    assert d.txt_dict(info)["v"] == "1"


def test_advertiser_lifecycle():
    api = FakeApi()
    adv = Advertiser(lambda: api, host=lambda: "DESK.local")
    info = service_info(IDENT, 47821, "0.2.0")
    assert adv.update(info)
    assert adv.info == info
    (reg,) = api.registered
    assert reg["instance"] == "Kitchen PC._shoutabout._tcp.local"
    assert reg["host"] == "DESK.local" and reg["port"] == 47821
    assert api.freed == [0xDEAD0]  # Windows' copy freed; ours never is
    assert adv.update(info)  # unchanged: no re-registration
    assert len(api.registered) == 1
    renamed = service_info(SimpleNamespace(id=IDENT.id, name="Study"), 47821, "0.2.0")
    assert adv.update(renamed)  # rename: withdraw the old one, announce the new one
    assert [r["instance"] for r in api.deregistered] == ["Kitchen PC._shoutabout._tcp.local"]
    assert api.registered[-1]["instance"] == "Study._shoutabout._tcp.local"
    moved = ServiceInfo(renamed.instance, 47900, renamed.txt)
    assert adv.update(moved)
    assert api.registered[-1]["port"] == 47900
    adv.stop()
    assert len(api.deregistered) == 3
    assert adv.info is None
    adv.stop()  # twice is fine


def test_advertiser_without_dns_sd_is_quiet():
    def unavailable():
        raise OSError("this Windows has no DnsServiceRegister")

    adv = Advertiser(unavailable, host=lambda: "h.local")
    assert not adv.update(service_info(IDENT, 1, "x"))
    assert adv.available is False
    assert not adv.update(service_info(IDENT, 1, "x"))
    adv.stop()


def test_advertiser_refused_registration():
    api = FakeApi(result=87)  # ERROR_INVALID_PARAMETER
    adv = Advertiser(lambda: api, host=lambda: "h.local")
    assert not adv.update(service_info(IDENT, 1, "x"))
    assert adv.info is None


def test_deregistration_that_never_confirms_keeps_structures_alive(monkeypatch):
    api = FakeApi(complete=False)
    adv = Advertiser(lambda: api, host=lambda: "h.local")
    monkeypatch.setattr(d, "DEREGISTER_WAIT_S", 0.01)
    adv.update(service_info(IDENT, 1, "x"))
    reg = adv._current
    before = len(d._orphans)
    reg.stop(wait_s=0.01)
    assert len(d._orphans) == before + 1
    d._orphans.remove(reg)


def test_callback_from_another_thread_never_raises():
    api = FakeApi(complete=False)
    reg = d.Registration(api, service_info(IDENT, 1, "x"), "h.local")
    api.free_instance = None  # would raise inside the callback
    t = threading.Thread(target=lambda: reg._on_complete(0, None, 0x1))
    t.start()
    t.join()
    reg._on_complete(5, None, None)
    assert reg.status == 5 and reg.registered.is_set()


def test_host_fqdn():
    assert d.host_fqdn("DESK-1") == "DESK-1.local"
    assert d.host_fqdn("box.example.com") == "box.local"
    assert d.host_fqdn().endswith(".local")
