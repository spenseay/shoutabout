"""Shared result types passed between the pipeline, server, Recent and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

Route = Literal["lan", "tunnel"]
Target = Literal["pc", "phone"]
TARGETS: tuple[str, ...] = ("pc", "phone")

CleanupStatus = Literal["applied", "fallback", "disabled", "skipped"]


class HubStatus(StrEnum):
    STARTING = "starting"
    IDLE = "idle"
    TRANSCRIBING = "transcribing"
    ERROR = "error"


@dataclass
class Timings:
    """Stage durations in milliseconds."""

    decode: float = 0.0
    stt: float = 0.0
    cleanup: float = 0.0
    paste: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, int]:
        return {
            "decode": round(self.decode),
            "stt": round(self.stt),
            "cleanup": round(self.cleanup),
            "paste": round(self.paste),
            "total": round(self.total),
        }


@dataclass
class CleanupResult:
    text: str
    status: CleanupStatus
    reason: str | None = None  # why it fell back / was skipped
    duration_ms: float = 0.0

    @property
    def fell_back(self) -> bool:
        return self.status == "fallback"


@dataclass
class PasteResult:
    pasted: bool
    warning: str | None = None


@dataclass
class TranscribeResult:
    text: str
    raw_text: str
    target: Target
    route: str  # "lan" | "tunnel" for HTTP requests, "cli" for the CLI
    pasted: bool
    warning: str | None
    cleanup: CleanupResult
    timings: Timings
    audio_seconds: float = 0.0
    request_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_response(self) -> dict[str, Any]:
        """The public API contract of POST /transcribe (see PLAN.md)."""
        return {
            "text": self.text,
            "raw_text": self.raw_text,
            "target": self.target,
            "pasted": self.pasted,
            "warning": self.warning,
            # "ok" | "warning": an exact string the iPhone Shortcut can compare against
            # ("has any value" is true even for empty/null values on current iOS).
            "status": "ok" if self.warning is None else "warning",
            "route": self.route,
            "cleanup": self.cleanup.status,
            "timings_ms": self.timings.as_dict(),
        }
