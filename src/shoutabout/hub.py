"""The long-running hub: tray icon + HTTP server + engine loader + LLM warmer.

```
main thread      tray message loop (or wait on the server with tray=False)
thread "server"  uvicorn (FastAPI app)
thread "engine"  load Whisper, then publish ctx.pipeline
thread "warmer"  keep the Ollama cleanup model loaded
thread "hotkey"  "Talk to this PC" (+ "hotkey-hook", "overlay"; see voice_runtime.py)
thread "ui"      the ShoutAbout window, the setup window and pairing prompts, while one is open
thread "discovery" announces the hub on the network once it's listening (hub_v2)
```

The model setup is picked once at start (``voice_runtime``: explicit config values win
over "auto"); the engine thread downloads the model first if it isn't here yet, with
progress in ``HubState.progress``.

Nothing you dictate is written to disk: :class:`~shoutabout.recent.Recent` keeps the last
few dictations and failed recordings in memory for the Recent page.

The other v2 parts (pairing, per-device tokens, discovery, the windows) live in
:mod:`hub_v2`; the blocks marked ``# --- v2-setup`` below wire them in.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any

from shoutabout import __version__
from shoutabout.config import ConfigError, Paths, ensure_initialized, load_config, load_token
from shoutabout.engine.base import EngineError
from shoutabout.inject.base import LogNotifier, Notifier
from shoutabout.logging_setup import redirect_std_streams, setup_logging
from shoutabout.models import HubStatus
from shoutabout.state import HubState

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\ShoutAboutHub"
ERROR_ALREADY_EXISTS = 183
ACCESS_NOT_CONFIGURED_WARNING = (
    "Cloudflare Access JWT validation is NOT configured: requests arriving via the tunnel "
    "are accepted with the bearer token alone. Set [access] team_domain and aud in "
    "config.toml."
)
SERVER_JOIN_TIMEOUT_S = 10.0
CLEANUP_RETRY_S = 30.0  # "auto" cleanup: how often to look for Ollama until it answers
GREET_WAIT_S = 30 * 60.0  # "Ready" after a launch waits this long at most (model download)


# ------------------------------------------------------------------ single instance


class SingleInstance:
    """Named-mutex guard (Windows). Elsewhere it always succeeds."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self._handle: Any = None
        self._kernel32: Any = None

    def acquire(self) -> bool:
        """True if this is the only instance (keep the object alive while running)."""
        if sys.platform != "win32":
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self.name)
        last_error = ctypes.get_last_error()
        if not handle:
            log.warning("couldn't create single-instance mutex (error %d)", last_error)
            return True
        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._kernel32 = kernel32
        return True

    def release(self) -> None:
        if self._handle and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._handle)
        self._handle = None


def is_elevated() -> bool:
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------- run


