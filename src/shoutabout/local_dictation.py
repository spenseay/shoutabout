""" "Talk to this PC": hotkey -> microphone -> the same pipeline as the phone -> paste.

Tap the hotkey (Right Ctrl by default), speak, tap it again (or, in hold mode, hold it while
you speak): the recording goes through exactly the pipeline ``POST /transcribe?target=pc``
uses (vocabulary hints, the cleanup and its guard, single-paragraph text, the paste rules,
history, pause), and the text is pasted into the window that has focus. The mouth pill
(:mod:`shoutabout.overlay`) shows what's happening; it never takes focus, so the paste lands
where you were typing.

Speed: when listening starts, the pill is told first (it shows a pre-rendered frame at
once) and the microphone opens right after, on the same thread; PortAudio was started in
the background when "Talk to this PC" started, so opening the stream is quick. Nothing at
all happens for a key press that turns out to be a shortcut (the hotkey machine decides
that before it says START), so Right Ctrl + C never flashes the pill or the microphone.

Phone requests and local dictation take turns through the hub's busy lock
(``HubState.busy``, which the pipeline holds while it works). A new recording may start
while the previous one is still being transcribed; it simply waits its turn.

When transcription fails after recording, the audio is kept (:mod:`failed_audio`) and the
History window offers "Try again", so a long dictation is never lost.

Threads: hotkey events arrive on the hotkey worker thread, the time limit on a short-lived
thread from the microphone, and each transcription runs on its own worker thread. All
state changes happen under one lock; nothing here blocks the keyboard hook itself.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import numpy as np

from shoutabout.config import Config
from shoutabout.engine.base import SAMPLE_RATE, EngineError
from shoutabout.hotkey import Action, HotkeyEvent, HotkeyListener, hotkey_label
from shoutabout.inject.base import ADMIN_WINDOW_WARNING
from shoutabout.mic import MicError, MicRecorder, has_input_device, no_mic_error, warm_up
from shoutabout.models import HubStatus
from shoutabout.overlay import OverlayLike, create_overlay
from shoutabout.pipeline import NO_SPEECH_WARNING, PAUSED_WARNING
from shoutabout.state import HubState

log = logging.getLogger(__name__)

TITLE = "ShoutAbout"
MIC_TITLE = "ShoutAbout can't hear you"
MIN_SECONDS = 0.35  # shorter than this is a slip of the finger, not a dictation
KEPT_NOTE = "Your recording is saved: open History from the tray menu to try again."

Notify = Callable[[str, str], None]


class DictationState(StrEnum):
    OFF = "off"  # not listening for the hotkey (turned off, paused or stopped)
    READY = "ready"  # waiting for the hotkey
    LISTENING = "listening"  # recording
    THINKING = "thinking"  # transcribing the last recording


def hint_text(cfg: Config) -> str:
    """One line for the tray / setup window, e.g. "Press Right Ctrl to talk, again to stop"."""
    d = cfg.dictation
    if not d.enabled:
        return "Talk to this PC is off."
    key = hotkey_label(d.hotkey)
    if d.mode == "toggle":
        return f"Press {key} to talk, and again to stop."
    return f"Hold {key} to talk."


def ready_text(cfg: Config) -> str:
    """The words of the pill that says ShoutAbout is ready, e.g. "Ready · Right Ctrl to talk"."""
    text = f"Ready · {hotkey_label(cfg.dictation.hotkey)} to talk"
    return text if len(text) <= 28 else "Ready to talk"


def overlay_warning(warning: str) -> str:
    """A few words for the overlay when the text wasn't pasted (it's on the clipboard)."""
    if warning == ADMIN_WINDOW_WARNING:
        return "Admin window"
    if warning == PAUSED_WARNING:
        return "Paused"
    lowered = warning.lower()
    if "locked" in lowered:
        return "PC is locked"
    if "modifier" in lowered:
        return "Key still held"
    return "Not pasted"


class LocalDictation:
    """Wires the hotkey, the microphone, the overlay and the pipeline together."""

    def __init__(
        self,
        cfg: Config,
        *,
        state: HubState,
        pipeline: Callable[[], Any | None],
        notify: Notify | None = None,
        listener_factory: Callable[..., Any] | None = None,
        recorder_factory: Callable[..., Any] | None = None,
        overlay_factory: Callable[[bool], OverlayLike] | None = None,
        failed: Any = None,
        mic_present: Callable[[], bool | None] | None = None,
        warm_mic: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._hub = state
        self._pipeline = pipeline
        self._notify_fn = notify
        self._listener_factory = listener_factory or HotkeyListener
        real_mic = recorder_factory is None
        self._recorder_factory = recorder_factory or MicRecorder
        self._overlay_factory = overlay_factory or create_overlay
        self._failed = failed  # failed_audio.FailedRecordings (None = never kept)
        # Is any microphone plugged in? (asked of Windows; with fake recorders: unknown)
        self._mic_present = mic_present or (has_input_device if real_mic else lambda: None)
        self._warm_mic = warm_mic if warm_mic is not None else (warm_up if real_mic else None)
        self._lock = threading.RLock()
        self._state = DictationState.OFF
        self._listener: Any = None
        self._overlay: OverlayLike | None = None
        self._recorder: Any = None
        self._generation = 0  # bumps on every new recording
        self._jobs = 0  # transcriptions running
        self._listeners: list[Callable[[DictationState], None]] = []
        self._paused_seen: bool | None = None
        self._told: set[str] = set()  # microphone problems already notified this session

    # ------------------------------------------------------------------ public

    @property
    def state(self) -> DictationState:
        with self._lock:
            return self._state

    @property
    def hint(self) -> str:
        return hint_text(self.cfg)

    def subscribe(self, listener: Callable[[DictationState], None]) -> None:
        """Called with the new state after every change (any thread)."""
        self._listeners.append(listener)

    def start(self) -> bool:
        """Listen for the hotkey. Returns True if listening. Never raises."""
        with self._lock:
            if self._state != DictationState.OFF:
                return True
            if not self.cfg.dictation.enabled:
                return False
            d = self.cfg.dictation
            overlay = self._overlay_factory(d.overlay)
            try:
                overlay.start()  # creates the window now and pre-renders its frames
            except Exception:
                log.exception("couldn't start the overlay; continuing without it")
                overlay = self._overlay_factory(False)
            try:
                listener = self._listener_factory(d.hotkey, d.mode, self._on_event)
                listener.start()
            except Exception as exc:
                log.error("couldn't listen for the dictation hotkey: %s", exc)
                self._safe(overlay.stop)
                self._notify(
                    "ShoutAbout can't use the hotkey",
                    f"Talk to this PC is off: {exc}",
                )
                return False
            self._overlay, self._listener = overlay, listener
            self._set_state(DictationState.READY)
        if self._warm_mic is not None:  # start PortAudio now, not on the first press
            threading.Thread(target=self._warm_mic, name="mic-warm", daemon=True).start()
        log.info("talk to this PC: %s (%s mode)", hint_text(self.cfg), self.cfg.dictation.mode)
        return True

    def stop(self) -> None:
        """Stop listening (drops a recording in progress). Never raises."""
        with self._lock:
            if self._state == DictationState.OFF and self._listener is None:
                return
            recorder, self._recorder = self._recorder, None
            listener, self._listener = self._listener, None
            overlay, self._overlay = self._overlay, None
            self._generation += 1
            self._set_state(DictationState.OFF)
        if recorder is not None:
            self._safe(recorder.cancel)
        if listener is not None:
            self._safe(listener.stop)
        if overlay is not None:
            self._safe(overlay.stop)

    def greet(self) -> bool:
        """Show the "Ready" pill (when the user has just opened ShoutAbout). False when
        it can't be shown now (not listening for the hotkey, or busy)."""
        with self._lock:
            overlay = self._overlay
            if overlay is None or self._state != DictationState.READY:
                return False
        overlay.notice(ready_text(self.cfg))
        return True

    def reload(self, cfg: Config) -> None:
        """Apply new [dictation] settings (hotkey, mode, overlay, on/off)."""
        was_on = self.state != DictationState.OFF
        self.stop()
        self.cfg = cfg
        if (was_on or cfg.dictation.enabled) and not self._hub.paused:
            self.start()

    def follow_pause(self) -> None:
        """Stop while the hub is paused and start again when it resumes."""
        self._paused_seen = self._hub.paused
        self._hub.subscribe(self._on_hub_change)

    # ----------------------------------------------------------------- events

    def _on_hub_change(self, hub: HubState) -> None:
        paused = hub.paused
        if paused == self._paused_seen:
            return
        self._paused_seen = paused
        # Not on this thread: it may be the pipeline's, inside the busy lock.
        target = self.stop if paused else self.start
        threading.Thread(target=target, name="dictation-pause", daemon=True).start()

    def _on_event(self, event: HotkeyEvent) -> None:
        if event.action == Action.START:
            self._begin()
        elif event.action == Action.STOP:
            self._finish(event.reason)
        elif event.action == Action.CANCEL:
            self._cancel(event.reason)

    def _on_limit(self) -> None:
        log.info("talk to this PC: reached %d s; stopping", self.cfg.dictation.max_seconds)
        listener = self._listener
        if listener is not None:
            listener.force_stop()
        self._finish("limit")

    def _begin(self) -> None:
        with self._lock:
            if self._state not in (DictationState.READY, DictationState.THINKING):
                return
            overlay, listener = self._overlay, self._listener
            if overlay is None or listener is None:
                return
            if self._hub.paused:
                overlay.notice("Paused")
                listener.force_stop()
                return
            if self._pipeline() is None:
                self._not_ready(overlay)
                listener.force_stop()
                return
            if self._mic_present() is False:  # no microphone at all: say so, once per press
                self._mic_problem(overlay, no_mic_error())
                listener.force_stop()
                return
            d = self.cfg.dictation
            overlay.listening()  # first: the pill shows at once
            recorder = self._recorder_factory(
                max_seconds=d.max_seconds,
                on_level=overlay.set_level,
                on_limit=self._on_limit,
            )
            try:
                recorder.start()
            except MicError as exc:
                self._mic_problem(overlay, exc)
                listener.force_stop()
                return
            except Exception as exc:
                log.exception("couldn't start recording")
                self._mic_problem(
                    overlay, MicError(f"Couldn't use the microphone: {exc}", "Mic problem")
                )
                listener.force_stop()
                return
            self._recorder = recorder
            self._generation += 1
            self._set_state(DictationState.LISTENING)

    def _mic_problem(self, overlay: OverlayLike, exc: MicError) -> None:
        """The red pill every time; the notification only once per session (per problem)."""
        overlay.error(exc.short)
        if exc.short in self._told:
            log.info("talk to this PC: %s", exc.short)
            return
        self._told.add(exc.short)
        self._notify(MIC_TITLE, str(exc))

    def _not_ready(self, overlay: OverlayLike) -> None:
        hub = self._hub
        if hub.status == HubStatus.ERROR:
            overlay.error("Not working")
            self._notify(TITLE, hub.error or "The speech engine isn't working; see the log.")
            return
        overlay.notice(hub.progress_text or "Getting ready…")

    def _finish(self, reason: str) -> None:
        with self._lock:
            recorder = self._recorder
            if recorder is None or self._state != DictationState.LISTENING:
                return
            self._recorder = None
            overlay = self._overlay
            try:
                audio = recorder.stop()
            except MicError as exc:
                if overlay is not None:
                    self._mic_problem(overlay, exc)
                self._settle()
                return
            except Exception:
                log.exception("stopping the recording failed")
                if overlay is not None:
                    overlay.error("Mic problem")
                self._settle()
                return
            seconds = len(audio) / SAMPLE_RATE
            if seconds < MIN_SECONDS:
                log.debug("talk to this PC: %.2f s is too short; ignored", seconds)
                if overlay is not None:
                    overlay.cancelled()
                self._settle()
                return
            if overlay is not None:
                overlay.thinking()
            self._jobs += 1
            generation = self._generation
            self._set_state(DictationState.THINKING)
        log.debug("talk to this PC: %.1f s recorded (%s)", seconds, reason)
        threading.Thread(
            target=self._transcribe,
            args=(audio, generation),
            name="dictation-transcribe",
            daemon=True,
        ).start()

    def _cancel(self, reason: str) -> None:
        with self._lock:
            recorder, self._recorder = self._recorder, None
            if recorder is None:
                return
            self._safe(recorder.cancel)
            overlay = self._overlay
            if overlay is not None:
                if reason == "escape":
                    overlay.cancelled()
                else:  # part of a keyboard shortcut: get out of the way quietly
                    overlay.hide()
            log.debug("talk to this PC: cancelled (%s)", reason)
            self._settle()

    def _keep(self, audio: np.ndarray, error: str) -> bool:
        """Keep a recording whose transcription failed (History > Try again)."""
        if self._failed is None:
            return False
        try:
            return self._failed.keep_samples(audio, route="local", error=error) is not None
        except Exception:
            log.exception("couldn't keep the failed recording")
            return False

    def _transcribe(self, audio: np.ndarray, generation: int) -> None:
        outcome: Callable[[OverlayLike], None]
        try:
            pipeline = self._pipeline()
            if pipeline is None:
                raise EngineError("The speech engine isn't ready.")
            result = pipeline.process_audio(audio, target="pc", route="local")
        except EngineError as exc:
            log.error("talk to this PC: %s", exc)
            kept = self._keep(audio, str(exc))
            self._notify(TITLE, f"{exc} {KEPT_NOTE}" if kept else str(exc))
            outcome = lambda o: o.error("Not working")  # noqa: E731
        except Exception as exc:
            log.exception("talk to this PC: transcription failed")
            if self._keep(audio, f"{type(exc).__name__}: {exc}"):
                self._notify(TITLE, f"Something went wrong. {KEPT_NOTE}")
            outcome = lambda o: o.error("Something broke")  # noqa: E731
        else:
            if result.pasted:
                outcome = lambda o: o.done()  # noqa: E731
            elif result.warning == NO_SPEECH_WARNING:
                outcome = lambda o: o.notice("Didn't catch that")  # noqa: E731
            else:
                warning = result.warning or "Not pasted"
                if warning != ADMIN_WINDOW_WARNING:  # the injector already said so
                    self._notify(TITLE, warning)
                outcome = lambda o: o.error(overlay_warning(warning))  # noqa: E731
        with self._lock:
            self._jobs -= 1
            overlay = self._overlay
            # A newer recording owns the overlay now; don't paint over it.
            if overlay is not None and generation == self._generation:
                self._safe(lambda: outcome(overlay))
            if self._state == DictationState.THINKING and self._jobs <= 0:
                self._set_state(DictationState.READY)

    # ---------------------------------------------------------------- helpers

    def _settle(self) -> None:
        """Back to READY (or THINKING while an earlier dictation is still running)."""
        self._set_state(DictationState.THINKING if self._jobs > 0 else DictationState.READY)

    def _set_state(self, state: DictationState) -> None:
        if state == self._state:
            return
        self._state = state
        for listener in list(self._listeners):
            try:
                listener(state)
            except Exception:
                log.exception("dictation state listener failed")

    def _notify(self, title: str, message: str) -> None:
        if self._notify_fn is None:
            log.warning("%s: %s", title, message)
            return
        try:
            self._notify_fn(title, message)
        except Exception:
            log.exception("notification failed")

    @staticmethod
    def _safe(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            log.exception("talk to this PC: cleanup step failed")
