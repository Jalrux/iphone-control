#!/usr/bin/env bash
# Mirror and control an iPhone over USB. See ./iphone.sh --help.
# Full mode (WebDriverAgent) needs ./setup.sh and ./resign.sh done once and Developer Mode; basic mode
# (Bluetooth input, USB or AirPlay picture) needs ./setup.sh --basic. The iPhone plugged in, trusting this computer.
set -u
umask 077
# go-ios: no agent spawned behind our back by every ios call, no stray device or host override
unset ENABLE_GO_IOS_AGENT udid GO_IOS_AGENT_HOST
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
IOS="$BIN/ios"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/iphone-control"
RUN="${XDG_RUNTIME_DIR:-$STATE/run}/iphone-control"   # never a shared /tmp dir
WDA_PORT="${IPHONE_WDA_PORT:-8100}"
MJPEG_PORT="${IPHONE_MJPEG_PORT:-9100}"
WDA_BIND="${IPHONE_WDA_BIND-127.0.0.1}"   # WebDriverAgent's USE_IP; empty = also the phone's Wi-Fi address
export GO_IOS_AGENT_PORT="${GO_IOS_AGENT_PORT:-60105}"
AGENT="http://127.0.0.1:$GO_IOS_AGENT_PORT"
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}" no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/iphone-control.desktop"
MODE_WANT="${IPHONE_MODE:-auto}" VIDEO_WANT="${IPHONE_VIDEO:-auto}"
SYSFS="${IPHONE_SYSFS:-/sys/bus/usb/devices}"   # tests point it at a fake tree
PROC="${IPHONE_PROC:-/proc}"                    # likewise (to find the running usbmuxd)
MUX_BIN=/usr/local/sbin/usbmuxd-iphone-control   # what ./setup.sh --usbmuxd installs
MUX_DROPIN=/etc/systemd/system/usbmuxd.service.d/iphone-control.conf
MUX_DIRS="/usr/sbin /usr/bin /sbin /bin /usr/local/sbin /usr/local/bin"   # the only usbmuxd (or MUX_BIN) run here
MUX_USERS="root usbmux usbmuxd"   # who runs usbmuxd: root, which it leaves for usbmux (usbmuxd on Fedora)
SYS_OWNER=root                    # who owns a program that status and doctor run, or a running usbmuxd's
H264_DECODERS="vah264dec avdec_h264 openh264dec"   # the window's order
H264_DEC=${IPHONE_DECODER:-}      # the window uses that one alone when it is set (trimmed, as it does)
H264_DEC=${H264_DEC#"${H264_DEC%%[![:space:]]*}"}
H264_DEC=${H264_DEC%"${H264_DEC##*[![:space:]]}"}
UDID='' VER='' MAJOR=0 WDA_STARTED=0 CAP_STARTED=0 FG='' HPY='' PY='' MODE='' WHY='' VIDEO='' NO_H264='' NO_MJPEG=''

usage() {
  cat <<EOF
Usage: ./iphone.sh [start | stop | status | doctor | --help]
  start    (default) start everything and open the "iPhone (USB)" window; closing it stops everything
  stop     stop the window, the screen capture, WebDriverAgent, the port forward and the tunnel
  status   what is running and installed, and when the WebDriverAgent signature runs out
  doctor   check every dependency and state file and print fixes (exit 1 if something is broken)

Environment:
  IPHONE_MODE=auto            full: WebDriverAgent input (go-ios, ./resign.sh, Developer Mode); basic: the
                              laptop is a Bluetooth mouse + keyboard (AssistiveTouch); auto: full when it can
  IPHONE_VIDEO=auto           usb: screen capture over the cable (a newer usbmuxd: ./setup.sh --usbmuxd);
                              mjpeg: WebDriverAgent's picture; airplay: uxplay; auto: usb when it starts,
                              else mjpeg (full mode) or airplay (basic mode, when uxplay is installed)
  IPHONE_UDID=<udid>          which iPhone, when several are plugged in (case and dashes are ignored)
  IPHONE_WDA_PORT=8100        local port for WebDriverAgent (taps, typing)
  IPHONE_MJPEG_PORT=9100      local port for the screen stream
  IPHONE_WDA_BIND=127.0.0.1   address WebDriverAgent listens on, on the phone ("" = also its Wi-Fi address)
  IPHONE_PYTHON=<path>        Python 3.9+ with PyGObject for the window (default: /usr/bin/python3)
  GO_IOS_AGENT_PORT=60105     port of the go-ios tunnel agent
  USBMUXD_SOCKET_ADDRESS=...  another usbmuxd: a socket path or a numeric IP:port (default /var/run/usbmuxd)
  Window: IPHONE_FAST_INPUT=1 (0 = WebDriverAgent's slower default waits), IPHONE_FPS=30,
          IPHONE_QUALITY=60 (JPEG), IPHONE_SCALE=100 (% of the phone's resolution; below 100
          WebDriverAgent re-encodes every frame and iOS may stop it), IPHONE_FIX_ORIENTATION=1 (0 = never
          rotate the picture), IPHONE_SCREENSHOT_DIR=<Pictures>/iPhone, IPHONE_DOUBLE_CLICK_MS=0
          (>0: hold a click back that long to send a double tap in one go), IPHONE_DECODER=<element>
          (an H.264 decoder for USB video), IPHONE_AUDIO=0 (1 = ask for the sound too; iOS 26 sends silence)
  Basic mode: IPHONE_HID_NAME=<Bluetooth name>, BT_INDEX=N (use hciN; default: the first LE adapter)
Ports are forwarded on 127.0.0.1 only. State: $STATE  Logs: $RUN
EOF
}

# the command first: --help and a typo change nothing on disk
CMD=${1:-start}
case $CMD in
  start|stop|status|doctor) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

for p in "$WDA_PORT" "$MJPEG_PORT" "$GO_IOS_AGENT_PORT"; do
  case $p in ''|*[!0-9]*) echo "bad port: '$p'"; exit 2 ;; esac
  if [ "$p" -lt 1 ] || [ "$p" -gt 65535 ]; then echo "bad port: $p"; exit 2; fi
done
[ "$WDA_PORT" != "$MJPEG_PORT" ] || { echo "IPHONE_WDA_PORT and IPHONE_MJPEG_PORT must differ"; exit 2; }
case $MODE_WANT in auto|full|basic) ;; *) echo "bad IPHONE_MODE: '$MODE_WANT' (auto, full or basic)"; exit 2 ;; esac
case $VIDEO_WANT in auto|usb|mjpeg|airplay) ;; *) echo "bad IPHONE_VIDEO: '$VIDEO_WANT' (auto, usb, mjpeg or airplay)"; exit 2 ;; esac
for v in BT_INDEX IPHONE_DOUBLE_CLICK_MS; do   # BT_INDEX ends up in a D-Bus object path
  case ${!v:-0} in ''|*[!0-9]*) echo "bad $v: '${!v}' (a number)"; exit 2 ;; esac
done
case $H264_DEC in *[!a-z0-9_]*) echo "bad IPHONE_DECODER: '$H264_DEC' (a GStreamer element, e.g. avdec_h264)"; exit 2 ;; esac
[ -z "$H264_DEC" ] || H264_DECODERS=$H264_DEC

if [ "$CMD" = start ] || [ "$CMD" = stop ]; then
  mkdir -p "$STATE" "$RUN" && chmod 700 "$STATE" "$RUN" || exit 1
  [ -O "$RUN" ] || { echo "$RUN is not owned by you"; exit 1; }
  cd "$STATE" || exit 1   # go-ios falls back to the working directory for its tunnel key (passed explicitly below)
  chmod -R go-rwx "$STATE" 2>/dev/null   # selfIdentity.plist holds a private key; older go-ios wrote it 0644
  # plumesign's Apple ID token and signing key: "account login" writes them with the login shell's umask
  [ "$CMD" = stop ] || chmod -R go-rwx "${XDG_CONFIG_HOME:-$HOME/.config}/PlumeImpactor" 2>/dev/null
else
  # status and doctor only read: no dirs made, no modes changed; go-ios gets a CWD it cannot write keys into
  cd "$STATE" 2>/dev/null || cd / || exit 1
fi

TTY=0
if [ -t 1 ] || [ -t 2 ]; then TTY=1; fi

# --- messages ----------------------------------------------------------------------------------------

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$RUN/iphone.log" 2>/dev/null; }
note() { log "$*"; if [ "$TTY" = 1 ]; then echo "$*"; fi; }   # progress: terminal only
say() {  # what the user must see: the terminal, else a desktop notification (or a dialog)
  log "$*"
  if [ "$TTY" = 1 ]; then echo "$*"; return; fi
  notify-send -a "iPhone Control" -i phone "iPhone Control" "$*" 2>/dev/null && return
  gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications \
    --method org.freedesktop.Notifications.Notify "iPhone Control" 0 phone "iPhone Control" "${*//\"/}" \
    '[]' '{}' 8000 >/dev/null 2>&1 && return
  if command -v zenity >/dev/null; then zenity --info --no-markup --title="iPhone Control" --text="$*" >/dev/null 2>&1 9>&- &
  elif command -v kdialog >/dev/null; then kdialog --title "iPhone Control" --passivepopup "$*" 8 >/dev/null 2>&1 9>&- &
  fi
}
redact() {  # UDIDs, Apple ID and team id out of anything shown
  sed -E 's/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}/<udid>/g; s/\b[0-9a-f]{40}\b/<udid>/g; s/\b[0-9A-Fa-f]{24}\b/<udid>/g
          s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/<apple-id>/g; s/(xctrunner)\.[A-Z0-9]{10}/\1.<team id>/g'
}
lasterr() {  # lasterr LOG: its last go-ios error or warning as "msg: err", else its last line
  local line
  line=$(grep -E '"level":"(ERROR|WARN)"|[Ee]rror' "$1" 2>/dev/null | tail -1)
  [ -n "$line" ] || line=$(tail -1 "$1" 2>/dev/null)
  printf '%s\n' "$line" | sed -E 's/.*"msg":"([^"]*)".*"(err|error)":"([^"]*)".*/\1: \3/; s/.*"msg":"([^"]*)".*/\1/' |
    cut -c1-200 | redact
}
fmt_date() { date -d "@$1" '+%a %d %b %Y %H:%M' 2>/dev/null || echo "@$1"; }

# --- helpers -----------------------------------------------------------------------------------------

find_python() {  # find_python [gi]: a Python 3.9+ (with GTK 3 + GStreamer bindings for "gi")
  local p check='import sys
if sys.version_info < (3, 9): sys.exit(1)'
  [ "${1:-}" = gi ] && check="$check"'
import gi; gi.require_version("Gtk", "3.0"); gi.require_version("Gst", "1.0")'
  for p in ${IPHONE_PYTHON:+"$IPHONE_PYTHON"} /usr/bin/python3 python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -I -c "$check" >/dev/null 2>&1; then command -v "$p"; return 0; fi
  done
  return 1
}
HPY=$(find_python) || HPY=
python_note() {  # python_note PY: why IPHONE_PYTHON was not used for the window (empty when it was, or unset)
  local want
  [ -n "${IPHONE_PYTHON:-}" ] || return 0
  want=$(command -v "$IPHONE_PYTHON" 2>/dev/null) || { echo "IPHONE_PYTHON=$IPHONE_PYTHON was not found"; return 0; }
  [ "$want" = "${1:-}" ] || echo "IPHONE_PYTHON=$IPHONE_PYTHON is older than 3.9 or lacks PyGObject (GTK 3, GStreamer)"
}

