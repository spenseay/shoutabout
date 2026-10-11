"""Hardware detection with fake probes: no GPU, driver or NVML needed."""

from __future__ import annotations

import subprocess
import sys

import pytest

from shoutabout import hardware
from shoutabout.hardware import (
    GIB,
    MIB,
    CudaStatus,
    GpuInfo,
    HardwareInfo,
    Probes,
    RawGpu,
    combine,
    detect,
    parse_smi_csv,
)

TYPES = ("float16", "float32", "int8", "int8_float16")
RTX = RawGpu("NVIDIA GeForce RTX 4080 SUPER", 16 * GIB, "591.86", (8, 9), 13010)


def probes(**kw) -> Probes:
    base = {
        "nvml": lambda: [RTX],
        "smi": lambda: pytest.fail("nvidia-smi must not run when NVML answered"),
        "cuda": lambda: CudaStatus(1, (TYPES,)),
        "cpu_name": lambda: "Intel(R) Core(TM) i7-14700KF",
        "cores": lambda: (20, 28),
        "ram_bytes": lambda: 64 * GIB,
    }
    base.update(kw)
    return Probes(**base)


def test_detect_with_a_usable_nvidia_gpu():
    hw = detect(probes=probes())
    assert hw.cpu_name == "Intel(R) Core(TM) i7-14700KF"
    assert (hw.physical_cores, hw.logical_cores, hw.ram_gb) == (20, 28, 64.0)
    gpu = hw.best_gpu
    assert gpu == GpuInfo(
        "NVIDIA GeForce RTX 4080 SUPER", 16.0, True, "591.86", (8, 9), TYPES, None
    )
    assert gpu.supports("float16") and not gpu.supports("bfloat16")
    assert "CUDA ok" in hw.summary() and "20 cores / 28 threads" in hw.summary()


def test_nvidia_smi_is_the_fallback_when_nvml_fails():
    smi = RawGpu("NVIDIA GeForce GTX 1650", 4 * GIB, "551.00", (7, 5))
    hw = detect(probes=probes(nvml=lambda: None, smi=lambda: [smi]))
    assert hw.best_gpu.name == "NVIDIA GeForce GTX 1650"
    assert hw.best_gpu.vram_gb == 4.0


def test_no_nvidia_gpu_never_probes_cuda():
    hw = detect(
        probes=probes(
            nvml=lambda: None,
            smi=lambda: None,
            cuda=lambda: pytest.fail("no GPU: CUDA must not be probed"),
        )
    )
    assert hw.gpus == () and hw.best_gpu is None
    assert "no NVIDIA GPU" in hw.summary()


def test_cuda_unusable_reason_is_kept():
    hw = detect(probes=probes(cuda=lambda: CudaStatus(1, reason="cuBLAS 12 isn't installed")))
    gpu = hw.best_gpu
    assert gpu.cuda_usable is False and gpu.compute_types == ()
    assert "cuBLAS" in gpu.reason
    assert "CUDA unusable: cuBLAS" in hw.summary()


def test_old_driver_means_cuda_unusable():
    old = RawGpu("NVIDIA GeForce GTX 970", 4 * GIB, "456.71", (5, 2), 11010)
    (gpu,) = combine([old], CudaStatus(1, (TYPES,)))
    assert gpu.cuda_usable is False
    assert "CUDA 11.1" in gpu.reason and "update the driver" in gpu.reason


def test_combine_maps_compute_types_by_index_and_flags_unseen_gpus():
    a = RawGpu("A", 8 * GIB)
    b = RawGpu("B", 24 * GIB)
    gpus = combine([a, b], CudaStatus(2, (("int8",), ("float16", "int8"))))
    assert [g.compute_types for g in gpus] == [("int8",), ("float16", "int8")]
    gpus = combine([a, b], CudaStatus(1, (("int8",),)))
    assert gpus[0].cuda_usable and not gpus[1].cuda_usable
    assert "can't see" in gpus[1].reason


def test_best_gpu_prefers_usable_then_biggest():
    usable = GpuInfo("small", 4.0, True, None)
    unusable = GpuInfo("big", 24.0, False, None, reason="x")
    hw = HardwareInfo((unusable, usable), "cpu", 4, 8, 16.0)
    assert hw.best_gpu is usable
    hw = HardwareInfo((GpuInfo("a", 8, True, None), GpuInfo("b", 12, True, None)), "c", 4, 8, 16)
    assert hw.best_gpu.name == "b"
    assert HardwareInfo((unusable,), "c", 4, 8, 16).best_gpu is unusable


