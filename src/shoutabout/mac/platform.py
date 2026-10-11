"""The glue the shared hub calls on macOS (every call site in hub.py, hub_v2.py,
voice_runtime.py and inject/__init__.py is marked ``# v2-macos``).

* :func:`acquire_single_instance`: one hub per user (an ``flock`` on ``hub.lock``);
* :func:`first_run_defaults`: a new Mac install gets Right ⌘ + press-to-start/stop and
  "Open at login" (the built app only);
* :func:`adapt_services`: the Mac versions of what the windows use (hotkey names, the
  LaunchAgent, Finder, the pasteboard);
* :func:`dictation_factories`: the event tap, the Mac microphone and the Mac overlay for
  :class:`shoutabout.local_dictation.LocalDictation`;
* :class:`MacHub`: the menu bar, the permission lines in it, the startup permission
  requests, the native fallback pairing prompt, and a watcher that turns the hotkey on as
  soon as Accessibility is allowed (no restart needed).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shoutabout.config import Paths

log = logging.getLogger(__name__)

PERMISSION_POLL_S = 2.0
LOCK_FILE = "hub.lock"

PERMISSION_LABELS = {
    "accessibility": "Allow pasting (Accessibility)…",
    "microphone": "Allow the microphone…",
    "input_monitoring": "Allow the shortcut key (Input Monitoring)…",
}


# ------------------------------------------------------------------ process


def acquire_single_instance(paths: Paths) -> Any:
    """An exclusive lock on ``<home>/hub.lock`` (released when the process ends, even if it
    crashes). Returns the open file to keep, or None when another hub holds it."""
    import fcntl

    paths.home.mkdir(parents=True, exist_ok=True)
    handle = open(paths.home / LOCK_FILE, "a+")  # noqa: SIM115 - held for the process life
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def redirect_output(paths: Paths) -> bool:
    """A Finder/launchd start has no terminal: send stray output (tracebacks from
    Cocoa callbacks, library prints) to ``stdout.log`` in the log folder. True if it did."""
    try:
        if sys.stderr is not None and sys.stderr.isatty():
            return False
    except (AttributeError, ValueError):
        pass
    from shoutabout.logging_setup import STD_LOG_FILE_NAME

    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(  # noqa: SIM115 - lives for the whole process
            paths.log_dir / STD_LOG_FILE_NAME, "a", encoding="utf-8", buffering=1
        )
        os.dup2(stream.fileno(), 1)
        os.dup2(stream.fileno(), 2)
    except OSError:
        return False
    sys.stdout = sys.stderr = stream
    return True


# Project folders the vocabulary suggestions may look at on a Mac. Never ~/Documents,
# ~/Desktop or ~/Downloads: macOS guards those, and reading them makes it ask the person
# "ShoutAbout would like to access files in your Documents folder" out of the blue.
MAC_PROJECT_ROOTS = (
    "~/Developer",
    "~/code",
    "~/Code",
    "~/repos",
    "~/projects",
    "~/Projects",
    "~/src",
    "~/git",
    "~/source/repos",
)


def learn_defaults(home: Path | None = None) -> dict[str, Any]:
    """``[learn]`` for a new Mac install: the unguarded project folders that exist, or no
    project scanning at all (the default list would include ~/Documents)."""
    base = home or Path.home()
    found: list[str] = []
    seen: list[Path] = []
    for root in MAC_PROJECT_ROOTS:
        path = base / root[2:]
        if not path.is_dir():
            continue
        if any(os.path.samefile(path, other) for other in seen):
            continue  # ~/code and ~/Code are one folder on a case-insensitive disk
        seen.append(path)
        found.append(root)
    if found:
        return {"project_roots": found}
    return {"seed_projects": False}


def first_run_defaults(paths: Paths, fresh: bool) -> None:
    """A fresh Mac install: Right ⌘, press to start and stop, open at login (built app)."""
    if not fresh:
        return
    from shoutabout.config_edit import update_config
    from shoutabout.mac.keys import DEFAULT_MAC_HOTKEY, DEFAULT_MAC_MODE

    changes: dict[str, dict[str, Any]] = {
        "dictation": {"hotkey": DEFAULT_MAC_HOTKEY, "mode": DEFAULT_MAC_MODE},
        "learn": learn_defaults(),
    }
    try:
        update_config(paths, changes)
        log.info("new Mac install: hotkey %s, %s mode", DEFAULT_MAC_HOTKEY, DEFAULT_MAC_MODE)
    except Exception:
        log.exception("couldn't save the Mac defaults")
    try:
        from shoutabout.mac.startup_mac import MacStartupManager

        startup = MacStartupManager()
        if startup.executable is not None and not startup.state().enabled:
            startup.set_enabled(True)
    except Exception:
        log.exception("couldn't turn on open at login")


# ------------------------------------------------------------------ services


def open_folder(path: Any) -> None:
    subprocess.Popen(["/usr/bin/open", str(path)])


def adapt_services(services: Any, paths: Paths, notify: Callable[[str, str], None]) -> None:
    """Swap the Windows-only service hooks for the Mac ones (in place)."""
    from shoutabout.mac import keys
    from shoutabout.mac.startup_mac import MacStartupManager

    def open_logs() -> None:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        open_folder(paths.log_dir)

    def copy_hub_token() -> None:
        from shoutabout.config import load_token
        from shoutabout.mac.paste import CONCEALED, CocoaPasteboard

        CocoaPasteboard().write_text(load_token(paths), (CONCEALED,))
        notify("Hub token copied", "Clipboard managers are asked not to keep it.")

    services.hotkey_choices = lambda: dict(keys.MAC_HOTKEY_CHOICES)
    services.hint_text = keys.hint_text
    services.startup = MacStartupManager()
    services.open_logs = open_logs
    services.copy_hub_token = copy_hub_token


def make_advertiser() -> Any:
    from shoutabout.mac.discovery_mac import MacAdvertiser

    return MacAdvertiser()


def dictation_factories() -> dict[str, Any]:
    """LocalDictation factories for macOS (empty elsewhere: the Windows defaults)."""
    if sys.platform != "darwin":
        return {}
    from shoutabout.mac.eventtap import MacHotkeyListener
    from shoutabout.mac.mic_mac import MacMicRecorder
    from shoutabout.mac.overlay_mac import create_mac_overlay

    return {
        "listener_factory": MacHotkeyListener,
        "recorder_factory": MacMicRecorder,
        "overlay_factory": create_mac_overlay,
    }


class LoggedPipeline:
    """The pipeline with one log line per local dictation (timings, never text), so the
    first Macs tell us how fast Whisper really is on Apple Silicon."""

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

    def process_audio(self, *args: Any, **kwargs: Any) -> Any:
        from shoutabout.pipeline import format_log_line

        result = self._pipeline.process_audio(*args, **kwargs)
        try:
            log.info("%s", format_log_line(result))
        except Exception:
            log.debug("couldn't log the dictation timings", exc_info=True)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pipeline, name)


def logged_pipeline(get: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap the hub's ``lambda: ctx.pipeline`` for macOS (unchanged elsewhere)."""
    if sys.platform != "darwin":
        return get

    def wrapped() -> Any:
        pipeline = get()
        return LoggedPipeline(pipeline) if pipeline is not None else None

    return wrapped


