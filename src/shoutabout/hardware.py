"""What this PC can run: NVIDIA GPUs (name, VRAM, CUDA usable?), CPU cores and RAM.

:func:`detect` feeds the automatic model choice (:mod:`shoutabout.modelpick`). It never
raises and caches its answer, because the hub, the setup window and ``doctor`` all ask.

How each fact is found, cheapest and most reliable first:

* **GPUs**: NVML (``nvml.dll``, which every NVIDIA driver installs in System32) through
  ctypes. It answers in a few milliseconds and never creates a CUDA context, so it costs no
  VRAM. If NVML is missing or fails, ``nvidia-smi --query-gpu`` is the fallback.
* **CUDA usable?** A GPU alone isn't enough: the driver must support CUDA 12, CTranslate2
  must see the device and cuBLAS 12 (the pip wheel, see :mod:`shoutabout.engine.cuda_dlls`)
  must load. The CTranslate2 query also lists the precisions ("compute types") the card
  supports, e.g. ``float16`` needs compute capability 7.0 or newer.
* **CPU and RAM**: psutil, plus the marketing name from the registry on Windows.
* **Apple Silicon** (v2-macos): the chip's name and its performance cores from ``sysctl``
  (:mod:`shoutabout.mac.sysinfo`); Whisper runs on the CPU there (CTranslate2 uses Apple's
  Accelerate), and only the performance cores are worth using.

Every probe is a plain function in :class:`Probes`, so the decision logic is unit-tested
with fakes on any OS and no test needs a GPU.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

GIB = 1024**3
MIB = 1024**2
# CTranslate2 4.x wheels are built against CUDA 12: the driver must support at least 12.0.
MIN_CUDA_DRIVER = 12000
SMI_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class GpuInfo:
    """One NVIDIA GPU. ``vram_gb`` is the total memory in GiB (what the box says, roughly)."""

    name: str
    vram_gb: float
    cuda_usable: bool
    driver: str | None
    compute_capability: tuple[int, int] | None = None
    # CTranslate2 compute types the card supports ("float16", "int8_float16", ...); empty
    # when CUDA isn't usable.
    compute_types: tuple[str, ...] = ()
    # Why CUDA can't be used (plain words), or None when it can.
    reason: str | None = None

    def supports(self, compute_type: str) -> bool:
        return compute_type in self.compute_types


@dataclass(frozen=True)
class HardwareInfo:
    gpus: tuple[GpuInfo, ...]
    cpu_name: str
    physical_cores: int
    logical_cores: int
    ram_gb: float
    # v2-macos: "Apple M2 Pro" on Apple Silicon Macs (None elsewhere) and its fast cores.
    apple_chip: str | None = None
    performance_cores: int = 0

    @property
    def best_gpu(self) -> GpuInfo | None:
        """The usable GPU with the most VRAM; else the largest GPU at all; else None."""
        if not self.gpus:
            return None
        usable = [g for g in self.gpus if g.cuda_usable]
        pool = usable or list(self.gpus)
        return max(pool, key=lambda g: g.vram_gb)

    def summary(self) -> str:
        """One log line, e.g. ``GPU NVIDIA GeForce RTX 4080 SUPER 16.0 GB (CUDA ok);
        CPU ... 20 cores / 28 threads; RAM 64 GB``."""
        gpu = self.best_gpu
        if self.apple_chip:  # v2-macos
            return (
                f"{self.apple_chip}, {self.performance_cores} performance cores, "
                f"{self.physical_cores} cores; RAM {self.ram_gb:.0f} GB"
            )
        if gpu is None:
            gpu_text = "no NVIDIA GPU"
        else:
            cuda = "CUDA ok" if gpu.cuda_usable else f"CUDA unusable: {gpu.reason or 'unknown'}"
            gpu_text = f"GPU {gpu.name} {gpu.vram_gb:.1f} GB ({cuda})"
        return (
            f"{gpu_text}; CPU {self.cpu_name}, {self.physical_cores} cores / "
            f"{self.logical_cores} threads; RAM {self.ram_gb:.0f} GB"
        )


@dataclass(frozen=True)
class RawGpu:
    """What NVML or nvidia-smi reports, before the CUDA check."""

    name: str
    vram_bytes: int
    driver: str | None = None
    compute_capability: tuple[int, int] | None = None
    cuda_driver_version: int | None = None  # e.g. 12080 = CUDA 12.8


@dataclass(frozen=True)
class CudaStatus:
    device_count: int
    # compute types per CTranslate2 device index
    compute_types: tuple[tuple[str, ...], ...] = ()
    reason: str | None = None  # why CUDA can't be used at all

    @property
    def usable(self) -> bool:
        return self.device_count > 0 and self.reason is None


# ------------------------------------------------------------------------------ NVML


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


def nvml_paths() -> list[str]:
    """Where nvml.dll lives: System32 on current drivers, NVSMI on old ones."""
    if sys.platform != "win32":
        return ["libnvidia-ml.so.1"]
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")  # env names are case-insensitive
    program_files = os.environ.get("PROGRAMW6432") or os.environ.get(
        "PROGRAMFILES", r"C:\Program Files"
    )
    return [
        str(Path(root) / "System32" / "nvml.dll"),
        str(Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvml.dll"),
    ]


def _load_nvml() -> ctypes.CDLL | None:
    for path in nvml_paths():
        if sys.platform == "win32" and not os.path.isfile(path):
            continue
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


def probe_nvml() -> list[RawGpu] | None:
    """GPUs via NVML. None if NVML isn't available or fails (try nvidia-smi then)."""
    lib = _load_nvml()
    if lib is None:
        return None
    init = getattr(lib, "nvmlInit_v2", None) or getattr(lib, "nvmlInit", None)
    if init is None or init() != 0:
        return None
    try:
        count = ctypes.c_uint(0)
        get_count = getattr(lib, "nvmlDeviceGetCount_v2", None) or lib.nvmlDeviceGetCount
        if get_count(ctypes.byref(count)) != 0:
            return None
        driver = None
        buf = ctypes.create_string_buffer(96)
        if lib.nvmlSystemGetDriverVersion(buf, len(buf)) == 0:
            driver = buf.value.decode("ascii", "replace") or None
        cuda_version = None
        cv = ctypes.c_int(0)
        getter = getattr(lib, "nvmlSystemGetCudaDriverVersion_v2", None) or getattr(
            lib, "nvmlSystemGetCudaDriverVersion", None
        )
        if getter is not None and getter(ctypes.byref(cv)) == 0 and cv.value > 0:
            cuda_version = cv.value
        get_handle = (
            getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None) or lib.nvmlDeviceGetHandleByIndex
        )
        gpus: list[RawGpu] = []
        for i in range(count.value):
            handle = ctypes.c_void_p()
            if get_handle(ctypes.c_uint(i), ctypes.byref(handle)) != 0:
                continue
            name_buf = ctypes.create_string_buffer(96)
            name = "NVIDIA GPU"
            if lib.nvmlDeviceGetName(handle, name_buf, len(name_buf)) == 0:
                name = name_buf.value.decode("utf-8", "replace").strip() or name
            mem = _NvmlMemory()
            total = 0
            if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
                total = int(mem.total)
            cc = None
            major, minor = ctypes.c_int(0), ctypes.c_int(0)
            get_cc = getattr(lib, "nvmlDeviceGetCudaComputeCapability", None)
            if get_cc is not None and get_cc(handle, ctypes.byref(major), ctypes.byref(minor)) == 0:
                cc = (major.value, minor.value)
            gpus.append(RawGpu(name, total, driver, cc, cuda_version))
        return gpus
    except Exception:
        log.debug("NVML query failed", exc_info=True)
        return None
    finally:
        try:
            lib.nvmlShutdown()
        except Exception:
            pass


