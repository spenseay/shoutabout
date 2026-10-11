# Speech models: the automatic choice

People never have to pick a model. On first run the PC looks at its own hardware, picks one of
three setups, downloads the model with a progress bar ("Getting ready… 42%"), and remembers
nothing else: the choice is made again at every start, so a new graphics card is used
automatically. Explicit values in `config.toml` always win.

Code: [`hardware.py`](../src/shoutabout/hardware.py) (what the PC has),
[`modelpick.py`](../src/shoutabout/modelpick.py) (the choice),
[`modelstore.py`](../src/shoutabout/modelstore.py) (the download),
[`voice_runtime.py`](../src/shoutabout/voice_runtime.py) (how the hub uses them).

## The three setups

| Shown as | When | Model, device, precision | 10 s dictation on the build PC | `speed` in `GET /info` | Text tidying (cleanup) |
|---|---|---|---|---|---|
| **Best quality** | NVIDIA GPU, CUDA works, ≥ 5 GB VRAM | `large-v3-turbo`, CUDA, `float16` | 0.17 s | `fast` | On if the GPU has ≥ 8 GB **and** a suitable model is already in Ollama |
| **Balanced** | NVIDIA GPU, CUDA works, 3–5 GB VRAM | `large-v3-turbo`, CUDA, `int8_float16` | 0.17 s | `ok` | Off |
| **Balanced** | NVIDIA GPU, CUDA works, 1.5–3 GB VRAM | `small.en`, CUDA, `int8_float16` | 0.16 s | `ok` | Off |
| **Balanced** | No usable GPU; ≥ 6 physical cores and ≥ 8 GB RAM | `small.en`, CPU, `int8`, threads = cores (max 8) | 1.2 s here; expect ~2–3 s on a 6–8 core laptop | `ok` | Off |
| **Light** | Everything else | `base.en`, CPU, `int8`, threads = cores (max 8) | 0.4 s here (4 threads); expect ~1–1.5 s on a 4-core laptop | `slow` | Off |

The explanation shown in the setup and settings windows is one plain sentence, for example
"Your NVIDIA GeForce RTX 4080 SUPER runs the most accurate speech model." or "No NVIDIA graphics
card was found, so your Intel Core i5-8250U (4 cores) runs a light speech model."

`speed` is what the iPhone uses to route: when the PC says `slow`, the phone transcribes on
its own and sends only the text (`POST /paste`).

### Why these thresholds

All numbers below are from the benchmark at the end of this page.

- **Best needs ≥ 5 GB of VRAM.** large-v3-turbo in `float16` added **2.4 GB** of VRAM when it
  loaded (CUDA context included). Cards sold as "6 GB" report about 6.0 GB, and the desktop,
  the browser and the compositor need 0.5–1 GB of their own, so 5 GB leaves real headroom.
- **3–5 GB runs the same model in `int8_float16`.** It added only **1.1 GB**, and it was exactly
  as fast and as accurate as `float16` on the test clips (identical transcripts, 0.17 s for
  10 s); 8-bit weights are widely reported to cost Whisper very little accuracy. (Best keeps
  `float16` because that is the precision the hub has always used.)
- **1.5–3 GB runs `small.en`** (+0.6 GB in `float16`; `int8_float16` is smaller still). Below
  1.5 GB the card isn't worth it and the CPU path is used.
- **Text tidying only on Best with ≥ 8 GB.** Whisper (2.4 GB) plus the 4B cleanup model
  (3.0 GiB, measured for v1) plus the desktop doesn't fit a 6 GB card, and on the CPU the
  cleanup model can't answer inside its 2 s budget, so it would only add delay before falling
  back to the raw text. It is also never downloaded automatically: cleanup turns on only when
  the configured model, or a known-good one (`qwen3:4b-instruct-2507-q4_K_M`,
  `granite4:micro`), is already pulled in a running Ollama. If Ollama starts after the hub
  (common at logon), the hub notices within 30 s and turns cleanup on then.
- **CPU: `small.en` needs ≥ 6 cores and ≥ 8 GB, otherwise `base.en`.** large-v3-turbo on the
  CPU took **3.2 s** for 10 s of speech even on this PC's fast cores (8 threads), which would
  be 6–10 s on a laptop: too slow to call "Balanced". `small.en` took 1.2–1.3 s here; laptop
  cores are roughly 2–3× slower per core than this desktop's, so ~2–3 s on a 6–8 core laptop.
  `base.en` took 0.42 s with 4 threads and 0.58 s with 2, so ~1–1.5 s on a typical 4-core
  laptop: "a few seconds" at worst, as the brief asks.
