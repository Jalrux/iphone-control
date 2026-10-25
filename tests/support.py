"""Shared test helpers: a fake usbmuxd, a fake WebDriverAgent, a fake capture socket, PNG, H.264 and IPA builders.

Nothing here touches a real iPhone, usbmuxd, Apple or the network: everything listens on 127.0.0.1
or on a UNIX socket in a temp dir.
"""
import base64
import datetime
import json
import os
import plistlib
import socket
import struct
import tempfile
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fake UDIDs that do not look like real ones (tests/test_privacy_and_integrity.py scans for those)
UDID_A = "00008130-FAKE0000000000AA"
UDID_B = "00008130-FAKE0000000000BB"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_ports(n):
    """n different free ports: every socket stays bound until all are chosen (one free_port() after another may
    get the same port twice, which made the script tests' forward and fake WebDriverAgent collide on CI)."""
    socks = []
    try:
        for _ in range(n):
            s = socket.socket()
            socks.append(s)
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


def _pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    for s in (src, dst):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


class FakeMux:
    """usbmuxd on an AF_UNIX socket (point USBMUXD_SOCKET_ADDRESS=UNIX:<path> at it).

    devices: [(DeviceID, UDID)] on USB (a Wi-Fi device is always listed too, and must be ignored).
    ports: {phone port: local TCP port} for Connect; other ports answer "connection refused".
    Port 62078 is a tiny lockdownd that answers GetValue ProductVersion with `ios_version`.
    paired: UDIDs with a pair record.
    tcp: listen on 127.0.0.1 TCP instead (USBMUXD_SOCKET_ADDRESS=127.0.0.1:<port>).
    """

    def __init__(self, devices=((7, UDID_A),), ports=None, ios_version="26.5.2", paired=None, path=None,
                 tcp=False):
        self.devices = list(devices)
        self.ports = dict(ports or {})
        self.ios_version = ios_version
        self.paired = set(paired if paired is not None else [u for _, u in self.devices])
        self.connects = []                  # (DeviceID, phone port) of every Connect
        self.dir = self.path = None
        self.accepted = []                  # every connection usbmuxd accepted (to check socket options)
        if tcp:
            self.sock = socket.create_server(("127.0.0.1", 0), backlog=16)
        else:
            if path is None:
                self.dir = tempfile.mkdtemp(prefix="fakemux-")
                path = os.path.join(self.dir, "usbmuxd")
            self.path = path
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(self.path)
            self.sock.listen(16)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def address(self):
        if self.path is None:
            return "127.0.0.1:%d" % self.sock.getsockname()[1]
        return "UNIX:" + self.path

    def close(self):
        self.sock.close()
        if self.path is None:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass
        if self.dir:
            try:
                os.rmdir(self.dir)
            except OSError:
                pass

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted.append(c)
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    @staticmethod
    def _send(c, msg):
        body = plistlib.dumps(msg)
        c.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)

    def _handle(self, c):
        try:
            length = struct.unpack("<IIII", _recv_exact(c, 16))[0]
            msg = plistlib.loads(_recv_exact(c, length - 16))
            kind = msg.get("MessageType")
            if kind == "ListDevices":
                devs = [{"DeviceID": d, "MessageType": "Attached",
                         "Properties": {"ConnectionType": "USB", "DeviceID": d, "SerialNumber": u}}
                        for d, u in self.devices]
                devs.append({"DeviceID": 99, "MessageType": "Attached",
                             "Properties": {"ConnectionType": "Network", "DeviceID": 99, "SerialNumber": "WIFI-ONLY"}})
                self._send(c, {"DeviceList": devs})
            elif kind == "ReadPairRecord":
                if msg.get("PairRecordID") in self.paired:
                    self._send(c, {"PairRecordData": b"fake pair record"})
                else:
                    self._send(c, {"MessageType": "Result", "Number": 2})
            elif kind == "Connect":
                dev, port = msg["DeviceID"], socket.ntohs(msg["PortNumber"])
                self.connects.append((dev, port))
                if dev not in [d for d, _ in self.devices]:
                    self._send(c, {"MessageType": "Result", "Number": 2})
                elif port == 62078:
                    self._send(c, {"MessageType": "Result", "Number": 0})
                    return self._lockdown(c)
                else:
                    try:
                        up = socket.create_connection(("127.0.0.1", self.ports[port]), timeout=5)
                        up.settimeout(None)
                    except (KeyError, OSError):
                        self._send(c, {"MessageType": "Result", "Number": 3})
                    else:
                        self._send(c, {"MessageType": "Result", "Number": 0})
                        threading.Thread(target=_pump, args=(up, c), daemon=True).start()
                        _pump(c, up)
                        return
            else:
                self._send(c, {"MessageType": "Result", "Number": 1})
        except OSError:
            pass
        c.close()

    def _lockdown(self, c):
        try:
            n = struct.unpack(">I", _recv_exact(c, 4))[0]
            req = plistlib.loads(_recv_exact(c, n))
            value = self.ios_version if req.get("Key") == "ProductVersion" else ""
            body = plistlib.dumps({"Request": "GetValue", "Key": req.get("Key"), "Value": value})
            c.sendall(struct.pack(">I", len(body)) + body)
        except OSError:
            pass
        c.close()


