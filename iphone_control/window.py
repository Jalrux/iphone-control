"""The iPhone window: the phone's live picture, mouse/keyboard -> phone.

Input (IPHONE_MODE): full mode drives the phone through WebDriverAgent (WdaBackend: W3C touches, typing, buttons,
the clipboard); basic mode is a Bluetooth LE mouse and keyboard for AssistiveTouch (BleBackend, blehid.py) and
needs no Developer Mode. The picture (IPHONE_VIDEO) comes from video.py: WebDriverAgent's MJPEG stream, the USB
screen capture or AirPlay.

All GTK work runs on the main thread: the WDA worker, the capture reader and GStreamer's streaming thread hand
results over with GLib.idle_add. The pure maths is in geometry.py, the HTTP client in wda.py.
"""
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib  # noqa: E402

# Before GTK starts: X11 takes WM_CLASS from these and Wayland the app_id (window rules, .desktop match)
GLib.set_prgname("iphone-control")
GLib.set_application_name("iPhone Control")

from gi.repository import Gdk, Gst, Gtk  # noqa: E402

Gdk.set_program_class("iphone-control")

from . import geometry, video  # noqa: E402
from .video import GST_PACKAGES, LIBSOUP_HINT, fit_caps  # noqa: E402
from .video import GST_ELEMENTS, SCALE_THREADS, SCALER, scaler  # noqa: E402,F401  (were here before video.py)
from .wda import INPUT_SETTINGS, KEYS, NAMED_KEYS, WDA, WDAError  # noqa: E402


def _env_int(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


WDA_URL = os.environ.get("IPHONE_WDA_URL", "http://127.0.0.1:8100")
MJPEG = os.environ.get("IPHONE_MJPEG_URL", "http://127.0.0.1:9100")
# Measured on an iPhone 15 with iOS 26.5: any IPHONE_SCALE below 100 makes WebDriverAgent shrink and re-encode
# every frame itself (UIKit/CoreGraphics, at quality >= 90 whatever is set). That kept it near iOS's limit of
# 80% of a core over 60 s, and iOS killed it (cpu_resource_fatal) about every 5 minutes at 60 %. At 100 the
# system's JPEG passes through untouched: no kills in 20 minutes at 20 and 30 fps. Quality then sets the
# bandwidth: about 265 KB a frame at 60, 64 Mbit/s at 30 fps, which USB 2 carries easily.
MJPEG_SETTINGS = {
    "mjpegServerFramerate": _env_int("IPHONE_FPS", 30, 1, 60),
    "mjpegScalingFactor": _env_int("IPHONE_SCALE", 100, 10, 100),
    "mjpegServerScreenshotQuality": _env_int("IPHONE_QUALITY", 60, 10, 100),
    # turn frames upright when the app in front is landscape, so taps map to WDA's landscape points
    "mjpegFixOrientation": os.environ.get("IPHONE_FIX_ORIENTATION", "1") != "0",
}
# quick touches (see wda.INPUT_SETTINGS); IPHONE_FAST_INPUT=0 leaves WDA's own (slower) defaults alone
FAST_INPUT = os.environ.get("IPHONE_FAST_INPUT", "1") != "0"
WDA_SETTINGS = dict(MJPEG_SETTINGS, **(INPUT_SETTINGS if FAST_INPUT else {}))
# With the USB capture nobody sees WebDriverAgent's stream; it only has to run (video.MjpegDrain). Quality made
# no difference to touches (10 as fast as 60, iPhone 15 Pro, iOS 26.5.2, Oct 2026), so the least bandwidth.
HIDDEN_QUALITY = 10
CAPTURE_SOCKET = os.environ.get("IPHONE_CAPTURE_SOCKET", "")
USB_WAITING = "waiting for the USB picture…"
# Full mode: no capture socket for this long before the first USB picture (the capture stopped while iphone.sh
# started WebDriverAgent, and its watchdog could not bring it back): WebDriverAgent's stream instead, for the run
NO_CAPTURE_S = 15
# 0: a click taps at once (see WdaBackend); above 0 a click waits up to this long for a double-click
DOUBLE_CLICK_MS = _env_int("IPHONE_DOUBLE_CLICK_MS", 0, 0, 1000)
HID_NAME = os.environ.get("IPHONE_HID_NAME") or None
BT_ADAPTER = f"hci{os.environ['BT_INDEX']}" if os.environ.get("BT_INDEX", "").isdigit() else None
AIRPLAY_NAME = os.environ.get("IPHONE_AIRPLAY_NAME") or socket.gethostname().split(".")[0] or "iPhone Control"
_PICTURES = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~/Pictures")
SHOTS = os.path.expanduser(os.environ.get("IPHONE_SCREENSHOT_DIR") or os.path.join(_PICTURES, "iPhone"))
STATE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "iphone-control")
# iphone.sh's $RUN: it finds uxplay.pid there
RUN = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or os.path.join(STATE, "run"), "iphone-control")
CONFIG = os.path.join(STATE, "config.json")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
RESIZE_MS = 100          # a new picture size is passed to the scaler after the window stopped changing this long

HELP = """Mouse: click = tap · double-click = double tap · right-click or hold = long press
drag = swipe (hold still first to drag an icon) · wheel / two fingers = scroll
Ctrl+wheel or touchpad pinch = zoom · back button or Alt+Left = back

Keys: typing goes to the focused field (input methods and dead keys work)
Enter, Backspace, Tab, arrows, Delete, Home, End, Page Up/Down, Escape as on the phone
Shift+arrows, Shift+Tab: iOS 17 and later
Ctrl+V types the laptop clipboard · Ctrl+Shift+V copies it to the iPhone clipboard
Ctrl+Shift+C copies the iPhone clipboard to the laptop
Ctrl+L open a URL · Ctrl+O open an app (bundle id, remembers the last 8)
Ctrl+Q or Ctrl+W close

F1 Home · F2 App switcher · F3/F4 volume − / + · F5 Control Centre
F6 lock / unlock · F7 notifications · F8 Action button
F12 or Ctrl+S screenshot, saved in {shots}"""

HELP_BASIC = """Basic mode: the laptop is a Bluetooth mouse and keyboard (AssistiveTouch on)
Mouse: click = tap · double-click = double tap · right-click or hold = long press
drag = swipe, live (hold still first to drag an icon) · wheel / two fingers = scroll
back button or Alt+Left = back

Keys: typing goes to the focused field (what a US keyboard can type)
Enter, Backspace, Tab, arrows, Delete, Home, End, Page Up/Down, Escape as on the phone
Ctrl+V types the laptop clipboard · Ctrl+L open a URL (through Spotlight)
Ctrl+Q or Ctrl+W close
Zoom, the iPhone clipboard and opening apps need full mode (WebDriverAgent)

F1 Home · F2 App switcher · F3/F4 volume − / + · F5 Control Centre
F6 lock · F7 notifications
F12 or Ctrl+S screenshot of the picture, saved in {shots}"""

CSS = b"""
.iphone-osd { background-color: rgba(0, 0, 0, 0.7); color: #fff; border-radius: 8px; padding: 6px 10px; }
.iphone-bar button { padding: 4px; min-width: 0; min-height: 0; }
"""

DOUBLE_PRESS = getattr(Gdk.EventType, "DOUBLE_BUTTON_PRESS", None) or Gdk.EventType._2BUTTON_PRESS
SUPER = Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.MOD4_MASK | Gdk.ModifierType.HYPER_MASK
SCROLL_STEPS = {Gdk.ScrollDirection.UP: (0, -1), Gdk.ScrollDirection.DOWN: (0, 1),
                Gdk.ScrollDirection.LEFT: (-1, 0), Gdk.ScrollDirection.RIGHT: (1, 0)}
