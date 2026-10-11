"""Automatic model choice: which Whisper model, device and precision this PC should use,
and whether the LLM cleanup is worth running.

People never have to see a model name. :func:`choose` maps the hardware
(:mod:`shoutabout.hardware`) to one of three tiers; :func:`resolve` applies config.toml on
top, where any explicit value wins over ``"auto"`` (so an existing config with explicit
values behaves exactly as before).

The tiers and every threshold come from measurements on the build PC (RTX 4080 SUPER,
i7-14700KF) with the repo's test clips; the numbers and the reasoning are in
docs/models.md. In short, for a 10 s dictation:

=========  =============================  ==========================  =========  =====
tier       when                           model / device / precision  10 s clip  speed
=========  =============================  ==========================  =========  =====
best       NVIDIA, CUDA ok, >= 5 GB VRAM  large-v3-turbo, cuda, fp16  0.17 s     fast
balanced   NVIDIA, CUDA ok, 3-5 GB VRAM   large-v3-turbo, cuda, int8  0.17 s     ok
balanced   NVIDIA, CUDA ok, 1.5-3 GB      small.en, cuda, int8/fp16   0.16 s     ok
balanced   no usable GPU, >= 6 cores and  small.en, cpu, int8         1.2 s *    ok
           >= 8 GB RAM
light      anything else                  base.en, cpu, int8          0.4 s *    slow
=========  =============================  ==========================  =========  =====

\\* on the build PC's fast cores; expect roughly 2-2.5x longer on a typical laptop.

Cleanup (local Ollama) only runs on the best tier with at least 8 GB of VRAM (Whisper
2.4 GB + the 4B cleanup model 3 GB + room for the desktop), and only when a known-good
model is already pulled in a reachable Ollama. Nothing is ever downloaded for it.

Apple Silicon Macs (v2-macos) run Whisper on the CPU's performance cores (CTranslate2 with
Apple Accelerate): ``small.en`` int8 ("balanced", speed "ok") with at least 4 performance
cores and 8 GB, otherwise ``base.en`` ("light"). ``large-v3-turbo`` only when picked by
hand. See docs/models.md ("Macs") for the estimates behind this; MLX on the GPU is the
future speed-up.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from shoutabout import modelstore

if TYPE_CHECKING:
    from shoutabout.config import Config, SttConfig
    from shoutabout.hardware import GpuInfo, HardwareInfo

log = logging.getLogger(__name__)

Tier = Literal["best", "balanced", "light"]
Speed = Literal["fast", "ok", "slow"]
Device = Literal["cuda", "cpu"]

AUTO = "auto"
TIERS: tuple[Tier, ...] = ("best", "balanced", "light")
SPEED: dict[str, Speed] = {"best": "fast", "balanced": "ok", "light": "slow"}
LABEL: dict[str, str] = {"best": "Best quality", "balanced": "Balanced", "light": "Light"}

BEST_MODEL = "large-v3-turbo"
SMALL_GPU_MODEL = "small.en"
CPU_MODEL = "small.en"
LIGHT_MODEL = "base.en"

# Thresholds (GiB of total VRAM / physical cores / GiB of RAM). See docs/models.md.
BEST_MIN_VRAM_GB = 5.0  # large-v3-turbo fp16 adds 2.4 GB; "6 GB" cards report ~6.0
TURBO_INT8_MIN_VRAM_GB = 3.0  # large-v3-turbo int8_float16 adds 1.1 GB
GPU_MIN_VRAM_GB = 1.5  # small.en adds 0.6 GB; below this the GPU isn't worth it
CLEANUP_MIN_VRAM_GB = 8.0  # Whisper 2.4 GB + cleanup LLM 3.0 GB + desktop headroom
STRONG_CPU_CORES = 6
STRONG_CPU_RAM_GB = 8.0
MAX_CPU_THREADS = 8  # more threads didn't help (hybrid CPUs: the extra ones are E-cores)

# v2-macos: Apple Silicon (CPU only; docs/models.md "Macs").
APPLE_BALANCED_MIN_PERF_CORES = 4
APPLE_BALANCED_MIN_RAM_GB = 8.0
APPLE_TURBO_OK_PERF_CORES = 8  # a hand-picked "Best" isn't called slow from 8 fast cores
APPLE_TURBO_OK_RAM_GB = 16.0
APPLE_MAX_THREADS = 10

# Cleanup models known to work with the prompt and the guard, best first (docs/licenses.md).
KNOWN_CLEANUP_MODELS: tuple[str, ...] = ("qwen3:4b-instruct-2507-q4_K_M", "granite4:micro")
OLLAMA_PROBE_TIMEOUT_S = 1.0

# Precision preference per device, best first.
GPU_COMPUTE_PREFERENCE = ("float16", "int8_float16", "int8", "float32")
GPU_COMPACT_PREFERENCE = ("int8_float16", "int8", "float16", "float32")


@dataclass(frozen=True)
class ModelChoice:
    tier: Tier
    stt_model: str
    device: Device
    compute_type: str
    download_mb: int
    cleanup: bool
    cleanup_model: str | None
    speed: Speed
    label: str  # "Best quality" / "Balanced" / "Light"
    explanation: str  # one or two plain sentences for the setup/settings window
    cpu_threads: int = 0  # CTranslate2 threads on the CPU (0 = its default)
    # "auto" = picked from the hardware; "manual" = a tier the person picked in Settings;
    # "config" = config.toml set everything; "mixed" = some of each.
    source: Literal["auto", "manual", "config", "mixed"] = "auto"
    cleanup_note: str = ""  # why cleanup is on or off, in plain words

    @property
    def summary(self) -> str:
        """One log line: ``Best quality: large-v3-turbo on cuda/float16, cleanup on (...)``."""
        cleanup = f"cleanup on ({self.cleanup_model})" if self.cleanup else "cleanup off"
        threads = (
            f", {self.cpu_threads} threads" if self.device == "cpu" and self.cpu_threads else ""
        )
        return (
            f"{self.label}: {self.stt_model} on {self.device}/{self.compute_type}{threads}, "
            f"{cleanup} [{self.source}]"
        )


# --------------------------------------------------------------------------- helpers


def _first_supported(gpu: GpuInfo | None, preference: Sequence[str]) -> str:
    """The first precision in ``preference`` the GPU supports (all, if unknown)."""
    if gpu is None or not gpu.compute_types:
        return preference[0]
    for compute in preference:
        if gpu.supports(compute):
            return compute
    return "float32"


def cpu_threads_for(hw: HardwareInfo) -> int:
    perf = getattr(hw, "performance_cores", 0) or 0
    if getattr(hw, "apple_chip", None) and perf > 0:  # v2-macos: the fast cores only
        return max(1, min(perf, APPLE_MAX_THREADS))
    return max(1, min(hw.physical_cores, MAX_CPU_THREADS))


def _short_cpu(name: str) -> str:
    for junk in ("(R)", "(TM)", "(tm)", " CPU", " Processor"):
        name = name.replace(junk, "")
    name = name.split("@")[0]
    return " ".join(name.split()) or "processor"


def _cores(hw: HardwareInfo) -> str:
    return f"{hw.physical_cores} core" + ("s" if hw.physical_cores != 1 else "")


def pick_cleanup_model(
    ollama_models: Sequence[str] | None, preferred: str | None = None
) -> str | None:
    """The configured model if it's pulled, else the first known-good one that is."""
    if not ollama_models:
        return None
    from shoutabout.cleanup import same_model

    candidates = [m for m in (preferred, *KNOWN_CLEANUP_MODELS) if m]
    for want in candidates:
        if any(same_model(have, want) for have in ollama_models):
            return want
    return None


