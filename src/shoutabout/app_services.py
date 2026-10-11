"""Everything the setup and settings windows need from the rest of the app, in one place.

The windows never import the hub, the engine or the v2-voice modules directly: they get an
:class:`AppServices`. The hub builds one with the live objects (pairing manager, device
store, hub state, a hot-reload hook); the ``setup``/``settings`` CLI commands build one
without a running hub; tests and screenshots pass fakes.

Integration points with the parallel "voice" work are marked ``# v2-voice:``. Each default
imports its module lazily and degrades gracefully when it isn't there yet, so this branch
works (and its tests pass) before the merge.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from shoutabout.config import Config, Paths, load_config
from shoutabout.devices import DeviceStore
from shoutabout.identity import IdentityStore

log = logging.getLogger(__name__)

# Labels used until shoutabout.local_dictation (v2-voice) is merged.
FALLBACK_HOTKEYS: dict[str, str] = {
    "right_ctrl": "Right Ctrl",
    "right_alt": "Right Alt",
    "f8": "F8",
}
DEFAULT_HOTKEY = "right_ctrl"
DEFAULT_MODE = "toggle"
TIER_NAMES = {"best": "Best", "balanced": "Balanced", "light": "Light"}


# ---------------------------------------------------------- v2-voice defaults


def default_detect_hardware(*, refresh: bool = False) -> Any:
    # v2-voice: shoutabout.hardware.detect(*, refresh=False) -> HardwareInfo
    from shoutabout import hardware  # type: ignore[attr-defined]

    return hardware.detect(refresh=refresh)


def default_resolve_model(cfg: Config, hw: Any = None) -> Any:
    # v2-voice: shoutabout.modelpick.resolve(cfg, hw=None) -> ModelChoice (config wins)
    from shoutabout import modelpick  # type: ignore[attr-defined]

    return modelpick.resolve(cfg, hw)


def default_auto_model(hw: Any) -> Any:
    # v2-voice: shoutabout.modelpick.choose(hw) -> ModelChoice (the automatic pick)
    from shoutabout import modelpick  # type: ignore[attr-defined]

    return modelpick.choose(hw)


def default_tier_choice(tier: str, hw: Any) -> Any:
    """The ModelChoice for a forced quality tier (Settings > Quality override).

    v2-voice: uses ``modelpick.choose_tier(tier, hw)`` if modelpick provides it; otherwise
    None, and Settings offers only "Automatic". Open question for the merge.
    """
    from shoutabout import modelpick  # type: ignore[attr-defined]

    fn = getattr(modelpick, "choose_tier", None)
    return fn(tier, hw) if callable(fn) else None


def default_model_ready(stt_model: str) -> bool:
    # v2-voice: shoutabout.modelstore.is_ready(stt_model) -> bool
    from shoutabout import modelstore  # type: ignore[attr-defined]

    return bool(modelstore.is_ready(stt_model))


def default_download_model(
    stt_model: str, *, progress: Callable[[int, int], None], cancel: threading.Event
) -> Any:
    # v2-voice: shoutabout.modelstore.download(stt_model, *, progress=cb(done, total),
    #                                          cancel=threading.Event()) -> Path
    from shoutabout import modelstore  # type: ignore[attr-defined]

    return modelstore.download(stt_model, progress=progress, cancel=cancel)


def default_hotkey_choices() -> dict[str, str]:
    # v2-voice: shoutabout.local_dictation.HOTKEY_CHOICES
    try:
        from shoutabout import local_dictation  # type: ignore[attr-defined]

        choices = dict(local_dictation.HOTKEY_CHOICES)
        if choices:
            return choices
    except Exception:
        log.debug("local dictation isn't available; using the built-in hotkey labels")
    return dict(FALLBACK_HOTKEYS)


def default_hint_text(cfg: Any) -> str:
    # v2-voice: shoutabout.local_dictation.hint_text(cfg) -> "Press Right Ctrl to talk, …"
    try:
        from shoutabout import local_dictation  # type: ignore[attr-defined]

        return str(local_dictation.hint_text(cfg))
    except Exception:
        view = dictation_view(cfg)
        label = default_hotkey_choices().get(view.hotkey, view.hotkey)
        if not view.enabled:
            return "Talk to this PC is off"
        return f"Press {label} to talk" if view.mode == "toggle" else f"Hold {label} to talk"


def default_copy_text(text: str) -> None:
    """History > Copy: put the text on the clipboard (kept out of clipboard history)."""
    from shoutabout.inject import clipboard

    clipboard.set_text(text, exclude_from_history=True)


def default_has_microphone() -> bool | None:
    """Is a microphone plugged in? (Windows' own count; nothing is opened.) None = unknown."""
    try:
        from shoutabout.mic import has_input_device

        return has_input_device()
    except Exception:
        log.debug("couldn't ask Windows about microphones", exc_info=True)
        return None


# ------------------------------------------------------------------ views


@dataclass(frozen=True)
class DictationView:
    enabled: bool = True
    hotkey: str = DEFAULT_HOTKEY
    mode: str = DEFAULT_MODE


def dictation_view(cfg: Any) -> DictationView:
    """``[dictation]`` as plain values, with the documented defaults when the schema (or the
    section) isn't there. v2-voice: Config.dictation."""
    section = getattr(cfg, "dictation", None)
    if section is None:
        return DictationView()
    mode = str(getattr(section, "mode", DEFAULT_MODE) or DEFAULT_MODE)
    return DictationView(
        enabled=bool(getattr(section, "enabled", True)),
        hotkey=str(getattr(section, "hotkey", DEFAULT_HOTKEY) or DEFAULT_HOTKEY),
        mode=mode if mode in ("hold", "toggle") else DEFAULT_MODE,
    )


def talk_instruction(hotkey_label: str, mode: str) -> str:
    """The one-line how-to on the "Talk to this PC" card."""
    if mode == "toggle":
        return f"Press {hotkey_label}, talk, press it again."
    return f"Hold {hotkey_label}, talk, let go."


def gpu_name(hw: Any) -> str | None:
    best = getattr(hw, "best_gpu", None)
    name = getattr(best, "name", None)
    return str(name) if name else None


# --------------------------------------------------------------- services


@dataclass
class AppServices:
    paths: Paths
    devices: DeviceStore
    identity: IdentityStore
    pairing: Any = None  # pair_requests.PairingManager; None when no hub runs in-process
    state: Any = None  # state.HubState of the running hub (None standalone)
    detect_hardware: Callable[..., Any] = default_detect_hardware
    resolve_model: Callable[..., Any] = default_resolve_model
    auto_model: Callable[[Any], Any] = default_auto_model
    tier_choice: Callable[[str, Any], Any] = default_tier_choice
    model_ready: Callable[[str], bool] = default_model_ready
    download_model: Callable[..., Any] = default_download_model
    hotkey_choices: Callable[[], dict[str, str]] = default_hotkey_choices
    hint_text: Callable[[Any], str] = default_hint_text
    # The hub's hot-reload hook: called with the changed "section.key" names after a
    # settings change was saved; returns those it could NOT apply live (need a restart).
    on_settings_saved: Callable[[set[str]], set[str]] | None = None
    lan_url: Callable[[], str | None] | None = None
    on_identity_changed: Callable[[], None] | None = None  # re-announce on the network
    open_teach: Callable[[], None] | None = None
    open_logs: Callable[[], None] | None = None
    copy_hub_token: Callable[[], None] | None = None
    open_history: Callable[[], None] | None = None  # the History window (needs the UI host)
    has_microphone: Callable[[], bool | None] = default_has_microphone
    # History: the running hub hands over its log (so clearing takes the same lock as
    # writing) and the failed-recordings folder; standalone, the files are used directly.
    history_log: Any = None  # history.HistoryLog
    failed: Any = None  # failed_audio.FailedRecordings
    copy_text: Callable[[str], None] | None = default_copy_text  # History > Copy
    # History > Try again: transcribe a kept recording (by id); returns the text. Only with
    # a running hub (it has the speech engine).
    retry_failed: Callable[[str], str] | None = None
    startup: Any = None  # winstartup.StartupManager
    version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --------------------------------------------------------------- config

    def config(self) -> Config:
        """config.toml as it is now on disk (defaults if missing or unreadable)."""
        try:
            return load_config(self.paths)
        except Exception:
            log.exception("couldn't read config.toml; showing defaults")
            return Config()

    def save_settings(self, changes: Mapping[str, Mapping[str, Any]]) -> set[str]:
        """Write ``changes`` to config.toml (comments kept) and hot-reload what the hub can.

        Returns the "section.key" names that need a restart. Raises
        :class:`~shoutabout.config_edit.ConfigEditError` if the change isn't valid.
        """
        from shoutabout.config_edit import update_config

        with self._lock:
            changed = update_config(self.paths, changes)
        keys = {f"{s}.{k}" for s, values in changes.items() for k in values}
        if not changed:
            return set()
        if self.on_settings_saved is None:  # no hub in this process: it applies on restart
            return keys
        try:
            return set(self.on_settings_saved(keys))
        except Exception:
            log.exception("hot-reloading settings failed")
            return keys

    # -------------------------------------------------------------- history

    def history_enabled(self) -> bool:
        return bool(getattr(getattr(self.config(), "history", None), "enabled", False))

    def read_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        from shoutabout.history import read_history

        path = getattr(self.history_log, "path", None) or self.paths.history_file
        return read_history(path, limit)

    def failed_recordings(self) -> Any:
        if self.failed is None:
            from shoutabout.failed_audio import FailedRecordings

            self.failed = FailedRecordings(self.paths.failed_dir, enabled=self.history_enabled())
        return self.failed

    def clear_history(self) -> None:
        """Delete every dictation record and every kept recording. Raises OSError."""
        from shoutabout.history import clear_history_file

        if self.history_log is not None:
            self.history_log.clear()
        else:
            clear_history_file(self.paths.history_file)
        self.failed_recordings().clear()

    # -------------------------------------------------------------- pairing

    def legacy_qr_text(self) -> str:
        """The v1 pairing payload (``python -m shoutabout pair``): LAN URL + hub token."""
        from shoutabout import netinfo, pairing
        from shoutabout.config import load_token

        url = self.lan_url() if self.lan_url is not None else netinfo.lan_url(self.config())
        if not url:
            raise pairing.PairingError("This PC has no home-network address right now.")
        payload = pairing.build_payload(lan_url=url, token=load_token(self.paths))
        return pairing.to_json(payload)
