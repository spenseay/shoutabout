"""End-to-end paste self-test on a real Windows desktop.

It ONLY ever pastes into its own small Tk window: the foreground window is checked to be
that window right before every keystroke (and the keystroke is refused otherwise). The
only keys sent are the paste chords (Ctrl+V, then Shift+Insert). Your clipboard is
saved first and put back at the end.

    .venv\\Scripts\\python.exe scripts\\paste_selftest.py

Exit code 0 = all checks passed, 1 = a check failed / aborted, 2 = not Windows.
"""

from __future__ import annotations

import sys
import time
import uuid

CTRL_V_TEXT = "ShoutAbout paste self-test ✓ 123"
SHIFT_INSERT_TEXT = "ShoutAbout shift-insert self-test ✓ 456"
FOREGROUND_WAIT_S = 10.0
RESTORE_DELAY_MS = 500


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        self.results.append((name, bool(ok)))
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(ok for _, ok in self.results)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    if sys.platform != "win32":
        print("[SKIP] paste self-test needs Windows")
        return 2

    import tkinter as tk

    from shoutabout.config import PasteConfig
    from shoutabout.inject import clipboard as cb
    from shoutabout.inject import win32 as w
    from shoutabout.inject.windows import PasteChord, Win32Api, WindowsPasteInjector

    report = Report()
    marker_ids = cb.exclusion_format_ids()

    class PrintNotifier:
        def notify(self, title: str, message: str) -> None:
            print(f"  (notification) {title}: {message}")

    class GuardedApi(Win32Api):
        """Win32Api that refuses to send the chord unless our window is in the foreground,
        and records what was on the clipboard at that moment."""

        def __init__(self, frame: int) -> None:
            self.frame = frame
            self.sent: list[PasteChord] = []
            self.refused = 0
            self.formats_at_send: list[tuple[int, str | None]] = []
            self.text_at_send: str | None = None

        def send_chord(self, chord: PasteChord) -> int:
            self.formats_at_send = cb.list_formats()
            self.text_at_send = cb.get_text()
            if not is_ours(w.user32.GetForegroundWindow(), self.frame):
                self.refused += 1
                print("  ABORT: the test window lost focus; keystroke not sent")
                return 0
            self.sent.append(chord)
            return super().send_chord(chord)

    def is_ours(hwnd: int | None, frame: int) -> bool:
        if not hwnd:
            return False
        return hwnd == frame or w.user32.GetAncestor(hwnd, w.GA_ROOT) == frame

    original = cb.snapshot()
    print(f"saved your clipboard: {original!r}")
    root = None
    try:
        # ---- 1. history/cloud exclusion markers are written with the text
        probe = f"ShoutAbout exclusion probe {uuid.uuid4().hex[:8]}"
        seq = cb.set_text(probe, exclude_from_history=True)
        formats = cb.list_formats()
        names = {name for _, name in formats if name}
        report.check(
            "exclusion formats present right after set_text",
            set(cb.EXCLUSION_FORMAT_NAMES) <= names,
            ", ".join(sorted(n for n in names if n in cb.EXCLUSION_FORMAT_NAMES)),
        )
        snap = cb.snapshot()
        report.check(
            "CanIncludeInClipboardHistory / CanUploadToCloudClipboard = DWORD 0",
            snap.get(marker_ids[cb.HISTORY_FORMAT_NAME]) == b"\0\0\0\0"
            and snap.get(marker_ids[cb.CLOUD_FORMAT_NAME]) == b"\0\0\0\0",
        )
        report.check("set_text returns the clipboard sequence number", seq == cb.sequence_number())
        report.check("get_text round trip", cb.get_text() == probe)

        # ---- 2. a known marker that the paste must restore afterwards
        marker = f"ShoutAbout self-test marker {uuid.uuid4().hex[:8]}"
        cb.set_text(marker, exclude_from_history=True)

        root = tk.Tk()
        root.title("ShoutAbout paste self-test")
        root.geometry("560x140+200+200")
        root.attributes("-topmost", True)
        text = tk.Text(root, height=4, width=60, font=("Segoe UI", 12))
        text.pack(fill="both", expand=True, padx=8, pady=8)
        root.update()
        frame = int(root.wm_frame(), 16)
        own_pid = w.kernel32.GetCurrentProcessId()
        w.user32.AllowSetForegroundWindow(own_pid)
        root.lift()
        root.focus_force()
        text.focus_force()
        w.user32.SetForegroundWindow(frame)

        def pump(seconds: float, until=None) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                root.update()
                if until is not None and until():
                    return
                time.sleep(0.01)

        pump(0.3, lambda: is_ours(w.user32.GetForegroundWindow(), frame))
        if not is_ours(w.user32.GetForegroundWindow(), frame):
            print("couldn't get foreground; click the test window")
            pump(FOREGROUND_WAIT_S, lambda: is_ours(w.user32.GetForegroundWindow(), frame))
        text.focus_force()
        pump(0.1)
        if not report.check(
            "test window is the foreground window",
            is_ours(w.user32.GetForegroundWindow(), frame),
            f"hwnd {frame:#x}",
        ):
            print("aborting without pasting")
            return 1

        api = GuardedApi(frame)
        exe = api.process_exe_name(api.window_pid(frame)) or "python.exe"

        def run_paste(label: str, payload: str, cfg: PasteConfig, chord: PasteChord) -> None:
            text.delete("1.0", "end")
            cb.set_text(marker, exclude_from_history=True)
            injector = WindowsPasteInjector(cfg, PrintNotifier(), api=api)
            sent_before = len(api.sent)
            if not is_ours(w.user32.GetForegroundWindow(), frame):
                report.check(f"{label}: foreground is still the test window", False)
                return
            t0 = time.perf_counter()
            result = injector.paste(payload)
            paste_ms = (time.perf_counter() - t0) * 1000
            pump(0.4, lambda: text.get("1.0", "end-1c") == payload)
            content = text.get("1.0", "end-1c")
            report.check(
                f"{label}: paste() reported success",
                result.pasted,
                f"{paste_ms:.0f} ms" if result.pasted else str(result.warning),
            )
            report.check(
                f"{label}: chord sent",
                api.sent[sent_before:] == [chord],
                ", ".join(c.value for c in api.sent[sent_before:]) or "none",
            )
            report.check(f"{label}: pasted text matches exactly", content == payload, repr(content))
            at_send = {name for _, name in api.formats_at_send if name}
            report.check(
                f"{label}: transcript + exclusion formats on clipboard at keystroke",
                api.text_at_send == payload and set(cb.EXCLUSION_FORMAT_NAMES) <= at_send,
            )
            injector.wait_idle(5)
            restored = cb.get_text()
            report.check(
                f"{label}: previous clipboard restored after {cfg.restore_delay_ms} ms",
                restored == marker,
                repr(restored),
            )

        run_paste(
            "Ctrl+V",
            CTRL_V_TEXT,
            PasteConfig(restore_delay_ms=RESTORE_DELAY_MS),
            PasteChord.CTRL_V,
        )
        run_paste(
            "Shift+Insert",
            SHIFT_INSERT_TEXT,
            PasteConfig(restore_delay_ms=RESTORE_DELAY_MS, shift_insert_processes=[exe]),
            PasteChord.SHIFT_INSERT,
        )
        report.check("no keystroke was refused by the focus guard", api.refused == 0)
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        try:
            cb.restore(original)
            after = cb.snapshot()
            same = all(after.get(fmt) == data for fmt, data in original.formats)
            if original.is_empty:
                same = after.is_empty
            report.check("your original clipboard was put back", same, repr(after))
        except Exception as exc:
            report.check("your original clipboard was put back", False, str(exc))

    print("RESULT:", "PASS" if report.ok else "FAIL")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
