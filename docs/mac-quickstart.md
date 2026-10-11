# ShoutAbout on your Mac: quick start

About 15 minutes, most of it waiting. You need an Apple Silicon Mac (M1 or newer) with
macOS 14 Sonoma or newer, an internet connection, and your Mac password (once, maybe).

**Have a `ShoutAbout.dmg` (the signed, notarized build from Codemagic)?** Skip steps 1 and
2: double-click it, drag **ShoutAbout** onto **Applications**, open it from Applications
(click **Open** when macOS asks), then continue at step 3. Where the .dmg comes from:
[release/codemagic.md](release/codemagic.md).

## 1. Get the files

Open this link in Safari (sign in to GitHub if it asks), or use the link you were sent:

    https://github.com/spenseay/shoutabout/archive/refs/heads/v2.zip

Safari downloads it and unpacks it into **Downloads** as a folder called
`shoutabout-v2`. (If you only see `shoutabout-v2.zip`, double-click it.)

## 2. Run one command

1. Open **Terminal**: press ⌘ Space, type `Terminal`, press Return.
2. Paste this line and press Return:

       bash ~/Downloads/shoutabout-v2/packaging/macos/build.sh

3. If macOS asks **"Terminal would like to access files in your Downloads folder"**, click
   **Allow**.
4. Wait. It downloads Python and the libraries, builds the app (5-10 minutes the first
   time), puts **ShoutAbout** in your Applications folder and opens it. You can close
   Terminal when it says `Done`.

## 3. Say yes to macOS (once)

A yellow mouth appears in the menu bar (top right of the screen). macOS then asks:

| You see | Click |
|---|---|
| **"ShoutAbout" would like to access the microphone.** | **Allow** |
| **"ShoutAbout" would like to control this computer using accessibility features.** | **Open System Settings**, then turn on the switch next to **ShoutAbout** (type your Mac password if asked). Then close System Settings. |
| **Allow "ShoutAbout" to find devices on local networks?** (only on some macOS versions) | **Allow** (the iPhone needs it to find your Mac) |
| **Do you want the application "ShoutAbout.app" to accept incoming network connections?** (only if the firewall is on) | **Allow** |

Accessibility is what lets ShoutAbout paste your words where you're typing and notice the
shortcut key. You don't need to restart anything: within a couple of seconds of turning it
on, the shortcut works.

If you missed a dialog: click the mouth in the menu bar. While something is missing, the
menu shows a line like **Allow pasting (Accessibility)…** that opens the right page.

Also normal on the first start: a notification **"ShoutAbout can't use the hotkey"** (it
goes away once Accessibility is on), and macOS's **"Background Items Added"** notice
(that's ShoutAbout opening at login; you can turn it off in ShoutAbout's Settings).

## 4. The setup window

The ShoutAbout window opens by itself:

1. **"Let's get you talking"**: it checks your Mac and downloads the speech model
   (about 490 MB, a minute or two). Click **Next** when the bar is full.
2. **"How will you use ShoutAbout?"**: leave **Talk to this Mac** on. Turn on **Talk from
   my iPhone** if you want to pair your phone now (step 6). Click **Next**.
3. **"Try it"**: press **Right ⌘ Command** once (the ⌘ key to the right of the space bar),
   say "This is my first dictation.", press **Right ⌘** again. The words appear in the box.
4. **"You're all set!"** → **Done**.

## 5. Dictate anywhere

Click into any text field (Notes, Mail, a browser), then:

* tap **Right ⌘** → a yellow mouth appears at the top of the screen → talk;
* tap **Right ⌘** again → the mouth "talks" for a second or two → your words are pasted.
* **Esc** while it listens cancels. Using ⌘ for shortcuts (⌘C, ⌘Tab, ⌘-click) never starts it.

ShoutAbout starts by itself when you log in (Settings > This Mac > Open at login).

## 6. Pair your iPhone

1. On the iPhone, open the **ShoutAbout** app (same Wi-Fi as the Mac).
2. It finds your Mac by itself; tap it.
3. The Mac shows **"<your iPhone> wants to dictate to this Mac. Code 4821."** Check the
   iPhone shows the same code, then click **Allow**.

Later: menu bar mouth > **Settings…** > **Phones** > **Add a phone**.

## If something goes wrong

| Problem | Do this |
|---|---|
| The build stops with `ERROR:` | Send the file `~/Downloads/shoutabout-v2/packaging/dist-mac/build.log` |
| No mouth in the menu bar after the build | Open **Applications** and double-click **ShoutAbout**. Still nothing: send the logs (below) |
| Right ⌘ does nothing | Menu bar mouth: is there an **Allow…** line? Click it and turn ShoutAbout on. Is the menu saying **Paused**? Click **Resume** |
| The mouth appears but nothing is pasted | System Settings > Privacy & Security > **Accessibility**: ShoutAbout must be on. The text is on your clipboard: press ⌘V yourself |
| "Mic blocked" | System Settings > Privacy & Security > **Microphone**: turn on ShoutAbout |
| You rebuilt the app and permissions stopped working | System Settings > Privacy & Security > Accessibility: select ShoutAbout, click **−**, then reopen ShoutAbout and allow again (a rebuilt app counts as a new app) |
| The iPhone doesn't find the Mac | Same Wi-Fi? On a work network, devices may not be allowed to see each other: try your home Wi-Fi |

**Logs to send back:** in Finder press ⇧⌘G, paste `~/Library/Logs/ShoutAbout`, press
Return, and send everything in that folder (`hub.log`, `ui.log`, `stdout.log`; they never
contain what you said, only lengths and timings). Also useful: in Terminal,

    /Applications/ShoutAbout.app/Contents/MacOS/ShoutAbout doctor

and send what it prints.

## Removing it

Quit it from the menu bar mouth, then drag **ShoutAbout** from Applications to the Bin.
Your settings are in `~/Library/Application Support/ShoutAbout`, the login item in
`~/Library/LaunchAgents/fyi.shoutabout.mac.plist`, the speech model in
`~/.cache/huggingface`. Delete those too if you like.
