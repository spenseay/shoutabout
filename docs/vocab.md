# Vocabulary: teaching ShoutAbout your words

Whisper gets everyday English right. It gets names wrong: your projects, your colleagues,
product names, jargon. `vocab.txt` is the list of words you want spelled your way. This page
covers how the list is used, the four ways to add to it, word suggestions, and the `/vocab`
API.

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
| Editor | Teach words > **Open vocab.txt**, then edit it | Reordering and bulk edits |

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

### 2. Teach words

Tray > **Teach words…** opens the ShoutAbout window on its **Teach words** page (or
`vocab review`).
- **Top:** a box plus an **Add word** button. Press Enter to add; the word goes to the top.
- **Suggestions** (left), each with the reason it was suggested. Tick some, then choose:
  - **Add selected** puts them at the top of the vocab, in the order shown.
  - **Dismiss** means they are never suggested again.
  - **Suggest from my project folders** looks through your project folders once, now
    ([below](#from-your-project-folders)).
- **Your words** (right): your vocab in priority order. Select one or more words, then
  **Remove** (or press Delete). **Open vocab.txt** opens the file in your editor.

### 3. CLI

```powershell
.\.venv\Scripts\python.exe -m shoutabout vocab list [--json]      # priority order
.\.venv\Scripts\python.exe -m shoutabout vocab add Chameleon CV    # one phrase; goes to the top
.\.venv\Scripts\python.exe -m shoutabout vocab remove Chameleon CV
.\.venv\Scripts\python.exe -m shoutabout vocab suggest [--json]    # list waiting suggestions
.\.venv\Scripts\python.exe -m shoutabout vocab suggest --projects  # look through project folders first
.\.venv\Scripts\python.exe -m shoutabout vocab seed [ROOT ...]     # look through project folders
.\.venv\Scripts\python.exe -m shoutabout vocab dismiss Some Word   # never suggest it again
.\.venv\Scripts\python.exe -m shoutabout vocab review              # Teach words in the window
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

ShoutAbout can suggest names from your project folders. **Nothing is added until you approve
it**: tick it and press **Add selected**, or use `vocab add`.

### From your project folders

Project folders are only looked at when you ask: **Suggest from my project folders** in Teach
words, `vocab seed` or `vocab suggest --projects`. ShoutAbout then scans these folders 2 levels
deep, whichever exist:
- Windows: `~/Documents`, `~/source/repos`, `~/repos`, `~/code` and `~/projects`.
- Mac: only folders macOS doesn't guard: `~/Developer`, `~/code`, `~/Code`, `~/repos`,
  `~/projects`, `~/Projects`, `~/src`, `~/git` and `~/source/repos`. Never `~/Documents`,
  `~/Desktop` or `~/Downloads`.

You can set your own with `[learn] project_roots`, or with `vocab seed ROOT...`.

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
`node_modules` or other projects, and it stops after a few thousand folders. Left out: terms
already in your vocab, anything you accepted or dismissed before, and generic folder names
(`src`, `docs`, `demo`…).

### When suggestions are refreshed

- **When you ask:** **Suggest from my project folders**, `vocab seed` and
  `vocab suggest --projects`.
- **By itself, only with `seed_projects = true`** (set by hand in `config.toml`): the hub looks
  90 seconds after it starts, then every 24 hours. When it finds **new** suggestions it shows
  one notification, **Teach words**: "N new words to teach." It doesn't nag about the same
  words again.

Settings in `config.toml` take effect after a hub restart:

```toml
[learn]
enabled = true
seed_projects = false      # true = also look through your project folders every day by itself
project_roots = []         # folders to scan; empty = the defaults above
notify = true              # one notification when the daily look finds new words
```

**Upgrading:** an existing install's `seed_projects = true` is switched off once
(`learn-projects-off` in `migrations.py`), and project suggestions still waiting are dropped.
Accepted and dismissed words stay remembered, so they don't come back (where they came from is
forgotten). Setting `seed_projects = true` again afterwards sticks.

### Where suggestions are kept

`%APPDATA%\shoutabout\vocab-suggestions.json` holds each suggestion with its `status`:
`pending`, `accepted` or `dismissed`, plus `source`, `count`, `detail` and `first_seen`.

Accepted and dismissed words are remembered, so they never come back, even if you later remove
an accepted word from `vocab.txt`. To start over, delete the file. To un-dismiss one word, add
it with `vocab add`.

## Privacy

- **Everything stays on this computer.** The scan runs locally, and nothing is uploaded.
- Project folders are looked at only when you ask (or with `seed_projects = true`). The scan is
  **read-only**: it lists folders and reads small files: `.git/config`, `package.json`,
  `pyproject.toml`, `Cargo.toml`, `go.mod` and the first 8 KB of a README.
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
