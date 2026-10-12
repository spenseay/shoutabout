# Third-party licenses

This inventory was generated on 2026-10-09 from the project venv (`.venv`, uv 0.13.0, Python
3.12.5). It covers every installed distribution, the native binaries that the wheels bundle, the
models, and the external tools. The venv matches `uv.lock` exactly: 47 packages, including
`shoutabout` itself and the `cuda` extra. On 2026-10-10 `sounddevice` (MIT) was added for "Talk
to this PC" (48 packages; its other dependency, `cffi`, was already here): see flag 9.

How the licenses were determined:

- `License-Expression` metadata, or `License` metadata when there is no expression.
- License classifiers.
- For packages whose metadata only says "BSD License" or similar, the license file shipped in the
  `*.dist-info` was read by hand.
- Bundled DLLs were listed from the installed wheels.
- The FFmpeg build flags were read from PyAV at runtime.

ShoutAbout's own code: the MIT License (see [LICENSE](../LICENSE)). The ShoutAbout name and the
mouth logo aren't covered by it.

> **Framing.** The repo vendors **no** third-party code, binaries or models. Everything below is
> downloaded by the user at install time from the upstream source (PyPI, Hugging Face, the Ollama
> registry, the vendors' installers). Running it locally triggers essentially no obligations.
> The flags below matter only if ShoutAbout ever ships a **frozen or packaged build** (an
> installer, a PyInstaller bundle, a zip with a venv) that redistributes these binaries.
>
> **Update (v2): that build now exists.** The Windows installer
> ([installer.md](installer.md#licenses-of-what-it-ships)) bundles the runtime dependencies
> and the cuBLAS/NVRTC DLLs. It leaves out `cudnn64_9.dll` and `nvblas64_12.dll`, and
> generates `THIRD-PARTY-NOTICES.txt` from the bundled distributions' license files.
>
> **Update (2026-10-10): flag 1 is resolved by removal.** The installer no longer contains
> PyAV or any FFmpeg file. Audio is decoded with numpy (WAV) and Windows Media Foundation (part
> of Windows, not redistributed), see [architecture.md](architecture.md#audio-decoding). PyAV
> is no longer a runtime dependency: it is the optional `pyav` extra, and is still installed in
> source checkouts only because faster-whisper depends on it. Four gates keep it out of the
> build: the PyInstaller spec excludes `av` and drops `av/`, `av.libs/`, `av-*.dist-info/` and
> every `avcodec*`/`avformat*`/`avutil*`/`swresample*`/`*x264*`/`*x265*` file
> (`packaging/bundle_rules.py`); `build.ps1` fails if one is in the output; the installer
> script excludes them; and the frozen self-test (`no-ffmpeg`) and the CI smoke test fail if
> any is installed. `packaging/notices.py` refuses to write notices that list PyAV.

## Flags (read these first)

| # | Component | Issue | Matters when |
|---|---|---|---|
| 1 | **`av` 18.1.0 (PyAV) wheel** | The Python code is BSD-3-Clause, but the wheel bundles **FFmpeg 8.1.2** plus codec libraries. FFmpeg reports "LGPL version 3 or later" because it is built with `--enable-version3`. It is also linked with **libx264 and libx265 (GPL-2.0-or-later)**, and pyav-ffmpeg patches FFmpeg's `configure` so that this doesn't force a GPL build. The maintainer describes the wheel as "LGPLv2.1+ with commercial exceptions" **or** "GPLv2+" (PyAV #2270). Treat a redistributed wheel as **GPL-2.0-or-later**. The wheel's `dist-info` contains only PyAV's BSD text and no FFmpeg or codec notices. | **Resolved (2026-10-10): not shipped.** The Windows app decodes AAC with Windows Media Foundation and never bundles PyAV (see the update above). It mattered when shipping a frozen binary or installer that contains the PyAV wheel; running it locally from PyPI (source checkouts) is fine. |
| 2 | **`ctranslate2` 4.8.2 wheel** | The package is MIT, but the wheel also contains **`cudnn64_9.dll`** (NVIDIA cuDNN, proprietary; ShoutAbout doesn't use it because the wheel is built with cuDNN off) and **`libiomp5md.dll`** (Intel OpenMP runtime, under Intel's redistributable-runtime terms). The wheel ships no license notice for either DLL. | Redistribution. Check the NVIDIA cuDNN SLA and Intel's terms, or strip `cudnn64_9.dll`. |
| 3 | **`nvidia-cublas-cu12`, `nvidia-cuda-nvrtc-cu12`** (optional `cuda` extra) | `LicenseRef-NVIDIA-Proprietary` (NVIDIA CUDA EULA). Redistribution is allowed only for the files listed in **Attachment A**: `cublas.dll`, `cublasLt.dll`, `nvblas.dll`, `nvrtc.dll` and `nvrtc-builtins.dll`, including their versioned names. The conditions are that the application has "material additional functionality", that the DLLs are accessed only by your application, and that your terms are consistent with the EULA. The SDK is licensed for systems with NVIDIA GPUs. | Redistribution. Today the user installs these from PyPI (`uv sync --extra cuda`), and they are never vendored. |
| 4 | **`tqdm`** 4.70.1 | `MPL-2.0 AND MIT` (file-level copyleft). | Only modifications to tqdm's own files must be published. Redistributing it unmodified needs the license notice and a pointer to the source. |
| 5 | **`certifi`** 2026.7.22 | `MPL-2.0` (Mozilla's CA bundle). | Same as tqdm. |
| 6 | **`onnxruntime`** 1.30.0 | The package is MIT, but `ThirdPartyNotices.txt` lists **Eigen (MPL-2.0)** compiled into `onnxruntime.dll`. | Same as tqdm. Keep `ThirdPartyNotices.txt` when redistributing. |
| 7 | **`numpy`** 2.5.3 wheel | The bundled OpenBLAS DLL contains the **GCC runtime (GPL-3.0-or-later WITH GCC-exception-3.1)**. The exception allows use by non-GPL programs. The wheel also bundles `msvcp140.dll` (Microsoft VC++ runtime). | Keep numpy's `LICENSE.txt`, which already includes these notices, when redistributing. |
| 8 | **Cleanup LLM weights** | These are downloaded by the user through Ollama. The default and all alternates are Apache-2.0. Models under custom licenses are excluded (see [Models](#models)). | Bundling model weights with a product. |
| 9 | **`sounddevice` 0.5.6 wheel** (added 2026-10-10 for "Talk to this PC") | sounddevice is MIT and its PortAudio DLL (`libportaudio64bit.dll`) is MIT. The wheel *also* ships `libportaudio*-asio.dll` builds made with Steinberg's **ASIO SDK**, which is not under a permissive license. sounddevice only loads the ASIO build when the `SD_ENABLE_ASIO` environment variable is set; ShoutAbout never sets it. | A packaged build must **leave out the `*-asio.dll` files** (keep `libportaudio64bit.dll`, `libportaudioarm64.dll` and sounddevice's / PortAudio's license texts). Running from PyPI is fine. |

There are no `UNKNOWN` licenses and no GPL, LGPL, AGPL or EPL **Python** packages. **pystray
(LGPL-3.0) was deliberately avoided**: the tray icon and balloon toasts are implemented with
ctypes (`tray.py`), and the icons are generated in pure Python with no Pillow. (infi.systray,
which is BSD, was the other option considered.)

**Brand.**
- The brand artwork (`brand/`, `src/shoutabout/brand.py`) is the owner's own design, rendered in
  pure Python.
- Its typefaces (Anton, Archivo and JetBrains Mono) are licensed under SIL OFL 1.1.
- They are not bundled on the PC side. The iOS app bundles Anton and Archivo with their OFL.txt.
  OFL fonts may be embedded in apps; the font files themselves can't be sold on their own.

## Runtime dependencies: direct (from `pyproject.toml`)

| Package | Version | License | Notes |
|---|---|---|---|
| fastapi | 0.141.1 | MIT | |
| uvicorn | 0.53.0 | BSD-3-Clause | |
| python-multipart | 0.0.32 | Apache-2.0 | |
| faster-whisper | 1.2.1 | MIT | Bundles the Silero VAD v6 model (MIT). Requires PyAV, which it imports for its own file decoder; ShoutAbout never calls it, and the Windows app runs it with a placeholder `av` module (`avshim.py`) |
| av (PyAV) | 18.1.0 | BSD-3-Clause (code); **bundled FFmpeg: see flag 1** | Since 2026-10-10 only the optional `pyav` extra (pinned `<19`), installed in source checkouts because faster-whisper requires it. **Never in the Windows app.** |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | **Bundled DLLs: see flag 7** |
| httpx | 0.28.1 | BSD-3-Clause | |
| PyJWT (`[crypto]`) | 2.15.0 | MIT | The `crypto` extra pulls in cryptography |
| segno | 1.6.6 | BSD-3-Clause | Metadata only says "BSD License"; the `LICENSE` file is 3-clause (verified) |
| psutil | 7.2.2 | BSD-3-Clause | |
| sounddevice | 0.5.6 | MIT | Microphone for "Talk to this PC". Imported only when recording starts. **Bundled PortAudio DLLs: see flag 9** |
| tomlkit | 0.15.1 | MIT | Comment-preserving config edits (Settings); `LICENSE` file is MIT (verified) |
| pyobjc-core, pyobjc-framework-Cocoa, -Quartz, -ApplicationServices, -AVFoundation (and their dependencies -CoreAudio, -CoreMedia, -CoreText) | 12.2.2 | MIT | **macOS only** (`sys_platform == 'darwin'` markers; never installed on Windows/Linux). The Mac app's menu bar, overlay, hotkey, pasting and permission checks ([macos.md](macos.md)). Pure bindings to Apple's system frameworks, which are part of macOS and not redistributed. |

## Runtime dependencies: transitive

| Package | Version | License | Required by | Notes |
|---|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | fastapi | |
| annotated-types | 0.8.0 | MIT | pydantic | |
| anyio | 4.15.1 | MIT | httpx, starlette | |
| certifi | 2026.7.22 | **MPL-2.0** | httpx, httpcore | Flag 5 |
| cffi | 2.1.1 | MIT-0 | cryptography | `LICENSE` file: "MIT No Attribution" (verified) |
| click | 8.5.0 | BSD-3-Clause | uvicorn, huggingface-hub | |
| colorama | 0.4.6 | BSD-3-Clause | tqdm, pytest (Windows only) | Metadata only says "BSD License"; the `LICENSE.txt` is 3-clause (verified) |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | PyJWT[crypto] | The wheel statically links OpenSSL 3 (Apache-2.0) |
| ctranslate2 | 4.8.2 | MIT | faster-whisper | **Bundled DLLs: flag 2** |
| filelock | 4.0.3 | MIT | huggingface-hub | |
| flatbuffers | 25.12.19 | Apache-2.0 | onnxruntime | Metadata says "Apache 2.0" |
| fsspec | 2026.9.0 | BSD-3-Clause | huggingface-hub | |
| h11 | 0.16.0 | MIT | uvicorn, httpcore | |
| hf-xet | 1.6.0 | Apache-2.0 | huggingface-hub | Rust binary |
| httpcore | 1.0.9 | BSD-3-Clause | httpx | |
| huggingface_hub | 1.33.0 | Apache-2.0 | faster-whisper, tokenizers | |
| idna | 3.20 | BSD-3-Clause | anyio, httpx | |
| onnxruntime | 1.30.0 | MIT | faster-whisper (Silero VAD) | **Eigen MPL-2.0 inside: flag 6** |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | huggingface-hub, onnxruntime | |
| protobuf | 7.36.2 | BSD-3-Clause | onnxruntime | Metadata says "3-Clause BSD License" |
| pycparser | 3.0 | BSD-3-Clause | cffi | |
| pydantic | 2.13.5 | MIT | fastapi | Also imported directly by `config.py` |
| pydantic_core | 2.46.5 | MIT | pydantic | Rust binary |
| PyYAML | 6.0.3 | MIT | ctranslate2, huggingface-hub | |
| starlette | 1.7.0 | BSD-3-Clause | fastapi | |
| tokenizers | 0.23.2 | Apache-2.0 | faster-whisper | Classifier only. The bundled CycloneDX SBOM lists only permissive Rust crates: MIT, Apache-2.0, Unlicense, BSD-2-Clause, Zlib, BSL-1.0 and Unicode-3.0 |
| tqdm | 4.70.1 | **MPL-2.0 AND MIT** | faster-whisper, huggingface-hub | Flag 4 |
| typing_extensions | 4.16.0 | PSF-2.0 | many | |
| typing-inspection | 0.4.4 | MIT | pydantic, fastapi | |

## Development dependencies (`[dependency-groups] dev`)

None of these are needed at runtime.

| Package | Version | License | Notes |
|---|---|---|---|
| pytest | 9.1.1 | MIT | direct |
| ruff | 0.16.9 | MIT | direct (Rust binary) |
| iniconfig | 2.3.0 | MIT | pytest |
| pluggy | 1.6.0 | MIT | pytest |
| Pygments | 2.21.0 | BSD-2-Clause | pytest |

`packaging` and `colorama` are shared with the runtime set, so they are listed above.

## Optional CUDA wheels (`[project.optional-dependencies] cuda`)

| Package | Version | License | Files | Notes |
|---|---|---|---|---|
| nvidia-cublas-cu12 | 12.9.2.10 | `LicenseRef-NVIDIA-Proprietary` (CUDA EULA) | `nvidia\cublas\bin\cublas64_12.dll`, `cublasLt64_12.dll`, `nvblas64_12.dll` | **Flag 3.** The user installs it from PyPI; it is not vendored. |
| nvidia-cuda-nvrtc-cu12 | 12.9.86 | `LicenseRef-NVIDIA-Proprietary` (CUDA EULA) | `nvidia\cuda_nvrtc\bin\nvrtc64_120_0.dll`, `nvrtc-builtins64_129.dll` | Pulled in by cuBLAS. **Flag 3.** |

`nvidia-cudnn-cu12` is **not** installed and not needed, because CTranslate2 >= 4.6.3 Windows
wheels are built without cuDNN.

## Native libraries bundled inside wheels

The license column for third-party DLLs gives the upstream project license. Except where noted,
the wheels ship no notices for them.

**`av` 18.1.0 (`site-packages\av.libs\`)**, in source checkouts only (none of these files is in
the Windows app since 2026-10-10):

| DLL | Upstream | License |
|---|---|---|
| `avcodec-62`, `avformat-62`, `avutil-60`, `avfilter-11`, `avdevice-62`, `swresample-6`, `swscale-9` | FFmpeg 8.1.2 | LGPL-3.0-or-later as configured. **The combination with x264/x265 is GPL-2.0-or-later.** |
| `libx264-165` | x264 | **GPL-2.0-or-later** |
| `libx265` | x265 | **GPL-2.0-or-later** |
| `libmp3lame-0` | LAME | LGPL-2.0-or-later |
| `libiconv-2` | GNU libiconv | LGPL-2.1-or-later |
| `libgcc_s_seh-1`, `libstdc++-6` | GCC runtime | GPL-3.0-or-later WITH GCC-exception-3.1 |
| `libwinpthread-1` | mingw-w64 winpthreads | MIT-style |
| `libopencore-amrnb-0`, `libopencore-amrwb-0` | opencore-amr | Apache-2.0 |
| `libopus-0` | Opus | BSD-3-Clause |
| `libvpx-1` | libvpx | BSD-3-Clause |
| `libdav1d` | dav1d | BSD-2-Clause |
| `libSvtAv1Enc` | SVT-AV1 | BSD-3-Clause-Clear (+ AOMedia patent license) |
| `libwebp`, `libwebpmux`, `libsharpyuv` | libwebp | BSD-3-Clause |
| `libvpl` | Intel VPL dispatcher | MIT |
| `zlib1` | zlib | Zlib |

FFmpeg was configured with these flags (relevant excerpt): `--enable-version3 --enable-libx264
--enable-libx265 --enable-libmp3lame --enable-libopus --enable-libvpx --enable-libdav1d
--enable-libsvtav1 --enable-libwebp --enable-libopencore-amrnb --enable-libopencore-amrwb
--enable-libvpl --enable-nvenc --enable-nvdec --enable-amf --enable-mediafoundation
--disable-programs`. NVENC, NVDEC and AMF are loaded from the GPU driver at runtime; they are
headers only and not bundled.

Codec patent licensing (H.264, HEVC, AAC) is a separate question from copyright licensing and is
out of scope here. ShoutAbout only **decodes** AAC.

**Other wheels:**

| Wheel | Bundled binary | License |
|---|---|---|
| ctranslate2 | `ctranslate2.dll` | MIT |
| ctranslate2 | `cudnn64_9.dll` | NVIDIA cuDNN SLA (proprietary) - **flag 2** |
| ctranslate2 | `libiomp5md.dll` | Intel OpenMP runtime, under Intel's redistributable terms - **flag 2** |
| numpy | `numpy.libs\libscipy_openblas64_*.dll` | OpenBLAS BSD-3-Clause, LAPACK BSD-3-Clause-Open-MPI, GCC runtime GPL-3.0-or-later WITH GCC-exception-3.1 (all in numpy's `LICENSE.txt`) |
| numpy | `numpy.libs\msvcp140-*.dll` | Microsoft Visual C++ runtime (redistributable) |
| onnxruntime | `onnxruntime.dll`, `onnxruntime_providers_shared.dll` | MIT; third-party notices (incl. Eigen MPL-2.0) are in `onnxruntime\ThirdPartyNotices.txt` |
| cryptography | `_rust.pyd` | Apache-2.0 OR BSD-3-Clause; OpenSSL 3 statically linked (Apache-2.0) |
| sounddevice | `_sounddevice_data\portaudio-binaries\libportaudio{64bit,32bit,arm64}.dll` | PortAudio, MIT |
| sounddevice | `_sounddevice_data\portaudio-binaries\libportaudio*-asio.dll` | PortAudio (MIT) built with the Steinberg ASIO SDK (not permissive) - **flag 9: never loaded by ShoutAbout, leave out of packaged builds** |

## Models

### Speech-to-text and VAD

| Model | License | Source | How it arrives |
|---|---|---|---|
| Whisper large-v3-turbo (OpenAI; 809M parameters) | MIT | `openai/whisper-large-v3-turbo` | - |
| CTranslate2 fp16 conversion (the model actually used) | MIT | `mobiuslabsgmbh/faster-whisper-large-v3-turbo`, which now redirects to `dropbox-dash/faster-whisper-large-v3-turbo` | `download-model` or the first run fetches it (`model.bin` is about 1.62 GB) into `%USERPROFILE%\.cache\huggingface\hub`. Not vendored. |
| Silero VAD v6 (`faster_whisper\assets\silero_vad_v6.onnx`, 1.2 MB) | MIT | snakers4/silero-vad | Bundled inside the faster-whisper wheel |
| Whisper small.en and base.en, CTranslate2 conversions (the "Balanced" CPU / small-GPU and "Light" tiers, see [models.md](models.md)) | MIT | `Systran/faster-whisper-small.en` (486 MB), `Systran/faster-whisper-base.en` (148 MB) | Downloaded on first run, only on PCs where the automatic choice picks them. Not vendored. |

### Cleanup LLMs (Ollama)

The rule is permissive licenses only (Apache-2.0 or MIT), so that both open-source and commercial
routes stay open.

| Ollama tag | Role | License | Notes |
|---|---|---|---|
| `qwen3:4b-instruct-2507-q4_K_M` | **Default** | Apache-2.0 | Non-thinking only, about 2.5 GB, IFEval 83.4. Pin the full tag: bare `qwen3:4b` may be the thinking variant. |
| `granite4:micro` (= `granite4:3b`) | Alternate | Apache-2.0 | Dense 3B, about 2.1 GB, no thinking mode |
| `granite4.2:3b` | Alternate | Apache-2.0 | Thinking is on by default, so always send `think: false` |
| `ministral-3:3b` | Alternate | Apache-2.0 | About 3.0 GB, includes vision |
| `qwen3.5:4b` | Alternate | Apache-2.0 | Thinking is on by default (`think: false`), about 3.3 GB, includes vision |
| `phi4-mini` | Considered | MIT | Not benchmarked |
| `olmo-3:7b-instruct` | Considered | Apache-2.0 | 7B, slower |
| `gemma4:e2b` / `gemma4:e4b` | Considered | **Apache-2.0** | Gemma 4 moved from the old Gemma Terms of Use to Apache-2.0. Earlier Gemma generations keep the Gemma Terms. Large downloads (4.3-6.6 GB) because they bundle audio and vision encoders. |
| LFM2 (Liquid AI) | **Excluded** | LFM Open License v1.0 | Bars commercial use for organisations with $10M or more in annual revenue |
| Llama (Meta) | **Excluded** | Llama Community License | Custom terms: acceptable-use policy, a monthly-active-user threshold, and attribution and naming requirements. Not permissive. |
| `qwen2.5-coder:3b` | Avoid | Qwen Research License (unverified) | The other Qwen2.5-Coder sizes are Apache-2.0 |

Quantized GGUF builds in the Ollama library carry the upstream model's license. The weights are
pulled by the user (`ollama pull ...`) and never vendored.

## External tools

| Tool | License | Role |
|---|---|---|
| Python 3.12 (python.org) | PSF-2.0 | Interpreter. The base `python.exe`/`pythonw.exe` that the firewall rules point to. |
| uv | MIT OR Apache-2.0 | Environment and lock management |
| Ollama (0.34.4 on the target PC) | MIT | Local LLM server for cleanup |
| cloudflared (2026.9.1 on the target PC) | Apache-2.0 | Cloudflare Tunnel connector (Windows service) |
| winget (Windows Package Manager client) | MIT | Optional installer for the tools above. Each package it installs has its own license. |
| NVIDIA display driver | NVIDIA proprietary | Installed separately by the user |
| Windows PowerShell 5.1, System.Speech | Part of Windows | Setup scripts and test-audio generation |
| Windows Media Foundation (`mfplat.dll`, `mfreadwrite.dll` and the AAC/MP3/FLAC decoders) | Part of Windows | Decodes phone recordings, called through ctypes. Not redistributed. Windows "N" editions get it with Microsoft's Media Feature Pack. |
| macOS `afconvert`, `afinfo`, `dns-sd`, `codesign`, `ditto`, `hdiutil` | Part of macOS | The Mac app decodes phone recordings with Core Audio through `afconvert` (no FFmpeg in the Mac app either) and announces itself with `dns-sd`; the build script signs and packs with the others. Not redistributed. |
| PyInstaller (Mac build) | GPL-2.0 with the bootloader exception | Build tool only, as on Windows: `packaging/macos/build.sh` makes `ShoutAbout.app` with it. |

Services used (governed by terms of service, not software licenses): Cloudflare (DNS, Tunnel,
Access), Hugging Face Hub (model download), the Ollama registry (model download), and PyPI.

**RoutineHub HubSign** (`https://hubsign.routinehub.services/sign`) is also used, but only at
setup time and only optionally. `make-shortcuts` sends it the iPhone shortcuts' structure (the
actions, the two URLs and the Wi-Fi name; never tokens, audio or text) to get Apple-signed
`.shortcut` files. `--no-sign` skips it, for anyone who can sign on a Mac. It's not used while
dictating, no RoutineHub code is vendored or installed, and the signed files it returns are the
user's own shortcuts.

## How to regenerate

Run these from the repo root in PowerShell.

```powershell
# 1. Every installed distribution: name, version, license expression/field, license classifiers
.\.venv\Scripts\python.exe -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version, (d.metadata.get('License-Expression') or d.metadata.get('License') or '').strip()[:60], '|', '; '.join(c.split(' :: ')[-1] for c in (d.metadata.get_all('Classifier') or []) if c.startswith('License ::'))) for d in sorted(m.distributions(), key=lambda d: d.metadata['Name'].lower())]"

# 2. License files shipped by a package whose metadata is vague (e.g. "BSD License")
Get-ChildItem .\.venv\Lib\site-packages\segno-*.dist-info -Recurse -Include LICENSE*, COPYING*

# 3. Native binaries bundled in wheels (look for new GPL/proprietary DLLs)
Get-ChildItem .\.venv\Lib\site-packages\av.libs, .\.venv\Lib\site-packages\numpy.libs | Select-Object Name
Get-ChildItem .\.venv\Lib\site-packages\ctranslate2\*.dll, .\.venv\Lib\site-packages\nvidia\*\bin\*.dll | Select-Object FullName

# 4. FFmpeg license and build flags inside the PyAV wheel
.\.venv\Scripts\python.exe -c "import av._core as c; print({k: v['license'] for k, v in c.library_meta.items()}); print(c.library_meta['libavcodec']['configuration'])"

# 5. Cross-check against the lock (should list the same packages)
uv export --frozen --no-hashes --extra cuda
```

Re-run this after any `uv lock --upgrade` or change to `exclude-newer`. Then update the tables and
look again at the flags, especially for new versions of `av` and `ctranslate2`, where bundled
binaries change between releases.
