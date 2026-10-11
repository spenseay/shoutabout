#!/bin/bash
# The "Developer ID Application" certificate that signs ShoutAbout.app for other Macs,
# on a Codemagic build machine (codemagic.yaml). Two jobs:
#
#   bash packaging/macos/developer-id.sh request   # workflow mac-signing-request (once)
#   bash packaging/macos/developer-id.sh install   # first step of workflow mac-release
#
# Why a request at all: Apple lets only the Account Holder create Developer ID
# certificates, on developer.apple.com; an App Store Connect API key can't (unlike the
# iPhone's distribution certificate, which Codemagic creates by itself). So, once:
#   request  makes packaging/dist-mac/signing-request/ShoutAbout-DeveloperID.certSigningRequest
#            from CERTIFICATE_PRIVATE_KEY (a public-key file, not a secret). The owner uploads
#            it at Certificates > + > Developer ID Application (docs/release/codemagic.md).
#            If a certificate for that key already exists, it says so and makes nothing.
#   install  finds that certificate through the API (it matches the private key), puts it
#            and its key into a new temporary keychain, and exports MAC_SIGN_IDENTITY (the
#            identity's SHA-1) to the later build steps through $CM_ENV.
#
# Needs (Codemagic environment group app_store_credentials): APP_STORE_CONNECT_ISSUER_ID,
# APP_STORE_CONNECT_KEY_IDENTIFIER, APP_STORE_CONNECT_PRIVATE_KEY, CERTIFICATE_PRIVATE_KEY;
# and Codemagic's CLI tools (app-store-connect, keychain), preinstalled on its machines.
# Never prints a secret.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REQUEST_DIR="$ROOT/packaging/dist-mac/signing-request"
CSR_NAME="ShoutAbout-DeveloperID.certSigningRequest"
# Apple makes Developer ID certificates of type DEVELOPER_ID_APPLICATION_G2; the CLI tools
# dropped that type in 0.59.0 and brought it back in 0.68.0 (their CHANGELOG).
MIN_CLI="0.68.0"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

need_env() {
  local missing=""
  for name in APP_STORE_CONNECT_ISSUER_ID APP_STORE_CONNECT_KEY_IDENTIFIER \
              APP_STORE_CONNECT_PRIVATE_KEY CERTIFICATE_PRIVATE_KEY; do
    [ -n "${!name:-}" ] || missing="$missing $name"
  done
  [ -z "$missing" ] || fail "missing in the Codemagic environment group app_store_credentials:$missing
(docs/release/codemagic.md, part C)."
  command -v app-store-connect >/dev/null \
    || fail "Codemagic's CLI tools aren't installed (pip3 install codemagic-cli-tools)."
  ensure_cli
}

ensure_cli() {  # upgrade the machine's CLI tools if they predate MIN_CLI
  local have py
  have="$(app-store-connect --version 2>&1 | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
  if python3 - "${have:-0}" "$MIN_CLI" <<'EOF'
import sys
version = lambda s: tuple(int(x) for x in s.split("."))
sys.exit(0 if version(sys.argv[1]) >= version(sys.argv[2]) else 1)
EOF
  then
    echo "Codemagic CLI tools $have"
    return 0
  fi
  echo "Codemagic CLI tools ${have:-(unknown version)} predate $MIN_CLI: upgrading them"
  # Into the Python that runs them (their script's #! line), else python3.
  py="$(head -n 1 "$(command -v app-store-connect)" | sed -e 's/^#! *//' -e 's/ .*//')"
  case "$py" in */python*) ;; *) py="python3" ;; esac
  "$py" -m pip install --quiet --upgrade "codemagic-cli-tools>=$MIN_CLI" \
    || echo "WARNING: couldn't upgrade them; going on with ${have:-the installed version}"
  hash -r
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/developer-id.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

key_file() {  # the certificate private key as a PEM file (LF line endings), mode 600
  local path="$WORK/certificate_key.pem"
  ( umask 077; printf '%s\n' "$CERTIFICATE_PRIVATE_KEY" | tr -d '\r' > "$path" )
  grep -q -- "-----BEGIN .*PRIVATE KEY-----" "$path" \
    || fail "CERTIFICATE_PRIVATE_KEY isn't a PEM private key: paste the whole file, including the BEGIN and END lines."
  echo "$path"
}

