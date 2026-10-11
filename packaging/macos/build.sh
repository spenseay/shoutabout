#!/bin/bash
# Build ShoutAbout.app on this Mac, install it in Applications and open it.
#
#   bash packaging/macos/build.sh            # build, install, open
#   bash packaging/macos/build.sh --no-open  # build and install only
#   bash packaging/macos/build.sh --no-install
#   bash packaging/macos/build.sh --ci       # build server: build and check only (below)
#
# Needs nothing installed beforehand: it fetches uv (Astral's Python manager, MIT/Apache) into
# ~/.local/bin if it isn't there, then Python 3.12 and every library into this folder's .venv.
# Apple Silicon (M1 or newer) and macOS 14 Sonoma or newer. Takes about 5-10 minutes the
# first time (downloads), a couple of minutes after that.
#
# Everything it prints also goes to packaging/dist-mac/build.log (send that file if it fails).
#
# --ci (codemagic.yaml, mac-release): never asks anything, never installs or opens the app,
# leaves packaging/dist-mac/ShoutAbout.app exactly as PyInstaller signed it (ad hoc) and
# skips the self-test and the zip/disk image: packaging/macos/release.sh signs it with the
# Developer ID certificate, runs the self-test on the signed app, notarizes and packs it.
set -euo pipefail

UV_VERSION="0.8.24"   # the uv that made uv.lock; newer ones read it too
PYTHON_VERSION="3.12"
APP_NAME="ShoutAbout"

OPEN_APP=1
INSTALL_APP=1
CI_MODE=0
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_APP=0 ;;
    --no-install) INSTALL_APP=0; OPEN_APP=0 ;;
    --ci) CI_MODE=1; INSTALL_APP=0; OPEN_APP=0 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Run natively on Apple Silicon even from a Terminal that runs under Rosetta.
if [ "$(uname -m)" != "arm64" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ] \
   && [ -z "${SHOUTABOUT_ARM64_REEXEC:-}" ]; then
  export SHOUTABOUT_ARM64_REEXEC=1
  exec arch -arm64 /bin/bash "$0" "$@"
fi

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
DIST="$ROOT/packaging/dist-mac"
WORK="$ROOT/packaging/build/pyinstaller-mac"
ASSETS="$ROOT/packaging/build/assets"
mkdir -p "$DIST" "$ASSETS"
LOG="$DIST/build.log"
exec > >(tee "$LOG") 2>&1

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\nThe full log is %s\n' "$*" "$LOG"; exit 1; }

say "Checking this Mac"
[ "$(uname -s)" = "Darwin" ] || fail "This script builds the Mac app; run it on a Mac."
[ "$(uname -m)" = "arm64" ] || fail "ShoutAbout for Mac needs Apple Silicon (M1 or newer)."
MACOS="$(sw_vers -productVersion)"
MAJOR="${MACOS%%.*}"
[ "$MAJOR" -ge 14 ] || fail "ShoutAbout needs macOS 14 Sonoma or newer (this Mac has $MACOS)."
echo "macOS $MACOS on $(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo 'Apple Silicon')"
# (No git here: on a Mac without the developer tools, /usr/bin/git pops up an installer.)
echo "Source: $ROOT"

say "Getting uv"
UV=""
if command -v uv >/dev/null 2>&1; then UV="$(command -v uv)"; fi
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
ver_num() { echo "$1" | awk -F. '{printf "%d\n", $1 * 1000000 + $2 * 1000 + $3}'; }
if [ -n "$UV" ]; then
  HAVE="$("$UV" --version | awk '{print $2}')"
  # Use an existing uv only if it's at least 0.8 (older ones may not read uv.lock).
  if [ "$(ver_num "$HAVE")" -lt "$(ver_num 0.8.0)" ]; then UV=""; fi
fi
if [ -z "$UV" ]; then
  echo "Installing uv $UV_VERSION into ~/.local/bin (official installer from astral.sh)"
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
    | env UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
  UV="$HOME/.local/bin/uv"
fi
[ -x "$UV" ] || fail "uv couldn't be installed (no internet, or a proxy blocks astral.sh?)."
"$UV" --version

say "Getting Python $PYTHON_VERSION"
# Always uv's own Python build (it includes Tk), never a Homebrew or system Python.
export UV_PYTHON_PREFERENCE=only-managed
"$UV" python install "$PYTHON_VERSION"

