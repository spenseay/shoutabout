# App Store listing (draft)

Drafts for App Store Connect and TestFlight, for the iPhone app `fyi.shoutabout.app`. Nothing
has been entered anywhere yet. The owner has a paid Apple Developer membership; builds come
from Codemagic ([LAUNCH.md](LAUNCH.md#apple-what-the-membership-covers)). Field limits are
Apple's as of October 2026
([app information](https://developer.apple.com/help/app-store-connect/reference/app-information/app-information),
[version information](https://developer.apple.com/help/app-store-connect/reference/app-information/platform-version-information));
lengths below were counted.

Keep it honest: the app has only run in the Simulator so far. Don't submit before the
real-iPhone checks in [ios-app.md › What's untested](../ios-app.md#whats-untested) pass.
And keep the brand rules: no version numbers in the copy, and the mouth always shows its red
tongue (in screenshots too).

## App information

| Field | Draft | Notes |
|---|---|---|
| Name (2–30) | `ShoutAbout` (10) | Must be unique on the App Store. Fallback: `ShoutAbout Dictation` (20). The name under the icon stays "ShoutAbout". |
| Subtitle (≤ 30) | `Private voice dictation` (23) | Alternative: `Voice typing, kept private` (26) |
| Bundle ID | `fyi.shoutabout.app` | From `ios/Config/Base.xcconfig` |
| SKU | `shoutabout-ios` | |
| Primary category | Productivity | |
| Secondary category | Utilities | |
| Content rights | Contains no third-party content | |
| Price | Free (USD 0), all territories | No in-app purchases |
| Copyright | `2026 Spencer Seay` | Or the company name, if you form one |
| Privacy Policy URL | `https://shoutabout.fyi/privacy` | Required. Apple also wants it reachable **inside the app** (guideline 5.1.1(i)): see [LAUNCH.md](LAUNCH.md#apple-what-the-membership-covers) |
| Support URL | `https://shoutabout.fyi/support` | The help page. Guideline **1.5** asks that the app and its Support URL include an easy way to contact you: set `contact_email` in `site/downloads.json` (a free forwarding address, [hosting.md › D](hosting.md#d-a-contact-address-optional)) before submitting, and it appears on that page. |
| Marketing URL | `https://shoutabout.fyi/` | |

## Version information (first release)

App Store Connect asks for a version string (the build's `MARKETING_VERSION`); it isn't part
of the copy below.

**Promotional text** (≤ 170; can change any time without review), 166 characters:

```text
Press the Action Button, talk, press again. Your words land on your clipboard, or where you're typing on your computer. Free. No account. Nothing leaves your devices.
```

**Keywords** (≤ 100 bytes, comma-separated, no spaces after commas; words already in the
name and subtitle don't need repeating), 99 bytes:

```text
dictation,speech to text,voice typing,transcribe,dictate,action button,windows,mac,computer,private
```

**Description** (≤ 4,000), 1,882 characters:

```text
ShoutAbout is private voice dictation. Press the Action Button (or tap the big mouth), say what you want to write, and press again. Your words are ready to paste on your iPhone, or typed straight into the window you're working in on your computer.

YOUR VOICE STAYS HOME
Your iPhone turns speech into text by itself, with Apple's on-device speech recognition. Or your own computer does it. Nothing goes to a cloud service, and nothing comes to us. No account, no ads, no tracking.

ONE PRESS, FROM ANYWHERE
Set the Action Button to ShoutAbout and dictate without opening the app. A little mouth pops up in the Dynamic Island and on the Lock Screen while it listens.

WORKS ON ITS OWN
Choose "This iPhone" and the text goes to your clipboard, ready to paste in any app. No computer needed.

OR TYPES ON YOUR COMPUTER
Get the free ShoutAbout app for Windows or Mac at shoutabout.fyi, and your iPhone becomes a microphone for your computer. Your computer shows up by itself on the same Wi-Fi: tap it, check the 4-digit code, click Allow. Then talk into your iPhone and the words land wherever you're typing: email, documents, chat. Away from home? Your iPhone writes the text itself, puts it on your clipboard, and tells you so.

SMALL TOUCHES
- Switch between your computer and this iPhone with one tap, or from Control Center
- Your last 200 results in History, on this iPhone only
- Your computer's custom words help it spell names right
- Light and dark mode

ShoutAbout never presses Enter and never treats what you say as a command. It only writes.

English for now. Requires iOS 26 or later. The Action Button is on iPhone 15 Pro and most newer models; any iPhone can use the Dictate control in Control Center. Typing on a computer needs ShoutAbout for Windows or Mac (free) on the same Wi-Fi.

Privacy policy: https://shoutabout.fyi/privacy
Help: https://shoutabout.fyi/support
```

**What's New:** not needed for the first version. For later versions, one plain sentence
about what changed, without the version number.

## Screenshots

The screenshot tour (`ios/ShoutAboutUITests/ScreenshotTests.swift`) uses only sample data
("Sample-Desktop", "Sample text: …"). The owner's rule for all marketing, store screenshots
included: the only mouth shown is the primary mark (black mouth, white teeth band, red
tongue), never without its tongue and **never with a yellow ring or outline** around it. In
today's app that rules out most screens:

| Tour shots | Mouth | Use? |
|---|---|---|
| `light-01-onboarding-choice` | the wordmark's mouth on yellow | yes |
| `dark-07-recording`, `dark-09-result-card` | black mouth on the yellow tile (the red ring on the recording screen surrounds the tile, not the mouth) | yes |
| `light-05/06/07/08/09/10/11-*` | black tile, mouth with a yellow ring | no |
| `island-*`, `lock-*`, `light-04-action-button-card` | mouth with a yellow ring in the Dynamic Island / Lock Screen | no |
| `dark-01-onboarding-choice` | the wordmark's mouth with a yellow ring | no |
| idle main screens, pairing screens | mouth ajar, no tongue | no |

Suggested set for now, with captions:

| # | Shot (from the tour) | Caption on the image |
|---|---|---|
| 1 | `light-01-onboarding-choice` | PRIVATE VOICE DICTATION. |
| 2 | `dark-07-recording` | PRESS. TALK. PRESS. |
| 3 | `dark-09-result-card` | PASTED ON YOUR COMPUTER. |

For a fuller set (the Action Button, the Dynamic Island, light mode), the app itself would
need the primary mark in those places (the mouth on a yellow tile instead of a ringed mouth
on black): a change for the iOS workstream, since App Store screenshots must show the real
app.

Size: the tour currently runs on iPhone 17 Pro (1206 × 2622). App Store Connect asks for the
largest iPhone display size; re-run the tour on the **iPhone 17 Pro Max** simulator (6.9")
for the required set (check the accepted pixel sizes in App Store Connect when uploading:
not verified here).

## App Privacy ("nutrition label")

Answer **"No, we do not collect data from this app"** → the label shows **Data Not
Collected**.

Why that's accurate under Apple's definition ("collect" = transmitting data off the device
in a way that lets you or your partners access it beyond servicing the request in real
time, [app privacy details](https://developer.apple.com/app-store/app-privacy-details/)):

- speech is turned into text on the iPhone, or on the **user's own computer** over the local
  network; the developer has no server and no access to either;
- no analytics, advertising, crash-reporting or other third-party SDKs (the app uses only
  Apple frameworks);
- nothing is sent to us; the only network traffic is to the user's computer (and, if the
  owner of the device sets it up, the user's own tunnel);
- `PrivacyInfo.xcprivacy` (app and widgets) already declares no tracking and no collected
  data.

Re-check this answer if anything is ever added that sends data off the device to us (for
example an update check or crash reporting).

## Age rating

Answer **None / No** to every question: no violence, sexual content, profanity, drugs,
horror, gambling or contests, no medical information, **no unrestricted web access**, **no
user-generated content shared with others**, no messaging or chat, no advertising, no
social-media features, no parental controls or age assurance needed. Expected rating:
**4+**. Apple's questionnaire changed in 2025 (new 4+/9+/13+/16+/18+ tiers) and gained
social-media questions in 2026; read each question as it appears.

## Export compliance

Pre-answered in the build: `ITSAppUsesNonExemptEncryption = NO` (the app uses only Apple's
HTTPS, and plain HTTP on the local network).

## EU trader status

App Store Connect asks once whether you're a trader under the EU Digital Services Act. A
trader's address, phone number and email are shown on the app's EU App Store page; a
non-trader's aren't. The owner decides ([hosting.md › D](hosting.md#d-a-contact-address-optional)).

## App Review information

- **Sign-in required:** No (no accounts).
- **Contact:** the owner's name, phone and email. Only Apple's reviewers see these; they
  aren't published.
- **Notes** (≤ 4,000):

```text
ShoutAbout is a voice dictation app. There is no account or sign-in.

How to try it without a computer: on first launch choose "This iPhone", allow the microphone and speech recognition, tap the big mouth, say a sentence, and tap again. The text appears and is copied to the clipboard. Transcription runs on the device (SpeechTranscriber / DictationTranscriber, or SFSpeechRecognizer with requiresOnDeviceRecognition = true).

Companion computer app (optional): ShoutAbout for Windows and for Mac are free desktop apps from https://shoutabout.fyi. When the user chooses "My computer", the iPhone finds the user's own computer on the local network (Bonjour service _shoutabout._tcp) and, after the user clicks Allow on the computer with a matching 4-digit code, sends recordings directly to that computer over the local network. This is why the app asks for Local Network access. We run no servers and receive no data. A short video of the computer flow: [VIDEO URL TO ADD].

Action Button: the "Dictate" App Intent (AudioRecordingIntent) records in the background and shows a Live Activity in the Dynamic Island and on the Lock Screen for the whole recording, as Apple requires for background recording. The "audio" background mode is used only during a dictation.

Camera: used only for the optional "Scan a code instead" pairing QR code.
```

Record the video before submitting (a phone filming the computer screen is fine). The
reviewer may not install the desktop app, and a missing explanation of the Local Network
prompt is a common reason for questions. (The Mac app is a free download, so a reviewer with
a Mac could try the linked mode themselves.)

## TestFlight

| Field | Draft |
|---|---|
| Beta App Description | `ShoutAbout: private voice dictation. Press the Action Button, talk, press again. The text lands on your clipboard, or on your computer with ShoutAbout for Windows or Mac.` (170) |
| Feedback email | Required for external testing, and **testers see it**: use the forwarding address ([hosting.md › D](hosting.md#d-a-contact-address-optional)) if you don't want to show your own. |
| What to Test | `Everything: setup, the Action Button and the Dynamic Island mouth, This iPhone mode, linking to a Windows PC or a Mac (same Wi-Fi), and what happens away from home. Tell us what confused you.` |
| Marketing URL / Privacy Policy URL | as above |

Internal testing (up to 100 people in your team) needs no review. External testing (up to
10,000 people, by email or **public link**) needs **TestFlight App Review** for the first
build of each version. Builds expire after 90 days. The public link goes into
`site/downloads.json` → `iphone.testflight_url` ([hosting.md › C](hosting.md#c-the-iphone-link)).

## Mac

The Mac app is distributed from the website, not the Mac App Store: a `ShoutAbout.dmg`
signed with a Developer ID and notarized by Apple (built by Codemagic), on the GitHub
downloads repo ([hosting.md › B](hosting.md#b-downloads-on-github-releases)). The same
membership covers that. A Mac App Store listing would need the app sandboxed, which the
global key and pasting into other apps make hard; not planned.
