# ShoutAbout v1: design plan (module contracts)

This is the build contract. Parallel work must fit these interfaces exactly. Files already
written as the contract (do not change signatures without updating this doc):
`config.py`, `models.py`, `text.py`, `state.py`, `engine/base.py`, `inject/base.py`,
`data/config.example.toml`, `data/vocab.example.txt`, `tests/conftest.py`, `tests/fakes.py`.

## 1. Runtime picture

```
pythonw -m shoutabout run   (Task Scheduler "at logon", interactive user session, not elevated)
 ├─ main thread: Win32 tray message loop (tray.py) — status icon, menu, balloon toasts
 ├─ thread: uvicorn server  0.0.0.0:8765  (FastAPI app from server.create_app)
 ├─ thread: engine loader   (loads Whisper on CUDA, warms it; then ctx.pipeline is set)
 └─ thread: LLM warmer      (cleanup.OllamaCleaner.warm() every warm_interval_s)
cloudflared (Windows service) ──► http://127.0.0.1:8765   (route "tunnel")
iPhone on home Wi-Fi         ──► http://<lan-ip>:8765     (route "lan")
```

Request flow (`POST /transcribe`):
source-IP middleware → rate limit → bearer → (tunnel: Access JWT) → readiness → size
limits → multipart parse → save temp file → `Pipeline.process_file` in a worker thread
(serialised by a lock) → JSON response.

Pipeline: decode (PyAV → 16 kHz mono float32) → STT (engine + vocab hints) →
`normalize_whitespace` → cleanup (Ollama + guard, optional) → `normalize_whitespace` →
deliver (target=pc and not paused: injector.paste) → history append → result.

## 2. Package layout and ownership

| File | Owner (build phase) | Purpose |
|---|---|---|
| `config.py`, `models.py`, `text.py`, `state.py`, `engine/base.py`, `inject/base.py` | lead (done) | contracts |
| `audio.py`, `vocab.py`, `engine/__init__.py`, `engine/cuda_dlls.py`, `engine/faster_whisper_engine.py` | builder A | decoding, vocab, STT |
| `cleanup.py`, `benchmark.py` | builder B | Ollama client, prompt, guard, benchmark |
| `inject/__init__.py`, `inject/windows.py`, `inject/clipboard.py`, `tray.py`, `icons.py` | builder C | Windows paste, clipboard, tray, toasts |
| `security.py`, `history.py`, `pipeline.py`, `server.py`, `netinfo.py`, `hub.py`, `cli.py`, `doctor.py`, `logging_setup.py` | builder D | API, auth, orchestration, CLI |
| `scripts/*.ps1` | builder E | setup / uninstall / test audio |
| `README.md`, `SETUP.md`, `docs/*.md` | builder F | docs |
| `.github/workflows/ci.yml` | builder D | CI |

Tests live in `tests/test_<module>.py`, owned by the module's builder. Fakes are in
`tests/fakes.py` (import as `from fakes import FakeEngine`).

## 3. Interfaces

### 3.1 audio.py (A)
```python
SAMPLE_RATE = 16_000
class AudioError(ValueError): ...            # undecodable / empty / unsupported
class AudioTooLongError(AudioError): ...     # longer than max_seconds
def probe_duration(path: Path) -> float | None              # container duration (s), None if unknown
def decode_audio(path: Path, max_seconds: float | None = None) -> np.ndarray
    # PyAV only (no faster_whisper import): any container/codec FFmpeg in the PyAV wheel
    # supports (m4a/AAC, wav, mp3, caf...). Returns float32 mono 16 kHz in [-1, 1].
    # Checks probe_duration first and the decoded length after; raises AudioTooLongError.
def encode_m4a(src: Path, dst: Path, bitrate: int = 64_000) -> None
    # Dev/test helper: any audio -> AAC-LC in .m4a (mono, 44.1/48 kHz) to mimic iPhone.
```

