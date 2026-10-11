# Native iPhone app

`ios/` holds the native SwiftUI app. It works on its own (the iPhone transcribes on-device and
copies the text) or linked to ShoutAbout on a Windows PC (the PC types the text into the window
you're in). It is built, signed and shipped to TestFlight entirely on rented Macs, so you
don't need a Mac: on Codemagic (`codemagic.yaml`, workflow `ios-testflight`; set-up:
[release/codemagic.md](release/codemagic.md)), or with the GitHub Actions workflow below
when GitHub's macOS minutes are available.

Status: v2. It compiles in CI, the logic is unit-tested with a mocked network, and a screenshot
tour runs in the iOS Simulator. Nothing has run on a real iPhone yet (see
[What's untested](#whats-untested)).

## Contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Decisions and tradeoffs](#decisions-and-tradeoffs)
4. [The cross-device contract (phone side)](#the-cross-device-contract-phone-side)
5. [CI: how the app is built](#ci-how-the-app-is-built)
6. [Screenshots](#screenshots)
7. [TestFlight without a Mac](#testflight-without-a-mac)
8. [Branding](#branding) and [App Store readiness](#app-store-readiness)
9. [What's untested](#whats-untested)
10. [Next steps](#next-steps)

## What it does

- **Setup in under a minute (first launch, at most three screens).**
  1. "Where should your words go?" **This iPhone** or **My computer** (both can be used later).
  2. This iPhone: one button asks for the microphone (and speech recognition, so the fallback
     engine also works later in the background). My computer: the PCs running ShoutAbout on
     the same Wi-Fi show up by themselves (Bonjour). Tap yours, then **Click Allow on your PC**
     (the PC shows the same 4-digit code). No QR code, no typing, no token.
     - Nothing found? "Install ShoutAbout on your PC", **Scan a code instead** (the older QR
       pairing), and under **More options** typing the PC's address or the old manual form.
  3. One press to talk. iOS doesn't let an app assign the Action Button or add a Control Center
     control by itself, so this is as close as it gets: the exact steps (Settings › Action
     Button › swipe to Controls › Choose a Control › ShoutAbout › Dictate) with an **Open
     Settings** button (`UIApplication.openSettingsURLString`, which lands on ShoutAbout's own
     page; the line under it says to go back to the main list), and an **Add to Control
     Center** card (swipe down › + › Add a Control › ShoutAbout › Dictate). The model identifier
     tells whether this iPhone has an Action Button (iPhone 15 Pro and later); without one the
     page leads with Control Center. **Done**.

  Every step can be skipped and redone (Settings › Connect a computer, Settings › Set up the
  Action Button / Add to Control Center, Settings › Advanced › Show the setup again).
- **One screen.**
  - Top: a small **Computer | Phone** switch, only when a computer is paired; the computer side
    shows the PC's name.
  - Middle: the big mouth button. Tap to talk, tap to send. While you talk the mouth opens and
    closes with your voice and punches on loud peaks; while it works it chews (saTalk) with a
    shimmer; when the words land (pasted or copied) it punches with a haptic and **zips shut**
    (the desktop's "Paddle pull" finish, ~0.7 s: a white slider zips the mouth into an ink line
    with white teeth, the pull swings and settles, the line flattens away and the idle mouth
    pops back; with Reduce Motion the sealed zipper just fades into it); on failure the tile
    turns red, shakes and offers **Retry** (the audio is kept).
  - Under it: one status line ("Your words land where you're typing on DESKTOP-ABC.").
  - Bottom: the last result as a compact card, always above the home indicator, with a copy
    button. **History** and **Settings** are small round buttons that open sheets.
  - The button is the app icon: a yellow tile with the mouth (black mouth, white teeth, red
    tongue, no ring) and a thin ink edge, on a yellow ground (light) or black (dark).
- **The Action Button pops up a mouth, like the old Siri.** `DictateIntent` records without
  opening the app. A little mouth appears in the Dynamic Island (and on the Lock Screen):
  - compact: the mouth opens with your voice on the left, the elapsed time on the right;
  - minimal: the mouth;
  - expanded (long-press): a big mouth, "LISTENING · to DESKTOP-ABC", the time and a red **Stop**;
  - Lock Screen: the same as a banner.

  Press the Action Button again (or Stop) and it sends: the mouth chews ("Sending"), then it
  zips shut and the island says "Pasted" or "Copied" (expanded / Lock Screen: "PASTED! on
  DESKTOP-ABC"). Live Activities can't run animations, so the app sends three stills along the
  zip (0.25 s apart) and the island animates between them; the sealed zipper stays about
  2 seconds, then the activity ends with it as the final content (left on the Lock Screen
  2 seconds more). No check marks. On failure it shows a short reason for 4 seconds.
  The first time, iOS itself asks "Allow Live Activities from ShoutAbout?" on the Lock Screen.

  **The clipboard from the background.** iOS keeps apps in the background away from the
  clipboard, and a dictation from the Action Button, Control Center or Stop finishes with the
  app in the background. So the app tries anyway (the pasteboard's change count says whether
  it took); when it didn't, the text waits as a `PendingCopy` in the App Group, the island says
  "Copy ›" (expanded / Lock Screen: "READY! Tap here to copy it") for 8 seconds, and the app
  writes it the moment it's in front: a tap on the Live Activity opens it (or just switch to
  it). A waiting copy is only applied for 10 minutes, so an old dictation never replaces
  something copied much later; after that the main screen says "Ready!" and its copy button
  does it. In the foreground (the in-app button) it's copied right away.
- **Where the words go** (details in [the contract](#the-cross-device-contract-phone-side)):
  - **Phone**: the text goes to the clipboard. At home, a paired PC that isn't slow writes it
    (best quality, nothing is pasted on the PC); otherwise the iPhone transcribes on-device.
  - **Computer** at home: the audio goes to the PC, which transcribes and pastes. If the PC says
    it's slow (CPU only), the iPhone transcribes and sends only the text, which is faster.
  - **Computer** away from home: the iPhone transcribes and copies, and says so: "Your PC isn't
    reachable, so it's on your clipboard."
  - The words are never lost: any problem with the PC falls back to the iPhone and the
    clipboard, with a short note.
- **Control Center**: **Dictate** (the same background recording) and a **Computer / Phone**
  toggle. **Lock Screen widget**: the mouth; tapping it opens the app recording.
- **Siri / Shortcuts**: "Dictate with ShoutAbout", "Open ShoutAbout and dictate" (the
  foreground fallback), "Toggle ShoutAbout target", plus Set Dictation Target.
- **History**: off by default (Settings › Keep history: "Saves what you dictate on this phone
  so you can copy it later."). An install that already had saved dictations when the switch
  arrived starts with it on, so nobody silently loses their list. On: the last 200 results, on
  this iPhone only (excluded from backups); swipe to copy or delete. Off: the latest result
  still shows on the main screen for the session, History says "History is off" with a button
  to turn it on, and turning it off with saved dictations asks keep or clear.
- **Vocabulary**: the PC's custom words (`GET/POST /vocab`), cached for dictating away from
  home. Fetched every time the app comes forward and the PC answers, when a recording starts
  and after it (on the home network), right after adding a word, and over the tunnel at most
  hourly when away. On-device text then gets the custom spelling, also for words that only
  sound alike ("Sophia" -> "Sofia", see `VocabFixer`).
- **Advanced** (Settings): the on-device engine (Automatic by default), downloading the model
  ahead of time, the older code pairing (QR or manual), and the owner's Cloudflare tunnel
  ("Use my tunnel when away"). The loud red alternate icon is in Settings.

## Architecture

```
ios/
  project.yml                 XcodeGen spec (the .xcodeproj is generated, never committed)
  Config/Base.xcconfig        BUNDLE_ID_PREFIX, APP_GROUP_ID, DEVELOPMENT_TEAM (empty), versions
  Privacy/{App,Widgets}/      PrivacyInfo.xcprivacy for the app and the widget extension
  scripts/make_icons.py       pure-Python (zlib) renderer for the app icons
  Shared/                     compiled into the app AND the widget extension
    Core/                     pure Foundation logic, also compiled into the unit tests
      Models.swift            DictationTarget, Route, OfflineEnginePreference, HistoryItem
      HubAPI.swift            /transcribe + /vocab wire types, HubFailure, HubEndpoint, parsing
      HubProtocol.swift       v2 wire types: /hello, /pair/*, /info, /paste, CachedHubInfo
      HubTransport.swift      the HubTransport protocol, PairedComputer, ComputerLink,
                              DiscoveredComputer (Bonjour TXT)
      DictationRouter.swift   where a recording goes (the v2 phone routing), with notices
      DevicePairing.swift     /hello -> /pair/request -> poll /pair/status
      DictationActivityState  the Live Activity's content + level throttle
      Pairing.swift           legacy pairing QR payload + URL rules (private hosts, VPN)
      RoutePlanner.swift      which failures are worth trying the next route for
      VocabFixer.swift        custom spelling of vocab terms in on-device transcripts (same
                              spelling, then sound-alike)
      SoundAlike.swift        phonetic key (simplified Metaphone), edit distance, common words
      SharedSettings.swift    App Group defaults (target, paired computer, /info cache, keep
                              history, pending copy, ...)
      DeviceFeatures.swift    does this iPhone have an Action Button (model identifier)
    LiveActivity/             DictationActivityAttributes (ActivityKit)
    UI/Theme.swift            ALL brand tokens (colours, type, spacing, geometry, motion)
    UI/Mouth.swift            MouthView (animatable shapes), TalkingMouthView, .shoutPunch()
    UI/ZipMouth.swift         ZipMouthView: the "done" zip, drawn from a ZipPose
                              (Core/ZipTimeline.swift: geometry + timeline, unit-tested)
    Intents/                  DictateIntent, StopDictationIntent, DictateInAppIntent,
                              Toggle/Set target intents, the Control Center toggle intent
  ShoutAbout/                 the app
    App/                      ShoutAboutApp (+ App Shortcuts), AppModel (state machine,
                              Debug screenshot scenes), DictationLauncher (intent entry points)
    Services/                 HubClient (LiveHubTransport), ComputerFinder (NWBrowser +
                              resolution), LiveActivityController, AudioRecorder,
                              OfflineTranscriber, KeychainStore, NetworkPath, HistoryStore
    Views/                    RootView, OnboardingView, ConnectComputerView (+ PairingModel),
                              MainView, HistoryView, SettingsView (+ Advanced), PairingViews
                              (legacy QR/manual), VocabView
    Resources/                Assets.xcassets (icons, launch colour), Fonts (+ OFL licences)
  ShoutAboutWidgets/          widget extension: Control Center controls, Lock Screen widget,
                              DictationLiveActivity (Dynamic Island + Lock Screen UI)
  ShoutAboutTests/            XCTest logic tests (no host app), mocked transport
  ShoutAboutUITests/          XCUITest screenshot tour (Debug sample data)
```

A dictation, step by step:

```
Action Button -> DictateIntent (app process, background)
   -> LiveActivityController.start      (BEFORE the mic: iOS stops a background recording
                                          without a Live Activity)
   -> AudioRecorder.start                (.m4a, AAC mono 16 kHz 32 kbps, mixable session)
   -> metering: the in-app mouth (20 Hz) + the island mouth (quantised level, <= 4 updates/s)
Action Button again / Stop -> stop -> DictationJob (snapshot of target, computer link, /info)
   -> DictationRouter.run (Shared/Core)
        Phone: PC at home and not slow -> POST /transcribe?target=phone -> clipboard
               otherwise (no PC, away, slow, any PC error) -> on-device -> clipboard
        Computer: GET /hello (1.5 s; /health for older PCs; the paired PC's id must match)
           reachable: /info speed (cached 24 h)
              fast/ok -> POST /transcribe?target=pc (raw body; multipart fallback)
              slow    -> on-device -> POST /paste {"text"}
           unreachable mid-way -> tunnel (if set up) -> on-device + clipboard
           PC answered "no" (401/409/503...) -> on-device + clipboard + note
   -> clipboard (if needed; from the background it may wait for the app: PendingCopy),
      history (if kept), haptic, the mouth zips shut (app + island), ~2.5 s
```

## Decisions and tradeoffs

### Action Button: `AudioRecordingIntent` + a Live Activity

`DictateIntent` adopts `AudioRecordingIntent` **and** `LiveActivityIntent`:

- `AudioRecordingIntent` tells iOS the intent records audio. Apple's documentation: you must
  start a Live Activity when the recording begins and keep it active for the whole recording,
  or iOS stops the recording.
- `LiveActivityIntent` makes iOS run the intent in the **app's** process (even when the Control
  Center control or the Live Activity's button triggers it, which live in the widget
  extension) and lets it start a Live Activity from the background.
- `supportedModes = [.background, .foreground(.dynamic)]`: it runs in the background, and only
  comes forward (`continueInForeground`) when it must: the microphone permission hasn't been
  asked yet, or Live Activities are turned off for the app.
- `authenticationPolicy = .requiresLocalDeviceAuthentication`: from a locked iPhone, iOS asks
  for Face ID first, so nobody can type into your PC with your phone without unlocking it.
- The second press stops and **waits** for the delivery inside `perform()` (plus a
  `beginBackgroundTask`), so iOS keeps the app alive while it uploads.
- The audio session is `.playAndRecord` with `.mixWithOthers`: a background app can't activate
  a session that would interrupt other audio.

The previous foreground behaviour stays available as **Dictate in App**
(`DictateInAppIntent`, `.foreground(.immediate)`).

### The Live Activity can't animate by itself

Live Activities render archived SwiftUI: no timers or repeating animations. So:

- the elapsed time is `Text(timerInterval:)`, which iOS updates by itself;
- the voice level is quantised to 5 steps and sent only when it changes, at most every 250 ms
  (`ActivityLevelThrottle`), and the mouth shapes animate between updates;
- while sending, the app advances the saTalk keyframe about three times a second;
- `LiveActivityBridge` is the only code that touches ActivityKit objects. It is nonisolated and
  passes only ids and Sendable states, so Swift 6 strict concurrency stays happy without
  `@unchecked`.

### Pairing: tap the PC, click Allow

- Discovery is `NWBrowser(for: .bonjourWithTXTRecord(type: "_shoutabout._tcp", domain: nil))`.
  The TXT record gives the PC's id, name and version. A PC on Wi-Fi and Ethernet shows once.
- Resolution opens (and immediately closes) a TCP connection to the Bonjour endpoint with IPv4
  forced, and reads `currentPath.remoteEndpoint`: that gives `http://<private IPv4>:<port>`.
- `GET /hello` confirms it is a ShoutAbout PC with pairing on. `POST /pair/request` makes the PC
  show "<iPhone> wants to dictate to this PC. Code 4821."; the phone shows the same code and
  polls `/pair/status` every second for up to 120 s. Five failed polls in a row (Wi-Fi blip)
  end it; a 404 means the PC forgot the request.
- On approval the **device token** goes into the keychain (`AfterFirstUnlockThisDeviceOnly`,
  App Group access group) and `{id, name, lan_url}` into the App Group.
- If the PC's address changes (a new DHCP lease), the app looks for the same id with Bonjour
  when it comes to the foreground and updates the address after `/hello` confirms the id.
- The iPhone's name in the request is `UIDevice.current.name` (just "iPhone" without a special
  entitlement); the device id is a random UUID created once.

### The token only goes to the paired PC

Before any authenticated call on the LAN, `/hello` must answer within 1.5 s and, when the
pairing recorded the PC's id, with that id. A different device (or another ShoutAbout PC) at the
same address on another network never gets the token. Plain-HTTP LAN URLs must be private
addresses; public URLs must be HTTPS (ATS `NSAllowsLocalNetworking`).

### On-device speech: picked automatically

| Engine | When |
|---|---|
| `DictationTranscriber` (`.longDictation`, contextual strings = the PC's vocab) | Automatic when there are custom words (it's the one that takes them as hints), when `SpeechTranscriber` isn't available, and the Advanced "Custom vocabulary" choice |
| `SpeechTranscriber` (iOS 26 long-form model) | Automatic without custom words, if the iPhone supports it (it ignores hints); also the fallback when `DictationTranscriber` can't run |
| `SFSpeechRecognizer` on-device | last resort (needs the speech permission, asked in onboarding) |

Whichever wrote it, `VocabFixer` then gives the custom words their spelling: same spelling in
another case or spacing ("better shell" -> "Better Shell"), and sound-alikes ("Sophia" ->
"Sofia"): same number of words, the same simplified-Metaphone key and at most 2 edits per
word, evidence the speaker meant it (the recognizer capitalised it, or another word of the
term matched exactly), common English words never turned into a term unless capitalised in
mid-sentence and the term is a name or brand, longest term first. The model downloads once
through `AssetInventory` (Settings › Advanced › Download the on-device model does it ahead of
time).

### Secrets and settings

- Keychain: the device token (v2), and for the older code pairing the hub token and the
  Cloudflare service token. Never synced, never logged, never in the repo.
- App Group `UserDefaults`: the target, the paired computer `{id, name, lan_url}`, the `/info`
  cache, onboarding done, the legacy URLs.
- Unsigned simulator builds have no entitlements; the keychain falls back to the default group.

### Swift 6 and identifiers

- Swift 6 language mode with complete strict concurrency. UI and state are `@MainActor`;
  services are stateless structs/enums or `@MainActor` classes; `@preconcurrency` imports for
  AVFoundation, Speech and Network. The UI-test target uses Swift 5 mode (XCTest UI APIs).
- Identifiers all come from `Config/Base.xcconfig`:

  | Item | Identifier |
  |---|---|
  | App | `$(BUNDLE_ID_PREFIX).app` → `fyi.shoutabout.app` |
  | Widgets | `fyi.shoutabout.app.widgets` |
  | Unit tests / UI tests | `fyi.shoutabout.app.tests` / `fyi.shoutabout.app.uitests` |
  | App Group | `group.fyi.shoutabout.app` |

## The cross-device contract (phone side)

The PC side is specified in the v2 brief and implemented by the Windows app. The phone uses:

| Call | Auth | Used for |
|---|---|---|
| Bonjour `_shoutabout._tcp`, TXT `v`, `id`, `name`, `ver` | none | finding PCs |
| `GET /hello` → `{"app":"shoutabout","v":1,"id","name","version","pairing"}` | none (LAN only) | pairing; the 1.5 s reachability check before every dictation |
| `POST /pair/request` `{"device_name","device_id"}` → 202 `{"request_id","code","expires_in"}` | none (LAN only) | pairing (409 busy, 429 rate limit, 422 bad input) |
| `GET /pair/status?request_id=` → `pending` / `denied` / `expired` / `approved` + `token` + `hub` / `claimed` | none (LAN only) | pairing (polled every 1 s, up to 120 s) |
| `GET /info` → `{"stt":{"speed":"fast|ok|slow"},"accepts":["audio","text"],...}` | bearer | slow PC? (cached 24 h per PC) |
| `POST /transcribe?target=pc` / `?target=phone` (raw audio body; multipart fallback) | bearer | the PC transcribes; `pc` also pastes, `phone` only returns the text |
| `POST /paste {"text"}` → `{"status":"ok","pasted":true}`, 409 when paused | bearer | the phone transcribed (slow PC) |
| `GET /health` | none | reachability for PCs from before `/hello` |
| `GET/POST /vocab` | bearer | custom words |

Routing rules (`DictationRouter`, every branch unit-tested):

- **Phone** → always the clipboard, best quality available. If a paired PC answers `/hello` on
  the LAN and its `/info` speed isn't `slow` (or it has no `/info`, an older PC), the audio goes
  to `POST /transcribe?target=phone`: the PC writes the text and pastes nothing. Otherwise (no
  PC, away, slow PC, or any PC error) the iPhone transcribes on-device. No note either way;
  the island says "Copied ✓" and History shows who wrote it. The tunnel is never used for it.
- **Computer**, on Wi-Fi (or a VPN address such as Tailscale's `100.64.0.0/10` on cellular):
  `/hello` within 1.5 s (on 404, `/health` in the time left), then `/info` (unless cached):
  - `speed` is `slow` and the PC accepts text → on-device, then `POST /paste` (> 10 000
    characters stays on the clipboard);
  - otherwise → upload the audio.
- PC unreachable (or it stops answering mid-upload) → the tunnel if it is set up (Advanced,
  legacy pairing) → otherwise on-device + clipboard + "Your PC isn't reachable, so it's on your
  clipboard."
- The PC answered but can't take it → on-device + clipboard with a note: 401/403 "didn't
  accept this iPhone… Connect again in Settings", 409 "paused", 503/429/5xx "busy", or the PC
  said `pasted: false`.
- Only when the iPhone can't transcribe either does the dictation fail; the audio is kept for
  Retry.

Older PCs keep working: no `/hello` → `/health`; no `/info` → send the audio, like before; the
legacy QR pairing (`python -m shoutabout pair`) and the Cloudflare route are under Advanced.

## CI: how the app is built

The workflow is [`.github/workflows/ios.yml`](../.github/workflows/ios.yml). It runs on pushes and
pull requests that touch `ios/**` or the workflow itself, and by hand (`workflow_dispatch`).

**`build` job** (on push and PR, `macos-26` runner):

1. Select Xcode 26.6 with `xcode-select`. This is the newest GA Xcode on the GA image.
2. `brew install xcodegen`, then `xcodegen generate`.
3. Pick the newest iOS 26+ simulator, preferring **iPhone 17 Pro** (Dynamic Island).
4. `xcodebuild build-for-testing ... CODE_SIGNING_ALLOWED=NO`, which compiles the app, the
   widget extension, the unit tests and the UI-test tour.
5. `xcodebuild test-without-building -only-testing:ShoutAboutTests` runs the logic tests.
6. With `screenshots` ticked (manual run, `testflight` off), the screenshot tour runs too
   (next section).

Superseded runs on the same branch are cancelled (`concurrency`). macOS minutes are billed at
10× on private repos: a push run takes a few minutes, a screenshot run about ten.

To move to Xcode 27, set `runs-on: xcode-27` and `XCODE_VERSION: "27.0"` together. That image
is still a preview on GitHub.

**`testflight` job** (manual dispatch only):

1. A tiny Linux job first checks that the four secrets exist. If any is missing, it fails
   with a clear message and no macOS minutes are spent.
2. The macOS job archives with automatic signing through the App Store Connect API key
   (`-allowProvisioningUpdates -authenticationKeyPath/-ID/-IssuerID`, with `DEVELOPMENT_TEAM`
   from the secret).
3. It exports an `.ipa` with `method = app-store-connect` and cloud signing, then uploads it
   with `xcrun altool --upload-app` using the same key. If altool fails, it retries with
   `xcodebuild -exportArchive` and `destination = upload`.
4. The build number is `<run_number>.<run_attempt>`; the version is `MARKETING_VERSION`.
5. The key file is deleted at the end.

## Screenshots

```powershell
gh workflow run ios.yml --ref v2-ios -f testflight=false -f screenshots=true
gh run download <run id> -n ios-screenshots -D .screens\ios
```

`ShoutAboutUITests/ScreenshotTests.swift` launches the Debug app once per scene with
`-ScreenshotScene <name>` (and `-ScreenshotAppearance dark` for the dark shots). The scenes
(`ScreenshotScene` in `AppModel.swift`, Debug builds only) fill the app with obviously made-up
data: "Sample-Desktop", "Sample text: …", the documentation-only address `192.0.2.10`. No
network, no keychain, nothing saved. Looping animations freeze on one frame in this mode
(`MotionPolicy.frozen`) so XCUITest sees an idle app.

The Live Activity shots start a sample activity from the app, then press Home
(`XCUIDevice.press(.home)`), long-press the island through SpringBoard, or lock the simulator
(`pressLockButton`, a private XCUIDevice selector; skipped if missing). The `island-alert`
scene also sends an update with an alert ~6 s after launch, which makes iOS expand the island by
itself. Screenshots are `XCTAttachment`s; CI exports them with
`xcrun xcresulttool export attachments` and renames them to the attachment names
(`light-05-main-computer.png`, `island-22-expanded-listening.png`, …).

## TestFlight without a Mac

**Use Codemagic now:** GitHub Actions doesn't start macOS jobs on this account, so the
iPhone app is built and uploaded by Codemagic's free plan instead. The click-by-click
guide is [release/codemagic.md](release/codemagic.md) (its Apple part is the same as steps
2-4 and 7 below). The steps below are for the GitHub workflow, which still works as it is.

You need to do these steps once in a browser. Each one is listed with where to click.

### 1. Find your Team ID

[developer.apple.com/account](https://developer.apple.com/account) → **Membership details** →
**Team ID** (10 characters, e.g. `ABCDE12345`).

### 2. Register the identifiers

[developer.apple.com](https://developer.apple.com/account/resources/identifiers/list) →
**Certificates, IDs & Profiles** → **Identifiers**.

Xcode's automatic signing can create these itself, but doing it by hand avoids first-run
surprises.

1. **App Group:**
   1. Click **+**, choose **App Groups**, then **Continue**.
   2. Description: `ShoutAbout`. Identifier: `group.fyi.shoutabout.app`.
   3. Click **Register**.
2. **App ID for the app:**
   1. Click **+**, choose **App IDs**, then **App**.
   2. Description: `ShoutAbout`. Bundle ID: **Explicit**, `fyi.shoutabout.app`.
   3. Under Capabilities, tick **App Groups**.
   4. Click **Continue**, then **Register**.
   5. Open the new App ID. Next to App Groups, click **Configure** (or **Edit**), tick
      `group.fyi.shoutabout.app`, then **Continue** and **Save**.
3. **App ID for the widgets:** repeat step 2 with Description `ShoutAbout Widgets` and Bundle
   ID `fyi.shoutabout.app.widgets`, with the same App Group.

If `fyi.shoutabout.app` is taken, change `BUNDLE_ID_PREFIX` in `ios/Config/Base.xcconfig`. All
the identifiers follow from it.

### 3. Create the app record

[App Store Connect](https://appstoreconnect.apple.com) → **Apps** → **+** → **New App**:

| Field | Value |
|---|---|
| Platforms | **iOS** |
| Name | `ShoutAbout` |
| Primary Language | English (U.S.) |
| Bundle ID | `fyi.shoutabout.app` |
| SKU | `shoutabout-ios` |
| User Access | Full Access |

The name must be unique on the App Store. If `ShoutAbout` is taken, use something like
`ShoutAbout Dictation`. The name under the icon on the phone stays "ShoutAbout"
(`CFBundleDisplayName`).

### 4. Create an App Store Connect API key

App Store Connect → **Users and Access** → **Integrations** → **App Store Connect API** →
**Team Keys** → **Generate API Key** (or **+**).

1. **Name:** `GitHub Actions ShoutAbout`.
2. **Access:** **Admin**.
   - App Manager is enough to upload builds. But the job also has Xcode create the
     certificates and profiles and use Apple's cloud-managed distribution certificate (cloud
     signing), and Apple's tooling has reported "Cloud signing permission error" for
     non-Admin keys.
   - If you'd rather try **App Manager** first, the only risk is a failed run. Regenerate the
     key as Admin if the archive or export step reports a permission error.
3. Click **Generate**, then **Download API Key**. You get `AuthKey_<KEYID>.p8`, and **you can
   only download it once**.
4. Note the **Key ID** (in the key's row) and the **Issuer ID** (shown above the table).

Treat the `.p8` like a password: anyone who has it can manage your apps. After step 5, delete
it from Downloads, or keep it only in your password manager.

### 5. Add the four GitHub secrets

In PowerShell in the repo folder (`gh` is logged in), replace the example values:

```powershell
gh secret set APPLE_TEAM_ID --body "ABCDE12345"
gh secret set APP_STORE_CONNECT_KEY_ID --body "ABC123XYZ9"
gh secret set APP_STORE_CONNECT_ISSUER_ID --body "69a6de70-0000-47e3-e053-5b8c7c11a4d1"
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\Downloads\AuthKey_ABC123XYZ9.p8")) | gh secret set APP_STORE_CONNECT_KEY_P8
gh secret list
```

The workflow also accepts the raw `.p8` text, but base64 avoids line-ending problems.

### 6. Run the workflow

```powershell
gh workflow run ios.yml --ref main -f testflight=true
gh run watch
```

Or go to GitHub → **Actions** → **iOS** → **Run workflow**, pick the branch, leave
**testflight** ticked, and click **Run workflow**.

If the run fails:
- If the archive step fails on signing, read the first `error:` line. The usual causes are a
  pending agreement in App Store Connect (**Business** → accept it), the key role (see step
  4), or a bundle ID mismatch.
- `altool` errors name the problem directly (missing app record, duplicate build number).

### 7. Install with TestFlight

1. Wait until App Store Connect → your app → **TestFlight** shows the build as **Ready to
   Submit** / **Ready to Test**. Processing takes 5 to 30 minutes, and Apple emails you.
   - Export compliance is pre-answered: `ITSAppUsesNonExemptEncryption = NO`, because the app
     uses only HTTPS.
2. Under **Internal Testing**, click **+**, create a group (e.g. `Me`), and add yourself (your
   Apple ID must be a user in **Users and Access**). Enable automatic distribution.
3. On the iPhone, install **TestFlight** from the App Store, open the invite email (or
   TestFlight itself), and tap **Install**.


### 8. First run on the phone

1. Open ShoutAbout. Pick **This iPhone** or **My computer**. For My computer, open ShoutAbout on
   the PC first (same Wi-Fi), tap the PC, and click **Allow** on the PC.
2. Set up the Action Button: **Settings** → **Action Button** → swipe to **Controls** → **Choose
   a Control** → **ShoutAbout** → **Dictate**. (Or **Shortcut** → ShoutAbout → **Dictate**.)
3. Optionally add **Dictate** and **Dictation Target** to Control Center, and the Lock Screen
   widget.
4. Expect these prompts the first time: **Microphone** and **Speech Recognition** (onboarding),
   **Local Network** (finding the PC), and on the first Action Button press possibly a request
   to allow Live Activities.

## Branding

- **Tokens.** Every colour, font, size, radius, mouth proportion and animation constant lives
  in `ios/Shared/UI/Theme.swift`, taken from the brand sheet.
  - Palette: Shout Yellow `#FFE600`, Black `#0A0A0A`, Tongue Red `#FF1A1A`, Teeth `#FFFFFF`.
  - The app draws only the primary mark: black mouth, white teeth band, red tongue, no lip
    ring (`Theme.MouthStyle.standard`), always on a yellow rounded tile like the app icon
    (corner radius 23 % of the tile, mouth 78 % wide, a thin ink edge where it sits on
    yellow). A failed dictation tints the tile red. Light mode has a yellow ground, dark
    mode a black one. In the Dynamic Island and on the Lock Screen the same small yellow
    tile sits inside the black Live Activity.
- **Type.** **Anton** (always upper-case) for headlines; **Archivo** 500/700 for the UI. Both
  SIL OFL 1.1, bundled with their licences (**Settings › Font licenses**). The widget extension
  bundles Anton (and its licence) for the Live Activity headline.
- **Mouth.** `MouthView` draws the mark from the sheet's geometry (two half-ellipse outline,
  teeth band, clipped tongue; OPEN / AJAR / SHUT). saTalk and saPunch are reusable
  (`TalkingMouthView`, `.shoutPunch(trigger:)`).
- **Icons.** `python ios/scripts/make_icons.py` renders them from the same geometry: Primary
  (yellow; the Dark appearance uses it too, no lip ring), Tinted (its greyscale), and the red
  Alt icon (Settings).

## App Store readiness

- `PrivacyInfo.xcprivacy` for the app and the widget extension: no tracking, no tracking
  domains, no collected data (audio and text only go between the user's own devices).
  Required-reason APIs: UserDefaults `CA92.1` (the app's own settings) and `1C8F.1` (shared
  with its own widget extension through the App Group); system boot time `35F9.1` (elapsed
  time between in-app events).
- Info.plist: `NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription`,
  `NSCameraUsageDescription` (QR code only), `NSLocalNetworkUsageDescription`,
  `NSBonjourServices = [_shoutabout._tcp]`, `NSSupportsLiveActivities = YES`, background mode
  `audio`, `ITSAppUsesNonExemptEncryption = NO`.
- Display name **ShoutAbout**; version `MARKETING_VERSION = 1.0.0` in `Config/Base.xcconfig`,
  build number from the CI run.

## What's untested

CI compiles everything, runs the logic tests, and runs the screenshot tour in the Simulator.
None of this has been tried on a real iPhone:

- the **Action Button** running `DictateIntent` in the background: does iOS start the
  microphone and keep it with the Live Activity, from a cold start, from a locked phone (with
  Face ID), with music playing; does the second press stop and send
- the **Live Activity** on a real Dynamic Island and Lock Screen: update rate, the level
  animation, Stop from the island, whether the zip stills animate (or just swap), and how it
  behaves when Live Activities are turned off (the intent should fall back to opening the app)
- **Bonjour** on a real network: the Local Network prompt, discovery, resolution to IPv4,
  re-finding the PC after its address changes
- **pairing with a real PC** (the PC side of `/hello`, `/pair/*`, `/info`, `/paste` is built in
  parallel and hasn't been run against this app)
- recording itself (AAC 16 kHz acceptance, metering), real uploads, the clipboard written from
  the background, on-device transcription and model downloads, haptics
- the Control Center controls, the Lock Screen widget, App Shortcuts phrases and Siri
- keychain access groups with real entitlements, the App Group shared with the extension
- the `testflight` job (signing, export, upload): never run, the secrets don't exist yet

## Next steps

1. Run it on a real iPhone (TestFlight) against the v2 Windows app and walk through
   [What's untested](#whats-untested).
2. **Keyboard extension** ("ShoutAbout keyboard"): a mic key that records and inserts the text
   in any app (the shared keychain group is already set up for it).
3. **Live streaming** to the PC while you talk, so long dictations finish sooner.
4. **On-device Parakeet** (FluidAudio, Core ML) as a stronger engine behind
   `OfflineTranscriber`.
