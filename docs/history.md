# Recent (no history)

ShoutAbout keeps no history of what you dictate. **Recent** (tray > **Recent…**, or the
**Recent** tab of the ShoutAbout window) holds only the last few dictations, so text that
landed in the wrong spot can be copied again.

Code: `recent.py` (the hub's memory), `recent_model.py` and `recent_ui.py` (the page).

## What is kept

- The **last 3 dictations meant for this computer**, newest first: "Talk to this PC", and
  phone dictations in Computer mode (text the phone sends to paste too). A dictation that
  only went to the phone's clipboard isn't kept: it never landed here.
- Each shows when ("14:03", "Yesterday 9:12"), where it came from ("This PC", "This Mac" or
  the phone's name), "Not pasted" when the paste didn't happen, the text (select it to copy a
  part), and **Copy**.
- The page updates by itself within about a second of a dictation, even while it's open.

## Recordings that couldn't be written out

If the speech engine fails *after* a recording was made (CUDA out of memory, a driver
problem), the audio isn't thrown away. The last 3 such recordings, from this computer's key
or a phone upload, are listed above the dictations under **Couldn't write these out**, with
the length and the reason. The notification says "Your recording is kept in Recent."

- **Try again** transcribes it with the current model and never pastes (the ShoutAbout window
  has the focus). The text appears at the top, marked "Written out later", and the recording
  is dropped.
- **Delete** drops it.
- Bad or too-long audio isn't kept: trying again wouldn't help.

## How long

Everything in Recent lives in ShoutAbout's memory only. Nothing is written to disk, a fourth
dictation pushes out the oldest, and it's all gone when ShoutAbout quits.

## Upgrading from a version with History

Earlier versions could keep `history.jsonl`, an `audio` folder and a `failed` folder in the
config folder (`%APPDATA%\shoutabout`, on a Mac `~/Library/Application Support/ShoutAbout`).
The first start of this version deletes them once, removes `[history]` from `config.toml`, and
drops word suggestions mined from the old history that were still waiting (`history-gone` in
`migrations.py`, remembered in `migrations.json`). Until then a leftover `[history]` section
still loads and is ignored.

## Measuring

`hub.log` has one line per request, without the text:
`transcribe id=… route=… target=… pasted=… cleanup=… audio_s=… decode=… stt=… cleanup=… paste=… total=…`.
A cleanup fallback logs `cleanup fell back to the raw transcript: <reason>`.

Average hub-side latency per route:

```powershell
Select-String "$env:APPDATA\shoutabout\logs\hub.log" -Pattern 'transcribe .* route=(\w+) .* total=(\d+)' |
  Group-Object { $_.Matches[0].Groups[1].Value } | ForEach-Object {
    [pscustomobject]@{ route = $_.Name; n = $_.Count
      avg_total_ms = [math]::Round(($_.Group | ForEach-Object { [int]$_.Matches[0].Groups[2].Value } | Measure-Object -Average).Average) }
  } | Format-Table -AutoSize
```

`total` is the hub's processing (decode to paste), not what the phone experiences: the upload
isn't in it.