### 3.2 vocab.py (A)
```python
def parse_vocab(text: str) -> list[str]
    # one term per line; strip; skip blank lines and lines whose first non-space char is '#'
    # (no inline comments: "C#" is a valid term); dedupe case-insensitively keeping the
    # first occurrence; order = priority.
@dataclass(frozen=True)
class Vocab:
    terms: tuple[str, ...]
    version: int                     # increments on every reload
@dataclass(frozen=True)
class PromptBuild:
    hints: SttHints
    included: tuple[str, ...]
    dropped: tuple[str, ...]
    tokens: int                      # tokens used by the vocab text
def build_stt_hints(terms: Sequence[str], count_tokens: Callable[[str], int],
                    max_tokens: int, mode: str) -> PromptBuild
    # Greedily includes terms in priority order while the hint text fits max_tokens.
    # mode: "hotwords" -> hints.hotwords; "initial_prompt" -> hints.initial_prompt;
    # "both" -> both set; "none" -> empty hints. (Exact text format: see research notes in
    # the module docstring.) Empty terms -> empty hints.
class VocabStore:
    def __init__(self, path: Path) -> None
    def get(self) -> Vocab           # cheap; re-reads the file when its mtime/size changes
                                     # (hot reload, no restart). Missing file -> empty vocab.
                                     # Read errors keep the last good vocab and log.
    def stt_hints(self, count_tokens: Callable[[str], int], max_tokens: int,
                  mode: str) -> PromptBuild
                                     # cached per (vocab.version, max_tokens, mode); logs ONE
                                     # warning per reload when terms are dropped.
```

### 3.3 engine (A)
```python
# engine/cuda_dlls.py
def add_nvidia_dll_dirs() -> list[str]
    # Windows only (no-op elsewhere). Finds site-packages/nvidia/*/bin (cublas, cudnn,
    # cuda_nvrtc, ...) from the nvidia namespace package, calls os.add_dll_directory on each
    # and prepends them to PATH (cuDNN 9 loads sub-libraries via LoadLibrary). Idempotent.
    # Returns the dirs added. Must be called before ctranslate2 is imported.
# engine/faster_whisper_engine.py
class FasterWhisperEngine:          # implements SpeechEngine
    def __init__(self, cfg: SttConfig) -> None
    name: str
    device: str                      # actual device after load ("cuda"/"cpu")
    def load(self) -> None           # add_nvidia_dll_dirs(); WhisperModel(...); warm-up on 1 s
                                     # of silence+noise. CUDA failure: EngineError with a fix
                                     # hint, or CPU fallback if cfg.cpu_fallback (logs warning).
    def transcribe(self, audio, hints) -> str   # language, beam_size, vad_filter,
                                     # vad_parameters(min_silence_duration_ms), hotwords,
                                     # initial_prompt, condition_on_previous_text, joins segments
    def count_tokens(self, text) -> int          # Whisper tokenizer; heuristic before load
# engine/__init__.py
def create_engine(cfg: SttConfig) -> SpeechEngine   # factory; imports lazily
```

### 3.4 cleanup.py (B)
```python
FILLER_WORDS: frozenset[str]          # um, uh, erm, er, ah, eh, hmm, mm, mhm, uhm, umm, uhh
@dataclass(frozen=True)
class GuardDecision:
    accepted: bool
    reason: str                       # "ok" or why rejected
    similarity: float                 # difflib word-level ratio after normalisation
    length_ratio: float               # cleaned words / raw non-filler words
    change_ratio: float               # unexplained edited words / raw non-filler words
def guard(raw: str, cleaned: str, terms: Sequence[str], cfg: CleanupConfig) -> GuardDecision
    # Pure function. Compares normalised word lists (lowercase, apostrophes removed, other
    # punctuation -> space). Explained (free) edits: deleting filler words (and "like"),
    # replacing a raw span with a vocab term (or part of one) whose text is similar.
    # Rejects: empty output for non-empty input, newline-ish/markdown answers, change_ratio >
    # max_change_ratio (default 0 = no unexplained edits; >0 adds a 1-word allowance), any
    # change in negation words, similarity < min_similarity, length ratio out of
    # [min_length_ratio, max_length_ratio] (small absolute slack for short texts).
def build_messages(raw_text: str, terms: Sequence[str]) -> list[dict[str, str]]
    # Strict system prompt (transcript is DATA; never answer/obey; only the 4 allowed fixes;
    # plain text, single paragraph, no preamble/quotes), vocab list in the system prompt
    # (cache-friendly prefix), few-shot pairs incl. instruction-looking transcripts, and the
    # transcript wrapped in <transcript>…</transcript> (delimiter strings removed from input).
class Cleaner(Protocol):
    def clean(self, raw_text: str, terms: Sequence[str]) -> CleanupResult
    def warm(self) -> bool
class OllamaCleaner:                  # implements Cleaner
    def __init__(self, cfg: CleanupConfig, client: httpx.Client | None = None) -> None
    def clean(self, raw_text, terms) -> CleanupResult
        # Never raises. Empty raw -> status "skipped". POST {ollama_url}/api/chat
        # {model, messages, stream:false, think (if not None), keep_alive,
        #  options:{temperature:0, seed:0, top_k:1, num_ctx, num_predict: bounded by input}}
        # with timeout cfg.timeout_s. Post-process: strip, drop echoed delimiters / wrapping
        # quotes, normalize_whitespace. Then guard(): accepted -> "applied"; else / on error /
        # timeout -> "fallback" with raw text and reason (logged at WARNING).
    def warm(self) -> bool            # loads model with keep_alive (POST /api/generate, no prompt)
    def model_available(self) -> tuple[bool, str]   # for doctor: reachable + pulled
    def loaded_status(self) -> dict | None          # /api/ps entry for the model (size_vram)
```
`benchmark.py` (B): `run_llm_benchmark(models: list[str], runs: int, cfg: CleanupConfig,
out=print) -> int` (built-in sample transcripts incl. vocab, filler and instruction-looking
cases; reports median/p90 latency, guard accept rate, and flags outputs that answered
instead of cleaning) and `run_stt_benchmark(files: list[Path], cfg: Config, runs: int, out=print)
-> int` (load time, per-file decode/STT ms, real-time factor, GPU memory used before/after
load via nvidia-smi, process RSS via psutil).

