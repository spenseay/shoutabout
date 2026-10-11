"""Announce this PC on the Wi-Fi with DNS-SD (Bonjour), so the iPhone finds it by itself.

Service type ``_shoutabout._tcp`` in ``local.``, on the hub's port. The instance name is
the PC's friendly name (:mod:`shoutabout.identity`); the TXT record carries::

    v=1   id=<installation UUID>   name=<friendly name>   ver=<app version>

No secrets ever go in it: anyone on the network can read it.

Windows 10 1809+ has an mDNS responder built in, reachable through ``DnsServiceRegister`` /
``DnsServiceDeRegister`` in dnsapi.dll, so this needs no third-party library (and no LGPL
zeroconf): it's plain ctypes. Registration is asynchronous: Windows calls our completion
callback on one of its threads. The structures handed to Windows (and the callback thunk)
are kept alive by :class:`Registration` until the deregistration has completed.

:class:`Advertiser` is what the hub uses: ``start`` while the hub runs, ``update`` after a
rename or a port change (it re-registers), ``stop`` on exit. It never raises: without
DNS-SD the phone can still pair with the QR code, so a failure is only logged.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from shoutabout import __version__

log = logging.getLogger(__name__)

SERVICE_TYPE = "_shoutabout._tcp"
DOMAIN = "local"
TXT_VERSION = "1"
DNS_QUERY_REQUEST_VERSION1 = 1
DNS_REQUEST_PENDING = 9506
ERROR_SUCCESS = 0
INSTANCE_MAX_BYTES = 63  # one DNS label
DEREGISTER_WAIT_S = 2.0

# --------------------------------------------------------------------- structures
# windns.h. Pointer-sized fields line up with the platform's C layout through ctypes.

LPWSTR = ctypes.c_wchar_p


class DNS_SERVICE_INSTANCE(ctypes.Structure):  # noqa: N801 - the Windows name
    _fields_ = [
        ("pszInstanceName", LPWSTR),
        ("pszHostName", LPWSTR),
        ("ip4Address", ctypes.c_void_p),  # IP4_ADDRESS* (NULL: Windows answers for the host)
        ("ip6Address", ctypes.c_void_p),  # IP6_ADDRESS*
        ("wPort", ctypes.c_uint16),
        ("wPriority", ctypes.c_uint16),
        ("wWeight", ctypes.c_uint16),
        ("dwPropertyCount", ctypes.c_uint32),
        ("keys", ctypes.POINTER(LPWSTR)),
        ("values", ctypes.POINTER(LPWSTR)),
        ("dwInterfaceIndex", ctypes.c_uint32),
    ]


# VOID WINAPI DNS_SERVICE_REGISTER_COMPLETE(DWORD Status, PVOID pQueryContext,
#                                           PDNS_SERVICE_INSTANCE pInstance)
_CALLBACK_FACTORY = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
REGISTER_COMPLETE = _CALLBACK_FACTORY(None, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)


class DNS_SERVICE_REGISTER_REQUEST(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("Version", ctypes.c_uint32),  # ULONG is 32-bit on Windows
        ("InterfaceIndex", ctypes.c_uint32),
        ("pServiceInstance", ctypes.POINTER(DNS_SERVICE_INSTANCE)),
        ("pRegisterCompletionCallback", REGISTER_COMPLETE),
        ("pQueryContext", ctypes.c_void_p),
        ("hCredentials", ctypes.c_void_p),
        ("unicastEnabled", ctypes.c_int),  # BOOL
    ]


# ------------------------------------------------------------------- pure helpers


def instance_label(name: str) -> str:
    """The friendly name as one DNS-SD instance label: no dots or backslashes (they would
    split or escape the label), at most 63 bytes of UTF-8, never empty."""
    label = "".join(" " if ch in ".\\" else ch for ch in name).strip()
    label = " ".join(label.split())
    while label and len(label.encode("utf-8")) > INSTANCE_MAX_BYTES:
        label = label[:-1].rstrip()
    return label or "ShoutAbout PC"


def service_fqdn(instance: str) -> str:
    return f"{instance_label(instance)}.{SERVICE_TYPE}.{DOMAIN}"


def host_fqdn(host: str | None = None) -> str:
    """``<computer name>.local``: Windows' own mDNS responder answers for that name."""
    name = (host or socket.gethostname() or "localhost").split(".")[0]
    return f"{name}.{DOMAIN}"


