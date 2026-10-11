# Building the iPhone and Mac apps on Codemagic

Codemagic builds both Apple apps on its Macs, for free: the iPhone app goes to TestFlight,
and the Mac app comes back as a signed, notarized `ShoutAbout.dmg` you can install on any
Mac. Nothing is built or installed on your own Mac except the finished app.

You do this once, in a browser, on your Windows PC. It takes about an hour, most of it
waiting for builds. After that, a new version is two clicks (or one git tag).

- [What you need](#what-you-need)
- [Part A: Apple Developer website](#part-a-apple-developer-website-15-minutes)
- [Part B: a signing key on your PC](#part-b-a-signing-key-on-your-pc-2-minutes)
- [Part C: Codemagic](#part-c-codemagic-10-minutes)
- [Part D: build the apps](#part-d-build-the-apps)
- [Part E: the iPhone app from TestFlight](#part-e-the-iphone-app-from-testflight)
- [Part F: the Mac app](#part-f-the-mac-app)
- [Next releases](#next-releases)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works) (for whoever maintains it)
- [Sources](#sources)

## What you need

- Your Apple Developer Program membership (paid), signed in as the **Account Holder**.
- Your GitHub account (the one that owns `spenseay/shoutabout`).
- Your iPhone, and the Mac where the app should end up.
- Nothing to install on the PC: Part B uses `ssh-keygen`, which comes with Windows 11.

Never paste any of the keys below into a chat, an email or a file in the repository.

## Part A: Apple Developer website (15 minutes)

### A1. Agreements

1. Open [appstoreconnect.apple.com](https://appstoreconnect.apple.com) and sign in.
2. If a banner asks you to review or accept an agreement, click it and accept. (Builds fail
   with "A required agreement is missing or has expired" until you do.)

### A2. The app's identifiers

Open [developer.apple.com/account/resources/identifiers](https://developer.apple.com/account/resources/identifiers/list)
(**Certificates, IDs & Profiles** > **Identifiers**).

1. **App Group**
   1. Click the blue **+**. Choose **App Groups**, click **Continue**.
   2. Description: `ShoutAbout`. Identifier: `group.fyi.shoutabout.app`.
   3. Click **Continue**, then **Register**.
2. **App ID for the app**
   1. Click **+**. Choose **App IDs**, **Continue**, then **App**, **Continue**.
   2. Description: `ShoutAbout`. Bundle ID: **Explicit**, `fyi.shoutabout.app`.
   3. In the Capabilities list, tick **App Groups**. Nothing else.
   4. Click **Continue**, then **Register**.
   5. Click the new `fyi.shoutabout.app` in the list. Next to **App Groups**, click
      **Configure** (or **Edit**), tick `group.fyi.shoutabout.app`, click **Continue**, then
      **Save** (and **Confirm** if it warns that profiles will be regenerated).
3. **App ID for the widgets**: repeat step 2 with Description `ShoutAbout Widgets` and Bundle
   ID `fyi.shoutabout.app.widgets`, with the same App Group.

The Mac app needs no App ID.

### A3. The app record in App Store Connect

1. [App Store Connect](https://appstoreconnect.apple.com) > **Apps** > the blue **+** >
   **New App**.
2. Fill in:

   | Field | Value |
   |---|---|
   | Platforms | **iOS** |
   | Name | `ShoutAbout` (if it's taken: `ShoutAbout Dictation`; the name under the icon on the phone stays ShoutAbout) |
   | Primary Language | English (U.S.) |
   | Bundle ID | **ShoutAbout - fyi.shoutabout.app** |
   | SKU | `shoutabout-ios` |
   | User Access | **Full Access** |

3. Click **Create**.

### A4. Your TestFlight group

Testers inside your own team ("internal testers") get every build without Apple's review.
You are not one automatically: you add yourself once.

1. In the app (App Store Connect > **Apps** > **ShoutAbout**), click the **TestFlight** tab.
2. In the sidebar, click the **+** next to **Internal Testing**.
3. Name the group `Me`, tick **Enable automatic distribution**, and click **Create**.
4. Click **Invite Testers** (or the **+** next to Testers), tick yourself, click **Add**.

If the TestFlight tab isn't ready before the first build exists, do this step after the
first iPhone build (Part D1) instead.

### A5. The App Store Connect API key (Codemagic signs in with it)

1. App Store Connect > **Users and Access** > the **Integrations** tab >
   **App Store Connect API** > **Team Keys**.
   - The very first time, there is a **Request Access** button: click it and accept.
2. Click **Generate API Key** (or the **+**).
3. Name: `Codemagic`. Access: **Admin**.
   - Admin, not App Manager: the builds create the iPhone's distribution certificate and
     provisioning profiles through this key. Codemagic's docs suggest App Manager, which
     may be enough; Admin avoids a failed first build.
4. Click **Generate**.
5. Write down two values from this page (they aren't secret, but you need them in Part C):
   - **Issuer ID**: above the table, with a **Copy** button (looks like
     `69a6de70-…-5b8c7c11a4d1`);
   - **Key ID**: in your key's row (10 characters, like `ABC123XYZ9`).
6. Click **Download** in your key's row. You get `AuthKey_<KeyID>.p8` in **Downloads**.
   **Apple lets you download it only once.** It's a password: anyone who has it can manage
   your apps. After Part C, delete it, or keep it only in a password manager.

## Part B: a signing key on your PC (2 minutes)

The certificates that sign the apps (one for the iPhone, one for the Mac) are made for a
private key that only you and Codemagic have. Make it once:

1. Open **PowerShell** (Start, type `PowerShell`, Enter).
2. Paste these two lines and press Enter:

   ```powershell
   New-Item -ItemType Directory -Force "$HOME\ShoutAbout-signing" | Out-Null
   ssh-keygen -t rsa -b 2048 -m PEM -f "$HOME\ShoutAbout-signing\cert_key"
   ```

3. When it asks `Enter passphrase`, press **Enter** (no passphrase), and **Enter** again.
4. You now have `C:\Users\<you>\ShoutAbout-signing\cert_key` (the secret one) and
   `cert_key.pub` (not needed). Keep `cert_key` there, or in your password manager. It is a
   secret: never put it in the repository or send it to anyone.

If PowerShell says `ssh-keygen` isn't recognized: Settings > System > Optional features >
**View features** > **OpenSSH Client** > **Install**, then repeat step 2.

## Part C: Codemagic (10 minutes)

### C1. Account and app

1. Open [codemagic.io/signup](https://codemagic.io/signup) and click **Sign up with GitHub**.
   Authorize Codemagic. Use the **personal account** (no team: the free 500 minutes are for
   personal accounts). Don't enable billing.
2. Click **Add application** > **GitHub** > **Next: Authorize integration** (if asked) >
   **Install GitHub App**. On GitHub, pick your account, choose **Only select repositories**,
   select `shoutabout`, and click **Install & Authorize**.
3. Back in Codemagic, select the repository `spenseay/shoutabout`. For the project type
   pick **iOS App** (any type works: the `codemagic.yaml` file in the repository decides
   what gets built). Click **Finish: Add application**.

### C2. The four secrets

In the app's page, open the **Environment variables** tab. Add these four variables. For
each one: type the **Variable name**, paste the **Variable value**, type the group name
`app_store_credentials` (create it the first time, then pick it), keep **Secret** ticked,
and click **Add**.

| Variable name | Value |
|---|---|
| `APP_STORE_CONNECT_ISSUER_ID` | the Issuer ID from A5 |
| `APP_STORE_CONNECT_KEY_IDENTIFIER` | the Key ID from A5 |
| `APP_STORE_CONNECT_PRIVATE_KEY` | the whole text of `AuthKey_<KeyID>.p8` (below) |
| `CERTIFICATE_PRIVATE_KEY` | the whole text of `cert_key` from Part B (below) |

To copy a file's whole text, paste one of these into PowerShell, then paste (Ctrl+V) into
the Value box. It must include the `-----BEGIN …-----` and `-----END …-----` lines.

```powershell
Get-Content "$HOME\Downloads\AuthKey_*.p8" -Raw | Set-Clipboard
Get-Content "$HOME\ShoutAbout-signing\cert_key" -Raw | Set-Clipboard
```

The group name and the variable names must be exactly as above.

## Part D: build the apps

Codemagic's free plan runs one build at a time; a second one waits in line. Start each
build like this: on the app's page click **Start new build**, choose the **branch** that
has `codemagic.yaml` (`main` once it's merged; until then the branch you were told), pick
the **workflow**, and click **Start new build**.

### D1. The iPhone app: workflow "iPhone app to TestFlight"

Takes about 10-20 minutes. When it's green, the app is uploaded. Apple then processes it
for 5-30 minutes and emails you; then it's in TestFlight (Part E).

### D2. Once: workflow "Mac signing request (once)"

Apple only lets the Account Holder make the Mac app's certificate (a "Developer ID
Application" certificate), on its website. This 2-minute build makes the request file:

1. Run the workflow **Mac signing request (once)**.
2. When it's green, open the build, then **Artifacts**, and download the archive (named
   like `shoutabout_…_artifacts.zip`). Open it: it holds
   `ShoutAbout-DeveloperID.certSigningRequest` and a `README.txt`. Copy the request file to
   your Desktop.
   - If the log says **"Already set up. Nothing to upload"**, skip to D3.
3. Open [developer.apple.com > Certificates](https://developer.apple.com/account/resources/certificates/list),
   click the blue **+**.
4. Under **Software**, choose **Developer ID Application**, click **Continue**.
5. If it asks for a Profile Type / Sub-CA, choose **G2 Sub-CA**. Click **Choose File**,
   pick `ShoutAbout-DeveloperID.certSigningRequest`, click **Continue**.
6. Done. You don't need to download the certificate: Codemagic fetches it.

### D3. The Mac app: workflow "Mac app (signed, notarized .dmg)"

Takes about 15-30 minutes (Apple's notarization is part of it). When it's green, the
**Artifacts** of the build hold:

- `ShoutAbout-mac.zip`: the app, signed and notarized;
- the archive `shoutabout_…_artifacts.zip`, with `ShoutAbout.dmg` inside (Codemagic lists
  .zip files on their own and packs every other file into that archive);
- `ShoutAbout-mac-logs.zip`: the build's logs (send it if something looks wrong).

## Part E: the iPhone app from TestFlight

1. On the iPhone, install **TestFlight** from the App Store and open it once. Use the same
   Apple Account as your developer account.
2. When Apple's email says the build is ready (or after 5-30 minutes), open TestFlight:
   **ShoutAbout** is listed. Tap **Install**. (If it isn't: App Store Connect > the app >
   **TestFlight** > your group `Me` must list you as a tester and the build; with automatic
   distribution off, click **Add Builds** and pick it.)
3. Open ShoutAbout and follow its three setup screens. The first run on the phone is
   described in [docs/ios-app.md](../ios-app.md#8-first-run-on-the-phone).

Builds for internal testers work for 90 days; a new build replaces the old one.

## Part F: the Mac app

On the Mac (no Terminal, nothing to build):

1. Get the file onto the Mac: sign in to [codemagic.io](https://codemagic.io) in Safari
   (with GitHub) and download from the build's **Artifacts**, or copy the file from your PC.
   - `ShoutAbout.dmg` (inside the artifacts archive; Safari unpacks archives by itself):
     double-click it, drag **ShoutAbout** onto **Applications** in the window that opens,
     then eject the disk (the ⏏ next to it in Finder).
   - Or `ShoutAbout-mac.zip`: Safari unpacks it to **ShoutAbout** in Downloads; drag it to
     **Applications**.
2. Open **Applications** and double-click **ShoutAbout**. macOS asks once: *"ShoutAbout" is
   an app downloaded from the Internet. Are you sure you want to open it?* Click **Open**.
3. Then the usual first start: steps 3-6 of [mac-quickstart.md](../mac-quickstart.md#3-say-yes-to-macos-once)
   (microphone, Accessibility, the setup window).

A work Mac may be managed by your company (MDM). If macOS says the administrator blocks
apps from outside the App Store, only your IT department can allow it.

## Next releases

- **By hand**: Part D1 and D3 again (D2 never again, until the certificate expires in
  about 5 years).
- **By tag**: push a git tag whose name starts with `release-` (for example
  `release-1.0.1`) and Codemagic builds both apps, one after the other.
- Raise the version for users in `ios/Config/Base.xcconfig` (`MARKETING_VERSION`) and in
  `pyproject.toml`/`src/shoutabout/__init__.py` (the Mac app). The iPhone build number is
  automatic: the newest TestFlight build + 1.
- Minutes: about 10-20 per iPhone build and 15-30 per Mac build (estimates until the first
  builds show the real numbers on the build page), so a full release of both costs about
  30-45 of the 500 free minutes a month. The counter resets on the 1st.

## Troubleshooting

The build page shows each step; the red one has the error. Common first-build problems:

| What the log says | Why | What to do |
|---|---|---|
| `Missing in the environment group app_store_credentials: …` | A variable is missing or misnamed, or the group isn't called `app_store_credentials` | Part C2: names and group exactly as listed |
| `NOT_AUTHORIZED`, `401`, `Authentication credentials are missing or invalid` | Wrong Issuer ID / Key ID, the .p8 text incomplete, or the key revoked | Re-copy all three from A5/C2 (the .p8 with its BEGIN and END lines) |
| `FORBIDDEN_ERROR`, `403`, `not allowed to perform this operation` | The API key's access is too low | A5 with **Admin**, then update the three API values in C2 |
| `A required agreement is missing or has expired` | Apple updated an agreement | Part A1 |
| `Did not find Bundle ID with identifier fyi.shoutabout.app` | The App IDs don't exist (or have a typo) | Part A2 |
| `… doesn't include the com.apple.security.application-groups entitlement` or `doesn't match the entitlements file's value for … application-groups` | App Groups isn't ticked or the group isn't assigned on one of the two App IDs | Part A2, steps 2.3-2.5 and 3, for **both** App IDs; then run D1 again (a new profile is made automatically) |
| `You already have a current Distribution certificate` / `maximum number of certificates` | Apple's limit on Apple Distribution certificates; the build tries to make one for your key | developer.apple.com > Certificates: revoke an Apple Distribution certificate you don't use, run D1 again |
| `App Store Connect has no app with bundle ID fyi.shoutabout.app` | No app record | Part A3 |
| Upload refused: `bundle version must be higher` | A build with that number exists | Run D1 again (the number is read from TestFlight each time) |
| The build is green but the phone shows nothing | You aren't in the internal group, or automatic distribution is off | Part A4; or add the build to the group by hand |
| `Apple has no Developer ID Application certificate for CERTIFICATE_PRIVATE_KEY yet` | Part D2 isn't done, or the upload was made from another file | Part D2 |
| `isn't a valid signing identity` | The Developer ID certificate expired or was revoked | Revoke it on developer.apple.com, then Part D2 again |
| `notarization of ShoutAbout… ended with 'Invalid'` | Apple found a problem in the app | See below |
| `Signing broke: …` / `the Hardened Runtime broke: …` | The signed app fails a self-test check that passed before signing | Send `ShoutAbout-mac-logs.zip` (it has both self-test reports) |
| `WARNING: PyObjC needed allow-unsigned-executable-memory` | The build passed, with one more exception than planned | Nothing for you; a maintainer adds it to `packaging/macos/ShoutAbout.entitlements` |
| The build waits and never starts ("queued" for 15+ minutes, nothing else running) | That Xcode version's machines are overloaded (it happened to 26.6, the default, in October 2026: [discussion #3570](https://github.com/orgs/codemagic-ci-cd/discussions/3570)), or Codemagic doesn't have the version any more | Cancel the build; a maintainer changes `xcode:` in `codemagic.yaml` to another version on [Codemagic's list](https://docs.codemagic.io/specs/versions-macos/) (usually the one before the default), then start it again |
| "Build minutes exhausted" | 500 free minutes used this month | Wait for the 1st (don't enable billing unless you want to pay) |
| On the Mac: "ShoutAbout is damaged" or "Apple cannot check it" | An old file from before notarization, or a broken download | Download the latest build's file again |

**Reading a notarization rejection.** The build log prints Apple's log right after the
failure ("Apple's notarization log"). The same file, `notary-app-log.json` (the app) or
`notary-dmg-log.json` (the disk image), is in `ShoutAbout-mac-logs.zip`, which the build
writes even when it fails (if the failed build lists Artifacts). Its `issues` list names
each file (`path`) and the reason (`message`), for example:

| `message` | Meaning |
|---|---|
| `The binary is not signed with a valid Developer ID certificate.` | A file was missed by the signing step |
| `The signature does not include a secure timestamp.` | Apple's timestamp server didn't answer for that file; run the build again |
| `The executable does not have the hardened runtime enabled.` | A helper program inside the app wasn't signed with the Hardened Runtime |
| `The binary uses an SDK older than the 10.9 SDK.` | A very old library in a Python package |

Send that file (or the logs zip) to whoever maintains the build; it's never secret.

## How it works

`codemagic.yaml` (repository root) has three workflows, all on Codemagic's free
`mac_mini_m2` machine with Xcode 26.5 (not the default 26.6, whose machines were
overloaded; `.github/workflows/ios.yml` is kept as it is, unused while GitHub Actions is
unavailable).

**ios-testflight**

1. Checks that the four variables exist.
2. Downloads XcodeGen 2.46.0 (checksum-verified, the same pin as
   `ios/scripts/run-on-iphone.sh`): `ios/scripts/install-xcodegen.sh`; `xcodegen generate`.
3. `keychain initialize` (a temporary keychain);
   `app-store-connect fetch-signing-files fyi.shoutabout.app --type IOS_APP_STORE --create`
   fetches, or creates, the App Store provisioning profiles for `fyi.shoutabout.app` and
   every bundle ID under it (the widgets: the prefix match is the CLI's default) and the
   Apple Distribution certificate for `CERTIFICATE_PRIVATE_KEY`;
   `keychain add-certificates`; `xcode-project use-profiles` switches both targets to
   manual signing with those profiles and writes the export options
   (`manageAppVersionAndBuildNumber` off, so the number below is kept).
4. `ios/scripts/next-build-number.sh`: finds the app by bundle ID and asks
   `app-store-connect get-latest-testflight-build-number`; build number = that + 1 (1 for the
   first upload), passed as `CURRENT_PROJECT_VERSION` (app and widgets get the same).
5. `xcode-project build-ipa` (Release, `generic/platform=iOS`). No tests in this workflow, to
   save minutes; the unit tests are in `ios.yml` for when GitHub Actions runs again.
6. Publishing: uploads the .ipa with the same API key. `submit_to_testflight` is off on
   purpose: it starts Apple's beta review, which only external testers need.

**mac-signing-request** runs `packaging/macos/developer-id.sh request`: if Apple has no
Developer ID Application certificate for `CERTIFICATE_PRIVATE_KEY`, it makes a signing
request (`openssl req`) from that key. Apple lets only the Account Holder create Developer
ID certificates, and not through an API key (Apple's Developer ID help page; fastlane's
match docs say the same), which is why this one step is manual.

**mac-release**

1. `developer-id.sh install`: lists the team's Developer ID certificates through the API,
   keeps the one that matches `CERTIFICATE_PRIVATE_KEY`, imports it (with Apple's Developer
   ID intermediate certificates) into a temporary keychain, and hands its SHA-1 to the next
   steps. It's first so a missing certificate fails in a minute. If the machine's CLI tools
   are older than 0.68.0 it upgrades them first: Apple's current Developer ID certificates
   have the type `DEVELOPER_ID_APPLICATION_G2`, which the tools dropped in 0.59.0 and
   brought back in 0.68.0 (their changelog).
2. `packaging/macos/build.sh --ci`: the normal Mac build (uv 0.8.24, uv's Python 3.12,
   `uv sync --locked`, PyInstaller, the bundle rules check that refuses PyAV/FFmpeg, ASIO
   PortAudio and Windows files), without installing, opening, re-signing ad hoc, self-testing
   or packing.
3. `packaging/macos/release.sh`:
   1. the bundle rules check again, then the app's `--self-test` as built;
   2. signs every nested Mach-O file first (`packaging/macos/macho.py` finds them by their
      header, a few hundred: Python extension modules, dylibs, libpython), in parallel, with
      `codesign --force --timestamp`, no entitlements, never `--deep`; helper executables
      (none today) with the Hardened Runtime; nested bundles (none today) after their
      contents; then the app itself with `--options runtime` and
      `packaging/macos/ShoutAbout.entitlements`; verifies (Developer ID authority, runtime
      flag, timestamp, every nested file);
   3. runs the self-test of the signed app and compares: any of `modules`, `dependencies`,
      `vad`, `ctranslate2`, `objc-callbacks` that passed unsigned must pass signed;
   4. notarizes a zip of the app (`xcrun notarytool submit --wait` with the API key written
      to a private temporary file), saves Apple's log, fails unless the status is
      `Accepted`, staples the ticket to the app;
   5. makes `ShoutAbout.dmg` (the stapled app and an Applications shortcut, UDZO), signs
      it, notarizes and staples it; zips the stapled app as `ShoutAbout-mac.zip`;
   6. asks Gatekeeper (`spctl`) about both, writes checksums, and zips the logs.

File names never change; the version is inside the app.

No dependency cache: Codemagic's cache can't store symbolic links, which uv's cache and its
Python installs are made of, and uv's documentation says re-downloading pre-built wheels
is often faster than restoring them in CI. Every macOS dependency in `uv.lock` is a wheel.

### The Mac app's entitlements

Notarization requires the Hardened Runtime, which switches off a few abilities unless the
signature asks for them. `packaging/macos/ShoutAbout.entitlements` asks for two:

| Entitlement | Why |
|---|---|
| `com.apple.security.device.audio-input` | Recording from the microphone. Under the Hardened Runtime an app without it can't use audio input at all. |
| `com.apple.security.cs.allow-jit` | When Cocoa or C code calls Python (the menu's delegates, the hotkey's event tap callback), PyObjC runs small executable stubs it makes at run time. PyObjC's code-signing notes ask for this entitlement first, and Apple's page for it names "certain Python frameworks". It only allows memory mapped with `MAP_JIT`. The self-test's `objc-callbacks` check exercises both kinds of callback in the signed app on the build machine. |

Deliberately left out:

| Entitlement | Why not |
|---|---|
| `com.apple.security.cs.allow-unsigned-executable-memory` | PyObjC's fallback if `allow-jit` isn't enough. Apple warns it exposes the app to memory-safety bugs. `release.sh` adds it only if the signed self-test shows PyObjC's callbacks fail with `allow-jit` alone, re-checks, and says so in the log (then it belongs in the file). |
| `com.apple.security.cs.disable-library-validation` | Library validation lets the app load only libraries signed by Apple or by the same team. Every library inside ShoutAbout.app is re-signed with your Developer ID, and system frameworks are Apple's, so nothing needs it; Apple says Gatekeeper checks apps without library validation more strictly. The signed self-test imports every dependency, so a library that didn't load would fail the build. |
| Network, file and Apple Events entitlements | The network/file ones belong to the App Sandbox, which a Developer ID app without the sandbox doesn't use. The app doesn't script other apps. Accessibility, Input Monitoring and the microphone prompt are privacy permissions the user grants, not entitlements. |

numpy, onnxruntime and CTranslate2 are ahead-of-time compiled libraries that load like any
other signed dylib; the self-test runs onnxruntime's voice detection and loads CTranslate2
in the signed app, so a Hardened Runtime problem there fails the build instead of reaching
your Mac. Entitlements go on the main executable only (Apple: never on library code).

### What only the first real builds can prove

Nothing here could run without a Mac, Codemagic and your Apple account. Checked on Windows:
the YAML parses and has the intended structure, every shell script passes `bash -n`, the
Mach-O lister was run on a fake bundle (thin and universal headers, Java class files,
object files, symlinks, a nested framework), the self-test check ran against stand-in
PyObjC modules, the whole test suite and ruff. Unproven until the first builds:

- Codemagic's machine: Xcode 26.5 on `mac_mini_m2`, the CLI tools' exact output and
  options as installed there, `$CM_ENV`, the shebang lines in the YAML scripts;
- the iPhone signing: profiles for both bundle IDs with the App Group, `use-profiles`
  resolving the bundle IDs from `Base.xcconfig`, the archive and export, the upload, and
  TestFlight's internal distribution;
- the Mac build with uv's Python on Codemagic, the number of files to sign and the time
  it takes, timestamps for hundreds of files in parallel, the self-test on a headless build
  machine (Tk needs the window server; if the self-test can't run there at all, the
  comparison is skipped with a warning), whether `allow-jit` alone is enough for PyObjC
  (the fallback above covers the other answer), notarization of a PyInstaller app with
  these entitlements, stapling, `hdiutil` on Codemagic's virtual machine, Gatekeeper's
  verdict;
- the build times above (estimates);
- the app itself on a Mac and an iPhone (see [macos.md](../macos.md) and
  [ios-app.md](../ios-app.md)).

## Sources

Codemagic:

- [codemagic.yaml reference](https://docs.codemagic.io/yaml-basic-configuration/yaml-getting-started/)
  (instance types, `xcode`, anchors, triggering, artifacts and which files are listed separately)
- [Alternative code signing methods](https://docs.codemagic.io/yaml-code-signing/alternative-code-signing-methods/)
  (`keychain initialize`, `fetch-signing-files --type IOS_APP_STORE --create`,
  `CERTIFICATE_PRIVATE_KEY` made with `ssh-keygen -t rsa -b 2048 -m PEM`)
- [Signing iOS apps](https://docs.codemagic.io/yaml-code-signing/signing-ios/) and
  [Signing macOS apps](https://docs.codemagic.io/yaml-code-signing/signing-macos/)
- [App Store Connect publishing](https://docs.codemagic.io/yaml-publishing/app-store-connect/)
  (`api_key`/`key_id`/`issuer_id`, `submit_to_testflight`, post-processing)
- [Build versioning](https://docs.codemagic.io/knowledge-codemagic/build-versioning/)
  (latest TestFlight build number)
- [Environment variables](https://docs.codemagic.io/yaml-basic-configuration/configuring-environment-variables/)
  and [using them](https://docs.codemagic.io/yaml-basic-configuration/using-environment-variables/) (`CM_ENV`)
- [Caching](https://docs.codemagic.io/knowledge-codemagic/caching/) (no symbolic links; 3 GB per workflow)
- [macOS build machines](https://docs.codemagic.io/specs/versions-macos/) (we use Xcode 26.5; 26.6 is the default)
- [Pricing](https://docs.codemagic.io/billing/pricing/) (500 free macOS M2 minutes a month for personal accounts)
- [Adding apps from GitHub](https://docs.codemagic.io/getting-started/adding-apps/) and
  [Common iOS issues](https://docs.codemagic.io/troubleshooting/common-ios-issues/)
- CLI tools: [fetch-signing-files](https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/app-store-connect/fetch-signing-files.md),
  [certificates list](https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/app-store-connect/certificates/list.md),
  [get-latest-testflight-build-number](https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/app-store-connect/get-latest-testflight-build-number.md),
  [publish](https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/app-store-connect/publish.md) (`--testflight`),
  [keychain add-certificates](https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/keychain/add-certificates.md),
  [xcode-project use-profiles](https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/xcode-project/use-profiles.md),
  [build-ipa](https://github.com/codemagic-ci-cd/cli-tools/blob/master/docs/xcode-project/build-ipa.md)

Apple:

- [Creating distribution-signed code for macOS](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac)
  (inside-out order, no `--deep`, entitlements only on executables, `--timestamp`, `-o runtime`)
- [Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
  (`notarytool submit --wait`, `notarytool log`, tickets for nested items, stapling, zips can't be stapled)
- [Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues)
- [Hardened Runtime](https://developer.apple.com/documentation/security/hardened-runtime) and the entitlements
  [audio input](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.device.audio-input),
  [allow JIT](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.allow-jit),
  [allow unsigned executable memory](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.allow-unsigned-executable-memory),
  [disable library validation](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.disable-library-validation)
- [Create Developer ID certificates](https://developer.apple.com/help/account/certificates/create-developer-id-certificates/)
  (Account Holder only)
- [Add internal testers](https://developer.apple.com/help/app-store-connect/test-a-beta-version/add-internal-testers)
  and [TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview)

Others:

- PyObjC: [Code signing and notarizing](https://pyobjc.readthedocs.io/en/latest/notes/codesigning.html)
- PyInstaller: [macOS binary code signing](https://pyinstaller.org/en/stable/feature-notes.html#macos-binary-code-signing)
- fastlane: [match](https://docs.fastlane.tools/actions/match/) (Developer ID certificates need the Account Holder, not an API key)
- uv: [Caching in continuous integration](https://docs.astral.sh/uv/concepts/cache/#caching-in-continuous-integration)
