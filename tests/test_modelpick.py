"""Automatic model choice: tiers, cleanup, and config.toml values winning over "auto"."""

from __future__ import annotations

import pytest

from shoutabout import hardware, modelpick
from shoutabout.config import Config, SttConfig, parse_config
from shoutabout.hardware import GpuInfo, HardwareInfo
from shoutabout.modelpick import (
    ModelChoice,
    choose,
    classify,
    concrete_stt,
    pick_cleanup_model,
    resolve,
    resolve_stt,
)

ALL_TYPES = ("float16", "float32", "int8", "int8_float16")
QWEN = "qwen3:4b-instruct-2507-q4_K_M"


def gpu(
    vram: float, *, usable: bool = True, types=ALL_TYPES, name="NVIDIA GeForce Test"
) -> GpuInfo:
    return GpuInfo(
        name,
        vram,
        usable,
        "591.86",
        (8, 9),
        types if usable else (),
        None if usable else "CTranslate2 found no CUDA device",
    )


def hw(
    *gpus: GpuInfo, cores: int = 8, ram: float = 16.0, cpu="Intel(R) Core(TM) i5-1240P"
) -> HardwareInfo:
    return HardwareInfo(tuple(gpus), cpu, cores, cores * 2, ram)


OWNER_PC = hw(gpu(16.0, name="NVIDIA GeForce RTX 4080 SUPER"), cores=20, ram=64.0)


@pytest.fixture(autouse=True)
def no_real_probes(monkeypatch):
    """resolve() must get hardware and Ollama answers from the test, never the machine."""

    def no_detect(**kw):
        raise AssertionError("hardware.detect() called unexpectedly")

    def no_ollama(url, timeout=1.0):
        raise AssertionError("Ollama probed unexpectedly")

    monkeypatch.setattr(hardware, "detect", no_detect)
    monkeypatch.setattr(modelpick, "ollama_models", no_ollama)


# --------------------------------------------------------------------------- tiers


def test_big_nvidia_gpu_gets_the_best_tier_with_cleanup():
    c = choose(OWNER_PC, ollama_models=[QWEN, "llama3:latest"])
    assert (c.tier, c.stt_model, c.device, c.compute_type) == (
        "best",
        "large-v3-turbo",
        "cuda",
        "float16",
    )
    assert (c.speed, c.label) == ("fast", "Best quality")
    assert c.cleanup and c.cleanup_model == QWEN
    assert c.download_mb == 1622
    assert (
        c.explanation == "Your NVIDIA GeForce RTX 4080 SUPER runs the most accurate speech model."
    )
    assert "on" in c.cleanup_note


def test_best_tier_without_ollama_or_model_has_no_cleanup():
    assert choose(OWNER_PC, ollama_models=None).cleanup is False
    assert "isn't running" in choose(OWNER_PC, ollama_models=None).cleanup_note
    c = choose(OWNER_PC, ollama_models=["llama3:latest"])
    assert c.cleanup is False and c.cleanup_model is None
    assert "no suitable model" in c.cleanup_note


def test_six_gb_card_is_best_but_cannot_afford_cleanup():
    c = choose(hw(gpu(6.0)), ollama_models=[QWEN])
    assert c.tier == "best" and c.compute_type == "float16"
    assert c.cleanup is False and "8 GB" in c.cleanup_note


def test_four_gb_card_runs_turbo_in_int8():
    c = choose(hw(gpu(4.0, name="NVIDIA GeForce GTX 1650")), ollama_models=[QWEN])
    assert (c.tier, c.stt_model, c.device, c.compute_type, c.speed) == (
        "balanced",
        "large-v3-turbo",
        "cuda",
        "int8_float16",
        "ok",
    )
    assert c.cleanup is False
    assert "compact" in c.explanation and "GTX 1650 (4 GB)" in c.explanation


def test_two_gb_card_runs_small_en():
    c = choose(hw(gpu(2.0)))
    assert (c.tier, c.stt_model, c.device, c.compute_type) == (
        "balanced",
        "small.en",
        "cuda",
        "int8_float16",
    )


def test_tiny_gpu_is_ignored():
    c = choose(hw(gpu(1.0), cores=8, ram=16))
    assert (c.tier, c.device, c.stt_model) == ("balanced", "cpu", "small.en")
    assert "too little memory" in c.explanation


