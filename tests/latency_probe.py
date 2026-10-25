#!/usr/bin/env python3
"""Touch latency on a real iPhone: how long a home-screen page swipe (or a tap) takes to reach the screen.

  python3 tests/latency_probe.py --i-am-not-using-the-phone [--swipes 4] [--ms 250] [--moves 1 | --window-drag |
                                 --tap [N]] [--settings '{"animationCoolOffTimeout": 0}'] [--json OUT] [--url U]
                                 [--mjpeg U] [--video mjpeg | --video usb [--capture-socket PATH]]

Needs ./iphone.sh running and the iPhone unlocked, upright and left alone. It presses Home twice (first home
page), then swipes to the next home page and back, --swipes times, in the middle of the screen only. --tap taps
the home screen's "Search" pill instead, N times (default 4): each tap opens Spotlight and Home closes it again.
The pill is looked up in the accessibility tree (at snapshotMaxDepth 50; the session's depth is put back) and
must have its place and shape; without it, or when the screen is not the first home page, nothing is tapped.
For every gesture it times, from the moment the request is sent:
  - the WebDriverAgent reply (WebDriverAgent runs one request at a time, so the next command waits this long);
  - the first frame on its own MJPEG connection that differs from the frame before the gesture (more than
    --moved of the screen below the status bar): when the picture starts to move.
--video usb times that frame on the USB screen capture instead (iphone_control/screencap.py's socket: --capture-
socket, default $IPHONE_CAPTURE_SOCKET). Its H.264 is only recorded during the run and decoded after it (GStreamer:
h264parse, avdec_h264); the MJPEG stream is still read, and timed for comparison, because WebDriverAgent's touches
are slower while nobody reads it. The capture process serves several clients at once (screencap.MAX_CLIENTS), so
the window keeps its USB picture; if the socket closes during the run (the capture process stopped, or cut the probe
off for falling behind), the run fails.
It prints the median, minimum and maximum of each, and the frame intervals. --settings applies WebDriverAgent
settings for the run (JSON object, e.g. snapshotMaxDepth, animationCoolOffTimeout, mjpegServerScreenshotQuality)
and puts the old values back at the end, also after an error. It never types, long-presses, locks or unlocks,
taps nothing but the Search pill (and that only with --tap), and ends with Home on the first home page. It reuses
the window's session when there is one.
Exit status: 0 measured, 1 a gesture did not register, the phone did not come back to the first page or the USB
capture was lost, 2 refused (no flag, bad options or --settings, --tap without GdkPixbuf, --video usb without a
socket), 3 not ready (WebDriverAgent, the stream or the capture unreachable, locked, landscape, no Search pill).
Not part of the unit tests; tests/test_latency_probe.py runs it against a simulated phone.
"""
import argparse
import http.client
import json
import os
import socket
import statistics
import struct
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from live_test import CELLS, CHANGED, GDK, PIXEL_DIFF, SAME, SPRINGBOARD, STATUS_BAR  # noqa: E402
from iphone_control import geometry  # noqa: E402
from iphone_control.wda import DEEP  # noqa: E402

FLAG = "--i-am-not-using-the-phone"
MOVED = 0.05             # fraction of grid cells that must differ for a frame to count as "moved"
SETTLE = 1.2             # seconds after a reply before the page counts as settled
FIRST_FRAME_S = 5.0      # give up waiting for a moved frame after this long
POINTER_HZ = 125         # --window-drag: a mouse sampled this often
HEADER_MAX = 4096        # longest MJPEG part header accepted
TAPS = 4                 # --tap without a number
SEARCH = "Search"        # the pill's accessibility label (an iPhone in English)
PILL_QUERY = f"label == '{SEARCH}' OR name == '{SEARCH}'"     # a WebDriverAgent predicate string
PILL_FOUND_MAX = 8       # elements labelled Search whose place is read, at most
# iphone_control/screencap.py's socket frames: "<IBq" (payload length, kind, arrival ns), then the payload:
# VIDEO_FORMAT "<II" width, height + the avcC record; VIDEO one keyframe byte + an access unit (4-byte NAL lengths)
CAPTURE_HEADER = struct.Struct("<IBq")
VIDEO_FORMAT, VIDEO = 1, 2
CAPTURE_FRAME_MAX = 64 << 20     # longest capture frame accepted
CAPTURE_KEEP = 512 << 20         # H.264 kept for decoding after the run, at most (~6 Mbit/s: ~1.5 MB a gesture)
GOP_MAX = 1000                   # access units kept since the last keyframe, at most (one comes every ~0.7 s)
H264_ELEMENTS = ("appsrc", "h264parse", "avdec_h264", "videoconvert", "videoscale", "appsink")


def _gdk_grid(jpeg):
    """A JPEG shrunk to CELLS (GdkPixbuf): a list of rows of colour tuples, like live_test.grid()."""
    cols, lines = CELLS
    loader = GDK.PixbufLoader()
    loader.write(jpeg)
    loader.close()
    small = loader.get_pixbuf().scale_simple(cols, lines, GDK.InterpType.BILINEAR)
    n, rs, px = small.get_n_channels(), small.get_rowstride(), small.get_pixels()
    return [[tuple(px[y * rs + x * n:y * rs + x * n + 3]) for x in range(cols)] for y in range(lines)]


# JPEG -> grid. There is no JPEG decoder in the standard library: without GdkPixbuf only the replies and the
# frame interval are measured. The unit tests put their own decoder here.
decode = _gdk_grid if GDK is not None else None


