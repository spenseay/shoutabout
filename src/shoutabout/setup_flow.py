"""The first-run setup, as plain logic (no Tk): screens, state, copy and background work.

Screens (docs/setup-experience.md):

0. ``allow`` "Allow access" (macOS only, first): one line per permission ShoutAbout needs
   (the microphone, Accessibility), each with **Allow** and a check mark that follows macOS
   (polled about once a second). When everything is allowed the flow moves on by itself;
   "Skip" moves on without it. The speech model downloads meanwhile.
1. ``check`` "Getting ready": download the speech model with a progress bar. The model is
   picked automatically; when it's already here (or the download is done) the flow moves on
   by itself.
2. ``try`` "Try it": a text box with focus; the real hotkey dictation pastes into it. Skipped
   when Talk to this PC is off or there's no microphone, and on a Mac until the microphone
   and the hotkey are allowed.
3. ``done`` "You're all set": how to talk, **Done**, and an optional "Connect your iPhone".
4. ``phone`` "Connect your iPhone" (optional): waits for the phone, approves it inline, or
   shows the old QR code.

The Tk view (:mod:`setup_ui`) polls :attr:`SetupFlow.version` and redraws when it moves;
worker threads and pairing callbacks only change state under the lock. Settings > "Add a
phone" opens the same flow in ``mode="phone"`` (just the phone screen); on a Mac,
``mode="allow"`` is just the permissions screen (a notification or the menu bar opens it).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shoutabout.app_services import AppServices, dictation_view, talk_instruction
from shoutabout.mac import access as mac_access
from shoutabout.text import normalize_whitespace

log = logging.getLogger(__name__)

SCREENS = ("allow", "check", "try", "done", "phone")
TRY_PHRASE = "This is my first dictation."

TITLES = {
    "allow": "Allow access",
    "check": "Getting ready",
    "try": "Try it",
    "done": "You're all set",
    "phone": "Connect your iPhone",
}

Runner = Callable[[Callable[[], None], str], None]


def thread_runner(work: Callable[[], None], name: str) -> None:
    threading.Thread(target=work, name=name, daemon=True).start()


# ------------------------------------------------------------------- copy


def _mb(n_bytes: float) -> str:
    return f"{n_bytes / 1_000_000:,.0f} MB"


def download_line(phase: str, done: int, total: int) -> str:
    """The one line on the "Getting ready" screen."""
    if phase == "checking":
        return "One moment…"
    if phase == "downloading":
        if total > 0:
            return f"Downloading the speech model… {int(100 * done / total)}%"
        return "Downloading the speech model…"
    if phase == "ready":
        return "Ready."
    if phase == "failed":
        return "Couldn't download the speech model. Check your internet, then try again."
    return ""


def size_line(phase: str, done: int, total: int) -> str:
    """The small line under the progress bar: "672 MB of 1,600 MB"."""
    if phase == "downloading" and total > 0:
        return f"{_mb(done)} of {_mb(total)}"
    return ""


NO_MIC_LINE = "No microphone found. Plug one in, or connect your iPhone."
# v2-macos: the "Allow access" screen (one short line per permission) and what follows it.
ALLOW_LABELS = {
    "microphone": "Microphone",
    "accessibility": "Accessibility",
    "input_monitoring": "Input Monitoring",
}
ALLOW_WHY = {
    "microphone": "to hear you",
    "accessibility": "for {key} and pasting",
    "input_monitoring": "for {key}",
}
ALLOW_HINT = "Turn on ShoutAbout there, then come back."
ALMOST_TITLE = "Almost there"
ACCESS_LINE = "Talk to this PC needs access first."
PASTE_NOTE = "Then press ⌘V to paste it."
PHONE_HOWTO = "Open ShoutAbout on your iPhone."
PHONE_WAITING = "Waiting for your iPhone…"
QR_NOTE = "Scan it in the ShoutAbout app. Show it only to your own phone."


def phone_status_line(state: str, name: str | None) -> str:
    who = name or "Your iPhone"
    return {
        "waiting": PHONE_WAITING,
        "asking": f"{who} wants to dictate to this PC.",
        "connecting": f"Connecting {who}…",
        "paired": f"{who} is connected.",
        "denied": "Okay, not that one. Still waiting…",
        "expired": "That took too long. Try again on your iPhone.",
        "error": "Couldn't save that phone. Try again.",
        "unavailable": "Start ShoutAbout first, then open this again.",
    }.get(state, PHONE_WAITING)


@dataclass(frozen=True)
class AllowRow:
    """One line of the "Allow access" screen."""

    key: str  # "microphone" | "accessibility" | "input_monitoring"
    label: str
    why: str
    granted: bool


# ------------------------------------------------------------------- flow


class SetupFlow:
    def __init__(
        self,
        services: AppServices,
        *,
        mode: str = "full",
        runner: Runner = thread_runner,
    ) -> None:
        self.svc = services
        self.mode = mode  # "full" | "phone" | "allow" (macOS)
        self._run = runner
        self._lock = threading.RLock()
        self.version = 0
        self.closed = False
        self.screen = "phone" if mode == "phone" else "check"
        # v2-macos: what macOS allows (shoutabout.mac.access status; None = nothing to ask)
        self.access = services.permissions if mode in ("full", "allow") else None
        self.perms: dict[str, Any] | None = None
        self.allow_hint = ""
        self._polling = False
        self._raise = False
        # getting ready
        self.phase = "idle"  # idle|checking|downloading|ready|failed|unknown
        self.choice: Any = None
        self.done_bytes = 0
        self.total_bytes = 0
        self._cancel = threading.Event()
        # talk to this PC (set in Settings; setup only reads it)
        view = dictation_view(services.config())
        try:  # Windows' own count of microphones; nothing is opened
            mic = services.has_microphone() if mode != "phone" else None
        except Exception:
            mic = None
        self.no_mic = mic is False
        self.pc_on = view.enabled and mode != "phone" and not self.no_mic
        self.hotkey_label = services.hotkey_choices().get(view.hotkey, view.hotkey)
        self.talk_mode = view.mode
        # the iPhone
        self.pair_state = "waiting" if services.pairing is not None else "unavailable"
        self.prompt: Any = None  # the PairPrompt being shown inline
        self.paired_name: str | None = None
        self.show_qr = False
        self.qr_text: str | None = None
        self.qr_error: str | None = None
        # try it
        self.got_text = False
        # v2-macos: on a Mac, start with "Allow access" while something is missing (the
        # first answer is read right away, so a Mac that allows everything never sees it)
        if self.access is not None:
            self.perms = self._read_permissions()
            if self.perms is not None and mac_access.missing(self.perms):
                self.screen = "allow"
            elif mode == "allow":  # all allowed already: say how to talk
                self.screen = "allow" if self.perms is None else "done"

    # ------------------------------------------------------------ plumbing

    def _bump(self) -> None:
        self.version += 1

    def title(self) -> str:
        if self.screen == "done" and self.needs_access():
            return ALMOST_TITLE  # not "You're all set" while Talk to this Mac can't work
        return TITLES[self.screen]

    # ---------------------------------------------- screen 0: allow access (macOS)

    def _read_permissions(self) -> dict[str, Any] | None:
        try:
            status = self.access.status() if self.access is not None else None
        except Exception as exc:
            log.warning("couldn't read the permissions: %s", exc)
            return None
        return status if isinstance(status, dict) else None

    def can_try(self) -> bool:
        """Try it needs the microphone and the hotkey (always true without a permissions
        gate: Windows, or when macOS can't be asked)."""
        return self.perms is None or mac_access.can_talk(self.perms)

    def needs_access(self) -> bool:
        """Talk to this Mac can't work yet: the microphone or the hotkey isn't allowed."""
        return self.access is not None and self.pc_on and not self.can_try()

    def allow_rows(self) -> list[AllowRow]:
        perms = self.perms or {}
        key = self.hotkey_label
        return [
            AllowRow(k, ALLOW_LABELS[k], ALLOW_WHY[k].format(key=key), mac_access.granted(k, perms))
            for k in mac_access.rows(perms)
        ]

    def poll_permissions(self) -> None:
        """Read the permissions again, in the background (the view calls this about once a
        second while it shows "Allow access" or "Getting ready")."""
        with self._lock:
            if self.access is None or self.closed or self._polling:
                return
            if self.screen not in ("allow", "check"):
                return
            self._polling = True
        self._run(self._poll_once, "setup-permissions")

    def _poll_once(self) -> None:
        try:
            self.refresh_permissions()
        finally:
            with self._lock:
                self._polling = False

    def refresh_permissions(self) -> None:
        status = self._read_permissions()
        if status is None:
            return
        with self._lock:
            if self.closed:
                return
            changed = status != self.perms
            self.perms = status
            if self.screen == "allow" and not mac_access.missing(status):
                self.screen = self._after_allow()
                self.allow_hint = ""
                self._raise = True  # probably still in System Settings: show the next step
                changed = True
            if changed:
                self._bump()

    def ask(self, key: str) -> None:
        """An "Allow" button: the hub asks macOS (its dialog) or opens System Settings."""
        if self.access is None:
            return
        self._run(lambda: self._ask(key), "setup-allow")

    def _ask(self, key: str) -> None:
        try:
            what = self.access.ask(key) if self.access is not None else None
        except Exception as exc:
            log.warning("couldn't ask for %s: %s", key, exc)
            what = None
        if what == "opened":
            with self._lock:
                self.allow_hint = ALLOW_HINT
                self._bump()
        self.refresh_permissions()

    def skip_allow(self) -> bool:
        """ "Skip": on without the permissions (Try it only if the microphone and the hotkey
        work). True when the window should just close (it was opened only for this)."""
        with self._lock:
            if self.screen != "allow":
                return False
            if self.mode != "full":
                return True
            self.screen = self._after_allow()
            self.allow_hint = ""
            self._bump()
            return False

    def back_to_allow(self) -> None:
        """ "Allow access" on the last screen, after a Skip."""
        with self._lock:
            if self.access is None or self.closed or self.screen != "done":
                return
            self.screen = "allow"
            self._bump()
        self.poll_permissions()

    def _after_allow(self) -> str:
        if self.mode != "full":
            return "done"
        if self.phase in ("ready", "unknown"):
            return self._after_check()
        return "check"  # still downloading (or failed: Try again / Skip there)

    def take_raise(self) -> bool:
        """Once after the flow left "Allow access" by itself: the window comes to the front
        (the person is probably still in System Settings)."""
        with self._lock:
            wanted, self._raise = self._raise, False
            return wanted

    # ------------------------------------------------- screen 1: getting ready

    def start(self) -> None:
        # From "Allow access" too: the model downloads while the person clicks Allow.
        if self.mode == "full" and self.screen in ("allow", "check") and self.phase == "idle":
            self._start_check()

    def _start_check(self) -> None:
        with self._lock:
            self.phase = "checking"
            self._cancel = threading.Event()
            self._bump()
        self._run(self._check_worker, "setup-check")

    def _check_worker(self) -> None:
        hw = choice = None
        try:
            hw = self.svc.detect_hardware()
        except Exception as exc:
            log.warning("couldn't detect this PC's hardware: %s", exc)
        try:
            choice = self.svc.resolve_model(self.svc.config(), hw)
        except Exception as exc:
            log.warning("couldn't pick a speech model: %s", exc)
        with self._lock:
            self.choice = choice
        model = getattr(choice, "stt_model", None)
        if not model:
            self._set_phase("unknown")
            return
        try:
            ready = bool(self.svc.model_ready(model))
        except Exception as exc:
            log.warning("couldn't check the speech model: %s", exc)
            self._set_phase("unknown")
            return
        if ready:
            self._set_phase("ready")
            return
        self._download(model)

    def _download(self, model: str) -> None:
        mb = getattr(self.choice, "download_mb", None)
        with self._lock:
            self.phase = "downloading"
            self.done_bytes = 0
            self.total_bytes = int(mb * 1_000_000) if isinstance(mb, int | float) else 0
            cancel = self._cancel
            self._bump()

        def progress(done: int, total: int) -> None:
            with self._lock:
                self.done_bytes = int(done)
                if total:
                    self.total_bytes = int(total)
                self._bump()

        try:
            self.svc.download_model(model, progress=progress, cancel=cancel)
        except Exception as exc:
            if cancel.is_set():
                return
            log.warning("speech model download failed: %s", exc)
            self._set_phase("failed")
            return
        if not cancel.is_set():
            self._set_phase("ready")

    def _set_phase(self, phase: str) -> None:
        """A new phase; "ready" (or "unknown": the hub gets the model ready when it starts)
        moves on to the next screen by itself (not from "Allow access": that waits for
        the permissions, or Skip)."""
        with self._lock:
            self.phase = phase
            if phase in ("ready", "unknown") and self.screen == "check" and not self.closed:
                self.screen = self._after_check()
            self._bump()

    def _after_check(self) -> str:
        return "try" if self.pc_on and self.can_try() else "done"

    def retry(self) -> None:
        if self.phase == "failed":
            model = getattr(self.choice, "stt_model", None)
            if model:
                with self._lock:
                    self._cancel = threading.Event()
                self._run(lambda: self._download(model), "setup-download")
            else:
                self._start_check()

    def skip_download(self) -> None:
        """ "Skip for now" after a failed download: the hub retries when it starts."""
        with self._lock:
            self._cancel.set()
            self.phase = "unknown"
            self.screen = "done"
            self._bump()

    def status_line(self) -> str:
        return download_line(self.phase, self.done_bytes, self.total_bytes)

    def size_line(self) -> str:
        return size_line(self.phase, self.done_bytes, self.total_bytes)

    def progress_fraction(self) -> float | None:
        if self.phase == "ready":
            return 1.0
        if self.phase == "downloading" and self.total_bytes > 0:
            return min(1.0, self.done_bytes / self.total_bytes)
        return None

    # ------------------------------------------------------- screen 2: try it

    def try_instruction(self) -> str:
        if self.talk_mode == "toggle":
            return f"Press {self.hotkey_label}, say this, then press it again:"
        return f"Hold {self.hotkey_label} and say:"

    def try_note(self) -> str:
        """v2-macos: pasting isn't allowed, so the words land on the clipboard: ⌘V."""
        if self.perms is not None and not mac_access.can_paste(self.perms):
            return PASTE_NOTE
        return ""

    def text_changed(self, text: str) -> None:
        if normalize_whitespace(text) and not self.got_text:
            with self._lock:
                self.got_text = True
                if self.screen == "try":
                    self.screen = "done"
                self._bump()

    def skip_try(self) -> None:
        with self._lock:
            if self.screen == "try":
                self.screen = "done"
                self._bump()

    # ------------------------------------------------------ screen 3: done

    def pc_line(self) -> str:
        return talk_instruction(self.hotkey_label, self.talk_mode)

    def done_line(self) -> str:
        if self.needs_access():
            return ACCESS_LINE
        if self.pc_on:
            return self.pc_line()
        if self.no_mic:
            return NO_MIC_LINE
        return ""

    # ------------------------------------------------- screen 4: the iPhone

    def connect_phone(self) -> None:
        """ "Connect your iPhone": show the phone screen (and a request already waiting)."""
        with self._lock:
            if self.closed or self.screen == "phone":
                return
            self.screen = "phone"
            self._bump()
        pairing = self.svc.pairing
        pending = pairing.pending() if pairing is not None else None
        if pending is not None:
            self.offer(pending)

    def offer(self, prompt: Any) -> bool:
        """A pairing request arrived. True if it's shown here (else the hub pops a prompt)."""
        with self._lock:
            if self.closed or self.screen != "phone":
                return False
            self.prompt = prompt
            self.pair_state = "asking"
            self.show_qr = False
            self._bump()
            return True

    def allow(self) -> None:
        prompt, pairing = self.prompt, self.svc.pairing
        if prompt is None or pairing is None:
            return
        try:
            ok = pairing.approve(prompt.request_id)
        except Exception as exc:
            log.warning("couldn't approve the phone: %s", exc)
            with self._lock:
                self.prompt, self.pair_state = None, "error"
                self._bump()
            return
        with self._lock:
            self.paired_name = prompt.device_name
            self.prompt = None
            self.pair_state = "connecting" if ok else "expired"
            self._bump()

    def deny(self) -> None:
        prompt, pairing = self.prompt, self.svc.pairing
        if prompt is None:
            return
        if pairing is not None:
            pairing.deny(prompt.request_id)
        with self._lock:
            self.prompt, self.pair_state = None, "denied"
            self._bump()

    def on_pair_finished(self, request_id: str, status: str) -> None:
        with self._lock:
            if self.prompt is not None and self.prompt.request_id == request_id:
                if status in ("expired", "denied"):
                    self.prompt = None
                    self.pair_state = "expired" if status == "expired" else "denied"
                    self._bump()
                return
            if status == "claimed" and self.pair_state == "connecting":
                self.pair_state = "paired"
                self._bump()
            elif status == "expired" and self.pair_state == "connecting":
                self.pair_state = "expired"
                self._bump()

    def phone_line(self) -> str:
        name = self.prompt.device_name if self.prompt is not None else self.paired_name
        return phone_status_line(self.pair_state, name)

    def toggle_qr(self) -> None:
        with self._lock:
            self.show_qr = not self.show_qr
            if self.show_qr and self.qr_text is None:
                try:
                    self.qr_text = self.svc.legacy_qr_text()
                    self.qr_error = None
                except Exception as exc:
                    log.warning("couldn't build the pairing code: %s", exc)
                    self.qr_error = "Couldn't make a code right now."
            self._bump()

    # ------------------------------------------------------------ the end

    def finish(self, remember: bool = True) -> None:
        """Done (or the window was closed): stop the download, say no to a phone still
        waiting, and (``remember``) write the setup flag so it doesn't open by itself again."""
        from shoutabout.firstrun import mark_setup_complete

        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._cancel.set()
            pending = self.prompt
        if pending is not None and self.svc.pairing is not None:
            self.svc.pairing.deny(pending.request_id)  # closing the window says no
        if self.mode == "full" and remember:
            done = self.screen in ("done", "phone")
            mark_setup_complete(self.svc.paths, "finished" if done else "skipped")
