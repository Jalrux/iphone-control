#!/usr/bin/env bash
# One-time setup on a new Linux machine. Safe to re-run; skips what is already in place.
#   ./setup.sh             install everything for full mode (sudo is only needed for missing system packages; it
#                          prints the command)
#   ./setup.sh --basic     only what basic mode needs: Python, GTK, GStreamer with H.264, BlueZ (no Apple ID, Rust,
#                          go-ios or WebDriverAgent)
#   ./setup.sh --usbmuxd   for USB video: build a newer usbmuxd and have systemd run it instead of the distro's
#                          (shows what it installs and asks first; needs sudo)
#   ./setup.sh --check     only report what is installed, missing or out of date; changes nothing
#                          (with --basic or --usbmuxd: only that part)
#   ./setup.sh --rebuild   force a fresh build of the patched plumesign
#   ./setup.sh --hyprland  also install the Hyprland float rule (Lua config only; otherwise it prints the rule)
#   ./setup.sh --yes       with --usbmuxd: install without asking
# Pins can be overridden: GO_IOS_VERSION + GO_IOS_SHA256, WDA_VERSION + WDA_SHA256, IMPACTOR_COMMIT, USBMUXD_COMMIT,
# CA_BUNDLE.
# Afterwards: log in to your Apple ID once, then ./resign.sh (see README.md).
set -Eeuo pipefail

usage() { sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "$0"; }
CHECK=0 REBUILD=0 HYPR_INSTALL=0 BASIC=0 USBMUXD=0 YES=0
for arg in "$@"; do
  case $arg in
    --check) CHECK=1 ;;
    --rebuild) REBUILD=1 ;;
    --hyprland) HYPR_INSTALL=1 ;;
    --basic) BASIC=1 ;;
    --usbmuxd) USBMUXD=1 ;;
    -y|--yes) YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done
if [ $USBMUXD = 1 ] && [ $((BASIC + REBUILD + HYPR_INSTALL)) -gt 0 ]; then
  echo "--usbmuxd runs on its own (with --check or --yes)" >&2; exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/iphone-control"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/iphone-control"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
MUX_BIN=/usr/local/sbin/usbmuxd-iphone-control                        # --usbmuxd: our build, next to the distro's
MUX_DROPIN=/etc/systemd/system/usbmuxd.service.d/iphone-control.conf   # ... and the unit override that starts it

# Overrides from the environment, kept aside while the pins below are set (iphone.sh doctor reads those lines)
env_pins=""
for v in GO_IOS_VERSION GO_IOS_SHA256 WDA_VERSION WDA_SHA256 IMPACTOR_COMMIT USBMUXD_COMMIT; do
  if [ -n "${!v:-}" ]; then env_pins+=$(printf '%s=%q;' "$v" "${!v}"); fi
done

# Pins. Download checksums = SHA-256 of the release assets (checked 23 Oct 2026 against GitHub's digests)
GO_IOS_VERSION=v1.3.2
GO_IOS_SHA256=a55fdb4c507391c0548252e01d1deb6ae3c7fd99cfbce842c6f80569cc125604   # go-ios-linux.zip
IOS_AMD64_SHA256=24d6149a9d9ab65e17e55313312bcc59c98a7d51d692f9c37b0a0ba26f57623e   # ios-amd64 in that zip
IOS_ARM64_SHA256=ae0553a2c74e1271b55ca90f317030df14eb6a141208002b6905ca4b2d6afe1b   # ios-arm64 in that zip
WDA_VERSION=v16.15.3
WDA_SHA256=2176793538d902f6b1e61291e9997f68d306541f222ea715d256a7c6ce9d4fc5      # WebDriverAgentRunner-Runner.zip
IMPACTOR_COMMIT=7f9885614fb071eae17866731a1e071805895e15   # main, 23 Oct 2026: Mac anisette server
# libimobiledevice/usbmuxd for USB video (1.1.1, the last release, forces the iPhone out of its capture
# configuration). Which commit builds depends on the distro's libplist:
USBMUXD_COMMIT=3ded00c9985a5108cfc7591a309f9a23d57a8cba          # master, 6 Dec 2025: libplist >= 2.6
USBMUXD_COMMIT_PLIST23=e9a0dce170f156846d0e56c7fe9cc1e828a34b44   # 14 Sep 2024, the last for libplist 2.3-2.5
GO_IOS_PIN=$GO_IOS_VERSION GO_IOS_PIN_SHA256=$GO_IOS_SHA256 WDA_PIN=$WDA_VERSION WDA_PIN_SHA256=$WDA_SHA256
USBMUXD_PIN=$USBMUXD_COMMIT
eval "$env_pins"
APPLE_ROOT_SHA256=B0:B1:73:0E:CB:C7:FF:45:05:14:2C:49:F1:29:5E:6E:DA:6B:CA:ED:7E:2C:68:C5:BE:91:B5:A1:10:01:F0:24
RUST_MIN=1.88.0   # Impactor: edition 2024 (1.85); time 0.3.47, plist 1.9, zip 8.6 and image 0.25.10 need 1.88

step=start
trap 'echo "setup.sh failed at line $LINENO (step: $step)" >&2' ERR
say() { step=$*; printf '\n== %s\n' "$*"; }
die() { echo "$*" >&2; exit 1; }
problems=0
report() { echo "$*"; problems=$((problems + 1)); }   # --check: one thing to fix
fail() { if [ $CHECK = 1 ]; then report "$*"; else die "$*"; fi; }

[ "$GO_IOS_VERSION" = "$GO_IOS_PIN" ] || [ "$GO_IOS_SHA256" != "$GO_IOS_PIN_SHA256" ] \
  || die "GO_IOS_VERSION=$GO_IOS_VERSION needs its own GO_IOS_SHA256 (of go-ios-linux.zip)"
[ "$WDA_VERSION" = "$WDA_PIN" ] || [ "$WDA_SHA256" != "$WDA_PIN_SHA256" ] \
  || die "WDA_VERSION=$WDA_VERSION needs its own WDA_SHA256 (of WebDriverAgentRunner-Runner.zip)"

case "$(uname -m)" in
  x86_64|amd64) GOARCH=amd64 IOS_SHA256=$IOS_AMD64_SHA256 ;;
  aarch64|arm64) GOARCH=arm64 IOS_SHA256=$IOS_ARM64_SHA256 ;;
  *) [ $((BASIC + USBMUXD)) -gt 0 ] || die "unsupported CPU $(uname -m): go-ios ships Linux binaries for x86_64 and aarch64 only"
     GOARCH="" IOS_SHA256="" ;;   # basic mode and usbmuxd need no go-ios