GST_ELEMENTS="souphttpsrc multipartdemux jpegdec videoconvert gtksink"   # WebDriverAgent's picture (MJPEG)
GST_USB="appsrc h264parse h264dec videoconvert gtksink"                 # the capture socket (H.264)
GST_AIRPLAY="udpsrc rtph264depay h264parse h264dec videoconvert gtksink"  # uxplay's RTP stream
gst_missing() {  # gst_missing PY [ERRLOG [ELEMENT...]]: those (default GST_ELEMENTS) PY cannot find; 1 when the
  # probe fails. "h264dec" is any of H264_DECODERS (IPHONE_DECODER alone, when set). stderr (registry rebuild
  # warnings, plugins that fail to load) goes to ERRLOG, never into the result
  local py=$1 err=${2:-/dev/null}
  shift $(($# < 2 ? $# : 2))
  # shellcheck disable=SC2086
  [ $# -gt 0 ] || set -- $GST_ELEMENTS
  "$py" -I -c 'import gi, sys; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None)
dec = sys.argv[1].split()
have = lambda e: any(Gst.ElementFactory.find(d) for d in dec) if e == "h264dec" else Gst.ElementFactory.find(e)
print(" ".join(e for e in sys.argv[2:] if not have(e)))' "$H264_DECODERS" "$@" 2>"$err"
}
gst_hint() {  # gst_hint MISSING...: which packages bring them
  local h="install the GStreamer base + good plugins and gtksink (gstreamer1.0-gtk3 on Debian/Ubuntu, gstreamer1-plugins-good-gtk on Fedora, gstreamer-plugins-good-gtk on openSUSE, gst-plugin-gtk on Arch)"
  case " $* " in *" souphttpsrc "*)
    h="$h; souphttpsrc also needs libsoup 3 (libsoup-3.0-0 / libsoup3 / libsoup-3_0-0 / libsoup3)" ;;
  esac
  case " $* " in *" h264parse "*|*" h264dec "*|*" rtph264depay "*)
    h="$h; H.264 needs the bad plugins and a decoder (gstreamer1.0-plugins-bad gstreamer1.0-libav / gstreamer1-plugins-bad-free gstreamer1-plugin-openh264 / gstreamer-plugins-bad gstreamer-plugin-openh264 / gst-plugins-bad gst-libav)${H264_DEC:+, or unset IPHONE_DECODER}" ;;
  esac
  echo "$h - ./setup.sh --check names the packages for this distro"
}
gst_names() {  # gst_names ELEMENT...: "a, b, an H.264 decoder (...)" for messages
  local e out=''
  for e in "$@"; do
    if [ "$e" = h264dec ] && [ -n "$H264_DEC" ]; then e="the H.264 decoder IPHONE_DECODER=$H264_DEC"
    elif [ "$e" = h264dec ]; then e="an H.264 decoder (${H264_DECODERS// /, })"; fi
    out+="${out:+, }$e"
  done
  echo "$out"
}
only() { local w kept=(); for w in $2; do [[ " $1 " != *" $w "* ]] || kept+=("$w"); done; echo "${kept[*]:-}"; }   # only KEEP WORDS

udid_key() { local u=${1//-/}; printf '%s' "${u,,}"; }
pick_udid() {  # pick_udid WANT <<<UDIDS: the UDID in the list that is WANT, ignoring case and dashes
  local want u
  want=$(udid_key "$1")
  while read -r u; do
    [ -n "$u" ] && [ "$(udid_key "$u")" = "$want" ] && { echo "$u"; return 0; }
  done
  return 1
}

desktop_unescape() {  # Desktop Entry string escapes: \s \n \t \r \\ (and \X = X)
  local s=$1 out='' c
  while [ -n "$s" ]; do
    c=${s:0:1} s=${s:1}
    if [ "$c" = "\\" ] && [ -n "$s" ]; then
      case ${s:0:1} in s) c=' ' ;; n) c=$'\n' ;; t) c=$'\t' ;; r) c=$'\r' ;; *) c=${s:0:1} ;; esac
      s=${s:1}
    fi
    out+=$c
  done
  printf '%s' "$out"
}
launcher_path() {  # launcher_path FILE: the program a .desktop entry starts (TryExec, else Exec's first argument)
  local v s out='' c
  v=$(sed -n -E 's/^TryExec[[:space:]]*=[[:space:]]*//p' "$1" | head -1)
  if [ -n "$v" ]; then desktop_unescape "$v"; return; fi
  v=$(sed -n -E 's/^Exec[[:space:]]*=[[:space:]]*//p' "$1" | head -1)
  s=$(desktop_unescape "$v")
  s=${s//%%/%}
  if [ "${s:0:1}" != '"' ]; then printf '%s' "${s%% *}"; return; fi
  s=${s:1}   # a quoted argument: \" \` \$ \\ stand for the character, it ends at the next plain "
  while [ -n "$s" ]; do
    c=${s:0:1} s=${s:1}
    if [ "$c" = "\\" ] && [ -n "$s" ]; then c=${s:0:1} s=${s:1}
    elif [ "$c" = '"' ]; then break; fi
    out+=$c
  done
  printf '%s' "$out"
}

# main() of iphone_control.MODULE in isolated mode (-I: no CWD, PYTHONPATH or user site on sys.path)
PYMOD='import importlib, sys; sys.path.insert(0, sys.argv[1]); sys.exit(importlib.import_module("iphone_control." + sys.argv[2]).main(sys.argv[3:]))'
pymod() { "$HPY" -I -c "$PYMOD" "$HERE" "$@"; }   # pymod MODULE ARGS...
# ...and for the capture, which also leaves its exit status in $IPHONE_CAP_STATUS (none: it crashed or was killed):
# the watchdog reads it there, as it cannot wait for a capture this shell started
CAPMOD='import importlib, os, sys; sys.path.insert(0, sys.argv[1]); rc = importlib.import_module("iphone_control." + sys.argv[2]).main(sys.argv[3:]) or 0
with open(os.environ["IPHONE_CAP_STATUS"], "w") as f: f.write("%d\n" % rc)
sys.exit(rc)'
phones() { pymod usbmux list 2>/dev/null | cut -f2; }   # UDIDs on USB, one per line
mux_up() {  # is usbmuxd reachable?
  if [ -n "${USBMUXD_SOCKET_ADDRESS:-}" ]; then pymod usbmux list >/dev/null 2>&1; else [ -S /var/run/usbmuxd ]; fi
}
sys_program() {  # sys_program PATH: an executable only SYS_OWNER can change (its file, not group/other-writable)
  local o m
  read -r o m <<<"$(stat -L -c '%U %a' -- "$1" 2>/dev/null)"
  [ "${o:-}" = "$SYS_OWNER" ] && [[ ${m:-} =~ ^[0-7]+$ ]] && [ $((8#$m & 8#022)) = 0 ] && [ -x "$1" ]
}
usbmuxd_running() {  # the program of a running usbmuxd (any user's: its cmdline is readable, its exe link is not).
                     # Anyone can start a process called usbmuxd, so only one that root or usbmuxd's user runs from
                     # a program only root can change counts
  local p a
  for p in "$PROC"/[0-9]*; do
    case $(cat "$p/comm" 2>/dev/null) in usbmuxd|usbmuxd-iphone-) ;; *) continue ;; esac   # comm: 15 characters
    [[ " $MUX_USERS " == *" $(stat -c %U "$p" 2>/dev/null) "* ]] || continue
    a=$(tr '\0' '\n' <"$p/cmdline" 2>/dev/null | head -1)
    [[ $a == /* ]] && sys_program "$a" && { echo "$a"; return 0; }
  done
  return 1
}
usbmuxd_unchecked() {  # the program of a usbmuxd that usbmuxd's user runs from a program root does not own (a
                       # hand-built test copy): named, never run, and not mistaken for the distro's
  local p a
  usbmuxd_running >/dev/null && return 1
  for p in "$PROC"/[0-9]*; do
    case $(cat "$p/comm" 2>/dev/null) in usbmuxd|usbmuxd-iphone-) ;; *) continue ;; esac
    [[ " $MUX_USERS " == *" $(stat -c %U "$p" 2>/dev/null) "* ]] || continue
    a=$(tr '\0' '\n' <"$p/cmdline" 2>/dev/null | head -1)
    [[ $a == /* ]] && { echo "$a"; return 0; }
  done
  return 1
}
usbmuxd_bin() {  # the daemon itself: the one running, else ours when ./setup.sh --usbmuxd put it in, else the
                 # distro's (sbin is often not on a user's PATH)
  local d
  usbmuxd_running && return 0
  if [ -x "$MUX_BIN" ] && grep -qsF "ExecStart=$MUX_BIN" "$MUX_DROPIN"; then echo "$MUX_BIN"; return 0; fi
  command -v usbmuxd 2>/dev/null && return 0
  for d in $MUX_DIRS; do [ -x "$d/usbmuxd" ] && { echo "$d/usbmuxd"; return 0; }; done
  return 1
}
mux_runnable() {  # mux_runnable PATH: a usbmuxd whose --version this runs: ours, or one in MUX_DIRS, root's alone
  [ "$1" = "$MUX_BIN" ] || { [ "${1##*/}" = usbmuxd ] && [[ " $MUX_DIRS " == *" ${1%/*} "* ]]; } || return 1
  sys_program "$1"
}
usbmuxd_installed() { usbmuxd_bin >/dev/null; }
apple_on_usb() {  # an iPhone/iPad in USB mux mode per sysfs, whether or not usbmuxd runs (the udev rule's ids)
  local d p
  for d in "$SYSFS"/*; do
    [ "$(cat "$d/idVendor" 2>/dev/null)" = 05ac ] || continue
    p=$(cat "$d/idProduct" 2>/dev/null)
    case $p in 12[9a][0-9a-f]|8600) return 0 ;; esac
  done
  return 1
}
# systemd distros: udev starts usbmuxd when an iPhone is plugged in and stops it when the last one goes
udev_starts_usbmuxd() { compgen -G '/usr/lib/udev/rules.d/*usbmuxd*' || compgen -G '/lib/udev/rules.d/*usbmuxd*' || compgen -G '/etc/udev/rules.d/*usbmuxd*'; } >/dev/null
mux_down_why() {  # usbmuxd is down: "idle" (normal with no phone plugged in), "noudev", "missing", "down"
  if [ -n "${USBMUXD_SOCKET_ADDRESS:-}" ]; then echo down
  elif ! usbmuxd_installed; then echo missing
  elif apple_on_usb; then echo down
  elif udev_starts_usbmuxd; then echo idle
  else echo noudev; fi
}
usb_config() {  # usb_config UDID: "CONFIGS CURRENT" of the phone's USB device in sysfs (1: not found)
  local d n c want
  want=$(udid_key "$1")
  for d in "$SYSFS"/*; do
    [ "$(cat "$d/idVendor" 2>/dev/null)" = 05ac ] || continue
    [ "$(udid_key "$(cat "$d/serial" 2>/dev/null)")" = "$want" ] || continue
    n=$(cat "$d/bNumConfigurations" 2>/dev/null) c=$(cat "$d/bConfigurationValue" 2>/dev/null)
    echo "${n//[!0-9]/} ${c//[!0-9]/}"
    return 0
  done
  return 1
}
# Measured (iPhone 15 Pro, iOS 26.5.2, Oct 2026): the capture interface exists only in configuration 5, which the
# phone adds after a vendor request. usbmuxd 1.1.1 then forces configuration 4; newer ones keep 5 when started
# with USBMUXD_DEFAULT_DEVICE_MODE=2 (./setup.sh --usbmuxd).
capture_state() {  # capture_state UDID: "STATE CURRENT": ready | idle (unconfigured: usbmuxd is not running) |
                   # old (5 configurations, usbmuxd keeps another) | off (4: nobody asked for 5) | none (not in sysfs)
  local n c
  read -r n c <<<"$(usb_config "$1")"
  if [ -z "${n:-}" ]; then echo none
  elif [ "${c:-0}" = 0 ]; then echo idle
  elif [ "$n" -ge 5 ] && [ "$c" = 5 ]; then echo ready
  elif [ "$n" -ge 5 ]; then echo "old $c"
  else echo "off $c"; fi
}
capture_text() {  # capture_text UDID: the USB video state in words, and the fix after " - "
  local s c
  read -r s c <<<"$(capture_state "$1")"
  case $s in
    ready) echo "ready (the iPhone is in its capture USB configuration, 5 of 5)" ;;
    idle) echo "the iPhone's USB is not configured (usbmuxd is not running)" ;;
    old) echo "usbmuxd keeps the iPhone in USB configuration $c, without the capture interface (usbmuxd 1.1.1 does) - ./setup.sh --usbmuxd" ;;
    off) echo "usbmuxd did not switch on the iPhone's capture configuration (it has 4) - ./setup.sh --usbmuxd" ;;
    *) echo "the iPhone is not in $SYSFS" ;;
  esac
}
usbmuxd_version() {  # empty for one this does not run (mux_runnable)
  mux_runnable "$1" || return 0
  timeout 5 "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+[^ ]*' | head -1
}
mux_too_old() { [ "$(printf '%s\n%s\n' "$1" 1.1.1 | sort -V | tail -1)" = 1.1.1 ]; }   # 1.1.1 and older
uxplay_version() { timeout 5 uxplay -v 2>&1 | grep -ioE 'uxplay[^0-9]{0,10}[0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1; }
uxplay_new() { [ -n "$1" ] && [ "$(printf '%s\n1.73\n' "$1" | sort -V | head -1)" = 1.73 ]; }   # uxplay_new VERSION
uxplay_ok() { uxplay_new "$(uxplay_version)"; }   # the window's AirPlay picture needs 1.73+ (-vrtp)
uxplay_found() {  # for messages: "not installed", "found 1.71"
  local v
  command -v uxplay >/dev/null 2>&1 || { echo "not installed"; return; }
  v=$(uxplay_version); echo "found ${v:-an unknown version}"
}
bt_adapters() {  # BlueZ's adapters that can host GATT services and advertise over LE (hci0 ...): what basic mode
                 # needs. 1 when bluetoothd does not answer, 2 when nothing here can ask it
  local out a v le=()
  if command -v busctl >/dev/null; then
    # --auto-start=no: asking must not start bluetoothd
    out=$(timeout 5 busctl --system --auto-start=no --list tree org.bluez 2>/dev/null) || return 1
    while read -r a; do
      v=$(timeout 5 busctl --system --auto-start=no introspect org.bluez "$a" 2>/dev/null)
      grep -q '^org\.bluez\.GattManager1 ' <<<"$v" && grep -q '^org\.bluez\.LEAdvertisingManager1 ' <<<"$v" && le+=("${a##*/}")
    done < <(grep -E '^/org/bluez/hci[0-9]+$' <<<"$out")
  elif command -v bluetoothctl >/dev/null; then
    out=$(timeout 5 bluetoothctl show 2>/dev/null) || return 1   # waits forever without bluetoothd
    grep -q 'SupportedInstances' <<<"$out" && le+=("the default adapter")
  else
    return 2
  fi
  echo "${le[*]}"
}
devmode_state() {  # on | off | unknown (go-ios reads it over lockdown; no tunnel needed)
  local v
  v=$(timeout 20 "$IOS" devmode get 2>/dev/null)
  if grep -q '"DeveloperModeEnabled": *true' <<<"$v"; then echo on
  elif grep -q '"DeveloperModeEnabled": *false' <<<"$v"; then echo off
  else echo unknown; fi
}
old_ios() { case $1 in 17.[0-3]|17.[0-3].*|17.4|17.4.0) return 0 ;; esac; return 1; }   # no rootless tunnel
resolve_mode() {  # resolve_mode [DEVMODE]: MODE (full | basic) and WHY from IPHONE_MODE and what is installed;
                  # DEVMODE (on | off | unknown) is the phone's Developer Mode, when it was asked
  MODE=full
  case $MODE_WANT in
    full) WHY="IPHONE_MODE=full"; return ;;
    basic) MODE=basic WHY="IPHONE_MODE=basic"; return ;;
  esac
  if [ ! -x "$IOS" ]; then MODE=basic WHY="go-ios is not installed; ./setup.sh adds full mode"
  elif [ -z "$(cfg wda_bundle)" ]; then MODE=basic WHY="WebDriverAgent is not installed; ./resign.sh adds full mode"
  elif old_ios "$VER"; then MODE=basic WHY="iOS $VER is too old for full mode's rootless tunnel (17.4.1 or newer)"
  elif [ "${1:-}" = off ]; then MODE=basic WHY="Developer Mode is off; Settings > Privacy & Security > Developer Mode turns on full mode"
  elif [ "${1:-}" = on ]; then WHY="go-ios, WebDriverAgent and Developer Mode are in place"
  elif [ "${1:-}" = unknown ]; then WHY="go-ios and WebDriverAgent are installed; Developer Mode could not be read"
  else WHY="go-ios and WebDriverAgent are installed"; fi
}
mode_line() {  # what the user is told once: "Full mode (why)"
  if [ "$MODE" = full ]; then echo "Full mode: WebDriverAgent input ($WHY)"
  else echo "Basic mode: Bluetooth input through AssistiveTouch ($WHY)"; fi
}

