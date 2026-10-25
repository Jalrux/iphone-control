# Contributing

Thanks for helping. Reports from distros, desktops, iPhones and iOS versions that are not yet tested are the most
useful thing right now (see the README's "Tested" table).

## Reporting a problem

Include:

- the distro and version, CPU (`uname -m`), desktop or window manager, Wayland or X11;
- the iPhone model and iOS version;
- the output of `./iphone.sh doctor`;
- the mode (full or basic) and the picture (USB, MJPEG or AirPlay), as `./iphone.sh status` shows them;
- the message you saw, and the relevant log from `$XDG_RUNTIME_DIR/iphone-control/` (`iphone.log`, `cap.log`,
  `wda.log`, `tunnel.log`, `image.log`, `fwd.log`, `window.log`, `uxplay.log`, `resign.log`).

**Redact before posting:** the phone's UDID (`00008130-…` or 40 hex digits), your team id (10 capital letters and
digits after `xctrunner.`), your Apple ID email, Bluetooth addresses (`AA:BB:CC:DD:EE:FF`) and the hostname in the
Bluetooth and AirPlay names. `./iphone.sh doctor` already redacts UDIDs, team ids and email addresses in the log
lines it prints; raw log files do not. Never post anything from `~/.config/PlumeImpactor` or
`~/.local/state/iphone-control`.

## Rules for changes

