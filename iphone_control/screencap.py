"""The iPhone's screen over USB, without Developer Mode: a capture process that serves it on a socket.

  python3 -m iphone_control.screencap [--udid U] --socket PATH [--audio]
  python3 -m iphone_control.screencap [--udid U] --record OUT.h264 [--wav OUT.pcm] [--seconds N]

It switches the phone's USB to its capture configuration when needed (usbmuxd keeps its own interface in it,
so lockdown, the port forward and WebDriverAgent keep working), runs the Valeria protocol (valeria.py) on the
capture interface and hands each frame to its local clients (up to MAX_CLIENTS). usbmuxd 1.1.1 forces the phone
back to its old configuration, which has no capture interface: then this exits 3 and says so.

It stays up for the whole run and clients connect and disconnect as often as they like: the phone answers one
capture session per USB enumeration, and the USB reset that gets it a new one drops every usbmux connection
(port forward, developer tunnel, WebDriverAgent), so this starts before those and keeps its session. A locked
phone keeps streaming (never under 39 frames a second through 20 s locked; iPhone 15 Pro, iOS 26.5.2, Oct 2026),
so a picture that stops means a stuck session: a stall watchdog resets the phone and starts a new one when no
picture comes for STALL_S while a session runs, at most once per STALL_S, and gives a session whose reset brought
no picture back RETRY_S (a second reset that soon only cuts WebDriverAgent again). A restart failed when the
trouble is back within RESTART_WINDOW_S of it; after RESTARTS failed restarts in a row it gives up (exit 4).

Sound: --audio asks the phone for it (HPA1). On iOS 26.5.2 (iPhone 15 Pro, Oct 2026) the phone still plays it on
its own speaker and sends only digital silence, so it is not asked for by default (--no-audio, kept for old
command lines).

Socket frames (SOCK_STREAM, mode 0600): a "<IBq" header (payload length, kind, the time the frame arrived from the
phone in this machine's CLOCK_MONOTONIC ns), then the payload:
  1 VIDEO_FORMAT  "<II" width, height, then the avcC record (AVCDecoderConfigurationRecord); sent again when it
                  changes (2556x1180 when an app turns to landscape)
  2 VIDEO         one byte (1 = keyframe), then the access unit: NAL units with 4-byte big-endian lengths
  3 AUDIO_FORMAT  "<dII" sample rate, channels, bits per sample (signed little-endian interleaved PCM)
  4 AUDIO         PCM samples
  5 STATUS        UTF-8 text for the window to show ("" clears it)
Up to MAX_CLIENTS clients at once (the window, and tests/latency_probe.py beside it); each new one first gets the
formats, the last status and the frames since the last keyframe, so it can decode at once (with no keyframe to
replay, its pictures start at the next one). A client more than CLIENT_LAG_S behind is cut off (it reconnects and
starts again at the last keyframe).

On SIGTERM, SIGINT or SIGHUP it ends the session the way the phone expects (HPA0 if sound was on, HPD0, the
phone's RELS, HPD0 again) within about 4 s. Exit status: 0 stopped (a signal, or --seconds), 1 error (a bad
command line too), 2 the phone was unplugged, 3 capture unsupported here (usbmuxd too old), 4 no picture (stuck
even after a reset: restart the iPhone; a phone locked before the start was not tried).
"""
import argparse
import collections
import ctypes
import errno
import os
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time

from . import usbfs, valeria

VIDEO_FORMAT, VIDEO, AUDIO_FORMAT, AUDIO, STATUS = 1, 2, 3, 4, 5
HEADER = struct.Struct("<IBq")
ENABLE = (0x40, 0x52, 0, 2)          # vendor request: add the capture configuration (wIndex 2 = "Valeria" mode)
READ_SIZE = 1 << 16
SWITCH_S = 15                        # the phone re-enumerates after ENABLE; usbmuxd then picks the configuration
SETTLE_S = 5                         # ...one without the capture interface that stays this long is usbmuxd's choice
RESET_SAME_NODE_S = 3                # a reset brings the phone back as a new device (new devnum) in ~1.5 s
FIRST_MESSAGE_S = 3                  # a fresh session pings within ~0.1 s; a phone that already had one says nothing
FIRST_PICTURE_S = 3                  # ...and its first picture within ~0.2 s
FIRST_FRAME_S = 10                   # after the reset: no picture by then = stuck (exit 4)
STALL_S = 5                          # no picture this long while a session runs (stuck: a locked phone streams on)
RETRY_S = 15                         # ...but this long when its reset brought none: each reset cuts WebDriverAgent,
                                     # the tunnel and the forward, and the watchdog needs ~5 s to bring them back