def run_hub(paths: Paths, *, tray: bool = True, greet: bool = False) -> int:
    """Run the hub. ``greet``: the user opened ShoutAbout (Start menu, the installer's
    last page), so say it's ready, or, when it already runs, show that copy's window."""
    had_console = sys.stderr is not None
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    redirect_std_streams(paths)
    if sys.platform == "darwin" and tray:  # v2-macos: no terminal -> stdout.log
        from shoutabout.mac.platform import redirect_output

        if redirect_output(paths):
            had_console = False

    # --- v2-setup: look at the install before anything is created (first-run detection)
    from shoutabout import firstrun

    before = firstrun.snapshot(paths)
    # --- end v2-setup
    try:
        ensure_initialized(paths)
        # One-time upgrades of values an older version wrote as defaults (never a choice).
        from shoutabout.migrations import migrate

        migrate(paths)
        cfg = load_config(paths)
    except (ConfigError, OSError) as exc:
        setup_logging(paths, "INFO", console=had_console)
        log.error("can't start: %s", exc)
        return 1
    setup_logging(paths, cfg.logging.level, console=had_console)

    instance = SingleInstance()
    if not instance.acquire():
        if greet:
            from shoutabout.reopen import ask_running_hub

            if ask_running_hub():
                log.info("ShoutAbout is already running: asked it to show its window")
                return 0
        log.warning("another ShoutAbout hub is already running; exiting")
        return 1
    mac_lock = None  # v2-macos: one hub per user (flock on <home>/hub.lock)
    if sys.platform == "darwin":
        from shoutabout.mac.platform import acquire_single_instance

        mac_lock = acquire_single_instance(paths)
        if mac_lock is None:
            log.warning("another ShoutAbout hub is already running; exiting")
            return 1
    # --- v2-setup: a fresh install moves off a busy port; decide on the setup window
    from shoutabout import hub_v2

    cfg = hub_v2.first_run_port(paths, cfg, before)
    show_setup = firstrun.needs_setup(paths, before)
    # --- end v2-setup
    if sys.platform == "darwin":  # v2-macos: Right ⌘ + press to start/stop, open at login
        from shoutabout.mac.platform import first_run_defaults

        first_run_defaults(paths, fresh=show_setup and not before.existing_install)
        try:
            cfg = load_config(paths)
        except ConfigError:
            log.exception("the config doesn't load after the Mac defaults")
    try:
        return _run(paths, cfg, tray=tray, show_setup=show_setup, greet=greet)
    except Exception:
        log.exception("hub crashed")
        return 1
    finally:
        instance.release()
        if mac_lock is not None:
            mac_lock.close()
        log.info("hub stopped")


