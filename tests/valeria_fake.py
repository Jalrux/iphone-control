"""A fake iPhone for the screen-capture tests: the device side of the Valeria protocol behind fake usbfs handles,
and a fake sysfs tree. Nothing here touches real USB, usbmuxd or a phone.

FakeSysfs() writes device directories the way /sys/bus/usb/devices has them (idVendor, idProduct, serial, busnum,
devnum, bConfigurationValue, bNumConfigurations and the raw descriptors, built here). FakePhone(sysfs) is one
iPhone in it, and phone.open is what screencap takes as its opener. The phone speaks from synthetic messages only
(no real screen content): PING, SYNC CWPA/AFMT/CVRP/CLOK/TIME/SKEW/OG/STOP, ASYN SPRP/TBAS, FEED (a
format description with a made-up avcC on the first one, NAL units that carry a frame counter), EAT! (silence),
and RELS after HPD0. It answers one session per enumeration, like the real one, sends a FEED only for a NEED, and
checks every message the host sends (problems; timeline has both sides, without the FEEDs and NEEDs).

Run as a script it runs screencap.main against a FakePhone in its own process, so the tests can send signals:
  python3 -I tests/valeria_fake.py SCENARIO.json -- SCREENCAP ARGS...
SCENARIO: {"report": file for phone.report() at the end, "phone": FakePhone knobs, "timing": screencap constants,
           "unplug_after": seconds after the first picture, or null}
"""
import contextlib
import ctypes
import errno
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from support import UDID_A  # noqa: E402
from iphone_control import screencap, usbfs, valeria  # noqa: E402
from iphone_control.valeria import _block as block  # noqa: E402

AUDIO_REF, VIDEO_REF, STOP_REF = 0x11A5E0000, 0x11B6F0000, 0x11C700000   # the phone's clocks (made up)
PTP, AUDIO_CONTROL, AUDIO_STREAM = (0x06, 0x01, 0x01), (0x01, 0x01, 0x00), (0x01, 0x02, 0x00)
CDC_NCM, CDC_DATA = (0x02, 0x0D, 0x00), (0x0A, 0x00, 0x01)
VALERIA_IN, VALERIA_OUT = 0x85, 0x04
# screencap's waits for the tests: long enough that a busy test machine does not stall a fake streaming at 200 fps
FAST = {"SWITCH_S": 3, "SETTLE_S": 0.5, "RESET_SAME_NODE_S": 0.5, "FIRST_MESSAGE_S": 1, "FIRST_PICTURE_S": 1,
        "FIRST_FRAME_S": 2, "STALL_S": 0.6, "RETRY_S": 1.5, "UNPLUG_S": 0.5, "RELEASE_S": 1, "RELEASE_MORE_S": 0.1,
        "POLL_S": 0.02, "LOOK_S": 0.02}


@contextlib.contextmanager
def fast_timing(**more):
    """screencap's waits shortened for the tests (FAST, plus any overrides)."""
    values = dict(FAST, **more)
    old = {k: getattr(screencap, k) for k in values}
    for k, v in values.items():
        setattr(screencap, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(screencap, k, v)


# ---- descriptors -------------------------------------------------------------------------------------------
def endpoint(address, kind=2, size=512):
    """An endpoint descriptor; kind 2 = bulk, 3 = interrupt."""
    return struct.pack("<BBBBHB", 7, 5, address, kind, size, 0 if kind == 2 else 8)


def interface(number, triple, eps=(), alt=0):
    """An interface descriptor and its endpoints (addresses, or (address, kind))."""
    eps = [e if isinstance(e, tuple) else (e, 2) for e in eps]
    return struct.pack("<9B", 9, 4, number, alt, len(eps), *triple, 0) + b"".join(endpoint(*e) for e in eps)


def configuration(value, *parts):
    body = b"".join(parts)
    i, numbers = 0, set()
    while i < len(body):                                  # bNumInterfaces counts interfaces, not settings
        if body[i + 1] == 4:
            numbers.add(body[i + 2])
        i += body[i]
    return struct.pack("<BBHBBBBB", 9, 2, 9 + len(body), len(numbers), value, 0, 0xC0, 250) + body


def device_descriptor(num_configs, product=0x12A8, vendor=usbfs.APPLE):
    return struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0, 0, 0, 64, vendor, product, 0x1610, 1, 2, 3,
                       num_configs)