esac
if [ "$GO_IOS_VERSION" != "$GO_IOS_PIN" ] || [ "$GO_IOS_SHA256" != "$GO_IOS_PIN_SHA256" ]; then
  IOS_SHA256=   # another release: trust the stamp written at install instead
fi

sha256() { { sha256sum "$1" 2>/dev/null || true; } | cut -d' ' -f1; }
ver_ge() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$2" ]; }   # ver_ge HAVE WANT
uses() { grep -qsw -- "$1" "$HERE/iphone.sh" "$HERE/resign.sh"; }                # a start script calls this tool
lacks() { uses "$1" && ! command -v "$1" >/dev/null; }

# ---------------------------------------------------------------- distro + package names
OS_ID="" OS_VER="" OS_IDS=""   # from /etc/os-release: ID, VERSION_ID, and ID plus ID_LIKE
for f in /etc/os-release /usr/lib/os-release; do
  if [ -r "$f" ]; then
    # shellcheck disable=SC1090
    os_line=$(. "$f"; printf '%s|%s|%s %s' "${ID:-}" "${VERSION_ID:-}" "${ID:-}" "${ID_LIKE:-}") || os_line=""
    IFS='|' read -r OS_ID OS_VER OS_IDS <<<"$os_line"
    break
  fi
done
distro_family() {  # deb | rpm | suse | arch | other
  local w
  for w in $OS_IDS; do
    case $w in
      debian|ubuntu) echo deb; return ;;
      fedora|rhel|centos) echo rpm; return ;;
      suse|opensuse|opensuse-*|sles|sled) echo suse; return ;;
      arch|archlinux) echo arch; return ;;
    esac
  done
  echo other
}
FAM=$(distro_family)
LEAP15=0   # openSUSE Leap 15: python3 is 3.6, so the window needs the python311 packages
if [ "$OS_ID" = opensuse-leap ] && [ "${OS_VER%%.*}" = 15 ]; then LEAP15=1; fi
root_ro() { awk '$2 == "/" { o = $4 } END { exit !(o ~ /(^|,)ro(,|$)/) }' /proc/self/mounts 2>/dev/null; }
IMAGE=""   # an image-based system (Fedora Atomic, openSUSE Aeon/Kalpa/MicroOS): dnf/zypper cannot change its root
if [ "$FAM" = rpm ] && [ -e /run/ostree-booted ]; then IMAGE=rpm-ostree
elif [ "$FAM" = suse ] && root_ro && command -v transactional-update >/dev/null; then IMAGE=transactional-update
fi

# key | Debian/Ubuntu (apt) | Fedora/RHEL (dnf) | openSUSE (zypper) | Arch/Manjaro (pacman) | other distros
# (+ joins several packages; souphttpsrc loads libsoup at run time, so Fedora and openSUSE name it; the H.264
# decoder is whichever of avdec_h264 / openh264dec the distro ships without extra repositories)
PKG_TABLE='
usbmuxd usbmuxd usbmuxd usbmuxd usbmuxd usbmuxd
python3 python3 python3 python3 python python3(3.9-or-newer)
gi python3-gi python3-gobject python3-gobject+python3-gobject-Gdk python-gobject PyGObject
gtk gir1.2-gtk-3.0 gtk3 typelib-1_0-Gtk-3_0 gtk3 GTK3-GObject-typelib
gst gir1.2-gstreamer-1.0 gstreamer1 typelib-1_0-Gst-1_0 gstreamer GStreamer-GObject-typelib
base gstreamer1.0-plugins-base gstreamer1-plugins-base gstreamer-plugins-base gst-plugins-base gst-plugins-base(videoconvert)
good gstreamer1.0-plugins-good gstreamer1-plugins-good+libsoup3 gstreamer-plugins-good+libsoup-3_0-0 gst-plugins-good gst-plugins-good(souphttpsrc,multipartdemux,jpegdec)
gtksink gstreamer1.0-gtk3 gstreamer1-plugins-good-gtk gstreamer-plugins-good-gtk gst-plugin-gtk gst-plugin-gtk(gtksink)
notify libnotify-bin libnotify libnotify-tools libnotify libnotify(notify-send)
cc build-essential gcc gcc base-devel gcc-or-clang
git git git git git git
curl curl curl curl curl curl
zip zip zip zip zip zip
unzip unzip unzip unzip unzip unzip
flock util-linux util-linux util-linux util-linux util-linux(flock)
ss iproute2 iproute iproute2 iproute2 iproute2(ss)
bad gstreamer1.0-plugins-bad gstreamer1-plugins-bad-free gstreamer-plugins-bad gst-plugins-bad gst-plugins-bad(h264parse)
h264dec gstreamer1.0-libav gstreamer1-plugin-openh264 gstreamer-plugin-openh264 gst-libav gst-libav(avdec_h264)-or-openh264
bluez bluez bluez bluez bluez+bluez-utils bluez(bluetoothd)
mux-build build-essential+autoconf+automake+libtool+pkg-config gcc+make+autoconf+automake+libtool+pkgconf-pkg-config gcc+make+autoconf+automake+libtool+pkgconf base-devel gcc,make,autoconf,automake,libtool,pkg-config
plist-dev libplist-dev libplist-devel pkgconfig(libplist-2.0) libplist libplist-2.0(headers)
usb-dev libusb-1.0-0-dev libusb1-devel pkgconfig(libusb-1.0) libusb libusb-1.0(headers)
glue-dev libimobiledevice-glue-dev libimobiledevice-glue-devel pkgconfig(libimobiledevice-glue-1.0) libimobiledevice-glue libimobiledevice-glue-1.0(headers)
limd-dev libimobiledevice-dev libimobiledevice-devel pkgconfig(libimobiledevice-1.0) libimobiledevice libimobiledevice-1.0(headers)
'
pkgs_for() {  # pkgs_for KEY -> this distro's package name(s)
  local col
  if [ $LEAP15 = 1 ]; then
    case $1 in python3) echo python311; return ;; gi) echo python311-gobject python311-gobject-Gdk; return ;; esac
  fi
  case $FAM in deb) col=2 ;; rpm) col=3 ;; suse) col=4 ;; arch) col=5 ;; *) col=6 ;; esac
  printf '%s\n' "$PKG_TABLE" | awk -v k="$1" -v c="$col" '$1 == k { gsub(/\+/, " ", $c); print $c }'
}
install_cmd() {  # empty on unknown distros
  local sudo=""; [ "$(id -u)" = 0 ] || sudo="sudo "
  case $IMAGE in
    rpm-ostree) echo "${sudo}rpm-ostree install"; return ;;
    transactional-update) echo "${sudo}transactional-update pkg install"; return ;;
  esac
  case $FAM in
    deb) echo "${sudo}apt install" ;; rpm) echo "${sudo}dnf install" ;;
    suse) echo "${sudo}zypper install" ;; arch) echo "${sudo}pacman -S --needed" ;;
  esac
}
install_note() {  # what to do after install_cmd, if anything
  if [ -n "$IMAGE" ]; then echo "  (an image-based system: the packages arrive with a new image, so reboot, then rerun ./setup.sh)"; fi
}
pkgs_line() {  # pkgs_line KEY...: their packages, once each, quoted where a shell needs it (zypper's pkgconfig(...))
  local k q="/[()]/s/.*/'&'/"
  [ "$FAM" != other ] || q=""   # no install command to paste them into
  for k in "$@"; do pkgs_for "$k"; done | tr ' ' '\n' | awk 'NF && !seen[$0]++' | sed "$q" | tr '\n' ' ' | sed 's/ $//'
}
only() { local w kept=(); for w in $2; do [[ " $1 " != *" $w "* ]] || kept+=("$w"); done; echo "${kept[*]:-}"; }   # only KEEP WORDS
BLUETOOTHD_DIRS="/usr/libexec/bluetooth /usr/lib/bluetooth /usr/sbin"   # where distros put bluetoothd (not on PATH)
have_bluez() {
  local d
  command -v bluetoothd >/dev/null && return 0
  for d in $BLUETOOTHD_DIRS; do [ -x "$d/bluetoothd" ] && return 0; done
  return 1
}
usbmuxd_bin() {  # the distro's usbmuxd (sbin is often not on a user's PATH)
  local d
  command -v usbmuxd 2>/dev/null && return 0
  for d in /usr/sbin /usr/bin /sbin; do [ -x "$d/usbmuxd" ] && { echo "$d/usbmuxd"; return 0; }; done
  return 1
}
usbmuxd_version() { { timeout 5 "$1" --version 2>/dev/null || true; } | grep -oE '[0-9]+\.[0-9]+[^ ]*' | head -n1 || true; }

