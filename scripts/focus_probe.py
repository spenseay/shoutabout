"""What would ShoutAbout think of the window you're in? Runs the paste's focus probe.

Click into a window within the delay; the probe then says "text" (a text box has the
focus: paste as usual), "none" (nowhere to type: the dictation is left on the clipboard,
with a pop-up) or "unknown" (paste, and leave the dictation on the clipboard), and why.
It only looks: nothing is typed or pasted.

    .venv\\Scripts\\python.exe scripts\\focus_probe.py            # in 3 s
    .venv\\Scripts\\python.exe scripts\\focus_probe.py --watch    # every second, Ctrl+C stops
    .venv\\Scripts\\python.exe scripts\\focus_probe.py --delay 5 --json

Exit code 0, or 2 when not on Windows or macOS.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from typing import Any


def make_probe() -> Any:
    if sys.platform == "win32":
        from shoutabout.inject.focus_win import WindowsFocusProbe

        return WindowsFocusProbe()
    if sys.platform == "darwin":
        from shoutabout.mac.focus_mac import MacFocusProbe

        return MacFocusProbe()
    return None


def describe(probe: Any) -> dict[str, Any]:
    focus = probe.probe()
    out: dict[str, Any] = {"kind": focus.kind, "reason": focus.reason, "ms": round(focus.ms, 1)}
    facts = getattr(probe, "last_facts", None)
    if facts is not None:
        out["facts"] = asdict(facts)
    return out


def line(result: dict[str, Any]) -> str:
    facts = result.get("facts") or {}
    uia = facts.get("uia") or {}
    where = facts.get("exe") or facts.get("app") or "?"
    detail = ", ".join(
        f"{k}={v}"
        for k, v in (
            ("window", facts.get("window_class")),
            ("focus", facts.get("focus_class")),
            ("caret", facts.get("caret")),
            ("uia", uia.get("control_type")),
            ("uia_class", uia.get("class_name")),
            ("framework", uia.get("framework")),
            ("value", uia.get("value_pattern")),
            ("read_only", uia.get("value_read_only")),
            ("text", uia.get("text_pattern")),
            ("text_edit", uia.get("text_edit_pattern")),
            ("role", facts.get("role")),
            ("subrole", facts.get("subrole")),
            ("error", facts.get("uia_error") or facts.get("error")),
        )
        if v not in (None, "", False)
    )
    return f"{result['kind']:<7} {result['reason']:<32} {result['ms']:>5.0f} ms  {where}  {detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--delay", type=float, default=3.0, help="seconds before looking")
    parser.add_argument("--watch", action="store_true", help="look every second until Ctrl+C")
    parser.add_argument("--json", action="store_true", help="print JSON with every fact")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    probe = make_probe()
    if probe is None:
        print("[SKIP] the focus probe needs Windows or macOS")
        return 2
    if not args.watch:
        print(f"Click into a window; looking in {args.delay:g} s…", file=sys.stderr)
        time.sleep(max(0.0, args.delay))
        result = describe(probe)
        print(json.dumps(result, indent=2) if args.json else line(result))
        return 0
    try:
        while True:
            result = describe(probe)
            print(json.dumps(result) if args.json else line(result), flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
