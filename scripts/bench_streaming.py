"""Benchmark streamed dictations ("transcribe while you talk") against whole files.

For each audio file it compares, with the hub's own Pipeline and the same model:

* **whole**: ``process_audio`` on the full recording (what ``POST /transcribe`` does after
  decoding): the wait is all of STT + cleanup;
* **streamed**: a :class:`shoutabout.streaming.StreamSession` fed the recording as 2 s
  chunks of 16-bit PCM (exactly what the phone sends), then ``finish()`` is timed: the
  wait after "stop". Simulated by default: each chunk goes in once the background has
  done what it can with the audio so far, so the cuts are the ones real time would make,
  without the waiting. ``--realtime`` feeds one chunk every 2 s instead (as slow as the
  audio is long);
* **streamed, no context**: the same without the previous stretch's words as context.

It prints the median finish latency over ``--runs`` and how much the texts differ (word
error rate between whole and streamed, and against ``--reference`` when given).

    .venv\\Scripts\\python.exe scripts\\bench_streaming.py talk.wav --reference talk.txt \\
        --config %APPDATA%\\shoutabout\\config.toml --cleanup --runs 3

The config is only read. Nothing is pasted, nothing goes to Recent, the hub isn't
involved; the speech model is loaded in this process (about 2 GB of VRAM for
large-v3-turbo). ``--cleanup`` uses the Ollama server and model from the config.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from shoutabout import streaming
from shoutabout.audio import decode_audio
from shoutabout.cli import normalize_words
from shoutabout.config import Config, example_vocab_text, parse_config
from shoutabout.engine.base import SAMPLE_RATE
from shoutabout.inject.base import NullInjector
from shoutabout.pipeline import Pipeline
from shoutabout.state import HubState
from shoutabout.vocab import build_stt_hints, parse_vocab

CHUNK_S = 2.0


class StaticVocab:
    """A fixed vocabulary in the shape the pipeline wants (no file watching)."""

    def __init__(self, terms: list[str]) -> None:
        self.terms = tuple(terms)

    def get(self) -> Any:
        return SimpleNamespace(terms=self.terms, version=1)

    def stt_hints(self, count_tokens: Any, max_tokens: int, mode: str) -> Any:
        return build_stt_hints(self.terms, count_tokens, max_tokens, mode)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate (substitutions + insertions + deletions) / reference words, on
    lower-cased words without punctuation."""
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def pcm_chunks(audio: np.ndarray) -> list[bytes]:
    pcm = (np.clip(audio, -1.0, 32767 / 32768) * 32768.0).astype("<i2").tobytes()
    step = int(CHUNK_S * SAMPLE_RATE) * 2
    return [pcm[i : i + step] for i in range(0, len(pcm), step)]


def run_whole(pipeline: Pipeline, audio: np.ndarray) -> dict[str, Any]:
    r = pipeline.process_audio(audio, target="phone", route="bench", deliver=False)
    t = r.timings.as_dict()
    return {"total": t["total"], "stt": t["stt"], "cleanup": t["cleanup"], "text": r.text,
            "raw": r.raw_text, "cleanup_status": r.cleanup.status}  # fmt: skip


def run_streamed(
    pipeline: Pipeline, audio: np.ndarray, *, realtime: bool, context: bool
) -> dict[str, Any]:
    streaming.USE_CONTEXT = context
    session = pipeline.open_stream(target="phone", route="bench")
    try:
        for seq, chunk in enumerate(pcm_chunks(audio)):
            if realtime and seq:
                time.sleep(CHUNK_S)
            session.append(seq, chunk)
            if not realtime:
                session.wait_idle(timeout=60)
        # In real time "stop" comes right after the last chunk (a stretch may still be
        # transcribing: that's part of the wait).
        t0 = time.perf_counter()
        r = session.finish(chunks=session.next_seq)
        wait = (time.perf_counter() - t0) * 1000
        tail_s = (len(audio) - (session.stretches[-1].end if session.stretches else 0)) / 16000
    finally:
        streaming.USE_CONTEXT = True
        session.cancel()
    t = r.timings.as_dict()
    x = r.extra
    return {"total": round(wait), "stt": t["stt"], "cleanup": t["cleanup"], "text": r.text,
            "raw": r.raw_text, "cleanup_status": r.cleanup.status, "tail_s": round(tail_s, 1),
            "stretches": x["stretches"], "bg_stt": round(x["bg_stt_ms"]),
            "bg_cleanup": round(x["bg_cleanup_ms"]),
            "cuts_s": [round(s.end / 16000, 1) for s in session.stretches]}  # fmt: skip