# ------------------------------------------------------------------------ nvidia-smi


def find_nvidia_smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    if sys.platform == "win32":
        for path in nvml_paths():
            candidate = Path(path).with_name("nvidia-smi.exe")
            if candidate.is_file():
                return str(candidate)
    return None


def parse_smi_csv(text: str) -> list[RawGpu]:
    """Rows of ``name, memory.total [MiB], driver_version[, compute_cap]``."""
    gpus: list[RawGpu] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[0]:
            continue
        try:
            vram = int(float(parts[1]) * MIB)
        except ValueError:
            continue
        cc = None
        if len(parts) > 3:
            try:
                major, _, minor = parts[3].partition(".")
                cc = (int(major), int(minor or 0))
            except ValueError:
                cc = None
        gpus.append(RawGpu(parts[0], vram, parts[2] or None, cc))
    return gpus


def probe_smi(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[RawGpu] | None:
    """GPUs via ``nvidia-smi``; None if it isn't installed or fails."""
    exe = find_nvidia_smi()
    if not exe:
        return None
    for fields in (
        "name,memory.total,driver_version,compute_cap",
        "name,memory.total,driver_version",
    ):
        try:
            proc = run(
                [exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=SMI_TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0:
            return parse_smi_csv(proc.stdout)
    return None


# ------------------------------------------------------------------------------ CUDA


def probe_cuda() -> CudaStatus:
    """Can CTranslate2 run on CUDA here? Loads ctranslate2 and cuBLAS (no VRAM is used)."""
    try:
        from shoutabout.engine.cuda_dlls import add_nvidia_dll_dirs

        add_nvidia_dll_dirs()
        import ctranslate2
    except Exception as exc:
        return CudaStatus(0, reason=f"CTranslate2 isn't installed ({type(exc).__name__})")
    try:
        count = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:
        return CudaStatus(0, reason=f"the CUDA device query failed ({type(exc).__name__})")
    if count <= 0:
        return CudaStatus(0, reason="CTranslate2 found no CUDA device (driver too old?)")
    if sys.platform == "win32":
        try:
            ctypes.WinDLL("cublas64_12.dll")
        except OSError:
            return CudaStatus(
                count, reason="cuBLAS 12 isn't installed (uv sync --extra cuda installs it)"
            )
    types: list[tuple[str, ...]] = []
    for i in range(count):
        try:
            types.append(tuple(sorted(ctranslate2.get_supported_compute_types("cuda", i))))
        except Exception:
            types.append(())
    return CudaStatus(count, tuple(types))


# ------------------------------------------------------------------------- CPU / RAM


def probe_cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg

            key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "ProcessorNameString")
                if str(value).strip():
                    return " ".join(str(value).split())
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown CPU"


def probe_cores() -> tuple[int, int]:
    """(physical, logical) cores; physical falls back to logical when unknown."""
    try:
        import psutil

        logical = psutil.cpu_count(logical=True) or os.cpu_count() or 1
        physical = psutil.cpu_count(logical=False) or logical
    except Exception:
        logical = os.cpu_count() or 1
        physical = logical
    return int(physical), int(logical)


def probe_ram_bytes() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        return 0


# --------------------------------------------------------------------------- detect


@dataclass(frozen=True)
class Probes:
    """The fact finders :func:`detect` calls (swap them for fakes in tests)."""

    nvml: Callable[[], list[RawGpu] | None] = probe_nvml
    smi: Callable[[], list[RawGpu] | None] = probe_smi
    cuda: Callable[[], CudaStatus] = probe_cuda
    cpu_name: Callable[[], str] = probe_cpu_name
    cores: Callable[[], tuple[int, int]] = probe_cores
    ram_bytes: Callable[[], int] = probe_ram_bytes
    apple: Callable[[], object] = lambda: _probe_apple()  # v2-macos


def _probe_apple() -> object:
    from shoutabout.mac.sysinfo import probe_apple_chip

    return probe_apple_chip()


def combine(raw: Sequence[RawGpu], cuda: CudaStatus | None) -> tuple[GpuInfo, ...]:
    """Merge what the driver reports with what CTranslate2 can use.

    CTranslate2 numbers devices like CUDA does (fastest first by default, which matches
    NVML order on single-GPU PCs, the common case). When the counts differ we can't map
    them reliably, so only GPU 0 gets the CTranslate2 compute types.
    """
    gpus: list[GpuInfo] = []
    for i, g in enumerate(raw):
        vram_gb = round(g.vram_bytes / GIB, 1)
        reason: str | None
        types: tuple[str, ...] = ()
        if g.cuda_driver_version is not None and g.cuda_driver_version < MIN_CUDA_DRIVER:
            major, minor = divmod(g.cuda_driver_version // 10, 100)
            reason = f"the NVIDIA driver only supports CUDA {major}.{minor}; update the driver"
        elif cuda is None:
            reason = "CUDA wasn't checked"
        elif not cuda.usable:
            reason = cuda.reason or "CTranslate2 can't use this GPU"
        elif i < cuda.device_count:
            reason = None
            if cuda.compute_types and (len(raw) == cuda.device_count or i == 0):
                types = cuda.compute_types[i] if i < len(cuda.compute_types) else ()
        else:
            reason = "CTranslate2 can't see this GPU"
        gpus.append(
            GpuInfo(
                name=g.name,
                vram_gb=vram_gb,
                cuda_usable=reason is None,
                driver=g.driver,
                compute_capability=g.compute_capability,
                compute_types=types,
                reason=reason,
            )
        )
    return tuple(gpus)


def _detect_apple(probes: Probes, chip: object) -> HardwareInfo:
    """v2-macos: an Apple Silicon Mac (no NVIDIA GPU, no CUDA probes)."""
    cores = _safe(probes.cores, (1, 1))
    physical, logical = cores if isinstance(cores, tuple) and len(cores) == 2 else (1, 1)
    ram = _safe(probes.ram_bytes, 0)
    perf = int(getattr(chip, "performance_cores", 0) or 0)
    return HardwareInfo(
        gpus=(),
        cpu_name=str(getattr(chip, "name", "Apple Silicon")),
        physical_cores=max(1, int(physical)),
        logical_cores=max(1, int(logical), int(physical)),
        ram_gb=round(int(ram) / GIB, 1) if isinstance(ram, int) else 0.0,
        apple_chip=str(getattr(chip, "name", "Apple Silicon")),
        performance_cores=max(1, perf or int(physical) // 2),
    )


_cache: HardwareInfo | None = None
_cache_lock = threading.Lock()


def _safe(fn: Callable[[], object], default: object) -> object:
    try:
        return fn()
    except Exception:
        log.debug("hardware probe %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return default


def _detect(probes: Probes) -> HardwareInfo:
    chip = _safe(probes.apple, None)  # v2-macos: an Apple Silicon Mac has no NVIDIA GPU
    if chip is not None and getattr(chip, "name", None):
        return _detect_apple(probes, chip)
    raw = _safe(probes.nvml, None)
    if raw is None:
        raw = _safe(probes.smi, None)
    raw_gpus: list[RawGpu] = list(raw) if isinstance(raw, list) else []
    cuda = None
    if raw_gpus:
        cuda = _safe(probes.cuda, CudaStatus(0, reason="the CUDA check failed"))
        if not isinstance(cuda, CudaStatus):
            cuda = CudaStatus(0, reason="the CUDA check failed")
    cores = _safe(probes.cores, (1, 1))
    physical, logical = cores if isinstance(cores, tuple) and len(cores) == 2 else (1, 1)
    ram = _safe(probes.ram_bytes, 0)
    name = _safe(probes.cpu_name, "unknown CPU")
    return HardwareInfo(
        gpus=combine(raw_gpus, cuda),
        cpu_name=str(name) or "unknown CPU",
        physical_cores=max(1, int(physical)),
        logical_cores=max(1, int(logical), int(physical)),
        ram_gb=round(int(ram) / GIB, 1) if isinstance(ram, int) else 0.0,
    )


def detect(*, refresh: bool = False, probes: Probes | None = None) -> HardwareInfo:
    """This PC's hardware. Never raises; cached after the first call (``refresh`` re-probes).

    Passing ``probes`` (tests) bypasses the cache.
    """
    global _cache
    if probes is not None:
        return _detect(probes)
    with _cache_lock:
        if _cache is None or refresh:
            try:
                _cache = _detect(Probes())
            except Exception:  # belt and braces: detect() must never raise
                log.exception("hardware detection failed")
                _cache = HardwareInfo((), "unknown CPU", 1, 1, 0.0)
            log.debug("hardware: %s", _cache.summary())
        return _cache


def with_gpus(hw: HardwareInfo, gpus: Sequence[GpuInfo]) -> HardwareInfo:
    """A copy of ``hw`` with other GPUs (handy for "what would a smaller card get?")."""
    return replace(hw, gpus=tuple(gpus))
