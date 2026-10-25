"""WebDriverAgent HTTP client (no GTK). Commands run in order on one worker thread.

The worker keeps one keep-alive connection (no proxy is ever used: WDA is on loopback), merges queued
typing into one request, reopens the session when WDA restarted and reapplies the settings.
WDA itself runs requests one at a time (FBWebServer: the main queue), so a second connection or worker
would only wait for the first: one ordered queue loses nothing. Typing is the only thing merged (every
/wda/keys request costs ~290 ms before the first character); gestures cannot be (see INPUT_SETTINGS).
"""
import base64
import binascii
import http.client
import json
import queue
import threading
import urllib.parse
from typing import NamedTuple

# GDK key name -> text for /wda/keys (works on every iOS version)
KEYS = {"Return": "\n", "KP_Enter": "\n", "ISO_Enter": "\n", "BackSpace": "\b", "Tab": "\t"}


class NamedKey(NamedTuple):
    """A key that types nothing: its XCUIKeyboardKey name and its HID keyboard usage (usage page 7)."""
    xcui: str                    # for /wda/element/0/keyboardInput, e.g. "XCUIKeyboardKeyLeftArrow"
    usage: int                   # for /wda/performIoHidEvent (and any HID keyboard), e.g. 0x50; None: no HID


# GDK key name -> NamedKey. Without Shift a key is one HID event, /wda/performIoHidEvent on page 7: ~215 ms a key,
# against ~400 ms for keyboardInput (hardware key events; WDA: "only supported since Xcode15 and iPadOS 17"),
# which may also look into the element tree (see WDA.key). HID Left moves the caret; HID Home does nothing in a
# single-line field (iPhone 15 Pro, iOS 26.5.2, Oct 2026). A modifier cannot be held across HID calls, so
# Shift+Tab and Shift+arrows (select) stay on keyboardInput. Delete is forward delete; BackSpace stays "\b" above.
NAMED_KEYS = {
    "Left": ("LeftArrow", 0x50), "Right": ("RightArrow", 0x4F), "Up": ("UpArrow", 0x52), "Down": ("DownArrow", 0x51),
    "KP_Left": ("LeftArrow", 0x50), "KP_Right": ("RightArrow", 0x4F), "KP_Up": ("UpArrow", 0x52),
    "KP_Down": ("DownArrow", 0x51),
    "Escape": ("Escape", 0x29), "Delete": ("ForwardDelete", 0x4C), "KP_Delete": ("ForwardDelete", 0x4C),
    "Home": ("Home", 0x4A), "KP_Home": ("Home", 0x4A), "End": ("End", 0x4D), "KP_End": ("End", 0x4D),
    "Page_Up": ("PageUp", 0x4B), "KP_Page_Up": ("PageUp", 0x4B),
    "Page_Down": ("PageDown", 0x4E), "KP_Page_Down": ("PageDown", 0x4E),
    "ISO_Left_Tab": ("Tab", 0x2B),       # Shift+Tab
}
NAMED_KEYS = {k: NamedKey("XCUIKeyboardKey" + name, usage) for k, (name, usage) in NAMED_KEYS.items()}
_BY_XCUI = {k.xcui: k for k in NAMED_KEYS.values()}
SHIFT = 1 << 1                   # XCUIKeyModifierShift
HID_KEYBOARD = 7                 # HID usage page: keyboard / keypad
HID_PRESS_S = 0.01               # seconds a HID key is held down

