<p align="center"><img src="brand/talk/app-icon-talk-256.png" width="160" alt="ShoutAbout: an open yellow mouth that talks"></p>

# ShoutAbout

**Free, private voice dictation for Windows, Mac and iPhone.** Talk, and clean text lands
where you're working. Your voice never leaves your own devices: speech recognition runs on your
computer (Whisper, with a model picked automatically for your hardware) or on your iPhone. No
account, no cloud.

- **Talk to your computer:** tap Right Ctrl (Right Command on a Mac), talk, tap again. The text
  is pasted into whatever you're typing in.
- **Use your iPhone as the microphone** for a computer that doesn't have one: press the Action
  Button, talk, and the text lands on your computer over your home Wi-Fi.
- **Or keep it on the phone:** the iPhone transcribes by itself and copies the text.

Download and help: **[shoutabout.fyi](https://shoutabout.fyi)**. Open source under the
[MIT License](LICENSE).

- Download for Windows: [below](#download-windows); how the installer is built, tested and
  released: [docs/installer.md](docs/installer.md)
- The website (`site/`, static, on Cloudflare Pages) and the go-live plan:
  [docs/launch/LAUNCH.md](docs/launch/LAUNCH.md), hosting: [docs/launch/hosting.md](docs/launch/hosting.md)
- Getting started (installer, about a minute): [below](#getting-started)
- The setup and settings windows: [docs/setup-experience.md](docs/setup-experience.md)
- The phone protocol (discovery, pairing, device tokens): [docs/protocol.md](docs/protocol.md)
- Install from source (developers): [SETUP.md](SETUP.md)
- iPhone Shortcuts: [docs/ios-shortcuts.md](docs/ios-shortcuts.md)
- Native iPhone app (SwiftUI, built and shipped to TestFlight by CI): [docs/ios-app.md](docs/ios-app.md)
- Building the iPhone and Mac apps on Codemagic (TestFlight, notarized Mac .dmg): [docs/release/codemagic.md](docs/release/codemagic.md)
- Pairing the iPhone app: [docs/pairing.md](docs/pairing.md)
- Brand (mark, palette, tray states, icons): [docs/brand.md](docs/brand.md)
- Cloudflare Tunnel + Access: [docs/cloudflare.md](docs/cloudflare.md)
- Internals: [docs/architecture.md](docs/architecture.md)
- Problems: [docs/troubleshooting.md](docs/troubleshooting.md)
- Owner test list: [docs/manual-test-checklist.md](docs/manual-test-checklist.md)
- History file: [docs/history.md](docs/history.md)
- Other transports (LAN-only, Tailscale, product ideas): [docs/networking-alternatives.md](docs/networking-alternatives.md)
- Talk to this PC (hotkey dictation, no phone): [docs/local-dictation.md](docs/local-dictation.md)
- Automatic model choice, tiers and benchmarks: [docs/models.md](docs/models.md)
- Third-party licenses: [docs/licenses.md](docs/licenses.md)
- Design contract: [PLAN.md](PLAN.md)

## Download (Windows)

1. Download **`ShoutAbout-Setup.exe`** from [shoutabout.fyi/download](https://shoutabout.fyi/download)
   (it's the latest [release](https://github.com/spenseay/shoutabout/releases/latest)).
2. Double-click it, click **Yes** on the Windows permission prompt, then **Next** and
   **Finish**. ShoutAbout starts next to the clock.
3. The first releases aren't code-signed, so Windows may say "Windows protected your PC".
   Click **More info**, then **Run anyway**.

Windows 10 (1809 or later) or 11, 64-bit. An NVIDIA GPU makes transcription fast, but it isn't
needed. To remove ShoutAbout, use **Settings > Apps**. Your settings, vocabulary and history are
kept unless you tick the box to remove them. Details, including what the installer changes on
your PC: [docs/installer.md](docs/installer.md).

## Mac (Apple Silicon, macOS 14+)

A menu bar app with the same features (tap Right ⌘ to talk, the iPhone pairs with it the same
way). Build it on the Mac with one command, no prior setup:
`bash packaging/macos/build.sh` (it installs ShoutAbout in Applications and opens it). Plain
steps: [docs/mac-quickstart.md](docs/mac-quickstart.md); details:
[docs/macos.md](docs/macos.md). A signed, notarized `ShoutAbout.dmg` for any Mac (nothing to
build there) comes from Codemagic: [docs/release/codemagic.md](docs/release/codemagic.md).

---

## Getting started

With the Windows installer there is nothing to configure by hand:

1. **Run the installer.** ShoutAbout starts and a yellow window says **Let's get you
   talking**. It looks at your PC (for example "Your NVIDIA GeForce RTX 4080 SUPER can run
   our most accurate model.") and downloads the speech model with a progress bar.
2. **How will you use ShoutAbout?** Leave **Talk to this PC** on to dictate with a key: press
   Right Ctrl, talk, press it again. Turn on **Talk from my iPhone**, open the ShoutAbout app on the
   phone (same Wi-Fi): it finds the PC by itself. The PC asks "Sam's iPhone wants to dictate
   to this PC. Code 4821"; if the phone shows the same code, click **Allow**.
3. **Try it.** Press Right Ctrl, say "This is my first dictation.", press it again. It appears
   in the box.
   **You're all set!** ShoutAbout lives in your system tray.

Later: tray > **Settings…** (hotkey, phones, quality, AI clean-up, history (off by default),
the PC's name, start with Windows) and tray > **Set up…** to run the steps again. Details:
[docs/setup-experience.md](docs/setup-experience.md). Everything stays on your devices: no
accounts, no cloud speech or AI.

Running from source (the v1 way, with the setup script, Cloudflare and iPhone Shortcuts) is
still supported: see [Quick start](#quick-start) and [SETUP.md](SETUP.md).

## How it works

```
 iPhone                                             Windows 11 PC
 ------                                             -------------
 Action Button -> "Dictate" Shortcut
   Record Audio ("Normal" -> .m4a / AAC)
   read iCloud Drive/Shortcuts/dictation-target.txt: pc | phone
   Wi-Fi name == home network?
     yes: POST http://<lan-ip>:8765/transcribe?target=pc|phone -----------------+
          Authorization: Bearer <hub token>                                     |
     no:  POST https://dictate.shoutabout.fyi/transcribe?target=pc|phone        |
          Authorization: Bearer <hub token>                                     |
          CF-Access-Client-Id / CF-Access-Client-Secret (service token)         |
          body: the recording itself (Request Body: File)                       |
            |                                                                   |
            v                                                                   |
     Cloudflare edge: Access policy (Service Auth)                              |
       adds Cf-Access-Jwt-Assertion                                             |
            |                                                                   |
            v                                                                   v
     cloudflared (Windows service) --> http://127.0.0.1:8765        0.0.0.0:8765 ShoutAbout hub
                                       (peer = loopback -> route "tunnel")  (peer = LAN -> route "lan")
   <-- JSON {"text", "raw_text", "pasted", "warning", "status", "route", "cleanup", "timings_ms"}
   Copy to Clipboard; a notification only if status isn't "ok" (success is silent)
```

The hub is a single process that starts at logon:

```
pythonw -m shoutabout run   (Task Scheduler "at logon", interactive user session, not elevated)
 |- main thread:    Win32 tray message loop (status icon, menu, balloon toasts)
 |- thread server:  uvicorn on 0.0.0.0:8765 (FastAPI: GET /health, POST /transcribe)
 |- thread engine:  loads Whisper on CUDA and warms it up, then enables the pipeline
 '- thread warmer:  keeps the Ollama cleanup model loaded (every warm_interval_s)
```

Request flow for `POST /transcribe`:

```
source-IP check (every path) -> rate limit -> auth-failure lockout -> bearer token
 -> Access JWT (tunnel route, if configured) -> engine ready? -> size limits
 -> multipart parse, or the raw audio body -> temp file
 -> [one request at a time]
      decode        WAV: numpy; AAC/MP3/FLAC: Windows Media Foundation (no FFmpeg)
                    -> 16 kHz mono float32; duration limit enforced
      STT           faster-whisper large-v3-turbo, CUDA fp16, VAD, vocab hints
      normalise     all whitespace -> single spaces (one paragraph, never a newline)
      cleanup       Ollama + safety guard; raw transcript on timeout/error/rejection
      deliver       target=pc and not paused: clipboard + Ctrl+V, clipboard restored
      history       append one line to history.jsonl
 -> JSON response
```

## Features

- **Talk to this PC.** Press Right Ctrl, speak, press it again: the text is pasted into the
  window you're typing in, no phone needed (Right Ctrl + C and other shortcuts still work as
  always; "Hold to talk" is an option). A small animated mouth pops up at the top of the screen
  the instant it's listening; Esc cancels. Same vocabulary, cleanup, paste rules and history
  as phone dictation ([docs/local-dictation.md](docs/local-dictation.md)).
- **Automatic model choice.** The PC detects its graphics card, processor and memory and picks
  the speech model, device and precision itself ("Best quality", "Balanced" or "Light"),
  downloads it on first run with progress (resumable), and turns the LLM cleanup on only when
  it can afford it and a suitable Ollama model is already installed. Explicit config values
  still win ([docs/models.md](docs/models.md)).
- **One-press capture.** The Action Button (or a Control Center control) runs a Shortcut that
  records audio and posts it to the PC.
- **Two modes.** In PC mode the text is pasted on the PC. In Phone mode it's copied to the
  iPhone clipboard. The **Dictation Target** Shortcut toggles the mode from Control Center, and
  the choice is kept in `iCloud Drive/Shortcuts/dictation-target.txt`, so it persists.
- **Home and away.** On the home Wi-Fi the phone talks to the PC directly over the LAN.
  Anywhere else it goes over HTTPS through Cloudflare Tunnel, with a Cloudflare Access service
  token. The Shortcut picks the route from the Wi-Fi name before it sends anything.
- **Local GPU speech-to-text.** On an NVIDIA card with 5 GB or more, faster-whisper
  large-v3-turbo runs in float16 on CUDA (smaller cards and CPU-only PCs get a lighter setup),
  with voice activity detection and English as the language. It needs only the cuBLAS pip wheel:
  no CUDA toolkit, no cuDNN, no FFmpeg. Phone recordings (AAC `.m4a`) are decoded by Windows
  Media Foundation, which is part of Windows.
- **Custom vocabulary.** `vocab.txt` holds one term per line, and the order sets priority.
  Edits reload without a restart. Terms are fed to Whisper as hotwords and an initial prompt
  within a token budget.
- **Optional LLM cleanup.** A local Ollama model fixes vocabulary spellings, punctuation and
  capitalization, and removes filler words. Nothing else is allowed to change: a code-level
  guard falls back to the raw transcript if the model rewrote, answered or timed out (2 s).
- **Safe paste.**
  - It uses the clipboard plus Ctrl+V, or Shift+Insert for mintty, PuTTY and kitty.
  - Your previous clipboard is restored afterwards.
  - Transcripts are excluded from Win+V clipboard history and from cloud clipboard.
  - Enter is never pressed, and the output is always a single paragraph.
  - Admin windows are detected. The text is left on the clipboard and a toast tells you.
- **Teach it your words.**
  - Add a word from the iPhone (**Teach ShoutAbout** shortcut), the tray (**Teach ShoutAbout…**)
    or `vocab add`.
  - ShoutAbout also suggests names from your project folders, and from your dictations while
    history is on. This is all local, and nothing is added without your OK
    ([docs/vocab.md](docs/vocab.md)).
- **Tray icon.** The brand mouth shows the status:
  - grey and humming while starting;
  - ajar when idle;
  - talking while transcribing;
  - shut when paused;
  - red on error.

  The menu is short: the status ("Ready", "Getting ready… 42%", "Paused"…), how to talk
  ("Press Right Ctrl to talk, and again to stop."), Pause/Resume, History…, Teach words…,
  Settings…, Set up… and Quit.
- **History (off by default).** Turn it on in Settings (**Keep a history of what I dictate**)
  or in the History window. Then everything you dictate (from this PC or a phone) is kept on
  this PC and listed in tray > **History…**, newest first, with Copy and search, so a long
  dictation is never lost. If transcribing a recording fails, the recording is kept too, with
  **Try again** (only while history is on). **Clear history** deletes it all
  ([docs/history.md](docs/history.md)).
- **Pairing without typing.** The PC announces itself on the Wi-Fi (DNS-SD
  `_shoutabout._tcp`). The iPhone app finds it, asks to pair, and the PC shows a prompt with
  a 4-digit code. Each phone gets its own token (stored hashed) that Settings can revoke
  ([docs/protocol.md](docs/protocol.md)).
- **Toasts** use the tray's own balloon notifications, so there are no extra dependencies.
- **History.** With history on, each request writes a `history.jsonl` line with the raw and
  final text, the cleanup outcome and timings ([docs/history.md](docs/history.md)).
- **CLI.**
  - `doctor` checks the whole setup end to end.
  - `transcribe` runs test files and scores them against a manifest.
  - `benchmark` measures the LLM and STT.
  - `token --qr` and `qr` move secrets to the phone without any cloud service.
  - `make-shortcuts` generates both iPhone shortcuts as signed files, ready to import.
- **Scripted setup.** A non-elevated installer, a separate elevated firewall step, Cloudflare
  route setup, an uninstaller and a test-audio generator.

## Quick start

Full instructions are in **[SETUP.md](SETUP.md)**. In outline:

```powershell
# Prerequisites:
#   - Windows 11 and a recent NVIDIA driver
#   - Python 3.12 from python.org (not the Microsoft Store build), uv and Ollama
#   - cloudflared, which is already installed on the target PC
cd "$env:USERPROFILE\Documents\shoutabout"

# 1. Install as your normal (non-admin) user. Firewall rules get their own UAC prompt.
#    This builds .venv (uv sync --locked --extra cuda), creates the config and token,
#    installs Ollama and pulls the cleanup model, downloads Whisper, registers the logon
#    task, and starts the hub.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -PublicHostname dictate.shoutabout.fyi
#    LAN-only (no Cloudflare):  ... -File .\scripts\setup.ps1 -SkipCloudflare
#    Other switches: -Port, -SkipOllama, -SkipModel, -SkipFirewall, -SkipTask, -NoStart

# 2. Check everything
.\.venv\Scripts\python.exe -m shoutabout doctor

# 3. Make the iPhone shortcuts (signed .shortcut files, no secrets inside)
.\.venv\Scripts\python.exe -m shoutabout make-shortcuts --ssid "<home-wifi-name>"

# 4. Move the hub token to the iPhone when Dictate asks for it (scan with the camera)
.\.venv\Scripts\python.exe -m shoutabout token --qr
```

Next:
1. Set up the tunnel route and the Access app ([docs/cloudflare.md](docs/cloudflare.md)).
2. Add the two shortcuts on the iPhone, answer Dictate's questions, assign Dictate to the Action
   Button, and add Dictation Target to Control Center
   ([docs/ios-shortcuts.md](docs/ios-shortcuts.md)).
3. Work through [docs/manual-test-checklist.md](docs/manual-test-checklist.md).

## Using it day to day

- **Dictate:** press the Action Button, talk, then tap stop. **Success is silent.** In PC mode
  the text is pasted into whichever PC window has focus, and it's also on the phone clipboard.
  In Phone mode it's only on the phone clipboard. A **Dictation problem** notification appears
  only when something needs a look: the hub is paused, an admin window or the lock screen had
  focus, nothing was heard, or a request failed.
- **PC or Phone mode:** run **Dictation Target** from Control Center. Its notification shows the
  new mode, and the mode persists until you change it.
- **Tray menu** (click the ShoutAbout icon on the PC):
  - **Pause / Resume** stops pasting on the PC. The text still comes back to the phone, with a
    warning.
  - **Teach words…**, **Settings…** (phones, hotkey, quality, history, the log folder, the hub
    token for Shortcuts), **Set up…**, **Quit**.
  - The icon shows the state: grey starting, green idle, blue transcribing, amber paused, red
    error.
- **Vocabulary:** add names and jargon to `vocab.txt` (tray > **Open vocab file**), one term per
  line, most important first. Saving is enough; it reloads without a restart.
- **History:** off by default. Once you turn it on (Settings, or the History window), every
  dictation is a line in `history.jsonl` ([docs/history.md](docs/history.md)).

## Repository layout

```
shoutabout/
  README.md  SETUP.md  PLAN.md  LICENSE
  pyproject.toml  uv.lock          dependencies (uv; exclude-newer date pins the resolution)
  scripts/
    setup.ps1                      main installer (runs as the user; see SETUP.md for switches)
    setup-admin.ps1                elevated step: Windows Firewall rules
    setup-cloudflare.ps1           tunnel route (existing dashboard tunnel, or CLI-managed tunnel)
    uninstall.ps1                  removes what setup created (per install-state.json)
    make_test_audio.ps1            Windows TTS -> test-audio\*.wav/.m4a + manifest.json
    compare_decoders.py            PyAV vs Media Foundation on real files (signal + transcripts)
    test-routes.ps1                curl.exe checks of /health and /transcribe on each route
    paste_selftest.py              paste/clipboard self-test on the real desktop
  packaging/                       Windows installer (docs/installer.md); macos/: the Mac app
    shoutabout.spec  entry.py      PyInstaller onedir build of ShoutAbout.exe
    installer.iss                  Inno Setup 6 script: ShoutAbout-Setup-<version>.exe
    build.ps1  smoke-test.ps1      local/CI build; CI-only silent install/uninstall test
    make_assets.py  notices.py     icon + wizard images from brand.py; THIRD-PARTY-NOTICES.txt
  src/shoutabout/
    __main__.py  cli.py            `python -m shoutabout <command>`
    hub.py                         long-running process: tray + server + loader + warmer threads
    server.py                      FastAPI app, source-IP middleware, error mapping
    security.py                    route classification, bearer check, Access JWT, rate limiter
    pipeline.py                    decode -> STT -> cleanup -> paste -> history
    audio.py                       decoder chain -> 16 kHz mono: WAV, Media Foundation, PyAV
    wavdecode.py  resample.py      numpy WAV reader; anti-aliasing polyphase resampler
    mfdecode.py                    Windows Media Foundation decoding through ctypes
    pyavdecode.py  avshim.py       optional PyAV decoder (source installs); `av` placeholder
    vocab.py                       vocab.txt parsing, hot reload, prompt token budget
    engine/                        SpeechEngine protocol, faster-whisper engine, CUDA DLL setup
    cleanup.py  benchmark.py       Ollama client, prompt, safety guard; benchmarks
    inject/                        Injector protocol; Windows clipboard + SendInput paste
    tray.py  icons.py  brand.py    ctypes tray icon (animated brand mouth), menu, balloon toasts;
                                   pure-Python brand renderer (see docs/brand.md, assets in brand/)
    history.py                     history.jsonl writer
    doctor.py                      setup checks
    shortcuts.py                   four iPhone shortcuts as plists or Cherri source (`make-shortcuts`)
    pairing.py                     pairing QR payload for the iPhone app (`pair`)
    discovery.py                   DNS-SD announcement (_shoutabout._tcp) via dnsapi.dll / ctypes
    pair_requests.py  devices.py   phone pairing requests; per-device tokens (devices.json)
    identity.py                    installation id + friendly name (identity.json)
    setup_flow.py  setup_ui.py     first-run setup window (logic / Tk view)
    settings_model.py settings_ui.py  settings window (logic / Tk view)
    approval_ui.py  ui_host.py  ui_kit.py  pairing prompt, the UI thread, brand widgets
    app_services.py  hub_v2.py     what the windows need; the hub's v2 wiring
    config_edit.py  firstrun.py  winstartup.py  comment-keeping config edits, first run, Run key
    config.py  models.py  state.py  text.py  netinfo.py  logging_setup.py
    frozen.py  selftest.py         ShoutAbout.exe entry point; `--self-test` bundle checks
    data/                          config.example.toml, vocab.example.txt
  tests/                           pytest; fakes in tests/fakes.py
  docs/                            the documents linked above
```

## CLI reference

Run the CLI with `.\.venv\Scripts\python.exe -m shoutabout <command>`. The `shoutabout`
console script inside the venv is equivalent.

- **Global options:**
  - `--home PATH` sets the config dir. It overrides the `SHOUTABOUT_HOME` environment
    variable; the default is `%APPDATA%\shoutabout`.
  - `--version` prints the version.
- **Exit codes:** `0` ok, `1` failure, `2` usage error.

| Command | What it does |
|---|---|
| `run [--no-tray]` | Runs the hub. The logon task uses `pythonw.exe -m shoutabout run`. `--no-tray` logs to the console instead (stop the task first, because only one hub can run). |
| `init [--rotate-token]` | Creates the config dir, `config.toml`, `vocab.txt` and the hub token if they are missing. It never overwrites them. `--rotate-token` issues a new token, so you must update the Shortcut afterwards. |
| `info [--json]` | Paths, port, LAN IP/URL, models, whether Access is enabled, and the public hostname. It never shows the token. |
| `token [--copy \| --qr]` | Prints the hub token. `--copy` puts it on the clipboard (excluded from history) and `--qr` shows it as a terminal QR code. |
| `qr [TEXT \| --clipboard] [--open]` | Shows any text as a QR code, for example a Cloudflare service-token ID or secret, so it can reach the phone without a cloud service. `--open` shows an SVG instead and deletes it after you press Enter. |
| `pair [--ssid NAME] [--lan-url URL] [--public-url URL \| --no-public] [--cloudflare \| --cf-file PATH] [--open \| --json [--show-secrets]]` | Shows the pairing QR code for the iPhone app. It contains the URLs, the hub token and (optionally) the Cloudflare service token, so only show it on your own screen ([docs/pairing.md](docs/pairing.md)). |
| `make-shortcuts [--which dictate,target,teach,offline] [--format shortcut\|cherri] [--ssid NAME] [--lan-url URL] [--public-url URL \| --no-public] [--out DIR] [--no-sign]` | Writes four iPhone shortcuts to `DIR` (default `shortcuts-out`), ready to import ([docs/ios-shortcuts.md](docs/ios-shortcuts.md)): **Dictate**, **Dictation Target**, **Teach ShoutAbout** and **Dictate (offline)**. `--format cherri` writes [Cherri](https://cherrilang.org) source instead; the official Cherri playground or compiler can compile and sign it. The Wi-Fi name defaults to the PC's current Wi-Fi (it prints the one it used), the LAN URL to `lan_url` + `/transcribe`, and the public URL to `https://<public_hostname>/transcribe` from `install-state.json`; without one, or with `--no-public`, Dictate is LAN-only. The files hold no secrets: Dictate and Teach ask for them on import. **Signing:** iOS only imports signed shortcuts.
<br>• The default tries RoutineHub's HubSign directly. It currently refuses clients other than Cherri, and we don't impersonate Cherri, so it exits 1 with a hint.
<br>• `--format cherri` lets the official Cherri playground or compiler sign the files legitimately. Those builds ask once for the pairing text (`pair`) instead of three separate secrets.
<br>• `--no-sign` writes unsigned `.plist` files for `shortcuts sign --mode anyone` on a Mac. |
| `transcribe FILE... [--no-cleanup] [--json]` | Transcribes files, folders or globs and prints the raw text, cleaned text and timings. If a `manifest.json` sits next to the files, it also scores word accuracy and vocabulary hits. It never pastes and never writes history. |
| `benchmark [--models a,b] [--runs N] [--stt FILE...]` | Benchmarks the cleanup LLMs (median/p90 latency, guard accept rate, answered-instead-of-cleaned flags). With `--stt` it benchmarks the STT instead: load time, decode/STT ms per file, real-time factor, VRAM and RSS. |
| `setup [--phone]` | Opens the setup window (`--phone`: just the add-a-phone step). Pairing requests are answered by the running hub, so use tray > Set up… when it runs. |
| `settings` | Opens the settings window. |
| `devices list [--json] \| remove NAME_OR_ID` | Lists paired phones (name, id, paired, last used; never tokens) or unpairs one, which revokes its token at once. |
| `doctor [--skip-model] [--skip-ollama] [--skip-tunnel] [--url URL] [--cf-client-id ID --cf-client-secret S]` | Checks everything and prints `[PASS]`, `[WARN]`, `[FAIL]` or `[SKIP]` with a fix hint for each check. It exits 1 on any FAIL. The service token can also come from `SHOUTABOUT_CF_CLIENT_ID` and `SHOUTABOUT_CF_CLIENT_SECRET`. |
| `vocab list\|add\|remove\|dismiss\|suggest [--projects]\|seed [ROOT...]\|review` | Teach, review and edit the vocabulary. `suggest` mines your dictation history while history is on (plus project folders with `--projects`), and `review` opens the Teach ShoutAbout window ([docs/vocab.md](docs/vocab.md)). Run it from a normal terminal, not from inside an app that redirects `%APPDATA%`. |
| `download-model` | Fetches the Whisper model into the cache (about 1.6 GB the first time). |
| `encode-m4a SRC DST [--bitrate N]` | Developer helper: encodes any audio file as AAC `.m4a` to mimic an iPhone recording. Needs PyAV, so it works from a source checkout only (the installed app has no FFmpeg). |

`doctor` runs these checks:
- config and token
- vocab
- audio decoding: a bundled AAC `.m4a` decodes (Windows Media Foundation; fails with the
  Media Feature Pack fix on Windows "N" editions)
- CUDA DLLs and model load
- Ollama is reachable, the model is pulled and it is on the GPU
- the firewall rule exists and no Python block rules exist
- the network profile is Private
- `/health` answers on loopback and on the LAN URL
- the cloudflared service is running
- the public `https://<public_hostname>/health` answers

Scripts are in `scripts\`. Run them with
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\<name>.ps1`. Their
parameters are documented in [SETUP.md](SETUP.md). The names they share with the CLI are fixed:
- scheduled task: `ShoutAbout Hub`
- firewall rule: `ShoutAbout-Hub-TCP-<port>`, in group `ShoutAbout`
- `install-state.json` in the config dir

## Configuration

The config file is `%APPDATA%\shoutabout\config.toml`.
- It is created from
  [`src/shoutabout/data/config.example.toml`](src/shoutabout/data/config.example.toml) by
  `init` or on the first run.
- If the file is missing, every setting takes its default.
- Unknown keys and invalid values stop the hub from starting. The reason is in `hub.log`.
- **After editing, restart the hub.** Use tray > Quit, then
  `Start-ScheduledTask -TaskName 'ShoutAbout Hub'`.
- `vocab.txt` is different: it reloads automatically.

### `[server]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `"0.0.0.0"` | Bind address. `0.0.0.0` covers both loopback (for cloudflared) and the LAN. `127.0.0.1` means tunnel/local only, with no LAN URL. |
| `port` | `47821` | TCP port (1-65535). New installs use 47821 (v1 used 8765, which other software also uses); on the very first run a busy port is swapped for the next free one and saved. If you change it, also update the firewall rule (re-run setup), the tunnel route's service URL and the Shortcut's LAN URL; the iPhone app finds the new port by itself. |
| `discovery` | `true` | Announces the PC on the home network (DNS-SD `_shoutabout._tcp`, no secrets) so the iPhone app finds it. `false` = pair with the QR code. |
| `lan_networks` | `["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]` | Source ranges accepted as the `lan` route. Only private ranges are allowed: RFC 1918, `100.64.0.0/10` (CGNAT/Tailscale), `169.254.0.0/16`, `fc00::/7` and `fe80::/10`. Narrow it to your subnet (e.g. `["192.168.1.0/24"]`) if you like. |
| `advertise_host` | unset | Overrides the LAN IP shown by the tray, `info` and `doctor` when auto-detection picks the wrong adapter (e.g. `"192.168.1.50"`). |
| `max_upload_mb` | `25` | Maximum upload size. Larger uploads get `413`. |
| `max_audio_seconds` | `600` | Maximum audio length. It is checked from the container metadata before decoding and from the decoded length after. Longer audio gets `413`. |
| `rate_limit_per_minute` | `30` | Requests per minute per (route, source IP). Above it the hub returns `429`. |
| `max_auth_failures` | `10` | Failed bearer/JWT attempts per (route, source IP) in 10 minutes before a `429` lockout. |

### `[stt]`

| Key | Default | Meaning |
|---|---|---|
| `engine` | `"faster-whisper"` | The only engine in v1. |
| `model` | `"auto"` | `"auto"` picks it from the hardware ([docs/models.md](docs/models.md)). Or a faster-whisper model name such as `"large-v3-turbo"` (maps to `mobiuslabsgmbh/faster-whisper-large-v3-turbo` on Hugging Face), or a path to a CTranslate2 model folder. v1 configs say `"large-v3-turbo"` and keep working exactly as before. |
| `device` | `"auto"` | `"auto"`, `"cuda"` or `"cpu"`. |
| `compute_type` | `"auto"` | `"auto"`, or a CTranslate2 compute type on CUDA (`"float16"`, `"int8_float16"`...). |
| `tier` | `"auto"` | With the three above on `"auto"`: `"best"`, `"balanced"` or `"light"` forces that setup (the settings window's override). |
| `cpu_fallback` | `false` | If CUDA fails to load: `false` stops with a clear error, `true` runs on the CPU (slow) and logs a warning. With `device = "auto"` the fallback is always on. |
| `cpu_compute_type` | `"int8"` | Compute type used on the CPU. |
| `language` | `"en"` | Fixed language, so language detection never runs. |
| `beam_size` | `5` | Beam search width (1-10). |
| `vad_filter` | `true` | Silero VAD drops silence before decoding, which reduces hallucinations on silence. |
| `vad_min_silence_ms` | `500` | VAD minimum silence (ms) that splits speech. |
| `prompt_mode` | `"both"` | How vocab reaches Whisper: `"both"`, `"hotwords"`, `"initial_prompt"` or `"none"`. |
| `max_prompt_tokens` | `200` | Token budget for the vocab hint text (8-223; Whisper's prompt limit is 223). Terms past the budget are dropped, lowest priority first, with one warning per reload. |
| `download_root` | unset | Model cache dir. Unset means the Hugging Face default, `%USERPROFILE%\.cache\huggingface\hub`. |

### `[cleanup]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `"auto"` | `"auto"` runs the cleanup only when the PC can afford it (the "Best quality" setup with 8 GB or more of VRAM) and a suitable model is already pulled in Ollama; nothing is downloaded for it. `true` always tries, `false` skips the LLM entirely (fastest; the response then has `cleanup: "disabled"`). |
| `ollama_url` | `"http://127.0.0.1:11434"` | Ollama server. |
| `model` | `"qwen3:4b-instruct-2507-q4_K_M"` | Ollama model tag. Pin the full tag. |
| `timeout_s` | `2.0` | Hard timeout for the cleanup call. On timeout the raw transcript is used. |
| `keep_alive` | `-1` | Ollama `keep_alive`: `-1` keeps the model loaded indefinitely, a string such as `"30m"` sets a duration, and `0` unloads it. |
| `think` | `false` | Sends `think: false`, which disables reasoning on hybrid-thinking models. Non-thinking models ignore it. |
| `num_ctx` | `4096` | Context size (>= 512). Keep it constant, because changing it reloads the model. Transcripts too long for it skip cleanup. |
| `warm_interval_s` | `300` | How often the warmer re-checks that the model is loaded (it reloads after an Ollama restart). `0` warms once at startup only. |
| `max_change_ratio` | `0.0` | Guard: maximum share of raw words changed without an explanation (filler removal and vocab fixes are free). `0` = any other word change falls back to the raw transcript. Negation words may never change. |
| `min_similarity` | `0.6` | Guard: minimum word-level similarity between raw and cleaned text. |
| `min_length_ratio` | `0.6` | Guard: lower bound for cleaned words divided by raw non-filler words. |
| `max_length_ratio` | `1.25` | Guard: upper bound for the same ratio. |

### `[paste]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` never pastes on the PC. `target=pc` then behaves like Phone mode and returns a warning. |
| `trailing_space` | `false` | Adds a space after the pasted text. The response text never has one. |
| `restore_clipboard` | `true` | Puts the previous clipboard contents back after pasting. |
| `restore_delay_ms` | `500` | Wait before restoring (0-5000). Raise it for RDP or VM windows. The restore is skipped if something else changed the clipboard in the meantime. |
| `exclude_from_history` | `true` | Marks the transcript, and the restored contents, as excluded from Win+V history and cloud clipboard. |
| `shift_insert_processes` | `["mintty.exe", "putty.exe", "kitty.exe"]` | Executables that get Shift+Insert instead of Ctrl+V. These are the only two key chords ever sent. |

### `[access]`

Cloudflare Access JWT validation on the tunnel route. It is enabled only when both
`team_domain` and `aud` are set. Otherwise the hub logs a loud warning at startup.

| Key | Default | Meaning |
|---|---|---|
| `team_domain` | `""` | `"yourteam"` or `"yourteam.cloudflareaccess.com"`. A `https://` prefix and any path are stripped. |
| `aud` | `[]` | Application Audience (AUD) tag(s) of the Access application, as a string or a list. |
| `allowed_client_ids` | `[]` | Optional. If set, only JWTs issued to these service-token Client IDs (the JWT `common_name`) are accepted. |

### `[dictation]` ("Talk to this PC")

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` turns the hotkey off (no keyboard listener at all). |
| `hotkey` | `"right_ctrl"` | `right_ctrl`, `right_alt`, `f8`, `f9`, `f10`, `scroll_lock` or `pause`. |
| `mode` | `"toggle"` | `"toggle"` (press to start, press again to stop) or `"hold"` (hold to talk). |
| `overlay` | `true` | Shows the mouth pill at the top of the screen. |
| `max_seconds` | `600` | Stops listening after this long (5-600). |

### `[learn]`, `[history]` and `[logging]`

| Key | Default | Meaning |
|---|---|---|
| `learn.enabled` | `true` | Suggests words to teach ShoutAbout from your dictation history (only while `history.enabled` is on). Checked at startup and every 24 h. |
| `learn.seed_projects` | `true` | Also suggests project and repo names found in your project folders. |
| `learn.project_roots` | `[]` | Folders scanned two levels deep. Empty means `~/Documents`, `~/source/repos`, `~/repos`, `~/code` and `~/projects`. |
| `learn.notify` | `true` | Shows one tray notification when new suggestions are waiting. |
| `history.enabled` | `false` | Appends one JSON line per request to `history.jsonl`, and keeps recordings that fail for **Try again**. Off by default for new installs; **Settings > Keep a history of what I dictate** or the History window turn it on (applied at once). Configs from earlier versions that say `true` stay on. |
| `history.keep_audio` | `false` | Also keeps each uploaded audio file in `audio\`. |
| `logging.level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

### Files in `%APPDATA%\shoutabout\`

| File | Contents |
|---|---|
| `config.toml` | Settings (above). |
| `vocab.txt` | Vocabulary, one term per line. `#` starts a comment line, and order sets priority. It hot-reloads. |
| `hub_token.txt` | Hub bearer token: 256 bits, 43 URL-safe characters. `init --rotate-token` replaces it. |
| `history.jsonl` | Request history, only while history is on ([docs/history.md](docs/history.md)). |
| `vocab-suggestions.json` | Pending, accepted and dismissed word suggestions ([docs/vocab.md](docs/vocab.md)). |
| `audio\` | Only present with `keep_audio = true`. |
| `logs\hub.log` | Hub log, rotating at 5 MB with 5 backups. |
| `logs\stdout.log` | Stray stdout/stderr under `pythonw` (download progress, library prints). |
| `install-state.json` | Written by the setup scripts: repo path, port, task and firewall names, public hostname, tunnel mode, what was created. |
| `devices.json` | Paired phones: name, id, paired/last used, and the SHA-256 of each phone's token (never the token). |
| `identity.json` | This installation's id (a UUID, announced on the network) and the custom PC name, if set. |
| `setup_complete` | Written when the setup window was finished or skipped (or for an install that predates it). |

## API (summary)

| Endpoint | Auth | Response |
|---|---|---|
| `GET /health` | Source-IP rule only | `200 {"ok": true}` |
| `POST /transcribe` | Source IP, `Authorization: Bearer <hub token or device token>`, and on the tunnel route `Cf-Access-Jwt-Assertion` (added by Cloudflare) | `200` JSON with `text`, `raw_text`, `target`, `pasted`, `warning`, `status`, `route`, `cleanup`, `timings_ms` |
| `GET /hello`, `POST /pair/request`, `GET /pair/status` | Home network only (404 through the tunnel or loopback), rate limited; pairing needs a click on the PC | see [docs/protocol.md](docs/protocol.md) |
| `GET /info`, `POST /paste` | Same as `/transcribe` | `/info`: id, name, version, model tier/speed; `/paste`: pastes phone-made text like a transcription |

`/transcribe` takes the audio in either of two ways:

- **Multipart form** (`Content-Type: multipart/form-data`): a `file` field with the audio, and a
  `target` field.
- **Raw body:** the request body is the audio itself, with `Content-Type: audio/*` (for example
  `audio/x-m4a`; `video/*`, `application/octet-stream` or no content type are accepted too), and
  the target in the query string: `POST /transcribe?target=phone`. This is what the iPhone
  Shortcut sends (Request Body: File).

`target` is `pc` or `phone`, in any case. **Missing or empty means `pc`**, for example when the
Shortcut's `dictation-target.txt` doesn't exist yet. Any other value gets `422`.

`status` is `"ok"` when there's no warning, and `"warning"` when `warning` is set (hub paused,
admin window, PC locked, no speech, and so on). It's an exact string that the Shortcut can
compare against; it stays silent only on `"ok"`. Error replies have no `status`.

Errors come back as `{"detail": "..."}`:

| Status | Meaning |
|---|---|
| 400 | Bad multipart, no `file` field in a form (`missing 'file' upload`), or an empty upload |
| 401 | Bearer token missing or wrong |
| 403 | Source address not allowed, or Access JWT invalid |
| 413 | Upload or audio too large |
| 415 | Audio can't be decoded, or a raw body whose `Content-Type` isn't audio (e.g. `application/json` or `text/plain`) |
| 422 | Bad `target` |
| 429 | Rate limited or locked out |
| 503 | Engine loading or failed, or this PC can't decode audio at all (Windows Media Foundation missing, e.g. a Windows "N" edition without the Media Feature Pack; the detail says how to fix it) |
| 500 | Unexpected error |

A paste that fails still returns `200`, with `pasted: false` and a `warning`. Full details are
in [docs/architecture.md](docs/architecture.md).

## Security model

- **App-level bearer token.** Every request to `/transcribe` must carry `Authorization: Bearer
  <hub token>`, which is compared in constant time (`hmac.compare_digest`). `/transcribe` is
  the only route that does anything. `/health` needs no token, returns only `{"ok": true}`, and
  still goes through the source-IP rule. Authentication happens before the request body is
  read. The OpenAPI and docs pages are disabled.
- **Source-IP allow-list (middleware, every path).** Routing and auth use only the TCP peer
  address. `X-Forwarded-For` and `Cf-Connecting-IP` never affect routing or authentication, and
  uvicorn's proxy headers are off; otherwise X-Forwarded-For could make a tunnel request look
  like a LAN one.
  - On the tunnel route, a valid `Cf-Connecting-IP` (set by Cloudflare) only selects the
    rate-limit/lockout bucket, so one remote client can't lock out the owner. A local process
    could rotate that header to evade the lockout, but it still needs the 256-bit hub token.
  - Loopback is the `tunnel` route, because cloudflared runs on this PC.
  - Addresses in `server.lan_networks` (private ranges only) are the `lan` route.
  - **Everything else gets 403, including `/health`.**
- **Cloudflare Access on the tunnel route.**
  - The public hostname is an Access self-hosted application with a **Service Auth** policy
    for one service token. Requests without the token never reach the PC.
  - The hub also validates the `Cf-Access-Jwt-Assertion` JWT itself:
    - RS256 only
    - signature checked against the team's JWKS, with keys cached and rotated
    - `aud` must match
    - `iss` must be exactly `https://<team>.cloudflareaccess.com`
    - `exp` and `iat` are required
    - if `allowed_client_ids` is set, `common_name` must be on it
  - A local process that talks to `127.0.0.1:8765` therefore needs both the hub token and a
    valid Access JWT.
  - **If `[access]` isn't configured, the hub logs a loud warning at startup.** In that case
    tunnel requests are accepted with the bearer token alone, and Access at the edge is the only
    other layer.
- **Plain HTTP on the LAN.** The token and the audio cross the home Wi-Fi unencrypted. WPA2/3
  protects against outsiders but not against other devices on the same network. This is
  accepted for v1, and LAN HTTPS is a future item
  ([docs/networking-alternatives.md](docs/networking-alternatives.md)).
- **Paste only, plain text only.**
  - The API can only ever paste plain text into the focused window.
  - There is no keystroke API and no command API.
  - The only keys ever sent are the paste chord (Ctrl+V, or Shift+Insert for listed
    terminals).
  - **Enter is never pressed.** Output is always one paragraph (all whitespace, including
    newlines, is collapsed), so a dictation can't submit a terminal prompt by itself.
  - Spoken "new line" or "send it" stays as words.
- **Non-elevated hub.** The hub runs as the logged-in user without admin rights. Windows UIPI
  blocks input into elevated windows. The hub detects them by comparing integrity levels,
  leaves the text on the clipboard and shows a toast instead.
- **Abuse limits.**
  - 30 requests per minute per (route, IP).
  - A lockout after 10 failed bearer/JWT attempts in 10 minutes.
  - Upload size and audio length limits.
  - Only one request is processed at a time.
  - Uploaded files are recognised by their first bytes (never the client's file name), and
    only allow-listed containers (MP4/M4A, MP3, ADTS AAC, WAV, FLAC, WMA, WebM, AMR) are
    handed to Windows Media Foundation. Plain WAV is read by ShoutAbout itself. In a source
    checkout, PyAV/FFmpeg is the fallback, restricted to an allow-list of audio/media
    demuxers and the `file` protocol.
  - Tunnel requests all share one key (`127.0.0.1`), so a misconfigured remote client can
    lock out remote use for 10 minutes. Restarting the hub clears the counters.
- **No secrets in logs.**
  - Request log lines contain IDs, routes, sizes and timings, but no text and no tokens.
  - Rejections log the reason, not the credential.
  - Cleanup fallbacks log the reason, not the transcript.
- **Local history, off by default.** Nothing you dictate is kept until you turn history on.
  Then `history.jsonl` holds transcript text and stays on the PC. Audio is not kept unless you
  turn that on too.
- **Secrets live in `%APPDATA%\shoutabout`, never in the repo.**
  - The hub token is in `hub_token.txt`.
  - The Access service-token secret exists only in Cloudflare and in the iPhone Shortcut.
    `doctor` accepts it through flags or environment variables for a one-off check.
  - `.gitignore` blocks the usual secret file names.
  - The hub token, and the `token --copy` output, are excluded from clipboard history.

## Privacy

- Audio and text are processed on the PC. Whisper and the cleanup LLM run locally. There is
  no cloud STT or LLM, and ShoutAbout itself sends no telemetry.
- The only path off the LAN is your own Cloudflare Tunnel, which is used when the phone isn't
  on the home Wi-Fi.
  - Cloudflare terminates TLS at its edge, so audio and text pass through Cloudflare's network
    in transit. That is inherent to Tunnel and Access.
  - ShoutAbout stores nothing at Cloudflare.
  - On the LAN, nothing leaves the house.
- At setup only, `make-shortcuts` sends the iPhone shortcuts' structure (actions, the two URLs
  and the home Wi-Fi name; never tokens, audio or text) to RoutineHub's HubSign service to get
  Apple-signed files. `--no-sign` skips it. Nothing is sent there while you dictate.
- One-time downloads:
  - the Whisper model from Hugging Face
  - the cleanup model from the Ollama registry
  - Python packages from PyPI
- After the first download, the engine loads the Whisper model with `local_files_only=True`, so
  starting the hub makes no request to huggingface.co. It only goes online if the model isn't
  in the cache.
- History is off by default. When you turn it on, the history (text) and optional audio stay
  in `%APPDATA%\shoutabout`. You can delete them at any time
  ([docs/history.md](docs/history.md)).

## Performance

**Target:** under 1.5 s at home for a 15 s dictation. The time runs from the upload reaching the
hub to the paste. It excludes the phone's own overhead: stopping the recording, running the
Shortcut, and the upload itself.

Measured on the target PC during the build (RTX 4080 SUPER 16 GB, Windows 11, Python 3.12).
The audio was SAPI text-to-speech encoded as AAC m4a, sent over HTTP to a running hub:

| Stage | Measured |
|---|---|
| Whisper large-v3-turbo fp16, load from cache + warm-up | 2.1-2.2 s (first download ~1.6 GB) |
| Decode, m4a/AAC (2-25 s clips) via Windows Media Foundation + numpy resampling | 6-60 ms, median 12 ms (PyAV/FFmpeg, used before: 5-22 ms, median 7 ms) |
| STT with vocab hints, 6 s / 9 s / 18 s clips | ~145 ms / ~145 ms / ~225 ms (RTF ≈ 0.015-0.023) |
| Cleanup LLM (`qwen3:4b-instruct-2507-q4_K_M`), warm | median 91 ms, p90 116 ms on short samples; ~360 ms for an 18 s paragraph |
| Paste (clipboard + Ctrl+V, measured by `scripts/paste_selftest.py`) | ~48 ms (clipboard is restored 500 ms later, after the response) |
| **Total, hub-side, ~6-9 s dictation** | **~290-310 ms** |
| **Total, hub-side, 18 s paragraph** | **~600 ms** (target: < 1.5 s) |
| Phone-observed round trip, LAN vs tunnel | not measurable without the phone; compare `timings_ms` + a stopwatch (see below) |
| VRAM: Whisper / cleanup LLM | +2.4 GB / 3.0 GiB (100% GPU per `ollama ps`) |
| RAM (hub process RSS with the model loaded) | ~670 MB |

The first request after start can be slower (~650 ms for a 6 s clip) while CUDA kernels and
Ollama's prompt cache warm up.

How to measure:
- **Per request:**
  - `timings_ms` in the response (`decode`, `stt`, `cleanup`, `paste`, `total`). These are
    hub-side numbers and exclude the upload.
  - The `transcribe ...` line in `hub.log`.
  - `history.jsonl`, which has ready-made PowerShell queries in
    [docs/history.md](docs/history.md).
- **Offline:**
  - STT: `python -m shoutabout benchmark --stt test-audio\*.m4a --runs 5` reports load time,
    decode/STT ms, real-time factor, GPU memory before and after load, and RSS.
  - Cleanup LLMs: `python -m shoutabout benchmark --models qwen3:4b-instruct-2507-q4_K_M,granite4:micro --runs 10`.
- **Memory:**
  - `nvidia-smi --query-gpu=memory.used,memory.total --format=csv`
  - `ollama ps`: `PROCESSOR` should read `100% GPU`.
  - Task Manager > Details (`pythonw.exe`), or Performance > GPU > Dedicated GPU memory.
- **Phone side:** wrap the Get Contents of URL action in the Shortcut with Get Current Date
  before and after it, or time it with a stopwatch.

## Assumptions & decisions

1. **One bind on `0.0.0.0` plus a middleware allow-list, not two binds.**
   - Binding the LAN IP fails when Wi-Fi or DHCP comes up after logon or the IP changes; a
     wildcard bind doesn't.
   - The route comes from the TCP peer: loopback is the tunnel (cloudflared on this PC),
     private LAN ranges are the LAN, and everything else gets 403.
2. **The engine takes decoded 16 kHz float32 samples, not a file path.** Decoding is timed
   separately, duration limits are enforced before the GPU runs, decoding is testable without
   CUDA, and engines are swappable (a Parakeet engine could implement the same protocol).
3. **No cuDNN, no CUDA toolkit.**
   - CTranslate2 >= 4.6.3 Windows wheels are built without cuDNN, so only cuBLAS 12 is needed.
     It comes from the `nvidia-cublas-cu12` wheel (`uv sync --extra cuda`).
   - The DLL folder is registered with `os.add_dll_directory` and also prepended to `PATH`,
     because CTranslate2 loads cuBLAS with a plain `LoadLibraryA`.
4. **No FFmpeg in the app; PyAV only in source checkouts.** PyAV's wheels bundle FFmpeg
   linked with x264/x265 (GPL), so the Windows app decodes audio with numpy (WAV) and Windows
   Media Foundation (AAC, MP3, FLAC, ...), and resamples with its own anti-aliasing filter
   (`audio.py`, `mfdecode.py`, `resample.py`). faster-whisper still imports `av` for its own
   file decoder, which ShoutAbout never calls; when PyAV is missing, `avshim.py` puts a
   placeholder module in its place first. PyAV stays installed in source checkouts (it is a
   faster-whisper dependency) and decodes there as the fallback after Media Foundation, and as
   the decoder on Linux CI. The `pyav` extra pins it below 19, which removed the
   `metadata_errors` argument that faster-whisper's own decoder passes.
5. **uv `exclude-newer = 2026-09-25`.** Only releases at least about two weeks old at build
   time are resolved, for stability. To update, bump the date and run `uv lock --upgrade` on
   purpose.
6. **Vocab goes to Whisper as both `hotwords` and `initial_prompt` within one token budget.**
   - The budget is `max_prompt_tokens = 200`, under Whisper's 223-token prompt limit. Terms
     are added greedily in priority order.
   - For clips of 30 s or less, both act on the same window. Hotwords persist past 30 s.
   - The budget protects output tokens: CTranslate2 allows `min(224, 448 - prompt)` new
     tokens per window.
   - Hotwords get the budget first. The top terms are then repeated as `initial_prompt` in
     whatever budget is left, so the total never exceeds `max_prompt_tokens`. The hint reads
     `In this dictation I mention A, B, C.`
   - An experiment on this PC used 12 sentences × 2 SAPI voices with a 14-term vocab
     including made-up names. "both" with the sentence format found 95% of terms (WER 1.4).
     Hotwords alone found 94%, `initial_prompt` alone 92%, and no hints only 43% (WER 12.8).
     Label formats ("Vocabulary: …") occasionally leaked into the output when VAD was off. The
     sentence format never did.
7. **Cleanup model `qwen3:4b-instruct-2507-q4_K_M`.**
   - It is Apache-2.0 and about 2.5 GB, with IFEval 83.4.
   - It only runs in non-thinking mode, so no reasoning can leak or loop; `think: false` is
     harmlessly ignored.
   - The full tag is pinned, because bare `qwen3:4b` may resolve to the thinking variant.
   - Alternates: `granite4:micro`, `granite4.2:3b` (needs `think: false`), `ministral-3:3b`
     and `qwen3.5:4b` (`think: false`). All are Apache-2.0.
   - License rule: permissive licenses only (Apache-2.0/MIT), which keeps open-source and
     commercial options open. LFM2 (revenue cap) and Llama (community license) are excluded.
8. **Guard design.** This is a pure function over normalized word lists (difflib word-level).
   - **Explained edits are free:**
     - removing filler words (um, uh, …), and "like" only where Whisper set it off with a
       comma (", like,"), so the verb in "I'd like to merge" can't vanish
     - removing stutters of words like "I I" / "the the" (but not grammatical repeats such as
       "had had" or "that that")
     - replacing a raw span with a vocabulary term it sounds like (character similarity
       >= 0.6 *and* a similar length, so a "fix" can't swallow a neighbouring word)
   - **The guard rejects:**
     - empty output
     - line breaks, markdown, code blocks or lists (signs of an answer)
     - **any** unexplained word edit (`max_change_ratio = 0.0` by default)
     - any change in negation words ("not", "don't", "never", …), whatever the thresholds
     - similarity < 0.6
     - length ratio outside [0.6, 1.25] (with +/-2 words of slack for short texts)
   - The default was tightened after an end-to-end test on this PC. The LLM turned
     "refactor the ShoutAboutHub" into "refactor the ShoutAbout", dropping "hub", and a
     1-word allowance would have accepted a dropped "don't" just as quietly. A rejected
     cleanup costs little, because Whisper's raw text is already punctuated and cased.
     Built-in benchmark samples still pass 100%.
   - Timeouts (2 s), errors and truncated output also fall back to the raw transcript, with
     the reason recorded.
   - The prompt is hardened as well:
     - the transcript is treated as data, inside `<transcript>` delimiters
     - few-shot examples include instruction-like dictation
     - the rule is repeated after the transcript
     - sampling is deterministic
9. **Custom ctypes tray instead of pystray.** pystray is LGPL-3.0, while the ctypes
   `Shell_NotifyIcon` version has no dependency and leaves licensing open. Icons are generated
   in pure Python (no Pillow).
10. **Toasts use the tray's balloon** (`Shell_NotifyIcon` with `NIF_INFO`). This needs no
    WinRT or toast library. Windows 11 shows them as transient banners (not kept in
    Notification Center), and Do Not Disturb suppresses them.
11. **Clipboard operations use a temporary owner window.** With `OpenClipboard(NULL)` the
    clipboard has no owner, and `SetClipboardData` fails. The transcript and the exclusion
    formats are written in one `OpenClipboard` session.
12. **Integrity-level check for admin windows.** `SendInput` into a higher-integrity process
    fails silently (UIPI). The injector compares the foreground process's token integrity level
    with its own, which predicts blocking better than the elevation flag. It does not pretend
    to paste.
13. **Shift+Insert for mintty, PuTTY and kitty**, where Ctrl+V doesn't paste by default. It is
    still a paste chord, never any other key, and the list is configurable.
14. **The hub is a logon scheduled task in the user session, not a Windows service.** A service
    runs in session 0, which has no access to the interactive desktop or clipboard, so it
    can't paste. The task is AtLogOn for the installing user, Interactive, RunLevel Limited,
    and is registered without admin.
15. **cloudflared stays a Windows service.** It is already installed, survives logoff, and
    proxies to `127.0.0.1:8765`.
16. **A dashboard-managed (remote) tunnel is preferred.** This PC already runs one, and adding a
    published-application route `dictate.shoutabout.fyi -> http://127.0.0.1:8765` needs no
    second service and no `config.yml`. A CLI, locally-managed path is available in
    `setup-cloudflare.ps1`.
17. **The Access application must exist before the tunnel route.** Otherwise the hostname is
    public for the gap.
18. **The mode (pc/phone) is a text file in iCloud Drive/Shortcuts** (`dictation-target.txt`).
    It is shared by the Dictate and Dictation Target Shortcuts, persists across runs, and needs
    no third-party app. Dictate sends the file's text as `?target=`; the hub treats a missing or
    empty value as pc.
19. **The route (LAN or tunnel) is chosen from the Wi-Fi name before sending.** Shortcuts has
    no try/catch: a failed Get Contents of URL stops the Shortcut, so "try LAN, fall back to
    tunnel" can't be built. iOS can hide Wi-Fi names without Location permission, but on the
    owner's iPhone the name was read with only the default first-run prompts. Checking the
    local IP prefix is an alternative.
20. **"Normal" recording quality** (AAC .m4a). Whisper resamples to 16 kHz mono anyway, and
    "Very High" is uncompressed and makes the upload much bigger.
21. **The `qr` command moves secrets to the phone** through the camera: the hub token and the
    service-token ID and secret. No cloud clipboard, email or notes app is involved.
22. **History is off by default, and JSONL without audio when on.** Nothing you dictate is
    kept unless you ask for it. Text is enough for the future correction UI, while audio is
    large and more sensitive.
23. **Config lives in `%APPDATA%` (Roaming) for simplicity.** That is one well-known folder for
    config, token, vocab, history and logs. Roaming profiles don't apply on a home PC.
24. **No spoken commands anywhere.** "New line", "period" and "send it" stay as words. Dictating
    into a terminal must never submit or format anything, and the prompt, few-shot examples and
    guard enforce this.
25. **Output is always a single paragraph.** Newlines are collapsed before cleanup and again
    after it.
26. **One request at a time.** The GPU and the keyboard focus are shared, and dictation is
    single-user.
27. **The engine loads in the background.** `/transcribe` returns `503` with the reason until it
    is ready. CUDA failure is a clear error unless `cpu_fallback = true`.
28. **The iPhone shortcuts are generated, then signed through RoutineHub's HubSign.**
    - `make-shortcuts` (`shortcuts.py`) writes both shortcuts as plists, so nobody has to build
      them by hand. iOS only imports Apple-signed shortcut files, and signing otherwise needs a
      Mac (`shortcuts sign`).
    - HubSign is used **only at setup time** and receives **only the shortcut structure**: the
      actions, the two URLs and the Wi-Fi name. Never the hub token or the Cloudflare secrets
      (Dictate asks for those as import questions on the phone), and never audio or text.
    - It's optional: `--no-sign` writes unsigned plists for someone with a Mac.
    - It's the one third-party service besides Cloudflare, and it isn't used at runtime.
29. **The Shortcut uploads the recording as the raw request body** (Request Body: File, with
    `?target=` in the URL). On the real iPhone, a Form body's File field was silently dropped:
    the hub received `application/x-www-form-urlencoded` without the file. The hub accepts both
    styles, so curl and other clients can keep using multipart.
30. **The response has a `status` field (`"ok"` or `"warning"`).** In Shortcuts, "has any value"
    is true even for empty or `null` values, and a raw Dictionary Value didn't match `is`. The
    Shortcut converts `status` to Text, stays silent only on exactly `ok`, and starts its
    notification body with literal text, because an empty body fails with "Please choose a
    value for each parameter".

## What's verified vs untested

This was built on the target PC itself (RTX 4080 SUPER, Windows 11, driver 591.86). Item-by-item
status is in [docs/manual-test-checklist.md](docs/manual-test-checklist.md).

**Verified on this PC during the build (actually run):**
- **GPU and models:**
  - CUDA via the `nvidia-cublas-cu12` wheel only (no cuDNN, no toolkit).
  - Whisper large-v3-turbo fp16 loads in ~2.1 s from cache and makes no network request after
    the first download.
  - The vocab-hint experiment (12 sentences × 2 voices).
- **Decoding:** AAC m4a decodes with Windows Media Foundation, with no FFmpeg in the app. On
  the 29 test clips, Media Foundation and the old PyAV decoder give the same length (0.00 %
  difference), zero lag, correlation >= 0.9998 on speech, and 28 of 29 identical Whisper
  transcripts (the other differs only in punctuation). The frozen app transcribed all 29 the
  same as a source run. The files were AAC m4a encoded by PyAV from SAPI text-to-speech, not
  real iPhone recordings.
- **Whole hub over HTTP:** real engine + Ollama, on `127.0.0.1` only (port 8799, scratch
  config), so every request was the `tunnel` route with `target=phone`.
  - `/health`, 401 without a token, and 200 with all fields and timings.
  - `history.jsonl` written, including a guard fallback.
  - Launched with `pythonw.exe` and the tray, like the scheduled task, with no console
    crashes; graceful Quit through the tray window.
- **Transcription quality and speed:**
  - 29 TTS clips (2 voices): 30/30 vocab terms right.
  - Instruction-looking dictation returned verbatim, never answered. "New line. Send it.
    Scratch that." stayed words.
  - Silence gave empty text.
  - Median hub time 235 ms; 18 s of speech ~600 ms.
  - A 9.5-minute file took 6.8 s. An 11-minute file was rejected at the 600 s limit.
- **Cleanup LLM:** median 91 ms, 3.0 GiB on 100% GPU, 0 answer leaks in the benchmark. The
  guard caught a real dropped word ("ShoutAboutHub" → "ShoutAbout") and fell back.
- **Paste:** `scripts/paste_selftest.py` pastes only into its own window.
  - Ctrl+V and Shift+Insert both delivered the exact text (~48 ms).
  - The previous clipboard was restored after 500 ms.
  - The clipboard-history and cloud exclusion formats were present.
- **Real Windows tests:** clipboard round-trip, race-free restore, struct sizes, process
  integrity queries, tray icon smoke test (add, notify, state changes, quit).
- **Scripts:** all parse in Windows PowerShell 5.1. `make_test_audio.ps1` was run for real.
  Read-only probes: the existing dashboard-managed `cloudflared` service is detected, the Wi-Fi
  profile is Private, there are no Python block rules, and the port-8765 conflict is detected.

**Covered by unit tests only (fakes, no real network):**
- The cleanup guard, including instruction-looking outputs, negation words, filler and
  stutter rules.
- Bearer auth.
- Cloudflare Access JWT validation, using a locally generated RSA key and a fake JWKS (never
  a real Cloudflare token).
- Route rules and public-IP rejection; rate limiting and lockout.
- The 413/415/422/503 paths; pause behaviour; the history schema; config validation; the CLI.

**Not run during the build (needs you, your phone or your Cloudflare account):**
- `setup.ps1` / `setup-admin.ps1` / `setup-cloudflare.ps1` / `uninstall.ps1` /
  `test-routes.ps1`. Only parse-checked, because they install software, create firewall rules
  and the logon task, and touch Cloudflare.
- Logon autostart.
- The hub listening on the LAN (`0.0.0.0`) and the firewall rules.
- Anything from the iPhone: Shortcuts, Action Button, Control Center, SSID routing, real
  iPhone audio, the permissions prompts. *Since then* the owner verified on a real iPhone:
  importing the signed shortcuts from `make-shortcuts`, home Wi-Fi detection, the LAN request
  with the recording as the raw body, Copy to Clipboard and the paste on the PC. The rest is
  listed in [docs/ios-shortcuts.md §11](docs/ios-shortcuts.md#11-verification-status).
- The Cloudflare tunnel route, Access app, service token and a real JWT.
- Pasting into Notepad, Windows Terminal + Claude Code, VS Code, Chrome/Edge, the Claude
  desktop app and Git Bash.
- The admin-window toast; the locked-PC behaviour.
- Visually checking Win+V history; the tray menu items by hand.
- Phone-observed latency, LAN vs tunnel.

## Future phases (context only, not commitments)

- An NVIDIA Parakeet TDT engine behind the same `SpeechEngine` protocol.
- A correction UI built on `history.jsonl`: review raw vs cleaned text and promote fixes into
  `vocab.txt`.
- Pairing and transport: QR-code pairing, mDNS discovery, per-device keys with revocation, and
  an end-to-end-encrypted relay for remote use
  ([docs/networking-alternatives.md](docs/networking-alternatives.md)).
- HTTPS on the LAN.
- Public release of the Windows installer ([docs/installer.md](docs/installer.md)). It depends
  on the license decision, and [docs/licenses.md](docs/licenses.md) lists the redistribution
  flags (PyAV's FFmpeg, the former blocker, is no longer shipped).

## Development

```powershell
uv sync --extra cuda            # always include the cuda extra; a plain `uv sync` removes it
uv run pytest                   # unit tests (no GPU / Ollama / desktop needed)
uv run pytest --run-gpu --run-ollama --run-windows   # opt-in hardware tests on the target PC
uv run ruff check .
```

## License

ShoutAbout's code is under the [MIT License](LICENSE). Third-party software, models and fonts
keep their own licenses (see [docs/licenses.md](docs/licenses.md)). The ShoutAbout name and the
mouth logo aren't covered by the MIT License: a modified version shouldn't be called ShoutAbout
or use the logo.