def _run(
    paths: Paths, cfg: Any, *, tray: bool, show_setup: bool = False, greet: bool = False
) -> int:
    from shoutabout.netinfo import is_loopback_host, lan_url
    from shoutabout.security import AccessJwtValidator
    from shoutabout.server import HubContext, create_app

    log.info(
        "ShoutAbout %s starting (pid %d, python %s, home %s)",
        __version__,
        os.getpid(),
        sys.version.split()[0],
        paths.home,
    )
    if is_elevated():
        log.warning(
            "the hub is running elevated (as administrator); it should run as a normal user"
        )

    try:
        token = load_token(paths)
    except ConfigError as exc:
        log.error("can't start: %s", exc)
        return 1

    access = None
    if cfg.access.enabled:
        access = AccessJwtValidator(cfg.access)
        log.info("Cloudflare Access JWT validation enabled (issuer %s)", access.issuer)
    else:
        log.warning("%s", ACCESS_NOT_CONFIGURED_WARNING)

    url = lan_url(cfg)
    if url:
        log.info("LAN URL: %s", url)
    elif is_loopback_host(cfg.server.host):
        log.info("LAN access disabled (server.host = %s)", cfg.server.host)
    else:
        log.warning("no private LAN IPv4 address detected (set server.advertise_host?)")

    state = HubState()
    ctx = HubContext(
        config=cfg, token=token, state=state, access=access, vocab_path=paths.vocab_file
    )
    app = create_app(ctx)

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=cfg.server.host,
            port=cfg.server.port,
            log_config=None,
            access_log=False,
            proxy_headers=False,  # never rewrite the peer address from X-Forwarded-For
            server_header=False,
            lifespan="off",
        )
    )
    stopping = threading.Event()

    # ---------------------------------------------------------------- tray / notifier
    tray_icon: Any = None
    notifier: Notifier = LogNotifier()

    def notify(title: str, message: str) -> None:
        try:
            notifier.notify(title, message)
        except Exception:
            log.exception("notification failed")

    def request_quit() -> None:
        log.info("quit requested")
        stopping.set()
        server.should_exit = True
        if tray_icon is not None:
            tray_icon.stop()

    # --- v2-macos: the menu bar, permissions and the native fallback pairing prompt
    mac_hub: Any = None
    if sys.platform == "darwin" and tray:
        from shoutabout.mac.platform import MacHub

        mac_hub = MacHub(paths, state, notify)
    # --- end v2-macos

    # The last few dictations, and recordings that couldn't be written out (Recent > Try
    # again): in memory only, gone when the hub quits.
    from shoutabout.recent import Recent

    recent = Recent()
    ctx.recent = recent

    # --- v2-setup: pairing, per-device tokens, discovery, the windows
    from shoutabout import hub_v2

    v2 = hub_v2.start(
        paths,
        cfg,
        ctx,
        state,
        with_ui=tray and sys.platform in ("win32", "darwin"),  # v2-macos: darwin too
        notify=notify,
        recent=recent,
        native_prompt=(
            (lambda prompt: mac_hub.native_prompt(prompt, ctx.pairing)) if mac_hub else None
        ),
    )
    # --- end v2-setup

    mac_reopen: Any = None
    if mac_hub is not None:  # v2-macos
        try:
            tray_icon = _build_tray(paths, cfg, token, state, notify, request_quit, v2, mac_hub)
            notifier = tray_icon
        except Exception:
            log.exception("couldn't create the menu bar icon; running without it")
            tray_icon = None
        if v2.ui is not None:  # opened again from Finder, Spotlight or Launchpad: the window
            from shoutabout.reopen import watch_mac

            ui = v2.ui  # ("Allow access" in front of it while a permission is missing)
            mac_reopen = watch_mac(lambda: mac_hub.reopened(ui))  # noqa: F841 - held weakly
        import signal

        signal.signal(signal.SIGTERM, lambda *_: request_quit())  # log out, pkill: quit cleanly

    if tray and sys.platform == "win32":
        _enable_dpi_awareness()  # crisp tray icon at 125-200% display scaling
        try:
            tray_icon = _build_tray(paths, cfg, token, state, notify, request_quit, v2)
            notifier = tray_icon
        except Exception:
            log.exception("couldn't create the tray icon; running without it")
            tray_icon = None

    # ------------------------------------------------------------- pipeline parts
    from shoutabout.inject import create_injector

    injector = create_injector(cfg.paste, notifier)
    # --- v2-voice: automatic model choice, cleanup and "Talk to this PC" -------------
    from shoutabout.voice_runtime import VoiceRuntime

    voice = VoiceRuntime(cfg, state, notify, recent=recent)
    try:
        voice.choose()
    except Exception:  # never stop the hub over the choice; the engine reports problems
        log.exception("couldn't decide the model setup")
    voice.start_dictation(lambda: ctx.pipeline)
    if mac_hub is not None:  # v2-macos: setup asks for permissions (else a notice), then watch
        mac_hub.start_permissions(voice, v2, first_run=show_setup)
    # --- end v2-voice -----------------------------------------------------------------
    # --- v2-setup: POST /paste pastes phone-made text with the same injector (and Recent)
    ctx.injector = injector
    services = v2.services

    def copy_text(text: str) -> None:
        from shoutabout.inject import clipboard

        clipboard.set_text(text, exclude_from_history=cfg.paste.exclude_from_history)

    def retry_failed(item_id: str) -> str:
        """Recent > Try again: transcribe a kept recording (never pasted: the ShoutAbout
        window has the focus). It joins Recent; the recording is dropped on success."""
        samples = recent.samples(item_id)
        item = recent.get_failed(item_id)
        if samples is None or item is None:
            raise LookupError("That recording is gone.")
        pipeline = ctx.pipeline
        if pipeline is None:
            raise RuntimeError("ShoutAbout is still getting ready. Try again in a moment.")
        result = pipeline.process_audio(
            samples,
            target="pc",
            route=item.route,
            deliver=False,
            extra={"source": item.source, "recovered": True},
        )
        if result.text:
            recent.remove_failed(item_id)
        return result.text

    services.copy_text = copy_text
    services.retry_failed = retry_failed

    def reload_cleanup(new_cfg: Any, _key: str) -> bool:
        """Turn LLM cleanup on/off live when the cleaner exists (else: restart)."""
        if voice.cleaner is None:
            return False
        cfg.cleanup.enabled = new_cfg.cleanup.enabled
        return True

    def reload_dictation(new_cfg: Any, _key: str) -> bool:
        """Hotkey, mode, overlay and on/off apply at once (starts dictation if it was off)."""
        cfg.dictation = new_cfg.dictation
        voice.reload_dictation(new_cfg)
        return True

    v2.reloaders["cleanup"] = reload_cleanup
    v2.reloaders["dictation"] = reload_dictation
    # --- end v2-setup

    # ------------------------------------------------------------------ threads
    server_failed = threading.Event()
    # Set by the server thread when uvicorn has really finished. Waiting on this instead of
    # Thread.join(): on Python 3.12 a Ctrl+C that lands inside join() can mark a still
    # running thread as stopped, which would skip the graceful shutdown.
    server_done = threading.Event()

    def serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # uvicorn sys.exit()s when it can't bind
            if not isinstance(exc, SystemExit):
                log.exception("server crashed")
        finally:
            if not server.started and not stopping.is_set():
                server_failed.set()
                msg = f"Couldn't listen on port {cfg.server.port} — is another copy running?"
                log.error("%s", msg)
                state.set_status(HubStatus.ERROR, msg)
                notify("ShoutAbout error", msg)
            server_done.set()

    def load_engine() -> None:
        try:
            from shoutabout.pipeline import Pipeline
            from shoutabout.vocab import VocabStore

            vocab = VocabStore(paths.vocab_file)
            engine = voice.create_engine(stopping)  # v2-voice: downloads the model if needed
            t0 = time.perf_counter()
            engine.load()
            log.info("speech engine ready: %s (%.1f s)", engine.name, time.perf_counter() - t0)
            try:
                build = vocab.stt_hints(
                    engine.count_tokens, cfg.stt.max_prompt_tokens, cfg.stt.prompt_mode
                )
                log.info(
                    "vocab: %d terms in the prompt (%d tokens), %d dropped",
                    len(build.included),
                    build.tokens,
                    len(build.dropped),
                )
            except Exception:
                log.exception("couldn't build vocab hints")
            ctx.pipeline = Pipeline(
                config=cfg,
                engine=engine,
                vocab=vocab,
                cleaner=voice.cleaner,  # v2-voice: may have been turned on since start
                injector=injector,
                state=state,
                recent=recent,
            )
            ctx.startup_error = None
            if not server_failed.is_set():
                state.set_status(HubStatus.IDLE)
        except EngineError as exc:
            _engine_failed(str(exc))
        except Exception as exc:
            log.exception("speech engine failed to load")
            _engine_failed(f"Speech engine failed to load: {exc}")

    def _engine_failed(msg: str) -> None:
        log.error("%s", msg)
        ctx.startup_error = msg
        state.set_status(HubStatus.ERROR, msg)
        notify("ShoutAbout error", msg)

    def warm_loop() -> None:
        interval = cfg.cleanup.warm_interval_s
        while not stopping.is_set():
            # v2-voice: "auto" cleanup turns on once Ollama has a suitable model.
            current = voice.cleaner or voice.poll_cleanup(ctx.pipeline)
            if current is None:
                if not voice.cleanup_pending:
                    return
                stopping.wait(min(interval, CLEANUP_RETRY_S) if interval > 0 else CLEANUP_RETRY_S)
                continue
            try:
                ok = current.warm()
                log.debug("cleanup model warm: %s", ok)
                if not ok:
                    log.warning("couldn't load cleanup model %s in Ollama", current.cfg.model)
            except Exception:
                log.exception("cleanup warm-up failed")
            if interval <= 0:
                return
            stopping.wait(interval)

    server_thread = threading.Thread(target=serve, name="server", daemon=True)
    engine_thread = threading.Thread(target=load_engine, name="engine", daemon=True)
    warm_thread = threading.Thread(target=warm_loop, name="warmer", daemon=True)
    server_thread.start()
    engine_thread.start()
    warm_thread.start()
    # --- v2-setup
    v2.announce_when_listening(server, stopping)
    if show_setup and v2.ui is not None:
        v2.ui.open_setup()
    if v2.ui is not None and sys.platform == "win32":
        from shoutabout.reopen import watch

        watch(v2.ui.open_window, stopping)  # Start menu while running: the window, at Home
    # --- end v2-setup
    if greet and not show_setup:
        threading.Thread(
            target=greet_when_ready,
            args=(state, voice, notify, stopping),
            name="greet",
            daemon=True,
        ).start()

    try:
        if tray_icon is not None:
            tray_icon.run()
        else:
            while not server_done.wait(0.5):  # short waits keep Ctrl+C responsive
                pass
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        stopping.set()
        voice.stop()  # v2-voice: removes the hotkey hook and the overlay
        server.should_exit = True
        if not server_done.wait(SERVER_JOIN_TIMEOUT_S):
            log.warning("server didn't stop within %.0f s; forcing exit", SERVER_JOIN_TIMEOUT_S)
            server.force_exit = True
            server_done.wait(2.0)
        wait_idle = getattr(injector, "wait_idle", None)
        if callable(wait_idle):  # let a pending clipboard restore finish before exiting
            try:
                wait_idle(2.0)
            except Exception:
                log.exception("waiting for the clipboard restore failed")
        v2.stop()  # v2-setup: close windows, withdraw the network announcement
        if mac_hub is not None:  # v2-macos
            mac_hub.stop()
    return 1 if server_failed.is_set() else 0


