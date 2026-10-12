#!/bin/bash
# Sign, check, notarize and pack ShoutAbout.app for other Macs (Developer ID: outside the
# Mac App Store). Runs on Codemagic after `build.sh --ci` (codemagic.yaml, mac-release),
# or on any Mac whose keychain has a "Developer ID Application" identity:
#
#   bash packaging/macos/release.sh                 # sign, self-test, notarize, staple, pack
#   bash packaging/macos/release.sh --no-notarize   # sign, self-test and pack only
#
# In:  packaging/dist-mac/ShoutAbout.app (build.sh --ci), packaging/macos/ShoutAbout.entitlements
#      MAC_SIGN_IDENTITY  SHA-1 or name of the identity (default: the keychain's first
#                         "Developer ID Application"; developer-id.sh install sets it)
#      APP_STORE_CONNECT_KEY_IDENTIFIER, APP_STORE_CONNECT_ISSUER_ID,
#      APP_STORE_CONNECT_PRIVATE_KEY  the App Store Connect API key notarytool signs in with
# Out: packaging/dist-mac/release/ShoutAbout.dmg           signed, notarized, stapled
#      packaging/dist-mac/release/ShoutAbout-mac.zip       the stapled app (ditto zip)
#      packaging/dist-mac/release/ShoutAbout-mac-logs.zip  notary logs, self-tests, signing
#                                                          report, checksums, build.log
# The file names never change; the version is inside the app (Get Info, About).
#
# Order (Apple, "Creating distribution-signed code for macOS" and "Customizing the
# notarization workflow"):
#  1. every nested Mach-O first (packaging/macos/macho.py lists them), with a secure
#     timestamp, no entitlements, no --deep; then the app with the Hardened Runtime and the
#     entitlements; verify;
#  2. the self-test of the app before and after signing: a check that passed unsigned must
#     still pass signed (that's what the Hardened Runtime and library validation could
#     break). If only PyObjC's callbacks break, the app is re-signed once with
#     com.apple.security.cs.allow-unsigned-executable-memory added (PyObjC's documented
#     fallback after allow-jit) and the build says so loudly;
#  3. notarize a zip of the app, staple the ticket to the app;
#  4. disk image of the stapled app (plus an Applications shortcut), sign it, notarize it,
#     staple it; Gatekeeper check of both.
set -euo pipefail

NOTARIZE=1
for arg in "$@"; do
  case "$arg" in
    --no-notarize) NOTARIZE=0 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
APP_NAME="ShoutAbout"
DIST="$ROOT/packaging/dist-mac"
APP="$DIST/$APP_NAME.app"
OUT="$DIST/release"
LOGS="$DIST/release-logs"
ENTITLEMENTS="$ROOT/packaging/macos/ShoutAbout.entitlements"
MACHO="$ROOT/packaging/macos/macho.py"
# Checks the signature can break (imports = library validation; vad/ctranslate2 = the
# native libraries; objc-callbacks = executable memory for PyObjC's stubs).
SIGNING_CHECKS="modules dependencies vad ctranslate2 objc-callbacks"
# Checks the signed app must pass on this Mac, whatever the unsigned one did (mac-mouth = the
# windows' mouth is an AppKit view at the screen's scale: only a real Mac can tell).
REQUIRED_CHECKS="mac-mouth"

