#!/usr/bin/env bash
# Remove what setup.sh and resign.sh installed on this computer; the repo itself stays.
#   ./uninstall.sh         ask before each step
#   ./uninstall.sh --yes   remove programs, launcher entry and caches without asking (your pairing key, Apple ID
#                          session, settings and the usbmuxd of setup.sh --usbmuxd only when you answer y)
# System packages are never touched. To take WebDriverAgent off the iPhone, see the end of the output.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/iphone-control"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/iphone-control"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/iphone-control.desktop"
PLUME_DATA="${XDG_CONFIG_HOME:-$HOME/.config}/PlumeImpactor"   # plumesign's saved Apple ID session
RUN="${XDG_RUNTIME_DIR:-$STATE/run}/iphone-control"
HYPR="${XDG_CONFIG_HOME:-$HOME/.config}/hypr"
HYPR_MARK='-- iphone-control window rule (added by setup.sh --hyprland)'   # setup.sh writes it + one dofile line
MUX_BIN=/usr/local/sbin/usbmuxd-iphone-control                        # setup.sh --usbmuxd: our usbmuxd build
MUX_DROPIN=/etc/systemd/system/usbmuxd.service.d/iphone-control.conf   # ... and the unit override that starts it
SYSTEMD_RUN=/run/systemd/system                                       # there when systemd is the init
YES=0

case "${1:-}" in
  "") ;;
  -y|--yes) YES=1 ;;
  -h|--help) sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p;}' "$0"; exit 0 ;;
  *) echo "usage: $0 [--yes | --help]"; exit 2 ;;
esac

ask() {  # ask QUESTION [auto]: yes/no; "auto" = yes under --yes
  local a
  [ "${2:-}" = auto ] && [ $YES = 1 ] && return 0
  if [ ! -t 0 ]; then echo "$1 [y/N] n (no terminal)"; return 1; fi
  read -rp "$1 [y/N] " a
  case $a in [yY]*) return 0 ;; *) return 1 ;; esac
}
gone() { rm -rf -- "$@" && printf 'removed %s\n' "$@"; }
as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

echo "== stopping iPhone Control"
[ -x "$HERE/iphone.sh" ] && "$HERE/iphone.sh" stop

# Only remove binaries this project put there: go-ios at the pinned version, plumesign with setup's stamp
pinned=$(sed -n 's/^GO_IOS_VERSION=v\{0,1\}\([0-9.]*\).*/\1/p' "$HERE/setup.sh" 2>/dev/null)
targets=()
[ -f "$DESKTOP" ] && targets+=("$DESKTOP")
if [ -x "$BIN/ios" ]; then
  if "$BIN/ios" version 2>/dev/null | grep -q "${pinned:-1.3.2}"; then targets+=("$BIN/ios")
  else echo "keeping $BIN/ios (not the go-ios ${pinned:-1.3.2} setup.sh installs)"; fi
fi
if [ -x "$BIN/plumesign" ]; then
  if [ -f "$STATE/plumesign.stamp" ]; then targets+=("$BIN/plumesign")
  else echo "keeping $BIN/plumesign (not built by setup.sh)"; fi
fi
[ -d "$CACHE" ] && targets+=("$CACHE")              # go-ios download, Impactor source and its cargo build (GBs)
[ -d "$HERE/build" ] && targets+=("$HERE/build")    # WebDriverAgent.ipa (setup.sh rebuilds it)
for f in iphone.log fwd.log wda.log wda.prev.log tunnel.log image.log window.log gst.log resign.log ca.log \
         cap.log cap.prev.log cap.sock cap.status uxplay.log mode lock; do
  [ -e "$RUN/$f" ] && targets+=("$RUN/$f")
done

echo "== Apple ID session (plumesign)"
if [ -d "$PLUME_DATA" ]; then
  if ask "Log out and delete the saved Apple ID session in $PLUME_DATA?"; then
    # log out while plumesign is still installed, so it drops its tokens too
    [ -x "$BIN/plumesign" ] && "$BIN/plumesign" account logout >/dev/null 2>&1
    gone "$PLUME_DATA"
  else echo "kept (later: $BIN/plumesign account logout, then delete $PLUME_DATA)"; fi
else echo "none saved"; fi

