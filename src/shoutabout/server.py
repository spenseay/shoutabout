"""HTTP API (FastAPI): ``GET /health``, ``POST /transcribe``, ``GET``/``POST /vocab``, and the
v2 phone protocol (docs/protocol.md): ``GET /hello``, ``POST /pair/request``,
``GET /pair/status`` (unauthenticated, home-network route only), ``GET /info`` and
``POST /paste`` (authenticated).

Order of checks for /transcribe (PLAN §5): source IP (middleware, every path) -> rate
limit per (route, ip) -> auth-failure lockout -> bearer -> Access JWT (tunnel route, if
configured) -> readiness -> size limits -> multipart parse -> pipeline in a worker thread.
/vocab goes through exactly the same auth steps (one shared helper, shared rate-limit and
lockout buckets), then reads or edits vocab.txt; it never pastes or types anything.
Authentication happens before the body is read. Secrets and term/transcript text are
never logged. A bearer token is either the legacy hub token or a paired phone's own token
(:mod:`shoutabout.devices`), checked together in constant time.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shoutabout import __version__
from shoutabout.config import Config
from shoutabout.models import TARGETS, CleanupResult, PasteResult, Timings, TranscribeResult
from shoutabout.pipeline import PASTE_ERROR_WARNING, PAUSED_WARNING, Pipeline, format_log_line
from shoutabout.security import (
    ACCESS_JWT_HEADER,
    HUB_TOKEN,
    AccessJwtError,
    AccessJwtValidator,
    RateLimiter,
    authenticate,
    classify_source,
    parse_ip,
    sha256_digest,
)
from shoutabout.state import HubState
from shoutabout.text import normalize_whitespace
from shoutabout.vocab import VocabStore, VocabTermError, add_term

log = logging.getLogger(__name__)

AUTH_FAILURE_WINDOW_S = 600.0
RATE_WINDOW_S = 60.0
# Multipart framing (boundaries, part headers, the `target` field) on top of the file.
MULTIPART_SLACK_BYTES = 64 * 1024
DEFAULT_SUFFIX = ".m4a"
_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,8}$")
VOCAB_MAX_BODY_BYTES = 4096
VOCAB_BODY_HINT = (
    'send the word as JSON {"term": "..."}, as plain text, as a form field "term", or as ?term='
)
PROTOCOL_VERSION = 1
# /hello and /pair/status (the phone polls once a second while it waits for approval).
PUBLIC_RATE_PER_MINUTE = 120
PAIR_MAX_BODY_BYTES = 2048
PASTE_MAX_CHARS = 10_000
# 10,000 characters, each possibly a 12-byte JSON escape (a surrogate pair), plus framing.
PASTE_MAX_BODY_BYTES = 160 * 1024
TIERS = ("best", "balanced", "light")
TIER_SPEED = {"best": "fast", "balanced": "ok", "light": "slow"}


@dataclass
class HubContext:
    config: Config
    token: str
    state: HubState
    pipeline: Pipeline | None = None  # None until the engine is loaded
    access: AccessJwtValidator | None = None  # None = JWT check disabled
    startup_error: str | None = None
    vocab_path: Path | None = None  # vocab.txt for /vocab; None = /vocab answers 503
    # --- v2 phone protocol (docs/protocol.md) ---
    devices: Any = None  # devices.DeviceStore: per-phone tokens (None = hub token only)
    pairing: Any = None  # pair_requests.PairingManager (None = pairing endpoints 404)
    identity: Any = None  # identity.IdentityStore (None = /hello answers 503)
    version: str = __version__
    # v2-voice: returns modelpick.resolve(cfg) (a ModelChoice) for GET /info; None = derive
    # a best guess from [stt] in the config.
    model_info: Callable[[], Any] | None = None
    injector: Any = None  # inject.Injector for POST /paste (independent of the engine)
    # recent.Recent: the last few dictations (/paste text too) and uploads the speech engine
    # couldn't write out (Recent > Try again), in memory only. None = not kept.
    recent: Any = None
    lan_url: Callable[[], str | None] | None = None  # fallback LAN URL for pairing


# --------------------------------------------------------------------- middleware


class SourceGuardMiddleware:
    """Pure ASGI: classify the TCP peer of every request; 403 if it isn't allowed."""

    def __init__(self, app: ASGIApp, lan_networks: tuple[str, ...]) -> None:
        self.app = app
        self.lan_networks = tuple(lan_networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        host = client[0] if client else None
        route = classify_source(host, self.lan_networks)
        if route is None:
            log.warning(
                "rejected route=- ip=%s reason=source-not-allowed path=%.100s",
                host,
                scope.get("path", ""),
            )
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = JSONResponse({"detail": "forbidden"}, status_code=403)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["route"] = route
        await self.app(scope, receive, send)


class _UploadTooLarge(Exception):
    pass


def _limited_receive(receive: Receive, max_bytes: int) -> Callable[[], Awaitable[Message]]:
    """Wrap ``receive`` so a body over ``max_bytes`` aborts even without Content-Length."""
    seen = 0

    async def wrapped() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > max_bytes:
                raise _UploadTooLarge
        return message

    return wrapped


# ------------------------------------------------------------------------ helpers


def _error(status: int, detail: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=dict(headers or {}))


def _reject(
    status: int,
    detail: str,
    *,
    route: str,
    ip: str,
    reason: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    log.warning("rejected route=%s ip=%s status=%d reason=%s", route, ip, status, reason)
    return _error(status, detail, headers)


def _safe_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if _SUFFIX_RE.match(suffix) else DEFAULT_SUFFIX


def _save_upload(upload: UploadFile, suffix: str, max_bytes: int) -> tuple[Path, int]:
    """Copy the spooled upload to a named temp file. Returns (path, size)."""
    fd, name = tempfile.mkstemp(prefix="shoutabout-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            upload.file.seek(0)
            shutil.copyfileobj(upload.file, out, 1024 * 1024)
            size = out.tell()
    except BaseException:
        _unlink(path)
        raise
    if size > max_bytes:
        _unlink(path)
        raise _UploadTooLarge
    return path, size


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("couldn't delete temp file %s", path)


def _map_pipeline_error(exc: BaseException) -> tuple[int, str]:
    # Duck-typed by class name so the server doesn't import the audio decoders eagerly.
    names = {cls.__name__ for cls in type(exc).__mro__}
    message = str(exc) or type(exc).__name__
    if "AudioTooLongError" in names:
        return 413, message
    if "DecoderUnavailableError" in names:
        # The PC can't decode audio at all (Media Foundation missing on Windows "N"
        # editions): not the upload's fault, and the message says how to fix it.
        log.error("audio decoder unavailable: %s", message)
        return 503, message
    if "AudioError" in names:
        # Decoder messages can include local temp paths; keep details in the log only.
        log.warning("audio rejected: %s", message)
        return 415, "unsupported or unreadable audio"
    if "EngineError" in names:
        return 503, message
    return 500, "internal error"


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):g} MB"


# ---------------------------------------------------------------------------- app


FORM_CONTENT_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")
_RAW_SUFFIXES = {
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/x-caf": ".caf",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
}


def _normalize_target(raw: str | None) -> str | None:
    """pc|phone (case/whitespace-insensitive). Missing/empty means the default, PC mode (e.g.
    the Shortcut's target file doesn't exist yet). None = invalid value."""
    target = (raw or "").strip().lower()
    if not target:
        return "pc"
    return target if target in TARGETS else None


def _is_raw_audio_type(content_type: str) -> bool:
    return (
        not content_type
        or content_type.startswith(("audio/", "video/"))
        or content_type == "application/octet-stream"
    )


def _suffix_for(content_type: str) -> str:
    # The decoders sniff the actual container from its bytes, so the suffix is only a hint.
    return _RAW_SUFFIXES.get(content_type, DEFAULT_SUFFIX)


async def _save_raw_body(request: Request, suffix: str, max_bytes: int) -> tuple[Path, int]:
    """Stream a raw request body (the audio itself) to a temp file. Returns (path, size)."""
    fd, name = tempfile.mkstemp(prefix="shoutabout-", suffix=suffix)
    path = Path(name)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise _UploadTooLarge
                out.write(chunk)
    except BaseException:
        _unlink(path)
        raise
    return path, size


def _describe_form(form: Any) -> str:
    parts = []
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            parts.append(
                f"{key!r}=file(name={value.filename!r}, type={value.content_type!r}, "
                f"size={value.size})"
            )
        else:
            parts.append(f"{key!r}=text(len={len(value)})")
    return ", ".join(parts) or "(none)"


def _remote_client_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


class _BadVocabBody(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _decode_text(data: bytes, content_type_header: str) -> str:
    charset = "utf-8"
    for param in content_type_header.split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip('"').lower()
    if charset in ("utf-8", "utf8"):
        charset = "utf-8-sig"  # tolerate a BOM
    try:
        return data.decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise _BadVocabBody(400, "send the term as UTF-8 text") from exc


async def _vocab_term_from_body(request: Request) -> str | None:
    """The term in a POST /vocab body (JSON, form, or raw text); None if the body has none."""
    header = request.headers.get("content-type", "")
    content_type = header.split(";")[0].strip().lower()
    if content_type in FORM_CONTENT_TYPES:
        try:
            form = await request.form(max_files=1, max_fields=8, max_part_size=VOCAB_MAX_BODY_BYTES)
        except StarletteHTTPException as exc:
            raise _BadVocabBody(400, str(exc.detail)) from exc
        try:
            value = form.get("term")
            if isinstance(value, UploadFile):  # a Shortcuts form "File" field holding text
                data = await value.read(VOCAB_MAX_BODY_BYTES + 1)
                return _decode_text(data, value.content_type or "")
            return value if isinstance(value, str) else None
        finally:
            await form.close()
    body = await request.body()
    if content_type == "application/json" or content_type.endswith("+json"):
        if not body.strip():
            return None
        try:
            data = json.loads(_decode_text(body, header))
        except ValueError as exc:
            raise _BadVocabBody(400, "invalid JSON") from exc
        if isinstance(data, dict):
            data = data.get("term")
        if data is None:
            return None
        if not isinstance(data, str):
            raise _BadVocabBody(400, '"term" must be a string')
        return data
    if (
        not content_type
        or content_type.startswith("text/")
        or content_type in ("application/octet-stream",)
    ):
        return _decode_text(body, header) if body else None
    raise _BadVocabBody(415, VOCAB_BODY_HINT)


def _vocab_message(term: str, status: str, count: int) -> str:
    if status == "added":
        return f'Added "{term}" to your vocab ({count} words).'
    return f'"{term}" is already in your vocab.'


async def _read_json_body(
    request: Request, max_bytes: int, *, route: str, ip: str
) -> Any | JSONResponse:
    """The request body parsed as JSON, or the error response (413 too big, 422 not JSON).

    The content type isn't trusted either way: a body that isn't UTF-8 JSON is a 422."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            return _reject(400, "invalid Content-Length", route=route, ip=ip, reason="bad-length")
        if declared > max_bytes:
            return _reject(413, "request body too large", route=route, ip=ip, reason="too-large")
    limited = Request(request.scope, _limited_receive(request.receive, max_bytes))
    try:
        raw = await limited.body()
    except _UploadTooLarge:
        return _reject(413, "request body too large", route=route, ip=ip, reason="too-large")
    except ClientDisconnect:
        return _reject(400, "client disconnected", route=route, ip=ip, reason="disconnect")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _reject(422, "send a JSON body", route=route, ip=ip, reason="bad-json")


def describe_stt(choice: Any, cfg: Config) -> tuple[dict[str, str], bool]:
    """``(stt, cleanup)`` for GET /info from a ModelChoice (or a guess from the config).

    speed: "fast" for the best tier, "ok" balanced, "slow" light/CPU. The phone transcribes
    on-device and sends only text (POST /paste) when it reads "slow".
    """
    tier = getattr(choice, "tier", None)
    device = getattr(choice, "device", None)
    speed = getattr(choice, "speed", None)
    if device not in ("cuda", "cpu"):
        device = cfg.stt.device if cfg.stt.device in ("cuda", "cpu") else None
    if tier not in TIERS:
        # "auto" without a known choice: don't call the PC slow (that sends phones on-device).
        tier = {"cuda": "best", "cpu": "light"}.get(device or "", "balanced")
    device = device or "cpu"
    if speed not in ("fast", "ok", "slow"):
        speed = TIER_SPEED[tier]
    cleanup = getattr(choice, "cleanup", None)
    if not isinstance(cleanup, bool):
        cleanup = cfg.cleanup.enabled is True
    return {"tier": tier, "device": device, "speed": speed}, cleanup


def _lan_url_for(request: Request, ctx: HubContext) -> str | None:
    """``http://<ip>:<port>`` the phone used to reach us (a private address: the request came
    over the LAN), else the hub's own LAN URL."""
    server = request.scope.get("server")
    if server and len(server) >= 2 and server[1]:
        ip = parse_ip(server[0])
        if ip is not None and ip.is_private and not ip.is_loopback and not ip.is_unspecified:
            host = f"[{ip}]" if ip.version == 6 else str(ip)
            return f"http://{host}:{int(server[1])}"
    if ctx.lan_url is not None:
        try:
            return ctx.lan_url()
        except Exception:
            log.exception("couldn't work out the LAN URL")
    return None


def _source_of(request: Request) -> str | None:
    """The paired phone's name (Recent says where a dictation came from), or None."""
    name = getattr(getattr(request.state, "auth", None), "name", None)
    return str(name) if name else None


def _keep_failed(
    ctx: HubContext,
    exc: BaseException,
    status: int,
    detail: str,
    path: Path | None,
    route: str,
    source: str | None,
) -> None:
    """An upload the speech engine couldn't transcribe is kept (in memory, decoded) for
    Recent > Try again. Not for bad or too-long audio, or a PC that can't decode audio:
    trying again wouldn't help."""
    if ctx.recent is None or ctx.pipeline is None or path is None:
        return
    names = {cls.__name__ for cls in type(exc).__mro__}
    if status != 500 and "EngineError" not in names:
        return
    try:
        audio = ctx.pipeline.decode(path)  # it decoded a moment ago: the engine failed
        ctx.recent.keep_failed(audio, route=route, source=source, error=detail)
    except Exception:
        log.exception("couldn't keep the failed upload")


def _paste_text(
    ctx: HubContext, text: str, route: str, source: str | None = None
) -> TranscribeResult:
    """Paste phone-made text like a transcription result: same injector, same paste-chord
    rules, same trailing-space setting, no cleanup. Kept in Recent like one."""
    t0 = time.perf_counter()
    payload = text + (" " if ctx.config.paste.trailing_space else "")
    # Take turns with phone audio and "Talk to this PC" (the pipeline holds the same lock).
    busy = getattr(ctx.state, "busy", None) or contextlib.nullcontext()
    try:
        with busy:
            paste = ctx.injector.paste(payload)
    except Exception:
        log.exception("paste failed")
        paste = PasteResult(pasted=False, warning=PASTE_ERROR_WARNING)
    elapsed = (time.perf_counter() - t0) * 1000.0
    result = TranscribeResult(
        text=text,
        raw_text=text,
        target="pc",
        route=route,
        pasted=bool(paste.pasted),
        warning=paste.warning,
        cleanup=CleanupResult(text=text, status="disabled", reason="text from the phone"),
        timings=Timings(paste=elapsed, total=elapsed),
        request_id=uuid.uuid4().hex[:12],
        extra={"stt_model": "phone", "llm_model": None, "source": source},
    )
    if ctx.recent is not None:
        ctx.recent.add(result)
    return result


def create_app(ctx: HubContext) -> FastAPI:
    app = FastAPI(
        title="ShoutAbout hub",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ctx = ctx
    server_cfg = ctx.config.server
    rate = RateLimiter(server_cfg.rate_limit_per_minute, RATE_WINDOW_S)
    failures = RateLimiter(server_cfg.max_auth_failures, AUTH_FAILURE_WINDOW_S)
    public_rate = RateLimiter(PUBLIC_RATE_PER_MINUTE, RATE_WINDOW_S)
    vocab_stores: dict[Path, VocabStore] = {}
    digest_cache: dict[str, bytes] = {}

    def hub_digest() -> bytes | None:
        """SHA-256 of the hub token (cached; None if there is no hub token)."""
        token = ctx.token
        if not token:
            return None
        digest = digest_cache.get(token)
        if digest is None:
            digest_cache.clear()
            digest = digest_cache[token] = sha256_digest(token)
        return digest

    def vocab_store() -> VocabStore | None:
        path = ctx.vocab_path
        if path is None:
            return None
        store = vocab_stores.get(Path(path))
        if store is None:
            store = vocab_stores[Path(path)] = VocabStore(Path(path))
        return store

    async def authorize(request: Request) -> tuple[str, str, JSONResponse | None]:
        """Steps 1-4, shared by every authenticated endpoint: ``(route, ip, rejection)``.

        One rate-limit bucket and one auth-failure bucket per (route, ip) across all
        endpoints, so /vocab can't be used to get extra token guesses.
        """
        route: str = getattr(request.state, "route", None) or "-"
        ip = request.client.host if request.client else "-"
        key = f"{route}|{ip}"
        if route == "tunnel":
            # Every tunnel request arrives from cloudflared on 127.0.0.1. Bucket rate limits
            # by the remote client (Cloudflare sets Cf-Connecting-IP) so one remote client
            # can't lock out the owner. Only used for bucketing, never for routing or auth.
            remote = _remote_client_ip(request.headers.get("cf-connecting-ip"))
            if remote:
                key = f"{key}|{remote}"
        cfg = ctx.config.server

        def deny(
            status: int, detail: str, reason: str, headers: Mapping[str, str] | None = None
        ) -> tuple[str, str, JSONResponse]:
            return (
                route,
                ip,
                _reject(status, detail, route=route, ip=ip, reason=reason, headers=headers),
            )

        # 1. rate limit per (route, ip)
        if not rate.allow(key):
            return deny(
                429,
                "rate limited; try again shortly",
                "rate-limit",
                {"Retry-After": str(int(RATE_WINDOW_S))},
            )

        # 2. auth-failure lockout
        if failures.count(key) >= cfg.max_auth_failures:
            return deny(
                429,
                "too many failed authentication attempts; try again later",
                "auth-lockout",
                {"Retry-After": str(int(AUTH_FAILURE_WINDOW_S))},
            )

        # 3. bearer token: the hub token or a paired phone's token (constant time)
        who = authenticate(request.headers.get("authorization"), hub_digest(), ctx.devices)
        if who is None:
            failures.allow(key)
            return deny(
                401, "missing or invalid bearer token", "bad-bearer", {"WWW-Authenticate": "Bearer"}
            )
        request.state.auth = who

        # 4. Cloudflare Access JWT on the tunnel route
        if route == "tunnel" and ctx.access is not None:
            assertion = request.headers.get(ACCESS_JWT_HEADER)
            if not assertion:
                failures.allow(key)
                return deny(403, "missing Cloudflare Access JWT", "jwt-missing")
            try:
                await run_in_threadpool(ctx.access.validate, assertion)
            except AccessJwtError as exc:
                failures.allow(key)
                return deny(403, f"Cloudflare Access JWT rejected: {exc}", f"jwt-invalid ({exc})")
            except Exception:
                log.exception("Access JWT validation crashed")
                failures.allow(key)
                return deny(403, "Cloudflare Access JWT rejected", "jwt-error")
        if who is not HUB_TOKEN and ctx.devices is not None:
            try:
                await run_in_threadpool(ctx.devices.touch, who.key)
            except Exception:
                log.exception("couldn't note the phone's last-used time")
        return route, ip, None

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/vocab")
    async def vocab_list(request: Request) -> JSONResponse:
        route, ip, rejection = await authorize(request)
        if rejection is not None:
            return rejection
        store = vocab_store()
        if store is None:
            return _reject(503, "vocab is not available", route=route, ip=ip, reason="no-vocab")
        vocab = await run_in_threadpool(store.get)
        log.info("vocab list route=%s count=%d", route, len(vocab.terms))
        return JSONResponse(
            {
                "terms": list(vocab.terms),
                # "say -> write" lines; the iPhone applies them to offline dictations too.
                "replacements": [{"say": say, "write": write} for say, write in vocab.replacements],
                "version": vocab.version,
                "count": len(vocab.terms),
            }
        )

    @app.post("/vocab")
    async def vocab_add(request: Request) -> JSONResponse:
        route, ip, rejection = await authorize(request)
        if rejection is not None:
            return rejection
        path = ctx.vocab_path
        if path is None:
            return _reject(503, "vocab is not available", route=route, ip=ip, reason="no-vocab")

        def invalid(reason: str, status: int = 400) -> JSONResponse:
            log.warning("rejected route=%s ip=%s status=%d reason=invalid-term", route, ip, status)
            return JSONResponse(
                {"detail": f"invalid term: {reason}", "status": "invalid", "reason": reason},
                status_code=status,
            )

        too_large = f"term too large (max {VOCAB_MAX_BODY_BYTES} bytes)"
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return _reject(
                    400, "invalid Content-Length", route=route, ip=ip, reason="bad-length"
                )
            if declared > VOCAB_MAX_BODY_BYTES:
                return _reject(413, too_large, route=route, ip=ip, reason="too-large")
        limited = Request(request.scope, _limited_receive(request.receive, VOCAB_MAX_BODY_BYTES))
        try:
            raw = await _vocab_term_from_body(limited)
        except _UploadTooLarge:
            return _reject(413, too_large, route=route, ip=ip, reason="too-large")
        except ClientDisconnect:
            return _reject(400, "client disconnected", route=route, ip=ip, reason="disconnect")
        except _BadVocabBody as exc:
            return invalid(exc.reason, exc.status)
        if raw is None or not raw.strip():
            raw = request.query_params.get("term") or raw
        if raw is None or not raw.strip():
            return invalid(f"no term given ({VOCAB_BODY_HINT})")

        try:
            result = await run_in_threadpool(add_term, Path(path), raw)
        except VocabTermError as exc:
            return invalid(str(exc))
        except OSError:
            log.exception("couldn't update %s route=%s", Path(path).name, route)
            return _error(500, "couldn't update vocab.txt")
        log.info(
            "vocab add route=%s added=%s status=%s chars=%d count=%d",
            route,
            "true" if result.added else "false",
            result.status,
            len(result.term),
            result.count,
        )
        return JSONResponse(
            {
                "added": result.added,
                "status": result.status,
                "term": result.term,
                "count": result.count,
                "reason": result.reason,
                "message": _vocab_message(result.term, result.status, result.count),
            }
        )

    # ------------------------------------------------- v2 phone protocol (no auth)

    def lan_gate(request: Request, what: str) -> tuple[str, JSONResponse | None]:
        """Pairing and discovery answer only on the home-network route: never through the
        tunnel or loopback (they look like unknown paths there), and rate limited."""
        route: str = getattr(request.state, "route", None) or "-"
        ip = request.client.host if request.client else "-"
        if route != "lan":
            log.warning("rejected route=%s ip=%s status=404 reason=%s-not-lan", route, ip, what)
            return ip, _error(404, "Not Found")
        if not public_rate.allow(f"lan|{ip}"):
            return ip, _reject(
                429,
                "rate limited; try again shortly",
                route=route,
                ip=ip,
                reason=f"{what}-rate",
                headers={"Retry-After": str(int(RATE_WINDOW_S))},
            )
        return ip, None

    def identity_now() -> Any:
        return ctx.identity.get() if ctx.identity is not None else None

    @app.get("/hello")
    async def hello(request: Request) -> JSONResponse:
        _ip, rejection = lan_gate(request, "hello")
        if rejection is not None:
            return rejection
        ident = await run_in_threadpool(identity_now)
        if ident is None:
            return _error(503, "hub identity is not available")
        pairing = ctx.pairing
        return JSONResponse(
            {
                "app": "shoutabout",
                "v": PROTOCOL_VERSION,
                "id": ident.id,
                "name": ident.name,
                "version": ctx.version,
                "pairing": bool(pairing is not None and getattr(pairing, "enabled", True)),
            }
        )

    @app.post("/pair/request")
    async def pair_request(request: Request) -> JSONResponse:
        from shoutabout.pair_requests import REQUEST_TTL_S, PairError, validate_request

        ip, rejection = lan_gate(request, "pair")
        if rejection is not None:
            return rejection
        route = "lan"
        if ctx.pairing is None:
            return _error(404, "Not Found")
        body = await _read_json_body(request, PAIR_MAX_BODY_BYTES, route=route, ip=ip)
        if isinstance(body, JSONResponse):
            return body
        try:
            name, device_id = validate_request(body)
            prompt = await run_in_threadpool(ctx.pairing.request, name, device_id, ip)
        except PairError as exc:
            headers = {"Retry-After": "60"} if exc.status == 429 else None
            return _reject(
                exc.status, exc.detail, route=route, ip=ip, reason=exc.reason, headers=headers
            )
        return JSONResponse(
            {"request_id": prompt.request_id, "code": prompt.code, "expires_in": REQUEST_TTL_S},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/pair/status")
    async def pair_status(request: Request) -> JSONResponse:
        ip, rejection = lan_gate(request, "pair-status")
        if rejection is not None:
            return rejection
        if ctx.pairing is None:
            return _error(404, "Not Found")
        request_id = request.query_params.get("request_id") or ""
        body = await run_in_threadpool(ctx.pairing.status, request_id)
        if body is None:
            return _reject(404, "unknown request_id", route="lan", ip=ip, reason="pair-unknown")
        if body.get("status") == "approved":
            ident = await run_in_threadpool(identity_now)
            body["hub"] = {
                "id": ident.id if ident else None,
                "name": ident.name if ident else None,
                "lan_url": _lan_url_for(request, ctx),
            }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    # ---------------------------------------------- v2 phone protocol (with auth)

    @app.get("/info")
    async def info(request: Request) -> JSONResponse:
        route, ip, rejection = await authorize(request)
        if rejection is not None:
            return rejection
        ident = await run_in_threadpool(identity_now)
        choice = None
        if ctx.model_info is not None:
            try:
                choice = await run_in_threadpool(ctx.model_info)
            except Exception as exc:  # the answer falls back to the [stt] config
                log.warning("couldn't work out the speech model for /info: %s", exc)
        stt, cleanup = describe_stt(choice, ctx.config)
        return JSONResponse(
            {
                "id": ident.id if ident else None,
                "name": ident.name if ident else None,
                "version": ctx.version,
                "stt": stt,
                "cleanup": cleanup,
                "accepts": ["audio", "text"],
            }
        )

    @app.post("/paste")
    async def paste(request: Request) -> JSONResponse:
        route, ip, rejection = await authorize(request)
        if rejection is not None:
            return rejection
        body = await _read_json_body(request, PASTE_MAX_BODY_BYTES, route=route, ip=ip)
        if isinstance(body, JSONResponse):
            return body
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            return _reject(422, 'send JSON {"text": "..."}', route=route, ip=ip, reason="bad-text")
        if len(text) > PASTE_MAX_CHARS:
            return _reject(
                413,
                f"text too long (max {PASTE_MAX_CHARS} characters)",
                route=route,
                ip=ip,
                reason="too-long",
            )
        text = normalize_whitespace(text)  # one paragraph, exactly like a transcription
        if not text:
            return _reject(422, "text is empty", route=route, ip=ip, reason="empty-text")
        if ctx.state.paused:
            log.info("paste route=%s chars=%d pasted=false reason=paused", route, len(text))
            return JSONResponse(
                {"detail": PAUSED_WARNING, "status": "paused", "pasted": False}, status_code=409
            )
        if ctx.injector is None:
            return _reject(503, "pasting isn't ready yet", route=route, ip=ip, reason="no-injector")
        result = await run_in_threadpool(_paste_text, ctx, text, route, _source_of(request))
        log.info(
            "paste id=%s route=%s chars=%d pasted=%s paste_ms=%d",
            result.request_id,
            route,
            len(text),
            str(result.pasted).lower(),
            round(result.timings.paste),
        )
        out: dict[str, Any] = {"status": "ok" if result.warning is None else "warning"}
        out["pasted"] = result.pasted
        if result.warning is not None:
            out["warning"] = result.warning
        return JSONResponse(out)

    @app.post("/transcribe")
    async def transcribe(request: Request) -> JSONResponse:
        route, ip, rejection = await authorize(request)
        if rejection is not None:
            return rejection
        cfg = ctx.config.server
        source = _source_of(request)
        extra = {"source": source} if source else None

        # 5. readiness
        pipeline = ctx.pipeline
        if pipeline is None:
            detail = ctx.startup_error or "hub is starting"
            return _reject(503, detail, route=route, ip=ip, reason="not-ready")

        # 6. size precheck
        max_bytes = cfg.max_upload_bytes
        body_limit = max_bytes + MULTIPART_SLACK_BYTES
        too_large = f"upload too large (max {_mb(max_bytes)})"
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return _reject(
                    400, "invalid Content-Length", route=route, ip=ip, reason="bad-length"
                )
            if declared > body_limit:
                return _reject(413, too_large, route=route, ip=ip, reason="too-large")

        limited = Request(request.scope, _limited_receive(request.receive, body_limit))
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in FORM_CONTENT_TYPES:
            # 7b. raw upload: the body IS the audio (iOS Shortcuts "Request Body: File");
            # target comes from the query string (?target=pc|phone).
            if not _is_raw_audio_type(content_type):
                return _reject(
                    415,
                    "send multipart/form-data, or the audio itself as the request body",
                    route=route,
                    ip=ip,
                    reason="bad-content-type",
                )
            target = _normalize_target(request.query_params.get("target"))
            if target is None:
                return _reject(
                    422, "target must be 'pc' or 'phone'", route=route, ip=ip, reason="bad-target"
                )
            tmp = None
            try:
                try:
                    tmp, size = await _save_raw_body(limited, _suffix_for(content_type), max_bytes)
                except _UploadTooLarge:
                    return _reject(413, too_large, route=route, ip=ip, reason="too-large")
                except ClientDisconnect:
                    return _reject(
                        400, "client disconnected", route=route, ip=ip, reason="disconnect"
                    )
                if size == 0:
                    return _reject(400, "empty upload", route=route, ip=ip, reason="no-file")
                try:
                    result = await run_in_threadpool(
                        pipeline.process_file, tmp, target=target, route=route, extra=extra
                    )
                except Exception as exc:
                    status, detail = _map_pipeline_error(exc)
                    if status == 500:
                        log.exception("transcription failed route=%s ip=%s", route, ip)
                    await run_in_threadpool(
                        _keep_failed, ctx, exc, status, detail, tmp, route, source
                    )
                    return _reject(status, detail, route=route, ip=ip, reason=type(exc).__name__)
            finally:
                _unlink(tmp)
            log.info("%s", format_log_line(result))
            return JSONResponse(result.to_response())

        # 7. multipart parse with a hard byte cap (chunked uploads have no Content-Length)
        try:
            form = await limited.form(max_files=1, max_fields=4, max_part_size=64 * 1024)
        except _UploadTooLarge:
            return _reject(413, too_large, route=route, ip=ip, reason="too-large")
        except StarletteHTTPException as exc:
            return _reject(400, str(exc.detail), route=route, ip=ip, reason="bad-multipart")
        except ClientDisconnect:
            return _reject(400, "client disconnected", route=route, ip=ip, reason="disconnect")

        tmp: Path | None = None
        try:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                # Names/types/sizes only (never values) to debug client form encodings.
                log.warning(
                    "no file upload; content-type=%r form fields: %s",
                    request.headers.get("content-type", "")[:60],
                    _describe_form(form),
                )
                return _reject(400, "missing 'file' upload", route=route, ip=ip, reason="no-file")
            target_raw = form.get("target")
            target = _normalize_target(target_raw if isinstance(target_raw, str) else None)
            if target is None:
                return _reject(
                    422,
                    "target must be 'pc' or 'phone'",
                    route=route,
                    ip=ip,
                    reason="bad-target",
                )
            try:
                tmp, _size = await run_in_threadpool(
                    _save_upload, upload, _safe_suffix(upload.filename), max_bytes
                )
            except _UploadTooLarge:
                return _reject(413, too_large, route=route, ip=ip, reason="too-large")

            # 8. pipeline (serialised inside Pipeline by a lock)
            try:
                result = await run_in_threadpool(
                    pipeline.process_file, tmp, target=target, route=route, extra=extra
                )
            except Exception as exc:
                status, detail = _map_pipeline_error(exc)
                if status == 500:
                    log.exception("transcription failed route=%s ip=%s", route, ip)
                await run_in_threadpool(_keep_failed, ctx, exc, status, detail, tmp, route, source)
                return _reject(status, detail, route=route, ip=ip, reason=type(exc).__name__)
        finally:
            _unlink(tmp)
            await form.close()

        log.info("%s", format_log_line(result))
        return JSONResponse(result.to_response())

    app.add_middleware(SourceGuardMiddleware, lan_networks=tuple(server_cfg.lan_networks))
    return app
