# ShoutAbout for macOS

The same product as on Windows, as a menu bar app for Apple Silicon Macs: **Talk to this
Mac** (tap Right ⌘, talk, tap Right ⌘ again; the mouth pill at the top centre), pairing the
iPhone app over Bonjour with Allow + code (the v2 protocol, unchanged), the automatic model
choice, the setup window on first run, the ShoutAbout window (Home, Teach words, Recent,
Settings), and Open at login.

The step-by-step for a person installing it is [mac-quickstart.md](mac-quickstart.md).

- [Requirements](#requirements)
- [Building it](#building-it)
- [Architecture](#architecture)
- [Permissions](#permissions)
- [Where things are](#where-things-are)
- [Speech model on Apple Silicon](#speech-model-on-apple-silicon)
- [Distributing to other Macs](#distributing-to-other-macs)
- [What is and isn't verified](#what-is-and-isnt-verified)
- [Troubleshooting](#troubleshooting)

## Requirements

- Apple Silicon (M1 or newer). Intel Macs aren't supported (the build would work, but the
  speech model would be too slow without the performance cores the tiers assume).
- macOS 14 Sonoma or newer: the locked `onnxruntime` (voice activity detection) and `av`
  wheels for arm64 are built for macOS 14.
- About 1.5 GB free: the build (~0.6 GB for `.venv`, ~0.3 GB for the app) and the speech
  model (486 MB).
- No Xcode or developer tools: everything installs as prebuilt wheels, and the build never
  calls `git`, `lipo` or a compiler.

## Building it

From the source folder (a clone or GitHub's *Download ZIP*):

```bash
bash packaging/macos/build.sh             # build, install in /Applications, open it
bash packaging/macos/build.sh --no-open   # build and install only
bash packaging/macos/build.sh --no-install
```

`build.sh` (everything it prints is also in `packaging/dist-mac/build.log`):

1. checks the Mac (Apple Silicon, macOS 14+); re-runs itself natively if the Terminal runs
   under Rosetta;
2. uses an existing `uv` ≥ 0.8, or installs uv 0.8.24 into `~/.local/bin` with Astral's
   official installer (`https://astral.sh/uv/0.8.24/install.sh`; it doesn't edit shell
   profiles);
3. `uv python install 3.12` (uv's own Python build, which includes Tk:
   `UV_PYTHON_PREFERENCE=only-managed`), then
   `uv sync --locked --python 3.12 --no-dev --group packaging`
   (no `cuda` extra on a Mac; PyObjC comes in through its `sys_platform == 'darwin'`
   markers) and checks that Tk, PyObjC, CTranslate2 and sounddevice import;
4. draws `ShoutAbout.icns` from `shoutabout.brand` (`packaging/macos/make_icns.py`: the
   primary app icon on Apple's 824/1024 grid, PNG slots 16-1024 px including @2x);
5. runs PyInstaller with `packaging/macos/ShoutAbout-mac.spec` → `packaging/dist-mac/ShoutAbout.app`
   (bundle id `fyi.shoutabout.mac`, `LSUIElement`, `NSMicrophoneUsageDescription`,
   `NSLocalNetworkUsageDescription`, `NSBonjourServices`, minimum macOS 14), then
   `packaging/macos/mac_rules.py check` fails the build if PyAV/FFmpeg, an ASIO PortAudio
   or a Windows file got in;
6. clears extended attributes (`xattr -cr`, which codesign requires) and signs ad hoc:
   `codesign --force --deep --sign -` (the identifier is the bundle id, `fyi.shoutabout.mac`);
7. runs the app's self-test (`ShoutAbout.app/Contents/MacOS/ShoutAbout --self-test --text`;
   a failure is reported but doesn't stop the install);
8. packs `ShoutAbout-<version>-mac.zip` (`ditto`) and `ShoutAbout-<version>.dmg`
   (`hdiutil`) next to it;
9. quits a running ShoutAbout, copies the app to `/Applications` (or `~/Applications` if
   that isn't writable) and opens it with `open`, so macOS treats it as its own app
   (permissions are asked for ShoutAbout, not for Terminal).

A locally built app has no quarantine attribute, so Gatekeeper doesn't block it.

From source, without building (for development): `uv sync`, then
`.venv/bin/python -m shoutabout run`. macOS then asks the permissions for **Terminal** (it
is the "responsible" app), and there's no app icon.

## Architecture

The hub is the same code as on Windows (server, pipeline, pairing, devices, model choice,
discovery contract, setup/settings logic). The Mac layer is `src/shoutabout/mac/`; the
shared modules only gained small hooks marked `# v2-macos`.

```
ShoutAbout.app (main process, "hub")                     window process ("mac-ui", on demand)
 main thread: NSApplication (accessory) event loop        main thread: Tk (UiHost)
   menu bar icon + menu (mac/menubar.py)                    the ShoutAbout window (Home, Teach
   mouth pill NSPanel (mac/overlay_mac.py)                  words, Recent, Settings), setup,
   native pairing alert (fallback only)                     pairing prompt (the shared Tk
   app delegate: "reopen" -> the window at Home             windows, with mac/tkmac.py's tweaks)
 Recent (memory), server, engine, warmer, learn      <---- JSON lines on stdin/stdout ---->
 "hotkey-hook": CGEventTap + CFRunLoop (mac/eventtap.py)     open a page, pair, Recent changed;
 "hotkey": HotkeyMachine worker (hotkey.py, shared)          approve/deny, settings saved,
 "permissions": turns the hotkey on once allowed             downloads, Recent, Try again,
 dns-sd (child, behind a watchdog shell)                     hub status (for Home)
```

**Why two processes.** Cocoa needs the main thread for the menu bar, the overlay and
NSApplication's event loop; Tk on macOS also needs the main thread and owns NSApplication
itself. Running both event loops in one process is fragile, so the hub is a pure Cocoa app
and the Tk windows (written for Windows, tested there) run in a child process with their
own main thread. They talk over the child's stdin/stdout (`mac/uilink.py`): the hub sends
`open` (the ShoutAbout window on a page, or setup)/`pair`/`pair-done`/`recent`/`quit`; the
windows call the hub for what only it can do (approve or deny a phone, apply saved settings
live, re-announce a new name, download the speech model, read Recent, Try again, the hub's
status for Home). Recent lives in the hub's memory only: the window process asks for it, and
the hub sends `recent` each time it changes, so an open Recent page shows a new dictation at
once. Files the windows edit directly (config.toml, devices.json, identity.json, the
LaunchAgent) are re-read by the hub by mtime, as on Windows. The child
quits when it has had no window for 1.5 s and the hub agrees (it compares message numbers,
so a request in flight is never lost), and when the hub goes away (the pipe closes). If
Tk can't start, pairing still works: a phone's request falls back to a native NSAlert
after 12 s without an acknowledgement.

**Hotkey** (`mac/eventtap.py`, `mac/keys.py`). A Quartz event tap on its own thread and
CFRunLoop sees key down/up, modifier changes and mouse clicks. Key codes become "virtual
keys" in the same number space as Windows, so the shared `HotkeyMachine` decides
(`MacHotkeyListener` is `HotkeyListener` with this tap): a clean tap of Right ⌘ starts, the
next clean tap stops, Esc cancels, and any chord (⌘C, ⌘Tab, ⌘-click, Shift+⌘...) never
starts listening. Modifier sides are told apart by IOKit's device bits in the event flags.
The tap is **listen-only** when Input Monitoring is allowed; otherwise an ordinary tap,
which macOS allows with Accessibility alone. Either way the callback returns every event
unchanged and never posts or swallows a key. A tap that macOS pauses (slow callback, some
user input) is turned straight back on. The Cmd+V ShoutAbout posts carries a mark in
`kCGEventSourceUserData` and is ignored, like injected events on Windows. The physical key
state isn't polled on macOS (it would need Input Monitoring too).

Default **Right ⌘ Command, press to start and press to stop** (a new Mac install writes
`hotkey = "right_cmd"`, `mode = "toggle"`). MacBook keyboards have no Right Ctrl; fn/Globe
starts macOS dictation or the emoji picker by default; Right Option types characters on
many layouts while held. Settings offers Right ⌘ Command, Right ⌥ Option and Right ⌃
Control (external keyboards), and Hold to talk.

**Overlay** (`mac/overlay_mac.py`). A borderless, non-activating `NSPanel` (it never
becomes key, so the paste lands in your window) at the status-window level, ignoring the
mouse, on every Space and over full-screen apps, at the top centre of
`NSScreen.mainScreen` (the screen with the active window) just under the menu bar. Frames
come from `overlay.frame_at`/`overlay.render` (the Windows overlay's pure functions) at the
screen's backing scale, become CGImages once and are cached; the first listening, thinking,
zip ("done") and cancel frames are rendered in the background when dictation starts, so the
pill appears at once and zips without waiting. Text pills use the bold system font drawn into a grey bitmap. A ~30 fps timer runs
only while the pill is visible.

**Paste** (`mac/paste.py`). Like Windows: keep every pasteboard item and type, put the
text on the general pasteboard marked `org.nspasteboard.TransientType`, `ConcealedType`
(with `[paste] exclude_from_history`) and `AutoGeneratedType`, current host only (no
Universal Clipboard to the iPhone), post **Cmd+V and nothing else** (key down + key up with
only the Command flag, via `CGEventPost`), and restore the old contents after
`restore_delay_ms` unless the pasteboard changed meanwhile. The V key code is looked up in
the current keyboard layout (`UCKeyTranslate` with Command held, on the main thread, as the
Text Input Sources API requires) so Dvorak/AZERTY get Cmd+V too. Without Accessibility,
macOS silently drops posted keys, so that's checked first: every dictation is then copied
instead (left on the clipboard), the pill says **Copied · ⌘V to paste**, and the first one
of a session notifies once. That is the whole experience on a managed Mac that never allows
Accessibility (the hotkey then needs Input Monitoring). With Secure Keyboard Entry active
(password fields), the text stays on the clipboard too.

**Microphone** (`mac/mic_mac.py`). The shared `MicRecorder` (sounddevice → PortAudio's
Core Audio backend). Before opening the device the permission is checked
(`AVCaptureDevice`): not asked yet → macOS asks now and the pill says "Allow the mic";
denied → "Mic blocked" and where to turn it on. (Core Audio hands a refused app silence
rather than an error.)

**Phone audio** (`mac/decode_mac.py`). `afinfo` checks the declared duration, then
`afconvert -f WAVE -d LEF32` decodes to float WAV at the file's own rate, which
`wavdecode` + `resample` turn into 16 kHz mono exactly like the Media Foundation path.
No FFmpeg/PyAV in the app. Containers handed to Core Audio: MP4/M4A, MP3, ADTS AAC, WAV,
FLAC, CAF, AIFF, AMR.

**Discovery** (`mac/discovery_mac.py`). `dns-sd -R <name> _shoutabout._tcp local <port>
v=1 id=… name=… ver=…` (the same TXT keys), started through a `/bin/sh` watchdog that
kills it when ShoutAbout's pipe closes, so a crashed hub is never left announced. The
instance name is the Mac's name (`scutil --get ComputerName`) or the one set in Settings.

**Open at login** (`mac/startup_mac.py`). `~/Library/LaunchAgents/fyi.shoutabout.mac.plist`
runs `…/ShoutAbout.app/Contents/MacOS/ShoutAbout run` at login (`RunAtLoad`, no
`KeepAlive`), attributed to the app through `AssociatedBundleIdentifiers`. On by default for
a new install of the built app; Settings > This Mac switches it.

**Menu** (`mac/menubar.py`). The Windows tray menu (`tray.build_menu`): status, how to
talk, Pause/Resume, Recent…, Teach words…, Settings…, Set up…, Quit, plus a line per missing
permission ("Allow pasting (Accessibility)…") that asks macOS (its dialog) or opens the
right System Settings pane. Recent…, Teach words… and Settings… open the ShoutAbout window
on that page. The icon is the brand tile at 18 pt with a bitmap per screen scale (18, 36,
54 px; `brand.menubar_frames`): at rest the mouth is open with its red tongue, like the app
icon and the Windows tray, with the tray's other states and
animations. It's a colour image, not a template (a template can't show the tongue); the
yellow tile reads on light and dark menu bars. Notifications use `NSUserNotificationCenter`
(a delegate shows them even while ShoutAbout is in front, and a click while a permission is
missing opens "Allow access"), else `osascript`.

**Opening it again** (`reopen.watch_mac`). Finder, Spotlight and Launchpad don't start a
second copy of a running app: they send it a "reopen" Apple event. The hub's application
delegate answers it by opening the ShoutAbout window at Home, with "Allow access" in front
of it while the microphone or Accessibility is missing (`MacHub.reopened`).

**Sharp on Retina.** Tk draws a bitmap one pixel per point, so on a Retina screen every
picture is scaled up 2x. The windows' mouth is therefore vector shapes on Tk's canvas, which
Aqua antialiases and draws at the backing scale (`ui_kit.Mouth`, from
`brand.mouth_polygons`); there is one mouth per screen at most, and none in the window's
tabs or the setup's header.

**Words.** The shared windows say "PC"; the window process rewrites widget texts on their
way to Tk ("Talk to this Mac", "Open at login", "menu bar", "log in"), sets Tk's scaling to
96/72 so sizes match the Windows design, and replaces `tk.Button` (Aqua ignores button
colours) with a flat clickable label (`mac/tkmac.py`). Tk puts the window process in the
Dock, and on macOS `wm iconphoto` would replace its Dock icon with the windows' 32 px photo
(blurry): it's ignored, so the Dock shows `ShoutAbout.icns` (16 to 1024 px).

## Permissions

| Permission | Why | How it's asked | If refused |
|---|---|---|---|
| Microphone | Talk to this Mac | The setup's first screen, "Allow access" (System Settings > Microphone every time, and the first time `AVCaptureDevice.requestAccess` too, with the Info.plist reason); or the first hotkey press | "Allow access" opens at every start; Home's line has **Allow**; pill "Mic blocked" (a notification once); menu line "Allow the microphone…"; "Try it" isn't shown |
| Accessibility | Pasting (posting Cmd+V), and the hotkey (an ordinary event tap) | "Allow access" (`AXIsProcessTrustedWithOptions` with the prompt, which puts ShoutAbout in the list, and System Settings > Accessibility) | "Allow access" opens at every start; Home's line has **Allow**; every dictation is copied ("Copied · ⌘V to paste"); menu line "Allow pasting (Accessibility)…"; the hotkey starts by itself as soon as it's on |
| Input Monitoring | Only if the hotkey can't be heard through Accessibility | Listed in "Allow access" only when Accessibility can't be read; menu line when neither is on | — |
| Local Network (macOS 15+) | The iPhone finding and reaching the Mac | macOS asks itself when needed | Pair with the QR code (Settings > Phones > Add a phone > Scan a code) |
| Firewall (if on) | Incoming phone connections | macOS asks when the server starts | The phone can't reach the Mac |

**Who asks, and nothing fails silently** (`mac/access.py`). Every request is made by the hub
(the window process forwards "Allow" over its link): it's the process that records, taps the
keyboard and posts Cmd+V, and it's ShoutAbout.app as launchd started it. (The window
process is the same executable forked by the hub, so macOS counts it as the app too.)
Every Allow also opens System Settings at the pane, so there is always a switch to flip
even when macOS shows no dialog. **The launch check** (`MacHub.check_access`): without the
microphone or Accessibility ShoutAbout can't work, so on a first run the setup opens on
"Allow access", and at every later start (not just once) "Allow access" opens alone, with a
notification as a second signal ("ShoutAbout needs access", "Click to allow …"; a click
opens the screen too). Opening ShoutAbout again while something is missing shows it in
front of the window, and Home's line for a missing permission has an **Allow** button. A
first-run setup closed with something still missing gets the notification once the window
is gone. The hub's permission watcher wakes as soon
as the setup sees a change and (re)starts the hotkey listener, also when Input Monitoring
turns on after a listen-only tap was made (such a tap hears nothing until it is made
again), so "Try it" works right after Allow, without relaunching.

macOS grants these to the app's **code signature**. An ad-hoc signed app gets a new one at
every build, so after rebuilding, remove ShoutAbout from System Settings > Privacy &
Security > Accessibility (select it, click −) and allow it again. The Developer ID builds
from Codemagic ([release/codemagic.md](release/codemagic.md)) are signed with the same team
and bundle ID every time, which is what macOS checks, so an update keeps the permissions.

`shoutabout doctor` shows the permissions too, but as macOS sees the process that runs it
(Terminal); the app writes its own view to `hub.log` at every start (`permissions:
microphone=…, accessibility=…, input monitoring=…`).

## Where things are

| What | Where |
|---|---|
| The app | `/Applications/ShoutAbout.app` (or `~/Applications`) |
| Settings, hub token, vocab, devices | `~/Library/Application Support/ShoutAbout/` |
| Logs | `~/Library/Logs/ShoutAbout/hub.log` (rotating), `ui.log` (the window process), and `stdout.log` (stray output and tracebacks of both) |
| Open at login | `~/Library/LaunchAgents/fyi.shoutabout.mac.plist` |
| Speech model | `~/.cache/huggingface/hub` (shared with other apps) |
| Single instance | `~/Library/Application Support/ShoutAbout/hub.lock` (an `flock`) |

`SHOUTABOUT_HOME` and `--home` still override the folder (logs then go to `<home>/logs`).

## Speech model on Apple Silicon

Whisper runs on the CPU's performance cores (CTranslate2's arm64 build uses Apple
Accelerate), in `int8`, with one thread per performance core (up to 10):

| Mac | Setup | `speed` |
|---|---|---|
| ≥ 4 performance cores and ≥ 8 GB (every M-series Mac) | Balanced: `small.en`, 486 MB | `ok` (the phone sends audio) |
| anything less | Light: `base.en`, 148 MB | `slow` (the phone sends text) |
| Settings > Quality > Best | `large-v3-turbo` on the CPU | `ok` with ≥ 8 performance cores and ≥ 16 GB, else `slow` |

Text tidying (Ollama) is off on Macs for now. Expected time for a 10 s dictation, **estimated,
not measured** (scaled from the Windows benchmark in [models.md](models.md): `small.en`
int8 took 1.3 s on 4 threads of an i7-14700KF; an M1 performance core does the same int8
work per clock at ~3.2 GHz instead of ~5.5): about 2 s on an M1, 1.5 s on an M2/M3, about
1 s on Pro/Max chips; `large-v3-turbo` would take several seconds. `hub.log` has the real
STT time of every dictation (`timings_ms`), so the first days of use will show the truth.
Levers if it's slow: `[stt] beam_size = 1` (20-35 % faster on the CPU in the Windows
benchmark) or `[stt] tier = "light"`. **Future speed-up:** MLX (Apple's GPU framework) runs
`large-v3-turbo` on the GPU well under a second; it would be a second engine behind the
existing `SpeechEngine` protocol, not built yet.

## Distributing to other Macs

`build.sh` also leaves `ShoutAbout-<version>-mac.zip` and `ShoutAbout-<version>.dmg` in
`packaging/dist-mac/`. They are **ad-hoc signed and not notarized**: copied to another
Mac, Gatekeeper refuses to open them ("cannot be opened because Apple cannot check it",
or "damaged" for a downloaded dmg).

The copies for other Macs are built on Codemagic (`codemagic.yaml`, workflow
`mac-release`; set-up and every step: [release/codemagic.md](release/codemagic.md)):
`build.sh --ci` builds the app, then `packaging/macos/release.sh` signs every nested
library and then the app with a *Developer ID Application* certificate, the Hardened
Runtime and `packaging/macos/ShoutAbout.entitlements` (microphone + PyObjC's JIT stubs;
the reasons for each entitlement, and for the ones left out, are in that guide), runs the
self-test on the signed app, notarizes and staples the app and `ShoutAbout.dmg`, and zips
the stapled app as `ShoutAbout-mac.zip`. `release.sh` also works on any Mac whose keychain
has the Developer ID identity (`--no-notarize` without an App Store Connect API key).

A person can still build it themselves with `build.sh` (a local build is never
quarantined).

## What is and isn't verified

There was no Mac while this was written. **Verified on Windows** (unit tests with fakes,
the full existing suite, ruff): the hotkey key-code and modifier logic and the tap glue
with a fake Quartz (tap, chords, ⌘-click, Esc, hold and toggle, the paused-tap re-enable,
our own Cmd+V ignored, which tap kind for which permissions); the overlay controller with
a fake panel (Retina sizing, caching, fades, stop); pasting with a fake pasteboard and key
poster (markers, restore, no restore over a newer copy, no Accessibility, password fields,
layout lookup); the microphone permission paths; the window-process link over real pipes
and the bridge/child logic (calls, pairing forwarding, acknowledgements and the native
fallback, idle/quit numbering, downloads); Bonjour command lines and the watchdog
lifecycle; the LaunchAgent; afconvert decoding through the real audio chain with a fake
`afconvert`; Apple Silicon detection and tiers; config, paths, self-test markers, the copy
filter, the menu rows, the `.icns` writer (the rendered icon was looked at) and the bundle
rules. Every line of the Cocoa glue (menu bar, panel, tap, pasteboard, permissions, native
prompt, the hub's Mac wiring) also runs against stand-in PyObjC modules, which catches
Python mistakes but not wrong Apple API use. The Mac PyInstaller spec ran on Windows up to
COLLECT (BUNDLE is macOS only), which caught one real bug.

**Not verified at all (macOS only):** every PyObjC/AppKit/Quartz call (status item, menu
delegate, NSPanel, CGImage, text drawing, event tap creation and callbacks, CGEventPost,
NSPasteboard, AVFoundation, AX checks, NSAlert, notifications, the reopen Apple event), Tk
on macOS (the windows' look, the label buttons, activation of the window process),
`dns-sd`/`afconvert` for real, the LaunchAgent at login, PyInstaller on macOS with uv's
Python, codesign, the self-test in the built app, and speech speed on Apple Silicon. The
first run on a Mac is the test; [mac-quickstart.md](mac-quickstart.md) says which logs to
send.

## Troubleshooting

| Problem | Look at / do |
|---|---|
| `build.sh` stops | `packaging/dist-mac/build.log`. `uv` can't download (proxy, no internet) → install uv by hand and re-run. PyInstaller errors → the log's last 50 lines |
| The app opens and closes at once | `~/Library/Logs/ShoutAbout/stdout.log` and `hub.log` ("hub crashed"). Run it in Terminal to see output: `/Applications/ShoutAbout.app/Contents/MacOS/ShoutAbout run --no-tray` (no menu bar; Ctrl+C quits) |
| "another ShoutAbout hub is already running" | One is running (menu bar). Or a stale lock: quit all with `pkill -x ShoutAbout` |
| Hotkey does nothing | `hub.log`: "listening for the dictation hotkey (… tap)"? If "can't use the hotkey": Accessibility. Keys typed in password fields and with Terminal's *Secure Keyboard Entry* are invisible to every app |
| Pasted nowhere | Accessibility (see above); after a rebuild, remove and re-add ShoutAbout in the Accessibility list |
| Windows don't appear | `ui.log` and `stdout.log` (Tk errors from the window process); `hub.log` says "started the window process". Pairing still works through the native alert |
| iPhone doesn't find the Mac | `hub.log`: "announced on the network as …". `dns-sd -B _shoutabout._tcp` in Terminal lists it. Guest/work Wi-Fi often blocks devices from seeing each other; macOS 15: System Settings > Privacy & Security > Local Network > ShoutAbout on |
| Slow | `hub.log` `timings_ms` per dictation; Settings > Quality > Light, or `[stt] beam_size = 1` in config.toml |
| Check everything | `/Applications/ShoutAbout.app/Contents/MacOS/ShoutAbout doctor` and `… --self-test --text` |
