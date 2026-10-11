#!/bin/bash
# Print the next iPhone build number: the newest TestFlight build of the app + 1 (1 for the
# very first upload). App Store Connect refuses a build number it has already seen.
#
#   bash ios/scripts/next-build-number.sh fyi.shoutabout.app
#
# Uses Codemagic's CLI (app-store-connect) with the App Store Connect API key in
# APP_STORE_CONNECT_ISSUER_ID / _KEY_IDENTIFIER / _PRIVATE_KEY. Finds the app by its bundle
# ID, so nobody has to copy the app's numeric Apple ID anywhere. Only the number goes to
# stdout; everything else to stderr.
set -euo pipefail

BUNDLE_ID="${1:?usage: next-build-number.sh BUNDLE_ID}"

# (The filter below keeps the exact bundle ID: the API's filter also matches longer ones.)
apps_json="$(app-store-connect apps list --bundle-id-identifier "$BUNDLE_ID" --json)"
app_id="$(printf '%s' "$apps_json" | python3 -c '
import json, sys
bundle = sys.argv[1]
try:
    apps = json.load(sys.stdin)
except ValueError:
    apps = []
ids = [a.get("id") for a in apps if a.get("attributes", {}).get("bundleId") == bundle]
print(ids[0] if ids else "")
' "$BUNDLE_ID")"
if [ -z "$app_id" ]; then
  echo "ERROR: App Store Connect has no app with bundle ID $BUNDLE_ID." >&2
  echo "Create it first: App Store Connect > Apps > + > New App (docs/release/codemagic.md, part A)." >&2
  exit 1
fi
echo "App Store Connect app $app_id ($BUNDLE_ID)" >&2

# Prints nothing (and exits 0) when the app has no TestFlight build yet.
latest="$(app-store-connect get-latest-testflight-build-number "$app_id")"
latest="${latest%%.*}"           # "12" from "12" or "12.1"
latest="${latest//[^0-9]/}"
echo "latest TestFlight build: ${latest:-none}" >&2
echo $(( ${latest:-0} + 1 ))