- **English-only models (`.en`) for the small sizes.** ShoutAbout transcribes English
  (`[stt] language = "en"`), and at small sizes the English-only Whisper models are the more
  accurate ones (the Whisper paper's English results: tiny.en < base.en < small.en, each
  clearly better than the multilingual model of the same size).
- **Threads = physical cores, at most 8.** CTranslate2 uses 4 threads unless told otherwise.
  8 threads were 12–19 % faster than 4; 16 were *slower* than 8 on this hybrid CPU (8 fast
  P-cores, 12 E-cores), so 8 is the cap.
- **distil-small.en was rejected**: twice as fast as small.en on the GPU, but it dropped half
  the sentence of the 10 s clip and half the vocabulary terms (68.7 % word accuracy).

### What the test clips can and can't tell

The test clips are Windows text-to-speech (clean, studio-quiet voices). Every model from tiny.en
up got 96–97 % word accuracy on them, so they measure **speed and memory**, not real-world
accuracy. On real voices, accents and noisy rooms the differences between sizes are much
larger, which is why the choice always takes the biggest model the hardware runs comfortably.

### Macs (Apple Silicon)

No NVIDIA GPU and no CUDA: Whisper runs on the performance cores (`hw.perflevel0`), one
thread each (up to 10), `int8`. Every M-series Mac (≥ 4 performance cores, ≥ 8 GB) gets
**Balanced** (`small.en`, speed `ok`); anything smaller **Light** (`base.en`, `slow`).
`large-v3-turbo` only when picked by hand (Best). Cleanup is off on Macs for now. The
expected times are estimates, not measurements; see [macos.md](macos.md#speech-model-on-apple-silicon).

## How config.toml overrides it

`[stt] model`, `device` and `compute_type` are `"auto"` in new configs, and `[cleanup] enabled`
is `"auto"`. Any explicit value wins:

| Config | Result |
|---|---|
| All explicit (a v1 config: `large-v3-turbo` / `cuda` / `float16`, `enabled = true`) | Exactly what v1 did. The hardware and Ollama aren't even probed. |
| `model = "small.en"`, the rest `"auto"` | small.en on the best device and precision for this PC. |
| `device = "cpu"`, the rest `"auto"` | The CPU choice for this PC's cores and RAM. |
| `tier = "best"` / `"balanced"` / `"light"` (model, device and compute type `"auto"`) | That tier, picked by hand (the settings window's override). It runs even when the PC can't really manage it (Best without a usable GPU runs on the CPU); the explanation then says "It will be slow on this PC" and `speed` is `slow`. |
| `[cleanup] enabled = true` / `false` | Forced on / off. |

With `device = "auto"` the engine also falls back to the CPU if the GPU fails to load (out of
memory because a game is running, a driver update in progress), instead of stopping.

`python -m shoutabout info` shows the resolved model, device and tier; `doctor` adds a "Model
choice" line with the reason and the detected hardware. The hub logs one line at start, e.g.
`speech model: Best quality: large-v3-turbo on cuda/float16, cleanup on (qwen3:4b-instruct-2507-q4_K_M) [auto]`.

## Downloads

| Model | Download |
|---|---|
| large-v3-turbo | 1,622 MB |
| small.en | 486 MB |
| base.en | 148 MB |

- The files go where faster-whisper keeps them: the Hugging Face cache
  (`%USERPROFILE%\.cache\huggingface\hub`) or `[stt] download_root`, in the standard cache
  layout. A model that is already there is never downloaded again or moved.
- Progress is reported in bytes (`HubState.progress` is the percent, `HubState.download` the
  bytes, `HubState.progress_text` the words). While it runs the hub is "starting" and
  `/transcribe` answers 503; "Talk to this PC" shows "Getting ready… 42%".
- **Resume.** The big file streams into `blobs/<sha256>.partial` and continues with an HTTP range
  request where it stopped (quitting, a dropped connection, sleep). Its SHA-256 is checked
  before it is used; a damaged file is thrown away. (Current huggingface_hub releases discard
  partial files, which is why `model.bin` is fetched by ShoutAbout itself; the small files and
  the cache bookkeeping still go through `huggingface_hub.snapshot_download`.)
- **Cancel.** Quitting the hub cancels within one 1 MB chunk.
- **Concurrent callers** (the setup window and the hub's loader) share one download: the second
  caller joins the first and gets the same progress and the same folder.
- Disk space is checked first ("There isn't enough disk space for the speech model: it needs
  about 1,622 MB and 900 MB is free.").

## Benchmark

`scripts/bench_models.py` (needs `scripts/make_test_audio.ps1` output). Run on 2026-10-10 on the
build PC: Intel Core i7-14700KF (8 P + 12 E cores), 64 GB RAM, NVIDIA GeForce RTX 4080 SUPER
16 GB (driver 591.86), Windows 11, Python 3.12, faster-whisper 1.2.1, CTranslate2 4.8.2. The
owner's hub and Ollama were running on the same GPU. Beam size 5 (the default), VAD on, the
example vocabulary as hints, exactly like the hub.

- **10 s clip**: `plain-01` + 0.3 s pause + `vocab-03` (10.1 s), median of 3 runs after the
  warm-up. **25 s clip**: `long-01` (24.8 s).
- **Accuracy**: mean word accuracy (`difflib` word match, as `shoutabout transcribe`) and
  vocabulary-term hits over the test clips (28 clips, both voices, on the GPU; 14 clips, Zira, on
  the CPU, to keep the CPU busy for less time).
- **VRAM / RAM**: growth when the model loaded (`nvidia-smi`, process RSS).

| Model, device, precision (threads, beam) | Load | 10 s clip | RTF | 25 s clip | Word acc. | Terms | VRAM |
|---|---|---|---|---|---|---|---|
| large-v3-turbo, cuda, float16 | 1.6 s | **0.17 s** | 0.016 | 0.29 s | 96.5 % | 100 % | +2,410 MB |
| large-v3-turbo, cuda, int8_float16 | 2.1 s | **0.17 s** | 0.016 | 0.29 s | 96.5 % | 100 % | +1,091 MB |
| small.en, cuda, float16 | 0.4 s | 0.16 s | 0.016 | 0.49 s | 96.8 % | 100 % | +584 MB |
| distil-small.en, cuda, float16 (rejected) | 0.3 s | 0.07 s | 0.007 | 0.23 s | 68.7 % | 50 % | +392 MB |
| tiny.en, cpu, int8 (4) | 0.6 s | 0.23 s | 0.022 | 0.39 s | 96.4 % | 93 % | RAM +127 MB |
| base.en, cpu, int8 (4) | 0.5 s | **0.42 s** | 0.041 | 0.71 s | 97.0 % | 93 % | RAM +139 MB |
| base.en, cpu, int8 (2) | 0.7 s | 0.58 s | 0.057 | 0.95 s | 97.0 % | 93 % | RAM +135 MB |
| small.en, cpu, int8 (4) | 1.5 s | 1.30 s | 0.129 | 2.20 s | 97.0 % | 100 % | RAM +317 MB |
| small.en, cpu, int8 (4, beam 1) | 1.5 s | 1.04 s | 0.103 | 1.42 s | 97.0 % | 100 % | RAM +323 MB |
| small.en, cpu, int8 (8) | 1.4 s | **1.15 s** | 0.114 | 2.08 s | 97.0 % | 100 % | RAM +341 MB |
| large-v3-turbo, cpu, int8 (4) | 5.7 s | 3.91 s | 0.387 | 4.58 s | 97.0 % | 100 % | RAM +946 MB |
| large-v3-turbo, cpu, int8 (8) | 4.7 s | 3.17 s | 0.313 | 3.75 s | 97.0 % | 100 % | RAM +921 MB |
| large-v3-turbo, cpu, int8 (16) | 5.6 s | 3.70 s | 0.366 | 4.30 s | 97.0 % | 100 % | RAM +948 MB |

RTF = seconds of computing per second of audio, for the 10 s clip. A short clip costs about
as much as a 30 s one on the CPU, because Whisper always encodes a 30 s window.

Notes:

- Greedy decoding (beam 1) saved 20–35 % on the CPU with identical results on these clips. It
  isn't used yet, because real speech benefits from the beam; it is the obvious next lever for
  the Light tier if laptops prove slow.
- Not measured: real laptops, older NVIDIA generations (the precision is taken from what
  CTranslate2 reports the card supports, e.g. Pascal cards without fast `float16` get `int8`),
  and AMD/Intel GPUs (CTranslate2 can't use them; those PCs get a CPU setup).

To re-run (it downloads models it doesn't have into `.tmp\hf`, never touching the main cache):

```powershell
.\.venv\Scripts\python.exe scripts\bench_models.py --audio test-audio --cache .tmp\hf `
  --config large-v3-turbo:cuda:float16 --config small.en:cpu:int8:8 --config base.en:cpu:int8:4
```
