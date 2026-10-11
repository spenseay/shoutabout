# Architecture

This is a short map of the v1 hub. The binding interface contract, with signatures, is in
[PLAN.md](../PLAN.md).

## Components

```
iPhone Shortcut --LAN HTTP--------------------------------------------> hub 0.0.0.0:<port>  (route "lan")
iPhone Shortcut --HTTPS--> Cloudflare Access --> cloudflared service --> hub via 127.0.0.1 (route "tunnel")

<port> = [server] port: 8765 by default, or what setup.ps1 -Port set (`shoutabout info` shows it)

hub (pythonw.exe -m shoutabout run, logon task, user session, not elevated)
 |- tray (main thread) ---- HubState (status, paused) <---- pipeline
 |- server thread: FastAPI --> Pipeline --> engine (Whisper, CUDA)
 |                                     |--> cleaner (Ollama HTTP, 127.0.0.1:11434) + guard
 |                                     |--> injector (clipboard + SendInput) --> focused window
 |                                     '--> HistoryLog (history.jsonl)
 |- engine loader thread (loads Whisper, then publishes ctx.pipeline)
 '- warmer thread (keeps the Ollama model loaded)
```

## Module map (`src/shoutabout/`)

| Module | Responsibility |
|---|---|
| `cli.py`, `__main__.py` | Subcommands (`run`, `init`, `info`, `token`, `qr`, `transcribe`, `benchmark`, `doctor`, `make-shortcuts`, `download-model`, `encode-m4a`). Imports are lazy, so quick commands start fast. |
| `hub.py` | `run_hub`: single-instance mutex, logging, startup warnings, and the four threads below. |
| `server.py` | `create_app(HubContext)`: the source-IP middleware (pure ASGI), `/health`, `/transcribe`, the order of checks, size caps, and the error-to-status mapping. OpenAPI and docs pages are disabled. |
| `security.py` | `classify_source` (peer IP to route), `bearer_ok` (constant-time compare), `AccessJwtValidator` (RS256 with a JWKS cache), `RateLimiter` (sliding window). |
| `pipeline.py` | `Pipeline.process_file`, serialised by a lock: decode, STT, normalise, cleanup, normalise, deliver, history. Sets the hub status. |
| `audio.py` | The decoder chain (see [Audio decoding](#audio-decoding)): container sniffing, WAV -> Media Foundation -> PyAV, mono + 16 kHz, duration checks, error types; `encode_m4a` for tests (PyAV only). |
| `wavdecode.py`, `resample.py` | Uncompressed WAV (PCM 8/16/24/32-bit, float, extensible, RF64) read with numpy; the mono downmix and the polyphase Kaiser-windowed-sinc resampler. |
| `mfdecode.py` | Windows Media Foundation through plain ctypes COM calls: byte stream -> source resolver -> source reader -> float PCM. COM per call, `MFStartup` once per process; plain-words errors for Windows "N" editions. |
| `pyavdecode.py`, `avshim.py` | The optional PyAV/FFmpeg decoder (source checkouts only, never in the installed app); the placeholder `av` module that lets faster-whisper import without PyAV. |
| `vocab.py` | `vocab.txt` parsing, mtime-based hot reload, building the prompt/hotword hints within a token budget. |
| `engine/base.py`, `engine/__init__.py` | The `SpeechEngine` protocol and `create_engine` factory. |
| `engine/faster_whisper_engine.py` | `WhisperModel` on CUDA (CPU fallback optional), warm-up, transcription with hints. |
| `engine/cuda_dlls.py` | Puts the pip-installed `nvidia\*\bin` folders on the DLL search path and on `PATH`. |
| `cleanup.py` | `OllamaCleaner` (chat call, warm-up, model status), the prompt builder, and `guard()`. |
| `benchmark.py` | LLM and STT benchmarks for the CLI. |
| `inject/base.py`, `inject/__init__.py` | The `Injector` and `Notifier` protocols, `NullInjector`, and the `create_injector` factory. |
| `inject/windows.py`, `inject/clipboard.py` | Foreground and integrity checks, clipboard snapshot/set/restore with exclusion formats, `SendInput` paste chord. |
| `tray.py`, `icons.py`, `brand.py` | ctypes `Shell_NotifyIcon` tray icon, menu and balloon toasts. Icons are the brand mouth rendered in pure Python (`brand.py`): one per state, plus a talking animation while transcribing ([brand.md](brand.md)). |
| `hardware.py`, `modelpick.py`, `modelstore.py` | v2 automatic model choice: NVIDIA GPU / CPU / RAM detection (NVML, nvidia-smi, CTranslate2), the best / balanced / light tiers with config.toml overrides, and the resumable model download with byte progress ([models.md](models.md)). |
| `local_dictation.py`, `hotkey.py`, `mic.py`, `overlay.py` | v2 "Talk to this PC": the observe-only `WH_KEYBOARD_LL` hotkey listener and its state machine, the microphone recorder (sounddevice), the mouth-pill layered window, and the controller that feeds `Pipeline.process_audio` ([local-dictation.md](local-dictation.md)). |
| `voice_runtime.py` | What `hub.py` calls for the above: choose the models at start, download in the engine thread (progress in `HubState`), turn "auto" cleanup on when Ollama appears, start/stop/reload "Talk to this PC". |
| `history.py` | `HistoryLog.append`, which is thread-safe and never raises; `read_history` and `clear` for the History window ([history.md](history.md)). |
| `failed_audio.py` | Recordings whose transcription failed, kept (5 newest, 120 MB) for History > Try again while history is on; it's off by default ([history.md](history.md)). |
| `history_model.py` / `history_ui.py` | The History window: logic (Tk-free, tested) and its Tk view on the UI thread. |
| `migrations.py` | One-time upgrades of config values an older version generated (toggle mode and the 600 s limit). |
| `doctor.py` | Setup checks, printed as `[PASS]`/`[WARN]`/`[FAIL]`/`[SKIP]` with fix hints. |
| `shortcuts.py` | The iPhone shortcuts (Dictate, Dictation Target) as plists, with the secrets as import questions; signing through RoutineHub's HubSign (setup time only). |
| `config.py` | Paths, the TOML schema (pydantic, unknown keys rejected), token generation and loading, `ensure_initialized`. |
| `config_edit.py` | Comment-preserving edits to config.toml (tomlkit), validated against the schema and written atomically. Used by the setup and settings windows. |
| `discovery.py` | DNS-SD announcement of `_shoutabout._tcp` (TXT `v`, `id`, `name`, `ver`) with `DnsServiceRegister` / `DnsServiceDeRegister` through ctypes ([protocol.md](protocol.md)). |
| `pair_requests.py`, `devices.py`, `identity.py` | Pairing requests (single pending, codes, expiry, rate limits, approval only from the PC UI); per-device tokens stored as SHA-256 in devices.json; the installation id and friendly name. |
| `hub_v2.py`, `app_services.py` | The hub's v2 wiring (pairing, discovery, windows, hot reload) and the bundle of services the windows use; `# v2-voice:` marks the hooks into hardware detection, model pick and download, and hotkey dictation. |
| `setup_flow.py` / `setup_ui.py`, `settings_model.py` / `settings_ui.py`, `approval_ui.py`, `ui_host.py`, `ui_kit.py` | First-run setup, settings, History and the pairing prompt: logic (Tk-free, tested) and Tk views on one UI thread ([setup-experience.md](setup-experience.md)). |
| `firstrun.py`, `winstartup.py` | First-run detection (existing installs never see the wizard) and port choice; Start with Windows (per-user Run key, installed app only). |
| `models.py`, `state.py`, `text.py`, `netinfo.py`, `logging_setup.py` | Result types; thread-safe `HubState`; whitespace normalisation; LAN IP and Wi-Fi name detection; rotating log and pythonw stream redirection. |
| `mac/` | v2-macos: the macOS layer (menu bar, CGEventTap hotkey, NSPanel overlay, NSPasteboard paste, permissions, the Tk window process and its link, dns-sd, LaunchAgent, afconvert, Apple Silicon facts). Hooks in the shared modules are marked `# v2-macos`. See [macos.md](macos.md). |
| `frozen.py`, `selftest.py` | The installed app's entry point (`ShoutAbout.exe`: no arguments runs the hub with the tray, `--self-test` checks the bundle, anything else is the CLI) and frozen-path helpers; the self-test's checks and JSON report. Packaging itself lives in `packaging/` ([installer.md](installer.md)). |

## Threads

| Thread | Runs | Notes |
|---|---|---|
| main | Tray message loop (`TrayIcon.run`), or a wait on the server thread with `--no-tray` | It must be the main thread for Win32 message handling. Quit stops uvicorn and the loop. |
| `server` | `uvicorn.Server.run()` on `host:port` | `proxy_headers=False`, no access log, `lifespan=off`. Request handlers hand blocking work (JWT JWKS fetch, file save, pipeline) to the thread pool. |
| `engine` | downloads the chosen model if it isn't cached yet (progress in `HubState.progress`), `create_engine` then `load()` (with warm-up), then sets `ctx.pipeline` | Until it finishes, `/transcribe` returns 503 `hub is starting`. On failure, `ctx.startup_error` is set and the hub goes to ERROR with a toast. |
| `warmer` | `OllamaCleaner.warm()` every `warm_interval_s` | Reloads the model after an Ollama restart and primes the prompt cache. Never raises. With `[cleanup] enabled = "auto"` it looks for Ollama every 30 s until a suitable model shows up. |
| `hotkey-hook`, `hotkey`, `overlay`, `overlay-warm`, `mic-warm`, `dictation-transcribe` | "Talk to this PC" ([local-dictation.md](local-dictation.md)) | The hook thread only queues key events; the worker runs the state machine; transcriptions take `HubState.busy` like phone requests. Stopped on Pause and Quit. |
| `discovery` | Waits until uvicorn listens, then announces the hub with DNS-SD | Windows calls the registration callback on its own thread pool. |
| `ui` | Tk: the setup and settings windows and pairing prompts (`ui_host`) | Started on demand, ends when the last window closes. Workers never hold Tk objects. |
| worker pool | `Pipeline.process_file` | The pipeline lock allows only one transcription at a time. A paste schedules a clipboard restore on a timer. |

## Request lifecycle (`POST /transcribe`)

1. **Source-IP middleware** (every path, including `/health`) classifies the TCP peer as
   `tunnel`, `lan` or reject. A rejection is `403 {"detail":"forbidden"}`.
2. **Rate limit** per client key: `rate_limit_per_minute`. Over it, the hub returns `429`. The
   key is `(route, peer ip)`. On the `tunnel` route the peer is always cloudflared on
   `127.0.0.1`, so if the request carries a `Cf-Connecting-IP` header that parses as an IP
   address (Cloudflare sets it to the remote client's IP), that address is added to the key.
   One remote client then can't exhaust the limits for everyone else, the owner included.
3. **Auth-failure lockout**: at `max_auth_failures` failures in 10 minutes per client key (as
   above), the hub returns `429`.
4. **Bearer token**: missing or wrong returns `401` with `WWW-Authenticate: Bearer`, and counts as
   a failure.
5. **Access JWT** (route `tunnel` with `[access]` configured only): a missing or invalid
   `Cf-Access-Jwt-Assertion` returns `403` and counts as a failure.
6. **Readiness**: if the pipeline isn't loaded yet, `503` with the startup reason.
7. **Size precheck**: `Content-Length` larger than `max_upload_mb` plus multipart slack returns
   `413`.
8. **Body**, with a hard byte cap (it also covers chunked uploads). `target` that isn't
   `pc`/`phone` returns `422`; missing or empty means `pc`.
   - **Multipart** (`multipart/form-data`, or `application/x-www-form-urlencoded`): expects 1
     file and at most 4 fields; `file` missing returns `400` and logs the form's field names and
     types (never values). `target` is a form field.
   - **Raw body** (any other content type): the body is the audio itself, as iOS Shortcuts
     sends it with Request Body: File. Only `audio/*`, `video/*`, `application/octet-stream` or
     no content type are accepted, otherwise `415`; an empty body returns `400`. `target` comes
     from the query string (`?target=phone`).
   - the upload is saved to a temp file
9. **Pipeline**, in the worker pool, under the lock:
   - decode: `AudioTooLongError` gives `413`, `DecoderUnavailableError` (no Media
     Foundation on this PC) gives `503` with the fix in the detail, any other `AudioError`
     gives `415`
   - STT with vocab hints: `EngineError` gives `503`
   - `normalize_whitespace`
   - cleanup (never raises; sets status `applied`/`fallback`/`disabled`/`skipped`)
   - `normalize_whitespace`
   - deliver: for `target=pc`, when not paused and the text is non-empty, call
     `injector.paste`. An empty transcript gets the warning `No speech detected.` on either
     target.
   - history append
10. The temp file is deleted, one `transcribe id=...` log line is written (no text), and the JSON
    response is returned.

Authentication finishes before the body is read. Forwarding headers are never used for routing
or auth: the route comes from the TCP peer alone, `X-Forwarded-For` is ignored, and uvicorn runs
with `proxy_headers=False`. The only use of `Cf-Connecting-IP` is the tunnel-route bucketing in
steps 2–3. A local process could send a different value on each request to dodge the lockout,
but every attempt still needs the 256-bit hub token (and the Access JWT, if configured).

## Audio decoding

The installed app ships no FFmpeg (PyAV's wheels bundle x264/x265, which are GPL; see
[licenses.md](licenses.md)). `audio.decode_audio(path, max_seconds)` returns 16 kHz mono
float32 in [-1, 1] and tries, in order:

1. **WAV** (`wavdecode.py`): PCM 8/16/24/32-bit and float 32/64-bit, also
   `WAVE_FORMAT_EXTENSIBLE` and RF64, read with numpy. The duration is checked from the header
   before any sample is read. Compressed WAV (ADPCM, mu-law, ...) goes on to the next decoder.
2. **Windows Media Foundation** (`mfdecode.py`, Windows only): ctypes COM calls, no
   comtypes/pywin32. The file (or bytes) becomes an `IMFByteStream`, the source resolver opens
   it as an `IMFMediaSource` (hinted by the sniffed container, never the client's file name),
   and an `IMFSourceReader` on the first audio stream returns 32-bit float PCM at the file's
   own rate and channel count. The declared duration (`MF_PD_DURATION`) is checked first, and
   reading stops as soon as the decoded length passes the limit. A mid-stream format change
   (e.g. an AAC decoder discovering SBR) starts a new segment that is resampled separately.
   Each call initialises COM on its thread (multithreaded apartment, or the thread's existing
   single-threaded one) and undoes exactly that; `MFStartup` runs once per process; every
   object is released and the file is closed before the call returns, so the hub can delete
   the upload at once.
3. **PyAV** (`pyavdecode.py`), only when installed: in source checkouts it is the fallback
   after Media Foundation and the decoder on Linux (CI). The installed app never has it.

Containers are sniffed from the first bytes: MP4/M4A/MOV/3GP, MP3 (also after an ID3 tag),
ADTS AAC, WAV, FLAC, WMA/ASF, Matroska/WebM and AMR are handed to Media Foundation. Ogg, AIFF,
CAF and unknown bytes never are; the installed app answers them with `415` and "supported:
m4a/AAC, MP3, WAV, FLAC". (Windows 11 has no Media Foundation reader for Ogg, AIFF or CAF.
The iPhone app and Shortcuts always send AAC `.m4a`.)

**Mono + 16 kHz** (`resample.py`): channels are averaged, then a rational polyphase resampler
(`up/down` after the gcd, e.g. 160/441 for 44.1 kHz) with a Kaiser-windowed sinc low-pass
(16 zero crossings, beta 8.6, cut-off at 95 % of the lower Nyquist frequency): flat to 6 kHz,
more than 70 dB down above 9 kHz, no delay. 25 s at 44.1 kHz takes about 5 ms.

**Errors:** `AudioTooLongError` (413), `DecoderUnavailableError` when Media Foundation is
missing, as on Windows "N" editions without the Media Feature Pack (503; the message says how
to install it), any other `AudioError` (415). `doctor` ("Audio decoding") and the self-test
(`audio`) decode a bundled 1 s AAC tone (`data/selftest.m4a`) the same way.

**faster-whisper without PyAV:** faster-whisper imports `av` at import time for its own file
decoder, which ShoutAbout never calls (the engine always gets samples).
`avshim.ensure_av_importable()` runs before faster-whisper is imported (engine, self-test) and,
only if `import av` fails, installs a placeholder module that raises a plain
`PyAVUnavailableError` as soon as anything uses it.

**Equivalence** (checked with `scripts/compare_decoders.py` on the 29 test clips, AAC 44.1 kHz
mono): the same length as PyAV (0.00 %), zero lag, level within 0.001 dB on speech,
correlation at least 0.9998 on speech, 35-74 dB signal-to-difference, and 28 of 29 identical
Whisper transcripts (the other differs only in commas). MP3 differs more: Media Foundation
keeps the encoder delay and padding that FFmpeg trims, so files are about 57 ms longer (36 ms
at the start). Whisper is as sensitive to harmless changes: 36 ms of extra leading silence on
the same PyAV decode changed 16 of 87 transcripts, and -70 dB of noise changed 8.

## The phone protocol (v2)

`GET /hello`, `POST /pair/request` and `GET /pair/status` need no token but answer only on the
`lan` route (404 through the tunnel and from loopback). `GET /info` and `POST /paste` go
through the same steps 1-5 as `/transcribe`. A bearer token is the hub token or a paired
phone's token, hashed once and compared in constant time with all of them. Full contract,
examples and limits: [protocol.md](protocol.md).

## Route and auth rules

| Source address (TCP peer) | Route | `/health` | `/transcribe` requires |
|---|---|---|---|
| Loopback `127.0.0.0/8`, `::1`, `::ffff:127.x` | `tunnel` (cloudflared) | 200 | Bearer token, **plus** a valid Access JWT if `[access]` is configured. If it isn't, requests are accepted and a loud warning is logged at startup. |
| Inside `[server] lan_networks` (private ranges only) | `lan` | 200 | Bearer token |
| Anything else (public IPs, malformed or missing peer) | none | **403** | Always **403** |

Access JWT checks:
- algorithm RS256
- signature verified against `https://<team_domain>/cdn-cgi/access/certs`, cached for an hour and
  refreshed on an unknown `kid`
- `aud` must be one of `[access] aud`
- `iss` must be exactly `https://<team_domain>`
- `exp` and `iat` are required, with 60 s leeway
- `common_name` must be in `allowed_client_ids` when that list is non-empty

## Response contract

```json
{"text": "...", "raw_text": "...", "target": "pc", "pasted": true, "warning": null,
 "status": "ok", "route": "lan", "cleanup": "applied",
 "timings_ms": {"decode": 0, "stt": 0, "cleanup": 0, "paste": 0, "total": 0}}
```

- `status` is `"ok"` when `warning` is `null` and `"warning"` otherwise: an exact string the
  iPhone Shortcut compares against, because "has any value" is true even for empty values.
- Errors are returned as `{"detail": "..."}`, without `status`.
- A failed paste is still `200`, with `pasted: false` and a `warning`.

## Failure modes

| Failure | Behaviour | User sees |
|---|---|---|
| CUDA / cuBLAS can't load | Engine thread fails. If `cpu_fallback` is set, it runs on the CPU with a warning instead. | Tray goes to ERROR with a toast. `/transcribe` returns `503` with the reason. `doctor` FAIL with a fix hint. |
| Model not cached and no network | Engine load fails | Same as above |
| Port already in use | uvicorn can't bind | Tray goes to ERROR with "Couldn't listen on port <port> — is another copy running?". The task exits with code 1. Fix: stop the other program, or re-run setup with `-Port`. |
| Second hub started | The mutex is already held | The second process logs and exits with code 1 |
| Config invalid / token missing | The hub doesn't start | `hub.log`: `can't start: ...` |
| Ollama down, model missing, timeout (2 s), guard rejection, output truncated | Raw transcript used; `cleanup: "fallback"` and the reason are logged and stored in history | The text arrives uncleaned |
| Upload too large, audio too long, undecodable | Request rejected before the GPU runs | `413` / `415` |
| PC locked, or the UAC secure desktop showing (the input desktop isn't "Default") | No paste; the text is left on the PC clipboard | `pasted: false` and "The PC is locked — text is on your clipboard." |
| No foreground window | No paste; the text is left on the PC clipboard | `pasted: false` and "No focused window on the PC (locked?) — text is on your clipboard." |
| Foreground window elevated (higher integrity) | No keystrokes. The clipboard holds the text and is not restored. | A toast plus the warning "Couldn't paste into an admin window — text is on your clipboard." |
| Clipboard changed before the restore | The restore is skipped (sequence number check) | The newer clipboard content is kept |
| Hub paused | Transcribes but doesn't paste | The phone gets the text and "Hub is paused — not pasted on the PC." |
| Empty transcript (silence) | Nothing pasted (either target) | `text: ""` and "No speech detected." |
| History write fails | Logged; the request still succeeds | Nothing |
| Tunnel down / route wrong | The request never reaches the hub | Cloudflare `502`/`530`/`1033` |
| Request takes over 125 s through the tunnel | Cloudflare times out; the hub still finishes and pastes | Cloudflare `524` |
| PC asleep | Nothing answers | A Shortcut connection error or timeout |
