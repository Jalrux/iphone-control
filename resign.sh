#!/usr/bin/env bash
# Sign WebDriverAgent with your Apple ID and install it on the plugged-in iPhone.
# Run once after setup; iphone.sh runs it again when the signature is about to run out.
#   ./resign.sh            sign + install (with several iPhones plugged in, IPHONE_UDID picks one)
#   ./resign.sh --record   only note the bundle id and expiry of an older build/WebDriverAgent-signed.ipa
#                          in the state dir, then delete that IPA (no Apple ID, no phone needed)
# Exit status: 0 ok, 1 failed, 2 usage, 3 Apple ID not logged in / session expired, 4 iPhone locked.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/iphone-control"
IPA="$HERE/build/WebDriverAgent.ipa"
SIGNED="$STATE/WebDriverAgent-signed.ipa"   # holds the team id and device UDIDs: kept out of the repo, deleted after use
OLD_SIGNED="$HERE/build/WebDriverAgent-signed.ipa"
LOG="$STATE/resign.log"                     # can contain the Apple ID: lives in the state dir only
LOGIN_HINT="in a terminal run:  $BIN/plumesign account login -u YOUR_APPLE_ID_EMAIL"
PLUME_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/PlumeImpactor"   # plumesign's Apple ID token and signing key

case "${1:-}" in   # --help and a typo change nothing on disk
  ""|--record) ;;
  -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--record | --help]"; exit 2 ;;
esac
mkdir -p "$STATE" && chmod 700 "$STATE"

# A Python 3.9+ (pyenv/conda shims may come first in PATH; IPHONE_PYTHON wins)
PY=
for p in ${IPHONE_PYTHON:+"$IPHONE_PYTHON"} /usr/bin/python3 python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -I -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PY=$p; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.9 or newer not found (set IPHONE_PYTHON)"; exit 1; }

redact() {  # keep the Apple ID, UDIDs and team id out of terminals and bug reports
  sed -E 's/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<apple-id>/g; s/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}/<udid>/g; s/\b[0-9A-Fa-f]{24}\b/<udid>/g
          s/\b[0-9a-f]{40}\b/<udid>/g; s/(xctrunner)\.[A-Z0-9]{10}/\1.<team id>/g'
}

