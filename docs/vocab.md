# Vocabulary: teaching ShoutAbout your words

The speech model gets everyday English right. It gets names wrong: your projects, your colleagues,
product names, jargon. `vocab.txt` is the list of words you want spelled your way, plus any
replacements (like your phone's text replacement: say "omw", get "on my way"). This page
covers how the list is used, the ways to add to it (mainly **Fix a word** on the Recent page),
and the `/vocab` API.

Run the CLI as `.\.venv\Scripts\python.exe -m shoutabout ...` from the repo folder.

## How the vocabulary works

`%APPDATA%\shoutabout\vocab.txt` holds one term or phrase per line.

- **Order is priority.** The terms at the top win when the list is too long.
- Lines that *start* with `#` are comments. `#` inside a term is fine, so `C#` and `F#` are
  terms.
- Duplicates are ignored, case-insensitively. The first spelling wins.
- The file **reloads automatically**. The hub re-reads it on the next dictation after it
  changes, so you never need to restart.

Each dictation passes the terms to the speech model in two ways, as `hotwords` and as an
`initial_prompt`, in the form `In this dictation I mention A, B, C.` The LLM cleanup also gets
the list, so it can fix near-miss spellings such as "better shel" to "Better Shell".

That's only a hint, though: a name the speech model already knows another spelling of still comes out
its way ("Sofia" as "Sophia"). So every dictation also gets a spelling pass at the end, with
or without the cleanup (`shoutabout/vocabfix.py`, the same rules as the iPhone app):
- A word said with different capitals or spacing gets your spelling: "better shell" becomes
  "Better Shell", "shout about" becomes "ShoutAbout".
- A word that **sounds like** yours and is spelled almost like it becomes yours: "Sophia"
  becomes "Sofia". To avoid false hits, an everyday word ("sofa", "cloud") only changes when
  the speech model capitalised it mid-sentence, as it does for a name.
- A word with a symbol also matches when you say the symbol: "/consolidate" catches "slash
  consolidate", "C#" catches "C sharp", "Node.js" catches "node dot js".

## Replacements

A line with an arrow is a replacement: what you say on the left, what gets written on the
right. Matching ignores case and works on whole words only.

```text
omw -> on my way
slash consolidate -> /consolidate
Sophia -> Sofia
```

Use one for a shortcut (`omw`), for anything that doesn't sound like what's written, or to
force a fix the sound-alike rules don't make. Replacements aren't sent to the speech model and don't
count toward the budget. Each spoken phrase can have one replacement; the first line wins.
`=>` and `→` work as the arrow too. To add one, fill in **When I say** on the Teach words
page, or run `vocab add "omw -> on my way"` (with the quotes).

**The budget.** The speech model's prompt is small. The vocab text gets `[stt] max_prompt_tokens`
(default 200) tokens, which is roughly 40 to 60 short terms. Terms past the budget are dropped
from the bottom up, and the hub log warns once per reload:
`vocab: 3 of 64 term(s) don't fit ...`. For that reason, a word you teach goes **to the top** of
the list, just below the comment header. To give an older word priority again, move it up in
the file.

## Ways to teach a word

| Where | How | Best for |
|---|---|---|
| Recent | **Fix a word** on the dictation, then type the right spelling ([below](#fix-a-word-recent)) | A word that just came out wrong on this computer. This is the main way. |
| iPhone | The **Teach ShoutAbout** shortcut: say or type the word | A name you just noticed was wrong on the phone |
| PC tray | Right-click the tray icon, then **Teach words…** | Adding words ahead of time, replacements, and the whole list |
| CLI | `vocab add Chameleon CV` | Scripts and quick fixes |
| Editor | Teach words > **Open vocab.txt**, then edit it | Reordering and bulk edits |

All of them write the same file. The phone, tray and CLI clean up what you give them:
- They trim whitespace, surrounding quotes and brackets, and trailing punctuation, so
  `“Chameleon CV.”` becomes `Chameleon CV`.
- They reject a term that is empty, longer than 64 characters or 6 words, has no letter, spans
  several lines, contains control characters, or starts with `#`.
- They never add a duplicate. "github" doesn't duplicate an existing "GitHub", and the existing
  line stays where it is.

Type the word **the way it should be written**, for example `KeyzPZ`, `iOS` or
`Priya Raghunathan`.

### 1. iPhone: the Teach ShoutAbout shortcut

The shortcut sends `POST /vocab` with the JSON body `{"term": "<word>"}`, using the same hub URL
and token as Dictate. It shows the reply's `message`. If the reply's `status` is `invalid`, it
shows the `reason` instead. See [the API reference](#api-reference) and
[ios-shortcuts.md](ios-shortcuts.md).

### 2. Teach words

Tray > **Teach words…** opens the ShoutAbout window on its **Teach words** page (or
`vocab review`).
- **Top:** a **Word** box plus an **Add** button. Press Enter to add; the word goes to the
  top. Fill in **When I say** as well to add a replacement instead: Word "on my way", When I
  say "omw".
- **Your words**: your words and replacements (`omw → on my way`) in priority order. Select
  one or more, then **Remove** (or press Delete). **Open vocab.txt** opens the file in your
  editor.

### 3. CLI

```powershell
.\.venv\Scripts\python.exe -m shoutabout vocab list [--json]      # priority order
.\.venv\Scripts\python.exe -m shoutabout vocab add Chameleon CV    # one phrase; goes to the top
.\.venv\Scripts\python.exe -m shoutabout vocab add "omw -> on my way"  # a replacement (keep the quotes)
.\.venv\Scripts\python.exe -m shoutabout vocab remove Chameleon CV
.\.venv\Scripts\python.exe -m shoutabout vocab review              # Teach words in the window
```

`add` and `remove` join their arguments into one phrase, so `vocab add Chameleon CV`
adds "Chameleon CV". `add` exits with code 1 and prints the reason if the term isn't valid.

### 4. Editing vocab.txt

Use any editor and save the file as UTF-8. The hub picks up the change on the next dictation.
The teaching tools keep your comments, blank lines, order, line endings and BOM. They write
atomically, through a temp file and a rename, under a `vocab.txt.lock` lock file, so a save
from the phone never clobbers a half-written file. If a crash ever leaves a `vocab.txt.lock`
behind, it is ignored after 30 seconds.

## Fix a word (Recent)

You usually notice a wrong word right after you dictate it. Fix it there:
1. Open the **Recent** page (tray > **Recent…**, or the menu bar on a Mac).
2. Optionally select the wrong word in that dictation's text.
3. Press **Fix a word** next to **Copy**. A row opens under the text: **Wrong** (filled in
   with what you selected), **Right**, **Save** and **Cancel**.
4. Type the right spelling and press **Save** (or Enter). Escape cancels.

ShoutAbout then works out what to teach:
- If the right spelling as a plain word would already fix the wrong one, it adds that
  **word**. That covers the same word with other capitals or spacing, a sound-alike ("Sophia"
  becomes "Sofia"), and a symbol said aloud ("slash consolidate" becomes "/consolidate").
- Otherwise it adds a **replacement**, such as `cloud code -> Claude Code`.

Either way it goes to the top of `vocab.txt`. The dictation's text is corrected right away, so
**Copy** copies the fixed text, and the status line says, for example,
`Saved. "Sophia" will come out "Sofia" from now on.` To undo it, remove the word or the
replacement on the Teach words page.

## Privacy

- **Everything stays on this computer.** Nothing is uploaded, and ShoutAbout never looks
  through your folders for words.
- `hub.log` never contains vocabulary terms. `/vocab` logs only counts, such as
  `vocab add route=lan added=true status=added chars=12 count=31`.
- `/vocab` only ever reads or edits `vocab.txt`. It never pastes or types anything on the PC.

**Upgrading:** earlier versions could suggest words from your project folders. That is gone.
The first start of this version deletes `vocab-suggestions.json` and the `[learn]` section of
`config.toml`, once (`suggestions-gone` in `migrations.py`). Words you already added stay in
`vocab.txt`.

## API reference

Both endpoints follow **exactly the same rules as `/transcribe`**, because one shared check runs
in this order:
1. the source address must be loopback or a configured private LAN range, otherwise 403
2. the rate limit
3. the lockout after repeated auth failures
4. `Authorization: Bearer <hub token>`
5. on the tunnel route, a valid Cloudflare Access JWT when `[access]` is configured

`/vocab` and `/transcribe` share the rate-limit and auth-failure counters, so one doesn't give a
caller extra attempts on the other.

### `GET /vocab`

```json
{"terms": ["Chameleon CV", "Better Shell", "Claude Code"],
 "replacements": [{"say": "omw", "write": "on my way"}], "version": 4, "count": 3}
```

The terms are in priority order. `replacements` lists the `say -> write` lines (the iPhone
applies them to offline dictations too); `count` counts the terms only. `version` goes up each
time the hub reloads the file.

### `POST /vocab`

Send one term in any of these forms. The body is capped at 4 KB.

| Form | Example |
|---|---|
| JSON (the iPhone shortcut) | `Content-Type: application/json`, `{"term": "Chameleon CV"}` (a bare JSON string also works) |
| Plain text | `Content-Type: text/plain` (or `application/octet-stream`, or none), body `Chameleon CV` |
| Form field | `term=Chameleon+CV` (urlencoded or multipart; a multipart *file* named `term` also works) |
| Query string | `POST /vocab?term=Chameleon%20CV` with an empty body |

The `200` reply:

```json
{"added": true, "status": "added", "term": "Chameleon CV", "count": 32, "reason": null,
 "message": "Added \"Chameleon CV\" to your vocab (32 words)."}
```

```json
{"added": false, "status": "exists", "term": "Chameleon CV", "count": 32,
 "reason": "already in vocab", "message": "\"Chameleon CV\" is already in your vocab."}
```

- `status` is one of the exact strings `added`, `exists` or `invalid`. Compare against it in
  Shortcuts: iOS treats empty or null values as "has any value", and its booleans are
  unreliable.
- `term` is the cleaned term. For `exists`, it is the spelling already in the file.
- `count` is the number of terms in `vocab.txt` afterwards.

Errors:

| Status | When | Body |
|---|---|---|
| 400 | Invalid or missing term, bad JSON, non-UTF-8 text | `{"detail": "invalid term: too long (max 64 characters)", "status": "invalid", "reason": "too long (max 64 characters)"}` |
| 401 | Missing or wrong bearer token | `{"detail": "missing or invalid bearer token"}` |
| 403 | Public source address, or a missing or invalid Access JWT on the tunnel | `{"detail": ...}` |
| 413 | Body over 4 KB | `{"detail": "term too large (max 4096 bytes)"}` |
| 415 | A non-text body such as `audio/*` | `{"detail": ..., "status": "invalid", "reason": ...}` |
| 429 | Rate limited, or locked out after failed auth | `{"detail": ...}`, with `Retry-After` |
| 503 | The hub has no vocab file configured | `{"detail": "vocab is not available"}` |

Try it from the PC in Windows PowerShell 5.1:

```powershell
$info = .\.venv\Scripts\python.exe -m shoutabout info --json | ConvertFrom-Json
$token = (Get-Content $info.token_file -Raw).Trim()
$h = @{ Authorization = "Bearer $token" }
Invoke-RestMethod "http://127.0.0.1:$($info.port)/vocab" -Headers $h
Invoke-RestMethod "http://127.0.0.1:$($info.port)/vocab" -Method Post -Headers $h `
  -ContentType "application/json" -Body '{"term": "Chameleon CV"}'
```