def test_detect_never_raises_when_every_probe_breaks():
    def boom():
        raise RuntimeError("broken")

    hw = detect(
        probes=Probes(nvml=boom, smi=boom, cuda=boom, cpu_name=boom, cores=boom, ram_bytes=boom)
    )
    assert hw == HardwareInfo((), "unknown CPU", 1, 1, 0.0)


def test_cuda_probe_crash_marks_gpu_unusable():
    def boom():
        raise OSError("driver crashed")

    hw = detect(probes=probes(cuda=boom))
    assert hw.best_gpu.cuda_usable is False


def test_detect_is_cached(monkeypatch):
    calls = []

    def fake_detect(p):
        calls.append(p)
        return HardwareInfo((), "cached", 2, 4, 8.0)

    monkeypatch.setattr(hardware, "_detect", fake_detect)
    monkeypatch.setattr(hardware, "_cache", None)
    assert detect().cpu_name == "cached"
    assert detect().cpu_name == "cached"
    assert len(calls) == 1
    detect(refresh=True)
    assert len(calls) == 2


def test_parse_smi_csv():
    text = (
        "NVIDIA GeForce RTX 4080 SUPER, 16376, 591.86, 8.9\n"
        "NVIDIA GeForce GTX 1060 3GB, 3072, 472.12\n"
        "garbage line\n"
        "Broken, notanumber, 1.0\n"
    )
    gpus = parse_smi_csv(text)
    assert gpus == [
        RawGpu("NVIDIA GeForce RTX 4080 SUPER", 16376 * MIB, "591.86", (8, 9)),
        RawGpu("NVIDIA GeForce GTX 1060 3GB", 3072 * MIB, "472.12", None),
    ]


def test_probe_smi_retries_without_compute_cap(monkeypatch):
    monkeypatch.setattr(hardware, "find_nvidia_smi", lambda: "nvidia-smi")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd[1])
        if "compute_cap" in cmd[1]:
            return subprocess.CompletedProcess(cmd, 2, "", "unknown field")
        return subprocess.CompletedProcess(cmd, 0, "Old GPU, 2048, 390.1\n", "")

    gpus = hardware.probe_smi(run)
    assert gpus == [RawGpu("Old GPU", 2048 * MIB, "390.1", None)]
    assert len(seen) == 2


def test_probe_smi_missing(monkeypatch):
    monkeypatch.setattr(hardware, "find_nvidia_smi", lambda: None)
    assert hardware.probe_smi() is None


def test_nvml_paths():
    paths = hardware.nvml_paths()
    if sys.platform == "win32":
        assert paths[0].lower().endswith(r"system32\nvml.dll")
        assert paths[1].lower().endswith(r"nvsmi\nvml.dll")
    else:
        assert paths == ["libnvidia-ml.so.1"]


def test_with_gpus_replaces_only_the_gpus():
    hw = HardwareInfo((), "cpu", 4, 8, 16.0)
    small = GpuInfo("x", 2.0, True, None)
    assert hardware.with_gpus(hw, [small]) == HardwareInfo((small,), "cpu", 4, 8, 16.0)


@pytest.mark.gpu
def test_real_nvidia_gpu_is_detected_and_picks_the_best_tier():
    """Opt-in (--run-gpu): on a PC with a CUDA-capable NVIDIA card and the cuda extra."""
    from shoutabout.modelpick import choose

    hw = detect(refresh=True)
    gpu = hw.best_gpu
    assert gpu is not None and gpu.cuda_usable, hw.summary()
    assert gpu.vram_gb >= 4 and "float16" in gpu.compute_types
    assert choose(hw).device == "cuda"


def test_real_probes_never_raise():
    """The real fact finders on whatever machine runs the tests (no GPU needed)."""
    physical, logical = hardware.probe_cores()
    assert 1 <= physical <= logical
    assert hardware.probe_ram_bytes() >= 0
    assert isinstance(hardware.probe_cpu_name(), str)
    assert hardware.probe_nvml() is None or isinstance(hardware.probe_nvml(), list)
