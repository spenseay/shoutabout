# Troubleshooting

Problems are grouped by area. Each entry gives the symptom, then **Cause** and **Fix**.

The PowerShell commands assume you are in the repo folder and use Windows PowerShell 5.1. They
call `curl.exe`, not `curl` (see [PowerShell gotchas](#powershell-gotchas)). Run the CLI as
`.\.venv\Scripts\python.exe -m shoutabout ...` from the repo folder. A bare `py` or `python`
starts the system Python, which doesn't have ShoutAbout installed.

The hub's default port is **8765**. If setup ran with `-Port` (for example **8766** when 8765 is taken), use
your port wherever this page shows 8765. `.\.venv\Scripts\python.exe -m shoutabout info` shows
the real `port` and `lan_url`. Snippets below that need them read them like this:

```powershell
$info = .\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json
$port = $info.port
$lan = $info.lan_url      # e.g. http://192.168.0.23:8766
```

## Start here

```powershell
.\.venv\Scripts\python.exe -m shoutabout doctor             # every check, with fix hints
.\.venv\Scripts\python.exe -m shoutabout info               # paths, LAN URL, models (no secrets)
Get-Content "$env:APPDATA\shoutabout\logs\hub.log" -Tail 60 -Wait     # live hub log
Get-Content "$env:APPDATA\shoutabout\logs\stdout.log" -Tail 60        # stray output under pythonw
```

- Each request logs one line: `transcribe id=... route=... target=... pasted=... cleanup=... total=...`.
- Each rejection logs one line: `rejected route=... ip=... status=... reason=...`.
- A cleanup fallback logs one line with the reason: `cleanup fell back to the raw transcript: ...`.
- ShoutAbout keeps no history: tray > **Recent…** has the text of the last 3 dictations
  ([history.md](history.md)).
- `scripts\test-routes.ps1` runs scripted `curl.exe` checks against each route.
- To watch the hub in a console, stop the task first (only one hub may run), then run it in the
  foreground:

```powershell
Stop-ScheduledTask -TaskName 'ShoutAbout Hub'
.\.venv\Scripts\python.exe -m shoutabout run --no-tray     # Ctrl+C to stop
Start-ScheduledTask -TaskName 'ShoutAbout Hub'             # back to normal
```

---

## GPU, model and hub startup

### "cublas64_12.dll is not found" / "Library cublas64_12.dll is not found or cannot be loaded"

**Cause:** the cuBLAS wheel is missing, or its `bin` folder isn't on the DLL search path.
- The most common reason is running a plain `uv sync`. It does an exact sync and **removes** the
  `cuda` extra.

**Fix:**
1. Run `uv sync --extra cuda`, then `.\.venv\Scripts\python.exe -m shoutabout doctor`. The CUDA check loads the model.
2. Confirm the DLLs exist with
   `Get-ChildItem .\.venv\Lib\site-packages\nvidia\cublas\bin`. You should see
   `cublas64_12.dll` and `cublasLt64_12.dll`.
3. Update the NVIDIA driver. Any current Game Ready or Studio driver supports CUDA 12, and
   `nvidia-smi` should list the GPU.
4. Don't install `nvidia-cublas-cu13`: CTranslate2 4.x is a CUDA 12 build. cuDNN is not needed.
5. Stopgap: set `[stt] cpu_fallback = true`. Transcription will run on the CPU and be much slower.

### "CUDA out of memory" (hub log or `transcribe`)

**Cause:** other programs already hold VRAM. Common culprits:
- games or other GPU apps
- **other Ollama models still resident**: large local models such as `qwen3.5:27b` take most
  of a 16 GB card
- a second hub or `transcribe` process

**Fix:**
1. Check what is using VRAM: `nvidia-smi` and `ollama ps`.
2. Unload models you don't need: `ollama stop <model>`.
3. Expect about 2-3 GB for Whisper and about 3 GB for the default cleanup model.
4. If VRAM is still tight, use a smaller cleanup model (`granite4:micro`), set
   `[cleanup] enabled = false`, or set `[stt] compute_type = "int8_float16"`.
5. Restart the hub.

### Model download fails (first run, `download-model`)

**Cause:**
- no access to `huggingface.co` (proxy, firewall, captive portal)
- not enough disk space: about 1.6 GB is needed in `%USERPROFILE%\.cache\huggingface\hub`
- Hugging Face rate limiting
- TLS inspection by security software

**Fix:**
1. Run `.\.venv\Scripts\python.exe -m shoutabout download-model` in a console to see the actual error.
2. If you're behind a proxy, set `HTTPS_PROXY` for your user.
3. Free some disk space.
4. Retry later.
5. Set `[stt] download_root` to a folder on a drive with space, then download again.
6. A warning about symlinks on Windows is harmless; files are copied instead.

### Hub not running: tray icon red/error, or no tray icon at all

**Cause:** a config error, a missing token, a failed engine load, the port already in use, or the
task not starting.

**Fix:**
1. Read `hub.log` and `stdout.log` (paths above). Typical lines:
   - `can't start: ... invalid config` means an unknown key or a bad value, for example a public
     range in `lan_networks`. Fix `config.toml`.
   - `hub token not found` means you should run `.\.venv\Scripts\python.exe -m shoutabout init`.
   - `Speech engine failed to load: ...` means CUDA trouble (see above).
   - `Couldn't listen on port 8765` (or your port) means the port is in use (see the next
     entry).
   - `another ShoutAbout hub is already running; exiting` means a second copy was started.
     Quit the first one from the tray, or check `Get-Process pythonw`.
2. Check the scheduled task:
   ```powershell
   Get-ScheduledTask -TaskName 'ShoutAbout Hub' | Get-ScheduledTaskInfo   # LastRunTime, LastTaskResult
   Get-ScheduledTask -TaskName 'ShoutAbout Hub' | Select-Object State
   Start-ScheduledTask -TaskName 'ShoutAbout Hub'
   ```
   `LastTaskResult` 0 means OK, and `267009` (0x41301) means it is still running. Any other
   value means the hub exited with an error, so read `hub.log`.
3. Look in the tray overflow (the `^` arrow) before deciding the icon is missing.
4. If the log says the hub runs **elevated**, the task was registered with "Run with highest
   privileges". Re-run `setup.ps1`: the task must be RunLevel Limited.

### Port 8765 (or your port) already in use

```powershell
$port = (.\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json).port
Get-NetTCPConnection -LocalPort $port -State Listen | Select-Object LocalAddress, OwningProcess
Get-Process -Id (Get-NetTCPConnection -LocalPort $port -State Listen).OwningProcess
```

**Fix:**
- If the owner is another `pythonw.exe` or `python.exe` hub, quit it.
- If it's an unrelated program (SETUP.md §3 notes the one found on this PC during the build),
  either stop that program or move the hub: re-run setup with `-Port <free
  port>`, e.g. `-Port 8766`. That updates `[server] port` and the firewall rule. Then update the
  tunnel route's Service URL (`http://127.0.0.1:<port>`) and the Shortcut's LAN URL by hand.

### `503` "hub is starting" (or a CUDA error message)

**Cause:** the engine thread is still loading Whisper, or the load failed.
- Loading takes about 2 s from cache. The first run first downloads about 1.6 GB.
- The `detail` field of the 503 gives the reason.

**Fix:** wait for the tray to show idle. If the detail is an error, see the CUDA entries above.

### `503` "Windows Media Foundation isn't available on this PC"

**Cause:** ShoutAbout decodes phone recordings (AAC `.m4a`) with Windows Media Foundation,
which is part of Windows. Windows "N" editions (for example "Windows 11 Pro N") ship without
it until Microsoft's Media Feature Pack is added. Plain WAV still works, and "Talk to this PC"
doesn't need it.

**Check:** `ShoutAbout.exe doctor` (or `python -m shoutabout doctor`) shows
`[FAIL] Audio decoding`. `winver` shows the edition.

**Fix:** Settings > Apps > Optional features > Add a feature (View features) > **Media
Feature Pack**, install it, restart the PC, then dictate again.

### The first dictation after a reboot is slow, or its cleanup fell back

**Cause:**
- The model warm-up hasn't finished yet.
- The Ollama model was cold. Loading it takes 1-3 s, which is longer than the 2 s cleanup
  timeout, so that request used the raw transcript.

**Fix:**
- Wait until the tray shows idle. The hub warms Whisper at startup, and the warmer loads the
  Ollama model and re-checks it every `warm_interval_s` (300 s).
- `ollama ps` should show the model with `UNTIL` = `Forever` (from `keep_alive = -1`).
- Ollama itself must start at logon. The Ollama app does this by default.

---

## Phone can't reach the hub on the LAN

First, test from the **iPhone**: open Safari on the same Wi-Fi and go to `lan_url` + `/health`,
e.g. `http://192.168.0.23:8765/health` (or the port you chose with `-Port`, e.g. 8766 on this
PC — check `.\.venv\Scripts\python.exe -m shoutabout info`). You should see `{"ok":true}`.

A test from the PC itself doesn't prove much: traffic to your own IP doesn't pass the inbound
firewall the same way. The command below only shows that the hub is listening:

```powershell
$lan = (.\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json).lan_url
curl.exe -s "$lan/health"
```

### Safari / Shortcut: "Could not connect to the server" or a timeout

Check these in order.

1. **The hub isn't running.** See above.
2. **The PC is asleep or hibernating.** Nothing answers, and v1 has no Wake-on-LAN. Set
   Settings > System > Power > Screen and sleep > "Make my device sleep after" to Never while
   plugged in, or accept that dictation needs the PC awake.
3. **The Wi-Fi profile is Public.** The firewall rule applies to the **Private** profile only.
   ```powershell
   Get-NetConnectionProfile | Select-Object InterfaceAlias, Name, NetworkCategory
   ```
   Fix it in Settings > Network & internet > Wi-Fi > (your network) > Network profile type:
   **Private**. Or run this elevated:
   `Set-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -NetworkCategory Private`.
4. **The firewall rule is missing or disabled.**
   ```powershell
   Get-NetFirewallRule -Group 'ShoutAbout' | Format-Table Name, Enabled, Profile, Direction, Action
   ```
   Expect `ShoutAbout-Hub-TCP-8765` (or the port you chose with `-Port`, e.g.
   `ShoutAbout-Hub-TCP-8766` on this PC — check `.\.venv\Scripts\python.exe -m shoutabout info`)
   plus the `-python` and `-pythonw` program rules, all `Enabled = True`, `Profile = Private`,
   `Action = Allow`. A rule for a different port than `info` shows means the hub moved without
   re-running setup. If any is missing or wrong, re-run `setup.ps1`.
5. **Block rules for Python, created when someone clicked Cancel on the "Allow Python" prompt.**
   Block rules beat allow rules. This lists them without admin:
   ```powershell
   Get-NetFirewallApplicationFilter | Where-Object Program -like '*\python*.exe' | Get-NetFirewallRule |
     Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } |
     Format-Table DisplayName, Profile, Enabled
   ```
   This removes them, and **needs an elevated PowerShell**:
   ```powershell
   Get-NetFirewallApplicationFilter | Where-Object Program -like '*\python*.exe' | Get-NetFirewallRule |
     Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | Remove-NetFirewallRule
   ```
6. **Microsoft Store Python.** Firewall rules for it point at a versioned `WindowsApps` path that
   changes on every update. Use the python.org installer and re-run setup.
7. **A VPN on the phone or the PC.**
   - A VPN on the phone may send LAN traffic into the tunnel.
   - A VPN on the PC can change the default route, so the detected LAN IP is wrong.
   - Disconnect the VPN, or allow LAN access in the VPN client.
8. **Guest Wi-Fi or AP/client isolation.** Many guest networks and some mesh systems block
   device-to-device traffic. Put the phone and PC on the main network, or turn off client
   isolation.
9. **Wrong IP.**
   - Run `.\.venv\Scripts\python.exe -m shoutabout info` to get the current URL. Check the
     port in it too: it must match the Shortcut's `LanURL`.
   - DHCP can hand out a new address. Create a **DHCP reservation** on the router, then update
     the Shortcut.
   - If the hub shows the address of the wrong adapter (Hyper-V, WSL or VPN), set
     `[server] advertise_host`.
10. **iOS Local Network permission.** Check Settings > Privacy & Security > Local Network, and
    allow Shortcuts if it's listed.
11. **Not a cause: the iPhone's "Private Wi-Fi Address".** It randomises the phone's MAC
    address, not anything the hub checks. You can leave it on.
12. **`403 {"detail":"forbidden"}` instead of a timeout** means the phone's source address isn't
    in `[server] lan_networks`. That happens if you narrowed the list, or if you use Tailscale
    without adding `100.64.0.0/10`. The hub log shows `reason=source-not-allowed` with the IP.

### Shortcut always takes the tunnel (or always the LAN) branch

**Cause:**
- **Get Network Details** returned an empty Wi-Fi name, because iOS hides network names without
  Location permission.
- Or the name in the Shortcut doesn't match exactly. The comparison is case- and
  space-sensitive. `make-shortcuts` prints the name it used; rebuild with
  `--ssid "<home-wifi-name>"` if it's wrong.

**Fix:**
1. Go to Settings > Privacy & Security > Location Services > Shortcuts, choose **While Using the
   App**, and turn **Precise Location** on.
2. Add a temporary **Show Result** of the network name to see what iOS returns.
3. Alternative: use **Get Current IP Address** (Local, IPv4) and check whether it begins with
   your LAN prefix, e.g. `192.168.1.`.
4. Details are in [ios-shortcuts.md](ios-shortcuts.md).

---

## Cloudflare tunnel route

Test from a PC that is *not* relying on the LAN path. Any machine works, because Access, not
the source address, protects the public hostname.

The easiest check is `scripts\test-routes.ps1`. It prompts for the service token (the secret
with hidden input) and keeps the secrets off curl's command line:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-routes.ps1 -PublicUrl https://dictate.shoutabout.fyi
```

By hand, put the service token in the two environment variables first. They last only for this
PowerShell window, and `doctor` and `test-routes.ps1` both read them:

```powershell
$env:SHOUTABOUT_CF_CLIENT_ID = Read-Host "CF Client ID"
$sec = Read-Host "CF Client Secret" -AsSecureString
$env:SHOUTABOUT_CF_CLIENT_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

# Without a service token: expect 302 (to the Access login) or 401/403, never 200
curl.exe -s -o NUL -w "%{http_code}`n" https://dictate.shoutabout.fyi/health

# With the service token: expect 200 {"ok":true}
curl.exe -s -H "CF-Access-Client-Id: $env:SHOUTABOUT_CF_CLIENT_ID" -H "CF-Access-Client-Secret: $env:SHOUTABOUT_CF_CLIENT_SECRET" https://dictate.shoutabout.fyi/health

# The whole chain, with fix hints (reads the token from the environment variables)
.\.venv\Scripts\python.exe -m shoutabout doctor
```

The second `curl.exe` puts the secret on its command line, which shows briefly in the process
list while curl runs. Afterwards, clear the variables with
`Remove-Item Env:SHOUTABOUT_CF_CLIENT_ID, Env:SHOUTABOUT_CF_CLIENT_SECRET`, or close the window.

### `302` redirect to `<team>.cloudflareaccess.com`, or the Shortcut gets an HTML login page

**Cause:** Access didn't accept the request as service auth. One of these:
- the `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers are missing or misspelled
- a value has a stray space
- the token expired or was revoked
- the application's policy action isn't **Service Auth**, or doesn't include this token

**Fix:**
1. Re-copy the Client ID and Secret into the Shortcut's tunnel branch. Use
   `.\.venv\Scripts\python.exe -m shoutabout qr --clipboard` to move them by QR code.
2. In Zero Trust, check the Access application's policy: Action **Service Auth**, Include
   **Service Token** = your token.
3. Check the token's expiry under Access controls > Service credentials.
4. Turn on the app's "401 Response for Service Auth policies" option, so failures return 401
   instead of a login page. The Shortcut then shows a clear error, not HTML.

### `401` from Access (with the 401 option, or strict service-token mode)

**Cause:** the same service-token problems as above. You can tell it isn't the hub because the
body is not the hub's JSON `{"detail": "missing or invalid bearer token"}`.

**Fix:** as above. If `hub.log` shows nothing for the request, it never reached the PC.

### `403` from the hub: `{"detail": "Cloudflare Access JWT rejected: ..."}` or `"missing Cloudflare Access JWT"`

**Cause:** the request passed Access, but the hub's JWT check failed. `hub.log` gives the reason,
for example `rejected route=tunnel ip=127.0.0.1 status=403 reason=jwt-invalid (Access JWT
audience mismatch)`.

| Log reason | Fix |
|---|---|
| `audience mismatch` | `[access] aud` must be the application's **AUD tag**: Access controls > Applications > Configure > Additional settings. |
| `issuer mismatch` | `[access] team_domain` must be your team domain from Zero Trust > Settings, e.g. `yourteam.cloudflareaccess.com`. |
| `couldn't fetch Cloudflare Access signing keys` | The PC can't reach `https://<team_domain>/cdn-cgi/access/certs`. Check the team domain spelling and the PC's internet access. |
| `signing key not found` / `signature invalid` | Usually the wrong team domain (keys from another team). Keys rotate every 6 weeks and are re-fetched automatically. |
| `expired` / `not yet valid` | The PC clock is off by more than 60 s. Run `w32tm /resync`, or enable automatic time. |
| `not issued to an allowed service token` | `[access] allowed_client_ids` doesn't contain this token's Client ID. Add it or empty the list. |
| `jwt-missing` | Access isn't in front of this hostname or path, or a Bypass policy applies. Check the Access application's public hostname. |

Restart the hub after editing `config.toml`.

### `403 {"detail":"forbidden"}` on the public URL

**Cause:** the hub saw a non-loopback, non-private source address. This shouldn't happen through
cloudflared, which connects from `127.0.0.1`. It means the tunnel route's service URL points at
something other than `http://127.0.0.1:<port>`, for example the PC's public IP or another proxy.

**Fix:** set the route's service to `http://127.0.0.1:8765` (or the port you chose with
`-Port`, e.g. `http://127.0.0.1:8766` on this PC — check
`.\.venv\Scripts\python.exe -m shoutabout info`).

### `502` Bad Gateway, `530`, or "Error 1033"

**Cause:**
- `1033` / `530`: the tunnel has no healthy connector, so cloudflared isn't running or isn't
  connected.
- `502`: cloudflared is connected but can't reach the hub. The hub may be down, or the route
  points at the wrong port or scheme (`https://` instead of `http://`, or `localhost` resolving
  to `::1`). A route still on `8765` after the hub moved to another port (e.g. 8766) gives a 502,
  or reaches whatever else listens on 8765.

**Fix:**
```powershell
Get-Service cloudflared            # Running?
Restart-Service cloudflared        # elevated
```
1. In the Cloudflare dashboard (Networking > Tunnels), check that the tunnel is **Healthy**.
2. Check that the published application route for `dictate.shoutabout.fyi` points to
   `http://127.0.0.1:8765` (or the port you chose with `-Port`, e.g. 8766 on this PC — check
   `.\.venv\Scripts\python.exe -m shoutabout info`).
3. Check that the hub is up, from the PC:
   ```powershell
   $port = (.\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json).port
   curl.exe -s "http://127.0.0.1:$port/health"
   ```

### `524` A timeout occurred

**Cause:** the hub took more than 125 s to answer. Cloudflare's proxy read timeout can't be
raised below the Enterprise plan. A very long recording on a busy GPU, or a request queued behind
another one, can do this.

**Fix:**
- Keep dictations under about 10 minutes.
- Make sure nothing else is saturating the GPU.
- Check `hub.log` for stuck requests.

### Shortcut gets an HTML challenge page, or "Get Dictionary Value" fails only on cellular

**Cause:** Bot Fight Mode, Browser Integrity Check or a WAF rule challenged the Shortcuts
request. Shortcuts can't solve challenges.

**Fix:**
1. Look under Security > Events for the hostname to see what fired.
2. Turn off Bot Fight Mode, or the specific rule, for the zone, or add a skip rule for
   `dictate.shoutabout.fyi` where your plan allows one.
3. The request is already authenticated by Access, so a challenge adds nothing.

### `413` through the tunnel only

**Cause:** Cloudflare's upload limit is 100 MB on Free and Pro. Normal-quality recordings are far
smaller. A `413` from the hub itself comes with a JSON `detail` (see below).

---

## HTTP errors from the hub

All hub errors are JSON `{"detail": "..."}`, and `hub.log` has a matching `rejected ...` line.

| Status | Detail / log reason | Cause and fix |
|---|---|---|
| 400 | `missing 'file' upload` / `no-file` | A form upload without a `file` part. Just before the `rejected` line, `hub.log` shows `no file upload; content-type='application/x-www-form-urlencoded' form fields: ...`: iOS silently dropped the File field of a **Form** request body. Import a fresh Dictate from `make-shortcuts`, or set **Request Body** to **File** = Recorded Audio ([ios-shortcuts.md Appendix A.6](ios-shortcuts.md#a6-send-it-two-request-actions)). |
| 400 | `empty upload` / `no-file` | A raw-body upload with no bytes: the Shortcut sent an empty variable instead of the Record Audio output. |
| 401 | `missing or invalid bearer token` / `bad-bearer` | The Shortcut's `Authorization` header is wrong. It must be exactly `Bearer <token>`. Common slips: a missing "Bearer ", a stray space or newline, or an old token after `init --rotate-token`. Re-copy with `.\.venv\Scripts\python.exe -m shoutabout token --qr` or Settings > Copy hub token (Shortcuts), and paste it into Dictate's **Hub token** Text action, which both branches use (in a hand-built Dictate, the `HubToken` Text action). |
| 403 | `forbidden` / `source-not-allowed` | The source IP is neither loopback nor in `lan_networks` (see the LAN section). |
| 403 | `Cloudflare Access JWT ...` | See the tunnel section. |
| 413 | `upload too large (max 25 MB)` or an audio-too-long message | Over `max_upload_mb` (25) or `max_audio_seconds` (600). Raise them in `config.toml` if you really dictate longer. |
| 415 | `unsupported or unreadable audio` | The upload isn't decodable audio. Usually the Shortcut sent the wrong variable (not the Record Audio output). The decoder's reason is in `hub.log` (`audio rejected: ...`). The installed app decodes m4a/AAC, MP3, WAV and FLAC (also WMA, WebM and AMR where Windows has the codec), not Ogg, AIFF or CAF. |
| 415 | `send multipart/form-data, or the audio itself as the request body` / `bad-content-type` | A raw body whose `Content-Type` isn't audio, e.g. `application/json` or `text/plain`: **Request Body** is set to JSON or Text instead of **File**. Raw uploads must be `audio/*`; `video/*`, `application/octet-stream` and no content type are accepted too. |
| 422 | `target must be 'pc' or 'phone'` | `?target=` in the URL (or the form field `target`) holds something else. Missing or empty is fine and means `pc`. Dictate sends the text of `dictation-target.txt`, so check that the file in Files > iCloud Drive > Shortcuts holds just `pc` or `phone`. Run **Dictation Target** to rewrite it, or delete it to go back to PC mode. |
| 429 | `rate limited; try again shortly` | More than 30 requests a minute from one source (`rate_limit_per_minute`). Wait a minute. |
| 429 | `too many failed authentication attempts` | 10 failed bearer/JWT attempts in 10 minutes from one source. Fix the credential, then wait up to 10 minutes or restart the hub, which clears the counters. On the LAN the source is the client's IP. Tunnel traffic all arrives from `127.0.0.1`, so there the hub keeps the counters per remote client, using the `Cf-Connecting-IP` header Cloudflare sets: a misconfigured client locks out only its own IP, not the owner's phone elsewhere. (The header is used only for this bucketing, never for routing or auth.) |
| 503 | `hub is starting` or an engine error | See [503](#503-hub-is-starting-or-a-cuda-error-message). |
| 503 | `Windows Media Foundation isn't available on this PC ...` | A Windows "N" edition without the Media Feature Pack: see [that entry](#503-windows-media-foundation-isnt-available-on-this-pc). |
| 500 | `internal error` | A bug. The traceback is in `hub.log`. |

Testing `/transcribe` from the PC: use the **LAN IP**, not `127.0.0.1`. Loopback is the tunnel
route, and once `[access]` is configured, loopback requests without an Access JWT are rejected
(403) by design.

```powershell
$lan = (.\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json).lan_url
$tok = (Get-Content "$env:APPDATA\shoutabout\hub_token.txt" -Raw).Trim()
curl.exe -s -H "Authorization: Bearer $tok" -F "file=@test-audio\plain-01.m4a" -F "target=phone" "$lan/transcribe"
# The same audio as a raw body, the way the iPhone Shortcut sends it:
curl.exe -s -H "Authorization: Bearer $tok" -H "Content-Type: audio/x-m4a" --data-binary "@test-audio\plain-01.m4a" "$lan/transcribe?target=phone"
```

(`scripts\make_test_audio.ps1` creates `test-audio\`; use any file name from there.)

---

## Shortcut errors

| Symptom | Cause | Fix |
|---|---|---|
| "Could not connect to the server" | The URL is unreachable from the current network | See the LAN section above, or check that the branch logic sent the tunnel URL when you're away. |
| "The request timed out" | The PC is asleep, the hub is hung, or the upload is slow on a weak signal | Wake the PC, check `hub.log`, retry on a better connection. |
| "The network connection was lost" on POST | Empty or malformed header rows in Get Contents of URL | Delete blank header rows. Keys must be exactly `Authorization`, `CF-Access-Client-Id` and `CF-Access-Client-Secret`. |
| Get Dictionary Value fails, or "the input is not a dictionary" | The response wasn't JSON: an Access login page, a Cloudflare error or challenge page, or a captive portal | Find out which with a temporary Show Result of the raw response, then use the Cloudflare section. |
| "Please choose a value for each parameter in this action" | An older or hand-built Dictate. Its Show Notification body can end up empty (for example just a `warning` variable, which is empty on success), or it checks the reply with "has any value" or compares a raw Dictionary Value, so the notification runs when it shouldn't. | Import a fresh Dictate from `make-shortcuts` ([ios-shortcuts.md §3](ios-shortcuts.md#3-make-the-shortcut-files-on-the-pc)). By hand: convert `status` with Get Text from Input, notify only when it isn't `ok`, and start the body with literal text ([Appendix A.7](ios-shortcuts.md#a7-handle-the-answer)). |
| A **Dictation problem** notification | The hub's reply didn't have `status` `ok`. The text after "Check the PC." is the hub's `warning` (the dictation worked, but e.g. `Hub is paused — not pasted on the PC.`, an admin window, a locked PC, or `No speech detected.`) or its error `detail` (e.g. `missing or invalid bearer token`). Nothing after "Check the PC." means the reply wasn't the hub's JSON: Cloudflare Access, an error or challenge page, or a captive portal. | Fix what it names: the [HTTP errors](#http-errors-from-the-hub) table covers each `detail`. For an empty one, see the [Cloudflare section](#cloudflare-tunnel-route). If *every* dictation shows it, the hub may be older than the `status` field: update it. |
| "Allow ... to connect to ...?" every time | The privacy prompt was answered with Allow Once | Tap **Always Allow**, or review it in the shortcut's Details > Privacy. Each host (LAN IP, public hostname) asks once. |
| The Action Button does nothing while locked | Some actions need the phone unlocked | Unlock (Face ID usually does it at a glance) or test step by step. See [ios-shortcuts.md](ios-shortcuts.md). |
| The mode never changes | Dictation Target wrote a numbered duplicate (`dictation-target 2.txt`) or `dictation-target.txt.txt` | Turn on **Overwrite If File Exists** in Save File, check the Subpath, and delete the stray files under Files > iCloud Drive > Shortcuts. |

### `make-shortcuts` problems

| Message | Cause and fix |
|---|---|
| `couldn't read this PC's Wi-Fi name` | The PC is on Ethernet, Wi-Fi is off, or Windows withholds Wi-Fi names from `netsh` without Location access. Pass the name yourself: `--ssid "<home-wifi-name>"`, exactly as the iPhone shows it in Settings > Wi-Fi. |
| The printed Wi-Fi name isn't the phone's home network | The PC uses a different network, e.g. the 5 GHz name while the phone uses the 2.4 GHz one. Rebuild with `--ssid`. |
| `no LAN URL` | `[server] host` is loopback-only, or no private IPv4 address was found. Pass `--lan-url http://<PC-LAN-IP>:<port>/transcribe`, or set `[server] advertise_host`. |
| `Public URL: none (no public_hostname in install-state.json)` | Setup ran without `-PublicHostname`, so Dictate is LAN-only. Pass `--public-url https://<host>/transcribe` to add the tunnel route. |
| `signing 'Dictate' failed: ...` | HubSign couldn't be reached, refused the request, or returned something that isn't a signed shortcut. `HTTP 302, redirecting to https://www.routinehub.co/...` means HubSign didn't accept the request at all. Nothing is written. Try again later, or use `--no-sign` and sign the `.plist` files on a Mac with `shortcuts sign --mode anyone`. |
| iOS won't open a `.plist` file | Unsigned files from `--no-sign` can't be imported. Sign them on a Mac first, or run `make-shortcuts` without `--no-sign`. |

---

## Paste problems (PC mode)

`pasted: true` means the hub sent the paste chord to the foreground window. It does **not** mean
the app inserted the text: no app reports that back. The text is always in the HTTP response
(so the Shortcut can show it) and in tray > **Recent…**.

### Text pasted into the wrong window, or nowhere

**Cause:** focus changed between when you started dictating and when the paste landed. Or no
window had focus (desktop, taskbar, just after closing a window). With no foreground window at
all, the hub doesn't paste: it leaves the text on the PC clipboard and returns `pasted: false`
with the warning `No focused window on the PC (locked?) — text is on your clipboard.`

**Fix:** click into the target before you finish talking. The hub pastes into whatever is in the
foreground at paste time. If you got the warning, click where the text should go and press
Ctrl+V.

### The PC is locked, or the screen is off and locked

**Cause:** before pasting, the hub checks which desktop is receiving input. While the PC is
locked, or the UAC secure desktop (a consent prompt) is showing, that isn't your normal desktop,
so the hub doesn't paste. It leaves the text on the PC clipboard and returns `pasted: false`
with the warning `The PC is locked — text is on your clipboard.` The phone shows that warning
(with ⚠️) and also has the text on its clipboard. No PC toast is shown.

**Fix:** unlock (or answer the UAC prompt), click into the target and press Ctrl+V. Or use Phone
mode while away from the desk.

### A held modifier key garbles the paste

**Cause:** keys you are physically holding (Shift, Ctrl, Alt, Win) combine with the synthetic
Ctrl+V. Shift+Ctrl+V means "paste as plain text" in browsers, and Win+V opens clipboard history.

**Fix:** don't hold modifier keys when the dictation lands. If the paste was lost, copy the text
from the phone notification, or from tray > **Recent…**.

### Nothing happens in Git Bash, MSYS2, Cygwin (mintty) or PuTTY

**Cause:** these terminals don't paste on Ctrl+V by default.

**Fix:** the hub sends **Shift+Insert** to processes in `[paste] shift_insert_processes` (the
defaults are `mintty.exe`, `putty.exe` and `kitty.exe`). For another terminal, find its exe name
under Task Manager > Details, add it to the list, and restart the hub.

### Windows Terminal asks "You are about to paste text that is longer than 5 KiB"

**Cause:** Windows Terminal's `largePasteWarning`, which is on by default.

**Fix:** confirm the dialog, or set `"largePasteWarning": false` in Windows Terminal's
`settings.json`. Since output is always one paragraph, the multi-line warning never appears.

### Claude Code shows `[Pasted text #1 +N lines]`

**Cause:** this is expected. Claude Code collapses large pastes (over about 800 characters) into
a placeholder. Nothing is submitted, because the hub never presses Enter.

### Remote Desktop / VM window: the old clipboard gets pasted, or nothing

**Cause:** the focused window is an RDP client (`mstsc.exe`) or a VM console. Ctrl+V is forwarded
to the remote machine, and clipboard redirection is asynchronous. The hub may restore your old
clipboard before the remote side has fetched the transcript.

**Fix:**
- Raise `[paste] restore_delay_ms` (e.g. `1500`), or set `restore_clipboard = false`.
- Make sure clipboard redirection is enabled for the session.
- Hyper-V basic sessions don't share the clipboard at all.

### Games and anti-cheat protected windows

**Cause:**
- Exclusive-fullscreen games and anti-cheat drivers often ignore injected input (`SendInput`).
- Some run elevated (see [Admin windows](#admin-windows)).

**Fix:** not supported. Use Phone mode, or paste manually.

### A clipboard manager (Ditto, CopyQ, ClipboardFusion, ...) interferes

**Cause:** the manager rewrites or grabs the clipboard between the hub setting it and the paste.
- The hub marks transcripts with Windows' clipboard-exclusion formats, which well-behaved
  managers skip.
- If anything changes the clipboard before the restore, the hub deliberately **skips** restoring
  (by sequence number) rather than overwriting the newer content.

**Fix:** exclude `pythonw.exe` in the manager, or pause it, or turn off `restore_clipboard`.

### The previous clipboard wasn't restored

**Cause:**
- Something changed the clipboard during the 500 ms restore delay. The restore is skipped by
  design.
- `restore_clipboard = false`.
- The previous content used formats that can't be safely copied, such as GDI or metafile
  handles, OLE private data or delayed-render data from some apps. Plain text, rich text, HTML,
  images (DIB) and file lists are restored.

**Fix:** this is expected in those cases. Copy the item again.

---

## Admin windows

**Symptom:** toast "Couldn't paste into an admin window — text is on your clipboard." The phone
gets the same warning, and `pasted: false`.

**Cause:** the foreground window runs elevated, at a higher integrity level, for example an
"Administrator: PowerShell" window, Task Manager, regedit or an installer. Windows UIPI silently
drops input from the non-elevated hub, so the hub doesn't try. It leaves the transcript on the
clipboard instead.

**Fix:**
- Press Ctrl+V yourself.
- Or run the target app non-elevated.
- **Don't** run the hub elevated to "fix" this. Its threat model depends on it being a normal
  user process, and it logs a warning if elevated.

---

## Cleanup fell back to the raw transcript

The response shows `cleanup: "fallback"`, and `hub.log` has the reason:

```powershell
Select-String -Path "$env:APPDATA\shoutabout\logs\hub.log" -Pattern 'cleanup fell back' | Select-Object -Last 20
```

| Reason | Cause | Fix |
|---|---|---|
| `ollama unreachable: ConnectError` | Ollama isn't running | Start the Ollama app from the Start menu, or run `ollama serve`. Check with `curl.exe -s http://127.0.0.1:11434/api/version`. Make sure Ollama starts at logon. |
| `ollama error: HTTP 404 ... not found` | The model isn't pulled, or the tag has a typo | `ollama pull qwen3:4b-instruct-2507-q4_K_M` (the tag must match `[cleanup] model` exactly). |
| `timeout after 2 s` | The model was cold (just loaded, Ollama restarted, or unloaded by another app), it's running on the CPU, or the GPU is busy | Check `ollama ps`. The warmer reloads the model every 300 s. If it's on the CPU, see [Ollama is using the CPU](#ollama-is-using-the-cpu). If it happens often while warm, raise `timeout_s` a little (2.5-3) or use a smaller model. |
| `guard: change_ratio 0.02 > 0.00 (1 unexplained word edit(s))` | The model changed, dropped or added a word beyond the allowed fixes (filler removal, vocab spelling, punctuation/case) | This is the guard working: by default any such word change falls back to Whisper's raw text, which is already punctuated. If it rejects cleanups you'd accept, allow a little slack with `max_change_ratio = 0.05` (then at least one changed word is tolerated). |
| `guard: negation words changed` | The model dropped or added "not", "don't", "never", … | Always rejected, whatever the thresholds: it would flip the meaning. |
| `guard: similarity 0.52 < 0.60` | The output diverged from what was said | As above (`min_similarity`). Don't go much below 0.5. |
| `guard: length_ratio 1.40 outside [0.60, 1.25]` | The model added text (an answer or explanation) or dropped a lot | Usually a real answer-instead-of-cleanup. Keep the thresholds. |
| `guard: output contains a line break` / `markdown` / `a code block` / `looks like a list or heading` | The model answered or formatted instead of cleaning | The guard working as intended. Try `.\.venv\Scripts\python.exe -m shoutabout benchmark` to compare models. |
| `guard: empty output` | The model returned nothing | Check the model in `ollama run <model>`. Consider an alternate. |
| `transcript too long for cleanup (~N tokens > num_ctx 4096)` | Very long dictation | Accept the raw text, or raise `[cleanup] num_ctx` to 8192 (more VRAM; restart the hub). |
| `output truncated at num_predict N` | The model produced far more text than the input | An answer; keep the fallback. |
| `unexpected error: ...` | A bug | `hub.log` (DEBUG level has the traceback). |

`cleanup: "disabled"` means `[cleanup] enabled = false`. `cleanup: "skipped"` means the
transcript was empty.

To tune the guard, use
`.\.venv\Scripts\python.exe -m shoutabout benchmark --models <model> --runs 10`. It reports the guard accept rate and
flags outputs that answered instead of cleaning.

---

## Wrong words / vocabulary

### A name or term keeps coming out wrong

**Fix:**
1. Open tray > **Recent…**, select the wrong word in that dictation, press **Fix a word**,
   type the right spelling and press **Save**. Or add it to `vocab.txt` yourself: tray >
   **Teach words…** (or **Open vocab.txt** there), or `%APPDATA%\shoutabout\vocab.txt`.
   - One term per line, spelled and cased exactly as you want it, e.g. `Better Shell`.
   - Changes apply on the next request, with no restart.
2. **Order sets priority.** Put the terms that matter most at the top.
3. Check with a recording: `.\.venv\Scripts\python.exe -m shoutabout transcribe my-test.m4a` shows the raw and cleaned
   text. If the raw text is already right, cleanup didn't spoil it.

### The log says terms were dropped

**Cause:** the hint text exceeds `[stt] max_prompt_tokens` (200; Whisper's limit is 223). At
startup the hub logs `vocab: N terms in the prompt (T tokens), M dropped`, and it logs one
warning per reload when terms are dropped. Terms are dropped from the bottom.

**Fix:**
- Remove terms Whisper already gets right.
- Move important terms up.
- Don't raise the budget past the default: a longer prompt leaves fewer output tokens per 30 s
  window.

### Vocabulary words appear in the text that you never said

**Cause:** hallucination on silence or noise. Whisper can echo hotword or prompt text.

**Fix:** keep `[stt] vad_filter = true`, trim the vocab list to terms you really use, and avoid
long silent stretches at the end of recordings.

---

## Toasts and tray

### No toast notifications

**Cause:**
- Do Not Disturb or Focus is on: only priority apps show banners.
- Notifications are turned off in Settings > System > Notifications.
- Hub toasts are balloon-style banners that **aren't kept in Notification Center**, so a missed
  one is gone.

**Fix:** turn off Do Not Disturb, or check `hub.log`, where warnings are always logged. The phone
gets the same warning in the response.

### Tray icon missing after Explorer restarts

**Fix:** the hub re-adds the icon when Explorer broadcasts `TaskbarCreated`. If it's still
missing, check the overflow area, then restart the task.

---

## Transcripts show up in Win+V clipboard history

**Cause:** this shouldn't happen. Transcripts are written with the exclusion formats
(`ExcludeClipboardContentFromMonitorProcessing`, `CanIncludeInClipboardHistory = 0`,
`CanUploadToCloudClipboard = 0`) in the same clipboard session as the text.

**Fix:**
1. Check that `[paste] exclude_from_history = true` and restart the hub.
2. Third-party clipboard managers may ignore the exclusion formats (see above).
3. If a transcript still appears, note the time and the target app and report it. Then clear the
   history from Win+V (`...` > Clear all).

---

## Ollama is using the CPU

```powershell
ollama ps     # PROCESSOR should be "100% GPU"; "100% CPU" or "40%/60% CPU/GPU" is the problem
```

**Cause:**
- Not enough free VRAM: games, other GPU apps, or other Ollama models (by default up to 3 per GPU
  stay loaded).
- After a driver update, Ollama may fail to detect the GPU.

**Fix:**
1. `ollama stop <other-model>`, then let the warmer reload the cleanup model (or restart the hub).
2. Close other GPU-heavy apps.
3. Update Ollama and the NVIDIA driver, then quit and restart the Ollama app.
4. `doctor` also reports whether the cleanup model is on the GPU.

---

## PowerShell gotchas

### "running scripts is disabled on this system"

**Cause:** the execution policy is Restricted, which is the default for Windows PowerShell 5.1
on clients.

**Fix:** run the scripts with a process-scoped bypass. It changes no system setting:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

### "...is not digitally signed" / scripts blocked after downloading a zip

**Cause:** the Mark-of-the-Web on files downloaded from the internet, together with the
RemoteSigned policy.

**Fix:** unblock the zip **before** extracting it (`Unblock-File .\shoutabout.zip`), or unblock
the extracted scripts:

```powershell
Get-ChildItem -Recurse .\scripts | Unblock-File
```

### `curl` behaves strangely, or complains about `-H` / `-F` parameters

**Cause:** in Windows PowerShell 5.1, `curl` is an alias for `Invoke-WebRequest`.

**Fix:** always type `curl.exe`. The real curl ships with Windows 10 1803+ and Windows 11.
Likewise, `sc` is an alias for `Set-Content`, so use `sc.exe` for service commands.

### `ConvertFrom-Json` mangles non-ASCII text from ShoutAbout's `.json` files

**Cause:** in 5.1, `Get-Content` defaults to the ANSI code page.

**Fix:** use `Get-Content -Encoding UTF8 ...`.
