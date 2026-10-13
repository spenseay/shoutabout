"""Configuration: paths, the TOML config schema, and the hub bearer token.

Everything user-specific (domain, IPs, tokens, model names) lives in the config dir,
never in code. The config dir defaults to ``%APPDATA%\\shoutabout`` on Windows and
``~/.config/shoutabout`` elsewhere, and can be overridden with ``SHOUTABOUT_HOME``. On
macOS (v2-macos) it is ``~/Library/Application Support/ShoutAbout``, with the logs in
``~/Library/Logs/ShoutAbout``.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import sys
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

APP_NAME = "shoutabout"
ENV_HOME = "SHOUTABOUT_HOME"
MIN_TOKEN_LENGTH = 32
# Default hub port for new installs. 8765 (the v1 default) collides with other software;
# existing configs keep whatever port they name explicitly.
DEFAULT_PORT = 47821

# Source networks a "LAN" request may come from. Anything configured in
# server.lan_networks must sit inside one of these (no public ranges, ever).
PRIVATE_PARENT_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # CGNAT / Tailscale (not enabled by default)
        "169.254.0.0/16",  # IPv4 link-local
        "fc00::/7",  # IPv6 unique local
        "fe80::/10",  # IPv6 link-local
    )
)


class ConfigError(RuntimeError):
    """Raised when the config file or token is missing or invalid."""


# --------------------------------------------------------------------------- paths


def default_home() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":  # v2-macos
        return mac_home()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / APP_NAME


def mac_home() -> Path:
    return Path.home() / "Library" / "Application Support" / "ShoutAbout"


def mac_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "ShoutAbout"


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(default_home())

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def token_file(self) -> Path:
        return self.home / "hub_token.txt"

    @property
    def vocab_file(self) -> Path:
        return self.home / "vocab.txt"

    @property
    def log_dir(self) -> Path:
        # v2-macos: the Mac app logs where Console.app looks (~/Library/Logs/ShoutAbout);
        # a custom home (SHOUTABOUT_HOME, --home, tests) keeps its own logs folder.
        if sys.platform == "darwin" and self.home == mac_home():
            return mac_log_dir()
        return self.home / "logs"

    # Where earlier versions kept a dictation history and its audio; nothing writes them
    # now (the ``history-gone`` migration deletes them, firstrun still looks for them).
    @property
    def history_file(self) -> Path:
        return self.home / "history.jsonl"

    @property
    def audio_dir(self) -> Path:
        return self.home / "audio"

    @property
    def install_state_file(self) -> Path:
        return self.home / "install-state.json"

    @property
    def suggestions_file(self) -> Path:  # earlier versions; migrations delete it
        return self.home / "vocab-suggestions.json"

    @property
    def devices_file(self) -> Path:
        """Paired phones: names and SHA-256 hashes of their tokens (never the tokens)."""
        return self.home / "devices.json"

    @property
    def identity_file(self) -> Path:
        """This installation's stable id and its friendly name (DNS-SD, /hello)."""
        return self.home / "identity.json"

    @property
    def failed_dir(self) -> Path:
        """Where earlier versions kept recordings that failed (now kept in memory only)."""
        return self.home / "failed"

    @property
    def migrations_file(self) -> Path:
        """Which one-time config upgrades already ran (shoutabout.migrations)."""
        return self.home / "migrations.json"

    @property
    def setup_flag_file(self) -> Path:
        """Present once the first-run setup window was finished (or skipped)."""
        return self.home / "setup_complete"


# -------------------------------------------------------------------------- schema


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ServerConfig(_Section):
    # 0.0.0.0 = loopback (cloudflared) + LAN. Use 127.0.0.1 for tunnel/local only.
    host: str = "0.0.0.0"
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    # Source ranges accepted as the "lan" route. Must be private ranges.
    lan_networks: list[str] = Field(
        default_factory=lambda: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    )
    # Override the LAN IP shown by the tray / doctor if auto-detection picks the wrong one.
    advertise_host: str | None = None
    # Announce this PC on the home network (DNS-SD "_shoutabout._tcp") so the iPhone app
    # finds it by itself. No secrets are announced. false = pair with the QR code instead.
    discovery: bool = True
    max_upload_mb: float = Field(default=25.0, gt=0)
    max_audio_seconds: float = Field(default=600.0, gt=0)
    rate_limit_per_minute: int = Field(default=30, ge=1)
    max_auth_failures: int = Field(default=10, ge=1)  # per source, per 10 minutes

    @field_validator("lan_networks")
    @classmethod
    def _private_only(cls, value: list[str]) -> list[str]:
        for item in value:
            try:
                net = ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid network {item!r}: {exc}") from exc
            if not any(
                net.version == parent.version and net.subnet_of(parent)  # type: ignore[arg-type]
                for parent in PRIVATE_PARENT_NETWORKS
            ):
                raise ValueError(
                    f"{item!r} is not a private range; the hub never accepts public sources"
                )
        return value

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)