# ---------------------------------------------------------------- --usbmuxd: a newer usbmuxd for USB video
# Measured (iPhone 15 Pro, iOS 26.5.2, Oct 2026): the screen-capture interface exists only in the iPhone's USB
# configuration 5. usbmuxd 1.1.1 forces 4; git master keeps 5 when started with USBMUXD_DEFAULT_DEVICE_MODE=2.
mux_in_place() { [ -x "$MUX_BIN" ] && grep -qsF "ExecStart=$MUX_BIN" "$MUX_DROPIN"; }
SYSTEMD_RUN=/run/systemd/system   # there when systemd is the init (sd_booted)
has_systemd() { [ -d "$SYSTEMD_RUN" ] && command -v systemctl >/dev/null; }
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
mux_user() {  # the account the distro's unit runs usbmuxd as (usbmux on Debian, Arch and openSUSE, usbmuxd on Fedora)
  local f u
  for f in /usr/lib/systemd/system/usbmuxd.service /lib/systemd/system/usbmuxd.service; do
    u=$(sed -n -E 's/^ExecStart=.*(--user|-U)[ =]([^ ]+).*/\2/p' "$f" 2>/dev/null | head -n1) || u=""
    if [ -n "$u" ]; then echo "$u"; return; fi
  done
  if ! getent passwd usbmux >/dev/null 2>&1 && getent passwd usbmuxd >/dev/null 2>&1; then echo usbmuxd; else echo usbmux; fi
}
mux_dropin() {
  cat <<EOF
# iPhone Control (setup.sh --usbmuxd): a newer usbmuxd, which keeps the iPhone's screen-capture USB configuration.
# Undo: ./uninstall.sh, or delete this file and $MUX_BIN, then systemctl daemon-reload and restart usbmuxd
[Service]
Environment=USBMUXD_DEFAULT_DEVICE_MODE=2
ExecStart=
ExecStart=$MUX_BIN --user $(mux_user) --systemd
EOF
}
ask() {  # ask QUESTION: y/N on the terminal; yes with --yes, no without a terminal
  local a=""
  [ $YES = 0 ] || return 0
  if [ ! -t 0 ]; then echo "$1 [y/N] n (no terminal; --yes answers yes)"; return 1; fi
  read -rp "$1 [y/N] " a || true
  case $a in [yY]*) return 0 ;; *) return 1 ;; esac
}
if [ $USBMUXD = 1 ]; then
  say "usbmuxd for USB video ($FAM, $(uname -m)) -> $MUX_BIN"
  plist=""
  if command -v pkg-config >/dev/null; then plist=$(pkg-config --modversion libplist-2.0 2>/dev/null) || plist=""; fi
  old_distro=0   # their libplist is 2.2, and no usbmuxd commit with USBMUXD_DEFAULT_DEVICE_MODE builds against it
  case "$OS_ID $OS_VER" in "debian 11"|"debian 12"|"ubuntu 20.04"|"ubuntu 22.04") old_distro=1 ;; esac
  if [ "$USBMUXD_COMMIT" != "$USBMUXD_PIN" ]; then commit=$USBMUXD_COMMIT   # from the environment, as given
  elif [ -n "$plist" ] && ver_ge "$plist" 2.6; then commit=$USBMUXD_COMMIT
  elif [ -n "$plist" ] && ver_ge "$plist" 2.3; then commit=$USBMUXD_COMMIT_PLIST23
  elif [ -n "$plist" ] || [ $old_distro = 1 ]; then
    echo "USB video needs a usbmuxd newer than 1.1.1, and those need libplist 2.3 or newer to build; this system has"
    echo "libplist ${plist:-2.2}. Nothing was changed. A newer distro has it (Debian 13, Ubuntu 24.04, Fedora, openSUSE"
    echo "Tumbleweed, Arch); until then full mode shows WebDriverAgent's picture, and basic mode can use AirPlay (uxplay)."
    exit 0
  else commit=$USBMUXD_COMMIT; fi   # libplist's headers are missing (named below); the newest pin until then
  mux_miss=()
  command -v git >/dev/null || mux_miss+=(git)
  for t in make autoconf automake; do command -v "$t" >/dev/null || mux_miss+=(mux-build); done
  command -v cc >/dev/null || command -v gcc >/dev/null || command -v clang >/dev/null || mux_miss+=(mux-build)
  command -v libtoolize >/dev/null || command -v glibtoolize >/dev/null || mux_miss+=(mux-build)
  if command -v pkg-config >/dev/null; then
    for kv in plist-dev:libplist-2.0 usb-dev:libusb-1.0 glue-dev:libimobiledevice-glue-1.0 limd-dev:libimobiledevice-1.0; do
      pkg-config --exists "${kv#*:}" || mux_miss+=("${kv%%:*}")
    done
  else
    mux_miss+=(mux-build plist-dev usb-dev glue-dev limd-dev)
  fi
  src="$CACHE/usbmuxd"
  # usbmuxd.stamp: the commit and the SHA-256 of the program built from it, which tells that build at $MUX_BIN
  # however it got there (without systemd, the user installs it by hand)
  mux_stamp() { echo "$commit $(sha256 "$1")"; }   # mux_stamp PROGRAM
  mux_built() { [ -x "$MUX_BIN" ] && [ "$(cat "$STATE/usbmuxd.stamp" 2>/dev/null)" = "$(mux_stamp "$MUX_BIN")" ]; }
  current=0
  if mux_built && { ! has_systemd || [ "$(cat "$MUX_DROPIN" 2>/dev/null)" = "$(mux_dropin)" ]; }; then current=1; fi
  if [ $CHECK = 0 ] && [ $current = 1 ]; then
    echo "already in place (usbmuxd $(usbmuxd_version "$MUX_BIN")$(has_systemd || echo "; without systemd it runs only when started by hand"))"
    exit 0
  fi
  if [ $CHECK = 1 ]; then
    if [ ! -x "$MUX_BIN" ]; then report "not installed: ./setup.sh --usbmuxd builds usbmuxd ${commit:0:7} and installs it"
    elif ! mux_built; then
      report "$MUX_BIN is not usbmuxd ${commit:0:7}: ./setup.sh --usbmuxd rebuilds it"
    elif ! has_systemd; then echo "$MUX_BIN is installed; without systemd it runs only when started by hand"
    elif [ "$(cat "$MUX_DROPIN" 2>/dev/null)" != "$(mux_dropin)" ]; then
      report "$MUX_DROPIN is missing or changed: ./setup.sh --usbmuxd writes it"
    else echo "in place: usbmuxd $(usbmuxd_version "$MUX_BIN") ($MUX_BIN), started by systemd with USBMUXD_DEFAULT_DEVICE_MODE=2"; fi
  fi
  if [ ${#mux_miss[@]} -gt 0 ] && { [ $CHECK = 0 ] || [ $problems -gt 0 ]; }; then
    pkgs=$(pkgs_line "${mux_miss[@]}")
    if [ -n "$(install_cmd)" ]; then
      echo "missing (to build it): $pkgs"
      echo "install with:  $(install_cmd) $pkgs"
      install_note
    else
      echo "missing to build it (package names vary by distro): $pkgs"
    fi
    exit 1
  fi
  [ $CHECK = 0 ] || exit $((problems > 0))
  sysd=0
  if has_systemd; then sysd=1; fi
  echo "This builds libimobiledevice/usbmuxd ${commit:0:12} (libplist ${plist:-?}) in $src"
  if [ $sysd = 1 ]; then
    echo "and installs, with sudo:"
    echo "  $MUX_BIN"
    echo "  $MUX_DROPIN:"
    mux_dropin | sed 's/^/      /'
    echo "then runs systemctl daemon-reload and systemctl restart usbmuxd (a plugged-in iPhone reconnects)."
    echo "The distro's usbmuxd stays installed; ./uninstall.sh goes back to it."
    ask "Build and install it?" || { echo "nothing changed"; exit 0; }
  fi
  mkdir -p "$CACHE" "$STATE"
  if [ ! -d "$src/.git" ]; then
    rm -rf "$src" && git init -q "$src" && git -C "$src" remote add origin https://github.com/libimobiledevice/usbmuxd
  fi
  git -C "$src" cat-file -e "$commit^{commit}" 2>/dev/null \
    || git -C "$src" fetch -q --depth 1 origin "$commit" || die "could not fetch usbmuxd $commit"
  git -C "$src" reset -q --hard "$commit"
  git -C "$src" clean -qfdx
  [ "$(git -C "$src" rev-parse HEAD)" = "$commit" ] || die "usbmuxd checkout is not $commit"
  log="$CACHE/usbmuxd-build.log"
  echo "building (log: $log)..."
  ( cd "$src" && NOCONFIGURE=1 ./autogen.sh && ./configure && make -j"$(nproc 2>/dev/null || echo 2)" ) >"$log" 2>&1 \
    || die "the usbmuxd build failed: $(grep -v '^ *$' "$log" | tail -n 1 | cut -c1-200) - see $log"
  [ -x "$src/src/usbmuxd" ] || die "the usbmuxd build made no src/usbmuxd - see $log"
  built=$(usbmuxd_version "$src/src/usbmuxd")
  if [ $sysd = 0 ]; then
    mux_stamp "$src/src/usbmuxd" >"$STATE/usbmuxd.stamp"   # what --check finds at $MUX_BIN once it is there
    echo "built usbmuxd ${built:-(version unknown)}. Without systemd nothing is installed; to use it, stop the distro's"
    echo "usbmuxd and, as root, install and start this one instead (with the iPhone plugged in):"
    echo "  install -m755 '$src/src/usbmuxd' $MUX_BIN"
    echo "  USBMUXD_DEFAULT_DEVICE_MODE=2 $MUX_BIN --user $(mux_user)"
    exit 0
  fi
  # explicit modes (install -D makes missing directories 755): sudo keeps a stricter umask of the user's, and a
  # usbmuxd.service.d only root can search hides the override from every check that runs as the user (this
  # script's, iphone.sh's, uninstall.sh's); a+rx also opens one that was made that way before
  as_root install -D -m755 "$src/src/usbmuxd" "$MUX_BIN"
  mux_dropin | as_root install -D -m644 /dev/stdin "$MUX_DROPIN"
  as_root chmod a+rx "$(dirname "$MUX_DROPIN")"
  mux_stamp "$MUX_BIN" >"$STATE/usbmuxd.stamp"
  as_root systemctl daemon-reload
  if as_root systemctl restart usbmuxd; then
    echo "ok: usbmuxd ${built:-} runs with USBMUXD_DEFAULT_DEVICE_MODE=2 (./iphone.sh doctor shows whether USB video is ready)"
  else
    echo "installed, but usbmuxd did not restart (systemctl status usbmuxd says why); it is used from its next start"
  fi
  exit 0
fi

pick_python() {  # same choice as iphone.sh: IPHONE_PYTHON, /usr/bin/python3, python3, python3.13 ... 3.9
  local p first="" ok='import sys; sys.exit(sys.version_info < (3, 9))' gi='import gi; gi.require_version("Gtk", "3.0"); gi.require_version("Gst", "1.0")'
  for p in ${IPHONE_PYTHON:+"$IPHONE_PYTHON"} /usr/bin/python3 python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if ! command -v "$p" >/dev/null 2>&1 || ! "$p" -I -c "$ok" 2>/dev/null; then continue; fi
    [ -n "$first" ] || first=$(command -v "$p")
    if "$p" -I -c "$gi" 2>/dev/null; then command -v "$p"; return; fi
  done
  echo "$first"   # Python 3.9+ without PyGObject (reported below), or nothing
}

WINDOW_KEYS="gi gtk gst base good gtksink"   # everything the window needs in full mode (MJPEG picture)
H264_KEYS="bad h264dec"                       # h264parse and a decoder: USB video and AirPlay
BASIC_KEYS="gi gtk gst base gtksink $H264_KEYS"   # the window in basic mode (no MJPEG)
probe_window_deps() {  # what the window (GTK 3 + GStreamer, via PyGObject) lacks -> package-table keys
  # Whatever cannot be checked yet is named too, so one install line covers it all (installed ones are skipped)
  "$PY" -I - <<'EOF' 2>/dev/null || echo "$WINDOW_KEYS $H264_KEYS"
import sys
try:
    import gi
except ImportError:
    print("gi gtk gst base good gtksink bad h264dec"); sys.exit()
miss = []
try:
    gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk, Gdk  # noqa: F401
except (ImportError, ValueError):
    miss.append("gtk")
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    key = {"souphttpsrc": "good", "multipartdemux": "good", "jpegdec": "good", "videoconvert": "base", "appsrc": "base",
           "gtksink": "gtksink", "h264parse": "bad"}
    miss += [key[e] for e in key if not Gst.ElementFactory.find(e)]
    if not any(Gst.ElementFactory.find(d) for d in ("vah264dec", "avdec_h264", "openh264dec")):
        miss.append("h264dec")
except (ImportError, ValueError):
    miss += ["gst", "base", "good", "gtksink", "bad", "h264dec"]   # the plugins cannot be checked without Gst
print(" ".join(dict.fromkeys(miss)))
EOF
}

CARGO="" RUST_V=""
rust_v() {  # rust_v CARGO -> the lower of its version and that of the rustc next to it (empty if either fails)
  local c r
  c=$("$1" --version 2>/dev/null | awk '{print $2}') || c=""
  r=$("$(dirname "$1")/rustc" --version 2>/dev/null | awk '{print $2}') || r=""
  c=${c%%-*} r=${r%%-*}
  if [ -z "$c" ] || [ -z "$r" ]; then echo ""; elif ver_ge "$c" "$r"; then echo "$r"; else echo "$c"; fi
}
find_rust() {  # sets CARGO and RUST_V: the first new-enough of cargo on PATH, ~/.cargo/bin (rustup not on PATH
               # yet) and Ubuntu's versioned /usr/lib/rust-1.NN/bin (newest first); else the newest one found
  local c v cands
  CARGO="" RUST_V=""
  # "|| true": set -E runs the ERR trap and set -e in a process substitution too, which ended the list early
  mapfile -t cands < <({ command -v cargo || true; } 2>/dev/null; echo "${CARGO_HOME:-$HOME/.cargo}/bin/cargo"
                       printf '%s\n' /usr/lib/rust-1.*/bin/cargo | sort -rV)
  for c in "${cands[@]}"; do
    [ -x "$c" ] || continue
    v=$(rust_v "$c"); [ -n "$v" ] || continue
    if ver_ge "$v" "$RUST_MIN"; then CARGO=$c RUST_V=$v; return; fi
    if [ -z "$RUST_V" ] || ver_ge "$v" "$RUST_V"; then CARGO=$c RUST_V=$v; fi
  done
  [ -n "$CARGO" ] || CARGO="${CARGO_HOME:-$HOME/.cargo}/bin/cargo"
}
rust_hint() {
  echo "Rust >= $RUST_MIN is needed to build plumesign (found: ${RUST_V:-none})."
  case $FAM in
    rpm) echo "  install:  $(install_cmd) cargo rust   (or rustup, below, if that is older)" ;;
    suse) echo "  install:  $(install_cmd) cargo   (or rustup, below, if that is older)" ;;
    arch) echo "  install:  $(install_cmd) rust   (or rustup, then: rustup default stable)" ;;
    deb) echo "  Ubuntu 22.04/24.04:  $(install_cmd) cargo-1.91 rustc-1.91   (setup finds /usr/lib/rust-1.NN itself)"
         echo "  Debian 13:  $(install_cmd) -t trixie-backports rustc cargo   (the default cargo is too old)" ;;
  esac
  echo "  rustup:   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal"
  echo "            then open a new terminal (or run: . \"\$HOME/.cargo/env\") and rerun ./setup.sh"
}

