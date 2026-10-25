# iPhone Control

Mirror and control an iPhone from a Linux computer over the USB cable. The phone's live screen appears in a
window; clicks, drags, scrolling and typing become real taps, swipes and keystrokes on the phone.
No Mac, no jailbreak, no paid developer account.

It has two modes. **Full mode** runs Appium's [WebDriverAgent](https://github.com/appium/WebDriverAgent) on the
phone (an XCTest runner that takes touch commands over HTTP), starts it from Linux with
[go-ios](https://github.com/danielpaulus/go-ios), and signs it with your Apple ID using a patched build of
[Impactor's](https://github.com/claration/impactor) `plumesign`. **Basic mode** needs no Developer Mode, no Apple ID
and no signing: the laptop becomes a Bluetooth mouse and keyboard, and the phone's AssistiveTouch pointer does the
touching. Both show the screen through the phone's own USB screen capture, the one QuickTime uses on a Mac (it
needs a usbmuxd newer than 1.1.1; without one, WebDriverAgent's stream or AirPlay stands in).

Not affiliated with or endorsed by Apple. iPhone and iOS are trademarks of Apple Inc.

- [Two modes](#two-modes)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Set up](#set-up-once-per-computer)
- [Use](#use)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations) and [FAQ](#faq)
- [Security and privacy](#security-and-privacy)
- [Uninstall](#uninstall)
- [Files](#files), [credits](#credits) and [license](#license)

More detail: [docs/how-it-works.md](docs/how-it-works.md) (the USB capture, Bluetooth, tunnel, disk image, signing,
the patches) and [CONTRIBUTING.md](CONTRIBUTING.md).

## Two modes

| | Full mode | Basic mode |
|---|---|---|
| Input | WebDriverAgent on the phone: W3C touches, typing, buttons | the laptop as a Bluetooth LE mouse and keyboard; with AssistiveTouch on, the phone's pointer taps |
| Needs | Developer Mode; WebDriverAgent signed with an Apple ID (a free one is enough; re-signed every 7 days, by itself); iOS 17.4.1 or newer; an x86_64 or aarch64 laptop | a Bluetooth LE adapter (BlueZ); AssistiveTouch on. No Developer Mode, Apple ID or signing |
| Picture | the USB screen capture; WebDriverAgent's MJPEG stream when that cannot start | the USB screen capture; AirPlay (UxPlay, over Wi-Fi) when that cannot start |
| Set up | `./setup.sh`, an Apple ID login, `./resign.sh` | `./setup.sh --basic` |

`./iphone.sh` takes full mode when go-ios, a signed WebDriverAgent and Developer Mode are in place, and basic mode
otherwise, and says which and why (`IPHONE_MODE=full` or `basic` decides by hand).

| | Full mode | Basic mode |
|---|---|---|
| taps | exact, sent at once | exact (the pointer is placed absolutely, no calibration) |
| drags | replayed when the mouse button is released (in about 0.25 s, ending at your speed) | live: the phone follows the mouse |
| double taps | a double-click is two taps about 330 ms apart, which iOS reads as a double tap (10 of 10 in a text field) | a double-click is two taps (not checked on a phone yet) |
| typing | any text, input methods and dead keys, 120 characters a second | what a US keyboard can type |
| clipboard | Ctrl+V types the laptop's; Ctrl+Shift+V / Ctrl+Shift+C copy to and from the iPhone's | Ctrl+V types the laptop's; the iPhone's is out of reach |
| open an app or URL | Ctrl+O by bundle id, Ctrl+L a URL | Ctrl+L only, typed into Spotlight (not checked on a phone yet) |
| pinch zoom | yes | no |
| Home, App Switcher, Control Centre, notifications, volume | yes | yes (a Bluetooth keyboard's keys) |
| latency: a tap, from sending it to a changed picture | 196 ms on the USB picture, 249 ms on MJPEG | 280-440 ms |

Measured on an iPhone 15 Pro with iOS 26.5.2 (Oct 2026); more under [Limitations](#limitations).

## How it works

```
 Laptop (Linux)                                                     iPhone
 --------------                                                     ------
 "iPhone (USB)" window  <-- H.264 --  iphone_control.screencap  <==  USB screen capture (no Developer Mode)
 GTK 3 + GStreamer                    ($RUN/cap.sock, private)
 (wda_mirror.py)
   full mode:           --- HTTP  -->  127.0.0.1:8100  --+          WebDriverAgentRunner (XCTest)
                        <-- MJPEG ---  127.0.0.1:9100  --+            :8100  taps, swipes, keys (W3C)
                        iphone_control.forward           |            :9100  the screen as MJPEG
                        (loopback only, blocks web pages)|
   basic mode:          --- Bluetooth LE (BlueZ): mouse + keyboard -->  AssistiveTouch pointer

 usbmuxd (system service)  ============ USB cable ==============   lockdown, usbmux, screen capture

 full mode only:
 go-ios   ios tunnel start --userspace   iOS 17+ developer tunnel, no root
          ios image auto                 developer disk image, personalised by Apple (gs.apple.com)
          ios runwda                     launches WebDriverAgent on the phone
 plumesign (resign.sh)                   signs WebDriverAgent with your Apple ID and installs it
```

`./iphone.sh` waits for the phone to be plugged in, trusted and unlocked, picks the mode and starts the USB screen
capture. In full mode it then opens the developer tunnel, mounts the developer disk image, starts WebDriverAgent
and forwards its two ports to 127.0.0.1; in basic mode the window registers the laptop with BlueZ as a Bluetooth LE
mouse and keyboard. A watchdog restarts whatever drops while the window is open. Closing the window stops
everything.

## Requirements

| | |
|---|---|
| Computer | Linux; Wayland or X11, any desktop. Full mode: x86_64 or aarch64 (go-ios ships no other Linux builds); basic mode: any CPU |
| iPhone | full mode: iOS 17.4.1 or newer (tested on 26.5.2); iOS 17.0 to 17.4.0 cannot use the rootless tunnel, iOS 16 and older need no tunnel but are untested. Basic mode and the USB picture: only tried on 26.5.2 (see [Tested](#tested)) |
| Cable | a data cable (a charge-only cable shows no phone) |
| Apple ID | full mode only: any Apple ID with two-factor authentication; a free one is enough. A second, spare Apple ID is a good idea (see [Security](#security-and-privacy)) |
| Python | 3.9 or newer with the distro's PyGObject (GTK 3 and GStreamer bindings). Standard library only, no pip |
| GStreamer | `videoconvert` (base), `gtksink`; WebDriverAgent's picture (`IPHONE_VIDEO=mjpeg`, or full mode's fallback when the USB picture cannot start): `souphttpsrc`, `multipartdemux`, `jpegdec` (good plugins); the USB picture: `appsrc` (base), `h264parse` (bad plugins) and an H.264 decoder (`vah264dec`, `avdec_h264` or `openh264dec`); AirPlay also `udpsrc` and `rtph264depay` (good plugins) |
| usbmuxd | running. Systemd distros start it through udev when the phone is plugged in; without systemd run `sudo usbmuxd`. The USB picture needs one newer than 1.1.1: [`./setup.sh --usbmuxd`](#the-usb-picture-a-newer-usbmuxd) |
| Bluetooth | basic mode: BlueZ (`bluetoothd` running) and an adapter that can do Bluetooth LE (4.0 or newer) |
| Rust | full mode: 1.88 or newer, only to build `plumesign` once (see below) |
| Internet | on the laptop for setup; in full mode also for re-signing and for the disk-image mount after every phone restart, and on the phone once, for the Trust step |
| AirPlay (optional) | basic mode's picture when the USB one cannot start: UxPlay 1.73 or newer, `avahi-daemon` running, and phone and laptop on one Wi-Fi network that lets devices see each other (eduroam and most guest networks do not) |

### Packages per distro

`./setup.sh` detects the distro and prints the exact install line for whatever is missing (`./setup.sh --basic`
prints basic mode's, a shorter one). Full mode, in full:

**Debian 12+ / Ubuntu 22.04+**

```
sudo apt install usbmuxd python3 python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-gtk3 git curl zip unzip build-essential libnotify-bin
```

Debian's and Ubuntu's own Rust is older than 1.88, so install it with [rustup](https://rustup.rs) (Debian 13 can
also use `sudo apt install -t trixie-backports rustc cargo`):

```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
. "$HOME/.cargo/env"
```

**Fedora**

```
sudo dnf install usbmuxd python3 python3-gobject gtk3 gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
  libsoup3 gstreamer1-plugins-good-gtk git curl zip unzip gcc libnotify cargo rust
```

**openSUSE Tumbleweed** (Leap 15's `python3` is 3.6, too old)

```
sudo zypper install usbmuxd python3 python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-Gst-1_0 \
  gstreamer-plugins-base gstreamer-plugins-good libsoup-3_0-0 gstreamer-plugins-good-gtk git curl zip unzip gcc \
  libnotify-tools cargo
```

**Arch Linux / Manjaro**

```
sudo pacman -S --needed usbmuxd python python-gobject gtk3 gstreamer gst-plugins-base gst-plugins-good \
  gst-plugin-gtk git curl zip unzip base-devel libnotify rust
```

If the distro's `cargo` is older than 1.88 (`cargo --version`), use rustup instead. libimobiledevice's tools are
not needed. Optional: `openssl` (adds the signing certificate's end date to the expiry check), `iproute2` (`ss`,
for port checks), `util-linux` (`flock`, stops two starts at once; installed almost everywhere).

**The USB picture and basic mode** add the bad plugins (`h264parse`), an H.264 decoder and BlueZ (`./setup.sh`
lists them as "optional"):

| Distro | Packages |
|---|---|
| Debian / Ubuntu | `gstreamer1.0-plugins-bad gstreamer1.0-libav bluez` |
| Fedora | `gstreamer1-plugins-bad-free gstreamer1-plugin-openh264 bluez` |
| openSUSE | `gstreamer-plugins-bad gstreamer-plugin-openh264 bluez` (or `gstreamer-plugins-libav` from Packman) |
| Arch / Manjaro | `gst-plugins-bad gst-libav bluez bluez-utils` |

Basic mode alone needs neither Rust, git, curl, zip nor the MJPEG plugins. These package names come from
`setup.sh`'s table; apart from Arch's they are not tried yet.

**UxPlay** (optional, for the AirPlay picture): 1.73 or newer. Arch: `uxplay` from the AUR. Fedora 43:
`sudo dnf install uxplay`. Debian 13 (1.71), Ubuntu 24.04 (1.68) and most other distros ship older versions:
build it from [github.com/FDH2/UxPlay](https://github.com/FDH2/UxPlay). It also needs `avahi-daemon`
(`sudo systemctl enable --now avahi-daemon`).

### Tested

| | |
|---|---|
| Arch Linux, x86_64, Hyprland (Wayland), iPhone 15 Pro on iOS 26.5.2 | full mode with WebDriverAgent's picture: works (23 Oct 2026) |
| the same machine and phone: the USB screen capture, and the laptop as a Bluetooth LE mouse with AssistiveTouch | each measured on the phone (Oct 2026); basic mode and the USB picture through `./iphone.sh` and the window: not yet |
| Debian, Ubuntu, Fedora, openSUSE, Manjaro | expected to work; not tested yet |
| aarch64 | expected to work (go-ios has an arm64 build); not tested yet |
| GNOME, KDE, Sway, i3, X11 sessions | expected to work; not tested yet |
| Other iPhones, other iOS 17.4.1+ versions, iPads | expected to work; not tested yet |

Reports for untested combinations are welcome (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Set up (once per computer)

### Full mode

1. **Laptop:** `./setup.sh`. It checks the system packages (and prints the `apt`/`dnf`/`zypper`/`pacman` line
   for anything missing), installs go-ios 1.3.2 to `~/.local/bin/ios`, builds the patched `plumesign` to
   `~/.local/bin/plumesign`, downloads WebDriverAgent and packs `build/WebDriverAgent.ipa`, builds the CA bundle
   and adds "iPhone Control" to the app launcher. Every download is checked against a pinned SHA-256.
   The first run compiles `plumesign`: several minutes and about 2 GB in `~/.cache/iphone-control`.
   Re-running is safe and skips what is in place; `./setup.sh --check` only reports.
2. **Phone:** plug it in, unlock it, tap **Trust This Computer** and enter the passcode.
3. **Developer Mode:** on a phone that has never seen Xcode the switch is hidden. Run
   `~/.local/bin/ios devmode reveal`, then on the phone Settings > Privacy & Security > **Developer Mode** > on.
   The phone restarts; unlock it and tap **Turn On**. (The switch also appears by itself after step 5.)
4. **Apple ID**, in a normal terminal (it asks for the password and the two-factor code, so it cannot run from the
   launcher):
   `~/.local/bin/plumesign account login -u you@example.com`
5. **Sign and install WebDriverAgent:** `./resign.sh` (about a minute; keep the phone unlocked).
6. **Phone:** Settings > General > **VPN & Device Management** > your Apple ID > **Trust**. The phone must be
   online for this one-time check (Wi-Fi or mobile data).
7. For the USB picture: `./setup.sh --usbmuxd` ([below](#the-usb-picture-a-newer-usbmuxd)). Without it, full mode
   shows WebDriverAgent's MJPEG stream.
8. `./iphone.sh`

### Basic mode

1. **Laptop:** `./setup.sh --basic`. It checks only what basic mode needs (Python, PyGObject, GTK, GStreamer with
   H.264, BlueZ), prints the install line for anything missing and adds "iPhone Control" to the app launcher.
   Nothing is downloaded or built, and any CPU will do. `./setup.sh --basic --check` only reports.
2. For the USB picture: `./setup.sh --usbmuxd` ([below](#the-usb-picture-a-newer-usbmuxd)). Without it, basic mode
   needs UxPlay for an AirPlay picture.
3. **Phone:** plug it in, unlock it, tap **Trust This Computer** and enter the passcode.
4. `./iphone.sh`. The window shows what to do on the phone until it is connected:
5. **Phone:** Settings > Bluetooth > "*&lt;hostname&gt;* Mouse" > **Pair**. Both screens show a
   six-digit code; if they match, answer **Yes** in the window.
6. **Phone:** Settings > Accessibility > Touch > **AssistiveTouch** on. A pointer appears; clicks in the
   window now tap.

The pairing is kept for later starts (not yet checked over many restarts). A phone that was paired with the old
`alt/bluetooth-mouse` or an earlier version must forget it first (Settings > Bluetooth > (i) next to the name >
**Forget This Device**): iOS keeps the old report map, and clicks then do nothing or land in the wrong place.

With the AirPlay picture, the phone mirrors over Wi-Fi: on the iPhone, Control Centre > **Screen Mirroring** > the
laptop's name (the hostname, or `IPHONE_AIRPLAY_NAME`). With a firewall, UxPlay needs TCP 7000, 7001 and 7100, UDP
6000, 6001 and 7011, and mDNS (UDP 5353). firewalld (Fedora, openSUSE):

```
sudo firewall-cmd --add-service=mdns --add-port=7000-7001/tcp --add-port=7100/tcp --add-port=6000-6001/udp --add-port=7011/udp
```

(add `--permanent` and run it again to keep the rules). ufw: `sudo ufw allow 5353/udp`, `sudo ufw allow 7000:7001/tcp`,
`sudo ufw allow 7100/tcp`, `sudo ufw allow 6000:6001/udp`, `sudo ufw allow 7011/udp`.

### The USB picture: a newer usbmuxd

The phone's screen capture lives in a USB configuration that the phone adds only on request. usbmuxd 1.1.1, the
last release, switches the phone straight back to its usual configuration, which has no capture interface
(`./iphone.sh doctor`: "usbmuxd keeps the iPhone in USB configuration 4"). usbmuxd from git keeps it when started
with `USBMUXD_DEFAULT_DEVICE_MODE=2`, and lockdown, the tunnel and WebDriverAgent keep working beside the capture
(measured on iOS 26.5.2). `./setup.sh --usbmuxd`:

- builds libimobiledevice's [usbmuxd](https://github.com/libimobiledevice/usbmuxd) at a pinned commit in
  `~/.cache/iphone-control/usbmuxd` (log: `usbmuxd-build.log` next to it). It needs git, a C compiler, make,
  autoconf, automake, libtool, pkg-config and the libplist, libusb, libimobiledevice-glue and libimobiledevice
  headers; it prints the install line for what is missing;
- installs, with sudo, `/usr/local/sbin/usbmuxd-iphone-control` and a systemd drop-in,
  `/etc/systemd/system/usbmuxd.service.d/iphone-control.conf`, that sets `USBMUXD_DEFAULT_DEVICE_MODE=2` and starts
  that binary instead of the distro's (as the distro's `usbmux` or `usbmuxd` user), then runs
  `systemctl daemon-reload` and `systemctl restart usbmuxd` (a plugged-in phone reconnects). The modes are set
  explicitly, whatever the umask: the program 755, the drop-in 644 in a directory of 755;
- prints all of this first and asks (`--yes` answers for you); `./setup.sh --usbmuxd --check` only reports. The
  distro's usbmuxd package stays installed, and `./uninstall.sh` goes back to it.

The build needs libplist 2.3 or newer (commit `e9a0dce` for 2.3-2.5, `3ded00c` for 2.6 and later). Debian 11 and 12
and Ubuntu 20.04 and 22.04 have libplist 2.2 or older: there `--usbmuxd` explains this and changes nothing, and the
picture stays WebDriverAgent's MJPEG stream in full mode and AirPlay in basic mode. Without systemd it builds
usbmuxd and prints how to install and run it by hand, but installs nothing; once it is installed by hand at
`/usr/local/sbin/usbmuxd-iphone-control`, `--check` and a re-run recognise it.

### Window rules (tiling window managers)

The window's X11 class (`WM_CLASS`) and Wayland `app_id` are both `iphone-control`. GNOME, KDE and other
stacking desktops need nothing. On a tiling window manager, float it at phone size:

- **Hyprland, Lua config:** `./setup.sh --hyprland` writes this rule to `~/.config/hypr/iphone-control.lua`
  and loads it from the end of `hyprland.lua` (backed up first):
  ```
  hl.window_rule({ match = { class = "^iphone-control$" }, float = true, center = true,
    size = { "(monitor_h*83/200)", "(monitor_h*9/10)" } })
  ```
- **Hyprland, `hyprland.conf` (0.53 and later):**
  ```
  windowrule {
    name = iphone-control
    match:class = ^(iphone-control)$
    float = on
    center = on
    size = (monitor_h*83/200) (monitor_h*9/10)
  }
  ```
  0.48 to 0.52: `windowrule = float, class:^(iphone-control)$` and
  `windowrule = size 23% 90%, class:^(iphone-control)$`; older: the same with `windowrulev2 = float,
  class:^(iphone-control)$`. The syntax changes between releases; see the
  [Hyprland wiki](https://wiki.hypr.land/Configuring/Window-Rules/).
- **Sway** (`~/.config/sway/config`): `for_window [app_id="^iphone-control$"] floating enable, resize set 420 900`
- **i3** (`~/.config/i3/config`): `for_window [class="(?i)^iphone-control$"] floating enable, resize set 420 900`
- **KDE**, optional: System Settings > Window Management > Window Rules > Add New, window class `iphone-control`.

`./setup.sh` prints the right one for the session it runs in.

## Use

Plug the iPhone in, unlock it, then start "iPhone Control" from the app launcher, or:

```
./iphone.sh            # start (the default); waits for plug-in, Trust and unlock
./iphone.sh status     # what is running, which phone and mode, when the signature runs out
./iphone.sh doctor     # check every dependency and state file, print fixes (exit 1 on a problem)
./iphone.sh stop       # stop everything left running
```

Closing the window stops everything. Started from the launcher, messages arrive as desktop notifications (also
when `IPHONE_MODE=auto` picks basic mode, with the reason).

### Mouse

| In the window | Full mode | Basic mode |
|---|---|---|
| click | tap, sent at once | tap |
| double-click | double tap (two taps) | double tap (two taps; not checked on a phone yet) |
| right-click, or click and hold (over 0.45 s) | long press | long press, held at least 0.8 s |
| drag | swipe (sent when the button is released, then replayed in about 0.25 s; the end keeps your speed) | swipe, live |
| hold still, then drag | drag an icon or a list item | the same, live (not checked on a phone yet) |
| mouse wheel, two-finger touchpad scroll (also sideways) | scroll | scroll (short drags of the pointer) |
| Ctrl + wheel, touchpad pinch | pinch zoom | - |
| mouse back button, Alt+Left | back (swipe from the left edge) | back (the same swipe) |

### Keyboard

| Key | Full mode | Basic mode |
|---|---|---|
| typing | text into the focused field; input methods (IBus, Fcitx), dead keys and compose work | the same, but only what a US keyboard can type (the window says what it skipped) |
| Enter, Backspace, Tab | the same on the phone | the same |
| arrows, Delete, Home, End, Page Up/Down, Escape | the hardware keys | the same keys |
| Shift+arrows, Shift+Tab | the same with Shift (iOS 17 and later) | the same |
| Ctrl+V | type the laptop clipboard | the same |
| Ctrl+Shift+V | copy the laptop clipboard to the iPhone clipboard (then paste on the phone) | - |
| Ctrl+Shift+C | copy the iPhone clipboard to the laptop | - |
| Ctrl+L | open a URL (`https://` is added when there is no scheme) | the same, typed into Spotlight (not checked on a phone yet) |
| Ctrl+O | open an app by bundle id, e.g. `com.apple.mobilesafari` (remembers the last 8) | - |
| F1 | Home | Home |
| F2 | app switcher | app switcher |
| F3 / F4 | volume down / up | volume down / up |
| F5 | Control Centre | Control Centre |
| F6 | lock / unlock | lock (not checked on a phone yet) |
| F7 | notifications | notifications |
| F8 | Action button | - |
| F12, Ctrl+S | screenshot, saved as a PNG in `<Pictures>/iPhone` (the XDG Pictures folder; or `IPHONE_SCREENSHOT_DIR`) | the same, from the picture's last frame |
| Ctrl+Q, Ctrl+W | close | close |

What basic mode cannot do shows a message instead ("Ctrl+O needs full mode (WebDriverAgent)"). The toolbar under
the picture has the same Home, app switcher, Control Centre, notifications, screenshot, volume and lock buttons,
plus a help button with the list for the mode it runs in.

### Settings (environment variables)

For `./iphone.sh`:

| Variable | Default | |
|---|---|---|
| `IPHONE_MODE` | auto | `full` (WebDriverAgent), `basic` (Bluetooth) or `auto`: full when go-ios, a signed WebDriverAgent, iOS 17.4.1+ and Developer Mode are there, else basic |
| `IPHONE_VIDEO` | auto | `usb` (the USB screen capture), `mjpeg` (WebDriverAgent's stream; full mode only), `airplay` (UxPlay) or `auto`: usb when the capture starts, else mjpeg in full mode and airplay in basic mode (when UxPlay 1.73+ is installed) |
| `IPHONE_AUDIO` | 0 | 1 also asks the USB capture for the phone's sound. On iOS 26.5.2 the phone keeps playing it itself and sends silence, and the window does not play it; kept for later iOS versions |
| `IPHONE_UDID` | | which iPhone, when several are plugged in (`./iphone.sh status` lists them; case and dashes are ignored) |
| `IPHONE_WDA_PORT` | 8100 | local port for WebDriverAgent (must differ from the next one) |
| `IPHONE_MJPEG_PORT` | 9100 | local port for the screen stream |
| `IPHONE_WDA_BIND` | 127.0.0.1 | where WebDriverAgent listens on the phone; empty = also the phone's Wi-Fi address (not recommended) |
| `IPHONE_PYTHON` | `/usr/bin/python3` | the Python 3.9+ with PyGObject to use |
| `GO_IOS_AGENT_PORT` | 60105 | port of go-ios' local tunnel agent |
| `USBMUXD_SOCKET_ADDRESS` | `/var/run/usbmuxd` | another usbmuxd: an absolute socket path, or a numeric `IP:port` such as `127.0.0.1:27015` (go-ios, `plumesign` and the window all read it; `plumesign` does not accept a hostname) |

For the window (passed through by `./iphone.sh`):

| Variable | Default | |
|---|---|---|
| `IPHONE_DOUBLE_CLICK_MS` | 0 | full mode. 0: a click taps at once, and a double-click is two taps, which iOS reads as a double tap. Above 0: a click waits up to that long (at most the desktop's double-click time) for a second one, and the pair goes as one double tap, which also selects a word |
| `IPHONE_FPS` | 30 | WebDriverAgent's stream, frames per second, 1-60. The phone captures at most 28-30 at full size: more only costs its CPU. Under the USB picture the stream still runs, unseen, at this rate: touches are slower without it ([why](docs/how-it-works.md#the-window)) |
| `IPHONE_QUALITY` | 60; 10 under the USB picture | JPEG quality of WebDriverAgent's stream, 10-100; lower = less bandwidth (about 60 Mbit/s at 60 and 30 fps), not less delay |
| `IPHONE_SCALE` | 100 | WebDriverAgent's picture size in % of the phone's resolution, 10-100. Below 100 WebDriverAgent shrinks every frame itself, which costs so much CPU on the phone that iOS stops it every few minutes - lower `IPHONE_QUALITY` instead |
| `IPHONE_FIX_ORIENTATION` | 1 | WebDriverAgent's picture: turn it with landscape apps; 0 = never. The USB picture turns by itself |
| `IPHONE_FAST_INPUT` | 1 | quick touches: WebDriverAgent's `snapshotMaxDepth` 1 and `animationCoolOffTimeout` 0 (a swipe touches down after about 70 ms instead of 290); 0 = WebDriverAgent's defaults ([why](docs/how-it-works.md#latency)) |
| `IPHONE_DECODER` | | the H.264 decoder for the USB and AirPlay picture, a GStreamer element name such as `nvh264dec`; by default the first installed of `vah264dec`, `avdec_h264`, `openh264dec`. `./iphone.sh` checks that it is installed |
| `IPHONE_AIRPLAY_NAME` | the hostname | the AirPlay receiver's name (Control Centre > Screen Mirroring) |
| `IPHONE_HID_NAME` | "*&lt;hostname&gt;* Mouse" | basic mode: the Bluetooth name the phone lists |
| `BT_INDEX` | | basic mode: use Bluetooth adapter hci*N* (default: the first switched-on one that can advertise over LE) |
| `IPHONE_SCREENSHOT_DIR` | `<Pictures>/iPhone` | where F12 saves |
| `IPHONE_WDA_URL` | `http://127.0.0.1:8100` | WebDriverAgent, only when running `wda_mirror.py` by hand (`iphone.sh` sets it) |
| `IPHONE_MJPEG_URL` | `http://127.0.0.1:9100` | the screen stream, likewise |
| `IPHONE_CAPTURE_SOCKET` | | the USB capture's socket, likewise (`iphone.sh` sets `$XDG_RUNTIME_DIR/iphone-control/cap.sock`); by hand, `IPHONE_VIDEO=usb` needs it |

Example: `IPHONE_MODE=basic ./iphone.sh` on a phone that has full mode set up; `IPHONE_QUALITY=40 IPHONE_FPS=20
./iphone.sh` on a slow USB port with WebDriverAgent's picture.

### When the signature runs out: re-signing

Full mode only. Apple limits how long the signed WebDriverAgent runs: **7 days with a free Apple ID**, up to a year
with a paid developer account. `resign.sh` reads the real expiry from the signed app's profile and stores it;
`./iphone.sh status` shows it.

When the expiry is less than an hour away, or WebDriverAgent fails to start because the app is missing or its
signature has expired, `./iphone.sh` re-signs and reinstalls it by itself (about a minute, phone unlocked, laptop
online). It tries at most once an hour, so a persistent failure does not run into Apple's limits; run
`./resign.sh` in a terminal to see why. If the Apple ID session itself has expired, log in again (set-up step 4).

`resign.sh` exit codes: 0 ok, 1 failed, 2 usage, 3 Apple ID not logged in or session expired, 4 iPhone locked.
`./resign.sh --record` imports the expiry of an install made by an older version (no phone or Apple ID needed).

### Free Apple ID limits

- The signature lasts 7 days (re-signed automatically, see above).
- At most 3 sideloaded apps can be active on a phone at once. WebDriverAgent is one; AltStore, SideStore and
  other sideloaded apps count too.
- At most 10 new App IDs per 7 days. Re-signing WebDriverAgent reuses its App ID, so this only matters when
  several sideloading tools share one Apple ID.

## Troubleshooting

Start with `./iphone.sh doctor`: it checks everything (the USB picture and Bluetooth too) and says what to do. Logs
are in `$XDG_RUNTIME_DIR/iphone-control/` (usually `/run/user/<uid>/iphone-control/`, or
`~/.local/state/iphone-control/run/iphone-control/` when `XDG_RUNTIME_DIR` is not set):
`iphone.log` (what `iphone.sh` did, timestamped), `cap.log` and `cap.prev.log` (the USB capture), `tunnel.log`,
`image.log`, `wda.log` and `wda.prev.log`, `fwd.log`, `window.log` (also Bluetooth), `uxplay.log` (AirPlay),
`gst.log`, `resign.log`, `ca.log`.

| You see | Why | Fix |
|---|---|---|
| "No iPhone on USB" | charge-only cable, or usbmuxd not running | a data cable; unlock the phone; `systemctl status usbmuxd` (without systemd: `sudo usbmuxd`) |
| "usbmuxd is not running" | the service did not start | `sudo systemctl start usbmuxd`, or plug the phone in again |
| "Several iPhones are plugged in" | | `IPHONE_UDID=<udid> ./iphone.sh` |
| "This computer is not trusted by the iPhone" | Trust was not tapped, or was reset | unlock, tap Trust, enter the passcode (or `~/.local/bin/ios pair`) |
| "Basic mode: Bluetooth input through AssistiveTouch (…)" | `IPHONE_MODE=auto` did not find full mode's pieces; the brackets say which (Developer Mode off, go-ios or WebDriverAgent missing, iOS too old) | nothing, if basic mode is what you want; otherwise set up full mode, or `IPHONE_MODE=full ./iphone.sh` to see why it fails |
| "No USB video (it needs a newer usbmuxd - ./setup.sh --usbmuxd installs one) - using …" | usbmuxd 1.1.1 keeps the phone out of its capture configuration | `./setup.sh --usbmuxd`. On Debian 12 / Ubuntu 22.04 it cannot be built (libplist 2.2 or older): the MJPEG or AirPlay picture stays |
| "Unlock the iPhone - waiting for it..." (at start, or while the window is open), "No USB video (the iPhone sends no picture - …) - using …" or "No picture from the iPhone - unlock it; if it stays black, restart the iPhone." | the capture got no picture even after a USB reset: usually the phone is stuck in an earlier capture session. A phone locked while the capture runs keeps streaming (never below 39 frames a second over 20 s locked; iPhone 15 Pro, iOS 26.5.2, Oct 2026); one locked before the start was not tried | unlock it; if it stays black, restart the iPhone. While the window is open, the watchdog tries again after a replug or every 30 s |
| "USB video stopped - restarting it..." / "The iPhone stopped sending its screen - starting a new capture..." (in the window) | the capture got no picture for 5 s (15 s after a reset that brought none back), or its process stopped. A new session resets the phone's USB, which also drops WebDriverAgent for about 5 s | nothing: both come back by themselves |
| "The iPhone stopped sending its screen - if it stays black, restart the iPhone." (in the window) | a stuck session that new captures do not fix: 3 restarts in a row brought no picture back, or lost it again within a minute | restart the iPhone |
| "USB video keeps stopping - close the window and start again" | the capture process failed 5 times in a row (an error, a crash or a kill), each within a minute of its start; no-picture exits and unplugs do not count | see `cap.log`; replug the phone, start again |
| "USB video did not start: …" | `IPHONE_VIDEO=usb` and the capture could not start (the rest of the message says why) | as in the rows above, or `IPHONE_VIDEO=auto` |
| "No picture for basic mode: USB video did not start (…), and AirPlay needs uxplay 1.73+ (…)." / "IPHONE_VIDEO=airplay needs uxplay 1.73+ (…) - ./iphone.sh doctor says where to get it." | basic mode has no WebDriverAgent picture, and UxPlay is missing or too old (the brackets say "not installed", or for example "found 1.71") | `./setup.sh --usbmuxd`, or install UxPlay 1.73+ ([UxPlay](#packages-per-distro)) |
| "IPHONE_VIDEO=mjpeg is WebDriverAgent's picture, which basic mode does not have" | | `IPHONE_VIDEO=usb`, `airplay` or `auto` |
| "On the iPhone: Control Centre > Screen Mirroring > …" stays in the window | AirPlay: the phone does not see the receiver | the phone on the same Wi-Fi; `avahi-daemon` running; the firewall ports ([Basic mode](#basic-mode)); a network that lets devices see each other; see `uxplay.log` |
| "On the iPhone: Settings > Bluetooth > … Mouse > Pair, then …" stays in the window | the phone has not connected over Bluetooth | pair it ([Basic mode](#basic-mode) steps 5-6); Bluetooth on, on the phone |
| "the iPhone disconnected from Bluetooth - waiting for it…" | the phone's Bluetooth went off or out of range | switch it on again, come closer |
| connected, but the phone shows no pointer | AssistiveTouch is off | Settings > Accessibility > Touch > AssistiveTouch on |
| the pointer shows, but clicks do nothing or land in the wrong place | the phone keeps the report map of an earlier pairing (the old `alt/bluetooth-mouse`, or an older version of this one) | on the phone: Settings > Bluetooth > (i) next to the name > Forget This Device, then pair again |
| "Bluetooth: … paired without a confirmed code - removed" or "Bluetooth: pairing with … on … refused: …" in `window.log`, or pairing fails | only pairing with a confirmed six-digit code is allowed, within 30 s of answering Yes; a device paired with the desktop's own Bluetooth settings while basic mode runs is removed, and pairing on another adapter is refused | pair from the iPhone and answer Yes in the window; pair other devices while iPhone Control is closed |
| "Bluetooth: bluetoothd is not running: …" | BlueZ is not started | `sudo systemctl enable --now bluetooth` |
| "Bluetooth: Bluetooth is off on hci0: …" / "Bluetooth: no Bluetooth LE adapter found …" | the adapter is off, blocked, or cannot do LE | `rfkill unblock bluetooth`, `bluetoothctl power on`; a Bluetooth 4.0+ adapter; `BT_INDEX=N` for another one |
| "Bluetooth: BlueZ does not allow this user to …" | started outside the desktop session (SSH, another seat) | start it from the desktop, or add the user to the `bluetooth` group |
| "“…” could not be typed over Bluetooth (US keyboard characters only)" | basic mode types like a US keyboard | type it on the phone, or use full mode |
| "Bluetooth: the iPhone is not connected" (when typing), with the pairing or "disconnected" hint below it | basic mode, and the phone is not connected over Bluetooth: nothing was typed | pair it, or bring it back in range (the hint says which) |
| "… needs full mode (WebDriverAgent)" | the clipboard, Ctrl+O, pinch and F8 need WebDriverAgent | full mode |
| "iOS 17.x is too old for the rootless tunnel" | iOS 17.0-17.4.0 (with `IPHONE_MODE=full`; auto takes basic mode) | update the phone |
| "Could not open the developer tunnel" | phone locked, not trusted, or Developer Mode off | unlock it and check both; see `tunnel.log` |
| "Another go-ios agent on port 60105 …" | an `ios` agent started by another tool | `~/.local/bin/ios tunnel stopagent`, or set `GO_IOS_AGENT_PORT` |
| "Unlock the iPhone" / "The iPhone stayed locked" / `DeviceLocked` in `image.log` | the developer disk image mounts only while the phone is unlocked (once after every phone restart) | unlock it and start again |
| "Turn on Developer Mode" | | `~/.local/bin/ios devmode reveal`, then set-up step 3 |
| "Could not mount the developer disk image: … x509: certificate signed by unknown authority" | the CA bundle with Apple's root is missing | `./setup.sh` (or `CA_BUNDLE=/path/to/system-bundle.pem ./setup.sh` if no system bundle is found) |
| "Could not mount the developer disk image" (other errors) | no internet, or Apple's signing server refused | check the laptop is online; see `image.log` and [go-ios #875](https://github.com/danielpaulus/go-ios/issues/875) |
| "Trust the developer app" | set-up step 6 not done (or the app was reinstalled) | Settings > General > VPN & Device Management > your Apple ID > Trust, with the phone online |
| "Apple ID session expired" | the saved login ran out | `~/.local/bin/plumesign account login -u <Apple ID>` in a terminal, then start again |
| "Re-signed less than an hour ago and it still fails" | the automatic re-sign runs at most once an hour | run `./resign.sh` in a terminal and read its message |
| login fails with -80045, -80046 or an HTML page | a stock `plumesign` (Flathub 2.7.0, release 2.7.1) instead of the patched one | use `~/.local/bin/plumesign`; `./setup.sh --rebuild`. See [Impactor #298](https://github.com/claration/impactor/issues/298) |
| Flatpak `plumesign` hangs at the password prompt | no terminal inside the sandbox | use `~/.local/bin/plumesign` |
| an App ID or app-count limit error while signing | [free Apple ID limits](#free-apple-id-limits) | remove another sideloaded app, or wait for the 7-day window |
| "WebDriverAgent did not start" | see `wda.log` | unlock the phone; Trust (step 6); Developer Mode on; `./resign.sh`. A code-signature error right after setup means an unpatched `plumesign`: `./setup.sh --rebuild && ./resign.sh` |
| "WebDriverAgent runs but does not answer on port 8100" | the forward, or the address WebDriverAgent listens on | see `fwd.log`; if it persists try `IPHONE_WDA_BIND= ./iphone.sh` |
| "WebDriverAgent keeps stopping" / "The developer tunnel dropped" | the tunnel disconnects ([go-ios #772](https://github.com/danielpaulus/go-ios/issues/772), reported on iOS 26.5) | unplug and replug, start again |
| "forward: cannot listen on 127.0.0.1:8100" | another program has the port | `./iphone.sh stop`; close that program; or `IPHONE_WDA_PORT=8101 ./iphone.sh` |
| "already starting or running" | an earlier start is still running | `./iphone.sh stop` |
| "No Python 3.9+ with PyGObject" / "GStreamer elements missing" | packages | the [package line](#packages-per-distro) for your distro, or `./setup.sh --check` (`--basic --check`) |
| "no GStreamer H.264 decoder: …" / `h264parse` missing | the USB and AirPlay picture need the bad plugins and a decoder | the [extra packages](#packages-per-distro) |
| "GStreamer elements missing: the H.264 decoder IPHONE_DECODER=…", "No USB video (GStreamer lacks the H.264 decoder IPHONE_DECODER=…" or "bad IPHONE_DECODER: …" (by hand, the window: "IPHONE_DECODER=…: no such GStreamer element installed") | `IPHONE_DECODER` names a decoder that is not installed (full mode then takes WebDriverAgent's picture), or is no element name | unset it, or name an installed decoder (`gst-inspect-1.0 \| grep h264`) |
| `souphttpsrc` missing although the good plugins are installed | the soup plugin loads libsoup 3 at run time and some distros do not pull it in | install libsoup 3: `libsoup-3_0-0` (openSUSE), `libsoup3` (Fedora), `libsoup-3.0-0` (Debian/Ubuntu) |
| "Not set up: … missing" / "WebDriverAgent is not installed yet" | `setup.sh` or `resign.sh` has not run (or a program was removed) | `./setup.sh`, then set-up steps 4-6 (or `./setup.sh --basic` for basic mode) |
| "No graphical session" | started from a plain console or over SSH without a display | start it from the desktop session |
| "The go-ios tunnel stopped: …" | the tunnel agent exited (phone unplugged, not trusted, Developer Mode off, or a go-ios error) | see `tunnel.log`; unlock, replug, start again |
| "WebDriverAgent still does not start after re-signing" / "Re-signing timed out" | the fresh signature did not help, or `plumesign` hung (Apple login, slow network) | see `wda.log` / `resign.log`; run `./resign.sh` in a terminal |
| "Port forward failed: …" | the local forward could not start | see `fwd.log`; usually the port is taken (next rows) |
| "Unlock the iPhone to reconnect." | WebDriverAgent stopped and the developer disk image needs the phone unlocked (after a phone restart) | unlock it; the watchdog reconnects by itself |
| "The WebDriverAgent signature ran out - close the window and start again to re-sign." | the 7-day signature is about to expire | close the window and run `./iphone.sh` again |
| "WebDriverAgent stopped - run ./iphone.sh again" (in the window) | WebDriverAgent quit and did not come back | run `./iphone.sh` again; replug if it repeats |
| "The window closed with an error (…)" | the GTK window crashed or could not start | see `window.log`; `./iphone.sh doctor` checks Python and GStreamer |
| the picture works but taps do nothing (full mode) | UI automation is off | on the phone: Settings > Developer > Enable UI Automation |
| "Shift+arrows, Shift+Tab and other Shift+key combinations need iOS 17 or later" | older iOS or WebDriverAgent | Shift+key combinations are switched off for the session; the keys without Shift and typing still work |
| "arrow, Delete, Home, End and Escape keys are not supported by this WebDriverAgent" | WebDriverAgent refused the HID key event | those keys are switched off for the session; typing still works |
| picture sideways, or taps off, in a landscape app | the orientation fix-up (not yet checked with every app, nor with the USB picture or in basic mode) | `IPHONE_FIX_ORIENTATION=0 ./iphone.sh` (WebDriverAgent's picture) |
| choppy picture | USB bandwidth: about 60 Mbit/s for WebDriverAgent's picture at the defaults, about 6 for the USB one | the USB picture; or a USB 3 port, or a lower `IPHONE_QUALITY` / `IPHONE_FPS` |
| swipes take a while to show on the phone | `IPHONE_FAST_INPUT=0`, another WebDriverAgent client (Appium, a script) opened its own session, which resets them, or WebDriverAgent's stream stopped under the USB picture ("WebDriverAgent's hidden stream: …" in `window.log`) | leave `IPHONE_FAST_INPUT` at 1 and restart the window; measure with `tests/latency_probe.py` ([CONTRIBUTING.md](CONTRIBUTING.md#tests)) |
| the picture freezes for a few seconds, then comes back ("watchdog: restarting WebDriverAgent" in `iphone.log`) | iOS stopped WebDriverAgent for using too much CPU (a `WebDriverAgentRunner-Runner.cpu_resource_fatal` report in Settings > Privacy & Security > Analytics Data) | keep `IPHONE_SCALE` at 100 (the default); the watchdog restarts it either way |
| nothing appears when started from the launcher | the error went to a notification that was missed | read `iphone.log`, or run `./iphone.sh` in a terminal |
| the window tiles instead of floating | no window rule | see [Window rules](#window-rules-tiling-window-managers) |

Before posting a log, remove your UDID, team id, Apple ID and Bluetooth addresses from it. `./iphone.sh doctor`
already redacts UDIDs, team ids and Apple IDs in the log lines it prints.

## Limitations

- **Full mode: gestures are replayed, not live.** WebDriverAgent runs a W3C gesture as one unit, so a drag is sent
  when the mouse button is released; the phone does not follow the pointer while you drag. The replay is quick: a
  slow drag is played back in about 0.25 s, with its last 0.1 s at your speed so it flings (or not) the same way,
  and a drag you stopped before letting go ends at rest. Scrolling sends short swipes (60 points per wheel notch,
  merged while one is in flight). Basic mode drags live: the finger goes down where the drag started and follows
  the mouse.
- **Typing** in full mode goes through WebDriverAgent's text input at 120 characters a second; Ctrl+V types at 300
  a second, in chunks of 500. Basic mode types what a US keyboard can type, nothing else (no accents, no emoji).
- **Basic mode** has no pinch, no iPhone clipboard, no apps by bundle id and no Action button, and F6 only locks.
  It does not know the screen's size in points: it guesses from the picture (3 pixels a point), which only changes
  scroll lengths and edge margins; where a click lands is exact. Not checked yet: taps in landscape apps, Ctrl+L,
  lock. While it runs, a device that pairs with that Bluetooth adapter without a code confirmed in the window is
  removed again, even headphones paired through the desktop's settings, and pairing on other adapters is refused:
  pair those while iPhone Control is closed.
  If `bluetoothd` restarts, Bluetooth input stops until iPhone Control is started again.
- **No audio.** The phone keeps playing its sound itself (speaker, headphones). The USB capture can ask for the
  sound (`IPHONE_AUDIO=1`), but on iOS 26.5.2 the phone still plays it itself and sends only silence, so it is
  off by default. Whether the AirPlay picture brings the sound to the laptop (UxPlay plays it by default) was not
  checked.
- **The status bar shows 09:41.** While the USB capture runs, the phone shows iOS's demo status bar (09:41)
  instead of the time.
- **A new capture session resets the phone's USB.** The phone answers one capture session per USB connection; a
  new one needs a USB reset, which drops every usbmux connection: in full mode WebDriverAgent, the tunnel and the
  forward are back after about 5 s. So `./iphone.sh` starts the capture first and keeps it for the whole run; the
  reset happens again only when the picture stops for 5 s or the capture process has to be restarted, and not
  within 15 s of a reset that brought no picture back. After 3 failed restarts in a row (each one's trouble back
  within a minute) the capture gives up with exit 4 rather than reset the phone in a loop. Locking the phone does
  not stop the picture: locked for 20 s with the capture running, it never sent fewer than 39 frames a second
  (iPhone 15 Pro, iOS 26.5.2, Oct 2026); locking it before the start was not tried.
- **Latency**, measured on an iPhone 15 Pro with iOS 26.5.2 (Oct 2026) with `tests/latency_probe.py`, from sending
  the command to the first changed frame (medians):

  | | USB picture | WebDriverAgent's picture (MJPEG) |
  |---|---|---|
  | a tap (full mode) | 196 ms | 249 ms |
  | a 250 ms page swipe sent by script | 180 ms | 190 ms |
  | a 600 ms mouse drag, as the window replays it | 246 ms | 254 ms |

  A click no longer waits about 200 ms for a possible double-click before its tap is sent. Each WebDriverAgent
  command still waits for the one before it: a tap's reply comes after about 0.34 s, a swipe's after about 0.6 s.
  A named key (arrows and the like) takes about 215 ms; 50 pasted characters took 409 ms at 400 a second (Ctrl+V
  sends 300), against 1120 ms at WebDriverAgent's default of 60. In basic mode a tap reaches the screen 280-440 ms
  after it is sent, and Control Centre 153 ms. Details: [docs/how-it-works.md](docs/how-it-works.md#latency). The
  USB picture is H.264 at about 42 frames a second on a still screen and about 50 while it moves, about 6 Mbit/s;
  WebDriverAgent's is MJPEG at 28 frames a second, about 60 Mbit/s at the defaults. USB 2 handles both easily.
- Protected video (DRM) may show black, as in any screen recording.
- One phone per running copy; `IPHONE_UDID` picks which.
- Full mode: the laptop needs internet to mount the developer disk image after each phone restart, and to re-sign.
- go-ios 1.3.2 mounts disk images with `mobile_image_mounter`; phones newer than it knows may need a newer go-ios
  (`GO_IOS_VERSION=… GO_IOS_SHA256=… ./setup.sh`).

## FAQ

**Does it need Wi-Fi?** No, except for the AirPlay picture. Everything else goes over the cable (and Bluetooth in
basic mode), so eduroam or captive portals do not matter. In full mode the phone needs internet once, for the Trust
step.

**Does the laptop need internet?** For setup. In full mode also for re-signing (weekly with a free Apple ID) and
for the first start after each phone restart.

**Mac, Xcode or jailbreak?** None.

**Paid developer account?** No. Full mode works with a free Apple ID (a paid one only makes the signature last
longer); basic mode needs no Apple ID at all.

**Which iPhones?** iOS 17.4.1 or newer is expected to work in full mode; tested on an iPhone 15 Pro with iOS
26.5.2. Basic mode and the USB picture have only been tried on the same phone (see [Tested](#tested)). Older iOS versions and iPads are
untested.

**Wayland or X11?** Both (GTK 3).

**Can I script the phone?** In full mode, yes: while the window is open, WebDriverAgent's API is at
`http://127.0.0.1:8100` (for example `curl http://127.0.0.1:8100/status`). Requests with an `Origin` header (web
pages) are refused. The USB picture can be read from `$XDG_RUNTIME_DIR/iphone-control/cap.sock` (its format is in
`iphone_control/screencap.py`).

**What happened to `alt/bluetooth-mouse/`?** Basic mode replaces it. It used the same idea, AirPlay and a
Bluetooth mouse for AssistiveTouch, but the pointer was relative and calibrated from the picture, Classic Bluetooth
had to be switched off, and the picture came over Wi-Fi. Now the pointer is placed absolutely, Classic Bluetooth
stays on, and the picture comes over USB. Forget its pairing on the phone before pairing again.

## Security and privacy

**WebDriverAgent (full mode).** It can tap, type and read the screen, and has no authentication. iPhone Control
keeps it local:

- The two ports are forwarded on **127.0.0.1 only**, by the small forwarder in `iphone_control/forward.py`
  (go-ios' own `ios forward` listens on every interface).
- The forwarder blocks web pages: a request with an `Origin` header, a cross-site `Sec-Fetch-Site`, or a `Host`
  other than `127.0.0.1`, `localhost` or `[::1]` (DNS rebinding) gets 403. Without this, any website open in the
  laptop's browser could drive the phone, because WebDriverAgent allows every origin (CORS `*`).
- On the phone, WebDriverAgent is told to listen on its loopback address only (`USE_IP=127.0.0.1`), not on the
  phone's Wi-Fi.
- Other programs on the laptop, under any user, can still reach 127.0.0.1:8100 while the window is open. Use it
  on computers you trust.

**The USB picture.** The capture process serves the phone's live screen on a UNIX socket,
`$XDG_RUNTIME_DIR/iphone-control/cap.sock`, mode 600 in a folder of mode 700: only your own user's programs can
read it. It listens on nothing else.

**Bluetooth (basic mode).** While the window is open, the laptop advertises over Bluetooth LE as
"*&lt;hostname&gt;* Mouse" (`IPHONE_HID_NAME`), visible to anyone nearby. A device can pair only after you confirm
its six-digit code in the window; pairing without a code is refused, and a device that bonds anyway is removed
again. The paired phone gets everything you type in the window. The reports go to `bluetoothd` only, not broadcast
on the system bus, so other local programs cannot read the keystrokes there (checked against a fake BlueZ, not yet
with a real adapter). The adapter's settings stay as they are, except Pairable, which is on while it runs and put
back afterwards. While basic mode runs, iPhone Control is BlueZ's default pairing agent: pairing requests on other
adapters are refused, and paired devices may use their services without the desktop's prompt.

**AirPlay**, only when the AirPlay picture is used (`IPHONE_VIDEO=airplay`, or basic mode when the USB picture
cannot start and UxPlay 1.73+ is installed): UxPlay has to listen on the local network (the ports
[above](#basic-mode)), announced under the hostname (`IPHONE_AIRPLAY_NAME`). It is started without UxPlay's PIN or
client restrictions, so anyone on that network can mirror a screen into the window. Only its hand-over to the window
(RTP) stays on 127.0.0.1. It runs only while the window does: the window stops it when it closes, the kernel sends
it SIGTERM when the window dies any other way, and `./iphone.sh stop` stops it from `$RUN/uxplay.pid`.

**The usbmuxd of `./setup.sh --usbmuxd`** is a system-wide change: systemd then runs a usbmuxd built from
libimobiledevice's git, at a pinned commit, for every program and user instead of the distro's, until
`./uninstall.sh` removes it. It shows what it installs and asks first, and needs sudo.

**Secrets on disk** (all private to your user):

| Path | Holds |
|---|---|
| `~/.config/PlumeImpactor/` | `plumesign`'s Apple ID session (`accounts.json`) and your signing key (`keys/*/key.pem`). `setup.sh` makes it private |
| `~/.local/state/iphone-control/selfIdentity.plist` | the pairing key go-ios uses for the developer tunnel |
| `~/.local/state/iphone-control/resign.log` | `plumesign` output; may contain your Apple ID |
| `~/.local/state/iphone-control/config.json` | WebDriverAgent's bundle id (contains your team id) and its expiry |
| `$XDG_RUNTIME_DIR/iphone-control/*.log` | contain the phone's UDID; cleared at logout when `XDG_RUNTIME_DIR` is set (otherwise they stay in `~/.local/state/iphone-control/run/iphone-control/` until the next start overwrites them, or `./uninstall.sh`) |
| `/var/lib/bluetooth/` | BlueZ's own store of the Bluetooth pairing with the phone (root only) |

The state folder is mode 700 and its contents are made private on every start. The signed app, which contains
your team id and the phone's UDID, is deleted after installing. Nothing personal is written into the repo.

**Your Apple ID.** The password goes only to Apple (SRP; it is never sent in clear). Apple's login also needs
"anisette" device-identity data, which `plumesign` fetches from a third-party server, `mac-ani.jkcoxson.com`
(Impactor's default; see [the patches](docs/how-it-works.md#the-patches)). That server sees your IP address and
the anisette requests, not your password. Use a secondary Apple ID if that matters to you.

**Downloads are pinned.** `setup.sh` checks go-ios 1.3.2 (`go-ios-linux.zip`, SHA-256 `a55fdb4c…cc125604`) and
WebDriverAgent 16.15.3 (`WebDriverAgentRunner-Runner.zip`, `21767935…6ce9d4fc5`), builds Impactor at a fixed commit
(`7f98856`), builds usbmuxd (only with `--usbmuxd`) at a fixed commit (`3ded00c`, or `e9a0dce` with libplist
2.3-2.5), and checks `certs/apple-root.pem` against Apple's published fingerprint.

**To revoke everything:** `./uninstall.sh` (answer y to the Apple ID, usbmuxd and state questions), then on the
phone remove WebDriverAgentRunner and, if nothing else needs it, turn Developer Mode off; forget
"*&lt;hostname&gt;* Mouse" under Settings > Bluetooth. You can also review the devices signed in to your Apple ID at
[account.apple.com](https://account.apple.com) > Devices.

## Uninstall

```
./uninstall.sh          # asks before each step
./uninstall.sh --yes    # removes programs, launcher entry, build/ and caches without asking;
                        # the Apple ID session, the usbmuxd override and the state folder still need a y
```

It stops iPhone Control and removes `~/.local/bin/ios` and `~/.local/bin/plumesign` (only the versions setup
installed), the launcher entry, `build/`, `~/.cache/iphone-control` and the logs and run files (`cap.status`,
`uxplay.log`, …); on request also `~/.config/PlumeImpactor` (after `plumesign account logout`), the usbmuxd of
`./setup.sh --usbmuxd` (with sudo, also when its drop-in folder is not readable by you; then usbmuxd restarts as
the distro's) and `~/.local/state/iphone-control`. System packages and the repo's own
files stay. By hand, the same is:

```
./iphone.sh stop
~/.local/bin/plumesign account logout
rm -f ~/.local/bin/ios ~/.local/bin/plumesign ~/.local/share/applications/iphone-control.desktop
rm -rf build ~/.cache/iphone-control ~/.config/PlumeImpactor ~/.local/state/iphone-control "$XDG_RUNTIME_DIR/iphone-control"
sudo rm -f /etc/systemd/system/usbmuxd.service.d/iphone-control.conf /usr/local/sbin/usbmuxd-iphone-control
sudo systemctl daemon-reload && sudo systemctl restart usbmuxd
```

On the phone: long-press WebDriverAgentRunner > Remove App > Delete App; check Settings > General > VPN & Device
Management; turn Developer Mode off if nothing else needs it. For basic mode: Settings > Bluetooth > (i) next to
"*&lt;hostname&gt;* Mouse" > Forget This Device, and AssistiveTouch off if you do not use it otherwise; on the
laptop `bluetoothctl devices` lists the phone and `bluetoothctl remove <its address>` drops the pairing. Remove the
window rule if you added one (`~/.config/hypr/iphone-control.lua` and the two `iphone-control` lines at the end of
`hyprland.lua`).

## Files

| | |
|---|---|
| `iphone.sh` | `start` / `stop` / `status` / `doctor`: the mode, USB capture, tunnel, disk image, WebDriverAgent, port forward, window, watchdog |
| `resign.sh` | sign WebDriverAgent with the logged-in Apple ID and install it |
| `setup.sh` | one-time install (`--basic`, `--usbmuxd`, `--check`, `--rebuild`, `--hyprland`) |
| `uninstall.sh` | remove what setup and resign installed |
| `wda_mirror.py` | entry point of the window |
| `iphone_control/window.py` | the GTK window: mouse, keys, toolbar, screenshots, clipboard; full mode's and basic mode's input |
| `iphone_control/video.py` | the picture: the USB capture's H.264, WebDriverAgent's MJPEG, AirPlay through UxPlay; the hidden MJPEG client |
| `iphone_control/screencap.py` | the USB capture process: the phone's capture configuration, its session and stall watchdog, the socket |
| `iphone_control/valeria.py` | the USB screen-capture protocol, ported from quicktime_video_hack (pure, unit-tested) |
| `iphone_control/usbfs.py` | the phone's USB device through sysfs and usbfs, stdlib only (no libusb) |
| `iphone_control/blehid.py` | basic mode: the laptop as a Bluetooth LE mouse and keyboard (BlueZ over D-Bus) and its pairing agent |
| `iphone_control/hid.py` | the Bluetooth report map, keymap and timing (pure, unit-tested) |
| `iphone_control/wda.py` | WebDriverAgent HTTP client (one worker thread; reopens the session when WebDriverAgent restarts) |
| `iphone_control/geometry.py` | window pixels to phone points, W3C gesture builders (pure, unit-tested) |
| `iphone_control/usbmux.py` | stdlib usbmuxd client: list phones, connect to a port on the phone |
| `iphone_control/forward.py` | loopback-only port forward with the web-page guard |
| `certs/apple-root.pem`, `certs/ca-bundle.sh` | Apple Root CA, and the script that adds it to the system CAs for go-ios |
| `patches/` | the two `plumesign` fixes ([why](docs/how-it-works.md#the-patches)) |
| `tests/` | tests, no phone needed: `python3 -m unittest discover -s tests` (see [CONTRIBUTING.md](CONTRIBUTING.md#tests)) |
| `tests/valeria_fake.py`, `tests/fixtures/h264/` | a fake iPhone for the capture tests; small synthetic H.264 clips for the picture tests |
| `tests/live_test.py` | optional end-to-end check against a real, idle iPhone (`--i-am-not-using-the-phone`) |
| `tests/latency_probe.py` | optional swipe or tap latency measurement on a real, idle iPhone, on WebDriverAgent's picture or the USB one (`--i-am-not-using-the-phone`) |
| `.github/workflows/ci.yml` | CI: ShellCheck, the tests on Python 3.9-3.13, the window, video and Bluetooth tests under Xvfb, `setup.sh --check` per distro |
| `CONTRIBUTING.md` | how to report problems, rules for changes, the tests |
| `LICENSE` | the licence |
| `docs/how-it-works.md` | the details |

Outside the repo: `~/.local/state/iphone-control` (pairing key, `config.json`, disk images, CA bundle, stamps),
`$XDG_RUNTIME_DIR/iphone-control` (pids, logs, the capture socket `cap.sock` and its exit status `cap.status`),
`~/.cache/iphone-control` (downloads, the Impactor build, the usbmuxd build), `~/.config/PlumeImpactor` (Apple ID
session); with `./setup.sh --usbmuxd`, `/usr/local/sbin/usbmuxd-iphone-control` and
`/etc/systemd/system/usbmuxd.service.d/iphone-control.conf`.

## Credits

- [Appium WebDriverAgent](https://github.com/appium/WebDriverAgent) (BSD-3-Clause): the server on the phone.
- [go-ios](https://github.com/danielpaulus/go-ios) by Daniel Paulus (MIT): tunnel, disk image, launching it.
- [quicktime_video_hack](https://github.com/danielpaulus/quicktime_video_hack) by Daniel Paulus (MIT): the USB
  screen-capture protocol and its documentation; `iphone_control/valeria.py` is a port of it.
- [Impactor](https://github.com/claration/impactor) by Samara M (MIT): `plumesign`, signing with an Apple ID;
  it includes [omnisette](https://github.com/SideStore/omnisette) (MPL-2.0) for the anisette data.
- [usbmuxd](https://github.com/libimobiledevice/usbmuxd) from libimobiledevice: the USB transport;
  `./setup.sh --usbmuxd` builds it from git for the USB picture.
- [BlueZ](https://www.bluez.org): the Bluetooth stack basic mode registers with.
- [UxPlay](https://github.com/FDH2/UxPlay) (GPL-3.0): the AirPlay receiver for the AirPlay picture.

## License

MIT, see [LICENSE](LICENSE). `patches/` modify Impactor (MIT, Samara M). `iphone_control/valeria.py` is ported from
quicktime_video_hack (MIT, Daniel Paulus). `setup.sh` downloads go-ios (MIT) and WebDriverAgent (BSD-3-Clause), and
with `--usbmuxd` usbmuxd's source; none is redistributed here. `certs/apple-root.pem` is Apple's public root
certificate from [apple.com/certificateauthority](https://www.apple.com/certificateauthority/).

This project is not affiliated with, authorised or endorsed by Apple Inc. Use it only with devices and accounts
you own, and within Apple's terms.