phone_query() {  # phone_query version|paired: iOS version (lockdown, no pairing needed) / pair record present?
  "$HPY" -I - "$HERE" "$UDID" "$1" 2>/dev/null <<'EOF'
import plistlib, struct, sys
sys.path.insert(0, sys.argv[1])
from iphone_control import usbmux
udid, what = sys.argv[2], sys.argv[3]
if what == "paired":
    sys.exit(0 if usbmux.request({"MessageType": "ReadPairRecord", "PairRecordID": udid}).get("PairRecordData") else 1)
dev = usbmux.device_id(udid)
if dev is None:
    sys.exit(1)
s = usbmux.connect(dev, 62078)
s.settimeout(5)
body = plistlib.dumps({"Label": "iphone-control", "Request": "GetValue", "Key": "ProductVersion"})
s.sendall(struct.pack(">I", len(body)) + body)
def recv(n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            sys.exit(1)
        buf += chunk
    return buf
print(plistlib.loads(recv(struct.unpack(">I", recv(4))[0])).get("Value", ""))
EOF
}

cfg() {  # cfg KEY: a value from config.json, empty when missing
  "$HPY" -I -c 'import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except (OSError, ValueError, AttributeError):
    print("")' "$STATE/config.json" "$1" 2>/dev/null
}

bg() {  # bg NAME CMD...: a background process; pid in $RUN/NAME.pid, output in $RUN/NAME.log
  local name=$1 pid; shift
  nohup "$@" </dev/null >"$RUN/$name.log" 2>&1 9>&- &
  pid=$!
  echo $pid >"$RUN/$name.pid"
  # until it has exec'd, the child's cmdline is still this script's, and ours() would call it gone. Usually a few
  # ms; up to 5 s on a loaded machine (a 1 s limit was too short when the whole test suite ran on a busy laptop)
  local i
  for ((i = 0; i < 100; i++)); do
    if ours "$name" "$pid" || [ ! -d "/proc/$pid" ]; then return 0; fi
    sleep 0.05
  done
}
fg_run() {  # run a long command so that a signal interrupts the wait (cleanup kills it)
  "$@" 9>&- &
  FG=$!
  wait "$FG"
  local rc=$?
  FG=
  return $rc
}
stop_fg() {  # end fg_run's command: env execs timeout, which leads its own process group (resign.sh, plumesign, ios)
  [ -n "$FG" ] || return 0
  kill -- -"$FG" 2>/dev/null || kill "$FG" 2>/dev/null
  FG=
}
ours() {  # ours NAME PID: is PID still the process we started as NAME (and not a recycled pid)?
  local cmd
  [ -O "/proc/$2" ] || return 1
  cmd=$(tr '\0' ' ' 2>/dev/null <"/proc/$2/cmdline") || return 1
  case $1 in
    iphone|watchdog) [[ $cmd == *iphone.sh* ]] ;;
    window) [[ $cmd == *wda_mirror.py* ]] ;;
    fwd) [[ $cmd == *" $HERE forward "* ]] ;;
    cap) [[ $cmd == *" $HERE screencap "* ]] ;;
    uxplay) [[ $cmd == *uxplay* ]] ;;   # the window's AirPlay receiver (the window writes its pid)
    *) [[ $cmd == *"$IOS "* ]] ;;
  esac
}
pid_of() {  # pid_of NAME: the live pid of our NAME process
  local p
  p=$(cat "$RUN/$1.pid" 2>/dev/null) || return 1
  [[ $p =~ ^[0-9]+$ ]] && ours "$1" "$p" && echo "$p"
}
alive() { pid_of "$1" >/dev/null; }
stop_one() {  # stop_one NAME [SECONDS]: TERM, wait up to SECONDS (3), then KILL
  local p i
  if p=$(pid_of "$1"); then
    kill "$p" 2>/dev/null
    for ((i = 0; i < ${2:-3} * 5; i++)); do ours "$1" "$p" || break; sleep 0.2; done
    if ours "$1" "$p"; then kill -9 "$p" 2>/dev/null; fi
  fi
  rm -f "$RUN/$1.pid"
}
stop_all() {  # the watchdog first, so it cannot restart anything during teardown
  local n
  # uxplay after the window, which stops its own: this one outlived a window that crashed or was killed, and still
  # takes AirPlay's fixed ports and announces itself on the network
  for n in watchdog window uxplay wda fwd fwd8100 fwd9100 tunnel; do stop_one "$n"; done
  # the capture last: on TERM it ends its session with the phone (up to ~4 s), or the next one gets no picture
  stop_one cap 6
  rm -f "$RUN/cap.sock"
}

# --- the pieces --------------------------------------------------------------------------------------

