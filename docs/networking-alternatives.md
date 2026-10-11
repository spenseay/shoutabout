# Networking alternatives

v1 uses two transports:
- the home LAN, over plain HTTP to `http://<lan-ip>:8765`
- Cloudflare Tunnel with Cloudflare Access, at `https://dictate.shoutabout.fyi`

This page covers how the hub stays independent of either one, how to run LAN-only, how
Tailscale would fit, and the likely direction for a product version. **Only the LAN and Cloudflare
paths are implemented and tested in v1. Everything else here is a sketch.**

## The hub is transport-agnostic

The hub only sees an HTTP request from a TCP peer address. Its rules don't depend on Cloudflare:

| Layer | Depends on Cloudflare? | Notes |
|---|---|---|
| Source-IP allow-list | No | Loopback is the `tunnel` route; private ranges in `[server] lan_networks` are the `lan` route; everything else gets 403. |
| Bearer token (`Authorization: Bearer <hub token>`) | No | Required on `/transcribe` for every route. |
| API (`POST /transcribe`, `GET /health`) | No | Same contract whatever carries the request. |
| Cloudflare Access JWT check | Yes, **optional** | Applies only to the `tunnel` route, and only when `[access] team_domain` and `aud` are set. |

Any transport that delivers the request to the hub works with no code changes, as long as the
request arrives either from loopback (a local reverse proxy or connector) or from a private
address you have listed in `lan_networks`.

## LAN-only (no Cloudflare)

Use this if you only dictate at home, or don't want a public hostname at all.

1. Install with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
   -SkipCloudflare`. It creates no tunnel route and no Access app, and leaves
   `install-state.json` with `tunnel_mode = none`.
2. In the **Dictate** Shortcut, use **only** the LAN URL (`http://<lan-ip>:8765/transcribe`) and
   drop the Wi-Fi-name check and the tunnel branch. The Location permission isn't needed either.
3. Give the PC a DHCP reservation on the router so the LAN IP doesn't change.
4. Run `doctor --skip-tunnel` to check the setup.
5. Optional hardening: set `[server] host = "0.0.0.0"` and narrow `lan_networks` to your subnet,
   e.g. `["192.168.1.0/24"]`.

Limitations:
- **Nothing works away from home.** On cellular, or on any other Wi-Fi, the Shortcut fails with
  "Could not connect to the server".
- The hub logs the "Access JWT validation is NOT configured" warning at startup. With no tunnel
  this is harmless: nothing reaches loopback except local processes, and those still need the
  bearer token.

## Tailscale (not implemented)