RESTARTS, RESTART_WINDOW_S = 3, 60   # failed restarts in a row (trouble back within this long): give up (exit 4)
UNPLUG_S = 3                         # after a transfer error: still gone after this long = unplugged
RELEASE_S = 3                        # how long the phone gets to release its clocks after HPA0/HPD0
RELEASE_MORE_S = 0.5                 # ...and for each further RELS after the first
WRITE_MS = 1000                      # a reply the phone does not take within this ends the session
STOP_WRITE_MS = 500
POLL_S = 0.2                         # how often the watchdog looks
LOOK_S = 0.25                        # how often sysfs is read while waiting for the phone
SKIP_LOG_S = 10                      # at most one "skipped a message" line this often
REPLAY_BYTES = 4 << 20               # frames kept for a new client (since the last keyframe; ~0.5 MB at 6 Mbit/s)
CLIENT_BACKLOG = 8 << 20             # unsent bytes per client before it is cut off (at most twice the replay)
CLIENT_LAG_S = 3                     # ...or unsent frames this old (a live picture that late is useless)
MAX_CLIENTS = 4                      # the window, the latency probe beside it, and a spare


class Unsupported(OSError):
    pass


class Unplugged(OSError):
    pass


class NoPicture(Exception):
    """No picture; args: the log line and the status text, when not the ones for a phone that sends none."""


class Stopped(Exception):
    """The stop event was set while waiting for the phone."""


def frame(kind, payload, pts=0):
    return HEADER.pack(len(payload), kind, pts) + payload


def read_frames(sock):
    """Iterate (kind, pts, payload) from a client socket until it closes (for tests and the window)."""
    buf = bytearray()
    while True:
        while len(buf) < HEADER.size:
            chunk = sock.recv(1 << 16)
            if not chunk:
                return
            buf += chunk
        n, kind, pts = HEADER.unpack_from(buf)
        while len(buf) < HEADER.size + n:
            chunk = sock.recv(max(1 << 16, HEADER.size + n - len(buf)))
            if not chunk:
                return
            buf += chunk
        yield kind, pts, bytes(buf[HEADER.size:HEADER.size + n])
        del buf[:HEADER.size + n]


class StopFlag:
    """threading.Event's set(), is_set() and wait(), for main's signal handlers. Event.set() takes the lock that
    Event.wait() holds around its waits, and a handler runs in the main thread, which spends its time in those
    waits: set() there would wait for itself for ever. This set() only sets a flag and writes a byte to a pipe
    that wait() selects on (a signal during the select ends it at once): no lock, nothing that blocks."""

    def __init__(self):
        self._r, self._w = os.pipe()
        os.set_blocking(self._w, False)
        self._set = False

    def set(self, *_):                    # also the handler itself: (signum, frame)
        self._set = True
        try:
            os.write(self._w, b"\0")
        except OSError:                   # the pipe is full: set long ago
            pass

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        if not self._set:
            select.select([self._r], [], [], timeout)
        return self._set

    def close(self):
        os.close(self._r)
        os.close(self._w)


def _pause(seconds, stop):
    """time.sleep(seconds), but Stopped as soon as stop is set."""
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise Stopped()


# ---- the device --------------------------------------------------------------------------------------------
def find_phone(udid, sysfs=usbfs.SYSFS):
    """The phone's sysfs entry; Unplugged when there is none."""
    dev = usbfs.find_device(udid, sysfs)
    if dev is None:
        if not udid and len(usbfs.apple_devices(sysfs)) > 1:
            raise OSError(errno.EINVAL, "more than one iPhone on USB - choose one with --udid")
        raise Unplugged(errno.ENODEV, "no iPhone on USB")
    return dev


