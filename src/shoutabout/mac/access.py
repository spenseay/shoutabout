"""What ShoutAbout needs from macOS, asked for in one place: the setup's first screen
("Allow access", :mod:`shoutabout.setup_flow`), the menu bar's "Allow …" lines and the
notice at start all go through here.

* :func:`status_of` turns a :class:`~shoutabout.mac.permissions.PermissionReport` into plain
  JSON values (it crosses the window link): ``microphone`` ("granted", "denied",
  "restricted", "undetermined" or "unknown"), ``accessibility`` and ``input_monitoring``
  (True, False or None = can't tell) and ``hotkey`` (the hub's hotkey listener is running).
* :func:`rows` is what the setup lists: the microphone and Accessibility. Accessibility is
  pasting *and* the hotkey: with it the event tap hears the key (:mod:`~shoutabout.mac.eventtap`
  makes an ordinary tap), so Input Monitoring is listed only when Accessibility can't be
  read at all (then a listen-only tap is the way to hear the key).
* :class:`HubAccess` (the hub) asks macOS; :class:`RemoteAccess` is the window process's
  handle on it, over the link.

**Which process asks.** macOS keeps privacy permissions per *responsible* app. The hub is
ShoutAbout.app as launchd started it; the window process is the same executable, forked by
the hub, so macOS counts it as the app too. Every request is still made by the hub: it is
the process that records, taps the keyboard and posts Cmd+V, so what it asks for is
exactly what it gets, however the window process was started. Every "Allow" also has a
way out that can't fail silently: System Settings opens at the right pane.

Unknown (None / "unknown") never blocks anything: it means the probe couldn't answer, not
that macOS said no.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

Status = dict[str, Any]

KEYS = ("microphone", "accessibility", "input_monitoring")
NAMES = {
    "microphone": "the microphone",
    "accessibility": "Accessibility",
    "input_monitoring": "Input Monitoring",
}
NOTICE_TITLE = "ShoutAbout needs access"
STATUS_TIMEOUT_S = 3.0


# ------------------------------------------------------------------ pure


def status_of(report: Any, hotkey: bool | None = None) -> Status:
    return {
        "microphone": str(report.microphone),
        "accessibility": report.accessibility,
        "input_monitoring": report.input_monitoring,
        "hotkey": hotkey,
    }


def rows(status: Status) -> list[str]:
    """The permissions the setup lists, in the order to ask for them."""
    out = ["microphone", "accessibility"]
    if status.get("accessibility") is None and status.get("input_monitoring") is False:
        out.append("input_monitoring")
    return out


def granted(key: str, status: Status) -> bool:
    """Allowed (or can't tell, which never blocks)."""
    if key == "microphone":
        return status.get("microphone", "unknown") in ("granted", "unknown")
    return status.get(key) is not False


def missing(status: Status) -> list[str]:
    """The listed permissions still to allow."""
    return [key for key in rows(status) if not granted(key, status)]


def can_hear_hotkey(status: Status) -> bool:
    """Can the event tap hear the hotkey? (:func:`~shoutabout.mac.eventtap.choose_listen_only`
    has a tap to try.)"""
    ax, im = status.get("accessibility"), status.get("input_monitoring")
    return ax is True or im is True or (ax is None and im is None)


def can_paste(status: Status) -> bool:
    return status.get("accessibility") is not False


def can_talk(status: Status) -> bool:
    """The microphone and the hotkey work: "Talk to this Mac" can be tried."""
    return granted("microphone", status) and can_hear_hotkey(status)


def notice(status: Status) -> tuple[str, str] | None:
    """The start-up notification when something is missing (clicking it opens the setup's
    "Allow access" screen), or None."""
    keys = missing(status)
    if not keys:
        return None
    names = [NAMES[k] for k in keys]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return NOTICE_TITLE, f"Click to allow {listed}."


# ------------------------------------------------------------------ the hub's side


class HubAccess:
    """Reads and asks for the permissions in the hub process (see the module docstring).

    ``api`` is :mod:`shoutabout.mac.permissions` (a fake in tests). ``hotkey`` says whether
    the hotkey listener runs; ``on_change`` is called when the answer changed (the hub's
    permission watcher wakes up then, so the hotkey works the moment it is allowed).
    """

    def __init__(
        self,
        *,
        api: Any = None,
        hotkey: Callable[[], bool | None] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        if api is None:
            from shoutabout.mac import permissions as api
        self._api = api
        self.hotkey = hotkey
        self.on_change = on_change
        self._lock = threading.Lock()
        self._asked: set[str] = set()
        self._last: Status | None = None

    def status(self) -> Status:
        hotkey = None
        if self.hotkey is not None:
            try:
                hotkey = self.hotkey()
            except Exception:
                log.debug("couldn't read the hotkey listener's state", exc_info=True)
        now = status_of(self._api.report(), hotkey)
        with self._lock:
            changed = self._last is not None and now != self._last
            self._last = now
        if changed and self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("permission change handler failed")
        return now

    def ask(self, which: str) -> str:
        """One "Allow": macOS's own dialog where it has one, and System Settings at the
        pane otherwise (or as well). Returns "asked" (a dialog), "opened" (System Settings)
        or "granted" (nothing to do)."""
        api = self._api
        if which == "microphone":
            state = api.microphone_status()
            if state == "granted":
                return "granted"
            with self._lock:
                first = which not in self._asked
                self._asked.add(which)
            # Not asked yet: macOS's dialog. Asked already (the dialog may be gone or
            # never have shown) or refused: the switch in System Settings.
            if state == "undetermined" and first and api.request_microphone():
                log.info("asked macOS for the microphone")
                return "asked"
            api.open_settings("microphone")
            return "opened"
        if which == "accessibility":
            if api.accessibility_trusted() is True:
                return "granted"
            # The prompt adds ShoutAbout to the Accessibility list (switched off) and shows
            # macOS's "would like to control this computer" dialog; the pane opens too, so
            # there is always something to click even when macOS shows no dialog.
            api.accessibility_trusted(prompt=True)
            api.open_settings("accessibility")
            log.info("asked macOS for Accessibility")
            return "opened"
        if which == "input_monitoring":
            if api.input_monitoring_granted() is True:
                return "granted"
            api.request_input_monitoring()
            api.open_settings("input_monitoring")
            log.info("asked macOS for Input Monitoring")
            return "opened"
        raise ValueError(f"unknown permission {which!r}")


# ------------------------------------------------------------------ the window's side


class RemoteAccess:
    """:class:`HubAccess` for the window process: the hub answers over the link. If the
    hub can't, "Allow" still opens System Settings from here."""

    def __init__(self, link: Any, *, open_pane: Callable[[str], Any] | None = None) -> None:
        self._link = link
        self._open_pane = open_pane

    def status(self) -> Status | None:
        try:
            value = self._link.call("permissions", timeout=STATUS_TIMEOUT_S)
        except Exception as exc:
            log.debug("couldn't ask the hub about permissions: %s", exc)
            return None
        return value if isinstance(value, dict) else None

    def ask(self, which: str) -> str:
        try:
            return str(self._link.call("allow", which=which))
        except Exception as exc:
            log.warning("the hub couldn't ask for %s (%s); opening System Settings", which, exc)
        open_pane = self._open_pane
        if open_pane is None:
            from shoutabout.mac.permissions import open_settings as open_pane
        open_pane(which)
        return "opened"
