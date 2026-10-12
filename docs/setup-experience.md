# The setup experience (PC)

The promise: a person who has never heard of Whisper finishes setup in about a minute. It's a
very small app, so the windows stay small too: as few screens as possible, one line of help
at most, no tours ("look for the icon next to the clock") and no disclaimers. Every screen
looks like the brand: Shout Yellow ground, black type, a red rule under the headline, the
talking mouth.

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
* **Add a phone:** Settings > Phones > **Add a phone** opens just the "Connect your iPhone"
  screen (`python -m shoutabout setup --phone`).

Setup changes no settings: Talk to this PC, the key and everything else are in Settings. The
model is picked automatically; downloading it is the only thing setup has to do. On a Mac it
first asks for the permissions macOS guards (screen 0).

## Screen 0 (Mac only): "Allow access"

The first screen on a Mac while something is missing (a Mac that allows everything never
sees it). One line per permission, a round mark that turns black with a tick as soon as
macOS allows it (read about once a second), and **Allow**:

| Line | Allow does |
|---|---|
| **Microphone** to hear you | macOS's own "would like to access the microphone" dialog; once asked (or refused), System Settings > Privacy & Security > Microphone |
| **Accessibility** for Right ⌘ Command and pasting | macOS's "would like to control this computer" prompt (it puts ShoutAbout in the list) and System Settings > … > Accessibility |
| **Input Monitoring** for Right ⌘ Command | only listed when Accessibility can't be read at all; System Settings > … > Input Monitoring |

When System Settings opened, one line says "Turn on ShoutAbout there, then come back." When
everything is allowed the window comes back to the front and moves on by itself (to "Try it"
when the model is ready, else "Getting ready", which downloads meanwhile). A small **Skip**
link moves on without them; "Try it" then only shows if the microphone and the hotkey work,
and the last screen says "Almost there" / "Talk to this Mac needs access first." with an
**Allow access** link back. Without Accessibility (a managed Mac may never allow it) "Try it"
still works when the hotkey does: "Then press ⌘V to paste it."

The hub asks macOS, not the window (`mac/access.py`): it's the process that records, taps
the keyboard and pastes. Clicking a ShoutAbout notification while something is missing opens
the same screen alone (`SetupFlow(mode="allow")`; Skip closes it, and once everything is
allowed it says how to talk).

## Screen 1: "Getting ready"

The mouth talks while the model downloads; no buttons. When the model is ready (or was
already there) the window moves on by itself.

| State | Copy |
|---|---|
| Checking | "One moment…" |
| Downloading | "Downloading the speech model… 42%", a progress bar, "672 MB of 1,600 MB" |
| Failed | "Couldn't download the speech model. Check your internet, then try again." + **Try again** and a small **Skip for now** link (to the last screen; the hub downloads the model when it starts, and the tray says "Getting ready… 42%") |

The model pick comes from `hardware.detect()` / `modelpick.resolve()`, the download from
`modelstore.download()`. When the PC can't be checked the window moves on too (the hub gets
the model ready when it starts). Closing the window cancels the download.

## Screen 2: "Try it"

"Press Right Ctrl, say this, then press it again:" (hold mode: "Hold Right Ctrl and say:") /
**"This is my first dictation."** above a big white text box that has the focus. The real
hotkey dictation pastes into it; as soon as words arrive the window moves on. **Skip** is a
small link. Skipped when Talk to this PC is off or Windows reports no microphone at all (and
on a Mac while the microphone or the hotkey isn't allowed).

## Screen 3: "You're all set"

The mouth talks once, then one line: "Press Right Ctrl, talk, press it again." (hold mode:
"Hold Right Ctrl, talk, let go."). With no microphone: "No microphone found. Plug one in, or
connect your iPhone." (Talk to this PC stays on, so a mic plugged in later just works.)
**Done** closes the window; a small **Connect your iPhone** link opens screen 4.

## Screen 4 (optional): "Connect your iPhone"

One card:

* "Open ShoutAbout on your iPhone." and, with the talking mouth, "Waiting for your iPhone…".
* When a phone asks: "Sam's iPhone wants to dictate to this PC." / CODE **4821** / "Allow it
  if your iPhone shows the same code." / **Allow** / **Don't allow** (Allow arms after a
  moment). Then "Connecting Sam's iPhone…" and "Sam's iPhone is connected."
* Don't allow: "Okay, not that one. Still waiting…"; expired: "That took too long. Try again
  on your iPhone."
* **Scan a code instead** shows the v1 pairing QR code (LAN address + hub token) with "Scan
  it in the ShoutAbout app. Show it only to your own phone." and **Hide the code**.
* Started from the CLI without a running hub: "Start ShoutAbout first, then open this again."
  (pairing requests are handled by the running hub).

**Done** closes the window (a request still on screen is answered "no"). A phone that asks
while another screen is showing gets the small approval prompt below.

## The approval prompt

When a phone asks to pair and the setup window isn't showing "Connect your iPhone", a small
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
| Talk to this PC | On/Off switch; **Key** dropdown (from `local_dictation.HOTKEY_CHOICES`); **Press to start and stop / Hold to talk**. Applied live. On a PC without a microphone: "No microphone found." |
| Phones | Each paired phone: name, "Used 5 minutes ago" / "Used yesterday" / "Not used yet", **Remove** (asks "Sure? Remove" first; revokes at once). **Add a phone**. |
| Quality | "Automatic: Best (NVIDIA GeForce RTX 4080 SUPER)" or a fixed Best / Balanced / Light; a hand-set model shows as "Custom: … (config.toml)" and is never overwritten unless you pick something. "Needs a 1,600 MB download. ShoutAbout gets it next time it starts." when the choice isn't downloaded. Needs a restart. |
| AI cleanup | **Only shown when a local Ollama answers with a suitable model** (`AppServices.cleanup_available`, looked for off the UI thread when the window opens); nothing anywhere asks people to install Ollama. **Automatic / On / Off**; "Fixes punctuation and filler words, on this PC." "Automatic" (the default) already turns it on by itself when Ollama is there. Applied live when the cleanup model was loaded at start, else after a restart. |
| History | **Keep a history of what I dictate** switch (`[history] enabled`), **off by default**; "Saved only on this PC." Applied live (the hub's `history` reloader). |
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
and "Saved only on this PC." over the red rule.

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
* History is off by default (`[history] enabled = false`). Then: "History is off." / "Turn it
  on to keep what you dictate, and recordings that fail." / **Turn history on** (applied
  live; Settings has the same switch).

`python -m shoutabout history` opens it without the hub (Try again then says "Start ShoutAbout
first, then try again."). Details: [history.md](history.md).

## Fresh-install details

* New configs use port **47821** (8765 collides with other software). On the very first
  run, if that port is taken, the next free one is picked and saved in config.toml. Phones
  find the PC through DNS-SD, so nobody types a port. Existing configs keep their port.
* `identity.json` (installation id + optional custom name) is created on first use.