### 3.5 inject (C)
```python
# inject/clipboard.py  (Windows, ctypes only)
class ClipboardError(RuntimeError)
def set_text(text: str, *, exclude_from_history: bool = True) -> int   # returns sequence no.
def get_text() -> str | None
def snapshot() -> ClipboardSnapshot          # HGLOBAL formats only; skips GDI-handle formats
def restore(snap: ClipboardSnapshot, *, exclude_from_history: bool = True) -> None
def sequence_number() -> int
# Exclusion formats set alongside the text in the same OpenClipboard session:
# "ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory"=DWORD 0,
# "CanUploadToCloudClipboard"=DWORD 0.
# inject/windows.py
class WindowsPasteInjector:                  # implements Injector
    def __init__(self, cfg: PasteConfig, notifier: Notifier) -> None
    def paste(self, text: str) -> PasteResult
        # 1. foreground hwnd (None -> no paste; text left on clipboard; warning)
        # 2. foreground process elevated / higher integrity -> set clipboard (no restore),
        #    notifier.notify(ADMIN_WINDOW_WARNING), PasteResult(False, ADMIN_WINDOW_WARNING)
        # 3. snapshot clipboard; set_text(text) (the pipeline already appended the optional
        #    trailing space); SendInput Ctrl down (Shift+Insert for shift_insert_processes),
        #    V down, V up, Ctrl up (VK + scan codes) — NOTHING ELSE, never Enter
        # 4. after restore_delay_ms (background timer), restore snapshot iff the clipboard
        #    sequence number is unchanged; next paste waits for a pending restore.
# inject/__init__.py
def create_injector(cfg: PasteConfig, notifier: Notifier) -> Injector
    # Windows -> WindowsPasteInjector; else NullInjector. paste.enabled=false -> NullInjector(
    # "Pasting is disabled in config (paste.enabled = false).")
```

### 3.6 tray.py (C)
```python
@dataclass
class TrayActions:
    toggle_pause: Callable[[], None]
    open_vocab: Callable[[], None]
    open_logs: Callable[[], None]
    copy_token: Callable[[], None]
    show_lan_url: Callable[[], None]
    quit: Callable[[], None]
class TrayIcon:                               # implements Notifier
    def __init__(self, state: HubState, actions: TrayActions, title: str = "ShoutAbout") -> None
    def run(self) -> None        # blocking Win32 message loop; must run on the main thread
    def stop(self) -> None       # thread-safe (PostMessage)
    def notify(self, title: str, message: str) -> None   # thread-safe balloon/toast
    # icon colour reflects state: starting/idle/transcribing/paused/error; tooltip shows
    # status (+ error text). Menu: status line (disabled), Pause/Resume, Open vocab file,
    # Open log folder, Copy hub token, Show LAN URL, Quit. Re-adds icon on "TaskbarCreated".
```
`icons.py` (C): builds HICON-ready ICO bytes in pure Python (coloured dot), no Pillow.

