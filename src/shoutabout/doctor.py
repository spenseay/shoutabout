"""``python -m shoutabout doctor``: check every link of the chain and say how to fix it.

Output format::

    [PASS] Config — C:\\Users\\me\\AppData\\Roaming\\shoutabout\\config.toml
    [FAIL] Firewall rule — ShoutAbout-Hub-TCP-8765 not found
           fix: run scripts\\setup.ps1 in an admin PowerShell

Each check is isolated (a crash becomes a FAIL for that check only). Exit code 1 if any
check FAILs. Windows checks query PowerShell read-only (``Get-*`` cmdlets); nothing is
changed. Secrets (hub token, service-token secret) are never printed.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from shoutabout.config import Config, ConfigError, Paths, load_token, parse_config

log = logging.getLogger(__name__)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
FIX_INDENT = "       fix: "
FIX_CONT = "            "
SAMPLE_TRANSCRIPT = (
    "um so I pushed the fix to git hub and uh opened a pull request for better shell"
)
PUBLIC_TIMEOUT_S = 10.0
LOCAL_TIMEOUT_S = 5.0
POWERSHELL_TIMEOUT_S = 90.0


@dataclass
class Check:
    name: str
    status: str  # PASS / WARN / FAIL / SKIP
    detail: str = ""
    fix: str | None = None


def format_check(check: Check) -> str:
    line = f"[{check.status}] {check.name}"
    if check.detail:
        line += f" — {check.detail}"
    if check.fix and check.status != PASS:
        fix_lines = check.fix.splitlines() or [check.fix]
        line += "\n" + FIX_INDENT + fix_lines[0]
        for extra in fix_lines[1:]:
            line += "\n" + FIX_CONT + extra
    return line


def estimate_tokens(text: str) -> int:
    """Rough Whisper token estimate (errs high) for when the model isn't loaded."""
    if not text:
        return 0
    return max(len(text.split()), math.ceil(len(text) / 3))


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class PowerShellError(RuntimeError):
    pass