# ---------------------------------------------------------------- PNG
def make_png(width, height, pixel):
    """An 8-bit RGB PNG; pixel(x, y) -> (r, g, b). Rows use filter 0 (none)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(pixel(x, y))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- H.264
class _Bits:
    def __init__(self):
        self.bits = []

    def u(self, n, v):
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def ue(self, v):                    # Exp-Golomb
        n = (v + 1).bit_length()
        self.u(n - 1, 0)
        self.u(n, v + 1)

    def se(self, v):
        self.ue(2 * v - 1 if v > 0 else -2 * v)

    def align(self):
        self.bits += [0] * (-len(self.bits) % 8)

    def nal(self, header):
        """rbsp_trailing_bits, then the NAL unit with its emulation prevention bytes."""
        self.bits.append(1)
        self.align()
        out, zeros = bytearray([header]), 0
        for i in range(0, len(self.bits), 8):
            b = int("".join(map(str, self.bits[i:i + 8])), 2)
            if zeros >= 2 and b <= 3:
                out.append(3)
                zeros = 0
            out.append(b)
            zeros = zeros + 1 if b == 0 else 0
        return bytes(out)


class TinyH264:
    """A valid H.264 stream made without an encoder: Baseline profile, every frame an IDR picture of I_PCM
    macroblocks (raw samples), so any decoder shows exactly the grey level asked for.

    avcc is the AVCDecoderConfigurationRecord (GStreamer's codec_data); frame(luma) is one access unit with 4-byte
    big-endian NAL lengths, like iphone_control/screencap.py's VIDEO payload after its keyframe byte."""

    def __init__(self, width=32, height=64):
        assert width % 16 == 0 and height % 16 == 0
        self.width, self.height = width, height
        b = _Bits()
        b.u(8, 66)                      # profile_idc: Baseline
        b.u(8, 0x40)                    # constraint_set1_flag (Constrained Baseline)
        b.u(8, 30)                      # level_idc 3.0
        for v in (0, 0, 2, 1):          # sps id, log2_max_frame_num_minus4, pic_order_cnt_type 2, max_num_ref_frames
            b.ue(v)
        b.u(1, 0)                       # gaps_in_frame_num_value_allowed_flag
        b.ue(width // 16 - 1)
        b.ue(height // 16 - 1)
        b.u(4, 0b1100)                  # frame_mbs_only, direct_8x8_inference, no cropping, no VUI
        self.sps = b.nal(0x67)
        b = _Bits()
        b.ue(0)                         # pps id
        b.ue(0)                         # sps id
        b.u(2, 0)                       # CAVLC, no bottom_field_pic_order_in_frame_present
        for v in (0, 0, 0):             # one slice group, one reference each way
            b.ue(v)
        b.u(3, 0)                       # no weighted prediction
        for v in (0, 0, 0):             # pic_init_qp, pic_init_qs, chroma_qp_index_offset
            b.se(v)
        b.u(3, 0b100)                   # deblocking_filter_control_present, no constrained intra, no redundant pics
        self.pps = b.nal(0x68)
        self.avcc = (bytes([1, self.sps[1], self.sps[2], self.sps[3], 0xFF, 0xE1])
                     + struct.pack(">H", len(self.sps)) + self.sps + b"\x01" + struct.pack(">H", len(self.pps))
                     + self.pps)
        self._frames = {}
        self._count = 0

    def frame(self, luma, chroma=128):
        """One access unit (an IDR picture) of a single colour; luma and chroma 1-255."""
        idr_id = self._count % 2           # two IDR pictures in a row need different idr_pic_ids
        self._count += 1
        key = (luma, chroma, idr_id)
        if key not in self._frames:
            b = _Bits()
            for v in (0, 7, 0):             # first_mb_in_slice, slice_type I, pps id
                b.ue(v)
            b.u(4, 0)                       # frame_num
            b.ue(idr_id)
            b.u(2, 0)                       # dec_ref_pic_marking: no_output_of_prior_pics, not long-term
            b.se(0)                         # slice_qp_delta
            b.ue(1)                         # disable_deblocking_filter_idc
            for _ in range(self.width // 16 * self.height // 16):
                b.ue(25)                    # mb_type I_PCM
                b.align()
                for v in [luma] * 256 + [chroma] * 128:
                    b.u(8, v)
            nal = b.nal(0x65)
            self._frames[key] = struct.pack(">I", len(nal)) + nal
        return self._frames[key]


# ---------------------------------------------------------------- capture socket
class FakeCapture:
    """The capture process's UNIX socket (iphone_control/screencap.py's frame format) in a temp dir.

    Each client gets VIDEO_FORMAT (fmt = (width, height, avcC)), then one VIDEO frame every `interval` s:
    frame(i) -> (keyframe, access unit). drop_after: close each client's connection after that many seconds
    (as when the capture process stops or restarts)."""
    HEADER = struct.Struct("<IBq")
    VIDEO_FORMAT, VIDEO = 1, 2

    def __init__(self, frame, fmt, interval=0.005, drop_after=None):
        self.frame, self.fmt, self.interval, self.drop_after = frame, fmt, interval, drop_after
        self.dir = tempfile.mkdtemp(prefix="fakecap-")
        self.path = os.path.join(self.dir, "cap.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(2)
        self.clients = []
        self.stopped = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _send(self, c, kind, payload):
        c.sendall(self.HEADER.pack(len(payload), kind, time.monotonic_ns()) + payload)

    def _serve(self, c):
        end = time.monotonic() + self.drop_after if self.drop_after is not None else None
        try:
            w, h, avcc = self.fmt
            self._send(c, self.VIDEO_FORMAT, struct.pack("<II", w, h) + avcc)
            i = 0
            while not self.stopped.is_set() and (end is None or time.monotonic() < end):
                key, unit = self.frame(i)
                self._send(c, self.VIDEO, (b"\x01" if key else b"\x00") + unit)
                i += 1
                time.sleep(self.interval)
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.stopped.set()
        self.sock.close()
        for c in self.clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            os.unlink(self.path)
            os.rmdir(self.dir)
        except OSError:
            pass


# ---------------------------------------------------------------- IPA
def bundle_id():
    """A WebDriverAgent-like bundle id with a team-id-shaped suffix, built at run time so the
    source never contains one (the privacy scan looks for them)."""
    return "com.example.WebDriverAgentRunner.xctrunner." + "T" * 5 + "0" * 5


def make_signed_ipa(path, bundle=None, created=None, expires=None, app="WebDriverAgentRunner-Runner.app"):
    """A minimal signed-looking IPA: Payload/<app>/Info.plist + embedded.mobileprovision (CMS-ish
    bytes around an XML plist with CreationDate/ExpirationDate, no DeveloperCertificates)."""
    created = created or datetime.datetime(2026, 10, 20, 12, 0, 0)
    expires = expires or created + datetime.timedelta(days=7)
    info = plistlib.dumps({"CFBundleIdentifier": bundle or bundle_id(), "CFBundleExecutable": "x"})
    prof = plistlib.dumps({"Name": "test profile", "CreationDate": created, "ExpirationDate": expires})
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"Payload/{app}/Info.plist", info)
        z.writestr(f"Payload/{app}/embedded.mobileprovision", b"0\x82\x10\x00CMS-junk" + prof + b"\x00sig")
        z.writestr(f"Payload/{app}/PlugIns/WebDriverAgentRunner.xctest/Info.plist", info)
    return path


def epoch(dt):
    return int((dt - datetime.datetime(1970, 1, 1)).total_seconds())


# ---------------------------------------------------------------- fake WebDriverAgent
class FakeWDAHandler(BaseHTTPRequestHandler):
    """Enough of WebDriverAgent for the client: sessions, settings, keys (with their typing frequency), HID
    events, actions, pasteboard, screenshots. Keep-alive (HTTP/1.1), like the real one. State lives on the
    server object."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, code, value, sid=None):
        body = json.dumps({"value": value, "sessionId": sid}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _any(self, method):
        st = self.server
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        with st.lock:
            st.calls.append((method, self.path, body))
            st.conns.add(self.client_address)
        hook = st.hooks.get(self.path.replace(f"/session/{st.live}", "/session/{sid}"))
        if hook:
            res = hook(self, method, body)
            if res == "drop":                  # close the connection without answering
                self.close_connection = True
                return
            if res is not None:
                return self.reply(*res)
        if self.path == "/status":
            return self.reply(200, {"os": {"name": "iOS", "version": "26.5.2"}, "ready": True})
        if self.path == "/session" and method == "POST":
            with st.lock:
                st.sessions += 1
                st.live = f"S{st.sessions}"
            return self.reply(200, {"capabilities": {}, "sessionId": st.live}, st.live)
        if self.path == "/screenshot":
            return self.reply(200, base64.b64encode(st.screenshot_png).decode())
        if self.path == "/wda/getPasteboard":
            return self.reply(200, base64.b64encode(st.pasteboard).decode())
        if self.path == "/wda/setPasteboard":
            st.pasteboard = base64.b64decode(body["content"])
            return self.reply(200, None)
        if self.path == "/wda/locked":
            return self.reply(200, st.locked)
        if self.path in ("/wda/lock", "/wda/unlock"):
            st.locked = self.path == "/wda/lock"
            return self.reply(200, None)
        if self.path == "/wda/performIoHidEvent":           # a session route only, as in WebDriverAgent
            return self.reply(404, {"error": "unknown command", "message": "Unhandled endpoint: " + self.path})
        if self.path.startswith("/wda/"):
            return self.reply(200, None)
        if self.path.startswith("/session/"):
            sid = self.path.split("/")[2]
            if sid != st.live:
                return self.reply(404, {"error": "invalid session id", "message": "Session does not exist"})
            if self.path.endswith("/window/size"):
                return self.reply(200, {"width": 393, "height": 852}, sid)
            if self.path.endswith("/wda/performIoHidEvent"):
                return self._hid(body, sid)
            if self.path.endswith("/wda/keys"):
                return self._keys(body, sid)
            return self.reply(200, None, sid)
        self.reply(404, {"error": "unknown command", "message": "Unhandled endpoint: " + self.path})

    def _hid(self, body, sid):
        """{"page", "usage", "duration"} like FBCustomCommands' handlePeformIOHIDEvent; kept in server.hid."""
        st = self.server
        b = body if isinstance(body, dict) else {}
        page, usage, duration = b.get("page"), b.get("usage"), b.get("duration")
        if not (isinstance(page, int) and isinstance(usage, int) and isinstance(duration, (int, float))
                and duration >= 0):
            return self.reply(400, {"error": "invalid argument", "message": f"bad HID event {body!r}"}, sid)
        with st.lock:
            st.hid.append((page, usage, duration))
        return self.reply(200, None, sid)

    def _keys(self, body, sid):
        """{"value": [...], "frequency"?}; kept in server.typed as (text, frequency or None)."""
        st = self.server
        b = body if isinstance(body, dict) else {}
        freq = b.get("frequency")
        if not isinstance(b.get("value"), list) or not (freq is None or (isinstance(freq, int) and freq > 0)):
            return self.reply(400, {"error": "invalid argument", "message": f"bad keys {body!r}"}, sid)
        with st.lock:
            st.typed.append(("".join(b["value"]), freq))
        if st.slow_keys:
            threading.Event().wait(st.slow_keys)
        return self.reply(200, None, sid)

    def do_GET(self):
        self._any("GET")

    def do_POST(self):
        self._any("POST")

    def do_DELETE(self):
        self._any("DELETE")


class FakeWDA(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler=FakeWDAHandler):
        super().__init__(("127.0.0.1", 0), handler)
        self.lock = threading.Lock()
        # path ("/session/{sid}/..." for the live session) -> fn(handler, method, body) returning
        # None (answer normally), (code, value[, sid]) or "drop"
        self.hooks = {}
        self.reset()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.calls, self.conns, self.sessions, self.live = [], set(), 0, None
            self.hid = []                   # (page, usage, duration) of every HID event, in order
            self.typed = []                 # (text, frequency or None) of every /wda/keys request
        self.hooks.clear()
        self.slow_keys = 0.0
        self.locked = False
        self.pasteboard = b""
        self.screenshot_png = make_png(4, 8, lambda x, y: (x * 60, y * 30, 200))

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def stop(self):
        self.shutdown()
        self.server_close()

    def paths(self, method=None):
        with self.lock:
            return [p for m, p, _ in self.calls if method is None or m == method]

    def bodies(self, suffix):
        with self.lock:
            return [b for _, p, b in self.calls if p.endswith(suffix)]


def read_png_size(png):
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", png[16:24])
