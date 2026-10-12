# Talk to this PC

Press **Right Ctrl**, speak, press it again: the text is pasted where you're typing. No phone
needed. A small yellow mouth pops up at the top of the screen the moment ShoutAbout starts
listening, and stays while it thinks.

It uses exactly the same steps as dictating from the iPhone in "Computer" mode: your vocabulary,
the optional text tidying with its safety guard, one paragraph, the same paste rules (Ctrl+V, or
Shift+Insert in terminals, never Enter), Pause, and Recent. Audio and text never leave the PC.

Code: [`local_dictation.py`](../src/shoutabout/local_dictation.py) (the controller),
[`hotkey.py`](../src/shoutabout/hotkey.py), [`mic.py`](../src/shoutabout/mic.py),
[`overlay.py`](../src/shoutabout/overlay.py), [`recent.py`](../src/shoutabout/recent.py).

## Using it

**Press to start, press to stop** (`mode = "toggle"`, the default):

| You do | ShoutAbout does |
|---|---|
| Tap Right Ctrl (press and release, under half a second, no other key in between) | The mouth appears at once and the microphone opens. Start talking. |
| Keep talking, type, use other keys | Nothing changes: other keys are just typing. Right Ctrl + V (a shortcut) doesn't stop it either. |
| Tap Right Ctrl again | The mouth "talks" while it transcribes, then zips shut and the pill flattens away: the text is pasted. |
| Right Ctrl + C, Right Ctrl + V, Ctrl + Shift + T… | Nothing at all: no pill, no microphone. Shortcuts work as always. |
| Hold Right Ctrl longer than half a second (on its own) | Nothing (that isn't a tap). |
| Esc while listening | Cancelled: the mouth shuts and fades, nothing is pasted. |
| Talk for 10 minutes | The pill shows no clock or countdown. At the limit (`max_seconds`, 600 s) it stops by itself and pastes what it heard. |

Precisely: listening starts at the key-up of a *clean tap*: the hotkey went down and up within
0.5 s and no other key went down in between (and no other key was already held). While
listening, the hotkey pressed and released on its own (any length) stops it; the hotkey together
with another key is a shortcut and doesn't. Esc cancels.

**Hold to talk** (`mode = "hold"`, Settings > "Hold to talk"):

| You do | ShoutAbout does |
|---|---|
| Hold Right Ctrl | After 0.2 s the mouth appears and the microphone opens. Start talking. |
| Let go | Transcribes and pastes, as above. |
| Tap Right Ctrl | Nothing (a tap isn't a dictation). |
| Right Ctrl + another key | A shortcut: nothing starts, or listening stops quietly and nothing is pasted. |

The mouth means:

| Look | State |
|---|---|
| A little yellow pill, the mouth opening with your voice | Listening |
| The mouth talking (the brand animation) | Transcribing |
| The mouth zips shut, the zipped line flattens away, then the pill flattens and fades (under 0.75 s) | Pasted |
| Yellow, "Didn't catch that" | Nothing was heard |
| Yellow, "Getting ready… 42%" | The speech model is still downloading or loading |
| Yellow, "Ready · Right Ctrl to talk" | You just opened ShoutAbout (Start menu, the installer's last page) and it's ready. Starting at sign-in stays quiet. |
| Red, "No mic" | This PC has no microphone. A notification explains, once per session. |
| Red, a few words ("Mic blocked", "Admin window", "PC is locked", "Not pasted") | A problem. When the text wasn't pasted it is on the clipboard (and in **Recent**), and a notification explains. |
| Red, "Not working" | Transcription failed. The recording is kept in memory: **Recent > Try again**. |

**The zip** (the owner's "Paddle pull" design, scaled into the little pill; nothing grows outside
it). Timings from the moment the text is pasted:

| When | What |
|---|---|
| 0–0.1 s | The mouth is wide open (tongue out); a white zipper slider pops in at its left (ease-out-back). |
| 0.03–0.33 s | The slider zips left to right along the mouth's centre line (ease-in-out). Behind it a V seam closes the mouth into an ink line with white zipper teeth; its paddle pull trails behind, swinging up to 40°. The tongue goes only as the zip covers it. |
| 0.33–0.51 s | Sealed: the line squashes once and the pull swings past and settles. |
| 0.47–0.63 s | The zipped line, slider and pull flatten away into nothing. |
| 0.6–0.73 s | The empty pill flattens and fades, and is gone. |

The pill is at full opacity from the first frame. The 22 frames (one per 1/30 s) are rendered
with the others in the background, for every monitor's DPI, so it never waits (about 6 ms a frame
at 100 %, 14 ms at 200 %: 0.12–0.3 s more of background work per DPI).

The pill sits top-centre on the monitor of the window you're working in, a few pixels below the
top of its work area (so below a taskbar docked at the top), at that monitor's scaling. It never
takes the keyboard focus (so the text lands in your window), and clicks go through it.

## Settings (`[dictation]` in config.toml)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` turns "Talk to this PC" off (no keyboard listener at all). |
| `hotkey` | `"right_ctrl"` | `right_ctrl`, `right_alt`, `f8`, `f9`, `f10`, `scroll_lock` or `pause`. |
| `mode` | `"toggle"` | `"toggle"` (press to start, press to stop) or `"hold"` (hold to talk). |
| `overlay` | `true` | `false` hides the mouth pill. |
| `max_seconds` | `600` | Stop listening after this long (5–600; 600 is the hub's own limit for a phone recording). |

Pause (tray) turns the hotkey off until you resume. Settings applies changes without a restart
(`VoiceRuntime.reload_dictation`).

**Upgrading:** a config.toml that an earlier version generated says `mode = "hold"` and
`max_seconds = 120` with the generated comment after them. Those two exact lines are switched to
the new defaults once, the first time the new version starts (`migrations.py`; it remembers in
`migrations.json`). A value you edited, or a config without those lines, is never touched.
(Choosing "Hold to talk" in Settings *before* upgrading writes the same line as the old default,
so that one case is switched to toggle once too; picking "Hold to talk" again keeps it.)

Hotkey notes:
- **Right Ctrl** is the safest choice: on its own it does nothing in Windows apps.
- **Right Alt**: on US-style layouts, releasing Alt can open an app's menu bar (Notepad, Explorer),
  which then takes the paste. On layouts where Right Alt is AltGr this doesn't happen (and AltGr's
  hidden Left Ctrl is ignored, so it works as a hotkey).
- **F10** also opens menu bars in many apps; **F8/F9/Scroll Lock/Pause** may mean something in
  specific apps (debuggers, Excel's Scroll Lock). The key still reaches the app: ShoutAbout only
  listens, it never swallows keys.
- Right Shift and the Menu key aren't offered: Shift is held while typing capitals, and releasing
  the Menu key opens a context menu.

## Privacy

- The microphone is opened **only while ShoutAbout is listening** (Windows shows its microphone
  indicator then) and closed as soon as it stops. Nothing opens it for a key press that turns out
  to be a shortcut. Whether a microphone exists at all is asked of Windows (`waveInGetNumDevs`),
  which opens nothing.
- The recording stays in memory and is never written to disk. If transcribing it **fails**, it
  is kept (in memory, the last 3) for **Recent > Try again**, until it's written out, deleted,
  or ShoutAbout quits.
- The keyboard listener sees key-down/key-up events to recognise the hotkey. It keeps nothing,
  logs nothing about keys, and passes every key on unchanged. Keys typed by programs (including
  ShoutAbout's own Ctrl+V) are ignored.
- Recent keeps the last 3 dictations in memory only ([history.md](history.md)).

## Problems

| You see | Fix |
|---|---|
| "No mic" + one notification | No microphone is plugged in (or Windows doesn't see one). Plug one in, or pick one in **Settings > System > Sound > Input**, or use your iPhone. The next press uses it (ShoutAbout re-reads the device list when the number of microphones changes). |
| "Mic blocked" + a notification | Windows is blocking desktop apps from the microphone. Open **Settings > Privacy & security > Microphone** and turn on **Microphone access** and **Let desktop apps access your microphone**. ShoutAbout reads these switches to tell you which one is off. |
| "Mic is silent" | The microphone sent pure silence: it's muted (a hardware switch or the Sound settings), or it's the wrong input. |
| "Not working" | The speech engine failed. The notification says why; the recording is in **Recent** with **Try again**. |
| Nothing happens when you press the key | Is the hub paused? Is `[dictation] enabled = true`? Did you hold it longer than half a second (toggle mode wants a tap)? Is an **admin** window focused (Windows doesn't let normal apps see keys typed into admin windows, and ShoutAbout couldn't paste there anyway)? `hub.log` says "listening for the dictation hotkey" when it's on. |
| "Admin window" | The text is on the clipboard: paste it yourself (Windows blocks typing into apps running as administrator). It's in Recent too. |
| "Getting ready…" | The speech model is downloading or loading; see [models.md](models.md). |
| The first word is cut off | Start talking when the mouth appears. |

## How it works

```
keyboard hook thread ("hotkey-hook")   WH_KEYBOARD_LL; observe-only: always CallNextHookEx,
   |  (vk, up/down, perf_counter time)    injected events skipped, nothing else done in the hook
hotkey worker ("hotkey")                HotkeyMachine: taps, holds, shortcuts, Esc; checks the
   |  START / STOP / CANCEL               physical key while busy (a release the lock screen
   v                                      swallowed still counts as a release)
LocalDictation                          START: overlay "listening" FIRST, then open the mic
   |                                     STOP: close the mic, overlay "thinking",
   |                                           worker thread -> Pipeline.process_audio(...)
   v                                     CANCEL: drop the recording
Pipeline (same as /transcribe)          holds HubState.busy: phone requests and local dictation
                                        take turns; STT -> cleanup -> paste -> Recent
overlay thread ("overlay")              layered window, created at start; ~30 fps while visible
overlay-warm / mic-warm (at start)      pre-render the pill's frames for every monitor's DPI;
                                        start PortAudio (lists devices, opens nothing)
```

- The hook is removed when "Talk to this PC" stops (Pause, Quit, settings change) and at process
  exit; Windows also removes it if the process dies.
- A new dictation can start while the previous one is still being transcribed; it waits its turn
  for the pipeline, and the old result doesn't overwrite the new "listening" pill.
- If a device refuses 16 kHz, the recorder opens it at its own rate and resamples.

### Speed: from the key to the pill

The owner's first laptop test found the pill "laggy". Measured here with injected key events
(`HotkeyListener` with a fake hook; the real overlay window; `ShowWindow` / `UpdateLayeredWindow`
timestamped with `perf_counter`; a fake recorder, since the build PC has no microphone):

| From the trigger to the pill fully visible | Before | After |
|---|---|---|
| Toggle mode (trigger = key-up of the tap) | 133–171 ms (half-visible after 55–102 ms) | 0.6–2.3 ms |
| Hold mode (trigger = end of the 0.2 s hold) | 134–193 ms | 1.0–3.8 ms |
| Hold mode, with the key state lagging like Windows' | 560–664 ms (the hold only started on auto-repeat) | 0.9–1.5 ms |
| Toggle mode, lagging key state | the pill showed at the key-*down* (so also for Right Ctrl + C) | 0.9–2.3 ms after the key-up; nothing for shortcuts |

Plus one display refresh for Windows to compose it (≤ 17 ms at 60 Hz). What made it slow, and
the fix for each:

1. **The physical key check raced the hook.** Windows calls a low-level keyboard hook *before* it
   updates `GetAsyncKeyState`; the worker read the key right after a key-down and could see it
   "up". In toggle mode that turned every key-down into a whole tap (the pill popped up during
   Right Ctrl + C); in hold mode it turned the press into a tap, and listening only began with the
   key's auto-repeat, about half a second later. Now an "up" reading only counts once 0.25 s have
   passed since the last hotkey event (it's only there to catch a release the lock screen took).
2. **A fade-in** of 0.12 s, drawn on a 33 ms timer that Windows rounds to 15.6 ms ticks: the pill
   now appears at full opacity.
3. **Rendering on first show.** Each listening frame took 8–20 ms to draw (pure-Python
   anti-aliasing), the first one when the pill was needed. Frames (the pill with its mouth × 14
   levels, the talk loop, the zip) are now cached, rendered on a background
   thread when "Talk to this PC" starts, for every monitor's DPI, and the first frame is copied
   into the window's bitmap ahead of time. Showing the pill is one `UpdateLayeredWindow` plus
   `ShowWindow(SW_SHOWNOACTIVATE)`.
4. **The microphone opened before the pill.** On the first press `import sounddevice` (PortAudio
   listing every device: ~100 ms here) plus opening the stream ran before the pill was asked
   for. Now the pill is told first, and PortAudio is started at start-up in the background.
5. **Timer granularity.** The hold timer waited on a queue with a timeout (rounded up to the next
   15.6 ms tick) and stamped keys with `time.monotonic` (15.6 ms resolution). Keys are now
   stamped with `perf_counter` and the last stretch of the hold delay is slept precisely; queued
   key events are judged by their own timestamps, so a busy moment never reorders a tap.

The microphone stream opens right after the pill (same thread). The time to open it on a real
device wasn't measured (no microphone on the build PC); in toggle mode people start speaking
after seeing the pill, so a few tens of milliseconds don't cost words.

## What was and wasn't tested

Tested automatically (fakes, no real keyboard hook, no real microphone): the hotkey state machine
event by event (taps, the 0.5 s tap limit, holds, shortcuts including Right Ctrl + C / V and
Ctrl + Shift + T in both modes with a lagging key state, Esc, missed releases, AltGr), the
listener with queued events and its timer precision, the hook glue with a fake user32
(pass-through, injected events, unhook on stop and on errors), the recorder with a fake audio
stream (levels, the time limit, resampling, privacy messages, digital silence, device rescans),
every overlay frame and its timeline (no fade-in, no glow or clock; the zip: the slider runs left to
right, the zipped line with its teeth where it passed, no tongue once sealed, all inside the
pill, pre-rendered), the placement
maths (top-centre, top-docked taskbar, other monitors), the controller end to end with the real
pipeline (paste, Recent when pasting fails, pause, busy lock, errors, "No mic" once per session,
failed recordings kept), and that a Right Ctrl + C never calls the overlay at all. The real
overlay window was shown (under a second) at the top of this PC's screen: it appeared within
0.1 s of being asked, didn't take the focus, and sat at the top of the work area.

**Not tested here** (the safety rules for building this forbid it on the owner's PC, which has no
microphone either): a real keyboard hook with real key presses, and a real microphone. See the
"Talk to this PC" section of the [manual test checklist](manual-test-checklist.md).