def ncm(first):
    """CDC-NCM: an association, the control interface with its class descriptors before its interrupt
    endpoint, and the data interface, whose endpoints are in alternate setting 1 only."""
    iad = struct.pack("<8B", 8, 0x0B, first, 2, *CDC_NCM, 0)
    control = struct.pack("<9B", 9, 4, first, 0, 1, *CDC_NCM, 0)
    functional = bytes([5, 0x24, 0, 0x10, 1]) + bytes([5, 0x24, 6, first, first + 1]) + \
        bytes([13, 0x24, 0x0F, 4, 0, 0, 0, 0, 0xEA, 5, 0, 0, 0])
    return iad + control + functional + endpoint(0x88, 3, 64) + interface(first + 1, CDC_DATA) + \
        interface(first + 1, CDC_DATA, (0x89, 0x0A), alt=1)


def iphone_descriptors(num_configs=5, product=0x12A8):
    """An iPhone's device descriptor and configurations: 1 PTP, 2 PTP + audio, 3 PTP + usbmux, 4 PTP + usbmux +
    CDC-NCM, and with the capture configuration 5 PTP + usbmux + Valeria + CDC-NCM."""
    ptp = interface(0, PTP, (0x81, 0x02, (0x83, 3)))
    mux = interface(1, usbfs.USBMUX, (0x84, 0x03))
    configs = [configuration(1, ptp),
               configuration(2, ptp, interface(1, AUDIO_CONTROL), interface(2, AUDIO_STREAM),
                             interface(2, AUDIO_STREAM, ((0x86, 1),), alt=1)),
               configuration(3, ptp, mux),
               configuration(4, ptp, mux, ncm(2)),
               configuration(5, ptp, mux, interface(2, usbfs.VALERIA, (VALERIA_IN, VALERIA_OUT)), ncm(3))]
    return device_descriptor(num_configs, product) + b"".join(configs[:num_configs])


