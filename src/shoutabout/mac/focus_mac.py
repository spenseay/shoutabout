"""The macOS focus probe: is there a text box to paste into? (Accessibility API.)

The focused element comes from the system-wide accessibility object
(``AXFocusedUIElement``); its role (``AXTextField``, ``AXTextArea``, ``AXComboBox``,
``AXSearchField``...), whether its ``AXValue`` can be set, whether it has a text selection
(``AXSelectedTextRange``) and, in browsers, whether it sits inside an editable area
(``AXEditableAncestor``: ``contenteditable`` in Gmail, Google Docs, Slack) decide it.
Terminals (Terminal, iTerm2, kitty, WezTerm, Alacritty, Ghostty, Warp...) always count as
text. The rules (:func:`classify_mac`) are pure and unit-tested; the calls go through a
tiny facade so tests use a fake.

Best effort, like the Windows probe: when macOS can't say (no Accessibility permission, an
app that doesn't answer within the messaging timeout, an unusual role) the answer is
``unknown`` and the paste goes ahead with the dictation left on the clipboard. ``none``
needs a clearly non-editable focus (a button, a list, a table, a menu, a read-only page).
Never raises; every AppKit / ApplicationServices import is lazy, so this module imports on
any OS.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from shoutabout.inject.focus import NONE, TEXT, UNKNOWN, Focus, LookWorker

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 0.15
AX_MESSAGING_TIMEOUT_S = 0.15

AX_ERROR_SUCCESS = 0
AX_ERROR_API_DISABLED = -25211
AX_ERROR_NO_VALUE = -25212

TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"})
TEXT_SUBROLES = frozenset({"AXSearchField", "AXSecureTextField"})
NOT_TYPABLE_ROLES = frozenset(
    {
        "AXButton",
        "AXCheckBox",
        "AXRadioButton",
        "AXPopUpButton",
        "AXMenuButton",
        "AXDisclosureTriangle",
        "AXList",
        "AXOutline",
        "AXRow",
        "AXTable",
        "AXBrowser",
        "AXMenu",
        "AXMenuBar",
        "AXMenuBarItem",
        "AXMenuItem",
        "AXTabGroup",
        "AXImage",
        "AXLink",
        "AXStaticText",
        "AXToolbar",
        "AXSlider",
        "AXScrollBar",
        "AXIncrementor",
        "AXColorWell",
        "AXDockItem",
    }
)
# Bundle identifiers, lower-case.
TERMINAL_APPS = frozenset(
    {
        "com.apple.terminal",
        "com.googlecode.iterm2",
        "net.kovidgoyal.kitty",
        "io.alacritty",
        "org.alacritty",
        "com.github.wez.wezterm",
        "com.mitchellh.ghostty",
        "dev.warp.warp-stable",
        "dev.warp.warp",
        "co.zeit.hyper",
        "org.tabby",
        "com.raphaelamorim.rio",
    }
)


@dataclass(frozen=True)
class MacFacts:
    """What the Mac probe looked at (None = the app didn't say)."""

    app: str = ""  # the focused element's app, bundle identifier, lower-case
    role: str | None = None
    subrole: str | None = None
    value_settable: bool | None = None
    selected_range: bool | None = None  # it has an AXSelectedTextRange
    editable_ancestor: bool | None = None  # inside an editable area (web content)
    error: str = ""  # why there's no focused element


def classify_mac(f: MacFacts) -> Focus:
    if f.app and f.app.lower() in TERMINAL_APPS:
        return Focus(TEXT, f"terminal {f.app}")
    role = f.role
    if role is None:
        return Focus(UNKNOWN, f.error or "no focused element")
    if role in TEXT_ROLES or f.subrole in TEXT_SUBROLES:
        if f.value_settable is False and not f.editable_ancestor:
            return Focus(UNKNOWN, f"{role} that can't be set")  # read-only? not sure enough
        return Focus(TEXT, role)
    if f.editable_ancestor:
        return Focus(TEXT, f"editable {role}")
    if role == "AXWebArea":
        if f.value_settable:
            return Focus(TEXT, "editable page")
        return Focus(NONE, "read-only page")
    if role in NOT_TYPABLE_ROLES and not f.value_settable:
        return Focus(NONE, role)
    if f.value_settable and f.selected_range:
        return Focus(TEXT, f"editable {role}")
    return Focus(UNKNOWN, role)


# ------------------------------------------------------------------ AX facade


class AxClient:
    """The real Accessibility calls (PyObjC's ApplicationServices)."""

    def __init__(self) -> None:
        import ApplicationServices as AS  # noqa: N814

        self._as = AS
        self._system = AS.AXUIElementCreateSystemWide()
        try:  # on the system-wide object it applies to every element
            AS.AXUIElementSetMessagingTimeout(self._system, AX_MESSAGING_TIMEOUT_S)
        except Exception:
            log.debug("couldn't set the AX messaging timeout", exc_info=True)

    def _copy(self, element: Any, attribute: str) -> tuple[int, Any]:
        err, value = self._as.AXUIElementCopyAttributeValue(element, attribute, None)
        return int(err), value

    def focused(self) -> MacFacts:
        err, element = self._copy(self._system, "AXFocusedUIElement")
        if err != AX_ERROR_SUCCESS or element is None:
            why = {
                AX_ERROR_API_DISABLED: "Accessibility isn't allowed",
                AX_ERROR_NO_VALUE: "nothing has the focus",
            }.get(err, f"AX error {err}")
            return MacFacts(app=self._frontmost(), error=why)
        role = self._string(element, "AXRole")
        subrole = self._string(element, "AXSubrole")
        settable = None
        try:
            s_err, s_value = self._as.AXUIElementIsAttributeSettable(element, "AXValue", None)
            settable = bool(s_value) if int(s_err) == AX_ERROR_SUCCESS else None
        except Exception:
            log.debug("AXValue settable check failed", exc_info=True)
        r_err, _rng = self._copy(element, "AXSelectedTextRange")
        e_err, ancestor = self._copy(element, "AXEditableAncestor")
        return MacFacts(
            app=self._app_of(element),
            role=role,
            subrole=subrole,
            value_settable=settable,
            selected_range=r_err == AX_ERROR_SUCCESS,
            editable_ancestor=(ancestor is not None) if e_err == AX_ERROR_SUCCESS else None,
        )

    def _string(self, element: Any, attribute: str) -> str | None:
        err, value = self._copy(element, attribute)
        return str(value) if err == AX_ERROR_SUCCESS and value is not None else None

    def _app_of(self, element: Any) -> str:
        try:
            err, pid = self._as.AXUIElementGetPid(element, None)
            if int(err) != AX_ERROR_SUCCESS:
                return self._frontmost()
            from AppKit import NSRunningApplication

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            bundle = app.bundleIdentifier() if app is not None else None
            return str(bundle).lower() if bundle else ""
        except Exception:
            log.debug("couldn't tell the focused app", exc_info=True)
            return ""

    def _frontmost(self) -> str:
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            bundle = app.bundleIdentifier() if app is not None else None
            return str(bundle).lower() if bundle else ""
        except Exception:
            return ""


# ----------------------------------------------------------------------- probe


class MacFocusProbe:
    """:class:`shoutabout.inject.focus.FocusProbe` for macOS."""

    def __init__(
        self,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        worker: LookWorker | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.timeout_s = timeout_s
        self._worker = worker if worker is not None else LookWorker(AxClient, "Accessibility")
        self._clock = clock
        self.last_facts: MacFacts | None = None

    def probe(self) -> Focus:
        t0 = self._clock()
        try:
            facts, error = self._worker.ask(self.timeout_s)
            if facts is None:
                facts = MacFacts(error=error or "no answer")
            elif error:
                facts = replace(facts, error=error)
            self.last_facts = facts
            answer = classify_mac(facts)
            return Focus(answer.kind, answer.reason, (self._clock() - t0) * 1000)
        except Exception as exc:
            log.debug("focus probe failed", exc_info=True)
            return Focus(
                UNKNOWN, f"probe failed: {type(exc).__name__}", (self._clock() - t0) * 1000
            )


def create_probe() -> MacFocusProbe | None:
    return MacFocusProbe() if sys.platform == "darwin" else None