def run_powershell(script: str, timeout: float = POWERSHELL_TIMEOUT_S) -> Any:
    """Run a read-only PowerShell snippet that prints JSON; return the parsed value."""
    prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", prefix + script],
            capture_output=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise PowerShellError("powershell.exe not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PowerShellError(f"PowerShell timed out after {timeout:.0f} s") from exc
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise PowerShellError(err[0] if err else f"PowerShell exit code {proc.returncode}")
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise PowerShellError(f"unexpected PowerShell output: {stdout[:200]}") from exc


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    return [v for v in value if isinstance(v, dict)]


# --------------------------------------------------------------- interpreters
# Pure functions over PowerShell JSON / HTTP responses, so they're unit-testable.


def interpret_port_rule(rule: Mapping[str, Any] | None, name: str, port: int) -> Check:
    title = "Firewall rule"
    create = (
        f'New-NetFirewallRule -Name "{name}" -DisplayName "ShoutAbout Hub (TCP {port})" '
        f"-Group ShoutAbout -Direction Inbound -Action Allow -Protocol TCP -LocalPort {port} "
        "-Profile Private -RemoteAddress LocalSubnet"
    )
    if not rule:
        return Check(
            title,
            FAIL,
            f"{name} not found (the phone can't reach the hub over Wi-Fi)",
            "run scripts\\setup.ps1 in an admin PowerShell, or (admin):\n" + create,
        )
    problems = []
    if rule.get("Enabled") != "True":
        problems.append("disabled")
    if rule.get("Direction") != "Inbound":
        problems.append(f"direction {rule.get('Direction')}")
    if rule.get("Action") != "Allow":
        problems.append(f"action {rule.get('Action')}")
    profile = str(rule.get("Profile") or "")
    if "Private" not in profile and profile != "Any":
        problems.append(f"profile {profile or '?'} (needs Private)")
    ports = str(rule.get("LocalPort") or "")
    if ports and ports != "Any" and str(port) not in ports.split(","):
        problems.append(f"local port {ports} (hub uses {port})")
    if problems:
        return Check(
            title,
            FAIL,
            f"{name}: " + ", ".join(problems),
            f"re-run scripts\\setup.ps1 (admin), or: Enable-NetFirewallRule -Name {name}",
        )
    return Check(title, PASS, f"{name} enabled, inbound allow TCP {port}, profile {profile}")


def interpret_program_rules(rules: Sequence[Mapping[str, Any]], names: Sequence[str]) -> Check:
    title = "Firewall program rules"
    by_name = {str(r.get("Name")): r for r in rules}
    present = [n for n in names if n in by_name]
    missing = [n for n in names if n not in by_name]
    disabled = [n for n in present if by_name[n].get("Enabled") != "True"]
    if missing or disabled:
        bits = []
        if missing:
            bits.append("missing " + ", ".join(missing))
        if disabled:
            bits.append("disabled " + ", ".join(disabled))
        return Check(
            title,
            WARN,
            "; ".join(bits) + " — Windows may show an 'allow Python' prompt",
            "re-run scripts\\setup.ps1 in an admin PowerShell",
        )
    return Check(title, PASS, ", ".join(present))


# --- installed app (packaging/installer.iss) ---------------------------------------------

APP_FIREWALL_RULE = "ShoutAbout"  # display name of the rule the Windows installer adds


def interpret_app_rule(rules: Sequence[Mapping[str, Any]], exe: str) -> Check:
    """The installer's rule: inbound, allow, for ShoutAbout.exe, on Private networks."""
    title = "Firewall rule"
    fix = (
        "reinstall ShoutAbout (Setup adds the rule), or in an admin PowerShell:\n"
        f'netsh advfirewall firewall add rule name="{APP_FIREWALL_RULE}" dir=in action=allow '
        f'protocol=TCP profile=private program="{exe}"'
    )

    def same_exe(program: Any) -> bool:
        return str(program or "").strip().lower() == exe.lower()

    mine = [r for r in rules if same_exe(r.get("Program"))]
    if not mine:
        return Check(
            title,
            FAIL,
            f"no '{APP_FIREWALL_RULE}' rule for {exe} (the phone can't reach this PC over Wi-Fi)",
            fix,
        )
    for rule in mine:
        profile = str(rule.get("Profile") or "")
        if (
            rule.get("Enabled") == "True"
            and rule.get("Direction") == "Inbound"
            and rule.get("Action") == "Allow"
            and ("Private" in profile or profile == "Any")
        ):
            return Check(title, PASS, f"'{APP_FIREWALL_RULE}' allows {exe} ({profile} networks)")
    first = mine[0]
    state = ", ".join(
        f"{key.lower()} {first.get(key)}" for key in ("Enabled", "Direction", "Action", "Profile")
    )
    return Check(title, FAIL, f"'{APP_FIREWALL_RULE}' rule doesn't allow the app: {state}", fix)


# -------------------------------------------------------------------------------------------


def interpret_block_rules(
    rules: Sequence[Mapping[str, Any]], title: str = "Python block rules"
) -> Check:
    blocking = [
        r
        for r in rules
        if r.get("Enabled") == "True"
        and r.get("Direction") == "Inbound"
        and r.get("Action") == "Block"
    ]
    if not blocking:
        subject = "python" if title.startswith("Python") else "ShoutAbout"
        return Check(title, PASS, f"no inbound block rules for {subject}")
    relevant = [
        r
        for r in blocking
        if any(p in str(r.get("Profile") or "") for p in ("Private", "Any", "Domain"))
    ]
    desc = "; ".join(
        f"{r.get('DisplayName') or r.get('Name')} [{r.get('Profile')}] {r.get('Program')}"
        for r in blocking
    )
    fix = "remove them (admin PowerShell):\n" + "\n".join(
        f"Remove-NetFirewallRule -Name {_ps_quote(str(r.get('Name')))}" for r in blocking
    )
    status = FAIL if relevant else WARN
    return Check(title, status, f"{len(blocking)} inbound block rule(s): {desc}", fix)


def interpret_network_profiles(profiles: Sequence[Mapping[str, Any]], lan_ip: str | None) -> Check:
    title = "Network profile"
    if not profiles:
        return Check(title, WARN, "no active network connection profile found")

    def fix_for(alias: str) -> str:
        return (
            "Settings > Network & internet > Wi-Fi (or Ethernet) > your network > "
            "Network profile type: Private network\n"
            f'or (admin PowerShell): Set-NetConnectionProfile -InterfaceAlias "{alias}" '
            "-NetworkCategory Private"
        )

    def ok(p: Mapping[str, Any]) -> bool:
        return p.get("NetworkCategory") in ("Private", "DomainAuthenticated")

    def label(p: Mapping[str, Any]) -> str:
        return f"{p.get('InterfaceAlias')} ({p.get('Name')}): {p.get('NetworkCategory')}"

    lan = [p for p in profiles if lan_ip and lan_ip in (p.get("IPv4") or [])]
    if lan:
        bad = [p for p in lan if not ok(p)]
        if bad:
            return Check(
                title,
                FAIL,
                f"{label(bad[0])} — the hub's firewall rule only allows Private networks",
                fix_for(str(bad[0].get("InterfaceAlias"))),
            )
        others = [p for p in profiles if p not in lan and not ok(p)]
        detail = label(lan[0])
        if others:
            detail += "; other: " + ", ".join(label(p) for p in others)
        return Check(title, PASS, detail)
    active = [
        p
        for p in profiles
        if p.get("IPv4Connectivity") in ("Internet", "LocalNetwork") or not lan_ip
    ] or list(profiles)
    bad = [p for p in active if not ok(p)]
    if bad:
        return Check(
            title,
            WARN,
            ", ".join(label(p) for p in bad),
            fix_for(str(bad[0].get("InterfaceAlias"))),
        )
    return Check(title, PASS, ", ".join(label(p) for p in active))


def interpret_cloudflared(service: Mapping[str, Any] | None, tunnel_expected: bool) -> Check:
    title = "cloudflared service"
    if not service:
        if not tunnel_expected:
            return Check(title, SKIP, "not installed (no tunnel configured)")
        return Check(
            title,
            FAIL,
            "service not installed",
            "re-run scripts\\setup.ps1, or install it with: cloudflared service install <token>",
        )
    status = service.get("Status")
    start = service.get("StartType")
    if status != "Running":
        return Check(
            title,
            FAIL,
            f"{status} (start type {start})",
            "Start-Service cloudflared (admin), and check the Windows Event Log for errors",
        )
    if start not in ("Automatic", "AutomaticDelayedStart"):
        return Check(
            title,
            WARN,
            f"running, but start type is {start}",
            "Set-Service cloudflared -StartupType Automatic (admin)",
        )
    return Check(title, PASS, f"running ({start})")


def normalize_public_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if not path.endswith("/health"):
        path += "/health"
    return base + path


def _is_cloudflare(response: httpx.Response) -> bool:
    server = response.headers.get("server", "").lower()
    return "cloudflare" in server or "cf-ray" in response.headers


def _hub_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def check_health(
    name: str,
    url: str,
    *,
    fix: str,
    note: str = "",
    transport: httpx.BaseTransport | None = None,
) -> Check:
    try:
        with httpx.Client(
            timeout=LOCAL_TIMEOUT_S, follow_redirects=False, transport=transport
        ) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        return Check(name, FAIL, f"{url}: {type(exc).__name__}: {exc}", fix)
    body = _hub_json(response)
    if response.status_code == 200 and body == {"ok": True}:
        return Check(name, PASS, f"{url} OK{note}")
    if response.status_code == 403 and body == {"detail": "forbidden"}:
        return Check(
            name,
            FAIL,
            f"{url}: 403 — the hub rejected this source address",
            "check server.lan_networks in config.toml covers your LAN",
        )
    port = urlsplit(url).port or 80
    return Check(
        name,
        FAIL,
        f"{url}: HTTP {response.status_code} — something other than the ShoutAbout hub is "
        f"answering on port {port}",
        f"find it: Get-NetTCPConnection -LocalPort {port} -State Listen | "
        "Select-Object OwningProcess; stop that program or change server.port "
        "(and the tunnel's service URL)",
    )


def check_public_url(
    url: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    port: int = 8765,
    transport: httpx.BaseTransport | None = None,
) -> Check:
    title = "Public URL"
    headers: dict[str, str] = {}
    with_token = bool(client_id and client_secret)
    if with_token:
        headers = {
            "CF-Access-Client-Id": client_id or "",
            "CF-Access-Client-Secret": client_secret or "",
        }
    host = urlsplit(url).hostname or url
    tunnel_fix = (
        f"is the hub running? Is the tunnel's ingress service http://127.0.0.1:{port} "
        f"for {host}? Check Get-Service cloudflared and the tunnel's public hostname config."
    )
    try:
        with httpx.Client(
            timeout=PUBLIC_TIMEOUT_S, follow_redirects=False, transport=transport
        ) as client:
            response = client.get(url, headers=headers)
    except httpx.ConnectError as exc:
        return Check(
            title,
            FAIL,
            f"{url}: can't connect ({exc})",
            f"check the DNS record for {host} (CNAME to <tunnel-id>.cfargotunnel.com) "
            "and that the tunnel is healthy in the Cloudflare dashboard",
        )
    except httpx.HTTPError as exc:
        return Check(title, FAIL, f"{url}: {type(exc).__name__}: {exc}", tunnel_fix)

    status = response.status_code
    body = _hub_json(response)
    if status == 200 and body == {"ok": True}:
        if not with_token:
            # Reaching the hub with no service token means Access isn't protecting the host.
            return Check(
                title,
                FAIL,
                f"{url} reached the hub WITHOUT a service token: Cloudflare Access is not "
                "protecting this hostname",
                f"create the Access application for {host} with a Service Auth policy "
                "(docs/cloudflare.md), and set [access] team_domain + aud in config.toml so "
                "the hub rejects requests without a valid Access JWT",
            )
        return Check(title, PASS, f"{url} OK end-to-end with the service token")
    blocked = (
        "Cloudflare Access is blocking unauthenticated requests (expected); pass "
        "service-token headers to test end-to-end"
    )
    location = response.headers.get("location", "")
    if 300 <= status < 400:
        loc_host = (urlsplit(location).hostname or "").lower()
        if loc_host.endswith(".cloudflareaccess.com"):
            if with_token:
                return Check(
                    title,
                    FAIL,
                    f"{url}: Access rejected the service token (redirect to login)",
                    "add a Service Auth policy with this service token to the Access "
                    "application, and check the Client ID/Secret",
                )
            return Check(title, PASS, blocked)
        return Check(title, WARN, f"{url}: unexpected redirect (HTTP {status}) to {location}")
    if status in (401, 403):
        if isinstance(body, dict) and body.get("detail") == "forbidden":
            return Check(
                title,
                FAIL,
                f"{url}: the hub rejected the tunnel's source address",
                "cloudflared must run on this PC and point at http://127.0.0.1:" + str(port),
            )
        if _is_cloudflare(response) or not isinstance(body, dict):
            if with_token:
                return Check(
                    title,
                    FAIL,
                    f"{url}: Access rejected the service token (HTTP {status})",
                    "add a Service Auth policy with this service token to the Access "
                    "application, and check the Client ID/Secret",
                )
            return Check(title, PASS, blocked)
    if status in (502, 503, 530):
        return Check(title, FAIL, f"{url}: HTTP {status} — tunnel not reaching the hub", tunnel_fix)
    return Check(title, WARN, f"{url}: unexpected HTTP {status}")


# ---------------------------------------------------------------------- doctor


class Doctor:
    def __init__(
        self,
        paths: Paths,
        *,
        skip_model: bool = False,
        skip_tunnel: bool = False,
        skip_ollama: bool = False,
        url: str | None = None,
        cf_client_id: str | None = None,
        cf_client_secret: str | None = None,
        out: Callable[[str], None] = print,
        powershell: Callable[[str], Any] = run_powershell,
    ) -> None:
        self.paths = paths
        self.skip_model = skip_model
        self.skip_tunnel = skip_tunnel
        self.skip_ollama = skip_ollama
        self.url = url
        self.cf_client_id = cf_client_id
        self.cf_client_secret = cf_client_secret
        self.out = out
        self.powershell = powershell
        self.cfg = Config()
        self.choice: Any = None  # shoutabout.modelpick.ModelChoice, once resolved
        self.install_state: dict[str, Any] = {}
        self.terms: tuple[str, ...] = ()
        self.results: list[Check] = []

    # ----------------------------------------------------------------- runner

    def run(self) -> int:
        self.install_state = self._read_install_state()
        steps: list[tuple[str, Callable[[], Check | list[Check] | None]]] = [
            ("Config", self.check_config),
            ("Model choice", self.check_choice),
            ("Hub token", self.check_token),
            ("Vocab", self.check_vocab),
            ("Audio decoding", self.check_audio),
            ("CUDA", self.check_cuda),
            ("Whisper model", self.check_model),
            ("Ollama", self.check_ollama),
            ("Firewall rule", self.check_firewall),
            ("Python block rules", self.check_block_rules),
            ("Network profile", self.check_network_profile),
            ("Hub (loopback)", self.check_hub_loopback),
            ("Hub (LAN URL)", self.check_hub_lan),
            ("cloudflared service", self.check_cloudflared),
            ("Access JWT check", self.check_access_config),
            ("Public URL", self.check_public),
            ("macOS", self.check_macos),  # v2-macos
        ]
        for name, fn in steps:
            try:
                produced = fn()
            except Exception as exc:
                log.debug("doctor check %s crashed", name, exc_info=True)
                produced = Check(name, FAIL, f"check crashed: {type(exc).__name__}: {exc}")
            checks = produced if isinstance(produced, list) else [produced] if produced else []
            for check in checks:
                self.results.append(check)
                self.out(format_check(check))
        failed = sum(c.status == FAIL for c in self.results)
        warned = sum(c.status == WARN for c in self.results)
        self.out("")
        self.out(f"{failed} failed, {warned} warnings, {len(self.results)} checks")
        return 1 if failed else 0

    def _read_install_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.paths.install_state_file.read_text("utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def _windows(self) -> bool:
        return sys.platform == "win32"

    def check_macos(self) -> list[Check] | None:
        """v2-macos: permissions, Bonjour, the chip, open at login (macOS only)."""
        if sys.platform != "darwin":
            return None
        from shoutabout.mac.doctor_mac import macos_rows

        return [Check(n, s, d, f) for n, s, d, f in macos_rows()]

    # ----------------------------------------------------------------- checks

    def check_config(self) -> Check:
        path = self.paths.config_file
        if not path.exists():
            return Check(
                "Config",
                WARN,
                f"{path} not found; using defaults",
                "python -m shoutabout init",
            )
        try:
            self.cfg = parse_config(path.read_text("utf-8"), str(path))
        except ConfigError as exc:
            first = str(exc).strip().splitlines()
            return Check(
                "Config",
                FAIL,
                " ".join(first[:3]),
                "fix the error in config.toml (compare with data/config.example.toml)",
            )
        return Check("Config", PASS, str(path))

    def _resolve(self) -> Any:
        if self.choice is None:
            from shoutabout.modelpick import resolve

            self.choice = resolve(self.cfg, probe_ollama=not self.skip_ollama)
        return self.choice

    def _stt(self) -> Any:
        """[stt] with "auto" replaced by what this PC runs."""
        from shoutabout.modelpick import concrete_stt

        return concrete_stt(self.cfg.stt, self._resolve())

    def check_choice(self) -> Check:
        choice = self._resolve()
        detail = f"{choice.summary}. {choice.explanation}"
        if choice.source != "config":
            from shoutabout.hardware import detect

            detail += f" ({detect().summary()})"
        return Check("Model choice", PASS, detail)

    def check_token(self) -> Check:
        try:
            token = load_token(self.paths)
        except ConfigError as exc:
            return Check("Hub token", FAIL, str(exc), "python -m shoutabout init")
        return Check("Hub token", PASS, f"present ({len(token)} chars) in {self.paths.token_file}")

    def check_vocab(self) -> Check:
        from shoutabout.vocab import VocabStore, build_stt_hints

        path = self.paths.vocab_file
        if not path.exists():
            return Check(
                "Vocab",
                WARN,
                f"{path} not found (no custom vocabulary)",
                "python -m shoutabout init",
            )
        self.terms = tuple(VocabStore(path).get().terms)
        stt = self.cfg.stt
        if stt.prompt_mode == "none":
            return Check(
                "Vocab",
                PASS,
                f"{len(self.terms)} terms; stt.prompt_mode = none "
                "(vocab not sent to Whisper, still used by cleanup)",
            )
        build = build_stt_hints(self.terms, estimate_tokens, stt.max_prompt_tokens, stt.prompt_mode)
        detail = (
            f"{len(self.terms)} terms, ~{build.tokens} prompt tokens (estimate, budget "
            f"{stt.max_prompt_tokens}), {len(build.dropped)} dropped"
        )
        if build.dropped:
            shown = ", ".join(build.dropped[:5]) + (" ..." if len(build.dropped) > 5 else "")
            return Check(
                "Vocab",
                WARN,
                f"{detail}: {shown}",
                "put the most important terms at the top of vocab.txt or shorten the list",
            )
        return Check("Vocab", PASS, detail)

    def check_audio(self) -> Check:
        """Decode the bundled 1 s AAC .m4a (the iPhone app's format) the way uploads are."""
        from shoutabout.audio import AudioError, DecoderUnavailableError, decode_audio_info
        from shoutabout.selftest import audio_sample_path

        name = "Audio decoding"
        try:
            decoded = decode_audio_info(audio_sample_path())
        except DecoderUnavailableError as exc:
            return Check(
                name,
                FAIL,
                "Windows Media Foundation is missing: phone recordings can't be decoded",
                str(exc),
            )
        except AudioError as exc:
            fix = (
                "reinstall ShoutAbout"
                if sys.platform == "win32"
                else "install PyAV in the venv: uv sync"
            )
            return Check(name, FAIL, f"can't decode AAC .m4a: {exc}", fix)
        via = {
            "mediafoundation": "Windows Media Foundation",
            "pyav": "PyAV (FFmpeg)",
        }.get(decoded.backend, decoded.backend)
        return Check(name, PASS, f"AAC .m4a decodes with {via}")

    def check_cuda(self) -> Check:
        stt = self._stt()
        if stt.device != "cuda":
            return Check("CUDA", SKIP, f"stt.device = {stt.device}")
        from shoutabout.engine.cuda_dlls import add_nvidia_dll_dirs

        dirs = add_nvidia_dll_dirs()
        if self._windows and not dirs:
            return Check(
                "CUDA",
                FAIL,
                "NVIDIA cuBLAS/cuDNN DLL folders not found in the venv",
                "uv sync --extra cuda   (or re-run scripts\\setup.ps1)",
            )
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        if count < 1:
            return Check(
                "CUDA",
                FAIL,
                "CTranslate2 sees no CUDA device",
                "install/update the NVIDIA driver (CUDA 12 capable); check nvidia-smi works",
            )
        dll_names = ", ".join(sorted({Path(d).parent.name for d in dirs})) or "system"
        detail = f"{count} CUDA device(s); NVIDIA DLLs: {dll_names}"
        try:
            supported = ctranslate2.get_supported_compute_types("cuda")
        except Exception:
            supported = None
        if supported is not None and stt.compute_type not in supported:
            return Check(
                "CUDA",
                WARN,
                f"{detail}; compute_type {stt.compute_type} not supported "
                f"(supported: {', '.join(sorted(supported))})",
                "set stt.compute_type in config.toml to a supported type",
            )
        return Check("CUDA", PASS, detail)

    def check_model(self) -> Check:
        name = "Whisper model"
        if self.skip_model:
            return Check(name, SKIP, "skipped (--skip-model)")
        import numpy as np

        from shoutabout.engine import create_engine
        from shoutabout.engine.base import EngineError, SttHints

        stt = self._stt()
        engine = create_engine(stt, cpu_threads=self._resolve().cpu_threads)
        t0 = time.perf_counter()
        try:
            engine.load()
        except EngineError as exc:
            return Check(
                name,
                FAIL,
                str(exc),
                "python -m shoutabout download-model; see SETUP.md (CUDA / cuDNN section)",
            )
        load_s = time.perf_counter() - t0
        audio = (np.random.default_rng(0).standard_normal(16_000) * 0.01).astype(np.float32)
        t1 = time.perf_counter()
        engine.transcribe(audio, SttHints())
        stt_ms = (time.perf_counter() - t1) * 1000
        device = getattr(engine, "device", stt.device)
        detail = (
            f"{engine.name} loaded in {load_s:.1f} s; 1 s test clip transcribed in {stt_ms:.0f} ms"
        )
        if self.terms and stt.prompt_mode != "none":
            from shoutabout.vocab import build_stt_hints

            build = build_stt_hints(
                self.terms, engine.count_tokens, stt.max_prompt_tokens, stt.prompt_mode
            )
            detail += f"; vocab prompt {build.tokens} tokens, {len(build.dropped)} dropped"
        if device != stt.device:
            return Check(
                name,
                WARN,
                f"{detail} — running on {device}, not {stt.device} (CPU fallback; slow)",
                "fix CUDA (see the CUDA check) or set stt.cpu_fallback = false to fail loudly",
            )
        return Check(name, PASS, detail)

    def check_ollama(self) -> list[Check]:
        cc = self.cfg.cleanup
        if self.skip_ollama:
            return [Check("Ollama", SKIP, "skipped (--skip-ollama)")]
        if cc.enabled is False:
            return [Check("Ollama", SKIP, "cleanup disabled (cleanup.enabled = false)")]
        if cc.enabled == "auto":
            choice = self._resolve()
            if not choice.cleanup or not choice.cleanup_model:
                return [Check("Ollama", SKIP, f"cleanup.enabled = auto: {choice.cleanup_note}")]
            cc = cc.model_copy(update={"model": choice.cleanup_model})
        from shoutabout.cleanup import OllamaCleaner

        cleaner = OllamaCleaner(cc)
        ok, message = cleaner.model_available()
        if not ok:
            return [
                Check(
                    "Ollama",
                    FAIL,
                    message,
                    f"install/start Ollama (https://ollama.com), then: ollama pull {cc.model}",
                )
            ]
        checks = [Check("Ollama", PASS, f"{cc.ollama_url}: {cc.model} available")]

        warmed = cleaner.warm()
        status = cleaner.loaded_status()
        checks.append(self._gpu_check(status, warmed))

        t0 = time.perf_counter()
        result = cleaner.clean(SAMPLE_TRANSCRIPT, self.terms)
        ms = (time.perf_counter() - t0) * 1000
        if result.status == "applied":
            checks.append(Check("Cleanup sample", PASS, f"{ms:.0f} ms: {result.text!r}"))
        else:
            checks.append(
                Check(
                    "Cleanup sample",
                    WARN,
                    f"{result.status} after {ms:.0f} ms ({result.reason})",
                    "try another model (python -m shoutabout benchmark --models a,b) or "
                    f"raise cleanup.timeout_s (now {cc.timeout_s} s)",
                )
            )
        return checks

    def _gpu_check(self, status: Mapping[str, Any] | None, warmed: bool) -> Check:
        name = "Ollama GPU"
        if not status:
            return Check(
                name,
                WARN,
                "model not loaded" + ("" if warmed else " (warm-up failed)"),
                "check `ollama ps`; is the GPU memory full?",
            )
        size = int(status.get("size") or 0)
        vram = int(status.get("size_vram") or 0)
        gb = vram / 1024**3
        if vram <= 0:
            return Check(
                name,
                WARN,
                "model is running on the CPU (slow)",
                "free GPU memory or pick a smaller model; check `ollama ps`",
            )
        if size and vram < size:
            return Check(
                name,
                WARN,
                f"only {vram / size:.0%} of the model is in VRAM ({gb:.1f} GB); rest on CPU",
                "free GPU memory, lower cleanup.num_ctx, or pick a smaller model",
            )
        return Check(name, PASS, f"fully on GPU ({gb:.1f} GB VRAM)")

    def _rule_name(self) -> str:
        name = self.install_state.get("firewall_rule_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return f"ShoutAbout-Hub-TCP-{self.cfg.server.port}"

    def _lan_disabled(self) -> bool:
        from shoutabout.netinfo import is_loopback_host

        return is_loopback_host(self.cfg.server.host)

    def check_firewall(self) -> list[Check]:
        if not self._windows:
            return [Check("Firewall rule", SKIP, "not Windows")]
        if self._lan_disabled():
            return [Check("Firewall rule", SKIP, "server.host is loopback (no LAN access)")]
        from shoutabout.frozen import is_frozen

        if is_frozen():  # the installed app: the installer's program rule, not setup.ps1's
            script = (
                "$out = New-Object System.Collections.ArrayList\n"
                f"foreach ($r in @(Get-NetFirewallRule -DisplayName {_ps_quote(APP_FIREWALL_RULE)} "
                "-ErrorAction SilentlyContinue)) {\n"
                "  $af = $r | Get-NetFirewallApplicationFilter\n"
                "  [void]$out.Add([pscustomobject]@{Name=[string]$r.Name; "
                "Enabled=[string]$r.Enabled; Direction=[string]$r.Direction; "
                "Action=[string]$r.Action; Profile=[string]$r.Profile; "
                "Program=[string]$af.Program})\n"
                "}\n"
                "ConvertTo-Json -InputObject $out.ToArray() -Compress"
            )
            return [interpret_app_rule(_as_list(self.powershell(script)), sys.executable)]
        port = self.cfg.server.port
        base = self._rule_name()
        names = [base, f"{base}-python", f"{base}-pythonw"]
        script = (
            "$out = New-Object System.Collections.ArrayList\n"
            f"foreach ($n in @({', '.join(_ps_quote(n) for n in names)})) {{\n"
            "  $r = Get-NetFirewallRule -Name $n -ErrorAction SilentlyContinue\n"
            "  if ($r) {\n"
            "    $pf = $r | Get-NetFirewallPortFilter\n"
            "    $af = $r | Get-NetFirewallApplicationFilter\n"
            "    [void]$out.Add([pscustomobject]@{Name=[string]$r.Name; "
            "Enabled=[string]$r.Enabled; Direction=[string]$r.Direction; "
            "Action=[string]$r.Action; Profile=[string]$r.Profile; "
            "LocalPort=[string]($pf.LocalPort -join ','); Program=[string]$af.Program})\n"
            "  }\n"
            "}\n"
            "ConvertTo-Json -InputObject $out.ToArray() -Compress"
        )
        rules = _as_list(self.powershell(script))
        by_name = {str(r.get("Name")): r for r in rules}
        checks = [interpret_port_rule(by_name.get(base), base, port)]
        checks.append(interpret_program_rules(rules, names[1:]))
        return checks

    def check_block_rules(self) -> Check:
        if not self._windows:
            return Check("Python block rules", SKIP, "not Windows")
        from shoutabout.frozen import is_frozen

        # The installed app is ShoutAbout.exe, not python: look for block rules on that.
        title, pattern = (
            ("Block rules", r"\\ShoutAbout\.exe$")
            if is_frozen()
            else ("Python block rules", "python")
        )
        script = (
            "$out = New-Object System.Collections.ArrayList\n"
            "foreach ($f in @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue "
            f"| Where-Object {{ $_.Program -match '{pattern}' }})) {{\n"
            "  $r = $f | Get-NetFirewallRule -ErrorAction SilentlyContinue\n"
            "  if ($r) { [void]$out.Add([pscustomobject]@{Name=[string]$r.Name; "
            "DisplayName=[string]$r.DisplayName; Program=[string]$f.Program; "
            "Profile=[string]$r.Profile; Enabled=[string]$r.Enabled; "
            "Direction=[string]$r.Direction; Action=[string]$r.Action}) }\n"
            "}\n"
            "ConvertTo-Json -InputObject $out.ToArray() -Compress"
        )
        return interpret_block_rules(_as_list(self.powershell(script)), title)

    def check_network_profile(self) -> Check:
        if not self._windows:
            return Check("Network profile", SKIP, "not Windows")
        if self._lan_disabled():
            return Check("Network profile", SKIP, "server.host is loopback (no LAN access)")
        from shoutabout.netinfo import lan_ipv4

        script = (
            "$out = New-Object System.Collections.ArrayList\n"
            "foreach ($p in @(Get-NetConnectionProfile -ErrorAction SilentlyContinue)) {\n"
            "  $ips = @(Get-NetIPAddress -InterfaceIndex $p.InterfaceIndex -AddressFamily IPv4 "
            "-ErrorAction SilentlyContinue | ForEach-Object { [string]$_.IPAddress })\n"
            "  [void]$out.Add([pscustomobject]@{InterfaceAlias=[string]$p.InterfaceAlias; "
            "Name=[string]$p.Name; NetworkCategory=[string]$p.NetworkCategory; "
            "IPv4Connectivity=[string]$p.IPv4Connectivity; IPv4=$ips})\n"
            "}\n"
            "ConvertTo-Json -InputObject $out.ToArray() -Compress -Depth 3"
        )
        lan_ip = (self.cfg.server.advertise_host or "").strip() or lan_ipv4()
        return interpret_network_profiles(_as_list(self.powershell(script)), lan_ip)

    def _start_fix(self) -> str:
        task = self.install_state.get("task_name") or "ShoutAbout Hub"
        return (
            f"start the hub: Start-ScheduledTask -TaskName {_ps_quote(str(task))}  "
            "(or: python -m shoutabout run --no-tray); see logs\\hub.log"
        )

    def check_hub_loopback(self) -> Check:
        url = f"http://127.0.0.1:{self.cfg.server.port}/health"
        return check_health("Hub (loopback)", url, fix=self._start_fix())

    def check_hub_lan(self) -> Check:
        name = "Hub (LAN URL)"
        if self._lan_disabled():
            return Check(name, SKIP, "server.host is loopback (no LAN access)")
        from shoutabout.netinfo import lan_url

        url = lan_url(self.cfg)
        if not url:
            return Check(
                name,
                WARN,
                "no private LAN IPv4 address detected",
                "connect to your home network, or set server.advertise_host in config.toml",
            )
        return check_health(
            name,
            url + "/health",
            fix=self._start_fix(),
            note=(
                " (from this PC: proves the hub listens on the LAN IP, but the firewall isn't "
                "exercised; open the URL in the phone's browser to test that)"
            ),
        )

    def _public_host(self) -> str | None:
        if self.url:
            return self.url
        host = self.install_state.get("public_hostname")
        return host.strip() if isinstance(host, str) and host.strip() else None

    def check_cloudflared(self) -> Check:
        title = "cloudflared service"
        if self.skip_tunnel:
            return Check(title, SKIP, "skipped (--skip-tunnel)")
        if not self._windows:
            return Check(title, SKIP, "not Windows")
        # setup may have installed a separately named service (install-state.json).
        name = str(self.install_state.get("cloudflared_service_name") or "cloudflared")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
            name = "cloudflared"
        script = (
            f"$s = Get-Service -Name '{name}' -ErrorAction SilentlyContinue\n"
            "if ($s) { ConvertTo-Json -InputObject ([pscustomobject]@{Name=[string]$s.Name; "
            "Status=[string]$s.Status; StartType=[string]$s.StartType}) -Compress } "
            "else { 'null' }"
        )
        service = self.powershell(script)
        mode = str(self.install_state.get("tunnel_mode") or "")
        expected = bool(self._public_host()) or mode not in ("", "none")
        return interpret_cloudflared(service if isinstance(service, dict) else None, expected)

    def check_access_config(self) -> Check:
        title = "Access JWT check"
        access = self.cfg.access
        if access.enabled:
            extra = " + client-id allow-list" if access.allowed_client_ids else ""
            return Check(title, PASS, f"validating JWTs from {access.team_domain}{extra}")
        if self.skip_tunnel and not self._public_host():
            return Check(title, SKIP, "no tunnel configured")
        return Check(
            title,
            WARN,
            "[access] team_domain/aud not set: tunnel requests are accepted with the hub "
            "token alone",
            "set [access] team_domain and aud in config.toml (SETUP.md step 5c), then "
            "restart the hub",
        )

    def check_public(self) -> Check:
        if self.skip_tunnel:
            return Check("Public URL", SKIP, "skipped (--skip-tunnel)")
        host = self._public_host()
        if not host:
            return Check(
                "Public URL",
                SKIP,
                "no public hostname (pass --url or run scripts\\setup.ps1)",
            )
        return check_public_url(
            normalize_public_url(host),
            client_id=self.cf_client_id,
            client_secret=self.cf_client_secret,
            port=self.cfg.server.port,
        )


def run_doctor(
    paths: Paths,
    *,
    skip_model: bool = False,
    skip_tunnel: bool = False,
    skip_ollama: bool = False,
    url: str | None = None,
    cf_client_id: str | None = None,
    cf_client_secret: str | None = None,
    out: Callable[[str], None] = print,
) -> int:
    return Doctor(
        paths,
        skip_model=skip_model,
        skip_tunnel=skip_tunnel,
        skip_ollama=skip_ollama,
        url=url,
        cf_client_id=cf_client_id,
        cf_client_secret=cf_client_secret,
        out=out,
    ).run()