[Tailscale](https://tailscale.com) builds a WireGuard mesh between your devices. It could replace
both the LAN path and the Cloudflare path with one URL that works everywhere. **This hasn't been
tried with ShoutAbout.**

Sketch:

1. Install Tailscale on the PC and on the iPhone and sign both into the same tailnet. Keep the
   iPhone VPN connected. Tailscale's iOS app supports VPN On Demand.
2. Find the PC's Tailscale IPv4 address (`100.x.y.z`, shown by `tailscale ip -4`) or its MagicDNS
   name (`<pc-name>.<tailnet>.ts.net`).
3. Allow Tailscale peers as a LAN source in `config.toml`. The config validator accepts the CGNAT
   range because it is private address space:
   ```toml
   [server]
   lan_networks = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]
   ```
   Restart the hub afterwards.
4. Open the firewall for the Tailscale interface. The setup rule is limited to the **Private**
   profile and `RemoteAddress LocalSubnet`, and Tailscale peers are not in the local subnet.
   Untested example, elevated:
   ```powershell
   New-NetFirewallRule -Name 'ShoutAbout-Hub-TCP-8765-tailscale' -DisplayName 'ShoutAbout Hub (Tailscale)' `
     -Group 'ShoutAbout' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 `
     -RemoteAddress 100.64.0.0/10 -InterfaceAlias 'Tailscale'
   ```
5. In the Shortcut, use `http://100.x.y.z:8765/transcribe` as the only URL; the Wi-Fi-name branch
   is no longer needed. Prefer the IPv4 address. The hub binds IPv4 `0.0.0.0` only, and MagicDNS
   can return Tailscale IPv6 addresses (`fd7a:115c:a1e0::/48`), which the hub isn't listening on.

The traffic is WireGuard-encrypted from end to end, so plain HTTP inside the tunnel is acceptable.
The bearer token is still required.

| | Cloudflare Tunnel + Access (v1) | Tailscale |
|---|---|---|
| Encryption | TLS from the phone to Cloudflare's edge, then the tunnel to the PC. **Cloudflare terminates TLS**, so it sees plaintext in transit. | WireGuard end to end. DERP relays forward encrypted packets only. |
| Extra software on the phone | None (Shortcuts only) | The Tailscale app, with its VPN connected |
| Public exposure | A public hostname, gated by Access | None. Only tailnet members can connect. |
| Needs a domain | Yes (`shoutabout.fyi`) | No |
| Home vs away | Two URLs; the Shortcut picks one from the Wi-Fi name | One URL |
| Accounts | A Cloudflare account (free plan is enough) | A Tailscale account; the coordination server is a third-party service |
| Extra auth layer | Access service token plus hub-side JWT validation | Tailnet ACLs (no hub-side check) |
| Battery / interference | None on the phone | VPN always on; it can conflict with other VPNs on the iPhone |

**Why it's out of scope for v1:** Tailscale is an account-based third-party service, it requires
an always-on VPN on the phone, and Cloudflare was already set up on this PC. It remains a
reasonable personal alternative.

## HTTPS on the LAN (future)

On the LAN the hub token and the audio travel as plain HTTP over the home Wi-Fi. To fix that
without installing a custom certificate authority on the phone:

1. Pick a hostname under your own domain, e.g. `hub.lan.shoutabout.fyi`.
2. Get a publicly trusted certificate with **Let's Encrypt DNS-01**, using a Cloudflare API token
   scoped to DNS edit on the `shoutabout.fyi` zone. DNS-01 needs no inbound port, so the PC stays
   unreachable from the internet.
3. Resolve the hostname to the PC's LAN IP. Either publish an A record pointing at the private
   address, or use split DNS on the router.
4. Have the hub serve TLS on the LAN port (uvicorn `ssl_certfile` / `ssl_keyfile`) and renew the
   certificate automatically. Let's Encrypt certificates last 90 days at most, and public
   certificate lifetimes are getting shorter.
5. Change the Shortcut to `https://hub.lan.shoutabout.fyi:8765/transcribe`.

Caveats:
- A public DNS record that points to a private IP reveals your LAN address. That is low risk, but
  worth knowing.
- Some routers' DNS-rebinding protection refuses such answers.
- iCloud Private Relay has broken local name resolution for Shortcuts before, which is why v1
  uses a raw IP.

## Likely product direction (sketch only)

If ShoutAbout becomes a product for people who don't own a domain or a Cloudflare account, the
transport should need no accounts, no DNS and no port forwarding:

1. **QR-code pairing.**
   - The PC shows a QR code with the LAN URL, a per-device key and the hub's public key.
   - The phone app scans it once. Nobody copies tokens by hand. (v1's `token --qr` is the manual
     version of this.)
2. **Local discovery.**
   - The hub advertises `_shoutabout._tcp` over mDNS/Bonjour, so the phone finds it on any LAN
     even after the DHCP address changes.
   - On iOS this needs the Local Network permission and an `NSBonjourServices` entry, which
     means a real app rather than Shortcuts.
3. **Per-device keys and revocation.**
   - Each paired phone gets its own key, stored hashed on the PC. Any phone can be revoked from
     the tray without re-pairing the others.
   - History and logs record which device sent each request.
4. **End-to-end-encrypted relay for remote use.**
   ```
   phone app ==(Noise IK over WebSocket/HTTPS)==> relay <==(outbound WebSocket)== hub on the PC
   ```
   - The hub keeps an outbound connection to a small relay service, so no port forwarding or
     inbound firewall rule is needed.
   - Phone and hub run a Noise-protocol handshake (e.g. `Noise_IK_25519_ChaChaPoly_BLAKE2s`,
     using the X25519 static keys exchanged during QR pairing), so the relay sees only
     ciphertext and timing metadata. It can never read the audio or the text.
   - The relay authenticates devices only to stop abuse (rate limits, quotas); it isn't trusted
     with any content.
   - When the phone is on the same LAN it talks to the hub directly, found through discovery,
     and skips the relay.
5. **Keep the v1 safety properties.** The API can still only paste plain text, never presses
   Enter, never runs commands, and the hub stays non-elevated.

None of this is designed in detail yet. The v1 code keeps it possible: the auth decision sits in
`security.py`/`server.py` behind the source-route abstraction, and the pipeline doesn't know which
transport delivered the audio.
