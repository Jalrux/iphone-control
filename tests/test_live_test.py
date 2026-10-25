"""tests/live_test.py against a simulated SpringBoard (a fake WebDriverAgent that keeps screen state).

Checks the live test's own safety: it refuses without the flag, never unlocks, taps or presses Return,
ends on the home screen, and reports failures instead of crashing.
"""
import contextlib
import io
import os
import random
import struct
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import live_test  # noqa: E402
from support import FakeWDA, free_port  # noqa: E402
from iphone_control import geometry  # noqa: E402

W, H = 393, 852
COLOURS = {"home0": 10, "home1": 50, "today": 90, "spotlight": 130, "control_centre": 170, "app_switcher": 210}


def fast_png(width, height, row_value):
    """An RGB PNG built row by row: row_value(y) -> one byte value for the whole row."""
    raw = b"".join(b"\0" + bytes([row_value(y)]) * (3 * width) for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


class Phone:
    """What the simulated phone shows; changed by the fake WebDriverAgent's hooks."""

    def __init__(self, pages=2, locked=False, spotlight_works=True, noise=0, empty_switcher=False):
        self.screen, self.page, self.text, self.hud = "home", 0, "", False
        self.pages, self.locked, self.spotlight_works = pages, locked, spotlight_works
        self.noise = noise                                     # iOS 26 never redraws a screen exactly the same
        self.empty_switcher = empty_switcher                   # no apps: it closes again before the shot
        self.gestures, self.keys = [], []

    def name(self):
        return f"home{self.page}" if self.screen == "home" else self.screen

    def png(self):
        base = COLOURS[self.name()]
        clock = random.randrange(256)
        jitter = [random.randint(-self.noise, self.noise) for _ in range(H)] if self.noise else None
        text_rows = range(120, 120 + 120 * len(self.text)) if self.screen == "spotlight" else range(0)

        def row(y):
            if y < H * 0.05:
                return clock                                   # the status bar changes all the time
            if y in text_rows:
                return 250
            if self.hud and 150 <= y < 360:
                return 220
            return min(255, max(0, base + y // 50 + (jitter[y] if self.noise else 0)))
        return fast_png(W, H, row)

    def gesture(self, body):
        self.gestures.append(body)
        acts = body["actions"][0]["actions"]
        x0, y0 = acts[0]["x"], acts[0]["y"]
        moves = [a for a in acts[1:] if a["type"] == "pointerMove"]
        dx, dy = moves[-1]["x"] - x0, moves[-1]["y"] - y0
        if self.screen != "home":
            return
        if y0 <= 5 and x0 > W / 2 and dy > 0:
            self.screen = "control_centre"
        elif y0 >= H - 5 and dy < 0:
            self.screen = "home" if self.empty_switcher else "app_switcher"
        elif abs(dx) > abs(dy):
            if dx < 0:
                self.page = min(self.page + 1, self.pages - 1)
            elif self.page == 0:
                self.screen = "today"
            else:
                self.page -= 1
        elif dy > 0 and self.spotlight_works:
            self.screen = "spotlight"

    def type(self, value):
        self.keys.extend(value)
        if self.screen == "spotlight":
            for ch in value:
                self.text = self.text[:-1] if ch == "\b" else self.text + ch


def hook_up(server, phone):
    import base64

    def button(h, m, b):
        if b["name"] == "home":
            phone.screen, phone.page, phone.text = "home", 0, ""
        phone.hud = b["name"] == "volumeUp"

    server.hooks.update({
        "/wda/homescreen": lambda h, m, b: None,     # like iOS 26: a no-op while SpringBoard is in front
        "/wda/locked": lambda h, m, b: (200, phone.locked),
        "/wda/activeAppInfo": lambda h, m, b: (200, {"bundleId": "com.apple.springboard"}),
        "/screenshot": lambda h, m, b: (200, base64.b64encode(phone.png()).decode()),
        "/session/{sid}/actions": lambda h, m, b: phone.gesture(b),
        "/session/{sid}/wda/keys": lambda h, m, b: phone.type(b["value"]),
        "/session/{sid}/wda/pressButton": button,
        "/session/{sid}/elements": lambda h, m, b: (200, [{"ELEMENT": "sf1"}] if phone.screen == "spotlight" else []),
        "/session/{sid}/element/sf1/attribute/value": lambda h, m, b: (200, phone.text or None),
    })


class LiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeWDA()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_live(self, phone, *extra):
        hook_up(self.server, phone)
        out = []
        rc = live_test.main([live_test.FLAG, "--url", self.server.url, "--out", self.tmp.name, "--delay-scale", "0",
                             *extra], log=out.append)
        return rc, "\n".join(out)

    def assertOnlySwipes(self, phone):
        for body in phone.gestures:
            for finger in body["actions"]:
                acts = finger["actions"]
                moves = [a for a in acts if a["type"] == "pointerMove"][1:]
                self.assertTrue(moves, body)
                far = max(abs(m["x"] - acts[0]["x"]) + abs(m["y"] - acts[0]["y"]) for m in moves)
                self.assertGreaterEqual(far, 2 * geometry.TAP_MAX_MOVE, body)
        self.assertNotIn("\n", phone.keys)                     # Return would launch a search result
        self.assertNotIn("/wda/unlock", self.server.paths())
        self.assertFalse([p for p in self.server.paths() if "/element/" in p and p.endswith("/click")])

    def test_refuses_without_the_flag(self):
        out = []
        self.assertEqual(live_test.main(["--url", self.server.url], log=out.append), 2)
        self.assertIn("Refusing to run", out[0])
        self.assertEqual(self.server.calls, [])

    def test_full_run_on_a_working_phone(self):
        phone = Phone()
        rc, out = self.run_live(phone)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("FAIL", out)
        for check in ("home screen", "swipe to the next home page", "swipe back to the first page", "Spotlight opens",
                      "search field has the text", "Backspace deleted the text", "Spotlight closes",
                      "Control Centre opens", "Control Centre closes", "App Switcher opens", "App Switcher closes",
                      "screenshot", "volume up, then down", "back on the home screen"):
            self.assertIn(f"PASS  {check}:", out)
        self.assertEqual(phone.screen, "home")
        self.assertEqual(phone.keys, list(live_test.PROBE_TEXT) + ["\b"] * len(live_test.PROBE_TEXT))
        self.assertOnlySwipes(phone)
        self.assertIn("DELETE", [m for m, _p, _b in self.server.calls])
        self.assertTrue(len(os.listdir(self.tmp.name)) >= 10)

    def test_redraw_noise_still_counts_as_the_same_screen(self):
        phone = Phone(noise=12)
        rc, out = self.run_live(phone)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS  swipe back to the first page:", out)
        self.assertIn("PASS  back on the home screen:", out)

    def test_home_is_the_button_not_the_homescreen_endpoint(self):
        phone = Phone()
        self.run_live(phone)
        self.assertNotIn("/wda/homescreen", self.server.paths())     # a no-op while Spotlight is open
        self.assertIn({"name": "home"}, self.server.bodies("/pressButton"))

    def test_an_empty_app_switcher_is_a_warning(self):
        phone = Phone(empty_switcher=True)
        rc, out = self.run_live(phone)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN  App Switcher opens:", out)

    def test_locked_phone_is_left_alone(self):
        phone = Phone(locked=True)
        rc, out = self.run_live(phone)
        self.assertEqual(rc, 3)
        self.assertIn("the iPhone is locked", out)
        self.assertEqual(phone.gestures, [])
        self.assertNotIn("/wda/unlock", self.server.paths())

    def test_single_home_page_is_a_warning(self):
        phone = Phone(pages=1)
        rc, out = self.run_live(phone)
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN  home pages", out)
        self.assertNotIn("today", [phone.name()])
        self.assertOnlySwipes(phone)

    def test_a_failing_step_is_reported_and_the_phone_left_home(self):
        phone = Phone(spotlight_works=False)
        rc, out = self.run_live(phone)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  Spotlight opens", out)
        self.assertEqual(phone.screen, "home")
        self.assertEqual(phone.keys, [])                        # nothing typed when Spotlight did not open

    def test_unreachable_wda(self):
        out = []
        rc = live_test.main([live_test.FLAG, "--url", f"http://127.0.0.1:{free_port()}", "--out", self.tmp.name],
                            log=out.append)
        self.assertEqual(rc, 3)
        self.assertIn("not ready", out[-1])

    def test_gesture_guard(self):
        live = live_test.Live(self.server.url, self.tmp.name, 0, log=lambda m: None)
        for bad in (geometry.tap(100, 100), geometry.tap(100, 100, hold_ms=800), geometry.double_tap(5, 5),
                    geometry.drag_to_gesture([(100, 100, 0), (101, 100, 0.9)]),
                    geometry.drag_to_gesture([(100, 100, 0), (101, 100, 0.8), (300, 100, 1.0)])):
            with self.assertRaises(ValueError):
                live.gesture(bad)
        self.assertEqual(self.server.calls, [])

    def test_difference_ignores_the_status_bar_and_small_changes(self):
        gdk = live_test.GDK
        self.addCleanup(setattr, live_test, "GDK", gdk)
        for decoder in ([gdk] if gdk else []) + [None]:             # GdkPixbuf when present, and the stdlib one
            with self.subTest(gdkpixbuf=decoder is not None):
                live_test.GDK = decoder
                a = fast_png(20, 100, lambda y: 1 if y < 5 else 50)
                b = fast_png(20, 100, lambda y: 200 if y < 5 else 50)
                c = fast_png(20, 100, lambda y: 150)
                self.assertEqual(live_test.difference(a, b), 0)
                self.assertEqual(live_test.difference(a, fast_png(20, 100, lambda y: 1 if y < 5 else 70)), 0)
                self.assertEqual(live_test.difference(a, c), 1.0)
                self.assertEqual(live_test.difference(a, fast_png(10, 100, lambda y: 50)), 1.0)
                with self.assertRaises(ValueError):
                    live_test.png_size(b"GIF89a")

    def test_cli_help_mentions_the_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            live_test.main(["--help"])
        self.assertIn(live_test.FLAG, out.getvalue())


if __name__ == "__main__":
    unittest.main()
