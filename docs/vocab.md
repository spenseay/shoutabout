# Vocabulary: teaching ShoutAbout your words

Whisper gets everyday English right. It gets names wrong: your projects, your colleagues,
product names, jargon. `vocab.txt` is the list of words you want spelled your way. This page
covers how the list is used, the four ways to add to it, the automatic suggestions, and the
`/vocab` API.

Run the CLI as `.\.venv\Scripts\python.exe -m shoutabout ...` from the repo folder.

## How the vocabulary works

`%APPDATA%\shoutabout\vocab.txt` holds one term or phrase per line.

- **Order is priority.** The terms at the top win when the list is too long.
- Lines that *start* with `#` are comments. `#` inside a term is fine, so `C#` and `F#` are
  terms.
- Duplicates are ignored, case-insensitively. The first spelling wins.
- The file **reloads automatically**. The hub re-reads it on the next dictation after it
  changes, so you never need to restart.

Each dictation passes the terms to Whisper in two ways, as `hotwords` and as an
`initial_prompt`, in the form `In this dictation I mention A, B, C.` The LLM cleanup also gets
the list, so it can fix near-miss spellings such as "better shel" to "Better Shell".

**The budget.** Whisper's prompt is small. The vocab text gets `[stt] max_prompt_tokens`
(default 200) tokens, which is roughly 40 to 60 short terms. Terms past the budget are dropped
from the bottom up, and the hub log warns once per reload:
`vocab: 3 of 64 term(s) don't fit ...`. For that reason, a word you teach goes **to the top** of
the list, just below the comment header. To give an older word priority again, move it up in
the file.

## Four ways to teach a word

| Where | How | Best for |
|---|---|---|
| iPhone | The **Teach ShoutAbout** shortcut: say or type the word | A name you just noticed was wrong |
| PC tray | Right-click the tray icon, then **Teach words…** | Reviewing suggestions and the whole list |
| CLI | `vocab add Chameleon CV` | Scripts and quick fixes |
| Editor | Tray > **Open vocab file**, then edit `vocab.txt` | Reordering and bulk edits |

All four write the same file. The phone, tray and CLI clean up what you give them:
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

### 2. The Teach ShoutAbout window

Open it from the tray menu with **Teach words…**, or with `vocab review`.
- **Top:** a box plus an **Add word** button. Press Enter to add.
- **Left:** pending suggestions, grouped as "From your dictations" and "From your projects",
  each with the reason it was suggested. Tick some, then choose:
  - **Add selected** puts them at the top of the vocab, in the order shown.
  - **Dismiss selected** means they are never suggested again.
  - **Rescan** looks for new suggestions now.
- **Right:** your vocab in priority order. Select one or more words, then **Remove** (or press
  Delete). **Open vocab.txt** opens the file in your editor.

There is also a small **Add a word…** box that has only the entry field. Its
**Review suggestions…** button grows it into the full window. Only one window is open at a
time: opening it again brings the existing one to the front.

### 3. CLI

```powershell
.\.venv\Scripts\python.exe -m shoutabout vocab list [--json]      # priority order
.\.venv\Scripts\python.exe -m shoutabout vocab add Chameleon CV    # one phrase; goes to the top
.\.venv\Scripts\python.exe -m shoutabout vocab remove Chameleon CV
.\.venv\Scripts\python.exe -m shoutabout vocab suggest [--projects] [--json]   # refresh + list pending
.\.venv\Scripts\python.exe -m shoutabout vocab seed [ROOT ...]     # project folders only
.\.venv\Scripts\python.exe -m shoutabout vocab dismiss Some Word   # never suggest it again
.\.venv\Scripts\python.exe -m shoutabout vocab review              # the Teach window
```

`add`, `remove` and `dismiss` join their arguments into one phrase, so `vocab add Chameleon CV`
adds "Chameleon CV". `add` exits with code 1 and prints the reason if the term isn't valid.

### 4. Editing vocab.txt

Use any editor and save the file as UTF-8. The hub picks up the change on the next dictation.
The teaching tools keep your comments, blank lines, order, line endings and BOM. They write
atomically, through a temp file and a rename, under a `vocab.txt.lock` lock file, so a save
from the phone never clobbers a half-written file. If a crash ever leaves a `vocab.txt.lock`
behind, it is ignored after 30 seconds.

## Suggestions

ShoutAbout looks for words you would probably want to teach. **Nothing is added until you
approve it**, in the window, with `vocab add`, or by ticking it.

### From your dictation history

