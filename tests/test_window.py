"""The window module: helpers and the input backends without a display, plus opt-in GTK window tests.

Needs PyGObject with GTK 3 and GStreamer (skipped otherwise). The backends run against a fake phone and a fake
Bluetooth HID (FakeBle, blehid's API) on a virtual clock. The window tests build the real window against a fake
WebDriverAgent, a fake MJPEG stream and a fake USB capture socket; they run only with IPHONE_TEST_WINDOW=1 and a
display (CI runs them under xvfb-run). They never map the window on screen.
"""
import json
import os
import socket
import stat
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
from support import FakeWDA, make_png, read_png_size  # noqa: E402

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: F401
    from iphone_control import geometry
    from iphone_control import window as W
    from iphone_control import wda as wdamod
    HAVE_GI = True
except (ImportError, ValueError):
    HAVE_GI = False


def run_py(code, env):
    e = dict(os.environ, **env)
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1])\n" + code, REPO],
                       env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(HAVE_GI, "needs PyGObject with GTK 3 and GStreamer")
class Helpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_save_png_is_private_and_never_overwrites(self):
        png = make_png(2, 2, lambda x, y: (255, 0, 0))
        folder = os.path.join(self.tmp.name, "shots")
        with mock.patch("time.strftime", return_value="iPhone_2026-10-23_12-00-00"):
            a = W.save_png(png, folder)
            b = W.save_png(png, folder)
        self.assertEqual(os.path.basename(a), "iPhone_2026-10-23_12-00-00.png")
        self.assertEqual(os.path.basename(b), "iPhone_2026-10-23_12-00-00_2.png")
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(a).st_mode), 0o600)
        with open(b, "rb") as f:
            self.assertEqual(f.read(), png)

    def test_save_png_refuses_other_data(self):
        for bad in (None, b"", b"GIF89a", b"<html>"):
            with self.assertRaises(ValueError):
                W.save_png(bad, self.tmp.name)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_config_is_merged_and_replaced_atomically(self):
        state = os.path.join(self.tmp.name, "state")
        cfg = os.path.join(state, "config.json")
        with mock.patch.object(W, "STATE", state), mock.patch.object(W, "CONFIG", cfg):
            self.assertEqual(W.load_config(), {})
            os.makedirs(state)
            with open(cfg, "w") as f:
                json.dump({"wda_bundle": "b", "expires_at": 5}, f)
            W.update_config(recent_apps=["com.apple.Maps"])
            self.assertEqual(W.load_config(), {"wda_bundle": "b", "expires_at": 5, "recent_apps": ["com.apple.Maps"]})
            self.assertEqual(os.listdir(state), ["config.json"])
            for junk in ("[1, 2]", "{nope", ""):
                with open(cfg, "w") as f:
                    f.write(junk)
                self.assertEqual(W.load_config(), {}, junk)

    def test_env_int_clamps(self):
        with mock.patch.dict(os.environ, {"X_TEST": "500"}):
            self.assertEqual(W._env_int("X_TEST", 30, 1, 60), 60)
        with mock.patch.dict(os.environ, {"X_TEST": "-3"}):
            self.assertEqual(W._env_int("X_TEST", 30, 1, 60), 1)
        with mock.patch.dict(os.environ, {"X_TEST": "fast"}):
            self.assertEqual(W._env_int("X_TEST", 30, 1, 60), 30)

    def test_settings_come_from_the_environment(self):
        code = ("import json; from iphone_control import window as w; from gi.repository import GLib, Gdk\n"
                "print(json.dumps(dict(s=w.MJPEG_SETTINGS, all=w.WDA_SETTINGS, wda=w.WDA_URL, mjpeg=w.MJPEG,"
                " shots=w.SHOTS, cfg=w.CONFIG, run=w.RUN,"
                " prg=GLib.get_prgname(), cls=Gdk.get_program_class())))")
        home = self.tmp.name
        got = run_py(code, {"IPHONE_FPS": "500", "IPHONE_SCALE": "5", "IPHONE_QUALITY": "0", "IPHONE_FIX_ORIENTATION": "0",
                            "IPHONE_WDA_URL": "http://127.0.0.1:18100", "IPHONE_MJPEG_URL": "http://127.0.0.1:19100",
                            "IPHONE_SCREENSHOT_DIR": "~/shots", "HOME": home, "XDG_STATE_HOME": home + "/st",
                            "IPHONE_FAST_INPUT": "0", "XDG_RUNTIME_DIR": ""})
        # iphone.sh's $RUN: ${XDG_RUNTIME_DIR:-$STATE/run}/iphone-control
        self.assertEqual(got["run"], os.path.join(home, "st", "iphone-control", "run", "iphone-control"))
        self.assertEqual(got["s"], {"mjpegServerFramerate": 60, "mjpegScalingFactor": 10,
                                    "mjpegServerScreenshotQuality": 10, "mjpegFixOrientation": False})
        self.assertEqual(got["all"], got["s"])                           # IPHONE_FAST_INPUT=0: WDA's defaults
        self.assertEqual((got["wda"], got["mjpeg"]), ("http://127.0.0.1:18100", "http://127.0.0.1:19100"))
        self.assertEqual(got["shots"], os.path.join(home, "shots"))
        self.assertEqual(got["cfg"], os.path.join(home, "st", "iphone-control", "config.json"))
        self.assertEqual((got["prg"], got["cls"]), ("iphone-control", "iphone-control"))
        env = {k: "" for k in ("IPHONE_FPS", "IPHONE_SCALE", "IPHONE_QUALITY", "IPHONE_FIX_ORIENTATION",
                               "IPHONE_SCREENSHOT_DIR", "IPHONE_FAST_INPUT")}
        got = run_py(code, dict(env, HOME=home, XDG_STATE_HOME="", IPHONE_FIX_ORIENTATION="1",
                                XDG_RUNTIME_DIR=home + "/rt"))
        self.assertEqual(got["run"], os.path.join(home, "rt", "iphone-control"))
        self.assertEqual(got["s"]["mjpegServerFramerate"], 30)
        self.assertEqual(got["s"]["mjpegScalingFactor"], 100)            # below 100 WDA re-encodes every frame
        self.assertEqual(got["s"]["mjpegServerScreenshotQuality"], 60)
        self.assertTrue(got["s"]["mjpegFixOrientation"])
        self.assertEqual(got["all"], dict(got["s"], snapshotMaxDepth=1, animationCoolOffTimeout=0))   # quick touches
        self.assertTrue(got["shots"].endswith(os.sep + "iPhone"))
        self.assertEqual(got["cfg"], os.path.join(home, ".local", "state", "iphone-control", "config.json"))

    def test_no_proxy_for_loopback(self):
        with mock.patch.dict(os.environ, {"no_proxy": "corp.example", "NO_PROXY": ""}):
            W._no_proxy_for_loopback()
            self.assertEqual(os.environ["no_proxy"], "corp.example,127.0.0.1,localhost,::1")
            self.assertEqual(os.environ["NO_PROXY"], "127.0.0.1,localhost,::1")

    def test_fail_notifies_without_a_terminal(self):
        with mock.patch.object(W.subprocess, "run") as run, mock.patch.object(W.sys, "stderr") as err:
            err.isatty.return_value = False
            with self.assertRaises(SystemExit) as cm:
                W.fail("it broke")
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(run.call_args[0][0][-1], "it broke")
        self.assertEqual(run.call_args[0][0][0], "notify-send")

    def test_fit_caps_bound_the_frame_but_never_enlarge_it(self):
        self.assertEqual(W.fit_caps(1, 1), "video/x-raw")                 # not laid out yet: any size
        self.assertEqual(W.fit_caps(748, 1620.0),
                         "video/x-raw,width=(int)[1,748],height=(int)[1,1620],pixel-aspect-ratio=(fraction)1/1")

    def test_scaler_is_optional(self):
        from gi.repository import Gst
        Gst.init(None)
        text = W.scaler()
        if Gst.ElementFactory.find("videoscale"):
            self.assertTrue(text.startswith("videoscale method=lanczos"), text)
            self.assertTrue(text.endswith("capsfilter name=fit caps=video/x-raw ! "), text)
        with mock.patch.object(W.Gst.ElementFactory, "find", return_value=None):
            self.assertEqual(W.scaler(), "")                               # old pipeline: gtksink scales

    def test_scaled_frames_keep_their_aspect(self):
        # what the window relies on: a range box makes videoscale fit portrait and landscape frames, never enlarge
        from gi.repository import Gst
        Gst.init(None)
        if not W.scaler():
            self.skipTest("no videoscale")
        out = []
        for w, h in ((1178, 2556), (2556, 1178), (300, 600)):
            p = Gst.parse_launch(f"videotestsrc num-buffers=1 ! video/x-raw,width={w},height={h} ! {W.scaler()}"
                                 "fakesink name=s")
            p.get_by_name("fit").set_property("caps", Gst.Caps.from_string(W.fit_caps(748, 1620)))
            p.set_state(Gst.State.PLAYING)
            p.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            st = p.get_by_name("s").get_static_pad("sink").get_current_caps().get_structure(0)
            out.append((st.get_value("width"), st.get_value("height")))
            p.set_state(Gst.State.NULL)
        self.assertEqual(out, [(747, 1620), (748, 345), (300, 600)])

    def test_gstreamer_elements(self):
        self.assertEqual(set(W.GST_ELEMENTS), {"souphttpsrc", "multipartdemux", "queue", "jpegdec", "videoconvert",
                                               "gtksink"})

    def test_bad_wda_url_exits_with_a_message(self):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("IPHONE_TEST_WINDOW") != "1":
            self.skipTest("needs a display and IPHONE_TEST_WINDOW=1")
        # stderr is not a terminal, so the window also sends a notification: catch it with a stub
        stub = os.path.join(self.tmp.name, "notify-send")
        with open(stub, "w") as f:
            f.write(f'#!/bin/sh\necho "$*" > "{self.tmp.name}/notified"\n')
        os.chmod(stub, 0o755)
        env = dict(os.environ, IPHONE_WDA_URL="ftp://nope", PATH=self.tmp.name + os.pathsep + os.environ["PATH"])
        r = subprocess.run([sys.executable, "-I", os.path.join(REPO, "wda_mirror.py")], stdin=subprocess.DEVNULL,
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("IPHONE_WDA_URL", r.stderr)
        with open(os.path.join(self.tmp.name, "notified")) as f:
            self.assertIn("IPHONE_WDA_URL", f.read())


@unittest.skipUnless(HAVE_GI, "needs PyGObject with GTK 3 and GStreamer")
class Choices(unittest.TestCase):
    """What the environment picks: the input mode, the picture, the settings, notifications."""

    def env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    def test_mode(self):
        for value, mode in (("", "full"), ("full", "full"), ("basic", "basic"), (" Basic ", "basic")):
            self.env(IPHONE_MODE=value)
            self.assertEqual(W.input_mode(), mode)
        self.env(IPHONE_MODE="auto")
        with mock.patch.object(W, "fail", side_effect=SystemExit(1)) as fail, self.assertRaises(SystemExit):
            W.input_mode()
        self.assertIn("full or basic", fail.call_args[0][0])

    def test_video_source_choice(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sock_path = os.path.join(tmp.name, "cap.sock")
        with mock.patch.object(W, "CAPTURE_SOCKET", sock_path):
            self.env(IPHONE_VIDEO="")
            self.assertEqual(W.video_kind("full"), "mjpeg")          # unset: wda_mirror.py by hand works as before
            self.assertEqual(W.video_kind("basic"), "airplay")       # no capture running
            self.env(IPHONE_VIDEO="auto")
            self.assertEqual(W.video_kind("full"), "mjpeg")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(sock_path)
            self.addCleanup(s.close)
            self.assertEqual(W.video_kind("full"), "usb")             # the capture runs
            self.assertEqual(W.video_kind("basic"), "usb")
            for kind in ("usb", "mjpeg", "airplay"):
                self.env(IPHONE_VIDEO=kind.upper())
                self.assertEqual(W.video_kind("full"), kind)
            for mode, kind, why in (("basic", "mjpeg", "needs full mode"),
                                    ("full", "vnc", "use usb, mjpeg or airplay")):
                self.env(IPHONE_VIDEO=kind)
                with mock.patch.object(W, "fail", side_effect=SystemExit(1)) as fail, self.assertRaises(SystemExit):
                    W.video_kind(mode)
                self.assertIn(why, fail.call_args[0][0])

    def test_video_source_classes(self):
        made, run = [], self.tmp_dir()
        with mock.patch.multiple(W.video, H264Source=mock.Mock(side_effect=lambda *a: made.append(("usb", a))),
                                 MjpegSource=mock.Mock(side_effect=lambda *a: made.append(("mjpeg", a))),
                                 AirplaySource=mock.Mock(side_effect=lambda *a, **k: made.append(("airplay", a, k)))), \
                mock.patch.object(W, "CAPTURE_SOCKET", "/run/cap.sock"), mock.patch.object(W, "MJPEG", "http://x:1"), \
                mock.patch.object(W, "RUN", run), mock.patch.object(W, "AIRPLAY_NAME", "Desk"):
            for kind in ("usb", "mjpeg", "airplay"):
                W.video_source(kind, "lost", "status")
        self.assertEqual(made[0], ("usb", ("/run/cap.sock", "lost", "status")))
        self.assertEqual(made[1], ("mjpeg", ("http://x:1", "lost", "status")))
        self.assertEqual(made[2][:2], ("airplay", ("Desk", "lost", "status")))
        self.assertTrue(made[2][2]["log_path"].endswith("uxplay.log"))
        self.assertEqual(made[2][2]["pid_path"], os.path.join(run, "uxplay.pid"))       # iphone.sh stops it too

    def test_key_routes(self):
        self.assertEqual([W.key_route(n, s) for n, s in (("Return", True), ("Left", False), ("Left", True),
                                                          ("ISO_Left_Tab", True), ("Escape", False))],
                         ["text", "hid", "keyboardInput", "keyboardInput", "hid"])

    def tmp_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_hidden_stream_settings(self):
        self.env(IPHONE_QUALITY="")
        usb = W.wda_settings("usb")
        self.assertEqual(usb["mjpegServerScreenshotQuality"], 10)       # nobody sees it under the USB capture...
        self.assertEqual(usb["mjpegServerFramerate"], W.WDA_SETTINGS["mjpegServerFramerate"])   # ...touches need it
        self.assertEqual({k: v for k, v in usb.items() if k != "mjpegServerScreenshotQuality"},
                         {k: v for k, v in W.WDA_SETTINGS.items() if k != "mjpegServerScreenshotQuality"})
        self.assertEqual(W.wda_settings("mjpeg"), W.WDA_SETTINGS)
        self.env(IPHONE_QUALITY="40")
        self.assertEqual(W.wda_settings("usb"), W.WDA_SETTINGS)          # the user's choice stays

    def test_notify_flag(self):
        for env, tty, want in (({}, False, True), ({}, True, False),
                               ({"IPHONE_FROM_SCRIPT": "1"}, False, False),           # iphone.sh reports it
                               ({"IPHONE_WDA_URL": "http://127.0.0.1:1", "IPHONE_MJPEG_URL": "http://127.0.0.1:2"},
                                False, True),                                         # no more guessing from these
                               ({"IPHONE_FROM_SCRIPT": "1", "IPHONE_NOTIFY": "1"}, False, True),
                               ({"IPHONE_NOTIFY": "0"}, False, False)):
            base = {k: "" for k in ("IPHONE_NOTIFY", "IPHONE_FROM_SCRIPT", "IPHONE_WDA_URL", "IPHONE_MJPEG_URL")}
            with mock.patch.dict(os.environ, dict(base, **env)), mock.patch.object(W.sys, "stderr") as err:
                err.isatty.return_value = tty
                self.assertEqual(W._notify_on_fail(), want, (env, tty))

    def test_estimated_size(self):
        self.assertEqual(W.estimated_size((1179, 2556)), (393, 852))          # 3 pixels a point
        self.assertEqual(W.estimated_size((2556, 1179)), (852, 393))
        w, h = W.estimated_size((886, 1920))                                   # a scaled-down AirPlay picture
        self.assertAlmostEqual(w, 320)
        self.assertAlmostEqual(h / w, 1920 / 886)

    def test_hid_keys_are_the_wda_keys(self):
        self.assertEqual(set(W.HID_KEYS), set(wdamod.KEYS) | set(wdamod.NAMED_KEYS))
        for name, key in wdamod.NAMED_KEYS.items():
            usage = getattr(key, "usage", None)
            if usage is not None:
                self.assertEqual(W.HID_KEYS[name], usage, name)

    def test_pointer_steps(self):
        steps = W.pointer_steps(geometry.scroll(200, 400, 1, (393, 852), per_unit=60))
        self.assertEqual([op for _, op, _, _ in steps], ["press"] + ["drag"] * 16 + ["release"])
        self.assertEqual(steps[0][1:], ("press", 200, 400))
        self.assertEqual(steps[-2][1:], ("drag", 200, 340))                   # wheel down: the finger moves up
        self.assertEqual(steps[-1], (370, "release", 200, 340))               # after the 120 ms rest
        gaps = [b[0] - a[0] for a, b in zip(steps[1:-1], steps[2:-1])]
        self.assertTrue(all(15 <= g <= 16 for g in gaps), gaps)
        long_press = W.pointer_steps(geometry.tap(10, 20, hold_ms=800))
        self.assertEqual(long_press, [(0, "press", 10, 20), (800, "release", 10, 20)])

    def test_help_matches_the_mode(self):
        full, basic = W.help_text(), W.help_text(basic=True)
        for key in ("Ctrl+Shift+V", "Ctrl+Shift+C", "Ctrl+O", "F8", "pinch"):
            self.assertIn(key, full)
            self.assertNotIn(key, basic)
        self.assertIn("Spotlight", basic)


class Later:
    """GLib.timeout_add / source_remove on a virtual clock; advance(ms) runs what falls due, in time order."""

    def __init__(self):
        self.now, self.n, self.due = 0, 0, {}

    def __call__(self, ms, fn):
        self.n += 1
        self.due[self.n] = (self.now + ms, self.n, fn)
        return self.n

    def cancel(self, sid):
        del self.due[sid]

    def clock(self):
        return self.now / 1000

    def advance(self, ms):
        end = self.now + ms
        while True:
            ready = sorted(e for e in self.due.values() if e[0] <= end)
            if not ready:
                break
            at, sid, fn = ready[0]
            del self.due[sid]
            self.now = max(self.now, at)
            fn()
        self.now = end


class FakePhone:
    """wda.WDA as far as WdaBackend uses it: every call is recorded."""
    size = (393, 852)

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **kw: self.calls.append((name, a, kw))

    def sent(self):
        return [a[0] for name, a, _kw in self.calls if name == "actions"]


class FakeBle:
    """blehid.BleHid's API (the contract), recording every call; text() leaves `untyped` characters out."""
    name = "Test Mouse"
    connected = True
    untyped = 0

    def __init__(self, *a, **kw):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*a, **kw):
            self.calls.append((name,) + a + ((kw,) if kw else ()))
            return self.untyped if name == "text" else None
        return call

    def ops(self, *names):
        return [c for c in self.calls if not names or c[0] in names]


def near(got, want, places=3):
    return len(got) == len(want) and all(abs(a - b) < 10 ** -places for a, b in zip(got, want))


@unittest.skipUnless(HAVE_GI, "needs PyGObject with GTK 3 and GStreamer")
class Clicks(unittest.TestCase):
    """WdaBackend's mouse button 1: taps at once by default, a double-click hold-back on request."""

    def backend(self, double_click_ms=0):
        self.later, self.phone = Later(), FakePhone()
        b = W.WdaBackend(self.phone, double_click_ms=double_click_ms, later=self.later, cancel=self.later.cancel)
        b.clock = self.later.clock
        b.double_click_time = lambda: 400                # not this desktop's setting
        b.video_changed((1179, 2556))
        return b

    def click(self, b, x, y, double=False, ms=80):
        b.down(x, y)
        if double:
            b.double()                                   # GTK's DOUBLE_PRESS comes after the second press
        self.later.advance(ms)
        b.up((x, y))
        self.later.advance(60)

    def test_a_click_taps_at_its_release(self):
        b = self.backend()
        self.click(b, 100, 200)
        self.assertEqual(self.phone.sent(), [geometry.tap(100, 200)])
        self.assertEqual(self.later.due, {})                        # nothing held back

    def test_a_double_click_is_two_taps_never_three(self):
        b = self.backend()
        self.click(b, 100, 200)
        self.click(b, 101, 200, double=True)
        # two requests back to back: iOS reads them as a double tap (they land ~330 ms apart)
        self.assertEqual(self.phone.sent(), [geometry.tap(100, 200), geometry.tap(101, 200)])

    def test_click_then_drag_is_the_tap_then_the_drag(self):
        b = self.backend()
        self.click(b, 100, 200)
        b.down(100, 200)
        b.double()
        for i in range(1, 6):
            self.later.advance(20)
            b.move(100, 200 + i * 20)
        b.up((100, 300))
        sent = self.phone.sent()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], geometry.tap(100, 200))
        moves = [a for a in sent[1]["actions"][0]["actions"] if a["type"] == "pointerMove"]
        self.assertGreater(len(moves), 1)                           # a swipe, not a second tap

    def test_held_still_is_a_long_press(self):
        b = self.backend()
        self.click(b, 50, 60, ms=600)
        self.assertEqual(self.phone.sent(), [geometry.tap(50, 60, hold_ms=600)])

    def test_hold_back_pairs_a_double_click_into_one_double_tap(self):
        b = self.backend(double_click_ms=300)
        b.down(100, 200)
        self.later.advance(50)
        b.up((100, 200))
        self.assertEqual(self.phone.sent(), [])                     # waiting for a second click
        self.assertEqual(len(self.later.due), 1)
        self.later.advance(100)
        self.click(b, 100, 200, double=True)
        self.assertEqual(self.phone.sent(), [geometry.double_tap(100, 200)])
        self.later.advance(1000)
        self.assertEqual(len(self.phone.sent()), 1)

    def test_hold_back_sends_a_lone_click_when_the_wait_is_over(self):
        b = self.backend(double_click_ms=300)
        b.down(100, 200)
        self.later.advance(50)
        b.up((100, 200))
        self.later.advance(249)
        self.assertEqual(self.phone.sent(), [])
        self.later.advance(1)
        self.assertEqual(self.phone.sent(), [geometry.tap(100, 200)])

    def test_hold_back_flushes_before_anything_else(self):
        b = self.backend(double_click_ms=300)
        b.down(100, 200)
        b.up((100, 200))
        b.flush()                                                   # e.g. a key pressed right after the click
        self.assertEqual(self.phone.sent(), [geometry.tap(100, 200)])
        self.assertEqual(self.later.due, {})

    def test_hold_back_waits_at_most_the_desktops_double_click_time(self):
        b = self.backend(double_click_ms=300)
        b.double_click_time = lambda: 250
        b.down(100, 200)
        self.later.advance(50)
        b.up((100, 200))
        self.later.advance(199)
        self.assertEqual(self.phone.sent(), [])
        self.later.advance(1)
        self.assertEqual(self.phone.sent(), [geometry.tap(100, 200)])

    def test_hold_back_ignores_a_second_press_off_the_picture(self):
        # GTK's DOUBLE_PRESS came, but its press landed in the letterbox: no down(), and no up() will follow
        b = self.backend(double_click_ms=300)
        b.down(100, 200)
        self.later.advance(50)
        b.up((100, 200))
        b.double()
        self.later.advance(250)
        self.assertEqual(self.phone.sent(), [geometry.tap(100, 200)])        # the first click still taps
        self.click(b, 300, 500)                                              # a click later on: a tap there
        self.later.advance(1000)
        self.assertEqual(self.phone.sent(), [geometry.tap(100, 200), geometry.tap(300, 500)])

    def test_system_gestures_need_the_size(self):
        b = self.backend()
        b.system("control_centre", None)
        self.assertEqual(self.phone.sent(), [])
        b.system("control_centre", (393, 852))
        self.assertEqual(self.phone.sent(), [geometry.edge_swipe("control_centre", (393, 852))])

    def test_the_shown_stream_gets_its_quality_back(self):
        b = self.backend()
        self.phone.settings = dict(W.wda_settings("mjpeg"), mjpegServerScreenshotQuality=W.HIDDEN_QUALITY)
        with mock.patch.dict(os.environ, {"IPHONE_QUALITY": ""}):
            b.stream_shown()
            b.stream_shown()                                         # once is enough
        want = W.MJPEG_SETTINGS["mjpegServerScreenshotQuality"]
        self.assertEqual(self.phone.calls, [("send", ("POST", "/session/{sid}/appium/settings",
                                                      {"settings": {"mjpegServerScreenshotQuality": want}}), {})])
        self.assertEqual(self.phone.settings["mjpegServerScreenshotQuality"], want)   # for a reopened session

    def test_rotation_rereads_the_size(self):
        b = self.backend()
        b.video_changed((1179, 2556))
        self.assertNotIn("refresh_size", [c[0] for c in self.phone.calls])
        b.video_changed((2556, 1179))
        self.assertEqual([c[0] for c in self.phone.calls], ["refresh_size"])
        self.assertEqual(b.current_size(), (852, 393))

    def test_keys_typing_and_paste(self):
        b = self.backend()
        b.key("Return")
        b.key("Left", fail="cb")
        b.key("ISO_Left_Tab", shift=True)
        self.assertEqual(b.type("hi"), 0)
        self.assertEqual(b.paste("pasted"), 0)
        self.assertEqual(self.phone.calls, [
            ("type", ("\n",), {}),
            ("key", (wdamod.NAMED_KEYS["Left"],), {"shift": False, "fail": "cb"}),
            ("key", (wdamod.NAMED_KEYS["ISO_Left_Tab"],), {"shift": True, "fail": None}),
            ("type", ("hi",), {}),
            ("type", ("pasted",), {"fast": True}),              # pasted text goes at TYPE_RATE_FAST
        ])
        self.assertEqual(b.features, {"clipboard", "apps", "pinch", "action"})