say() { printf '\n==> %s  [%s]\n' "$1" "$(date +%H:%M:%S)"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this runs on a Mac (codesign, notarytool, hdiutil)."
[ -x "$APP/Contents/MacOS/$APP_NAME" ] || fail "$APP is missing: run packaging/macos/build.sh --ci first."
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/shoutabout-release.XXXXXX")"
finish() {  # on every exit, success or not: the logs zip (an artifact), then cleanup
  cp "$DIST/build.log" "$LOGS/" 2>/dev/null || true
  rm -f "$OUT/$APP_NAME-mac-logs.zip"
  ditto -c -k --keepParent "$LOGS" "$OUT/$APP_NAME-mac-logs.zip" 2>/dev/null || true
  rm -rf "$WORK"  # holds the notary key file
}
trap finish EXIT
rm -rf "$OUT" "$LOGS"
mkdir -p "$OUT" "$LOGS"
VERSION="$("$PY" -c 'import sys; sys.path.insert(0, "src"); import shoutabout; print(shoutabout.__version__)')"
echo "ShoutAbout $VERSION, $(sw_vers -productName) $(sw_vers -productVersion), $(xcrun --find notarytool 2>/dev/null || echo 'no notarytool')"

# --------------------------------------------------------------------------- identity
IDENTITY="${MAC_SIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
  IDENTITY="$(security find-identity -v -p codesigning \
    | awk '/"Developer ID Application: / { print $2; exit }')"
fi
[ -n "$IDENTITY" ] || fail "no Developer ID Application identity in the keychain (packaging/macos/developer-id.sh install)."
security find-identity -v -p codesigning | grep -F "$IDENTITY" | sed 's/^ */signing as /' || true

# --------------------------------------------------------------------------- self-test
# selftest LABEL -> writes $LOGS/selftest-LABEL.json (missing if the app crashed or hung)
selftest() {
  local json="$LOGS/selftest-$1.json" pid waited=0
  rm -f "$json"
  "$APP/Contents/MacOS/$APP_NAME" --self-test --text --out "$json" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge 300 ]; then
      echo "the self-test hung for 5 minutes; stopping it"
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$json"
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid" 2>/dev/null || echo "(self-test exit code $?)"
}

# regressions BEFORE AFTER -> names of SIGNING_CHECKS that passed in BEFORE but not in AFTER
# ("crashed" if AFTER has no report while BEFORE has one)
regressions() {
  "$PY" - "$1" "$2" "$SIGNING_CHECKS" <<'EOF'
import json, sys
from pathlib import Path

def load(path):
    try:
        return {c["name"]: c["status"] for c in json.loads(Path(path).read_text())["checks"]}
    except Exception:
        return None

before, after, watched = load(sys.argv[1]), load(sys.argv[2]), sys.argv[3].split()
if before is None:
    print("unknown")  # the unsigned app crashed too: nothing to compare with
elif after is None:
    print("crashed")
else:
    print(" ".join(n for n in watched if before.get(n) == "pass" and after.get(n) != "pass"))
EOF
}

# not_passed REPORT -> names of REQUIRED_CHECKS that didn't pass ("no report" if it's missing)
not_passed() {
  "$PY" - "$1" "$REQUIRED_CHECKS" <<'EOF'
import json, sys
from pathlib import Path

try:
    checks = {c["name"]: c["status"] for c in json.loads(Path(sys.argv[1]).read_text())["checks"]}
except Exception:
    print("no report")
else:
    print(" ".join(n for n in sys.argv[2].split() if checks.get(n) != "pass"))
EOF
}

# --------------------------------------------------------------------------- signing
sign_app() {  # sign_app ENTITLEMENTS_FILE: the outer bundle (main executable + seal)
  codesign --force --timestamp --options runtime --entitlements "$1" \
    --sign "$IDENTITY" "$APP"
}