# WDA settings for quick touches (measured on an iPhone 15, iOS 26.5, WDA 16.15). Before the finger goes down,
# XCTest takes two accessibility snapshots of the app in front for every pointerMove: ~110 ms each at WDA's
# default depth of 50 (more on busy screens: ~290 ms before a 2-move swipe on the home screen, ~590 in the App
# Library), ~27 ms at depth 1 (a 2-move swipe touches down after ~70 ms). After every W3C action WDA also waits
# for the app's animations to settle (animationCoolOffTimeout, ~380 ms a swipe), before replying and before
# the next command can start. Not useful here: waitForIdleTimeout (no effect on /actions), the
# shouldWaitForQuiescence capability (read only with a bundleId) and defaultActiveApplication (adds a hit
# test of up to 0.3 s to every request while an app is in front).
# Every new WDA session resets both to WDA's defaults (50 and 2; the MJPEG settings carry over), so they need no
# undoing and are applied again whenever the session is reopened.
# Touches are also quicker while WDA's MJPEG capture runs at ~30 fps: a swipe's first moved frame came after 294 ms
# at mjpegServerFramerate 1, 245 ms at 10 and 173-177 ms at 30 (the quality made no difference), and WDA captures
# only while a client is connected to its MJPEG port (iPhone 15 Pro, iOS 26.5.2, Oct 2026).
# Queued gestures cannot be merged: all touches of one W3C /actions request start at the same moment (WDA and
# XCTest ignore leading pauses and path offsets), so each gesture is a request of its own.
INPUT_SETTINGS = {"snapshotMaxDepth": 1, "animationCoolOffTimeout": 0}
DEEP = 50                        # WDA's default snapshotMaxDepth: for requests that look into the element tree

# /wda/keys costs ~290 ms a request plus the typing, at the request's "frequency" (characters a second; WDA's
# default maxTypingFrequency is 60): 50 characters took 1120 ms at 60/s and 409 ms at 400/s, and arrived intact
# (iPhone 15 Pro, iOS 26.5.2, Oct 2026). Queued keystrokes are merged into one request (WDA._next).
TYPE_CHUNK = 500                 # characters per /wda/keys request
TYPE_RATE = 120                  # characters a second for typing...
TYPE_RATE_FAST = 300             # ...and for pasted text
WDA_TYPE_RATE = 60               # WDA's own rate, for a /wda/keys request without a frequency
INVALID_SESSION = "invalid session id"
_STOP = object()


class WDAError(OSError):
    """An HTTP error reply from WebDriverAgent, with its W3C error code and message."""

    def __init__(self, code, error, message):
        super().__init__(f"{error or code}: {message}")
        self.code, self.error, self.message = code, error, message


def _error(code, reason, raw):
    try:
        v = json.loads(raw).get("value") or {}
        if not isinstance(v, dict):
            v = {"message": str(v)}
    except (ValueError, AttributeError):
        v = {}
    return WDAError(code, v.get("error"), (v.get("message") or reason or "")[:300])


def normalise_text(text):
    """Windows/browser line ends -> one Return each."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def type_chunks(text, size=TYPE_CHUNK):
    return [text[i:i + size] for i in range(0, len(text), size)]


def typing_timeout(n, rate=TYPE_RATE):
    """Seconds to allow for typing n characters at rate characters a second."""
    return n / rate + 10


class Job:
    __slots__ = ("method", "path", "body", "done", "fail", "timeout", "deep")

    def __init__(self, method, path, body, done, fail, timeout, deep=False):
        self.method, self.path, self.body = method, path, body
        self.done, self.fail, self.timeout = done, fail, timeout
        self.deep = deep                  # needs the full element tree: run at snapshotMaxDepth DEEP

    def typing(self, like=None):
        """Plain typing (no callbacks), which can be merged; with like: typing at like's rate."""
        return (self.path.endswith("/wda/keys") and self.done is None and self.fail is None
                and isinstance(self.body, dict) and isinstance(self.body.get("value"), list)
                and (like is None or self.body.get("frequency") == like.body.get("frequency")))


