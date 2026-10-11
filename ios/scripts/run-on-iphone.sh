#!/bin/bash
# Put ShoutAbout on your own iPhone from a Mac, with a free Apple ID (no paid developer
# account). Run it from the ios folder or anywhere:   bash ios/scripts/run-on-iphone.sh
#
# What it does:
#   1. checks that Xcode is installed (and uses it even if xcode-select points elsewhere);
#   2. downloads XcodeGen (pinned version, checksum-verified) into ios/.tools/;
#   3. writes ios/Config/Local.xcconfig once: a personal bundle ID prefix (so the official
#      fyi.shoutabout IDs stay free for the paid account) and your Personal Team, if Xcode
#      already knows your Apple ID;
#   4. generates ShoutAbout.xcodeproj and opens it in Xcode.
# Then pick your iPhone at the top of the Xcode window and press Run (the play button).
#
# Free installs expire after 7 days: run this again (or just press Run in Xcode) to renew.
set -euo pipefail

XCODEGEN_VERSION="2.46.0"
XCODEGEN_SHA256="4d9e34b62172d645eed6457cac13fc222569974098ef4ee9c3368bedf0196806"
XCODEGEN_URL="https://github.com/yonaskolb/XcodeGen/releases/download/${XCODEGEN_VERSION}/xcodegen.zip"

IOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$IOS_DIR"

say() { printf '\n== %s\n' "$*"; }
fail() { printf '\nProblem: %s\n' "$*" >&2; exit 1; }

# 1. Xcode -------------------------------------------------------------------------------
if [ -d /Applications/Xcode.app ]; then
    export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi
xcodebuild -version >/dev/null 2>&1 || fail "Xcode isn't installed. Get it from the App Store, open it once, then run this again."
say "Using $(xcodebuild -version | head -1)"
if ! xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then
    fail "Xcode hasn't finished setting up. Open Xcode once and let it install its components (it asks for your Mac password), then run this again."
fi

# 2. XcodeGen ----------------------------------------------------------------------------
TOOLS="$IOS_DIR/.tools"
XCODEGEN="$TOOLS/xcodegen/bin/xcodegen"
if [ ! -x "$XCODEGEN" ]; then
    say "Downloading XcodeGen $XCODEGEN_VERSION"
    mkdir -p "$TOOLS"
    curl -fsSL -o "$TOOLS/xcodegen.zip" "$XCODEGEN_URL"
    echo "$XCODEGEN_SHA256  $TOOLS/xcodegen.zip" | shasum -a 256 -c - >/dev/null \
        || fail "the XcodeGen download didn't match its checksum; try again later."
    rm -rf "$TOOLS/xcodegen"
    unzip -q -o "$TOOLS/xcodegen.zip" -d "$TOOLS"
    rm -f "$TOOLS/xcodegen.zip"
    chmod +x "$XCODEGEN"
fi

# 3. Personal settings -------------------------------------------------------------------
LOCAL="$IOS_DIR/Config/Local.xcconfig"
personal_team() {
    # The free "Personal Team" Xcode created when you added your Apple ID (Settings >
    # Accounts). Empty if Xcode doesn't know one yet: you then pick it in the Signing tab.
    defaults export com.apple.dt.Xcode - 2>/dev/null \
        | plutil -extract IDEProvisioningTeamByIdentifier json -o - - 2>/dev/null \
        | xcrun python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
teams = [t for v in data.values() for t in (v if isinstance(v, list) else [])]
free = [t for t in teams if t.get("isFreeProvisioningTeam") or "personal" in str(t.get("teamType", "")).lower()]
pick = (free or teams or [{}])[0]
print(pick.get("teamID", ""))
' 2>/dev/null || true
}
if [ ! -f "$LOCAL" ]; then
    suffix="$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 6)"
    team="$(personal_team)"
    cat >"$LOCAL" <<EOF
// Personal build settings for this Mac (not committed). Written by scripts/run-on-iphone.sh.
// A personal bundle ID prefix keeps the official fyi.shoutabout IDs free for a paid account.
BUNDLE_ID_PREFIX = fyi.shoutabout.p${suffix}
DEVELOPMENT_TEAM = ${team}
EOF
    say "Wrote Config/Local.xcconfig (bundle prefix fyi.shoutabout.p${suffix}${team:+, team $team})"
elif ! grep -q '^DEVELOPMENT_TEAM = [A-Z0-9]' "$LOCAL"; then
    team="$(personal_team)"
    if [ -n "$team" ]; then
        sed -i '' "s/^DEVELOPMENT_TEAM = .*/DEVELOPMENT_TEAM = ${team}/" "$LOCAL"
        say "Found your Personal Team ($team)"
    fi
fi

# 4. Generate and open -------------------------------------------------------------------
say "Generating ShoutAbout.xcodeproj"
"$XCODEGEN" generate --quiet
open "$IOS_DIR/ShoutAbout.xcodeproj"

cat <<'EOF'

== Xcode is opening. Now:
   1. Plug in your iPhone (unlock it; tap "Trust" if it asks).
   2. At the top of the Xcode window, click the device name and choose your iPhone.
   3. Press Run (the play button). The first build takes a few minutes.
   If Xcode says "Signing requires a development team": click the error, choose
   "Your Name (Personal Team)" in Team, for the ShoutAbout and ShoutAboutWidgets targets.
   If the iPhone says "Developer Mode required": Settings > Privacy & Security >
   Developer Mode > On, restart the phone, then press Run again.
   If it says "Untrusted Developer": Settings > General > VPN & Device Management >
   your Apple ID > Trust.
EOF