def _gstreamer():
    """GStreamer with every element _gst_grids needs (gst-libav for avdec_h264), or None."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return None
    ok = Gst.init_check(None)
    if not (ok[0] if isinstance(ok, tuple) else ok) or not all(Gst.ElementFactory.find(e) for e in H264_ELEMENTS):
        return None
    return Gst


GST = _gstreamer()


def _gst_grids(fmt, units):
    """H.264 access units in order from a keyframe -> a grid like _gdk_grid's for each (None: not decoded).

    fmt = (width, height, avcC). Runs after the probe's run, never while frames are timed.
    """
    Gst, (cols, lines), (width, height, avcc) = GST, CELLS, fmt
    # multi-tap bilinear averages what it shrinks, like GdkPixbuf's BILINEAR for the MJPEG frames
    pipe = Gst.parse_launch("appsrc name=src format=time ! h264parse ! avdec_h264 ! videoconvert ! videoscale "
                            f"method=bilinear2 add-borders=false ! video/x-raw,format=RGB,width={cols},height={lines} "
                            "! appsink name=sink sync=false")
    src, sink = pipe.get_by_name("src"), pipe.get_by_name("sink")
    caps = Gst.Caps.from_string(f"video/x-h264,stream-format=avc,alignment=au,width={width},height={height}")
    caps.set_value("codec_data", Gst.Buffer.new_wrapped(bytes(avcc)))
    src.set_property("caps", caps)
    out, stride, bus = [None] * len(units), (cols * 3 + 3) & ~3, pipe.get_bus()   # rows are 4-byte aligned
    pipe.set_state(Gst.State.PLAYING)
    try:
        for i, data in enumerate(units):
            buf = Gst.Buffer.new_wrapped(bytes(data))
            buf.pts = buf.dts = i * Gst.MSECOND            # the unit's index, to find it again after decoding
            buf.duration = Gst.MSECOND
            src.emit("push-buffer", buf)
        src.emit("end-of-stream")
        end = time.monotonic() + 30 + len(units) * 0.1
        while time.monotonic() < end:
            sample = sink.emit("try-pull-sample", Gst.SECOND // 2)
            if sample is None:
                if sink.get_property("eos") or bus.pop_filtered(Gst.MessageType.ERROR):
                    break
                continue
            buf = sample.get_buffer()
            i = buf.pts // Gst.MSECOND
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok or not 0 <= i < len(out):
                continue
            try:
                px = bytes(info.data)
            finally:
                buf.unmap(info)
            out[i] = [[tuple(px[y * stride + x * 3:y * stride + x * 3 + 3]) for x in range(cols)]
                      for y in range(lines)]
    finally:
        pipe.set_state(Gst.State.NULL)
    return out


# H.264 access units -> grids, after the run (the unit tests put their own decoder here too)
decode_h264 = _gst_grids if GST is not None else None


def difference(ga, gb):
    """Fraction of grid cells below the status bar that differ between two decoded frames (0 = same)."""
    if len(ga) != len(gb) or len(ga[0]) != len(gb[0]):
        return 1.0
    top = int(len(ga) * STATUS_BAR)
    cells = [(pa, pb) for ra, rb in zip(ga[top:], gb[top:]) for pa, pb in zip(ra, rb)]
    return sum(max(abs(x - y) for x, y in zip(pa, pb)) > PIXEL_DIFF for pa, pb in cells) / max(len(cells), 1)


def spread(values):
    """{"n", "median", "min", "max"} in whole ms, or None for no values."""
    if not values:
        return None
    return {"n": len(values), "median": round(statistics.median(values)), "min": round(min(values)),
            "max": round(max(values))}


def percentile(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def intervals(times):
    """{"n", "median", "p95", "fps"} of the gaps between arrival times (ms), or None."""
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    if not gaps:
        return None
    return {"n": len(gaps), "median": round(statistics.median(gaps), 1), "p95": round(percentile(gaps, 0.95), 1),
            "fps": round(len(gaps) / ((times[-1] - times[0]) or 1), 1)}


def moved_after(frames, t0, threshold):
    """Arrival of the first frame after t0 that differs from the last one before it by more than threshold.

    frames = [(arrival, grid or None), ...] in arrival order; None = not decoded (skipped)."""
    ref = None
    for t, g in frames:
        if g is None:
            continue
        if t <= t0:
            ref = g
        elif ref is None:
            return None
        elif difference(ref, g) > threshold:
            return t
    return None


# ---------------------------------------------------------------- the stream
class Stream(threading.Thread):
    """Reads the MJPEG stream on its own connection and records when each whole frame arrived.

    Frames are only kept (and decoded later, off this thread) between mark() and the next mark(), so the
    arrival times are not delayed by decoding.
    """

    def __init__(self, url):
        super().__init__(name="mjpeg", daemon=True)
        u = urllib.parse.urlsplit(url)
        if u.scheme != "http" or not u.hostname:
            raise ValueError(f"not an http:// URL: {url}")
        self.host, self.port, self.path = u.hostname, u.port or 80, u.path or "/"
        self.cond = threading.Condition()
        self.frames = []          # (arrival, jpeg) since mark(), plus the last one before it
        self.times = []           # arrival of every frame
        self.error = None
        self.sock = None
        self.recording = False    # keep frames (between mark() and release())
        self._ended = False

    def run(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
            # the forward's guard wants a local Host (and no Origin): what the window's souphttpsrc sends too
            self.sock.sendall(f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n\r\n".encode())
            self._read(self.sock)
        except (OSError, ValueError, EOFError) as e:
            if not self._ended:
                self.error = str(e) or type(e).__name__
        finally:
            with self.cond:
                self._ended = True
                self.cond.notify_all()

    def _read(self, sock):
        buf = bytearray()

        def until(sep, start=0, limit=None):
            while True:
                i = buf.find(sep, start)
                if i >= 0:
                    return i
                if limit and len(buf) > limit:
                    raise ValueError("no MJPEG part header")
                data = sock.recv(1 << 18)
                if not data:
                    raise EOFError("the stream ended")
                buf.extend(data)

        head = until(b"\r\n\r\n", limit=16384)
        status = bytes(buf[:buf.find(b"\r\n")]).split()
        if len(status) < 2 or status[1] != b"200":
            raise ValueError("the stream answered " + bytes(buf[:buf.find(b"\r\n")]).decode("latin-1"))
        del buf[:head + 4]
        while not self._ended:
            start = until(b"\xff\xd8", limit=HEADER_MAX)            # a JPEG starts after each part header
            length = None
            for line in bytes(buf[:start]).split(b"\r\n"):
                k, _, v = line.partition(b":")
                if k.strip().lower() == b"content-length":
                    length = int(v)
            if length is not None:
                while len(buf) < start + length:
                    data = sock.recv(1 << 18)
                    if not data:
                        raise EOFError("the stream ended")
                    buf.extend(data)
                end = start + length
            else:                                                    # no length: up to the JPEG's end marker
                end = until(b"\xff\xd9", start + 2) + 2
            t = time.monotonic()
            jpeg = bytes(buf[start:end])
            del buf[:end]
            with self.cond:
                self.times.append(t)
                self.frames.append((t, jpeg))
                if not self.recording:
                    del self.frames[:-1]
                self.cond.notify_all()

    def mark(self):
        """Start keeping frames; returns the latest frame (arrival, jpeg) or None."""
        with self.cond:
            del self.frames[:-1]
            self.recording = True
            return self.frames[-1] if self.frames else None

    def release(self):
        with self.cond:
            self.recording = False
            del self.frames[:-1]

    def frame_after(self, t, timeout):
        """The first frame that arrived after t (waiting up to timeout s), or None."""
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                for f in self.frames:
                    if f[0] > t:
                        return f
                left = end - time.monotonic()
                if left <= 0 or self._ended:
                    return None
                self.cond.wait(left)

    def first_moved(self, t0, ref, threshold, timeout):
        """Arrival time of the first frame after t0 that differs from the grid ref by more than threshold."""
        end, i = t0 + timeout, 0
        while True:
            with self.cond:
                while i >= len(self.frames) and not self._ended and time.monotonic() < end:
                    self.cond.wait(max(0.001, end - time.monotonic()))
                if i >= len(self.frames):
                    return None
                t, jpeg = self.frames[i]
            i += 1
            if t > end:
                return None
            if t > t0 and difference(ref, decode(jpeg)) > threshold:
                return t

    def stop(self):
        with self.cond:
            self._ended = True
            self.cond.notify_all()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.join(5)
        if self.sock is not None:
            self.sock.close()


class Capture(threading.Thread):
    """Reads the USB capture socket (iphone_control/screencap.py) and records when each H.264 access unit arrived.

    H.264 only decodes in order from a keyframe, so nothing is decoded here or during the run: from mark() to
    release() it keeps the units, starting at the keyframe before the mark, for decode_h264 after the run.
    """

    def __init__(self, path):
        super().__init__(name="capture", daemon=True)
        self.path = path
        self.cond = threading.Condition()
        self.format = None        # (width, height, avcC)
        self.gop = []             # (arrival, data) since the last keyframe
        self.segments = []        # [format, [(arrival, data), ...]] for each mark(), from a keyframe on
        self.recording = False
        self.kept = 0             # bytes in segments
        self.full = False         # CAPTURE_KEEP reached: later marks keep nothing
        self.times = []           # arrival of every access unit since the first mark()
        self._marked = False
        self.error = None
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)    # here, so stop() can always shut it
        self._ended = False

    def run(self):
        try:
            self.sock.settimeout(5)
            self.sock.connect(self.path)
            self.sock.settimeout(None)
            if not self._ended:
                self._read(self.sock)
        except (OSError, ValueError, EOFError) as e:
            if not self._ended:
                self.error = str(e) or type(e).__name__
        finally:
            with self.cond:
                self._ended = True
                self.cond.notify_all()

    def _read(self, sock):
        buf, head, frames = bytearray(), CAPTURE_HEADER.size, 0

        def fill(n):
            while len(buf) < n:
                data = sock.recv(1 << 18)
                if not data:                      # screencap.Server closes a client for these (cap.log says which)
                    raise EOFError("the capture socket closed: the capture process stopped, or cut this probe off "
                                   "for falling behind (see cap.log beside the socket)" if frames else
                                   "the capture socket closed before any frame: the capture process already serves "
                                   "as many clients as it takes, or is stopping (see cap.log beside the socket)")
                buf.extend(data)

        while not self._ended:
            fill(head)
            n, kind, _ = CAPTURE_HEADER.unpack_from(buf)
            if n > CAPTURE_FRAME_MAX:
                raise ValueError(f"a capture frame of {n} bytes: is this the capture socket?")
            fill(head + n)
            t = time.monotonic()
            payload = bytes(buf[head:head + n])
            del buf[:head + n]
            frames += 1
            if kind == VIDEO_FORMAT and n >= 8:
                self._format(struct.unpack_from("<II", payload) + (payload[8:],))
            elif kind == VIDEO and n > 1:
                self._unit(t, payload[0] == 1, payload[1:])

    def _format(self, fmt):
        with self.cond:
            if fmt != self.format:                # a new stream (the picture turned): the old units are no use
                self.format, self.gop = fmt, []
                if self.recording:
                    self.segments.append([fmt, []])
            self.cond.notify_all()

    def _unit(self, t, key, data):
        with self.cond:
            if self._marked:
                self.times.append(t)
            if self.recording and not self.full:
                self.segments[-1][1].append((t, data))
                self.kept += len(data)
                self.full = self.kept > CAPTURE_KEEP
            if key:
                self.gop = [(t, data)]
            elif self.gop and len(self.gop) < GOP_MAX:
                self.gop.append((t, data))
            self.cond.notify_all()

    def wait_picture(self, timeout):
        """True once the format and a keyframe arrived (waiting up to timeout s)."""
        end = time.monotonic() + timeout
        with self.cond:
            while not (self.format and self.gop):
                left = end - time.monotonic()
                if left <= 0 or self._ended:
                    return False
                self.cond.wait(left)
            return True

    def mark(self):
        """Start keeping units (from the last keyframe on); returns the segment's index in segments."""
        with self.cond:
            if not self._marked:                  # the frame interval from here: not the replay a new client gets
                self._marked, self.times = True, []
            units = [] if self.full else list(self.gop)
            self.segments.append([self.format, units])
            self.kept += sum(len(d) for _, d in units)
            self.recording = True
            return len(self.segments) - 1

    def release(self):
        with self.cond:
            self.recording = False

    def stop(self):
        with self.cond:
            self._ended, self.recording = True, False
            self.cond.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self.ident is not None:
            self.join(5)
        self.sock.close()