def cleanup_affordable(tier: Tier, gpu: GpuInfo | None) -> bool:
    return tier == "best" and gpu is not None and gpu.vram_gb >= CLEANUP_MIN_VRAM_GB


def _cleanup_decision(
    tier: Tier,
    gpu: GpuInfo | None,
    ollama_models: Sequence[str] | None,
    preferred: str | None,
) -> tuple[bool, str | None, str]:
    if not cleanup_affordable(tier, gpu):
        return False, None, "Text tidying is off: it needs a graphics card with 8 GB or more."
    if ollama_models is None:
        return False, None, "Text tidying is off: Ollama isn't running."
    model = pick_cleanup_model(ollama_models, preferred)
    if model is None:
        return False, None, "Text tidying is off: no suitable model is installed in Ollama."
    return True, model, "Text tidying is on (local Ollama)."


# ---------------------------------------------------------------------------- choose


def choose(
    hw: HardwareInfo,
    *,
    ollama_models: Sequence[str] | None = None,
    device: Device | None = None,
    preferred_cleanup: str | None = None,
) -> ModelChoice:
    """The automatic choice for this hardware.

    ``ollama_models``: tags pulled in a reachable Ollama (None = not reachable / not
    asked), which decides whether cleanup can run. ``device`` forces the device (used when
    config.toml pins it).
    """
    if getattr(hw, "apple_chip", None):  # v2-macos
        return _apple_choice(hw)
    gpu = hw.best_gpu
    usable = gpu if gpu is not None and gpu.cuda_usable else None
    if device == "cpu" or (device is None and (usable is None or usable.vram_gb < GPU_MIN_VRAM_GB)):
        return _cpu_choice(hw, gpu, forced=device == "cpu")
    return _gpu_choice(hw, usable or gpu, ollama_models, preferred_cleanup)