def _unsupported(config, offered):
    if offered:
        return Unsupported(errno.ENOTSUP, f"the iPhone offers screen capture, but usbmuxd keeps it in USB "
                                          f"configuration {config} (usbmuxd 1.1.1 does this; a newer usbmuxd is "
                                          "needed, see ./iphone.sh doctor)")
    return Unsupported(errno.ENOTSUP, "the iPhone did not offer its screen-capture configuration")


def _enable(node, opener):
    """Ask the phone for its capture configuration; False if its node vanished first (look again)."""
    try:
        h = opener(node)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENODEV):
            return False
        raise
    try:
        h.control(*ENABLE)
    except OSError:
        pass          # it often re-enumerates before it answers (EPIPE, EPROTO, ENODEV): the result shows in sysfs
    finally:
        h.close()
    return True


def wait_capture_config(udid, log, sysfs=usbfs.SYSFS, opener=usbfs.Handle, stop=None, timeout=None):
    """The phone in its capture configuration: (Device, interface number, in endpoint, out endpoint).

    Asks the phone once for the configuration if it does not offer it. Raises Unplugged when the phone is gone,
    Unsupported when it settles in a configuration without the capture interface (usbmuxd chose another one, or
    the phone has none)."""
    dev = find_phone(udid, sysfs)
    start = time.monotonic()
    end = start + (SWITCH_S if timeout is None else timeout)
    asked = state = None              # the node the request went to; what sysfs showed, since when
    since, last = start, (None, None)
    while True:
        dev = usbfs.find_device(udid, sysfs) or dev
        try:
            node, config, offered = dev.node, dev.config, dev.offers(usbfs.VALERIA)
            found = dev.find(usbfs.VALERIA) if config else None
        except (OSError, ValueError):                     # re-enumerating
            node = config = offered = found = None
        if found:
            number, eps = found
            ins, outs = [e for e in eps if e & 0x80], [e for e in eps if not e & 0x80]
            if ins and outs:
                return dev, number, ins[0], outs[0]
        now = time.monotonic()
        if (node, config, offered) != state:
            state, since = (node, config, offered), now
        if node and config:
            last = (config, offered)
        if node and not offered and asked is None:
            log("switching the iPhone's USB to its screen-capture configuration")
            if _enable(node, opener):
                asked = node
        elif node and config and now - since >= SETTLE_S and asked != node:
            raise _unsupported(config, offered)
        if now >= end:
            if usbfs.find_device(udid, sysfs) is None:
                raise Unplugged(errno.ENODEV, "the iPhone went away")
            raise _unsupported(*last)
        _pause(LOOK_S, stop)


def reset_device(udid, log, sysfs=usbfs.SYSFS, opener=usbfs.Handle, stop=None, timeout=None):
    """Re-enumerate the phone (a USB reset) and wait until it is back in its capture configuration.

    After one capture session the phone answers no new one until it enumerates again (seen on iOS 26.5): it
    sends nothing, not even its PING. A reset is the software version of replugging the cable (~1.5 s)."""
    dev = find_phone(udid, sysfs)
    log("resetting the iPhone's USB connection to start a new capture")
    try:
        old = dev.node
        h = opener(old)
    except (OSError, ValueError) as e:
        if getattr(e, "errno", errno.ENOENT) not in (errno.ENOENT, errno.ENODEV):
            raise
        old = h = None                                    # already re-enumerating
    if h is not None:
        try:
            h.reset()
        finally:
            h.close()
    start = time.monotonic()
    end = start + (SWITCH_S if timeout is None else timeout)
    while time.monotonic() < end:
        _pause(LOOK_S, stop)
        d = usbfs.find_device(udid, sysfs)
        try:
            if d and (d.node != old or time.monotonic() - start >= RESET_SAME_NODE_S) and d.config and \
                    d.find(usbfs.VALERIA):
                return
        except (OSError, ValueError):
            continue
    log("the iPhone did not come back from the reset in time")


def unplugged(udid, node, sysfs=usbfs.SYSFS, stop=None):
    """After a transfer error: True when the phone left USB and is still gone UNPLUG_S later, False when it
    stayed (the session broke) or came back as a new device (it re-enumerated)."""
    start = time.monotonic()
    while True:
        d = usbfs.find_device(udid, sysfs)
        try:
            now_node = d.node if d else None
        except (OSError, ValueError):
            now_node = None
        now = time.monotonic()
        if now_node and (now_node != node or now - start >= UNPLUG_S / 3):    # back, or never left
            return False
        if now - start >= UNPLUG_S:
            return now_node is None
        _pause(min(LOOK_S, 0.1), stop)


