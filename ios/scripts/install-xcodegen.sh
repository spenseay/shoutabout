#!/bin/bash
# Download the pinned XcodeGen release (checksum-verified) for a build machine:
#
#   bash ios/scripts/install-xcodegen.sh [DIR]   # default DIR: ios/.tools
#
# Prints the path of the xcodegen binary on the last line. Same version and checksum as
# scripts/run-on-iphone.sh (tests/test_codemagic.py keeps them equal); a download instead of
# `brew install` keeps the version fixed and skips Homebrew's slow self-update.
set -euo pipefail

XCODEGEN_VERSION="2.46.0"
XCODEGEN_SHA256="4d9e34b62172d645eed6457cac13fc222569974098ef4ee9c3368bedf0196806"
XCODEGEN_URL="https://github.com/yonaskolb/XcodeGen/releases/download/${XCODEGEN_VERSION}/xcodegen.zip"

IOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$IOS_DIR/.tools}"
XCODEGEN="$DEST/xcodegen/bin/xcodegen"

if [ ! -x "$XCODEGEN" ] || ! "$XCODEGEN" --version 2>/dev/null | grep -q "$XCODEGEN_VERSION"; then
  mkdir -p "$DEST"
  curl -fsSL --retry 3 -o "$DEST/xcodegen.zip" "$XCODEGEN_URL"
  echo "$XCODEGEN_SHA256  $DEST/xcodegen.zip" | shasum -a 256 -c - >/dev/null || {
    echo "ERROR: the XcodeGen download doesn't match its checksum" >&2
    exit 1
  }
  rm -rf "$DEST/xcodegen"
  unzip -q -o "$DEST/xcodegen.zip" -d "$DEST"
  rm -f "$DEST/xcodegen.zip"
  chmod +x "$XCODEGEN"
fi
"$XCODEGEN" --version >&2
echo "$XCODEGEN"