sign_nested() {
  "$PY" "$MACHO" list "$APP" > "$LOGS/signed-files.txt"
  local libs exes bundles
  libs="$(grep -c '^lib' "$LOGS/signed-files.txt" || true)"
  exes="$(grep -c '^exe' "$LOGS/signed-files.txt" || true)"
  bundles="$(grep -c '^bundle' "$LOGS/signed-files.txt" || true)"
  echo "$libs libraries, $exes helper executables, $bundles nested bundles"

  # Libraries don't contain each other: sign them in parallel (each codesign call asks
  # timestamp.apple.com), then retry any that failed one at a time.
  if ! "$PY" "$MACHO" list --kind lib -0 "$APP" \
      | xargs -0 -n 16 -P 4 codesign --force --timestamp --sign "$IDENTITY" >"$LOGS/sign.log" 2>&1; then
    echo "some libraries didn't sign in the parallel pass; retrying them one by one"
    # codesign's messages, without the file names, most frequent first
    grep -v "replacing existing signature" "$LOGS/sign.log" | sed -E 's#^/[^:]*: ##' \
      | sort | uniq -c | sort -rn | head -n 8 || true
    "$PY" "$MACHO" list --kind lib "$APP" | while IFS= read -r lib; do
      if codesign --verify --strict "$lib" 2>/dev/null \
          && codesign -dvv "$lib" 2>&1 | grep -q '^Timestamp='; then
        continue  # this one made it
      fi
      for attempt in 1 2 3; do
        codesign --force --timestamp --sign "$IDENTITY" "$lib" && break
        [ "$attempt" = 3 ] && fail "couldn't sign $lib"
        sleep 3
      done
    done
  fi
  # Helper executables: Hardened Runtime, an identifier under the app's, no entitlements.
  "$PY" "$MACHO" list --kind exe "$APP" | while IFS= read -r exe; do
    codesign --force --timestamp --options runtime -i "fyi.shoutabout.mac.$(basename "$exe")" \
      --sign "$IDENTITY" "$exe"
  done
  # Nested bundles, deepest first (macho.py's order), after everything inside them.
  "$PY" "$MACHO" list --kind bundle "$APP" | while IFS= read -r bundle; do
    codesign --force --timestamp --options runtime --sign "$IDENTITY" "$bundle"
  done
}

verify_signature() {
  codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | tee "$LOGS/codesign-verify.txt"
  codesign -dv --verbose=4 "$APP" > "$LOGS/codesign-details.txt" 2>&1
  grep -q 'Authority=Developer ID Application' "$LOGS/codesign-details.txt" \
    || fail "the app isn't signed with a Developer ID Application certificate"
  grep -q 'flags=.*runtime' "$LOGS/codesign-details.txt" || fail "the Hardened Runtime is off"
  grep -q '^Timestamp=' "$LOGS/codesign-details.txt" || fail "no secure timestamp"
  codesign -d --entitlements - --xml "$APP" > "$LOGS/entitlements.plist" 2>/dev/null || true
  echo "entitlements:"
  plutil -p "$LOGS/entitlements.plist" 2>/dev/null || cat "$LOGS/entitlements.plist"
  # Every nested library, in parallel (a bad one fails notarization much later).
  "$PY" "$MACHO" list --kind lib -0 "$APP" | xargs -0 -n 32 -P 6 codesign --verify --strict \
    || fail "a nested library has no valid signature (see above)"
}

say "Checking the bundle (no PyAV/FFmpeg, ASIO or Windows files)"
"$PY" packaging/macos/mac_rules.py check "$APP" || fail "a file that must not ship is in the app."

say "Self-test of the app as built (PyInstaller's ad-hoc signature)"
selftest unsigned

say "Signing every library and executable inside the app"
xattr -cr "$APP"   # codesign refuses files with Finder info or quarantine attributes
sign_nested
say "Signing the app (Hardened Runtime + $(basename "$ENTITLEMENTS"))"
sign_app "$ENTITLEMENTS"
verify_signature

say "Self-test of the signed app"
selftest signed
BROKEN="$(regressions "$LOGS/selftest-unsigned.json" "$LOGS/selftest-signed.json")"
if [ "$BROKEN" = "unknown" ]; then
  echo "WARNING: the self-test didn't produce a report even before signing (see the output"
  echo "above), so it can't tell whether signing broke anything. Continuing."
elif [ -n "$BROKEN" ]; then
  echo "Signing broke: $BROKEN"
  case " $BROKEN " in
    *" objc-callbacks "*|*" crashed "*)
      say "Retrying with com.apple.security.cs.allow-unsigned-executable-memory"
      cp "$ENTITLEMENTS" "$WORK/fallback.entitlements"
      plutil -insert com.apple.security.cs.allow-unsigned-executable-memory -bool true \
        "$WORK/fallback.entitlements"
      sign_app "$WORK/fallback.entitlements"
      verify_signature
      selftest signed-fallback
      BROKEN="$(regressions "$LOGS/selftest-unsigned.json" "$LOGS/selftest-signed-fallback.json")"
      [ -z "$BROKEN" ] || fail "the signed app still fails: $BROKEN (logs: ShoutAbout-mac-logs.zip)"
      echo "WARNING: PyObjC needed allow-unsigned-executable-memory on top of allow-jit. Add"
      echo "it to packaging/macos/ShoutAbout.entitlements (docs/release/codemagic.md)."
      echo "needs com.apple.security.cs.allow-unsigned-executable-memory" > "$LOGS/ENTITLEMENT-FALLBACK.txt"
      ;;
    *) fail "the Hardened Runtime broke: $BROKEN (logs: ShoutAbout-mac-logs.zip)" ;;
  esac
