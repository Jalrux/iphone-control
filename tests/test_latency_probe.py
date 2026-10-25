"""tests/latency_probe.py against a simulated home screen: a fake WebDriverAgent plus a fake MJPEG stream (and a
fake USB capture socket) whose frames show the page the phone is on.

Checks the numbers it reports, and its safety: it refuses without the flag, only swipes the middle of the home
screen, taps nothing but the Search pill (with --tap), ends with Home on the first page and always puts
WebDriverAgent's settings back.
"""
import contextlib
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import latency_probe  # noqa: E402
from support import FakeCapture, FakeWDA, TinyH264, free_port  # noqa: E402

W, H = 393, 852
VALUES = {0: 10, 1: 120, 2: 200}          # what each home page looks like (one grey level per page)
APP = 250                                  # any other screen
SPOTLIGHT = 60                             # Spotlight, open
SETTINGS = {"mjpegServerFramerate": 30, "mjpegScalingFactor": 100, "mjpegServerScreenshotQuality": 60,
            "snapshotMaxDepth": 50, "waitForIdleTimeout": 10, "animationCoolOffTimeout": 2}
PILL = {"x": 151.0, "y": 689.0, "width": 90.0, "height": 30.0}     # the Search pill above the Dock, centre (196, 704)


def fake_jpeg(value, size=60):
    return b"\xff\xd8" + bytes([value]) * size + b"\xff\xd9"


def fake_decode(jpeg):
    v = jpeg[2]
    return [[(v, v, v)] * latency_probe.CELLS[0] for _ in range(latency_probe.CELLS[1])]


def fake_unit(value, key):
    """A fake H.264 access unit: one NAL (IDR or not) whose last byte is the screen's grey level."""
    return struct.pack(">I", 2) + bytes([0x65 if key else 0x41, value])


def fake_decode_h264(fmt, units):
    return [fake_decode(b"\xff\xd8" + u[-1:]) for u in units]