# record IPA: write wda_bundle, signed_at and expires_at (profile or signing certificate end, whichever
# comes first) to config.json atomically; prints "bundle expires_at"
record() {
  "$PY" -I - "$1" "$STATE/config.json" <<'EOF'
import calendar, json, os, plistlib, subprocess, sys, time, zipfile

ipa, path = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(ipa) as z:
    info = next((n for n in z.namelist()
                 if n.startswith("Payload/") and n.count("/") == 2 and n.endswith(".app/Info.plist")), None)
    if info is None:
        sys.exit("the signed IPA has no Payload/*.app/Info.plist")
    bundle = plistlib.loads(z.read(info))["CFBundleIdentifier"]
    try:
        raw = z.read(info[:-len("Info.plist")] + "embedded.mobileprovision")
    except KeyError:
        raw = b""
signed = expires = None
if b"<?xml" in raw and b"</plist>" in raw:
    prof = plistlib.loads(raw[raw.index(b"<?xml"):raw.index(b"</plist>") + 8])
    # plistlib gives naive UTC datetimes: timegm, not .timestamp() (that would apply the local offset)
    if "CreationDate" in prof:
        signed = calendar.timegm(prof["CreationDate"].utctimetuple())
    if "ExpirationDate" in prof:
        expires = calendar.timegm(prof["ExpirationDate"].utctimetuple())
    ends = []
    for der in prof.get("DeveloperCertificates", []):
        try:
            out = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-enddate"], input=der,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout.decode()
            ends.append(calendar.timegm(time.strptime(out.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z")))
        except (OSError, IndexError, ValueError, subprocess.SubprocessError):
            pass  # no openssl: the profile's date alone
    if ends:
        expires = min(expires or max(ends), max(ends))
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {}
cfg.update(wda_bundle=bundle, signed_at=signed or int(time.time()))
if expires:
    cfg["expires_at"] = expires
else:
    cfg.pop("expires_at", None)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print(bundle, expires or "")
EOF
}

say_installed() {  # say_installed "bundle expires_at"
  local bundle=${1%% *} exp=${1#* }
  echo "installed ${bundle%.*}.<team id>"
  if [ -n "$exp" ]; then echo "signature valid until $(date -d "@$exp" '+%a %d %b %Y %H:%M' 2>/dev/null || echo "$exp")"; fi
}

if [ "${1:-}" = --record ]; then
  [ -f "$OLD_SIGNED" ] || { echo "no $OLD_SIGNED to record"; exit 1; }
  out=$(record "$OLD_SIGNED") || exit 1
  rm -f "$OLD_SIGNED"
  say_installed "$out"
  exit 0
fi

# one signing at a time (a manual run and iphone.sh's automatic one)
if command -v flock >/dev/null; then
  exec 8>>"$STATE/resign.lock"
  flock -n 8 || { echo "resign.sh is already running"; exit 1; }
fi

[ -f "$IPA" ] || { echo "build/WebDriverAgent.ipa missing - run ./setup.sh first"; exit 1; }
[ -x "$BIN/plumesign" ] || { echo "$BIN/plumesign missing - run ./setup.sh first"; exit 1; }
export RUST_LOG=info   # plumesign's "No accounts found", "Saved accounts" and "Installation complete!" are info lines

# plumesign runs in the background so a signal reaches it: timeout puts it in its own process group, out of
# reach of Ctrl+C and of iphone.sh's stop, and bash runs traps only once a foreground command ends.
# fd 8 (the lock) stays here, so a plumesign that outlives us cannot block the next re-sign.
CHILD=
on_signal() { [ -z "$CHILD" ] || kill "$CHILD" 2>/dev/null; exit "$1"; }   # timeout passes TERM on to plumesign
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
trap 'on_signal 129' HUP
run_child() {  # run_child CMD...: run CMD (stdout+stderr in $LOG) so that a signal stops it; its exit status
  local rc=0
  "$@" </dev/null >"$LOG" 2>&1 8>&- &
  CHILD=$!
  wait "$CHILD" || rc=$?
  CHILD=
  return $rc
}
private_plume() { chmod -R go-rwx "$PLUME_CFG" 2>/dev/null || true; }   # "account login" uses the user's umask

rc=0
run_child timeout 20 "$BIN/plumesign" account list || rc=$?
if grep -q "No accounts found" "$LOG"; then
  echo "Not logged in to an Apple ID - $LOGIN_HINT"
  exit 3
elif [ $rc -eq 124 ]; then
  echo "plumesign account list did not finish in 20 s - rebuild it: $HERE/setup.sh --rebuild"; exit 1
elif [ $rc -ne 0 ]; then   # a plumesign that cannot run must not pass as "logged in" (one line: iphone.sh shows the last)
  echo "plumesign does not run (exit $rc): $(grep -v '^ *$' "$LOG" | tail -1 | cut -c1-160 | redact) - rebuild it: $HERE/setup.sh --rebuild"
  exit 1
fi
private_plume

# plumesign's --udid wants usbmuxd's device number (1, 2, ...), not the phone's UDID
mux() {
  "$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from iphone_control.usbmux import main; sys.exit(main(sys.argv[2:]))' \
    "$HERE" "$@"
}
phones=$(mux list 2>/dev/null | cut -f2) || phones=
if [ -n "${IPHONE_UDID:-}" ]; then
  DEV=$(mux device-id --udid "$IPHONE_UDID" 2>/dev/null) || DEV=
  [ -n "$DEV" ] || { echo "iPhone $IPHONE_UDID is not on USB (plugged in: $(paste -sd' ' - <<<"${phones:-none}"))"; exit 1; }
elif [ "$(printf '%s\n' "$phones" | grep -c .)" -gt 1 ]; then
  echo "Several iPhones are plugged in - choose one:  IPHONE_UDID=<udid> $0"
  printf '%s\n' "$phones" | sed 's/^/  /'
  exit 1
else
  DEV=$(mux device-id 2>/dev/null) || DEV=
  [ -n "$DEV" ] || { echo "No iPhone on USB. Plug it in, unlock it, tap Trust."; exit 1; }
fi

# iphone.sh reads this line as "plumesign was asked" (its hourly re-sign limit): keep the wording
echo "signing + installing WebDriverAgent - keep the iPhone unlocked (about a minute)..."
rm -f "$SIGNED"
rc=0
run_child timeout 600 "$BIN/plumesign" sign -p "$IPA" --apple-id --register-and-install --udid "$DEV" -o "$SIGNED" || rc=$?
private_plume   # signing can add a certificate and key there
if [ $rc -ne 0 ] || ! grep -q "Installation complete" "$LOG" || [ ! -s "$SIGNED" ]; then
  errs=$(grep -vE 'DEBUG|apple_codesign|Restoring session' "$LOG" | grep -iE 'error|fail|denied|invalid|locked' | tail -5) || true
  printf '%s\n' "$errs" | redact
  rm -f "$SIGNED"
  if [ $rc -eq 124 ]; then
    echo "plumesign did not finish in 10 minutes. Full log: $LOG"
  elif grep -qiE 'DeviceLocked|device is locked|passcode' <<<"$errs"; then
    echo "The iPhone is locked - unlock it and run $0 again."; exit 4
  elif grep -qiE 'No account|not logged|session|token|unauthori[sz]ed|\b401\b|-20101|-22406|reauth|password' <<<"$errs"; then
    echo "Apple ID session expired - $LOGIN_HINT"; exit 3
  else
    echo "signing/installing failed. Full log: $LOG"
  fi
  exit 1
fi

out=$(record "$SIGNED") || { rm -f "$SIGNED"; exit 1; }
rm -f "$SIGNED" "$OLD_SIGNED"
say_installed "$out"
echo "First time only: on the iPhone, Settings > General > VPN & Device Management > your Apple ID > Trust."