# The USB screen capture (iphone_control/screencap.py): H.264 over the cable, no Developer Mode needed. It stays
# up for the whole run; the window connects to its socket (and reconnects) as often as it likes.
start_cap() {
  local audio=
  [ "${IPHONE_AUDIO:-0}" != 1 ] || audio=1   # iOS 26.5.2 sends silence and keeps the sound on the phone
  [ -f "$RUN/cap.log" ] && mv -f "$RUN/cap.log" "$RUN/cap.prev.log"
  rm -f "$RUN/cap.sock" "$RUN/cap.status"
  CAP_STARTED=$SECONDS
  bg cap env IPHONE_CAP_STATUS="$RUN/cap.status" "$HPY" -I -c "$CAPMOD" "$HERE" screencap --udid "$UDID" \
    --socket "$RUN/cap.sock" ${audio:+"--audio"}
}
cap_up() {  # cap_up SECONDS: 0 once it prints "capturing", else its exit status (screencap.py: 1 error,
            # 2 unplugged, 3 unsupported here, 4 no picture), 124 when no picture came in time (it is stopped)
  local pid rc end=$((SECONDS + $1))
  pid=$(cat "$RUN/cap.pid" 2>/dev/null) || return 1
  while :; do
    grep -qx capturing "$RUN/cap.log" 2>/dev/null && return 0
    if ! alive cap; then
      wait "$pid" 2>/dev/null; rc=$?
      [ $rc -ne 127 ] || rc=1   # not our child
      return $rc
    fi
    [ $SECONDS -lt $end ] || break
    sleep 0.2
  done
  stop_one cap 6
  return 124
}
cap_resets() {  # how often the running capture reset the phone's USB (screencap logs each one)
  local n
  n=$(grep -c "^screencap: resetting the iPhone's USB" "$RUN/cap.log" 2>/dev/null) || n=0
  echo "$n"
}
phone_back() {  # phone_back SECONDS: wait until usbmuxd lists the phone again (after a USB reset or a replug)
  local end=$((SECONDS + $1))
  until phones | grep -Fqix "$UDID"; do
    [ $SECONDS -lt $end ] || return 1
    sleep 0.5
  done
}
capture() {  # the capture, waiting (2 minutes) while the phone is locked or unplugged: 0 capturing, else cap_up's,
             # or 5: usbmuxd lists the phone, but this computer's USB does not have it
  local rc told='' end=$((SECONDS + 120))
  while :; do
    start_cap
    cap_up 25; rc=$?
    case $rc in
      0) break ;;
      4) [ $SECONDS -lt $end ] || return 4
         [ "$told" = locked ] || { say "Unlock the iPhone - waiting for it..."; told=locked; }
         sleep 2 ;;
      2) [ $SECONDS -lt $end ] || return 2
         # it looks for the phone in this computer's sysfs, which loses an unplugged phone a moment before
         # usbmuxd does: listed there a second later but not here is another machine's usbmuxd
         # (USBMUXD_SOCKET_ADDRESS), whose phone this computer cannot capture
         sleep 1
         if phones | grep -Fqix "$UDID" && ! usb_config "$UDID" >/dev/null; then return 5; fi
         [ "$told" = gone ] || { say "iPhone unplugged - plug it back in."; told=gone; }
         phone_back $((end - SECONDS)) || return 2 ;;
      *) return $rc ;;
    esac
  done
  # its USB reset (a new session needs one) re-enumerates the phone: usbmuxd lists it again a moment later
  phone_back 20 || return 2
}
# shellcheck disable=SC2086
pick_video() {  # VIDEO for the window: the USB capture when IPHONE_VIDEO allows it; when it cannot start and
                # IPHONE_VIDEO=auto, WebDriverAgent's picture in full mode, AirPlay in basic mode
  local rc why m
  VIDEO=$VIDEO_WANT
  case $VIDEO_WANT in mjpeg|airplay) return 0 ;; esac
  if [ -n "$NO_H264" ]; then
    why="GStreamer lacks $(gst_names $NO_H264): $(gst_hint $NO_H264)"
  else
    note "starting the USB screen capture..."
    capture; rc=$?
    if [ $rc -eq 0 ]; then VIDEO=usb; return 0; fi
    [ $rc -ne 2 ] || fail "The iPhone was unplugged - plug it in and start again."
    log "screen capture did not start ($rc): $(lasterr "$RUN/cap.log")"
    case $rc in
      3) why="it needs a newer usbmuxd - ./setup.sh --usbmuxd installs one" ;;
      4) why="the iPhone sends no picture - unlock it; if it stays black, restart the iPhone" ;;
      5) why="the iPhone is not on this computer's USB${USBMUXD_SOCKET_ADDRESS:+ (USBMUXD_SOCKET_ADDRESS: a usbmuxd on another machine)}" ;;
      124) why="no picture in 25 s. Log: $RUN/cap.log" ;;
      *) why="$(lasterr "$RUN/cap.log"). Log: $RUN/cap.log" ;;
    esac
  fi
  [ "$VIDEO_WANT" = auto ] || fail "USB video did not start: $why"
  if [ "$MODE" = full ]; then
    [ -z "$NO_MJPEG" ] ||
      fail "No USB video ($why), and WebDriverAgent's picture needs GStreamer's $(gst_names $NO_MJPEG) - $(gst_hint $NO_MJPEG)."
    VIDEO=mjpeg; say "No USB video ($why) - using WebDriverAgent's picture."
  elif ! uxplay_ok; then
    fail "No picture for basic mode: USB video did not start ($why), and AirPlay needs uxplay 1.73+ ($(uxplay_found))."
  elif m=$(gst_missing "$PY" /dev/null $GST_AIRPLAY) && [ -z "$m" ]; then
    VIDEO=airplay; say "No USB video ($why) - using AirPlay: on the iPhone, Control Centre > Screen Mirroring."
  else
    fail "No picture for basic mode: USB video did not start ($why), and AirPlay needs GStreamer's $(gst_names $m) - $(gst_hint $m)."
  fi
}

agent_up() { curl -fs --noproxy '*' -m 1 -o /dev/null "$AGENT/health"; }
tunnel_ok() { curl -fs --noproxy '*' -m 2 "$AGENT/tunnel/$UDID" 2>/dev/null | grep -q '"rsdPort": *[1-9]'; }
start_tunnel() {  # go-ios agent (userspace, no root), only for this phone; a running agent is reused
  agent_up && return 0
  bg tunnel "$IOS" tunnel start --userspace --pair-record-path="$STATE"
}
wait_tunnel() {  # wait_tunnel SECONDS: 0 up, 1 timed out, 2 our agent exited
  local end=$((SECONDS + $1)) told=0
  until tunnel_ok; do
    if [ -f "$RUN/tunnel.pid" ] && ! alive tunnel; then return 2; fi
    [ $SECONDS -lt $end ] || return 1
    if [ $told = 0 ] && [ $((end - SECONDS)) -lt $(($1 - 8)) ]; then
      say "Unlock the iPhone (tap Trust if it asks) - waiting for it..."; told=1
    fi
    sleep 1
  done
}

ca_bundle() {  # system CAs + Apple Root CA, rebuilt when stale (go-ios needs it for gs.apple.com)
  local ca
  if [ -x "$HERE/certs/ca-bundle.sh" ] && ca=$("$HERE/certs/ca-bundle.sh" "$STATE/ca-bundle-with-apple.pem" 2>"$RUN/ca.log"); then
    echo "$ca"; return 0
  fi
  [ -s "$STATE/ca-bundle-with-apple.pem" ] && { echo "$STATE/ca-bundle-with-apple.pem"; return 0; }
  return 1
}
mount_ddi() {  # developer disk image, once per phone restart: 0 mounted, 4 phone locked, 1 failed (image.log)
  local ca
  "$IOS" image list >"$RUN/image.log" 2>&1 && grep -q 'image signature' "$RUN/image.log" && return 0
  ca=$(ca_bundle) || { echo "CA bundle missing: $(cat "$RUN/ca.log" 2>/dev/null)" >>"$RUN/image.log"; return 1; }
  # "image auto" exits 0 even when the download fails, so success is read from its log
  fg_run env SSL_CERT_FILE="$ca" timeout 300 "$IOS" image auto --basedir="$STATE/ddi" >>"$RUN/image.log" 2>&1
  grep -qE 'success mounting image|already a developer image mounted' "$RUN/image.log" && return 0
  grep -q DeviceLocked "$RUN/image.log" && return 4
  return 1
}
ddi_ready() {  # ddi_ready SECONDS: mount, waiting that long for the phone to be unlocked
  local end=$((SECONDS + $1)) told=0 rc
  while :; do
    mount_ddi; rc=$?
    [ $rc -eq 4 ] && [ $SECONDS -lt $end ] || return $rc
    [ $told = 1 ] || { say "Unlock the iPhone - waiting for it..."; told=1; }
    sleep 3
  done
}

start_fwd() {  # [quiet] loopback-only forward: local WDA/MJPEG ports -> phone 8100/9100 (go-ios forward binds 0.0.0.0)
  bg fwd "$HPY" -I -c "$PYMOD" "$HERE" forward --udid "$UDID" "$WDA_PORT:8100" "$MJPEG_PORT:9100"
  for _ in $(seq 25); do
    grep -q '^forwarding' "$RUN/fwd.log" 2>/dev/null && return 0
    alive fwd || break
    sleep 0.2
  done
  alive fwd && return 0
  [ "${1:-}" = quiet ] && return 1
  if grep -q 'cannot listen' "$RUN/fwd.log" 2>/dev/null; then
    say "$(tail -1 "$RUN/fwd.log") - run ./iphone.sh stop, close the program using it$(port_owner "$WDA_PORT" "$MJPEG_PORT"), or set IPHONE_WDA_PORT / IPHONE_MJPEG_PORT"
  else
    say "Port forward failed: $(tail -1 "$RUN/fwd.log" 2>/dev/null | redact)"
  fi
  return 1
}
port_owner() {  # port_owner PORT...: " (program, ...)" listening on them, when ss can tell
  local p o n=()
  for p in "$@"; do
    o=$(ss -Hltnp "sport = :$p" 2>/dev/null | grep -o 'users:(("[^"]*' | cut -d'"' -f2 | head -1)
    [ -z "$o" ] || n+=("$o")
  done
  [ ${#n[@]} -eq 0 ] || (IFS=,; echo " (${n[*]})")
}

start_wda() {
  local b
  stop_one wda
  [ -f "$RUN/wda.log" ] && mv -f "$RUN/wda.log" "$RUN/wda.prev.log"
  b=$(cfg wda_bundle)
  WDA_STARTED=$SECONDS
  # --log-output=-: WebDriverAgent's own output lands in wda.log; USE_IP keeps it off the phone's Wi-Fi
  bg wda "$IOS" runwda --bundleid="$b" --testrunnerbundleid="$b" --xctestconfig=WebDriverAgentRunner.xctest \
    --log-output=- ${WDA_BIND:+"--env=USE_IP=$WDA_BIND"}
}
wda_status() { curl -fs --noproxy '*' -m 2 "http://127.0.0.1:$WDA_PORT/status"; }
ios_of() { tr -d '\n' | grep -o '"os" *: *{[^}]*}' | grep -o '"version" *: *"[^"]*"' | head -1 | cut -d'"' -f4; }   # /status JSON -> iOS version (not WDA's build.version)
wda_up() {  # wda_up SECONDS: 1 when it does not answer in time or runwda exits
  local end=$((SECONDS + $1))
  while [ $SECONDS -lt $end ]; do
    wda_status >/dev/null && return 0
    alive wda || return 1
    sleep 1
  done
  return 1
}
wda_failure() {  # why WebDriverAgent did not start, from wda.log
  local f="$RUN/wda.log"
  if grep -qE 'cannot get (test )?app information' "$f" 2>/dev/null; then echo missing
  elif grep -qiE 'expired|ApplicationVerificationFailed|0xe8008018|0xe800801c|invalid code ?signature' "$f" 2>/dev/null; then echo expired
  elif grep -qiE 'not been explicitly trusted|untrusted|not trusted' "$f" 2>/dev/null; then echo untrusted
  elif grep -qiE 'locked' "$f" 2>/dev/null; then echo locked
  elif grep -qiE 'tunnel connection|failed to get tunnel info' "$f" 2>/dev/null; then echo tunnel
  elif alive wda; then echo silent
  else echo other; fi
}

expiring() {  # the signature runs out within an hour (unknown: no)
  local e
  e=$(cfg expires_at)
  [ -n "$e" ] && [ "$(date +%s)" -ge $((e - 3600)) ]
}
resign_now() {  # at most one automatic re-sign an hour (free Apple IDs are rate-limited)
  local rc
  if [ -n "$(find "$STATE/last-resign" -mmin -60 2>/dev/null)" ]; then
    say "Re-signed less than an hour ago and it still fails - run $HERE/resign.sh in a terminal to see why."
    return 1
  fi
  say "Re-signing WebDriverAgent with your Apple ID (about a minute, keep the iPhone unlocked)..."
  fg_run env IPHONE_UDID="$UDID" timeout 600 "$HERE/resign.sh" </dev/null >"$RUN/resign.log" 2>&1; rc=$?
  # the hour only counts once plumesign really asked Apple, and not when the user is told to fix something
  # and start again (3 log in, 4 unlock, 124 timed out)
  case $rc in 3|4|124) ;; *)
    if grep -q '^signing + installing' "$RUN/resign.log" 2>/dev/null; then touch "$STATE/last-resign"; fi ;;
  esac
  case $rc in
    0) return 0 ;;
    3) say "Apple ID session expired: in a terminal run  $BIN/plumesign account login -u <your Apple ID>  then start again." ;;
    4) say "The iPhone is locked - unlock it and start again." ;;
    124) say "Re-signing timed out. Log: $RUN/resign.log" ;;
    *) say "Re-signing failed: $(grep -v '^ *$' "$RUN/resign.log" | tail -1 | redact)" ;;
  esac
  return 1
}

