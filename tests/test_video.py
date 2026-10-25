"""The window's picture sources (iphone_control/video.py), with fake capture sockets, MJPEG servers and uxplay.

Needs PyGObject with GStreamer (skipped otherwise); the H.264 tests also need h264parse and a decoder (plugins-bad
and gst-libav or openh264). No display: the pipelines end in a fakesink named sink instead of gtksink.

tests/fixtures/h264/WxH.b64 are the bytes a client reads from screencap.py's socket (a VIDEO_FORMAT frame, then 5
VIDEO frames, the first a keyframe), base64: synthetic clips made with
  videotestsrc num-buffers=5 pattern=ball ! video/x-raw,width=W,height=H,framerate=30/1 ! openh264enc gop-size=30
  ! h264parse ! video/x-h264,stream-format=avc,alignment=au ! appsink
with the avcC record from the caps' codec_data, framed with screencap.frame().
"""
import base64
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
from support import read_png_size  # noqa: E402

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    from iphone_control import screencap
    from iphone_control import video as V
    HAVE_GST = True
except (ImportError, ValueError):
    HAVE_GST = False

FAKE_SINK = "fakesink name=sink sync=false"
FIXTURES = os.path.join(HERE, "fixtures", "h264")


def clip(name):
    with open(os.path.join(FIXTURES, name + ".b64")) as f:
        return base64.decodebytes(f.read().encode())


def frames_of(data):
    """[(kind, payload), ...] of a capture stream."""
    out, i = [], 0
    while i < len(data):
        n, kind, _pts = screencap.HEADER.unpack_from(data, i)
        out.append((kind, data[i + screencap.HEADER.size:i + screencap.HEADER.size + n]))
        i += screencap.HEADER.size + n
    return out


def spin(until, timeout=10):
    """Run the GLib main loop until until() is true (or the timeout); returns until()."""
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        if not ctx.iteration(False):
            time.sleep(0.005)
    return until()


def have_h264():
    return HAVE_GST and not V.H264Source.missing()


class FakeCapture:
    """screencap.py's socket (at path, else in a new temporary folder): each client gets `script` as it is when the
    client connects, a list of bytes to send or "close"; then it is held open."""

    def __init__(self, script=(), path=None):
        self.dir = None if path else tempfile.mkdtemp(prefix="fakecap-")
        self.path = path or os.path.join(self.dir, "cap.sock")
        self.script = list(script)
        self.clients = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.clients.append(c)
            for step in self.script:
                if step == "close":
                    c.close()
                    break
                c.sendall(step)

    def send(self, data):
        self.clients[-1].sendall(data)

    def close(self):
        for c in self.clients:
            c.close()
        self.sock.close()
        os.unlink(self.path)
        if self.dir:
            os.rmdir(self.dir)


class Lost:
    def __init__(self):
        self.why, self.status = [], []

    def __call__(self, why):
        self.why.append(why)


def dec_size(src):
    caps = src.pipeline.get_by_name("dec").get_static_pad("src").get_current_caps()
    if not caps:
        return None
    s = caps.get_structure(0)
    return s.get_value("width"), s.get_value("height")


def png_of(src):
    got = []
    src.last_frame_png(got.append)
    spin(lambda: got, 10)
    return got[0] if got else None