# ---------------------------------------------------------------- what is already in place
plumesign_stamp="$IMPACTOR_COMMIT $(cat "$HERE"/patches/*.patch | sha256sum | cut -c1-16)"
plumesign_current() { [ -x "$BIN/plumesign" ] && [ "$(cat "$STATE/plumesign.stamp" 2>/dev/null)" = "$plumesign_stamp" ]; }
need_plumesign=1
if [ $REBUILD = 0 ] && plumesign_current; then need_plumesign=0; fi

ios_stamp() { echo "$GO_IOS_VERSION $GO_IOS_SHA256 $GOARCH $1"; }
ios_current() {
  local have; have=$(sha256 "$BIN/ios")
  [ -n "$have" ] && { [ "$have" = "$IOS_SHA256" ] || [ "$(cat "$STATE/go-ios.stamp" 2>/dev/null)" = "$(ios_stamp "$have")" ]; }
}

IPA="$HERE/build/WebDriverAgent.ipa"
ipa_want="$WDA_VERSION $WDA_SHA256"
ipa_current() { [ -s "$IPA" ] && [ "$(cat "$IPA.stamp" 2>/dev/null)" = "$ipa_want" ]; }

# ---------------------------------------------------------------- 1. system packages
say "system packages ($FAM, $(uname -m))"
missing=()
PY=$(pick_python)
if [ $BASIC = 1 ]; then want=$BASIC_KEYS; else want=$WINDOW_KEYS; fi
if [ -z "$PY" ]; then
  echo "no Python 3.9 or newer found (or set IPHONE_PYTHON=/path/to/python3)"
  gaps="$WINDOW_KEYS $H264_KEYS"   # PyGObject and the rest cannot be probed without it
  read -r -a py_miss <<<"python3 $(only "$want" "$gaps")"