# ---------------------------------------------------------------- WebDriverAgent
class Kept:
    """One kept-alive connection to WebDriverAgent, like the window's worker, so a swipe's time is the
    request itself and not a new connection through the forward."""

    def __init__(self, url):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "http" or not u.hostname:
            raise ValueError(f"not an http:// URL: {url}")
        self.host, self.port, self.prefix = u.hostname, u.port or 80, u.path.rstrip("/")
        self.conn = None
        self.sid = None

    def call(self, method, path, body=None, timeout=60, whole=False):
        """The reply's value (whole=True: the whole reply). Raises OSError, also for a reply that broke off."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        path = self.prefix + path.replace("{sid}", self.sid or "")
        for attempt in (1, 2):
            fresh = self.conn is None
            if fresh:
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            self.conn.timeout = timeout
            if self.conn.sock is not None:
                self.conn.sock.settimeout(timeout)
            try:
                self.conn.request(method, path, body=data, headers=headers)
                r = self.conn.getresponse()
                raw = r.read()
            except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected):
                self.close()
                if attempt == 1 and not fresh:          # WebDriverAgent closed an idle connection
                    continue
                raise
            except (OSError, http.client.HTTPException) as e:
                self.close()
                if isinstance(e, OSError):
                    raise
                # IncompleteRead, BadStatusLine...: the forward dropped mid-reply. Not sent again (it may have run);
                # an OSError, so the callers' handlers still restore the settings, press Home and end the session
                raise OSError(f"{method} {path}: a broken reply: {e!r}") from e
            try:
                reply = json.loads(raw) if raw else {}
            except ValueError:
                reply = {}
            if r.status >= 400:
                v = reply.get("value") if isinstance(reply.get("value"), dict) else {}
                raise OSError(f"{method} {path}: HTTP {r.status} {v.get('error') or ''} {v.get('message') or ''}"
                              .strip())
            if whole:
                return reply
            return reply.get("value") if isinstance(reply, dict) else None

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def check_swipe(w3c, size):
    """Refuse anything but a one-finger horizontal swipe through the middle of the screen: no tap, no hold,
    nothing near an edge (Control Centre, notifications, Home), nothing vertical (Spotlight)."""
    w, h = size
    fingers = w3c["actions"]
    if len(fingers) != 1:
        raise ValueError("refusing a gesture with more than one finger")
    acts = fingers[0]["actions"]
    types = [a["type"] for a in acts]
    if types[0] != "pointerMove" or types.count("pointerDown") != 1 or types[-1] != "pointerUp":
        raise ValueError("refusing a gesture that is not down - move - up")
    after = acts[types.index("pointerDown") + 1:-1]
    if not after or after[0]["type"] != "pointerMove":
        raise ValueError("refusing a gesture that holds before moving")
    points = [a for a in acts if a["type"] == "pointerMove"]
    for p in points:
        if not (w * 0.05 <= p["x"] <= w * 0.95 and h * 0.3 <= p["y"] <= h * 0.7):
            raise ValueError("refusing a gesture outside the middle of the screen")
    dx, dy = points[-1]["x"] - points[0]["x"], points[-1]["y"] - points[0]["y"]
    if abs(dx) < w * 0.3 or abs(dy) > abs(dx) / 4:
        raise ValueError("refusing a gesture that is not a long horizontal swipe")
    if sum(a["duration"] for a in after if a["type"] == "pause") > 300:
        raise ValueError("refusing a gesture that rests long")
    return w3c


class NotReady(RuntimeError):
    """The phone is not in a state to measure (exit status 3)."""


def pick_pill(rects, size):
    """(x, y, rect): the centre of the Search pill among the rects of the elements labelled Search.

    NotReady unless that is one wide, short element in the middle of the screen's lower part, above the Dock
    (iPhone 15 Pro, iOS 26.5.2: around (196, 704) of 393 x 852 points), which is what a tap may hit."""
    w, h = size
    ok = []
    for r in rects:
        try:
            x, y, rw, rh = (float(r[k]) for k in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            continue
        cx, cy = x + rw / 2, y + rh / 2
        if (30 <= rw <= w * 0.6 and 16 <= rh <= 64 and rw >= rh and w * 0.3 <= cx <= w * 0.7
                and h * 0.6 <= cy <= h * 0.92 and x >= 0 and y >= 0 and x + rw <= w and y + rh <= h):
            ok.append((cx, cy, r))
    if not ok:
        seen = f" ({len(rects)} element(s) labelled {SEARCH}, none of the pill's place and shape)" if rects else ""
        raise NotReady(f'no "{SEARCH}" pill above the Dock{seen}: it can be turned off in Settings > Home Screen & '
                       "App Library, and has another name on an iPhone in another language - nothing tapped")
    if any(abs(cx - ok[0][0]) + abs(cy - ok[0][1]) > 4 for cx, cy, _ in ok):
        raise NotReady(f"more than one element labelled {SEARCH} where the pill would be - nothing tapped")
    return ok[0]


def check_tap(w3c, rect):
    """Refuse anything but one quick one-finger tap inside rect (the Search pill): no hold, no second tap, no move."""
    fingers = w3c["actions"]
    if len(fingers) != 1:
        raise ValueError("refusing a gesture with more than one finger")
    acts = fingers[0]["actions"]
    if [a["type"] for a in acts] != ["pointerMove", "pointerDown", "pause", "pointerUp"]:
        raise ValueError("refusing a gesture that is not one tap")
    if acts[2]["duration"] > 100:
        raise ValueError("refusing a tap that holds (a long press)")
    x, y = acts[0]["x"], acts[0]["y"]
    if not (rect["x"] <= x <= rect["x"] + rect["width"] and rect["y"] <= y <= rect["y"] + rect["height"]):
        raise ValueError("refusing a tap outside the Search pill")
    return w3c


def swipe_gesture(x0, x1, y, ms, moves=1, window_drag=False, size=None):
    """The W3C swipe from x0 to x1: `moves` even moves over ms, or what the window sends for that mouse drag."""
    if window_drag:
        n = max(1, round(ms / 1000 * POINTER_HZ))
        track = [(x0 + (x1 - x0) * i / n, y, ms / 1000 * i / n) for i in range(n + 1)]
        try:
            return geometry.drag_to_gesture(track, size)
        except TypeError:                                 # drag_to_gesture without the size argument
            return geometry.drag_to_gesture(track)
    return geometry.gesture([(x0, y, 0)] + [(x0 + (x1 - x0) * i / moves, y, ms * i / moves)
                                            for i in range(1, moves + 1)])


def parse_settings(text):
    try:
        settings = json.loads(text)
    except ValueError as e:
        raise ValueError(f"--settings is not JSON: {e}") from None
    if not isinstance(settings, dict) or not settings:
        raise ValueError("--settings must be a JSON object with at least one setting")
    scale = settings.get("mjpegScalingFactor", 100)
    if not isinstance(scale, (int, float)) or scale < 100:
        # below 100 WebDriverAgent re-encodes every frame and iOS kills it for CPU (cpu_resource_fatal); the
        # setting outlives this run and takes effect when WebDriverAgent next starts
        raise ValueError("refusing mjpegScalingFactor below 100: iOS stops WebDriverAgent for CPU every few minutes")
    return settings


# ---------------------------------------------------------------- the run
class Probe:
    def __init__(self, args, log=print):
        self.a, self.say = args, log
        self.wda = Kept(args.url)
        self.kind = "tap" if args.tap else "swipe"
        self.pad = 2 if args.tap else 5              # labels: tap numbers, or left / right
        self.stream = None
        self.capture = None           # --video usb
        self.size = None
        self.own_session = False
        self.saved = None             # settings as they were before --settings
        self.rows = []                # one dict per gesture
        self.pending = []             # --video usb: (row, capture segment, t0) to time after the run
        self.failed = []

    def wait(self, seconds):
        if seconds and self.a.delay_scale:
            time.sleep(seconds * self.a.delay_scale)

    def home(self):
        self.wda.call("POST", "/session/{sid}/wda/pressButton", {"name": "home"})
        self.wait(1.5)                                    # also keeps two presses from being a double press

    def latest(self, after=None, timeout=2.0):
        """The first frame after `after` (default: now), decoded, or None."""
        f = self.stream.frame_after(time.monotonic() if after is None else after, timeout)
        return (f[0], decode(f[1])) if f and decode else (f[0], None) if f else None

    def front_app(self):
        return (self.wda.call("GET", "/wda/activeAppInfo") or {}).get("bundleId")

    # ---- steps --------------------------------------------------------
    def preflight(self):
        whole = self.wda.call("GET", "/status", timeout=5, whole=True)
        st = whole.get("value") or {}
        if self.wda.call("GET", "/wda/locked", timeout=10):
            raise RuntimeError("the iPhone is locked - unlock it first (this probe never unlocks it)")
        # the window's session when it has one (a new session would end it; the window would reopen its own)
        self.wda.sid = whole.get("sessionId")
        reply = None
        if self.wda.sid:
            try:
                reply = self.wda.call("GET", "/session/{sid}/window/size", timeout=10)
            except OSError:
                self.wda.sid = None
        if not self.wda.sid:
            self.wda.sid = self._new_session()
            self.own_session = True
            reply = self.wda.call("GET", "/session/{sid}/window/size", timeout=10)
        self.size = (reply["width"], reply["height"])
        if self.size[0] > self.size[1]:
            raise RuntimeError("the screen is in landscape - turn the iPhone upright first")
        self.stream = Stream(self.a.mjpeg)
        self.stream.start()
        if not self.stream.frame_after(0, 5):
            raise RuntimeError(f"no frames from the stream at {self.a.mjpeg}: {self.stream.error or 'timed out'}")
        video = ""
        if self.a.video == "usb":
            self.capture = Capture(self.a.capture_socket)
            self.capture.start()
            if not self.capture.wait_picture(5):
                raise RuntimeError(f"no frames from the USB capture at {self.a.capture_socket}: "
                                   f"{self.capture.error or 'timed out'}")
            video = f"; USB capture {self.capture.format[0]}x{self.capture.format[1]}"
        os_v = ((st.get("os") or {}).get("version") if isinstance(st, dict) else None) or "?"
        self.say(f"WebDriverAgent ready: iOS {os_v}, screen {self.size[0]}x{self.size[1]} points, "
                 f"{'a new' if self.own_session else 'the existing'} session{video}")

    def _new_session(self):
        reply = self.wda.call("POST", "/session", {"capabilities": {"alwaysMatch": {}}}, timeout=30, whole=True)
        return reply.get("sessionId") or (reply.get("value") or {})["sessionId"]

    def apply_settings(self):
        current = self.wda.call("GET", "/session/{sid}/appium/settings") or {}
        want = self.a.settings or {}
        shown = {k: current.get(k) for k in ("mjpegServerFramerate", "mjpegScalingFactor",
                                             "mjpegServerScreenshotQuality", "snapshotMaxDepth",
                                             "waitForIdleTimeout", "animationCoolOffTimeout") if k in current}
        if want:
            missing = [k for k in want if k not in current]
            if missing:
                self.say(f"note: WebDriverAgent does not report {', '.join(missing)}; they are not restored after")
            self.saved = {k: current[k] for k in want if k in current}
            self.wda.call("POST", "/session/{sid}/appium/settings", {"settings": want})
            shown.update(want)
        self.settings_used = shown
        self.say("settings: " + json.dumps(shown, sort_keys=True))

    def restore_settings(self):
        if not self.saved:
            return
        try:
            self.wda.call("POST", "/session/{sid}/appium/settings", {"settings": self.saved})
            self.say("settings restored: " + json.dumps(self.saved, sort_keys=True))
            self.saved = None
        except OSError as e:
            self.say(f"COULD NOT RESTORE the settings {json.dumps(self.saved, sort_keys=True)}: {e}")
            self.failed.append("restore settings")

    def to_first_page(self):
        self.home()
        self.home()                                       # a second Home goes to the first page
        app = self.front_app()
        if app not in (SPRINGBOARD, None):
            raise RuntimeError(f"not on the home screen after Home (front app {app})")

    def one_gesture(self, gesture, label):
        """Send one gesture and time it; returns the frame after it settled (as latest()).

        With --video usb nothing is decoded until the capture has its frames for the gesture, so a JPEG decode
        never holds up the thread that records when they arrive."""
        before = self.stream.mark()
        segment = self.capture.mark() if self.capture else None
        ref = decode(before[1]) if decode and before else None
        try:
            self.wda.call("GET", "/status", timeout=5)    # the kept connection is open before timing starts
            t0 = time.monotonic()
            self.wda.call("POST", "/session/{sid}/actions", gesture)
            t1 = time.monotonic()
            if self.capture:
                self.wait(SETTLE)
                self.capture.release()
            moved = None
            if ref is not None:
                tm = self.stream.first_moved(t0, ref, self.a.moved, max(0.5, FIRST_FRAME_S * self.a.delay_scale))
                moved = None if tm is None else (tm - t0) * 1000
        finally:
            self.stream.release()
            if self.capture:
                self.capture.release()
        row = {self.kind: label, "reply_ms": round((t1 - t0) * 1000),
               "moved_ms": None if moved is None else round(moved)}
        if self.kind == "swipe":
            row["moves"] = sum(a["type"] == "pointerMove" for a in gesture["actions"][0]["actions"]) - 1
        self.rows.append(row)
        seen = "not measured" if ref is None else "none within 5 s" if moved is None else f"{moved:5.0f} ms"
        if self.capture:                                  # the capture's own is only known after the run
            row["mjpeg_moved_ms"], row["moved_ms"] = row["moved_ms"], None
            self.pending.append((row, segment, t0))
            seen = f"(MJPEG) {seen}, on the USB capture: after the run"
        self.say(f"{self.kind} {label:{self.pad}}: WDA reply {row['reply_ms']:5d} ms, first moved frame {seen}")
        if not self.capture:
            self.wait(SETTLE)
        return self.latest(after=t1 + SETTLE * self.a.delay_scale)

    def swipes(self):
        w, h = self.size
        start = self.latest()
        home_ref = start[1] if start else None

        def swipe(x0, x1, label):
            gesture = check_swipe(swipe_gesture(x0, x1, h * 0.5, self.a.ms, self.a.moves, self.a.window_drag,
                                                self.size), self.size)
            return self.one_gesture(gesture, label)

        for i in range(self.a.swipes):
            self.wait(0.3)
            after = swipe(w * 0.85, w * 0.15, "left")
            if home_ref is not None and after and after[1] is not None and difference(home_ref, after[1]) < CHANGED:
                self.say("FAIL  the swipe to the next home page did not change the screen")
                self.failed.append(f"swipe {i + 1} left")
                self.to_first_page()
                continue
            after = swipe(w * 0.15, w * 0.85, "right")
            if home_ref is not None and after and after[1] is not None:
                d = difference(home_ref, after[1])
                if d > SAME:
                    self.say(f"FAIL  not back on the first home page ({d:.0%} of the screen differs); Home")
                    self.failed.append(f"swipe {i + 1} right")
                    self.to_first_page()

    # ---- taps on the Search pill -------------------------------------
    def search_pill(self):
        """(x, y, rect) of the home screen's Search pill (pick_pill), looked up at snapshotMaxDepth DEEP (the
        window's session keeps 1, too shallow for an element query); the session's depth is put back after."""
        depth = (self.wda.call("GET", "/session/{sid}/appium/settings") or {}).get("snapshotMaxDepth")
        deeper = isinstance(depth, int) and depth < DEEP
        if deeper:
            self.wda.call("POST", "/session/{sid}/appium/settings", {"settings": {"snapshotMaxDepth": DEEP}})
        try:
            found = self.wda.call("POST", "/session/{sid}/elements", {"using": "predicate string",
                                                                      "value": PILL_QUERY}, timeout=60) or []
            rects = []
            for el in found[:PILL_FOUND_MAX]:
                uid = (el.get("ELEMENT") or next(iter(el.values()), None)) if isinstance(el, dict) else None
                if uid:
                    rects.append(self.wda.call("GET", f"/session/{{sid}}/element/{uid}/rect", timeout=30))
        finally:
            if deeper:
                try:
                    self.wda.call("POST", "/session/{sid}/appium/settings", {"settings": {"snapshotMaxDepth": depth}})
                except OSError as e:
                    self.say(f"COULD NOT RESTORE snapshotMaxDepth {depth}: {e}")
                    self.failed.append("restore snapshotMaxDepth")
                    raise
        return pick_pill(rects, self.size)

    def check_home(self, home_ref):
        """Raise unless the first home page is on screen as it was when the pill was found."""
        app = self.front_app()
        now = self.latest()
        d = difference(home_ref, now[1]) if now and now[1] is not None else 1.0
        if app not in (SPRINGBOARD, None) or d > SAME:
            raise RuntimeError(f"not on the first home page before a tap ({d:.0%} of the screen differs, front app "
                               f"{app}) - nothing tapped")

    def taps(self):
        start = self.latest()
        home_ref = start[1] if start else None
        if home_ref is None:
            raise NotReady("no picture of the home screen to check each tap against")
        x, y, rect = self.search_pill()
        self.say(f"{SEARCH} pill at ({x:.0f}, {y:.0f}), {rect['width']:.0f} x {rect['height']:.0f} points")
        for i in range(self.a.tap):
            self.wait(0.3)
            self.check_home(home_ref)                     # the pill is where it was: the screen has not changed
            after = self.one_gesture(check_tap(geometry.tap(x, y), rect), str(i + 1))
            if after is None or after[1] is None or difference(home_ref, after[1]) < CHANGED:
                self.say("FAIL  the tap on the Search pill did not open Spotlight")
                self.failed.append(f"tap {i + 1}")
                self.to_first_page()
                return
            self.home()                                   # closes Spotlight
            back = self.latest()
            d = difference(home_ref, back[1]) if back and back[1] is not None else 1.0
            if d > SAME:
                self.say(f"FAIL  Home did not close Spotlight ({d:.0%} of the screen differs from the first page)")
                self.failed.append(f"tap {i + 1} Home")
                self.to_first_page()

    # ---- after the run ------------------------------------------------
    def time_capture(self):
        """Decode what the USB capture kept and fill in each gesture's first moved frame on it."""
        if not self.pending or decode_h264 is None:
            return
        n = sum(len(self.capture.segments[seg][1]) for _, seg, _ in self.pending)
        self.say(f"decoding {n} H.264 frames of the USB capture...")
        if self.capture.full:
            self.say(f"note: only the first {CAPTURE_KEEP >> 20} MB of the capture were kept: later "
                     f"{self.kind}s are not timed on it")
        for row, seg, t0 in self.pending:
            fmt, units = self.capture.segments[seg]
            try:
                grids = decode_h264(fmt, [d for _, d in units]) if fmt and units else []
            except Exception as e:                    # GLib.Error from GStreamer, among others
                self.say(f"FAIL  the USB capture could not be decoded: {e}")
                self.failed.append("decode the USB capture")
                return
            tm = moved_after([(t, g) for (t, _), g in zip(units, grids)], t0, self.a.moved)
            row["moved_ms"] = None if tm is None else round((tm - t0) * 1000)
            seen = "none" if tm is None else f"{row['moved_ms']:5d} ms"
            self.say(f"{self.kind} {row[self.kind]:{self.pad}}: first moved frame (USB capture) {seen}")

    def summary(self):
        noun = self.kind + "s"
        reply = spread([r["reply_ms"] for r in self.rows])
        moved = spread([r["moved_ms"] for r in self.rows if r["moved_ms"] is not None])
        mjpeg = spread([r["mjpeg_moved_ms"] for r in self.rows if r.get("mjpeg_moved_ms") is not None])
        interval = intervals(list(self.stream.times) if self.stream else [])
        capture = intervals(list(self.capture.times)) if self.capture else None
        on = ", USB capture" if self.capture else ""
        self.say("")
        if reply:
            self.say(f"send -> WDA reply:          median {reply['median']} ms (min {reply['min']}, max "
                     f"{reply['max']}), {reply['n']} {noun}")
        if moved:
            missed = len(self.rows) - moved["n"]
            self.say(f"send -> first moved frame:  median {moved['median']} ms (min {moved['min']}, max "
                     f"{moved['max']}), {moved['n']} {noun}" + (f", {missed} without one" if missed else "") + on)
        elif self.rows and self.capture and decode_h264 is None:
            self.say("send -> first moved frame:  not measured (no H.264 decoder: GStreamer's h264parse and "
                     "avdec_h264 from gst-libav, with PyGObject)")
        elif self.rows and self.capture:
            self.say("send -> first moved frame:  none on the USB capture")
        elif self.rows and decode:
            self.say(f"send -> first moved frame:  none within {FIRST_FRAME_S:.0f} s")
        elif self.rows:
            self.say("send -> first moved frame:  not measured (no JPEG decoder: install PyGObject's GdkPixbuf)")
        if mjpeg:
            self.say(f"  the same on MJPEG:        median {mjpeg['median']} ms (min {mjpeg['min']}, max "
                     f"{mjpeg['max']}), {mjpeg['n']} {noun}")
        if capture:
            self.say(f"USB capture frame interval: median {capture['median']} ms (p95 {capture['p95']}), "
                     f"{capture['fps']} fps")
        if interval:
            self.say(f"MJPEG frame interval:       median {interval['median']} ms (p95 {interval['p95']}), "
                     f"{interval['fps']} fps")
        result = {"settings": getattr(self, "settings_used", None), "gesture": self.kind, "video": self.a.video,
                  noun: self.rows, "reply_ms": reply, "moved_ms": moved, "frame_interval_ms": interval,
                  "failed": self.failed}
        if self.kind == "swipe":
            result["swipe_ms"] = self.a.ms
        if self.capture:
            result.update(mjpeg_moved_ms=mjpeg, capture_frame_interval_ms=capture)
        if self.a.json:
            with open(self.a.json, "w") as f:
                json.dump(result, f, indent=1)
        return result

    def run(self):
        try:
            return self._run()
        finally:
            self.close()                                  # whatever ended the run (Ctrl-C too): no session left

    def _run(self):
        try:
            self.preflight()
            self.apply_settings()
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as e:
            self.say(f"not ready: {e}")
            self.restore_settings()
            return 3
        ready = True
        try:
            self.to_first_page()
            if self.a.tap:
                self.taps()
            else:
                self.swipes()
        except NotReady as e:
            self.say(f"not ready: {e}")
            ready = False
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as e:
            self.say(f"FAIL  {e}")
            self.failed.append(str(e))
        finally:
            try:
                self.home()                               # always end on the first home page
            except OSError as e:
                self.say(f"FAIL  Home: {e}")
                self.failed.append("final Home")
            self.restore_settings()
        if not ready:
            return 3
        if self.capture:
            if self.capture.error:
                self.say(f"FAIL  USB capture: {self.capture.error}")
                self.failed.append("USB capture lost")
            self.capture.stop()                           # nothing more is kept, or counted, while this decodes
            self.time_capture()
        self.summary()
        return 1 if self.failed or not self.rows else 0

    def close(self):
        if self.stream is not None:
            self.stream.stop()
        if self.capture is not None:
            self.capture.stop()
        if self.own_session:
            try:
                self.wda.call("DELETE", "/session/{sid}", timeout=10)
            except OSError:
                pass
        self.wda.close()