def wait_until(test, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(0.01)
    return bool(test())


class Phone:
    """The simulated phone: home pages, Spotlight, Home, settings, the Search pill in the accessibility tree. A
    swipe shows the new page (a tap on the pill: Spotlight) after `starts` seconds and WebDriverAgent replies after
    `replies` seconds; Home closes Spotlight (unless spotlight_sticks), else goes to the first page."""

    def __init__(self, pages=2, starts=0.03, replies=0.08, swipes_work=True, app=None, locked=False,
                 found=(PILL,), taps_work=True, spotlight_sticks=False, depth=50):
        self.page, self.pages, self.app, self.locked = 0, pages, app, locked
        self.starts, self.replies, self.swipes_work = starts, replies, swipes_work
        self.found, self.taps_work, self.spotlight_sticks = list(found), taps_work, spotlight_sticks
        self.spotlight = False
        self.settings = dict(SETTINGS, snapshotMaxDepth=depth)
        self.posted = []                      # every settings POST, in order
        self.gestures, self.buttons = [], []
        self.queries = []                     # (body, snapshotMaxDepth) of every element query
        self.tap_depths = []                  # snapshotMaxDepth at every tap

    def value(self):
        return APP if self.app else SPOTLIGHT if self.spotlight else VALUES[self.page]

    def gesture(self, body):
        self.gestures.append(body)
        moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
        time.sleep(self.starts)
        if len(moves) == 1:                   # a tap
            self.tap_depths.append(self.settings["snapshotMaxDepth"])
            x, y, p = moves[0]["x"], moves[0]["y"], PILL
            if (self.taps_work and not self.app and PILL in self.found and p["x"] <= x <= p["x"] + p["width"]
                    and p["y"] <= y <= p["y"] + p["height"]):
                self.spotlight = True
        elif self.swipes_work and not self.app and not self.spotlight:
            dx = moves[-1]["x"] - moves[0]["x"]
            self.page = min(self.page + 1, self.pages - 1) if dx < 0 else max(self.page - 1, 0)
        time.sleep(max(0, self.replies - self.starts))

    def button(self, body):
        self.buttons.append(body["name"])
        if body["name"] == "home" and self.spotlight:
            self.spotlight = self.spotlight_sticks
        elif body["name"] == "home":
            self.page = 0

    def elements(self, body):
        self.queries.append((body, self.settings["snapshotMaxDepth"]))
        return 200, [{"ELEMENT": f"el{i}", "element-6066-11e4-a52e-4f735466cecf": f"el{i}"}
                     for i in range(len(self.found))]


class FakeStream:
    """An MJPEG server like WebDriverAgent's on port 9100: multipart parts with Content-Length (or without)."""

    def __init__(self, phone, interval=0.005, lengths=True, status=b"200 OK"):
        self.phone, self.interval, self.lengths, self.status = phone, interval, lengths, status
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.clients = []
        self.requests = []
        self.stopped = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            self.requests.append(c.recv(4096))
            c.sendall(b"HTTP/1.0 " + self.status + b"\r\nContent-Type: multipart/x-mixed-replace; "
                      b"boundary=--BoundaryString\r\n\r\n")
            while not self.stopped.is_set():
                jpeg = fake_jpeg(self.phone.value())
                head = b"--BoundaryString\r\nContent-type: image/jpeg\r\n"
                if self.lengths:
                    head += b"Content-Length: %d\r\n" % len(jpeg)
                part = head + b"\r\n" + jpeg + b"\r\n\r\n"
                c.sendall(part[:7])                    # split, so the reader has to join recv()s
                c.sendall(part[7:])
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


def hook_up(server, phone):
    def settings(h, m, b):
        if m == "POST":
            phone.posted.append(dict(b["settings"]))
            phone.settings.update(b["settings"])
        return 200, dict(phone.settings)

    server.hooks.update({
        "/wda/locked": lambda h, m, b: (200, phone.locked),
        "/wda/activeAppInfo": lambda h, m, b: (200, {"bundleId": phone.app or "com.apple.springboard"}),
        "/session/{sid}/actions": lambda h, m, b: phone.gesture(b),
        "/session/{sid}/wda/pressButton": lambda h, m, b: phone.button(b),
        "/session/{sid}/appium/settings": settings,
        "/session/{sid}/elements": lambda h, m, b: phone.elements(b),
    })
    for i, rect in enumerate(phone.found):
        server.hooks[f"/session/{{sid}}/element/el{i}/rect"] = lambda h, m, b, r=rect: (200, dict(r))


def broken_reply(h, m, b):
    """A reply that breaks off in its body, as when the USB forward drops (http.client: IncompleteRead)."""
    h.send_response(200)
    h.send_header("Content-Length", "100")
    h.end_headers()
    h.wfile.write(b'{"value": nu')
    return "drop"


def garbage_reply(h, m, b):
    h.wfile.write(b"garbage\r\n\r\n")                    # http.client: BadStatusLine
    return "drop"


def capture_of(phone, gop=10, h264=None):
    """A fake capture socket showing the phone's screen: fake units, or TinyH264 pictures with h264."""
    if h264 is None:
        return FakeCapture(lambda i: (i % gop == 0, fake_unit(phone.value(), i % gop == 0)), (40, 87, b"\x01avcC"))
    return FakeCapture(lambda i: (True, h264.frame(phone.value())), (h264.width, h264.height, h264.avcc))


class LatencyProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeWDA()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        for name, fake in (("decode", fake_decode), ("decode_h264", fake_decode_h264)):
            self.addCleanup(setattr, latency_probe, name, getattr(latency_probe, name))
            setattr(latency_probe, name, fake)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ)                  # never the real capture socket
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("IPHONE_CAPTURE_SOCKET", None)

    def run_probe(self, phone, *extra, stream=None, hooks=None, capture=None):
        hook_up(self.server, phone)
        self.server.hooks.update(hooks or {})
        if stream is None:
            stream = FakeStream(phone)
        self.addCleanup(stream.close)
        if capture is not None:
            self.addCleanup(capture.close)
            extra = ("--video", "usb", "--capture-socket", capture.path) + extra
        out = []
        rc = latency_probe.main([latency_probe.FLAG, "--url", self.server.url, "--mjpeg", stream.url,
                                 "--delay-scale", "0.02", "--swipes", "2", *extra], log=out.append)
        return rc, "\n".join(out)

    def results(self):
        with open(os.path.join(self.tmp.name, "r.json")) as f:
            return json.load(f)

    def assertOnlyPillTaps(self, phone):
        self.assertTrue(phone.gestures)
        for body in phone.gestures:
            self.assertEqual(len(body["actions"]), 1)
            acts = body["actions"][0]["actions"]
            self.assertEqual([a["type"] for a in acts], ["pointerMove", "pointerDown", "pause", "pointerUp"])
            self.assertLessEqual(acts[2]["duration"], 100)
            self.assertEqual((acts[0]["x"], acts[0]["y"]), (196, 704))          # the middle of the pill
        for path in self.server.paths():
            self.assertFalse(path.endswith(("/click", "/wda/keys", "/wda/lock", "/wda/unlock", "/url",
                                            "/wda/apps/launch", "/wda/touchAndHold", "/value")), path)

    def assertOnlyMiddleSwipes(self, phone):
        self.assertTrue(phone.gestures)
        for body in phone.gestures:
            self.assertEqual(len(body["actions"]), 1)
            acts = body["actions"][0]["actions"]
            self.assertEqual([a["type"] for a in acts[:2]], ["pointerMove", "pointerDown"])
            self.assertEqual(acts[-1]["type"], "pointerUp")
            moves = [a for a in acts if a["type"] == "pointerMove"]
            for m in moves:
                self.assertTrue(W * 0.05 <= m["x"] <= W * 0.95 and H * 0.3 <= m["y"] <= H * 0.7, m)
            self.assertGreater(abs(moves[-1]["x"] - moves[0]["x"]), W * 0.5)
            self.assertEqual(moves[-1]["y"], moves[0]["y"])
        for path in self.server.paths():                 # nothing but these is ever sent
            self.assertFalse(path.endswith(("/click", "/wda/keys", "/wda/lock", "/wda/unlock", "/url",
                                            "/wda/apps/launch", "/wda/touchAndHold")), path)

    def test_refuses_without_the_flag(self):
        out = []
        self.assertEqual(latency_probe.main(["--url", self.server.url], log=out.append), 2)
        self.assertIn("Refusing to run", out[0])
        self.assertEqual(self.server.calls, [])

    def test_measures_reply_and_first_moved_frame(self):
        phone = Phone()
        rc, out = self.run_probe(phone, "--json", os.path.join(self.tmp.name, "r.json"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(phone.gestures), 4)             # two swipes there and back
        self.assertOnlyMiddleSwipes(phone)
        with open(os.path.join(self.tmp.name, "r.json")) as f:
            r = json.load(f)
        self.assertEqual(r["reply_ms"]["n"], 4)
        self.assertEqual(r["moved_ms"]["n"], 4)
        self.assertGreaterEqual(r["reply_ms"]["min"], 75)    # the simulated WebDriverAgent takes 80 ms
        self.assertGreaterEqual(r["moved_ms"]["min"], 25)    # the page changes 30 ms after the request
        self.assertLess(r["moved_ms"]["max"], r["reply_ms"]["min"] + 20)
        self.assertLessEqual(r["reply_ms"]["min"], r["reply_ms"]["median"])
        self.assertLessEqual(r["reply_ms"]["median"], r["reply_ms"]["max"])
        self.assertGreater(r["frame_interval_ms"]["median"], 2)
        self.assertEqual(r["failed"], [])
        for line in ("send -> WDA reply:", "send -> first moved frame:", "MJPEG frame interval:"):
            self.assertIn(line, out)
        self.assertRegex(out, r"send -> WDA reply: +median \d+ ms \(min \d+, max \d+\), 4 swipes")
        self.assertEqual(phone.page, 0)
        self.assertEqual(phone.buttons[-1], "home")             # ends with Home
        self.assertEqual(phone.posted, [])                      # no --settings: nothing changed
        self.assertNotIn("/wda/homescreen", self.server.paths())

    def test_settings_are_applied_then_restored(self):
        phone = Phone()
        rc, out = self.run_probe(phone, "--settings", '{"snapshotMaxDepth": 1, "animationCoolOffTimeout": 0}')
        self.assertEqual(rc, 0, out)
        self.assertEqual(phone.posted, [{"snapshotMaxDepth": 1, "animationCoolOffTimeout": 0},
                                        {"snapshotMaxDepth": 50, "animationCoolOffTimeout": 2}])
        self.assertEqual(phone.settings, SETTINGS)
        self.assertIn('"snapshotMaxDepth": 1', out)              # the settings the run used are printed
        self.assertIn("settings restored", out)
        # the change happened before the first swipe and the restore after the last
        posts = [i for i, (m, p, _) in enumerate(self.server.calls)
                 if m == "POST" and p.endswith("/appium/settings")]
        acts = [i for i, (_, p, _) in enumerate(self.server.calls) if p.endswith("/actions")]
        self.assertLess(posts[0], acts[0])
        self.assertGreater(posts[-1], acts[-1])

    def test_settings_are_restored_when_a_swipe_fails(self):
        phone = Phone()
        boom = {"/session/{sid}/actions": lambda h, m, b: (500, {"error": "unknown error", "message": "boom"})}
        rc, out = self.run_probe(phone, "--settings", '{"animationCoolOffTimeout": 0}', hooks=boom)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", out)
        self.assertIn("boom", out)
        self.assertEqual(phone.settings, SETTINGS)
        self.assertEqual(phone.posted[-1], {"animationCoolOffTimeout": 2})
        self.assertEqual(phone.buttons[-1], "home")

    def test_a_broken_reply_is_an_oserror(self):
        k = latency_probe.Kept(self.server.url)
        for path, hook in (("/status", broken_reply), ("/wda/locked", garbage_reply)):
            with self.subTest(path=path):
                self.server.hooks[path] = hook
                with self.assertRaises(OSError) as cm:
                    k.call("GET", path)
                self.assertIn(f"GET {path}: a broken reply", str(cm.exception))
                self.assertIsNone(k.conn)                        # never reused
        self.server.hooks.clear()
        self.assertEqual(k.call("GET", "/status")["ready"], True)
        self.assertEqual(self.server.paths(), ["/status", "/wda/locked", "/status"])     # nothing sent twice
        k.close()

    def test_settings_and_session_are_put_back_after_broken_replies(self):
        phone = Phone()
        sent = []

        def actions(h, m, b):
            sent.append(b)
            return broken_reply(h, m, b)

        def home(h, m, b):                               # the final Home breaks off too
            return broken_reply(h, m, b) if sent else phone.button(b)
        hooks = {"/session/{sid}/actions": actions, "/session/{sid}/wda/pressButton": home}
        rc, out = self.run_probe(phone, "--settings", '{"animationCoolOffTimeout": 0}',
                                 "--json", os.path.join(self.tmp.name, "r.json"), hooks=hooks)
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(sent), 1)                          # the broken swipe is not sent again
        self.assertIn("FAIL  POST /session/S1/actions: a broken reply: IncompleteRead", out)
        self.assertIn("FAIL  Home: POST /session/S1/wda/pressButton: a broken reply", out)
        self.assertEqual(phone.posted, [{"animationCoolOffTimeout": 0}, {"animationCoolOffTimeout": 2}])
        self.assertEqual(phone.settings, SETTINGS)
        self.assertEqual(self.server.paths("DELETE"), ["/session/S1"])
        self.assertIn("final Home", self.results()["failed"])

    def test_an_unexpected_error_still_cleans_up(self):
        phone = Phone()
        ctrl_c = mock.patch.object(latency_probe.Probe, "swipes", side_effect=KeyboardInterrupt)
        with ctrl_c, self.assertRaises(KeyboardInterrupt):
            self.run_probe(phone, "--settings", '{"animationCoolOffTimeout": 0}')
        self.assertEqual(phone.settings, SETTINGS)
        self.assertEqual(phone.buttons[-1], "home")
        self.assertEqual(self.server.paths("DELETE"), ["/session/S1"])

    def test_refuses_a_scaling_factor_below_100_and_bad_json(self):
        for bad in ('{"mjpegScalingFactor": 50}', "not json", "[1, 2]", "{}"):
            with self.subTest(settings=bad):
                out = []
                rc = latency_probe.main([latency_probe.FLAG, "--url", self.server.url, "--settings", bad],
                                        log=out.append)
                self.assertEqual(rc, 2)
                self.assertIn("Refusing to run", out[0])
        self.assertEqual(self.server.calls, [])
        self.assertEqual(latency_probe.parse_settings('{"mjpegScalingFactor": 100, "snapshotMaxDepth": 1}'),
                         {"mjpegScalingFactor": 100, "snapshotMaxDepth": 1})

    def test_locked_phone_is_left_alone(self):
        phone = Phone(locked=True)
        rc, out = self.run_probe(phone)
        self.assertEqual(rc, 3)
        self.assertIn("locked", out)
        self.assertEqual(phone.gestures, [])
        self.assertEqual(phone.buttons, [])
        self.assertNotIn("/wda/unlock", self.server.paths())

    def test_an_app_in_front_is_never_swiped(self):
        phone = Phone(app="com.example.app")                   # Home does not get out of it (simulated)
        rc, out = self.run_probe(phone)
        self.assertEqual(rc, 1)
        self.assertIn("not on the home screen", out)
        self.assertEqual(phone.gestures, [])

    def test_a_swipe_that_does_not_register_fails(self):
        phone = Phone(swipes_work=False)
        rc, out = self.run_probe(phone)
        self.assertEqual(rc, 1)
        self.assertIn("did not change the screen", out)
        self.assertIn("first moved frame:  none within", out)
        self.assertEqual(len(phone.gestures), 2)               # no swipe back after a swipe that did nothing
        self.assertEqual(phone.page, 0)
        self.assertEqual(phone.buttons[-1], "home")

    def test_reuses_the_windows_session(self):
        phone = Phone()
        with self.server.lock:
            self.server.sessions, self.server.live = 1, "S1"
        self.server.hooks["/status"] = lambda h, m, b: (200, {"ready": True, "os": {"version": "26.5.2"}}, "S1")
        hook_up(self.server, phone)
        stream = FakeStream(phone)
        self.addCleanup(stream.close)
        out = []
        rc = latency_probe.main([latency_probe.FLAG, "--url", self.server.url, "--mjpeg", stream.url,
                                 "--delay-scale", "0.02", "--swipes", "1"], log=out.append)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("/session", self.server.paths("POST"))
        self.assertEqual(self.server.paths("DELETE"), [])
        self.assertIn("the existing session", "\n".join(out))

    def test_own_session_is_deleted(self):
        phone = Phone()
        rc, out = self.run_probe(phone, "--swipes", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("a new session", out)
        self.assertEqual(self.server.paths("DELETE"), ["/session/S1"])

    def test_no_stream_is_not_ready(self):
        phone = Phone()
        hook_up(self.server, phone)
        out = []
        rc = latency_probe.main([latency_probe.FLAG, "--url", self.server.url, "--mjpeg",
                                 f"http://127.0.0.1:{free_port()}", "--delay-scale", "0"], log=out.append)
        self.assertEqual(rc, 3)
        self.assertIn("no frames", out[-1])
        self.assertEqual(phone.gestures, [])

    def test_stream_error_status_is_not_ready(self):
        phone = Phone()
        rc, out = self.run_probe(phone, stream=FakeStream(phone, status=b"403 Forbidden"))
        self.assertEqual(rc, 3)
        self.assertIn("403", out)

    def test_parts_without_content_length(self):
        phone = Phone()
        rc, out = self.run_probe(phone, "--swipes", "1", stream=FakeStream(phone, lengths=False))
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"first moved frame: +median \d+ ms")

    def test_the_stream_request_passes_the_forward_guard(self):
        phone = Phone()
        stream = FakeStream(phone)
        rc, out = self.run_probe(phone, "--swipes", "1", stream=stream)
        self.assertEqual(rc, 0, out)
        req = stream.requests[0].decode()
        port = stream.url.rsplit(":", 1)[1]
        self.assertIn(f"Host: 127.0.0.1:{port}\r\n", req)
        self.assertNotIn("Origin", req)

    def test_unreachable_wda(self):
        out = []
        rc = latency_probe.main([latency_probe.FLAG, "--url", f"http://127.0.0.1:{free_port()}"], log=out.append)
        self.assertEqual(rc, 3)
        self.assertIn("not ready", out[-1])

    def test_without_a_decoder_only_replies_are_timed(self):
        latency_probe.decode = None
        phone = Phone()
        rc, out = self.run_probe(phone, "--swipes", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("first moved frame:  not measured (no JPEG decoder", out)
        self.assertIn("send -> WDA reply:", out)

    def test_moves_and_window_drag(self):
        for extra, check in ((("--moves", "4"), lambda moves: self.assertEqual(moves, 5)),
                             (("--window-drag", "--ms", "600"), lambda moves: self.assertGreaterEqual(moves, 2))):
            with self.subTest(extra=extra):
                self.server.reset()
                phone = Phone()
                rc, out = self.run_probe(phone, "--swipes", "1", *extra)
                self.assertEqual(rc, 0, out)
                self.assertOnlyMiddleSwipes(phone)
                for body in phone.gestures:
                    check(sum(a["type"] == "pointerMove" for a in body["actions"][0]["actions"]))

    def test_swipe_guard(self):
        from iphone_control import geometry
        ok = latency_probe.swipe_gesture(W * 0.85, W * 0.15, H / 2, 250)
        self.assertIs(latency_probe.check_swipe(ok, (W, H)), ok)
        for bad in (geometry.tap(200, 400), geometry.tap(200, 400, hold_ms=800), geometry.double_tap(200, 400),
                    geometry.gesture([(200, 300, 0), (200, 600, 250)]),                 # down the middle: Spotlight
                    geometry.edge_swipe("control_centre", (W, H)), geometry.edge_swipe("app_switcher", (W, H)),
                    geometry.back_swipe((W, H)), geometry.pinch(200, 400, 2, (W, H)),
                    geometry.gesture([(300, 400, 0), (100, 400, 250)], hold_ms=600),     # long press, then drag
                    geometry.gesture([(300, 400, 0), (100, 400, 250)], release_ms=800),
                    geometry.gesture([(300, 400, 0), (280, 400, 250)])):                 # too short
            with self.assertRaises(ValueError, msg=bad):
                latency_probe.check_swipe(bad, (W, H))

    def test_difference_and_spread(self):
        a, b = fake_decode(fake_jpeg(10)), fake_decode(fake_jpeg(120))
        self.assertEqual(latency_probe.difference(a, a), 0)
        self.assertEqual(latency_probe.difference(a, b), 1.0)
        top = [row[:] for row in a]
        top[0] = [(255, 255, 255)] * len(top[0])                  # the status bar does not count
        self.assertEqual(latency_probe.difference(a, top), 0)
        self.assertEqual(latency_probe.spread([3, 1, 2]), {"n": 3, "median": 2, "min": 1, "max": 3})
        self.assertIsNone(latency_probe.spread([]))

    @unittest.skipIf(latency_probe.GDK is None, "needs GdkPixbuf")
    def test_real_jpeg_decoder(self):
        gdk = latency_probe.GDK

        def jpeg(rgb):
            pb = gdk.Pixbuf.new(gdk.Colorspace.RGB, False, 8, 120, 260)
            pb.fill((rgb[0] << 24) | (rgb[1] << 16) | (rgb[2] << 8) | 0xFF)
            ok, data = pb.save_to_bufferv("jpeg", ["quality"], ["60"])
            self.assertTrue(ok)
            return bytes(data)
        dark, light = latency_probe._gdk_grid(jpeg((20, 30, 40))), latency_probe._gdk_grid(jpeg((200, 210, 220)))
        self.assertEqual((len(dark[0]), len(dark)), latency_probe.CELLS)
        self.assertEqual(latency_probe.difference(dark, latency_probe._gdk_grid(jpeg((20, 30, 40)))), 0)
        self.assertEqual(latency_probe.difference(dark, light), 1.0)

    def test_cli_help_mentions_the_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            latency_probe.main(["--help"])
        self.assertIn(latency_probe.FLAG, out.getvalue())
        for option in ("--settings", "--tap", "--video", "--capture-socket"):
            self.assertIn(option, out.getvalue())

    # ---- --tap: the Search pill ----------------------------------------
    def test_tap_times_the_search_pill_and_puts_the_depth_back(self):
        phone = Phone(depth=1)                              # the window's session: INPUT_SETTINGS
        rc, out = self.run_probe(phone, "--tap", "3", "--json", os.path.join(self.tmp.name, "r.json"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(phone.gestures), 3)
        self.assertOnlyPillTaps(phone)
        # found once, at WebDriverAgent's full depth; the taps themselves run at the session's depth
        self.assertEqual(phone.queries, [({"using": "predicate string", "value": latency_probe.PILL_QUERY}, 50)])
        self.assertEqual(phone.tap_depths, [1, 1, 1])
        self.assertEqual(phone.posted, [{"snapshotMaxDepth": 50}, {"snapshotMaxDepth": 1}])
        self.assertEqual(phone.settings, dict(SETTINGS, snapshotMaxDepth=1))
        self.assertEqual(phone.buttons, ["home"] * 6)        # first page, Home after each tap, the final Home
        self.assertFalse(phone.spotlight)
        r = self.results()
        self.assertEqual((r["gesture"], r["video"]), ("tap", "mjpeg"))
        self.assertEqual([t["tap"] for t in r["taps"]], ["1", "2", "3"])
        self.assertEqual((r["reply_ms"]["n"], r["moved_ms"]["n"]), (3, 3))
        self.assertGreaterEqual(r["moved_ms"]["min"], 25)    # Spotlight shows 30 ms after the request
        self.assertGreaterEqual(r["reply_ms"]["min"], 75)
        self.assertNotIn("swipe_ms", r)
        self.assertIn("Search pill at (196, 704), 90 x 30 points", out)
        self.assertRegex(out, r"send -> first moved frame: +median \d+ ms \(min \d+, max \d+\), 3 taps")

    def test_tap_at_full_depth_changes_no_setting(self):
        phone = Phone()
        rc, out = self.run_probe(phone, "--tap", "1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(phone.posted, [])
        self.assertEqual([d for _, d in phone.queries], [50])

    def test_tap_without_the_pill_taps_nothing(self):
        for found in ([], [dict(PILL, y=20.0)], [{"x": 30, "y": 600, "width": 64, "height": 84}],
                      [dict(PILL, x=40.0), dict(PILL, x=260.0)], [{"oops": 1}]):
            with self.subTest(found=found):
                self.server.reset()
                phone = Phone(depth=1, found=found)
                rc, out = self.run_probe(phone, "--tap")
                self.assertEqual(rc, 3, out)
                self.assertIn("not ready:", out)
                self.assertIn("nothing tapped", out)
                self.assertEqual(phone.gestures, [])
                self.assertEqual(phone.posted[-1], {"snapshotMaxDepth": 1})            # the depth is back
                self.assertEqual(phone.buttons[-1], "home")

    def test_tap_on_nested_pill_elements_is_one_tap_target(self):
        phone = Phone(found=(PILL, dict(PILL, x=152.0, width=88.0)))         # e.g. a button inside its container
        rc, out = self.run_probe(phone, "--tap", "1")
        self.assertEqual(rc, 0, out)
        self.assertOnlyPillTaps(phone)

    def test_depth_is_put_back_when_the_lookup_fails(self):
        phone = Phone(depth=1)
        boom = {"/session/{sid}/elements": lambda h, m, b: (500, {"error": "unknown error", "message": "boom"})}
        rc, out = self.run_probe(phone, "--tap", hooks=boom)
        self.assertEqual(rc, 1, out)
        self.assertIn("boom", out)
        self.assertEqual(phone.gestures, [])
        self.assertEqual(phone.posted, [{"snapshotMaxDepth": 50}, {"snapshotMaxDepth": 1}])

    def test_a_tap_that_does_not_open_spotlight_stops(self):
        phone = Phone(taps_work=False)
        rc, out = self.run_probe(phone, "--tap", "3")
        self.assertEqual(rc, 1)
        self.assertIn("did not open Spotlight", out)
        self.assertEqual(len(phone.gestures), 1)
        self.assertEqual(phone.buttons[-1], "home")

    def test_no_tap_while_spotlight_stays_open(self):
        phone = Phone(spotlight_sticks=True)          # Home does not close it: the pill's place is the keyboard now
        rc, out = self.run_probe(phone, "--tap", "3")
        self.assertEqual(rc, 1)
        self.assertIn("Home did not close Spotlight", out)
        self.assertIn("not on the first home page before a tap", out)
        self.assertEqual(len(phone.gestures), 1)                    # never a second tap

    def test_no_tap_in_an_app(self):
        phone = Phone(app="com.example.app")
        rc, out = self.run_probe(phone, "--tap")
        self.assertEqual(rc, 1)
        self.assertEqual(phone.gestures, [])
        self.assertEqual(phone.queries, [])

    def test_tap_needs_a_jpeg_decoder_and_a_sane_count(self):
        latency_probe.decode = None
        for extra in (["--tap"], ["--tap", "0"], ["--tap", "21"]):
            with self.subTest(extra=extra):
                out = []
                self.assertEqual(latency_probe.main([latency_probe.FLAG, "--url", self.server.url, *extra],
                                                    log=out.append), 2)
                self.assertIn("Refusing to run", out[0])
        self.assertEqual(self.server.calls, [])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            latency_probe.main([latency_probe.FLAG, "--tap", "--window-drag"])     # one kind of gesture

    def test_tap_guard(self):
        from iphone_control import geometry
        ok = geometry.tap(196, 704)
        self.assertIs(latency_probe.check_tap(ok, PILL), ok)
        for bad in (geometry.tap(196, 600), geometry.tap(100, 704), geometry.tap(196, 704, hold_ms=800),
                    geometry.double_tap(196, 704), geometry.pinch(196, 704, 2, (W, H)),
                    geometry.gesture([(160, 704, 0), (230, 704, 100)])):
            with self.assertRaises(ValueError, msg=bad):
                latency_probe.check_tap(bad, PILL)

    def test_pick_pill(self):
        self.assertEqual(latency_probe.pick_pill([PILL], (W, H)), (196.0, 704.0, PILL))
        self.assertEqual(latency_probe.pick_pill([{"x": 0}, PILL], (W, H))[:2], (196.0, 704.0))
        for bad in ([], [dict(PILL, width=300.0)], [dict(PILL, height=10.0)], [dict(PILL, y=840.0)],
                    [dict(PILL, x=0.0)], [dict(PILL, width=20.0, height=30.0)], [PILL, dict(PILL, y=600.0)]):
            with self.assertRaises(latency_probe.NotReady, msg=bad):
                latency_probe.pick_pill(bad, (W, H))

    # ---- --video usb: the USB capture ----------------------------------
    def test_usb_video_times_the_capture_after_the_run(self):
        phone = Phone()
        calls = []

        def decoder(fmt, units):
            calls.append((len(phone.gestures), len(phone.buttons), fmt, units[0]))
            return fake_decode_h264(fmt, units)
        latency_probe.decode_h264 = decoder
        rc, out = self.run_probe(phone, "--json", os.path.join(self.tmp.name, "r.json"), capture=capture_of(phone))
        self.assertEqual(rc, 0, out)
        self.assertOnlyMiddleSwipes(phone)
        r = self.results()
        self.assertEqual((r["gesture"], r["video"]), ("swipe", "usb"))
        self.assertEqual((r["moved_ms"]["n"], r["mjpeg_moved_ms"]["n"]), (4, 4))
        self.assertGreaterEqual(r["moved_ms"]["min"], 25)    # the page changes 30 ms after the request
        self.assertLess(r["moved_ms"]["max"], r["reply_ms"]["min"] + 20)
        self.assertEqual([s["moved_ms"] is not None and s["mjpeg_moved_ms"] is not None for s in r["swipes"]],
                         [True] * 4)
        self.assertGreater(r["capture_frame_interval_ms"]["median"], 2)
        self.assertGreater(r["frame_interval_ms"]["median"], 2)          # the MJPEG stream was read all along
        # decoded only after the run (all 4 swipes and the final Home), once per swipe, each from a keyframe
        self.assertEqual(len(calls), 4)
        self.assertEqual({c[:2] for c in calls}, {(4, len(phone.buttons))})
        self.assertEqual({c[2] for c in calls}, {(40, 87, b"\x01avcC")})
        self.assertEqual({c[3][4] for c in calls}, {0x65})
        for line in ("first moved frame (USB capture)", "the same on MJPEG:", "USB capture frame interval:",
                     "MJPEG frame interval:", "USB capture 40x87"):
            self.assertIn(line, out)
        self.assertRegex(out, r"send -> first moved frame: +median \d+ ms \(min \d+, max \d+\), 4 swipes, USB")

    def test_usb_video_and_taps(self):
        phone = Phone()
        rc, out = self.run_probe(phone, "--tap", "2", "--json", os.path.join(self.tmp.name, "r.json"),
                                 capture=capture_of(phone))
        self.assertEqual(rc, 0, out)
        self.assertOnlyPillTaps(phone)
        r = self.results()
        self.assertEqual((r["moved_ms"]["n"], r["mjpeg_moved_ms"]["n"]), (2, 2))

    def test_usb_video_needs_its_socket(self):
        out = []
        rc = latency_probe.main([latency_probe.FLAG, "--url", self.server.url, "--video", "usb"], log=out.append)
        self.assertEqual(rc, 2)
        self.assertIn("--capture-socket", out[0])
        self.assertEqual(self.server.calls, [])

    def test_usb_socket_from_the_environment(self):
        phone = Phone()
        capture = capture_of(phone)
        os.environ["IPHONE_CAPTURE_SOCKET"] = capture.path
        self.addCleanup(capture.close)
        rc, out = self.run_probe(phone, "--swipes", "1", "--video", "usb")
        self.assertEqual(rc, 0, out)
        self.assertTrue(capture.clients)

    def test_unreachable_capture_is_not_ready(self):
        phone = Phone()
        rc, out = self.run_probe(phone, "--video", "usb", "--capture-socket", os.path.join(self.tmp.name, "none"))
        self.assertEqual(rc, 3)
        self.assertIn("no frames from the USB capture", out)
        self.assertEqual(phone.gestures, [])
        self.assertEqual(phone.buttons, [])

    def test_capture_lost_during_the_run_fails(self):
        phone = Phone()
        cap = capture_of(phone)
        cap.drop_after = 0.25                         # as when the capture process stops, or cuts the probe off
        rc, out = self.run_probe(phone, "--swipes", "4", capture=cap)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL  USB capture: the capture socket closed: the capture process stopped", out)
        self.assertEqual(phone.buttons[-1], "home")
        self.assertIn("send -> WDA reply:", out)

    def test_usb_without_an_h264_decoder_times_the_replies(self):
        latency_probe.decode_h264 = None
        phone = Phone()
        rc, out = self.run_probe(phone, "--swipes", "1", capture=capture_of(phone))
        self.assertEqual(rc, 0, out)
        self.assertIn("first moved frame:  not measured (no H.264 decoder", out)
        self.assertIn("the same on MJPEG:", out)

    def test_capture_keeps_each_mark_from_the_keyframe_before(self):
        phone = Phone()
        cap = capture_of(phone, gop=4)
        self.addCleanup(cap.close)
        c = latency_probe.Capture(cap.path)
        c.start()
        self.addCleanup(c.stop)
        self.assertTrue(c.wait_picture(5))
        self.assertEqual(c.format, (40, 87, b"\x01avcC"))
        time.sleep(0.1)
        seg = c.mark()
        time.sleep(0.05)
        c.release()
        kept = len(c.segments[seg][1])
        time.sleep(0.1)                                    # nothing is kept between marks
        self.assertEqual(len(c.segments[seg][1]), kept)
        units = [d for _, d in c.segments[seg][1]]
        self.assertEqual(units[0][4], 0x65)                # starts at a keyframe...
        self.assertEqual(sum(u[4] == 0x65 for u in units[:4]), 1)   # ...the last one before the mark
        self.assertGreater(len(c.times), 10)
        c.stop()
        self.assertIsNone(c.error)

    def test_moved_after(self):
        g = {v: fake_decode(bytes([0xff, 0xd8, v])) for v in (10, 120)}
        frames = [(1.0, g[10]), (2.0, g[10]), (3.0, None), (4.0, g[120])]
        self.assertEqual(latency_probe.moved_after(frames, 2.5, 0.05), 4.0)
        self.assertEqual(latency_probe.moved_after(frames, 1.5, 0.05), 4.0)
        self.assertIsNone(latency_probe.moved_after(frames, 4.0, 0.05))
        self.assertIsNone(latency_probe.moved_after(frames[2:], 2.5, 0.05))     # nothing before t0 to compare with
        self.assertEqual(latency_probe.intervals([0, 0.01, 0.03])["median"], 15.0)
        self.assertIsNone(latency_probe.intervals([1.0]))

    def test_capture_reads_beside_the_window_until_the_socket_is_full(self):
        from iphone_control import screencap, valeria
        path = os.path.join(self.tmp.name, "cap.sock")
        server = screencap.Server(path, lambda m: None)                  # the real one, fed by hand
        self.addCleanup(server.close)
        fmt = {"width": 40, "height": 87, "avcc": b"\x01avcC"}
        server.event(valeria.Video(fake_unit(10, True), 0, True, fmt))
        others = []
        for _ in range(screencap.MAX_CLIENTS - 1):                     # the window, and more
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(s.close)
            s.connect(path)
            others.append(s)
        self.assertTrue(wait_until(lambda: len(server.clients) == screencap.MAX_CLIENTS - 1))
        c = latency_probe.Capture(path)
        c.start()
        self.addCleanup(c.stop)
        self.assertTrue(c.wait_picture(5))
        self.assertEqual(c.format, (40, 87, b"\x01avcC"))
        seg = c.mark()
        server.event(valeria.Video(fake_unit(120, False), 1, False))
        self.assertTrue(wait_until(lambda: len(c.segments[seg][1]) == 2))
        c.release()
        window = others[0]                                             # still has its picture, frame for frame
        window.settimeout(5)
        frames = screencap.read_frames(window)
        self.assertEqual([next(frames)[0] for _ in range(3)], [screencap.VIDEO_FORMAT, screencap.VIDEO,
                                                                screencap.VIDEO])
        late = latency_probe.Capture(path)                             # one client more than it takes
        late.start()
        self.addCleanup(late.stop)
        self.assertFalse(late.wait_picture(5))
        self.assertIn("closed before any frame: the capture process already serves as many clients", late.error)
        self.assertIsNone(c.error)
        server.close()                                                 # the capture process stops
        c.join(5)
        self.assertIn("the capture socket closed: the capture process stopped", c.error)

    def test_capture_frames_match_screencap(self):
        from iphone_control import screencap
        self.assertEqual(latency_probe.CAPTURE_HEADER.format, screencap.HEADER.format)
        self.assertEqual((latency_probe.VIDEO_FORMAT, latency_probe.VIDEO), (screencap.VIDEO_FORMAT, screencap.VIDEO))
        self.assertEqual(FakeCapture.HEADER.format, screencap.HEADER.format)

    @unittest.skipIf(latency_probe.GST is None, "needs GStreamer with h264parse and avdec_h264")
    def test_real_h264_decoder(self):
        h264 = TinyH264()
        units = [h264.frame(v) for v in (10, 10, 120, 200)]
        grids = latency_probe._gst_grids((h264.width, h264.height, h264.avcc), units)
        self.assertTrue(all(grids), grids)
        self.assertEqual((len(grids[0][0]), len(grids[0])), latency_probe.CELLS)
        self.assertEqual(latency_probe.difference(grids[0], grids[1]), 0)
        self.assertEqual(latency_probe.difference(grids[1], grids[2]), 1.0)
        self.assertEqual(latency_probe.difference(grids[2], grids[3]), 1.0)
        self.assertEqual(latency_probe._gst_grids((h264.width, h264.height, h264.avcc), []), [])

    @unittest.skipIf(latency_probe.GST is None, "needs GStreamer with h264parse and avdec_h264")
    def test_simulated_run_with_the_real_h264_decoder(self):
        latency_probe.decode_h264 = latency_probe._gst_grids
        phone = Phone()
        rc, out = self.run_probe(phone, "--swipes", "1", "--json", os.path.join(self.tmp.name, "r.json"),
                                 capture=capture_of(phone, h264=TinyH264()))
        self.assertEqual(rc, 0, out)
        r = self.results()
        self.assertEqual(r["moved_ms"]["n"], 2)
        self.assertGreaterEqual(r["moved_ms"]["min"], 25)


if __name__ == "__main__":
    unittest.main()