@unittest.skipUnless(HAVE_GST, "needs PyGObject with GStreamer")
class Helpers(unittest.TestCase):
    def test_decoder_order_and_override(self):
        def only(*names):
            return mock.patch.object(V.Gst.ElementFactory, "find", side_effect=lambda n: n in names or None)
        with mock.patch.dict(os.environ, {"IPHONE_DECODER": ""}):
            with only("avdec_h264", "openh264dec", "vah264dec", "nvh264dec"):
                self.assertEqual(V.decoder(), "vah264dec")                 # hardware first
            with only("openh264dec", "avdec_h264", "nvh264dec"):
                self.assertEqual(V.decoder(), "avdec_h264")                # never nvh264dec on its own
            with only("nvh264dec"):
                self.assertIsNone(V.decoder())
                self.assertEqual(V.H264Source.missing()[-1], "an H.264 decoder")
        with mock.patch.dict(os.environ, {"IPHONE_DECODER": "nvh264dec"}):
            with only("nvh264dec", "avdec_h264"):
                self.assertEqual(V.decoder(), "nvh264dec")
            with only("avdec_h264"):
                self.assertIsNone(V.decoder())                             # asked for, not there
        with mock.patch.dict(os.environ, {"IPHONE_DECODER": "fakesink ! x"}), only("fakesink ! x"):
            self.assertIsNone(V.decoder())                                 # an element name, not a pipeline

    def test_h264_caps_carry_the_size_and_the_avcc_record(self):
        caps = V.h264_caps(1180, 2556, b"\x01\x64\x00\x33\xff")
        s = caps.get_structure(0)
        self.assertEqual(s.get_name(), "video/x-h264")
        self.assertEqual((s.get_value("stream-format"), s.get_value("alignment")), ("avc", "au"))
        self.assertEqual((s.get_value("width"), s.get_value("height")), (1180, 2556))
        ok, info = s.get_value("codec_data").map(Gst.MapFlags.READ)
        self.assertEqual(bytes(info.data), b"\x01\x64\x00\x33\xff")

    def test_fixtures_are_what_the_capture_socket_sends(self):
        for name, size in (("64x128", (64, 128)), ("128x64", (128, 64))):
            frames = frames_of(clip(name))
            self.assertEqual(frames[0][0], screencap.VIDEO_FORMAT)
            self.assertEqual(struct.unpack_from("<II", frames[0][1]), size)
            self.assertEqual([k for k, _ in frames[1:]], [screencap.VIDEO] * 5)
            self.assertEqual([p[0] for _, p in frames[1:]], [1, 0, 0, 0, 0])      # a keyframe, then P-frames

    def test_uxplay_check(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"PATH": tmp.name}):
            self.assertIn("uxplay is not installed", V.uxplay_problem())
            for out, ok in (("UxPlay 1.68: An Open-Source AirPlay mirroring server", False),
                            ("uxplay 1.73.2 ...", True), ("garbage", False)):
                fake_uxplay(tmp.name, version=out)
                problem = V.uxplay_problem()
                self.assertEqual(problem is None, ok, (out, problem))
                if not ok:
                    self.assertIn("UxPlay 1.73+ is needed for -vrtp", problem)


