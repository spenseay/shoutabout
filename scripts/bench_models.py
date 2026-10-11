"""Benchmark Whisper models/devices/precisions on this PC (evidence for docs/models.md).

For each configuration it loads the model (load + warm-up time, RAM and VRAM growth), then
transcribes:

* a ~10 s dictation (two test clips joined with a short pause): median latency over
  ``--runs`` runs, the number the tiers are designed around;
* the ~25 s ``long-01`` clip (median latency);
* every clip in the manifest once, for word accuracy and vocabulary-term hits (with the
  example vocabulary as hints, like a fresh install).

Usage (needs test audio from scripts/make_test_audio.ps1):

    .venv\\Scripts\\python.exe scripts\\bench_models.py --audio test-audio \\
        --config large-v3-turbo:cuda:float16 --config small.en:cpu:int8:4 \\
        --cache .tmp\\hf --json bench.json

A configuration is ``model:device:compute_type[:cpu_threads[:beam_size]]``. Models that
aren't downloaded yet are fetched into ``--cache`` first (with resume). Nothing is pasted,
no history is written, and the hub is not involved.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from shoutabout import modelstore
from shoutabout.audio import decode_audio
from shoutabout.cli import word_accuracy
from shoutabout.config import SttConfig, example_vocab_text
from shoutabout.engine.base import SAMPLE_RATE
from shoutabout.engine.faster_whisper_engine import FasterWhisperEngine
from shoutabout.vocab import build_stt_hints, parse_vocab

TEN_SECOND_CLIPS = ("plain-01.m4a", "vocab-03.m4a")
LONG_CLIP = "long-01.m4a"
GAP_S = 0.3


@dataclass
class Clip:
    name: str
    audio: np.ndarray
    text: str
    terms: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return len(self.audio) / SAMPLE_RATE


@dataclass
class Result:
    config: str
    model: str
    device: str
    compute_type: str
    cpu_threads: int
    beam_size: int
    load_s: float
    ten_s_clip_s: float
    ten_s_audio_s: float
    long_clip_s: float
    long_audio_s: float
    accuracy: float
    term_hit_rate: float
    clips: int
    rss_mb: float | None
    vram_mb: int | None
    error: str | None = None


def gpu_used_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        return int(float(out.strip().splitlines()[0]))
    except Exception:
        return None


def rss_mib() -> float | None:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 2**20
    except Exception:
        return None


def load_clips(folder: Path) -> tuple[list[Clip], Clip, Clip]:
    manifest = json.loads((folder / "manifest.json").read_text("utf-8-sig"))
    clips: list[Clip] = []
    by_name: dict[str, Clip] = {}
    for entry in manifest:
        name = entry["file"]
        if not name.endswith(".m4a") or entry.get("kind") == "silence":
            continue
        clip = Clip(name, decode_audio(folder / name), entry["text"], entry.get("expect_terms", []))
        clips.append(clip)
        by_name[name] = clip
    gap = np.zeros(int(GAP_S * SAMPLE_RATE), dtype=np.float32)
    parts = [by_name[n] for n in TEN_SECOND_CLIPS]
    ten = Clip(
        "10s",
        np.concatenate([parts[0].audio, gap, parts[1].audio]),
        " ".join(p.text for p in parts),
        [t for p in parts for t in p.terms],
    )
    return clips, ten, by_name[LONG_CLIP]


def parse_config(spec: str) -> tuple[str, str, str, int, int]:
    parts = spec.split(":")
    if len(parts) < 3:
        raise SystemExit(f"bad --config {spec!r}: expected model:device:compute[:threads[:beam]]")
    threads = int(parts[3]) if len(parts) > 3 and parts[3] else 0
    beam = int(parts[4]) if len(parts) > 4 and parts[4] else 5
    return parts[0], parts[1], parts[2], threads, beam


def timed(engine: FasterWhisperEngine, clip: Clip, hints, runs: int) -> tuple[float, str]:
    times, text = [], ""
    for _ in range(runs):
        t0 = time.perf_counter()
        text = engine.transcribe(clip.audio, hints)
        times.append(time.perf_counter() - t0)
    return statistics.median(times), text


def bench(spec: str, args: argparse.Namespace, clips, ten: Clip, long: Clip) -> Result:
    model, device, compute, threads, beam = parse_config(spec)
    cache = None
    if not modelstore.is_ready(model):
        cache = str(Path(args.cache).resolve())
        if not modelstore.is_ready(model, cache_dir=cache):
            print(f"  downloading {model} into {cache} ...", flush=True)
            modelstore.download(model, cache_dir=cache)
    cfg = SttConfig(
        model=model,
        device="cuda" if device == "cuda" else "cpu",
        compute_type=compute if device == "cuda" else "float16",
        cpu_compute_type=compute if device == "cpu" else "int8",
        beam_size=beam,
        download_root=cache,
    )
    terms = parse_vocab(example_vocab_text())
    gc.collect()
    rss0, vram0 = rss_mib(), gpu_used_mib()
    t0 = time.perf_counter()
    engine = FasterWhisperEngine(cfg, cpu_threads=threads)
    try:
        engine.load()
    except Exception as exc:
        return Result(
            spec,
            model,
            device,
            compute,
            threads,
            beam,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            error=str(exc),
        )
    load_s = time.perf_counter() - t0
    rss1, vram1 = rss_mib(), gpu_used_mib()
    hints = build_stt_hints(
        terms, engine.count_tokens, cfg.max_prompt_tokens, cfg.prompt_mode
    ).hints

    ten_s, ten_text = timed(engine, ten, hints, args.runs)
    long_s, _ = timed(engine, long, hints, max(1, args.runs - 1))
    accs, hits, total_terms = [], 0, 0
    for clip in clips:
        text = engine.transcribe(clip.audio, hints)
        accs.append(word_accuracy(clip.text, text))
        for term in clip.terms:
            total_terms += 1
            hits += term in text
    result = Result(
        config=spec,
        model=model,
        device=device,
        compute_type=compute,
        cpu_threads=threads,
        beam_size=beam,
        load_s=round(load_s, 2),
        ten_s_clip_s=round(ten_s, 3),
        ten_s_audio_s=round(ten.seconds, 1),
        long_clip_s=round(long_s, 3),
        long_audio_s=round(long.seconds, 1),
        accuracy=round(statistics.mean(accs), 4),
        term_hit_rate=round(hits / total_terms, 4) if total_terms else 1.0,
        clips=len(clips),
        rss_mb=round(rss1 - rss0) if rss0 is not None and rss1 is not None else None,
        vram_mb=(vram1 - vram0) if vram0 is not None and vram1 is not None else None,
    )
    print(f"  10 s clip -> {ten_text!r}", flush=True)
    del engine
    gc.collect()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audio", default="test-audio", help="folder with manifest.json")
    parser.add_argument("--config", action="append", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cache", default=".tmp/hf", help="download cache for new models")
    parser.add_argument("--voices", default="all", choices=("all", "zira", "david"))
    parser.add_argument("--json", help="append results to this JSON-lines file")
    args = parser.parse_args(argv)

    clips, ten, long = load_clips(Path(args.audio))
    if args.voices == "zira":
        clips = [c for c in clips if "-david" not in c.name]
    elif args.voices == "david":
        clips = [c for c in clips if "-david" in c.name]
    print(f"{len(clips)} clips; 10 s clip = {ten.seconds:.1f} s; long clip = {long.seconds:.1f} s")
    header = (
        f"{'config':<34} {'load s':>6} {'10s clip':>8} {'RTF':>6} {'25s clip':>8} "
        f"{'acc':>6} {'terms':>6} {'RAM MB':>7} {'VRAM MB':>7}"
    )
    rows = []
    for spec in args.config:
        print(f"{spec} ...", flush=True)
        r = bench(spec, args, clips, ten, long)
        rows.append(r)
        if args.json:
            with open(args.json, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(r)) + "\n")
        if r.error:
            print(f"  failed: {r.error}")
            continue
        print(
            f"  load {r.load_s:.1f} s, 10 s clip {r.ten_s_clip_s:.2f} s, 25 s clip "
            f"{r.long_clip_s:.2f} s, accuracy {r.accuracy:.1%}, terms {r.term_hit_rate:.0%}",
            flush=True,
        )
    print()
    print(header)
    print("-" * len(header))
    for r in rows:
        if r.error:
            print(f"{r.config:<34} failed: {r.error[:60]}")
            continue
        print(
            f"{r.config:<34} {r.load_s:>6.1f} {r.ten_s_clip_s:>8.2f} "
            f"{r.ten_s_clip_s / r.ten_s_audio_s:>6.3f} {r.long_clip_s:>8.2f} "
            f"{r.accuracy:>6.1%} {r.term_hit_rate:>6.0%} {r.rss_mb or 0:>7} {r.vram_mb or 0:>7}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