class FakeSysfs:
    """A /sys/bus/usb/devices look-alike in a temp dir, with a root hub, a mouse and an interface directory that
    must all be ignored. Directories appear and vanish whole (written aside, then renamed)."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="fakesysfs-")
        self.root = os.path.join(self.dir, "devices")
        os.mkdir(self.root)
        self.write("usb1", idVendor="1d6b", idProduct="0002", serial="0000:00:14.0", busnum=1, devnum=1,
                   bConfigurationValue=1, bNumConfigurations=1, descriptors=device_descriptor(1, 2, 0x1D6B))
        self.write("1-1", idVendor="046d", idProduct="c52b", serial="", busnum=1, devnum=2, bConfigurationValue=1,
                   bNumConfigurations=1, descriptors=device_descriptor(1, 0xC52B, 0x046D))
        os.mkdir(os.path.join(self.root, "1-1:1.0"))

    def write(self, name, **attrs):
        stage = tempfile.mkdtemp(dir=self.dir)
        for k, v in attrs.items():
            with open(os.path.join(stage, k), "wb") as f:
                f.write(v if isinstance(v, bytes) else f"{v}\n".encode())
        self.remove(name)
        os.rename(stage, os.path.join(self.root, name))

    def remove(self, name):
        path = os.path.join(self.root, name)
        if os.path.isdir(path):
            gone = tempfile.mkdtemp(dir=self.dir)
            os.rename(path, os.path.join(gone, name))
            shutil.rmtree(gone, ignore_errors=True)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ---- synthetic media ---------------------------------------------------------------------------------------
def index_dict(magic, d):
    """A dictionary with uint16 index keys (idxk), as format descriptions and sample attachments use."""
    entries = b"".join(block(valeria.KEYV, block(valeria.IDXK, struct.pack("<H", k)) + value(v)) for k, v in d.items())
    return block(magic, entries)


def value(v):
    return index_dict(valeria.DICT, v) if isinstance(v, dict) and v and isinstance(next(iter(v)), int) \
        else valeria.pack_value(v)


def make_avcc(width, height):
    """A made-up AVCDecoderConfigurationRecord: High profile, 4-byte lengths, one SPS (carrying the size, so a
    rotation changes it), one PPS."""
    sps = bytes([0x67, 0x64, 0x00, 0x33]) + struct.pack(">HH", width, height)
    pps = bytes([0x68, 0xEE, 0x3C, 0xB0])
    return bytes([1, 0x64, 0x00, 0x33, 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps + b"\x01" + \
        struct.pack(">H", len(pps)) + pps


def video_format(width, height):
    return block(valeria.FDSC, block(valeria.MDIA, struct.pack("<I", valeria.VIDE)) +
                 block(valeria.VDIM, struct.pack("<II", width, height)) +
                 block(valeria.CODC, struct.pack("<I", 0x61766331)) +
                 index_dict(valeria.EXTN, {valeria.AVCC_KEY: {valeria.AVCC_SUBKEY: make_avcc(width, height)},
                                           52: "H.264"}))


def audio_format():
    asbd = struct.pack("<dIIIIIIII", 48000.0, valeria.LPCM, 76, 4, 1, 4, 2, 16, 0)
    return block(valeria.FDSC, block(valeria.MDIA, struct.pack("<I", valeria.SOUN)) + block(valeria.ASBD, asbd))


def access_unit(counter, keyframe):
    """NAL units with 4-byte lengths: an SEI, then an IDR (keyframe) or a P slice, both carrying counter."""
    units = [bytes([0x06, 0x05]) + struct.pack(">I", counter),
             bytes([0x65 if keyframe else 0x41]) + struct.pack(">I", counter) + bytes(counter % 7)]
    return b"".join(struct.pack(">I", len(u)) + u for u in units)


def counter_of(au):
    """The frame counter access_unit() put in its slice (for a VIDEO frame: the payload after its keyframe byte)."""
    unit = next(u for u in valeria.nal_units(au) if u[0] & 0x1F in (1, valeria.IDR))
    return struct.unpack(">I", unit[1:5])[0]


def sample_buffer(data, pts, scale, fmt=None, samples=1):
    t = valeria.pack_time(pts, scale)
    parts = [block(valeria.OPTS, t), block(valeria.STIA, valeria.pack_time(1, 60) + t + valeria.pack_time(0, 0, 0)),
             block(valeria.SDAT, data), block(valeria.NSMP, struct.pack("<I", samples)),
             block(valeria.SSIZ, struct.pack("<I", len(data)))]
    if fmt:
        parts.append(fmt)
    parts.append(index_dict(valeria.SATT, {28: {35: False, 46: True}, 30: valeria.Number(0)}))
    parts.append(block(valeria.SARY, index_dict(valeria.DICT, {4: False})))
    return block(valeria.SBUF, b"".join(parts))


def feed(counter, keyframe, fmt=None):
    sbuf = sample_buffer(access_unit(counter, keyframe), counter * 16_666_667, 1_000_000_000, fmt)
    return valeria.packet(valeria.ASYN, VIDEO_REF, valeria.FEED, sbuf)


def eat(counter, fmt=False):
    sbuf = sample_buffer(bytes(4096), counter * 1024, 48000, audio_format() if fmt else None, samples=1024)
    return valeria.packet(valeria.ASYN, AUDIO_REF, valeria.EAT, sbuf)


def sync(ref, kind, corr, payload=b""):
    return valeria.packet(valeria.SYNC, ref, kind, struct.pack("<Q", corr) + payload)


# ---- the phone ---------------------------------------------------------------------------------------------
class FakeSession:
    """One capture session, on the phone's side. mode: "fresh" (streams), "silent" (says nothing: the phone had
    a session since it enumerated). first_after: seconds from its start before the first picture may go."""

    def __init__(self, phone, handle, mode, pictures, first_after=0):
        self.phone, self.handle, self.mode, self.pictures = phone, handle, mode, pictures
        self.begun, self.first_after, self.first_at = time.monotonic(), first_after, None
        self.cond = threading.Condition()
        self.out = bytearray()            # to the host, not read yet
        self.err = None                   # errno for the host's reads once the transfer is killed
        self.timeline, self.replies = [], {}
        self.credits = self.needs = self.frames = self.audio_frames = 0
        self.audio = False                # the host asked for sound
        self.hpd0 = 0
        self.host_audio_clock = self.host_video_clock = None
        self._asked = {}                  # correlation id -> what was asked
        self._corr = 0x200000000
        self._buf = bytearray()
        threading.Thread(target=self._run, name="fake-phone", daemon=True).start()

    # -- the wire
    def wake(self, err):
        with self.cond:
            if self.err is None:
                self.err = err
            self.cond.notify_all()

    def read(self, size):
        with self.cond:
            while not self.out and self.err is None:
                self.cond.wait()
            if self.err is not None:
                raise usbfs.UsbError(self.err, os.strerror(self.err))
            n = min(size, len(self.out), self.phone.chunk or size)
            data = bytes(self.out[:n])
            del self.out[:n]
            return data

    def send(self, msg, note=None):
        with self.cond:
            if self.err is not None:
                return False
            self.out += msg
            if note:
                self.timeline.append("phone:" + note)
            self.cond.notify_all()
            return True

    def from_host(self, data):
        self._buf += data
        while len(self._buf) >= 4:
            n = struct.unpack_from("<I", self._buf)[0]
            if n < 8 or len(self._buf) < n:
                if n < 8:
                    self.problem(f"bad host message length {n}")
                    self._buf.clear()
                return
            msg, self._buf = bytes(self._buf[4:n]), self._buf[n:]
            self._host_message(msg)

    def problem(self, text):
        self.phone.problems.append(text)

    def _host_message(self, msg):
        magic = struct.unpack_from("<I", msg)[0]
        with self.cond:
            if magic == valeria.PING:
                if struct.pack("<I", 16) + msg != valeria.ping():
                    self.problem(f"bad PING reply {msg.hex()}")
                self.timeline.append("PING")
            elif magic == valeria.RPLY:
                corr = struct.unpack_from("<Q", msg, 4)[0]
                self.replies[corr] = msg[16:]
                if msg[12:16] != bytes(4):
                    self.problem("RPLY without its zero field")
                self.timeline.append("RPLY:" + self._asked.get(corr, "?"))
            elif magic == valeria.ASYN:
                ref, kind = struct.unpack_from("<QI", msg, 4)
                self._asyn(ref, kind, msg[16:])
            else:
                self.problem(f"unknown host message {msg[:4]!r}")
            self.cond.notify_all()

    def _asyn(self, ref, kind, body):
        name = valeria._name(kind).upper()
        if kind == valeria.NEED:
            if ref != VIDEO_REF:
                self.problem(f"NEED for clock {ref:x}, not the video clock")
            self.needs += 1
            self.credits += 1
            return
        self.timeline.append(name)
        if kind == valeria.HPD1:
            want = {"Valeria": True, "HEVCDecoderSupports444": True,
                    "DisplaySize": {"Width": valeria.Number(1920.0), "Height": valeria.Number(1200.0)}}
            if ref != valeria.EMPTY or valeria.parse_dict(body) != want:
                self.problem(f"unexpected HPD1 {body.hex()}")
        elif kind == valeria.HPA1:
            d = valeria.parse_dict(body)
            if ref != AUDIO_REF or d.get("deviceUID") != "Valeria" or len(d.get("formats", b"")) != 56:
                self.problem(f"unexpected HPA1 {d}")
            self.audio = True
        elif kind == valeria.HPA0:
            if ref != AUDIO_REF:
                self.problem(f"HPA0 for clock {ref:x}, not the audio clock")
            if not self.audio:
                self.problem("HPA0 without HPA1")
        elif kind == valeria.HPD0:
            if ref != valeria.EMPTY:
                self.problem(f"HPD0 with clock {ref:x}")
            self.hpd0 += 1
            if self.phone.unplug_on == "HPD0":
                threading.Thread(target=self.phone.unplug, daemon=True).start()
        else:
            self.problem(f"unexpected ASYN {name}")

    # -- the device script
    def _ask(self, ref, kind, payload=b"", wait=2.0):
        """Send a SYNC request; its reply payload, or None (a problem) when none comes."""
        self._corr += 1
        corr = self._corr
        self._asked[corr] = "OG" if kind == valeria.OG else valeria._name(kind).upper()      # OG is "go! "
        if not self.send(sync(ref, kind, corr, payload), self._asked[corr]):
            return None
        end = time.monotonic() + wait
        with self.cond:
            while corr not in self.replies and self.err is None:
                left = end - time.monotonic()
                if left <= 0:
                    self.problem(f"no reply to {self._asked[corr]}")
                    return None
                self.cond.wait(left)
            return self.replies.get(corr)

    def _until(self, test, wait):
        end = time.monotonic() + wait
        with self.cond:
            while not test() and self.err is None:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
            return test()

    def _run(self):
        try:
            if self.mode == "silent":
                return
            self._script()
        except Exception as e:                            # a bug in the fake shows as a problem
            self.problem(f"fake phone failed: {e!r}")

    def _script(self):
        if not self.send(valeria.ping(), "PING") or not self._until(lambda: "PING" in self.timeline, 2):
            if self.err is None:
                self.problem("no PING reply")
            return
        r = self._ask(valeria.EMPTY, valeria.CWPA, struct.pack("<Q", AUDIO_REF))
        if r is None:
            return
        self.host_audio_clock = struct.unpack("<Q", r)[0]
        at = self.timeline.index("RPLY:CWPA")
        if self.timeline[at - 2:at] != ["HPD1", "HPD1"]:
            self.problem(f"no HPD1 twice before the CWPA reply: {self.timeline}")
        r = self._ask(valeria.EMPTY, valeria.CVRP, struct.pack("<Q", VIDEO_REF) + valeria.pack_dict({"Valeria": True}))
        if r is None:
            return
        self.host_video_clock = struct.unpack("<Q", r)[0]
        if not self._until(lambda: self.credits, 1):
            return self.problem("no NEED after the CVRP reply")
        r = self._ask(self.host_video_clock, valeria.CLOK)
        if r is None or len(r) != 8:
            return self.problem(f"bad CLOK reply {r!r}")
        clock = struct.unpack("<Q", r)[0]
        last = -1
        for _ in range(2):
            r = self._ask(clock, valeria.TIME)
            if r is None or len(r) != 24:
                return self.problem(f"bad TIME reply {r!r}")
            t, scale, flags, epoch = valeria.parse_time(r)
            if scale != 1_000_000_000 or flags != 1 or epoch != 0 or t < last:
                self.problem(f"bad TIME {t} {scale} {flags} {epoch}")
            last = t
        self.send(valeria.packet(valeria.ASYN, VIDEO_REF, valeria.SPRP, block(valeria.KEYV, block(
            valeria.STRK, b"ObeyEmptyMediaMarkers") + valeria.pack_value(True))), "SPRP")
        self.send(valeria.packet(valeria.ASYN, VIDEO_REF, valeria.TBAS, struct.pack("<Q", STOP_REF)), "TBAS")
        if self.audio:
            r = self._ask(self.host_audio_clock, valeria.AFMT, struct.pack("<dIIIIIIII", 48000.0, valeria.LPCM,
                                                                           76, 4, 1, 4, 2, 16, 0))
            if r is None or valeria.parse_dict(r) != {"Error": valeria.Number(0)}:
                self.problem(f"bad AFMT reply {r!r}")
        self._stream()

    def _stream(self):
        p = self.phone
        size, sent_og, n = p.size, False, 0
        while not self.hpd0 and self.err is None:
            time.sleep(1 / p.fps)
            n += 1
            allowed = (self.pictures is None or self.frames < self.pictures) and \
                time.monotonic() - self.begun >= self.first_after
            if allowed and self.credits and p.video:
                fmt = None
                if p.rotate_at is not None and self.frames == p.rotate_at:
                    size = (size[1], size[0])
                    fmt = video_format(*size)
                elif self.frames == 0:
                    fmt = video_format(*size)
                key = fmt is not None or self.frames % p.keyframe_every == 0
                with self.cond:
                    self.credits -= 1
                if not self.send(feed(p.counter, key, fmt)):
                    return
                p.counter += 1
                self.frames += 1
                self.first_at = self.first_at or time.monotonic()
                if self.frames == 1 and len(p.sessions) <= p.garbage_sessions:
                    for g in p.garbage:
                        self.send(g, "garbage")
            if self.audio and p.audio_samples and n % 4 == 0:
                self.send(eat(self.audio_frames, fmt=self.audio_frames == 0))
                self.audio_frames += 1
                if self.audio_frames % 8 == 0:
                    r = self._ask(self.host_audio_clock, valeria.SKEW)
                    if r is None or len(r) != 8:
                        return self.problem(f"bad SKEW reply {r!r}")
            if not sent_og and self.frames >= 3:
                sent_og = True
                if self._ask(STOP_REF, valeria.OG, struct.pack("<I", 1)) != bytes(4):
                    self.problem("bad OG reply")
        if self.err is not None:
            return
        if self._ask(STOP_REF, valeria.STOP) != bytes(4):
            self.problem("bad STOP reply")
        for ref in (VIDEO_REF, STOP_REF)[:p.rels]:
            self.send(valeria.packet(valeria.ASYN, ref, valeria.RELS), "RELS")
        if p.rels and not self._until(lambda: self.hpd0 >= 2, 2) and self.err is None:
            self.problem("no HPD0 after the RELS")


class FakeHandle:
    """What screencap gets from phone.open(node): usbfs.Handle's methods, on the fake phone."""

    def __init__(self, phone, node):
        self.phone, self.node, self.devnum = phone, node, phone.devnum
        self.claimed, self.cleared = set(), set()
        self.session = None
        self.closed = False

    def _check(self):
        if self.closed:
            raise usbfs.UsbError(errno.EBADF, "the USB device is closed")
        if not self.phone.present or self.phone.devnum != self.devnum:
            raise usbfs.UsbError(errno.ENODEV, os.strerror(errno.ENODEV))

    def dead(self):
        """The device went away: a transfer in flight fails with ESHUTDOWN, later calls with ENODEV."""
        if self.session:
            self.session.wake(errno.ESHUTDOWN)

    def control(self, request_type, request, value=0, index=0, data=b"", length=None, timeout=1000):
        self._check()
        self.phone.controls.append((request_type, request, value, index))
        if (request_type, request, value, index) == screencap.ENABLE:
            self.phone.enable()
            raise usbfs.UsbError(errno.EPIPE, os.strerror(errno.EPIPE))     # it re-enumerates before answering
        return 0

    def claim(self, number):
        self._check()
        if number not in usbfs.interfaces(self.phone.descriptors(), self.phone.config):
            raise usbfs.UsbError(errno.EINVAL, os.strerror(errno.EINVAL))
        self.claimed.add(number)

    def release(self, number):
        self.claimed.discard(number)
        if self.session:
            self.session.wake(errno.ESHUTDOWN)            # the kernel kills the interface's transfers

    def clear_halt(self, ep):
        self._check()
        self.cleared.add(ep)
        if {VALERIA_IN, VALERIA_OUT} <= self.cleared and 2 in self.claimed and self.session is None:
            self.session = self.phone.new_session(self)

    def bulk_read(self, ep, buf, timeout=0):
        self._check()
        if ep != VALERIA_IN or 2 not in self.claimed or self.session is None:
            raise usbfs.UsbError(errno.EINVAL, f"read from endpoint {ep:#x} before the session")
        data = self.session.read(ctypes.sizeof(buf))
        ctypes.memmove(buf, data, len(data))
        return len(data)

    def bulk_write(self, ep, data, timeout=2000):
        self._check()
        if ep != VALERIA_OUT or self.session is None:
            raise usbfs.UsbError(errno.EINVAL, f"write to endpoint {ep:#x} before the session")
        self.phone.write_timeouts.add(timeout)
        deaf = self.phone.deaf
        if deaf is True or deaf and len(data) >= 20 and valeria._name(struct.unpack_from("<I", data, 16)[0]).upper() \
                in deaf:
            time.sleep(min(timeout, 50) / 1000)
            raise usbfs.UsbError(errno.ETIMEDOUT, os.strerror(errno.ETIMEDOUT))
        self.session.from_host(bytes(data))
        return len(data)

    def reset(self):
        self._check()
        self.phone.reset()

    def close(self):
        for n in list(self.claimed):
            self.release(n)
        self.closed = True