watchdog() {  # while the window is open: restart the capture, the forward, the tunnel and WebDriverAgent when
              # they stop (basic mode: only the capture)
  trap - EXIT INT
  trap 'stop_fg; exit 0' TERM HUP   # stop_one's TERM also ends an image mount in flight
  local missed=0 gone=0 back=0 locked=0 fails=0 silent=0 wda=1 capfails=0 captold=0 unlock=0 again rc n \
        resetting=0 resets resetat=$CAP_STARTED
  resets=$(cap_resets)
  while sleep 3; do
    # A capture's USB reset takes the phone off usbmuxd for a few seconds, which is no unplug: a restarted one's,
    # and its own after a stalled picture, which it logs. One look can fall into that gap; an unplug lasts two.
    n=$(cap_resets)
    [ "$n" -le "$resets" ] || resetting=$((SECONDS + 15)) resetat=$SECONDS
    resets=$n
    if ! phones | grep -Fqix "$UDID"; then
      missed=$((missed + 1))
      [ $gone = 1 ] || [ $missed -lt 2 ] || [ $SECONDS -lt $resetting ] || { say "iPhone unplugged - plug it back in."; gone=1; }
      continue
    fi
    missed=0
    if [ $gone = 1 ]; then say "iPhone back - reconnecting..."; gone=0 back=1; fi
    if [ "$VIDEO" = usb ] && [ $capfails -lt 5 ] && ! alive cap; then
      rc=$(cat "$RUN/cap.status" 2>/dev/null) || rc=   # none: it crashed or was killed
      again=0
      if [ "$rc" = 4 ]; then
        # no picture: a stuck session (a locked phone keeps streaming), which is no failure. A new capture resets
        # the phone's USB, which also drops WebDriverAgent and the tunnel: so only after a replug, or every 30 s.
        # The capture's own last line says what to do (restart the iPhone after a session that broke mid-run)
        if [ $unlock = 0 ]; then
          if grep -q 'new captures do not bring it back' "$RUN/cap.log" 2>/dev/null; then
            say "The iPhone stopped sending its screen - if it stays black, restart the iPhone."
          else say "No picture from the iPhone - unlock it; if it stays black, restart the iPhone."; fi
          unlock=1
        fi
        if [ $back = 1 ] || [ $((SECONDS - CAP_STARTED)) -ge 30 ]; then again=1; fi
      elif [ "$rc" = 2 ]; then
        again=1   # it saw the phone go, and it is back
      else
        if [ $((SECONDS - CAP_STARTED)) -lt 60 ]; then capfails=$((capfails + 1)); else capfails=0; fi
        if [ $capfails -ge 5 ]; then
          say "USB video keeps stopping - close the window and start again (log: $RUN/cap.log)."
        else
          # after a replug a stopped capture is expected; otherwise the picture froze, so say it once
          [ $back = 1 ] || [ $captold = 1 ] || { say "USB video stopped - restarting it..."; captold=1; }
          again=1
        fi
      fi
      if [ $again = 1 ]; then
        log "watchdog: restarting the screen capture (it ended: ${rc:-killed})"
        start_cap
        resetting=$((SECONDS + 15)) resetat=$SECONDS resets=0
      fi
    elif [ $unlock = 1 ] && grep -qx capturing "$RUN/cap.log" 2>/dev/null; then
      unlock=0   # a picture again: the next lock is told again
    fi
    back=0
    [ "$MODE" = full ] && [ $wda = 1 ] || continue
    alive fwd || start_fwd quiet || continue
    if alive wda; then   # alive but deaf for 15 s (e.g. the cable was pulled): restart it
      if wda_status >/dev/null; then silent=0; continue; fi
      silent=$((silent + 1)); [ $silent -ge 5 ] || continue
    fi
    silent=0
    if [ "$MAJOR" -ge 17 ] && ! tunnel_ok; then
      [ -f "$RUN/tunnel.pid" ] && ! alive tunnel && start_tunnel
      continue
    fi
    mount_ddi; case $? in
      0) locked=0 ;;
      4) [ $locked = 1 ] || { say "Unlock the iPhone to reconnect."; locked=1; }; continue ;;
      *) continue ;;
    esac
    # giving up on WebDriverAgent leaves the capture watched
    if expiring; then say "The WebDriverAgent signature ran out - close the window and start again to re-sign."; wda=0; continue; fi
    if [ $resetat -ge $WDA_STARTED ]; then :   # a capture's USB reset ended it, no failure of its own
    elif [ $((SECONDS - WDA_STARTED)) -lt 60 ]; then fails=$((fails + 1)); else fails=0; fi
    if [ $fails -ge 5 ]; then say "WebDriverAgent keeps stopping - close the window and start again (log: $RUN/wda.log)."; wda=0; continue; fi
    log "watchdog: restarting WebDriverAgent"
    start_wda
    wda_up 30 || true
  done
}

# --- commands ----------------------------------------------------------------------------------------

take_lock() {  # take_lock -n | -w SECONDS
  command -v flock >/dev/null || return 0
  exec 9>>"$RUN/lock"
  flock "$@" 9
}
cleanup() {
  trap - EXIT INT TERM HUP
  stop_fg   # a re-sign or image mount in flight, with everything it started
  stop_all
  rm -f "$RUN/iphone.pid" "$RUN/mode"
}

cmd_stop() {
  local p
  if p=$(pid_of iphone); then kill "$p" 2>/dev/null; fi   # its trap stops everything (the capture takes up to 6 s)
  take_lock -w 15 || true
  stop_all
  rm -f "$RUN/iphone.pid" "$RUN/mode"
  echo "stopped"
}

fail() { say "$*"; exit 1; }

words() { printf '%s\n' "$@" | awk 'NF && !seen[$0]++' | paste -sd' ' -; }   # words W...: once each, in order
basic_ok() {  # what basic mode needs, once it is chosen
  [ "$VIDEO_WANT" != mjpeg ] ||
    fail "IPHONE_VIDEO=mjpeg is WebDriverAgent's picture, which basic mode does not have ($WHY) - use usb, airplay or auto."
  # shellcheck disable=SC2086
  [ -z "$NO_H264" ] || fail "GStreamer elements missing: $(gst_names $NO_H264) - $(gst_hint $NO_H264)."
}

start_full() {  # full mode: the signature, the developer tunnel, the disk image, the forward and WebDriverAgent
  local rc kind msg told='' resigned=0 deadline
  # 1. the signature: renew it before it runs out rather than after a failed start
  if [ -z "$(cfg expires_at)" ] && [ -f "$HERE/build/WebDriverAgent-signed.ipa" ]; then
    "$HERE/resign.sh" --record >>"$RUN/iphone.log" 2>&1   # older installs: note the expiry, drop the IPA
  fi
  if expiring; then
    note "The WebDriverAgent signature runs out $(fmt_date "$(cfg expires_at)")."
    resign_now || exit 1
    resigned=1
  fi

  # 2. iOS 17+ developer tunnel (userspace, no root)
  if [ "$MAJOR" -ge 17 ]; then
    old_ios "$VER" && fail "iOS $VER is too old for the rootless tunnel - update the iPhone to iOS 17.4.1 or newer."
    if ! tunnel_ok; then
      note "opening the developer tunnel..."
      start_tunnel
      wait_tunnel 60; rc=$?
      if [ $rc -eq 2 ]; then
        fail "The go-ios tunnel stopped: $(lasterr "$RUN/tunnel.log"). Log: $RUN/tunnel.log"
      elif [ $rc -ne 0 ] && agent_up && ! alive tunnel; then
        fail "Another go-ios agent on port $GO_IOS_AGENT_PORT has no tunnel for this iPhone - stop it ($IOS tunnel stopagent) or set GO_IOS_AGENT_PORT."
      elif [ $rc -ne 0 ]; then
        fail "Could not open the developer tunnel - unlock the iPhone, tap Trust, check Developer Mode. $(lasterr "$RUN/tunnel.log")"
      fi
    fi
  fi

  # 3. developer disk image (once per phone restart; the phone must be unlocked)
  note "mounting the developer disk image..."
  ddi_ready 120; rc=$?
  if [ $rc -eq 4 ]; then
    fail "The iPhone stayed locked - unlock it and start again."
  elif [ $rc -ne 0 ]; then
    if [ "$MAJOR" -ge 16 ] && timeout 20 "$IOS" devmode get 2>/dev/null | grep -q '"DeveloperModeEnabled": *false'; then
      fail "Turn on Developer Mode: Settings > Privacy & Security > Developer Mode, then restart the iPhone (missing? run: $IOS devmode reveal)."
    fi
    fail "Could not mount the developer disk image: $(lasterr "$RUN/image.log"). Log: $RUN/image.log"
  fi

  # 4. port forward + WebDriverAgent; wait for unlock / Trust, re-sign only when that is the cause
  start_fwd || exit 1
  note "starting WebDriverAgent..."
  start_wda
  deadline=$((SECONDS + 150))
  until wda_up 40; do
    kind=$(wda_failure)
    log "WebDriverAgent did not start ($kind): $(lasterr "$RUN/wda.log")"
    if [ "$kind" = missing ] || [ "$kind" = expired ] ||
       { [ "$kind" = other ] && [ -z "$(cfg expires_at)" ] && [ $SECONDS -ge $deadline ]; }; then
      [ $resigned = 0 ] || fail "WebDriverAgent still does not start after re-signing: $(lasterr "$RUN/wda.log"). Log: $RUN/wda.log"
      resign_now || exit 1
      resigned=1 deadline=$((SECONDS + 150))
    elif [ $SECONDS -ge $deadline ]; then
      case $kind in
        untrusted) fail "Trust the developer app: on the iPhone, Settings > General > VPN & Device Management > your Apple ID > Trust." ;;
        silent) fail "WebDriverAgent runs but does not answer on port $WDA_PORT - see $RUN/fwd.log; if it persists, try IPHONE_WDA_BIND= ./iphone.sh" ;;
        tunnel) fail "The developer tunnel dropped: $(lasterr "$RUN/tunnel.log"). Unplug and replug the iPhone, then start again." ;;
        *) fail "WebDriverAgent did not start: $(lasterr "$RUN/wda.log"). Unlock the iPhone and check Settings > General > VPN & Device Management > Trust. Log: $RUN/wda.log" ;;
      esac
    else
      case $kind in
        untrusted) msg="Trust the developer app: Settings > General > VPN & Device Management > your Apple ID > Trust - waiting..." ;;
        tunnel) msg="The developer tunnel is reconnecting - keep the iPhone unlocked..." ;;
        *) msg="Unlock the iPhone and keep it unlocked - waiting for WebDriverAgent..." ;;
      esac
      [ "$told" = "$msg" ] || { say "$msg"; told=$msg; }
      [ "$kind" = tunnel ] && wait_tunnel 20
      sleep 5
    fi
    start_wda
  done
  note "ready: iOS $(wda_status | ios_of)"
}

