# The setup experience (PC)

The promise: a person who has never heard of Whisper finishes setup in about a minute. Every
screen asks for at most one decision, uses plain words, and looks like the brand: Shout
Yellow ground, black type, a red rule under the headline, the talking mouth.

Code: the logic and every line of copy are in `setup_flow.py` and `settings_model.py` (no
Tk, unit-tested); `setup_ui.py`, `settings_ui.py` and `approval_ui.py` only draw them.
`ui_host.py` runs all three on one UI thread.

To see the screens with made-up data (nothing real is touched):

```powershell
.\.venv\Scripts\python.exe scripts\capture_ui.py .screens
```

## When the setup window appears

* **First run:** the hub opens it by itself when the `setup_complete` flag (config dir) is
  missing. Finishing or closing it writes the flag (`finished` / `skipped`).
* **Never on an existing install:** a pre-v2 install that's in use (it has a hub token and
  a dictation history, a vocab that isn't the example, or the v1 setup script's
  install-state.json) is marked as set up the first time the new hub starts. A working PC
  never suddenly shows a wizard.
* **Any time:** tray > **Set up…**, or `python -m shoutabout setup`.
* **Add a phone:** Settings > Phones > **Add a phone** opens just the iPhone card
  (`python -m shoutabout setup --phone`).

## Screen 1: "Let's get you talking" (STEP 1 OF 3)

The mouth talks while the PC is checked and the model downloads.

| State | Copy |
|---|---|
| Checking | "Checking this PC…" |
| Best tier on a GPU | "Your NVIDIA GeForce RTX 4080 SUPER can run our most accurate model." |
| Balanced on a GPU | "Your <GPU> runs our balanced model: quick and accurate." |
| Light (CPU) | "We picked our lightest model, so it stays quick on this PC." |
| Unknown | "We'll pick the best model for this PC." |
| Downloading | progress bar + "Getting the speech model… 42% (672 MB of 1,600 MB)" |
| Already there | full bar + "Speech model ready." (no download at all) |
| Failed | "Couldn't download the speech model. Check your internet, then try again." + **Try again** and a small **Skip for now** link |
| Footer | "Everything runs on this PC. Your voice never leaves it." |

**Next** is enabled once the model is ready (or when the PC can't be checked: then the hub
gets the model ready when it starts, and the tray says "Getting ready… 42%"). Hardware
detection and the model pick come from `hardware.detect()` / `modelpick.resolve()`; the
download from `modelstore.download()`. Closing the window cancels the download.

## Screen 2: "How will you use ShoutAbout?" (STEP 2 OF 3)

Two big cards, each with a switch. Click anywhere on a card to turn it on or off.

**Talk to this PC** (on by default)
: On: "Press Right Ctrl, talk, press it again." (or "Hold Right Ctrl, talk, let go." in hold
  mode), then "The words appear wherever you're typing."
: Off: "Off. Turn it on to dictate with a key."
: No microphone on this PC (Windows' own count, nothing is opened): the card starts **off** and
  says "No microphone found on this PC. Use your iPhone, or plug in a mic." Leaving the screen
  saves that (`[dictation] enabled = false`) unless you switched it on; switched on anyway, it
  adds "No microphone found yet: plug one in first."
: The switch saves `[dictation] enabled` at once (comments in config.toml are kept).

**Talk from my iPhone**
: On: "Open ShoutAbout on your iPhone. It finds this PC by itself." and, with the talking
  mouth, "Waiting for your iPhone…".
: When a phone asks: "Sam's iPhone wants to dictate to this PC." / CODE **4821** / "Allow it
  if your iPhone shows the same code." / **Allow** / **Don't allow** (Allow arms after a
  moment). Then "Connecting Sam's iPhone…" and "Sam's iPhone is connected."
: Don't allow: "Okay, not that one. Still waiting for your iPhone…"
: Expired: "That took too long. Try again on your iPhone."
: **Scan a code instead** shows the v1 pairing QR code (LAN address + hub token) with "Scan
  it in the ShoutAbout app. It's a key to this PC: show it only to your own phone." and
  **Hide the code**.
: Started from the CLI without a running hub: "Start ShoutAbout first, then open this
  again." (pairing requests are handled by the running hub).

**Next** waits while a request is on screen. **Back** goes to screen 1.

## Screen 3: "Try it" (STEP 3 OF 3)

"Press Right Ctrl, say this, then press it again:" (hold mode: "Hold Right Ctrl and say:") /
**"This is my first dictation."** above a big white text box
that has the focus. The real hotkey dictation (or the phone, set to Computer) pastes into
it; as soon as words arrive the window moves on. "Your words land here, as if you'd typed
them." **Skip** and **Back** are small links. With only the iPhone chosen the line reads "Tap
Computer on your iPhone, then say:". With neither chosen this screen is skipped.