- **No personal data in the repo**: no Apple IDs, team ids, UDIDs, hostnames, home paths or real names in code,
  docs, tests or fixtures. Machine state belongs in `~/.local/state/iphone-control`, logs in
  `$XDG_RUNTIME_DIR/iphone-control`. The history counts too: commit as a GitHub
  `<id>+<user>@users.noreply.github.com` address and follow [Before publishing](#before-publishing).
- **Python**: 3.9+, standard library only, plus PyGObject from the distro, which only `window.py`, `video.py`,
  `blehid.py`, `wda_mirror.py` and the tests may import. Keep GTK and GLib out of `geometry.py`, `wda.py`,
  `usbmux.py`, `forward.py`, `usbfs.py`, `valeria.py`, `screencap.py` and `hid.py` so they stay testable (the
  integrity test imports every other module with `gi` blocked), and keep the modules flat in `iphone_control/`.
- **Shell**: bash with `set -u`, short comments, and clean under ShellCheck:
  `shellcheck -x iphone.sh resign.sh setup.sh uninstall.sh certs/ca-bundle.sh`.
- **Portability**: no distro-specific paths or tools without a fallback. New system dependencies go into
  `setup.sh`'s package table for all four families (apt, dnf, zypper, pacman) and into the README.
- **Security**: nothing may listen on anything but 127.0.0.1 or a UNIX socket of mode 600 in `$RUN`, except UxPlay
  for the AirPlay picture (the phone reaches it over Wi-Fi), and only while that picture is used; keep the
  forward's request guard; keep Bluetooth pairing behind a code the user confirmed; keep secrets mode 600 / 700.
- **Pins**: a new go-ios or WebDriverAgent version needs its SHA-256 (from the GitHub release's asset digest,
  checked against a fresh download). A new Impactor commit needs `./setup.sh --rebuild` and a check that both
  patches still apply or are upstream. A new usbmuxd commit (`USBMUXD_COMMIT`) needs a build against the libplist
  it requires and a check on a phone that the USB picture still starts. Update the pins table in
  `docs/how-it-works.md`.
- **Docs**: user-facing changes (keys, commands, variables, messages) go into `README.md`; internals into
  `docs/how-it-works.md`.
- **Latency**: measure input or video changes with `tests/latency_probe.py` before and after (swipes, `--tap`,
  and `--video usb` for the USB picture), and give the numbers. What was measured, and what turned out not to
  help, is in [docs/how-it-works.md](docs/how-it-works.md#latency). In short: send gestures as W3C `/actions` with
  plain viewport moves and as few of them as keep the gesture (each one costs two accessibility snapshots before
  the finger goes down), one gesture per request (all touches in a request start at once, so gestures cannot be
  merged), never a request of pauses only, no second connection (WebDriverAgent runs one request at a time), no
  `defaultActiveApplication`, keep `IPHONE_SCALE` at 100, and keep WebDriverAgent's MJPEG capture running at
  about 30 frames a second, also under the USB picture (its touches are slower without it).
- **Hardware in tests**: no test may touch a real iPhone, usbmuxd, USB device (`/dev/bus/usb`), Bluetooth adapter
  or the system D-Bus. Use the fakes below.

## Tests

No phone is needed for the suite; nothing in it talks to an iPhone, usbmuxd, a USB device, a Bluetooth adapter,
the system D-Bus, Apple or the network:

```
python3 -m unittest discover -s tests
```

It takes about 7 minutes, most of it `test_scripts.py` (about 6). One module: `python3 -m unittest tests.test_hid`,
or `python3 -m unittest discover -s tests -p 'test_hid.py'`. If a `tests` package is installed in your user
site-packages (`~/.local/lib/python3.X/site-packages/tests`, left there by some pip package), the dotted form
imports that one instead and fails with "No module named tests.test_hid": `python3 -s -m unittest tests.test_hid`
skips the user site (`discover -s tests` is not affected).

| File | Covers |
|---|---|
| `tests/test_geometry.py` | pixel-to-point mapping and the W3C gesture builders (every finger starts with a zero-length move, never a leading pause) |
| `tests/test_wda.py` | the WebDriverAgent client against a fake WebDriverAgent server, including named keys as HID events and the typing rates |
| `tests/test_usbmux.py` | the usbmuxd client and the loopback port forward, against a fake usbmuxd |
| `tests/test_usbfs.py` | the USB layer: descriptors and lookup in a fake sysfs tree, the kernel's ioctl numbers, the error paths against a plain file; no USB device is opened |
| `tests/test_valeria.py` | the capture protocol, against quicktime_video_hack's protocol fixtures and synthetic sample buffers |
| `tests/test_screencap.py` | the capture process against a fake iPhone: the socket server and its clients, sessions, the reset fallback, the stall watchdog, exit codes, signals in a real subprocess (about 35 s) |
| `tests/test_video.py` | the window's picture sources against a fake capture socket, a fake MJPEG server and a fake `uxplay` (its pid file, and that it dies with its parent); the H.264 tests need the bad plugins and a decoder |
| `tests/test_hid.py` | the Bluetooth report map, packing, keymap, and every action's reports and waits on a simulated clock |
| `tests/test_blehid.py` | `blehid` against a fake `bluetoothd` on a private `dbus-daemon`: registration, the GATT tree, notifications, the pairing agent, removal of unconfirmed bonds, errors; a D-Bus activation stand-in for `bluetoothd` (it only leaves a mark) checks that `blehid` never starts one; never the system bus (needs PyGObject and `dbus-daemon`) |
| `tests/test_window.py` | the window's helpers and both input backends, against a fake phone and a fake Bluetooth HID on a virtual clock; the real GTK window only with `IPHONE_TEST_WINDOW=1` and a display. The window in a subprocess never imports the real `blehid` (`run_window` blocks it by default) |
| `tests/test_scripts.py` | `iphone.sh`, `resign.sh`, `uninstall.sh`, `certs/ca-bundle.sh` in a sandbox with fake `ios`, `plumesign`, usbmuxd and screen capture (it replaces `iphone_control/screencap.py` in the sandbox's copy of the repo, so `iphone.sh` runs it through its real command line), a fake sysfs and `/proc` and stub `busctl`, `bluetoothctl`, `usbmuxd`, `uxplay`, `sudo` and `systemctl`: the modes, the capture and its fallbacks, the watchdog, `status` and `doctor`, removing the usbmuxd override |
| `tests/test_setup.py` | `setup.sh` with stub tools: `--check`, `--basic`, `--usbmuxd` (with `sudo`, `systemctl`, `git`, `make` and `pkg-config` stubbed), distro and CPU detection, pins, launcher entry, Hyprland rule |
| `tests/test_privacy_and_integrity.py` | repo-wide guards: no personal data, pinned downloads, loopback defaults, file modes, stdlib-only Python 3.9, `gi` only where allowed |
| `tests/test_live_test.py` | the live test's own safety, against a simulated home screen |
| `tests/test_latency_probe.py` | the latency probe's numbers and safety, against a simulated home screen, MJPEG stream and capture socket |
| `tests/support.py`, `tests/scriptenv.py`, `tests/valeria_fake.py` | shared fakes (usbmuxd, WebDriverAgent, a capture socket, a hand-built H.264 stream; the script sandbox; an iPhone that speaks the capture protocol, and its sysfs tree); not tests |
| `tests/fixtures/h264/` | two synthetic 5-frame clips (64x128 and 128x64) as a capture socket sends them, in base64; `test_video.py`'s docstring has the command that made them |

Switches:

- `IPHONE_TEST_WINDOW=1` builds the real window (never mapped) against a fake stream, also with the USB picture
  and in basic mode. It needs PyGObject and a display; without one use
  `IPHONE_TEST_WINDOW=1 xvfb-run -a python3 -m unittest discover -s tests -p 'test_window.py'`.
- `IPHONE_SLOW_TESTS=1` adds the tunnel-timeout scenarios (about 60 s each).

Hooks, for the tests only: `iphone.sh` reads the USB devices from `IPHONE_SYSFS` (default `/sys/bus/usb/devices`)
and the running usbmuxd from `IPHONE_PROC` (default `/proc`), so `scriptenv.py` points both at fake trees
(`Sandbox.process()` adds a process to the fake `/proc`, and `Sandbox.run()` takes `umask=`). It also rewrites
`MUX_DIRS` and `SYS_OWNER` in its copy of `iphone.sh`, so the stub `usbmuxd` stands in for the system's.
`test_setup.py` and `test_scripts.py` rewrite `MUX_BIN`, `MUX_DROPIN` and `SYSTEMD_RUN` in their copies of
`setup.sh` and `uninstall.sh`, and `screencap.main()` takes its USB opener and sysfs root only from code
(`valeria_fake.py`), so no test reaches `/dev/bus/usb`, `/usr/local` or `/etc`.

Add a test with every behaviour change.

**The live test** (`tests/live_test.py`) drives a real phone, so it is not part of the suite. It sends only
system gestures on the home screen (Home, page swipes, Spotlight, Control Centre, App Switcher, volume), never
taps an app or sends Return, and ends on the home screen. It needs full mode. Start `./iphone.sh`, unlock the
phone, leave it alone, then:

```
python3 tests/live_test.py --i-am-not-using-the-phone [--url http://127.0.0.1:8100] [--out DIR]
```

Exit status: 0 passed, 1 a step failed, 2 refused (no flag), 3 not ready (WebDriverAgent unreachable or the
phone locked). The screenshots in `--out` show the phone's screen; check them before attaching any.

**The latency probe** (`tests/latency_probe.py`) also drives a real phone, so it is not part of the suite either.
It needs full mode. It presses Home twice, then swipes to the next home page and back in the middle of the screen,
and nothing else: no typing, long presses, locking or unlocking, and no taps except with `--tap`. For each gesture
it times, from the moment the request is sent, WebDriverAgent's reply and the first frame that differs from the
frame before (more than 5 % of the screen below the status bar), then prints the median, minimum and maximum of
both and the frame interval. It reuses the window's session, so the window can stay open:

```
python3 tests/latency_probe.py --i-am-not-using-the-phone [--swipes 4] [--ms 250] [--moves N | --window-drag | --tap [N]]
                               [--settings '{"snapshotMaxDepth": 50}'] [--json results.json]
                               [--video mjpeg | --video usb [--capture-socket PATH]]
```

`--moves N` sends each swipe with N moves after the touch-down point (default 1; each further one costs two
accessibility snapshots); `--window-drag` sends what the window would for a mouse drag of `--ms`. `--tap [N]` taps
the home screen's Search pill instead, N times (default 4, at most 20): the pill is looked up once in the
accessibility tree (at `snapshotMaxDepth` 50, then the session's depth is put back) and must have the pill's place
and shape, the screen must still be the first home page before every tap (checked with GdkPixbuf, so `--tap`
refuses without it), and Home closes Spotlight after each one; a phone in another language has no "Search" pill
and is refused. `--video usb` times the first changed frame on the USB capture's socket (`--capture-socket`,
default `$IPHONE_CAPTURE_SOCKET`, with no fallback path, so it never takes a capture by accident): the H.264 is
only recorded during the run and decoded afterwards (GStreamer `h264parse`, `avdec_h264`), and WebDriverAgent's
MJPEG stream is still read, and timed for comparison, because its touches are slower while nobody reads it. The
capture serves up to four clients, so the window can keep its picture. `--settings` applies WebDriverAgent
settings for the run, as a JSON object, and puts the old values back at the end, also after an error; it refuses
`mjpegScalingFactor` below 100. Compare one change at a time, with the same `--ms` and the same home screen: the
snapshot cost grows with what is on screen. `--tap` and `--video usb` have run against the simulation only so far;
the October 2026 tap and USB numbers in the docs were measured with the scripts they were built from.
Exit status: 0 measured, 1 a gesture did not register, the phone did not come back to the first page or the USB
capture was lost, 2 refused (no flag, bad options or `--settings`, `--tap` without GdkPixbuf, `--video usb` without
a socket), 3 not ready (WebDriverAgent, the stream or the capture unreachable, the phone locked or in landscape, no
Search pill). The MJPEG frame timing needs PyGObject's GdkPixbuf to decode the JPEGs; without it only the replies
are timed.

**CI** (`.github/workflows/ci.yml`) runs on every push and pull request: ShellCheck, `bash -n` and a Python 3.9
compile of every file, and of the capture, video and Bluetooth modules by name (so a renamed one fails); the suite
on Python 3.9-3.13 (x86_64, aarch64, Ubuntu 22.04, and once with `IPHONE_SLOW_TESTS=1`), where PyGObject is
missing and the window, video and Bluetooth tests skip themselves; the whole suite under Xvfb with
`IPHONE_TEST_WINDOW=1` on the system Python, with the Debian/Ubuntu packages plus `gstreamer1.0-plugins-bad`,
`gstreamer1.0-libav` and `dbus`, after checking that the H.264 and RTP elements are there (`appsrc`, `h264parse`,
`avdec_h264`, `udpsrc`, `rtph264depay`); and, per distro (Debian 12 and 13, Ubuntu 22.04 and 24.04, Fedora,
openSUSE Tumbleweed, Arch, some on aarch64), installs exactly what `./setup.sh --check` asks for and runs the suite
on that distro's Python. The distro jobs do not install the optional lines (H.264, BlueZ, usbmuxd), so there the
tests that need them skip themselves.

Changes that only a real phone can show (gestures, keys, orientation, tunnel and signing errors, the USB capture,
Bluetooth pairing and AssistiveTouch) should say in the pull request what was tried on which device and iOS
version.

## Before publishing

The privacy test scans the working tree only. Git history and commit metadata are published too, and an early
commit can still hold a hostname, an email or a path that the current files no longer have. Check all three
before the first push (and before any force-push of rewritten history).

1. **Who the commits say you are.** Every address here becomes public:

   ```
   git log --all --format='%an <%ae> | %cn <%ce>' | sort -u
   ```

2. **What the history contains.** Put your hostname, user name, real name and anything else personal in
   `words`, then search every diff and commit message:

   ```
   words="$(hostname -s)|$(id -un)|your-real-name"
   git log --all -p --format='%H %an %ae %cn %ce%n%B' | grep -n -i -E "$words"
   git log --all -p | grep -o -E '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' | sort -u
   git log --all -p | grep -o -E 'xctrunner\.[A-Z0-9]{10}|\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b' | sort -u
   ```

   The first must print nothing. The second may show only `noreply` and `example.com` addresses. The third must
   print nothing (team ids, UDIDs). 40-hex strings in the history are commit ids, which are fine.

3. **The tree.** `IPHONE_PRIVACY_WORDS="name1 name2" python3 -m unittest discover -s tests` passes. The test
   already adds this machine's hostname; the variable adds the other words from step 2.

If step 1 or 2 finds anything, rewrite the history **before** pushing; once it is pushed, treat it as public.
Set the identity to publish under first:

```
git config user.name  "Your Name"
git config user.email "<id>+<user>@users.noreply.github.com"
```

- **Squash to one fresh commit** (simplest while the history is short and unpublished; it commits the tree as
  it is now):

  ```
  git checkout --orphan fresh && git add -A && git commit -m "iPhone Control: mirror and control an iPhone over USB"
  git branch -M fresh main             # also delete other branches, tags and remotes that hold old commits
  git reflog expire --expire=now --all && git gc --prune=now      # drop the old commits locally too
  ```

- **Keep the commits** with `git filter-repo` (packaged as `git-filter-repo` on all four distro families).
  It resets the working tree, so commit or stash your work first. Write the old address and the words to
  replace into two files outside the repo, one entry per line:

  ```
  # mailmap:   Your Name <id+user@users.noreply.github.com> <old@example.com>
  # replace:   oldhost==>my-laptop
  #            regex:(?i)\boldhost\b==>my-laptop
  git filter-repo --force --mailmap ../mailmap --replace-text ../replace --replace-message ../replace
  ```

  `filter-repo` removes the `origin` remote on purpose; add it back after checking the result with steps 1-3.

Then run steps 1-3 again; push only when all three are clean.