cmd_start() {
  local miss=() m need d rc told='' end
  # what start needs, before taking anything over; auto mode is basic without go-ios or WebDriverAgent
  resolve_mode
  if [ "$MODE" = full ]; then
    [ -x "$IOS" ] || miss+=("go-ios ($IOS)")
    command -v curl >/dev/null || miss+=(curl)
  fi
  [ -n "$HPY" ] || miss+=("Python 3.9+")
  [ -f "$HERE/iphone_control/usbmux.py" ] || miss+=("iphone_control/usbmux.py")
  [ ${#miss[@]} -eq 0 ] || fail "Not set up: ${miss[*]} missing - run $HERE/setup.sh$([ "$MODE" = full ] || echo " --basic")"
  [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ] || fail "No graphical session (neither WAYLAND_DISPLAY nor DISPLAY is set)."
  if ! PY=$(find_python gi); then
    m=$(python_note "")
    fail "No Python 3.9+ with PyGObject (GTK 3, GStreamer)${m:+ ($m)} - ./setup.sh --check names the packages, or set IPHONE_PYTHON."
  fi
  m=$(python_note "$PY"); [ -z "$m" ] || note "$m - using $PY"
  # GStreamer: MJPEG for WebDriverAgent's picture, H.264 for the USB capture and AirPlay. Full mode with
  # IPHONE_VIDEO=auto takes either (USB first, else MJPEG), so there one of them is enough; that mode may still
  # turn basic (Developer Mode off), which never uses MJPEG and checks H.264 then (basic_ok).
  case $VIDEO_WANT in
    usb) need=$GST_USB ;;
    mjpeg) need=$GST_ELEMENTS ;;
    airplay) need=$GST_AIRPLAY ;;
    *) if [ "$MODE" = full ]; then need="$GST_ELEMENTS $GST_USB"; else need=$GST_USB; fi ;;
  esac
  # shellcheck disable=SC2086
  need=$(words $need)
  # shellcheck disable=SC2086
  m=$(gst_missing "$PY" "$RUN/gst.log" $need) ||
    fail "$PY could not load GStreamer: $(grep -v '^ *$' "$RUN/gst.log" 2>/dev/null | tail -1 | cut -c1-200). Run ./iphone.sh doctor."
  if [ "$MODE" = full ] && [ "$VIDEO_WANT" = auto ]; then
    NO_H264=$(only "$GST_USB" "$m") NO_MJPEG=$(only "$GST_ELEMENTS" "$m")
    if [ -z "$NO_H264" ] || [ -z "$NO_MJPEG" ]; then m=; fi
  fi
  # shellcheck disable=SC2086
  [ -z "$m" ] || fail "GStreamer elements missing: $(gst_names $m) - $(gst_hint $m)."
  [ "$MODE" = basic ] || [ -n "$(cfg wda_bundle)" ] || fail "WebDriverAgent is not installed yet - run $HERE/resign.sh first (see README)."
  [ "$MODE" = full ] || basic_ok
  [ "$VIDEO_WANT" != airplay ] || uxplay_ok || fail "IPHONE_VIDEO=airplay needs uxplay 1.73+ ($(uxplay_found)) - ./iphone.sh doctor says where to get it."

  take_lock -n || { say "iPhone Control is already starting or running (./iphone.sh stop ends it)."; exit 0; }
  stop_all   # leftovers of a run that was killed
  echo $$ >"$RUN/iphone.pid"
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  log "start (pid $$)"
  [ "$TTY" = 1 ] || say "Starting - this can take up to a minute."

  # 0. the phone (pinned by UDID, so every step talks to the same one)
  end=$((SECONDS + 120))
  while :; do
    m=$(phones)
    if [ -n "${IPHONE_UDID:-}" ]; then
      UDID=$(pick_udid "$IPHONE_UDID" <<<"$m") || UDID=
    elif [ "$(grep -c . <<<"$m")" -gt 1 ]; then
      fail "Several iPhones are plugged in - choose one: IPHONE_UDID=<udid> ./iphone.sh  ($(paste -sd' ' - <<<"$m"))"
    else
      UDID=$m
    fi
    [ -n "$UDID" ] && break
    if [ $SECONDS -ge $end ]; then
      if ! mux_up && [ "$(mux_down_why)" = missing ]; then fail "usbmuxd is not installed - install the usbmuxd package, then plug the iPhone in again"; fi
      mux_up || fail "usbmuxd is not running - plug the iPhone in again, or start it: sudo systemctl start usbmuxd (no systemd: sudo usbmuxd)"
      fail "No iPhone${IPHONE_UDID:+ $IPHONE_UDID} on USB."
    fi
    [ -n "$told" ] || { say "Plug the iPhone in with a cable and unlock it - waiting for it..."; told=1; }
    sleep 2
  done
  told=
  export GO_IOS_UDID=$UDID   # go-ios otherwise takes "the first device"
  VER=$(phone_query version) || VER=
  MAJOR=${VER%%.*}
  [[ $MAJOR =~ ^[0-9]+$ ]] || MAJOR=17   # unknown: assume a current iOS
  note "iPhone ${VER:-(iOS version unknown)}"

  end=$((SECONDS + 120))
  until phone_query paired; do
    [ $SECONDS -lt $end ] || fail "This computer is not trusted by the iPhone - unlock it, tap Trust and enter the passcode (or run: $IOS pair)."
    [ -n "$told" ] || { say "Unlock the iPhone and tap Trust - waiting for it..."; told=1; }
    sleep 2
  done
  told=

  # the mode: auto takes full mode only with Developer Mode (iOS 16+) and the rootless tunnel (17.4.1+)
  if [ "$MODE_WANT" = auto ] && [ "$MODE" = full ]; then
    d=
    if [ "$MAJOR" -ge 16 ] && ! old_ios "$VER"; then d=$(devmode_state); fi
    resolve_mode "$d"
    [ "$MODE" = full ] || basic_ok
  fi
  if [ "$MODE" = basic ] && [ "$MODE_WANT" = auto ]; then say "$(mode_line)"; else note "$(mode_line)"; fi

  # USB video first: a new capture session resets the phone's USB, which drops every usbmux connection
  # (tunnel, forward, WebDriverAgent), so it starts before them and stays up for the whole run
  pick_video
  printf '%s %s %s\n' "$MODE" "$VIDEO" "$WHY" >"$RUN/mode"

  # 1.-4. full mode: signature, tunnel, disk image, forward + WebDriverAgent
  if [ "$MODE" = full ]; then
    start_full
    # that can take minutes (a re-sign, the disk image, an unlock) with nothing watching the capture: one that
    # stopped meanwhile starts again (its USB reset drops the forward and WebDriverAgent, which the watchdog restarts)
    if [ "$VIDEO" = usb ] && ! alive cap; then
      log "the screen capture stopped while starting ($(cat "$RUN/cap.status" 2>/dev/null || echo killed)): $(lasterr "$RUN/cap.log")"
      pick_video
      printf '%s %s %s\n' "$MODE" "$VIDEO" "$WHY" >"$RUN/mode"
    fi
  elif [ "$VIDEO" = usb ]; then note "ready: USB picture, Bluetooth input"
  else note "ready: AirPlay picture, Bluetooth input"; fi

  # 5. watchdog, then the window (closing it stops everything)
  watchdog 9>&- &
  echo $! >"$RUN/watchdog.pid"
  export IPHONE_MODE=$MODE IPHONE_VIDEO=$VIDEO IPHONE_FROM_SCRIPT=1
  if [ "$VIDEO" = usb ]; then export IPHONE_CAPTURE_SOCKET="$RUN/cap.sock"; else unset IPHONE_CAPTURE_SOCKET; fi
  if [ "$MODE" = full ]; then
    export IPHONE_WDA_URL="http://127.0.0.1:$WDA_PORT" IPHONE_MJPEG_URL="http://127.0.0.1:$MJPEG_PORT"
  else
    unset IPHONE_WDA_URL IPHONE_MJPEG_URL
  fi
  # stdout too (video lost / WDA ready / client errors); every print is flushed, so a fatal error stays last
  "$PY" -I "$HERE/wda_mirror.py" </dev/null >"$RUN/window.log" 2>&1 9>&- &
  echo $! >"$RUN/window.pid"
  wait $!; rc=$?
  [ $rc -eq 0 ] && exit 0
  say "The window closed with an error ($rc): $(tail -1 "$RUN/window.log" 2>/dev/null)"
  exit 1
}

usbmuxd_text() {  # the usbmuxd that runs (else the one systemd starts), its version, and whether it is too old
  local b v
  if b=$(usbmuxd_unchecked); then echo "running from $b (not a program only root can change, so not checked)"; return; fi
  b=$(usbmuxd_bin) || { echo "not installed"; return; }
  v=$(usbmuxd_version "$b")
  if [ -z "$v" ]; then echo "version unknown ($b)"
  elif [ "$b" = "$MUX_BIN" ]; then echo "$v ($b, from ./setup.sh --usbmuxd)"
  elif mux_too_old "$v"; then echo "$v ($b) - too old for USB video: ./setup.sh --usbmuxd"
  else echo "$v ($b)"; fi
}
mode_now() {  # MODE and WHY as a start would choose them now ($UDID, $VER: the phone, when there is one)
  local d=
  resolve_mode
  if [ "$MODE_WANT" = auto ] && [ "$MODE" = full ] && [ -n "$UDID" ] && [ "$MAJOR" -ge 16 ] && phone_query paired; then
    d=$(devmode_state)
  fi
  resolve_mode "$d"
}