@unittest.skipUnless(HAVE_GI, "needs PyGObject with GTK 3 and GStreamer")
class Bluetooth(unittest.TestCase):
    """BleBackend: the window's input as blehid calls, in fractions of the picture, live."""

    def setUp(self):
        self.later, self.hid = Later(), FakeBle()
        self.b = W.BleBackend(self.hid, later=self.later, cancel=self.later.cancel)
        self.b.clock = self.later.clock
        self.b.video_changed((1179, 2556))                           # 393 x 852 points, estimated

    def test_a_click_is_a_tap_where_it_was(self):
        self.b.down(196.5, 426)
        self.later.advance(90)
        self.b.up((196.5, 426))
        self.b.double()                                              # never held back: no effect
        self.b.down(196.5, 426)
        self.later.advance(90)
        self.b.up((196.5, 426))
        self.assertEqual(len(self.hid.calls), 2)
        for c in self.hid.calls:
            self.assertEqual(c[0], "tap")
            self.assertTrue(near(c[1:], (0.5, 0.5)), c)
        self.assertEqual(self.later.due, {})

    def test_a_drag_is_live(self):
        self.b.down(100, 400)
        self.b.move(104, 400)                                        # inside the tap slop: nothing yet
        self.assertEqual(self.hid.calls, [])
        self.b.move(130, 400)
        self.b.move(160, 410)
        self.b.up((170, 410))
        names = [c[0] for c in self.hid.calls]
        self.assertEqual(names, ["press", "drag_to", "drag_to", "release"])
        self.assertTrue(near(self.hid.calls[0][1:], (100 / 393, 400 / 852)))       # down where it started
        self.assertTrue(near(self.hid.calls[2][1:], (160 / 393, 410 / 852)))
        self.assertTrue(near(self.hid.calls[3][1:], (170 / 393, 410 / 852)))
        self.assertEqual(self.later.due, {})

    def test_holding_still_is_a_long_press_held_long_enough(self):
        self.b.down(50, 60)
        self.later.advance(geometry.LONG_PRESS_MS - 1)
        self.assertEqual(self.hid.calls, [])
        self.later.advance(1)
        self.assertEqual([c[0] for c in self.hid.calls], ["press"])   # the finger goes down while held
        self.later.advance(100)
        self.b.up((50, 60))
        self.assertEqual(len(self.hid.calls), 1)                      # stays down 800 ms from the press
        self.later.advance(W.LONG_HOLD_MS - 101)
        self.assertEqual(len(self.hid.calls), 1)
        self.later.advance(1)
        self.assertEqual([c[0] for c in self.hid.calls], ["press", "release"])

    def test_hold_then_drag_moves_what_it_holds(self):
        self.b.down(50, 60)
        self.later.advance(600)
        self.b.move(50, 200)
        self.b.up((50, 210))
        self.assertEqual([c[0] for c in self.hid.calls], ["press", "drag_to", "release"])

    def test_right_click_long_press(self):
        self.b.long_press(196.5, 426)
        self.assertEqual(self.hid.calls[0][0], "tap")
        self.assertTrue(near(self.hid.calls[0][1:3], (0.5, 0.5)))
        self.assertEqual(self.hid.calls[0][3], {"hold_ms": 800})

    def test_scroll_is_a_short_pointer_drag(self):
        done = []
        self.b.scroll(196.5, 426, 0, 60, (393, 852), lambda: done.append(1))
        self.assertEqual([c[0] for c in self.hid.calls], ["press"])
        self.later.advance(1000)
        names = [c[0] for c in self.hid.calls]
        self.assertEqual(names[0], "press")
        self.assertEqual(names[-1], "release")
        self.assertEqual(set(names[1:-1]), {"drag_to"})
        (_p, x0, y0), (_r, x1, y1) = self.hid.calls[0], self.hid.calls[-1]
        self.assertAlmostEqual(x1, x0)
        self.assertAlmostEqual((y0 - y1) * 852, 60, delta=1)          # wheel down: the finger moves up 60 points
        self.assertEqual(done, [1])

    def test_back_is_a_drag_from_the_left_edge(self):
        self.b.back((393, 852))
        self.later.advance(1000)
        first, last = self.hid.calls[0], self.hid.calls[-1]
        self.assertEqual((first[0], last[0]), ("press", "release"))
        self.assertLess(first[1], 0.01)
        self.assertAlmostEqual(last[1], 0.6, places=2)

    def test_buttons_and_system_gestures(self):
        self.b.home()
        for kind in ("app_switcher", "control_centre", "notifications"):
            self.b.system(kind, (393, 852))
        self.b.volume(False)
        self.b.volume(True)
        self.b.lock()
        self.assertEqual(self.hid.calls, [("home",), ("app_switcher",), ("control_centre",), ("notifications",),
                                          ("consumer", 0xEA), ("consumer", 0xE9), ("consumer", 0x30)])
        self.assertEqual(self.b.features, frozenset())
        self.assertFalse(self.b.screenshot(lambda png: None))         # the window saves the picture instead

    def test_keys_and_typing(self):
        self.b.key("Left")
        self.b.key("ISO_Left_Tab", shift=True)
        self.b.key("Return")
        self.b.key("F13")                                             # no HID usage: nothing
        self.assertEqual(self.b.type("hello"), 0)
        self.hid.untyped = 2
        self.assertEqual(self.b.paste("héllo ✓"), 2)                  # what it could not type is counted
        self.assertEqual(self.hid.calls, [("key", 0x50, 0), ("key", 0x2B, 0x02), ("key", 0x28, 0),
                                          ("text", "hello"), ("text", "héllo ✓")])

    def test_open_url_goes_through_spotlight(self):
        self.b.open_url("https://example.com")
        self.assertEqual(self.hid.calls, [("spotlight",)])
        self.later.advance(W.SPOTLIGHT_MS)
        self.assertEqual(self.hid.calls[1:], [("text", "https://example.com")])
        self.later.advance(2000)
        self.assertEqual(self.hid.calls[2:], [("key", 0x28)])

    def test_close_lifts_a_held_finger_and_stops_bluetooth(self):
        self.b.down(50, 60)
        self.later.advance(500)
        self.b.close()
        self.assertEqual([c[0] for c in self.hid.calls], ["press", "release", "stop"])
        self.assertEqual(self.later.due, {})

    def names(self, start=0):
        return [c[0] for c in self.hid.calls[start:]]

    def test_a_new_press_first_lifts_a_long_press_still_held(self):
        self.b.down(50, 60)
        self.later.advance(geometry.LONG_PRESS_MS)                    # held still: the finger is down
        self.b.up((50, 60))                                           # ...and stays down until LONG_HOLD_MS
        self.later.advance(100)
        self.b.down(200, 400)
        self.assertEqual(self.names(), ["press", "release"])          # lifted now, where it was
        self.assertTrue(near(self.hid.calls[1][1:], (50 / 393, 60 / 852)))
        self.b.move(200, 500)
        self.later.advance(2000)                                      # its old release never comes
        self.b.move(200, 600)
        self.b.up((200, 600))
        self.assertEqual(self.names(2), ["press", "drag_to", "drag_to", "release"])
        self.assertTrue(near(self.hid.calls[-1][1:], (200 / 393, 600 / 852)))
        self.assertEqual(self.later.due, {})

    def test_a_new_press_first_ends_a_scroll_still_playing(self):
        done = []
        self.b.scroll(196.5, 426, 0, 60, (393, 852), lambda: done.append(1))
        self.later.advance(100)                                       # its finger is down, dragging
        n = len(self.hid.calls)
        self.b.down(100, 200)
        self.assertEqual(self.hid.calls[n:], [("release",) + self.hid.calls[n - 1][1:]])   # lifted where it was
        self.assertEqual(done, [1])                                   # the window can send the next one
        self.b.move(100, 300)
        self.later.advance(1000)                                      # no more scroll steps
        self.b.scroll(196.5, 426, 0, 60, (393, 852), lambda: done.append(2))
        self.assertEqual(done, [1, 2])                                # button 1 has the pointer: not played
        self.b.up((100, 300))
        self.assertEqual(self.names(n + 1), ["press", "drag_to", "release"])
        self.assertEqual(self.later.due, {})

    def test_a_scroll_first_lifts_a_long_press_still_held(self):
        self.b.down(50, 60)
        self.later.advance(geometry.LONG_PRESS_MS)
        self.b.up((50, 60))
        self.b.scroll(196.5, 426, 0, 60, (393, 852), lambda: None)
        self.assertEqual(self.names(), ["press", "release", "press"])
        self.later.advance(2000)
        self.assertEqual(self.names()[-1], "release")
        self.assertEqual(self.hid.calls[-1][1:], self.hid.calls[-2][1:])          # the scroll's own end
        self.assertEqual(self.later.due, {})

    def test_close_during_a_scroll_lifts_its_finger(self):
        done = []
        self.b.back((393, 852))
        self.b.scroll(196.5, 426, 0, 60, (393, 852), lambda: done.append(1))
        self.later.advance(100)
        self.b.close()
        self.assertEqual(self.names()[-2:], ["release", "stop"])
        self.assertEqual((done, self.later.due), ([], {}))            # the window is going: nothing waits

    def test_typing_while_the_iphone_is_not_connected(self):
        from iphone_control.blehid import BleError
        self.hid.connected = False
        self.assertEqual(self.b.type("a"), W.NOT_CONNECTED)
        self.assertEqual(self.b.paste("abc"), W.NOT_CONNECTED)
        self.assertEqual(self.hid.calls, [])
        self.hid.connected = True
        for gone in (BleError("the iPhone is not connected"), None, -1):    # it went in between
            self.hid.text = mock.Mock(side_effect=gone) if isinstance(gone, Exception) else mock.Mock(return_value=gone)
            self.assertEqual(self.b.type("a"), W.NOT_CONNECTED, gone)
        self.hid.text = mock.Mock(return_value=1)
        self.assertEqual(self.b.type("é"), 1)                         # connected: what it could not type


