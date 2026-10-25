#!/usr/bin/env bash
# Print the path of a CA bundle for go-ios: this system's CAs + Apple Root CA (apple-root.pem).
# go-ios needs Apple's root to reach gs.apple.com (developer disk image signing), and some distros' stores lack it.
# Rebuilt when missing, or when the system bundle or apple-root.pem changed, so it never goes stale.
#   certs/ca-bundle.sh [OUT]       print OUT (default $XDG_STATE_HOME/iphone-control/ca-bundle-with-apple.pem)
#   certs/ca-bundle.sh --source    only print which system bundle would be used
#   CA_BUNDLE=/path/bundle.pem     use that system bundle instead of the auto-detected one
# Use:  SSL_CERT_FILE="$("$HERE/certs/ca-bundle.sh")" ios image auto ...
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
APPLE="$DIR/apple-root.pem"
APPLE_PEM_SHA256=f476b5c8ebe6468094981fb6a986e3d930fd020f6f379248000395a1a854c125   # Apple Inc. Root, SHA-256 fp B0:B1:73:0E:...:F0:24

# The system bundle, in the order Go's crypto/x509 looks for it (Debian/Ubuntu/Arch, Fedora/RHEL, openSUSE, ...)
system_ca() {
  local ca
  if [ -n "${CA_BUNDLE:-}" ]; then
    [ -r "$CA_BUNDLE" ] && [ -s "$CA_BUNDLE" ] && { echo "$CA_BUNDLE"; return 0; }
    echo "CA_BUNDLE=$CA_BUNDLE is not a readable file" >&2; return 1
  fi
  for ca in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/ca-bundle.pem \
            /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/pki/tls/cacert.pem /etc/ssl/cert.pem; do
    [ -r "$ca" ] && [ -s "$ca" ] && { echo "$ca"; return 0; }
  done
  echo "no system CA bundle found - rerun with CA_BUNDLE=/path/to/bundle.pem" >&2
  return 1
}

ca=$(system_ca) || exit 1
[ "${1:-}" = --source ] && { echo "$ca"; exit 0; }

[ "$(sha256sum <"$APPLE" | cut -d' ' -f1)" = "$APPLE_PEM_SHA256" ] || { echo "$APPLE changed (checksum mismatch)" >&2; exit 1; }

out="${1:-${XDG_STATE_HOME:-$HOME/.local/state}/iphone-control/ca-bundle-with-apple.pem}"
head="# system CAs from $ca + Apple Root CA (certs/ca-bundle.sh)"
if [ ! -s "$out" ] || [ "$ca" -nt "$out" ] || [ "$APPLE" -nt "$out" ] || [ "$(head -n1 "$out")" != "$head" ]; then
  mkdir -p "$(dirname "$out")" || exit 1
  tmp=$(mktemp "$out.XXXXXX") || exit 1
  if { echo "$head"; cat "$ca"; echo; cat "$APPLE"; } >"$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$out"; then :; else
    rm -f "$tmp"; echo "could not write $out" >&2; exit 1
  fi
fi
echo "$out"