TOUCHPAD_PX = 10         # GTK: a smooth-scroll delta of 1.0 from a touchpad is about 10 px of finger travel
LONG_HOLD_MS = 800       # a right-click's long press; in basic mode a held button is down at least this long
# "host:port..." with a dotted host or localhost has no scheme ("tel:123" and "maps:..." do)
HOST_PORT = re.compile(r"^(localhost|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+):\d+(?:[/?#]|$)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# Basic mode. GDK key name -> HID keyboard usage: the keys of wda.KEYS and wda.NAMED_KEYS (ISO_Left_Tab is
# Shift+Tab; the Bluetooth keyboard holds Shift in the same report, so Shift+arrows select too).
HID_KEYS = {
    "Return": 0x28, "KP_Enter": 0x28, "ISO_Enter": 0x28, "Escape": 0x29, "BackSpace": 0x2A,
    "Tab": 0x2B, "ISO_Left_Tab": 0x2B, "Home": 0x4A, "KP_Home": 0x4A, "Page_Up": 0x4B, "KP_Page_Up": 0x4B,
    "Delete": 0x4C, "KP_Delete": 0x4C, "End": 0x4D, "KP_End": 0x4D, "Page_Down": 0x4E, "KP_Page_Down": 0x4E,
    "Right": 0x4F, "KP_Right": 0x4F, "Left": 0x50, "KP_Left": 0x50, "Down": 0x51, "KP_Down": 0x51,
    "Up": 0x52, "KP_Up": 0x52,
}
HID_SHIFT = 0x02         # left Shift in the keyboard report's modifier byte
RETURN = 0x28
VOLUME_UP, VOLUME_DOWN, POWER = 0xE9, 0xEA, 0x30      # consumer usages (power = lock; not tried on a phone yet)
PRESS_SETTLE_MS = 60     # blehid moves the pointer, then waits this long before a press lands
# Picture pixels per point in basic mode, which has no WebDriverAgent to ask the size: every iPhone since the X
# draws 3 pixels a point (1179 x 2556 = 393 x 852 on an iPhone 15 Pro). A picture narrower than MIN_WIDTH_PT
# points (the narrowest iPhone) at that rate is a scaled-down one, e.g. some AirPlay streams.
PX_PER_PT = 3
MIN_WIDTH_PT = 320
SPOTLIGHT_MS = 600       # Spotlight opening before its field takes typing (a guess, not measured)
RESULTS_MS = 500         # typed text to Return, so the top hit is the URL (a guess, not measured)
KEY_MS = 16              # one typed character: two reports, one per 8 ms tick
NOT_CONNECTED = -1       # BleBackend.type: nothing typed, the iPhone is not connected over Bluetooth


def help_text(basic=False):
    home = os.path.expanduser("~")
    shots = "~" + SHOTS[len(home):] if home != "/" and SHOTS.startswith(home + os.sep) else SHOTS
    return (HELP_BASIC if basic else HELP).format(shots=shots)


def with_scheme(url):
    """What Ctrl+L opens: url itself when it has a scheme, else https://url."""
    return url if SCHEME.match(url) and not HOST_PORT.match(url) else "https://" + url


def save_png(png, folder):
    """Write png into folder, private (0700 folder, 0600 file) and never overwriting; returns the path."""
    if not png or png[:8] != PNG_MAGIC:
        raise ValueError("WebDriverAgent did not send a PNG")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    stamp = time.strftime("iPhone_%Y-%m-%d_%H-%M-%S")
    for n in range(1, 1000):
        path = os.path.join(folder, f"{stamp}_{n}.png" if n > 1 else f"{stamp}.png")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(png)
        except OSError:
            os.unlink(path)
            raise
        return path
    raise FileExistsError(f"too many screenshots in one second in {folder}")


def load_config():
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def update_config(**changes):
    """Merge into config.json (shared with resign.sh) and replace it atomically."""
    cfg = load_config()
    cfg.update(changes)
    os.makedirs(STATE, exist_ok=True)
    tmp = f"{CONFIG}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


def wda_settings(kind):
    """WDA_SETTINGS for the picture kind: under the USB capture the stream runs unseen, at HIDDEN_QUALITY unless
    IPHONE_QUALITY says otherwise (its frame rate stays: the touches need it)."""
    if kind == "usb" and not os.environ.get("IPHONE_QUALITY", "").strip():
        return dict(WDA_SETTINGS, mjpegServerScreenshotQuality=HIDDEN_QUALITY)
    return dict(WDA_SETTINGS)


def estimated_size(video_size):
    """Basic mode's guess at the screen in points, from the picture (see PX_PER_PT). Only gesture thresholds and
    lengths use it (tap slop, scroll distance, edge margins), so a wrong guess makes them a little longer or
    shorter; where a click lands is exact, as it is sent in fractions of the picture."""
    w, h = video_size
    px = min(PX_PER_PT, min(w, h) / MIN_WIDTH_PT)
    return w / px, h / px


def _once(fn, *args):
    """fn(*args) as a one-shot GLib timeout."""
    fn(*args)
    return False


def key_route(name, shift):
    """How full mode sends a key of wda.KEYS or wda.NAMED_KEYS (as WDA.key does): "text" (/wda/keys), "hid" (one HID
    event: a named key without Shift) or "keyboardInput" (XCTest's hardware keys, iOS 17 and later: Shift held,
    which a HID event cannot carry)."""
    if name in KEYS:
        return "text"
    return "keyboardInput" if shift or NAMED_KEYS[name].usage is None else "hid"


def pointer_steps(w3c, step_ms=16):
    """A one-finger W3C gesture (geometry.py) -> [(ms, "press" | "drag" | "release", x, y), ...] for a pointer:
    moves while the finger is down become drags in steps of about step_ms, pauses only time."""
    t, x, y, down, out = 0, 0, 0, False, []
    for a in w3c["actions"][0]["actions"]:
        if a["type"] == "pointerMove":
            ms = a.get("duration", 0)
            if down:
                n = max(1, round(ms / step_ms))
                out += [(t + ms * i / n, "drag", x + (a["x"] - x) * i / n, y + (a["y"] - y) * i / n)
                        for i in range(1, n + 1)]
            t, x, y = t + ms, a["x"], a["y"]
        elif a["type"] == "pause":
            t += a["duration"]
        elif a["type"] in ("pointerDown", "pointerUp"):
            down = a["type"] == "pointerDown"
            out.append((t, "press" if down else "release", x, y))
    return out


# ---------------------------------------------------------------- input backends
class Backend:
    """What the window drives, in the phone's points as the picture shows them (geometry.oriented). Mouse button 1
    arrives as down(x, y), move(x, y), up(point or None); each backend decides what reaches the phone when."""
    features = frozenset()    # of "clipboard", "apps" (by bundle id), "pinch", "action" (the Action button)
    clock = staticmethod(time.monotonic)

    def __init__(self):
        self.video_size = None

    @property
    def size(self):
        return None

    def current_size(self):
        return geometry.oriented(self.size, self.video_size) if self.size else None

    def video_changed(self, size):
        self.video_size = size

    def double(self):
        """GTK saw a double-click (its second press)."""

    def flush(self):
        """Send anything held back now: what the window sends next must come after it."""

    def close(self):
        pass


class WdaBackend(Backend):
    """Full mode: WebDriverAgent (wda.py) runs W3C touches, one request at a time, so a mouse drag is replayed in
    one go on release (geometry.drag_to_gesture) and the rest maps to its HTTP API.

    GTK sends a double-click as PRESS, RELEASE, PRESS, DOUBLE_PRESS, RELEASE. With double_click_ms 0 (the default)
    every click taps at its release: two tap requests back to back land ~330 ms apart, which iOS reads as a double
    tap (10/10 in a text field, iPhone 15 Pro, iOS 26.5.2, Oct 2026); a click then a drag is a tap, then the
    drag. Above 0, a click's tap waits that long (at most GTK's double-click time) for a second click, and the pair
    becomes one geometry.double_tap() request, which also selects a word.
    """
    features = frozenset({"clipboard", "apps", "pinch", "action"})

    def __init__(self, phone, double_click_ms=None, later=GLib.timeout_add, cancel=GLib.source_remove):
        super().__init__()
        self.phone = phone
        self.double_click_ms = DOUBLE_CLICK_MS if double_click_ms is None else double_click_ms
        self.later, self.cancel = later, cancel
        self.track = None                 # mouse drag in progress: [(x, y, seconds), ...] in points
        self.first = None                 # second click of a double-click: the first click's point
        self._tap_at, self._tap_id = None, 0   # a click's tap held back for a double-click, and its timer

    @property
    def size(self):
        return self.phone.size

    def video_changed(self, size):
        old, self.video_size = self.video_size, size
        if old and (old[0] > old[1]) != (size[0] > size[1]):
            self.phone.refresh_size()     # WDA's size swaps with the app's orientation

    # ---- mouse button 1 --------------------------------------------------
    def down(self, x, y):
        self.track = [(x, y, self.clock())]

    def move(self, x, y):
        if self.track and abs(x - self.track[-1][0]) + abs(y - self.track[-1][1]) > 6:
            self.track.append((x, y, self.clock()))

    def double(self):
        if self.track:                                       # its second press is down on the picture
            self.first = self._take_tap()                    # None: the first tap has gone already

    def up(self, p):
        """Button 1 released at p (None: outside the picture)."""
        if not self.track:
            return
        track, first, self.track, self.first = self.track, self.first, None, None
        if p:
            track.append((p[0], p[1], self.clock()))
        x0, y0, t0 = track[0]
        still = max(abs(x - x0) + abs(y - y0) for x, y, _ in track) < geometry.TAP_MAX_MOVE
        if first:                                            # second click of a double-click
            if still:
                return self.phone.actions(geometry.double_tap(*first))
            self.phone.actions(geometry.tap(*first))        # click, then drag: the tap goes first
        else:
            self.flush()                                     # an earlier click that did not pair up
        held_ms = (track[-1][2] - t0) * 1000
        wait = self._double_wait(held_ms) if still and held_ms <= geometry.LONG_PRESS_MS else 0
        if wait > 0:
            self._tap_at = (x0, y0)
            self._tap_id = self.later(wait, self._tap_due)
        else:
            self.phone.actions(geometry.drag_to_gesture(track, self.current_size()))   # size: the home indicator

    def _double_wait(self, held_ms):
        """How long a click's tap waits for a second click (ms): 0, or until the double-click time is up."""
        if not self.double_click_ms:
            return 0
        return int(max(0, min(self.double_click_time(), self.double_click_ms) - held_ms))

    @staticmethod
    def double_click_time():
        """GTK's double-click time (ms): a second click after it is no double-click for GTK either."""
        s = Gtk.Settings.get_default()
        return s.props.gtk_double_click_time if s else 400

    def _take_tap(self):
        """Cancel the waiting tap and return its point (None if there is none)."""
        if self._tap_id:
            self.cancel(self._tap_id)
            self._tap_id = 0
        at, self._tap_at = self._tap_at, None
        return at

    def _tap_due(self):
        self._tap_id = 0
        self.flush()
        return False

    def flush(self):
        at = self._take_tap()
        if at:
            self.phone.actions(geometry.tap(*at))

    def close(self):
        self._take_tap()

    # ---- gestures ----------------------------------------------------------
    def long_press(self, x, y):
        self.phone.actions(geometry.tap(x, y, hold_ms=LONG_HOLD_MS))

    def scroll(self, x, y, dx, dy, size, done):
        self.phone.actions(geometry.scroll(x, y, dy, size, dx=dx, per_unit=1), done=lambda _r: done())

    def pinch(self, x, y, scale, size, done=None):
        self.phone.actions(geometry.pinch(x, y, scale, size), done=done and (lambda _r: done()))

    def back(self, size):
        self.phone.actions(geometry.back_swipe(size))

    def system(self, kind, size):         # app_switcher, control_centre, notifications: edge swipes
        if size:
            self.phone.actions(geometry.edge_swipe(kind, size))

    # ---- buttons, keys, the rest -------------------------------------------
    def home(self):
        self.phone.home()

    def volume(self, up):
        self.phone.button("volumeUp" if up else "volumeDown")

    def lock(self):
        self.phone.toggle_lock()

    def action(self):
        self.phone.button("action")

    def type(self, text):
        """Returns how many characters could not be typed: none."""
        self.phone.type(text)
        return 0

    def paste(self, text):
        self.phone.type(text, fast=True)                     # at wda.TYPE_RATE_FAST: it need not look typed
        return 0

    def key(self, name, shift=False, fail=None):
        if name in KEYS:
            self.phone.type(KEYS[name])
        elif name in NAMED_KEYS:
            self.phone.key(NAMED_KEYS[name], shift=shift, fail=fail)

    def open_url(self, url):
        self.phone.open_url(url)
        return 0

    def launch(self, bundle):
        self.phone.launch(bundle)

    def screenshot(self, done):
        """done(png_or_None) on the worker thread; True: it was asked for."""
        self.phone.screenshot(done)
        return True

    def set_clipboard(self, text, done):
        self.phone.set_pasteboard(text, done)

    def get_clipboard(self, done):
        self.phone.get_pasteboard(done)

    def alive(self, timeout):
        self.phone.status(timeout=timeout)

    def stream_shown(self):
        """The window shows WebDriverAgent's stream after all (no USB capture): its quality as for mjpeg, not
        HIDDEN_QUALITY, now and for any session WebDriverAgent reopens."""
        key = "mjpegServerScreenshotQuality"
        want, settings = wda_settings("mjpeg")[key], self.phone.settings
        if settings and settings.get(key) != want:
            settings[key] = want
            self.phone.send("POST", "/session/{sid}/appium/settings", {"settings": {key: want}})


class BleBackend(Backend):
    """Basic mode: the laptop as a Bluetooth LE mouse and keyboard (blehid.py) for AssistiveTouch.

    The pointer is placed absolutely, in fractions of the picture, so a click lands exactly; the size in points is
    only estimated (estimated_size), for thresholds. Nothing waits: a click taps when it is released, so a
    double-click is two taps. A drag is live: the finger goes down where it started once it moved TAP_MAX_MOVE and
    follows every motion event, with no replay. A button held still for LONG_PRESS_MS presses there and then and
    stays down at least LONG_HOLD_MS (a long press; moving on drags what it holds). Scrolling and back are short
    pointer drags (geometry's gestures played through the pointer), the system gestures blehid's key combinations.
    There is one pointer: a new gesture first ends what an earlier one still has on a timer (_finish).
    """

    def __init__(self, hid=None, later=GLib.timeout_add, cancel=GLib.source_remove):
        super().__init__()
        self.hid = hid                    # a blehid.BleHid (set by main once the window exists)
        self.later, self.cancel = later, cancel
        self._start = self._last = None   # button 1: where it went down, where it is
        self._pressed_at = None           # the phone's finger is down since (seconds)
        self._held = self._moved = False
        self._hold_id = 0
        self._release_id, self._release_at = 0, None    # a long press's release, held back until LONG_HOLD_MS
        self._play_id = 0                 # the next step of a gesture played through the pointer (play)
        self._play_down = self._play_done = None        # ...where its finger is down (None: up), and its done()

    @property
    def size(self):
        return estimated_size(self.video_size) if self.video_size else None

    def _f(self, x, y):
        w, h = self.current_size()
        return min(max(x / w, 0.0), 1.0), min(max(y / h, 0.0), 1.0)

    # ---- mouse button 1 --------------------------------------------------
    def down(self, x, y):
        self._stop_hold()
        self._finish()
        self._start = self._last = (x, y)
        self._pressed_at, self._held, self._moved = None, False, False
        self._hold_id = self.later(geometry.LONG_PRESS_MS, self._hold)

    def _hold(self):
        self._hold_id = 0
        if self._start and self._pressed_at is None:
            self._press(*self._start)
            self._held = True
        return False

    def _stop_hold(self):
        if self._hold_id:
            self.cancel(self._hold_id)
            self._hold_id = 0

    def _finish(self):
        """What an earlier gesture still has on a timer ends now, where its finger is: a long press's held-back
        release, or the rest of a scroll or back swipe (whose done() then runs). Left to run, its steps would act
        on the next gesture's finger: drag it to their points and lift it there."""
        if self._release_id:
            self.cancel(self._release_id)
            self._release_due()
        if self._play_id:
            self.cancel(self._play_id)
            self._play_id = 0
            down, done, self._play_down, self._play_done = self._play_down, self._play_done, None, None
            if down:
                self.hid.release(*down)
            if done:
                done()

    def _release_due(self):
        self._release_id = 0
        self.hid.release(*self._release_at)
        return False

    def _press(self, x, y):
        self.hid.press(*self._f(x, y))
        self._pressed_at = self.clock()

    def move(self, x, y):
        if not self._start:
            return
        if self._pressed_at is None:
            if abs(x - self._start[0]) + abs(y - self._start[1]) < geometry.TAP_MAX_MOVE:
                return
            self._stop_hold()
            self._press(*self._start)
        self._last, self._moved = (x, y), True
        self.hid.drag_to(*self._f(x, y))

    def up(self, p):
        if not self._start:
            return
        self._stop_hold()
        start, at, self._start = self._start, p or self._last, None
        if self._pressed_at is None:
            return self.hid.tap(*self._f(*start))
        f = self._f(*at)
        keep = LONG_HOLD_MS - (self.clock() - self._pressed_at) * 1000 if self._held and not self._moved else 0
        self._pressed_at = None
        if keep > 0:
            self._release_at = f
            self._release_id = self.later(int(keep), self._release_due)
        else:
            self.hid.release(*f)

    def close(self):
        self._stop_hold()
        self._play_done = None            # the window is closing: nothing waits for a scroll any more
        self._finish()
        if self._pressed_at is not None and self._last:
            self.hid.release(*self._f(*self._last))
        if self.hid:
            self.hid.stop()

    # ---- gestures ----------------------------------------------------------
    def long_press(self, x, y):
        self._finish()
        self.hid.tap(*self._f(x, y), hold_ms=LONG_HOLD_MS)

    def play(self, w3c, done=None):
        """A one-finger gesture from geometry.py through the pointer, at its own times (after the press has
        settled); done() on the main loop after the release, or when the next gesture cuts it short. Nothing is
        played while button 1 has the pointer (done() at once)."""
        self._finish()
        if self._start:
            if done:
                done()
            return
        steps = pointer_steps(w3c)
        times = [ms + (PRESS_SETTLE_MS if i else 0) for i, (ms, _op, _x, _y) in enumerate(steps)]
        self._play_done = done

        def step(i):
            self._play_id = 0
            _ms, op, x, y = steps[i]
            f = self._f(x, y)
            {"press": self.hid.press, "drag": self.hid.drag_to, "release": self.hid.release}[op](*f)
            self._play_down = None if op == "release" else f
            if i + 1 < len(steps):
                self._play_id = self.later(max(1, int(times[i + 1] - times[i])), lambda: step(i + 1))
            else:
                end, self._play_done = self._play_done, None
                if end:
                    end()
            return False
        step(0)

    def scroll(self, x, y, dx, dy, size, done):
        # the mouse wheel's own report does not scroll iOS: a short drag like WebDriverAgent's (no fling)
        self.play(geometry.scroll(x, y, dy, size, dx=dx, per_unit=1), done)

    def back(self, size):
        self.play(geometry.back_swipe(size))

    def system(self, kind, size):
        {"app_switcher": self.hid.app_switcher, "control_centre": self.hid.control_centre,
         "notifications": self.hid.notifications}[kind]()

    # ---- buttons, keys, the rest -------------------------------------------
    def home(self):
        self.hid.home()

    def volume(self, up):
        self.hid.consumer(VOLUME_UP if up else VOLUME_DOWN)

    def lock(self):
        self.hid.consumer(POWER)

    def type(self, text):
        """Returns how many characters could not be typed (a US keyboard's characters only), or NOT_CONNECTED:
        then blehid typed nothing."""
        if not self.hid.connected:
            return NOT_CONNECTED
        try:
            n = self.hid.text(text)
        except OSError:                   # blehid.BleError: it disconnected
            return NOT_CONNECTED
        return NOT_CONNECTED if n is None or n < 0 else n

    paste = type

    def key(self, name, shift=False, fail=None):
        if name in HID_KEYS:
            self.hid.key(HID_KEYS[name], HID_SHIFT if shift else 0)

    def open_url(self, url):
        """Through Spotlight, which opens a typed web address or app link on Return; returns 0 (typed later)."""
        self.hid.spotlight()
        self.later(SPOTLIGHT_MS, lambda: _once(self.hid.text, url))
        self.later(SPOTLIGHT_MS + len(url) * KEY_MS + RESULTS_MS, lambda: _once(self.hid.key, RETURN))
        return 0

    def screenshot(self, done):
        return False                                         # no WebDriverAgent: the window saves the picture


# ---------------------------------------------------------------- the window
def video_source(kind, on_lost, on_status):
    """The picture for IPHONE_VIDEO kind (usb, mjpeg or airplay)."""
    if kind == "usb":
        return video.H264Source(CAPTURE_SOCKET, on_lost, on_status)
    if kind == "airplay":
        os.makedirs(RUN, mode=0o700, exist_ok=True)
        return video.AirplaySource(AIRPLAY_NAME, on_lost, on_status, log_path=os.path.join(RUN, "uxplay.log"),
                                   pid_path=os.path.join(RUN, "uxplay.pid"))
    return video.MjpegSource(MJPEG, on_lost, on_status)


class Mirror(Gtk.Window):
    def __init__(self, backend, kind="mjpeg"):
        super().__init__(title="iPhone")
        self.input = WdaBackend(backend) if isinstance(backend, WDA) else backend
        self.kind = kind
        self.video_size = None            # current frame size; landscape when the app in front is
        self.dragging = False             # button 1 is down on the picture
        self.scroll_acc = [0.0, 0.0]      # points of scrolling not sent yet (x, y)
        self.scroll_at = None
        self.scroll_busy = False          # one scroll swipe in flight; the rest is merged meanwhile
        self.pinch_acc = 0.0              # Ctrl+wheel notches not sent yet
        self.pinch_at = None
        self.pinch_busy = False           # one Ctrl+wheel pinch in flight; further notches are merged meanwhile
        self.pinch_point = None
        self.keys_off = set()             # key_route()s WebDriverAgent refused ("unsupported"): dropped from then on
        self._state_text = self._waiting_text()      # persistent status; None = all fine
        self._input_text = None           # the Bluetooth state (basic mode), shown above it
        self._source_text = None          # the picture source's own (the USB capture's STATUS), between them
        self._flash_id = self._retry_id = 0
        self._backoff, self._fails, self._probing = 500, 0, False
        self._no_socket_since = None      # the USB capture's socket is missing since (monotonic seconds)
        self._last_log = 0.0
        self._pair = None                 # the pairing question while it is open
        self._closed = False
        self.set_icon_name("phone")

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.picture = Gtk.EventBox()
        self._fit_id = 0
        self._use_video(kind)
        self.drain = None                 # WebDriverAgent's stream, kept running unseen under the USB capture
        if kind == "usb" and isinstance(self.input, WdaBackend):
            # its trouble goes to the log only: a WebDriverAgent outage shows in the input anyway
            self.drain = video.MjpegDrain(MJPEG, lambda m: print(m, flush=True))

        mask = (Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        if hasattr(Gdk.EventMask, "TOUCHPAD_GESTURE_MASK"):
            mask |= Gdk.EventMask.TOUCHPAD_GESTURE_MASK
        self.picture.add_events(mask)
        self.picture.connect("button-press-event", self.on_press)
        self.picture.connect("motion-notify-event", self.on_motion)
        self.picture.connect("button-release-event", self.on_release)
        self.picture.connect("scroll-event", self.on_scroll)
        self.picture.connect("event", self.on_gesture)

        # status bubble at the bottom of the picture; clicks and scrolls pass through it
        self.status = Gtk.Label(label=self._state_text)
        self.status.set_halign(Gtk.Align.CENTER)
        self.status.set_valign(Gtk.Align.END)
        self.status.set_margin_bottom(12)
        self.status.set_margin_start(8)
        self.status.set_margin_end(8)
        self.status.set_line_wrap(True)
        self.status.set_justify(Gtk.Justification.CENTER)
        self.status.get_style_context().add_class("iphone-osd")
        self.status.set_no_show_all(True)
        self.status.show()
        overlay = Gtk.Overlay()
        overlay.add(self.picture)
        overlay.add_overlay(self.status)
        overlay.set_overlay_pass_through(self.status, True)

        self.bar = self.toolbar()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(overlay, True, True, 0)
        box.pack_start(self.bar, False, False, 0)
        self.add(box)

        # input methods (dead keys, compose, IBus/Fcitx); the IM shows its own preedit window
        self.im = Gtk.IMMulticontext()
        self.im.set_use_preedit(False)
        self.im.connect("commit", lambda _im, text: (self.flush_tap(), self._type(text)))
        self.connect("realize", lambda w: self.im.set_client_window(w.get_window()))
        self.connect("unrealize", lambda _w: self.im.set_client_window(None))
        self.connect("focus-in-event", lambda *_: self.im.focus_in())
        self.connect("focus-out-event", lambda *_: self.im.focus_out())
        self.connect("key-press-event", self.on_key)
        self.connect("key-release-event", lambda _w, ev: self.im.filter_keypress(ev))
        self.connect("destroy", self.quit)

        self._bar_h = max(self.bar.get_preferred_height()[1], 24)
        size = self.input.size
        portrait = geometry.oriented(size, (1, 2)) if size else (393, 852)
        self._aspect = portrait[0] / portrait[1]
        self.set_default_size(*self._default_size(self._aspect))
        self._hints(self._aspect)
        self.video.start()
        if self.drain:
            self.drain.start()

    @property
    def basic(self):
        return isinstance(self.input, BleBackend)

    def _waiting_text(self):
        if self.kind == "airplay":
            return f"On the iPhone: Control Centre > Screen Mirroring > {AIRPLAY_NAME}"
        if self.kind == "usb" and not _is_socket(CAPTURE_SOCKET):
            return USB_WAITING
        return "connecting to the iPhone…"

    # ---- window shape --------------------------------------------------
    def _workarea(self):
        d = Gdk.Display.get_default()
        m = d.get_primary_monitor() or d.get_monitor(0)       # primary is often None on Wayland
        return m.get_workarea() if m else None

    def _default_size(self, aspect):
        a = self._workarea()
        if not a:
            return 390, 860
        return geometry.window_size(a.width, a.height, aspect, self._bar_h)

    def _hints(self, aspect):
        """Minimum size; on X11 also the picture's aspect, which window managers keep while resizing.

        Not on Wayland: GTK would shrink its own buffer to the aspect inside the size the compositor
        chose (a window rule or a tile), leaving a gap. The letterbox maths copes with any shape.
        """
        g = Gdk.Geometry()
        g.min_width, g.min_height = 180, 200
        hints = Gdk.WindowHints.MIN_SIZE
        if Gdk.Display.get_default().__gtype__.name.startswith("GdkX11"):
            g.base_width, g.base_height = 0, self._bar_h    # the aspect applies to the picture only
            g.min_aspect = g.max_aspect = aspect
            hints |= Gdk.WindowHints.BASE_SIZE | Gdk.WindowHints.ASPECT
        self.set_geometry_hints(None, g, hints)

    def _turn(self, aspect):
        """The picture changed orientation: turn the window with it, within the screen."""
        self._aspect = aspect
        self._hints(aspect)
        a = self.picture.get_allocation()
        if a.width < 50 or a.height < 50:                    # not laid out yet
            w, h = self._default_size(aspect)
            return self.resize(w, h)
        vw, vh = self.video_size
        _ox, _oy, s = geometry.letterbox(a.width, a.height, vh, vw)   # size of the old picture
        w, h = vw * s, vh * s
        area = self._workarea()
        if area:
            f = min(1, area.width * 0.9 / w, (area.height * 0.9 - self._bar_h) / h)
            w, h = w * f, h * f
        self.resize(max(int(w), 180), max(int(h), 1) + self._bar_h)

    # ---- toolbar -------------------------------------------------------
    def toolbar(self):
        bar = Gtk.Box(spacing=0)
        bar.set_halign(Gtk.Align.CENTER)
        bar.get_style_context().add_class("iphone-bar")
        theme = Gtk.IconTheme.get_default()
        for icons, tip, fn in [
            (("go-home-symbolic", "go-home"), "Home (F1)", self.input.home),
            (("view-app-grid-symbolic", "view-grid-symbolic"), "App switcher (F2)",
             lambda: self.edge("app_switcher")),
            (("preferences-system-symbolic", "emblem-system-symbolic"), "Control Centre (F5)",
             lambda: self.edge("control_centre")),
            (("preferences-system-notifications-symbolic", "mail-unread-symbolic"), "Notifications (F7)",
             lambda: self.edge("notifications")),
            (("camera-photo-symbolic", "camera-photo"), "Screenshot (F12)", self.screenshot),
            (("audio-volume-low-symbolic", "audio-volume-low"), "Volume down (F3)",
             lambda: self.input.volume(False)),
            (("audio-volume-high-symbolic", "audio-volume-high"), "Volume up (F4)",
             lambda: self.input.volume(True)),
            (("system-lock-screen-symbolic", "changes-prevent-symbolic"),
             "Lock (F6)" if self.basic else "Lock / unlock (F6)", self.input.lock),
            (("help-about-symbolic", "help-browser-symbolic", "dialog-question-symbolic"), "Help",
             None),
        ]:
            name = next((i for i in icons if theme.has_icon(i)), None)
            b = (Gtk.Button.new_from_icon_name(name, Gtk.IconSize.BUTTON) if name
                 else Gtk.Button(label=tip.split(" ")[0][:4]))
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.set_can_focus(False)               # keep keyboard focus for typing
            if fn is None:
                pop = Gtk.Popover()
                pop.set_relative_to(b)
                label = Gtk.Label(label=help_text(self.basic))
                for side in ("top", "bottom", "start", "end"):
                    getattr(label, f"set_margin_{side}")(10)
                pop.add(label)
                b.connect("clicked", lambda _b, p=pop: (p.show_all(), p.popup() if hasattr(p, "popup") else None))
            else:
                b.connect("clicked", lambda _b, f=fn: (self.flush_tap(), f()))
            bar.pack_start(b, False, False, 0)
        return bar

    # ---- status --------------------------------------------------------
    def _show(self, text):
        if text:
            self.status.set_text(text)
            self.status.show()
        else:
            self.status.hide()

    def _status_text(self):
        return "\n".join(t for t in (self._input_text, self._source_text, self._state_text) if t) or None

    def _refresh(self):
        if not self._flash_id:
            self._show(self._status_text())

    def set_status(self, text):
        """Persistent status of the picture (None = all fine); shown again when a flash ends."""
        self._state_text = text
        self._refresh()

    def set_input_status(self, text):
        """Persistent status of the input (basic mode: Bluetooth), shown above the picture's."""
        self._input_text = text
        self._refresh()

    def on_status(self, text):
        """The picture source's own status (the USB capture's STATUS frames, e.g. "unlock it"): persistent like
        set_status, until the source clears it with ""."""
        text = text or None
        if text == self._source_text:
            return
        if text:
            print(text, flush=True)
        self._source_text = text
        self._refresh()

    def flash(self, text, echo=True):
        """Show text for 2.5 s (main thread; from other threads use GLib.idle_add)."""
        if echo:
            print(text, flush=True)
        self._show(text)
        if self._flash_id:
            GLib.source_remove(self._flash_id)
        self._flash_id = GLib.timeout_add(2500, self._flash_over)
        return False

    def _flash_over(self):
        self._flash_id = 0
        self._show(self._status_text())
        return False

    def log_line(self, msg):
        """WDA's log (any thread): stdout, and on screen at most every 2 s."""
        print(msg, flush=True)
        GLib.idle_add(self._log_flash, msg)

    def _log_flash(self, msg):
        now = time.monotonic()
        if now - self._last_log >= 2:
            self._last_log = now
            self.flash(msg, echo=False)
        return False

    # ---- Bluetooth (basic mode) ------------------------------------------
    def hid_name(self):
        return (getattr(self.input.hid, "name", None) or HID_NAME
                or f"{socket.gethostname().split('.')[0] or 'Linux'} Mouse")

    def on_ble_state(self, state):
        """blehid's state (main loop): what to do on the phone while it is not connected."""
        if state == "advertising":
            self.set_input_status(f"On the iPhone: Settings > Bluetooth > {self.hid_name()} > Pair, "
                                  "then Accessibility > Touch > AssistiveTouch on")
        elif state == "connected":
            self.set_input_status(None)                       # blehid's log line says so
        elif state == "disconnected":
            self.set_input_status("the iPhone disconnected from Bluetooth - waiting for it…")

    def confirm_pairing(self, passkey, alias, reply):
        """blehid asks (main loop) whether the iPhone shows the same code; reply(bool) once, from a dialog that
        does not block the main loop (Bluetooth keeps running while it is open)."""
        if self._pair:
            self._pair.response(Gtk.ResponseType.NO)        # a newer request replaces it
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO, text=f"Pair with {alias}?")
        d.format_secondary_markup(f"Does the iPhone show this code?\n\n"
                                  f"<span size='xx-large' weight='bold'>{int(passkey):06d}</span>")
        answered = []

        def respond(dialog, response):
            if not answered:
                answered.append(response)
                reply(response == Gtk.ResponseType.YES)
            if self._pair is dialog:
                self._pair = None
            dialog.destroy()
        d.connect("response", respond)
        self._pair = d
        d.show_all()

    # ---- video ---------------------------------------------------------
    def _use_video(self, kind):
        """The picture for kind (usb, mjpeg or airplay) into the window, not started yet."""
        self.kind = kind
        self.video = video_source(kind, self.on_lost, self.on_status)
        self.pipeline = self.video.pipeline
        self.set_title(self.video.title)
        # the phone's own frame size (before scaling) decides orientation and the touch mapping
        self.pipeline.get_by_name("dec").get_static_pad("src").connect("notify::caps", self.on_caps)
        self.fit = self.video.fit         # None without videoscale
        self._box = self._alloc = None
        self.picture.add(self.video.widget)
        if self.fit:
            self.video.widget.connect("size-allocate", self._resized)

    def _show_wda_stream(self):
        """Full mode with no USB capture for NO_CAPTURE_S: WebDriverAgent's MJPEG stream is the picture for the
        rest of the run (it also keeps WebDriverAgent's capture running, so the hidden stream stops)."""
        self.picture.remove(self.video.widget)
        self.video.close()
        if self.drain:
            self.drain.stop()
            self.drain = None
        self.input.stream_shown()
        self._use_video("mjpeg")
        self.video.widget.show()
        self._source_text, self._fails, self._backoff = None, 0, 500
        self.set_status(self._waiting_text())
        self.flash(f"no USB picture for {NO_CAPTURE_S} s - showing WebDriverAgent's picture instead")
        self.video.start()

    def _capture_missing(self):
        """Seconds the USB capture's socket has been missing in a row (0 when it just went), or None while it is
        there."""
        if _is_socket(CAPTURE_SOCKET):
            self._no_socket_since = None
            return None
        now = time.monotonic()
        if self._no_socket_since is None:
            self._no_socket_since = now
        return now - self._no_socket_since

    def _resized(self, w, alloc):
        size = (alloc.width, alloc.height, w.get_scale_factor())
        if size == self._alloc:                       # gtksink re-allocates with every frame
            return
        self._alloc = size
        if self._fit_id:
            GLib.source_remove(self._fit_id)
        self._fit_id = GLib.timeout_add(RESIZE_MS, self._fit_video)

    def _fit_video(self):
        """Let the scaler shrink frames to the picture's size in device pixels (aspect kept, never enlarged)."""
        self._fit_id = 0
        w = self.video.widget
        a, f = w.get_allocation(), w.get_scale_factor()
        box = fit_caps(a.width * f, a.height * f)
        if box != self._box:
            self._box = box
            self.fit.set_property("caps", Gst.Caps.from_string(box))
        return False

    def on_caps(self, pad, _pspec):                  # GStreamer streaming thread
        caps = pad.get_current_caps()
        if caps:
            s = caps.get_structure(0)
            GLib.idle_add(self._video_up, s.get_value("width"), s.get_value("height"))

    def _video_up(self, w, h):
        self.video_size = (w, h)
        self._fails, self._backoff = 0, 500
        self.set_status(None)
        self.input.video_changed((w, h))              # WDA re-reads its size when the orientation changed
        if (w > h) != (self._aspect > 1):
            self._turn(w / h)
        return False

    def on_lost(self, why):
        """The picture failed or ended (WDA or the capture restarted, cable pulled): retry with backoff. A USB
        capture whose socket is not there (yet) is waited for the same way: iphone.sh's watchdog restarts it."""
        if self._retry_id or self._closed:
            return                                    # several elements report the same failure
        self.video.stop()
        self._fails += 1
        missing = self._capture_missing() if self.kind == "usb" else None
        if (missing is not None and missing >= NO_CAPTURE_S and not self.video_size
                and isinstance(self.input, WdaBackend)):
            print(f"video lost ({why})", flush=True)
            return self._show_wda_stream()
        if self._fails % 5 == 0 and not self._probing and self.kind == "mjpeg":
            self._probe()
        elif self.kind == "airplay":
            self.set_status(self._waiting_text())
        elif missing is not None:
            self.set_status(USB_WAITING)
        elif not (self._state_text or "").startswith("WebDriverAgent stopped"):
            self.set_status("video lost - reconnecting…")
        print(f"video lost ({why}); retrying in {self._backoff} ms", flush=True)
        self._retry_id = GLib.timeout_add(self._backoff, self._restart)
        self._backoff = min(self._backoff * 2, 5000)

    def _restart(self):
        self._retry_id = 0
        self.video.start()
        return False

    def _probe(self):
        """Is WebDriverAgent itself still there? Asked on a short-lived thread."""
        self._probing = True

        def run():
            try:
                self.input.alive(timeout=2)
                ok = True
            except Exception:
                ok = False
            GLib.idle_add(self._probed, ok)
        threading.Thread(target=run, daemon=True).start()

    def _probed(self, ok):
        self._probing = False
        if not ok:
            self.set_status("WebDriverAgent stopped - run ./iphone.sh again (or replug the iPhone)")
        return False

    def point(self, x, y):
        if not self.video_size or not self.input.size:
            return None
        a = self.picture.get_allocation()
        return geometry.to_phone(x, y, (a.width, a.height), self.video_size, self.input.size)

    def current_size(self):
        return self.input.current_size()

    def _px_per_pt(self):
        a = self.picture.get_allocation()
        vw, vh = self.video_size
        _ox, _oy, s = geometry.letterbox(max(a.width, 1), max(a.height, 1), vw, vh)
        return max(s * vw / self.current_size()[0], 0.01)

    # ---- mouse ---------------------------------------------------------
    # Button 1 goes to the backend as down / move / up (WdaBackend replays a drag on release, BleBackend follows
    # it live); GTK's DOUBLE_PRESS after the second press of a double-click is passed on as double().
    def on_press(self, _w, ev):
        if ev.type == DOUBLE_PRESS:
            if ev.button == 1 and self.dragging:             # its second press landed on the picture
                self.input.double()
            return True
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if ev.button != 1:
            self.flush_tap()
        if ev.button == 8:                                   # mouse "back" button
            return self.back()
        p = self.point(ev.x, ev.y)
        if not p:
            return False
        if ev.button == 3:                                   # right-click = long press
            self.input.long_press(*p)
        elif ev.button == 1:
            self.dragging = True
            self.input.down(*p)
        return True

    def on_motion(self, _w, ev):
        if self.dragging:
            p = self.point(ev.x, ev.y)
            if p:
                self.input.move(*p)
        return False

    def on_release(self, _w, ev):
        if ev.button != 1 or not self.dragging:
            return False
        self.dragging = False
        self.input.up(self.point(ev.x, ev.y))
        return True

    def flush_tap(self):
        """A click's tap held back for a double-click goes now: anything else sent to the phone comes after it."""
        self.input.flush()

    def on_scroll(self, _w, ev):
        self.flush_tap()
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            if ev.is_scroll_stop_event():
                return True
            _ok, dx, dy = ev.get_scroll_deltas()
        else:
            dx, dy = SCROLL_STEPS.get(ev.direction, (0, 0))
        if ev.state & Gdk.ModifierType.CONTROL_MASK:          # Ctrl+wheel = pinch zoom
            if "pinch" in self.input.features:
                self.pinch_acc += dy
                self.pinch_point = self.point(ev.x, ev.y) or self.pinch_point
                self._pump_pinch()
            return True
        p = self.point(ev.x, ev.y)
        if not p:
            return False
        dev = ev.get_source_device()
        if dev and dev.get_source() == Gdk.InputSource.TOUCHPAD:
            unit = TOUCHPAD_PX / self._px_per_pt()           # follow the fingers 1:1
        else:
            unit = geometry.SCROLL_PT
        self.scroll_acc[0] += dx * unit
        self.scroll_acc[1] += dy * unit
        self.scroll_at = p
        self._pump_scroll()
        return True

    def _pump_scroll(self):
        """Send what has built up as one swipe; while it runs, further scrolling is merged."""
        if self.scroll_busy or not self.scroll_at:
            return False
        ax, ay = self.scroll_acc
        lim = geometry.MIN_SCROLL_PT
        if abs(ax) < lim and abs(ay) < lim:
            return False
        w, h = self.current_size()
        sx = max(-w * 0.8, min(w * 0.8, ax)) if abs(ax) >= lim else 0
        sy = max(-h * 0.6, min(h * 0.6, ay)) if abs(ay) >= lim else 0
        # what one swipe cannot carry stays for the next; small jitter on the other axis is dropped
        self.scroll_acc = [ax - sx if sx else 0.0, ay - sy if sy else 0.0]
        self.scroll_busy = True
        self.input.scroll(*self.scroll_at, sx, sy, (w, h), done=lambda: GLib.idle_add(self._scroll_done))
        return False

    def _scroll_done(self):
        self.scroll_busy = False
        return self._pump_scroll()

    def _pump_pinch(self):
        """Send the notches built up as one pinch; while it runs (WDA does one thing at a time), further
        notches are merged instead of queueing a backlog of pinches."""
        if self.pinch_busy or abs(self.pinch_acc) < 1 or not self.pinch_point:
            return False
        scale, self.pinch_acc = 1.25 ** -self.pinch_acc, 0.0
        self.pinch_busy = True
        self.input.pinch(*self.pinch_point, scale, self.current_size(),
                         done=lambda: GLib.idle_add(self._pinch_done))
        return False

    def _pinch_done(self):
        self.pinch_busy = False
        return self._pump_pinch()

    def on_gesture(self, _w, ev):
        """Touchpad pinch -> two-finger pinch on the phone (sent when the fingers lift)."""
        if ev.type != Gdk.EventType.TOUCHPAD_PINCH:
            return False
        if "pinch" not in self.input.features:
            return True
        tp = ev.touchpad_pinch
        if tp.phase == Gdk.TouchpadGesturePhase.BEGIN:
            self.flush_tap()
            self.pinch_at = self.point(tp.x, tp.y)
        elif tp.phase == Gdk.TouchpadGesturePhase.END and self.pinch_at:
            if abs(tp.scale - 1) > 0.05:
                self.input.pinch(*self.pinch_at, tp.scale, self.current_size())
            self.pinch_at = None
        elif tp.phase == Gdk.TouchpadGesturePhase.CANCEL:
            self.pinch_at = None
        return True

    def edge(self, kind):
        self.flush_tap()
        self.input.system(kind, self.current_size())      # basic mode needs no size: also before the first picture

    def back(self):
        self.flush_tap()
        if self.input.size:
            self.input.back(self.current_size())
        return True

    # ---- keyboard ------------------------------------------------------
    def on_key(self, _w, ev):
        self.flush_tap()                                      # a click just before typing lands first
        name = Gdk.keyval_name(ev.keyval) or ""
        state = ev.state
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        fkeys = {"F1": self.input.home, "F2": lambda: self.edge("app_switcher"),
                 "F3": lambda: self.input.volume(False), "F4": lambda: self.input.volume(True),
                 "F5": lambda: self.edge("control_centre"), "F6": self.input.lock,
                 "F7": lambda: self.edge("notifications"), "F12": self.screenshot}
        if "action" in self.input.features:
            fkeys["F8"] = self.input.action
        if name in fkeys and not ctrl:
            fkeys[name]()
            return True
        if state & Gdk.ModifierType.MOD1_MASK:                # Alt: only Alt+Left (back); the rest is the desktop's
            return self.back() if name in ("Left", "KP_Left") else False
        if state & SUPER:
            return False
        if ctrl:
            k = name.lower()
            if k == "v" and shift:
                if self._can("clipboard", "Ctrl+Shift+V"):
                    self.clipboard().request_text(self._to_phone_clipboard)
            elif k == "v":
                self.clipboard().request_text(lambda _c, t: self._paste(t or ""))
            elif k == "c" and shift:
                if self._can("clipboard", "Ctrl+Shift+C"):
                    self.input.get_clipboard(lambda t: GLib.idle_add(self._from_phone_clipboard, t))
            elif k == "s":
                self.screenshot()
            elif k == "l":
                self.open_url()
            elif k == "o":
                if self._can("apps", "Ctrl+O"):
                    self.open_app()
            elif k in ("q", "w"):
                self.destroy()
            else:
                self.im.filter_keypress(ev)                    # e.g. Ctrl+Shift+U unicode entry
            return True
        if self.im.filter_keypress(ev):                        # the input method takes it first
            return True
        if name in KEYS or name in NAMED_KEYS:
            route = key_route(name, shift)
            if route not in self.keys_off:
                self.input.key(name, shift=shift, fail=lambda e, r=route: self._named_key_failed(e, r))
        else:
            ch = chr(Gdk.keyval_to_unicode(ev.keyval) or 0)
            if ch and ch.isprintable():
                self._type(ch)
        return True

    def _can(self, feature, what):
        if feature in self.input.features:
            return True
        self.flash(f"{what} needs full mode (WebDriverAgent)")
        return False

    def _type(self, text):
        self._untyped(text, self.input.type(text))

    def _paste(self, text):
        if text:
            self._untyped(text, self.input.paste(text))

    def _untyped(self, text, n):
        """n characters of text could not be typed (basic mode: what a US keyboard has, nothing else), or none of
        it (NOT_CONNECTED); the Bluetooth hint stays in the flash, which hides it for a while."""
        if n == NOT_CONNECTED:
            self.flash("\n".join(t for t in ("Bluetooth: the iPhone is not connected", self._input_text) if t))
        elif n:
            what = f"“{text}”" if n == len(text) <= 3 else f"{n} of {len(text)} characters"
            self.flash(f"{what} could not be typed over Bluetooth (US keyboard characters only)")

    def _named_key_failed(self, err, route):                  # worker thread
        """WebDriverAgent refused a key; "unsupported" turns off only its route (see key_route)."""
        if isinstance(err, WDAError) and "unsupported" in (err.error or "").lower():
            self.keys_off.add(route)
            GLib.idle_add(self.flash, "Shift+arrows, Shift+Tab and other Shift+key combinations need iOS 17 or later"
                          if route == "keyboardInput" else
                          "arrow, Delete, Home, End and Escape keys are not supported by this WebDriverAgent")
        else:
            self.log_line(f"key: {err}")

    @staticmethod
    def clipboard():
        return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    def _to_phone_clipboard(self, _clip, text):
        if not text:
            return self.flash("the laptop clipboard has no text")
        self.input.set_clipboard(text, lambda r: r is not None and GLib.idle_add(
            self.flash, "laptop clipboard copied to the iPhone - paste it there"))

    def _from_phone_clipboard(self, text):
        if text is None:
            return self.flash("could not read the iPhone clipboard")
        clip = self.clipboard()
        clip.set_text(text, -1)
        clip.store()
        return self.flash(f"iPhone clipboard copied to the laptop ({len(text)} characters)")

    # ---- dialogs -------------------------------------------------------
    def ask(self, title, prompt, choices=()):
        d = Gtk.Dialog(title=title, transient_for=self, modal=True)
        d.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Open", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        if choices:
            field = Gtk.ComboBoxText.new_with_entry()
            for c in choices:
                field.append_text(c)
            field.set_active(0)
            entry = field.get_child()
        else:
            field = entry = Gtk.Entry()
        entry.set_activates_default(True)
        entry.set_width_chars(32)
        area = d.get_content_area()
        area.set_spacing(6)
        area.set_border_width(10)
        area.add(Gtk.Label(label=prompt, xalign=0))
        area.add(field)
        d.show_all()
        ok = d.run() == Gtk.ResponseType.OK
        text = entry.get_text().strip()
        d.destroy()
        return text if ok and text else None

    def open_url(self):
        how = " through Spotlight" if self.basic else ""
        url = self.ask("Open URL", f"Open on the iPhone{how} (web address or app link):")
        if url:
            self.input.open_url(with_scheme(url))

    def open_app(self):
        recent = [a for a in load_config().get("recent_apps", []) if isinstance(a, str)]
        bundle = self.ask("Open app", "Bundle id, e.g. com.apple.mobilesafari:", recent)
        if not bundle:
            return
        self.input.launch(bundle)
        try:
            update_config(recent_apps=([bundle] + [a for a in recent if a != bundle])[:8])
        except OSError as e:
            print(f"recent apps not saved: {e}", flush=True)

    # ---- screenshot ----------------------------------------------------
    def screenshot(self):
        """WebDriverAgent's full-size PNG in full mode, else (or when that fails) the last decoded frame."""
        def save(png, frame):                                 # worker thread
            if not png and not frame:
                return self.video.last_frame_png(lambda p: save(p, True))
            try:
                msg = f"saved {save_png(png, SHOTS)}" if png else "screenshot not saved: no picture yet"
            except (OSError, ValueError) as e:
                msg = f"screenshot not saved: {getattr(e, 'strerror', None) or e}"
            GLib.idle_add(self.flash, msg)
        if not self.input.screenshot(lambda png: save(png, False)):
            self.video.last_frame_png(lambda png: save(png, True))

    def shutdown(self):
        """Stop the timers, the picture, the hidden stream and the input (also before an early exit)."""
        if self._closed:
            return
        self._closed = True
        if self._pair:
            self._pair.destroy()
        self.input.close()
        for attr in ("_retry_id", "_fit_id"):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        if self.drain:
            self.drain.stop()
        self.video.close()

    def quit(self, *_):
        self.shutdown()
        Gtk.main_quit()


def _notify_on_fail():
    """Desktop notification for a fatal error? IPHONE_NOTIFY=0/1 decides; otherwise only without a terminal
    and not under iphone.sh (IPHONE_FROM_SCRIPT=1), which reports window.log's last line itself."""
    flag = os.environ.get("IPHONE_NOTIFY", "").strip()
    if flag in ("0", "1"):
        return flag == "1"
    if os.environ.get("IPHONE_FROM_SCRIPT", "").strip() == "1":
        return False
    return not sys.stderr.isatty()


def fail(msg):
    """Stop with a message: on stderr, and as a desktop notification when no one else reports it."""
    print(msg, file=sys.stderr, flush=True)
    if _notify_on_fail():
        try:
            subprocess.run(["notify-send", "-a", "iPhone Control", "iPhone Control", msg],
                           timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    sys.exit(1)


def _no_proxy_for_loopback():
    """WDA is on loopback: keep any http_proxy away from it (libsoup reads no_proxy)."""
    hosts = "127.0.0.1,localhost,::1"
    for var in ("no_proxy", "NO_PROXY"):
        cur = os.environ.get(var, "")
        os.environ[var] = f"{cur},{hosts}" if cur else hosts


def _is_socket(path):
    try:
        return bool(path) and stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def input_mode():
    """IPHONE_MODE: full (WebDriverAgent; the default) or basic (Bluetooth). iphone.sh works out auto."""
    mode = os.environ.get("IPHONE_MODE", "").strip().lower() or "full"
    if mode not in ("full", "basic"):
        fail(f"IPHONE_MODE={mode}: the window takes full or basic (./iphone.sh works out auto)")
    return mode


def video_kind(mode):
    """IPHONE_VIDEO: usb, mjpeg or airplay. Unset in full mode: mjpeg, so wda_mirror.py by hand works as before;
    auto (or unset in basic mode): usb while the capture's socket is there, else mjpeg (full) or airplay (basic)."""
    kind = os.environ.get("IPHONE_VIDEO", "").strip().lower()
    if not kind and mode == "full":
        return "mjpeg"
    if kind in ("", "auto"):
        return "usb" if _is_socket(CAPTURE_SOCKET) else ("mjpeg" if mode == "full" else "airplay")
    if kind not in ("usb", "mjpeg", "airplay"):
        fail(f"IPHONE_VIDEO={kind}: use usb, mjpeg or airplay")
    if kind == "mjpeg" and mode == "basic":
        fail("IPHONE_VIDEO=mjpeg needs full mode: the MJPEG stream comes from WebDriverAgent")
    return kind


def _check_video(kind):
    """fail() unless the picture for kind can run here; a USB capture that is not running (yet) is waited for."""
    source = {"usb": video.H264Source, "airplay": video.AirplaySource}.get(kind, video.MjpegSource)
    missing = source.missing()
    if kind == "mjpeg" and missing == ["souphttpsrc"]:
        fail(f"{LIBSOUP_HINT}. Run ./setup.sh.")
    if os.environ.get("IPHONE_DECODER", "").strip() and "an H.264 decoder" in missing:
        fail(f"IPHONE_DECODER={os.environ['IPHONE_DECODER'].strip()}: no such GStreamer element installed")
    if missing == ["an H.264 decoder"]:
        fail(f"{video.DECODER_HINT}. Run ./setup.sh.")
    if missing:
        hint = GST_PACKAGES if kind == "mjpeg" else video.H264_PACKAGES
        fail(f"missing GStreamer elements: {', '.join(missing)} ({hint}). Run ./setup.sh.")
    if kind == "usb" and not CAPTURE_SOCKET:
        fail("IPHONE_VIDEO=usb needs IPHONE_CAPTURE_SOCKET, the USB capture's socket (./iphone.sh sets both)")
    if kind == "usb" and not _is_socket(CAPTURE_SOCKET):          # Mirror.on_lost retries, or falls back
        print(f"no USB capture socket at {CAPTURE_SOCKET} yet - waiting for it (./iphone.sh starts the capture)",
              flush=True)
    if kind == "airplay":
        problem = video.uxplay_problem()
        if problem:
            fail(problem)


def _connect_wda(kind):
    """Full mode: WebDriverAgent up, a session open with the settings for this picture."""
    try:
        phone = WDA(WDA_URL)
    except ValueError as e:
        fail(f"IPHONE_WDA_URL: {e}")
    try:
        st = phone.status()
        version = (st.get("os") or {}).get("version", "?") if isinstance(st, dict) else "?"
    except Exception as e:                    # OSError, a bad reply, ...
        fail(f"WebDriverAgent is not reachable on {phone.url} ({e}) - start it with ./iphone.sh")
    print(f"WDA ready: iOS {version}", flush=True)
    try:
        phone.connect(wda_settings(kind))
    except Exception as e:
        fail(f"Could not open a WebDriverAgent session ({e}). Unlock the iPhone and run ./iphone.sh again.")
    return phone


def _start_bluetooth(win, backend):
    """Basic mode: the Bluetooth LE mouse and keyboard (blehid), reporting to the window."""
    try:
        from .blehid import BleError, BleHid
    except ImportError as e:
        win.shutdown()
        fail(f"Bluetooth input is not available ({e}) - run ./setup.sh")
    try:
        backend.hid = BleHid(name=HID_NAME, adapter=BT_ADAPTER, on_state=win.on_ble_state,
                             confirm=win.confirm_pairing, log=win.log_line)
        backend.hid.start()
    except (BleError, GLib.Error) as e:
        backend.hid = None
        win.shutdown()
        fail(f"Bluetooth: {getattr(e, 'message', None) or e}")


def main():
    mode = input_mode()
    kind = video_kind(mode)
    _no_proxy_for_loopback()
    ok = Gtk.init_check(None)
    if not (ok[0] if isinstance(ok, tuple) else ok):
        fail("No graphical session found (DISPLAY / WAYLAND_DISPLAY not set).")
    # Until the window runs, INT and TERM end the start at once but through atexit, which stops uxplay (the default
    # TERM skips it; Bluetooth's start-up blocks on D-Bus for seconds): iphone.sh's stop may come at any time
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))
    Gst.init(None)
    _check_video(kind)
    backend = WdaBackend(_connect_wda(kind)) if mode == "full" else BleBackend()
    try:
        win = Mirror(backend, kind)
    except GLib.Error as e:
        fail(f"GStreamer pipeline failed ({e.message}) - run ./setup.sh to see which package is missing")
    if mode == "full":
        backend.phone.log = win.log_line
    else:
        _start_bluetooth(win, backend)
    Gtk.Window.set_default_icon_name("phone")             # docks and alt-tab on X11
    for sig in (signal.SIGINT, signal.SIGTERM):           # close cleanly (PyGObject wakes the loop)
        signal.signal(sig, lambda *_: GLib.idle_add(win.destroy))
    win.show_all()
    Gtk.main()