def _gpu_choice(
    hw: HardwareInfo,
    gpu: GpuInfo | None,
    ollama_models: Sequence[str] | None,
    preferred_cleanup: str | None,
) -> ModelChoice:
    vram = gpu.vram_gb if gpu is not None else 0.0
    name = gpu.name if gpu is not None else "graphics card"
    if gpu is None or vram >= BEST_MIN_VRAM_GB:
        tier: Tier = "best"
        model = BEST_MODEL
        compute = _first_supported(gpu, GPU_COMPUTE_PREFERENCE)
        explanation = f"Your {name} runs the most accurate speech model."
    elif vram >= TURBO_INT8_MIN_VRAM_GB:
        tier, model = "balanced", BEST_MODEL
        compute = _first_supported(gpu, GPU_COMPACT_PREFERENCE)
        explanation = (
            f"Your {name} ({vram:.0f} GB) runs the most accurate speech model in a compact form."
        )
    else:
        tier, model = "balanced", SMALL_GPU_MODEL
        compute = _first_supported(gpu, GPU_COMPACT_PREFERENCE)
        explanation = f"Your {name} ({vram:.0f} GB) runs a smaller, fast speech model."
    cleanup, cleanup_model, note = _cleanup_decision(tier, gpu, ollama_models, preferred_cleanup)
    return ModelChoice(
        tier=tier,
        stt_model=model,
        device="cuda",
        compute_type=compute,
        download_mb=modelstore.download_mb(model),
        cleanup=cleanup,
        cleanup_model=cleanup_model,
        speed=SPEED[tier],
        label=LABEL[tier],
        explanation=explanation,
        cpu_threads=cpu_threads_for(hw),
        cleanup_note=note,
    )


def _cpu_choice(hw: HardwareInfo, gpu: GpuInfo | None, *, forced: bool) -> ModelChoice:
    cpu = _short_cpu(hw.cpu_name)
    if gpu is None:
        why = "No NVIDIA graphics card was found, so"
    elif forced:
        why = "Set to use the processor, so"
    elif not gpu.cuda_usable:
        why = f"Your {gpu.name} can't be used right now ({gpu.reason or 'CUDA unavailable'}), so"
    else:
        why = f"Your {gpu.name} has too little memory, so"
    strong = hw.physical_cores >= STRONG_CPU_CORES and hw.ram_gb >= STRONG_CPU_RAM_GB
    if strong:
        tier: Tier = "balanced"
        model = CPU_MODEL
        explanation = f"{why} your {cpu} ({_cores(hw)}) runs a fast, accurate speech model."
    else:
        tier, model = "light", LIGHT_MODEL
        explanation = f"{why} your {cpu} ({_cores(hw)}) runs a light speech model."
    return ModelChoice(
        tier=tier,
        stt_model=model,
        device="cpu",
        compute_type="int8",
        download_mb=modelstore.download_mb(model),
        cleanup=False,
        cleanup_model=None,
        speed=SPEED[tier],
        label=LABEL[tier],
        explanation=explanation,
        cpu_threads=cpu_threads_for(hw),
        cleanup_note="Text tidying is off: it needs a graphics card with 8 GB or more.",
    )


