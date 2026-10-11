"""``shoutabout doctor`` on macOS: permissions, Bonjour, the chip, open at login.

macOS answers permission questions for the *responsible* app: run from Terminal, that's
Terminal, not ShoutAbout. So the permission lines say so, and the running app writes its
own view to hub.log at start ("permissions: microphone=granted, accessibility=True, ...").
"""

from __future__ import annotations

from pathlib import Path

from shoutabout.mac import permissions

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
SEEN_FROM = " (as macOS sees this process)"

Row = tuple[str, str, str, str | None]  # name, status, detail, fix


def permission_rows(report: permissions.PermissionReport, in_app: bool = False) -> list[Row]:
    """``in_app``: asked from inside the running app (else macOS answers for the Terminal
    that runs ``doctor``, and the lines say so)."""
    note = "" if in_app else SEEN_FROM
    rows: list[Row] = []
    mic = report.microphone
    if mic == "granted":
        rows.append(("Microphone", PASS, "allowed" + note, None))
    elif mic == "undetermined":
        rows.append(("Microphone", WARN, "not asked yet" + note, "macOS asks the first time"))
    elif mic in ("denied", "restricted"):
        rows.append(("Microphone", FAIL, mic + note, permissions.MICROPHONE_HELP))
    else:
        rows.append(("Microphone", SKIP, "unknown", None))
    if report.accessibility is True:
        rows.append(("Accessibility", PASS, "allowed: pasting and the hotkey work" + note, None))
    elif report.accessibility is False:
        rows.append(("Accessibility", FAIL, "not allowed" + note, permissions.ACCESSIBILITY_HELP))
    else:
        rows.append(("Accessibility", SKIP, "unknown", None))
    if report.input_monitoring is True:
        rows.append(("Input Monitoring", PASS, "allowed" + note, None))
    elif report.accessibility is True:
        rows.append(("Input Monitoring", PASS, "not needed (Accessibility is on)", None))
    elif report.input_monitoring is False:
        rows.append(
            ("Input Monitoring", WARN, "not allowed" + note, permissions.INPUT_MONITORING_HELP)
        )
    return rows


def macos_rows(in_app: bool = False) -> list[Row]:
    from shoutabout.mac.discovery_mac import DNS_SD
    from shoutabout.mac.startup_mac import MacStartupManager
    from shoutabout.mac.sysinfo import probe_apple_chip

    rows = permission_rows(permissions.report(), in_app)
    chip = probe_apple_chip()
    if chip is None:
        rows.append(("Apple Silicon", FAIL, "not an Apple Silicon Mac", "needs M1 or newer"))
    else:
        rows.append(
            (
                "Apple Silicon",
                PASS,
                f"{chip.name}: {chip.performance_cores} performance + "
                f"{chip.efficiency_cores} efficiency cores",
                None,
            )
        )
    if Path(DNS_SD).exists():
        rows.append(("Bonjour (dns-sd)", PASS, DNS_SD, None))
    else:
        rows.append(("Bonjour (dns-sd)", FAIL, f"{DNS_SD} is missing", "pair with the QR code"))
    state = MacStartupManager().state()
    rows.append(("Open at login", PASS if state.enabled else SKIP, state.note, None))
    return rows