list_certificates() {  # list_certificates DIR: saves a .p12 per matching certificate in DIR
  # MAC_APP_DIRECT = Developer ID distribution: both DEVELOPER_ID_APPLICATION and the newer
  # DEVELOPER_ID_APPLICATION_G2 types. The listing itself (kept out of the log: it names
  # every Developer ID certificate of the team) is filtered by --certificate-key before
  # --save, so only certificates made from CERTIFICATE_PRIVATE_KEY end up in DIR.
  mkdir -p "$1"
  app-store-connect certificates list --profile-type MAC_APP_DIRECT \
    --certificate-key "@file:$KEY" --save --certificates-dir "$1" --json > "$WORK/listing.json"
}

count_p12() {  # count_p12 DIR
  find "$1" -maxdepth 1 -name '*.p12' -type f | wc -l | tr -d ' '
}

request() {
  need_env
  KEY="$(key_file)"
  say "Looking for a Developer ID Application certificate made from CERTIFICATE_PRIVATE_KEY"
  list_certificates "$WORK/found"
  rm -rf "$REQUEST_DIR"
  mkdir -p "$REQUEST_DIR"
  if [ "$(count_p12 "$WORK/found")" -gt 0 ]; then
    cat > "$REQUEST_DIR/README.txt" <<'EOF'
Nothing to do: Apple already has a Developer ID Application certificate for this key.
Start the mac-release workflow.
EOF
    say "Already set up. Nothing to upload: start the mac-release workflow."
    return 0
  fi
  say "None yet: making the certificate signing request"
  # The subject is only a label: Apple writes the team's name into the certificate.
  openssl req -new -sha256 -key "$KEY" -subj "/CN=ShoutAbout Developer ID/" \
    -out "$REQUEST_DIR/$CSR_NAME"
  openssl req -in "$REQUEST_DIR/$CSR_NAME" -noout -verify >/dev/null 2>&1 \
    || fail "the signing request didn't verify"
  cat > "$REQUEST_DIR/README.txt" <<EOF
Upload $CSR_NAME to Apple (it holds only the public half of the key):
developer.apple.com > Account > Certificates, IDs & Profiles > Certificates > (+)
> Developer ID Application > Continue > G2 Sub-CA > Choose File > Continue.
You don't need to download the certificate. Then start the mac-release workflow.
EOF
  cat "$REQUEST_DIR/README.txt"
  say "Done: download $CSR_NAME from this build's Artifacts and upload it to Apple"
}

probe() {  # probe IDENTITY [codesign options...]: sign a scratch copy of /usr/bin/true
  local identity="$1"
  shift
  cp -f /usr/bin/true "$WORK/probe"
  if codesign --force --timestamp "$@" --sign "$identity" "$WORK/probe" 2>"$WORK/probe.log"; then
    return 0
  fi
  sed 's/^/  codesign: /' "$WORK/probe.log" >&2
  return 1
}

repair() {  # repair IDENTITY KEYCHAIN: what's wrong (Apple's checks), then known fixes in turn
  local identity="$1" keychain_path="$2" leaf="$WORK/leaf.pem"
  say "codesign failed: checking the certificate chain and the key"
  security find-certificate -a -c "Developer ID Application" -Z "$keychain_path" \
    | awk '/^SHA-1/ { n++ } END { print "  Developer ID Application certificates in the keychain: " n+0 }'
  security find-certificate -c "Developer ID Application" -p "$keychain_path" > "$leaf" || true
  openssl x509 -in "$leaf" -noout -subject -issuer -enddate 2>&1 | sed 's/^/  leaf /' || true
  echo "  chain check (security verify-cert, code signing policy):"
  security verify-cert -c "$leaf" -p codeSign 2>&1 | sed 's/^/    /' || true
  echo "  trust settings (user, then admin):"
  { security dump-trust-settings 2>&1; security dump-trust-settings -d 2>&1; } \
    | head -n 20 | sed 's/^/    /' || true
  security list-keychains -d user | sed 's/^/  search list: /' || true

  say "Trying fixes"
  echo "- unlock the keychain again"
  security unlock-keychain -p "" "$keychain_path" 2>&1 | sed 's/^/  /' || true
  if probe "$identity"; then echo "  that fixed it"; return 0; fi
  echo "- the intermediate in the login keychain too"
  security add-certificates -k "$HOME/Library/Keychains/login.keychain-db" \
    "$WORK/DeveloperIDG2CA.cer" 2>&1 | grep -v "already exists" | sed 's/^/  /' || true
  if probe "$identity"; then echo "  that fixed it"; return 0; fi
  # Diagnostics only (the build doesn't use these options):
  echo "- (diagnostic) only this keychain (--keychain)"
  probe "$identity" --keychain "$keychain_path" && echo "  works with --keychain"
  echo "- (diagnostic) no secure timestamp"
  probe "$identity" --timestamp=none && echo "  works without a timestamp: timestamp.apple.com is the problem"
  return 1
}