def median(values: list[float]) -> float:
    return round(statistics.median(values)) if values else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("audio", nargs="+", type=Path, help="audio files (16 kHz WAV is exact)")
    ap.add_argument("--reference", action="append", type=Path, default=[],
                    help="the spoken text, one file per audio file (in order)")  # fmt: skip
    ap.add_argument("--config", type=Path, help="config.toml to read [stt]/[cleanup] from")
    ap.add_argument("--vocab", type=Path, help="vocab.txt (default: the example vocabulary)")
    ap.add_argument("--cleanup", action="store_true", help="run the LLM cleanup (Ollama)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--realtime", action="store_true", help="feed one chunk every 2 s")
    ap.add_argument("--no-context-run", action="store_true",
                    help="skip the comparison run without context")  # fmt: skip
    ap.add_argument("--json", type=Path, help="write all results here")
    args = ap.parse_args(argv)

    cfg = parse_config(args.config.read_text("utf-8")) if args.config else Config()
    if not args.config:
        cfg.stt.model, cfg.stt.device, cfg.stt.compute_type = "large-v3-turbo", "cuda", "float16"
    vocab_text = args.vocab.read_text("utf-8-sig") if args.vocab else example_vocab_text()
    vocab = StaticVocab(list(parse_vocab(vocab_text)))

    from shoutabout.engine import create_engine

    engine = create_engine(cfg.stt)
    t = time.perf_counter()
    engine.load()
    print(f"loaded {engine.name} in {time.perf_counter() - t:.1f} s", flush=True)
    cleaner = None
    if args.cleanup:
        from shoutabout.cleanup import OllamaCleaner

        cleaner = OllamaCleaner(cfg.cleanup)
        cleaner.warm()
    else:
        cfg.cleanup.enabled = False
    pipeline = Pipeline(config=cfg, engine=engine, vocab=vocab, cleaner=cleaner,
                        injector=NullInjector("benchmark"), state=HubState())  # fmt: skip
    pipeline.process_audio(np.zeros(SAMPLE_RATE * 3, np.float32), deliver=False)  # warm up

    report: list[dict[str, Any]] = []
    for i, path in enumerate(args.audio):
        audio = decode_audio(path, None)
        ref = args.reference[i].read_text("utf-8") if i < len(args.reference) else None
        seconds = len(audio) / SAMPLE_RATE
        print(f"\n== {path.name}: {seconds:.1f} s", flush=True)
        modes: dict[str, list[dict[str, Any]]] = {"whole": [], "streamed": []}
        if not args.no_context_run:
            modes["streamed_no_context"] = []
        for run in range(args.runs):
            modes["whole"].append(run_whole(pipeline, audio))
            modes["streamed"].append(
                run_streamed(pipeline, audio, realtime=args.realtime, context=True)
            )
            if "streamed_no_context" in modes:
                modes["streamed_no_context"].append(
                    run_streamed(pipeline, audio, realtime=args.realtime, context=False)
                )
            print(f"  run {run + 1}: whole {modes['whole'][-1]['total']} ms, "
                  f"streamed {modes['streamed'][-1]['total']} ms", flush=True)  # fmt: skip
        whole = modes["whole"][0]
        entry: dict[str, Any] = {"file": path.name, "seconds": round(seconds, 1)}
        for name, runs in modes.items():
            first = runs[0]
            row = {
                "wait_ms": median([r["total"] for r in runs]),
                "stt_ms": median([r["stt"] for r in runs]),
                "cleanup_ms": median([r["cleanup"] for r in runs]),
                "cleanup": first["cleanup_status"],
                "wer_vs_whole_raw": round(wer(whole["raw"], first["raw"]), 4),
                "wer_vs_whole_text": round(wer(whole["text"], first["text"]), 4),
                "text": first["text"],
                "raw": first["raw"],
            }
            if ref is not None:
                row["wer_vs_reference_raw"] = round(wer(ref, first["raw"]), 4)
                row["wer_vs_reference_text"] = round(wer(ref, first["text"]), 4)
            for key in ("tail_s", "stretches", "bg_stt", "bg_cleanup", "cuts_s"):
                if key in first:
                    row[key] = first[key]
            entry[name] = row
        report.append(entry)
        print(f"  {'mode':<22}{'wait ms':>9}{'stt':>7}{'clean':>7}  cleanup   "
              f"{'WER/whole':>9}{'WER/ref':>9}")  # fmt: skip
        for name in modes:
            row = entry[name]
            ref_wer = row.get("wer_vs_reference_text")
            print(f"  {name:<22}{row['wait_ms']:>9}{row['stt_ms']:>7}{row['cleanup_ms']:>7}  "
                  f"{row['cleanup']:<9}{row['wer_vs_whole_text']:>9.1%}"
                  f"{'' if ref_wer is None else f'{ref_wer:>9.1%}'}")  # fmt: skip
        s = entry["streamed"]
        print(f"  streamed: {s['stretches']} stretches cut at {s['cuts_s']} s, "
              f"tail {s['tail_s']} s, background stt {s['bg_stt']} ms + "
              f"cleanup {s['bg_cleanup']} ms")  # fmt: skip
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
