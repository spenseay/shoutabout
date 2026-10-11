# Launch checklist

Goal (the owner, 2026-10-10): "I want a homepage. I want to put this online so other people
can use it. For now let's make it totally free." And, after the first draft of the site:
no running costs, a streamlined brand (the wordmark as the headline, the mouth always with
its red tongue), no version numbers in anything people see, and no public email unless he
decides to have one.

This page is the go-live plan: what's ready, the order to switch things on, what it costs
(nothing new), and the decisions left. Nothing here has been published or deployed. Hosting
steps are in [hosting.md](hosting.md), the App Store and TestFlight drafts in
[store-listing.md](store-listing.md).

- [Where things stand](#where-things-stand)
- [Go-live, in order](#go-live-in-order)
- [The switches](#the-switches)
- [Costs](#costs)
- [Windows: the SmartScreen prompt, and the free fix](#windows-the-smartscreen-prompt-and-the-free-fix)
- [Apple: what the membership covers](#apple-what-the-membership-covers)
- [Contact and support](#contact-and-support)
- [Legal drafts](#legal-drafts)
- [Licence for a free, closed app](#licence-for-a-free-closed-app)
- [Versions and updates](#versions-and-updates)
- [Press kit basics](#press-kit-basics)
- [Open decisions for the owner](#open-decisions-for-the-owner)

## Where things stand

| Part | State | How that's known |
|---|---|---|
| **Website** (`site/`) | Ready to deploy. Home (wordmark headline, three ways to use it, privacy, a short FAQ), download (Windows, Mac, iPhone), setup guide, help/support, privacy (draft), terms (draft), open-source licences, 404. Every download button says *Coming soon* until its switch is flipped; no email address is shown until one is configured. | Checked locally in headless Chrome: every page at 1440 and 390 px, light and dark, no console errors, no overflow, links checked, `html-validate` clean ([hosting.md › What's verified](hosting.md#whats-verified)). Screenshots in `.screens/site2/` (not committed). |
| **Hosting plan** | Cloudflare Pages (Git integration, `main`), downloads on GitHub Releases with stable names. Written, not executed. | [hosting.md](hosting.md) |
| **Release script** | `scripts/release/publish-github-release.ps1`: uploads `ShoutAbout-Setup.exe` / `ShoutAbout.dmg`, carries the other platform's file forward so both `latest` links keep working. | Parsed, dry-run on dummy files. Never published anything. |
| **Windows installer** | Builds with Inno Setup ([installer.md](../installer.md)); unsigned. Needs a fresh build and a test on a real PC before the first release. | [installer.md › What's verified](../installer.md#whats-verified) |
| **Mac app** | Menu-bar app for Apple silicon, macOS 14+. Public builds: Developer ID-signed and notarized DMG from **Codemagic** (another workstream is setting that up). | [macos.md](../macos.md) |
| **iPhone app** | Builds and uploads to TestFlight from **Codemagic** (same workstream). Then TestFlight review and a public link. | [ios-app.md](../ios-app.md) |

## Go-live, in order

Each step can happen on its own day; the site is safe to publish first because every button
waits for its switch.

1. **Read the site**: `python scripts\site\serve.py`, then http://127.0.0.1:18400, or the
   screenshots in `.screens/site2/`.
2. **Legal bits** ([below](#legal-drafts)): the publisher name, the state in the terms, the
   *Draft* banners.
3. **Website live** ([hosting.md › A](hosting.md#a-the-website-on-cloudflare-pages)):
   connect the repo to Cloudflare Pages (production branch `main`, output `site`, no build
   command), attach `shoutabout.fyi` and `www.shoutabout.fyi`. The `dictate` DNS record is
   never touched.
4. **Windows** ([hosting.md › B](hosting.md#b-downloads-on-github-releases)): build and test the
   installer, publish it with
   the release script, check the link, then `windows.live: true`.
5. **Mac**: when Codemagic produces a signed, notarized `ShoutAbout.dmg`, check it on a Mac,
   publish it with the same script (it keeps the Windows file in the new release), then
   `mac.live: true`.
6. **iPhone** ([hosting.md › C](hosting.md#c-the-iphone-link)): first TestFlight build →
   external group → TestFlight review → public link → `iphone.testflight_url` and
   `iphone.live: true`.
7. **Contact, if wanted** ([hosting.md › D](hosting.md#d-a-contact-address-optional)):
   needed before the App Store submission (guideline 1.5), optional before.
8. **Check** ([hosting.md › Checks](hosting.md#checks-after-deploying)): pages, downloads,
   no cookies, the tunnel still works.

## The switches

All in `site/downloads.json`; edit, commit, push to `main` (Pages redeploys in about a
minute). Details: [hosting.md › The go-live switches](hosting.md#the-go-live-switches).

| Flip | When |
|---|---|
| `windows.live` → `true` | `https://github.com/spenseay/shoutabout/releases/latest/download/ShoutAbout-Setup.exe` downloads the tested installer |
| `mac.live` → `true` | `…/releases/latest/download/ShoutAbout.dmg` downloads the notarized DMG |
| `iphone.testflight_url` = the public link, `iphone.live` → `true` | TestFlight review passed and the public link is on |
| `iphone.app_store_url` = the App Store link | the App Store version is out (it replaces TestFlight on the buttons) |
| `contact_email` = an address | you want people to be able to email you (empty = no email anywhere) |

## Costs

Nothing new to pay for. The only paid item is the Apple membership the owner already has.

| Item | Cost | Notes |
|---|---|---|
| Website: Cloudflare Pages, Free plan | $0 | Unlimited static requests and bandwidth, 500 builds a month |
| Domain `shoutabout.fyi` | already owned | its yearly renewal, as today |
| Downloads: GitHub Releases on a public repo | $0 | Files up to 2 GiB each, no bandwidth bill |
| Builds: Codemagic, free plan for individuals | $0 | 500 macOS (M2) build minutes a month, one build at a time: plenty for the iPhone and Mac apps |
| Apple Developer Program | $99/year, **already paid** | TestFlight, the App Store, and Developer ID signing + notarization for the Mac |
| Windows signing | $0 | None for now (SmartScreen asks once); the Microsoft Store route below is free |
| Contact address: Cloudflare Email Routing | $0 | Optional |
| Lawyer review of the privacy policy and terms | optional | |
| Trademark filing | optional, per class | |

Free alternatives to Cloudflare Pages exist (Vercel's Hobby plan, Render's static sites), but
both come with monthly bandwidth allowances and would mean pointing the domain elsewhere;
Pages sits where the domain already is ([hosting.md › A](hosting.md#a-the-website-on-cloudflare-pages)).

## Windows: the SmartScreen prompt, and the free fix

**For now:** the installer is not code-signed, so Microsoft Defender SmartScreen shows
**"Windows protected your PC"**, with **Run anyway** behind **More info**. The site says so
plainly (download page, setup guide, FAQ, release notes). On Windows 11 PCs with **Smart App
Control** on, unsigned apps without reputation are blocked with no Run anyway button
([Microsoft](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation));
the site says that too.

**Next step, free: the Microsoft Store.** Registering as an individual developer in Partner
Center costs nothing. An app submitted as an **MSIX** package is signed by Microsoft, and
installs from the Store show no SmartScreen prompt, Smart App Control included. The work, in
short:

1. Partner Center → *Get started for free* → **Individual developer** → reserve the name
   *ShoutAbout*.
2. Package the PyInstaller build as MSIX (the MSIX Packaging Tool, or `makeappx` with a
   manifest), as a full-trust desktop app (`runFullTrust`, which Store review asks you to
   justify: a dictation app needs a global key and pasting).
3. Things MSIX does differently, to plan for: no custom installer steps (the firewall rule
   becomes a manifest `FirewallRules` entry, starting at sign-in a `StartupTask`); the app's
   writes to `%APPDATA%` are redirected into the package's private folder; uninstalling
   removes that data.
4. Submit with the same privacy policy URL and screenshots; then point the Windows button
   at the Store listing (a small change to the download page and `downloads.json`).

The paid routes (Azure Artifact Signing at $9.99/month, or an OV certificate) are dropped
from the plan: they cost money every year and still start with no SmartScreen reputation.

## Apple: what the membership covers

The owner's paid Apple Developer membership ($99/year, already active) covers everything on
the Apple side:

- **TestFlight**: internal testers right away; external testers and the public link after
  TestFlight review of the first build of each version. Builds expire after 90 days.
- **The App Store**: the iPhone app's listing ([store-listing.md](store-listing.md)).
- **The Mac download**: Developer ID signing and notarization, so the DMG from the website
  opens without Gatekeeper warnings (macOS only asks once whether to open an app from the
  internet).

Builds come from **Codemagic** (free plan, 500 macOS minutes a month), which signs with the
membership's certificates and uploads to App Store Connect; that pipeline is set up in
another workstream. Before App Review: a real-iPhone test, the privacy policy link inside the
app (guideline 5.1.1(i): **Settings › Privacy policy** → `https://shoutabout.fyi/privacy`),
and a short video of the computer flow for the reviewer.

## Contact and support

The owner isn't sure about a public email, so the site works without one:

- `/support` (the **Help** link everywhere) has the FAQ, quick fixes, and, for beta testers,
  how to send feedback from TestFlight.
- With `contact_email` empty, no address appears anywhere; the privacy policy and terms point
  to `/support` instead.
- Setting `contact_email` shows the address in the footer, on `/support` and in the legal
  pages, with no other change.

**Before the App Store submission, add an address anyway.** Apple's App Review Guideline
1.5 asks that the app and its **Support URL** (`https://shoutabout.fyi/support`) include an
easy way to contact the developer, and TestFlight's external testing asks for a feedback
email that testers see. A free **Cloudflare Email Routing** forwarding address (for example
`support@shoutabout.fyi` → your inbox, $0) does both without publishing a personal address:
[hosting.md › D](hosting.md#d-a-contact-address-optional). A lawyer may also want a contact
address in the privacy policy, since beta testers' names and emails reach you through
TestFlight.

Support routine: keep saved replies from the quick fixes (iPhone doesn't find the computer →
Private network on Windows / Local Network on iPhone and Mac; SmartScreen; Accessibility on
the Mac; admin windows on Windows), and never ask for recordings.

## Legal drafts

`site/privacy.html` and `site/terms.html` are plain-English drafts that match what the
software does today (microphone only between the key presses, recordings kept in memory,
history (off by default) and logs on the computer, on-device recognition on iPhone, LAN-only pairing, model
downloads from Hugging Face, Accessibility on the Mac, downloads served by GitHub, the
optional tunnel and Shortcuts signing). They are **not legal advice**. To decide:

- **Name.** Both say ShoutAbout is made by **Spencer Seay** (the copyright holder in
  `LICENSE`). As an individual in Apple's program, the App Store shows your legal name as the
  seller anyway.
- **Governing law.** `terms.html` has `[STATE]` to fill in.
- **Draft banners.** Each page starts with a dashed "Draft" note: remove it once you (or a
  lawyer) are happy.
- **NVIDIA's EULA.** The Windows installer redistributes NVIDIA's cuBLAS/NVRTC under the
  CUDA EULA, which expects end-user terms consistent with it ([licenses.md](../licenses.md),
  flag 3). The draft limits those parts to use within ShoutAbout and bars reverse
  engineering; a lawyer should confirm that's enough.
- **When the software changes,** update the privacy page first (for example an update check,
  see below).

## Licence: MIT, open source

Decided on 2026-10-10: ShoutAbout is free and its code is public under the MIT License
(`LICENSE`), at https://github.com/spenseay/shoutabout.

- The apps' end users also get the simple terms in `site/terms.html` (no warranty, liability
  cap, privacy). They never limit what the MIT License allows for the code.
- The ShoutAbout name and the mouth logo aren't covered by the MIT License: forks may not
  call themselves ShoutAbout or use the logo, or present themselves as the official app.
- The Windows installer ships `LICENSE.txt` (the MIT notice must come with every copy) next
  to `THIRD-PARTY-NOTICES.txt`.
- App Store downloads also fall under Apple's standard EULA; the terms page says so.
- Third-party components keep their own licences ([licenses.md](../licenses.md)); every
  dependency was chosen to be compatible with this.

## Versions and updates

- **People never see a version number on the website or in a file name.** The downloads are
  always `ShoutAbout-Setup.exe` and `ShoutAbout.dmg`, and the links always point at the
  latest release.
- Versions still exist where the tools need them: `pyproject.toml` and
  `src/shoutabout/__init__.py` (Windows and Mac builds), `MARKETING_VERSION` in
  `ios/Config/Base.xcconfig` (iPhone), and the release tag `v<version>` and title on the
  GitHub releases page. App Store Connect needs a version string too. SemVer: x.y.z.
- **How people get updates, now:** the website always serves the latest files; running a
  newer Windows installer upgrades in place and keeps settings
  ([installer.md › Upgrades](../installer.md#upgrades)); the Mac app is replaced by dragging
  the new one into Applications; iPhone updates come from TestFlight / the App Store; the
  Microsoft Store would update the Windows app by itself.
- **Optional later: an update check.** The apps could read
  `https://shoutabout.fyi/downloads.json` at most once a day and say "A new version is ready"
  with a link to the download page. That would need a version field added to the file (only
  add fields, never rename), a setting to turn it off, and the privacy policy updated first:
  it's a request to the internet.

## Press kit basics

| | |
|---|---|
| Name | **ShoutAbout** (one word, capital S and A) |
| Logo | The wordmark: `SH` + the open mouth with its red tongue as the O + `UT`, over `ABOUT`, in Anton. The only mouth used in marketing is this primary mark (black mouth, white teeth, red tongue): never without its tongue, never with a ring or outline around it. On black, it sits on a small yellow tile (like the app icon) or pill. |
| One line | Private voice dictation for your PC, Mac and iPhone. Free. |
| Short (about 50 words) | ShoutAbout is free, private voice dictation. Press a key on your Windows PC or Mac, or the Action Button on your iPhone, and talk: clean text lands wherever you're typing. Speech is turned into text on your own devices, with no cloud service, no account and no tracking. Setup takes about a minute. |
| Facts | Free · Windows 10 (1809+) and 11, 64-bit · Macs with Apple silicon, macOS 14+ · iPhone with iOS 26 (TestFlight beta first) · English · No account · Data Not Collected |
| Logo and icons | `site/assets/img/og.png` (the wordmark, 1200 × 630), `brand/app-icon/primary-1024.png` (and `dark`, `alt`, `bleed`), `brand/mouth-512.png`, `site/icon-512.png` |
| Screenshots (sample data only, primary mark only) | `site/assets/img/shots/*.webp`: the PC setup window, the pill, and the iPhone onboarding screen. The site draws the iPhone in CSS instead of using the app's other screens, whose record button (light mode) and Dynamic Island / Lock Screen show the mouth with a yellow ring. |
| Brand rules | [brand.md](../brand.md): Shout Yellow `#FFE600`, black `#0A0A0A`, Tongue Red `#FF1A1A` only for the tongue and small marks; Anton (uppercase) and Archivo |
| Contact | `/support`, plus `contact_email` if set |

Don't claim what isn't measured: no accuracy percentages, no "fastest", and each platform is
"coming soon" until its switch is on.

## Open decisions for the owner

1. **Go-ahead** for the website first, with every download on *Coming soon*?
2. **Publisher name** on the legal pages: your name, or a company?
3. **Governing-law state** for the terms; lawyer review or not; when to remove the *Draft*
   banners.
4. **Licence:** decided: MIT, open source (see above).
5. **Contact:** no email for now (the site handles that), and a free forwarding address
   (`support@shoutabout.fyi`) before the App Store? Which address for TestFlight's feedback
   email?
6. **EU trader status** in App Store Connect (see [hosting.md › D](hosting.md#d-a-contact-address-optional)).
7. **Microsoft Store:** start the MSIX packaging after the first Windows release?
8. **Update check** in the apps later: yes or no, on or off by default?
9. **Wording:** "free" without "forever" (the site never promises it stays free), "English
   for now", "works offline" (true once the model is downloaded).