class FakePhone:
    """One iPhone in a FakeSysfs. Knobs (attributes, also changeable while it runs):
      fresh          answers the next session (False: it already had one since it enumerated, so it is silent)
      pictures       pictures per session, the last entry repeating (None = no limit; 0 = a stuck session)
      first_after    seconds from each session's start to its first picture, the last entry repeating
      configs/config bNumConfigurations / bConfigurationValue at the start (5/5: usbmuxd keeps capture on)
      on_enable      what the ENABLE request does: "capture" (re-enumerates in configuration 5), "force4" (offers 5,
                     usbmuxd 1.1.1 puts it back in 4) or "ignore"
      rels           RELS it sends after HPD0 (0 = it never confirms)
      deaf           host writes time out (True, or a set of ASYN names: {"HPD0"}); unplug_on: "HPD0" = vanish
                     when the host sends it
      rotate_at      the picture number at which it turns to landscape
      garbage        messages sent after the first picture, in the first garbage_sessions sessions
      chunk          at most this many bytes per host read (None: as much as fits)"""

    def __init__(self, sysfs, udid=UDID_A, name="1-2", devnum=5, configs=5, config=5, fresh=True, pictures=None,
                 on_enable="capture", rels=2, fps=200, keyframe_every=10, size=(1180, 2556), chunk=None,
                 rotate_at=None, garbage=(), garbage_sessions=1, deaf=False, unplug_on=None, video=True,
                 audio_samples=True, reenumerate_s=0.05, first_after=(0,)):
        self.sysfs, self.udid, self.name, self.devnum = sysfs, udid, name, devnum
        self.configs, self.config, self.fresh, self.pictures = configs, config, fresh, list(pictures or [None])
        self.on_enable, self.rels, self.fps, self.keyframe_every = on_enable, rels, fps, keyframe_every
        self.size, self.chunk, self.rotate_at = tuple(size), chunk, rotate_at
        self.garbage, self.garbage_sessions = list(garbage), garbage_sessions
        self.deaf, self.unplug_on, self.video, self.audio_samples = deaf, unplug_on, video, audio_samples
        self.reenumerate_s, self.first_after = reenumerate_s, list(first_after)
        self.lock = threading.RLock()
        self.present, self.unplugged = True, False
        self.counter = 0                  # frame counter across sessions
        self.handles, self.sessions, self.problems, self.controls = [], [], [], []
        self.write_timeouts, self.reset_times = set(), []
        self.resets = self.enables = 0
        self._write()

    @property
    def node(self):
        return f"/dev/bus/usb/001/{self.devnum:03d}"

    def descriptors(self):
        return iphone_descriptors(self.configs)

    def _write(self):
        self.sysfs.write(self.name, idVendor="05ac", idProduct="12a8", serial=self.udid.replace("-", ""),
                         busnum=1, devnum=self.devnum, bConfigurationValue=self.config,
                         bNumConfigurations=self.configs, descriptors=self.descriptors())

    def open(self, node):
        with self.lock:
            if not self.present or node != self.node:
                raise usbfs.UsbError(errno.ENOENT, f"cannot open {node}: No such file or directory")
            h = FakeHandle(self, node)
            self.handles.append(h)
            return h

    def new_session(self, handle):
        with self.lock:
            n = len(self.sessions)
            pictures = self.pictures[min(n, len(self.pictures) - 1)]
            s = FakeSession(self, handle, "fresh" if self.fresh else "silent", pictures,
                            self.first_after[min(n, len(self.first_after) - 1)])
            self.fresh = False            # one session per enumeration
            self.sessions.append(s)
            return s

    def _gone(self):
        self.present = False
        for h in self.handles:
            h.dead()
        self.sysfs.remove(self.name)

    def _reenumerate(self, change=None):
        """It disconnects (its sysfs directory goes, transfers fail), then comes back with a new devnum."""
        with self.lock:
            self._gone()

        def back():
            time.sleep(self.reenumerate_s)
            with self.lock:
                if self.unplugged or not os.path.isdir(self.sysfs.root):
                    return
                if change:
                    change()
                self.devnum += 1
                self.fresh, self.present = True, True
                self._write()
        threading.Thread(target=back, daemon=True).start()

    def reset(self):
        self.resets += 1
        self.reset_times.append(time.monotonic())
        self._reenumerate()

    def enable(self):
        self.enables += 1
        if self.on_enable == "capture":
            self._reenumerate(lambda: self.__dict__.update(configs=5, config=5))
        elif self.on_enable == "force4":
            self._reenumerate(lambda: self.__dict__.update(configs=5, config=4))

    def unplug(self):
        with self.lock:
            self.unplugged = True
            self._gone()

    def report(self):
        return {"problems": self.problems, "resets": self.resets, "enables": self.enables,
                "sessions": [{"mode": s.mode, "timeline": s.timeline, "frames": s.frames, "needs": s.needs,
                              "audio": s.audio} for s in self.sessions],
                "write_timeouts": sorted(self.write_timeouts)}


# ---- as a script: screencap.main in its own process ----------------------------------------------------------
def run_screencap(scenario, args):
    sysfs = FakeSysfs()
    phone = FakePhone(sysfs, **scenario.get("phone", {}))
    for k, v in scenario.get("timing", {}).items():
        setattr(screencap, k, v)
    if scenario.get("unplug_after") is not None:
        def unplug_later():
            while not any(s.frames for s in phone.sessions):
                time.sleep(0.01)
            time.sleep(scenario["unplug_after"])
            phone.unplug()
        threading.Thread(target=unplug_later, daemon=True).start()
    try:
        return screencap.main(args, opener=phone.open, sysfs=sysfs.root)
    finally:
        with open(scenario["report"], "w") as f:
            json.dump(phone.report(), f)
        sysfs.cleanup()


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        scene = json.load(f)
    sys.exit(run_screencap(scene, sys.argv[3:] if sys.argv[2:3] == ["--"] else sys.argv[2:]))