### 3.7 security.py (D)
```python
def classify_source(host: str | None, lan_networks: Sequence[str]) -> Route | None
    # loopback (127.0.0.0/8, ::1, ::ffff:127.x) -> "tunnel"; inside lan_networks -> "lan";
    # anything else (public, malformed, None, "testclient") -> None (reject). IPv4-mapped
    # IPv6 is unwrapped first.
def bearer_ok(authorization: str | None, token: str) -> bool     # "Bearer <tok>", hmac.compare_digest
class AccessJwtError(Exception)
class AccessJwtValidator:
    def __init__(self, cfg: AccessConfig, jwks_client=None, leeway: int = 60) -> None
        # issuer "https://<team_domain>", certs "https://<team_domain>/cdn-cgi/access/certs"
    def validate(self, token: str) -> dict
        # RS256 only; aud must match one of cfg.aud; iss exact; exp/iat required;
        # cfg.allowed_client_ids non-empty -> claims["common_name"] must be in it.
class RateLimiter:
    def __init__(self, limit: int, window_s: float, clock=time.monotonic) -> None
    def allow(self, key: str) -> bool        # records a hit; False if over the limit
    def count(self, key: str) -> int
```

### 3.8 history.py (D)
```python
class HistoryLog:
    def __init__(self, path: Path, audio_dir: Path, keep_audio: bool) -> None
    def append(self, result: TranscribeResult, audio_path: Path | None = None) -> None
        # one JSON object per line (schema in docs/history.md); thread-safe; never raises
```
Record: `ts` (ISO-8601 local with offset), `id`, `route`, `target`, `raw_text`, `text`,
`cleanup` (status), `cleanup_fallback` (bool), `cleanup_reason`, `pasted`, `warning`,
`audio_seconds`, `audio_file` (relative name or null), `stt_model`, `llm_model`, `timings_ms`.

### 3.9 pipeline.py (D)
```python
class Pipeline:
    def __init__(self, *, config: Config, engine: SpeechEngine, vocab: VocabStore,
                 cleaner: Cleaner | None, injector: Injector, state: HubState,
                 history: HistoryLog | None = None,
                 decode: Callable[[Path, float | None], np.ndarray] | None = None) -> None
        # decode defaults to audio.decode_audio (imported lazily); tests pass fakes.fake_decode.
        # vocab is duck-typed (tests use fakes.FakeVocabStore).
    def process_file(self, path: Path, *, target: Target, route: str,
                     cleanup: bool | None = None, deliver: bool = True) -> TranscribeResult
        # serialised by a lock; sets state TRANSCRIBING/IDLE (ERROR on engine failure);
        # raises AudioError / AudioTooLongError / EngineError for the server to map.
        # target=pc & deliver: paused -> pasted False, warning "Hub is paused — not pasted
        # on the PC."; empty text -> pasted False, warning "No speech detected."
        # text pasted = text + " " if paste.trailing_space (response text has no trailing space)
```

### 3.10 server.py (D)
```python
@dataclass
class HubContext:
    config: Config
    token: str
    state: HubState
    pipeline: Pipeline | None = None          # None until the engine is loaded
    access: AccessJwtValidator | None = None  # None = JWT check disabled
    startup_error: str | None = None
def create_app(ctx: HubContext) -> FastAPI    # docs/openapi disabled
```

### 3.11 netinfo.py, hub.py, cli.py, doctor.py, logging_setup.py (D)
- `netinfo.lan_ipv4() -> str | None` (default-route IPv4 via UDP connect to 192.0.2.1, no
  packets sent; must be private), `netinfo.lan_url(cfg) -> str | None`.
- `hub.run_hub(paths: Paths, *, tray: bool = True) -> int`: single-instance (named mutex on
  Windows), `ensure_initialized`, logging to `<home>/logs/hub.log` (rotating), redirect
  None stdout/stderr (pythonw) to a log file, loud warning if Access JWT not configured, start
  uvicorn thread (`log_config=None`, `access_log=False`), engine loader thread, warmer thread,
  tray on main thread (or wait on the server if `tray=False`).