# ---------------------------------------------------------------- smoke test with a real GTK window
class Mjpeg(BaseHTTPRequestHandler):
    frames = []
    stop = threading.Event()
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
            for _ in range(200):
                if Mjpeg.stop.is_set():
                    return
                data = Mjpeg.frames[0]
                self.wfile.write(b"--BoundaryString\r\nContent-type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                 % len(data) + data + b"\r\n\r\n")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass


class Fake:
    """A stand-in for a Gdk event: only the fields the handlers read."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def jpeg(width, height, path):
    from gi.repository import Gst
    Gst.init(None)
    p = Gst.parse_launch(f"videotestsrc num-buffers=1 ! video/x-raw,width={width},height={height} ! jpegenc ! "
                         f'filesink location="{path}"')
    p.set_state(Gst.State.PLAYING)
    p.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    p.set_state(Gst.State.NULL)
    with open(path, "rb") as f:
        return f.read()


@unittest.skipUnless(HAVE_GI and os.environ.get("IPHONE_TEST_WINDOW") == "1"
                     and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
                     "set IPHONE_TEST_WINDOW=1 with a display (CI: xvfb-run) to build the real window")
class WindowSmoke(unittest.TestCase):
    def test_window_against_fake_wda_and_stream(self):
        from gi.repository import GLib, Gdk, Gtk
        from iphone_control.wda import WDA
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Mjpeg.frames = [jpeg(180, 390, os.path.join(tmp.name, "p.jpg"))]
        wda = FakeWDA()
        mj = ThreadingHTTPServer(("127.0.0.1", 0), Mjpeg)
        mj.daemon_threads = True
        threading.Thread(target=mj.serve_forever, daemon=True).start()
        self.addCleanup(wda.stop)
        self.addCleanup(mj.server_close)
        self.addCleanup(mj.shutdown)
        self.assertTrue(Gtk.init_check(None)[0] if isinstance(Gtk.init_check(None), tuple) else Gtk.init_check(None))
        with mock.patch.object(W, "MJPEG", f"http://127.0.0.1:{mj.server_port}"), \
                mock.patch.object(W, "SHOTS", os.path.join(tmp.name, "shots")):
            phone = WDA(wda.url)
            phone.connect(W.WDA_SETTINGS)
            self.addCleanup(phone.close)
            win = W.Mirror(phone)
            win.get_child().show_all()               # lay out without mapping the toplevel
            seen = {}

            def key(name, state=0):
                ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
                ev.set_device(Gdk.Display.get_default().get_default_seat().get_keyboard())
                k = ev.key
                k.window = win.get_window()
                k.keyval = Gdk.keyval_from_name(name)
                k.state = state
                return win.on_key(win, k)

            def check():
                seen["video"] = win.video_size
                win.realize()
                for name in ("a", "BackSpace", "Return", "Left", "F1", "F3"):
                    key(name)
                seen["alt-left"] = key("Left", Gdk.ModifierType.MOD1_MASK)
                seen["super"] = key("x", Gdk.ModifierType.SUPER_MASK)
                win.screenshot()
                win.edge("control_centre")
                x0, y0 = win.picture.get_allocation().width / 2, win.picture.get_allocation().height * 0.5
                press = Gdk.EventType.BUTTON_PRESS
                win.on_press(None, Fake(type=press, button=1, x=x0, y=y0))      # a quick swipe to the left
                for i in range(1, 11):
                    win.on_motion(None, Fake(x=x0 - i * 6, y=y0))
                win.on_release(None, Fake(button=1, x=x0 - 66, y=y0))
                for _ in range(4):                                             # four Ctrl+wheel notches
                    win.on_scroll(None, Fake(direction=Gdk.ScrollDirection.UP, state=Gdk.ModifierType.CONTROL_MASK,
                                             x=x0, y=y0, get_source_device=lambda: None))

                def done():
                    sw = win.pipeline.get_by_name("sink").props.widget
                    a, f = sw.get_allocation(), sw.get_scale_factor()
                    seen["fit"] = win.fit and (win.fit.get_property("caps").to_string(), a.width * f, a.height * f)
                    Gtk.main_quit()
                    return False
                GLib.timeout_add(1500, done)
                return False
            GLib.timeout_add(2500, check)
            GLib.timeout_add(20000, lambda: (Gtk.main_quit(), False)[1])     # never hang the suite
            Gtk.main()
            from gi.repository import Gst
            win.pipeline.set_state(Gst.State.NULL)
        self.assertEqual(seen.get("video"), (180, 390))
        keys = "".join("".join(b["value"]) for b in wda.bodies("/wda/keys"))
        self.assertEqual(keys, "a\b\n")
        # an unmodified named key is one HID event (usage page 7), with no keyboardInput round trip
        self.assertEqual([(b["page"], b["usage"]) for b in wda.bodies("/wda/performIoHidEvent")], [(7, 0x50)])
        self.assertEqual(wda.bodies("/keyboardInput"), [])
        self.assertNotIn("/wda/homescreen", wda.paths("POST"))      # a no-op while Control Centre is open
        self.assertEqual(sorted(b["name"] for b in wda.bodies("/pressButton")), ["home", "volumeDown"])
        self.assertTrue(seen["alt-left"])
        self.assertFalse(seen["super"])                        # Super+key belongs to the desktop
        swipes = wda.bodies("/actions")
        # back swipe, Control Centre, the drag, the first notch's pinch, then the other three merged into one
        self.assertEqual(len(swipes), 5)
        self.assertEqual(sum(1 for a in swipes[2]["actions"][0]["actions"] if a["type"] == "pointerMove"), 2)
        self.assertEqual([len(s["actions"]) for s in swipes[3:]], [2, 2])                # two fingers each
        spread = [abs(s["actions"][1]["actions"][2]["x"] - s["actions"][0]["actions"][2]["x"]) for s in swipes[3:]]
        self.assertGreater(spread[1], spread[0])                          # 1.25 ** 3 zooms further than 1.25
        if seen["fit"]:                                     # frames are fitted to the picture in device pixels
            caps, w, h = seen["fit"]
            self.assertIn(f"width=(int)[ 1, {w} ], height=(int)[ 1, {h} ]", caps)
        self.assertEqual(len(os.listdir(os.path.join(tmp.name, "shots"))), 1)
        self.assertEqual(wda.bodies("/appium/settings")[0]["settings"], W.WDA_SETTINGS)
        win.quit = lambda *_: None



WINDOW = (HAVE_GI and os.environ.get("IPHONE_TEST_WINDOW") == "1"
          and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def mjpeg_server(test, width=180, height=390):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    Mjpeg.frames, Mjpeg.connects = [jpeg(width, height, os.path.join(tmp.name, "p.jpg"))], 0
    mj = ThreadingHTTPServer(("127.0.0.1", 0), Mjpeg)
    mj.daemon_threads = True
    threading.Thread(target=mj.serve_forever, daemon=True).start()
    test.addCleanup(mj.server_close)
    test.addCleanup(mj.shutdown)
    return f"http://127.0.0.1:{mj.server_port}"


def key_event(win, name, state=0):
    from gi.repository import Gdk
    ev = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    ev.set_device(Gdk.Display.get_default().get_default_seat().get_keyboard())
    k = ev.key
    k.window = win.get_window()
    k.keyval = Gdk.keyval_from_name(name)
    k.state = state
    return win.on_key(win, k)


def run_steps(*steps, gap=1500):
    """Run the GTK main loop; each step after the one before (ms), then quit. Never hangs the suite."""
    from gi.repository import GLib, Gtk

    def at(i):
        if i == len(steps):
            Gtk.main_quit()
        else:
            steps[i]()
            GLib.timeout_add(gap, at, i + 1)
        return False

    def give_up():
        guard[:] = []
        Gtk.main_quit()
        return False
    GLib.timeout_add(2000, at, 0)
    guard = [GLib.timeout_add(30000, give_up)]
    Gtk.main()
    for g in guard:                       # left behind, it would end a later test's main loop
        GLib.source_remove(g)


# for main() in a subprocess: the real blehid made unimportable, so no test reaches the system's Bluetooth
NO_BLUETOOTH = "sys.modules['iphone_control.blehid'] = None\n"


def window_code(prelude):
    return ("import sys; sys.path.insert(0, sys.argv[1])\n" + prelude
            + "from iphone_control.window import main\nmain()\n")


def fake_blehid(start):
    """A prelude that replaces blehid: BleHid.start() runs the statement start (BleError is defined)."""
    return ("import types\nm = types.ModuleType('iphone_control.blehid')\n"
            "class BleError(Exception): pass\n"
            "class BleHid:\n"
            "    def __init__(self, **kw): print('BleHid', sorted(kw), flush=True)\n"
            f"    def start(self): {start}\n"
            "    def stop(self): pass\n"
            "m.BleError, m.BleHid = BleError, BleHid\nsys.modules['iphone_control.blehid'] = m\n")


def recorded_flashes(win):
    """Everything win.flash() shows from now on."""
    shown, flash = [], win.flash
    win.flash = lambda text, echo=True: (shown.append(text), flash(text, echo))[1]
    return shown


@unittest.skipUnless(WINDOW, "set IPHONE_TEST_WINDOW=1 with a display (CI: xvfb-run) to build the real window")
class WindowModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.shots = os.path.join(self.tmp.name, "shots")

    def shot_sizes(self):
        names = sorted(os.listdir(self.shots)) if os.path.isdir(self.shots) else []
        out = []
        for n in names:
            with open(os.path.join(self.shots, n), "rb") as f:
                out.append(read_png_size(f.read()))
        return out

    def test_usb_capture_with_webdriveragent_kept_warm(self):
        from gi.repository import Gst
        from iphone_control import video as V
        from iphone_control.wda import WDA
        from test_video import FakeCapture, clip
        Gst.init(None)
        if V.H264Source.missing():
            self.skipTest(f"missing {V.H264Source.missing()}")
        url = mjpeg_server(self)
        wda = FakeWDA()
        self.addCleanup(wda.stop)
        cap = FakeCapture([clip("64x128")])
        self.addCleanup(cap.close)
        seen = {}
        with mock.patch.object(W, "MJPEG", url), mock.patch.object(W, "SHOTS", self.shots), \
                mock.patch.object(W, "CAPTURE_SOCKET", cap.path), mock.patch.dict(os.environ, {"IPHONE_QUALITY": ""}):
            phone = WDA(wda.url)
            phone.connect(W.wda_settings("usb"))
            self.addCleanup(phone.close)
            win = W.Mirror(W.WdaBackend(phone), "usb")
            self.addCleanup(win.shutdown)
            win.get_child().show_all()

            def portrait():
                seen.update(portrait=win.video_size, aspect0=win._aspect, title=win.get_title(),
                            drain=win.drain.connects)
                win.screenshot()                                  # WebDriverAgent's full-size PNG
                cap.send(clip("128x64"))                          # an app turned to landscape

            def landscape():
                seen.update(landscape=win.video_size, aspect=win._aspect)
                wda.hooks["/screenshot"] = lambda *_: (500, {"error": "unknown error", "message": "broken"})
                win.screenshot()                                  # falls back to the decoded frame
            run_steps(portrait, landscape)
        self.assertEqual(seen.get("portrait"), (64, 128))
        self.assertEqual(seen["title"], "iPhone (USB)")
        self.assertLess(seen["aspect0"], 1)
        self.assertEqual(seen.get("landscape"), (128, 64))
        self.assertGreater(seen["aspect"], 1)                     # the window turned with the picture
        self.assertGreaterEqual(len([p for p in wda.paths("GET") if p.endswith("/window/size")]), 2)   # re-read
        self.assertGreaterEqual(seen["drain"], 1)                 # WebDriverAgent's stream kept running, unseen
        self.assertGreaterEqual(Mjpeg.connects, 1)
        self.assertEqual(wda.bodies("/appium/settings")[0]["settings"]["mjpegServerScreenshotQuality"], 10)
        self.assertEqual(sorted(self.shot_sizes()), [(4, 8), (128, 64)])

    def test_basic_mode_over_bluetooth(self):
        from gi.repository import Gdk, Gtk
        url = mjpeg_server(self)
        hid = FakeBle()
        replies, seen = [], {}
        with mock.patch.object(W, "MJPEG", url), mock.patch.object(W, "SHOTS", self.shots):
            win = W.Mirror(W.BleBackend(hid), "mjpeg")
            self.addCleanup(win.shutdown)
            win.get_child().show_all()
            # no picture yet (e.g. AirPlay not started): the system gestures are key combinations, they go
            win.edge("control_centre")
            win.edge("app_switcher")
            win.back()                                            # a swipe: needs the picture's size
            seen["early"], hid.calls[:] = list(hid.calls), []

            def first():
                seen["video"] = win.video_size
                seen["size"] = win.input.current_size()
                win.realize()
                win.on_ble_state("advertising")
                seen["advertising"] = win._status_text()
                win.on_ble_state("connected")
                seen["connected"] = win._status_text()
                a = win.picture.get_allocation()
                x, y = a.width / 2, a.height / 2
                win.on_press(None, Fake(type=Gdk.EventType.BUTTON_PRESS, button=1, x=x, y=y))
                win.on_release(None, Fake(button=1, x=x, y=y))
                for name in ("F1", "F3", "F8", "Left"):
                    key_event(win, name)
                seen["ctrl-o"] = key_event(win, "o", Gdk.ModifierType.CONTROL_MASK) and win.status.get_text()
                win._paste("pasted")
                hid.untyped = 1
                win._type("é")
                seen["untyped"] = win.status.get_text()
                hid.untyped = 0
                win.on_ble_state("disconnected")
                hid.connected = False
                win._type("a")
                seen["offline"] = win.status.get_text()
                hid.connected = True
                win.on_ble_state("connected")
                win.on_scroll(None, Fake(direction=Gdk.ScrollDirection.UP, state=Gdk.ModifierType.CONTROL_MASK,
                                         x=x, y=y, get_source_device=lambda: None))          # no pinch: ignored
                seen["before-scroll"] = len(hid.calls)
                win.on_scroll(None, Fake(direction=Gdk.ScrollDirection.DOWN, state=0, x=x, y=y,
                                         get_source_device=lambda: None))
                win.screenshot()                                  # no WebDriverAgent: the decoded frame
                win.confirm_pairing(123456, "iPhone", replies.append)
                seen["code"] = win._pair.get_property("secondary-text")
                win.confirm_pairing(654321, "iPhone", replies.append)     # replaces it: the first is a No
                win._pair.response(Gtk.ResponseType.YES)
                seen["dialog"] = win._pair
            run_steps(first)
        self.assertEqual(seen.get("video"), (180, 390))
        self.assertEqual(seen["early"], [("control_centre",), ("app_switcher",)])
        # not the US keyboard: nothing was typed, and the Bluetooth hint stays in sight
        self.assertEqual(seen["offline"], "Bluetooth: the iPhone is not connected\n"
                                          "the iPhone disconnected from Bluetooth - waiting for it…")
        self.assertTrue(near(seen["size"], (320, 320 * 390 / 180)))
        self.assertIn("Settings > Bluetooth > Test Mouse > Pair", seen["advertising"])
        self.assertIn("AssistiveTouch on", seen["advertising"])
        self.assertIsNone(seen["connected"])
        calls = hid.calls
        self.assertEqual(calls[0][0], "tap")
        self.assertTrue(near(calls[0][1:], (0.5, 0.5), places=2), calls[0])
        self.assertEqual(calls[1:6], [("home",), ("consumer", 0xEA), ("key", 0x50, 0), ("text", "pasted"),
                                      ("text", "é")])                    # F8: no Action button over Bluetooth
        self.assertEqual(seen["ctrl-o"], "Ctrl+O needs full mode (WebDriverAgent)")
        self.assertIn("“é” could not be typed", seen["untyped"])
        self.assertEqual(seen["before-scroll"], 6)
        scroll = [c[0] for c in calls[6:]]
        self.assertEqual((scroll[0], scroll[-1]), ("press", "release"))
        self.assertIn("123456", seen["code"])
        self.assertEqual(replies, [False, True])
        self.assertIsNone(seen["dialog"])
        self.assertEqual(self.shot_sizes(), [(180, 390)])

    def run_window(self, env, prelude=NO_BLUETOOTH):
        """main() in a subprocess; prelude runs first (by default it makes the real blehid unimportable: a window
        test must never reach the system's Bluetooth)."""
        e = dict(os.environ, IPHONE_NOTIFY="0", IPHONE_WDA_URL="ftp://never-used", **env)
        return subprocess.run([sys.executable, "-I", "-c", window_code(prelude), REPO], stdin=subprocess.DEVNULL,
                              env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=60)

    def test_basic_mode_needs_no_webdriveragent_and_says_what_is_missing(self):
        from gi.repository import Gst
        from iphone_control import video as V
        from test_video import FakeCapture
        Gst.init(None)
        r = self.run_window({"IPHONE_MODE": "basic", "IPHONE_VIDEO": "mjpeg"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("IPHONE_VIDEO=mjpeg needs full mode", r.stderr)
        if not V.AirplaySource.missing():
            r = self.run_window({"IPHONE_MODE": "basic", "IPHONE_VIDEO": "airplay", "PATH": self.tmp.name})
            self.assertEqual(r.returncode, 1)
            self.assertIn("uxplay is not installed", r.stderr)
        if V.H264Source.missing():
            self.skipTest(f"missing {V.H264Source.missing()}")
        gone = os.path.join(self.tmp.name, "cap.sock")
        r = self.run_window({"IPHONE_MODE": "basic", "IPHONE_VIDEO": "usb", "IPHONE_CAPTURE_SOCKET": gone})
        self.assertEqual(r.returncode, 1)
        # no capture running yet is no error: the window would wait for it (it stops at Bluetooth here)
        self.assertIn(f"no USB capture socket at {gone} yet - waiting for it", r.stdout)
        self.assertIn("Bluetooth input is not available", r.stderr)
        cap = FakeCapture()
        self.addCleanup(cap.close)
        env = {"IPHONE_MODE": "basic", "IPHONE_VIDEO": "usb", "IPHONE_CAPTURE_SOCKET": cap.path}
        r = self.run_window(env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Bluetooth input is not available", r.stderr)
        self.assertNotIn("waiting for it", r.stdout)
        r = self.run_window(env, prelude=fake_blehid("raise BleError('no Bluetooth LE adapter found (try: rfkill "
                                                     "unblock bluetooth)')"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("Bluetooth: no Bluetooth LE adapter found", r.stderr)
        self.assertIn("BleHid ['adapter', 'confirm', 'log', 'name', 'on_state']", r.stdout)
        self.assertNotIn("WDA", r.stdout + r.stderr)               # WebDriverAgent was never asked

    def test_usb_capture_is_waited_for_and_its_status_stays(self):
        from gi.repository import Gst
        from iphone_control import screencap
        from iphone_control import video as V
        from test_video import FakeCapture, clip
        Gst.init(None)
        if V.H264Source.missing():
            self.skipTest(f"missing {V.H264Source.missing()}")
        path, caps, seen = os.path.join(self.tmp.name, "cap.sock"), [], {}
        note = "No picture from the iPhone - unlock it; if it stays black, restart the iPhone."
        with mock.patch.object(W, "CAPTURE_SOCKET", path), mock.patch.object(W, "SHOTS", self.shots):
            win = W.Mirror(W.BleBackend(FakeBle()), "usb")           # the capture is not running (yet)
            self.addCleanup(win.shutdown)
            win.get_child().show_all()
            seen["start"] = win.status.get_text()

            def waiting():
                seen["waiting"] = win._status_text()
                cap = FakeCapture([screencap.frame(screencap.STATUS, note.encode()), clip("64x128")], path=path)
                self.addCleanup(cap.close)
                caps.append(cap)                                  # the watchdog brought it back

            def picture():
                seen.update(video=win.video_size, told=win.status.get_text(), flash=win._flash_id)
                caps[0].send(screencap.frame(screencap.STATUS, b""))

            def cleared():
                seen["cleared"] = win._status_text()
            run_steps(waiting, picture, cleared, gap=3000)
        self.assertEqual(seen["start"], W.USB_WAITING)
        self.assertEqual(seen["waiting"], W.USB_WAITING)            # retried with the backoff, not given up
        self.assertEqual(seen.get("video"), (64, 128))
        self.assertEqual((seen["told"], seen["flash"]), (note, 0))  # persistent, not a 2.5 s flash
        self.assertIsNone(seen["cleared"])

    def test_full_mode_shows_webdriveragents_stream_when_no_capture_comes(self):
        from gi.repository import Gst
        from iphone_control import video as V
        from iphone_control.wda import WDA
        Gst.init(None)
        if V.H264Source.missing():
            self.skipTest(f"missing {V.H264Source.missing()}")
        url = mjpeg_server(self)
        wda = FakeWDA()
        self.addCleanup(wda.stop)
        seen = {}
        with mock.patch.object(W, "MJPEG", url), mock.patch.object(W, "SHOTS", self.shots), \
                mock.patch.object(W, "CAPTURE_SOCKET", os.path.join(self.tmp.name, "cap.sock")), \
                mock.patch.object(W, "NO_CAPTURE_S", 1), mock.patch.dict(os.environ, {"IPHONE_QUALITY": ""}):
            phone = WDA(wda.url)
            phone.connect(W.wda_settings("usb"))
            self.addCleanup(phone.close)
            win = W.Mirror(W.WdaBackend(phone), "usb")
            self.addCleanup(win.shutdown)
            win.get_child().show_all()
            flashes = recorded_flashes(win)
            seen.update(start=win.status.get_text(), drain=bool(win.drain))

            def later():
                seen.update(kind=win.kind, video=win.video_size, drain_after=win.drain)
            run_steps(lambda: None, later)
        self.assertEqual((seen["start"], seen["drain"]), (W.USB_WAITING, True))
        self.assertEqual(seen.get("kind"), "mjpeg")
        self.assertEqual(seen["video"], (180, 390))                  # WebDriverAgent's picture
        self.assertIsNone(seen["drain_after"])                       # the stream shown keeps WDA capturing
        self.assertIn("no USB picture for 1 s - showing WebDriverAgent's picture instead", flashes)
        quality = [b["settings"].get("mjpegServerScreenshotQuality") for b in wda.bodies("/appium/settings")]
        self.assertEqual(quality[0], W.HIDDEN_QUALITY)
        self.assertEqual(quality[-1], W.MJPEG_SETTINGS["mjpegServerScreenshotQuality"])   # its own quality again

    def test_an_unsupported_shift_key_leaves_the_other_named_keys_on(self):
        from gi.repository import Gdk
        from iphone_control.wda import WDA
        url = mjpeg_server(self)
        wda = FakeWDA()
        self.addCleanup(wda.stop)
        # WebDriverAgent before Xcode 15 / iOS 17: no keyboardInput (Shift+keys), HID events work
        wda.hooks["/session/{sid}/wda/element/0/keyboardInput"] = lambda *_: (
            500, {"error": "unsupported operation", "message": "only supported since Xcode15 and iPadOS 17"})
        shift, seen = Gdk.ModifierType.SHIFT_MASK, {}
        with mock.patch.object(W, "MJPEG", url), mock.patch.object(W, "SHOTS", self.shots):
            phone = WDA(wda.url)
            phone.connect(W.WDA_SETTINGS)
            self.addCleanup(phone.close)
            win = W.Mirror(phone)
            self.addCleanup(win.shutdown)
            win.get_child().show_all()
            flashes = recorded_flashes(win)

            def select():
                win.realize()
                key_event(win, "Right", shift)                    # refused

            def keys():
                for name, state in (("Right", 0), ("Left", shift), ("Escape", 0)):
                    key_event(win, name, state)
                seen["off"] = set(win.keys_off)
            run_steps(select, keys)
        self.assertEqual(seen.get("off"), {"keyboardInput"})
        self.assertEqual(len(wda.bodies("/keyboardInput")), 1)       # Shift+Left: not sent again
        self.assertEqual([(b["page"], b["usage"]) for b in wda.bodies("/wda/performIoHidEvent")],
                         [(7, 0x4F), (7, 0x29)])                       # Right and Escape still go
        self.assertIn("Shift+arrows, Shift+Tab and other Shift+key combinations need iOS 17 or later", flashes)

    def test_a_stop_while_starting_still_stops_uxplay(self):
        # iphone.sh's stop sends TERM whenever it comes: here while Bluetooth is still starting (D-Bus calls of up
        # to 10 s), after the window started uxplay
        from gi.repository import Gst
        from iphone_control import video as V
        from test_video import fake_uxplay, running
        Gst.init(None)
        if V.AirplaySource.missing():
            self.skipTest(f"missing {V.AirplaySource.missing()}")
        bin_dir, run = os.path.join(self.tmp.name, "bin"), os.path.join(self.tmp.name, "run")
        os.makedirs(bin_dir)
        fake_uxplay(bin_dir)
        mark, pid_path = os.path.join(self.tmp.name, "starting"), os.path.join(run, "uxplay.pid")
        prelude = (f"import iphone_control.window as w\nw.RUN = {run!r}\n"
                   + fake_blehid(f"open({mark!r}, 'w').close(); import time; time.sleep(30)"))
        env = dict(os.environ, IPHONE_NOTIFY="0", IPHONE_MODE="basic", IPHONE_VIDEO="airplay",
                   IPHONE_AIRPLAY_NAME="Test Mac", PATH=bin_dir + os.pathsep + os.environ["PATH"])
        p = subprocess.Popen([sys.executable, "-I", "-c", window_code(prelude), REPO], stdin=subprocess.DEVNULL,
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.communicate()))
        end = time.monotonic() + 30
        while not (os.path.exists(mark) and os.path.exists(pid_path)) and p.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        if p.poll() is not None:
            self.fail(f"the window exited ({p.returncode}): {p.communicate()[1]}")
        with open(pid_path) as f:
            uxplay = int(f.read())
        self.addCleanup(lambda: running(uxplay) and os.kill(uxplay, 9))
        p.terminate()
        _out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)                     # a clean exit, not killed by the signal
        self.assertFalse(running(uxplay))
        self.assertFalse(os.path.exists(pid_path))                   # atexit stopped it


if __name__ == "__main__":
    unittest.main()
