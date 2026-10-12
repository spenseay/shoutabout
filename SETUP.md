# ShoutAbout setup

> **Just want to use ShoutAbout on Windows?** Download and run
> `ShoutAbout-Setup-<version>.exe` instead: see [Download](README.md#download-windows) and
> [docs/installer.md](docs/installer.md). It needs no Python, terminal or config files. This
> guide is the **developer setup** from a clone of the repo, and it keeps working alongside the
> installer. Don't run both at once: they share the config folder and only one hub runs at a
> time.

This guide takes you from nothing to working dictation:

- press the iPhone Action Button, speak, tap stop;
- the text lands in the focused window on this PC (**PC mode**) or on the phone clipboard
  (**Phone mode**);
- it works at home over Wi-Fi and anywhere else through `https://dictate.shoutabout.fyi`.

Do the steps in order. Expect about an hour, most of it downloads and iPhone taps.

**Conventions**

- Run every command in a **normal Windows PowerShell** window (not "Run as administrator"),
  from the repo folder, unless a step says otherwise.
- The CLI is `.\.venv\Scripts\python.exe -m shoutabout <command>`. A bare `python` won't find
  it.
- In PowerShell, `curl` is an alias for something else, so always type **`curl.exe`**.
- `<PC-LAN-IP>` means this PC's Wi-Fi address. Get it from
  `.\.venv\Scripts\python.exe -m shoutabout info` (`lan_ip` / `lan_url`).
- **Port.** The hub's default port is **8765**. If you pick another one with `-Port` in step 3
  (for example **8766** when 8765 is taken), use your port wherever this guide shows
  8765. `.\.venv\Scripts\python.exe -m shoutabout info` always shows the real `port` and
  `lan_url`, and the PowerShell snippets read them from `info --json`, so you can paste those
  as they are.
- iPhone behaviour that couldn't be confirmed from a PC is marked **(verify on your phone)**.

## Contents

1. [Prerequisites check](#1-prerequisites-check)
2. [Fixed LAN IP and a Private network](#2-fixed-lan-ip-and-a-private-network)
3. [Clone and run setup](#3-clone-and-run-setup)
4. [Check Ollama](#4-check-ollama)
5. [Cloudflare: Access first, then the route](#5-cloudflare-access-first-then-the-route)
6. [First local test](#6-first-local-test)
7. [Network tests from the PC](#7-network-tests-from-the-pc)
8. [iPhone](#8-iphone)
9. [End-to-end tests](#9-end-to-end-tests)
10. [Customizing](#10-customizing)
11. [Troubleshooting](#11-troubleshooting)
12. [Uninstall](#12-uninstall)

---

## 1. Prerequisites check

Open **Windows PowerShell**: Start menu > "Windows PowerShell", or the Windows PowerShell
profile in Windows Terminal. Don't use "Run as administrator". Setup asks for admin rights itself,
once. The hub and its scheduled task must run as your normal user, so they can paste into your
normal windows.

Confirm the window isn't elevated. This must print `False`:

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
```

Check each prerequisite:

| Check | Command | Expect |
|---|---|---|
| Windows 11 | `[Environment]::OSVersion.Version` (or `winver`) | `10.0`, build 22000 or later. This PC: 26200. |
| NVIDIA driver | `nvidia-smi` | Your GPU (RTX 4080 SUPER) and `CUDA Version: 12.x` or later in the header. **No CUDA toolkit or cuDNN install is needed.** |
| Python 3.12 | `py -3.12 --version` and `python --version` | `Python 3.12.x` |
| winget | `winget --version` | any version |
| Git | `git --version` | any version |
| Disk space | `"{0:N1} GB free" -f ((Get-PSDrive C).Free / 1GB)` | at least **8 GB** |

**Python.** Use the python.org build, not the Microsoft Store one. The firewall rules point at
the interpreter's path, and the Store build moves on every update. `py -0p` shows the path;
python.org lives under `...\AppData\Local\Programs\Python\Python312\`, which is what this PC has.
To install:

```powershell
winget install --id Python.Python.3.12
```

**Git:**

```powershell
winget install --id Git.Git
```

**Disk budget:**

| Item | Size |
|---|---|
| `.venv` (includes the NVIDIA cuBLAS wheel, about 550 MB) | ~1.5 GB |
| Whisper large-v3-turbo | 1.6 GB |
| Ollama cleanup model | 2.5 GB |
| uv's download cache | the rest |

**Execution policy.** Windows PowerShell often refuses to run `.ps1` files. Always start scripts
like this:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\<script>.ps1
```

`-ExecutionPolicy Bypass` applies to that one process and changes nothing permanently.

If you downloaded the repo as a **ZIP** instead of cloning it, unblock the ZIP before extracting:

```powershell
Unblock-File .\shoutabout-main.zip
```

Or unblock the scripts after extracting:

```powershell
Get-ChildItem -Recurse .\scripts | Unblock-File
```

## 2. Fixed LAN IP and a Private network

At home the phone talks straight to `http://<PC-LAN-IP>:8765` (or `:<port>` if you choose
another port in step 3). That address must never change, and Windows must treat your Wi-Fi as
**Private**.

### 2.1 Find the adapter, its MAC address and the current IP

```powershell
Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name, InterfaceDescription, MacAddress
Get-NetIPConfiguration -InterfaceAlias 'Wi-Fi'
ipconfig /all
```

On this PC:

- The adapter that matters is **Wi-Fi** (Intel Wi-Fi 6E AX211). Its IPv4 address is in
  `192.168.0.0/24`.
- Ignore **vEthernet (WSL (Hyper-V firewall))** (a `172.x` address, used by WSL) and the
  disconnected Ethernet and Bluetooth adapters.

Write down the Wi-Fi **MacAddress**, the **IPv4Address**, and the **IPv4DefaultGateway** (your
router, usually `192.168.0.1`).

**Random hardware addresses.** If Windows randomizes the Wi-Fi MAC, the router sees a different
device and the reservation won't apply. Go to **Settings > Network & internet > Wi-Fi > (your
network) properties > Random hardware address** and set it to **Off**. Also check the global
**Random hardware addresses** switch on the Wi-Fi page. Reconnect, then run `Get-NetAdapter`
again to get the MAC that's actually used.

### 2.2 Reserve the address on the router

1. Open `http://<gateway>` (for example `http://192.168.0.1`) in a browser, or the router's
   app, and log in.
2. Find the DHCP reservation page. Names vary by router: **DHCP Server > Address Reservation**,
   **LAN > DHCP > Static Leases**, **Reserved IP**, **Manually Assigned IP**, or
   **Reservations** in mesh apps.
3. Add a reservation: select the PC in the client list, or enter the Wi-Fi MAC. The simplest
   choice is the address it has **now**.
4. Save. Some routers apply the change only after a reboot.

### 2.3 Renew and confirm

Turn Wi-Fi off and on, or reboot. Alternatively, run this in an **admin** PowerShell:

```powershell
ipconfig /release "Wi-Fi"
ipconfig /renew "Wi-Fi"
```

Then confirm the address in a normal PowerShell:

```powershell
Get-NetIPConfiguration -InterfaceAlias 'Wi-Fi' | Select-Object InterfaceAlias, IPv4Address, IPv4DefaultGateway
```

If you later plug in Ethernet, that adapter has its own MAC and gets its own address. Reserve it
too, and rebuild the iPhone shortcuts with `make-shortcuts` (§8).

### 2.4 Confirm the network profile is Private

```powershell
Get-NetConnectionProfile | Select-Object Name, InterfaceAlias, NetworkCategory
```

On this PC, Wi-Fi is already **Private**.

If it says **Public**, the hub's firewall rule (Private networks only) doesn't apply. The phone
then times out at home, while tests from the PC itself still pass. Fix it, but only for your
own trusted home network:

- **Settings > Network & internet > Wi-Fi > (your network) properties > Network profile type >
  Private network**, or
- in an admin PowerShell:
  ```powershell
  Set-NetConnectionProfile -InterfaceAlias 'Wi-Fi' -NetworkCategory Private
  ```

The phone must also be on the same network: not a guest network, and not one with "AP/client
isolation" turned on.

## 3. Clone and run setup

On this PC the code is already in `Documents\shoutabout` (after the v1 PR is merged,
`git checkout main; git pull`):

```powershell
cd "$HOME\Documents\shoutabout"
```

On a fresh machine:

```powershell
cd "$HOME\Documents"
git clone https://github.com/spenseay/shoutabout.git
cd shoutabout
```

**Port check first.** Another program may already be listening on `127.0.0.1:8765`. Check now:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" | Select-Object ProcessId, CommandLine }
```

- **Nothing listed:** use the default port 8765.
- **Something listed:** either stop that program for good, or give ShoutAbout another port
  (e.g. `-Port 8766`). `setup.ps1` stops with a clear message if the port is taken.
- **If you change the port**, use it everywhere this guide says 8765: the Cloudflare service URL
  (`http://127.0.0.1:8766`), the firewall rule name, and the LAN URL in the iPhone Shortcut.
  The PowerShell test snippets read it themselves. `python -m shoutabout info` always shows the
  right values.

Run setup. Pass `-PublicHostname dictate.shoutabout.fyi` so the Cloudflare step knows the
hostname (or `-SkipCloudflare` to leave Cloudflare out for now):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -PublicHostname dictate.shoutabout.fyi
```

If 8765 is taken (the likely case on this PC):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -PublicHostname dictate.shoutabout.fyi -Port 8766
```

Options, which can be combined:

| Option | Effect |
|---|---|
| `-PublicHostname <host>` | Records the public hostname and hands it to the Cloudflare step (`scripts\setup-cloudflare.ps1`). |
| `-Port 8765` | Hub port. 8765 is the default. Written to `config.toml` and used for the firewall rule and the tunnel service URL. A later re-run without `-Port` keeps the port from `config.toml`. |
| `-SkipCloudflare` | No Cloudflare step at all (no prompts). Run `scripts\setup-cloudflare.ps1` later. |
| `-SkipOllama` | Don't install Ollama or pull the cleanup model. |
| `-SkipModel` | Don't download Whisper now. |
| `-SkipFirewall` | No firewall rules, and no UAC prompt. |
| `-SkipTask` | No scheduled task. |
| `-NoStart` | Don't start the hub at the end. |

What it does:

1. **Checks the prerequisites** from step 1.
2. **Installs uv** (`winget install astral-sh.uv`) if it's missing.
3. **Builds `.venv`** with `uv sync --locked --extra cuda`. That installs the pinned packages,
   including NVIDIA's cuBLAS as a pip wheel (about 550 MB). No system CUDA toolkit is needed.
4. **Creates your config** with `shoutabout init`, in `%APPDATA%\shoutabout\`: `config.toml`,
   `vocab.txt` and `hub_token.txt`. Existing files are kept.
5. **Downloads Whisper** large-v3-turbo with `shoutabout download-model`. It's about 1.6 GB and
   goes into `%USERPROFILE%\.cache\huggingface\hub`.
6. **Sets up Ollama:** installs it if missing (this PC already has 0.34.4), then pulls
   `qwen3:4b-instruct-2507-q4_K_M` (about 2.5 GB, Apache-2.0).
7. **Registers the scheduled task** `ShoutAbout Hub`. It runs
   `.venv\Scripts\pythonw.exe -m shoutabout run` at logon (15 s after logon), as you, not
   elevated, and restarts it if it crashes.
8. **Adds firewall rules. This is the one UAC prompt.** An elevated window opens and creates
   **ShoutAbout Hub (TCP 8765)** (or the port you chose with `-Port`, e.g. 8766 on this PC —
   check `python -m shoutabout info`), which allows Private networks and your local subnet
   only. It also adds rules for your base `python.exe` and `pythonw.exe`, so Windows doesn't
   show an "allow Python?" prompt. If it finds old **Python block rules**, it lists them and
   asks `Remove these Python block rules now? [y/N]`. Answer `y`.
9. **Runs the Cloudflare step** (`scripts\setup-cloudflare.ps1`). It runs unless you passed
   `-SkipCloudflare`. Without `-PublicHostname` it first asks for a hostname; press Enter on the
   empty prompt to skip the Cloudflare step. On this PC it finds the existing `cloudflared`
   service and only **prints** the Access and route steps; it changes nothing on the PC or in
   Cloudflare. **Don't do them yet:** step 5 does them in the safe order. It then pauses at:

   ```
   Press Enter once the Access application exists (or type "later" to continue without it)
   ```

   The Access application doesn't exist yet (you create it in step 5), so type **`later`** and
   press Enter. The script prints a warning and the route steps, and setup carries on.

   *On a machine with no tunnel yet*, this step offers to create one. Its **Locally-managed**
   option creates the public DNS route right after that prompt, even if you type `later`. There,
   either do step 5a before running setup, or use `-SkipCloudflare` and run
   `setup-cloudflare.ps1` after 5a.
10. **Writes** `%APPDATA%\shoutabout\install-state.json`.
11. **Starts the task**, waits up to 30 s for `/health`, runs `doctor`, and prints a summary
    with next steps. It never prints the hub token.

**Downloads total about 5 GB:** about 1 GB of Python packages, 1.6 GB Whisper and 2.5 GB LLM.
That's roughly 10 minutes at 100 Mbit/s.

**Setup is safe to re-run.** It keeps your existing config, vocab and token, and replaces its
own firewall rules and task. If a step only warned (for example a failed `ollama pull`), fix the
cause and run it again. If Python 3.12 is missing, uv downloads a private copy for the venv.

**What success looks like:**

- **Tray icon:** grey (starting), then **green** (idle) a few seconds later. Loading Whisper from
  the cache takes about 2 s on this PC. The icon may be hidden behind the taskbar's **^**
  overflow; drag it onto the taskbar.
  - Blue = transcribing.
  - Amber = paused.
  - Red = error. Hover for the reason.
- **`doctor` output:** `[PASS]` for Config, Hub token, Vocab, CUDA, Whisper model, Ollama, Ollama
  GPU, Cleanup sample, Firewall rule, Firewall program rules, Python block rules, Network
  profile, Hub (loopback), Hub (LAN URL) and cloudflared service.
- **Expected at this point:** **Public URL** fails, because there's no route yet (step 5). For now
  you can run `doctor --skip-tunnel`.

**If Windows shows "Windows Defender Firewall has blocked some features of Python":** tick
**Private networks** only, untick Public, and click **Allow access**. Never click Cancel.
Cancel creates *block* rules, which override every allow rule. See §11 if that happened.

## 4. Check Ollama

```powershell
ollama list
ollama ps
.\.venv\Scripts\python.exe -m shoutabout doctor --skip-tunnel
```

- **`ollama list`** includes `qwen3:4b-instruct-2507-q4_K_M` (about 2.5 GB).
- **`ollama ps`**, once the hub has started, shows that model with **PROCESSOR `100% GPU`** and
  **UNTIL `Forever`**. The hub keeps it loaded (`keep_alive = -1`) and re-warms it every 5
  minutes.
  - A CPU/GPU split, or `100% CPU`, means VRAM is short. Close GPU-heavy apps, or unload other
    models with `ollama stop <model>`.
- **`doctor`** checks Ollama, the model, the GPU, and a **Cleanup sample**, and prints how long
  one cleanup took. On this PC during the build it was about 70–160 ms once the model was warm
  (median 91 ms). The very first call after a load can take ~0.5–1.5 s.

## 5. Cloudflare: Access first, then the route

A tunnel route makes the hostname reachable from the internet. Put Cloudflare Access in front of
it **before** creating the route. The full guide, with older dashboard names and the other
tunnel paths, is [docs/cloudflare.md](docs/cloudflare.md).

### 5a. Access: service token, policy, application

1. **Service token.** Go to **Zero Trust > Access controls > Service credentials > Service
   Tokens > Create Service Token**.
   - **Name:** `shoutabout-iphone`
   - **Duration:** 1 year, or Non-expiring
   - Select **Generate token**.
   - **Copy the Client ID and Client Secret into your password manager now.** The secret is
     shown only once.
2. **Policy.** Go to **Access controls > Policies > Add a policy**.
   - **Name:** `ShoutAbout iPhone`
   - **Action:** **Service Auth**
   - **Include:** **Service Token** = `shoutabout-iphone`
   - Save.
3. **Application.** Go to **Access controls > Applications > Create new application > Self-hosted
   and private**.
   - **Add public hostname:** `dictate` . `shoutabout.fyi`, with no path.
   - **Policy:** ShoutAbout iPhone.
   - **Additional settings:** turn on **401 Response for Service Auth policies**.
   - Select **Create**.
4. Copy the **Application Audience (AUD) Tag** (app > **Configure** > **Additional settings**)
   and your **team domain** (**Zero Trust > Settings**, for example
   `yourteam.cloudflareaccess.com`).

Details: [docs/cloudflare.md §3–§5](docs/cloudflare.md#3-create-the-service-token).

### 5b. Route the hostname through your existing tunnel

On this PC there's already a **Cloudflared** Windows service running a dashboard-managed
tunnel (`tunnel run --token-file C:\ProgramData\cloudflared\token`). Add a route to it. Don't
install a second `cloudflared` service.

1. Go to **Networking > Tunnels** and select that tunnel. It's the **Healthy** one whose
   connector is this PC.
2. Open **Routes > Add route > Published application**.
   - **Subdomain:** `dictate`
   - **Domain:** `shoutabout.fyi`
   - **Path:** empty
   - **Service URL:** `http://127.0.0.1:8765` (not `localhost`) — or the port you chose with
     `-Port`, e.g. `http://127.0.0.1:8766` on this PC; check `python -m shoutabout info`
3. Select **Add route**.

To see these steps for this PC again, with the Service URL filled in for your real port:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname dictate.shoutabout.fyi
```

No tunnel yet, or you want a CLI-managed one? See
[docs/cloudflare.md §6](docs/cloudflare.md#6-route-the-hostname-through-a-tunnel), Paths B and C.

### 5c. Let the hub verify Access's JWT

1. Open the config:
   ```powershell
   notepad "$env:APPDATA\shoutabout\config.toml"
   ```
2. Fill in `[access]`. `allowed_client_ids` is optional. If you use it, paste the Client ID
   exactly as Cloudflare shows it. It already ends in `.access`, so don't append anything.
   ```toml
   [access]
   team_domain = "yourteam.cloudflareaccess.com"
   aud = ["<AUD tag>"]
   allowed_client_ids = ["0123abcd....access"]   # the whole Client ID, as shown
   ```
3. Restart the hub: right-click the tray icon > **Quit**, then:
   ```powershell
   Start-ScheduledTask -TaskName 'ShoutAbout Hub'
   Get-Content "$env:APPDATA\shoutabout\logs\hub.log" -Tail 30 | Select-String 'Access'
   ```
   The log should say `Cloudflare Access JWT validation enabled`.

## 6. First local test

```powershell
.\.venv\Scripts\python.exe -m shoutabout doctor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_test_audio.ps1
.\.venv\Scripts\python.exe -m shoutabout transcribe test-audio
```

- **`make_test_audio.ps1`** uses Windows' built-in voices (Zira and David) to speak test sentences
  into `test-audio\`. Each clip is saved as `.wav` and as an iPhone-like `.m4a`, plus a
  `manifest.json` of what was said. Use `-Voice Zira|David|Both` to choose the voice.
- **`transcribe`** never pastes. It prints raw text, cleaned text, the cleanup status, timings,
  and accuracy against the manifest.
  - `transcribe test-audio\*.m4a` tests only the iPhone-format files.
  - `--no-cleanup` shows Whisper alone.
  - It loads a second copy of Whisper next to the hub's, which is fine on 16 GB.

What to check:

- **Vocab clips:** the `terms:` line shows **`Better Shell [hit]`**, spelled with that
  capitalization.
- **Instruction-looking clips**, like questions or "ignore that and …": the output is the
  **same words as text**, not an answer.
  - `cleanup: applied` means the LLM edited the text.
  - `cleanup: fallback (...)` means the safety guard rejected the LLM's version and kept the raw
    transcript. That's safe.
- **Timings:** on this GPU during the build, `stt` took about **145 ms** for 6–9 s clips and
  ~225 ms for an 18 s paragraph. `cleanup` took about **70–160 ms** for a sentence and ~360 ms
  for a long paragraph. `total` was ~300 ms for a sentence and ~600 ms for 18 s of speech.

## 7. Network tests from the PC

**Loopback is the tunnel route.** The hub treats requests from `127.0.0.1` as coming through
cloudflared. With `[access]` configured, it rejects `/transcribe` on `127.0.0.1` because there's
no Access JWT. So test the LAN route with the **LAN address**, as below.

The quickest way to run every test below, positive and negative, is the script. It prompts for
the Cloudflare Client ID and Secret (the secret with hidden input), or reads them from
`SHOUTABOUT_CF_CLIENT_ID` / `SHOUTABOUT_CF_CLIENT_SECRET`, and hands secrets to `curl.exe`
through a temporary file instead of the command line:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-routes.ps1 -PublicUrl https://dictate.shoutabout.fyi
```

To run the tests by hand, read on.

### 7.1 LAN

```powershell
$info = .\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json
$port = $info.port
$lan = $info.lan_url
$lan
$tok = (Get-Content "$env:APPDATA\shoutabout\hub_token.txt").Trim()
$f = (Get-ChildItem .\test-audio\*.m4a | Select-Object -First 1).FullName
```

`$lan` should print `http://<PC-LAN-IP>:<port>`, for example `http://192.168.0.23:8766` if you
chose `-Port 8766`. If it's empty, the LAN IP wasn't detected; set `[server] advertise_host`.

Run each test:

```powershell
curl.exe -s "http://127.0.0.1:$port/health"
curl.exe -s "$lan/health"
```
Expect `{"ok":true}` twice. The first is the address cloudflared uses (the Service URL in step
5b); the second is the one the phone uses at home.

```powershell
curl.exe -s -H "Authorization: Bearer $tok" -F "file=@$f" -F "target=phone" "$lan/transcribe"
```
Expect JSON with `"route":"lan"`, `"target":"phone"`, `"pasted":false` and the text.

```powershell
curl.exe -s -i -F "file=@$f" -F "target=phone" "$lan/transcribe"
```
Expect `HTTP/1.1 401` and `{"detail":"missing or invalid bearer token"}`.

### 7.2 Public (through Cloudflare)

```powershell
$pub = "https://dictate.shoutabout.fyi"
$cfId = Read-Host "CF Client ID"
$sec = Read-Host "CF Client Secret" -AsSecureString
$cfSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
```

Note: a `curl.exe` command with a secret in a `-H` header briefly shows that secret in the
process list (any program on the PC can read command lines) while curl runs. `test-routes.ps1`
avoids this.

Run each test:

```powershell
curl.exe -s -i "$pub/health"
```
Access should block this: expect `401` (or `302` to `…cloudflareaccess.com`, or `403`). If you
see `{"ok":true}`, Access isn't protecting the hostname; fix 5a.

```powershell
curl.exe -s -H "CF-Access-Client-Id: $cfId" -H "CF-Access-Client-Secret: $cfSecret" -H "Authorization: Bearer $tok" -F "file=@$f" -F "target=phone" "$pub/transcribe"
```
Expect HTTP 200 with JSON containing `"route":"tunnel"`.

```powershell
curl.exe -s -i -H "CF-Access-Client-Id: $cfId" -H "CF-Access-Client-Secret: $cfSecret" -F "file=@$f" -F "target=phone" "$pub/transcribe"
```
Expect `HTTP/1.1 401` from the **hub**: `{"detail":"missing or invalid bearer token"}`.

`doctor` can run the public check end to end too. Give it the service token through environment
variables (they last only for this PowerShell window) rather than `--cf-client-secret` on the
command line:

```powershell
$env:SHOUTABOUT_CF_CLIENT_ID = $cfId
$env:SHOUTABOUT_CF_CLIENT_SECRET = $cfSecret
.\.venv\Scripts\python.exe -m shoutabout doctor --skip-model
```

`test-routes.ps1` reads the same two variables, so with them set it won't prompt.

When you're done, clear the secrets from the session, or close the window:

```powershell
Remove-Item Env:SHOUTABOUT_CF_CLIENT_ID, Env:SHOUTABOUT_CF_CLIENT_SECRET -ErrorAction SilentlyContinue
Remove-Variable cfId, cfSecret, sec, tok -ErrorAction SilentlyContinue
```

Notes:

- **Lockout:** 10 failed auth attempts from one source within 10 minutes cause a 10-minute
  lockout (429). On the LAN, the source is the client's IP. Through the tunnel every request
  arrives from cloudflared on `127.0.0.1`, so the hub counts failures per remote client, using
  the `Cf-Connecting-IP` header Cloudflare sets. Your negative tests from the PC can lock out
  further tests from your home's public IP, but not the phone on cellular. Run each negative
  test once. Restarting the hub clears the counters.
- **This doesn't test the firewall.** A request from the PC to its own LAN address doesn't
  exercise the inbound firewall rule. The phone tests in step 9 do. For a quick check, open
  `lan_url` + `/health` in Safari on the phone, e.g. `http://192.168.0.23:8765/health` (or the
  port you chose with `-Port`, e.g. 8766 on this PC — check `python -m shoutabout info`). It
  should show `{"ok":true}`.

## 8. iPhone

Import the ready-made shortcuts. The details are in
[docs/ios-shortcuts.md](docs/ios-shortcuts.md); in short:

1. **Make the shortcut files** on the PC
   ([§3](docs/ios-shortcuts.md#3-make-the-shortcut-files-on-the-pc)):
   ```powershell
   .\.venv\Scripts\python.exe -m shoutabout make-shortcuts --ssid "<home-wifi-name>"
   ```
   This writes signed `Dictate.shortcut` and `Dictation Target.shortcut` files to
   `shortcuts-out\`. They hold no secrets. The LAN URL comes from `info` and the public URL from
   setup's `-PublicHostname`. To get an Apple-signed file without a Mac, the shortcut structure
   (no secrets, no audio) is sent to RoutineHub's HubSign service; `--no-sign` skips that if you
   have a Mac to sign on.
2. **Add them on the iPhone** ([§4](docs/ios-shortcuts.md#4-add-them-on-the-iphone)): get both
   files onto the phone, for example by sending them to yourself in a chat or email that you
   open on the phone. Open each one, choose **Open in Shortcuts** if asked, and add it.
3. **Answer Dictate's three questions**
   ([§5](docs/ios-shortcuts.md#5-answer-the-import-questions)): the hub token, the Cloudflare
   Client ID and the Client Secret. Show each as a QR code on the PC and scan it with the phone:
   ```powershell
   .\.venv\Scripts\python.exe -m shoutabout token --qr
   .\.venv\Scripts\python.exe -m shoutabout qr --clipboard   # after copying each Cloudflare value
   ```
4. **Action Button:** **Settings > Action Button > Shortcut > Choose a Shortcut > Dictate**
   ([§6](docs/ios-shortcuts.md#6-assign-the-action-button)).
5. **Control Center:** **+ > Add a Control > Shortcut > Choose > Dictation Target**
   ([§7](docs/ios-shortcuts.md#7-add-the-control-center-control)).
6. **Permissions** ([§8](docs/ios-shortcuts.md#8-first-runs-and-permissions)): answer **Always
   Allow** for the microphone, the file, and each network destination. Run once on home Wi-Fi and
   once on cellular.

Success is silent: the text lands on the PC (PC mode) and on the phone clipboard. A notification
appears only when something needs a look. To build the shortcuts by hand instead, see
[Appendix A](docs/ios-shortcuts.md#appendix-a-build-dictate-by-hand).

## 9. End-to-end tests

To see what the hub did last, read the last `transcribe` line in `hub.log` (it has no text;
tray > **Recent…** has the text of the last 3 dictations):

```powershell
Select-String "$env:APPDATA\shoutabout\logs\hub.log" -Pattern 'transcribe id=' | Select-Object -Last 1 | ForEach-Object Line
```

| # | Test | Do | Expected |
|---|---|---|---|
| 1 | **PC mode → Notepad** | Run **Dictation Target** until it says **Dictation → PC**. Open Notepad and click into it. On home Wi-Fi, press the Action Button, say "Testing ShoutAbout with Better Shell", and tap stop. | The text appears in Notepad about a second after you tap stop. The tray icon turns blue, then green. The phone has the text on its clipboard and shows no notification (success is silent). `hub.log`: `route=lan target=pc pasted=true`. |
| 2 | **Claude Code in Windows Terminal** | Start `claude` in Windows Terminal, click into its prompt, and dictate a request. | The text appears at the prompt and **nothing is submitted**. The hub sends only Ctrl+V, never Enter. Review it and press Enter yourself. Long dictation (over 800 characters or 3 lines) shows as `[Pasted text #1 …]`, which is normal. |
| 3 | **Phone mode → Notes** | In Control Center, run **Dictation Target** until it says **Dictation → Phone**. Dictate, then open Notes and paste. | Nothing is pasted on the PC. The note gets the text. `hub.log`: `target=phone pasted=false`. Switch back to PC afterwards. |
| 4 | **Cellular** | Turn Wi-Fi off in Settings, then dictate. The first time, approve the connection to `dictate.shoutabout.fyi` with **Always Allow** **(verify on your phone)**. | It works like test 1. `hub.log`: **`route=tunnel`**. |
| 5 | **Pause** | Tray > **Pause** (the icon turns amber). Dictate in PC mode. | Nothing is pasted. The phone shows a **Dictation problem** notification, "Check the PC. Hub is paused — not pasted on the PC.", and the text is on the phone clipboard. Tray > **Resume** afterwards. |
| 6 | **Admin window** (optional) | Focus an elevated PowerShell window and dictate in PC mode. | Not pasted. The PC shows a toast, the text is left on the PC clipboard (press Ctrl+V yourself), and the phone shows a **Dictation problem** notification. |

**Compare timings** across the last few requests:

```powershell
Select-String "$env:APPDATA\shoutabout\logs\hub.log" -Pattern 'transcribe id=' | Select-Object -Last 10 | ForEach-Object Line
```

The timings cover only the work on the PC: decode, STT, cleanup and paste. The difference you
feel between `lan` and `tunnel` is upload time over the network, which isn't recorded.

## 10. Customizing

Edit `config.toml`, then restart the hub:

```powershell
notepad "$env:APPDATA\shoutabout\config.toml"
```

To restart: tray > **Quit**, then:

```powershell
Start-ScheduledTask -TaskName 'ShoutAbout Hub'
```

**Vocabulary.** Open it from tray > **Teach words…** > **Open vocab.txt**, or with:

```powershell
notepad "$env:APPDATA\shoutabout\vocab.txt"
```

- Write one term or phrase per line, spelled the way you want it (for example `Better Shell`,
  `Claude Code`). Lines starting with `#` are comments.
- **Order is priority.** Whisper's hint budget is about 200 tokens, so with a long list only the
  top terms are sent. `hub.log` notes which terms were dropped.
- The LLM cleanup gets the list too.
- Changes apply on the **next dictation**. No restart needed.

**Switch the cleanup model.** Pull a candidate, benchmark it against the current one, and switch
if it wins:

```powershell
ollama pull granite4:micro
.\.venv\Scripts\python.exe -m shoutabout benchmark --models qwen3:4b-instruct-2507-q4_K_M,granite4:micro --runs 5
```

The benchmark reports median and p90 latency and the guard accept rate, and flags outputs that
*answered* instead of cleaning. To switch:

1. Set `[cleanup] model = "granite4:micro"` in `config.toml`.
2. Restart the hub.
3. Unload the old model: `ollama stop qwen3:4b-instruct-2507-q4_K_M`. Otherwise it stays in VRAM
   until Ollama restarts.

Other Apache-2.0 candidates are `granite4.2:3b` and `ministral-3:3b`.

**Turn off cleanup.** Set `[cleanup] enabled = false`. This is the fastest option; responses then
show `"cleanup":"disabled"`. Cleanup also falls back to the raw text by itself if it takes longer
than `timeout_s` (2.0 s).

**Trailing space.** Set `[paste] trailing_space = true` to paste a space after each dictation,
which is handy when you dictate sentence by sentence.

**Run in a console for debugging.** Tray > **Quit**, then:

```powershell
.\.venv\Scripts\python.exe -m shoutabout run --no-tray
```

Ctrl+C stops it. Afterwards, run `Start-ScheduledTask -TaskName 'ShoutAbout Hub'`.

## 11. Troubleshooting

Start with `doctor`. Every failed check prints a `fix:` hint. The log is at
`%APPDATA%\shoutabout\logs\hub.log`; tray > **Settings…** > **Open log folder**, or:

```powershell
.\.venv\Scripts\python.exe -m shoutabout doctor
Get-Content "$env:APPDATA\shoutabout\logs\hub.log" -Tail 50
```

| Problem | Fix |
|---|---|
| Tray is red, or doctor's **CUDA** check fails with errors about `cublas64_12.dll` | The cuBLAS wheel is missing. Run `uv sync --locked --extra cuda`, or re-run `setup.ps1`. A plain `uv sync` **removes** it. Update the NVIDIA driver if `nvidia-smi` shows CUDA below 12. No CUDA toolkit or cuDNN is needed. |
| The phone times out at home, but the PC tests in step 7 pass | The network is **Public** (§2.4); a **Python block rule** exists (below); the firewall rule is missing (re-run `setup.ps1`); the Shortcut's LAN URL has the wrong port (compare with `lan_url` from `info`); the phone is on a guest network or isolated Wi-Fi; or the PC is asleep. |
| The "allow Python" prompt was cancelled, or doctor shows **Python block rules** | List them with the first command below. In an **admin** PowerShell, remove them with the second, or re-run `setup.ps1` and answer `y`. |
| At home, `hub.log` shows `route=tunnel` | The Shortcut didn't see your Wi-Fi name: grant Location to Shortcuts, or check that the name given to `make-shortcuts --ssid` matches exactly (rebuild if not). See [ios-shortcuts.md §8](docs/ios-shortcuts.md#8-first-runs-and-permissions). |
| Public URL gives 302, 401 or 403 | See the response table in [cloudflare.md §8](docs/cloudflare.md#8-verify-with-curlexe). A 302/401 from Access means a bad or missing service token. A JSON 403 from the hub means a JWT problem: check `team_domain` and `aud`. A challenge page means Bot Fight Mode or the WAF ([§10](docs/cloudflare.md#10-gotchas)). |
| The Shortcut shows an error or a "Dictation problem" notification | See [ios-shortcuts.md §13](docs/ios-shortcuts.md#13-phone-side-troubleshooting). |
| `hub is starting` (503) | Whisper is still loading, which takes a few seconds after logon plus the task's 15 s delay. If it persists, hover the tray icon or read `hub.log`. |
| The paste lands in the wrong place, or nowhere | The text goes to whichever window has keyboard focus **when transcription finishes**, so click into the target first and don't switch windows meanwhile. **Git Bash** (mintty) and PuTTY get Shift+Insert automatically (`[paste] shift_insert_processes`). **Windows Terminal** asks for confirmation for pastes over 5 KiB. With the PC locked or no window focused, nothing is pasted and the phone shows a warning. |
| An **admin (elevated) window** has focus | Windows blocks pasting into elevated apps from a normal process. The hub leaves the text on the PC clipboard and shows a toast. Press Ctrl+V yourself, or run that app non-elevated. |
| The hub didn't start after logon | Run `Get-ScheduledTask -TaskName 'ShoutAbout Hub' \| Get-ScheduledTaskInfo` and check `LastTaskResult`. Then run `Start-ScheduledTask -TaskName 'ShoutAbout Hub'`. Only one hub can run at a time. |
| `Couldn't listen on port 8765` (or your port) | Another process holds the port. Find it with `Get-NetTCPConnection -LocalPort $port -State Listen \| Select-Object OwningProcess`, then `Get-Process -Id <pid>` (get `$port` as in §7.1). Stop that program, or re-run setup with another `-Port` and then update the Cloudflare Service URL (§5b) and rebuild the iPhone shortcuts with `make-shortcuts` (§8). |

Python block rules. List them (works without admin):

```powershell
Get-NetFirewallApplicationFilter | Where-Object Program -like '*\python*.exe' | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | Select-Object Name, DisplayName, Profile
```

Remove them (**admin** PowerShell):

```powershell
Get-NetFirewallApplicationFilter | Where-Object Program -like '*\python*.exe' | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | Remove-NetFirewallRule
```

Check ShoutAbout's own rules:

```powershell
Get-NetFirewallRule -Group ShoutAbout | Select-Object Name, DisplayName, Enabled, Profile
```

More cases are in [docs/troubleshooting.md](docs/troubleshooting.md).

## 12. Uninstall

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1
```

By default it stops the hub and removes the scheduled task and the firewall rules (one UAC
prompt). Add options to remove more:

| Option | Also removes |
|---|---|
| `-RemoveData` | `%APPDATA%\shoutabout`: config, vocab, hub token and logs |
| `-RemoveTunnel` | Tunnel pieces that setup *created*. Nothing on this PC, because the existing tunnel was reused. |
| `-RemoveModels` | The Whisper model in the Hugging Face cache, and the Ollama cleanup model |
| `-RemoveVenv` | `.venv` |
| `-All` | All of the above |
| `-Yes` | Don't ask for confirmation |

What stays on the PC:

- the repo folder;
- uv, Python, Git, and Ollama (with any other models);
- the pre-existing `cloudflared` service and its tunnel.

Remove these yourself:

- **Cloudflare:** the `dictate.shoutabout.fyi` route and DNS record, the Access application, the
  policy and the service token. Delete the route first; see
  [docs/cloudflare.md §12](docs/cloudflare.md#12-removing-everything).
- **iPhone:** the two shortcuts, the Action Button and Control Center assignments, and
  `iCloud Drive/Shortcuts/dictation-target.txt` (and `last-dictation.m4a`).
- **Router:** the DHCP reservation, if you no longer want it.