# --- v2-macos: Apple Silicon -----------------------------------------------------------


def _perf_cores_text(hw: HardwareInfo) -> str:
    n = getattr(hw, "performance_cores", 0) or hw.physical_cores
    return f"{n} performance core" + ("s" if n != 1 else "")


def apple_strong(hw: HardwareInfo) -> bool:
    perf = getattr(hw, "performance_cores", 0) or 0
    return perf >= APPLE_BALANCED_MIN_PERF_CORES and hw.ram_gb >= APPLE_BALANCED_MIN_RAM_GB


def _apple_choice(hw: HardwareInfo) -> ModelChoice:
    chip = getattr(hw, "apple_chip", None) or "Mac"
    if apple_strong(hw):
        tier: Tier = "balanced"
        model = CPU_MODEL
        explanation = f"Your {chip} ({_perf_cores_text(hw)}) runs a fast, accurate speech model."
    else:
        tier, model = "light", LIGHT_MODEL
        explanation = f"Your {chip} ({_perf_cores_text(hw)}) runs a light speech model."
    return ModelChoice(
        tier=tier,
        stt_model=model,
        device="cpu",
        compute_type="int8",
        download_mb=modelstore.download_mb(model),
        cleanup=False,
        cleanup_model=None,
        speed=SPEED[tier],
        label=LABEL[tier],
        explanation=explanation,
        cpu_threads=cpu_threads_for(hw),
        cleanup_note="Text tidying is off on Macs for now.",
    )


def _apple_tier(tier: Tier, hw: HardwareInfo) -> ModelChoice:
    """A hand-picked tier on an Apple Silicon Mac (everything runs on the CPU)."""
    perf = getattr(hw, "performance_cores", 0) or 0
    warning = ""
    if tier == "best":
        model = BEST_MODEL
        if perf < APPLE_TURBO_OK_PERF_CORES or hw.ram_gb < APPLE_TURBO_OK_RAM_GB:
            warning = "It will be slow on this Mac: it waits a few seconds after you stop."
    elif tier == "balanced":
        model = CPU_MODEL
        if not apple_strong(hw):
            warning = "It may be slow on this Mac."
    else:
        model = LIGHT_MODEL
    explanation = f"Chosen manually: {LABEL[tier]}."
    if warning:
        explanation += " " + warning
    return ModelChoice(
        tier=tier,
        stt_model=model,
        device="cpu",
        compute_type="int8",
        download_mb=modelstore.download_mb(model),
        cleanup=False,
        cleanup_model=None,
        speed="slow" if warning else ("ok" if tier == "best" else SPEED[tier]),
        label=LABEL[tier],
        explanation=explanation,
        cpu_threads=cpu_threads_for(hw),
        source="manual",
        cleanup_note="Text tidying is off on Macs for now.",
    )


# --- end v2-macos ----------------------------------------------------------------------


# ------------------------------------------------------------------- manual tier


