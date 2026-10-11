"""The first-run setup, as plain logic (no Tk): screens, state, copy and background work.

Screens (docs/setup-experience.md):

1. ``check`` "Let's get you talking": look at the PC, say in one line what it can run,
   download the speech model with a progress bar (skipped when it's already there).
2. ``use`` "How will you use ShoutAbout?": two cards, "Talk to this PC" (the hotkey) and
   "Talk from my iPhone" (waits for the phone, approves it inline, or shows the old QR code).
   On a PC with no microphone at all, "Talk to this PC" starts off and says so.
3. ``try`` "Try it": a text box with focus; the real hotkey dictation pastes into it.
4. ``done`` "You're all set!".

The Tk view (:mod:`setup_ui`) polls :attr:`SetupFlow.version` and redraws when it moves;
worker threads and pairing callbacks only change state under the lock. Settings > "Add a
phone" opens the same flow in ``mode="phone"`` (just the phone card, then Done).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from shoutabout.app_services import AppServices, dictation_view, gpu_name, talk_instruction
from shoutabout.text import normalize_whitespace

log = logging.getLogger(__name__)

SCREENS = ("check", "use", "try", "done")
TRY_PHRASE = "This is my first dictation."

TITLES = {
    "check": "Let's get you talking",
    "use": "How will you use ShoutAbout?",
    "try": "Try it",
    "done": "You're all set!",
    "phone": "Add your iPhone",
}

Runner = Callable[[Callable[[], None], str], None]


def thread_runner(work: Callable[[], None], name: str) -> None:
    threading.Thread(target=work, name=name, daemon=True).start()


# ------------------------------------------------------------------- copy


def hardware_line(hw: Any, choice: Any) -> str:
    """One plain sentence about what this PC can run."""
    tier = getattr(choice, "tier", None)
    gpu = gpu_name(hw)
    if tier == "best" and gpu:
        return f"Your {gpu} can run our most accurate model."
    if tier == "balanced" and gpu:
        return f"Your {gpu} runs our balanced model: quick and accurate."
    if tier == "balanced":
        return "This PC runs our balanced model: quick and accurate."
    if tier == "light":
        return "We picked our lightest model, so it stays quick on this PC."
    if tier == "best":
        return "This PC can run our most accurate model."
    return "We'll pick the best model for this PC."


def _mb(n_bytes: float) -> str:
    return f"{n_bytes / 1_000_000:,.0f} MB"


def download_line(phase: str, done: int, total: int) -> str:
    if phase == "checking":
        return "Checking this PC…"
    if phase == "downloading":
        if total > 0:
            pct = int(100 * done / total)
            return f"Getting the speech model… {pct}% ({_mb(done)} of {_mb(total)})"
        return "Getting the speech model…"
    if phase == "ready":
        return "Speech model ready."
    if phase == "failed":
        return "Couldn't download the speech model. Check your internet, then try again."
    if phase == "unknown":
        return "ShoutAbout gets the speech model ready when it starts."
    return ""


NO_MIC_LINE = "No microphone found on this PC. Use your iPhone, or plug in a mic."
PC_OFF_LINE = "Off. Turn it on to dictate with a key."
PHONE_WAITING = "Waiting for your iPhone…"
PHONE_HOWTO = "Open ShoutAbout on your iPhone. It finds this PC by itself."


def phone_status_line(state: str, name: str | None) -> str:
    who = name or "Your iPhone"
    return {
        "waiting": PHONE_WAITING,
        "asking": f"{who} wants to dictate to this PC.",
        "connecting": f"Connecting {who}…",
        "paired": f"{who} is connected.",
        "denied": "Okay, not that one. Still waiting for your iPhone…",
        "expired": "That took too long. Try again on your iPhone.",
        "error": "Couldn't save that phone. Try again.",
        "unavailable": "Start ShoutAbout first, then open this again.",
    }.get(state, PHONE_WAITING)


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
        self.mode = mode  # "full" | "phone"
        self._run = runner
        self._lock = threading.RLock()
        self.version = 0
        self.closed = False
        self.screen = "use" if mode == "phone" else "check"
        # screen 1
        self.phase = "idle"  # idle|checking|downloading|ready|failed|unknown
        self.hw: Any = None
        self.choice: Any = None
        self.hardware_text = ""
        self.done_bytes = 0
        self.total_bytes = 0
        self._cancel = threading.Event()
        # screen 2
        cfg = services.config()
        view = dictation_view(cfg)
        # No microphone at all (Windows says so; nothing is opened): the card starts off and
        # says why; leaving this screen saves that, unless the person switched it on.
        try:
            mic = services.has_microphone() if mode != "phone" else None
        except Exception:
            mic = None
        self.no_mic = mic is False
        self.pc_on = view.enabled and mode != "phone" and not self.no_mic
        self._save_pc_off = self.no_mic and view.enabled and mode != "phone"
        self.hotkey_label = services.hotkey_choices().get(view.hotkey, view.hotkey)
        self.talk_mode = view.mode
        self.phone_on = mode == "phone"
        self.save_error: str | None = None
        self.pair_state = "waiting" if services.pairing is not None else "unavailable"
        self.prompt: Any = None  # the PairPrompt being shown inline
        self.paired_name: str | None = None
        self.show_qr = False
        self.qr_text: str | None = None
        self.qr_error: str | None = None
        # screen 3
        self.got_text = False

    # ------------------------------------------------------------ plumbing

    def _bump(self) -> None:
        self.version += 1

    def title(self) -> str:
        if self.mode == "phone":
            return TITLES["phone"]
        return TITLES[self.screen]

    def step_label(self) -> str:
        if self.mode == "phone" or self.screen == "done":
            return ""
        return f"STEP {SCREENS.index(self.screen) + 1} OF 3"

    # ------------------------------------------------------- screen 1: check

    def start(self) -> None:
        if self.screen == "check" and self.phase == "idle":
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
            self.hw, self.choice = hw, choice
            self.hardware_text = hardware_line(hw, choice)
            self._bump()
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
        with self._lock:
            self.phase = phase
            self._bump()

    def retry(self) -> None:
        if self.phase == "failed":
            model = getattr(self.choice, "stt_model", None)
            if model:
                with self._lock:
                    self._cancel = threading.Event()
                self._run(lambda: self._download(model), "setup-download")
            else:
                self._start_check()

    def status_line(self) -> str:
        return download_line(self.phase, self.done_bytes, self.total_bytes)

    def progress_fraction(self) -> float | None:
        if self.phase == "ready":
            return 1.0
        if self.phase == "downloading" and self.total_bytes > 0:
            return min(1.0, self.done_bytes / self.total_bytes)
        return None

    def can_continue(self) -> bool:
        if self.screen == "check":
            return self.phase in ("ready", "unknown")
        if self.screen == "use":
            return self.prompt is None
        return True

    # ------------------------------------------------------- navigation

    def next(self) -> None:
        if self.screen == "use" and self._save_pc_off:
            self._persist_pc_off()
        with self._lock:
            if self.mode == "phone":
                self.screen = "done"
            elif self.screen == "check":
                self.screen = "use"
            elif self.screen == "use":
                self.screen = "try" if (self.pc_on or self.phone_on) else "done"
            elif self.screen == "try":
                self.screen = "done"
            self._bump()

    def back(self) -> None:
        with self._lock:
            if self.mode != "phone" and self.screen in ("use", "try"):
                self.screen = SCREENS[SCREENS.index(self.screen) - 1]
                self._bump()

    def skip_download(self) -> None:
        """ "Skip for now" after a failed download: the hub retries when it starts."""
        with self._lock:
            self._cancel.set()
            self.phase = "unknown"
            self.screen = "use"
            self._bump()

    # ------------------------------------------------------- screen 2: use

    def _persist_pc_off(self) -> None:
        """Save the no-microphone default (the card showed "off"). Best effort."""
        from shoutabout.config_edit import ConfigEditError

        self._save_pc_off = False
        try:
            self.svc.save_settings({"dictation": {"enabled": False}})
        except (ConfigEditError, OSError) as exc:
            log.warning("couldn't save dictation.enabled: %s", exc)

    def toggle_pc(self) -> None:
        from shoutabout.config_edit import ConfigEditError

        self._save_pc_off = False  # the person decided
        new = not self.pc_on
        try:
            self.svc.save_settings({"dictation": {"enabled": new}})
        except (ConfigEditError, OSError) as exc:
            log.warning("couldn't save dictation.enabled: %s", exc)
            with self._lock:
                self.save_error = "Couldn't save that setting."
                self._bump()
            return
        with self._lock:
            self.pc_on = new
            self.save_error = None
            self._bump()

    def toggle_phone(self) -> None:
        with self._lock:
            self.phone_on = not self.phone_on
            if not self.phone_on:
                self.show_qr = False
            self._bump()
        if self.phone_on:
            pairing = self.svc.pairing
            pending = pairing.pending() if pairing is not None else None
            if pending is not None:
                self.offer(pending)

    def offer(self, prompt: Any) -> bool:
        """A pairing request arrived. True if it's shown here (else the hub pops a prompt)."""
        with self._lock:
            if self.closed or not self.phone_on or self.screen != "use":
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

    def pc_line(self) -> str:
        return talk_instruction(self.hotkey_label, self.talk_mode)

    def pc_card_text(self) -> str:
        """The main line on the "Talk to this PC" card."""
        if self.pc_on:
            return self.pc_line()
        return NO_MIC_LINE if self.no_mic else PC_OFF_LINE

    def pc_card_note(self) -> str:
        """The smaller line under it (empty = none)."""
        if not self.pc_on:
            return ""
        if self.no_mic:
            return "No microphone found yet: plug one in first."
        return "The words appear wherever you're typing."

    # ------------------------------------------------------- screen 3: try

    def try_instruction(self) -> str:
        if self.pc_on:
            if self.talk_mode == "toggle":
                return f"Press {self.hotkey_label}, say this, then press it again:"
            return f"Hold {self.hotkey_label} and say:"
        return "Tap Computer on your iPhone, then say:"

    def text_changed(self, text: str) -> None:
        if normalize_whitespace(text) and not self.got_text:
            with self._lock:
                self.got_text = True
                self.screen = "done"
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
            mark_setup_complete(self.svc.paths, "finished" if self.screen == "done" else "skipped")
