"""LAN address discovery for the tray / CLI / doctor."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import subprocess
import sys

from shoutabout.config import Config

log = logging.getLogger(__name__)

NETSH_TIMEOUT_S = 10.0
# "    SSID                   : MyHomeWiFi" (but not "BSSID" / "AP BSSID")
_NETSH_SSID_RE = re.compile(r"^[ \t]*SSID[ \t]*:[ \t]?(.*?)[ \t\r]*$", re.MULTILINE)

# TEST-NET-1 (RFC 5737): never routed, so connecting a UDP socket to it only asks the OS
# which local address the default route would use. No packet is sent.
_PROBE_ADDR = ("192.0.2.1", 9)

RFC1918 = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_rfc1918(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(addr in net for net in RFC1918)


def lan_ipv4() -> str | None:
    """The private IPv4 address of the default-route interface, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(_PROBE_ADDR)
            ip = sock.getsockname()[0]
    except OSError as exc:
        log.debug("LAN IP detection failed: %s", exc)
        return None
    if isinstance(ip, str) and is_rfc1918(ip):
        return ip
    log.debug("default-route address %r is not a private LAN address", ip)
    return None


def is_loopback_host(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def lan_url(cfg: Config) -> str | None:
    """``http://<lan-ip>:<port>`` for the phone on home Wi-Fi, or None if unavailable."""
    if is_loopback_host(cfg.server.host):
        return None  # the hub only listens on loopback: no LAN access
    host = (cfg.server.advertise_host or "").strip() or lan_ipv4()
    if not host:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    return f"http://{host}:{cfg.server.port}"


def parse_netsh_ssid(output: str) -> str | None:
    """The first connected Wi-Fi name in ``netsh wlan show interfaces`` output, or None."""
    for match in _NETSH_SSID_RE.finditer(output):
        if match.group(1):
            return match.group(1)
    return None


def wifi_ssid() -> str | None:
    """The Wi-Fi name this PC is connected to (Windows only), or None.

    None on Ethernet, when Wi-Fi is off, off Windows, or when Windows withholds the name
    (recent Windows 11 builds require Location access for Wi-Fi names).
    """
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            timeout=NETSH_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("netsh failed: %s", exc)
        return None
    # netsh writes in the console (OEM) code page when piped.
    try:
        output = proc.stdout.decode("oem", errors="replace")
    except LookupError:
        output = proc.stdout.decode("utf-8", errors="replace")
    return parse_netsh_ssid(output)
