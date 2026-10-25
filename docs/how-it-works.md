# How iPhone Control works

The [README](../README.md) covers setting it up and using it. This page is for anyone who wants to know what
happens underneath, debug a failure that the troubleshooting table does not cover, or change the code.

Contents: [the pieces](#the-pieces) · [what start does](#what-start-does) · [usbmuxd and pairing](#usbmuxd-and-pairing) ·
[USB screen capture](#usb-screen-capture) · [the developer tunnel](#the-developer-tunnel) ·
[the developer disk image](#the-developer-disk-image) · [WebDriverAgent](#webdriveragent) ·
[the port forward](#the-port-forward) · [signing](#signing) · [the patches](#the-patches) ·
[Bluetooth input](#bluetooth-input-basic-mode) · [the window](#the-window) · [latency](#latency) ·
[the watchdog](#the-watchdog) · [files](#files-and-state) · [pins and updates](#pins-and-updates) ·
[known upstream issues](#known-upstream-issues) · [other approaches](#other-approaches)

## The pieces

An iPhone will only run code that Apple's chain of trust allows, and it only exposes developer features after a
few handshakes. iPhone Control strings together existing tools to get through each one from Linux, or, in basic
mode, around them:

| Need | Solved by |
|---|---|
| talk to the phone over USB | `usbmuxd` (system service) and a small stdlib client, `iphone_control/usbmux.py` |
| see the screen, without Developer Mode | the phone's USB screen-capture interface, the one QuickTime uses: `iphone_control/usbfs.py`, `valeria.py`, `screencap.py`, with a usbmuxd that leaves that interface on (`./setup.sh --usbmuxd`) |
| touch it, without Developer Mode (basic mode) | the laptop as a Bluetooth LE mouse and keyboard for AssistiveTouch: `iphone_control/hid.py`, `blehid.py`, through BlueZ |
| reach the developer services on iOS 17+ (full mode, as are the next rows) | go-ios' userspace tunnel (`ios tunnel start --userspace`) |
| make the phone able to run test code | the developer disk image (`ios image auto`), personalised by Apple |
| an app that streams the screen and accepts touches | Appium's WebDriverAgent, an XCTest runner |
| get WebDriverAgent onto the phone without a Mac | `plumesign` (Impactor) signs it with an Apple ID and installs it |
| start it | `ios runwda` launches the runner through the tunnel |
| reach its two ports from the laptop, safely | `iphone_control/forward.py`, loopback only, with a guard against web pages |
| show it and send input | `wda_mirror.py`, `iphone_control/window.py` and `video.py` (GTK 3 + GStreamer) |

`iphone.sh` orchestrates all of it; `setup.sh` installs the tools once (`--basic` only basic mode's packages,
`--usbmuxd` the newer usbmuxd), `resign.sh` handles signing.

## What start does

`./iphone.sh` (or `./iphone.sh start`) runs these steps. Each waits for the user where that makes sense, and
says what it is waiting for (in the terminal, or as a desktop notification when started from the launcher).

0. **Checks**: `IPHONE_MODE` (auto, full, basic), `IPHONE_VIDEO` (auto, usb, mjpeg, airplay), the ports, `BT_INDEX`,
   `IPHONE_DOUBLE_CLICK_MS` and `IPHONE_DECODER` (an element name) first (a bad value exits 2). A first guess at the
   mode: auto is basic without go-ios or a recorded WebDriverAgent bundle id. Full mode needs go-ios, curl and that
   bundle id; both modes need a Python 3.9+ with PyGObject, a graphical session, and the GStreamer elements of the
   picture they will use: MJPEG with `IPHONE_VIDEO=mjpeg`, H.264 with `usb` and in basic mode, RTP and H.264 for
   airplay. Full mode with auto needs one of MJPEG and H.264 and notes which is missing (that picture is then
   skipped). `IPHONE_DECODER`, when set, is the only H.264 decoder checked. `IPHONE_VIDEO=airplay` needs `uxplay`
   1.73 or newer. Then it takes a lock (`flock` on `$RUN/lock`), so a second start says "already starting or
   running" instead of fighting the first.
1. **The phone**: waits up to two minutes for an iPhone on usbmuxd (`usbmux list`), picks it (or the one in
   `IPHONE_UDID`), reads its iOS version straight from lockdown (port 62078), and waits for a pair record, i.e. for
   Trust to be tapped. `GO_IOS_UDID` is exported so every go-ios call talks to the same phone.
2. **The mode**: with `IPHONE_MODE=auto` and full mode still possible, `ios devmode get` (over lockdown, no tunnel
   needed) reads Developer Mode on iOS 16+: off means basic mode, an answer it cannot read full mode. iOS 17.0 to
   17.4.0 means basic mode (no rootless tunnel). The mode and its reason go to `iphone.log`, to `$RUN/mode` (for
   `status`) and to the user, as a desktop notification when auto picked basic mode.
3. **The USB capture** (unless `IPHONE_VIDEO` is mjpeg or airplay): starts
   `python3 -I -c … screencap --udid <udid> --socket $RUN/cap.sock` (plus `--audio` for `IPHONE_AUDIO=1`) and waits
   up to 25 s for its stdout line `capturing`. Its exit status says why not: 4 (no picture even after a reset: a
   stuck session; a phone locked before the start was not tried) gives "Unlock the iPhone - waiting for it..." and
   another try, for up to two minutes; 2 (unplugged) waits for the phone within the same two minutes, except when
   usbmuxd still lists it a second later but this computer's sysfs does not (another machine's usbmuxd,
   `USBMUXD_SOCKET_ADDRESS`), which falls back at once ("the iPhone is not on this computer's USB"); 3 (usbmuxd
   keeps the phone out of its capture configuration) and errors give up. Once it captures, `iphone.sh` waits until
   usbmuxd lists the phone again (the capture's USB reset re-enumerates it). The capture comes before everything
   else because a new capture session resets the phone's USB, which drops every usbmux connection ([USB screen
   capture](#usb-screen-capture)); the process then stays up for the whole run. When it cannot start and
   `IPHONE_VIDEO=auto`, full mode takes WebDriverAgent's MJPEG ("No USB video (…) - using WebDriverAgent's
   picture."), basic mode AirPlay when UxPlay 1.73+ and its elements are there, and otherwise it stops with the fix
   (exit 3 names `./setup.sh --usbmuxd`). Full mode with auto and no H.264 elements skips the capture ("No USB video
   (GStreamer lacks …)"). With `IPHONE_VIDEO=usb` any failure stops the start.
4. **The signature** (full mode, like steps 5-7): if `config.json`'s `expires_at` is less than an hour away,
   re-sign first (see [signing](#signing)). Installs made by older versions are imported with `resign.sh --record`.
5. **Tunnel** (iOS 17+): reuses a running go-ios agent on `GO_IOS_AGENT_PORT` if it already has a tunnel for this
   phone, otherwise starts `ios tunnel start --userspace --pair-record-path=$STATE` and waits for the agent's
   `/tunnel/<udid>` to report an RSD port.
6. **Disk image**: `ios image list`; if nothing is mounted, `ios image auto --basedir=$STATE/ddi` with
   `SSL_CERT_FILE` pointing at the CA bundle. It retries for two minutes while the phone is locked. If it still
   fails, it checks Developer Mode (`ios devmode get`) to give the right message.
7. **Forward and WebDriverAgent**: starts the forward (`127.0.0.1:IPHONE_WDA_PORT -> phone:8100`,
   `127.0.0.1:IPHONE_MJPEG_PORT -> phone:9100`), then `ios runwda`, and polls `GET /status` through the forward.
   If WebDriverAgent does not answer, `wda.log` is read to tell "app missing", "signature expired",
   "developer not trusted", "phone locked" and "tunnel dropped" apart. Missing or expired triggers one re-sign;
   the others wait (up to 150 s) and tell the user what to tap.
8. **Watchdog and window**: steps 4-7 can take minutes (a re-sign, the disk image, an unlock) with nothing watching
   the capture, so a capture that stopped meanwhile is started again first, or falls back as in step 3 (its USB
   reset drops the forward and WebDriverAgent, which the watchdog restarts). Then it starts the
   [watchdog](#the-watchdog) in the background, then the window (`python3 -I wda_mirror.py`) with `IPHONE_MODE`,
   `IPHONE_VIDEO`, `IPHONE_FROM_SCRIPT=1` (the window leaves reporting a fatal error to `iphone.sh` instead of
   sending its own notification), `IPHONE_CAPTURE_SOCKET` (only for the USB picture) and `IPHONE_WDA_URL` /
   `IPHONE_MJPEG_URL` (only in full mode). When the window closes, the `EXIT` trap stops the watchdog, the window,
   UxPlay (from `$RUN/uxplay.pid`, in case it outlived a window that crashed or was killed),
   WebDriverAgent, the forward, the tunnel and, last, the capture, with 6 s before a KILL: on TERM it ends its
   session the way the phone expects, which takes up to about 4 s.

`./iphone.sh stop` signals a running `iphone.sh` (its trap cleans up), waits up to 15 s for the lock, then stops
whatever is left from the pid files. Every pid is checked against `/proc/<pid>/cmdline` before it is signalled,
so a recycled pid is never killed.

`./iphone.sh status` and `./iphone.sh doctor` change nothing except fixing the permissions of `$STATE` and `$RUN`
and refreshing the CA bundle (`doctor`). `status` shows the mode and why (the running one, or what a start would
choose), the capture process, the USB video state of each phone and the usbmuxd version. `doctor` checks tools,
Python, GStreamer elements, the installed go-ios and `plumesign` against the pins, the Apple ID login, the IPA, the
CA bundle, the signature's expiry, the launcher entry, file permissions, the phone (pairing, iOS version, tunnel,
Developer Mode, disk image), the USB picture (the usbmuxd that runs, read from `/proc/<pid>/cmdline`, and its
version; the phone's USB configuration from sysfs; the H.264 elements, with `IPHONE_DECODER` when set), basic mode
(`bluetoothd` and its LE adapters through `busctl --auto-start=no`, so asking never starts it, with `bluetoothctl`
as a fallback; UxPlay's version), who listens on the two ports, and whether WebDriverAgent and the stream answer.
What only full mode needs is a warning in basic mode, and the USB-video checks fail only where the USB picture is
needed (`IPHONE_VIDEO=usb`, or basic mode without UxPlay). `doctor` exits 1 when anything FAILs. A running usbmuxd
counts only when root, `usbmux` or `usbmuxd` runs it from a program owned by root that only root can change (its
comm is `usbmuxd`, or `usbmuxd-iphone-` for setup's build started by hand: the kernel keeps 15 characters).
`--version` is run only on `/usr/local/sbin/usbmuxd-iphone-control` or on a `usbmuxd` in `/usr/sbin`, `/usr/bin`,
`/sbin`, `/bin`, `/usr/local/sbin` or `/usr/local/bin`, root's alone; for any other `status` shows
"version unknown (<path>)". `IPHONE_SYSFS` (default `/sys/bus/usb/devices`) and `IPHONE_PROC` (default `/proc`) point
both at fake trees in the tests.

## usbmuxd and pairing

Every connection to an iPhone over USB goes through `usbmuxd`, a system daemon that multiplexes TCP-like
connections over the cable. Clients talk to it on `/var/run/usbmuxd` (or `USBMUXD_SOCKET_ADDRESS`: an absolute socket path or a numeric
`IP:port`, the forms go-ios, `plumesign` and `usbmux.py` all accept; `plumesign` rejects hostnames) with small
plist messages: `ListDevices`, `Connect` (to a port on the phone), `ReadPairRecord`. `iphone_control/usbmux.py`
implements the few messages needed, so libimobiledevice's command-line tools are not required.

usbmuxd also chooses which of the phone's USB configurations is used, which decides whether the screen capture is
there at all ([USB screen capture](#usb-screen-capture)).

usbmuxd numbers devices (`DeviceID` 1, 2, …) in addition to the phone's UDID. `plumesign --udid` wants that
number, not the UDID, which is why `resign.sh` calls `usbmux device-id` first.

When the user taps **Trust**, the phone and usbmuxd exchange keys and usbmuxd stores a *pair record*. go-ios
additionally creates its own identity for the iOS 17 tunnel, `selfIdentity.plist`, which includes a private key.
go-ios writes it into the current directory by default, so `iphone.sh` runs from the state directory and passes
`--pair-record-path=$STATE` explicitly; the file never lands in the repo.

## USB screen capture

On a Mac, QuickTime and OBS show an iPhone's screen over the cable with no Developer Mode, only a trusted phone:
the phone streams its screen as hardware-encoded H.264 through a capture interface in a USB configuration it keeps
hidden. iPhone Control does the same from Linux, with the standard library only: `usbfs.py` (the USB device),
`valeria.py` (the protocol, pure) and `screencap.py` (the process that runs it and serves the frames). The protocol
is ported from Daniel Paulus' [quicktime_video_hack](https://github.com/danielpaulus/quicktime_video_hack) (MIT),
whose `doc/` folder describes the messages; the messages built here are byte-identical to its fixtures
(`tests/test_valeria.py`). Measured on an iPhone 15 Pro with iOS 26.5.2, October 2026, like everything below.

**The USB configuration.** The phone offers 4 USB configurations. After a vendor request (`bmRequestType` 0x40,
`bRequest` 0x52, `wIndex` 2) it re-enumerates with a fifth: PTP, usbmux, the capture interface (class FF, subclass
2A, protocol FF, "Valeria") and CDC-NCM. usbmuxd then chooses the configuration: usbmuxd 1.1.1 sets 4 again, which
has no capture interface; usbmuxd from git keeps 5 when it runs with `USBMUXD_DEFAULT_DEVICE_MODE=2`. Its own
usbmux interface is in 5 as well, so lockdown, the port forward, the developer tunnel, the disk-image mount and
WebDriverAgent with its MJPEG stream all keep working beside the capture. `screencap.py` sends the request at most
once and reads the outcome from sysfs (`bNumConfigurations`, `bConfigurationValue`): 5 configurations offered but
another one in use, or still 4 after the request, for 5 s, means usbmuxd chose, and it exits 3. `./iphone.sh
status` and `doctor` read the same two files. `./setup.sh --usbmuxd` builds such a usbmuxd (see the
[README](../README.md#the-usb-picture-a-newer-usbmuxd)); git master needs libplist 2.6 or newer, the last commit for
libplist 2.3-2.5 is `e9a0dce` (14 Sep 2024), and Debian 12's and Ubuntu 22.04's libplist 2.2 builds neither.

**No root, no libusb.** The phone is found in `/sys/bus/usb/devices` (vendor 05ac, its serial is the UDID, the raw
descriptors are there too); transfers are the usbfs ioctls that libusb uses itself (control, bulk, claim and
release, reset, clear halt), with the 64-bit structure layouts (x86_64 and aarch64). The desktop session already
has read and write access to the phone's `/dev/bus/usb` node through udev's `uaccess` tag (seen on Arch; not
checked on other distros). Only the capture interface is claimed: a kernel driver on it is detached, but an
interface another program holds is left alone (EBUSY).

**The protocol** (`valeria.py`). Every message starts with its length (uint32, little endian, itself included) and
a 4-byte magic. The phone sends PING, then asks for clock references in SYNC messages (CWPA, CVRP, CLOK, TIME,
SKEW, OG, STOP, and AFMT for sound); the host answers each, and with its reply to CWPA asks for video (HPD1) and,
only with `--audio`, sound (HPA1). Each FEED from the phone holds one frame and is answered with a NEED, which asks
for the next. A FEED's sample buffer is one H.264 access unit, NAL units with 4-byte lengths; the first one also
carries the format description with SPS and PPS (the avcC record). The stream is H.264 High profile, 1180 x 2556 on
this phone, with no B-frames and a keyframe about every 0.7 s: about 42 frames a second on a still screen, about 50
while it moves, about 6 Mbit/s (WebDriverAgent's MJPEG: 28 frames a second, about 60 Mbit/s). When an app turns to
landscape, a new format arrives with 2556 x 1180. Sound comes as 48 kHz 16-bit stereo LPCM (EAT! messages), but
on iOS 26.5.2 the phone keeps playing it on its own speaker and sends digital silence, so `screencap.py` does not
ask for it unless given `--audio` (`IPHONE_AUDIO=1`).

**One session per enumeration.** After one capture session the phone answers no new one until it re-enumerates: it
stays silent, or pings and sends no picture. A USB reset (`USBDEVFS_RESET`) re-enumerates it in about 1.5 s, but
drops every usbmux connection on the way: the port forward, the developer tunnel and WebDriverAgent (the
[watchdog](#the-watchdog) has them back in about 5 s). So `screencap.py` tries first and resets only when there is
no PING within 3 s or no picture 3 s after it, then tries once more; no picture within 10 s after the reset means
the phone is stuck (exit 4). Locking it is not the cause: a phone locked while a capture runs keeps streaming at
(see the stall watchdog below); locking it before the start was not tried. `iphone.sh` starts it before the tunnel and
WebDriverAgent and keeps it for the whole run: the window and other clients connect to its socket, and reconnect, as
often as they like, without a new session.

**The stall watchdog.** While a session runs, no picture for 5 s, or a reader error, ends the session, resets the
phone and starts a new one; it is logged, and the window shows "The iPhone stopped sending its screen - starting a
new capture..." until the picture is back. A stall is a stuck session, not the phone locking: locked for 20 s with
the capture running, the phone never sent fewer than 39 frames a second (46 on average). Resets are at least 5 s
apart, and at least 15 s after a reset that brought no picture back, with that session kept open meanwhile (the
picture may still come, and each reset cuts WebDriverAgent, the tunnel and the forward again). A restart failed when
the trouble is back within a minute of it, or it never brought a picture; one that lasted longer ends the row. After
3 failed restarts in a row it exits 4 rather than reset the phone, and its usbmux connections, in a loop, and the
window shows "The iPhone stopped sending its screen - if it stays black, restart the iPhone." After a transfer error
it watches sysfs for 3 s, so a phone that only re-enumerated is not taken for unplugged (exit 2).

**Stopping.** On SIGTERM, SIGINT or SIGHUP it ends the session the way the phone expects: HPA0 (only with sound),
HPD0, up to 3 s for the phone's RELS (and 0.5 s for a second one), then HPD0 again. Writes time out after 0.5 s, so
the whole stop takes at most about 4 s; `iphone.sh` stops it last and gives it 6 s before KILL. The signal handlers
only set a flag and write a byte to a pipe that the waits select on (`StopFlag`), so a signal can never deadlock the
main thread's waits. Exit status: 0 stopped, 1 error (a bad command line too), 2 unplugged, 3 unsupported here
(usbmuxd too old), 4 no picture (stuck even after a reset, or after 3 failed restarts). The command line `iphone.sh`
starts it with also writes that status to `$RUN/cap.status`, for the watchdog.

**The socket.** `$RUN/cap.sock`, a stream socket of mode 600 (a stale socket there is replaced, any other file at
that path refused; on exit it removes only its own). Each frame is a `<IBq` header (payload length, kind, and the
time the frame arrived from the phone in this machine's `CLOCK_MONOTONIC` nanoseconds), then the payload:

| Kind | Payload |
|---|---|
| 1 VIDEO_FORMAT | `<II` width, height, then the avcC record; sent again when it changes (an app turned landscape) |
| 2 VIDEO | one byte (1 = keyframe), then the access unit: NAL units with 4-byte big-endian lengths |
| 3 AUDIO_FORMAT | `<dII` sample rate, channels, bits per sample (signed little-endian interleaved PCM) |
| 4 AUDIO | PCM samples |
| 5 STATUS | UTF-8 text for the window to show (`""` clears it) |

It serves up to 4 clients at once: the window, `tests/latency_probe.py --video usb` beside it, and a spare. Each new
client first gets the formats, the last status and the frames since the last keyframe, so it can decode at once
(with none to replay, its picture starts at the next keyframe). Every client has its own queue and send thread, so
a slow one holds up neither the phone nor the others; one that falls 3 s or 8 MB behind is cut off and reconnects at
the last keyframe.

```
python3 -m iphone_control.screencap [--udid U] --socket PATH [--audio]
python3 -m iphone_control.screencap [--udid U] --record OUT.h264 [--wav OUT.pcm] [--seconds N]
```

`--record` writes the picture as Annex-B H.264 instead (and with `--wav` the sound as raw PCM), for debugging; the
recording shows the phone's screen, so it stays out of the repo (`.gitignore`).

## The developer tunnel

From iOS 17 Apple moved developer services (test runner management, the disk image, instruments) behind
*RemoteXPC*, reachable only through a tunnel to the phone. Apple's own tools open it as a kernel TUN interface,
which needs root. go-ios can instead run the tunnel's network stack in userspace (`--userspace`), so nothing needs
root. Its agent listens on `127.0.0.1:60105` (`GO_IOS_AGENT_PORT`) and later `ios` commands find the tunnel
through it.

The userspace tunnel needs **iOS 17.4.1 or newer**: go-ios 1.3.2 refuses it for 17.0-17.4.0
(`tunnel_api.go`), where the phone only offers the older, network-based pairing. `iphone.sh` checks the version:
auto mode takes basic mode there, and `IPHONE_MODE=full` says why it cannot start. iOS 16 and older have no
tunnel; their services are reachable over lockdown directly, so the step is skipped (untested).

## The developer disk image

A phone only runs test bundles such as WebDriverAgent while the developer disk image (DDI) is mounted. It stays
mounted until the phone restarts, so this step does real work once per phone restart.

On iOS 17+ the image is *personalised*: the phone gives a nonce, and Apple's signing server
(`gs.apple.com`, the TSS service) signs the image for that phone and that boot. Two consequences:

- **The phone must be unlocked** while mounting. Otherwise the mount fails with `DeviceLocked`. `iphone.sh` reads
  that from `image.log` and waits for the user to unlock.
- **The laptop must be online, and must trust Apple's root.** `gs.apple.com`'s certificate chains to the
  2006 *Apple Root CA*, which some distros' Mozilla-based CA stores do not include. Go's TLS then fails with
  `x509: certificate signed by unknown authority`. `certs/apple-root.pem` is that root (SHA-256 fingerprint
  `B0:B1:73:0E:…:F0:24`, from apple.com/certificateauthority). Go only takes extra roots through
  `SSL_CERT_FILE`, which *replaces* the system store, so `certs/ca-bundle.sh` writes the system bundle plus
  Apple's root into `$STATE/ca-bundle-with-apple.pem`. It finds the system bundle in the same places Go does
  (`/etc/ssl/certs/ca-certificates.crt`, `/etc/pki/tls/certs/ca-bundle.crt`, `/etc/ssl/ca-bundle.pem`, …),
  verifies `apple-root.pem`'s checksum, and rebuilds the bundle whenever the system store or the root is newer.
  `CA_BUNDLE=/path` overrides the system bundle.

`ios image auto` exits 0 even when the download fails, so `iphone.sh` decides from the log text
(`success mounting image`, `already a developer image mounted`, `DeviceLocked`). Images are cached in
`$STATE/ddi`.

go-ios 1.3.2 mounts with `mobile_image_mounter`. Very new devices may need the newer cryptex-based mounter,
which is only in go-ios releases after 1.3.2; the pins can be overridden (see [pins](#pins-and-updates)).

## WebDriverAgent

[WebDriverAgent](https://github.com/appium/WebDriverAgent) is an XCTest UI-test runner that never finishes its
"test": instead it serves an HTTP API (port 8100) built on XCTest's automation APIs, and an MJPEG stream of the
screen (port 9100). Appium publishes a prebuilt, unsigned `WebDriverAgentRunner-Runner.zip` for real devices;
`setup.sh` downloads it, checks its SHA-256, removes debug symbols and repacks it as `build/WebDriverAgent.ipa`.

`iphone.sh` starts it with

```
ios runwda --bundleid=<id> --testrunnerbundleid=<id> --xctestconfig=WebDriverAgentRunner.xctest \
  --log-output=- --env=USE_IP=127.0.0.1
```

where `<id>` is the bundle id the signing gave it (`com.facebook.WebDriverAgentRunner.xctrunner.<team id>`,
stored as `wda_bundle` in `config.json`). `USE_IP` makes WebDriverAgent listen on the phone's loopback
only, rather than also on its Wi-Fi address. `--log-output=-` puts the runner's own log in `wda.log`.

The window talks to it with plain HTTP (`iphone_control/wda.py`, `http.client`, no proxy ever):

- `POST /session`, then `POST /session/<id>/appium/settings` for the stream: `mjpegServerFramerate`
  (`IPHONE_FPS`, 30), `mjpegScalingFactor` (`IPHONE_SCALE`, 100), `mjpegServerScreenshotQuality`
  (`IPHONE_QUALITY`, 60; 10 under the USB picture, where nobody sees the stream) and `mjpegFixOrientation`
  (`IPHONE_FIX_ORIENTATION`).
  - **Keep the scale at 100.** Below 100, WebDriverAgent shrinks and re-encodes every frame itself in UIKit and
    CoreGraphics, at quality 90 or more whatever is set. iOS kills any process above 80% of one core averaged
    over 60 s (crash report `cpu_resource_fatal`). Measured on an iPhone 15 with iOS 26.5, it was killed at
    60 % scale about every 5 minutes, at 20 fps as at 30. At 100 the system's own JPEG passes through untouched:
    no kills in 20 minutes at 20 and 30 fps, including the live test.
  - At 100, the quality applies directly (and takes effect at once; the scale only on a new WebDriverAgent).
    Full-size frames (1178 x 2556) of the home screen are about 438 KB at quality 90, 344 at 75, 265 at 60,
    222 at 50 and 183 at 40.
  - Not measured: a landscape app with `mjpegFixOrientation`, where WebDriverAgent has to turn every frame
    itself and may get close to the limit again (`IPHONE_FIX_ORIENTATION=0` if it restarts there).
  - Neither a lower quality nor a higher frame rate makes the picture noticeably quicker: WebDriverAgent captures at
    most 28-30 frames a second at full size (about 35 ms a screenshot, even when 60 is asked for), and the stream
    never backed up at any quality from 20 to 60. A higher rate only costs phone CPU. A lower one makes touches
    slower, though, also when the stream is not shown (see [the window](#the-window)).
- In the same call, two settings for quick touches (`wda.INPUT_SETTINGS`; `IPHONE_FAST_INPUT=0` leaves
  WebDriverAgent's defaults): `snapshotMaxDepth` 1 (default 50) and `animationCoolOffTimeout` 0 (default 2).
  Together they bring a swipe's touch-down from about 290 ms to about 70 ms after the request, and its reply
  from about 1.2 s to 0.6 s ([latency](#latency)). The depth also limits element lookups, so
  `keyboardInput` with Shift (see below), which may look for the focused field, runs at depth 50 and the worker
  sets 1 again afterwards (2 ms a settings call). Every new WebDriverAgent session resets these two to the
  defaults (the MJPEG settings carry over), so the window applies them again whenever it reopens its session, and
  nothing needs undoing when it closes.
- `GET /session/<id>/window/size` gives the screen in points (393 x 852 on an iPhone 15 Pro).
- Touches are W3C pointer actions, `POST /session/<id>/actions`, with plain viewport `pointerMove`s. A whole
  gesture runs as one unit on the phone and ends with the finger lifted, which is why a mouse drag is collected
  and sent on release; it cannot be split into several requests without becoming several swipes. Nor can queued
  gestures be merged into one request: all touches in one request start at the same moment (WebDriverAgent and
  XCTest ignore leading pauses and path offsets), so every gesture is a request of its own. Every
  `pointerMove` costs time before the finger goes down (see [latency](#latency)), so swipes are sent with few.
  A request made only of pauses makes XCTest wait about 5 s for a touch path; the client never sends one.
- Text: `POST /session/<id>/wda/keys` with a `frequency` (characters a second) per request: 120 for typing
  (`TYPE_RATE`), 300 for Ctrl+V (`TYPE_RATE_FAST`); WebDriverAgent's own default is 60. Each request costs about
  290 ms before the first character; 50 characters arrived intact at 400 a second (409 ms, against 1120 ms at 60).
  Long text is sent in chunks of 500, and queued typing is merged into one request only at the same rate.
- Named keys (arrows, Delete, Home, End, Page Up/Down, Escape, Tab): without Shift, one
  `POST /session/<id>/wda/performIoHidEvent` with `{"page": 7, "usage": U, "duration": 0.01}`, a HID keyboard
  usage, about 215 ms a key (HID Home does nothing in a single-line field; HID Left moves the caret). Shift+Tab and
  Shift+arrows use `/wda/element/0/keyboardInput` with `XCUIKeyboardKey…` names and a modifier, about 400 ms,
  because a modifier cannot be held across HID calls. `wda.NAMED_KEYS` maps each GDK key name to both, as
  `NamedKey(xcui, usage)`. Only `keyboardInput` (Shift held) needs iOS 17 / Xcode 15 builds. An "unsupported" error
  turns off only the route it came from, for the session (`window.key_route`, `Mirror.keys_off`): for
  `keyboardInput` the window says "Shift+arrows, Shift+Tab and other Shift+key combinations need iOS 17 or later",
  for the HID event "arrow, Delete, Home, End and Escape keys are not supported by this WebDriverAgent".
- Buttons and system: `/session/<id>/wda/pressButton` (`home`, `volumeUp`, `volumeDown`, `action`),
  `/wda/lock`, `/wda/unlock`, `/wda/locked`, `/session/<id>/url`,
  `/session/<id>/wda/apps/launch`, `/wda/setPasteboard`, `/wda/getPasteboard`, `/screenshot`.
  Home is the `home` button press, not `/wda/homescreen`: that endpoint returns at once without pressing
  anything while SpringBoard is in front, so it cannot close Spotlight, Control Centre or the App Switcher
  (seen on iOS 26.5). The HID Home key (consumer usage 0x40) is no quicker: both reply after about 465 ms.

All commands run in order on one worker thread with a kept-alive connection. WebDriverAgent itself handles
requests one at a time (its HTTP routes run on the main queue), so a second connection would only wait for the
first. Queued typing is merged into one request, scroll swipes and Ctrl+wheel pinches are merged while one is
in flight, and when WebDriverAgent restarts (the session id becomes invalid) the worker opens a new session and
re-applies the settings.

## The port forward

WebDriverAgent's ports are on the phone. usbmuxd's `Connect` message opens a connection to any TCP port on the
phone over the cable (iproxy uses the same message); this works for ports 8100 and 9100 on iOS 17+ too, without
the tunnel.

go-ios' `ios forward` and libimobiledevice's `iproxy` both listen on **all** interfaces by default, which would
put an unauthenticated remote-control API on the local network. `iphone_control/forward.py` listens on
127.0.0.1 only, and checks every HTTP request on each connection before passing it on:

| Request | Answer | Why |
|---|---|---|
| has an `Origin` header | 403 | a web page (WebDriverAgent sends `Access-Control-Allow-Origin: *`, so a page could otherwise drive the phone) |
| `Sec-Fetch-Site` other than `none` | 403 | a cross-site browser request |
| `Host` other than `127.0.0.1`, `localhost`, `[::1]` (optionally with the local port), or more than one `Host` | 403 | DNS rebinding |
| chunked body | 501 | not needed by any client, and simpler to check |
| request head over 16 KiB | 431 | |
| malformed | 400 | |

The window (`http.client`, GStreamer's `souphttpsrc`) and `curl` send none of the refused headers.
The forward turns Nagle off (`TCP_NODELAY`) on the local sockets: without it, the small tail of most MJPEG
frames waited for the reader's delayed ACK, frames arrived in pairs, and each one landed about 28 ms (median,
up to 47) later than on a direct usbmux connection.
`--raw` turns the guard off (plain TCP). The forward prints `forwarding …` once it listens, exits 1 with
`cannot listen` when a port is taken, and exits 2 when the phone is unplugged, so the watchdog restarts it when
the phone comes back.

```
python3 -m iphone_control.usbmux list                 # DeviceID<TAB>UDID per phone
python3 -m iphone_control.usbmux device-id [--udid U]
python3 -m iphone_control.forward [--udid U] [--raw] 8100 9100      # LOCAL[:REMOTE] each
```

## Signing

iOS runs third-party code only if it is signed by a certificate that a provisioning profile ties to the device.
With an Apple ID, Apple's developer services issue both; with a free ("personal team") account the profile lasts
7 days and covers only the devices registered to that account.

`resign.sh` does the whole round with `plumesign sign --apple-id --register-and-install`: registers the phone
with the account, creates or reuses the App ID and a development certificate, signs every bundle inside the IPA,
and installs the result over usbmuxd. Then it:

- reads the bundle id and the profile's `CreationDate` and `ExpirationDate` from the signed IPA (and, if
  `openssl` is installed, the signing certificate's end date; the earlier wins), and writes `wda_bundle`,
  `signed_at` and `expires_at` to `$STATE/config.json` atomically;
- deletes the signed IPA, which contains the team id and the device's UDID;
- keeps `plumesign`'s output in `$STATE/resign.log` (it can contain the Apple ID) and shows only redacted
  error lines;
- maps failures to exit codes: 3 when the Apple ID is not logged in or its session expired, 4 when the phone is
  locked, 1 for anything else.

`iphone.sh` re-signs automatically when `expires_at` is less than an hour away, or when `wda.log` says the app is
missing or its signature is invalid. `$STATE/last-resign` limits automatic attempts to one an hour, and
`$STATE/resign.lock` keeps a manual and an automatic run from overlapping.

The Apple ID session lives in `~/.config/PlumeImpactor` (`accounts.json`, and the signing key in
`keys/*/key.pem`). Logging in needs a terminal: `plumesign account login -u <Apple ID>` asks for the password
and the two-factor code. The password is used in Apple's SRP exchange and is not sent anywhere else.

Apple's login also needs *anisette* data, a set of device-identity headers a real Apple device would send.
Impactor fetches them from an anisette server; its current default is `mac-ani.jkcoxson.com`, which emulates a
Mac. That server sees the requester's IP address and the anisette requests, but not the password.

## The patches

`setup.sh` builds `plumesign` from Impactor at a pinned commit with the patches in `patches/` applied. A patch
that is already upstream is skipped (`git apply --reverse --check`); one that no longer applies stops setup
with a message pointing here. Both were checked against Impactor `main` at `7f98856` on 23 Oct 2026.

**`0001-anisette-keep-server-client-info.patch`.** Around 12 Oct 2026 Apple began rejecting the sign-in of free
sideloading tools: errors -80045 or -80046, or an HTML page instead of a plist. Impactor `main` switched to a Mac
anisette server, but its anisette client still replaced the server's client-info string with a hard-coded
Windows one, and Apple rejects a Mac identity with a Windows client string (-80046). The patch keeps the server's
own client info. Flathub's 2.7.0 and the 2.7.1 release both still fail to log in
([Impactor #298](https://github.com/claration/impactor/issues/298)). The Flatpak build also freezes at the
password prompt, because there is no controlling terminal inside the sandbox; that is why a native build is used.

**`0002-sign-xctest-bundles.patch`.** `plumesign` signs `.app`, `.appex` and `.framework` bundles, but treated
`.xctest` as unknown and left `PlugIns/WebDriverAgentRunner.xctest` unsigned. iOS refuses to load an unsigned test
bundle, so WebDriverAgent installed fine and then never started. The patch signs `.xctest` like a framework.

## Bluetooth input (basic mode)

Without Developer Mode nothing on the phone takes touches from a computer, but iOS takes a Bluetooth mouse: with
AssistiveTouch on, it shows a pointer, and a click taps where the pointer is. `iphone_control/blehid.py` makes the
laptop such a mouse and keyboard over Bluetooth LE (HID over GATT), through BlueZ's D-Bus API with Gio (no
dbus-python). The report map, the keymap and every timing are in `hid.py`, which has no GLib or D-Bus and is
unit-tested on a simulated clock. Measured on an iPhone 15 Pro with iOS 26.5.2, October 2026, with AssistiveTouch
on, like everything below.

**The reports.** The report map has four input reports: 1 a relative mouse, 2 a keyboard, 3 consumer keys, and 4
an absolute pointer: 3 buttons and X and Y as uint16, 0 to 32767 across the screen (descriptor
`05010902a10185040901a1000509190129031500250195037501810295017505810105010930093116000026ff7f751095028102c0c0`).
Report 4 puts the AssistiveTouch pointer exactly where asked, so nothing needs calibrating: the window sends
fractions of the picture, which are fractions of the screen as the phone shows it. With two mouse collections iOS
ignores the relative report's buttons, so clicks and drags use report 4's button bits. The relative report's wheel
does not scroll: scrolling is a short pointer drag, as with WebDriverAgent.

**Timing.** Every action is a queue of reports and waits on the GLib main loop, one report per 8 ms tick at most
(about one per BLE connection event).

| | |
|---|---|
| a press lands where the pointer was *before* the report | so a tap is: move, wait 60 ms (50 ms was enough; 20 ms failed 1 in 2), press (report 4 with button 1 at the same point), 50 ms, release |
| a tap, from the command to the screen changing | 280-440 ms |
| a live drag | move to the start, wait, press, then absolute moves with button 1 held, release: the home screen changes page. The window sends one move per mouse motion event, and a move still queued is replaced by the newest (`hid.swipe` paces them every 12 ms) |
| an absolute report equal to the one before it | ignored by iOS, so a repeat is nudged by one unit |

**Keys.** Typing is HID keyboard usages with a US keymap; characters a US keyboard cannot type are counted, and the
window says what it skipped. While the phone is not connected nothing is typed, and the window says
"Bluetooth: the iPhone is not connected" (with the pairing or disconnected hint below it). The named keys are
keyboard usages too. Consumer usages: Home 0x40 (0x223 does nothing); Home twice 120 ms apart opens the App
Switcher; Search 0x221 opens Spotlight; volume 0xE9 / 0xEA; lock 0x30 (not tried on a phone yet). The Globe (fn)
key, consumer usage 0x29D, held over a keyboard key: fn+C opens Control Centre (153 ms), fn+N Notification Centre;
fn+Up does not open the App Switcher, and Cmd+Space does nothing. Ctrl+L opens Spotlight, waits 600 ms, types the
URL and presses Return; those waits are guesses, not measured.

**Advertising and pairing.** `blehid` registers with `bluetoothd` a GATT application (HID 1812 with the four input
reports, battery 180f, device information 180a), an LE advertisement (peripheral, the HID service, appearance
mouse, the name "*&lt;hostname&gt;* Mouse" or `IPHONE_HID_NAME`, discoverable by itself) and a pairing agent
(DisplayYesNo). The adapter is left as it is apart from Pairable, which is on while it runs and put back by
`stop()`; Discoverable, Alias and Powered are never written. Classic Bluetooth (BR/EDR) stays on and the adapter is
not made Classic-discoverable, and the phone pairs over LE all the same, so headphones keep working. (The old
`alt/bluetooth-mouse/` switched the adapter to LE only, with sudo, because a dual-mode adapter let the phone pair
over Classic, where no HID service exists.) The adapter is `BT_INDEX`'s hci*N*, else the first switched-on one with
both a GATT manager and an LE advertising manager (a switched-off one only when none is on).

- `RequestConfirmation` shows the six-digit code in the window with Yes and No, without blocking the main loop.
  Yes lets that one pairing through: it is good for 30 s (the SMP timeout), and a pairing that fails (the link
  drops first) or the bond it allowed uses it up. Trusted is set once Paired arrives; pairing again needs a new
  code. `Cancel` and closing the window reject it, and so does a second request while one waits. Pairing that types
  a code or needs none (`RequestPasskey`, `RequestPinCode`, `RequestAuthorization`) is refused.
- The agent is BlueZ's default one (`RequestDefaultAgent`): bluetoothd routes a pairing the phone starts, and the
  adapters' IO capability, through the default agent only. So it is asked about every device: `AuthorizeService`
  is allowed for any paired device on any adapter (as if trusted) and refused for unpaired ones, HID included, and
  pairing requests from devices on other adapters are refused while it runs (close iPhone Control to pair them).
- A device under that adapter that turns Paired or Bonded without a confirmed code is removed again
  (`Adapter1.RemoveDevice`). That includes one paired through the desktop's own Bluetooth settings while basic mode
  runs: BlueZ gives no way to tell such a bond from a stranger's.
- Method calls from anyone but `bluetoothd`'s unique bus name get AccessDenied, signals from anyone else are
  ignored, and the reports go out as `PropertiesChanged` signals addressed to `bluetoothd`, not broadcast, so other
  local programs cannot read keystrokes on the system bus (checked on a private test bus, not yet with a real
  adapter).
- After a report-map change, the phone must forget the device and pair again: iOS keeps the map it read.

`connected` means the phone subscribed to an input report and is connected. The phone is a device that read the
HID service's encrypted attributes or paired with a confirmed code; for a subscriber that read nothing (bonded
before, it kept what it read), it is the paired device connected on the adapter, looked up in bluetoothd. The
window shows the pairing hint while it is not ("On the iPhone: Settings > Bluetooth > … Mouse > Pair, then
Accessibility > Touch > AssistiveTouch on") and "the iPhone disconnected from Bluetooth" after it was. Start-up
errors become plain messages: `bluetoothd` not running, no LE adapter, Bluetooth off (`rfkill unblock bluetooth` or
`bluetoothctl power on`), or AccessDenied for a user outside the desktop session. If `bluetoothd` restarts,
`blehid` logs it and does not register again: Bluetooth input needs a new start. `stop()` then sends the new one
nothing (every call goes to the old one's unique bus name and never starts a bluetoothd by D-Bus activation), and
nothing is put back on an adapter that went away.

## The window

`wda_mirror.py` checks the Python version and PyGObject, sets the program name (which becomes the X11
`WM_CLASS` and the Wayland `app_id`, `iphone-control`, for window rules and the launcher's `StartupWMClass`), and
calls `iphone_control.window.main()`.

`main()` reads `IPHONE_MODE` (full when unset, so `wda_mirror.py` by hand works as before) and `IPHONE_VIDEO`,
checks the GStreamer elements of that picture (a USB capture socket that is not there yet is waited for), and in
full mode asks WebDriverAgent for `/status` and opens a session with the stream settings; in basic mode it never
contacts WebDriverAgent and starts `blehid` once the window exists. Then it builds the window:

- **Video** (`iphone_control/video.py`): one of three sources, each ending in a decoder named `dec`, the scaler's
  caps filter `fit` and `gtksink` named `sink`.
  - **MJPEG** (WebDriverAgent's stream): `souphttpsrc ! multipartdemux ! queue (leaky, 2 buffers) ! jpegdec !
    videoscale ! videoconvert ! gtksink`. The leaky queue drops old frames rather than building up delay.
  - **H.264** (the USB capture): `appsrc (live, timestamped as they arrive) ! h264parse ! decoder ! queue (leaky, 2
    buffers) ! videoscale ! videoconvert ! gtksink`. A reader thread connects to `IPHONE_CAPTURE_SOCKET` and pushes
    each access unit into `appsrc`; VIDEO_FORMAT sets the caps (`video/x-h264, stream-format=avc, alignment=au` with
    the avcC record as `codec_data`), and a rotation sends a new format and so new caps. The leaky queue sits after
    the decoder because compressed H.264 cannot be dropped: every frame after a lost one would be broken until the
    next keyframe. The capture sends no B-frames, so the decoder gives every frame out at once, also the last one
    before the screen goes still. STATUS text goes to the status bubble and stays there until the capture clears it
    (an empty STATUS, or a connection's first picture with no STATUS before it); 10 s without a frame, or the socket
    closing, counts as lost, and the reconnect gets the replay from the last keyframe.
  - **AirPlay**: the source starts UxPlay 1.73 or newer itself
    (`uxplay -n <name> -nh -p -vrtp "config-interval=1 ! udpsink host=127.0.0.1 port=<P>"`), which forwards the
    phone's H.264 untouched over RTP to a local UDP port, and decodes it with `udpsrc address=127.0.0.1 !
    rtph264depay ! h264parse ! decoder ! …`. UxPlay is started again when it exits and stopped when the window
    closes, also during the window's start-up; it gets SIGTERM when the window dies in any other way
    (`PR_SET_PDEATHSIG`), and `./iphone.sh stop` stops it too. Its log is `$RUN/uxplay.log`, and its pid is in
    `$RUN/uxplay.pid` while it runs (both mode 600). AirPlay sends frames only while the screen changes.
  - The decoder is the first installed of `vah264dec` (VA-API, hardware), `avdec_h264` (FFmpeg) and `openh264dec`;
    `nvh264dec` only when `IPHONE_DECODER` names it (the code says it holds a few frames back; not measured here).
    There is no fallback to the next decoder when one fails at run time: `IPHONE_DECODER` is the way out.
  - `videoscale` (Lanczos, up to 4 threads) shrinks each frame to the picture's size in device pixels on
    GStreamer's thread; left to `gtksink`, cairo did it on the GTK main thread in about 20 ms a frame, and mouse
    events waited behind it (up to 28 ms). Measured offline on recorded frames into 748 x 1620: 24 ms from frame to
    drawn before, 10.5 ms after, with the same sharpness (SSIM 0.997 against a reference; bilinear is cheaper but
    visibly softer). The touch mapping and the orientation come from the decoder's frame size, before scaling.
  - Every source keeps its last decoded frame (a probe on `dec`'s source pad, so at the phone's resolution): F12
    saves it as a PNG (`appsrc ! videoconvert ! pngenc`) in basic mode, and in full mode when WebDriverAgent's own
    screenshot fails.
  - When the picture fails it reconnects with backoff (0.5 s doubling to 5 s); for MJPEG every fifth failure asks
    WebDriverAgent whether it is still alive. A capture socket that is missing, also at start, is waited for the
    same way ("waiting for the USB picture…"). In full mode, after 15 s without the socket and before the first USB
    picture (the capture stopped while `iphone.sh` started WebDriverAgent and did not come back), the window shows
    WebDriverAgent's MJPEG stream instead for the rest of the run, at its normal quality rather than 10, stops the
    hidden stream, and says so in a flash and in `window.log`.
  - **The hidden MJPEG stream.** In full mode with the USB picture, the window still keeps a connection to
    WebDriverAgent's MJPEG port and throws the frames away (`video.MjpegDrain`, reconnecting with the same backoff),
    with `mjpegServerScreenshotQuality` 10 unless `IPHONE_QUALITY` is set. WebDriverAgent captures the screen only
    while a client is connected, and its touches are slower without that capture at about 30 frames a second: a
    swipe's first moved frame came 294 ms after the request at 1 frame a second, 245 ms at 10 and 173-177 ms at 30;
    the quality made no difference (10 as quick as 60).
- **Mapping**: `geometry.letterbox` finds where the frame is drawn inside the widget; `geometry.to_phone` maps a
  pixel to a phone point. Both coordinates are logical pixels, so HiDPI scaling cancels out. When the frame turns
  landscape (`mjpegFixOrientation`, or a new format from the USB capture), the axes swap and the window turns with
  it; in full mode WebDriverAgent's size is read again. Basic mode has no WebDriverAgent to ask the size in points,
  so it estimates it as picture pixels / 3, with at least 320 points across the short side. Only gesture
  thresholds and lengths use the estimate (tap slop, scroll distance, edge margins); the pointer goes by fractions
  of the picture, so where a click lands is exact.
- **Input backends**: `WdaBackend` (full mode) drives WebDriverAgent through `wda.WDA`; `BleBackend` (basic mode)
  drives `blehid`. Each says what it can do (`features`: the clipboard, apps by bundle id, pinch, the Action
  button); the keys and the help text follow it, and the rest shows "… needs full mode (WebDriverAgent)".
- **Clicks** (full mode): a click taps when the button is released, at once. GTK sends a double-click as press,
  release, press, double-press, release; two tap requests sent back to back land about 330 ms apart on the phone,
  and iOS read them as a double tap 10 times out of 10 in a text field (with 50 ms more between them, as two
  single taps). So nothing is held back, a click then a drag is a tap then the drag, and a double-click is never
  three taps. `IPHONE_DOUBLE_CLICK_MS` above 0 brings back the old behaviour: a click waits that long (at most
  GTK's double-click time) for a second one, and the pair goes as one `geometry.double_tap()` request, which also
  selects a word; the wait cost every click about 200 ms.
- **Gestures** (`geometry.py`, pure functions with unit tests): a press that moves less than 10 points is a tap,
  held over 0.45 s a long press. In full mode a drag is replayed quickly (`shape_swipe`): one of up to 0.25 s as
  it was; a longer one keeps its last 0.1 s at the user's speed, because iOS decides fling or not, next page or
  not, from how the finger lifts, and the path before that is hurried (at most 0.15 s). A drag that stopped (still
  for 0.1 s before release) ends at rest, so it does not fling; long stops are cut to 0.3 s, except from the home
  indicator, where up to 1 s is kept because that hold opens the App Switcher. A swipe is sent with only a few
  moves (`MAX_SWIPE_MOVES`), chosen so the replay keeps its path and timing. Holding still before dragging starts
  with a long press (to move icons; up to 12 points with their timing); scrolling is a short swipe that starts
  inside the safe middle of the screen and pauses before lifting so it does not fling; pinch uses two pointers;
  back is a swipe from the left edge; Control Centre, notifications and the app switcher are edge swipes. In basic
  mode a drag is live: once the mouse moved 10 points the finger goes down where it started and follows every
  motion event; a button held still for 0.45 s presses there and then and stays down at least 0.8 s (a long
  press; moving on drags what it holds). Scrolling and back are the same gestures played through the pointer, and
  the system gestures are `blehid`'s key combinations (Home twice, fn+C, fn+N).
- **Keyboard**: every key goes to the GTK input method first (IBus, Fcitx, dead keys), then Return, BackSpace and
  Tab are sent as `\n`, `\b`, `\t`, named keys go as HID events (Shift: `keyboardInput`), and anything printable is
  typed; Ctrl+V types at the paste rate. In basic mode all of it becomes Bluetooth keyboard usages.
- **Status**: the client's messages (video lost, failed commands) go to stdout (`window.log`) and, at most every
  2 s, to a bubble over the picture. In basic mode the Bluetooth state stays above it while the phone is not
  connected, and a pairing request opens a Yes/No dialog with the code. WebDriverAgent's own log is `wda.log`.

Fatal errors go to stderr (`window.log`) and, without a terminal and not under `iphone.sh`
(`IPHONE_FROM_SCRIPT=1`), to a desktop notification; the window then exits 1, which `iphone.sh` reports.

## Latency

Measured in October 2026 on an iPhone 15 Pro with iOS 26.5.2. `tests/latency_probe.py` measures the totals on any
phone ([CONTRIBUTING.md](../CONTRIBUTING.md#tests)): a 250 ms page swipe on the home screen sent with two
`pointerMove`s (to where the finger goes down, then one move), the window's replay of a mouse drag
(`--window-drag`) or a tap on the Search pill (`--tap`), each timed from sending the request to its reply and to
the first changed frame, on WebDriverAgent's MJPEG stream or, with `--video usb`, on the USB capture.

**This version against the one before** (medians, to the first changed frame):

| | before | now, USB picture | now, MJPEG picture |
|---|---|---|---|
| page swipe sent by script | 180 ms (reply 578 ms) | 180 ms | 190 ms |
| replay of a 600 ms mouse drag | 235 ms (reply 629 ms) | 246 ms | 254 ms |
| tap | the same tap, plus about 200 ms that every click waited for a possible double-click | 196 ms (reply 342 ms) | 249 ms |
| the picture | MJPEG, 27.7 frames a second | H.264, about 42 frames a second on a still screen and 50 while it moves, about 6 Mbit/s | MJPEG, as before |

Swipes and drags did not get quicker: the USB picture shows them about 10 ms before the MJPEG one does. Taps show
about 50 ms earlier on it, and no longer wait for a double-click. The commands themselves (replies):

| | before | now |
|---|---|---|
| a named key | about 400 ms (`keyboardInput`) | about 215 ms (`performIoHidEvent`); with Shift still about 400 |
| typing 50 characters | 1120 ms at 60 a second | 409 ms at 400 a second (typing is sent at 120, Ctrl+V at 300) |
| Home | about 465 ms (`pressButton`) | the same; the HID Home key is as slow |

Basic mode, through Bluetooth: a tap reaches the screen 280-440 ms after it is sent, and Control Centre (fn+C)
153 ms.

**WebDriverAgent's touches need its own capture running** (`mjpegServerFramerate`; the first moved frame of a swipe
on the USB picture):

| frame rate | first moved frame | reply |
|---|---|---|
| 1 | 294 ms | 673 ms |
| 10 | 245 ms | |
| 30 | 173-177 ms | 582-586 ms |

The JPEG quality made no difference (10 as quick as 60), and WebDriverAgent captures only while a client is
connected to its MJPEG port: hence the hidden stream at 30 frames a second and quality 10 under the USB picture.

The measurements below were made earlier, with WebDriverAgent 16.15.2 and the window's stream settings (quality
60, 30 fps, scale 100), on the MJPEG picture. The stages come from WebDriverAgent's own log (`wda.log` has the
phone's millisecond timestamps of XCTest's steps) and from recordings of the stream.

**From the request to the touch** (times from sending the request):

| | WebDriverAgent's defaults | `INPUT_SETTINGS` (depth 1, cool-off 0) |
|---|---|---|
| the request reaches the phone | 1-6 ms (a kept-alive `GET /status` round trip is 0.9 ms) | the same |
| the finger touches down: XCTest finds the app in front, then for every `pointerMove` takes two accessibility snapshots of it to place the point | 290 ms on the first home page, 380 on the third, 590 in the App Library; about 110 ms more for every further move (a 14-move drag: 1.6 s) | 65-80 ms, on the first page and in the App Library alike; about 27 ms more for every further move |
| the gesture runs; XCTest reports it done | gesture time + about 260 ms, mostly after the finger lifts | the same |
| WebDriverAgent waits for animations to settle (`animationCoolOffTimeout`) | about 380 ms | none |
| the reply; only now can the next command start | 1183 ms (1179-1185) | 577 ms (569-586) |
| the first changed frame in the stream | 419 ms (392-426) | 210-216 ms (180-249) |

The waits after the finger lifts do not delay the picture, but they delay whatever is queued behind: two quick
swipes in a row finished 2.3 s after the first was sent with the defaults, 1.15 s with `INPUT_SETTINGS`. With
both settings, Control Centre opened (its reply 1.4 s down to 0.57 s), page swipes sent mid-animation still
registered, and the App Switcher gesture behaved as with the defaults (whether the switcher itself appeared
could not be confirmed either way). Typing and named keys were measured later (above); not tried with these
settings: an app in front.

**From the phone's screen to the window** (MJPEG, quality 60, 30 fps):

| | |
|---|---|
| capture: WebDriverAgent takes a screenshot back to back, about 35 ms each, so 28-30 fps at most | about 17 ms waiting for the next one, on average |
| USB transfer of a 263 KB frame | about 5 ms |
| WebDriverAgent's queue for each client (it keeps at most 4 unsent frames and drops older ones; never reached here, even with 4 clients and 30 MB/s through usbmux) | 3-17 ms |
| the forward, before `TCP_NODELAY` | about 28 ms (now gone) |
| decode, scale and draw in the window | 24 ms before `videoscale`, 10.5 ms with it (offline) |
| compositor and display | not measured |

So the MJPEG picture is about 0.1-0.15 s behind the phone, less with the two fixes (about 40 ms, measured stage by
stage, not end to end). The USB picture was not measured stage by stage. Most of a swipe's delay was on the input
side: the drag only reaches the phone on release, it used to be replayed at the user's speed, and every move cost
two snapshots.

**What does not help** (measured; listed so nobody "optimises" this way again):

- Merging queued gestures into one W3C request: all touches in one request start at the same moment (WebDriverAgent
  and XCTest ignore leading pauses and path offsets), so they would run at once instead of one after the other.
- Holding a click back to send a double tap in one request: it cost every click about 200 ms, and two plain tap
  requests back to back already make a double tap.
- `waitForIdleTimeout`: no effect on `/actions`. The `shouldWaitForQuiescence` capability is only read together
  with a bundle id. `defaultActiveApplication`: no gain on the home screen, and with an app in front it adds a
  hit test of up to 0.3 s to every request.
- More connections or pipelining: WebDriverAgent runs requests one at a time; a second swipe sent 50 ms after
  the first started only after the first one's reply.
- `/wda/dragfromtoforduration` and `/wda/pressAndDragWithVelocity`: six snapshots, touch-down after about
  550 ms with the defaults. Moves relative to the pointer cost the same as viewport moves.
- A W3C request with only pauses: about 5.3 s.
- The Home button: the press starts 2 ms after the request, but the reply always takes about 460 ms, by
  `pressButton` and by the HID Home key alike.
- A lower JPEG quality: quality 40 saves about 2 ms of transfer a frame. A higher frame rate: capture is the
  limit, so it only adds phone CPU, which is what iOS kills WebDriverAgent for (`cpu_resource_fatal`). A scale
  below 100: those kills. A lower frame rate under the USB picture: slower touches (above).
- An early measurement suggested quality 30 showed a swipe after 415 ms against 1188 at quality 60. It was not
  reproduced: measured again at quality 60, the first changed frame came after 392-426 ms. That script decoded every frame on
  the thread that read the stream (about 21 ms a frame); the probe only records arrival times there and decodes
  afterwards.
- Bluetooth: a press only 20 ms after the move (failed 1 in 2); the mouse wheel (does not scroll); a relative
  mouse calibrated from the picture, as `alt/bluetooth-mouse/` had (the absolute report places the pointer
  exactly).

## The watchdog

While the window is open, a background loop checks every 3 s:

1. Is the phone still on usbmuxd? When two looks in a row miss it, say "iPhone unplugged - plug it back in." once
   and wait, but not within 15 s of a capture's USB reset, which takes the phone away for a moment: one after the
   watchdog restarted the capture, and the capture's own after a stall, seen in its "screencap: resetting the
   iPhone's USB connection…" lines in `cap.log`.
2. With the USB picture: is the capture process running? If not, its exit status in `$RUN/cap.status` decides
   (written by the command line `iphone.sh` starts it with; none when it crashed or was killed). 4 (no picture)
   says "Unlock the iPhone - waiting for it..." once and starts it again after a replug or every 30 s, never
   counted. 2 (unplugged; the phone is back) starts it again at once, not counted either. Any other stop restarts
   it and says "USB video stopped - restarting it..." once (not after a replug, where that is expected); five of
   those in a row, each less than a minute after its start, end the restarts with "USB video keeps stopping -
   close the window and start again". The capture process has its own stall watchdog inside
   ([USB screen capture](#usb-screen-capture)).

In basic mode that is all. In full mode it goes on:

3. Is the forward running? Restart it if not.
4. Does WebDriverAgent answer? Five silent checks in a row (15 s) count as a failure.
5. On iOS 17+, is the tunnel up? Restart our tunnel process if it died.
6. Is the disk image mounted? Remount it (waiting for unlock) after a phone restart.
7. Is the signature about to run out (within an hour)? Then tell the user to close and start again, which
   re-signs.
8. Restart WebDriverAgent. Five failures in a row, each within a minute of its start, say "WebDriverAgent keeps
   stopping - close the window and start again"; a WebDriverAgent that a capture's USB reset ended does not count.
   That, like an expiring signature (step 7), ends only the watching of steps 3-8: the capture stays watched.

The window reconnects its picture and its session on its own once the capture or WebDriverAgent is back.

## Files and state

Nothing machine-specific or personal is kept in the repo.

| Path | What | Written by |
|---|---|---|
| `$STATE` = `${XDG_STATE_HOME:-~/.local/state}/iphone-control` (mode 700) | | |
| `config.json` | `wda_bundle`, `signed_at`, `expires_at` (epoch seconds), `recent_apps` | resign.sh, the window |
| `selfIdentity.plist` | go-ios tunnel identity (private key) | go-ios |
| `ddi/` | developer disk images | go-ios |
| `ca-bundle-with-apple.pem` | system CAs + Apple Root CA | certs/ca-bundle.sh |
| `last-resign`, `resign.lock`, `resign.log` | re-sign rate limit, lock, plumesign output | iphone.sh, resign.sh |
| `go-ios.stamp`, `plumesign.stamp`, `usbmuxd.stamp` | what setup installed, to skip it next time (`usbmuxd.stamp`: the commit and the SHA-256 of the program built, also written without systemd) | setup.sh |
| `$RUN` = `${XDG_RUNTIME_DIR:-$STATE/run}/iphone-control` (mode 700, owned by the user) | | |
| `iphone.pid`, `cap.pid`, `tunnel.pid`, `fwd.pid`, `wda.pid`, `watchdog.pid`, `window.pid`, `lock` | running processes | iphone.sh |
| `cap.sock` | the USB capture's socket (mode 600) | screencap.py |
| `cap.status` | the capture's exit status, for the watchdog | iphone.sh (the capture's command line) |
| `mode` | the running mode, picture and reason (for `status`) | iphone.sh |
| `iphone.log`, `cap.log`, `cap.prev.log`, `tunnel.log`, `image.log`, `wda.log`, `wda.prev.log`, `fwd.log`, `window.log`, `gst.log`, `resign.log`, `ca.log` | logs | iphone.sh |
| `uxplay.log`, `uxplay.pid` | UxPlay's output and, while it runs, its pid (both mode 600), with the AirPlay picture; `iphone.sh` stops it from the pid file too | the window |
| `~/.cache/iphone-control` | go-ios zip, WebDriverAgent zip, Impactor clone and its cargo build, the usbmuxd clone and `usbmuxd-build.log` | setup.sh |
| `~/.local/bin/ios`, `~/.local/bin/plumesign` | the two tools | setup.sh |
| `~/.config/PlumeImpactor` | Apple ID session, signing key | plumesign |
| `~/.local/share/applications/iphone-control.desktop` | launcher entry | setup.sh |
| `build/WebDriverAgent.ipa`, `build/WebDriverAgent.ipa.stamp` | unsigned IPA and its version | setup.sh |
| `/usr/local/sbin/usbmuxd-iphone-control`, `/etc/systemd/system/usbmuxd.service.d/iphone-control.conf` | the newer usbmuxd and the drop-in that starts it with `USBMUXD_DEFAULT_DEVICE_MODE=2` | setup.sh --usbmuxd (sudo) |
| `/var/lib/bluetooth/` | the Bluetooth pairing with the phone | bluetoothd |

## Pins and updates

`setup.sh` pins every download:

| | Version | SHA-256 |
|---|---|---|
| go-ios `go-ios-linux.zip` | v1.3.2 | `a55fdb4c507391c0548252e01d1deb6ae3c7fd99cfbce842c6f80569cc125604` |
| `ios-amd64` in it | | `24d6149a9d9ab65e17e55313312bcc59c98a7d51d692f9c37b0a0ba26f57623e` |
| `ios-arm64` in it | | `ae0553a2c74e1271b55ca90f317030df14eb6a141208002b6905ca4b2d6afe1b` |
| WebDriverAgent `WebDriverAgentRunner-Runner.zip` | v16.15.3 | `2176793538d902f6b1e61291e9997f68d306541f222ea715d256a7c6ce9d4fc5` |
| Impactor | commit `7f9885614fb071eae17866731a1e071805895e15` | (git commit) |
| usbmuxd (`--usbmuxd`), libplist 2.6 or newer | commit `3ded00c9985a5108cfc7591a309f9a23d57a8cba` (6 Dec 2025) | (git commit) |
| usbmuxd (`--usbmuxd`), libplist 2.3-2.5 | commit `e9a0dce170f156846d0e56c7fe9cc1e828a34b44` (14 Sep 2024) | (git commit) |
| `certs/apple-root.pem` (file) | | `f476b5c8ebe6468094981fb6a986e3d930fd020f6f379248000395a1a854c125` |

To try another release without editing the script, give both the version and its checksum:

```
GO_IOS_VERSION=v1.x.y GO_IOS_SHA256=<sha of go-ios-linux.zip> ./setup.sh
WDA_VERSION=v1x.y.z WDA_SHA256=<sha of WebDriverAgentRunner-Runner.zip> ./setup.sh
IMPACTOR_COMMIT=<commit> ./setup.sh --rebuild
USBMUXD_COMMIT=<commit> ./setup.sh --usbmuxd
```

A new version without its own checksum is refused. `plumesign` needs Rust 1.88 or newer at the pinned commit
(edition 2024, and several dependencies need 1.88). The usbmuxd commit is fetched from
`github.com/libimobiledevice/usbmuxd` and checked out exactly (`git rev-parse HEAD`); `USBMUXD_COMMIT` from the
environment is used as given, whatever the libplist. `./iphone.sh doctor` compares the installed tools with the
pins in `setup.sh` and warns when they differ.

## Known upstream issues

- [go-ios #772](https://github.com/danielpaulus/go-ios/issues/772): on iOS 26.5 the userspace tunnel can drop
  shortly after connecting. Not seen with 26.5.2 here; the watchdog restarts the tunnel and WebDriverAgent.
- [go-ios #875](https://github.com/danielpaulus/go-ios/issues/875): `ios image auto` failures; the TLS case is
  what the CA bundle fixes.
- [go-ios #631](https://github.com/danielpaulus/go-ios/issues/631) (closed): `runwda` on iOS 26 needs the
  userspace tunnel, which is what is used.
- [Impactor #298](https://github.com/claration/impactor/issues/298): SRP error -80046 at login; fixed here by
  `patches/0001`.
- usbmuxd 1.1.1, the last release, switches the phone out of its capture configuration; git keeps it with
  `USBMUXD_DEFAULT_DEVICE_MODE=2`, but needs libplist 2.3 or newer (master: 2.6), which Debian 12 and Ubuntu 22.04
  do not have (`./setup.sh --usbmuxd`).

## Other approaches

- **QuickTime-style USB screen capture** (as in [quicktime_video_hack](https://github.com/danielpaulus/quicktime_video_hack)):
  now the USB picture, see [USB screen capture](#usb-screen-capture). It gives video only, no input, which is why
  input comes from WebDriverAgent or Bluetooth. [Issue #159](https://github.com/danielpaulus/quicktime_video_hack/issues/159)
  reports it broken on iOS 18; on iOS 26.5.2 it works, with a usbmuxd that keeps the capture configuration (whether
  that was #159's cause was not checked).
- **AirPlay + a relative Bluetooth mouse** (the earlier `alt/bluetooth-mouse/`): UxPlay received the screen over
  Wi-Fi, and the laptop moved the AssistiveTouch pointer as a relative mouse, from the corner by a number of counts
  calibrated from the picture, with the adapter switched to LE only. Basic mode replaces it: the absolute pointer
  report needs no calibration, Classic Bluetooth stays on, and AirPlay is only the fallback picture.
- **CoreDevice screen stream** (`pymobiledevice3 developer core-device display`): a hardware-encoded HEVC
  stream over the iOS 17+ developer tunnel. Not tried: the USB capture already gives hardware H.264 without
  Developer Mode, and this one needs its own tunnel next to go-ios', which may disturb the running one, a Linux
  HEVC decoder, and nothing confirms it on iOS 26.5 or says what its latency is.
- **Wi-Fi WebDriverAgent**: possible (`IPHONE_WDA_BIND=`), but it exposes an unauthenticated control API on the
  network and is slower; USB is used instead.
