# The Windows installer

ShoutAbout for Windows ships as one file, `ShoutAbout-Setup-<version>.exe`. People download it,
double-click it, click **Next** and **Install**, and ShoutAbout starts. They need no Python, no
terminal and no config files.

This page covers what the installer does to a PC, how it's built and tested, and how to release
it. The developer setup (a clone, `uv`, `scripts\setup.ps1`) still works and is described in
[SETUP.md](../SETUP.md).

- [For people installing it](#for-people-installing-it)
- [What the installer changes on a PC](#what-the-installer-changes-on-a-pc)
- [Uninstalling](#uninstalling)
- [Upgrades](#upgrades)
- [Silent install and uninstall](#silent-install-and-uninstall)
- [ShoutAbout.exe command line](#shoutaboutexe-command-line)
- [Building it locally](#building-it-locally)
- [How CI builds and tests it](#how-ci-builds-and-tests-it)
- [Releasing](#releasing)
- [Code signing](#code-signing)
- [Size](#size)
- [Licenses of what it ships](#licenses-of-what-it-ships)
- [Notes for developers](#notes-for-developers)
- [What's verified](#whats-verified)

## For people installing it

1. Download `ShoutAbout-Setup-<version>.exe`.
2. Double-click it. Windows asks once for permission to make changes (UAC): click **Yes**.
3. Click **Next**. Leave **Start ShoutAbout when I sign in** ticked, unless you don't want it.
4. On the last page, leave **Start ShoutAbout now** ticked and click **Finish**.

ShoutAbout then starts, and its own setup window takes it from there. The wizard itself says
as little as possible: one line on the welcome page and nothing but "ShoutAbout is ready" on
the last one.

**"Windows protected your PC".** The first releases aren't code-signed, so Microsoft
Defender SmartScreen warns about an unrecognised app. Click **More info**, check that the app
is `ShoutAbout-Setup-<version>.exe`, then click **Run anyway**. The warning goes away once
releases are signed (see [Code signing](#code-signing)). Some browsers also say the download
"isn't commonly downloaded": choose **Keep**.

Requirements: 64-bit Windows 10 version 1809 or later, or Windows 11. An NVIDIA GPU makes
transcription fast, but isn't needed. The installer needs about 1.1 GB of disk space, and the
speech model is downloaded on first run.

## What the installer changes on a PC

| What | Where | Removed by the uninstaller |
|---|---|---|
| The app | `C:\Program Files\ShoutAbout\` (`ShoutAbout.exe`, `_internal\`, `THIRD-PARTY-NOTICES.txt`, `unins000.exe`) | Yes |
| Start menu shortcut | `ShoutAbout`, for all users (`%ProgramData%\Microsoft\Windows\Start Menu\Programs`) | Yes |
| Apps list entry | *Settings > Apps > Installed apps* (`HKLM\...\Uninstall\{B70711A6-7F23-457D-B910-31004776AB13}_is1`) | Yes |
| Firewall rule | One inbound rule named **ShoutAbout**: program `C:\Program Files\ShoutAbout\ShoutAbout.exe`, TCP, any port, **Private networks only**. It lets the phone reach the PC at home, and never on public Wi-Fi. | Yes |
| Start at sign-in | Only if **Start ShoutAbout when I sign in** is ticked (the default), on the **first** install: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `ShoutAbout` = `"C:\Program Files\ShoutAbout\ShoutAbout.exe" run`, for the user who ran Setup. After that, the app's settings own this value; upgrades never change it. | Yes (the value and its Task Manager "startup approved" twin) |
| Your data | Nothing at install time. The app creates `%APPDATA%\shoutabout` (settings, vocabulary, history, logs, hub token) on first run, and downloads the speech model. | Only if you ask (below) |
| Setup log | `%TEMP%\Setup Log <date> #<n>.txt` | No (it's in Temp) |

The installer doesn't install services, drivers, scheduled tasks or other programs. It doesn't
change the network profile and doesn't open any port on public networks.

If the Wi-Fi is set to **Public** in Windows, the rule doesn't apply, and Windows may show its
own "allow access?" prompt the first time ShoutAbout listens. The phone can only reach the PC
once the network is **Private** (Settings > Network & internet > Wi-Fi > your network >
Network profile type > Private network). ShoutAbout's own checks still refuse anything that
isn't a private address.

## Uninstalling

Use **Settings > Apps > Installed apps > ShoutAbout > Uninstall**. ShoutAbout asks one
question:

> ShoutAbout will be removed from this PC.
> ☐ Also remove my settings, vocabulary and history

The uninstaller then:

1. stops ShoutAbout for every signed-in user (it asks it to quit first, then ends it after
   5 s);
2. removes the files, the Start menu shortcut, the firewall rule and the start-at-sign-in Run
   value;
3. only if the box is ticked, deletes `%APPDATA%\shoutabout` and `%LOCALAPPDATA%\shoutabout`
   for the user running the uninstaller.

Left the box unticked? Reinstalling picks up the same settings, vocabulary and history.

What always stays: the speech model in the Hugging Face cache
(`%USERPROFILE%\.cache\huggingface\hub`), because other apps can share that cache; any Ollama
models; and the paired phone's settings. Delete those by hand if you like.

If a different administrator account approved the UAC prompt, that account's `%APPDATA%` is
the one cleaned, and the Run value of the user who installed stays behind (Windows ignores it
once the exe is gone).

## Upgrades

Running a newer `ShoutAbout-Setup-<version>.exe` updates ShoutAbout in place:

- it skips the start-at-sign-in question (the app's settings own it by then);
- it stops a running ShoutAbout, replaces `C:\Program Files\ShoutAbout\_internal\` completely
  (no stale files), and updates the firewall rule;
- settings, vocabulary and history are kept;
- the last page offers **Start ShoutAbout now**. A silent upgrade restarts ShoutAbout by itself
  if it was running before.

Running the same version again repairs an installation the same way.

## Silent install and uninstall

For scripted installs (and CI):

```powershell
# install or upgrade, no UI; doesn't start the app (unless it was running before an upgrade)
.\ShoutAbout-Setup-0.1.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="C:\Temp\shoutabout-setup.log"

# uninstall, keep the user's data
& "C:\Program Files\ShoutAbout\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART

# uninstall and delete %APPDATA%\shoutabout too
& "C:\Program Files\ShoutAbout\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /REMOVEUSERDATA
```

Use `/VERYSILENT` for the uninstaller. `/SILENT` (what the Apps list runs) still shows the
one-question dialog. Other standard Inno Setup switches work too, for example
`/MERGETASKS="!autostart"`, which installs without start-at-sign-in (not tested). The
uninstaller runs from a temporary copy, so a script should wait until
`C:\Program Files\ShoutAbout\ShoutAbout.exe` is gone, as `smoke-test.ps1` does.

## ShoutAbout.exe command line

`ShoutAbout.exe` is a windowed program (no console window):

| Command | What it does |
|---|---|
| `ShoutAbout.exe` or `ShoutAbout.exe run` | Runs ShoutAbout: the hub with the tray icon, exactly like `python -m shoutabout run`, with no console. The Start menu shortcut and the installer start it without arguments; the sign-in Run value is `"<exe>" run`. Both behave the same (`--home DIR` may be added). |
| `ShoutAbout.exe --self-test [--home DIR] [--out FILE] [--text]` | Checks that the installation has everything it needs and exits 0 (all good) or 1. Without `--home` it uses a temporary config folder and never touches yours. It never opens the microphone, pastes, or opens a port. |
| `ShoutAbout.exe run --no-tray`, `ShoutAbout.exe <command> ...` | The normal CLI for support and advanced use, e.g. `ShoutAbout.exe doctor`, `ShoutAbout.exe info`, `ShoutAbout.exe pair`. See the [CLI reference](../README.md#cli-reference). |

The self-test checks:

| Check | Proves |
|---|---|
| `modules` | every `shoutabout` module imports |
| `dependencies` | every runtime dependency in `pyproject.toml` imports |
| `config` | config.toml, vocab.txt and the hub token can be created and loaded |
| `tk` | the bundled Tcl/Tk works (a hidden window, never shown) |
| `audio` | Windows Media Foundation decodes the bundled 1 s AAC `.m4a` tone (the iPhone app's format: 16 kHz mono) to 1 s of 440 Hz, and a 48 kHz WAV resamples to 16 kHz |
| `no-ffmpeg` | the installed app contains no PyAV and no FFmpeg file (`avcodec*`, `avformat*`, `*x264*`, `*x265*`, ...), and `import av` doesn't find a real PyAV (required when frozen; SKIP from source) |
| `vad` | onnxruntime runs the Silero voice-activity model |
| `downloads` | huggingface_hub sees `hf_xet`, so model downloads use the fast Xet transfer |
| `ctranslate2` | CTranslate2 loads; CPU compute types and the CUDA device count (0 is fine) |
| `cublas` | `cublas64_12.dll` loads from the bundle by name, as CTranslate2 loads it (no GPU needed) |
| `hardware` | hardware detection, when the build has it (otherwise SKIP) |
| `brand` | the tray icon renders, and the app icon is installed |

A windowed program doesn't hold the console. CLI output still appears in the window you typed
the command in, but the prompt may come back first. To wait for the command and keep its
output tidy, pipe it: `& "C:\Program Files\ShoutAbout\ShoutAbout.exe" doctor | Out-Host`.
From a source checkout, the same self-test runs with `python -m shoutabout.selftest --text`.

## Building it locally

You need the repo, uv, and (only for the setup .exe) [Inno Setup 6](https://jrsoftware.org/isinfo.php).
Building never installs or runs ShoutAbout.

```powershell
uv sync --locked --extra cuda --group packaging
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\build.ps1
```

`build.ps1`:

1. reads the version from `src/shoutabout/__init__.py`;
2. renders `ShoutAbout.ico` (16-256 px) and the wizard images from `shoutabout.brand` into
   `packaging\build\assets\` (`packaging\make_assets.py`; no image tools);
3. runs PyInstaller with `packaging\shoutabout.spec`, which gives
   `packaging\dist\ShoutAbout\ShoutAbout.exe` and its `_internal\` folder (about 30 s), and
   fails if a forbidden file is in it (`packaging\bundle_rules.py check`, see below);
4. writes `packaging\dist\size-report.txt`;
5. compiles `packaging\installer.iss` with ISCC, which gives
   `packaging\dist\ShoutAbout-Setup-<version>.exe`. Without Inno Setup it stops after step 4
   with a warning. `-SkipInstaller` skips this step on purpose.

To try the build without installing anything, run its self-test against a scratch folder:

```powershell
$env:SHOUTABOUT_HOME = "$PWD\.tmp\home"
Start-Process .\packaging\dist\ShoutAbout\ShoutAbout.exe -ArgumentList '--self-test','--out',"$PWD\.tmp\selftest.json" -Wait
Get-Content .\.tmp\selftest.json
```

Don't run `packaging\smoke-test.ps1` on your own PC. It really installs and uninstalls
ShoutAbout and deletes `%APPDATA%\shoutabout`, so it refuses to run outside CI unless you pass
`-Force` (for a throwaway VM).

### What gets bundled

`shoutabout.spec` needs no edits when the app gains dependencies:

- every `shoutabout` module and package data file (examples, any fonts added later);
- for every runtime dependency in `pyproject.toml`, read from the installed metadata (the same
  list the self-test checks): its metadata, its data files minus sources, stubs and tests, and
  its DLLs. A new dependency such as `sounddevice` (with its PortAudio DLL) or `tomlkit` is
  picked up by the next build;
- PyInstaller's hooks cover the rest of the import graph: onnxruntime, Tcl/Tk for tkinter,
  and uvicorn's dynamically imported modules;
- never PyAV (`av` is excluded from the analysis): audio is decoded with numpy and Windows
  Media Foundation ([architecture.md](architecture.md#audio-decoding)), and faster-whisper's
  `import av` gets the placeholder from `shoutabout/avshim.py`. PyAV is no longer a runtime
  dependency in `pyproject.toml` (only the optional `pyav` extra), so it isn't in the list
  above either;
- from the `cuda` extra, only `cublas64_12.dll`, `cublasLt64_12.dll`, `nvrtc64_120_0.dll` and
  `nvrtc-builtins64_*.dll`, under `_internal\nvidia\<lib>\bin` (where
  `engine/cuda_dlls.py` looks).

`packaging\bundle_rules.py` lists what is left out, matched by path inside the bundle:

- **Never shipped (licensing):** anything matching `*asio*`. sounddevice's wheel carries
  ASIO-enabled PortAudio builds made with Steinberg's ASIO SDK, whose license isn't
  permissive; the plain PortAudio DLL (MIT) is all ShoutAbout needs. Three gates enforce it:
  the spec drops the files, `build.ps1` fails if one is in the onedir, and the installer
  script excludes `*asio*` too. The CI smoke test then fails if any `*asio*` file was
  installed. (`SD_ENABLE_ASIO` therefore doesn't work in the installed app.)
- **Never shipped (licensing): PyAV and FFmpeg.** `av/`, `av.libs/`, `av-*.dist-info/` and any
  `avcodec*`, `avformat*`, `avutil*`, `avdevice*`, `avfilter*`, `swresample*`, `swscale*`,
  `postproc*`, `*x264*` or `*x265*` file (PyAV's FFmpeg links x264/x265, which are GPL). The
  same gates apply: the spec drops them and also stops if any `av` module got into the
  analysis, `build.ps1` fails if one is in the onedir, the installer script excludes them,
  `notices.py` refuses to list PyAV, the self-test's `no-ffmpeg` check fails, and the CI smoke
  test checks the installed folder.
- **Not needed (size):** `nvblas64_12.dll` (a BLAS shim nothing loads), the `.alt` NVRTC
  build, `cudnn64_9.dll` from the CTranslate2 wheel (CTranslate2 for Windows is built
  without cuDNN), and PortAudio for other platforms (32-bit, ARM64, macOS).

The dev and build tools and optional heavy packages (torch, transformers, onnxruntime's
tooling, CTranslate2's model converters) are excluded from the analysis. The metadata of every
runtime dependency is copied (recursively), because libraries look each other up with
`importlib.metadata`: without it huggingface_hub wouldn't use `hf_xet`'s fast downloads.
Nothing is UPX-compressed: UPX breaks CUDA DLLs and upsets antivirus scanners.

`THIRD-PARTY-NOTICES.txt` is generated at build time (`packaging\notices.py`). It holds the
license and notice texts of every distribution whose files are in the bundle, NVIDIA's EULA,
and Python's license, which covers Tcl/Tk and OpenSSL.

## How CI builds and tests it

`.github/workflows/windows-installer.yml` runs on `windows-latest`:

- **When:** by hand (*Actions > Windows installer > Run workflow*), on `v*` tags, and on pushes
  to `main` or `v2-*` branches that touch `src/`, `packaging/`, `pyproject.toml`, `uv.lock` or
  the workflow. A newer push to the same branch cancels a running build; tag builds always
  finish.
- **Build:** Python 3.12 from python.org (`actions/setup-python`), then
  `uv sync --locked --extra cuda --no-dev --group packaging`. Inno Setup 6 is installed with
  Chocolatey if the image lacks it. `installer.iss` is compiled once against a placeholder
  folder, which catches script errors in seconds. Then `packaging\build.ps1 -RequireInstaller`.
- **Smoke test** (`packaging\smoke-test.ps1`, as administrator, Windows PowerShell 5.1):
  1. silent install: files, Apps list entry and version, Start menu shortcut, exactly one
     **ShoutAbout** firewall rule (inbound, allow, TCP, **Private** only, for
     `ShoutAbout.exe`), the Run value; no `*asio*` file installed; `ShoutAbout.exe` is a
     windowed (GUI-subsystem) program; a silent install doesn't start the app;
  2. `ShoutAbout.exe --self-test`: every check passes, including cuBLAS from the bundle;
  3. CLI pass-through: `--version` and `info --json`;
  4. starts the installed app exactly as Windows does at sign-in, with the command line in
     the Run value (`"<exe>" run`), and a throwaway config (`SHOUTABOUT_HOME`, 127.0.0.1,
     port 18765, the small `tiny.en` model on the CPU, no LLM); waits for `/health`; posts a
     sentence spoken by Windows' text-to-speech to `/transcribe?target=phone`. The text must
     come back, and nothing is pasted;
  5. silent upgrade while the app runs: the app is stopped, updated and started again
     (without arguments, the Start menu's way);
  6. silent uninstall: app stopped; files, firewall rule, Run value, shortcut and Apps entry
     gone; `%APPDATA%\shoutabout` kept;
  7. reinstall, then uninstall with `/REMOVEUSERDATA`: `%APPDATA%\shoutabout` removed.
- **Wizard screenshots** (`packaging\wizard-screenshots.ps1`, best effort): installs once
  more through the interactive wizard, pressing Enter on each page, then runs the uninstaller
  the way Windows Settings does and accepts the one-question dialog; one screenshot per
  page.
- **Artifacts:** `ShoutAbout-Setup-<version>.exe` (kept 30 days), `installer-reports`
  (the size report, the self-test JSON, and the setup, uninstall and hub logs) and
  `installer-screenshots` (kept 14 days, uploaded even when a step fails).

A run takes about 15 minutes: PyInstaller about 1 minute, Inno Setup's compression about
4 minutes, the smoke test about 5 minutes.

## Releasing

1. Set the new version in **both** `pyproject.toml` (`version`) and
   `src/shoutabout/__init__.py` (`__version__`), merge to `main`, and wait for the
   **Windows installer** run on `main` to pass.
2. Tag and push: `git tag v0.2.0` then `git push origin v0.2.0`. CI fails if the tag doesn't
   match `__version__`.
3. The tag build runs the same build and smoke test, then attaches the installer to a
   **draft** GitHub release named "ShoutAbout 0.2.0".
4. Download the installer from the draft and try it on a real PC: install, the setup window,
   dictation, uninstall. Then edit the notes and click **Publish release**.

Releases are never published automatically. Before the **first public** release, resolve the
licensing flag below.

## Code signing

Builds are **unsigned** until a certificate exists. Signing happens only when one of these is
configured; otherwise the step is skipped without failing:

| Setting | Meaning |
|---|---|
| secret `SHOUTABOUT_SIGN_PFX_BASE64` | the code-signing certificate as a base64 `.pfx` |
| secret `SHOUTABOUT_SIGN_PFX_PASSWORD` | its password |
| variable `SHOUTABOUT_SIGN_TIMESTAMP_URL` | optional RFC 3161 timestamp server (default `http://timestamp.digicert.com`) |
| env `SHOUTABOUT_SIGN_THUMBPRINT` (local builds) | a certificate already in `Cert:\CurrentUser\My`, e.g. on a hardware token |

`build.ps1` imports the `.pfx` into the current user's certificate store for the build only,
and removes it afterwards; the password never appears on a command line. Inno Setup then signs
`ShoutAbout.exe`, the installer and the uninstaller with `signtool.exe` from the Windows SDK
(SHA-256 with an RFC 3161 timestamp).

Since 2023, publicly trusted code-signing keys must live on hardware (a token or a cloud HSM),
so a plain `.pfx` mostly fits test certificates or providers that issue one. For a cloud HSM,
such as Azure Trusted Signing or a CA's cloud signing service, replace the `/Sshoutabout=`
command in `build.ps1` with that provider's signing command. An EV certificate builds
SmartScreen reputation at once; a standard one builds it over downloads. **None of this has
been run yet**: no certificate exists.

## Size

Measured on version 0.1.0 (PyInstaller 6.22.3, Python 3.12; the same sizes locally and in CI):

| Part | Size on disk |
|---|---|
| NVIDIA cuBLAS + cuBLASLt (`_internal\nvidia\cublas`) | 736 MB (cuBLASLt alone is 638 MB) |
| NVIDIA NVRTC (`_internal\nvidia\cuda_nvrtc`) | 93 MB |
| FFmpeg via PyAV (`av.libs`, `av`) | **not shipped since 2026-10-10** (was 66 MB, 84 files) |
| CTranslate2 (incl. Intel OpenMP) | 59 MB |
| onnxruntime (Silero VAD) | 36 MB |
| numpy + OpenBLAS | 26 MB |
| cryptography, hf_xet, tokenizers, pydantic-core | 31 MB |
| Python, Tcl/Tk, the rest; `ShoutAbout.exe` (10 MB) | about 40 MB |
| **Installed total** | **1,020 MB** (1,069,862,682 bytes, 1,314 files; about 192 MB without the NVIDIA DLLs). With FFmpeg it was 1,086 MB (1,138,501,083 bytes, 1,398 files), measured locally on the same day. |
| **`ShoutAbout-Setup-0.1.0.exe`** (LZMA2, solid) | **524 MB** download (CI build, still with FFmpeg; not rebuilt since, because Inno Setup isn't installed locally and CI is paused) |

cuBLAS is most of it. CTranslate2 needs it only on NVIDIA GPUs, and it holds kernels for every
GPU generation. On an RTX 4080 (Ada), float16 transcription didn't load NVRTC at all, but
cuBLASLt loads it on demand for some kernels on other GPUs, and NVIDIA lists it as a cuBLAS
dependency, so it stays. A smaller installer for PCs without NVIDIA GPUs would download the
NVIDIA DLLs on first run only when such a GPU is present. That's an open product decision.

## Licenses of what it ships

The installer **redistributes** third-party binaries, so the flags in
[licenses.md](licenses.md) now apply:

- **Flag 1 (was the release blocker): PyAV's FFmpeg is no longer shipped.** Its build links
  x264 and x265 (GPL-2.0-or-later), and the project rule is no GPL/LGPL in anything shipped.
  The app now decodes audio with numpy (WAV) and Windows Media Foundation, which is part of
  Windows and not redistributed ([architecture.md](architecture.md#audio-decoding)); the
  gates above keep every PyAV/FFmpeg file out.
- **Flag 2:** `cudnn64_9.dll` is **not** shipped. Intel OpenMP (`libiomp5md.dll`) is shipped
  under Intel's redistributable-runtime terms.
- **Flag 3:** cuBLAS, cuBLASLt and NVRTC are on the NVIDIA CUDA EULA's Attachment A
  (redistributable), and the EULA text is in `THIRD-PARTY-NOTICES.txt`. `nvblas64_12.dll` isn't
  shipped.
- **Flags 4-7** (MPL-2.0 files in tqdm, certifi and onnxruntime; numpy's bundled runtime
  notices): the notices file carries their license texts. The files are shipped unmodified.

PyInstaller (GPL-2.0 with a bootloader exception) and Inno Setup (its own permissive license)
are build tools only.

## Notes for developers

- **Frozen helpers** (`shoutabout.frozen`): `is_frozen()`, `bundle_dir()`, `app_dir()`,
  `app_icon_path()` (the bundled `ShoutAbout.ico`, for Tk windows' `iconbitmap`), and
  `app_command()`. `app_command()` returns the command that starts ShoutAbout: the installed
  `ShoutAbout.exe`, or `pythonw -m shoutabout run` from source.
- **Start at sign-in contract:** value `ShoutAbout` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, data = `"<path of ShoutAbout.exe>" run`
  (the same string the settings window's start-with-Windows switch writes, so it shows as
  on). The installer writes it once; the app's settings own it afterwards; the uninstaller
  deletes it.
- **Taskbar identity:** the Start menu shortcut has AppUserModelID `ShoutAbout.App`
  (`frozen.APP_USER_MODEL_ID`). `ShoutAbout.exe` sets the same ID at start, so its windows
  group under the shortcut.
- **Logs:** the frozen app logs to `%APPDATA%\shoutabout\logs\hub.log` like the source
  version. Stray library output goes to `stdout.log` next to it, because a windowed process
  has no console.
- **Single instance:** the frozen app and a source-run hub share the `Local\ShoutAboutHub`
  mutex, so only one hub runs per Windows session.
- **doctor in the installed app** checks the installer's **ShoutAbout** program rule and
  block rules on `ShoutAbout.exe`, instead of `setup.ps1`'s port and Python rules.

## What's verified

Verified (October 2026, version 0.1.0):

- **On the build PC** (RTX 4080 SUPER, Windows 11), with a scratch `SHOUTABOUT_HOME` and
  without installing anything:
  - the onedir build, and its `bundle_rules.py` check;
  - `ShoutAbout.exe --self-test`: all checks pass, including `downloads`; `hardware` is
    skipped because that module isn't merged yet;
  - `--version`, `info --json` and an unknown command through the CLI pass-through;
  - `ShoutAbout.exe transcribe` of a 17.7 s TTS clip on the GPU (float16): 234 ms of speech
    to text, from the bundled cuBLAS;
  - a build with `sounddevice` and `tomlkit` added as dependencies (as the other v2 branches
    do): the bundle holds `sounddevice`, `_sounddevice`, `_sounddevice_data` and all of
    `tomlkit`, plus their metadata, and of PortAudio only `libportaudio64bit.dll` (no ASIO,
    32-bit, ARM64 or macOS builds). Its self-test wasn't run on this PC, because importing
    sounddevice initialises the audio system.
  - **without FFmpeg** (2026-10-10, branch `v2-nofmpeg`): the onedir build (the
    `bundle_rules.py` check passed, and no `av`, `av.libs`, `avcodec*`, `avformat*`, `x264`
    or `x265` file is in it); `ShoutAbout.exe --self-test`: all 12 checks pass, `audio`
    decodes the bundled AAC tone with Media Foundation and `no-ffmpeg` passes;
    `ShoutAbout.exe transcribe --no-cleanup` of the 29 AAC test clips on the GPU: the same
    29 transcripts as the same command from source, and 28 of 29 the same as with the old
    PyAV decoder (the other differs in punctuation only).
- **In CI** (GitHub Actions run 38065282556, Windows Server 2025, Inno Setup 6.7.1): the
  build, and 58 of 58 smoke-test checks: silent install, the self-test, the CLI, the installed
  hub transcribing a spoken sentence on the CPU (`tiny.en`), the silent upgrade with the app
  running (stopped, updated, restarted), the uninstall keeping user data, and the uninstall
  with `/REMOVEUSERDATA`. The interactive wizard pages (Welcome, start-at-sign-in,
  installing) rendered with the brand images.

Not verified yet:

- the changes made after that run, because GitHub Actions stopped starting jobs ("recent
  account payments have failed or your spending limit needs to be increased"): the
  `*asio*` gates, starting the app from the Run value in the smoke test, the `downloads`
  check in CI, the Install button caption and the tasks-page heading, and the screenshots
  of the Finished page and the uninstall dialog, and everything about removing FFmpeg (the
  installer's FFmpeg excludes and the smoke test's new FFmpeg, `audio` and `no-ffmpeg`
  assertions; the setup .exe size without FFmpeg). They pass unit tests and the local build;
  re-run the workflow once Actions works again;
- decoding on a Windows "N" edition (with and without the Media Feature Pack), and on
  Windows 10; only Windows 11 Home was tried;
- SmartScreen behaviour with a real download;
- installing on a real home PC, including the first-run setup window and LAN access from a
  phone through the firewall rule;
- a standard user approving UAC with someone else's administrator account;
- code signing;
- the tray icon on CI, where there may be no visible desktop. The hub runs either way.
