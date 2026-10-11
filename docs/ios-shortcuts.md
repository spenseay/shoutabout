# iPhone Shortcuts

Four shortcuts run ShoutAbout on the iPhone:

- **Dictate** runs from the Action Button. It records, sends the audio to the hub and copies the
  text to the phone clipboard. In PC mode the hub also pastes it on the PC. **Success is
  silent:** a notification appears only when something needs a look.
- **Dictation Target** runs from Control Center. It flips between **PC** mode (the hub pastes into
  the focused PC window) and **Phone** mode (text only comes back to the phone).
- **Teach ShoutAbout** adds a word or phrase to the hub's vocabulary, so later dictations spell
  it right ([§14](#14-teach-shoutabout)).
- **Dictate (offline)** records and transcribes on the iPhone itself with Apple's speech
  recognition, without the PC ([§15](#15-dictate-offline)).

The easy way is to **import ready-made shortcuts**. One command on the PC writes them as signed
files; you open them on the phone and answer three questions. You don't need a Mac or a
third-party app on the phone, but signing needs a working service: see
[§16](#16-signing-status-and-options). Building Dictate by hand is in
[Appendix A](#appendix-a-build-dictate-by-hand).

**What has been checked on a real iPhone.** These were tried on the owner's iPhone and are marked
**(verified on a real iPhone)**:
- importing the signed files made by `make-shortcuts`
- the home Wi-Fi check, which read the network name with only the default first-run prompts
- the LAN request, with the recording uploaded as the request body
- Copy to Clipboard, and the paste on the PC

Anything else that Apple doesn't document is marked **(verify on your phone)**. Menu labels that
might be worded a little differently are marked **(label may differ slightly)**.

## Contents

1. [What you're building](#1-what-youre-building)
2. [Before you start](#2-before-you-start)
3. [Make the shortcut files on the PC](#3-make-the-shortcut-files-on-the-pc)
4. [Add them on the iPhone](#4-add-them-on-the-iphone)
5. [Answer the import questions](#5-answer-the-import-questions)
6. [Assign the Action Button](#6-assign-the-action-button)
7. [Add the Control Center control](#7-add-the-control-center-control)
8. [First runs and permissions](#8-first-runs-and-permissions)
9. [Test it](#9-test-it)
10. [What you'll see](#10-what-youll-see)
11. [Verification status](#11-verification-status)
12. [Security notes](#12-security-notes)
13. [Phone-side troubleshooting](#13-phone-side-troubleshooting)
14. [Teach ShoutAbout](#14-teach-shoutabout)
15. [Dictate (offline)](#15-dictate-offline)
16. [Signing: status and options](#16-signing-status-and-options)

Appendices:
- [A: Build "Dictate" by hand](#appendix-a-build-dictate-by-hand)
- [B: Build "Dictation Target" by hand](#appendix-b-build-dictation-target-by-hand)
- [C: Optional "Resend Dictation"](#appendix-c-optional-resend-dictation)
- [D: Routing alternatives](#appendix-d-routing-alternatives)

---

## 1. What you're building

```
Action Button ─► Dictate
   read the target from iCloud Drive/Shortcuts/dictation-target.txt   (no file yet = pc)
   record (tap to stop)
   on home Wi-Fi?  yes ─► POST http://<PC-LAN-IP>:<port>/transcribe?target=<pc|phone>
                          Authorization: Bearer <hub token>
                   no  ─► POST https://dictate.shoutabout.fyi/transcribe?target=<pc|phone>
                          CF-Access-Client-Id, CF-Access-Client-Secret, Authorization
                   body: the recording itself (Request Body: File)
   copy the reply's text to the clipboard
   reply's status is "ok"?  yes ─► done, no notification
                            no  ─► "Dictation problem": Check the PC. <warning> <detail>

Control Center ─► Dictation Target
   flips dictation-target.txt between "pc" and "phone" and shows a notification
```

The hub answers with JSON. These are the fields Dictate uses:

| Field | Meaning |
|---|---|
| `text` | The transcript. `""` when nothing was heard. |
| `status` | `ok`, or `warning` when `warning` is set. Error replies have no `status`. |
| `warning` | For example `Hub is paused — not pasted on the PC.` or `No speech detected.`; otherwise `null`. |
| `detail` | Only on errors (401, 413, 503, …): what went wrong. |

The full contract is in the [README](../README.md#api-summary).

## 2. Before you start

- **Phone:** an iPhone 15 Pro or later (it needs the Action Button), on iOS 26 or 27. iCloud
  Drive must be on, because both shortcuts share a file in **iCloud Drive > Shortcuts**.
- **PC:** the hub is running, the tray icon is green, and you've finished SETUP steps 1–7.
- **Your home Wi-Fi name**, exactly as the iPhone shows it in **Settings > Wi-Fi**. Case and
  spaces matter.
- **For use away from home:** the Cloudflare service token's Client ID and Client Secret
  ([cloudflare.md §3](cloudflare.md#3-create-the-service-token)). Without a public URL, Dictate
  works on home Wi-Fi only.

## 3. Make the shortcut files on the PC

In the repo folder:

```powershell
.\.venv\Scripts\python.exe -m shoutabout make-shortcuts --ssid "<home-wifi-name>"
```

It prints the values it used and writes `Dictate.shortcut`, `Dictation Target.shortcut`,
`Teach ShoutAbout.shortcut` and `Dictate (offline).shortcut` to `shortcuts-out\` in the current
folder.

| Value | Default | To change it |
|---|---|---|
| Which shortcuts | All four | `--which dictate,target,teach,offline` (any of them, comma-separated) |
| Home Wi-Fi name | The PC's current Wi-Fi (read with `netsh wlan show interfaces`). The command prints the name it used. Only Dictate and Teach ShoutAbout need it. | `--ssid "<home-wifi-name>"` |
| LAN URL | `lan_url` from `python -m shoutabout info`, plus `/transcribe`: `http://<PC-LAN-IP>:<port>/transcribe`. Teach ShoutAbout uses `/vocab` on the same host. | `--lan-url http://<PC-LAN-IP>:<port>/transcribe` |
| Public URL | `https://<public_hostname>/transcribe`, from `install-state.json` (setup's `-PublicHostname`), e.g. `https://dictate.shoutabout.fyi/transcribe` | `--public-url https://<host>/transcribe`, or `--no-public` |
| Output folder | `shortcuts-out\` | `--out <folder>` |
| Output format | Signed `.shortcut` files | `--no-sign` for unsigned `.plist` files, or `--format cherri` for Cherri source ([§16](#16-signing-status-and-options)) |

`--which offline,target` needs no Wi-Fi name or URLs: those two never talk to the hub.

- **Check the Wi-Fi name.** If the PC and the phone use different networks at home (for example
  separate 2.4 GHz and 5 GHz names), pass the phone's network with `--ssid`.
- **LAN only.** With `--no-public`, or when setup recorded no public hostname, Dictate never
  sends anything away from home Wi-Fi. It shows "Not on home Wi-Fi and no public URL
  configured" and stops, and it only asks for the hub token **(verify on your phone)**.
- **No secrets in the files.** The hub token and the Cloudflare values aren't in them. Dictate
  asks for them when you add it on the phone (§5). The files hold only the actions, the two
  URLs and the Wi-Fi name.
- **Signing.** iOS only imports shortcut files signed by Apple, and signing normally needs a
  Mac. So `make-shortcuts` sends the shortcut structure (the actions, URLs and Wi-Fi name; no
  tokens, no audio, no text) to RoutineHub's **HubSign** service, which returns the signed file.
  This happens only here, at setup. HubSign plays no part when you dictate. HubSign may refuse
  anything but the Cherri compiler; see [§16](#16-signing-status-and-options) for the other
  routes.
  - **If you have a Mac**, `--no-sign` writes unsigned `.plist` files instead and contacts
    nothing. Sign each on the Mac with
    `shortcuts sign --mode anyone --input Dictate.plist --output Dictate.shortcut`
    **(not tested; no Mac was used)**.
  - **Without a Mac**, `--format cherri` writes Cherri source that the official Cherri
    playground or compiler can compile and sign ([§16](#16-signing-status-and-options)).
  - **If signing fails**, the command exits with an error and writes nothing. Try again a minute
    later, or use one of the routes in §16.
- **When to rebuild:** the PC's LAN address or port changed, the public hostname changed, or you
  use another Wi-Fi network at home. Then add the new files on the phone again (§4). Rotating
  the hub token doesn't need a rebuild (§5).

## 4. Add them on the iPhone

1. **Get the files onto the phone.** Any way works, for example:
   - send them to yourself in a chat or email that you open on the phone, or
   - copy them to iCloud Drive from the PC, or onto a USB-C drive that you plug into the phone,
     and find them in the **Files** app.

   The files hold no secrets, only the URLs and the Wi-Fi name.
2. **Open `Dictate.shortcut`** on the phone. If you see a preview rather than Shortcuts, tap
   **Share**, then **Open in Shortcuts** **(label may differ slightly)**.
3. Shortcuts shows what the shortcut does. Tap **Add Shortcut**. Dictate asks the three questions
   from §5 while it's being added **(verified on a real iPhone)**.
4. Do the same with `Dictation Target.shortcut` and `Dictate (offline).shortcut`, which ask
   nothing, and `Teach ShoutAbout.shortcut`, which asks the same questions as Dictate.
5. If shortcuts with these names already exist (an older version, or ones you built by hand),
   iOS may offer **Replace**, or add the new one as "Dictate 2". Keep only the new one, then check
   that the Action Button and the Control Center control use it (§6, §7) **(verify on your
   phone)**.

## 5. Answer the import questions

Dictate asks for three values. A LAN-only Dictate asks only for the first. **Teach ShoutAbout
asks the same questions** and keeps its own copy of the answers, so answer them for both.

| # | Question starts with | Value |
|---|---|---|
| 1 | Paste the hub token | The hub token. |
| 2 | Paste the Cloudflare service token Client ID | The Client ID, ending in `.access`. |
| 3 | Paste the Cloudflare service token Client Secret | The Client Secret. |

Move each value from the PC to the phone as a QR code, without any cloud service.

**Hub token.** In the repo folder on the PC:

```powershell
.\.venv\Scripts\python.exe -m shoutabout token --qr
```

**Cloudflare Client ID and Secret.** Copy each value from where you saved it, such as your
password manager or the dashboard right after creating the token, then run:

```powershell
.\.venv\Scripts\python.exe -m shoutabout qr --clipboard
```

Do this once for the Client ID and once for the Client Secret.

**On the iPhone:**

1. Open **Camera** and point it at the QR code, or use the **Code Scanner** control in Control
   Center.
2. Tap the text banner that appears, then **Copy**. Whether Camera offers **Copy** for plain-text
   QR codes is unconfirmed; Code Scanner is the likelier one to show the text with a Copy option
   **(verify on your phone)**.
3. Switch back to Shortcuts and paste into the question's field.
4. Check that there's **no trailing space or line break**. A stray line break in a header value
   can make requests fail with "The network connection was lost".

If switching apps loses the import screen, leave the answers empty and add the shortcut anyway.
Then open Dictate in the Shortcuts editor and paste each value into its **Text** action (see
"Where the answers live" below).

**If the terminal QR code won't scan:** show it as a large image instead. The image is deleted
when you press Enter:

```powershell
.\.venv\Scripts\python.exe -m shoutabout token --copy
.\.venv\Scripts\python.exe -m shoutabout qr --clipboard --open
```

**Other fallbacks:**

- If your password manager has an iPhone app, copy the Cloudflare values from there.
- Type the value by hand.
- Use a temporary note stored **On My iPhone** (not in iCloud), and delete it afterwards.

**Clipboard history on the PC:** `token --copy` keeps the token out of Windows clipboard
history. Anything you copy from a browser or password manager is **not** kept out. If Clipboard
history is on (**Win+V**), delete those entries there afterwards.

**Where the answers live.** iOS stores them in the first **Text** actions of Dictate (and of
Teach ShoutAbout), labelled **Hub token**, **CF Client ID** and **CF Client Secret**. To change
one later, for example after `init --rotate-token`, open each shortcut in the Shortcuts editor
and paste the new value into that Text action. Because these Text actions now hold secrets,
**never share Dictate or Teach ShoutAbout** once they're set up.

## 6. Assign the Action Button

Go to **Settings > Action Button**. Swipe to **Shortcut**, tap **Choose a Shortcut**, and pick
**Dictate**.

## 7. Add the Control Center control

1. Swipe down from the top-right corner to open Control Center.
2. Tap **+** at the top left, then **Add a Control**.
3. Search for **Shortcut** and tap the **Shortcut** control.
4. Tap **Choose** and pick **Dictation Target**.
5. Drag the control where you want it, then tap an empty area to finish.

You can add a second control for **Dictate** the same way. Running a shortcut from a control is
expected to show only a small run banner, not open the whole Shortcuts app **(verify on your
phone)**.

## 8. First runs and permissions

There's no way to pre-approve these. Each prompt appears the first time an action needs it.
Answer **Always Allow** where offered. Prompts offer **Allow Once / Always Allow / Don't Allow**.

1. **Notifications:** check that **Settings > Notifications > Shortcuts > Allow Notifications**
   is on, or problem notifications won't appear.
2. **Microphone:** the first **Record Audio** asks for microphone access. Allow it. To check
   later: **Settings > Privacy & Security > Microphone > Shortcuts** **(verify on your phone)**.
3. **Files:** the first file read or write asks whether the shortcut may access
   `dictation-target.txt`. Answer **Always Allow**. Do this once in **each** shortcut. If you're
   asked every time, check **Settings > Privacy & Security > Files & Folders > Shortcuts**
   **(verify on your phone)**.
4. **Network destinations:** each new address prompts once per shortcut, along the lines of
   "Allow 'Dictate' to connect to <PC-LAN-IP>?" **(exact wording: verify on your phone)**.
   Answer **Always Allow**. Run **Dictate** once **on home Wi-Fi** (LAN route) and once **on
   cellular with Wi-Fi off** (tunnel route) so both destinations get approved.
5. **Local Network:** if the LAN route fails while the PC tests in SETUP step 7 pass, open
   **Settings > Privacy & Security > Local Network**. Make sure **Shortcuts** is on, if it's
   listed. It's unconfirmed whether Shortcuts appears there **(verify on your phone)**.
6. **Location (for the Wi-Fi name):** iOS can hide Wi-Fi names from apps without Location
   access. On the owner's iPhone the name was read with only the default first-run prompts
   **(verified on a real iPhone)**. If **Network Name** comes back empty on yours (symptom: at
   home, the hub history shows `"route":"tunnel"`, or a LAN-only Dictate says "Not on home
   Wi-Fi"), go to **Settings > Privacy & Security > Location Services > Shortcuts** and choose
   **While Using the App**, with **Precise Location** on **(verify on your phone)**.
7. **Large uploads:** if a long recording triggers a prompt about sharing large amounts of data,
   allow it, or turn on **Settings > Apps > Shortcuts > Advanced > Allow Sharing Large Amounts of
   Data** **(verify on your phone)**.

To review or reset a shortcut's grants, open it in the editor, then go to **Details** (the ⓘ or
the name menu) > **Privacy**. **Reset Privacy** clears them.

## 9. Test it

The full end-to-end list is in [SETUP.md §9](../SETUP.md#9-end-to-end-tests).

| # | Do | Expected |
|---|---|---|
| 1 | **PC mode, home Wi-Fi.** Open Notepad on the PC and click into it. Press the Action Button, say a sentence, and tap stop. | The text appears in Notepad about a second later and is also on the phone clipboard. No notification. **(verified on a real iPhone)** |
| 2 | **Phone mode.** Run **Dictation Target** from Control Center until it says **Dictation → phone**. Dictate, then paste in Notes. | Nothing is pasted on the PC, and the note gets the text. Switch back to PC afterwards. **(verify on your phone)** |
| 3 | **Away from home.** Turn Wi-Fi off in Settings and dictate. Approve the connection to the public hostname with **Always Allow**. | Same as test 1. The hub history shows `route tunnel`. **(verify on your phone)** |
| 4 | **A problem.** On the PC, tray > **Pause**, then dictate in PC mode. | A **Dictation problem** notification: `Check the PC. Hub is paused — not pasted on the PC.` The text is on the phone clipboard. Tray > **Resume** afterwards. **(verify on your phone)** |

## 10. What you'll see

- **Success** (`status` is `ok`): nothing pops up. The text is on the phone clipboard. In PC mode
  it's also pasted into the focused PC window.
- **Success with a warning** (`status` is `warning`): a **Dictation problem** notification that
  reads `Check the PC.` followed by the warning, for example:
  - `Hub is paused — not pasted on the PC.`
  - `Couldn't paste into an admin window — text is on your clipboard.` (the PC clipboard)
  - `The PC is locked — text is on your clipboard.` (the PC clipboard)
  - `No speech detected.` The transcript is empty, so Copy to Clipboard copies empty text
    **(verify on your phone whether that clears the phone clipboard)**.

  When there is text, it's also on the phone clipboard.
- **Hub error** (wrong token 401, too long 413, still loading 503, …): `Check the PC.` followed by
  the hub's `detail`, e.g. `Check the PC.  missing or invalid bearer token`. Get Contents of URL
  is expected to return the body of a 4xx/5xx reply without stopping **(verify on your phone)**.
- **`Check the PC.` with nothing after it:** the reply wasn't the hub's JSON. Usually Cloudflare
  Access refused the request, or a Cloudflare error, challenge or captive-portal page came back.
  Shortcuts may show an error alert instead **(verify on your phone)**. See §13.
- **PC unreachable, no network, or a timeout:** Shortcuts shows its own error alert and **stops**.
  Shortcuts has no try/catch, so the shortcut can't fall back from the LAN URL to the public URL.
  There's no timeout setting; the default is probably about 60 s **(verify on your phone)**.

## 11. Verification status

**Verified on a real iPhone** (the owner's):
- [x] Importing the signed files made by `make-shortcuts`, including the import questions.
- [x] The home Wi-Fi check, with only the default first-run prompts.
- [x] The LAN request, with the recording as the request body (**Request Body: File**) and
      `?target=` in the URL.
- [x] Copy to Clipboard, and the paste on the PC (PC mode).

**Learned on the real iPhone.** These are built into the generated shortcuts and Appendix A:
- A **Form** request body with a **File** field was silently dropped. The hub received
  `application/x-www-form-urlencoded` without the file and answered `missing 'file' upload`.
  Sending the recording as the whole body (**Request Body: File**) works.
- **"has any value"** is true even for empty or `null` values, so it can't tell success from a
  problem.
- Comparing a raw **Dictionary Value** with **is** didn't match. Converting it with **Get Text
  from Input** first does.
- A **Show Notification** whose body can end up empty fails with "Please choose a value for each
  parameter in this action". The body must start with literal text.

**Not verified yet.** Check these once and note the results:
- [ ] The tunnel route from cellular, including the public hostname's network prompt.
- [ ] **Dictation Target** switching the mode, and Phone mode end to end.
- [ ] A warning or error notification (tray > Pause is an easy way to get one).
- [ ] What Copy to Clipboard does on an error reply or with an empty transcript.
- [ ] A LAN-only Dictate (`--no-public`) away from home Wi-Fi.
- [ ] What the recording UI looks like from the Action Button (a compact banner or the Dynamic
      Island is expected), and whether pressing the Action Button again stops the recording.
- [ ] Whether it works while **locked**: does Record Audio start, does it ask for Face ID, and can
      it read `dictation-target.txt` while locked?
- [ ] Whether **Normal** quality gives a small `.m4a`: on the order of 100–200 KB for 10 s
      ([Appendix A.3](#a3-read-the-target-and-record)).
- [ ] The error wording, and roughly how long it takes, when the PC is off.
- [ ] Whether Camera or Code Scanner offers **Copy** for a plain-text QR code.
- [ ] **Teach ShoutAbout**: the JSON request on both routes, the `added` / `exists` /
      invalid-term notifications, and the keyboard mic in the Ask for Input field.
- [ ] **Dictate (offline)**: that the **Transcribe Audio** action imports and runs from the
      generated file (its identifier comes from Cherri's action list), whether it works in
      Airplane Mode, the "Nothing heard" case, and the language it uses.
- [ ] A Cherri-built shortcut on the phone (Teach ShoutAbout and Dictate (offline) were signed
      with the Cherri 2.3.0 playground): the single pairing-text question, **Get Dictionary
      from Input** on the pasted JSON, and the header dictionaries, which Cherri writes without
      the `WFItemType` key that the plist builder sets.

## 12. Security notes

- **The Text actions of Dictate and Teach ShoutAbout hold secrets** once you've answered the
  import questions (in a Cherri build, the whole pairing text). Never share or export them.
  Dictation Target and Dictate (offline) hold none.
- The files from `make-shortcuts` hold **no** secrets: only the actions, the URLs and the
  Wi-Fi name. HubSign (or the Cherri playground, with `--format cherri`) sees exactly that,
  once, at setup.
- Shortcuts sync through iCloud. Turn on **Advanced Data Protection** (Settings > your name >
  iCloud) if you want them, and the files in iCloud Drive, end-to-end encrypted.
- **The LAN route is plain HTTP.** The hub token and audio cross your home Wi-Fi unencrypted.
  Wi-Fi encryption keeps outsiders out, but another device on your network could read them. This
  is accepted for v1; LAN HTTPS is a future item. The public route is HTTPS end to end to
  Cloudflare.
- **The route follows the Wi-Fi name.** Another network with the same name, such as a router's
  default name, would get the LAN request, with your hub token, over plain HTTP. Give your home
  network a distinctive name.
- **If the phone is lost:**
  1. Rotate the hub token on the PC with
     `.\.venv\Scripts\python.exe -m shoutabout init --rotate-token`, then restart the hub.
  2. Delete or rotate the Cloudflare service token. See
     [cloudflare.md §11](cloudflare.md#11-rotating-secrets).

## 13. Phone-side troubleshooting

More cases, including the hub's error codes, are in [troubleshooting.md](troubleshooting.md).

| Symptom | Likely cause / fix |
|---|---|
| Error alert "could not connect to the server" at home | The PC is asleep or off, the hub isn't running, the Windows network is **Public**, or a firewall block rule exists. On the phone, open `http://<PC-LAN-IP>:<port>/health` in Safari (`lan_url` from `python -m shoutabout info`): you should get `{"ok":true}`. If the PC's address or port changed, rebuild with `make-shortcuts` (§3). Then work through [SETUP.md §11](../SETUP.md#11-troubleshooting). |
| Always uses the tunnel at home, or a LAN-only Dictate says "Not on home Wi-Fi" at home | **Network Name** is empty (Location permission, §8 step 6), or the Wi-Fi name given to `make-shortcuts` doesn't match exactly. Rebuild with `--ssid "<home-wifi-name>"`. |
| `Check the PC.  missing or invalid bearer token` | The hub token in Dictate's **Hub token** Text action has extra whitespace, or the token was rotated. Paste it again (§5). |
| `Check the PC.` with nothing after it, or an HTML page | Cloudflare Access rejected the request (check the **CF Client ID** and **CF Client Secret** Text actions), or a Cloudflare error or challenge page came back. See [cloudflare.md §8](cloudflare.md#8-verify-with-curlexe). |
| "Please choose a value for each parameter in this action" | An older or hand-built Dictate whose notification body can be empty. Import a fresh one from `make-shortcuts`, or start the body with literal text ([Appendix A.7](#a7-handle-the-answer)). |
| `Check the PC.  missing 'file' upload` | An older or hand-built Dictate that sends a **Form** body; iOS dropped the file field. Import a fresh one, or set **Request Body** to **File** ([Appendix A.6](#a6-send-it-two-request-actions)). |
| Every dictation shows **Dictation problem**, even though the text arrived | The hub is older than the `status` field (update it), or a hand-built If compares the raw Dictionary Value. Use **Get Text from Input** first ([Appendix A.7](#a7-handle-the-answer)). |
| "The network connection was lost" | An empty or malformed header row, or a line break in a header value (often a pasted secret). |
| `too many failed authentication attempts` | 10 bad tokens in 10 minutes. Wait 10 minutes or restart the hub. |
| `hub is starting` (503) | The hub loads Whisper after you log on (a few seconds). Try again. |
| `upload too large` (413) | The recording is over 25 MB or 10 minutes, or Audio Quality is Very High. |
| No notification and nothing pasted on the PC | The target is Phone (run **Dictation Target**): the text is on the phone clipboard. |
| No notification even when something is wrong | **Settings > Notifications > Shortcuts** is off. |
| Teach ShoutAbout says `Not added: Not Found` | The hub is older than `POST /vocab`. Update and restart it. |

## 14. Teach ShoutAbout

Adds a word or phrase to the hub's vocabulary (`vocab.txt`): names, product names, jargon. The
hub passes the vocabulary to Whisper and the cleanup step, so later dictations spell it the way
you taught it. It never pastes anything.

```
Teach ShoutAbout
   Ask for Input "Word or phrase to teach ShoutAbout"   (type it, or use the keyboard's mic)
   on home Wi-Fi?  yes ─► POST http://<PC-LAN-IP>:<port>/vocab
                          Authorization: Bearer <hub token>
                   no  ─► POST https://<public host>/vocab
                          CF-Access-Client-Id, CF-Access-Client-Secret, Authorization
                   body: JSON {"term": "<what you entered>"}
   reply's status is "added"?   ─► "Taught: <term>"
   reply's status is "exists"?  ─► "Already in your vocab: <term>"
   anything else                ─► "Not added: <detail>"   (invalid term, wrong token, ...)
```

- **Import:** the same way as Dictate (§3–§5). A plist build asks the **same three questions**
  (one for a LAN-only build); a Cherri build ([§16](#16-signing-status-and-options)) asks once
  for the pairing text instead. It routes by the same home Wi-Fi name. Its answers are stored
  in its own Text actions: after `init --rotate-token`, update both shortcuts.
- **Always one notification.** Unlike Dictate, it confirms success too, because you asked for a
  change. The texts come from the hub's reply (`status`, `term`, `detail`), compared as Text with
  **is**, as in Dictate.
- **Dictating the word:** the question is an ordinary text field, so the keyboard's mic button
  works there (**Settings > General > Keyboard > Enable Dictation**). That's Apple's keyboard
  dictation, not ShoutAbout's, so check the spelling before you tap **Done**: what you see is
  exactly what gets taught.
- **Run it from** the Shortcuts app, a Home Screen icon, Siri ("Teach ShoutAbout"), or a second
  Control Center control (§7).
- **First run:** the same network prompts as Dictate (§8 step 4), once per destination.
- The hub's side: `POST /vocab` (see the [README](../README.md#api-summary)). It answers with
  `status` `added`, `exists` or `invalid`, plus `term`, `reason` and `message`; errors (401, 503,
  …) have only `detail`.

Not yet run on a real iPhone **(verify on your phone)**.

## 15. Dictate (offline)

Records and transcribes **on the iPhone** with Apple's **Transcribe Audio** action (iOS 17 and
later), then copies the text to the clipboard. It doesn't contact the PC or anything else, has
no import questions and holds no secrets. Use it when the PC is off or unreachable.

```
Dictate (offline)
   Record Audio (Normal, starts immediately, tap to stop)
   Transcribe Audio   (Apple's speech recognition)
   0 characters?  yes ─► "Nothing heard"   (the clipboard is left alone)
                  no  ─► Copy to Clipboard, silently
```

How it differs from Dictate:

| | Dictate | Dictate (offline) |
|---|---|---|
| Recognition | Whisper on the PC's GPU | Apple's speech recognition on the iPhone |
| Accuracy | Higher, with your vocabulary and the LLM cleanup | Lower: no vocabulary, no cleanup |
| Needs | The PC on, the hub running, a network | Nothing but the iPhone |
| Pastes on the PC | Yes, in PC mode | Never: phone clipboard only |
| Dictation Target | Used | Ignored |
| Secrets | Hub token (and Cloudflare values) | None |

- **Import:** open `Dictate (offline).shortcut` and add it (§4). There are no questions.
- **Run it from** a Control Center control, a Home Screen icon, or Back Tap
  (**Settings > Accessibility > Touch > Back Tap**). Keep the Action Button on Dictate.
- **Unverified (verify on your phone):**
  - The action identifier (`com.apple.ShortcutsActions.TranscribeAudioAction`, one parameter
    `audio`) comes from the Cherri compiler's action list. If the imported shortcut shows an
    unknown action, add **Transcribe Audio** by hand between **Record Audio** and **Count**.
  - Whether it works in Airplane Mode, and which language it uses (expected: the iPhone's
    language).
  - Others have reported **Transcribe Audio** stopping long recordings ("didn't finish in time",
    around 90 minutes) and, on iOS 26, failing with "No common audio files among modules".

## 16. Signing: status and options

iOS only imports shortcut files that Apple's signing has processed, and only Apple's
`shortcuts sign` command on a Mac does that. Every route below ends at a Mac somewhere.

**Status.** By default `make-shortcuts` sends the unsigned shortcut to RoutineHub's **HubSign**
service, identifying itself honestly as `shoutabout/<version>`. In testing, HubSign answered with
a redirect instead of a signed file: it seems to accept only clients it knows, such as the Cherri
compiler. ShoutAbout doesn't pretend to be Cherri. When HubSign refuses, the command exits with
an error and writes nothing.

**Options without a Mac:**

1. **Cherri** (most practical). `make-shortcuts --format cherri` writes `Dictate.cherri` and the
   others: [Cherri](https://cherrilang.org) source for the same shortcuts, with no secrets (the
   same actions, URLs and Wi-Fi name as the plist). It sends and signs nothing itself. Then
   either:
   - paste each file into the official **Cherri playground**,
     [playground.cherrilang.org](https://playground.cherrilang.org), press the hammer to
     compile, then the share button and **Sign**, and download the signed shortcut. Save it
     under the shortcut's name (for example `Dictate (offline).shortcut`): iOS names an imported
     shortcut after its file; or
   - run the official **Cherri compiler**, `cherri Dictate.cherri`. Off a Mac it signs through
     HubSign itself, which is Cherri's documented behavior. Cherri publishes builds for macOS and
     Linux only; on Windows, use the Linux build in WSL. Cherri calls Windows untested.

   **Checked with the Cherri 2.3.0 playground (2026-10-10):** all four files compile, and the
   compiled plists have the expected actions, parameters and variable links. Teach ShoutAbout
   and Dictate (offline) were signed this way. None of the Cherri builds has been run on an
   iPhone yet.

   **One question instead of three.** Cherri 2.3.0, which the playground runs, works out each
   import question's action index with a counter that skips `const x = action()` lines, so
   three questions all pointed at the first action (fixed in Cherri's main branch after 2.3.0,
   not yet released). The Cherri source therefore asks a single question, the **pairing text**:
   a bare Comment action comes first, the pasted text goes into the Text action after it
   (index 1 in every Cherri version), and **Get Dictionary from Input** reads `token`,
   `cf_client_id` and `cf_client_secret` out of it. To answer it, run
   `.\.venv\Scripts\python.exe -m shoutabout pair --cloudflare` (or `--cf-file <file>`) on the
   PC, scan the QR code with the iPhone Camera or Code Scanner, choose **Copy** (never Search:
   it holds secrets), and paste. See [pairing.md](pairing.md).
2. **Build by hand** on the iPhone ([Appendix A](#appendix-a-build-dictate-by-hand) and
   [B](#appendix-b-build-dictation-target-by-hand)). Shortcuts you build yourself need no
   signing.

**With a Mac** (yours or someone's): `make-shortcuts --no-sign`, then on the Mac
`shortcuts sign --mode anyone --input <name>.plist --output <name>.shortcut` for each file
**(not tested; no Mac was used)**.

**Looked at and ruled out:**
- Running your own signing server: the server behind HubSign,
  [scaxyz/shortcut-signing-server](https://github.com/scaxyz/shortcut-signing-server), wraps
  `shortcuts sign`, so it needs a Mac too.
- Calling HubSign as Cherri: that would be impersonation. RoutineHub publishes no API or terms
  for HubSign that we could find; Cherri's README links it to RoutineHub's membership page.

---

## Appendix A: Build "Dictate" by hand

Importing the generated shortcut (§3–§5) is the recommended way. This appendix builds the same
design by hand, with **Set Variable** names so it's easier to follow in the editor.

### A.1 Shortcuts editor basics

- **New shortcut:** Shortcuts app > **Shortcuts** tab > **+** (top right). To name it, tap the
  name at the top > **Rename**.
- **Add an action:** use the **Search Actions** field at the bottom of the editor and tap a
  result. New actions are added at the end. **Drag** an action by its title to move it, for
  example into an If branch.
- **If blocks:** adding **If** also inserts **Otherwise** and **End If**. Put actions inside a
  branch by dragging them between those markers.
- **Edit a field:** tap a blue placeholder to edit it. **Show More** on an action reveals its
  extra options.
- **Insert a variable into a text field:** tap the field, then tap the variable in the bar above
  the keyboard, or tap **Select Variable** and pick it **(label may differ slightly)**. Variables
  made with **Set Variable** appear by name.
- **Duplicate an action:** touch and hold it > **Duplicate** **(verify on your phone)**.

Create a new shortcut named **Dictate** and add the actions below in order. The numbers are only
for reference. Move the secrets to the phone as in [§5](#5-answer-the-import-questions).

### A.2 Settings and secrets

| # | Action | Set it to |
|---|---|---|
| 1 | **Text** | paste the **hub token** |
| 2 | **Set Variable** | Variable Name `HubToken`. Input: **Text** (from #1) |
| 3 | **Text** | paste the **CF Client ID** (ends in `.access`) |
| 4 | **Set Variable** | `CFId` |
| 5 | **Text** | paste the **CF Client Secret** |
| 6 | **Set Variable** | `CFSecret` |
| 7 | **Text** | your home Wi-Fi name, `<home-wifi-name>` |
| 8 | **Set Variable** | `HomeSSID` |
| 9 | **Text** | `http://<PC-LAN-IP>:<port>/transcribe`: `lan_url` from `python -m shoutabout info`, plus `/transcribe` (port 8765 by default, or the one you chose with `-Port`) |
| 10 | **Set Variable** | `LanURL` |

**Set Variable** reads as "Set variable [Variable Name] to [Input]". Its input defaults to the
previous action's output, so it should show **Text**. **Never share this shortcut**: a shared
copy includes the contents of these Text actions.

### A.3 Read the target and record

| # | Action | Set it to |
|---|---|---|
| 11 | **Get File from Folder** | Folder: **Shortcuts**. File path: `dictation-target.txt`. **Error If Not Found: OFF** (under Show More; label may differ slightly). |
| 12 | **Get Text from Input** | Input: **File** (from #11) |
| 13 | **Set Variable** | `Target`. Input: **Text** (from #12) |
| 14 | **Record Audio** | **Audio Quality: Normal**. **Start Recording: Immediately**. **Finish Recording: On Tap**. |

No file yet, or an empty one, means **pc**: the hub treats a missing or empty target as `pc`.
Any other value than `pc` or `phone` gets a 422 error.

Why **Normal** quality:

- It's expected to produce a compressed AAC `.m4a` file. The source for this is from 2015, so
  it's unconfirmed on current iOS **(verify on your phone)**. Small files upload quickly on
  cellular.
- Whisper downsamples everything to 16 kHz mono, so higher quality gains nothing.
- **Very High** may be uncompressed WAV: about 10× bigger, roughly 5–6 MB per minute. That's slow
  on cellular, and a 10-minute clip would exceed the hub's 25 MB cap.

To check what you actually get, record 10 seconds. A compressed clip is on the order of 100–200
KB; WAV is over 1 MB.

### A.4 Optional: keep the last recording

Add this so a failed upload isn't lost. [Appendix C](#appendix-c-optional-resend-dictation)
shows how to resend it.

| # | Action | Set it to |
|---|---|---|
| 15 | **Save File** | Input: **Recorded Audio**. **Ask Where to Save: OFF**. Destination: **Shortcuts** folder. Subpath: `last-dictation.m4a`. **Overwrite If File Exists: ON** |

Turn **Overwrite If File Exists** on, or every run creates a numbered copy. Your most recent
recording then sits in iCloud Drive/Shortcuts. Leave this action out if you don't want that.

### A.5 Pick the route

| # | Action | Set it to |
|---|---|---|
| 16 | **Get Network Details** | Network: **Wi-Fi**. Detail: **Network Name**. The action reads "Get Wi-Fi network's Network Name" (label may differ slightly). |
| 17 | **If** | Input: **Network Details**. Condition: **is**. Value: insert the variable **HomeSSID** |

If your router has two network names (for example separate 2.4 GHz and 5 GHz names), add a
second condition to the If: **Network Details is** `<other name>`, and set the If to **Any**.

### A.6 Send it (two request actions)

The two routes need different headers. Headers are fixed rows in each action, so each branch
gets its own **Get Contents of URL** action. Both send the recording itself as the request body
and put the target in the URL **(verified on a real iPhone for the LAN request)**.

**#18, inside the If (home Wi-Fi): Get Contents of URL**

- **URL:** insert the variable **LanURL**, type `?target=`, then insert the variable **Target**.
- Tap **Show More**, then:
  - **Method:** **POST**
  - **Headers:** tap **Add new header**:

    | Key | Value |
    |---|---|
    | `Authorization` | type `Bearer ` (the word, then **one space**), then insert **HubToken** |

  - **Request Body:** **File**. File: **Recorded Audio**.

**Don't use Request Body: Form.** On a real iPhone, a Form body's File field was silently
dropped, and the hub answered `missing 'file' upload`.

**#19, in Otherwise (anywhere else): Get Contents of URL**

Tip: duplicate #18, drag the copy below **Otherwise**, then edit it.

- **URL:** type `https://dictate.shoutabout.fyi/transcribe?target=` (your own hostname; always
  `https://`), then insert **Target**.
- **Method:** **POST**
- **Headers:**

  | Key | Value |
  |---|---|
  | `CF-Access-Client-Id` | **CFId** |
  | `CF-Access-Client-Secret` | **CFSecret** |
  | `Authorization` | `Bearer ` + **HubToken** |

- **Request Body:** **File**. File: **Recorded Audio**.

**#20 End If.** Its output, **If Result**, is the reply from whichever request ran.

Don't leave empty header rows, and spell the keys exactly. Malformed header rows have caused "The
network connection was lost" errors.

### A.7 Handle the answer

| # | Action | Set it to |
|---|---|---|
| 21 | **Get Dictionary Value** | Get **Value** for `text` in **If Result** |
| 22 | **Copy to Clipboard** | **Dictionary Value** (from #21). **Local Only** is your choice (ON keeps it off Universal Clipboard). Leave **Expire At** empty. |
| 23 | **Get Dictionary Value** | Get **Value** for `warning` in **If Result** |
| 24 | **Set Variable** | `Warning` |
| 25 | **Get Dictionary Value** | Get **Value** for `detail` in **If Result** |
| 26 | **Set Variable** | `Detail` |
| 27 | **Get Dictionary Value** | Get **Value** for `status` in **If Result** |
| 28 | **Get Text from Input** | Input: **Dictionary Value** (from #27) |
| 29 | **If** | Input: **Text** (from #28). Condition: **is**. Value: `ok` |
| 30 | **Otherwise** → **Show Notification** | Leave the If branch empty. Body: type `Check the PC. ` then insert **Warning**, type a space, then insert **Detail**. Title (Show More): `Dictation problem` |
| 31 | **End If** | (inserted automatically) |

Why it's built like this:

- **Silent on success.** Only a `status` of exactly `ok` skips the notification. "has any value"
  can't be used for this: it's true even for empty or `null` values.
- **Get Text from Input before the If.** Comparing the raw Dictionary Value with **is** didn't
  match on a real iPhone.
- **Literal text first in the notification.** On an error reply both **Warning** and **Detail**
  can be empty or missing. A notification body that ends up empty fails with "Please choose a
  value for each parameter in this action".

### A.8 The finished shortcut at a glance

```
Text "<hub token>"                        → Set Variable HubToken
Text "<CF Client ID>"                     → Set Variable CFId
Text "<CF Client Secret>"                 → Set Variable CFSecret
Text "<home-wifi-name>"                   → Set Variable HomeSSID
Text "http://<PC-LAN-IP>:<port>/transcribe" → Set Variable LanURL   (port: 8765 by default)
Get File from Folder  dictation-target.txt in Shortcuts (Error If Not Found OFF)
Get Text from Input                       → Set Variable Target
Record Audio  (Normal, Immediately, On Tap)
[Save File  Recorded Audio → Shortcuts/last-dictation.m4a, overwrite]   (optional)
Get Network Details  Wi-Fi → Network Name
If Network Details is HomeSSID:
    Get Contents of URL  LanURL?target=Target  POST
                         Authorization: Bearer HubToken
                         Request Body: File = Recorded Audio
Otherwise:
    Get Contents of URL  https://dictate.shoutabout.fyi/transcribe?target=Target  POST
                         CF-Access-Client-Id: CFId, CF-Access-Client-Secret: CFSecret,
                         Authorization: Bearer HubToken
                         Request Body: File = Recorded Audio
End If
Get Dictionary Value text in If Result    → Copy to Clipboard
Get Dictionary Value warning              → Set Variable Warning
Get Dictionary Value detail               → Set Variable Detail
Get Dictionary Value status               → Get Text from Input
If Text is "ok":
    (nothing)
Otherwise:
    Show Notification "Dictation problem": "Check the PC. " Warning " " Detail
End If
```

**Debugging tip:** to see exactly what the hub returned, temporarily add **Quick Look** with
**If Result** right after #20.

## Appendix B: Build "Dictation Target" by hand

Create a new shortcut named **Dictation Target**:

| # | Action | Set it to |
|---|---|---|
| 1 | **Get File from Folder** | Folder: **Shortcuts**. Path: `dictation-target.txt`. **Error If Not Found: OFF** |
| 2 | **Get Text from Input** | Input: **File** |
| 3 | **If** | **Text** **is** `phone` |
| 4 | ↳ **Text** | `pc` |
| 5 | **Otherwise** → **Text** | `phone` |
| 6 | **End If** | |
| 7 | **Save File** | Input: **If Result**. **Ask Where to Save: OFF**. Destination: **Shortcuts** folder. **Subpath:** `dictation-target.txt`. **Overwrite If File Exists: ON** |
| 8 | **Show Notification** | Body: `Dictation → ` then insert **If Result**. Title: `Dictation` |

How it behaves:

- With no file yet, **Dictate** uses **PC**. The first run of Dictation Target creates the file
  and switches to **Phone**.
- Afterwards, check in **Files > iCloud Drive > Shortcuts** that the file is named exactly
  `dictation-target.txt` and not `dictation-target.txt.txt` **(verify on your phone)**.

**iOS 27 alternative (optional):** iOS 27 added **Store Content** and **Get Stored Content**,
each with a **Global Value** toggle. With **Global Value ON** in *both* shortcuts, you can:

- replace **Get File from Folder** + **Get Text from Input** with **Get Stored Content**, key
  `dictation-target`;
- replace **Save File** with **Store Content**, same key.

That avoids the iCloud file. These names come from early iOS 27 sources **(verify on your
phone)**. The file method works on both iOS 26 and 27, and it's what the generated shortcuts
use.

## Appendix C: Optional "Resend Dictation"

This needs the **Save File** action from [A.4](#a4-optional-keep-the-last-recording). An
imported Dictate doesn't have it: add it in the editor, right after **Record Audio**.

1. In the Shortcuts list, touch and hold **Dictate** > **Duplicate**. Rename the copy to
   **Resend Dictation**.
2. Delete **Record Audio** and **Save File**.
3. In their place, add **Get File from Folder**: Folder **Shortcuts**, path `last-dictation.m4a`,
   **Error If Not Found: ON**. Follow it with **Set Variable** `Audio`.
4. In **both** Get Contents of URL actions, change the **Request Body** file to **Audio**. The old
   **Recorded Audio** reference will show as missing.

Run it from the Shortcuts app, or add it as another Control Center control, after a failed
upload.

## Appendix D: Routing alternatives

- **Always use the public URL.** This is the simplest and most robust option. It works from home
  too, because the request goes out to Cloudflare and back. It's a little slower, and the history
  always shows `"route":"tunnel"`. In the editor:
  1. Drag the public **Get Contents of URL** out of the Otherwise branch, to just below **End
     If**.
  2. Delete **Get Network Details**, the LAN **Get Contents of URL**, and the **If**,
     **Otherwise** and **End If** around them.
  3. Point the four **Get Dictionary Value** actions at **Contents of URL** instead of the
     missing **If Result** **(verify on your phone)**.
- **Route by IP address instead of Wi-Fi name.** Replace **Get Network Details** with **Get
  Current IP Address**, with **Address: Local** and **Type: IPv4**. Then make the If: **If Current
  IP Address begins with** your LAN prefix, e.g. `192.168.0.`
  - **Risk:** many other networks (cafés, hotels, friends' homes) use the same `192.168.0.x`
    range. There, the shortcut would send your hub token and audio over plain HTTP to whatever
    device holds your PC's address.
  - Prefer the Wi-Fi name, or the always-public option.