class SttConfig(_Section):
    engine: Literal["faster-whisper"] = "faster-whisper"
    # "auto" = picked from this PC's hardware (shoutabout.modelpick); explicit values win.
    model: str = "auto"
    device: Literal["auto", "cuda", "cpu"] = "auto"
    compute_type: str = "auto"
    # With the three above on "auto": "auto" picks the quality tier from the hardware;
    # "best" / "balanced" / "light" force one (the Settings window's override).
    tier: Literal["auto", "best", "balanced", "light"] = "auto"
    # If CUDA fails to initialise: false = fail with a clear error, true = run on CPU.
    # (With device = "auto" ShoutAbout always falls back to the CPU.)
    cpu_fallback: bool = False
    cpu_compute_type: str = "int8"
    language: str | None = "en"
    beam_size: int = Field(default=5, ge=1, le=10)
    vad_filter: bool = True
    vad_min_silence_ms: int = Field(default=500, ge=0)
    # How vocab is fed to Whisper: as `hotwords`, as `initial_prompt`, both, or not at all.
    prompt_mode: Literal["both", "hotwords", "initial_prompt", "none"] = "both"
    # Token budget for the vocab hint text (Whisper's prompt limit is ~224 tokens).
    max_prompt_tokens: int = Field(default=200, ge=8, le=223)
    # Model download/cache dir; empty = Hugging Face default cache.
    download_root: str | None = None


class CleanupConfig(_Section):
    # "auto" = on only when this PC can afford it and a suitable model is already pulled
    # in Ollama (shoutabout.modelpick); true / false force it.
    enabled: Literal["auto"] | bool = "auto"
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b-instruct-2507-q4_K_M"
    timeout_s: float = Field(default=2.0, gt=0)
    # Ollama keep_alive: -1 keeps the model loaded indefinitely; or a duration like "30m".
    keep_alive: int | str = -1
    # Send think=false (disables reasoning on hybrid-thinking models). None = don't send.
    think: bool | None = False
    num_ctx: int = Field(default=4096, ge=512)
    # Re-check every N seconds that the model is loaded (re-loads it after an Ollama restart).
    warm_interval_s: int = Field(default=300, ge=0)
    # Safety-guard thresholds (see cleanup.guard()).
    # Fraction of words the LLM may change beyond the allowed fixes (filler removal, vocab
    # spelling, punctuation/case). 0 = none: any other word change falls back to raw text.
    max_change_ratio: float = Field(default=0.0, ge=0, le=1)
    min_similarity: float = Field(default=0.6, ge=0, le=1)
    min_length_ratio: float = Field(default=0.6, ge=0)
    max_length_ratio: float = Field(default=1.25, ge=1)


class PasteConfig(_Section):
    # false = never paste on the PC (target=pc then behaves like phone mode + warning).
    enabled: bool = True
    trailing_space: bool = False
    restore_clipboard: bool = True
    restore_delay_ms: int = Field(default=500, ge=0, le=5000)
    exclude_from_history: bool = True
    # Processes (exe names, case-insensitive) that don't paste on Ctrl+V; these get the
    # other standard paste chord, Shift+Insert, instead. Never any other key.
    shift_insert_processes: list[str] = Field(
        default_factory=lambda: ["mintty.exe", "putty.exe", "kitty.exe"]
    )


class AccessConfig(_Section):
    # Cloudflare Access JWT validation for the tunnel route. Both empty = disabled (warns).
    team_domain: str = ""  # e.g. "yourteam.cloudflareaccess.com"
    aud: list[str] = Field(default_factory=list)  # Application Audience (AUD) tag(s)
    # Optional: only accept JWTs issued to these service-token Client IDs (`common_name`).
    allowed_client_ids: list[str] = Field(default_factory=list)

    @field_validator("aud", mode="before")
    @classmethod
    def _aud_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value

    @field_validator("team_domain")
    @classmethod
    def _normalize_team_domain(cls, value: str) -> str:
        v = value.strip().lower()
        for prefix in ("https://", "http://"):
            if v.startswith(prefix):
                v = v[len(prefix) :]
        v = v.split("/", 1)[0]
        if v and "." not in v:
            v = f"{v}.cloudflareaccess.com"
        return v

    @property
    def enabled(self) -> bool:
        return bool(self.team_domain and self.aud)


class LegacyHistoryConfig(_Section):
    """``[history]`` of earlier versions. ShoutAbout keeps no history now (Recent holds the
    last few dictations in memory); the keys still load, unused, and the ``history-gone``
    migration removes the section."""

    enabled: bool = False
    keep_audio: bool = False


class LegacyLearnConfig(_Section):
    """``[learn]`` of earlier versions (word suggestions from project folders, since
    removed). The keys still load, unused, and the ``suggestions-gone`` migration removes
    the section."""

    enabled: bool = True
    seed_projects: bool = False
    project_roots: list[str] = Field(default_factory=list)
    notify: bool = True