def choose_tier(
    tier: Tier,
    hw: HardwareInfo,
    *,
    ollama_models: Sequence[str] | None = None,
    preferred_cleanup: str | None = None,
) -> ModelChoice:
    """The setup for a quality tier the person picked (Settings: Best / Balanced / Light).

    The tier is honoured even when this PC can't really run it (Best without a usable
    graphics card runs on the processor); the explanation then says it will be slow, and
    ``speed`` is "slow" so the phone transcribes on its own instead.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if getattr(hw, "apple_chip", None):  # v2-macos
        return _apple_tier(tier, hw)
    gpu = hw.best_gpu
    usable = gpu if gpu is not None and gpu.cuda_usable and gpu.vram_gb >= GPU_MIN_VRAM_GB else None
    strong = hw.physical_cores >= STRONG_CPU_CORES and hw.ram_gb >= STRONG_CPU_RAM_GB
    warning = ""
    device: Device
    if tier == "best":
        model = BEST_MODEL
        if usable is not None:
            device = "cuda"
            big = usable.vram_gb >= BEST_MIN_VRAM_GB
            compute = _first_supported(
                usable, GPU_COMPUTE_PREFERENCE if big else GPU_COMPACT_PREFERENCE
            )
            if usable.vram_gb < TURBO_INT8_MIN_VRAM_GB:
                warning = "It may not fit in your graphics card's memory and could be slow."
        else:
            device, compute = "cpu", "int8"
            warning = (
                "It will be slow on this PC: there's no graphics card it can use, so it runs "
                "on the processor."
            )
    elif tier == "balanced":
        if usable is not None:
            device = "cuda"
            model = BEST_MODEL if usable.vram_gb >= TURBO_INT8_MIN_VRAM_GB else SMALL_GPU_MODEL
            compute = _first_supported(usable, GPU_COMPACT_PREFERENCE)
        else:
            device, model, compute = "cpu", CPU_MODEL, "int8"
            if not strong:
                warning = "It may be slow on this processor."
    else:  # light: the smallest model, on the processor, leaving the graphics card free
        device, model, compute = "cpu", LIGHT_MODEL, "int8"
    cleanup, cleanup_model, note = _cleanup_decision(
        tier, usable if device == "cuda" else None, ollama_models, preferred_cleanup
    )
    explanation = f"Chosen manually: {LABEL[tier]}."
    if warning:
        explanation += " " + warning
    return ModelChoice(
        tier=tier,
        stt_model=model,
        device=device,
        compute_type=compute,
        download_mb=modelstore.download_mb(model),
        cleanup=cleanup,
        cleanup_model=cleanup_model,
        speed="slow" if warning else SPEED[tier],
        label=LABEL[tier],
        explanation=explanation,
        cpu_threads=cpu_threads_for(hw),
        source="manual",
        cleanup_note=note,
    )


# --------------------------------------------------------------------------- resolve


def is_auto(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == AUTO


def stt_has_auto(stt: SttConfig) -> bool:
    return any(is_auto(v) for v in (stt.model, stt.device, stt.compute_type))


def classify(model: str, device: str) -> Tier:
    """The tier an explicit (model, device) pair behaves like."""
    name = model.strip().lower()
    big = any(k in name for k in ("large", "turbo", "medium"))
    if device == "cuda":
        return "best" if big else "balanced"
    return "balanced" if big or "small" in name else "light"


def ollama_models(url: str, timeout: float = OLLAMA_PROBE_TIMEOUT_S) -> list[str] | None:
    """Model tags pulled in the Ollama at ``url``; None if it doesn't answer. Never raises."""
    try:
        import httpx

        resp = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=timeout, trust_env=False)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug("Ollama not reachable at %s: %s", url, type(exc).__name__)
        return None
    names: list[str] = []
    for entry in data.get("models") or [] if isinstance(data, dict) else []:
        if isinstance(entry, dict):
            for key in ("name", "model"):
                if isinstance(entry.get(key), str) and entry[key] not in names:
                    names.append(entry[key])
    return names