class Capture:
    """One capture session on the phone's capture interface.

    on_event(event) gets valeria's Video and Audio events on the reader thread. opener(node) opens the device:
    usbfs.Handle, or in the tests a fake with the same methods (control, bulk_read, bulk_write, clear_halt,
    claim, release, reset, close); sysfs is where the phone is looked up. One lock serialises every write (the
    reader's replies, close()'s goodbye). A message that cannot be read is logged and skipped; only a transfer
    error or a stream it lost track of ends the reader (error says why)."""

    def __init__(self, udid, on_event, audio=False, log=None, opener=usbfs.Handle, sysfs=usbfs.SYSFS):
        self.udid, self.on_event, self.opener, self.sysfs = udid, on_event, opener, sysfs
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.session = valeria.Session(video=True, audio=audio)
        self.usb = self.node = self.iface = self.ep_in = self.ep_out = None
        self.write_lock = threading.Lock()
        self.cond = threading.Condition()
        self.released = 0                # RELS seen
        self.alive = threading.Event()   # the phone said something (PING comes first)
        self.first = threading.Event()   # ...and sent a picture
        self.ended = threading.Event()   # the reader stopped
        self.error = None
        self.size = None                 # (width, height) of the latest video format
        self.started = self.last_video = None
        self.closing = False
        self._reader = None
        self._skipped, self._skip_logged = 0, float("-inf")

    def start(self, stop=None):
        for attempt in range(3):
            dev, self.iface, self.ep_in, self.ep_out = wait_capture_config(self.udid, self.log, self.sysfs,
                                                                           self.opener, stop)
            try:
                self.node = dev.node
                self.usb = self._open(self.node)
                break
            except (OSError, ValueError) as e:
                if getattr(e, "errno", None) not in (errno.ENOENT, errno.ENODEV, errno.ESHUTDOWN) or attempt == 2:
                    raise
                _pause(0.5, stop)                         # it re-enumerated right then: look again
        self.started = time.monotonic()
        self._reader = threading.Thread(target=self._read, name="usb-in", daemon=True)
        self._reader.start()

    def _open(self, node):
        usb = self.opener(node)
        try:
            usb.claim(self.iface)
            for ep in (self.ep_in, self.ep_out):          # no data flows before both halts are cleared
                usb.clear_halt(ep)
        except BaseException:
            usb.close()
            raise
        return usb

    def send(self, data, timeout=WRITE_MS):
        with self.write_lock:
            if self.closing:
                raise usbfs.UsbError(errno.ESHUTDOWN, "the capture is closing")
            self.usb.bulk_write(self.ep_out, data, timeout)

    def wait(self, event, seconds, stop=None):
        """event.wait(seconds), but False as soon as the reader has ended, and Stopped once stop is set."""
        end = time.monotonic() + seconds
        while not event.is_set():
            if self.ended.is_set():
                return event.is_set()
            if stop is not None and stop.is_set():
                raise Stopped()
            left = end - time.monotonic()
            if left <= 0:
                return False
            event.wait(min(left, 0.05))
        return True

    def stalled(self):
        """Seconds since the last picture (since the start before the first one)."""
        return time.monotonic() - (self.last_video or self.started or time.monotonic())

    def _read(self):
        buf = ctypes.create_string_buffer(READ_SIZE)
        pending = bytearray()
        try:
            while not self.closing:
                n = self.usb.bulk_read(self.ep_in, buf)
                pending += memoryview(buf)[:n]
                pos = 0
                while len(pending) - pos >= 4:
                    length = struct.unpack_from("<I", pending, pos)[0]
                    if not 4 <= length <= valeria.MAX_MESSAGE:
                        raise valeria.ProtocolError(f"bad message length {length}")
                    if len(pending) - pos < length:
                        break
                    msg = bytes(pending[pos + 4:pos + length])
                    pos += length
                    self._receive(msg)
                del pending[:pos]
        except Exception as e:                            # OSError: unplugged, reset, or the session broke
            if not self.closing:
                self.error = e
        finally:
            self.ended.set()
            with self.cond:
                self.cond.notify_all()

    def _receive(self, msg):
        self.alive.set()
        try:
            replies, events = self.session.receive(msg)
        except valeria.ProtocolError as e:
            return self._skip(e)
        for r in replies:
            self.send(r)
        for ev in events:
            if isinstance(ev, valeria.Released):
                with self.cond:
                    self.released += 1
                    self.cond.notify_all()
                continue
            if isinstance(ev, valeria.Dropped):
                self._skip(ev.reason)
                continue
            if isinstance(ev, valeria.Video):
                if ev.format:
                    self.size = (ev.format["width"], ev.format["height"])
                self.last_video = time.monotonic()
                self.first.set()
            try:
                self.on_event(ev)
            except Exception as e:                        # a bad frame must not end the capture
                self._skip(f"frame dropped: {e!r}")

    def _skip(self, reason):
        """Log a message that could not be used: the first, then one line per SKIP_LOG_S with the count."""
        self._skipped += 1
        now = time.monotonic()
        if now - self._skip_logged >= SKIP_LOG_S:
            more = f" (and {self._skipped - 1} more)" if self._skipped > 1 else ""
            self.log(f"skipped a message from the iPhone: {reason}{more}")
            self._skipped, self._skip_logged = 0, now

    def _wait_released(self, count, seconds):
        """Until the phone has released count clocks; False on timeout or once the reader has ended."""
        end = time.monotonic() + seconds
        with self.cond:
            while self.released < count:
                left = end - time.monotonic()
                if left <= 0 or self.ended.is_set():
                    return False
                self.cond.wait(left)
        return True

    def _goodbye(self):
        for m in self.session.stop():
            self.send(m, STOP_WRITE_MS)
        if self._wait_released(1, RELEASE_S):
            self._wait_released(2, RELEASE_MORE_S)        # one RELS per clock: two in qvh's recordings
            self.send(valeria.packet(valeria.ASYN, valeria.EMPTY, valeria.HPD0), STOP_WRITE_MS)
        elif not self.ended.is_set():
            self.log("the iPhone did not confirm the end of the capture")

    def close(self):
        """End the session the way the phone expects (else it may not stream again until it re-enumerates) and
        let go of the interface. Takes at most about RELEASE_S + 1 s, and no time once the phone is gone."""
        if self.usb is None:
            return
        try:
            if self.alive.is_set() and not self.ended.is_set():
                self._goodbye()
        except OSError as e:
            self.log(f"ending the capture: {e.strerror or e}")
        finally:
            with self.write_lock:
                self.closing = True
            self.usb.release(self.iface)                  # the kernel kills the reader's transfer: it returns
            if self._reader is not None:
                self._reader.join(0.5)
            self.usb.close()
            self.usb = None