say "Installing ShoutAbout's libraries into .venv"
"$UV" sync --locked --python "$PYTHON_VERSION" --no-dev --group packaging
PY="$ROOT/.venv/bin/python"
"$PY" - <<'EOF'
import platform, sys, tkinter
print("Python", sys.version.split()[0], platform.machine())
print("Tcl/Tk", tkinter.Tcl().eval("info patchlevel"))
import objc, AppKit, Quartz, ApplicationServices, AVFoundation, ctranslate2, sounddevice
print("PyObjC", objc.__version__, "| CTranslate2", ctranslate2.__version__)
EOF

say "Drawing the app icon"
"$PY" packaging/macos/make_icns.py --out "$ASSETS/ShoutAbout.icns"

say "Building $APP_NAME.app (PyInstaller)"
rm -rf "$DIST/$APP_NAME.app" "$DIST/$APP_NAME"
SHOUTABOUT_ASSETS="$ASSETS" "$ROOT/.venv/bin/pyinstaller" packaging/macos/ShoutAbout-mac.spec \
  --noconfirm --clean --distpath "$DIST" --workpath "$WORK"
APP="$DIST/$APP_NAME.app"
[ -x "$APP/Contents/MacOS/$APP_NAME" ] || fail "PyInstaller didn't produce $APP."
"$PY" packaging/macos/mac_rules.py check "$APP" || fail "a file that must not ship is in the app."

if [ "$CI_MODE" = 1 ]; then
  say "Built $APP ($("$PY" -c 'import shoutabout; print(shoutabout.__version__)'))"
  echo "Next: bash packaging/macos/release.sh (Developer ID signing, self-test, notarization)"
  exit 0
fi

say "Signing (ad hoc: this Mac only)"
xattr -cr "$APP"   # codesign refuses files with Finder info or quarantine attributes
# PyInstaller already signs the bundle ad hoc; signing it again as a whole makes sure every
# nested library is covered. If that fails, PyInstaller's signature stays.
if ! codesign --force --deep --sign - "$APP"; then   # identifier = CFBundleIdentifier
  echo "WARNING: re-signing failed; keeping PyInstaller's own ad-hoc signature."
fi
if codesign --verify --deep --strict "$APP"; then
  echo "signature ok"
else
  echo "WARNING: macOS reports a signature problem; the app may not open (see the log)."
fi

say "Self-test of the built app"
SELFTEST_OK=1
"$APP/Contents/MacOS/$APP_NAME" --self-test --text --out "$DIST/selftest.json" || SELFTEST_OK=0
if [ "$SELFTEST_OK" = 0 ]; then
  echo "WARNING: the self-test reported a problem (see above). Installing anyway."
fi

say "Packing a zip and a disk image (for other Macs later; they need notarizing first)"
VERSION="$("$PY" -c 'import shoutabout; print(shoutabout.__version__)')"
rm -f "$DIST/$APP_NAME-$VERSION-mac.zip" "$DIST/$APP_NAME-$VERSION.dmg"
ditto -c -k --keepParent "$APP" "$DIST/$APP_NAME-$VERSION-mac.zip"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$APP" -ov -format UDZO \
  "$DIST/$APP_NAME-$VERSION.dmg" || echo "(no disk image: hdiutil failed; the zip is there)"

if [ "$INSTALL_APP" = 1 ]; then
  say "Installing"
  DEST="/Applications"
  if [ ! -w "$DEST" ]; then DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
  if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
    echo "Quitting the running ShoutAbout"
    pkill -TERM -x "$APP_NAME" || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -x "$APP_NAME" >/dev/null || break; sleep 0.5; done
    pkill -KILL -x "$APP_NAME" 2>/dev/null || true
  fi
  rm -rf "$DEST/$APP_NAME.app"
  ditto "$APP" "$DEST/$APP_NAME.app"
  echo "Installed $DEST/$APP_NAME.app"
  if [ "$OPEN_APP" = 1 ]; then
    say "Opening ShoutAbout"
    open "$DEST/$APP_NAME.app"
    cat <<EOF

ShoutAbout is starting: look for the yellow mouth in the menu bar (top right).
macOS will ask for the microphone and for Accessibility: allow both.
Logs: ~/Library/Logs/ShoutAbout   Build log: $LOG
EOF
  fi
fi
say "Done"