def test_precision_follows_what_the_card_supports():
    pascal = gpu(8.0, types=("float32", "int8", "int8_float32"))
    assert choose(hw(pascal)).compute_type == "int8"
    only_fp32 = gpu(8.0, types=("float32",))
    assert choose(hw(only_fp32)).compute_type == "float32"
    unknown = gpu(8.0, types=())
    assert choose(hw(unknown)).compute_type == "float16"


def test_strong_cpu_without_gpu_is_balanced_small_en():
    c = choose(hw(cores=8, ram=16, cpu="AMD Ryzen 7 7840U w/ Radeon 780M Graphics"))
    assert (c.tier, c.stt_model, c.device, c.compute_type, c.speed) == (
        "balanced",
        "small.en",
        "cpu",
        "int8",
        "ok",
    )
    assert c.cpu_threads == 8
    assert c.cleanup is False
    assert c.explanation.startswith("No NVIDIA graphics card was found")
    assert "AMD Ryzen 7 7840U" in c.explanation and "8 cores" in c.explanation


def test_typical_laptop_is_light_base_en():
    c = choose(hw(cores=4, ram=8, cpu="Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz"))
    assert (c.tier, c.stt_model, c.device, c.speed, c.label) == (
        "light",
        "base.en",
        "cpu",
        "slow",
        "Light",
    )
    assert c.cpu_threads == 4
    assert "Intel Core i5-8250U (4 cores)" in c.explanation
    assert c.download_mb == 148


def test_many_cores_but_little_ram_is_light():
    assert choose(hw(cores=12, ram=4)).tier == "light"


def test_threads_are_capped():
    assert choose(hw(cores=32, ram=64)).cpu_threads == modelpick.MAX_CPU_THREADS
    assert choose(hw(cores=1, ram=4)).cpu_threads == 1


def test_unusable_gpu_falls_back_to_cpu_and_says_why():
    c = choose(hw(gpu(12.0, usable=False), cores=6, ram=16))
    assert c.device == "cpu" and c.tier == "balanced"
    assert "can't be used right now" in c.explanation


def test_forced_cpu_device():
    c = choose(OWNER_PC, device="cpu")
    assert c.device == "cpu" and c.stt_model == "small.en"
    assert c.explanation.startswith("Set to use the processor")


def test_every_tier_maps_to_the_info_speed():
    assert modelpick.SPEED == {"best": "fast", "balanced": "ok", "light": "slow"}


# ------------------------------------------------------------------------- cleanup


def test_pick_cleanup_model_prefers_the_configured_one():
    assert pick_cleanup_model(["granite4:micro", QWEN], "granite4:micro") == "granite4:micro"
    assert pick_cleanup_model(["granite4:micro", QWEN], None) == QWEN
    assert pick_cleanup_model(["granite4:micro"], "my-own:7b") == "granite4:micro"
    assert pick_cleanup_model(["llama3"], "llama3:latest") == "llama3:latest"  # same_model
    assert pick_cleanup_model([], QWEN) is None
    assert pick_cleanup_model(None, QWEN) is None