def run(udid, sink, stop, log, audio=False, seconds=0, ready=None, opener=usbfs.Handle, sysfs=usbfs.SYSFS):
    """Capture until stop is set (or for `seconds` once it has a picture); returns the exit status.

    sink.event(ev) gets every frame (on the reader thread), sink.status(text), if it has one, what the user
    should know; ready() runs once, at the first picture."""
    status = getattr(sink, "status", lambda text: None)

    def new():
        c = Capture(udid, sink.event, audio, log, opener, sysfs)
        c.start(stop)
        return c

    def failed(c):
        """Unplugged, or the error that ended c's reader."""
        if unplugged(udid, c.node, sysfs, stop):
            return Unplugged(errno.ENODEV, "the iPhone was unplugged")
        return c.error

    cap = None
    try:
        cap = new()
        # a phone that already had a session since it enumerated stays silent, or pings and sends no picture
        if not (cap.wait(cap.alive, FIRST_MESSAGE_S, stop) and cap.wait(cap.first, FIRST_PICTURE_S, stop)):
            if cap.error is not None:
                err = failed(cap)
                if isinstance(err, Unplugged):
                    raise err
            cap.close()
            cap = None
            reset_device(udid, log, sysfs, opener, stop)
            cap = new()
            if not cap.wait(cap.first, FIRST_FRAME_S, stop):
                if cap.error is not None:
                    raise failed(cap)
                raise NoPicture()
        size = "%dx%d" % cap.size if cap.size else "the screen"
        log(f"capturing {size}" + (" with sound" if audio else ", the sound stays on the iPhone"))
        if ready:
            ready()
        end = time.monotonic() + seconds if seconds else None
        failures = 0                      # failed restarts in a row
        restarted = False                 # cap is a restart's session...
        back = True                       # ...and its picture has come ("capturing again")
        while not stop.wait(POLL_S):
            if end and time.monotonic() >= end:
                break
            if not back and cap.first.is_set():
                back = True
                log("capturing again")
                status("")
            # a session whose reset brought no picture (yet) gets RETRY_S: a second reset that soon only cuts
            # WebDriverAgent again, and the picture may still come
            limit = STALL_S if cap.first.is_set() else RETRY_S
            if cap.error is not None:
                err = failed(cap)
                if isinstance(err, Unplugged):
                    raise err
                why = f"the capture stopped ({getattr(err, 'strerror', None) or err})"
            elif cap.stalled() >= limit:
                why = f"no picture from the iPhone for {limit:g} s"
            else:
                continue
            # A restart failed when the trouble is back within RESTART_WINDOW_S of it, picture or not: a session
            # that breaks right after its first picture must not reset the phone (and its usbmux connections) in a
            # loop. One that lasted longer ends the row.
            lasted = cap.first.is_set() and time.monotonic() - cap.started > RESTART_WINDOW_S
            failures = failures + 1 if restarted and not lasted else 0
            if failures >= RESTARTS:
                log(f"{why}, again")
                raise NoPicture("the iPhone stopped sending its screen and new captures do not bring it back - "
                                "restart the iPhone",
                                "The iPhone stopped sending its screen - if it stays black, restart the iPhone.")
            log(f"{why} - starting a new capture")
            status("The iPhone stopped sending its screen - starting a new capture...")
            cap.close()
            _pause(max(0.0, cap.started + limit - time.monotonic()), stop)    # resets at least `limit` apart
            cap = None
            reset_device(udid, log, sysfs, opener, stop)
            cap = new()
            restarted, back = True, False
        return 0
    except Stopped:
        return 0
    except NoPicture as e:
        line, text = e.args or ("the iPhone sends no picture - unlock it; if it stays black, restart the iPhone",
                                "No picture from the iPhone - unlock it; if it stays black, restart the iPhone.")
        log(line)
        status(text)
        return 4
    except Unsupported as e:
        log(e.strerror or str(e))
        return 3
    except Unplugged as e:
        log(e.strerror or str(e))
        return 2
    except (OSError, valeria.ProtocolError) as e:
        log(f"error: {getattr(e, 'strerror', None) or e}")
        return 1
    finally:
        if cap is not None:
            cap.close()


