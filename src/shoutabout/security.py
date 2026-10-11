"""Source-address routing, bearer auth, Cloudflare Access JWT validation, rate limiting.

Routing is decided from the TCP peer address only (``scope["client"]``); forwarding
headers such as X-Forwarded-For or Cf-Connecting-IP are never trusted:

* loopback (127.0.0.0/8, ::1, ::ffff:127.x) -> ``"tunnel"`` (cloudflared runs on this PC)
* inside ``server.lan_networks`` (private ranges only) -> ``"lan"``
* anything else -> ``None`` (the server answers 403 on every path)

Secrets (hub token, JWTs) are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any, Protocol

import jwt

from shoutabout.config import PRIVATE_PARENT_NETWORKS, AccessConfig
from shoutabout.models import Route

log = logging.getLogger(__name__)

ACCESS_JWT_HEADER = "Cf-Access-Jwt-Assertion"
JWT_ALGORITHMS = ("RS256",)
JWKS_LIFESPAN_S = 3600.0  # re-fetch the JWK set hourly (and on an unknown kid)
JWKS_TIMEOUT_S = 5.0

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


# ------------------------------------------------------------------ source routing


def parse_ip(host: object) -> IPAddress | None:
    """Parse a peer host into an IP address; IPv4-mapped IPv6 is unwrapped. None if invalid."""
    if not isinstance(host, str):
        return None
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return None
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_private_network(net: IPNetwork) -> bool:
    return any(
        net.version == parent.version and net.subnet_of(parent)  # type: ignore[arg-type]
        for parent in PRIVATE_PARENT_NETWORKS
    )


@lru_cache(maxsize=32)
def _lan_networks(lan_networks: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    nets: list[IPNetwork] = []
    for item in lan_networks:
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            log.warning("ignoring invalid lan network %r", item)
            continue
        # Defence in depth: config validation already rejects public ranges.
        if not _is_private_network(net):
            log.warning("ignoring non-private lan network %r", item)
            continue
        nets.append(net)
    return tuple(nets)


def classify_source(host: str | None, lan_networks: Sequence[str]) -> Route | None:
    """Map the TCP peer address to a route, or None to reject the request."""
    ip = parse_ip(host)
    if ip is None:
        return None
    if ip.is_loopback:
        return "tunnel"
    for net in _lan_networks(tuple(lan_networks)):
        if ip.version == net.version and ip in net:
            return "lan"
    return None


# ------------------------------------------------------------------------- bearer


def bearer_ok(authorization: str | None, token: str) -> bool:
    """True iff ``authorization`` is ``Bearer <token>`` (scheme case-insensitive)."""
    if not authorization or not token:
        return False
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    presented = parts[1].strip()
    if not presented:
        return False
    try:
        return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
    except UnicodeEncodeError:
        return False


MAX_BEARER_CHARS = 512  # real tokens are 43 characters


def parse_bearer(authorization: str | None) -> str | None:
    """The token in ``Authorization: Bearer <token>``, or None (scheme case-insensitive)."""
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    presented = parts[1].strip()
    if not presented or len(presented) > MAX_BEARER_CHARS:
        return None
    return presented


def sha256_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", errors="surrogatepass")).digest()


class DeviceMatcher(Protocol):
    def match_digest(self, digest: bytes) -> Any: ...


HUB_TOKEN = "hub"  # authenticate() result for the legacy hub token


def authenticate(
    authorization: str | None, hub_token_digest: bytes | None, devices: DeviceMatcher | None
) -> Any:
    """Who sent the request: :data:`HUB_TOKEN`, a paired device record, or None.

    The presented token is hashed once and compared, in constant time, with the hub token's
    hash *and* every device hash, always all of them (no early exit), so timing reveals
    neither which credential matched nor how long the real token is.
    """
    presented = parse_bearer(authorization)
    digest = sha256_digest(presented) if presented is not None else sha256_digest("\0")
    hub_ok = hub_token_digest is not None and hmac.compare_digest(digest, hub_token_digest)
    device = devices.match_digest(digest) if devices is not None else None
    if presented is None:
        return None
    if hub_ok:
        return HUB_TOKEN
    return device


# ---------------------------------------------------------------- Access JWT


class AccessJwtError(Exception):
    """The Cloudflare Access JWT is missing or invalid. The message is short and non-secret."""


class _SigningKey(Protocol):
    key: Any


class JwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> _SigningKey: ...


class AccessJwtValidator:
    """Validates ``Cf-Access-Jwt-Assertion`` against the team's Access signing keys."""

    def __init__(
        self, cfg: AccessConfig, jwks_client: JwksClient | None = None, leeway: int = 60
    ) -> None:
        if not cfg.enabled:
            raise ValueError("Access JWT validation needs access.team_domain and access.aud")
        self.issuer = f"https://{cfg.team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.audience = list(cfg.aud)
        self.allowed_client_ids = frozenset(cfg.allowed_client_ids)
        self.leeway = leeway
        self._jwks: JwksClient = jwks_client or jwt.PyJWKClient(
            self.certs_url,
            cache_keys=True,
            lifespan=JWKS_LIFESPAN_S,
            timeout=JWKS_TIMEOUT_S,
        )

    def validate(self, token: str) -> dict:
        """Return the verified claims or raise AccessJwtError. May block on a JWKS fetch."""
        if not token or not token.strip():
            raise AccessJwtError("missing Access JWT")
        token = token.strip()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AccessJwtError("malformed Access JWT") from exc
        if header.get("alg") not in JWT_ALGORITHMS:
            raise AccessJwtError("unexpected Access JWT algorithm")
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError as exc:
            raise AccessJwtError("couldn't fetch Cloudflare Access signing keys") from exc
        except jwt.PyJWTError as exc:
            raise AccessJwtError("Access JWT signing key not found") from exc
        except Exception as exc:  # network / unexpected JWKS failures
            raise AccessJwtError("couldn't fetch Cloudflare Access signing keys") from exc
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(JWT_ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AccessJwtError("Access JWT expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AccessJwtError("Access JWT audience mismatch") from exc
        except jwt.InvalidIssuerError as exc:
            raise AccessJwtError("Access JWT issuer mismatch") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AccessJwtError(f"Access JWT missing claim {exc.claim!r}") from exc
        except jwt.InvalidSignatureError as exc:
            raise AccessJwtError("Access JWT signature invalid") from exc
        except jwt.ImmatureSignatureError as exc:
            raise AccessJwtError("Access JWT not yet valid") from exc
        except jwt.PyJWTError as exc:
            raise AccessJwtError("invalid Access JWT") from exc
        if self.allowed_client_ids:
            client_id = claims.get("common_name")
            if not isinstance(client_id, str) or client_id not in self.allowed_client_ids:
                raise AccessJwtError("Access JWT not issued to an allowed service token")
        return claims


# ------------------------------------------------------------------- rate limit


class RateLimiter:
    """Sliding-window counter per key. Thread-safe."""

    _SWEEP_EVERY = 1024

    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def _prune(self, key: str, now: float) -> deque[float] | None:
        hits = self._hits.get(key)
        if hits is None:
            return None
        cutoff = now - self.window_s
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def _sweep(self, now: float) -> None:
        for key in list(self._hits):
            self._prune(key, now)

    def allow(self, key: str) -> bool:
        """Record a hit for ``key``; False (and not recorded) if the limit is reached."""
        with self._lock:
            now = self._clock()
            self._calls += 1
            if self._calls % self._SWEEP_EVERY == 0:
                self._sweep(now)
            hits = self._prune(key, now)
            if hits is not None and len(hits) >= self.limit:
                return False
            self._hits.setdefault(key, deque()).append(now)
            return True

    def count(self, key: str) -> int:
        """Hits for ``key`` inside the current window."""
        with self._lock:
            hits = self._prune(key, self._clock())
            return len(hits) if hits is not None else 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
