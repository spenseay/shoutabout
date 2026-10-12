"""One UI thread for the ShoutAbout window, the setup window and pairing prompts.

Tk objects must be created, used and freed on a single thread, and that thread must never be
the tray's (its Win32 message loop is separate). :class:`UiHost` starts a "ui" thread on
first use with a hidden Tk root; each window is a Toplevel of it. Any thread asks for a
window with :meth:`open_window` (the one ShoutAbout window, on a page: Home, Teach words,
Recent, Settings), :meth:`open_setup` or (from the pairing manager) a prompt: requests are
queued and picked up by the Tk thread within 100 ms. Asking for the ShoutAbout window while
it's open switches it to that page and brings it to the front. When the last window closes
the thread ends (and starts again on the next request).

Pairing requests go to the setup window when it's showing "Connect your iPhone" (approved
inline there), otherwise to a small always-on-top prompt.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import queue
import threading
from typing import Any

from shoutabout.app_services import AppServices

log = logging.getLogger(__name__)

PUMP_MS = 100
IDLE_EXIT_MS = 1500


class UiHost:
    def __init__(self, services: AppServices) -> None:
        self.svc = services
        self._lock = threading.Lock()
        self._requests: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = False
        # Tk thread only:
        self._windows: dict[str, Any] = {}
        self._idle_ms = 0
        if services.pairing is not None:
            services.pairing.subscribe(self._on_pair_request, self._on_pair_finished)

    # ------------------------------------------------------ any thread

    def open_setup(self, mode: str = "full") -> None:
        self._send("setup", mode)

    def open_window(self, page: str = "home") -> None:
        """The ShoutAbout window on ``page`` ("home", "teach", "recent", "settings")."""
        self._send("main", page)

    def open_settings(self) -> None:
        self.open_window("settings")

    def open_recent(self) -> None:
        self.open_window("recent")

    def open_teach(self) -> None:
        self.open_window("teach")

    def _on_pair_request(self, prompt: Any) -> None:
        self._send("pair", prompt)

    def _on_pair_finished(self, request_id: str, status: Any) -> None:
        self._send("pair-done", (request_id, str(status)), start=False)

    def _send(self, kind: str, arg: Any, *, start: bool = True) -> None:
        with self._lock:
            if self._stopping:
                return
            alive = self._thread is not None and self._thread.is_alive()
            if not alive and not start:
                return
            self._requests.put((kind, arg))
            if not alive:
                self._thread = threading.Thread(target=self._main, name="ui", daemon=True)
                self._thread.start()

    def wait(self) -> None:
        """Block until every window is closed (the CLI's ``setup`` / ``settings``)."""
        while True:
            with self._lock:
                thread = self._thread
            if thread is None or not thread.is_alive():
                return
            thread.join(0.25)  # short joins keep Ctrl+C responsive

    def is_open(self, name: str) -> bool:
        return name in self._windows

    def shutdown(self, timeout: float = 2.0) -> None:
        """Close every window and wait (briefly) for the UI thread to end."""
        with self._lock:
            self._stopping = True
            thread = self._thread
            self._requests.put(("quit", None))
        if self.svc.pairing is not None:
            self.svc.pairing.unsubscribe(self._on_pair_request)
            self.svc.pairing.unsubscribe(self._on_pair_finished)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # ------------------------------------------------------ Tk thread

    def _main(self) -> None:
        while True:
            try:
                self._session()
            except Exception:
                log.exception("the ShoutAbout window thread failed")
            with self._lock:
                if self._stopping or self._requests.empty():
                    self._thread = None
                    return

    def _session(self) -> None:
        from shoutabout import ui_kit

        ui_kit.enable_dpi_awareness()
        import tkinter as tk

        root: Any = tk.Tk()
        root.withdraw()
        self._root = root
        self._idle_ms = 0
        try:
            root.after(0, self._pump)
            root.mainloop()
        finally:
            for window in list(self._windows.values()):
                with contextlib.suppress(Exception):
                    getattr(window, "shutdown", window.close)()
            self._windows.clear()
            with contextlib.suppress(Exception):
                root.destroy()
            # Free every Tk object on this thread (a Tcl object freed elsewhere aborts).
            self._root = None
            root = None
            gc.collect()

    def _pump(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                kind, arg = self._requests.get_nowait()
                if kind == "quit":
                    root.quit()
                    return
                self._handle(kind, arg)
        except queue.Empty:
            pass
        except Exception:
            log.exception("window request failed")
        if self._windows:
            self._idle_ms = 0
        else:
            self._idle_ms += PUMP_MS
            if self._idle_ms >= IDLE_EXIT_MS:
                root.quit()
                return
        root.after(PUMP_MS, self._pump)

    def _toplevel(self) -> Any:
        import tkinter as tk

        return tk.Toplevel(self._root)

    def _closed(self, name: str) -> None:
        self._windows.pop(name, None)

    def _handle(self, kind: str, arg: Any) -> None:
        if kind == "setup":
            self._show_setup(arg)
        elif kind == "main":
            self._show_main(arg)
        elif kind == "pair":
            self._show_pair(arg)
        elif kind == "pair-done":
            request_id, status = arg
            prompt = self._windows.get(f"pair:{request_id}")
            if prompt is not None:  # answered (here or elsewhere), expired or collected
                prompt.close()
            setup = self._windows.get("setup")
            if setup is not None:
                setup.flow.on_pair_finished(request_id, status)

    def _show_setup(self, mode: str) -> None:
        from shoutabout.setup_flow import SetupFlow
        from shoutabout.setup_ui import SetupWindow

        existing = self._windows.get("setup")
        if existing is not None:
            existing.bring_to_front()
            return
        flow = SetupFlow(self.svc, mode=mode or "full")
        self._windows["setup"] = SetupWindow(
            self._toplevel(), flow, on_close=lambda: self._closed("setup")
        )
        self._windows["setup"].bring_to_front()
        pending = self.svc.pairing.pending() if self.svc.pairing is not None else None
        if pending is not None and f"pair:{pending.request_id}" not in self._windows:
            flow.offer(pending)

    def _show_main(self, page: Any) -> None:
        """The one ShoutAbout window, on ``page``; never a second copy."""
        from shoutabout.app_window import AppWindow

        existing = self._windows.get("main")
        if existing is not None:
            existing.show(page)
            existing.bring_to_front()
            return
        self._windows["main"] = AppWindow(
            self._toplevel(),
            self.svc,
            page=page or "home",
            on_close=lambda: self._closed("main"),
            on_add_phone=lambda: self._show_setup("phone"),
        )
        self._windows["main"].bring_to_front()

    def _show_pair(self, prompt: Any) -> None:
        from shoutabout.approval_ui import ApprovalPrompt

        setup = self._windows.get("setup")
        if setup is not None and setup.flow.offer(prompt):
            return
        pairing = self.svc.pairing
        if pairing is None:
            return
        name = f"pair:{prompt.request_id}"
        if name in self._windows:
            return
        self._windows[name] = ApprovalPrompt(
            self._toplevel(),
            prompt,
            approve=pairing.approve,
            deny=pairing.deny,
            seconds_left=pairing.seconds_left,
            on_close=lambda: self._closed(name),
        )