else
  echo "signed app passes every check the unsigned one passed"
fi
LAST_REPORT="$LOGS/selftest-signed.json"
[ -f "$LOGS/selftest-signed-fallback.json" ] && LAST_REPORT="$LOGS/selftest-signed-fallback.json"
FAILED="$(not_passed "$LAST_REPORT")"
[ -z "$FAILED" ] || fail "the signed app fails: $FAILED (see its self-test above)"
echo "signed app passes $REQUIRED_CHECKS"

# --------------------------------------------------------------------------- notarization
KEY_FILE=""
notary_key() {
  for name in APP_STORE_CONNECT_KEY_IDENTIFIER APP_STORE_CONNECT_ISSUER_ID APP_STORE_CONNECT_PRIVATE_KEY; do
    [ -n "${!name:-}" ] || fail "$name is missing (Codemagic environment group app_store_credentials)."
  done
  KEY_FILE="$WORK/AuthKey_${APP_STORE_CONNECT_KEY_IDENTIFIER}.p8"
  ( umask 077; printf '%s\n' "$APP_STORE_CONNECT_PRIVATE_KEY" | tr -d '\r' > "$KEY_FILE" )
  if ! grep -q "BEGIN PRIVATE KEY" "$KEY_FILE"; then  # also accept the base64 of the file
    ( umask 077; printf '%s' "$APP_STORE_CONNECT_PRIVATE_KEY" | base64 --decode > "$KEY_FILE" ) 2>/dev/null || true
    grep -q "BEGIN PRIVATE KEY" "$KEY_FILE" \
      || fail "APP_STORE_CONNECT_PRIVATE_KEY isn't the .p8 file's text (BEGIN PRIVATE KEY ...)."
  fi
}

notary_field() {  # notary_field FILE NAME: the last NAME in the JSON object(s) notarytool printed
  "$PY" - "$1" "$2" <<'EOF' 2>/dev/null || true
import json, sys
text, name, value, pos = open(sys.argv[1]).read(), sys.argv[2], "", 0
decoder = json.JSONDecoder()
while True:
    start = text.find("{", pos)
    if start < 0:
        break
    try:
        obj, pos = decoder.raw_decode(text, start)
    except ValueError:
        pos = start + 1
        continue
    if isinstance(obj, dict) and obj.get(name):
        value = str(obj[name])
print(value)
EOF
}

notarize() {  # notarize FILE LABEL: submit, wait, keep Apple's log, fail unless Accepted
  local file="$1" label="$2" result="$LOGS/notary-$2.json"
  say "Notarizing $(basename "$file") (Apple: most within 5 minutes, 98 % within 15)"
  local rc=0
  xcrun notarytool submit "$file" --key "$KEY_FILE" --key-id "$APP_STORE_CONNECT_KEY_IDENTIFIER" \
    --issuer "$APP_STORE_CONNECT_ISSUER_ID" --wait --timeout 40m --output-format json \
    > "$result" || rc=$?
  cat "$result"; echo
  local id status
  id="$(notary_field "$result" id)"
  status="$(notary_field "$result" status)"
  if [ -n "$id" ]; then
    xcrun notarytool log "$id" --key "$KEY_FILE" --key-id "$APP_STORE_CONNECT_KEY_IDENTIFIER" \
      --issuer "$APP_STORE_CONNECT_ISSUER_ID" "$LOGS/notary-$label-log.json" >/dev/null 2>&1 || true
  fi
  if [ "$status" != "Accepted" ]; then
    if [ -f "$LOGS/notary-$label-log.json" ]; then
      echo "Apple's notarization log (the \"issues\" say which file and why):"
      cat "$LOGS/notary-$label-log.json"
    fi
    fail "notarization of $(basename "$file") ended with '${status:-no answer}' (notarytool exit $rc)."
  fi
  # Accepted can still carry warnings worth fixing before the next release.
  "$PY" - "$LOGS/notary-$label-log.json" <<'EOF' || true
import json, sys
try:
    issues = json.load(open(sys.argv[1])).get("issues") or []
except Exception:
    issues = []
for i in issues:
    print(f"notary {i.get('severity')}: {i.get('path')}: {i.get('message')}")
EOF
}