def main(argv=None, log=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(FLAG, dest="ok", action="store_true",
                    help="required: you are not using the iPhone and it may be swiped for about a minute")
    ap.add_argument("--url", default=os.environ.get("IPHONE_WDA_URL", "http://127.0.0.1:8100"))
    ap.add_argument("--mjpeg", default=os.environ.get("IPHONE_MJPEG_URL", "http://127.0.0.1:9100"))
    ap.add_argument("--swipes", type=int, default=4, help="swipes there and back (default 4: 8 swipes)")
    ap.add_argument("--ms", type=int, default=250, help="swipe duration in ms (default 250)")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--moves", type=int, default=1,
                     help="pointer moves after the touch-down point (default 1, the cheapest)")
    how.add_argument("--window-drag", action="store_true",
                     help="send what the window sends for a mouse drag of --ms (geometry.drag_to_gesture)")
    how.add_argument("--tap", type=int, nargs="?", const=TAPS, metavar="N",
                     help=f"instead of swiping, tap the home screen's {SEARCH} pill N times (default {TAPS}): "
                          "it opens Spotlight, then Home closes it")
    ap.add_argument("--settings", type=str, help="WebDriverAgent settings for the run, as a JSON object")
    ap.add_argument("--moved", type=float, default=MOVED, help=f"fraction of the screen that counts as moved "
                                                               f"(default {MOVED})")
    ap.add_argument("--video", choices=("mjpeg", "usb"), default="mjpeg",
                    help="time the first moved frame on WebDriverAgent's MJPEG stream (default) or the USB capture")
    ap.add_argument("--capture-socket", default=os.environ.get("IPHONE_CAPTURE_SOCKET"),
                    help="--video usb: the capture process's socket (default $IPHONE_CAPTURE_SOCKET)")
    ap.add_argument("--json", help="also write the results to this file")
    ap.add_argument("--delay-scale", type=float, default=1.0, help=argparse.SUPPRESS)   # tests: small
    a = ap.parse_args(argv)
    if not a.ok:
        log(f"Refusing to run: this swipes the real iPhone's home screen (with --tap: taps its Search pill) and\n"
            f"presses Home. Close what you are doing on it, unlock it, leave it alone, then run again with {FLAG}.")
        return 2
    if not (1 <= a.swipes <= 50 and 50 <= a.ms <= 2000 and 1 <= a.moves <= 30 and 0 < a.moved < 1
            and (a.tap is None or 1 <= a.tap <= 20)):
        log("Refusing to run: --swipes 1-50, --ms 50-2000, --moves 1-30, --tap 1-20, --moved between 0 and 1")
        return 2
    if a.tap and decode is None:
        log("Refusing to run: --tap checks the screen before every tap, which needs PyGObject's GdkPixbuf")
        return 2
    if a.video == "usb" and not a.capture_socket:
        log("Refusing to run: --video usb needs the capture socket: --capture-socket PATH or IPHONE_CAPTURE_SOCKET\n"
            "(./iphone.sh serves it at $XDG_RUNTIME_DIR/iphone-control/cap.sock while it captures)")
        return 2
    try:
        a.settings = parse_settings(a.settings) if a.settings is not None else None
    except ValueError as e:
        log(f"Refusing to run: {e}")
        return 2
    return Probe(a, log).run()


if __name__ == "__main__":
    sys.exit(main())