class DictationConfig(_Section):
    """ "Talk to this PC": press a key, speak, press it again; the text is pasted where
    you're typing."""

    enabled: bool = True
    hotkey: str = "right_ctrl"  # one of shoutabout.hotkey.HOTKEY_CHOICES
    # "toggle": press to start, press again to stop (the default); "hold": hold to talk.
    mode: Literal["hold", "toggle"] = "toggle"
    overlay: bool = True  # the small mouth at the top of the screen
    # Stop listening after this long. 600 = the hub's own limit for a phone recording.
    max_seconds: int = Field(default=600, ge=5, le=600)

    @field_validator("hotkey")
    @classmethod
    def _known_hotkey(cls, value: str) -> str:
        from shoutabout.hotkey import HOTKEY_CHOICES
        from shoutabout.mac.keys import MAC_HOTKEY_CHOICES  # v2-macos: right_cmd, ...

        key = value.strip().lower()
        if key not in HOTKEY_CHOICES and key not in MAC_HOTKEY_CHOICES:
            choices = MAC_HOTKEY_CHOICES if sys.platform == "darwin" else HOTKEY_CHOICES
            raise ValueError(f"unknown hotkey {value!r}; use one of: {', '.join(choices)}")
        return key


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Config(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    paste: PasteConfig = Field(default_factory=PasteConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    history: LegacyHistoryConfig = Field(default_factory=LegacyHistoryConfig)  # unused
    learn: LegacyLearnConfig = Field(default_factory=LegacyLearnConfig)  # unused
    dictation: DictationConfig = Field(default_factory=DictationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------- load/save


def example_config_text() -> str:
    return resources.files("shoutabout.data").joinpath("config.example.toml").read_text("utf-8")


def example_vocab_text() -> str:
    return resources.files("shoutabout.data").joinpath("vocab.example.txt").read_text("utf-8")


def parse_config(text: str, source: str = "config.toml") -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from exc
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source}: invalid config:\n{exc}") from exc


def load_config(paths: Paths) -> Config:
    """Load config.toml; a missing file means all defaults."""
    if not paths.config_file.exists():
        return Config()
    return parse_config(paths.config_file.read_text("utf-8"), str(paths.config_file))


def load_token(paths: Paths) -> str:
    try:
        token = paths.token_file.read_text("utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"hub token not found at {paths.token_file}; run `python -m shoutabout init`"
        ) from exc
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"hub token in {paths.token_file} is too short (<{MIN_TOKEN_LENGTH})")
    return token


def generate_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits, 43 URL-safe chars


def set_server_port(paths: Paths, port: int) -> bool:
    """Set ``[server] port`` in config.toml, preserving comments. Returns True if changed."""
    if not 1 <= port <= 65535:
        raise ConfigError(f"invalid port {port}")
    text = paths.config_file.read_text("utf-8")
    lines = text.splitlines(keepends=True)
    in_server = False
    server_header = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_server = stripped == "[server]"
            if in_server:
                server_header = i
            continue
        key = stripped.split("=", 1)[0].strip()
        if in_server and key == "port":
            new = f"port = {port}\n"
            if line == new:
                return False
            lines[i] = new
            break
    else:
        if server_header is None:
            lines.append(f"\n[server]\nport = {port}\n")
        else:
            lines.insert(server_header + 1, f"port = {port}\n")
    new_text = "".join(lines)
    parse_config(new_text, str(paths.config_file))  # never write an invalid config
    paths.config_file.write_text(new_text, "utf-8")
    return True


@dataclass
class InitReport:
    created: list[Path]
    existing: list[Path]


def ensure_initialized(paths: Paths, *, rotate_token: bool = False) -> InitReport:
    """Create the config dir, config.toml, vocab.txt and the hub token if missing.

    Idempotent: existing files are never overwritten (except the token with rotate_token).
    """
    created: list[Path] = []
    existing: list[Path] = []
    for d in (paths.home, paths.log_dir):
        d.mkdir(parents=True, exist_ok=True)

    if paths.config_file.exists():
        existing.append(paths.config_file)
    else:
        paths.config_file.write_text(example_config_text(), "utf-8")
        created.append(paths.config_file)

    if paths.vocab_file.exists():
        existing.append(paths.vocab_file)
    else:
        paths.vocab_file.write_text(example_vocab_text(), "utf-8")
        created.append(paths.vocab_file)

    token_ok = False
    if paths.token_file.exists() and not rotate_token:
        try:
            load_token(paths)
            token_ok = True
        except ConfigError:
            token_ok = False
    if token_ok:
        existing.append(paths.token_file)
    else:
        paths.token_file.write_text(generate_token() + "\n", "utf-8")
        created.append(paths.token_file)
    return InitReport(created=created, existing=existing)