# ------------------------------------------------------------------ the hub's Mac parts


def permission_items(report: Any) -> list[tuple[int, str]]:
    """Menu lines for missing permissions: (index into PERMISSION_KEYS, label)."""
    missing = report.missing()
    return [
        (PERMISSION_KEYS.index(m), PERMISSION_LABELS[m]) for m in missing if m in PERMISSION_LABELS
    ]


PERMISSION_KEYS = ("accessibility", "microphone", "input_monitoring")


class MacHub:
    """The Mac-only parts of a running hub (main thread for the Cocoa ones)."""

    def __init__(self, paths: Paths, state: Any, notify: Callable[[str, str], None]) -> None:
        self.paths = paths
        self.state = state
        self.notify = notify
        self.menubar: Any = None
        self._stopping = threading.Event()
        self._voice: Any = None

    def build_menubar(self, actions: Any) -> Any:
        from shoutabout.mac.menubar import MacMenuBar
        from shoutabout.mac.permissions import report

        def extra() -> list[tuple[int, str]]:
            return permission_items(report())

        self.menubar = MacMenuBar(
            self.state, actions, extra_items=extra, on_extra=self.open_permission
        )
        return self.menubar

    def open_permission(self, index: int) -> None:
        from shoutabout.mac import permissions

        key = PERMISSION_KEYS[index]
        if key == "microphone" and permissions.microphone_status() == "undetermined":
            permissions.request_microphone()
            return
        if key == "input_monitoring":
            permissions.request_input_monitoring()
        permissions.open_settings(key)

    def request_permissions(self) -> None:
        """At start: let macOS ask for what's missing (each dialog appears once)."""
        from shoutabout.mac import permissions

        now = permissions.report()
        log.info(
            "permissions: microphone=%s, accessibility=%s, input monitoring=%s",
            now.microphone,
            now.accessibility,
            now.input_monitoring,
        )
        if now.microphone == "undetermined":
            permissions.request_microphone()
        if now.accessibility is False:
            permissions.accessibility_trusted(prompt=True)

    def watch_permissions(self, voice: Any) -> None:
        """Turn "Talk to this Mac" on as soon as macOS allows the hotkey listener."""
        self._voice = voice
        threading.Thread(target=self._watch, name="permissions", daemon=True).start()

    def _watch(self) -> None:
        from shoutabout.local_dictation import DictationState
        from shoutabout.mac.permissions import report

        allowed_before = report().can_hear_hotkey
        retried_off = False  # one retry per "allowed" spell when the listener is off
        while not self._stopping.wait(PERMISSION_POLL_S):
            allowed = report().can_hear_hotkey
            newly = allowed and not allowed_before
            allowed_before = allowed
            if not allowed:
                retried_off = False
                continue
            voice = self._voice
            if voice is None or not voice.cfg.dictation.enabled or self.state.paused:
                continue
            dictation = getattr(voice, "dictation", None)
            current = dictation.state if dictation is not None else DictationState.OFF
            if current in (DictationState.LISTENING, DictationState.THINKING):
                if newly:
                    allowed_before = False  # try again on the next round
                continue
            off = current == DictationState.OFF
            if not newly and not (off and not retried_off):
                continue
            retried_off = retried_off or off
            # (Re)start the listener: a tap made before the permission may hear nothing.
            log.info("the hotkey permission is on now; starting talk to this Mac")
            try:
                voice.reload_dictation(voice.cfg)
            except Exception:
                log.exception("couldn't start talk to this Mac")

    def native_prompt(self, prompt: Any, pairing: Any) -> None:
        """The fallback pairing prompt: a plain macOS alert (main thread)."""
        from shoutabout.mac.cocoa import call_on_main

        def ask() -> None:
            from AppKit import NSAlert, NSApplication

            alert = NSAlert.alloc().init()
            alert.setMessageText_(f"{prompt.device_name} wants to dictate to this Mac.")
            alert.setInformativeText_(
                f"Code {prompt.code}. Allow it if your iPhone shows the same code."
            )
            alert.addButtonWithTitle_("Allow")
            alert.addButtonWithTitle_("Don't Allow")
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            answer = alert.runModal()
            allowed = int(answer) == 1000  # NSAlertFirstButtonReturn
            try:
                if allowed:
                    pairing.approve(prompt.request_id)
                else:
                    pairing.deny(prompt.request_id)
            except Exception as exc:
                log.warning("couldn't answer the pairing request: %s", exc)

        call_on_main(ask)

    def stop(self) -> None:
        self._stopping.set()
