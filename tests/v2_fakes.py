"""Fakes for the setup / settings logic: hardware, model choices, and AppServices."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from shoutabout.app_services import AppServices
from shoutabout.config import Paths, ensure_initialized
from shoutabout.devices import DeviceStore
from shoutabout.identity import IdentityStore
from shoutabout.pair_requests import PairingManager


@dataclass(frozen=True)
class Choice:
    tier: str
    stt_model: str
    device: str
    compute_type: str
    download_mb: int
    cleanup: bool
    speed: str
    label: str


GPU = SimpleNamespace(name="NVIDIA GeForce RTX 4080 SUPER", vram_gb=16.0, cuda_usable=True)
GPU_HW = SimpleNamespace(gpus=(GPU,), best_gpu=GPU, cpu_name="Ryzen 7", ram_gb=32)
CPU_HW = SimpleNamespace(gpus=(), best_gpu=None, cpu_name="Intel Core i5-8250U", ram_gb=8)
BEST = Choice("best", "large-v3-turbo", "cuda", "float16", 1600, True, "fast", "Best")
BALANCED = Choice("balanced", "distil-large-v3", "cuda", "int8_float16", 1500, True, "ok", "Bal")
LIGHT = Choice("light", "base.en", "cpu", "int8", 150, False, "slow", "Light")
TIERS = {"best": BEST, "balanced": BALANCED, "light": LIGHT}
HOTKEYS = {"right_ctrl": "Right Ctrl", "right_alt": "Right Alt", "f8": "F8"}


def inline_runner(work, _name) -> None:
    """Run background work at once, on the calling thread (deterministic tests)."""
    work()


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


class Downloads:
    """Fake modelstore: records downloads, reports progress, can fail or block."""

    def __init__(self, ready: set[str] | None = None, fail: bool = False) -> None:
        self.ready = set(ready or ())
        self.fail = fail
        self.calls: list[str] = []
        self.gate: threading.Event | None = None

    def is_ready(self, model: str) -> bool:
        return model in self.ready

    def download(self, model: str, *, progress, cancel) -> str:
        self.calls.append(model)
        progress(0, 1000)
        if self.gate is not None:
            self.gate.wait(5)
        if cancel.is_set():
            raise RuntimeError("cancelled")
        progress(500, 1000)
        if self.fail:
            raise OSError("no internet")
        progress(1000, 1000)
        self.ready.add(model)
        return f"/models/{model}"


class FakeAccess:
    """The macOS permissions as the setup sees them (shoutabout.mac.access status dicts).
    ``ask`` records the key and grants it when ``grant_on_ask`` (a click in macOS's dialog)."""

    def __init__(
        self,
        microphone: str = "undetermined",
        accessibility: bool | None = False,
        input_monitoring: bool | None = False,
        *,
        grant_on_ask: bool = False,
        answer: str = "opened",
    ) -> None:
        self.now = {
            "microphone": microphone,
            "accessibility": accessibility,
            "input_monitoring": input_monitoring,
            "hotkey": False,
        }
        self.grant_on_ask = grant_on_ask
        self.answer = answer
        self.asked: list[str] = []
        self.reads = 0
        self.broken = False

    def status(self) -> dict:
        self.reads += 1
        if self.broken:
            raise ConnectionError("the hub went away")
        return dict(self.now)

    def ask(self, which: str) -> str:
        self.asked.append(which)
        if self.grant_on_ask:
            self.grant(which)
        return self.answer

    def grant(self, *keys: str) -> None:
        for key in keys:
            self.now[key] = "granted" if key == "microphone" else True


def make_services(
    tmp_path,
    *,
    hw: Any = GPU_HW,
    choice: Any = BEST,
    downloads: Downloads | None = None,
    with_pairing: bool = True,
    saved: list | None = None,
    restart: set[str] | None = None,
    **overrides: Any,
) -> AppServices:
    paths = Paths(tmp_path / "home")
    ensure_initialized(paths)
    overrides.setdefault("has_microphone", lambda: True)  # never ask the real PC
    overrides.setdefault("copy_text", lambda _text: None)  # never touch the clipboard
    overrides.setdefault("cleanup_available", lambda _cfg: False)  # never ask a real Ollama
    devices = DeviceStore(paths.devices_file)
    clock = Clock()
    downloads = downloads or Downloads(ready={"large-v3-turbo"})
    svc = AppServices(
        paths=paths,
        devices=devices,
        identity=IdentityStore(paths.identity_file, default_name=lambda: "DESK-1"),
        pairing=(
            PairingManager(devices, clock=clock, code_factory=lambda: "4821")
            if with_pairing
            else None
        ),
        detect_hardware=lambda **_k: hw,
        resolve_model=lambda _cfg, _hw=None: choice,
        auto_model=lambda _hw: choice,
        tier_choice=lambda tier, _hw: TIERS.get(tier),
        model_ready=downloads.is_ready,
        download_model=downloads.download,
        hotkey_choices=lambda: dict(HOTKEYS),
        hint_text=lambda _cfg: "Hold Right Ctrl to talk",
        version="0.2.0",
        **overrides,
    )
    svc.extra["clock"] = clock
    svc.extra["downloads"] = downloads
    if saved is not None:
        # Record settings writes instead of touching the schema-checked config.toml.
        def save(changes):
            saved.append({k: dict(v) for k, v in changes.items()})
            return set(restart or ())

        svc.save_settings = save  # type: ignore[method-assign]
    return svc


def shown_marks(widget: Any) -> int:
    """How many brand marks show in ``widget``'s tree: a mouth drawn on a canvas (polygons:
    macOS) or any picture in a label or button (the bitmap mouth, an icon). Widgets that
    aren't laid out (a hidden page, a button not packed) don't count."""
    total = 0
    for child in widget.winfo_children():
        if child.winfo_manager():
            total += _is_mark(child) + shown_marks(child)
    return total


def _is_mark(widget: Any) -> bool:
    kind = widget.winfo_class()
    if kind == "Canvas":
        return any(widget.type(i) == "polygon" for i in widget.find_all())
    return kind in ("Label", "Button") and bool(str(widget.cget("image")))