def running(pid):
    """pid is a live process (not gone, not a zombie waiting for its new parent to reap it)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def wait_gone(pid, timeout=5):
    end = time.monotonic() + timeout
    while running(pid) and time.monotonic() < end:
        time.sleep(0.05)
    return not running(pid)


def kill_if_running(pid):
    if running(pid):
        os.kill(pid, signal.SIGKILL)


def fake_uxplay(folder, version="UxPlay 1.73", log=None):
    """A uxplay on PATH: -v prints version; otherwise it records its arguments and waits to be stopped."""
    path = os.path.join(folder, "uxplay")
    with open(path, "w") as f:
        f.write('#!/bin/sh\nif [ "$1" = -v ]; then echo "%s"; exit 0; fi\n' % version
                + (f'printf "%s\\n" "$@" > "{log}"\n' if log else "")
                + 'trap "exit 0" TERM\nwhile :; do sleep 0.05; done\n')
    os.chmod(path, 0o755)
    return path


# ---------------------------------------------------------------- the USB capture
@unittest.skipUnless(have_h264() if HAVE_GST else False, "needs h264parse and an H.264 decoder")
class Capture(unittest.TestCase):
    def setUp(self):
        self.portrait, self.landscape = clip("64x128"), clip("128x64")
        self.lost, self.status = Lost(), []

    def source(self, cap):
        src = V.H264Source(cap.path, self.lost, self.status.append, sink=FAKE_SINK)
        self.addCleanup(src.close)
        return src

    def test_frames_are_decoded_at_once_with_their_caps(self):
        cap = FakeCapture([self.portrait])
        self.addCleanup(cap.close)
        src = self.source(cap)
        src.start()
        self.assertTrue(spin(lambda: src.frames == 5), src.frames)
        # nothing follows the fifth frame, and it was decoded anyway (no decoder delay to wait out)
        self.assertEqual(dec_size(src), (64, 128))
        s = src.src.get_property("caps").get_structure(0)
        self.assertEqual((s.get_value("width"), s.get_value("height")), (64, 128))
        ok, info = s.get_value("codec_data").map(Gst.MapFlags.READ)
        self.assertEqual(bytes(info.data), frames_of(self.portrait)[0][1][8:])      # the avcC record
        self.assertEqual(self.lost.why, [])
        self.assertIsNotNone(src.pipeline.get_by_name("fit") if V.scaler() else True)
        self.assertIsNotNone(src.pipeline.get_by_name("sink"))

    def test_rotation_is_a_new_caps_and_the_last_frame_is_a_png(self):
        cap = FakeCapture([self.portrait])
        self.addCleanup(cap.close)
        src = self.source(cap)
        src.start()
        self.assertTrue(spin(lambda: src.frames == 5))
        png = png_of(src)
        self.assertEqual(read_png_size(png), (64, 128))                   # full size, before the scaler
        cap.send(self.landscape)                                          # an app turned to landscape
        self.assertTrue(spin(lambda: src.frames == 10), src.frames)
        self.assertEqual(dec_size(src), (128, 64))
        self.assertEqual(read_png_size(png_of(src)), (128, 64))
        self.assertEqual(self.lost.why, [])

    def test_last_frame_png_without_a_frame(self):
        cap = FakeCapture()
        self.addCleanup(cap.close)
        src = self.source(cap)
        got = []
        src.last_frame_png(got.append)
        self.assertEqual(got, [None])

    def test_socket_end_is_a_loss_and_start_reconnects(self):
        cap = FakeCapture([self.portrait, "close"])
        self.addCleanup(cap.close)
        src = self.source(cap)
        src.start()
        self.assertTrue(spin(lambda: self.lost.why and src.frames == 5), (self.lost.why, src.frames))
        self.assertEqual(self.lost.why, ["the USB capture closed its socket"])
        src.stop()                                                      # what the window does, then after a backoff:
        src.start()
        self.assertTrue(spin(lambda: len(self.lost.why) == 2 and src.frames == 10), (self.lost.why, src.frames))
        self.assertEqual(len(cap.clients), 2)                             # the capture replays format + frames

    def test_stop_reports_nothing(self):
        cap = FakeCapture([self.portrait])
        self.addCleanup(cap.close)
        src = self.source(cap)
        src.start()
        self.assertTrue(spin(lambda: src.frames == 5))
        src.stop()
        spin(lambda: False, 0.5)
        self.assertEqual(self.lost.why, [])

    def test_no_capture_running(self):
        src = V.H264Source(os.path.join(tempfile.gettempdir(), "no-such-capture.sock"), self.lost, sink=FAKE_SINK)
        self.addCleanup(src.close)
        src.start()
        self.assertTrue(spin(lambda: self.lost.why))
        self.assertIn("no-such-capture.sock", self.lost.why[0])

    def test_status_text_and_frames_before_the_format(self):
        frames = frames_of(self.portrait)
        early = screencap.frame(screencap.VIDEO, frames[2][1])              # no format yet: dropped
        note = screencap.frame(screencap.STATUS, "resetting the iPhone's USB connection".encode())
        cap = FakeCapture([early, note, self.portrait])
        self.addCleanup(cap.close)
        src = self.source(cap)
        src.start()
        self.assertTrue(spin(lambda: src.frames == 5 and self.status))
        self.assertEqual(self.status, ["resetting the iPhone's USB connection"])
        self.assertEqual(self.lost.why, [])

    def test_a_status_stays_until_a_picture_comes_without_one(self):
        # the capture replays its status before the frames: a connection's first picture without one clears it
        note = screencap.frame(screencap.STATUS, b"No picture from the iPhone - unlock it")
        cap = FakeCapture([note, self.portrait, "close"])
        self.addCleanup(cap.close)
        src = self.source(cap)
        src.start()
        self.assertTrue(spin(lambda: self.lost.why and src.frames == 5), (self.lost.why, src.frames))
        self.assertEqual(self.status, ["No picture from the iPhone - unlock it"])          # not cleared
        cap.script = [self.portrait]                       # e.g. a new capture process, with nothing to say
        src.stop()
        src.start()
        self.assertTrue(spin(lambda: src.frames == 10 and len(self.status) == 2), (src.frames, self.status))
        self.assertEqual(self.status[1], "")
        cap.send(screencap.frame(screencap.STATUS, b"The iPhone stopped sending its screen"))
        cap.send(self.portrait)
        self.assertTrue(spin(lambda: src.frames == 15))
        spin(lambda: False, 0.2)
        self.assertEqual(self.status[2:], ["The iPhone stopped sending its screen"])      # cleared once only

    def test_a_stalled_capture_is_a_loss(self):
        cap = FakeCapture([self.portrait])
        self.addCleanup(cap.close)
        with mock.patch.object(V, "STALL_S", 0.3):
            src = self.source(cap)
            src.start()
            self.assertTrue(spin(lambda: self.lost.why, 5))
        self.assertEqual(self.lost.why, ["no picture from the USB capture for 0.3 s"])


# ---------------------------------------------------------------- MJPEG
class Mjpeg(BaseHTTPRequestHandler):
    frame = b""
    frames = 100              # per connection, then the stream ends
    connects = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        Mjpeg.connects += 1
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=--BoundaryString")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for _ in range(Mjpeg.frames):
                self.wfile.write(b"--BoundaryString\r\nContent-type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                 % len(Mjpeg.frame) + Mjpeg.frame + b"\r\n\r\n")
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass


def jpeg(width, height):
    p = Gst.parse_launch(f"videotestsrc num-buffers=1 ! video/x-raw,width={width},height={height} ! jpegenc ! "
                         "appsink name=out")
    p.set_state(Gst.State.PLAYING)
    sample = p.get_by_name("out").emit("try-pull-sample", 10 * Gst.SECOND)
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    data = bytes(info.data)
    buf.unmap(info)
    p.set_state(Gst.State.NULL)
    return data


@unittest.skipUnless(HAVE_GST, "needs PyGObject with GStreamer")
class MjpegStream(unittest.TestCase):
    def setUp(self):
        Mjpeg.frame, Mjpeg.frames, Mjpeg.connects = jpeg(90, 180), 100, 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Mjpeg)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_mjpeg_source_keeps_the_names_and_the_last_frame(self):
        if V.MjpegSource.missing():
            self.skipTest(f"missing {V.MjpegSource.missing()}")
        lost = Lost()
        src = V.MjpegSource(self.url, lost, sink=FAKE_SINK)
        self.addCleanup(src.close)
        for name in ("dec", "sink") + (("fit",) if V.scaler() else ()):
            self.assertIsNotNone(src.pipeline.get_by_name(name), name)
        src.start()
        self.assertTrue(spin(lambda: src.frames >= 2))
        self.assertEqual(dec_size(src), (90, 180))
        self.assertEqual(read_png_size(png_of(src)), (90, 180))

    def test_drain_stays_connected_and_comes_back(self):
        Mjpeg.frames = 5                                              # the stream ends after 5 frames
        logs = []
        d = V.MjpegDrain(self.url + "/", logs.append).start()
        self.addCleanup(d.stop)
        self.assertTrue(spin(lambda: d.connects >= 2, 10), d.connects)       # reconnected on its own
        self.assertGreater(d.received, 5 * len(Mjpeg.frame))
        self.assertIn("WebDriverAgent's hidden stream: the stream ended - reconnecting", logs)
        Mjpeg.frames = 10 ** 6
        d.stop()
        n = Mjpeg.connects
        spin(lambda: False, 1.5)
        self.assertLessEqual(Mjpeg.connects, n + 1)                  # stopped: no more reconnects

    def test_drain_backs_off_while_nothing_listens(self):
        self.server.shutdown()
        self.server.server_close()
        logs = []
        d = V.MjpegDrain(self.url, logs.append).start()
        self.addCleanup(d.stop)
        spin(lambda: False, 2)
        self.assertEqual(d.connects, 0)
        self.assertEqual(len(logs), 1, logs)                          # one message per outage, not per attempt


# ---------------------------------------------------------------- AirPlay
@unittest.skipUnless(have_h264() and not V.AirplaySource.missing() if HAVE_GST else False,
                     "needs udpsrc, rtph264depay, h264parse and an H.264 decoder")
class Airplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = os.path.join(self.tmp.name, "args")
        fake_uxplay(self.tmp.name, log=self.args)
        env = mock.patch.dict(os.environ, {"PATH": self.tmp.name + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)

    def test_the_source_runs_uxplay_and_stops_it(self):
        lost = Lost()
        log = os.path.join(self.tmp.name, "uxplay.log")
        src = V.AirplaySource("Test Mac", lost, log_path=log, sink=FAKE_SINK, port=V.free_udp_port())
        self.addCleanup(src.close)
        src.start()
        self.assertTrue(spin(lambda: os.path.exists(self.args) and os.path.getsize(self.args)))
        with open(self.args) as f:
            args = f.read().splitlines()
        self.assertEqual(args[:5], ["-n", "Test Mac", "-nh", "-p", "-vrtp"])
        self.assertEqual(args[5], f"config-interval=1 ! udpsink host=127.0.0.1 port={src.port}")
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
        proc = src.proc
        src.stop()                                                    # a pipeline restart keeps the receiver
        self.assertIsNone(proc.poll())
        src.close()
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(src.proc)
        self.assertEqual(lost.why, [])

    def test_uxplay_exiting_is_a_loss_and_start_runs_it_again(self):
        lost = Lost()
        src = V.AirplaySource("Test Mac", lost, log_path=os.path.join(self.tmp.name, "uxplay.log"), sink=FAKE_SINK)
        self.addCleanup(src.close)
        src.start()
        first = src.proc
        first.terminate()
        self.assertTrue(spin(lambda: lost.why))
        self.assertEqual(lost.why, ["uxplay exited"])
        src.stop()
        src.start()
        self.assertIsNotNone(src.proc)
        self.assertNotEqual(src.proc.pid, first.pid)

    def test_the_pid_file_holds_uxplay_while_it_runs(self):
        lost, pid_path = Lost(), os.path.join(self.tmp.name, "uxplay.pid")
        src = V.AirplaySource("Test Mac", lost, log_path=os.path.join(self.tmp.name, "uxplay.log"), sink=FAKE_SINK,
                              pid_path=pid_path)
        self.addCleanup(src.close)
        src.start()
        with open(pid_path) as f:
            self.assertEqual(f.read(), f"{src.proc.pid}\n")             # what iphone.sh's pid files hold
        self.assertEqual(stat.S_IMODE(os.stat(pid_path).st_mode), 0o600)
        src.proc.terminate()
        self.assertTrue(spin(lambda: lost.why))
        self.assertFalse(os.path.exists(pid_path))                       # it exited on its own
        src.stop()
        src.start()
        with open(pid_path) as f:
            self.assertEqual(int(f.read()), src.proc.pid)
        src.close()
        self.assertFalse(os.path.exists(pid_path))

    def test_uxplay_dies_with_the_window(self):
        # a window that is killed or crashes runs no atexit: uxplay must not stay on the network anyway
        pid_path = os.path.join(self.tmp.name, "uxplay.pid")
        code = ("import os, signal, sys; sys.path.insert(0, sys.argv[1])\n"
                "import gi; gi.require_version('Gst', '1.0')\nfrom gi.repository import Gst\nGst.init(None)\n"
                "from iphone_control import video as V\n"
                f"src = V.AirplaySource('Test Mac', print, log_path=sys.argv[2], sink={FAKE_SINK!r}, "
                "pid_path=sys.argv[3])\n"
                "src.start()\nos.kill(os.getpid(), signal.SIGKILL)\n")
        r = subprocess.run([sys.executable, "-I", "-c", code, REPO, os.path.join(self.tmp.name, "uxplay.log"),
                            pid_path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, -signal.SIGKILL, r.stderr)
        with open(pid_path) as f:                                        # nothing ran after the kill
            pid = int(f.read())
        self.addCleanup(kill_if_running, pid)
        self.assertTrue(wait_gone(pid), f"uxplay {pid} outlived its parent")

    def test_rtp_from_uxplay_is_decoded(self):
        # what uxplay -vrtp sends: rtph264pay with config-interval=1 into udpsink
        if not Gst.ElementFactory.find("rtph264pay"):
            self.skipTest("no rtph264pay")
        lost = Lost()
        src = V.AirplaySource("Test Mac", lost, log_path=os.path.join(self.tmp.name, "uxplay.log"), sink=FAKE_SINK)
        self.addCleanup(src.close)
        src.start()
        frames = frames_of(clip("64x128"))
        avcc = frames[0][1][8:]
        send = Gst.parse_launch(
            f"appsrc name=a format=time caps={V.H264_CAPS},width=64,height=128,codec_data=(buffer){avcc.hex()} ! "
            f"h264parse ! rtph264pay config-interval=1 pt=96 ! udpsink host=127.0.0.1 port={src.port}")
        a = send.get_by_name("a")
        send.set_state(Gst.State.PLAYING)
        for n, (_kind, payload) in enumerate(frames[1:]):
            buf = Gst.Buffer.new_wrapped(payload[1:])
            buf.pts = n * Gst.SECOND // 30
            a.emit("push-buffer", buf)
        a.emit("end-of-stream")
        self.assertTrue(spin(lambda: src.frames >= 1, 10), src.frames)
        send.set_state(Gst.State.NULL)
        self.assertEqual(dec_size(src), (64, 128))


if __name__ == "__main__":
    unittest.main()
