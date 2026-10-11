"""The settings page, as plain logic (no Tk): what to show and how each change is saved.

Every change is saved the moment it's made (no Save button): config values through
:mod:`config_edit` (comments in config.toml are kept), the computer name in identity.json,
phones in devices.json, start-up in the Run key. The hub then hot-reloads what it can
(dictation, cleanup, history, the name); for the rest the page says "Restart ShoutAbout to
apply".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from shoutabout.app_services import TIER_NAMES, AppServices, dictation_view, gpu_name

log = logging.getLogger(__name__)

QUALITY_CHOICES = ("auto", "best", "balanced", "light")
CLEANUP_CHOICES = {"auto": "Automatic", "on": "On", "off": "Off"}
MODE_CHOICES = {"toggle": "Press to start and stop", "hold": "Hold to talk"}
NO_MIC_NOTE = "No microphone found on this PC. Use your iPhone, or plug in a mic."
CLEANUP_NOTE = "Fixes punctuation and filler words with a small AI model on this PC (Ollama)."
HISTORY_LABEL = "Keep a history of what I dictate"
HISTORY_NOTE = (
    "Saved only on this PC, so you can look back, or try again when a recording doesn't go "
    "through. Off by default."
)
RESTART_NOTE = "Saved. Restart ShoutAbout to apply."
SAVED_NOTE = "Saved."


# ----------------------------------------------------------------- pure copy


def last_used_text(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return "Not used yet"
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return "Not used yet"
    now = now or datetime.now().astimezone()
    if when.tzinfo is None:
        when = when.astimezone()
    delta = now - when
    if delta < timedelta(minutes=1):
        return "Used just now"
    if delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() // 60)
        return f"Used {minutes} minute{'s' if minutes != 1 else ''} ago"
    local = when.astimezone(now.tzinfo)
    if local.date() == now.date():
        return f"Used today at {local.hour}:{local.minute:02d}"
    if local.date() == (now - timedelta(days=1)).date():
        return "Used yesterday"
    return f"Used on {local.day} {local:%b}"


def hardware_label(hw: Any) -> str:
    gpu = gpu_name(hw)
    if gpu:
        return gpu
    cpu = getattr(hw, "cpu_name", None)
    return "CPU" if not cpu else f"CPU: {cpu}"


def automatic_label(choice: Any, hw: Any) -> str:
    """ "Automatic: Best (NVIDIA GeForce RTX 4080 SUPER)"."""
    if choice is None:
        return "Automatic"
    tier = getattr(choice, "tier", None)
    name = TIER_NAMES.get(str(tier), str(getattr(choice, "label", "") or "").strip() or "Best")
    return f"Automatic: {name} ({hardware_label(hw)})"


def download_note(choice: Any, ready: bool | None) -> str:
    if choice is None or ready is not False:
        return ""
    mb = getattr(choice, "download_mb", None)
    size = f"a {int(mb):,} MB" if isinstance(mb, int | float) and mb > 0 else "a"
    return f"Needs {size} download. ShoutAbout gets it next time it starts."


def cleanup_choice(raw: Any) -> str:
    if raw is True:
        return "on"
    if raw is False:
        return "off"
    return "auto"


def cleanup_value(choice: str) -> Any:
    return {"on": True, "off": False}.get(choice, "auto")


# ------------------------------------------------------------------ the page


@dataclass
class DeviceRow:
    key: str
    name: str
    last_used: str


@dataclass
class SettingsView:
    pc_enabled: bool = True
    hotkey: str = "right_ctrl"
    hotkeys: dict[str, str] = field(default_factory=dict)
    mode: str = "toggle"
    mic_missing: bool = False
    devices: list[DeviceRow] = field(default_factory=list)
    quality: str = "auto"  # auto | best | balanced | light | custom
    quality_options: list[str] = field(default_factory=lambda: ["auto"])
    automatic_label: str = "Automatic"
    custom_label: str = ""
    quality_note: str = ""
    cleanup: str = "auto"
    history_on: bool = False
    name: str = ""
    startup_on: bool = False
    startup_editable: bool = False
    startup_note: str = ""
    version: str = ""
    loading_quality: bool = True


class SettingsModel:
    def __init__(self, services: AppServices) -> None:
        self.svc = services
        self.hw: Any = None
        self.auto: Any = None
        self.tiers: dict[str, Any] = {}
        self.startup_state: Any = None  # winstartup.StartupState, read off the UI thread
        self.message = ""
        self.error = False

    # -------------------------------------------------------------- reading

    def mic_missing(self) -> bool:
        try:
            return self.svc.has_microphone() is False
        except Exception:
            return False

    def view(self) -> SettingsView:
        """Everything cheap to read (config, phones, name, start-up). Fast."""
        svc = self.svc
        cfg = svc.config()
        dv = dictation_view(cfg)
        ident = svc.identity.get()
        devices = [
            DeviceRow(d.key, d.name, last_used_text(d.last_seen)) for d in svc.devices.list()
        ]
        startup = self.startup_state
        view = SettingsView(
            pc_enabled=dv.enabled,
            hotkey=dv.hotkey,
            hotkeys=svc.hotkey_choices(),
            mode=dv.mode,
            mic_missing=self.mic_missing(),
            devices=devices,
            cleanup=cleanup_choice(getattr(cfg.cleanup, "enabled", "auto")),
            history_on=bool(getattr(getattr(cfg, "history", None), "enabled", False)),
            name=ident.name,
            startup_on=bool(startup and startup.enabled),
            startup_editable=bool(startup and startup.editable),
            startup_note=startup.note if startup else "",
            version=svc.version,
        )
        self._fill_quality(view, cfg)
        return view

    def load_models(self) -> None:
        """The slow part (hardware detection, per-tier choices, the start-up state: it may
        ask ``schtasks``). Call off the UI thread."""
        self._read_startup()
        try:
            self.hw = self.svc.detect_hardware()
        except Exception as exc:
            log.warning("couldn't detect hardware for settings: %s", exc)
            self.hw = None
        try:
            self.auto = self.svc.auto_model(self.hw) if self.hw is not None else None
        except Exception as exc:
            log.warning("couldn't work out the automatic model: %s", exc)
            self.auto = None
        tiers: dict[str, Any] = {}
        for tier in ("best", "balanced", "light"):
            try:
                choice = self.svc.tier_choice(tier, self.hw)
            except Exception:
                choice = None
            if choice is not None:
                tiers[tier] = choice
        self.tiers = tiers

    def _read_startup(self) -> None:
        if self.svc.startup is None:
            self.startup_state = None
            return
        try:
            self.startup_state = self.svc.startup.state()
        except Exception:
            log.exception("couldn't read the start-up setting")
            self.startup_state = None

    def _fill_quality(self, view: SettingsView, cfg: Any) -> None:
        stt = cfg.stt
        explicit = {"model": stt.model, "device": stt.device, "compute_type": stt.compute_type}
        view.loading_quality = self.hw is None and self.auto is None and not self.tiers
        view.automatic_label = automatic_label(self.auto, self.hw)
        view.quality_options = ["auto", *self.tiers]
        if all(str(v) == "auto" for v in explicit.values()):
            view.quality = "auto"
        else:
            view.quality = "custom"
            for tier, choice in self.tiers.items():
                if (
                    getattr(choice, "stt_model", None) == stt.model
                    and getattr(choice, "device", None) == stt.device
                    and getattr(choice, "compute_type", None) == stt.compute_type
                ):
                    view.quality = tier
            if view.quality == "custom":
                view.custom_label = f"Custom: {stt.model} on {stt.device} (config.toml)"
        selected = self.auto if view.quality == "auto" else self.tiers.get(view.quality)
        view.quality_note = download_note(selected, self._ready(selected))

    def _ready(self, choice: Any) -> bool | None:
        model = getattr(choice, "stt_model", None)
        if not model:
            return None
        try:
            return bool(self.svc.model_ready(model))
        except Exception:
            return None

    # -------------------------------------------------------------- changes

    def _save(self, changes: dict[str, dict[str, Any]]) -> bool:
        from shoutabout.config_edit import ConfigEditError

        try:
            restart = self.svc.save_settings(changes)
        except (ConfigEditError, OSError) as exc:
            log.warning("couldn't save settings: %s", exc)
            self.message, self.error = "Couldn't save that. Is config.toml valid?", True
            return False
        self.message = RESTART_NOTE if restart else SAVED_NOTE
        self.error = False
        return True

    def set_pc_enabled(self, on: bool) -> bool:
        return self._save({"dictation": {"enabled": bool(on)}})

    def set_hotkey(self, hotkey: str) -> bool:
        if hotkey not in self.svc.hotkey_choices():
            return False
        return self._save({"dictation": {"hotkey": hotkey}})

    def set_mode(self, mode: str) -> bool:
        if mode not in MODE_CHOICES:
            return False
        return self._save({"dictation": {"mode": mode}})

    def set_quality(self, quality: str) -> bool:
        if quality == "auto":
            values = {"model": "auto", "device": "auto", "compute_type": "auto"}
        else:
            choice = self.tiers.get(quality)
            if choice is None:
                return False
            values = {
                "model": str(choice.stt_model),
                "device": str(choice.device),
                "compute_type": str(choice.compute_type),
            }
        return self._save({"stt": values})

    def set_cleanup(self, choice: str) -> bool:
        if choice not in CLEANUP_CHOICES:
            return False
        return self._save({"cleanup": {"enabled": cleanup_value(choice)}})

    def set_history(self, on: bool) -> bool:
        """[history] enabled: the hub applies it at once (no restart)."""
        return self._save({"history": {"enabled": bool(on)}})

    def rename(self, name: str) -> bool:
        try:
            self.svc.identity.rename(name)
        except (ValueError, OSError) as exc:
            self.message, self.error = str(exc) or "Couldn't save the name.", True
            return False
        hook = self.svc.on_identity_changed
        if hook is not None:
            try:
                hook()
            except Exception:
                log.exception("couldn't announce the new name")
        self.message, self.error = SAVED_NOTE, False
        return True

    def remove_device(self, key: str) -> bool:
        try:
            removed = self.svc.devices.remove(key)
        except OSError:
            self.message, self.error = "Couldn't remove that phone.", True
            return False
        self.message, self.error = ("Removed. That phone can't dictate here any more.", False)
        return removed

    def set_startup(self, on: bool) -> bool:
        if self.svc.startup is None:
            return False
        try:
            self.startup_state = self.svc.startup.set_enabled(on)
        except (PermissionError, OSError) as exc:
            log.warning("couldn't change start with Windows: %s", exc)
            self.message, self.error = "Couldn't change that.", True
            return False
        self.message, self.error = SAVED_NOTE, False
        return True