class WDA:
    def __init__(self, url="http://127.0.0.1:8100", log=print):
        self.url = url.rstrip("/")
        u = urllib.parse.urlsplit(self.url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"not an http:// URL: {url}")
        self._https = u.scheme == "https"
        self._host, self._port = u.hostname, u.port or (443 if self._https else 80)
        self._prefix = u.path
        self.log = log
        self.sid = None
        self.size = None              # screen size in points, in the current orientation
        self.settings = None          # appium settings (MJPEG, INPUT_SETTINGS) reapplied to every new session
        self.jobs = queue.Queue()
        self._peek = None             # a job taken out while merging typing, run next
        self._conn = None             # the worker's keep-alive connection
        self._thread = None

    # ---- plumbing ------------------------------------------------------
    def _open(self, timeout):
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=timeout)

    def _request(self, conn, method, path, body):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        conn.request(method, self._prefix + path.replace("{sid}", self.sid or ""), body=data,
                     headers=headers)
        r = conn.getresponse()
        raw = r.read()                     # the whole body, so the connection can be reused
        if r.status >= 400:
            raise _error(r.status, r.reason, raw)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise WDAError(r.status, "invalid reply", raw[:120].decode("utf-8", "replace")) from None

    def call(self, method, path, body=None, timeout=30):
        """One request on its own connection (any thread). Raises WDAError or OSError."""
        conn = self._open(timeout)
        try:
            return self._request(conn, method, path, body)
        finally:
            conn.close()

    def _drop(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def _call_kept(self, job):
        """A request on the worker's keep-alive connection; a stale one is replaced once."""
        for attempt in (1, 2):
            reused = self._conn is not None and self._conn.sock is not None
            if self._conn is None:
                self._conn = self._open(job.timeout)
            self._conn.timeout = job.timeout
            if self._conn.sock is not None:
                self._conn.sock.settimeout(job.timeout)
            try:
                return self._request(self._conn, job.method, job.path, job.body)
            except WDAError:
                raise                       # a full HTTP reply: the connection is still good
            except (ConnectionResetError, BrokenPipeError):   # incl. http.client.RemoteDisconnected
                self._drop()
                if attempt == 1 and reused:  # WDA closed the idle connection; the request never ran
                    continue
                raise
            except BaseException:
                self._drop()
                raise

    def status(self, timeout=5):
        return self.call("GET", "/status", timeout=timeout)["value"]

    def connect(self, settings=None):
        """Open a session (applying the settings), read the screen size in points, start the worker."""
        self.settings = dict(settings) if settings else None
        self.new_session()
        size = self.call("GET", "/session/{sid}/window/size")["value"]
        self.size = (size["width"], size["height"])
        self.start()
        return self

    def new_session(self):
        r = self.call("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
        self.sid = r.get("sessionId") or r["value"]["sessionId"]
        self._apply_settings()

    def _apply_settings(self):
        """WDA keeps these globally and forgets them when it restarts, so every new session sets them."""
        if not self.settings:
            return
        try:
            self.call("POST", "/session/{sid}/appium/settings", {"settings": self.settings})
            return
        except WDAError as e:
            if "mjpegFixOrientation" not in self.settings:
                return self.log(f"WDA settings not applied: {e}")
            first = e
        except (OSError, ValueError, http.client.HTTPException) as e:
            return self.log(f"WDA settings not applied: {e}")
        # older WebDriverAgent without mjpegFixOrientation: apply the rest
        rest = {k: v for k, v in self.settings.items() if k != "mjpegFixOrientation"}
        try:
            self.call("POST", "/session/{sid}/appium/settings", {"settings": rest})
            self.log(f"stream rotation not supported by this WebDriverAgent ({first})")
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.log(f"WDA settings not applied: {e}")

    def _reopen(self):
        try:
            self.new_session()
            return True
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
            self.log(f"could not reopen WDA session: {e}")
            return False

    # ---- the worker ----------------------------------------------------
    def start(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._worker, name="wda", daemon=True)
            self._thread.start()
        return self

    def close(self, timeout=5):
        """Stop the worker after the queued commands and close its connection."""
        if self._thread:
            self.jobs.put(_STOP)
            self._thread.join(timeout)
            self._thread = None

    def _next(self):
        """The next job; consecutive typing at one rate (key repeat, fast typing) is merged into one request."""
        job, self._peek = (self._peek or self.jobs.get()), None
        if job is _STOP or not job.typing():
            return job
        text = list(job.body["value"])
        while len(text) < TYPE_CHUNK:
            try:
                nxt = self.jobs.get_nowait()
            except queue.Empty:
                break
            if nxt is not _STOP and nxt.typing(job) and len(text) + len(nxt.body["value"]) <= TYPE_CHUNK:
                text += nxt.body["value"]
            else:
                self._peek = nxt
                break
        job.body = dict(job.body, value=text)
        job.timeout = max(job.timeout, typing_timeout(len(text), job.body.get("frequency") or WDA_TYPE_RATE))
        return job

    def _worker(self):
        while True:
            job = self._next()
            if job is _STOP:
                break
            try:
                self._run(job)
            except Exception as e:        # never let anything kill the only worker
                self.log(f"{job.path}: unexpected error: {e!r}")
        self._drop()

    def _depth(self, depth):
        """Set snapshotMaxDepth on the worker's connection; a failure is logged, never raised."""
        try:
            self._call_kept(Job("POST", "/session/{sid}/appium/settings", {"settings": {"snapshotMaxDepth": depth}},
                                None, None, 5))
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.log(f"snapshotMaxDepth {depth} not set: {e}")

    def _run(self, job):
        depth = (self.settings or {}).get("snapshotMaxDepth")
        if not (job.deep and isinstance(depth, int) and depth < DEEP):
            return self._run_one(job)
        self._depth(DEEP)                 # e.g. XCTest looks for the field with keyboard focus deep in the tree
        try:
            self._run_one(job)
        finally:
            self._depth(depth)

    def _run_one(self, job):
        try:
            result = self._call_kept(job)
        except WDAError as e:
            if not (e.error == INVALID_SESSION and "{sid}" in job.path and self._reopen()):
                return self._failed(job, e)
            try:                          # WDA restarted: same command on the new session
                if job.deep and (self.settings or {}).get("snapshotMaxDepth", DEEP) < DEEP:
                    self._depth(DEEP)     # reopening applied INPUT_SETTINGS again; _run sets it back after
                result = self._call_kept(job)
            except (OSError, ValueError, http.client.HTTPException) as e2:
                return self._failed(job, e2)
        except (OSError, ValueError, http.client.HTTPException) as e:
            return self._failed(job, e)
        self._finish(job, result)

    def _failed(self, job, err):
        if job.fail:
            try:
                job.fail(err)
            except Exception as e:
                self.log(f"{job.path}: callback failed: {e!r}")
        else:
            self.log(f"{job.path}: {err}")
        self._finish(job, None)

    def _finish(self, job, result):
        if job.done:
            try:
                job.done(result)
            except Exception as e:        # e.g. a full disk while saving a screenshot
                self.log(f"{job.path}: callback failed: {e!r}")

    def send(self, method, path, body=None, done=None, fail=None, timeout=30, deep=False):
        """Queue a command. On the worker thread afterwards: fail(error) if it failed (else the error
        is logged), then done(reply_or_None). deep = it needs the whole element tree (see INPUT_SETTINGS)."""
        self.jobs.put(Job(method, path, body, done, fail, timeout, deep))

    # ---- phone actions -------------------------------------------------
    def actions(self, w3c, done=None):
        # never only pauses: XCTest then waits ~5 s for a touch path that never comes
        self.send("POST", "/session/{sid}/actions", w3c, done)

    def type(self, text, fast=False):
        """Type text at TYPE_RATE, or pasted text (fast) at TYPE_RATE_FAST; long text goes in TYPE_CHUNK pieces."""
        rate = TYPE_RATE_FAST if fast else TYPE_RATE
        for chunk in type_chunks(normalise_text(text or "")):
            self.send("POST", "/session/{sid}/wda/keys", {"value": list(chunk), "frequency": rate},
                      timeout=typing_timeout(len(chunk), rate))

    def key(self, xcui_name, shift=False, fail=None):
        """A named key: a NAMED_KEYS value or its XCUIKeyboardKey name, e.g. an arrow, with Shift held or not.
        fail(error) if the phone refused it. Unshifted keys are HID events; only the keyboardInput route for Shift
        combinations (iOS 17 / Xcode 15 builds) can be "unsupported"."""
        key = xcui_name if isinstance(xcui_name, NamedKey) else _BY_XCUI.get(xcui_name, NamedKey(xcui_name, None))
        if not shift and key.usage is not None:
            # one HID key press: needs no element tree, so no snapshotMaxDepth round trips
            return self.send("POST", "/session/{sid}/wda/performIoHidEvent",
                             {"page": HID_KEYBOARD, "usage": key.usage, "duration": HID_PRESS_S}, fail=fail)
        k = {"key": key.xcui, "modifierFlags": SHIFT} if shift else key.xcui
        # XCTest may look for the focused field deep in the tree: not at snapshotMaxDepth 1 (whether it has to was
        # not measured, so it runs at WDA's default depth; /wda/keys and HID events need no snapshot)
        self.send("POST", "/session/{sid}/wda/element/0/keyboardInput", {"keys": [k]}, fail=fail, deep=True)

    def home(self):
        # the Home button itself: /wda/homescreen returns at once without pressing while SpringBoard is in
        # front, so it never closes Spotlight, Control Centre, Notification Centre or the App Switcher. HID
        # consumer Home (page 12, usage 0x40) is no quicker: both reply after ~465 ms (iOS 26.5.2, Oct 2026).
        self.send("POST", "/session/{sid}/wda/pressButton", {"name": "home"})

    def lock(self):
        self.send("POST", "/wda/lock", {})

    def unlock(self):
        self.send("POST", "/wda/unlock", {})

    def toggle_lock(self):
        def got(r):
            if r is not None:
                self.unlock() if r.get("value") else self.lock()
        self.send("GET", "/wda/locked", None, got)

    def button(self, name):            # volumeUp / volumeDown / action (iPhone 15 Pro and later)
        self.send("POST", "/session/{sid}/wda/pressButton", {"name": name})

    def open_url(self, url):
        self.send("POST", "/session/{sid}/url", {"url": url})

    def launch(self, bundle_id):
        self.send("POST", "/session/{sid}/wda/apps/launch", {"bundleId": bundle_id})

    def refresh_size(self):
        """Re-read the screen size (it swaps when the app in front rotates)."""
        def got(r):
            v = (r or {}).get("value") or {}
            if "width" in v and "height" in v:
                self.size = (v["width"], v["height"])
        self.send("GET", "/session/{sid}/window/size", None, got)

    def set_pasteboard(self, text, done=None):
        body = {"content": base64.b64encode(text.encode()).decode(), "contentType": "plaintext"}
        self.send("POST", "/wda/setPasteboard", body, done)

    def get_pasteboard(self, done):
        """done(text_or_None) on the worker thread."""
        def decode(r):
            try:
                text = base64.b64decode(r["value"]).decode("utf-8", "replace") if r else None
            except (binascii.Error, ValueError, TypeError, KeyError):
                text = None
            done(text)
        self.send("POST", "/wda/getPasteboard", {"contentType": "plaintext"}, decode)

    def screenshot(self, done):
        """done(png_bytes_or_None) on the worker thread."""
        def decode(r):
            try:
                png = base64.b64decode(r["value"]) if r and r.get("value") else None
            except (binascii.Error, ValueError, TypeError):
                png = None
            done(png)
        self.send("GET", "/screenshot", None, decode)
