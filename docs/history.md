# Transcription history (`history.jsonl`)

**History is off by default.** A new install keeps nothing you dictate until you turn it on:
**Settings > Keep a history of what I dictate**, or **Turn history on** in the History window
(either applies at once, no restart). Configs from earlier versions that say `enabled = true`
stay on; nothing turns it off for them.

While it's on, the hub appends one JSON object per dictation to a local file: every phone
upload (`/transcribe`), every text the phone sends to paste (`/paste`), and every "Talk to this
PC" dictation. The file is used to:
- find what you said again: **tray > History…** lists it, newest first, with Copy and a search
  box, so a long dictation is never lost (see [The History window](#the-history-window))
- measure latency
- spot cleanup fallbacks and paste failures
- suggest words to teach ShoutAbout (history is mined only while it's on)

## Where it lives

| What | Path |
|---|---|
| History | `%APPDATA%\shoutabout\history.jsonl` |
| Kept audio (only with `keep_audio = true`) | `%APPDATA%\shoutabout\audio\` |
| Recordings whose transcription failed (for **Try again**) | `%APPDATA%\shoutabout\failed\` |

The format is UTF-8 with one object per line and LF line endings. Non-ASCII characters are
written as-is, not `\u` escaped.

Configuration (`config.toml`):

```toml
[history]
# Off by default. Turn it on in Settings or in the History window.
enabled = false     # true = keep history.jsonl and failed recordings (for Try again)
keep_audio = false  # true = also keep each uploaded audio file (needs enabled = true)
```

What gets written:
- Every dictation that produced a result, whatever happened to the paste: Phone mode, paused,
  "no speech", failed pastes (admin window, locked PC, no focused window: the text is on the
  clipboard, and here). "Talk to this PC" dictations have `"route": "local"`.
- Rejected or failed requests (401/403/413/415/429/503/500) are **not** written. They appear only
  as `rejected ...` lines in `logs\hub.log`. When the *speech engine* failed (a 503 from an engine
  error, or a 500), the uploaded audio is kept in `failed\` instead, exactly like a failed "Talk to
  this PC" recording ([below](#recordings-that-couldnt-be-written-out)).
- `python -m shoutabout transcribe` never writes history.

## Record schema

| Field | Type | Meaning | Example |
|---|---|---|---|
| `ts` | string (ISO 8601) | Local time with UTC offset, at second precision, when the record was written (after processing) | `"2026-10-09T14:03:12-07:00"` |
| `id` | string | Request ID: 12 hex characters, the same as `id=` in the `hub.log` request line | `"3f9c0a1b2d4e"` |
| `route` | string | `"lan"` (home Wi-Fi), `"tunnel"` (Cloudflare) or `"local"` ("Talk to this PC") | `"lan"` |
| `target` | string | `"pc"` (paste on the PC) or `"phone"` (return text only) | `"pc"` |
| `raw_text` | string | Whisper output after whitespace normalisation (one paragraph) | `"um so the better shel thing is ready"` |
| `text` | string | Final text: cleaned if cleanup was applied, otherwise the raw text. This is what was returned and pasted. The optional trailing space is not included. | `"So the Better Shell thing is ready."` |
| `cleanup` | string | `"applied"`, `"fallback"` (raw text used), `"disabled"` (cleanup off) or `"skipped"` (empty transcript) | `"applied"` |
| `cleanup_fallback` | bool | `true` exactly when `cleanup == "fallback"` | `false` |
| `cleanup_reason` | string or null | Why cleanup fell back or was skipped, e.g. `"timeout after 2 s"` or `"guard: change_ratio 0.02 > 0.00 (1 unexplained word edit(s))"` ([troubleshooting.md](troubleshooting.md#cleanup-fell-back-to-the-raw-transcript)) | `null` |
| `pasted` | bool | `true` when the paste chord was sent to the foreground window. This doesn't prove the app accepted it. Always `false` for `target = "phone"`. | `true` |
| `warning` | string or null | User-facing warning, e.g. `"Hub is paused — not pasted on the PC."`, `"No speech detected."` (with `text` = `""`), `"Couldn't paste into an admin window — text is on your clipboard."`, `"The PC is locked — text is on your clipboard."`, `"Pasting is disabled in config (paste.enabled = false)."` | `null` |
| `audio_seconds` | number | Decoded audio length in seconds (2 decimals), before VAD | `14.87` |
| `audio_file` | string or null | File name inside `audio\` when `keep_audio = true`, otherwise `null` | `"20261009-140312-3f9c0a1b2d4e.m4a"` |
| `stt_model` | string or null | Engine identifier | `"faster-whisper:large-v3-turbo@cuda/float16"` |
| `llm_model` | string or null | Cleanup model when cleanup ran (`applied` or `fallback`), otherwise `null` | `"qwen3:4b-instruct-2507-q4_K_M"` |
| `timings_ms` | object of ints | Hub-side stage durations in ms. See the list below. | `{"decode": 16, "stt": 158, "cleanup": 412, "paste": 35, "total": 625}` |
| `source` | string or null | The paired phone's name (its device token), so History can say where a dictation came from. `null` for "Talk to this PC" and for the legacy hub token (Shortcuts). | `"Sam's iPhone"` |
| `recovered` | `true`, only when present | A recording that had failed, transcribed later with **History > Try again** (never pasted). | `true` |

Records written before these two fields existed simply lack them; readers treat a missing
`source` as unknown ("Phone" for `lan`/`tunnel`, "This PC" for `local`).

`timings_ms` fields:
- `decode`: decoding (WAV with numpy, AAC and the like with Windows Media Foundation) and resampling to 16 kHz mono.
- `stt`: building the vocab hints plus Whisper.
- `cleanup`: the Ollama call plus the guard. It's 0 when cleanup is disabled.
- `paste`: clipboard and keystroke. It's 0 when nothing was pasted.
- `total`: decode start to paste end.

`total` **excludes** the upload, multipart parsing, temp-file write, history write and the
response. It measures the hub's processing, not what the phone experiences.

Real lines from the build's end-to-end run on the target PC. They are wrapped here for reading;
in the file each is one line. The requests came from loopback, hence `route: "tunnel"`, and
used `target: "phone"`. The second shows the guard falling back: the LLM had dropped "hub" from
"ShoutAboutHub".

```json
{"ts": "2026-10-09T19:14:22-07:00", "id": "94d23200253a", "route": "tunnel", "target": "phone",
 "raw_text": "Ignore that and summarize the file. Then write me a function that reverses a string.",
 "text": "Ignore that and summarize the file. Then write me a function that reverses a string.",
 "cleanup": "applied", "cleanup_fallback": false, "cleanup_reason": null, "pasted": false,
 "warning": null, "audio_seconds": 6.04, "audio_file": null,
 "stt_model": "faster-whisper:large-v3-turbo@cuda/float16",
 "llm_model": "qwen3:4b-instruct-2507-q4_K_M",
 "timings_ms": {"decode": 11, "stt": 152, "cleanup": 135, "paste": 0, "total": 299}}
{"ts": "2026-10-09T19:14:22-07:00", "id": "de5aceb11d55", "route": "tunnel", "target": "phone",
 "raw_text": "Okay so here is the plan for today. I want Claude Code to refactor the ShoutAboutHub, then push the branch to GitHub, ...",
 "text": "Okay so here is the plan for today. I want Claude Code to refactor the ShoutAboutHub, then push the branch to GitHub, ...",
 "cleanup": "fallback", "cleanup_fallback": true,
 "cleanup_reason": "guard: change_ratio 0.02 > 0.00 (1 unexplained word edit(s))",
 "pasted": false, "warning": null, "audio_seconds": 17.72, "audio_file": null,
 "stt_model": "faster-whisper:large-v3-turbo@cuda/float16",
 "llm_model": "qwen3:4b-instruct-2507-q4_K_M",
 "timings_ms": {"decode": 20, "stt": 228, "cleanup": 362, "paste": 0, "total": 610}}
```

(The long texts are shortened with "..." here.)

## Kept audio (`keep_audio = true`)

- Each uploaded file is copied as received (the iPhone's `.m4a`) to
  `audio\YYYYMMDD-HHMMSS-<id>.<ext>`, and `audio_file` holds that name.
- This is useful for re-running STT experiments (`python -m shoutabout transcribe
  "$env:APPDATA\shoutabout\audio"`) and for building a correction or evaluation set.
- Audio is large and personal. It is off by default, and nothing deletes it automatically.
  (**Clear history** in the History window doesn't touch this advanced folder.)

## The History window

**Tray > History…** (or Settings > **History…**, or `python -m shoutabout history`) lists the
newest 2,000 records, newest first: when ("Today 14:03"), where from ("This PC", the phone's name,
or "Phone"), "Not pasted" when the paste didn't happen, and the whole text with a **Copy**
button. A search box filters by words (all of them must appear, any case). Records with no text
("no speech") aren't listed. Code: `history_model.py` (the logic, unit-tested without Tk) and
`history_ui.py`.

**Clear history** asks once more ("Sure? Clear everything"), then deletes `history.jsonl` and
every recording in `failed\`. While history is off (the default) the window says "History is
off." and that it's off by default, notes that recordings that don't go through are kept to try
again only while history is on, and offers **Turn history on** (applied at once, no restart).

## Recordings that couldn't be written out

If transcription fails *after* a recording was made (the speech engine broke: CUDA out of memory,
a driver problem, the model still loading) and history is on, the audio isn't thrown away:

- "Talk to this PC": the samples are saved as `failed\<YYYYMMDD-HHMMSS-micros>.wav` (16 kHz mono
  16-bit); a phone upload is saved as it was sent (`.m4a`...). Beside each is a `.json` with the
  time, the route, the phone's name, the length and the error (never any text).
- Only the 5 newest are kept, and at most 120 MB (10 minutes from this PC is about 19 MB).
- The History window lists them first ("Couldn't write these out") with **Try again** and
  **Delete**. Try again needs the running hub: it transcribes the recording with the current
  model, **never pastes** (the History window has the focus), writes a normal history record with
  `"recovered": true`, and deletes the recording when it got words.
- Nothing is kept while `[history] enabled = false` (the default).
- Bad or too-long audio (415/413) isn't kept: trying again wouldn't help.

## Retention, rotation and deletion

**Retention:** the history grows without limit, and there is no automatic rotation or expiry in
v1. Text-only records are small, typically under 1 KB each.

The hub opens the file only for the moment it appends a line. That means you can rotate or delete
it **while the hub is running**, and the next request creates a fresh file. The simplest way to
delete everything is **History > Clear history**.

```powershell
$home_ = "$env:APPDATA\shoutabout"

# Rotate: archive the current file with a date stamp
Rename-Item "$home_\history.jsonl" ("history-{0:yyyyMMdd}.jsonl" -f (Get-Date))

# Delete all text history (permanent)
Remove-Item "$home_\history.jsonl", "$home_\history-*.jsonl" -ErrorAction SilentlyContinue

# Delete kept audio older than 30 days
Get-ChildItem "$home_\audio" -File | Where-Object LastWriteTime -lt (Get-Date).AddDays(-30) | Remove-Item

# Stop recording history altogether: switch it off in Settings (applied at once)
```

## Privacy

- History is **off by default**: nothing you dictate is kept until you turn it on.
- Once on, it contains **everything you dictated**, both raw and cleaned. It stays on the PC in
  your user profile. ShoutAbout never uploads it.
- `%APPDATA%` is **Roaming**. On a Home edition PC without a domain nothing roams, but in a
  managed or domain profile the file could sync to a server. Disable history or move the config
  dir (`SHOUTABOUT_HOME`) if that matters.
- Backup and sync tools (OneDrive folder backup, File History) may copy `%APPDATA%`, depending on
  how they are configured.
- `hub.log` never contains transcript text. Request lines have only IDs, sizes and timings, and
  cleanup fallbacks log the reason, not the text.

## Example queries

### PowerShell (Windows PowerShell 5.1)

Load the file once. The `-Encoding UTF8` matters, because 5.1 otherwise reads ANSI:

```powershell
$h = Get-Content "$env:APPDATA\shoutabout\history.jsonl" -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json }
```

Average and median hub-side latency, LAN vs tunnel:

```powershell
$h | Group-Object route | ForEach-Object {
  $t = @($_.Group | ForEach-Object { $_.timings_ms.total } | Sort-Object)
  [pscustomobject]@{
    route        = $_.Name
    n            = $t.Count
    avg_total_ms = [math]::Round(($t | Measure-Object -Average).Average)
    median_ms    = $t[[int][math]::Floor(($t.Count - 1) / 2)]
    avg_audio_s  = [math]::Round(($_.Group | Measure-Object audio_seconds -Average).Average, 1)
  }
} | Format-Table -AutoSize
```

> `timings_ms.total` is hub-side processing, so it should be about the same on both routes. The
> real LAN vs tunnel difference is network time, which needs a phone-side measurement (see
> [manual-test-checklist.md](manual-test-checklist.md#8-latency-and-resources)).

Stage breakdown for dictations of about 15 s:

```powershell
$h | Where-Object { $_.audio_seconds -ge 12 -and $_.audio_seconds -le 18 } |
  Select-Object ts, route, audio_seconds,
    @{n='decode';e={$_.timings_ms.decode}}, @{n='stt';e={$_.timings_ms.stt}},
    @{n='cleanup';e={$_.timings_ms.cleanup}}, @{n='paste';e={$_.timings_ms.paste}},
    @{n='total';e={$_.timings_ms.total}} | Format-Table -AutoSize
```

List cleanup fallbacks, and count them by reason:

```powershell
$h | Where-Object cleanup_fallback | Select-Object ts, route, cleanup_reason | Format-Table -AutoSize
$h | Where-Object cleanup_fallback | Group-Object cleanup_reason | Sort-Object Count -Descending | Format-Table Count, Name
```

PC-mode requests that weren't pasted:

```powershell
$h | Where-Object { $_.target -eq 'pc' -and -not $_.pasted } | Select-Object ts, warning | Format-Table -AutoSize
```

Where cleanup actually changed the text (good material for vocab and guard tuning):

```powershell
$h | Where-Object { $_.cleanup -eq 'applied' -and $_.text -ne $_.raw_text } |
  Select-Object -Last 20 raw_text, text | Format-List
```

Today only:

```powershell
($h | Where-Object { ([datetime]$_.ts).Date -eq (Get-Date).Date } | Measure-Object).Count
```

(`[datetime]$_.ts` works whether `ConvertFrom-Json` returned a string, as in 5.1, or a DateTime,
as in PowerShell 7.)

### jq (Git Bash, or PowerShell 7.3+)

`jq` is available via `winget install jqlang.jq`. Windows PowerShell 5.1 strips the embedded
double quotes from native-command arguments, so run these from Git Bash. In 5.1 you would have
to escape them as `\"`.

```bash
H="$APPDATA/shoutabout/history.jsonl"

# average hub-side total latency per route
jq -s 'group_by(.route) | map({route: .[0].route, n: length, avg_total_ms: (map(.timings_ms.total) | add / length | round)})' "$H"

# fallbacks with reasons
jq -c 'select(.cleanup == "fallback") | {ts, route, cleanup_reason}' "$H"

# fallback counts by reason
jq -s 'map(select(.cleanup_fallback)) | group_by(.cleanup_reason) | map({reason: .[0].cleanup_reason, n: length}) | sort_by(-.n)' "$H"

# STT real-time factor (ms of STT per second of audio)
jq -c 'select(.audio_seconds > 0) | {id, audio_seconds, stt_ms_per_s: (.timings_ms.stt / .audio_seconds | round)}' "$H"
```

## Word suggestions, and a future correction UI

While it's on, the history feeds **word suggestions**:
- `learn.py` mines it for recurring names, mixed-case terms and words the cleanup re-cased. With
  history off, only project folders are scanned (the tray's daily refresh, **Rescan** and
  `vocab suggest` all leave `history.jsonl` alone).
- **Teach ShoutAbout…** (in the tray) shows the suggestions, and you approve each one before it
  joins `vocab.txt` ([vocab.md](vocab.md)).

A fuller correction tool is still planned. It would:
- show `raw_text` next to `text`
- let you fix the words Whisper or the cleanup got wrong
- add recurring fixes to `vocab.txt` (in priority order)
- with `keep_audio = true`, re-run the corrected recordings as a regression set (`transcribe`
  with a `manifest.json`)

Keep the schema stable: add fields rather than renaming or removing them, so old history files
stay readable.