# ---- the server --------------------------------------------------------------------------------------------
def _kill(s):
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    s.close()


def listen(path):
    """A listening UNIX socket at path, mode 0600 from the start. A socket left by an earlier run is replaced;
    any other file there is an error."""
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise OSError(errno.EEXIST, "it exists and is not a socket")
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        s.bind(path)
    except BaseException:
        s.close()
        raise
    finally:
        os.umask(old)
    s.listen(4)
    return s


class _Client:
    """One connected client: its own queue and send thread, so a slow client never holds up the others."""

    def __init__(self, sock, keyless):
        self.sock = sock
        self.queue = collections.deque()  # (frame, time queued)
        self.queued = 0
        self.keyless = keyless            # no keyframe sent yet: P frames would only decode as garbage
        self.gone = False

    def push(self, data, now):
        self.queue.append((data, now))
        self.queued += len(data)


class Server:
    """Serves frames on a UNIX socket to up to MAX_CLIENTS clients (the window, and the latency probe beside it),
    each starting with a replay of the format and the frames since the last keyframe.

    event() and status() come from the reader thread; an accept thread takes clients, and each client has a send
    thread, so a slow client never holds up the phone or the other clients. A client that falls CLIENT_LAG_S or
    CLIENT_BACKLOG behind is cut off (it reconnects and starts again at the last keyframe)."""

    def __init__(self, path, log):
        self.path, self.log = path, log
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.clients = []
        self.closed = False
        self.video_format = self.audio_format = self.last_status = None
        self.gop = []                     # frames since the last keyframe
        self.gop_bytes = 0
        self.sock = listen(path)
        self._inode = os.stat(path).st_ino
        threading.Thread(target=self._accept, name="cap-accept", daemon=True).start()

    def _accept(self):
        while True:
            try:
                s, _ = self.sock.accept()
            except OSError as e:
                if self.closed:
                    return
                self.log(f"capture socket: {e.strerror or e}")      # out of file descriptors and the like
                time.sleep(0.5)
                continue
            s.settimeout(CLIENT_LAG_S)    # a send stuck for this long ends the client too
            with self.lock:
                if self.closed or len(self.clients) >= MAX_CLIENTS:
                    c = None
                else:
                    c = _Client(s, keyless=not self.gop)
                    now = time.monotonic()
                    for f in [self.video_format, self.audio_format, self.last_status] + self.gop:
                        if f:
                            c.push(f, now)
                    self.clients.append(c)
            if c is None:
                if not self.closed:
                    self.log(f"capture socket: more than {MAX_CLIENTS} clients - refusing one")
                _kill(s)
                continue
            threading.Thread(target=self._send, args=(c,), name="cap-send", daemon=True).start()

    def _send(self, c):
        while True:
            with self.lock:
                while not (self.closed or c.gone or c.queue):
                    self.cond.wait()
                if self.closed or c.gone:
                    return
                data, _ = c.queue.popleft()
                c.queued -= len(data)
            try:
                c.sock.sendall(data)
            except OSError:               # gone, or stuck for CLIENT_LAG_S (socket.timeout)
                self._drop(c)
                return

    def _drop(self, c, why=None):
        with self.lock:
            if c.gone:
                return
            c.gone = True
            c.queue.clear()
            c.queued = 0
            self.clients.remove(c)
            self.cond.notify_all()
        if why:
            self.log(why)
        _kill(c.sock)

    def _deliver(self, frames, video=None):
        """Queue frames for every client (lock held); returns the clients to cut off for falling behind.
        video: (frame, is a keyframe) for a picture, which a client without a keyframe yet skips."""
        slow, now = [], time.monotonic()
        for c in self.clients:
            if c.queue and (now - c.queue[0][1] > CLIENT_LAG_S or c.queued > CLIENT_BACKLOG):
                slow.append(c)
                continue
            for f in frames:
                c.push(f, now)
            if video is not None:
                c.keyless = c.keyless and not video[1]
                if not c.keyless:
                    c.push(video[0], now)
        self.cond.notify_all()
        return slow

    def event(self, ev):
        now = time.monotonic_ns()
        out, pic = [], None
        with self.lock:
            if isinstance(ev, valeria.Video):
                f = ev.format
                if f and f.get("avcc"):
                    fmt = frame(VIDEO_FORMAT, struct.pack("<II", f["width"], f["height"]) + f["avcc"], now)
                    if self.video_format is None or fmt[HEADER.size:] != self.video_format[HEADER.size:]:
                        self.gop, self.gop_bytes = [], 0          # frames before it belong to the old format
                        out.append(fmt)
                    self.video_format = fmt
                if ev.data:
                    v = b"".join((HEADER.pack(len(ev.data) + 1, VIDEO, now), b"\x01" if ev.keyframe else b"\x00",
                                  ev.data))
                    if ev.keyframe or self.gop_bytes + len(v) > min(REPLAY_BYTES, CLIENT_BACKLOG // 2):
                        self.gop, self.gop_bytes = [], 0
                    if ev.keyframe or self.gop:
                        self.gop.append(v)
                        self.gop_bytes += len(v)
                    pic = (v, ev.keyframe)
            elif isinstance(ev, valeria.Audio):
                a = ev.asbd or valeria.default_asbd()
                fmt = frame(AUDIO_FORMAT, struct.pack("<dII", a["sample_rate"], a["channels"], a["bits"]))
                if fmt != self.audio_format:
                    self.audio_format = fmt
                    out.append(fmt)
                out.append(frame(AUDIO, ev.data, now))
            slow = self._deliver(out, pic)
        for c in slow:
            self._drop(c, "capture client too slow - disconnecting it")

    def status(self, text):
        f = frame(STATUS, text.encode(), time.monotonic_ns())
        with self.lock:
            self.last_status = f if text else None
            slow = self._deliver([f])
        for c in slow:
            self._drop(c, "capture client too slow - disconnecting it")

    def close(self):
        with self.lock:
            self.closed = True
            clients, self.clients = self.clients, []
            for c in clients:
                c.gone = True
            self.cond.notify_all()
        _kill(self.sock)                  # shutdown() is what wakes the accept thread
        for c in clients:
            _kill(c.sock)
        try:
            if os.stat(self.path).st_ino == self._inode:      # not a socket another process has put there since
                os.unlink(self.path)
        except OSError:
            pass


class Recorder:
    """--record: Annex-B H.264 (and raw PCM) files, for tests and checks."""

    def __init__(self, h264, pcm=None):
        self.h264 = open(h264, "wb")
        self.pcm = None
        try:
            self.pcm = open(pcm, "wb") if pcm else None
        except OSError:
            self.h264.close()
            raise
        self.frames = self.keyframes = 0

    def event(self, ev):
        if isinstance(ev, valeria.Video):
            if ev.format and ev.format.get("avcc"):
                for unit in valeria.avcc_parameter_sets(ev.format["avcc"]):
                    self.h264.write(b"\x00\x00\x00\x01" + unit)
            if ev.data:
                for unit in valeria.nal_units(ev.data):
                    self.h264.write(b"\x00\x00\x00\x01" + unit)
                self.frames += 1
                self.keyframes += ev.keyframe
        elif isinstance(ev, valeria.Audio) and self.pcm:
            self.pcm.write(ev.data)

    def close(self):
        self.h264.close()
        if self.pcm:
            self.pcm.close()


class _Args(argparse.ArgumentParser):
    def error(self, message):                             # argparse's 2 means "unplugged" here
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def main(argv=None, opener=usbfs.Handle, sysfs=usbfs.SYSFS):
    """The command line. opener and sysfs let the tests put a fake phone behind it; nothing outside the code
    can set them."""
    ap = _Args(prog="python3 -m iphone_control.screencap",
               description="The iPhone's screen over USB (no Developer Mode needed).")
    ap.add_argument("--udid", default=os.environ.get("IPHONE_UDID"))
    ap.add_argument("--socket", help="serve frames on this UNIX socket")
    ap.add_argument("--record", metavar="OUT.h264", help="write Annex-B H.264 instead")
    ap.add_argument("--wav", metavar="OUT.pcm", help="with --record: also the sound, raw 48 kHz s16le stereo")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (from the first picture)")
    sound = ap.add_mutually_exclusive_group()
    sound.add_argument("--audio", action="store_true",
                       help="also ask the iPhone for its sound (iOS 26.5 keeps it on its speaker and sends silence)")
    sound.add_argument("--no-audio", dest="audio", action="store_false", help="do not ask for the sound (default)")
    a = ap.parse_args(argv)
    if bool(a.socket) == bool(a.record):
        ap.error("give either --socket or --record")
    if a.wav and not a.record:
        ap.error("--wav needs --record")
    if a.seconds < 0:
        ap.error("--seconds must not be negative")
    log = lambda m: print(f"screencap: {m}", file=sys.stderr, flush=True)  # noqa: E731

    def ready():
        try:
            print("capturing", flush=True)
        except OSError:
            pass                          # nobody reads stdout

    stop = StopFlag()
    handlers = {sig: signal.signal(sig, stop.set) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    sink = None
    try:
        try:
            sink = Recorder(a.record, a.wav) if a.record else Server(a.socket, log)
        except OSError as e:
            log(f"cannot write {a.record or a.socket}: {e.strerror or e}")
            return 1
        rc = run(a.udid, sink, stop, log, audio=a.audio or bool(a.wav), seconds=a.seconds,
                 ready=ready if a.socket else None, opener=opener, sysfs=sysfs)
    finally:
        if sink is not None:
            sink.close()
        for sig, h in handlers.items():               # before the pipe closes: its number may be reused
            signal.signal(sig, signal.SIG_DFL if h is None else h)
        stop.close()
    if isinstance(sink, Recorder):
        log(f"recorded {sink.frames} frames ({sink.keyframes} keyframes)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
