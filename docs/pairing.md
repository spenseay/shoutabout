# Pairing the iPhone app (`pair`)

> **v2:** the iPhone app normally pairs without any code to scan: it finds the PC on the
> Wi-Fi and the PC asks "Sam's iPhone wants to dictate to this PC. Code 4821 [Allow]".
> That flow, and the per-phone tokens it creates, are in [protocol.md](protocol.md). The QR
> code below still works: it's the "Scan a code instead" link in the setup window, and the
> way to pair without DNS-SD.

The ShoutAbout iPhone app sets itself up from one QR code that the PC shows:

```powershell
.\.venv\Scripts\python.exe -m shoutabout pair
```

The QR code holds everything the app needs to reach the hub: its addresses, the hub token, the
Cloudflare service token if you use the public route, and your home Wi-Fi name. Scan it with the
app, and you're done. Shortcuts built with Cherri (`make-shortcuts --format cherri`) ask for the
same text once when you add them: scan the code with the Camera or Code Scanner, choose
**Copy** (never Search), and paste it ([ios-shortcuts.md §16](ios-shortcuts.md#16-signing-status-and-options)).
The plist-built shortcuts ask for the three secrets separately instead.

**The QR code contains secrets.** Show it only on your own screen, to your own phone. See
[Security](#security).

## Contents

1. [What `pair` does](#1-what-pair-does)
2. [Options](#2-options)
3. [The Cloudflare service token](#3-the-cloudflare-service-token)
4. [Payload format](#4-payload-format)
5. [Security](#security)

## 1. What `pair` does

1. Reads the hub token from the config dir (`hub_token.txt`; run `init` first if there is none).
2. Works out the addresses, the same way as `make-shortcuts`:
   - **LAN URL:** `lan_url` from `python -m shoutabout info`, e.g. `http://192.168.1.50:8765`.
   - **Public URL:** `https://<public_hostname>` from `install-state.json` (setup's
     `-PublicHostname`), or none.
3. Reads the PC's current Wi-Fi name (as `make-shortcuts` does). If Windows won't tell (Ethernet,
   Wi-Fi off, no Location access), the payload has no Wi-Fi name and `pair` says so.
4. Adds the Cloudflare service token if you give it one (§3).
5. Prints a short summary **without** the secrets, a warning, and the QR code.

Nothing is written to disk and nothing is sent anywhere. With `--open` the QR code becomes a
temporary SVG image, opened in your image viewer and deleted when you press Enter.

## 2. Options

| Option | Effect |
|---|---|
| `--ssid "<name>"` | Home Wi-Fi name, exactly as the iPhone shows it (default: the PC's current Wi-Fi). |
| `--lan-url http://<ip>:<port>` | LAN address of the hub (default: `lan_url` from `info`). A `/transcribe` URL works too. |
| `--public-url https://<host>` | Public address (default: from `install-state.json`). Must be `https://`. |
| `--no-public` | LAN only: no public URL in the payload. |
| `--cloudflare` | Ask for the Cloudflare Client ID and Secret (typed or pasted, input hidden). |
| `--cf-file <path>` | Read the Cloudflare values from a text file (§3). |
| `--open` | Show the QR code as a large image instead of in the terminal. |
| `--json` | Print the payload as JSON instead of a QR code, **with the secrets replaced by `<hidden>`**. |
| `--json --show-secrets` | Print the full payload, secrets included. For debugging only. |
| `--home <dir>` | Use another config dir. |

If the terminal QR code won't scan (small font, odd colors), use `--open`.

## 3. The Cloudflare service token

Only needed when the app should work away from home through the Cloudflare tunnel
([cloudflare.md §3](cloudflare.md#3-create-the-service-token)). Give it in **one** of these ways;
the first one present wins:

1. `--cf-file <path>`: a text file with the two lines the Cloudflare dashboard shows when you
   create the token:

   ```
   CF-Access-Client-Id: <id>.access
   CF-Access-Client-Secret: <secret>
   ```

   `curl` lines such as `-H "CF-Access-Client-Id: ..."` work too. Delete the file afterwards.
2. `--cloudflare`: `pair` asks for both values with hidden input.
3. The environment variables `SHOUTABOUT_CF_CLIENT_ID` and `SHOUTABOUT_CF_CLIENT_SECRET` (the same
   ones `doctor` uses).

Without any of them, the payload has no Cloudflare keys. Giving only one of the two values is an
error.

## 4. Payload format

The QR code holds one compact JSON object (UTF-8, no spaces). The app reads exactly these keys:

```json
{
  "shoutabout": 1,
  "lan_url": "http://192.168.1.50:8765",
  "public_url": "https://dictate.example.com",
  "token": "<hub token>",
  "cf_client_id": "<id>.access",
  "cf_client_secret": "<secret>",
  "ssid": "<home Wi-Fi name>"
}
```

| Key | Type | Meaning |
|---|---|---|
| `shoutabout` | number | Payload version, currently `1`. The app should refuse versions it doesn't know. |
| `lan_url` | string | The hub on the home network: `http://<lan-ip>:<port>`. No trailing slash; the app adds `/transcribe`, `/vocab`, `/health`. |
| `public_url` | string or `null` | The hub through the Cloudflare tunnel: `https://<host>`, or `null` for LAN only. |
| `token` | string | The hub token, sent as `Authorization: Bearer <token>` on both routes. |
| `cf_client_id` | string, optional | Cloudflare Access service token Client ID (`CF-Access-Client-Id` header). Missing when not given. |
| `cf_client_secret` | string, optional | Its Client Secret (`CF-Access-Client-Secret` header). Present exactly when `cf_client_id` is. |
| `ssid` | string or `null` | The home Wi-Fi name, for choosing the LAN route. `null` if unknown. |

The two URLs may carry a path prefix if the hub sits behind one (`https://example.com/hub`).

## Security

- **The QR code is a key to your hub.** Anyone who scans it can send audio to the hub, read and
  change your vocabulary, and (with the Cloudflare values) reach it from the internet. Show it
  only on your own screen, to your own phone. Don't photograph it, screenshot it, share your
  screen while it's up, or paste the `--show-secrets` JSON anywhere.
- `pair` keeps the secrets out of everything else it prints. `--json` hides them unless you add
  `--show-secrets`. `--open` deletes its image when you press Enter; if it can't, it tells you
  the file to delete.
- Close or clear the terminal after scanning: the QR code stays in the scrollback.
- **If the QR code leaked** (or a phone with the app is lost):
  1. Rotate the hub token: `.\.venv\Scripts\python.exe -m shoutabout init --rotate-token`, then
     restart the hub. The old token stops working.
  2. If the payload had the Cloudflare values, delete or rotate the service token
     ([cloudflare.md §11](cloudflare.md#11-rotating-secrets)).
  3. Run `pair` again and scan the new code with your phone. Update the Dictate and Teach
     ShoutAbout shortcuts too, if you use them ([ios-shortcuts.md §5](ios-shortcuts.md#5-answer-the-import-questions)).
- The LAN route is plain HTTP, as for the shortcuts: the token crosses your home Wi-Fi
  unencrypted ([ios-shortcuts.md §12](ios-shortcuts.md#12-security-notes)).