cmd_status() {
  local p n e now u v st m why one=''
  now=$(date +%s)
  echo "iPhone Control"
  if p=$(pid_of iphone); then echo "  running:        yes (pid $p)"; else echo "  running:        no"; fi
  for n in cap tunnel fwd wda watchdog window uxplay; do
    if alive "$n"; then printf '    %-14s running\n' "$n"; elif [ -f "$RUN/$n.pid" ]; then printf '    %-14s stopped\n' "$n"; fi
  done
  if [ -n "$p" ] && [ -f "$RUN/mode" ] && read -r m v why <"$RUN/mode"; then
    echo "  mode:           $m ($why)"
    case $v in
      usb) echo "  picture:        USB capture ($RUN/cap.sock)" ;;
      mjpeg) echo "  picture:        WebDriverAgent (MJPEG on 127.0.0.1:$MJPEG_PORT)" ;;
      *) echo "  picture:        AirPlay (uxplay)" ;;
    esac
  fi
  if st=$(wda_status); then
    v=$(ios_of <<<"$st")
    echo "  WebDriverAgent: answering on 127.0.0.1:$WDA_PORT (iOS ${v:-?}), screen on 127.0.0.1:$MJPEG_PORT"
  else
    echo "  WebDriverAgent: not answering on 127.0.0.1:$WDA_PORT"
  fi
  if [ -z "$HPY" ]; then echo "  iPhones on USB: ? (no Python 3.9+)"
  elif ! mux_up; then
    case $(mux_down_why) in
      idle) echo "  iPhones on USB: none (usbmuxd starts on plug-in)" ;;
      noudev) echo "  iPhones on USB: none (usbmuxd is not running - sudo usbmuxd once the iPhone is plugged in)" ;;
      missing) echo "  iPhones on USB: ? (usbmuxd is not installed)" ;;
      *) echo "  iPhones on USB: ? (usbmuxd is not running)" ;;
    esac
  else
    u=$(phones)
    [ -n "$u" ] || echo "  iPhones on USB: none"
    while read -r UDID; do
      [ -n "$UDID" ] || continue
      if tunnel_ok; then n="tunnel up"; else n="no tunnel"; fi
      echo "  iPhone on USB:  $UDID ($n)"
      echo "    USB video:    $(capture_text "$UDID")"
    done <<<"$u"
    if [ -n "${IPHONE_UDID:-}" ]; then one=$(pick_udid "$IPHONE_UDID" <<<"$u") || one=
    elif [ "$(grep -c . <<<"$u")" = 1 ]; then one=$u; fi
  fi
  if [ -z "$p" ] && [ -n "$HPY" ]; then   # what a start would choose
    UDID=$one VER=
    [ -z "$UDID" ] || export GO_IOS_UDID=$UDID   # Developer Mode of that phone, not go-ios's "first device"
    [ -z "$UDID" ] || VER=$(phone_query version) || VER=
    MAJOR=${VER%%.*}; [[ $MAJOR =~ ^[0-9]+$ ]] || MAJOR=0
    mode_now
    if [ "$MODE_WANT" = auto ]; then m="auto -> $MODE"; else m=$MODE; fi
    echo "  mode:           $m ($WHY)"
  fi
  echo "  usbmuxd:        $(usbmuxd_text)"
  if [ -x "$IOS" ]; then echo "  go-ios:         $("$IOS" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) ($IOS)"
  else echo "  go-ios:         not installed - run ./setup.sh"; fi
  if [ -x "$BIN/plumesign" ]; then echo "  plumesign:      installed ($BIN/plumesign)"
  else echo "  plumesign:      not installed - run ./setup.sh"; fi
  [ -n "$HPY" ] || { echo "  (no Python 3.9+: cannot read config.json)"; return 0; }
  n=$(cfg wda_bundle)
  if [ -z "$n" ]; then echo "  WebDriverAgent: not installed - run ./resign.sh"; return 0; fi
  echo "  WebDriverAgent: ${n%.*}.<team id>"
  e=$(cfg signed_at)
  [ -n "$e" ] && echo "  signed:         $(fmt_date "$e") ($(( (now - e) / 86400 )) days ago)"
  e=$(cfg expires_at)
  if [ -z "$e" ]; then
    echo "  expires:        unknown (re-signing records it; older installs: ./resign.sh --record)"
  elif [ "$now" -ge "$e" ]; then
    echo "  expires:        EXPIRED $(fmt_date "$e") - ./iphone.sh re-signs it (or ./resign.sh)"
  else
    echo "  expires:        $(fmt_date "$e") (in $(( (e - now) / 86400 )) days $(( (e - now) % 86400 / 3600 )) h)"
  fi
}

