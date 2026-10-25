"""WDA client against a fake WebDriverAgent (no phone needed)."""
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from support import FakeWDA, free_port, make_png  # noqa: E402
from iphone_control import wda as W  # noqa: E402
from iphone_control.wda import WDA, WDAError  # noqa: E402


class Pure(unittest.TestCase):
    def test_key_maps(self):
        self.assertEqual(W.KEYS, {"Return": "\n", "KP_Enter": "\n", "ISO_Enter": "\n", "BackSpace": "\b", "Tab": "\t"})
        self.assertTrue(all(isinstance(v, W.NamedKey) for v in W.NAMED_KEYS.values()))
        self.assertTrue(all(v.xcui.startswith("XCUIKeyboardKey") for v in W.NAMED_KEYS.values()))
        self.assertEqual(W.NAMED_KEYS["Left"], ("XCUIKeyboardKeyLeftArrow", 0x50))
        self.assertEqual(W.NAMED_KEYS["KP_Down"], W.NamedKey(xcui="XCUIKeyboardKeyDownArrow", usage=0x51))
        self.assertEqual(W.NAMED_KEYS["Delete"].xcui, "XCUIKeyboardKeyForwardDelete")   # BackSpace stays "\b"
        self.assertEqual(W.NAMED_KEYS["ISO_Left_Tab"].xcui, "XCUIKeyboardKeyTab")       # Shift+Tab
        self.assertNotIn("BackSpace", W.NAMED_KEYS)
        self.assertFalse(set(W.KEYS) & set(W.NAMED_KEYS))
        self.assertEqual(W.SHIFT, 2)

    def test_hid_usages(self):
        # USB HID keyboard usages (page 7), as tried on the phone with /wda/performIoHidEvent
        usages = {"Right": 0x4F, "Left": 0x50, "Down": 0x51, "Up": 0x52, "Home": 0x4A, "End": 0x4D, "Page_Up": 0x4B,
                  "Page_Down": 0x4E, "Escape": 0x29, "Delete": 0x4C, "ISO_Left_Tab": 0x2B}
        self.assertEqual({k: W.NAMED_KEYS[k].usage for k in usages}, usages)
        for name, key in W.NAMED_KEYS.items():
            if name.startswith("KP_"):                   # the keypad's keys do what the main ones do
                self.assertEqual(key, W.NAMED_KEYS[name[3:]], name)
        self.assertEqual(len({k.xcui for k in W.NAMED_KEYS.values()}), len({k.usage for k in W.NAMED_KEYS.values()}))
        self.assertEqual((W.HID_KEYBOARD, W.HID_PRESS_S), (7, 0.01))

    def test_text_helpers(self):
        self.assertEqual(W.normalise_text("a\r\nb\rc\n"), "a\nb\nc\n")
        self.assertEqual([len(c) for c in W.type_chunks("x" * 1100)], [500, 500, 100])
        self.assertEqual(W.type_chunks(""), [])
        self.assertEqual((W.TYPE_RATE, W.TYPE_RATE_FAST, W.WDA_TYPE_RATE), (120, 300, 60))
        self.assertEqual(W.typing_timeout(120), 11)
        self.assertEqual(W.typing_timeout(300, W.TYPE_RATE_FAST), 11)
        self.assertEqual(W.typing_timeout(60, W.WDA_TYPE_RATE), 11)
        for rate in (W.TYPE_RATE, W.TYPE_RATE_FAST):
            self.assertGreater(W.typing_timeout(W.TYPE_CHUNK, rate), W.TYPE_CHUNK / rate)

    def test_merging_keeps_each_rate_and_its_timeout(self):
        p = WDA("http://127.0.0.1:1")                     # the worker never runs: only the queue is read
        p.type("a" * 300, fast=True)
        p.type("b" * 100, fast=True)
        p.type("c")
        p.type("d")
        p.send("POST", "/session/{sid}/wda/keys", {"value": ["e"]})       # no frequency: WDA's own rate
        p.send("POST", "/session/{sid}/wda/keys", {"value": ["f"]})
        first = p._next()
        self.assertEqual(first.body, {"value": list("a" * 300 + "b" * 100), "frequency": 300})
        self.assertEqual(first.timeout, W.typing_timeout(400, W.TYPE_RATE_FAST))
        second = p._next()
        self.assertEqual(second.body, {"value": ["c", "d"], "frequency": 120})
        self.assertEqual(second.timeout, W.typing_timeout(2, W.TYPE_RATE))
        third = p._next()
        self.assertEqual(third.body, {"value": ["e", "f"]})
        self.assertEqual(third.timeout, max(30, W.typing_timeout(2, W.WDA_TYPE_RATE)))

    def test_odd_keys_bodies_are_never_merged(self):
        p = WDA("http://127.0.0.1:1")
        p.send("POST", "/session/{sid}/wda/keys", None)
        p.type("x")
        self.assertIsNone(p._next().body)                  # passed on as it is (WDA answers with an error)
        self.assertEqual(p._next().body, {"value": ["x"], "frequency": W.TYPE_RATE})

    def test_error_parsing(self):
        e = W._error(404, "Not Found", b'{"value": {"error": "unknown command", "message": "m"}}')
        self.assertIsInstance(e, OSError)
        self.assertEqual((e.code, e.error, e.message), (404, "unknown command", "m"))
        self.assertEqual(W._error(500, "Server Error", b"<html>").message, "Server Error")
        self.assertEqual(W._error(500, "x", b'{"value": "plain text"}').message, "plain text")
        self.assertEqual(len(W._error(500, "x", b'{"value": {"message": "%s"}}' % (b"y" * 999)).message), 300)

    def test_urls(self):
        for bad in ("ftp://127.0.0.1:8100", "127.0.0.1:8100", "http://", ""):
            with self.assertRaises(ValueError, msg=bad):
                WDA(bad)
        self.assertEqual(WDA("http://127.0.0.1:8100/").url, "http://127.0.0.1:8100")
        self.assertEqual(WDA("https://localhost")._port, 443)
        self.assertEqual(WDA("http://[::1]:8100")._host, "::1")


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeWDA()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        self.logs = []
        self.phone = WDA(self.server.url, log=self.logs.append).connect({"mjpegScalingFactor": 60})

    def tearDown(self):
        self.phone.close()

    def flush(self):
        done = threading.Event()
        self.phone.send("GET", "/status", None, lambda _r: done.set())
        self.assertTrue(done.wait(10), "the worker is stuck")

    def wait_result(self, start):
        """start(callback) -> the value the worker passed to the callback."""
        out, ev = [], threading.Event()
        start(lambda *a: (out.append(a[0] if a else None), ev.set()))
        self.assertTrue(ev.wait(10))
        return out[0]

    def actions_sent(self):
        return [p for m, p in ((m, p) for m, p, _ in self.server.calls) if m == "POST" and p != "/session"
                and not p.endswith("/appium/settings")]

    # ---- session ------------------------------------------------------
    def test_connect_reads_size_and_applies_mjpeg_settings(self):
        self.assertEqual(self.phone.size, (393, 852))
        self.assertEqual(self.server.bodies("/appium/settings"), [{"settings": {"mjpegScalingFactor": 60}}])
        self.assertEqual(self.phone.sid, "S1")
        self.assertEqual(self.phone.status()["ready"], True)

    def test_expired_session_is_replaced_and_command_retried(self):
        old = self.phone.sid
        self.server.live = "someone-else"            # WDA restarted: our session is gone
        self.phone.actions({"actions": ["x"]})
        self.flush()
        self.assertNotEqual(self.phone.sid, old)
        retried = [p for p in self.server.paths() if p == f"/session/{self.phone.sid}/actions"]
        self.assertEqual(len(retried), 1)
        self.assertEqual(self.logs, [])

    def test_new_session_reapplies_the_settings(self):
        self.server.live = "gone"
        self.phone.home()
        self.phone.actions({"actions": []})
        self.flush()
        new = self.phone.sid
        self.assertIn(("POST", f"/session/{new}/appium/settings", {"settings": {"mjpegScalingFactor": 60}}),
                      self.server.calls)

    def test_unknown_command_does_not_open_a_new_session(self):
        self.server.hooks["/session/{sid}/nope"] = lambda h, m, b: (404, {"error": "unknown command", "message": "x"})
        self.phone.send("POST", "/session/{sid}/nope", {})
        self.flush()
        self.assertEqual([p for p in self.server.paths("POST") if p == "/session"], ["/session"])  # connect()'s only
        self.assertEqual(self.phone.sid, "S1")
        self.assertTrue(any("unknown command: x" in m for m in self.logs), self.logs)

    def test_session_cannot_be_reopened(self):
        self.server.live = "gone"
        self.server.hooks["/session"] = lambda h, m, b: (500, {"error": "session not created", "message": "locked"})
        got = self.wait_result(lambda cb: self.phone.send("POST", "/session/{sid}/actions", {}, cb))
        self.assertIsNone(got)
        self.assertTrue(any("could not reopen WDA session" in m for m in self.logs), self.logs)

    def test_input_settings(self):
        self.assertEqual(W.INPUT_SETTINGS, {"snapshotMaxDepth": 1, "animationCoolOffTimeout": 0})
        # measured to bring nothing or to cost time: never set them for speed
        for slow in ("waitForIdleTimeout", "defaultActiveApplication", "shouldWaitForQuiescence"):
            self.assertNotIn(slow, W.INPUT_SETTINGS)
        self.assertGreater(W.DEEP, W.INPUT_SETTINGS["snapshotMaxDepth"])

    def shallow(self):
        self.phone.close()
        self.server.reset()
        settings = dict(W.INPUT_SETTINGS, mjpegScalingFactor=100)
        self.phone = WDA(self.server.url, log=self.logs.append).connect(settings)
        return self.phone

    def settings_and(self, word):
        """The settings bodies and the requests whose path has word, in the order they arrived."""
        return [b["settings"] if p.endswith("/appium/settings") else word for m, p, b in self.server.calls
                if p.endswith("/appium/settings") or word in p]

    def test_shift_keys_run_at_the_full_snapshot_depth(self):
        self.shallow().key(W.NAMED_KEYS["Left"], shift=True)
        self.phone.actions({"actions": [1]})
        self.flush()
        self.assertEqual(self.settings_and("keyboardInput")[1:], [{"snapshotMaxDepth": W.DEEP}, "keyboardInput",
                                                                 {"snapshotMaxDepth": 1}])
        self.assertEqual(len(self.server.bodies("/appium/settings")), 3)   # connect + around the key, not /actions
        self.assertEqual(self.logs, [])

    def test_plain_named_keys_are_hid_events_with_no_depth_round_trips(self):
        self.shallow()
        for name in ("Left", "Escape", "Page_Down", "Delete"):
            self.phone.key(W.NAMED_KEYS[name])
        self.phone.key("XCUIKeyboardKeyUpArrow")                    # by name: the same HID event
        self.flush()
        self.assertEqual(self.server.hid, [(7, 0x50, 0.01), (7, 0x29, 0.01), (7, 0x4E, 0.01), (7, 0x4C, 0.01),
                                           (7, 0x52, 0.01)])
        self.assertEqual(self.server.bodies("/performIoHidEvent")[0], {"page": 7, "usage": 0x50, "duration": 0.01})
        self.assertTrue(all(p == f"/session/{self.phone.sid}/wda/performIoHidEvent" for p in self.server.paths()
                            if "IoHid" in p))
        self.assertEqual(len(self.server.bodies("/appium/settings")), 1)        # connect()'s only
        self.assertEqual(self.server.bodies("/keyboardInput"), [])
        self.assertEqual(self.logs, [])

    def test_a_shift_key_after_a_wda_restart_still_runs_deep(self):
        self.shallow()
        self.server.live = "someone-else"            # a new session reset WDA's depth; ours is gone
        self.phone.key("XCUIKeyboardKeyLeftArrow", shift=True)
        self.flush()
        order = self.settings_and("keyboardInput")
        last_key = len(order) - 1 - order[::-1].index("keyboardInput")
        before = [b for b in order[:last_key] if b != "keyboardInput"]
        self.assertEqual(before[-1], {"snapshotMaxDepth": W.DEEP})        # set again after reopening (depth 1)
        self.assertEqual(order[-1], {"snapshotMaxDepth": 1})

    def test_depth_is_restored_when_the_key_fails(self):
        self.shallow()
        self.server.hooks["/session/{sid}/wda/element/0/keyboardInput"] = lambda h, m, b: (
            500, {"error": "unknown error", "message": "no focus"})
        errs = []
        self.phone.key("XCUIKeyboardKeyLeftArrow", shift=True, fail=errs.append)
        self.flush()
        self.assertEqual(errs[0].message, "no focus")
        self.assertEqual(self.server.bodies("/appium/settings")[-1], {"settings": {"snapshotMaxDepth": 1}})

    def test_no_depth_change_when_the_depth_was_never_lowered(self):
        self.phone.key("XCUIKeyboardKeyLeftArrow", shift=True)
        self.flush()
        self.assertEqual(len(self.server.bodies("/appium/settings")), 1)        # connect()'s only
        self.assertEqual(len(self.server.bodies("/keyboardInput")), 1)

    def test_a_hid_key_after_a_wda_restart_is_sent_again_on_the_new_session(self):
        self.shallow()
        self.server.live = "someone-else"
        self.phone.key(W.NAMED_KEYS["Right"])
        self.flush()
        self.assertNotEqual(self.phone.sid, "S1")
        self.assertEqual(self.server.hid, [(7, 0x4F, 0.01)])                   # the old session's try was refused
        self.assertEqual(self.server.paths()[-2], f"/session/{self.phone.sid}/wda/performIoHidEvent")
        self.assertNotIn({"settings": {"snapshotMaxDepth": W.DEEP}}, self.server.bodies("/appium/settings"))
        self.assertEqual(self.logs, [])

    def test_rotation_setting_is_dropped_on_an_old_wda(self):
        def old_wda(_h, _m, body):
            if "mjpegFixOrientation" in body["settings"]:
                return 500, {"error": "invalid argument", "message": "unknown setting"}
        self.server.reset()
        self.server.hooks["/session/{sid}/appium/settings"] = old_wda
        logs = []
        p = WDA(self.server.url, log=logs.append).connect({"mjpegScalingFactor": 50, "mjpegFixOrientation": True})
        try:
            self.assertEqual(self.server.bodies("/appium/settings")[-1], {"settings": {"mjpegScalingFactor": 50}})
            self.assertTrue(any("rotation not supported" in m for m in logs), logs)
        finally:
            p.close()

    # ---- commands -----------------------------------------------------
    def test_commands_reach_the_right_endpoints_in_order(self):
        self.phone.home()
        self.phone.type("hi\n")
        self.phone.button("volumeUp")
        self.phone.lock()
        self.phone.actions({"actions": []})
        self.phone.open_url("https://example.com")
        self.phone.launch("com.apple.Preferences")
        self.phone.unlock()
        self.flush()
        sid = self.phone.sid
        self.assertEqual(self.actions_sent(), [
            f"/session/{sid}/wda/pressButton", f"/session/{sid}/wda/keys", f"/session/{sid}/wda/pressButton", "/wda/lock",
            f"/session/{sid}/actions", f"/session/{sid}/url", f"/session/{sid}/wda/apps/launch", "/wda/unlock"])
        self.assertEqual(self.server.bodies("/wda/keys"), [{"value": ["h", "i", "\n"], "frequency": W.TYPE_RATE}])
        self.assertEqual(self.server.bodies("/pressButton"), [{"name": "home"}, {"name": "volumeUp"}])
        self.assertEqual(self.server.bodies("/url"), [{"url": "https://example.com"}])
        self.assertEqual(self.server.bodies("/apps/launch"), [{"bundleId": "com.apple.Preferences"}])

    def test_named_keys_with_and_without_shift(self):
        self.phone.key(W.NAMED_KEYS["Left"])
        self.phone.key(W.NAMED_KEYS["ISO_Left_Tab"], shift=True)
        self.phone.key("XCUIKeyboardKeyRightArrow", shift=True)
        self.phone.key("XCUIKeyboardKeyF1")                   # no HID usage known for it: keyboardInput
        self.flush()
        self.assertEqual(self.server.bodies("/performIoHidEvent"), [{"page": 7, "usage": 0x50, "duration": 0.01}])
        self.assertEqual(self.server.bodies("/keyboardInput"), [
            {"keys": [{"key": "XCUIKeyboardKeyTab", "modifierFlags": 2}]},
            {"keys": [{"key": "XCUIKeyboardKeyRightArrow", "modifierFlags": 2}]},
            {"keys": ["XCUIKeyboardKeyF1"]}])
        self.assertTrue(all(p.endswith("/wda/element/0/keyboardInput") for p in self.server.paths()
                            if "keyboardInput" in p))

    def test_unsupported_named_key_calls_fail_then_done_never_logs(self):
        # window.py turns named keys off on "unsupported": that error must reach fail(), from either route
        for route, shift in (("/session/{sid}/wda/performIoHidEvent", False),
                             ("/session/{sid}/wda/element/0/keyboardInput", True)):
            with self.subTest(route=route):
                self.server.hooks[route] = lambda h, m, b: (
                    500, {"error": "unsupported operation", "message": "typeKey API is only supported since Xcode15"})
                errs, ev = [], threading.Event()
                self.phone.key(W.NAMED_KEYS["Left"], shift=shift, fail=lambda e: (errs.append(e), ev.set()))
                self.assertTrue(ev.wait(5))
                self.flush()
                self.assertIsInstance(errs[0], WDAError)
                self.assertEqual((errs[0].code, errs[0].error), (500, "unsupported operation"))
                self.assertEqual(self.logs, [])

    def test_fail_then_done_order(self):
        order, ev = [], threading.Event()
        self.server.hooks["/wda/homescreen"] = lambda h, m, b: (500, {"error": "unknown error", "message": "boom"})
        self.phone.send("POST", "/wda/homescreen", {}, done=lambda r: (order.append(("done", r)), ev.set()),
                        fail=lambda e: order.append(("fail", e.message)))
        self.assertTrue(ev.wait(5))
        self.assertEqual(order, [("fail", "boom"), ("done", None)])

    def test_typing_is_merged_under_a_slow_keyboard(self):
        self.server.slow_keys = 0.3
        for ch in "hello world":
            self.phone.type(ch)
        self.flush()
        keys = ["".join(b["value"]) for b in self.server.bodies("/wda/keys")]
        self.assertEqual("".join(keys), "hello world")
        self.assertLess(len(keys), 5)
        self.assertEqual({f for _, f in self.server.typed}, {W.TYPE_RATE})

    def test_pasted_text_is_typed_fast_and_never_merged_with_typing(self):
        gate = threading.Event()
        self.server.hooks["/wda/gate"] = lambda h, m, b: gate.wait(5) and None
        self.phone.send("GET", "/wda/gate")   # the worker waits here while the rest queues
        self.phone.type("a")
        self.phone.type("b")
        self.phone.type("PASTE", fast=True)
        self.phone.type("d", fast=True)
        self.phone.type("e")
        self.phone.type("f")
        gate.set()
        self.flush()
        self.assertEqual(self.server.typed, [("ab", 120), ("PASTEd", 300), ("ef", 120)])

    def test_merged_typing_never_exceeds_a_chunk(self):
        self.server.slow_keys = 0.2
        self.phone.type("a")                 # the worker blocks on this one while the rest queues
        for _ in range(700):
            self.phone.type("b")
        self.flush()
        keys = ["".join(b["value"]) for b in self.server.bodies("/wda/keys")]
        self.assertEqual("".join(keys), "a" + "b" * 700)
        self.assertTrue(all(len(k) <= W.TYPE_CHUNK for k in keys), [len(k) for k in keys])

    def test_typing_is_not_merged_across_other_commands(self):
        self.server.slow_keys = 0.2
        self.phone.type("a")
        self.phone.type("b")
        self.phone.home()
        self.phone.type("c")
        self.flush()
        seq = [("keys", "".join(b["value"])) if p.endswith("/wda/keys") else ("home", "")
               for m, p, b in self.server.calls
               if p.endswith("/wda/keys") or (p.endswith("/wda/pressButton") and b == {"name": "home"})]
        self.assertEqual(seq[-2:], [("home", ""), ("keys", "c")])
        self.assertEqual("".join(t for k, t in seq), "abc")

    def test_long_text_and_crlf(self):
        for fast, rate in ((False, W.TYPE_RATE), (True, W.TYPE_RATE_FAST)):
            with self.subTest(fast=fast):
                self.server.typed.clear()
                self.phone.type("x" * 1100 + "\r\n", fast=fast)
                self.flush()
                self.assertEqual("".join(t for t, _ in self.server.typed), "x" * 1100 + "\n")
                self.assertTrue(all(len(t) <= W.TYPE_CHUNK for t, _ in self.server.typed))
                self.assertGreaterEqual(len(self.server.typed), 3)
                self.assertEqual({f for _, f in self.server.typed}, {rate})

    def test_callback_exception_does_not_kill_the_worker(self):
        self.phone.send("GET", "/status", None, lambda r: 1 / 0)
        self.phone.send("GET", "/status", None, fail=lambda e: 1 / 0)
        self.flush()                           # a later command still runs
        self.assertTrue(any("/status: callback failed: ZeroDivisionError" in m for m in self.logs), self.logs)

    def test_keep_alive_one_connection_for_the_worker(self):
        before = set(self.server.conns)
        for _ in range(5):
            self.phone.home()
        self.flush()
        self.assertEqual(len(self.server.conns - before), 1)

    def test_idle_connection_closed_by_wda_is_reopened(self):
        self.flush()                           # the worker now holds an open connection
        dropped = []

        def drop_once(_h, _m, _b):
            if not dropped:
                dropped.append(1)
                return "drop"
        self.server.hooks["/wda/homescreen"] = drop_once
        got = self.wait_result(lambda cb: self.phone.send("POST", "/wda/homescreen", {}, cb))
        self.assertEqual(got, {"value": None, "sessionId": None})
        self.assertEqual(self.logs, [])

    def test_invalid_json_reply(self):
        def junk(h, _m, _b):
            h.send_response(200)
            h.send_header("Content-Length", "5")
            h.end_headers()
            h.wfile.write(b"<html")
            return "drop"
        self.server.hooks["/wda/homescreen"] = junk
        errs = []
        self.wait_result(lambda cb: self.phone.send("POST", "/wda/homescreen", {}, cb, fail=errs.append))
        self.assertEqual(errs[0].error, "invalid reply")

    def test_toggle_lock(self):
        self.server.locked = False
        self.phone.toggle_lock()
        self.flush()
        self.flush()
        self.assertIn("/wda/lock", self.server.paths("POST"))
        self.assertTrue(self.server.locked)
        self.phone.toggle_lock()
        self.flush()
        self.flush()
        self.assertIn("/wda/unlock", self.server.paths("POST"))

    def test_refresh_size_after_rotation(self):
        self.server.hooks["/session/{sid}/window/size"] = lambda h, m, b: (200, {"width": 852, "height": 393})
        self.phone.refresh_size()
        self.flush()
        self.assertEqual(self.phone.size, (852, 393))
        self.server.hooks["/session/{sid}/window/size"] = lambda h, m, b: (200, {"oops": 1})
        self.phone.refresh_size()
        self.flush()
        self.assertEqual(self.phone.size, (852, 393))         # a bad reply keeps the old size

    # ---- data ---------------------------------------------------------
    def test_screenshot_decodes_png(self):
        png = make_png(3, 5, lambda x, y: (1, 2, 3))
        self.server.screenshot_png = png
        self.assertEqual(self.wait_result(self.phone.screenshot), png)

    def test_bad_screenshot_gives_none(self):
        self.server.hooks["/screenshot"] = lambda h, m, b: (200, "!!not base64!!")
        self.assertIsNone(self.wait_result(self.phone.screenshot))
        self.server.hooks["/screenshot"] = lambda h, m, b: (200, None)
        self.assertIsNone(self.wait_result(self.phone.screenshot))
        self.server.hooks["/screenshot"] = lambda h, m, b: (500, {"error": "unknown error", "message": "no"})
        self.assertIsNone(self.wait_result(self.phone.screenshot))

    def test_pasteboard_round_trip_utf8(self):
        self.wait_result(lambda cb: self.phone.set_pasteboard("héllo ✓", cb))
        self.assertEqual(self.server.bodies("/wda/setPasteboard")[0]["contentType"], "plaintext")
        self.assertEqual(self.server.pasteboard, "héllo ✓".encode())
        self.assertEqual(self.wait_result(self.phone.get_pasteboard), "héllo ✓")

    def test_bad_pasteboard_gives_none(self):
        for bad in ("abc", 123):              # bad padding, not a string
            self.server.hooks["/wda/getPasteboard"] = lambda h, m, b, v=bad: (200, v)
            self.assertIsNone(self.wait_result(self.phone.get_pasteboard), bad)
        self.server.hooks["/wda/getPasteboard"] = lambda h, m, b: (500, {"error": "x", "message": "y"})
        self.assertIsNone(self.wait_result(self.phone.get_pasteboard))

    # ---- network ------------------------------------------------------
    def test_unreachable_wda_is_logged_not_raised(self):
        logs = []
        dead = WDA(f"http://127.0.0.1:{free_port()}", log=logs.append)
        dead.sid = "x"
        dead.start()
        try:
            got = self.wait_result(lambda cb: dead.send("POST", "/wda/homescreen", {}, cb))
            self.assertIsNone(got)
            self.assertTrue(any("homescreen" in line for line in logs), logs)
            with self.assertRaises(OSError):
                dead.status(timeout=2)
        finally:
            dead.close()

    def test_proxy_settings_are_ignored(self):
        saved = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy")}
        os.environ.update(http_proxy=f"http://127.0.0.1:{free_port()}", HTTP_PROXY=f"http://127.0.0.1:{free_port()}")
        os.environ.pop("no_proxy", None)
        try:
            self.assertTrue(self.phone.status()["ready"])
            self.phone.home()
            self.flush()
            self.assertEqual(self.logs, [])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_url_prefix_is_kept(self):
        self.server.hooks["/prefix/status"] = lambda h, m, b: (200, {"ready": "prefixed"})
        self.assertEqual(WDA(self.server.url + "/prefix").status()["ready"], "prefixed")

    def test_close_stops_the_worker(self):
        self.phone.close()
        self.assertIsNone(self.phone._thread)
        self.phone.close()                     # twice is fine


if __name__ == "__main__":
    unittest.main()