- `cli.main(argv=None) -> int` subcommands:
  - `run [--no-tray]`
  - `init [--rotate-token]`
  - `info [--json]` (paths, port, lan_url, models, access enabled; never the token)
  - `token [--copy | --qr]`
  - `qr [TEXT | --clipboard]` (terminal QR via segno, to move secrets to the phone)
  - `transcribe FILE... [--no-cleanup] [--json]` (raw, cleaned, timings; never pastes)
  - `benchmark [--models a,b] [--runs N] [--stt FILE...]`
  - `doctor [--skip-model] [--skip-tunnel] [--cf-client-id ID --cf-client-secret S]`
  - `download-model` (fetch the Whisper model into the cache)
  - `encode-m4a SRC DST` (dev helper)
- `doctor` prints `[PASS]/[WARN]/[FAIL]/[SKIP] name — detail` + `fix:` hint per check,
  exit 1 on any FAIL: config+token, vocab, CUDA DLLs + model load, Ollama reachable + model
  pulled (+ on GPU), firewall rule present + no python block rules, network profile
  Private, hub /health on loopback and LAN URL, cloudflared service running, public
  `https://<public_hostname>/health` (with optional CF service-token headers).
  `public_hostname` comes from `install-state.json` or `--url`.

## 4. API contract

`GET /health` → `200 {"ok": true}` (no auth; source-IP rule still applies).

`POST /transcribe` — either a multipart form: `file` (audio; m4a/AAC from iOS), `target`
(`pc`|`phone`, case/whitespace-insensitive), **or** the raw audio as the request body
(`Content-Type: audio/*`, `application/octet-stream` or none) with `?target=pc|phone` in the
query string — the iOS Shortcut uses the raw form, because iOS silently dropped the file field of
a generated Form body. A missing/empty target means `pc`. Non-audio raw bodies get 415. Headers: `Authorization: Bearer <hub token>` always; tunnel
route also carries `Cf-Access-Jwt-Assertion` (added by Cloudflare Access).

`200`:
```json
{"text": "...", "raw_text": "...", "target": "pc", "pasted": true, "warning": null,
 "route": "lan", "cleanup": "applied",
 "timings_ms": {"decode": 0, "stt": 0, "cleanup": 0, "paste": 0, "total": 0}}
```
`cleanup` ∈ applied | fallback | disabled | skipped. Paste failure is still 200 with
`pasted:false` + `warning`.

Errors (JSON `{"detail": "..."}`): 400 bad multipart / missing file · 401 bad/missing bearer
(`WWW-Authenticate: Bearer`) · 403 non-private source or invalid/missing Access JWT on the
tunnel route · 413 upload > `max_upload_mb` or audio > `max_audio_seconds` · 415 undecodable
audio · 422 bad `target` · 429 rate limited / too many auth failures · 503 engine still
loading or failed (detail says why) · 500 unexpected.

## 5. Route and auth rules

| Source address (TCP peer) | Route | Requirements on /transcribe |
|---|---|---|
| loopback 127.0.0.0/8, ::1 | `tunnel` (cloudflared) | bearer; plus valid Access JWT **if** `[access]` configured (else allowed + loud startup warning) |
| in `server.lan_networks` (private only) | `lan` | bearer; JWT not expected |
| anything else | — | 403 on every path, incl. /health |

Headers like X-Forwarded-For / Cf-Connecting-IP are never trusted for routing. Order:
source IP → rate limit (per route+IP) → auth-failure lockout → bearer → JWT → readiness →
size → parse. Auth happens before the body is read. Secrets are never logged.

## 6. Logging, history, files