@dataclass(frozen=True)
class ServiceInfo:
    instance: str
    port: int
    txt: tuple[tuple[str, str], ...] = ()


def service_info(identity: Any, port: int, version: str = __version__) -> ServiceInfo:
    """What to announce for an :class:`~shoutabout.identity.Identity` on ``port``."""
    txt = (
        ("v", TXT_VERSION),
        ("id", str(identity.id)),
        ("name", str(identity.name)),
        ("ver", version),
    )
    return ServiceInfo(instance=str(identity.name), port=int(port), txt=txt)


@dataclass
class Built:
    """The structures for one registration plus everything they point into."""

    instance: DNS_SERVICE_INSTANCE
    request: DNS_SERVICE_REGISTER_REQUEST
    keepalive: list[Any] = field(default_factory=list)


def build_registration(
    instance_name: str,
    host_name: str,
    port: int,
    txt: Sequence[tuple[str, str]],
    callback: Any,
    *,
    interface_index: int = 0,
) -> Built:
    """Fill the DNS_SERVICE_INSTANCE and DNS_SERVICE_REGISTER_REQUEST for one service.

    ``callback`` must be a :data:`REGISTER_COMPLETE` instance. Every buffer the structures
    point at is kept in ``keepalive``; hold on to the result until Windows is done with it.
    """
    if not 1 <= port <= 65535:
        raise ValueError(f"bad port {port}")
    keys = [k for k, _ in txt]
    values = [v for _, v in txt]
    key_array = (LPWSTR * max(1, len(keys)))(*keys)
    value_array = (LPWSTR * max(1, len(values)))(*values)
    inst = DNS_SERVICE_INSTANCE()
    inst.pszInstanceName = instance_name
    inst.pszHostName = host_name
    inst.ip4Address = None
    inst.ip6Address = None
    inst.wPort = port
    inst.wPriority = 0
    inst.wWeight = 0
    inst.dwPropertyCount = len(keys)
    inst.keys = ctypes.cast(key_array, ctypes.POINTER(LPWSTR))
    inst.values = ctypes.cast(value_array, ctypes.POINTER(LPWSTR))
    inst.dwInterfaceIndex = interface_index
    req = DNS_SERVICE_REGISTER_REQUEST()
    req.Version = DNS_QUERY_REQUEST_VERSION1
    req.InterfaceIndex = interface_index
    req.pServiceInstance = ctypes.pointer(inst)
    req.pRegisterCompletionCallback = callback
    req.pQueryContext = None
    req.hCredentials = None
    req.unicastEnabled = 0
    return Built(inst, req, [key_array, value_array, instance_name, host_name, callback])


def read_instance(inst: DNS_SERVICE_INSTANCE) -> dict[str, Any]:
    """The fields of an instance as plain Python (tests, logging)."""
    count = int(inst.dwPropertyCount)
    return {
        "instance": inst.pszInstanceName,
        "host": inst.pszHostName,
        "port": int(inst.wPort),
        "txt": [(inst.keys[i], inst.values[i]) for i in range(count)],
        "interface": int(inst.dwInterfaceIndex),
    }


# ------------------------------------------------------------------------- the API


class DnsSdApi(Protocol):
    """The two dnsapi.dll calls (a fake in tests)."""

    def register(self, request: Any) -> int: ...

    def deregister(self, request: Any) -> int: ...

    def free_instance(self, instance_ptr: int) -> None: ...


class WindowsDnsSd:
    """dnsapi.dll, loaded on first use. Raises OSError where DNS-SD isn't available."""

    def __init__(self) -> None:
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("DNS-SD registration needs Windows")
        dll = ctypes.WinDLL("dnsapi", use_last_error=True)
        try:
            self._register = dll.DnsServiceRegister
            self._deregister = dll.DnsServiceDeRegister
            self._free = dll.DnsServiceFreeInstance
        except AttributeError as exc:  # older than Windows 10 1809
            raise OSError("this Windows has no DnsServiceRegister (needs 10 1809+)") from exc
        req_ptr = ctypes.POINTER(DNS_SERVICE_REGISTER_REQUEST)
        self._register.argtypes = [req_ptr, ctypes.c_void_p]
        self._register.restype = ctypes.c_uint32
        self._deregister.argtypes = [req_ptr, ctypes.c_void_p]
        self._deregister.restype = ctypes.c_uint32
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def register(self, request: Any) -> int:
        return int(self._register(ctypes.byref(request), None))

    def deregister(self, request: Any) -> int:
        return int(self._deregister(ctypes.byref(request), None))

    def free_instance(self, instance_ptr: int) -> None:
        if instance_ptr:
            self._free(instance_ptr)