echo "== programs, launcher entry, caches"
if [ ${#targets[@]} -eq 0 ]; then echo "nothing to remove"
else
  printf '  %s\n' "${targets[@]}"
  if ask "Remove these?" auto; then gone "${targets[@]}"; else echo "kept"; fi
fi

echo "== Hyprland window rule (setup.sh --hyprland)"
hypr_lines() { [ -f "$HYPR/hyprland.lua" ] && grep -qxF -- "$HYPR_MARK" "$HYPR/hyprland.lua"; }
if [ -f "$HYPR/iphone-control.lua" ] || hypr_lines; then
  [ -f "$HYPR/iphone-control.lua" ] && echo "  $HYPR/iphone-control.lua"
  hypr_lines && echo "  2 lines at the end of $HYPR/hyprland.lua (\"$HYPR_MARK\" and the dofile line after it)"
  if ask "Remove them (hyprland.lua is backed up next to it first)?"; then
    if hypr_lines; then
      tmp=$(mktemp "$HYPR/.hyprland.lua.XXXXXX") &&
        awk -v m="$HYPR_MARK" '
          skip { skip = 0; if (index($0, "do local f = ") == 1 && index($0, "iphone-control.lua")) next }
          $0 == m { held = 0; skip = 1; next }       # the blank line setup.sh put before it goes too
          held { print ""; held = 0 }
          $0 == "" { held = 1; next }
          { print }
          END { if (held) print "" }' "$HYPR/hyprland.lua" >"$tmp" &&
        cp -p "$HYPR/hyprland.lua" "$HYPR/hyprland.lua.bak.$(date +%s)" &&
        cat "$tmp" >"$HYPR/hyprland.lua" &&   # in place: keeps a symlinked (dotfiles) config and its mode
        echo "removed the iphone-control lines from $HYPR/hyprland.lua (backup next to it)" ||
        echo "could not edit $HYPR/hyprland.lua - remove the two lines by hand"
      rm -f "${tmp:-}"
    fi
    [ -f "$HYPR/iphone-control.lua" ] && gone "$HYPR/iphone-control.lua"
  else echo "kept"; fi
else echo "none installed"; fi
[ -d "$HYPR" ] && echo "  (a windowrule for class iphone-control you added to hyprland.conf yourself stays: remove it by hand)"

echo "== usbmuxd for USB video (setup.sh --usbmuxd)"
mux=()
for f in "$MUX_DROPIN" "$MUX_BIN"; do [ -e "$f" ] && mux+=("$f"); done
# a drop-in directory this user cannot search (made under a strict umask) hides the override: removing only the
# program would leave usbmuxd's unit starting a missing file
d=$(dirname "$MUX_DROPIN")
if [ -d "$d" ] && [ ! -x "$d" ]; then mux=("$MUX_DROPIN" ${mux[@]+"${mux[@]}"}); fi
if [ ${#mux[@]} -eq 0 ]; then echo "none installed"
else
  printf '  %s\n' "${mux[@]}"
  if ask "Remove them with sudo (usbmuxd restarts as the distro's version)?"; then
    if as_root rm -f -- "${mux[@]}"; then
      printf 'removed %s\n' "${mux[@]}"
      rm -f "$STATE/usbmuxd.stamp"
      as_root rmdir "$(dirname "$MUX_DROPIN")" 2>/dev/null   # only when nothing else overrides usbmuxd
      if [ -d "$SYSTEMD_RUN" ] && command -v systemctl >/dev/null; then
        as_root systemctl daemon-reload && as_root systemctl restart usbmuxd ||
          echo "  usbmuxd did not restart (systemctl status usbmuxd says why); it starts again with the iPhone"
      fi
    else echo "could not remove them - by hand: sudo rm ${mux[*]}; sudo systemctl daemon-reload; sudo systemctl restart usbmuxd"; fi
  else echo "kept (later: sudo rm ${mux[*]}, then sudo systemctl daemon-reload; sudo systemctl restart usbmuxd)"; fi
fi

echo "== machine state ($STATE)"
if [ -d "$STATE" ]; then
  echo "  the tunnel pairing key, WebDriverAgent's bundle id and signature date, developer disk images"
  if ask "Delete $STATE?"; then gone "$STATE"; else echo "kept"; fi
else echo "none"; fi

cat <<EOF

Done. The repo's own files in $HERE are untouched; ./setup.sh sets everything up again.
On the iPhone (optional):
  - long-press WebDriverAgentRunner on the home screen > Remove App > Delete App
  - Settings > General > VPN & Device Management: remove your Apple ID's developer profile if it is still listed
  - Settings > Privacy & Security > Developer Mode: turn it off if nothing else needs it
  - to forget this computer: Settings > General > Transfer or Reset iPhone > Reset > Reset Location & Privacy
EOF