def greet_when_ready(
    state: HubState,
    voice: Any,
    notify: Any,
    stopping: threading.Event,
    *,
    poll_s: float = 0.25,
    wait_s: float = GREET_WAIT_S,
) -> str:
    """After the user opened ShoutAbout: once the speech engine is ready, show the "Ready"
    pill, or a notification when Talk to this PC is off. Returns what it did (tests)."""
    deadline = time.monotonic() + wait_s
    while state.status == HubStatus.STARTING:
        if stopping.wait(poll_s) or time.monotonic() > deadline:
            return "gave up"
    if state.status == HubStatus.ERROR:
        return "error"  # already notified by the engine loader
    from shoutabout.local_dictation import DictationState

    dictation = getattr(voice, "dictation", None)
    if dictation is not None and dictation.state != DictationState.OFF:
        return "pill" if dictation.greet() else "busy"
    notify("ShoutAbout is running", "")
    return "notified"


def _enable_dpi_awareness() -> None:
    try:
        import ctypes

        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # type: ignore[attr-defined]
    except Exception:
        log.debug("couldn't enable DPI awareness", exc_info=True)


def _build_tray(
    paths: Paths,
    cfg: Any,
    token: str,
    state: HubState,
    notify: Any,
    request_quit: Any,
    v2: Any = None,
    mac_hub: Any = None,
) -> Any:
    from shoutabout.tray import TrayActions, TrayIcon

    def guarded(name: str, fn: Any) -> Any:
        def run() -> None:
            try:
                fn()
            except Exception as exc:
                log.exception("tray action %s failed", name)
                notify("ShoutAbout", f"{name} failed: {exc}")

        return run

    ui = getattr(v2, "ui", None)
    services = getattr(v2, "services", None)

    def hint() -> str | None:
        # v2-voice: local_dictation.hint_text(cfg) -> "Press Right Ctrl to talk, and again to stop."
        return services.hint_text(services.config()) if services is not None else None

    # Recent…, Teach words… and Settings… open the one ShoutAbout window on that page.
    actions = TrayActions(
        toggle_pause=guarded("Pause", state.toggle_paused),
        quit=request_quit,
        recent=guarded("Recent", ui.open_recent) if ui is not None else None,
        teach=guarded("Teach words", ui.open_teach) if ui is not None else None,
        settings=guarded("Settings", ui.open_settings) if ui is not None else None,
        setup=guarded("Set up", ui.open_setup) if ui is not None else None,
        hint=hint if services is not None else None,
    )
    if mac_hub is not None:  # v2-macos
        return mac_hub.build_menubar(actions)
    return TrayIcon(state, actions)