class Registration:
    """One live registration: owns the structures and the callback until it's undone."""

    def __init__(self, api: DnsSdApi, info: ServiceInfo, host: str) -> None:
        self.api = api
        self.info = info
        self.registered = threading.Event()
        self.unregistered = threading.Event()
        self.status: int | None = None
        self._deregistering = False
        self._callback = REGISTER_COMPLETE(self._on_complete)
        self.built = build_registration(
            service_fqdn(info.instance), host, info.port, info.txt, self._callback
        )

    def _on_complete(self, status: int, _context: Any, instance: Any) -> None:
        # Runs on a Windows thread pool thread. Must never raise.
        try:
            # Windows hands us its own copy of the instance to free; never free ours.
            if instance and instance != ctypes.addressof(self.built.instance):
                self.api.free_instance(instance)
            if self._deregistering:
                self.unregistered.set()
                if status != ERROR_SUCCESS:
                    log.warning("DNS-SD deregistration finished with status %d", status)
                return
            self.status = int(status)
            if status == ERROR_SUCCESS:
                log.info("announced on the network as %r", self.info.instance)
            else:
                log.warning("DNS-SD registration failed with status %d", status)
            self.registered.set()
        except Exception:
            log.exception("DNS-SD callback failed")

    def start(self) -> bool:
        result = self.api.register(self.built.request)
        if result != DNS_REQUEST_PENDING:
            log.warning("DnsServiceRegister refused the request (status %d)", result)
            return False
        return True

    def stop(self, wait_s: float = DEREGISTER_WAIT_S) -> None:
        self._deregistering = True
        result = self.api.deregister(self.built.request)
        if result != DNS_REQUEST_PENDING:
            log.warning("DnsServiceDeRegister refused the request (status %d)", result)
            return
        if not self.unregistered.wait(wait_s):
            # Keep the structures alive anyway: Windows may still call back later.
            _orphans.append(self)
            log.warning("DNS-SD deregistration didn't confirm within %.0f s", wait_s)


_orphans: list[Registration] = []  # registrations Windows might still call back into


class Advertiser:
    """Keeps this PC announced while the hub runs. Thread-safe; never raises."""

    def __init__(
        self,
        api_factory: Callable[[], DnsSdApi] = WindowsDnsSd,
        *,
        host: Callable[[], str] = host_fqdn,
    ) -> None:
        self._api_factory = api_factory
        self._api: DnsSdApi | None = None
        self._host = host
        self._lock = threading.Lock()
        self._current: Registration | None = None
        self.available = True

    @property
    def info(self) -> ServiceInfo | None:
        with self._lock:
            return self._current.info if self._current is not None else None

    def _get_api(self) -> DnsSdApi | None:
        if self._api is None and self.available:
            try:
                self._api = self._api_factory()
            except OSError as exc:
                self.available = False
                log.warning("network discovery is unavailable: %s (pair with the QR code)", exc)
        return self._api

    def update(self, info: ServiceInfo) -> bool:
        """Announce ``info`` (re-registering if it differs from what's announced)."""
        with self._lock:
            if self._current is not None and self._current.info == info:
                return True
            api = self._get_api()
            if api is None:
                return False
            self._stop_locked()
            try:
                reg = Registration(api, info, self._host())
                if not reg.start():
                    return False
            except Exception:
                log.exception("couldn't announce this PC on the network")
                return False
            self._current = reg
            return True

    start = update

    def _stop_locked(self) -> None:
        reg, self._current = self._current, None
        if reg is None:
            return
        try:
            reg.stop()
        except Exception:
            log.exception("couldn't withdraw the network announcement")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


def txt_dict(info: ServiceInfo) -> Mapping[str, str]:
    return dict(info.txt)