Suggestions come from `history.jsonl` ([history.md](history.md)), using the cleaned `text` of
each dictation. History is off by default, and it is mined only while it's on (Settings > Keep a
history of what I dictate); with it off, only project folders are suggested from.
- **Cleanup corrections.** Words the LLM cleanup re-cased or corrected into a proper-looking
  word, for example "github" to "GitHub". One occurrence is enough. The detail shows what
  Whisper heard.
- **Capitalised names.** Capitalised words and 2 to 3 word Capitalised Phrases that appear
  **mid-sentence** at least twice, such as "Priya Raghunathan" or "Chameleon CV". A capital
  letter at the start of a sentence doesn't count. In "Tell Priya Raghunathan…", the name
  counts once it has also been seen mid-sentence somewhere else.
- **Mixed-case and code-like tokens** that appear at least twice anywhere, such as `GitHub`,
  `iOS`, `ShoutAbout` or `GPT5`.

What is left out:
- terms already in your vocab
- anything you accepted or dismissed before
- words that are mostly written in lowercase ("Plan" vs "plan")
- runs of 4 or more capitalised words, which are usually titles
- a built-in stoplist of everyday capitalised words: I, I'm, OK, days, months, greetings,
  common acronyms (AI, PC, URL…) and names Whisper already knows (Google, Windows, iPhone…)

### From your project folders

ShoutAbout scans these folders 2 levels deep: `~/Documents`, `~/source/repos`, `~/repos`,
`~/code` and `~/projects`, whichever exist. You can set your own with `[learn] project_roots`,
or with `vocab seed ROOT...`.

A folder is a project if it has `.git`, `pyproject.toml`, `package.json`, `Cargo.toml`,
`go.mod` or a `*.sln`. Candidates come from:
- the folder name
- git remote repo names, read from `.git/config` (git itself never runs)
- the package name in `package.json`, `pyproject.toml`, `Cargo.toml` or `go.mod`
- the README's first `# Title`

Names are turned into how you'd say them: `chameleon-cv` becomes "Chameleon CV",
`better_shell` becomes "Better Shell", and `ios-app` becomes "iOS App". Common acronyms stay in
capitals (AI, API, CV, UI, UX, GPU, LLM, SDK, CLI, DB, ID…), and mixed-case names stay as they
are (`ShoutAbout`). Clones and git worktrees of the same repo count as one suggestion, so you
might see "+10 more folders". Variant folders such as `app-review`, `app-src` or
`plot-twist-beta` fold into their project. The scan never enters hidden folders, `.venv`,
`node_modules` or other projects, and it stops after a few thousand folders.

### When suggestions are refreshed

- **The hub:** 90 seconds after it starts, then every 24 hours. When a refresh finds **new**
  suggestions, the tray shows one notification, **Teach words**: "N new words to teach." (the
  title is the menu item's name). It doesn't nag about the same words again.
- **Rescan** in the window, `vocab suggest` and `vocab seed` refresh whenever you run them.

Settings in `config.toml` take effect after a hub restart:

```toml
[learn]
enabled = true             # false = no background suggestions (the CLI and window still work)
seed_projects = true       # also suggest names from project folders
project_roots = []         # folders to scan; empty = the defaults above
notify = true              # one tray notification when new suggestions are waiting
```

### Where suggestions are kept

`%APPDATA%\shoutabout\vocab-suggestions.json` holds each suggestion with its `status`:
`pending`, `accepted` or `dismissed`, plus `source`, `count`, `detail` and `first_seen`.

Accepted and dismissed words are remembered, so they never come back, even if you later remove
an accepted word from `vocab.txt`. To start over, delete the file. To un-dismiss one word, add
it with `vocab add`.

## Privacy

- **Everything stays on this PC.** Mining and scanning run locally, and nothing is uploaded.
- The project scan is **read-only**. It lists folders and reads small files: `.git/config`,
  `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod` and the first 8 KB of a README.
- `vocab-suggestions.json` contains words taken from your dictations, and sometimes the
  misheard form, as in `Whisper heard "github"`. Treat it like `history.jsonl`. Turning history
  off (the default) or deleting `history.jsonl` stops history suggestions. `[learn] enabled =
  false` stops the background job.
- `hub.log` never contains vocabulary terms. `/vocab` logs only counts, such as
  `vocab add route=lan added=true status=added chars=12 count=31`, and suggestion refreshes log
  `vocab suggestions refreshed: 3 new, 12 pending`.
- `/vocab` only ever reads or edits `vocab.txt`. It never pastes or types anything on the PC.

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
{"terms": ["Chameleon CV", "Better Shell", "Claude Code"], "version": 4, "count": 3}
```

The terms are in priority order. `version` goes up each time the hub reloads the file.

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