install() {
  need_env
  KEY="$(key_file)"
  say "Fetching the Developer ID Application certificate for CERTIFICATE_PRIVATE_KEY"
  list_certificates "$WORK/certificates"
  local found
  found="$(count_p12 "$WORK/certificates")"
  if [ "$found" -eq 0 ]; then
    fail "Apple has no Developer ID Application certificate for CERTIFICATE_PRIVATE_KEY yet.
Run the mac-signing-request workflow once, upload the file it makes to Apple
(docs/release/codemagic.md, part D), then start mac-release again."
  fi
  echo "found $found certificate(s) for this key"

  say "Temporary keychain"
  keychain initialize
  keychain add-certificates --certificate "$WORK/certificates/*.p12"
  # The Developer ID intermediate certificates (G2 for certificates made since 2019, and the
  # original), so codesign can build the chain to Apple's root. They go into the System
  # keychain: in a session without a logged-in desktop (a build machine) the trust service
  # doesn't look for intermediates in the user's keychains, and codesign then fails with
  # "unable to build chain to self-signed root" + errSecInternalComponent (Apple forums
  # threads 733941/737019). Codemagic's machines have the iPhone intermediates there, not
  # these. Also into the temporary keychain, for a machine where sudo needs a password.
  local keychain_path
  keychain_path="$(security default-keychain | sed -e 's/^ *"//' -e 's/" *$//')"
  for ca in DeveloperIDG2CA.cer DeveloperIDCA.cer; do
    if curl -fsSL --retry 3 -o "$WORK/$ca" "https://www.apple.com/certificateauthority/$ca"; then
      # add-certificates takes DER .cer files as they are (import guesses the format).
      sudo -n security add-certificates -k /Library/Keychains/System.keychain "$WORK/$ca" 2>&1 \
        | grep -v "already exists" | sed 's/^/  System keychain: /' || true
      security add-certificates -k "$keychain_path" "$WORK/$ca" 2>&1 \
        | grep -v "already exists" || true
    else
      echo "(couldn't download $ca; relying on the machine's own copy)"
    fi
  done
  security find-certificate -a -c "Developer ID Certification Authority" \
    /Library/Keychains/System.keychain "$keychain_path" \
    | awk -F'"' '/^keychain:/ { k = $2 } /"labl"/ { print "  intermediate: " $4 " in " k }' || true

  local identity
  identity="$(security find-identity -v -p codesigning \
    | awk '/"Developer ID Application: / { print $2; exit }')"
  if [ -z "$identity" ]; then
    security find-identity -v -p codesigning || true
    fail "the certificate is in the keychain but isn't a valid signing identity (see the list above)."
  fi
  security find-identity -v -p codesigning | grep -F "$identity" | sed 's/^ */  using /'

  # Let codesign use the key without a prompt nobody can answer on a build machine (the
  # usual cause of errSecInternalComponent). Codemagic's keychain has an empty password.
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "" "$keychain_path" \
    >/dev/null 2>&1 || echo "(couldn't set the key's partition list; going on)"
  # Sign a scratch copy of a system binary now, so a keychain problem fails here in seconds
  # instead of after the 1-minute build, with codesign's own words in the log.
  if ! probe "$identity" && ! repair "$identity" "$keychain_path"; then
    fail "codesign can't sign with the Developer ID identity (codesign's messages are above)."
  fi
  echo "  test signature ok"
  if [ -n "${CM_ENV:-}" ]; then
    echo "MAC_SIGN_IDENTITY=$identity" >> "$CM_ENV"
  fi
  say "Developer ID identity ready"
}

case "${1:-}" in
  request) request ;;
  install) install ;;
  *) echo "usage: $0 request|install" >&2; exit 2 ;;
esac