cmd_doctor() {  # its whole output goes through redact (bug reports quote it); status shows UDIDs, for local use
  local FAILS=0 c p v e now n u st owner addr commit stamp exe tmp sum rc dm=''
  ok() { printf '  ok    %s\n' "$1"; }
  warn() { printf '  warn  %s\n' "$1"; [ -z "${2:-}" ] || printf '        -> %s\n' "$2"; }
  bad() { printf '  FAIL  %s\n' "$1"; [ -z "${2:-}" ] || printf '        -> %s\n' "$2"; FAILS=$((FAILS + 1)); }
  info() { printf '  -     %s\n' "$1"; }
  need() {  # need full|basic|usb MSG [FIX]: a missing piece; it only FAILs when the chosen mode needs it
    local n=$1; shift
    if [ "$n" = full ] && [ "$MODE" = basic ]; then
      if [ "$MODE_WANT" = basic ]; then info "$1 (full mode only)"; else warn "$1 (full mode only)" "${2:-}"; fi
    elif [ "$n" = basic ] && [ "$MODE" = full ]; then info "$1 (basic mode only)"
    elif [ "$n" = usb ] && ! usb_needed; then warn "$@"
    else bad "$@"; fi
  }
  # USB video is needed with IPHONE_VIDEO=usb, and in basic mode when AirPlay cannot take over
  usb_needed() { [ "$VIDEO_WANT" = usb ] || { [ "$MODE" = basic ] && [ "$VIDEO_WANT" = auto ] && ! uxplay_ok; }; }
  now=$(date +%s)
  resolve_mode   # Developer Mode is only known after the iPhone section

  echo "System ($(uname -sm), ${XDG_SESSION_TYPE:-unknown} session)"
  for c in curl flock timeout ss; do
    if command -v "$c" >/dev/null; then ok "$c"
    elif [ "$c" = ss ]; then warn "ss missing (port checks skipped)" "install iproute2"
    elif [ "$c" = curl ]; then need full "curl missing" "install it with your package manager"
    else bad "$c missing" "install it with your package manager (util-linux, coreutils)"; fi
  done
  if [ -n "$HPY" ]; then ok "Python $("$HPY" -I -c 'import platform; print(platform.python_version())') ($HPY)"
  else bad "no Python 3.9+" "install python3 (openSUSE Leap: python311), or set IPHONE_PYTHON"; fi
  if PY=$(find_python gi); then
    ok "PyGObject with GTK 3 + GStreamer ($PY)"
    v=$(python_note "$PY"); [ -z "$v" ] || warn "$v" "the window uses $PY instead"
    if ! v=$(gst_missing "$PY"); then
      bad "GStreamer does not load in $PY" "$PY -c 'import gi; gi.require_version(\"Gst\", \"1.0\"); from gi.repository import Gst; Gst.init(None)' shows why"
    else
      for e in $GST_ELEMENTS; do
        if [[ " $v " != *" $e "* ]]; then ok "GStreamer $e"
        elif { [ "$VIDEO_WANT" = usb ] || [ "$VIDEO_WANT" = airplay ]; } && [[ " $GST_USB $GST_AIRPLAY " != *" $e "* ]]; then
          info "GStreamer element $e missing (only WebDriverAgent's picture needs it; IPHONE_VIDEO=$VIDEO_WANT)"
        else need full "GStreamer element $e missing" "$(gst_hint "$e")"; fi
      done
    fi
  else
    v=$(python_note "")
    bad "no Python 3.9+ with PyGObject, GTK 3 and GStreamer${v:+ ($v)}" "Debian/Ubuntu: python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0; Fedora: python3-gobject gtk3 gstreamer1; openSUSE: python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-Gst-1_0; Arch: python-gobject gtk3 gstreamer (./setup.sh --check names them); or set IPHONE_PYTHON"
  fi
  if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then ok "display (${WAYLAND_DISPLAY:-$DISPLAY})"
  else warn "no WAYLAND_DISPLAY / DISPLAY here" "the window needs a graphical session"; fi
  if command -v notify-send >/dev/null || command -v gdbus >/dev/null; then ok "desktop notifications (for launcher starts)"
  else warn "no notify-send or gdbus" "errors from the app launcher only reach $RUN/iphone.log (install libnotify)"; fi

  echo "Installed"
  if [ ! -x "$IOS" ]; then need full "go-ios missing ($IOS)" "./setup.sh"
  elif ! v=$("$IOS" version 2>&1); then bad "go-ios does not run: $(tail -1 <<<"$v")" "wrong CPU architecture? re-run ./setup.sh"
  else
    e=$(sed -n 's/^GO_IOS_VERSION=v\{0,1\}\([0-9.]*\).*/\1/p' "$HERE/setup.sh" 2>/dev/null); e=${e:-1.3.2}
    if grep -q "$e" <<<"$v"; then ok "go-ios $e"; else warn "go-ios is not $e: $(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' <<<"$v" | head -1)" "./setup.sh installs the tested version"; fi
  fi
  if [ ! -x "$BIN/plumesign" ]; then need full "plumesign missing ($BIN/plumesign)" "./setup.sh"
  else
    commit=$(sed -n 's/^IMPACTOR_COMMIT=\([0-9a-f]*\).*/\1/p' "$HERE/setup.sh" 2>/dev/null)
    stamp="$commit $(cat "$HERE"/patches/*.patch 2>/dev/null | sha256sum | cut -c1-16)"
    if [ "$(cat "$STATE/plumesign.stamp" 2>/dev/null)" = "$stamp" ]; then ok "plumesign (patched, up to date)"
    else warn "plumesign was built from other sources or patches" "./setup.sh --rebuild"; fi
    # read once, then judged: a plumesign that does not run must not look like "logged in"
    v=$(RUST_LOG=info timeout 20 "$BIN/plumesign" account list </dev/null 2>&1); rc=$?
    if grep -q "No accounts found" <<<"$v"; then
      need full "plumesign: no Apple ID logged in" "in a terminal: $BIN/plumesign account login -u YOUR_APPLE_ID_EMAIL"
    elif [ $rc -eq 124 ]; then bad "plumesign account list did not finish in 20 s" "./setup.sh --rebuild"
    elif [ $rc -ne 0 ]; then
      bad "plumesign does not run (exit $rc): $(grep -v '^ *$' <<<"$v" | tail -1 | cut -c1-200)" "./setup.sh --rebuild"
    elif grep -q "Saved accounts" <<<"$v"; then ok "plumesign: Apple ID saved"
    else warn "plumesign account list: could not tell whether an Apple ID is saved" "run $BIN/plumesign account list"; fi
  fi
  e="${XDG_CONFIG_HOME:-$HOME/.config}/PlumeImpactor"
  if [ -d "$e" ] && [ -n "$(find "$e" -perm /077 2>/dev/null | head -1)" ]; then
    warn "$e is readable by others (Apple ID token, signing key)" "chmod -R go-rwx '$e' (iphone.sh and resign.sh do it)"
  fi
  if [ -f "$HERE/build/WebDriverAgent.ipa" ]; then ok "build/WebDriverAgent.ipa"; else need full "build/WebDriverAgent.ipa missing" "./setup.sh"; fi
  [ -f "$HERE/build/WebDriverAgent-signed.ipa" ] && warn "build/WebDriverAgent-signed.ipa holds your team id and device UDIDs" "./resign.sh --record notes its expiry and deletes it"
  # checked, not built: start builds (and refreshes) $STATE/ca-bundle-with-apple.pem itself
  sum=$(sed -n 's/^APPLE_PEM_SHA256=\([0-9a-f]*\).*/\1/p' "$HERE/certs/ca-bundle.sh" 2>/dev/null)
  if [ ! -x "$HERE/certs/ca-bundle.sh" ]; then bad "certs/ca-bundle.sh missing" "restore it: git checkout certs/"
  elif ! tmp=$("$HERE/certs/ca-bundle.sh" --source 2>&1); then bad "no system CA bundle for the Apple Root CA bundle: $tmp" "install ca-certificates, or set CA_BUNDLE=/path/to/system-bundle.pem"
  elif [ -z "$sum" ] || [ "$(sha256sum <"$HERE/certs/apple-root.pem" 2>/dev/null | cut -d' ' -f1)" != "$sum" ]; then
    bad "certs/apple-root.pem is missing or changed" "restore it: git checkout certs/"
  elif [ -s "$STATE/ca-bundle-with-apple.pem" ]; then ok "CA bundle with Apple Root CA ($STATE/ca-bundle-with-apple.pem, from $tmp)"
  else ok "CA bundle with Apple Root CA: system CAs from $tmp (./iphone.sh builds it at start)"; fi
  if [ -n "$HPY" ]; then
    n=$(cfg wda_bundle); e=$(cfg expires_at)
    if [ -z "$n" ]; then need full "WebDriverAgent not signed/installed yet" "./resign.sh"
    elif [ -z "$e" ]; then warn "WebDriverAgent signature: expiry unknown" "./resign.sh --record (or ./resign.sh)"
    elif [ "$now" -ge "$e" ]; then need full "WebDriverAgent signature EXPIRED $(fmt_date "$e")" "./iphone.sh re-signs it, or ./resign.sh"
    elif [ $((e - now)) -lt 86400 ]; then warn "WebDriverAgent signature runs out $(fmt_date "$e")" "./iphone.sh re-signs it when it starts"
    else ok "WebDriverAgent signature valid until $(fmt_date "$e") ($(( (e - now) / 86400 )) days)"; fi
  fi
  if [ -f "$DESKTOP" ]; then
    exe=$(launcher_path "$DESKTOP")
    if [ ! -x "$exe" ]; then bad "launcher entry points to a missing $exe" "re-run ./setup.sh from this folder"
    elif [ "$exe" != "$HERE/iphone.sh" ]; then warn "launcher entry starts another copy ($exe)" "re-run ./setup.sh from this folder to use this one"
    else ok "launcher entry ($exe)"; fi
  else warn "no launcher entry ($DESKTOP)" "./setup.sh adds it"; fi
  for e in "$STATE"/selfIdentity.plist "$STATE"/config.json; do
    [ -f "$e" ] || continue
    if [ -n "$(find "$e" -perm /077 2>/dev/null)" ]; then warn "$e is readable by others" "chmod 600 '$e' (iphone.sh does it at start)"; fi
  done

  echo "iPhone"
  if [ -z "$HPY" ]; then bad "cannot query usbmuxd without Python"
  elif ! mux_up; then
    case $(mux_down_why) in
      idle) warn "usbmuxd not running (it starts when the iPhone is plugged in)" "plug the iPhone in with a data cable, unlock it" ;;
      noudev) warn "usbmuxd not running, and no udev rule starts it on plug-in" "once the iPhone is plugged in: sudo usbmuxd" ;;
      missing) bad "usbmuxd is not installed" "install the usbmuxd package (./setup.sh --check names what is missing)" ;;
      *) if [ -n "${USBMUXD_SOCKET_ADDRESS:-}" ]; then
           bad "usbmuxd does not answer at USBMUXD_SOCKET_ADDRESS=$USBMUXD_SOCKET_ADDRESS" "start that usbmuxd, or unset USBMUXD_SOCKET_ADDRESS"
         else
           bad "usbmuxd is not running although an iPhone is plugged in" "plug the iPhone in again, or: sudo systemctl start usbmuxd (no systemd: sudo usbmuxd)"
         fi ;;
    esac
  else
    ok "usbmuxd"
    u=$(phones)
    n=$(grep -c . <<<"$u")
    if [ -n "${IPHONE_UDID:-}" ]; then UDID=$(pick_udid "$IPHONE_UDID" <<<"$u") || UDID=
      [ -n "$UDID" ] || bad "IPHONE_UDID=$IPHONE_UDID is not on USB (found: $(paste -sd' ' - <<<"$u"))"
    elif [ "$n" -gt 1 ]; then bad "several iPhones on USB: $(paste -sd' ' - <<<"$u")" "set IPHONE_UDID to one of them (./iphone.sh status lists them)"
    elif [ "$n" -eq 0 ]; then warn "no iPhone on USB" "plug it in with a cable (a charge-only cable shows nothing), unlock it"
    else UDID=$u; fi
  fi
  if [ -n "$UDID" ]; then
    export GO_IOS_UDID=$UDID
    VER=$(phone_query version) || VER=
    MAJOR=${VER%%.*}; [[ $MAJOR =~ ^[0-9]+$ ]] || MAJOR=0
    ok "iPhone $UDID, iOS ${VER:-unknown}"
    if phone_query paired; then ok "paired (this computer is trusted)"
    else bad "not paired with this computer" "unlock the iPhone and tap Trust (or run: $IOS pair)"; fi
    if old_ios "$VER"; then need full "iOS $VER: the rootless tunnel needs iOS 17.4.1 or newer" "update the iPhone"; fi
    if [ -x "$IOS" ] && [ "$MODE_WANT" != basic ] && { [ "$MAJOR" -ge 16 ] || [ "$MAJOR" = 0 ]; }; then
      dm=$(devmode_state)
      case $dm in
        on) ok "Developer Mode on" ;;
        off) need full "Developer Mode off" "Settings > Privacy & Security > Developer Mode (missing? $IOS devmode reveal)" ;;
        *) warn "Developer Mode: could not read it" ;;
      esac
    fi
    if [ "$MODE" = full ] && { [ "$MAJOR" -ge 17 ] || [ "$MAJOR" = 0 ]; }; then
      if tunnel_ok; then ok "developer tunnel up"
      elif agent_up; then warn "a go-ios agent runs on port $GO_IOS_AGENT_PORT but has no tunnel for this iPhone" "unlock the iPhone; or $IOS tunnel stopagent"
      else warn "no developer tunnel (./iphone.sh starts it)"; fi
    fi
    if [ "$MODE" = full ] && { [ "$MAJOR" -lt 17 ] || tunnel_ok; }; then
      v=$(timeout 20 "$IOS" image list 2>&1)
      if grep -q 'image signature' <<<"$v"; then ok "developer disk image mounted"
      elif grep -q DeviceLocked <<<"$v"; then warn "developer disk image: the iPhone is locked"
      else warn "developer disk image not mounted (./iphone.sh mounts it)"; fi
    fi
  fi
  [ -z "$dm" ] || resolve_mode "$dm"
  if [ "$MODE_WANT" = auto ]; then echo "Mode: auto -> $MODE ($WHY)"; else echo "Mode: $MODE ($WHY)"; fi

  echo "USB video (IPHONE_VIDEO=$VIDEO_WANT)"
  if [ "$VIDEO_WANT" = mjpeg ] || [ "$VIDEO_WANT" = airplay ]; then info "not used (IPHONE_VIDEO=$VIDEO_WANT)"
  else
    if e=$(usbmuxd_unchecked); then info "usbmuxd running from $e (not a program only root can change, so not checked)"
    elif ! e=$(usbmuxd_bin); then :   # the iPhone section said so
    elif [ "$e" = "$MUX_BIN" ]; then ok "usbmuxd $(usbmuxd_version "$e") ($e, from ./setup.sh --usbmuxd)"
    elif v=$(usbmuxd_version "$e") && [ -n "$v" ] && mux_too_old "$v"; then
      need usb "usbmuxd $v ($e) is too old for USB video" "./setup.sh --usbmuxd builds a newer one and starts it instead"
    else info "usbmuxd ${v:-(version unknown)} ($e)"; fi
    if [ -n "$UDID" ]; then
      read -r c _ <<<"$(capture_state "$UDID")"
      v=$(capture_text "$UDID")
      case $c in
        ready) ok "${v/ready (/USB video ready (}" ;;
        old|off) need usb "${v% - *}" "${v##* - }" ;;
        *) warn "USB video: $v" ;;
      esac
    fi
    # shellcheck disable=SC2086
    if [ -n "$PY" ] && v=$(gst_missing "$PY" /dev/null $GST_USB); then
      if [ -z "$v" ]; then ok "GStreamer H.264 ($(words $GST_USB | sed "s/h264dec/${H264_DEC:-a decoder}/"))"
      else need usb "GStreamer lacks $(gst_names $v)" "$(gst_hint $v)"; fi
    fi
  fi

  echo "Basic mode (the laptop as a Bluetooth mouse + keyboard)"
  v=$(bt_adapters); rc=$?
  if [ $rc -eq 2 ]; then need basic "cannot ask bluetoothd (neither busctl nor bluetoothctl)" "install systemd's busctl or BlueZ' bluetoothctl"
  elif [ $rc -ne 0 ]; then need basic "bluetoothd does not answer" "install bluez and start it: sudo systemctl enable --now bluetooth"
  elif [ -n "${BT_INDEX:-}" ] && [[ " $v " != *" hci$BT_INDEX "* ]]; then
    need basic "BT_INDEX=$BT_INDEX: hci$BT_INDEX cannot advertise over LE (LE adapters: ${v:-none})" "unset BT_INDEX, or pick another"
  elif [ -n "$v" ]; then ok "Bluetooth LE adapter: $v"
  else need basic "no Bluetooth adapter that can advertise over LE" "switch it on (rfkill unblock bluetooth), or plug in a Bluetooth 4.0+ dongle"; fi
  if ! command -v uxplay >/dev/null 2>&1; then
    if [ "$VIDEO_WANT" = airplay ]; then bad "uxplay missing (IPHONE_VIDEO=airplay)" "install UxPlay 1.73+"
    else info "uxplay not installed (optional: the AirPlay picture when USB video cannot start)"; fi
  else
    v=$(uxplay_version)
    if uxplay_new "$v"; then ok "uxplay $v (AirPlay picture)"
    elif [ "$VIDEO_WANT" = airplay ]; then bad "uxplay ${v:-(version unknown)}: the AirPlay picture needs 1.73 or newer" "build it from https://github.com/FDH2/UxPlay"
    else warn "uxplay ${v:-(version unknown)}: the AirPlay picture needs 1.73 or newer" "build it from https://github.com/FDH2/UxPlay"; fi
  fi

  echo "Session (logs in $RUN)"
  if p=$(pid_of iphone); then ok "iphone.sh running (pid $p)"; else echo "  -     iphone.sh not running"; fi
  for n in cap tunnel fwd wda watchdog window uxplay; do
    if alive "$n"; then ok "$n running"; elif [ -f "$RUN/$n.pid" ]; then warn "$n stopped (stale pid file)" "./iphone.sh stop"; fi
  done
  if command -v ss >/dev/null; then
    for p in "$WDA_PORT" "$MJPEG_PORT"; do
      while read -r _ _ _ addr _ owner; do
        [ -n "$addr" ] || continue
        case $addr in
          127.0.0.1:*|\[::1\]:*) ;;
          *) bad "port $p listens on $addr ${owner:+($owner)} - reachable from the network" "./iphone.sh stop; an old go-ios 'ios forward' binds 0.0.0.0" ; continue ;;
        esac
        if v=$(pid_of fwd) && grep -q "pid=$v," <<<"$owner"; then ok "port $p: our forward on $addr"
        else
          bad "port $p is taken by ${owner:-a program of another user}" "close it, or set IPHONE_$([ "$p" = "$WDA_PORT" ] && echo WDA || echo MJPEG)_PORT"
        fi
      done < <(ss -Hltnp "sport = :$p" 2>/dev/null)
    done
  fi
  if st=$(wda_status); then ok "WebDriverAgent answers (iOS $(ios_of <<<"$st"))"
    if curl -s --noproxy '*' -m 3 "http://127.0.0.1:$MJPEG_PORT" 2>/dev/null | head -c 4096 | grep -qai 'image/jpeg'; then ok "screen stream sends JPEG frames"
    else warn "screen stream sends nothing on port $MJPEG_PORT"; fi
  elif alive wda; then warn "WebDriverAgent runs but does not answer on port $WDA_PORT" "see $RUN/wda.log and $RUN/fwd.log"
  fi
  for n in cap wda tunnel image fwd window; do
    [ -s "$RUN/$n.log" ] || continue
    echo "  last lines of $n.log:"
    tail -3 "$RUN/$n.log" | cut -c1-200 | redact | sed 's/^/        /'
  done
  echo
  if [ $FAILS -eq 0 ]; then echo "No problems found."; else echo "$FAILS problem(s) found."; return 1; fi
}

case $CMD in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  doctor) cmd_doctor | redact; exit "${PIPESTATUS[0]}" ;;
esac