def test_ollama_models_parses_tags(monkeypatch):
    import httpx

    monkeypatch.undo()  # use the real ollama_models with a fake HTTP layer

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": QWEN, "model": QWEN}, {"name": "granite4:micro"}, "junk"]}

    seen = {}

    def fake_get(url, timeout, trust_env):
        seen.update(url=url, timeout=timeout, trust_env=trust_env)
        return Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    assert modelpick.ollama_models("http://127.0.0.1:11434/") == [QWEN, "granite4:micro"]
    assert seen == {"url": "http://127.0.0.1:11434/api/tags", "timeout": 1.0, "trust_env": False}

    def down(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    assert modelpick.ollama_models("http://127.0.0.1:11434") is None


# ------------------------------------------------------------------------- resolve


def owner_config() -> Config:
    """A v1 config.toml: every value explicit (the owner's live setup)."""
    return parse_config(
        """
[stt]
model = "large-v3-turbo"
device = "cuda"
compute_type = "float16"
[cleanup]
enabled = true
model = "qwen3:4b-instruct-2507-q4_K_M"
"""
    )


def test_explicit_config_never_probes_and_keeps_its_values():
    c = resolve(owner_config())  # the fixture fails on any hardware / Ollama probe
    assert (c.stt_model, c.device, c.compute_type) == ("large-v3-turbo", "cuda", "float16")
    assert c.cleanup is True and c.cleanup_model == QWEN
    assert (c.source, c.tier, c.speed) == ("config", "best", "fast")
    assert c.explanation == "Set in config.toml."
    assert c.cpu_threads == 0
    stt = concrete_stt(owner_config().stt, c)
    assert stt == owner_config().stt  # nothing changes for the engine
    assert resolve_stt(owner_config().stt) == (owner_config().stt, 0)


def test_explicit_cleanup_false_and_cpu():
    cfg = parse_config(
        '[stt]\nmodel = "small"\ndevice = "cpu"\ncompute_type = "float16"\n'
        "[cleanup]\nenabled = false\n"
    )
    c = resolve(cfg)
    assert (c.device, c.compute_type, c.tier, c.cleanup) == ("cpu", "int8", "balanced", False)
    assert "config.toml" in c.cleanup_note


def test_auto_config_uses_the_hardware():
    c = resolve(Config(), OWNER_PC, ollama=[QWEN])
    assert c.source == "auto"
    assert (c.stt_model, c.device, c.compute_type, c.cleanup) == (
        "large-v3-turbo",
        "cuda",
        "float16",
        True,
    )
    assert c.cpu_threads == 8


def test_auto_cleanup_probes_ollama_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        modelpick, "ollama_models", lambda url, timeout=1.0: calls.append(url) or [QWEN]
    )
    c = resolve(Config(), OWNER_PC)
    assert c.cleanup and calls == ["http://127.0.0.1:11434"]
    c = resolve(Config(), OWNER_PC, probe_ollama=False)
    assert c.cleanup is False and len(calls) == 1


def test_auto_detects_hardware_when_not_given(monkeypatch):
    monkeypatch.setattr(hardware, "detect", lambda **kw: hw(cores=4, ram=8))
    c = resolve(Config(), probe_ollama=False)
    assert (c.tier, c.stt_model, c.device) == ("light", "base.en", "cpu")


def test_explicit_model_with_auto_device_and_precision():
    cfg = Config(stt=SttConfig(model="medium.en"))
    c = resolve(cfg, hw(gpu(4.0)), probe_ollama=False)
    assert (c.stt_model, c.device, c.compute_type) == ("medium.en", "cuda", "int8_float16")
    assert c.source == "mixed" and "config.toml" in c.explanation
    assert c.tier == "best"  # a big model on a GPU behaves like the best tier


def test_explicit_cpu_device_with_auto_model():
    cfg = Config(stt=SttConfig(device="cpu"))
    c = resolve(cfg, OWNER_PC, ollama=[QWEN])
    assert (c.stt_model, c.device, c.compute_type) == ("small.en", "cpu", "int8")
    assert c.cleanup is False  # the CPU tier can't afford it
    stt = concrete_stt(cfg.stt, c)
    assert (stt.model, stt.device) == ("small.en", "cpu")
    assert stt.cpu_fallback is False  # the device was explicit


def test_explicit_cuda_without_a_usable_gpu_keeps_cuda():
    cfg = Config(stt=SttConfig(device="cuda"))
    c = resolve(cfg, hw(gpu(8.0, usable=False)), probe_ollama=False)
    assert (c.device, c.stt_model) == ("cuda", "large-v3-turbo")


def test_concrete_stt_turns_on_cpu_fallback_for_auto_devices():
    cfg = Config()
    c = resolve(cfg, OWNER_PC, probe_ollama=False)
    stt = concrete_stt(cfg.stt, c)
    assert (stt.model, stt.device, stt.compute_type, stt.cpu_fallback) == (
        "large-v3-turbo",
        "cuda",
        "float16",
        True,
    )
    assert cfg.stt.model == "auto"  # the original is untouched


def test_resolve_stt_for_auto(monkeypatch):
    monkeypatch.setattr(hardware, "detect", lambda **kw: hw(cores=6, ram=16))
    stt, threads = resolve_stt(SttConfig())
    assert (stt.model, stt.device, threads) == ("small.en", "cpu", 6)