else
  gaps=$(probe_window_deps)
  read -r -a py_miss <<<"$(only "$want" "$gaps")"
fi
missing+=("${py_miss[@]}")
if ! { command -v usbmuxd || [ -x /usr/sbin/usbmuxd ] || [ -x /usr/bin/usbmuxd ] || [ -x /sbin/usbmuxd ] || [ -S /var/run/usbmuxd ]; } >/dev/null; then
  missing+=(usbmuxd)
fi
if [ $BASIC = 1 ]; then
  have_bluez || missing+=(bluez)
  need_plumesign=0   # basic mode downloads and builds nothing
else
  for t in git curl zip unzip; do command -v $t >/dev/null || missing+=("$t"); done
fi
rust_bad=0
if [ $need_plumesign = 1 ]; then
  command -v cc >/dev/null || command -v gcc >/dev/null || command -v clang >/dev/null || missing+=(cc)
  find_rust
  if [ -z "$RUST_V" ] || ! ver_ge "$RUST_V" "$RUST_MIN"; then rust_bad=1; fi
fi

if [ ${#missing[@]} -gt 0 ] || [ $rust_bad = 1 ]; then
  if [ ${#missing[@]} -gt 0 ]; then
    pkgs=$(for k in "${missing[@]}"; do pkgs_for "$k"; done | tr ' ' '\n' | awk 'NF && !seen[$0]++' | tr '\n' ' ')
    if [ -n "$(install_cmd)" ]; then
      echo "missing: $pkgs"
      echo "install with:  $(install_cmd) $pkgs"
      install_note
    else
      echo "missing (package names vary by distro): $pkgs"
    fi
  fi
  if [ $LEAP15 = 1 ] && [[ " ${missing[*]} " == *" python3 "* || " ${missing[*]} " == *" gi "* ]]; then
    echo "  (Leap 15's python3 is 3.6, too old: python311* are the Python 3.11 packages; or upgrade to Leap 16)"
  fi
  if [ $rust_bad = 1 ]; then rust_hint; fi
  [ $CHECK = 1 ] || exit 1
  problems=$((problems + 1))
else
  echo "ok (python: $PY${RUST_V:+, rust $RUST_V})"
fi
# optional: iphone.sh works without these, with less safety or feedback
if ! command -v notify-send >/dev/null && ! command -v gdbus >/dev/null; then
  echo "note: without notify-send, errors are not shown when started from the app launcher: $(install_cmd) $(pkgs_for notify)"
fi
if lacks flock; then echo "note: flock is missing (two starts at once are not prevented): $(install_cmd) $(pkgs_for flock)"; fi
if lacks ss; then echo "note: ss is missing (port checks are skipped): $(install_cmd) $(pkgs_for ss)"; fi
# optional groups: never "install with:" lines, which CI and people install as the required set
if [ $BASIC = 0 ]; then
  read -r -a opt <<<"$(only "$H264_KEYS" "$gaps")"
  have_bluez || opt+=(bluez)
  if [ ${#opt[@]} -gt 0 ]; then
    cmd=$(install_cmd)
    echo "optional, for USB video and basic mode (Bluetooth input): ${cmd:+$cmd }$(pkgs_line "${opt[@]}")"
  fi
fi
mux=$(usbmuxd_bin) && muxv=$(usbmuxd_version "$mux") || muxv=""
if [ -n "$muxv" ] && [ "$(printf '%s\n1.1.1\n' "$muxv" | sort -V | tail -n1)" = 1.1.1 ] && ! mux_in_place; then
  echo "optional: USB video needs a usbmuxd newer than $muxv (the last release; it keeps the iPhone out of its capture configuration): ./setup.sh --usbmuxd"
fi
if [ $BASIC = 1 ] && ! command -v uxplay >/dev/null; then
  echo "optional: uxplay 1.73+, for an AirPlay picture when USB video cannot start (Arch: AUR; Debian 13 and Ubuntu 24.04 ship older ones)"
fi

# sections 2-5 are full mode's: basic mode downloads and builds nothing
if [ $BASIC = 0 ]; then
  # ---------------------------------------------------------------- downloads
  fetch() {  # fetch URL FILE SHA256: reuse the cached copy or download, and keep it only if the checksum matches
    local url=$1 out=$2 sum=$3
    if [ -f "$out" ] && [ "$(sha256 "$out")" = "$sum" ]; then return 0; fi
    curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -o "$out.part" "$url" || { rm -f "$out.part"; die "download failed: $url"; }
    [ "$(sha256 "$out.part")" = "$sum" ] || { rm -f "$out.part"; die "checksum mismatch (corrupt or changed download): $url"; }
    mv -f "$out.part" "$out"
  }

  [ $CHECK = 1 ] || mkdir -p "$BIN" "$STATE" "$CACHE" "$HERE/build"

  # ---------------------------------------------------------------- 2. go-ios
  say "go-ios $GO_IOS_VERSION ($GOARCH) -> $BIN/ios"
  if ios_current; then
    echo "already installed"
  elif [ $CHECK = 1 ]; then
    if [ -e "$BIN/ios" ]; then report "a different ios binary is installed (setup.sh would replace it)"; else report "not installed"; fi
  else
    if [ -e "$BIN/ios" ]; then echo "replacing $BIN/ios ($("$BIN/ios" version 2>/dev/null | tail -n1 || echo "does not run here"))"; fi
    zip="$CACHE/go-ios-linux-$GO_IOS_VERSION.zip"
    fetch "https://github.com/danielpaulus/go-ios/releases/download/$GO_IOS_VERSION/go-ios-linux.zip" "$zip" "$GO_IOS_SHA256"
    rm -rf "$CACHE/go-ios" && unzip -oq "$zip" -d "$CACHE/go-ios"
    [ -f "$CACHE/go-ios/ios-$GOARCH" ] || die "go-ios-linux.zip has no ios-$GOARCH"
    install -m755 "$CACHE/go-ios/ios-$GOARCH" "$BIN/ios.new" && mv -f "$BIN/ios.new" "$BIN/ios"
    "$BIN/ios" version >/dev/null 2>&1 || die "the installed $BIN/ios does not run on this machine"
    ios_stamp "$(sha256 "$BIN/ios")" >"$STATE/go-ios.stamp"
    echo "ok ($("$BIN/ios" version 2>/dev/null | tail -n1))"
  fi

  # ---------------------------------------------------------------- 3. CA bundle
  say "Apple Root CA (go-ios needs it to get the developer disk image signed)"
  fp=$("${PY:-python3}" -I -c 'import ssl,hashlib,sys;print(":".join("%02X" % b for b in hashlib.sha256(ssl.PEM_cert_to_DER_cert(open(sys.argv[1]).read())).digest()))' \
    "$HERE/certs/apple-root.pem" 2>/dev/null) || fp="(could not read it)"
  if [ "$fp" != "$APPLE_ROOT_SHA256" ]; then
    fail "certs/apple-root.pem fingerprint mismatch: $fp"
  elif [ $CHECK = 1 ]; then
    if ca=$("$HERE/certs/ca-bundle.sh" --source); then echo "ok (system CAs: $ca)"; else problems=$((problems + 1)); fi
  else
    bundle=$("$HERE/certs/ca-bundle.sh") || die "could not build the CA bundle"
    echo "ok ($bundle, system CAs from $("$HERE/certs/ca-bundle.sh" --source))"
  fi

  # ---------------------------------------------------------------- 4. plumesign
  say "plumesign (Impactor ${IMPACTOR_COMMIT:0:7} + patches/) -> $BIN/plumesign"
  if [ $need_plumesign = 0 ]; then
    echo "already built"
  elif [ $CHECK = 1 ]; then
    if [ -x "$BIN/plumesign" ]; then report "out of date (setup.sh would rebuild it)"; else report "not built"; fi
  else
    src="$CACHE/impactor"
    if [ ! -d "$src/.git" ]; then
      rm -rf "$src" && git init -q "$src" && git -C "$src" remote add origin https://github.com/claration/impactor
    fi
    # offline re-runs work once the pinned commit is here; GitHub serves a fetch by commit id
    git -C "$src" cat-file -e "$IMPACTOR_COMMIT^{commit}" 2>/dev/null \
      || git -C "$src" fetch -q --depth 1 origin "$IMPACTOR_COMMIT" || die "could not fetch Impactor $IMPACTOR_COMMIT"
    git -C "$src" reset -q --hard "$IMPACTOR_COMMIT"
    git -C "$src" clean -qfdx -e target/
    [ "$(git -C "$src" rev-parse HEAD)" = "$IMPACTOR_COMMIT" ] || die "Impactor checkout is not $IMPACTOR_COMMIT"
    for p in "$HERE"/patches/*.patch; do
      if git -C "$src" apply --reverse --check "$p" 2>/dev/null; then echo "already upstream: $(basename "$p")"; continue; fi
      git -C "$src" apply --check "$p" 2>/dev/null \
        || die "$(basename "$p") no longer applies to Impactor $IMPACTOR_COMMIT - see docs/how-it-works.md (The patches)"
      git -C "$src" apply "$p" && echo "applied $(basename "$p")"
    done
    ( cd "$src" && PATH="$(dirname "$CARGO"):$PATH" CARGO_TARGET_DIR="$src/target" "$CARGO" build --release --locked -p plumesign ) \
      || die "cargo build failed - it needs Rust >= $RUST_MIN and a C compiler (gcc or clang); CPUs other than x86_64/aarch64 also need cmake"
    install -m755 "$src/target/release/plumesign" "$BIN/plumesign.new" && mv -f "$BIN/plumesign.new" "$BIN/plumesign"
    echo "$plumesign_stamp" >"$STATE/plumesign.stamp"
    echo "ok"
  fi

  # ---------------------------------------------------------------- 5. WebDriverAgent
  say "WebDriverAgent $WDA_VERSION -> build/WebDriverAgent.ipa"
  if ipa_current; then
    echo "already built"
  elif [ $CHECK = 1 ]; then
    if [ -e "$IPA" ]; then report "out of date or unverified (setup.sh would rebuild it)"; else report "not built"; fi
  else
    zip="$CACHE/WebDriverAgentRunner-Runner-$WDA_VERSION.zip"
    fetch "https://github.com/appium/WebDriverAgent/releases/download/$WDA_VERSION/WebDriverAgentRunner-Runner.zip" "$zip" "$WDA_SHA256"
    rm -rf "$CACHE/wda" && mkdir -p "$CACHE/wda/Payload"
    unzip -oq "$zip" -d "$CACHE/wda"
    [ -d "$CACHE/wda/WebDriverAgentRunner-Runner.app/PlugIns/WebDriverAgentRunner.xctest" ] \
      || die "unexpected WebDriverAgent zip layout (no PlugIns/WebDriverAgentRunner.xctest)"
    mv "$CACHE/wda/WebDriverAgentRunner-Runner.app" "$CACHE/wda/Payload/"
    rm -rf "$CACHE"/wda/Payload/*/PlugIns/*.dSYM
    rm -f "$IPA.tmp"
    ( cd "$CACHE/wda" && zip -qr -X "$IPA.tmp" Payload )
    unzip -tq "$IPA.tmp" >/dev/null || { rm -f "$IPA.tmp"; die "the built IPA is broken"; }
    mv -f "$IPA.tmp" "$IPA" && echo "$ipa_want" >"$IPA.stamp"
    echo "ok"
  fi
fi

# ---------------------------------------------------------------- 6. launcher entry
say "app launcher entry -> $APPS/iphone-control.desktop"
desktop_entry() {
  # Desktop Entry spec: Exec gets the path in quotes with \ " ` $ escaped, then the string-level \ and %
  local exe="$HERE/iphone.sh" q s
  q=$(printf '%s' "$exe" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g' -e 's/%/%%/g')
  s=$(printf '%s' "$exe" | sed -e 's/\\/\\\\/g')
  cat <<EOF
[Desktop Entry]
Type=Application
Name=iPhone Control
GenericName=iPhone Mirror
Comment=Mirror and control the iPhone over USB
Exec="$q"
TryExec=$s
Icon=phone
Terminal=false
StartupNotify=false
StartupWMClass=iphone-control
Categories=Utility;
Keywords=iPhone;iOS;mirror;phone;
EOF
}
entry="$APPS/iphone-control.desktop"
if [[ $HERE == *$'\n'* ]]; then
  echo "skipped: the repo path contains a newline"
elif [ "$(cat "$entry" 2>/dev/null)" = "$(desktop_entry)" ]; then
  echo "already up to date"
elif [ $CHECK = 1 ]; then
  if [ -e "$entry" ]; then report "out of date (setup.sh would rewrite it)"; else report "not installed"; fi
else
  mkdir -p "$APPS"
  desktop_entry >"$entry.tmp" && chmod 644 "$entry.tmp" && mv -f "$entry.tmp" "$entry"
  if command -v desktop-file-validate >/dev/null; then desktop-file-validate "$entry" || true; fi
  if command -v update-desktop-database >/dev/null; then update-desktop-database -q "$APPS" 2>/dev/null || true; fi
  echo "ok (search 'iPhone Control' in the launcher)"
fi

# ---------------------------------------------------------------- 7. window manager (user config only changes with --hyprland)
say "window manager"
HYPR="$CONF/hypr"
LUA_RULE='hl.window_rule({ match = { class = "^iphone-control$" }, float = true, center = true,
  size = { "(monitor_h*83/200)", "(monitor_h*9/10)" } })'
hyprlang_rule() {
  cat <<'EOF'
windowrule {
  name = iphone-control
  match:class = ^(iphone-control)$
  float = on
  center = on
  size = (monitor_h*83/200) (monitor_h*9/10)
}
EOF
}
hypr_install_lua() {  # the rule in its own file + one guarded dofile line at the end of hyprland.lua (backed up first)
  local rule="$HYPR/iphone-control.lua" lpath
  printf -- '-- iPhone Control (%s/setup.sh --hyprland): float the mirror window at phone size, centred.\n-- To remove: delete this file and the iphone-control lines at the end of hyprland.lua.\n%s\n' \
    "$HERE" "$LUA_RULE" >"$rule"
  lpath=$(printf '%s' "$rule" | sed 's/[\\"]/\\&/g')
  cp -p "$HYPR/hyprland.lua" "$HYPR/hyprland.lua.bak.$(date +%s)"
  printf '\n-- iphone-control window rule (added by setup.sh --hyprland)\ndo local f = "%s"; local h = io.open(f, "r"); if h then h:close(); dofile(f) end end\n' \
    "$lpath" >>"$HYPR/hyprland.lua"
  echo "installed $rule and loaded it from $HYPR/hyprland.lua (backup next to it); Hyprland reloads on save"
}
if [ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] || [ $HYPR_INSTALL = 1 ]; then
  have=$(grep -rlsF --include='*.lua' --include='*.conf' 'iphone-control' "$HYPR" 2>/dev/null | head -n1) || have=""
  if [ -n "$have" ]; then
    echo "Hyprland: a rule for class iphone-control is already in $have"
  elif [ $HYPR_INSTALL = 1 ] && [ -f "$HYPR/hyprland.lua" ]; then
    if [ $CHECK = 1 ]; then report "Hyprland: float rule not installed yet (setup.sh --hyprland adds it)"; else hypr_install_lua; fi
  elif [ -f "$HYPR/hyprland.lua" ]; then
    printf 'Hyprland: to float the window at phone size, rerun with --hyprland or add to %s:\n%s\n' "$HYPR/hyprland.lua" "$LUA_RULE"
  else
    if [ $HYPR_INSTALL = 1 ]; then echo "Hyprland: no Lua config ($HYPR/hyprland.lua), so nothing was changed."; fi
    echo "Hyprland: to float the window at phone size, add to hyprland.conf:"
    hyprlang_rule
    echo "(before Hyprland 0.53:  windowrule = float, class:^(iphone-control)\$  and  windowrule = size 23% 90%, class:^(iphone-control)\$)"
  fi
elif [ -n "${SWAYSOCK:-}" ]; then
  echo "sway: to float the window, add to ~/.config/sway/config:"
  echo '  for_window [app_id="^iphone-control$"] floating enable, resize set 420 900'
elif [ -n "${I3SOCK:-}" ]; then
  echo "i3: to float the window, add to ~/.config/i3/config:"
  echo '  for_window [class="(?i)^iphone-control$"] floating enable, resize set 420 900'
else
  echo "nothing needed (${XDG_CURRENT_DESKTOP:-this desktop} opens it as a normal window)"
fi

# ---------------------------------------------------------------- done
PLUME="$CONF/PlumeImpactor"   # plumesign's Apple ID token (accounts.json) and signing key (keys/*/key.pem)
LOGIN="" LOGIN_ERR=""   # in | out | broken (plumesign did not run; LOGIN_ERR = its last line)
login_state() {  # one `account list`: its output and exit status are both checked
  local out rc=0
  LOGIN=out LOGIN_ERR=""
  [ -x "$BIN/plumesign" ] || return 0
  out=$(RUST_LOG=info "$BIN/plumesign" account list </dev/null 2>&1) || rc=$?
  if grep -q "No accounts found" <<<"$out"; then return 0; fi
  if [ $rc = 0 ]; then LOGIN=in; return 0; fi
  LOGIN=broken
  LOGIN_ERR=$(grep -v '^[[:space:]]*$' <<<"$out" | tail -n 1 | cut -c1-200) || true
  LOGIN_ERR="exit $rc${LOGIN_ERR:+: $LOGIN_ERR}"
}
[ $BASIC = 1 ] || login_state
broken_msg="plumesign does not run ($LOGIN_ERR) - ./setup.sh --rebuild"
if [ $CHECK = 1 ] && [ $BASIC = 1 ]; then
  echo
  if [ $problems = 0 ]; then echo "everything basic mode needs is in place"; else echo "$problems thing(s) to fix - run ./setup.sh --basic"; fi
  exit $((problems > 0))
fi
if [ $CHECK = 1 ]; then
  say "Apple ID"
  if [ -d "$PLUME" ] && [ -n "$(find "$PLUME" -perm /077 -print -quit 2>/dev/null)" ]; then
    report "$PLUME (Apple ID token + signing key) is readable by other users; setup.sh makes it private"
  fi
  case $LOGIN in
    in) echo "logged in" ;;
    broken) report "$broken_msg" ;;
    *) report "not logged in: $BIN/plumesign account login -u YOUR_APPLE_ID_EMAIL" ;;
  esac
  echo
  if [ $problems = 0 ]; then echo "everything is in place"; else echo "$problems thing(s) to fix - run ./setup.sh"; fi
  exit $((problems > 0))
fi

say "done"
if [ -d "$PLUME" ]; then chmod -R go-rwx "$PLUME"; fi
if [ $BASIC = 1 ]; then
  echo "Next: plug in + unlock the iPhone and run   $HERE/iphone.sh   (basic mode: the window shows how to pair it"
  echo "      over Bluetooth and turn on AssistiveTouch)"
  if [ -n "$muxv" ] && ! mux_in_place && [ "$(printf '%s\n1.1.1\n' "$muxv" | sort -V | tail -n1)" = 1.1.1 ]; then
    echo "      For the picture over USB, also run   $HERE/setup.sh --usbmuxd   (or install uxplay 1.73+ for AirPlay)"
  fi
elif [ "$LOGIN" = broken ]; then
  die "$broken_msg"
elif [ "$LOGIN" = out ]; then
  echo "Next: in a normal terminal run   $BIN/plumesign account login -u YOUR_APPLE_ID_EMAIL"
  echo "      then plug in + unlock the iPhone and run   $HERE/resign.sh"
else
  expires=$(grep -o '"expires_at": *[0-9]*' "$STATE/config.json" 2>/dev/null | grep -o '[0-9]*$' || true)
  if [ -n "$expires" ] && [ "$expires" -gt $(($(date +%s) + 3600)) ]; then   # already signed and installed
    echo "Next: plug in + unlock the iPhone and run   $HERE/iphone.sh"
    echo "      (WebDriverAgent is signed until $(date -d "@$expires" '+%d %b %Y' 2>/dev/null || echo "@$expires"))"
  else
    echo "Next: plug in + unlock the iPhone and run   $HERE/resign.sh   (once), then   $HERE/iphone.sh"
  fi
fi