## Screen 4: "You're all set!"

The mouth talks once. "ShoutAbout lives in your system tray." plus one reminder per way
chosen ("Press Right Ctrl, talk, press it again." / "On your iPhone, tap Computer and talk.").
**Done** closes the window.

## The approval prompt

When a phone asks to pair and the setup window isn't showing the iPhone card, a small
always-on-top window appears in the bottom-right corner:

> mouth · **Sam's iPhone wants to dictate to this PC.**
> CODE **4821** · "Same code on your iPhone?"
> **Allow** · **Don't allow** · "Closes in 1:59"

It never takes the keyboard focus, no key answers it, Allow arms after one second, closing
it means Don't allow, and it disappears when the request expires (120 s) or is answered
elsewhere.

## Settings (tray > Settings…)

One short page (it scrolls on small screens). Every change saves at once; the bottom line
says "Saved." or "Saved. Restart ShoutAbout to apply."

| Section | What's there |
|---|---|
| Talk to this PC | On/Off switch; **Key** dropdown (from `local_dictation.HOTKEY_CHOICES`); **Press to start and stop / Hold to talk**. Applied live. On a PC without a microphone: "No microphone found on this PC. Use your iPhone, or plug in a mic." |
| Phones | Each paired phone: name, "Used 5 minutes ago" / "Used yesterday" / "Not used yet", **Remove** (asks "Sure? Remove" first; revokes at once). **Add a phone**. |
| Quality | "Automatic: Best (NVIDIA GeForce RTX 4080 SUPER)" or a fixed Best / Balanced / Light; a hand-set model shows as "Custom: … (config.toml)" and is never overwritten unless you pick something. "Needs a 1,600 MB download. ShoutAbout gets it next time it starts." when the choice isn't downloaded. Needs a restart. |
| Clean up wording with AI | **Automatic / On / Off**; "Fixes punctuation and filler words with a small AI model on this PC (Ollama)." Applied live when the cleanup model was loaded at start, else after a restart. |
| History | **Keep a history of what I dictate** switch (`[history] enabled`), **off by default**; "Saved only on this PC, so you can look back, or try again when a recording doesn't go through. Off by default." Applied live (the hub's `history` reloader). |
| This PC | **Name** (what the iPhone lists; re-announced at once) and **Start with Windows** (the installed app: a per-user Run entry; a source checkout: shown read-only, e.g. "Starts when you sign in (setup script task).") |
| Footer | **History…**, **Teach words…**, the version, and small links: Open log folder, Copy hub token (Shortcuts). |

## The tray menu

```
Ready                         <- or "Getting ready… 42%", "Listening", "Writing…",
Press Right Ctrl to talk,        "Paused", "Needs attention: …" (greyed: information)
  and again to stop.
-----------------
Pause / Resume
History…
Teach words…
Settings…
Set up…
-----------------
Quit
```

The old items (open vocab file, open log folder, copy hub token, show LAN URL) moved to the
settings window (logs and token) or the Teach window (vocab), or are covered by pairing.

## History (tray > History…, or Settings > History…)

So a long dictation is never lost. Same look as the other windows: the mouth icon, **HISTORY**
over the red rule, "What you said, newest first. It stays on this PC."

* A **Search** box (every word you type must appear; any case).
* One card per dictation, newest first: "Today 14:03 · This PC" (or the phone's name, e.g.
  "Sam's iPhone"; "Not pasted" when the paste didn't happen), the full text wrapped, and
  **Copy**. The newest 150 matches are drawn; the count says when there are more.
* **Couldn't write these out** (only when there are some): recordings whose transcription failed,
  "Today 14:03 · This PC · 2 min 05 s", the reason, **Try again** (transcribes it with the
  running hub, never pastes: the new text appears at the top of the list, marked "Written out
  later") and **Delete**.
* **Clear history** asks "Sure? Clear everything" (three seconds), then deletes
  `history.jsonl` and every kept recording.
* History is off by default (`[history] enabled = false`). Then: "History is off." / "It's off
  by default, so ShoutAbout doesn't keep what you dictate. Turn it on (here or in Settings) to
  find your dictations in this window. They stay on this PC." / "Recordings that don't go
  through are kept to try again only while history is on." / **Turn history on** (applied
  live).

`python -m shoutabout history` opens it without the hub (Try again then says "Start ShoutAbout
first, then try again."). Details: [history.md](history.md).

## Fresh-install details

* New configs use port **47821** (8765 collides with other software). On the very first
  run, if that port is taken, the next free one is picked and saved in config.toml. Phones
  find the PC through DNS-SD, so nobody types a port. Existing configs keep their port.
* `identity.json` (installation id + optional custom name) is created on first use.