def resolve(
    cfg: Config,
    hw: HardwareInfo | None = None,
    *,
    ollama: Sequence[str] | None = None,
    probe_ollama: bool = True,
) -> ModelChoice:
    """The model setup the hub will run: config.toml values win, ``"auto"`` ones are
    picked from the hardware.

    A config with explicit model, device and compute type (and a true/false cleanup
    switch) never probes the hardware or Ollama and behaves exactly as before. Cleanup
    ``"auto"`` asks Ollama (``probe_ollama``) unless ``ollama`` lists the pulled models.
    """
    stt = cfg.stt
    cleanup_auto = is_auto(cfg.cleanup.enabled)
    need_hw = stt_has_auto(stt) or cleanup_auto
    if need_hw and hw is None:
        from shoutabout.hardware import detect

        hw = detect()

    pulled: Sequence[str] | None = ollama
    if cleanup_auto and pulled is None and probe_ollama:
        pulled = ollama_models(cfg.cleanup.ollama_url)

    forced_device: Device | None = None if is_auto(stt.device) else stt.device  # type: ignore[assignment]
    forced_tier = None if is_auto(stt.tier) else stt.tier
    auto: ModelChoice | None = None
    if hw is not None and forced_tier is not None and forced_device is None:
        auto = choose_tier(
            forced_tier, hw, ollama_models=pulled, preferred_cleanup=cfg.cleanup.model
        )
    elif hw is not None:
        auto = choose(
            hw, ollama_models=pulled, device=forced_device, preferred_cleanup=cfg.cleanup.model
        )

    device: Device = forced_device or (auto.device if auto else "cuda")
    model = (auto.stt_model if auto else BEST_MODEL) if is_auto(stt.model) else stt.model
    if is_auto(stt.compute_type):
        if device == "cpu":
            compute = stt.cpu_compute_type
        elif auto is not None and auto.device == "cuda":
            compute = auto.compute_type
        else:
            compute = _first_supported(hw.best_gpu if hw else None, GPU_COMPUTE_PREFERENCE)
    else:
        compute = stt.compute_type if device == "cuda" else stt.cpu_compute_type

    explicit = [not is_auto(v) for v in (stt.model, stt.device, stt.compute_type)]
    source: Literal["auto", "manual", "config", "mixed"]
    if all(explicit) and not cleanup_auto:
        source = "config"
    elif not any(explicit) and cleanup_auto:
        source = auto.source if auto is not None else "auto"
    else:
        source = "mixed"

    same = auto is not None and model == auto.stt_model and device == auto.device
    tier = auto.tier if same and auto is not None else classify(model, device)
    speed = auto.speed if same and auto is not None else SPEED[tier]

    gpu = hw.best_gpu if hw is not None else None
    if cleanup_auto:
        cleanup, cleanup_model, note = _cleanup_decision(
            tier, gpu if device == "cuda" else None, pulled, cfg.cleanup.model
        )
    elif cfg.cleanup.enabled:
        cleanup, cleanup_model, note = True, cfg.cleanup.model, "Text tidying is on (config.toml)."
    else:
        cleanup, cleanup_model, note = False, None, "Text tidying is off (config.toml)."

    if source == "config" or auto is None:
        explanation = "Set in config.toml."
    elif source == "mixed":
        explanation = auto.explanation + " Some settings come from config.toml."
    else:
        explanation = auto.explanation
    threads = auto.cpu_threads if (auto is not None and is_auto(stt.device)) else 0
    return ModelChoice(
        tier=tier,
        stt_model=model,
        device=device,
        compute_type=compute,
        download_mb=modelstore.download_mb(model),
        cleanup=cleanup,
        cleanup_model=cleanup_model,
        speed=speed,
        label=LABEL[tier],
        explanation=explanation,
        cpu_threads=threads,
        source=source,
        cleanup_note=note,
    )


def concrete_stt(stt: SttConfig, choice: ModelChoice) -> SttConfig:
    """``stt`` with every ``"auto"`` replaced by the choice (what the engine loads).

    With an automatic device the engine may also fall back to the processor when the
    graphics card fails (out of memory because a game is running, a driver update...):
    a consumer install should keep working, slowly, rather than stop.
    """
    update: dict[str, object] = {}
    if is_auto(stt.model):
        update["model"] = choice.stt_model
    if is_auto(stt.device):
        update["device"] = choice.device
        update["cpu_fallback"] = True
    if is_auto(stt.compute_type):
        update["compute_type"] = choice.compute_type if choice.device == "cuda" else "float16"
    if not update:
        return stt
    return stt.model_copy(update=update)


def resolve_stt(stt: SttConfig, hw: HardwareInfo | None = None) -> tuple[SttConfig, int]:
    """(concrete SttConfig, cpu_threads) for an [stt] section that may say "auto".
    Explicit sections come back unchanged with 0 threads (CTranslate2's default)."""
    if not stt_has_auto(stt):
        return stt, 0
    from shoutabout.config import Config

    choice = resolve(Config(stt=stt), hw, probe_ollama=False)
    return concrete_stt(stt, choice), choice.cpu_threads


def with_cleanup(choice: ModelChoice, cleanup: bool, model: str | None, note: str) -> ModelChoice:
    return replace(choice, cleanup=cleanup, cleanup_model=model, cleanup_note=note)
