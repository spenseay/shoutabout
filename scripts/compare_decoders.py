"""Compare ShoutAbout's audio decoders on real files: PyAV/FFmpeg vs. the app's own chain.

Evidence for the change that took FFmpeg out of the Windows app (docs/architecture.md,
"Audio decoding"). For every file it decodes twice:

* ``pyav``: the old decoder (FFmpeg + swresample), only available in source checkouts;
* ``app``: what the installed app does: WAV with numpy, everything else with Windows Media
  Foundation (on Windows), both resampled by shoutabout.resample;

and reports the length of each (and the difference in %), the level (RMS dBFS), the lag that
best aligns the two (cross-correlation, in ms), the correlation after alignment and the
difference level (signal-to-difference ratio, dB).

``--stt`` also runs both decodes through one throw-away Whisper engine (never the hub:
nothing is pasted, no history is written) and compares the transcripts, plus word accuracy
against ``manifest.json`` when the folder has one (scripts/make_test_audio.ps1 writes it).
``--no-pyav-check`` repeats the app transcripts in a child process where ``import av``
fails, like the installed app (faster-whisper then gets shoutabout.avshim's placeholder).

Usage (Windows, from the repo root; HF_HUB_OFFLINE=1 keeps it from touching the network):

    $env:HF_HUB_OFFLINE = "1"
    .venv\\Scripts\\python.exe scripts\\compare_decoders.py test-audio\\*.m4a `
        --stt --no-pyav-check --json .tmp\\compare.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
MAX_LAG_S = 0.25


@dataclass
class Row:
    file: str
    app_backend: str = ""
    pyav_s: float = 0.0
    app_s: float = 0.0
    length_diff_pct: float = 0.0
    pyav_rms_db: float = 0.0
    app_rms_db: float = 0.0
    rms_diff_db: float = 0.0
    lag_ms: float = 0.0
    correlation: float = 0.0
    diff_db: float = 0.0
    pyav_decode_ms: float = 0.0
    app_decode_ms: float = 0.0
    pyav_text: str | None = None
    app_text: str | None = None
    isolated_text: str | None = None
    same_text: bool | None = None
    pyav_accuracy: float | None = None
    app_accuracy: float | None = None
    notes: list[str] = field(default_factory=list)


def rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12))


def align(ref: np.ndarray, test: np.ndarray, max_lag: int) -> tuple[int, float, float]:
    """(lag, correlation, signal-to-difference dB): ``test[i + lag]`` lines up with
    ``ref[i]``; the lag is searched within +-max_lag samples by FFT cross-correlation."""
    n = len(ref) + len(test)
    size = 1 << (n - 1).bit_length()
    xc = np.fft.irfft(np.fft.rfft(test, size) * np.conj(np.fft.rfft(ref, size)), size)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    values = np.concatenate([xc[: max_lag + 1], xc[size - max_lag :]])
    lag = int(lags[int(np.argmax(values))])
    if lag >= 0:
        a, b = ref, test[lag:]
    else:
        a, b = ref[-lag:], test
    m = min(len(a), len(b))
    a, b = a[:m].astype(np.float64), b[:m].astype(np.float64)
    corr = float(np.corrcoef(a, b)[0, 1]) if m > 1 and a.std() > 0 and b.std() > 0 else 0.0
    noise = float(np.sum((a - b) ** 2))
    sdr = float(10 * np.log10(np.sum(a**2) / noise)) if noise > 0 else float("inf")
    return lag, corr, sdr


def decode(path: Path, backends: tuple[str, ...]):
    from shoutabout.audio import decode_audio_info

    t0 = time.perf_counter()
    info = decode_audio_info(path, backends=backends)
    return info, (time.perf_counter() - t0) * 1000


def load_manifest(files: list[Path]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for folder in {f.parent for f in files}:
        manifest = folder / "manifest.json"
        if manifest.is_file():
            for entry in json.loads(manifest.read_text("utf-8-sig")):
                texts[entry["file"]] = entry["text"]
    return texts


def make_engine(args: argparse.Namespace):
    from shoutabout.config import SttConfig
    from shoutabout.engine.faster_whisper_engine import FasterWhisperEngine

    cfg = SttConfig(
        model=args.model,
        device=args.device,
        compute_type=args.compute_type,
        beam_size=args.beam,
        download_root=args.cache,
    )
    engine = FasterWhisperEngine(cfg)
    engine.load()
    return engine


def transcribe(engine, audio: np.ndarray) -> str:
    from shoutabout.engine.base import SttHints

    return engine.transcribe(audio, SttHints())  # no vocab: compare the decoders only


def worker(args: argparse.Namespace) -> int:
    """Child process: PyAV blocked, app decoders + the engine, JSON on stdout."""
    sys.modules["av"] = None  # type: ignore[assignment]  # `import av` now fails
    from shoutabout import avshim

    real = avshim.ensure_av_importable()
    engine = make_engine(args)
    out = {"real_pyav": real, "texts": {}, "backends": {}}
    for f in args.files:
        info, _ms = decode(Path(f), ("wav", "mediafoundation", "pyav"))
        out["backends"][Path(f).name] = info.backend
        out["texts"][Path(f).name] = transcribe(engine, info.samples)
    print(json.dumps(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", help="audio files, folders or glob patterns")
    parser.add_argument("--stt", action="store_true", help="also compare the transcripts")
    parser.add_argument("--no-pyav-check", action="store_true", help="repeat without PyAV")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam", type=int, default=5)
    parser.add_argument("--cache", default=None, help="model cache (default: Hugging Face's)")
    parser.add_argument("--json", type=Path, help="write the rows here")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args)

    from shoutabout.audio import MEDIA_FOUNDATION, PYAV, WAV
    from shoutabout.cli import expand_inputs, word_accuracy

    files, problems = expand_inputs(args.files)
    for p in problems:
        print(p, file=sys.stderr)
    if not files:
        return 2
    texts = load_manifest(files)
    app_chain = (WAV, MEDIA_FOUNDATION) if sys.platform == "win32" else (WAV,)
    engine = make_engine(args) if args.stt else None

    rows: list[Row] = []
    for path in files:
        row = Row(path.name)
        try:
            ref, ref_ms = decode(path, (PYAV,))
            app, app_ms = decode(path, app_chain)
        except Exception as exc:
            row.notes.append(f"decode failed: {type(exc).__name__}: {exc}")
            rows.append(row)
            continue
        a, b = ref.samples, app.samples
        lag, corr, sdr = align(a, b, int(MAX_LAG_S * SAMPLE_RATE))
        row.app_backend = app.backend
        row.pyav_s, row.app_s = len(a) / SAMPLE_RATE, len(b) / SAMPLE_RATE
        row.length_diff_pct = 100.0 * (len(b) - len(a)) / max(1, len(a))
        row.pyav_rms_db, row.app_rms_db = rms_db(a), rms_db(b)
        row.rms_diff_db = row.app_rms_db - row.pyav_rms_db
        row.lag_ms, row.correlation, row.diff_db = 1000 * lag / SAMPLE_RATE, corr, sdr
        row.pyav_decode_ms, row.app_decode_ms = ref_ms, app_ms
        if engine is not None:
            row.pyav_text = transcribe(engine, a)
            row.app_text = transcribe(engine, b)
            row.same_text = row.pyav_text == row.app_text
            expected = texts.get(path.name)
            if expected is not None:
                row.pyav_accuracy = word_accuracy(expected, row.pyav_text)
                row.app_accuracy = word_accuracy(expected, row.app_text)
        rows.append(row)

    isolated: dict | None = None
    if args.no_pyav_check and args.stt:
        cmd = [sys.executable, __file__, "--worker", *[str(f) for f in files]]
        cmd += ["--model", args.model, "--device", args.device]
        cmd += ["--compute-type", args.compute_type, "--beam", str(args.beam)]
        if args.cache:
            cmd += ["--cache", args.cache]
        done = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ))
        if done.returncode != 0:
            print(done.stderr, file=sys.stderr)
            return 1
        isolated = json.loads(done.stdout.strip().splitlines()[-1])
        for row in rows:
            row.isolated_text = isolated["texts"].get(row.file)
            if isolated["backends"].get(row.file) != row.app_backend:
                row.notes.append(f"child used {isolated['backends'].get(row.file)}")

    print_table(rows, isolated)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps([asdict(r) for r in rows], indent=2), "utf-8")
    bad = [r for r in rows if r.notes or (r.same_text is False)]
    return 1 if bad else 0


def print_table(rows: list[Row], isolated: dict | None) -> None:
    head = (
        f"{'file':<26} {'app':>5} {'pyav s':>7} {'app s':>7} {'len %':>6} {'dB diff':>7} "
        f"{'lag ms':>6} {'corr':>8} {'SDR dB':>7} {'ms pyav/app':>11}  text"
    )
    print(head)
    print("-" * len(head))
    for r in rows:
        if r.notes and not r.app_backend:
            print(f"{r.file:<26} {'; '.join(r.notes)}")
            continue
        same = {True: "same", False: "DIFFERENT", None: ""}[r.same_text]
        if r.isolated_text is not None:
            same += " / no-PyAV " + ("same" if r.isolated_text == r.app_text else "DIFFERENT")
        print(
            f"{r.file:<26} {r.app_backend[:5]:>5} {r.pyav_s:>7.3f} {r.app_s:>7.3f} "
            f"{r.length_diff_pct:>+6.2f} {r.rms_diff_db:>+7.3f} {r.lag_ms:>+6.1f} "
            f"{r.correlation:>8.5f} {r.diff_db:>7.1f} "
            f"{r.pyav_decode_ms:>5.0f}/{r.app_decode_ms:<5.0f}  {same}"
        )
    ok = [r for r in rows if r.app_backend]
    if not ok:
        return
    print("-" * len(head))
    print(
        f"{len(ok)} files; |length diff| max {max(abs(r.length_diff_pct) for r in ok):.2f} %; "
        f"|level diff| max {max(abs(r.rms_diff_db) for r in ok):.3f} dB; "
        f"lag {min(r.lag_ms for r in ok):+.1f}..{max(r.lag_ms for r in ok):+.1f} ms; "
        f"correlation min {min(r.correlation for r in ok):.5f}, "
        f"median {statistics.median(r.correlation for r in ok):.5f}; "
        f"SDR min {min(r.diff_db for r in ok):.1f} dB"
    )
    if any(r.same_text is not None for r in ok):
        same = sum(bool(r.same_text) for r in ok)
        print(f"transcripts identical: {same}/{len(ok)}")
        acc = [(r.pyav_accuracy, r.app_accuracy) for r in ok if r.pyav_accuracy is not None]
        if acc:
            print(
                f"word accuracy vs manifest: PyAV {statistics.mean(a for a, _ in acc):.4f}, "
                f"app {statistics.mean(b for _, b in acc):.4f} (mean of {len(acc)})"
            )
    if isolated is not None:
        same = sum(r.isolated_text == r.app_text for r in ok)
        print(
            f"without PyAV (child process, real PyAV importable: {isolated['real_pyav']}): "
            f"{same}/{len(ok)} transcripts identical to the app decode above"
        )


if __name__ == "__main__":
    sys.exit(main())
