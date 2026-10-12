"""The hub's v2 parts: pairing, per-device tokens, discovery, the ShoutAbout window.

Kept out of :mod:`hub` so the hub's own changes stay small (other work edits it too).
:func:`start` builds everything and hangs it on the server's :class:`HubContext`;
:meth:`V2Hub.stop` undoes it on exit. Integration points with the "voice" work are marked
``# v2-voice:``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shoutabout import __version__
from shoutabout.config import Config, ConfigError, Paths, load_config
from shoutabout.firstrun import InstallSnapshot, is_setup_complete, pick_port, port_is_free

log = logging.getLogger(__name__)

LISTEN_WAIT_S = 30.0
MODEL_INFO_TTL_S = 300.0

Reloader = Callable[[Config, str], bool]


def first_run_port(paths: Paths, cfg: Config, before: InstallSnapshot) -> Config:
    """On a fresh install, move off a port another program already uses (and save it).

    Never on an existing install: its port is in shortcuts, firewall rules and tunnels."""
    if before.existing_install or before.had_flag or is_setup_complete(paths):
        return cfg
    host, port = cfg.server.host, cfg.server.port
    if port_is_free(host, port):
        return cfg
    new = pick_port(port + 1, lambda p: port_is_free(host, p))
    if new is None:
        log.warning("port %d is busy and no free port was found near it", port)
        return cfg
    from shoutabout.config_edit import ConfigEditError, update_config

    try:
        update_config(paths, {"server": {"port": new}})
        cfg = load_config(paths)
    except (ConfigEditError, ConfigError, OSError):
        log.exception("couldn't save the new port")
        return cfg
    log.info("port %d is in use by another program; this install now uses port %d", port, new)
    return cfg


@dataclass
class V2Hub:
    paths: Paths
    services: Any  # app_services.AppServices
    ui: Any = None  # ui_host.UiHost (macOS: mac.ui_bridge.MacUiBridge); None without a tray
    advertiser: Any = None  # discovery.Advertiser (None when [server] discovery = false)
    port: int = 0
    # "section" -> fn(new_config, "section.key") -> True if applied live. The hub registers
    # what it can reload without a restart.
    reloaders: dict[str, Reloader] = field(default_factory=dict)
    _model_info: tuple[float, Any] | None = None

    # ------------------------------------------------------------ discovery

    def announce(self) -> None:
        if self.advertiser is None:
            return
        from shoutabout.discovery import service_info

        try:
            info = service_info(self.services.identity.get(), self.port, __version__)
        except Exception:
            log.exception("couldn't build the network announcement")
            return
        self.advertiser.update(info)

    def announce_when_listening(self, server: Any, stopping: threading.Event) -> None:
        """Announce once uvicorn is listening (so the phone never finds a dead port)."""
        if self.advertiser is None:
            return

        def wait() -> None:
            deadline = time.monotonic() + LISTEN_WAIT_S
            while not stopping.is_set() and time.monotonic() < deadline:
                if getattr(server, "started", False):
                    self.announce()
                    return
                stopping.wait(0.2)

        threading.Thread(target=wait, name="discovery", daemon=True).start()

    # ------------------------------------------------------------ /info

    def model_info(self) -> Any:
        """The model setup for GET /info: what the running hub picked at start
        (``HubState.choice``), else modelpick.resolve(config), cached for a few minutes."""
        running = getattr(self.services.state, "choice", None)
        if running is not None:
            return running
        cached = self._model_info
        now = time.monotonic()
        if cached is not None and now - cached[0] < MODEL_INFO_TTL_S:
            return cached[1]
        try:
            choice = self.services.resolve_model(load_config(self.paths), None)
        except ImportError:  # v2-voice: modelpick not merged yet; /info guesses from config
            choice = None
        self._model_info = (now, choice)
        return choice

    # ------------------------------------------------------------ settings

    def on_settings_saved(self, keys: set[str]) -> set[str]:
        """Hot-reload what can be; return the keys that need a restart."""
        self._model_info = None
        try:
            new_cfg = load_config(self.paths)
        except ConfigError:
            log.exception("the saved config doesn't load")
            return set(keys)
        restart = set()
        for key in sorted(keys):
            reloader = self.reloaders.get(key.split(".", 1)[0])
            try:
                applied = bool(reloader(new_cfg, key)) if reloader is not None else False
            except Exception:
                log.exception("couldn't apply %s live", key)
                applied = False
            if not applied:
                restart.add(key)
        if restart:
            log.info("settings saved; restart needed for %s", ", ".join(sorted(restart)))
        return restart

    def stop(self) -> None:
        if self.ui is not None:
            try:
                self.ui.shutdown(2.0)
            except Exception:
                log.exception("closing the ShoutAbout windows failed")
        if self.advertiser is not None:
            self.advertiser.stop()


def start(
    paths: Paths,
    cfg: Config,
    ctx: Any,
    state: Any,
    *,
    with_ui: bool,
    notify: Callable[[str, str], None],
    recent: Any = None,
    native_prompt: Callable[[Any], None] | None = None,
) -> V2Hub:
    """Build the v2 parts and wire them into ``ctx`` (the server's HubContext)."""
    from shoutabout import netinfo
    from shoutabout.app_services import AppServices
    from shoutabout.devices import DeviceStore
    from shoutabout.identity import IdentityStore
    from shoutabout.pair_requests import PairingManager
    from shoutabout.winstartup import StartupManager

    devices = DeviceStore(paths.devices_file)
    identity = IdentityStore(paths.identity_file)
    pairing = PairingManager(devices)
    # Without a window nobody can say yes on the PC, so don't accept requests at all.
    pairing.enabled = with_ui

    def lan_url() -> str | None:
        return netinfo.lan_url(cfg)

    def open_logs() -> None:
        os.startfile(paths.log_dir)  # type: ignore[attr-defined]  # Windows only

    def copy_hub_token() -> None:
        from shoutabout.config import load_token
        from shoutabout.inject import clipboard

        clipboard.set_text(load_token(paths), exclude_from_history=True)
        notify("Hub token copied", "")

    services = AppServices(
        paths=paths,
        devices=devices,
        identity=identity,
        pairing=pairing,
        state=state,
        recent=recent,
        lan_url=lan_url,
        open_logs=open_logs,
        copy_hub_token=copy_hub_token,
        startup=StartupManager(),
        version=__version__,
    )
    # --- v2-macos: Mac services (hotkey names, LaunchAgent, Finder, pasteboard)
    if sys.platform == "darwin":
        from shoutabout.mac import platform as mac

        mac.adapt_services(services, paths, notify)
    # --- end v2-macos
    advertiser = None
    if getattr(cfg.server, "discovery", True):
        if sys.platform == "darwin":  # v2-macos: dns-sd instead of the Windows DNS-SD API
            advertiser = mac.make_advertiser()
        else:
            from shoutabout.discovery import Advertiser

            advertiser = Advertiser()
    hub = V2Hub(paths=paths, services=services, advertiser=advertiser, port=cfg.server.port)
    services.on_settings_saved = hub.on_settings_saved
    services.on_identity_changed = hub.announce

    ctx.devices = devices
    ctx.pairing = pairing
    ctx.identity = identity
    ctx.lan_url = lan_url
    ctx.model_info = hub.model_info
    ctx.version = __version__

    if with_ui and sys.platform == "darwin":  # v2-macos: the windows run in a child process
        from shoutabout.mac.ui_bridge import MacUiBridge

        hub.ui = MacUiBridge(
            services, on_identity_changed=hub.announce, native_prompt=native_prompt
        )
    elif with_ui:
        from shoutabout.ui_host import UiHost

        hub.ui = UiHost(services)
    return hub
