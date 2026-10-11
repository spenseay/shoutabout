"""The pairing QR code for the ShoutAbout iPhone app (``python -m shoutabout pair``).

The QR code holds one compact JSON object, the *pairing payload*::

    {"shoutabout": 1,
     "lan_url": "http://<lan-ip>:<port>",
     "public_url": "https://<host>" | null,
     "token": "<hub token>",
     "cf_client_id": "...", "cf_client_secret": "...",   # only when given
     "ssid": "<home Wi-Fi name>" | null}

The app reads exactly these keys. The payload holds secrets (the hub token, and the Cloudflare
service token when given), so it's only shown on the PC's own screen: the QR code in the
terminal or as a temporary image, and ``--json`` prints it redacted unless ``--show-secrets``.
Nothing here writes the payload to disk or sends it anywhere.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PAYLOAD_VERSION = 1
CF_ID_ENV = "SHOUTABOUT_CF_CLIENT_ID"
CF_SECRET_ENV = "SHOUTABOUT_CF_CLIENT_SECRET"
SECRET_KEYS = ("token", "cf_client_id", "cf_client_secret")
REDACTED = "<hidden>"

# "CF-Access-Client-Id: abc.access", also inside a curl line: -H "CF-Access-Client-Id: ..."
_CF_LINE_RE = re.compile(r"CF-Access-Client-(Id|Secret)\s*[:=]\s*[\"']?([^\s\"']+)", re.IGNORECASE)
_HUB_ENDPOINTS = ("/transcribe", "/vocab")


class PairingError(ValueError):
    """The pairing payload can't be built from what was given."""


@dataclass(frozen=True)
class CloudflareCredentials:
    """A Cloudflare Access service token (the secret is never shown in repr)."""

    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return f"CloudflareCredentials(client_id={REDACTED!r}, client_secret={REDACTED!r})"


def cloudflare_credentials(
    client_id: str | None, client_secret: str | None, source: str
) -> CloudflareCredentials | None:
    """Both values (stripped), neither (None), or a :class:`PairingError` naming ``source``."""
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        missing = "Client ID" if not client_id else "Client Secret"
        raise PairingError(f"{source} has the Cloudflare service token without its {missing}")
    if any(ch.isspace() for ch in client_id + client_secret):
        raise PairingError(f"{source}: the Cloudflare values can't contain spaces or line breaks")
    return CloudflareCredentials(client_id, client_secret)


def parse_cf_text(text: str, source: str = "the Cloudflare file") -> CloudflareCredentials:
    """Read ``CF-Access-Client-Id: ...`` / ``CF-Access-Client-Secret: ...`` lines.

    That's how the Cloudflare dashboard shows a new service token; ``-H "..."`` curl lines
    work too. Raises :class:`PairingError` unless both values are there exactly once.
    """
    found: dict[str, list[str]] = {"id": [], "secret": []}
    for match in _CF_LINE_RE.finditer(text):
        found[match.group(1).lower()].append(match.group(2))
    for key, label in (("id", "CF-Access-Client-Id"), ("secret", "CF-Access-Client-Secret")):
        if not found[key]:
            raise PairingError(f"{source} has no '{label}: ...' line")
        if len(set(found[key])) > 1:
            raise PairingError(f"{source} has more than one different '{label}' value")
    credentials = cloudflare_credentials(found["id"][0], found["secret"][0], source)
    if credentials is None:  # unreachable: both values were found above
        raise PairingError(f"{source} has no Cloudflare service token")
    return credentials


def cf_from_env(environ: Mapping[str, str] | None = None) -> CloudflareCredentials | None:
    """``SHOUTABOUT_CF_CLIENT_ID`` / ``SHOUTABOUT_CF_CLIENT_SECRET``, if both are set."""
    env = os.environ if environ is None else environ
    return cloudflare_credentials(
        env.get(CF_ID_ENV), env.get(CF_SECRET_ENV), f"{CF_ID_ENV}/{CF_SECRET_ENV}"
    )


def base_url(url: str, *, https_only: bool = False) -> str:
    """The hub's base URL: ``scheme://host[:port]`` plus any path prefix.

    A trailing ``/transcribe`` or ``/vocab`` is dropped, so the URLs printed by ``info`` and
    ``make-shortcuts`` work too. A bare host name (no scheme) is taken as ``https://`` when
    ``https_only``.
    """
    url = url.strip()
    if https_only and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme.lower() not in allowed or not parts.netloc:
        scheme = "https://" if https_only else "http:// or https://"
        raise PairingError(f"not a valid URL (expected {scheme}host[:port]): {url!r}")
    if "?" in url or "#" in url:
        raise PairingError(f"the URL must not have a query string or fragment: {url!r}")
    path = parts.path.rstrip("/")
    for endpoint in _HUB_ENDPOINTS:
        if path.endswith(endpoint):
            path = path[: -len(endpoint)]
            break
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))


def build_payload(
    *,
    lan_url: str,
    token: str,
    public_url: str | None = None,
    cloudflare: CloudflareCredentials | None = None,
    ssid: str | None = None,
) -> dict[str, Any]:
    """The pairing payload (keys in the documented order; CF keys only when given)."""
    token = token.strip()
    if not token:
        raise PairingError("the hub token is empty")
    payload: dict[str, Any] = {
        "shoutabout": PAYLOAD_VERSION,
        "lan_url": base_url(lan_url),
        "public_url": base_url(public_url, https_only=True) if public_url else None,
        "token": token,
    }
    if cloudflare is not None:
        payload["cf_client_id"] = cloudflare.client_id
        payload["cf_client_secret"] = cloudflare.client_secret
    payload["ssid"] = ssid if ssid and ssid.strip() else None
    return payload


def redacted(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A copy with the secret values replaced by ``<hidden>``."""
    return {k: (REDACTED if k in SECRET_KEYS and v else v) for k, v in payload.items()}


def to_json(payload: Mapping[str, Any]) -> str:
    """Compact JSON (smallest QR code); non-ASCII Wi-Fi names stay readable."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
