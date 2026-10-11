# Cloudflare: tunnel + Access for `dictate.shoutabout.fyi`

This guide reflects the Cloudflare dashboard as of October 2026. Cloudflare renames menus
often. Where a label isn't confirmed by Cloudflare's own docs, it's marked
**(label may differ slightly)**. Older names are given where they're useful.

Run every command in a normal **Windows PowerShell** window in the repo folder, unless a step
says "admin PowerShell". In PowerShell, `curl` is an alias for `Invoke-WebRequest`, so always
type **`curl.exe`**.

**Port.** This guide shows the hub's default port, **8765**. If you ran setup with `-Port`
(for example **8766** when another program already uses 8765), use your port wherever
you see 8765, in particular in the tunnel route's **Service URL**. Check it with
`.\.venv\Scripts\python.exe -m shoutabout info` (`port`). `scripts\setup-cloudflare.ps1` reads
it from there and prints the right Service URL.

## Contents

1. [How the pieces fit](#1-how-the-pieces-fit)
2. [The order matters](#2-the-order-matters)
3. [Service token](#3-create-the-service-token)
4. [Reusable Service Auth policy](#4-create-the-reusable-policy)
5. [Access application](#5-create-the-access-application)
6. [Tunnel route: Path A, B or C](#6-route-the-hostname-through-a-tunnel)
7. [Turn on JWT checking in the hub](#7-turn-on-jwt-checking-in-the-hub)
8. [Verify with curl.exe](#8-verify-with-curlexe)
9. [Limits](#9-limits)
10. [Gotchas](#10-gotchas)
11. [Rotating secrets](#11-rotating-secrets)
12. [Removing everything](#12-removing-everything)

---

## 1. How the pieces fit

```
iPhone (cellular)
  │  HTTPS POST https://dictate.shoutabout.fyi/transcribe
  │  headers: CF-Access-Client-Id, CF-Access-Client-Secret, Authorization: Bearer <hub token>
  ▼
Cloudflare edge ── Access checks the service token. If it's missing or wrong, the request
  │                 stops here and never reaches your PC.
  │                 If it's valid, Access adds a signed JWT header: Cf-Access-Jwt-Assertion.
  ▼
Cloudflare Tunnel ── cloudflared on your PC keeps an outbound connection open.
  │                   No port forwarding and no public IP.
  ▼
http://127.0.0.1:8765 ── (your hub port) the hub sees a loopback peer, so route = "tunnel":
                          1. checks the bearer token      (always)
                          2. checks the Access JWT        (once [access] is set in config.toml)
                          3. transcribes
```

| Layer | Job | What it stops |
|---|---|---|
| **Tunnel** | Transport only. It gets requests from the internet to `127.0.0.1:8765` without opening a port on your router. | Nothing by itself. A route with no Access app in front is open to the whole internet. |
| **Access + service token** | The gate. Cloudflare rejects anything that lacks your token's Client ID and Secret. | Scanners, bots and anyone who finds the hostname. |
| **Hub JWT check** | Defence in depth. The hub verifies Access's signature (`aud`, issuer, expiry, and optionally the Client ID). | Requests that reach the tunnel without passing through Access, such as another hostname or route on the same tunnel pointing at the hub by mistake. |
| **Hub bearer token** | Application-level secret, checked on every route. On the LAN it's the only check. | Anything the layers above miss. |

The route is decided by the TCP peer address alone: loopback is the tunnel, a private LAN
address is the LAN, and anything else gets 403. The hub never uses `Cf-Connecting-IP` or
`X-Forwarded-For` for routing or auth. It does use a valid `Cf-Connecting-IP` (the remote
client's IP, set by Cloudflare) on the tunnel route for one thing: to keep separate rate-limit
and auth-failure counters per remote client, so one misbehaving client can't lock out the
owner. A local process could send a different value each time to dodge the lockout, but it
would still need the 256-bit hub token.

## 2. The order matters

Cloudflare's own warning: if a published application has no Access application in front of
it, it "will be available to anyone on the Internet". So:

1. Create the **service token** (§3).
2. Create the **Service Auth policy** that allows only that token (§4).
3. Create the **Access application** for `dictate.shoutabout.fyi` with that policy (§5).
4. **Only then** add the tunnel route that makes the hostname live (§6).
5. Turn on JWT checking in the hub (§7) and verify (§8).

**First time in Zero Trust?** If you've never opened **Zero Trust** in the dashboard, the first
visit asks you to choose a **team name** (it becomes `<team>.cloudflareaccess.com`) and a plan.
Pick **Free**. It may ask for a payment method even on Free **(label may differ slightly)**.
Service tokens don't use seats.

## 3. Create the service token

1. In the Cloudflare dashboard, go to **Zero Trust > Access controls > Service credentials >
   Service Tokens**.
2. Select **Create Service Token**.
3. **Name:** `shoutabout-iphone`.
4. **Service Token Duration:** choose **1 year** and set an expiry reminder (see §11), or
   choose **Non-expiring** if you'd rather never touch it. The exact options **(may differ
   slightly)**.
5. Select **Generate token**.
6. **Copy both values now.** "This is the only time Cloudflare Access will display the Client
   Secret."
   - **Client ID** looks like `0123456789abcdef0123456789abcdef.access`.
   - **Client Secret**: tokens created since 26 Aug 2026 look like `cfast_` + 48 characters.
     Older 64-character hex secrets still work.
7. Store them:
   - In your **password manager**, as the recovery copy.
   - In the **iPhone Shortcut**: Dictate asks for them when you add it (see
     [ios-shortcuts.md §5](ios-shortcuts.md#5-answer-the-import-questions)).
   - Nowhere else: not in the repo, not in `config.toml`, not in a notes app that syncs.
     The hub never needs the secret.

## 4. Create the reusable policy

1. Go to **Zero Trust > Access controls > Policies > Add a policy**.
2. **Policy name:** `ShoutAbout iPhone`.
3. **Action:** **Service Auth**. This is required. With any other action, Access sends the
   request to a human login page.
4. **Session duration:** leave the default. Service-token requests carry their headers every
   time, so it doesn't matter.
5. **Add include:**
   - **Selector:** **Service Token**
   - **Value:** `shoutabout-iphone`

   Don't use **Any Access Service Token**. That would admit every token in your account.
6. Leave **Require** and **Exclude** empty, then **Save**.

Legacy app-scoped policies can't be added to newly created Access applications, so use this
reusable policy.

## 5. Create the Access application

1. Go to **Zero Trust > Access controls > Applications > Create new application**. In older
   dashboards this was **Access > Applications > Add an application**.
2. Choose **Self-hosted and private**. In older dashboards: **Self-hosted**.
3. If asked for an application name, use `ShoutAbout dictate` **(label may differ slightly)**.
4. Select **Add public hostname** and fill in:
   - **Subdomain:** `dictate`
   - **Domain:** `shoutabout.fyi` (from the dropdown)
   - **Path:** leave empty, so the whole host is protected, including `/health`.
5. **Access policies:** select the existing policy **ShoutAbout iPhone**.
6. **Identity providers / login methods:** leave them as they are. No human ever logs in here.
7. **Additional settings:** turn on **401 Response for Service Auth policies**. Access then
   returns a plain **401** to a request without a valid token, instead of a 302 redirect to an
   HTML login page. That gives the Shortcut and `curl.exe` a clear error. Leave CORS, cookie
   settings and App Launcher alone.
8. Select **Create**. Older dashboards say **Save** or **Add application**.

### 5.1 Note the AUD tag and team domain (needed in §7)

- **AUD tag:** Applications > **ShoutAbout dictate** > **Configure** > **Additional settings** >
  **Application Audience (AUD) Tag**. It's a 64-character hex string. Older dashboards showed
  it on the app's **Overview** tab.
- **Team domain:** **Zero Trust > Settings** shows the team name and domain, such as
  `yourteam.cloudflareaccess.com`.

### 5.2 Strict service token mode (new, October 2026)

Zero Trust has an organisation-wide switch at **Access controls > Access settings > Manage
service tokens** **(label may differ slightly)**.

- **Strict mode on:** failed service-token requests get **401/403** instead of a 302, only
  Service Auth policies apply, and **no `CF_Authorization` cookie** is issued, so the client must
  send the headers on every request.
- Organisations created on or after 5 Oct 2026 have it on and can't turn it off.

That suits ShoutAbout: the Shortcut and the hub's tests send both headers on every request.
Either setting works.

## 6. Route the hostname through a tunnel

Pick one path.

| Path | When | What runs on the PC |
|---|---|---|
| **A** | A dashboard-managed `cloudflared` service already exists. **This is your PC.** | The existing service. Nothing new. |
| **B** | No tunnel yet. Cloudflare recommends this path. | One new `cloudflared` service, configured from the dashboard. |
| **C** | You want the tunnel config in a local file (CLI-managed). | One new `cloudflared` service with a `config.yml`. |

`scripts\setup-cloudflare.ps1 -PublicHostname dictate.shoutabout.fyi` detects which case
applies (`-Mode auto`, the default). It records the hostname in
`%APPDATA%\shoutabout\install-state.json`, so `doctor` knows which public URL to test.

- **A** (`existing`): it prints the dashboard steps and changes nothing locally.
- **B** (`-Mode remote`):
  1. It prints the dashboard steps for creating the tunnel.
  2. It asks you to paste the tunnel token. You can paste just the token or the whole
     `cloudflared.exe service install ...` command. The input is hidden and never echoed.
  3. It runs `cloudflared.exe service install <token>` in an elevated window (one UAC
     prompt).
- **C** (`-Mode local`): it runs the CLI steps for you.

Before the route step, the script prints the Access steps and pauses at `Press Enter once the
Access application exists (or type "later" to continue without it)`. Press Enter once §3–§5 are
done. If they aren't (for example when `setup.ps1` runs this step before you've reached SETUP
step 5), type `later`. In Path A that only skips ahead to the printed route steps. In Path C the
script still creates the DNS route right afterwards, so finish §3–§5 before running Path C.

### Path A: add a route to the existing tunnel (this PC)

On your PC we found a `cloudflared` Windows service named **Cloudflared** ("Cloudflared
agent"). It runs as LocalSystem and starts automatically. Its command line is:

```
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel run --token-file C:\ProgramData\cloudflared\token
```

A token-run tunnel is **remotely managed**: its routes come only from the dashboard, and a local
`config.yml` ingress block has no effect. One tunnel can carry many hostnames, so you only add a
route. **Don't install a second `cloudflared` service** and don't run `cloudflared service
install`, because the service name would collide.

To check the service:

```powershell
Get-Service cloudflared
Get-CimInstance Win32_Service -Filter "Name='cloudflared'" | Select-Object Name, State, PathName
```

**Optional: find which dashboard tunnel this is.** The token file contains the tunnel ID. This
command prints **only** the ID (field `t`), not the secret. Reading the file may need an admin
PowerShell. If the command errors, match by connector instead (step 2 below).

```powershell
$raw = (Get-Content C:\ProgramData\cloudflared\token -Raw).Trim()
([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($raw)) | ConvertFrom-Json).t
```

Steps (do §3–§5 first):

1. Go to **Networking > Tunnels**. The same tunnels also appear under **Zero Trust > Networks >
   Connectors**, and both UIs have full tunnel management.
2. Select the tunnel whose ID matches the output above. Otherwise pick the one that's
   **Healthy** and whose connector shows this PC's name (`$env:COMPUTERNAME`) **(label may
   differ slightly)**.
3. Open the **Routes** tab and select **Add route** > **Published application**.
4. Fill in:
   - **Subdomain:** `dictate`
   - **Domain:** `shoutabout.fyi`
   - **Path:** leave empty
   - **Service URL:** `http://127.0.0.1:8765` (or the port you chose with `-Port`, e.g.
     `http://127.0.0.1:8766` on this PC — check `python -m shoutabout info`)
5. Leave **Additional application settings** (origin settings) at their defaults.
6. Select **Add route**.

Older Zero Trust UI: tunnel > **Edit** > **Public Hostname** (later **Published application
routes**) tab > **Add a published application route**. Fill in Subdomain `dictate`, Domain
`shoutabout.fyi`, **Service Type** `HTTP` and **URL** `127.0.0.1:8765` (or your port, e.g.
`127.0.0.1:8766`).

Notes:

- Use `127.0.0.1`, not `localhost`. `localhost` can resolve to `::1`, and the hub listens on
  IPv4 only (`0.0.0.0`), so you'd get a 502.
- Paths aren't rewritten: `/transcribe` arrives as `/transcribe`.
- The dashboard creates a proxied DNS record `dictate` → `<tunnel-id>.cfargotunnel.com`. If a
  `dictate` DNS record already exists, delete it first or the route can't be saved.
- `dictate.shoutabout.fyi` is one level below the zone, so Cloudflare's free Universal SSL
  certificate already covers it. No certificate work is needed.
- **If `shoutabout.fyi` isn't in the Domain dropdown,** the existing tunnel belongs to a
  different Cloudflare account. You can add the zone to that account. Otherwise, create a tunnel
  in the right account (Path B) and run it as a second service with a **unique name**: Windows
  requires unique service names, and Cloudflare documents this as supported. The `sc.exe`
  syntax below is unverified; run it in an admin PowerShell, then check the result with
  `sc.exe qc cloudflared-dictate`:

  ```powershell
  # Save the new tunnel's token (from the dashboard install command) to a file only admins can read first.
  sc.exe create cloudflared-dictate binPath= "\"C:\Program Files (x86)\cloudflared\cloudflared.exe\" tunnel run --token-file C:\ProgramData\cloudflared-dictate\token" start= auto DisplayName= "Cloudflared dictate"
  sc.exe start cloudflared-dictate
  ```

  Use `sc.exe`, not `sc`: in PowerShell `sc` is an alias for `Set-Content`.

### Path B: new dashboard-managed tunnel (no tunnel yet)

Do §3–§5 first.

1. Install cloudflared:
   ```powershell
   winget install --id Cloudflare.cloudflared
   ```
2. Go to **Networking > Tunnels > Create Tunnel**. Choose **Cloudflared**, name it
   `shoutabout`, then choose **Windows** / **64-bit** **(labels may differ slightly)**.
3. The dashboard shows an install command with a long token. Run it in an **admin
   PowerShell**:
   ```powershell
   cloudflared.exe service install <TUNNEL_TOKEN>
   ```
   This creates the `cloudflared` service. The token ends up in the service's command line. To
   keep it in a file instead, as this PC does: cloudflared 2025.4.0 or later supports
   `tunnel run --token-file <path>` for remote tunnels, set in the service's `ImagePath`.
4. Wait for the tunnel to show **Healthy**, then continue to the route step: **Published
   application** with the same values as Path A step 4.
5. Record the hostname for `doctor`:
   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname dictate.shoutabout.fyi
   ```

### Path C: locally-managed tunnel (CLI, config file)

Use this only when **no** `cloudflared` service exists on the PC. The script automates all of
it:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-cloudflare.ps1 -PublicHostname dictate.shoutabout.fyi -Mode local -TunnelName shoutabout
```

The manual equivalent follows. Do §3–§5 first, because `route dns` makes the hostname live.

1. Install cloudflared, then log in and create the tunnel (normal PowerShell):
   ```powershell
   winget install --id Cloudflare.cloudflared
   cloudflared tunnel login
   cloudflared tunnel create shoutabout
   cloudflared tunnel list
   cloudflared tunnel route dns shoutabout dictate.shoutabout.fyi
   ```
   - `login` opens a browser. Pick the `shoutabout.fyi` zone. It writes
     `%USERPROFILE%\.cloudflared\cert.pem`, which is needed only to create or delete tunnels and
     DNS routes, not to run them.
   - `create` writes `%USERPROFILE%\.cloudflared\<UUID>.json`. This credentials file works like a
     password for that one tunnel.
   - `route dns` creates the proxied CNAME `dictate` → `<UUID>.cfargotunnel.com`.
2. **Windows service gotcha:** the service runs as **LocalSystem**, so it doesn't read your
   `%USERPROFILE%\.cloudflared`. Its files belong in
   `C:\Windows\System32\config\systemprofile\.cloudflared\`. In an **admin PowerShell**:
   ```powershell
   $sp = 'C:\Windows\System32\config\systemprofile\.cloudflared'
   New-Item -ItemType Directory -Force $sp
   Copy-Item "$env:USERPROFILE\.cloudflared\<UUID>.json" $sp
   ```
   The admin PowerShell's `$env:USERPROFILE` is still your profile if your own account
   elevated. If you elevated as a different account, use the full path instead.
3. Create `C:\Windows\System32\config\systemprofile\.cloudflared\config.yml`:
   ```yaml
   tunnel: <UUID>
   credentials-file: C:\Windows\System32\config\systemprofile\.cloudflared\<UUID>.json
   ingress:
     - hostname: dictate.shoutabout.fyi
       service: http://127.0.0.1:8765
     - service: http_status:404
   ```
   The last rule is a required catch-all. Replace `8765` with the port you chose with `-Port`,
   if any (e.g. 8766 on this PC — check `python -m shoutabout info`). The script writes your
   real port for you.

   Optional: cloudflared can also check the Access JWT itself ("Protect with Access"). Add
   this under the first rule:
   ```yaml
       originRequest:
         access:
           required: true
           teamName: <team>
           audTag: [<AUD tag>]
   ```
4. Validate the config:
   ```powershell
   cloudflared tunnel --config C:\Windows\System32\config\systemprofile\.cloudflared\config.yml ingress validate
   cloudflared tunnel --config C:\Windows\System32\config\systemprofile\.cloudflared\config.yml ingress rule https://dictate.shoutabout.fyi/transcribe
   ```
5. Install the service, then point it at the config by editing **ImagePath** (admin
   PowerShell). Cloudflare warns that extra spaces or characters in ImagePath can stop the
   service from starting.
   ```powershell
   cloudflared.exe service install
   $exe = (Get-Command cloudflared).Source
   Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\cloudflared' -Name ImagePath -Value "`"$exe`" --config=C:\Windows\System32\config\systemprofile\.cloudflared\config.yml tunnel run"
   sc.exe stop cloudflared
   sc.exe start cloudflared
   ```
   After any later `config.yml` change, run `sc.exe stop cloudflared` and then
   `sc.exe start cloudflared`.

## 7. Turn on JWT checking in the hub

Until `[access]` is filled in, the hub accepts tunnel requests with the bearer token alone,
and logs a loud warning in `hub.log` at startup.

1. Open `%APPDATA%\shoutabout\config.toml`:
   ```powershell
   notepad "$env:APPDATA\shoutabout\config.toml"
   ```
2. Fill in the `[access]` section. `allowed_client_ids` is optional; it accepts only JWTs issued
   to that service token. Paste the Client ID exactly as Cloudflare shows it. It already ends in
   `.access`, so don't append anything.
   ```toml
   [access]
   team_domain = "yourteam.cloudflareaccess.com"
   aud = ["<64-hex-char AUD tag from §5.1>"]
   allowed_client_ids = ["0123abcd....access"]   # the whole Client ID, as shown
   ```
3. Restart the hub: right-click the tray icon > **Quit**, then run:
   ```powershell
   Start-ScheduledTask -TaskName 'ShoutAbout Hub'
   ```
4. Confirm the log shows the JWT check enabled:
   ```powershell
   Get-Content "$env:APPDATA\shoutabout\logs\hub.log" -Tail 30 | Select-String 'Access'
   ```
   Expect `Cloudflare Access JWT validation enabled (issuer https://yourteam.cloudflareaccess.com)`.

What the hub checks:

- Algorithm **RS256**.
- Signature, against `https://<team>/cdn-cgi/access/certs`.
- `aud` contains one of your tags.
- `iss` is exactly `https://<team>.cloudflareaccess.com`.
- `exp` and `iat` are present and current.
- Optionally, `common_name` is in `allowed_client_ids`.

Cloudflare rotates the signing keys every 6 weeks. The hub re-fetches them hourly and whenever an
unknown key ID appears, so there's nothing to do.

## 8. Verify with curl.exe

The easiest check is the script. It runs the LAN tests and all of the public tests below,
prompts for the Client ID and Secret (the secret with hidden input) unless
`SHOUTABOUT_CF_CLIENT_ID` / `SHOUTABOUT_CF_CLIENT_SECRET` are set, and passes secrets to
`curl.exe` through a temporary file, not the command line:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-routes.ps1 -PublicUrl https://dictate.shoutabout.fyi
```

To run the checks by hand, do it from the repo folder. They need a test clip: generate one with
`scripts\make_test_audio.ps1` (see [SETUP.md](../SETUP.md#6-first-local-test)) or point `$f` at any
`.m4a` file.

```powershell
$pub = "https://dictate.shoutabout.fyi"
$port = (.\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json).port
$tok = (Get-Content "$env:APPDATA\shoutabout\hub_token.txt").Trim()
$f = (Get-ChildItem .\test-audio\*.m4a | Select-Object -First 1).FullName
$cfId = Read-Host "CF Client ID"
$sec = Read-Host "CF Client Secret" -AsSecureString
$cfSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
```

A `curl.exe` command with a secret in a `-H` header shows that secret in the process list (any
program on the PC can read command lines) for as long as curl runs. `test-routes.ps1` avoids
this.

**1. No headers.** Access should block the request, and you must **not** see `{"ok":true}`:

```powershell
curl.exe -s -i "$pub/health"
```

Expect `HTTP/1.1 401` with the 401 option on or strict mode (`403` is also possible), or `302`
with `location: https://yourteam.cloudflareaccess.com/cdn-cgi/access/login/...`. If you get
`200 {"ok":true}`, the hostname isn't protected: go back to §5.

**2. With the service token.** This reaches the hub:

```powershell
curl.exe -s -H "CF-Access-Client-Id: $cfId" -H "CF-Access-Client-Secret: $cfSecret" "$pub/health"
```

Expect `{"ok":true}`.

**3. Full transcription through the tunnel:**

```powershell
curl.exe -s -H "CF-Access-Client-Id: $cfId" -H "CF-Access-Client-Secret: $cfSecret" -H "Authorization: Bearer $tok" -F "file=@$f" -F "target=phone" "$pub/transcribe"
```

Expect JSON with `"route":"tunnel"`, `"target":"phone"`, `"pasted":false` and your text.

**4. Service token but no bearer.** Access lets it through and the hub refuses it:

```powershell
curl.exe -s -i -H "CF-Access-Client-Id: $cfId" -H "CF-Access-Client-Secret: $cfSecret" -F "file=@$f" -F "target=phone" "$pub/transcribe"
```

Expect `HTTP/1.1 401` and `{"detail":"missing or invalid bearer token"}`.

**5. Optional: prove the JWT check is on.** Loopback counts as the tunnel route, and a direct
request carries no JWT:

```powershell
curl.exe -s -i -H "Authorization: Bearer $tok" -F "file=@$f" -F "target=phone" "http://127.0.0.1:$port/transcribe"
```

Expect `403` and `{"detail":"missing Cloudflare Access JWT"}`.

`doctor` can run the public check with the token too. Give it the token through the environment
variables `SHOUTABOUT_CF_CLIENT_ID` and `SHOUTABOUT_CF_CLIENT_SECRET` (they last only for this
PowerShell window) rather than `--cf-client-secret` on the command line:

```powershell
$env:SHOUTABOUT_CF_CLIENT_ID = $cfId
$env:SHOUTABOUT_CF_CLIENT_SECRET = $cfSecret
.\.venv\Scripts\python.exe -m shoutabout doctor --skip-model
```

When you're done, close the window, or run:

```powershell
Remove-Item Env:SHOUTABOUT_CF_CLIENT_ID, Env:SHOUTABOUT_CF_CLIENT_SECRET -ErrorAction SilentlyContinue
Remove-Variable cfId, cfSecret, sec, tok -ErrorAction SilentlyContinue
```

> **Lockout:** each failed bearer or JWT check counts against the source, and 10 failures in 10
> minutes give that source 429 for 10 minutes. Every tunnel request arrives from cloudflared on
> `127.0.0.1`, so on the tunnel route the hub keys the counters by the `Cf-Connecting-IP` header
> Cloudflare sets: failed tests from your home connection lock out only that public IP, not the
> phone on cellular. Run the negative tests once each. Restarting the hub clears the counters.

### What each response means

| You get | Sent by | Meaning / fix |
|---|---|---|
| `302` → `…cloudflareaccess.com/cdn-cgi/access/login…` | Access | No valid service token, and the 401 option is off. |
| `401` (not JSON) | Access | No valid service token. Check the Client ID/Secret and header names. |
| `403` Access "Forbidden" page | Access | The token is valid but the policy doesn't include it. Check §4 Include and §5 policies. |
| `403` with header `cf-mitigated: challenge`, "Just a moment…" HTML | Cloudflare security (Bot Fight Mode, Browser Integrity Check, WAF) | See §10. |
| `502` / `530` / "Error 1033" | Tunnel | The tunnel is down, the route is missing, or the hub isn't running. Check `Get-Service cloudflared`, the route's Service URL (it must use the hub's port), and `curl.exe "http://127.0.0.1:$port/health"`. |
| `524` | Cloudflare | The hub took longer than 125 s to respond. |
| `401` `{"detail":"missing or invalid bearer token"}` | Hub | Wrong or missing `Authorization: Bearer`. |
| `403` `{"detail":"missing Cloudflare Access JWT"}` or `"Cloudflare Access JWT rejected: …"` | Hub | `team_domain` or `aud` is wrong, `allowed_client_ids` doesn't match, or the request bypassed Access. |
| `403` `{"detail":"forbidden"}` | Hub | The source address isn't loopback or a private LAN address. cloudflared must run on this PC. |
| `429` JSON | Hub | Rate limit (30/min) or auth lockout. |
| `503` JSON | Hub | Still loading the model, or the engine failed. The detail says which. |

## 9. Limits

- **Upload size:** Cloudflare allows 100 MB per request on Free and Pro, then returns 413. The
  hub caps uploads at **25 MB** and audio at **10 minutes** (`[server] max_upload_mb`,
  `max_audio_seconds`). The Shortcut's Normal-quality AAC recording is far below both.
- **Response time:** Cloudflare's proxy read timeout is **125 s**. Past that you get **524**,
  and only Enterprise plans can raise it. On this RTX 4080 SUPER, Whisper took about 140–170 ms
  for 6–19 s clips. Ten minutes of audio transcribes in seconds, plus the LLM cleanup with its
  2 s timeout. That's well under 125 s.
- **Slow uploads:** a very slow cellular upload can fail before it reaches any size limit
  (proxy write timeout 30 s). Keep recordings in Normal quality.
- **Account limits:** 50 service tokens, 500 Access applications, and 1,000 tunnels per account.
  Service tokens **don't** use Zero Trust seats.

## 10. Gotchas

- **Bot Fight Mode / Browser Integrity Check.** iOS Shortcuts isn't a browser and may get
  challenged. A Cloudflare challenge returns `403` with an HTML "Just a moment…" page and the
  header `cf-mitigated: challenge`, rather than Access's 401 or the hub's JSON. Fix:
  - Add a skip rule under **Security > Security rules > Create rule > Custom rule**. Older
    dashboards: **Security > WAF > Custom rules** **(label may differ slightly)**.
    - **When:** Hostname equals `dictate.shoutabout.fyi`
    - **Action:** **Skip**
    - **Skip:** Browser Integrity Check, Security Level, and all remaining custom rules
  - Bot Fight Mode (the Free-plan bot feature) can't be skipped per hostname. If that's what
    challenges you, turn it off for the zone under **Security > Bots** **(label may differ
    slightly)**.
- **Don't cache.** Don't add Cache Rules or "Cache Everything" for this hostname. POSTs aren't
  cached by default, but a cached `/health` would hide outages.
- **Always use `https://`** in the Shortcut. A redirect from `http://` turns the POST into a GET.
- **Client IP.** The hub sees every tunnel request as coming from `127.0.0.1`, which is how it
  knows the route is the tunnel. Cloudflare puts the phone's real IP in `Cf-Connecting-IP`. The
  hub never uses that header for routing or auth. It only uses a valid one to keep the
  rate-limit and auth-failure counters separate per remote client (see §1).
- **Two hostnames on one tunnel.** If you ever route another hostname to `127.0.0.1:8765` (your
  hub port) without an Access app, the JWT check (§7) is what stops it.
- **Chunked uploads.** cloudflared talks HTTP/1.1 to the hub, and uvicorn handles chunked request
  bodies. Leave **Disable chunked encoding** off unless uploads arrive empty or truncated.

## 11. Rotating secrets

- **Service token expiry.** Go to **Zero Trust > Access controls > Service credentials >
  Service Tokens** and open the token.
  - **Refresh** extends it by one year and keeps the same secret.
  - **Edit** sets a new duration.
  - Set an expiry alert one week ahead. Cloudflare offers this as a notification, under
    **Notifications** in the account **(label may differ slightly)**.
- **New secret.** Since August 2026 a rotation can keep the old secret valid for a **grace
  period** of 1 hour to 30 days. During it, update the `CFSecret` Text action in the Shortcut and
  your password manager, then let the old secret expire. You can also temporarily **disable** a
  token.
- **New token (new Client ID).** Update the policy's Include (§4), `allowed_client_ids` in
  `config.toml` (§7), and both `CFId` and `CFSecret` in the Shortcut, then restart the hub.
- **Automatic cleanup.** If you enable automatic cleanup of inactive service tokens (added
  September 2026), a token you haven't used for a long time could be removed.
- **Hub bearer token.** Run `.\.venv\Scripts\python.exe -m shoutabout init --rotate-token`,
  restart the hub, and paste the new token into the Shortcut's `HubToken` Text action.

## 12. Removing everything

`scripts\uninstall.ps1 -RemoveTunnel` removes only the tunnel pieces that ShoutAbout's setup
created, as recorded in `install-state.json`. On this PC that's nothing, because the existing
tunnel was reused. Remove the rest in the dashboard:

1. **Networking > Tunnels > your tunnel > Routes:** delete the `dictate.shoutabout.fyi` route.
   Also delete the `dictate` DNS record if it remains.
2. **Zero Trust > Access controls > Applications:** delete **ShoutAbout dictate**.
3. **Access controls > Policies:** delete **ShoutAbout iPhone**.
4. **Access controls > Service credentials > Service Tokens:** delete **shoutabout-iphone**.

Delete the route first, so the hostname never sits unprotected.