def test_classify():
    assert classify("large-v3-turbo", "cuda") == "best"
    assert classify("small.en", "cuda") == "balanced"
    assert classify("large-v3", "cpu") == "balanced"
    assert classify("small", "cpu") == "balanced"
    assert classify("base.en", "cpu") == "light"
    assert classify("tiny", "cpu") == "light"


def test_summary_line():
    c = choose(hw(cores=4, ram=8))
    assert c.summary == "Light: base.en on cpu/int8, 4 threads, cleanup off [auto]"
    c = choose(OWNER_PC, ollama_models=[QWEN])
    assert c.summary.startswith("Best quality: large-v3-turbo on cuda/float16, cleanup on (qwen3")


def test_with_cleanup_returns_a_copy():
    c = choose(OWNER_PC)
    on = modelpick.with_cleanup(c, True, QWEN, "on")
    assert on.cleanup and not c.cleanup and isinstance(on, ModelChoice)


# --------------------------------------------------------------- manual tiers


def test_manual_best_on_a_big_gpu():
    c = modelpick.choose_tier("best", OWNER_PC, ollama_models=[QWEN])
    assert (c.tier, c.stt_model, c.device, c.compute_type, c.speed) == (
        "best",
        "large-v3-turbo",
        "cuda",
        "float16",
        "fast",
    )
    assert c.source == "manual" and c.explanation == "Chosen manually: Best quality."
    assert c.cleanup and c.cleanup_model == QWEN


def test_manual_best_without_a_gpu_warns_it_is_slow():
    c = modelpick.choose_tier("best", hw(cores=4, ram=8))
    assert (c.stt_model, c.device, c.compute_type) == ("large-v3-turbo", "cpu", "int8")
    assert c.speed == "slow" and c.tier == "best"
    assert "will be slow" in c.explanation and c.explanation.startswith("Chosen manually")
    assert c.cleanup is False


def test_manual_best_on_a_small_gpu_uses_int8_and_warns():
    c = modelpick.choose_tier("best", hw(gpu(4.0)))
    assert (c.device, c.compute_type, c.speed) == ("cuda", "int8_float16", "fast")
    c = modelpick.choose_tier("best", hw(gpu(2.0)))
    assert c.speed == "slow" and "may not fit" in c.explanation


def test_manual_balanced_and_light():
    c = modelpick.choose_tier("balanced", OWNER_PC, ollama_models=[QWEN])
    assert (c.stt_model, c.device, c.compute_type, c.speed) == (
        "large-v3-turbo",
        "cuda",
        "int8_float16",
        "ok",
    )
    assert c.cleanup is False  # only the best tier runs the cleanup model
    assert modelpick.choose_tier("balanced", hw(gpu(2.0))).stt_model == "small.en"
    weak = modelpick.choose_tier("balanced", hw(cores=2, ram=4))
    assert (weak.device, weak.speed) == ("cpu", "slow") and "slow" in weak.explanation
    strong = modelpick.choose_tier("balanced", hw(cores=8, ram=16))
    assert (strong.stt_model, strong.speed) == ("small.en", "ok")
    light = modelpick.choose_tier("light", OWNER_PC)
    assert (light.stt_model, light.device, light.speed, light.label) == (
        "base.en",
        "cpu",
        "slow",
        "Light",
    )
    assert light.explanation == "Chosen manually: Light."
    with pytest.raises(ValueError):
        modelpick.choose_tier("ultra", OWNER_PC)  # type: ignore[arg-type]


def test_config_tier_override_goes_through_resolve():
    cfg = parse_config('[stt]\ntier = "light"\n')
    c = resolve(cfg, OWNER_PC, ollama=[QWEN])
    assert (c.tier, c.stt_model, c.device, c.source) == ("light", "base.en", "cpu", "manual")
    assert c.cleanup is False
    stt = concrete_stt(cfg.stt, c)
    assert (stt.model, stt.device) == ("base.en", "cpu")
    # explicit values still win over a forced tier
    cfg = parse_config('[stt]\ntier = "light"\nmodel = "small.en"\n')
    c = resolve(cfg, OWNER_PC, probe_ollama=False)
    assert c.stt_model == "small.en" and c.source == "mixed"
    with pytest.raises(Exception):  # noqa: B017 - ConfigError
        parse_config('[stt]\ntier = "ultra"\n')
