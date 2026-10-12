"""The glue the shared hub calls on macOS (every call site in hub.py, hub_v2.py,
voice_runtime.py and inject/__init__.py is marked ``# v2-macos``).

* :func:`acquire_single_instance`: one hub per user (an ``flock`` on ``hub.lock``);
* :func:`first_run_defaults`: a new Mac install gets Right ⌘ + press-to-start/stop and
  "Open at login" (the built app only);
* :func:`adapt_services`: the Mac versions of what the windows use (hotkey names, the
  LaunchAgent, Finder, the pasteboard);
* :func:`dictation_factories`: the event tap, the Mac microphone and the Mac overlay for
  :class:`shoutabout.local_dictation.LocalDictation`;
* :class:`MacHub`: the menu bar, the permission lines in it, the one place that asks macOS
  for permissions (:class:`shoutabout.mac.access.HubAccess`, also behind the setup's
  "Allow access" screen), the launch check (the microphone or Accessibility not allowed:
  "Allow access" opens at every start, and when ShoutAbout is opened again while it runs,
  plus a notification whose click opens it too), the native fallback pairing prompt, and
  a watcher that (re)starts the hotkey listener as soon as a permission for it is allowed
  (no restart needed).
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


# Project folders "Suggest from my project folders" (Teach words) looks at on a Mac. Never
# ~/Documents, ~/Desktop or ~/Downloads: macOS guards those, and reading them makes it ask
# the person "ShoutAbout would like to access files in your Documents folder" out of the blue.
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


def mac_project_roots(home: Path | None = None) -> list[Path]:
    """The unguarded project folders that exist (``[learn] project_roots`` left empty)."""
    base = home or Path.home()
    found: list[Path] = []
    for root in MAC_PROJECT_ROOTS:
        path = base / root[2:]
        if not path.is_dir():
            continue
        try:
            if any(os.path.samefile(path, other) for other in found):
                continue  # ~/code and ~/Code are one folder on a case-insensitive disk
        except OSError:
            continue
        found.append(path)
    return found


def first_run_defaults(paths: Paths, fresh: bool) -> None:
    """A fresh Mac install: Right ⌘, press to start and stop, open at login (built app).
    (Project folders are never scanned unless the person asks: ``[learn] seed_projects``
    is off everywhere.)"""
    if not fresh:
        return
    from shoutabout.config_edit import update_config
    from shoutabout.mac.keys import DEFAULT_MAC_HOTKEY, DEFAULT_MAC_MODE

    changes: dict[str, dict[str, Any]] = {
        "dictation": {"hotkey": DEFAULT_MAC_HOTKEY, "mode": DEFAULT_MAC_MODE},
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
        notify("Hub token copied", "")

    def missing_permissions() -> list[str]:  # Home's status line (and its "Allow")
        from shoutabout.mac import access
        from shoutabout.mac.permissions import report

        return access.missing(access.status_of(report()))  # what "Allow access" lists

    services.hotkey_choices = lambda: dict(keys.MAC_HOTKEY_CHOICES)
    services.hint_text = keys.hint_text
    services.startup = MacStartupManager()
    services.open_logs = open_logs
    services.copy_hub_token = copy_hub_token
    services.missing_permissions = missing_permissions


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
    """Menu lines for missing permissions: (index into PERMISSION_KEYS, label). The
    microphone is listed before macOS has asked too (the line asks)."""
    missing = report.missing()
    if report.microphone == "undetermined" and "microphone" not in missing:
        missing = ["microphone", *missing]
    return [
        (PERMISSION_KEYS.index(m), PERMISSION_LABELS[m]) for m in missing if m in PERMISSION_LABELS
    ]


PERMISSION_KEYS = ("accessibility", "microphone", "input_monitoring")


def _hotkey_grants(report: Any) -> tuple[bool, bool]:
    """The permissions that let the event tap hear the hotkey: (Accessibility, Input
    Monitoring)."""
    return report.accessibility is True, report.input_monitoring is True


def _in_background(work: Callable[[], None], name: str) -> None:
    threading.Thread(target=work, name=name, daemon=True).start()


_NOTICE_DELEGATE: Any = None


def _notice_delegate_class() -> Any:
    """The NSUserNotificationCenter delegate (defined once per process): banners show even
    while ShoutAbout is in front, and a click on one goes to :meth:`MacHub.notice_clicked`."""
    global _NOTICE_DELEGATE
    if _NOTICE_DELEGATE is None:
        import objc
        from Foundation import NSObject

        try:
            protocols = [objc.protocolNamed("NSUserNotificationCenterDelegate")]
        except Exception:
            protocols = []

        class ShoutAboutNoticeDelegate(NSObject, protocols=protocols):  # type: ignore[call-arg]
            def userNotificationCenter_shouldPresentNotification_(
                self, _center: Any, _note: Any
            ) -> bool:
                return True

            def userNotificationCenter_didActivateNotification_(
                self, _center: Any, _note: Any
            ) -> None:
                owner = getattr(self, "owner", None)
                if owner is not None:
                    owner.notice_clicked()

        _NOTICE_DELEGATE = ShoutAboutNoticeDelegate
    return _NOTICE_DELEGATE


class MacHub:
    """The Mac-only parts of a running hub (main thread for the Cocoa ones)."""

    def __init__(self, paths: Paths, state: Any, notify: Callable[[str, str], None]) -> None:
        from shoutabout.mac.access import HubAccess

        self.paths = paths
        self.state = state
        self.notify = notify
        self.menubar: Any = None
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._voice: Any = None
        self._ui: Any = None
        self._delegate: Any = None
        # The watcher's memory (its thread only): the hotkey permissions last seen, and
        # whether it already retried a listener that is off although allowed.
        self._grants = (False, False)
        self._retried_off = False
        self._told = True  # the notice about missing permissions was posted (or not needed)
        self._setup_seen = False
        self.access = HubAccess(hotkey=self._hotkey_on, on_change=self._wake.set)

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
        """A menu line "Allow …": macOS's dialog, or System Settings at the pane."""
        self.access.ask(PERMISSION_KEYS[index])
        self._wake.set()

    # -- start-up

    def start_permissions(self, voice: Any, v2: Any, *, first_run: bool) -> None:
        """Hand the setup window its "Allow access" screen, check what macOS allows, and
        start the watcher.

        Every start: when the microphone or Accessibility (pasting and the hotkey) isn't
        allowed, ShoutAbout can't work, so "Allow access" opens by itself, with the
        notification as a second signal (:meth:`check_access`). A first run's setup window
        opens on that screen anyway; the notification then waits until it is gone."""
        self._voice = voice
        self._ui = getattr(v2, "ui", None)
        services = getattr(v2, "services", None)
        if services is not None:
            services.permissions = self.access
        self.listen_for_clicks()
        now = self.access.status()
        log.info(
            "permissions: microphone=%s, accessibility=%s, input monitoring=%s",
            now["microphone"],
            now["accessibility"],
            now["input_monitoring"],
        )
        if first_run and self._ui is not None:
            self._told = False  # said once the setup window is gone (tell_after_setup)
        else:
            self.check_access(now)
        self.watch_permissions(voice)

    def check_access(self, status: Any = None) -> bool:
        """The launch check: something ShoutAbout needs isn't allowed -> the setup's "Allow
        access" screen, and the notification. True if something is missing."""
        from shoutabout.mac import access

        status = status if status is not None else self.access.status()
        if not access.missing(status):
            self._told = True
            return False
        log.info("missing permissions at start: %s", ", ".join(access.missing(status)))
        self.open_allow()
        self.tell_missing(status)
        return True

    def open_allow(self, ui: Any = None) -> bool:
        """Open the setup's "Allow access" screen (off this thread: it may start the window
        process). False without windows."""
        ui = ui if ui is not None else self._ui
        if ui is None:
            return False
        _in_background(lambda: ui.open_setup("allow"), "open-allow")
        return True

    def reopened(self, ui: Any) -> None:
        """ShoutAbout opened again while it runs (Finder, Spotlight, Launchpad, the Dock):
        the ShoutAbout window at Home, with "Allow access" in front of it while the
        microphone or Accessibility isn't allowed. (Called off the main thread.)"""
        from shoutabout.mac import access

        try:
            missing = access.missing(self.access.status())
        except Exception:
            log.exception("couldn't read the permissions")
            missing = []
        ui.open_window("home")
        if missing:
            log.info("opened again with permissions missing: %s", ", ".join(missing))
            ui.open_setup("allow")

    def tell_missing(self, status: Any = None) -> bool:
        """One notification when a permission is missing ("Click to allow …"; the click
        opens "Allow access"). True if it posted one."""
        from shoutabout.mac import access

        self._told = True
        words = access.notice(status if status is not None else self.access.status())
        if words is None:
            return False
        self.notify(*words)
        return True

    def tell_after_setup(self) -> bool:
        """A first run: once the setup window has come and gone, say (once) what is still
        missing, so a closed setup never leaves the hotkey silently deaf."""
        ui = self._ui
        if self._told or ui is None:
            return False
        if getattr(ui, "running", False):
            self._setup_seen = True
            return False
        if not self._setup_seen:
            return False
        return self.tell_missing()

    def listen_for_clicks(self) -> None:
        """Clicking a ShoutAbout notification while a permission is missing opens the
        setup's "Allow access" screen (every such notification is about one)."""
        from shoutabout.mac.cocoa import call_on_main

        def install() -> None:
            from Foundation import NSUserNotificationCenter

            center = NSUserNotificationCenter.defaultUserNotificationCenter()
            if center is None:  # no bundle (run from source): osascript notifications
                return
            delegate = _notice_delegate_class().alloc().init()
            delegate.owner = self
            self._delegate = delegate  # the center doesn't keep its delegate alive
            center.setDelegate_(delegate)

        call_on_main(install)

    def notice_clicked(self) -> None:
        from shoutabout.mac import access

        ui = self._ui
        if ui is None:
            return
        try:
            if not access.missing(self.access.status()):
                return
        except Exception:
            log.exception("couldn't read the permissions")
            return
        self.open_allow(ui)

    # -- the hotkey watcher

    def _hotkey_on(self) -> bool | None:
        from shoutabout.local_dictation import DictationState

        dictation = getattr(self._voice, "dictation", None)
        if dictation is None:
            return None
        return dictation.state != DictationState.OFF

    def watch_permissions(self, voice: Any) -> None:
        """(Re)start "Talk to this Mac" as soon as macOS allows the hotkey listener."""
        from shoutabout.mac.permissions import report

        self._voice = voice
        self._grants = _hotkey_grants(report())
        threading.Thread(target=self._watch, name="permissions", daemon=True).start()

    def _watch(self) -> None:
        from shoutabout.mac.permissions import report

        while not self._stopping.is_set():
            # Every few seconds, or at once when the setup window saw a change.
            self._wake.wait(PERMISSION_POLL_S)
            self._wake.clear()
            if self._stopping.is_set():
                return
            try:
                self.check_hotkey(report())
                self.tell_after_setup()
            except Exception:
                log.exception("the permission watcher failed")

    def check_hotkey(self, now: Any) -> bool:
        """One look at the permissions. Restart the hotkey listener when a permission that
        lets it hear the key was just turned on (a tap made before that may hear nothing,
        e.g. a listen-only tap made before Input Monitoring), or once when it is off although
        allowed. True if it restarted it."""
        from shoutabout.local_dictation import DictationState

        before, grants = self._grants, _hotkey_grants(now)
        newly = any(g and not b for g, b in zip(grants, before, strict=True))
        self._grants = grants
        if not now.can_hear_hotkey:
            self._retried_off = False
            return False
        voice = self._voice
        if voice is None or not voice.cfg.dictation.enabled or self.state.paused:
            return False
        dictation = getattr(voice, "dictation", None)
        current = dictation.state if dictation is not None else DictationState.OFF
        if current in (DictationState.LISTENING, DictationState.THINKING):
            if newly:
                self._grants = before  # not mid-dictation: try again on the next round
            return False
        off = current == DictationState.OFF
        if not newly and not (off and not self._retried_off):
            return False
        self._retried_off = self._retried_off or off
        log.info("the hotkey permission is on now; starting talk to this Mac")
        try:
            voice.reload_dictation(voice.cfg)
        except Exception:
            log.exception("couldn't start talk to this Mac")
        return True

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
        self._wake.set()
