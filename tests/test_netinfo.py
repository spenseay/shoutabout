from __future__ import annotations

import socket

import pytest

from shoutabout import netinfo
from shoutabout.config import Config


class FakeSocket:
    def __init__(self, ip=None, error=None):
        self.ip = ip
        self.error = error
        self.connected = None

    def __call__(self, family, kind):
        assert family == socket.AF_INET
        assert kind == socket.SOCK_DGRAM
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, addr):
        if self.error:
            raise self.error
        self.connected = addr

    def getsockname(self):
        return (self.ip, 54321)


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("192.168.1.50", "192.168.1.50"),
        ("10.0.0.5", "10.0.0.5"),
        ("172.20.1.1", "172.20.1.1"),
        ("8.8.8.8", None),
        ("100.64.0.1", None),
        ("169.254.3.4", None),
        ("127.0.0.1", None),
        ("0.0.0.0", None),
    ],
)
def test_lan_ipv4(monkeypatch, ip, expected):
    fake = FakeSocket(ip)
    monkeypatch.setattr(netinfo.socket, "socket", fake)
    assert netinfo.lan_ipv4() == expected
    assert fake.connected == ("192.0.2.1", 9)


def test_lan_ipv4_no_route(monkeypatch):
    monkeypatch.setattr(netinfo.socket, "socket", FakeSocket(error=OSError("unreachable")))
    assert netinfo.lan_ipv4() is None


def test_lan_ipv4_real_socket_is_private_or_none():
    ip = netinfo.lan_ipv4()
    assert ip is None or netinfo.is_rfc1918(ip)


def test_lan_url_detected(monkeypatch):
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: "192.168.1.50")
    assert netinfo.lan_url(Config()) == "http://192.168.1.50:47821"


def test_lan_url_advertise_host_wins(monkeypatch):
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: "192.168.1.50")
    cfg = Config()
    cfg.server.advertise_host = "192.168.1.50"
    cfg.server.port = 9000
    assert netinfo.lan_url(cfg) == "http://192.168.1.50:9000"


def test_lan_url_ipv6_advertise(monkeypatch):
    cfg = Config()
    cfg.server.advertise_host = "fd00::5"
    assert netinfo.lan_url(cfg) == "http://[fd00::5]:47821"


def test_lan_url_none_without_ip(monkeypatch):
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: None)
    assert netinfo.lan_url(Config()) is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_lan_url_none_when_loopback_only(monkeypatch, host):
    monkeypatch.setattr(netinfo, "lan_ipv4", lambda: "192.168.1.50")
    cfg = Config()
    cfg.server.host = host
    assert netinfo.lan_url(cfg) is None


@pytest.mark.parametrize(
    ("ip", "ok"),
    [
        ("10.1.1.1", True),
        ("172.31.0.1", True),
        ("172.32.0.1", False),
        ("192.168.5.5", True),
        ("garbage", False),
        ("fd00::1", False),
    ],
)
def test_is_rfc1918(ip, ok):
    assert netinfo.is_rfc1918(ip) is ok


NETSH_CONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Example Wireless Adapter
    GUID                   : 00000000-0000-0000-0000-000000000000
    Physical address       : 02:00:00:00:00:01
    Interface type         : Primary
    State                  : connected
    SSID                   : Example Home WiFi
    AP BSSID               : 02:00:00:00:00:02
    BSSID                  : 02:00:00:00:00:02
    Network type           : Infrastructure
    Radio type             : 802.11ax
    Authentication         : WPA2-Personal
""".replace("\n", "\r\n")

NETSH_DISCONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    State                  : disconnected
"""

NETSH_NO_LOCATION = """
Network shell commands need location permission to access WLAN information.
"""


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (NETSH_CONNECTED, "Example Home WiFi"),
        ("    BSSID : 02:00:00:00:00:02\n    SSID : Net-5G \n", "Net-5G"),
        ("    SSID                   : \n    SSID : Second\n", "Second"),
        (NETSH_DISCONNECTED, None),
        (NETSH_NO_LOCATION, None),
        ("", None),
    ],
)
def test_parse_netsh_ssid(output, expected):
    assert netinfo.parse_netsh_ssid(output) == expected


def test_wifi_ssid_off_windows(monkeypatch):
    monkeypatch.setattr(netinfo.sys, "platform", "linux")
    assert netinfo.wifi_ssid() is None


def test_wifi_ssid_parses_netsh(monkeypatch):
    class Proc:
        stdout = NETSH_CONNECTED.encode("ascii")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Proc()

    monkeypatch.setattr(netinfo.sys, "platform", "win32")
    monkeypatch.setattr(netinfo.subprocess, "run", fake_run)
    assert netinfo.wifi_ssid() == "Example Home WiFi"
    assert calls == [["netsh", "wlan", "show", "interfaces"]]


def test_wifi_ssid_netsh_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("netsh")

    monkeypatch.setattr(netinfo.sys, "platform", "win32")
    monkeypatch.setattr(netinfo.subprocess, "run", fake_run)
    assert netinfo.wifi_ssid() is None