`%APPDATA%\shoutabout\`: `config.toml`, `vocab.txt`, `hub_token.txt`, `history.jsonl`,
`audio/` (only if keep_audio), `logs/hub.log`, `install-state.json` (written by setup.ps1:
repo path, task name, firewall rule name, public hostname, tunnel mode/name).
Request log line: `transcribe id=… route=lan target=pc chars=123 pasted=true cleanup=applied
audio_s=4.2 decode=12 stt=210 cleanup=180 paste=40 total=455`.

### 6.1 Names shared by scripts (E) and doctor/CLI (D)

- Scheduled task: `ShoutAbout Hub` (folder `\`), action
  `<repo>\.venv\Scripts\pythonw.exe -m shoutabout run`, start-in `<repo>`, AtLogOn for the
  installing user, LogonType Interactive, RunLevel Limited.
- Firewall rule: Name `ShoutAbout-Hub-TCP-<port>`, DisplayName `ShoutAbout Hub (TCP <port>)`,
  Group `ShoutAbout`, Inbound, Allow, TCP, LocalPort `<port>`, Profile Private,
  RemoteAddress LocalSubnet. Plus program+port rules `ShoutAbout-Hub-TCP-<port>-python` /
  `-pythonw` for the BASE interpreter (`sys._base_executable` dir; venv python is a
  redirector) so Windows doesn't show the "allow Python" prompt. Only the firewall step is
  elevated; the scheduled task is registered non-elevated (user can do it for themselves).
- `install-state.json` (in the config dir, written/merged by setup scripts, read by doctor
  and uninstall):
  ```json
  {"repo": "C:\\...\\shoutabout", "port": 8765, "task_name": "ShoutAbout Hub",
   "firewall_rule_name": "ShoutAbout-Hub-TCP-8765",
   "public_hostname": "dictate.shoutabout.fyi",
   "tunnel_mode": "existing-remote | remote | local | none",
   "tunnel_name": "shoutabout",
   "created": {"tunnel": false, "dns_route": false, "cloudflared_service": false,
               "cloudflared_config": false},
   "updated": "2026-10-09T12:00:00-07:00"}
  ```
- Test audio (`scripts/make_test_audio.ps1` → `test-audio/`): `<name>.wav` + `<name>.m4a`
  and `test-audio/manifest.json`: `[{"file": "vocab-01.m4a", "text": "<spoken sentence>",
  "kind": "vocab|instruction|filler|plain", "expect_terms": ["Better Shell"]}]`.
  `python -m shoutabout transcribe test-audio\*.m4a` shows expected text, word accuracy and
  vocab-term hits when a manifest entry exists for a file.
- CLI exit codes: 0 ok, 1 failure, 2 usage error. `info --json` keys: `home`, `config_file`,
  `vocab_file`, `token_file`, `log_dir`, `history_file`, `port`, `host`, `lan_ip`, `lan_url`,
  `stt_model`, `stt_device`, `cleanup_enabled`, `cleanup_model`, `ollama_url`,
  `access_enabled`, `public_hostname`.

## 7. Testing

Unit tests (Linux/Windows, no GPU): config, text, vocab (+hot reload), prompt budget, guard
(incl. instruction-looking outputs), cleaner with httpx MockTransport, security (bearer, JWT
with a generated RSA key + fake JWKS client, classify_source incl. public IPs), rate limiter,
server contract via `httpx.AsyncClient(transport=httpx.ASGITransport(app, client=(ip, port)))`
or `TestClient(app, client=(ip, port))`, pipeline with fakes, history schema, audio decode of a
generated WAV/m4a. Opt-in: `--run-gpu`, `--run-ollama`, `--run-windows`.

## 8. Decisions settled during the build (measured on the target PC)
- Cleanup default model: `qwen3:4b-instruct-2507-q4_K_M` (Apache-2.0, non-thinking). Live
  benchmark (11 samples × 3): median 90 ms / p90 116 ms, 100% guard-accepted, 0 answer
  leaks, 3.0 GiB VRAM, fixed every vocab term. `granite4:micro` was 83 ms but missed
  "Claude Code".
- STT vocab hints: `prompt_mode = "both"` with the budget given to hotwords first and the
  top terms repeated as initial_prompt in the leftover budget (total ≤ max_prompt_tokens);
  text format `In this dictation I mention A, B, C.` — best of a 12-sentence × 2-voice
  experiment (95% term hits, WER 1.4 vs 43% / 12.8 with no hints); never leaked on silence.
- CUDA: only the cuBLAS 12 wheel is needed (no cuDNN, no toolkit); DLL dir on PATH too.
- Default port stays 8765 (brief), but a PC may already have another app on 127.0.0.1:8765,
  so setup detects conflicts and `init --port N` rewrites `[server] port`.
- uvicorn `proxy_headers=False`: otherwise X-Forwarded-For from loopback (cloudflared)
  would rewrite the peer address and could fake the "lan" route.
- Tunnel-route rate-limit buckets are keyed by `Cf-Connecting-IP` (set by Cloudflare) so one
  remote client can't lock out the owner; routing/auth never use that header.