staple() {  # staple PATH: attach the ticket and check it
  local attempt
  for attempt in 1 2 3; do  # the ticket can take a moment to show up in Apple's CDN
    xcrun stapler staple "$1" && break
    [ "$attempt" = 3 ] && fail "couldn't staple $(basename "$1")"
    sleep 15
  done
  xcrun stapler validate "$1"
}

make_dmg() {
  local stage="$WORK/dmg" attempt
  rm -rf "$stage"
  mkdir -p "$stage"
  ditto "$APP" "$stage/$APP_NAME.app"
  ln -s /Applications "$stage/Applications"   # drag the app onto it to install
  rm -f "$OUT/$APP_NAME.dmg"
  for attempt in 1 2 3; do  # hdiutil sometimes reports "Resource busy" on build machines
    hdiutil create -volname "$APP_NAME" -srcfolder "$stage" -ov -format UDZO -fs HFS+ \
      "$OUT/$APP_NAME.dmg" && break
    [ "$attempt" = 3 ] && fail "hdiutil couldn't make the disk image"
    sleep 5
  done
  hdiutil verify "$OUT/$APP_NAME.dmg"
  codesign --force --timestamp -i "fyi.shoutabout.mac.dmg" --sign "$IDENTITY" "$OUT/$APP_NAME.dmg"
  codesign --verify --verbose=2 "$OUT/$APP_NAME.dmg"
}

if [ "$NOTARIZE" = 1 ]; then
  notary_key
  say "Zipping the app for notarization"
  ditto -c -k --keepParent "$APP" "$WORK/$APP_NAME-notarize.zip"
  du -h "$WORK/$APP_NAME-notarize.zip"
  notarize "$WORK/$APP_NAME-notarize.zip" app
  say "Stapling the ticket to the app"
  staple "$APP"
fi

say "Final checks of the app"
"$PY" packaging/macos/mac_rules.py check "$APP" || fail "a file that must not ship is in the app."
codesign --verify --deep --strict "$APP"

say "Disk image $APP_NAME.dmg"
make_dmg
if [ "$NOTARIZE" = 1 ]; then
  notarize "$OUT/$APP_NAME.dmg" dmg
  say "Stapling the ticket to the disk image"
  staple "$OUT/$APP_NAME.dmg"
fi

say "Zip of the app ($APP_NAME-mac.zip)"
ditto -c -k --keepParent "$APP" "$OUT/$APP_NAME-mac.zip"

if [ "$NOTARIZE" = 1 ]; then
  say "Gatekeeper's verdict (as on a Mac that downloaded these files)"
  spctl --assess --type execute --verbose=4 "$APP" 2>&1 | tee "$LOGS/spctl-app.txt" || true
  spctl --assess --type open --context context:primary-signature --verbose=4 \
    "$OUT/$APP_NAME.dmg" 2>&1 | tee "$LOGS/spctl-dmg.txt" || true
  if grep -q "rejected" "$LOGS/spctl-app.txt" "$LOGS/spctl-dmg.txt"; then
    fail "Gatekeeper rejects the result (see above)"
  fi
fi

say "Checksums"
( cd "$OUT" && shasum -a 256 "$APP_NAME.dmg" "$APP_NAME-mac.zip" ) | tee "$LOGS/SHA256SUMS.txt"
echo "ShoutAbout $VERSION" > "$LOGS/VERSION.txt"
du -h "$OUT"/*
say "Done: $OUT/$APP_NAME.dmg and $APP_NAME-mac.zip (ShoutAbout $VERSION); the logs zip follows"
