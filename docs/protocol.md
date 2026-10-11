# The phone protocol (v2), as the PC implements it

This is how the iPhone app finds a ShoutAbout PC, pairs with it and talks to it. The PC side
lives in `src/shoutabout/` (`discovery.py`, `pair_requests.py`, `devices.py`, `identity.py`
and the endpoints in `server.py`). Everything here runs over the home network only.

1. [Discovery](#1-discovery-dns-sd)
2. [Unauthenticated endpoints](#2-unauthenticated-endpoints-home-network-only)
3. [Pairing, step by step](#3-pairing-step-by-step)
4. [Device tokens](#4-device-tokens)
5. [Authenticated endpoints](#5-authenticated-endpoints)
6. [Errors and limits](#6-errors-and-limits)
7. [Security notes](#7-security-notes)
8. [Trying it with curl](#8-trying-it-with-curl)

## 1. Discovery (DNS-SD)

While the hub runs it announces itself with DNS-SD (Bonjour/mDNS):

| | |
|---|---|
| Service type | `_shoutabout._tcp`, domain `local.` |
| Port | the hub's port (`[server] port`; new installs default to 47821) |
| Instance name | the PC's friendly name: the Windows computer name, or the name set in Settings > This PC. Dots and backslashes become spaces; at most 63 bytes of UTF-8. |
| Host | `<computer name>.local` (Windows' own mDNS responder answers for it) |
| TXT `v` | `1` (protocol version) |
| TXT `id` | the installation's UUID (`identity.json`; never changes) |
| TXT `name` | the friendly name, exactly as shown in the app |
| TXT `ver` | the app version, e.g. `0.1.0` |

There are no secrets in the announcement. The phone should identify "its" PC by `id`, not by
name or address: both can change.

**How:** `DnsServiceRegister` / `DnsServiceDeRegister` from Windows' `dnsapi.dll` (Windows 10
1809 or later) through ctypes: no third-party library. The hub registers once it is
listening, re-registers after a rename or a port change, and deregisters on exit. Without
DNS-SD (older Windows, or `[server] discovery = false`) the phone can still pair with the QR
code ([pairing.md](pairing.md)).

**iPhone side:** `NWBrowser(for: .bonjour(type: "_shoutabout._tcp", domain: nil))`; the app's
Info.plist needs `NSBonjourServices = ["_shoutabout._tcp"]` and an
`NSLocalNetworkUsageDescription`.

## 2. Unauthenticated endpoints (home network only)

These answer **only on the LAN route**: the TCP peer must be in `[server] lan_networks`
(private ranges). Through the Cloudflare tunnel or from the PC itself (loopback) they answer
`404` as if they didn't exist; from a public address every path is `403`, as always. Routing
uses the TCP peer address only: `X-Forwarded-For`, `Cf-Connecting-IP`, `Forwarded` and
friends are ignored.

### `GET /hello`

```json
{"app": "shoutabout", "v": 1, "id": "6f1c2a8e-0b7d-4a43-9d5e-2c1b0a9f8e7d",
 "name": "DESKTOP-ABC", "version": "0.1.0", "pairing": true}
```

`pairing` is `false` when the hub has no window to ask in (it runs without a tray, e.g.
`run --no-tray`); `/pair/request` then answers `503`. The phone can also use `/hello` as a
quick "is my PC here?" check (rate limit below).

### `POST /pair/request`

Body (JSON, at most 2 KB; unknown fields are ignored):

```json
{"device_name": "Sam's iPhone", "device_id": "0d5e9b1a-7c3f-4e2d-8a6b-1f0e9d8c7b6a"}
```

* `device_name`: 1-60 characters after cleaning (control characters, bidi overrides and
  zero-width characters are removed, whitespace is collapsed).
* `device_id`: the phone's own UUID (any standard UUID spelling; stored in canonical form).
  Pairing again with the same `device_id` replaces that phone's old token.

`202`:

```json
{"request_id": "Gg0COuGpnI3jToOubh_kgDnN9eftqsQu", "code": "4821", "expires_in": 120}
```

* `request_id`: 192 random bits, URL-safe base64 (32 characters). Keep it secret: it
  collects the token.
* `code`: 4 random digits. The phone shows it; the PC shows the same code in its prompt.

The PC immediately shows an approval prompt (a small always-on-top window, or inline in the
setup window when it's open on the iPhone card):

> **Sam's iPhone wants to dictate to this PC.**  CODE **4821**  [Allow] [Don't allow]

### `GET /pair/status?request_id=…`

The phone polls this once a second for up to 120 seconds:

| Body | Meaning |
|---|---|
| `{"status": "pending"}` | Still waiting for a click on the PC. |
| `{"status": "approved", "token": "…", "hub": {"id": "…", "name": "…", "lan_url": "http://192.168.1.50:47821"}}` | Allowed. **The token is in this one reply only.** |
| `{"status": "claimed"}` | Allowed, and the token was already collected. |
| `{"status": "denied"}` | Someone clicked Don't allow (or closed the prompt). |
| `{"status": "expired"}` | Nobody answered within 120 s, or the token wasn't collected within 120 s of Allow (it's revoked then). |
| `404` | Unknown `request_id` (or forgotten: finished requests are kept for 10 minutes). |

`hub.lan_url` is the address and port the phone's own request arrived on, so it's always one
that works from the phone. Replies carry `Cache-Control: no-store`.

## 3. Pairing, step by step

```
iPhone                                        PC (hub)
------                                        --------
browse _shoutabout._tcp ------------------->  (announced: name, id, port)
GET  /hello                       <-------->  200 {"id", "name", "pairing": true}
POST /pair/request {name, device_id} ------>  202 {"request_id", "code": "4821"}
show "Code 4821"                              prompt: "Sam's iPhone wants to dictate… 4821"
GET  /pair/status (every 1 s)     <-------->  {"status": "pending"}
                                              person clicks Allow (only a click can)
GET  /pair/status                 <-------->  {"status": "approved", "token", "hub"}
store token (Keychain), hub id, lan_url
GET  /pair/status                 <-------->  {"status": "claimed"}
GET  /info  (Bearer <token>)      <-------->  200 {"stt": {"speed": "fast"}, ...}
```

There is no network call that approves or denies: only the buttons on the PC. Nothing is
ever approved automatically.

## 4. Device tokens

* On Allow the PC mints a token: 32 random bytes, URL-safe base64 without padding
  (43 characters).
* `devices.json` (config dir) stores only its **SHA-256**, with the phone's name, id, when it
  was paired and when it was last used. Writes are atomic (temp file + rename). A corrupt
  file is moved aside (`devices.json.bad-<time>`) and no phone token is accepted until a
  phone pairs again; the hub token keeps working.
* `Authorization: Bearer <token>` accepts the **legacy hub token** (existing shortcuts and
  the QR-paired app keep working) **or any device token**. The presented token is hashed
  once and compared with `hmac.compare_digest` against the hub token's hash and every device
  hash, always all of them, so timing says nothing about which (if any) matched.
* Removing a phone (Settings > Phones > Remove, or `python -m shoutabout devices remove
  <name>`) revokes it at once, also when another process removed it: the hub re-reads
  devices.json whenever it changes.
* Failed tokens count towards the same lockout as before (10 failures per 10 minutes per
  source), and on the tunnel route the Cloudflare Access JWT is still required when
  `[access]` is configured.

## 5. Authenticated endpoints

Existing: `POST /transcribe`, `GET`/`POST /vocab`, `GET /health` (no auth) are unchanged
([architecture.md](architecture.md)).

### `GET /info`

```json
{"id": "6f1c2a8e-…", "name": "DESKTOP-ABC", "version": "0.1.0",
 "stt": {"tier": "best", "device": "cuda", "speed": "fast"},
 "cleanup": true, "accepts": ["audio", "text"]}
```

* `stt.tier`: `best` | `balanced` | `light`; `stt.device`: `cuda` | `cpu`.
* `stt.speed`: `fast` (best), `ok` (balanced), `slow` (light / CPU). When it reads `slow`
  the phone should transcribe on-device and send only the text (`POST /paste`).
* `cleanup`: whether local LLM cleanup is on.
* The values come from the automatic model pick (`modelpick.resolve(config)`, cached for
  five minutes and refreshed after a settings change). If that can't be worked out, they're
  derived from `[stt] device` (cuda = best/fast, cpu = light/slow).

### `POST /paste`

Body: `{"text": "…"}` (at most 10,000 characters). The text is pasted **exactly like a
transcription result**: whitespace collapsed to one paragraph (newlines never reach the
paste target), control characters dropped, the same paste injector and paste-chord rules
(Ctrl+V, or Shift+Insert for listed terminals), the same `trailing_space` setting, written
to `history.jsonl` like a dictation. LLM cleanup is **not** applied: the phone already made
the final text. The text is never interpreted, and only its length is logged.

| Reply | When |
|---|---|
| `200 {"status": "ok", "pasted": true}` | Pasted. |
| `200 {"status": "warning", "pasted": false, "warning": "…"}` | Couldn't paste (admin window, locked PC, pasting turned off); the warning says where the text is, e.g. "…text is on your clipboard." |
| `409 {"detail": "Hub is paused — not pasted on the PC.", "status": "paused", "pasted": false}` | ShoutAbout is paused. |
| `413` | Over 10,000 characters (or the body is over 160 KB). |
| `422` | Not JSON, no `text` string, or only whitespace. |
| `503` | The paster isn't ready (very early during start-up). |

`/paste` works while the speech model is still loading.

## 6. Errors and limits

| Endpoint | Limit | Over it |
|---|---|---|
| `POST /pair/request` | 5 a minute per source address, 30 a minute in total; one pending request at a time; a minute's cool-down for an address after Don't allow | `429` (with `Retry-After`), `409` when another phone is waiting |
| `GET /hello`, `GET /pair/status` | 120 a minute per source address (the phone polls once a second) | `429` |
| Authenticated endpoints | as before: `rate_limit_per_minute` per (route, address) and the auth-failure lockout | `429` |

Bad input: `422` for a body that isn't the expected JSON (garbage, wrong types, too deeply
nested); `413` for an oversized body (checked from `Content-Length` and while streaming).

## 7. Security notes

* **Approval needs a person at the PC.** The prompt doesn't take the keyboard focus, has no
  default button (no key press can allow), arms Allow only after a second (a click meant for
  the window underneath can't land on it), and closing it means Don't allow. It closes by
  itself when the request expires or is answered elsewhere.
* **The code** lets the person match the prompt to their own phone. A device on the Wi-Fi
  can name itself anything, so the prompt asks "Same code on your iPhone?".
* **Pairing is invisible from the internet and from the PC itself** (tunnel/loopback = 404).
* **Tokens:** device tokens are stored hashed; the plain token exists only in the one
  status reply and on the phone. An approved token that isn't collected within 120 s is
  revoked.
* **Plain HTTP on the LAN** (as in v1): someone sniffing the home Wi-Fi could see a token in
  transit. WPA2/3 keeps outsiders out; LAN HTTPS is future work.
* **Names are cleaned** before they reach a prompt, a log line or devices.json; logs record
  only their length.
* **No secrets in DNS-SD.**

## 8. Trying it with curl

From another device on the Wi-Fi (not the PC itself: that's the loopback route):

```sh
curl http://192.168.1.50:47821/hello
curl -X POST http://192.168.1.50:47821/pair/request \
     -H 'Content-Type: application/json' \
     -d '{"device_name":"curl test","device_id":"0d5e9b1a-7c3f-4e2d-8a6b-1f0e9d8c7b6a"}'
# click Allow on the PC, then:
curl 'http://192.168.1.50:47821/pair/status?request_id=<request_id>'
curl -H 'Authorization: Bearer <token>' http://192.168.1.50:47821/info
curl -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
     -d '{"text":"Hello from curl."}' http://192.168.1.50:47821/paste
```

Remove the test device afterwards: Settings > Phones > Remove, or
`python -m shoutabout devices remove "curl test"`.
