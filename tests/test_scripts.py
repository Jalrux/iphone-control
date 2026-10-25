"""iphone.sh, resign.sh, uninstall.sh and certs/ca-bundle.sh run against fakes (see scriptenv.py).

No real iPhone, usbmuxd, go-ios, plumesign, screen capture, Bluetooth, Apple server or network is used: the
scripts run from a copy of the repo with a temp HOME, so ~/.local/bin/ios and plumesign are the fakes.
"""
import datetime
import json
import os
import pty
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from support import UDID_A, UDID_B, bundle_id, epoch, make_signed_ipa  # noqa: E402
from scriptenv import SYSTEM_PATH, Sandbox, stamp_installed, write_exec  # noqa: E402

HAVE = {t: shutil.which(t, path=SYSTEM_PATH) for t in ("bash", "curl", "flock", "timeout", "ss", "sha256sum")}
ME = pwd.getpwuid(os.getuid()).pw_name
OLD_UXPLAY = "#!/bin/sh\necho 'UxPlay 1.71: An open-source AirPlay mirroring server.'\n"   # Debian 13's
NEEDED = ("bash", "curl", "timeout", "sha256sum")
MISSING = [t for t in NEEDED if not HAVE[t]]


@unittest.skipIf(MISSING or not sys.platform.startswith("linux"), f"needs Linux and {', '.join(MISSING)}")
class ScriptTest(unittest.TestCase):
    sandbox_args = {}

    def setUp(self):
        self.sb = Sandbox(**self.sandbox_args)
        self.addCleanup(self.sb.close)

    def assertNoLeftovers(self):
        self.assertTrue(self.sb.no_leftovers(), f"processes left running: {self.sb.leftovers()}")

    def start_and_wait(self, timeout=90, **kw):
        p = self.sb.start(**kw)
        try:
            rc = p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.fail(f"iphone.sh did not finish in {timeout} s\nnotes:\n{self.sb.notes()}\nlog:\n"
                      f"{self.sb.runlog('iphone.log')}")
        return rc

    def debug(self):
        logs = "".join(f"\n--- {n}:\n{self.sb.runlog(n)}" for n in ("cap.log", "tunnel.log", "fwd.log", "wda.log",
                                                                       "window.log"))
        return (f"\n--- notify:\n{self.sb.notes()}\n--- iphone.log:\n{self.sb.runlog('iphone.log')}"
                f"\n--- calls:\n" + "\n".join(self.sb.calls()) + f"\n--- out:\n{self.sb.output()}" + logs)


# ====================================================================== iphone.sh start
class Start(ScriptTest):
    def test_happy_path_wires_everything_to_loopback(self):
        sb = self.sb
        sb.installed()
        rc = self.start_and_wait()
        self.assertEqual(rc, 0, self.debug())
        win = sb.window()
        self.assertEqual(win["status"]["ready"], True, "the window reached WebDriverAgent through the forward")
        self.assertEqual(win["IPHONE_WDA_URL"], f"http://127.0.0.1:{sb.wda_port}")
        self.assertEqual(win["IPHONE_MJPEG_URL"], f"http://127.0.0.1:{sb.mjpeg_port}")
        self.assertTrue(win["NO_PROXY"].startswith("127.0.0.1,localhost"))
        self.assertTrue(win["no_proxy"].startswith("127.0.0.1,localhost"))
        self.assertFalse(win["stdin_tty"])
        self.assertIn("-I", win["argv"])
        self.assertEqual((win["IPHONE_MODE"], win["IPHONE_VIDEO"], win["IPHONE_FROM_SCRIPT"]), ("full", "usb", "1"))
        self.assertEqual(win["IPHONE_CAPTURE_SOCKET"], os.path.join(sb.rundir, "cap.sock"))
        self.assertTrue(win["capture_socket"], "the window found the capture's socket")
        self.assertEqual(len(sb.calls("ios devmode get")), 1)          # auto mode asked for Developer Mode
        self.assertIn("Full mode: WebDriverAgent input (go-ios, WebDriverAgent and Developer Mode are in place)",
                      sb.runlog("iphone.log"))
        # go-ios: pinned to the phone, tunnel key in the state dir, WDA kept off Wi-Fi, no 'ios forward'
        calls = sb.calls("ios ")
        self.assertTrue(all(f"[GO_IOS_UDID={UDID_A}]" in c for c in calls), calls)
        self.assertIn(f"--pair-record-path={sb.state}", sb.calls("ios tunnel start")[0])
        runwda = sb.calls("ios runwda")
        self.assertEqual(len(runwda), 1)
        self.assertIn("--env=USE_IP=127.0.0.1", runwda[0])
        self.assertIn(f"--bundleid={bundle_id()} --testrunnerbundleid={bundle_id()}", runwda[0])
        self.assertIn("--xctestconfig=WebDriverAgentRunner.xctest", runwda[0])
        self.assertFalse([c for c in calls if c.startswith("ios forward")])
        self.assertIn(("ios image auto --basedir=" + os.path.join(sb.state, "ddi")), sb.calls("ios image auto")[0])
        # go-ios got a CA bundle with Apple's root for the disk image download
        ca = sb.read("ssl_cert_file")
        self.assertEqual(ca, os.path.join(sb.state, "ca-bundle-with-apple.pem"))
        with open(ca) as f:
            bundle = f.read()
        self.assertTrue(bundle.startswith("# system CAs from " + sb.ca_bundle))
        self.assertIn("ZmFrZSBzeXN0ZW0gQ0E=", bundle)
        with open(os.path.join(sb.repo, "certs", "apple-root.pem")) as f:
            self.assertIn(f.read().strip(), bundle)
        # no re-sign, a single notification (no terminal), everything stopped afterwards
        self.assertEqual(sb.calls("plumesign"), [])
        self.assertEqual(sb.notes().count("\n"), 1, sb.notes())
        self.assertIn("Starting - this can take up to a minute.", sb.notes())
        self.assertIn("ready: iOS 26.5.2", sb.runlog("iphone.log"))
        self.assertNoLeftovers()
        self.assertFalse([f for f in os.listdir(sb.rundir) if f.endswith(".pid")])
        self.assertFalse(sb.port_open(sb.wda_port))

    def test_private_state_and_run_dirs(self):
        sb = self.sb
        sb.installed()
        os.chmod(sb.state, 0o755)
        ident = os.path.join(sb.state, "selfIdentity.plist")
        with open(ident, "w") as f:
            f.write("key")
        os.chmod(ident, 0o644)                       # as older go-ios wrote it
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertEqual(sb.mode(sb.state), 0o700)
        self.assertEqual(sb.mode(sb.rundir), 0o700)
        self.assertEqual(sb.mode(ident), 0o600)
        self.assertEqual(sb.mode(os.path.join(sb.state, "config.json")) & 0o077, 0)
        for name in os.listdir(sb.rundir):
            self.assertEqual(sb.mode(os.path.join(sb.rundir, name)) & 0o077, 0, name)

    def test_empty_bind_lets_wda_listen_on_wifi(self):
        self.sb.installed()
        self.assertEqual(self.start_and_wait(env={"IPHONE_WDA_BIND": ""}), 0, self.debug())
        self.assertNotIn("USE_IP", self.sb.calls("ios runwda")[0])

    def test_ios_16_needs_no_tunnel(self):
        self.sb.mux.ios_version = "16.7.10"
        self.sb.installed()
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertEqual(self.sb.calls("ios tunnel"), [])
        self.assertEqual(len(self.sb.calls("ios runwda")), 1)

    def test_ios_17_before_17_4_is_refused(self):
        self.sb.mux.ios_version = "17.2.1"
        self.sb.installed()
        self.assertEqual(self.start_and_wait(env={"IPHONE_MODE": "full"}), 1)
        self.assertIn("iOS 17.2.1 is too old for the rootless tunnel", self.sb.notes())
        self.assertEqual(self.sb.calls("ios "), [])
        self.assertNoLeftovers()

    def test_several_phones_need_iphone_udid(self):
        self.sb.mux.devices = [(7, UDID_A), (9, UDID_B)]
        self.sb.installed()
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("Several iPhones are plugged in - choose one: IPHONE_UDID=<udid>", self.sb.notes())
        self.assertIn(UDID_A, self.sb.notes())
        self.assertIn(UDID_B, self.sb.notes())
        self.assertNoLeftovers()

    def test_iphone_udid_picks_one_ignoring_case(self):
        self.sb.mux.devices = [(7, UDID_A), (9, UDID_B)]
        self.sb.mux.paired.add(UDID_B)
        self.sb.installed()
        self.assertEqual(self.start_and_wait(env={"IPHONE_UDID": UDID_B.lower()}), 0, self.debug())
        self.assertTrue(all(f"[GO_IOS_UDID={UDID_B}]" in c for c in self.sb.calls("ios ")))
        self.assertIn((9, 8100), self.sb.mux.connects)      # the forward went to that phone

    def test_busy_port_is_reported(self):
        self.sb.installed()
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.sb.wda_port))
            busy.listen()
            self.assertEqual(self.start_and_wait(), 1)
        self.assertIn(f"forward: cannot listen on 127.0.0.1:{self.sb.wda_port}", self.sb.notes())
        self.assertIn("set IPHONE_WDA_PORT / IPHONE_MJPEG_PORT", self.sb.notes())
        self.assertEqual(self.sb.calls("ios runwda"), [])
        self.assertNoLeftovers()

    def test_waits_while_the_phone_is_locked_for_the_disk_image(self):
        self.sb.installed()
        self.sb.put("image_plan", "locked\nok\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertIn("Unlock the iPhone - waiting for it...", self.sb.notes())
        self.assertEqual(len(self.sb.calls("ios image auto")), 2)
        self.assertEqual(self.sb.calls("plumesign"), [])

    def test_disk_image_mounted_once(self):
        self.sb.installed()
        self.sb.put("mounted")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertEqual(self.sb.calls("ios image auto"), [])

    def test_locked_phone_while_starting_wda_waits_without_resigning(self):
        self.sb.installed()
        self.sb.put("wda_plan", "locked\nok\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertIn("Unlock the iPhone and keep it unlocked - waiting for WebDriverAgent...", self.sb.notes())
        self.assertEqual(len(self.sb.calls("ios runwda")), 2, self.debug())
        self.assertEqual(self.sb.calls("plumesign"), [])

    def test_untrusted_developer_app_waits_without_resigning(self):
        self.sb.installed()
        self.sb.put("wda_plan", "untrusted\nok\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertIn("Trust the developer app: Settings > General > VPN & Device Management", self.sb.notes())
        self.assertEqual(self.sb.calls("plumesign"), [])

    def test_missing_app_is_resigned_once_then_started(self):
        sb = self.sb
        sb.installed(recent_apps=["com.apple.Preferences"])
        sb.put("wda_plan", "missing-until-resign\nmissing-until-resign\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertIn("Re-signing WebDriverAgent with your Apple ID", sb.notes())
        sign = sb.calls("plumesign sign")
        self.assertEqual(len(sign), 1)
        self.assertIn("--udid 7 ", sign[0])                 # usbmuxd's DeviceID, not the UDID
        self.assertEqual(len(sb.calls("ios runwda")), 2)
        cfg = sb.config()
        self.assertEqual(cfg["signed_at"], epoch(sb.now))
        self.assertEqual(cfg["expires_at"], epoch(sb.now + datetime.timedelta(days=7)))
        self.assertEqual(cfg["recent_apps"], ["com.apple.Preferences"])     # other keys kept
        self.assertTrue(os.path.exists(os.path.join(sb.state, "last-resign")))
        self.assertNoLeftovers()

    def test_expired_app_is_resigned(self):
        self.sb.installed(expires_in_days=10)
        self.sb.put("wda_plan", "expired\nok\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertEqual(len(self.sb.calls("plumesign sign")), 1)

    def test_signature_running_out_is_renewed_before_starting(self):
        self.sb.installed(expires_in_days=-1)
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertEqual(len(self.sb.calls("plumesign sign")), 1)
        self.assertGreater(self.sb.config()["expires_at"], time.time() + 6 * 86400)
        # re-signed before the tunnel was even opened
        calls = self.sb.read("calls.log").splitlines()
        self.assertLess(calls.index(next(c for c in calls if c.startswith("plumesign sign"))),
                        calls.index(next(c for c in calls if c.startswith("ios tunnel"))))

    def test_resign_at_most_once_an_hour(self):
        self.sb.installed(expires_in_days=-1)
        open(os.path.join(self.sb.state, "last-resign"), "w").close()
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("Re-signed less than an hour ago", self.sb.notes())
        self.assertEqual(self.sb.calls("plumesign"), [])

    def test_expired_apple_id_session_is_explained(self):
        self.sb.installed()
        self.sb.put("wda_plan", "missing\n")
        self.sb.put("nologin")
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("Apple ID session expired: in a terminal run", self.sb.notes())
        self.assertIn("plumesign account login -u <your Apple ID>", self.sb.notes())
        self.assertNoLeftovers()

    def test_resign_failure_is_reported_without_personal_data(self):
        self.sb.installed()
        self.sb.put("wda_plan", "missing\n")
        self.sb.put("plume_plan", f"fail:provisioning error for someone@example.com on {UDID_A[:8]}-0011223344556677")
        self.assertEqual(self.start_and_wait(), 1)
        notes = self.sb.notes()
        self.assertIn("Re-signing failed", notes)
        self.assertNotIn("someone@example.com", notes)
        self.assertNotIn("0011223344556677", notes)

    def test_still_failing_after_resign_stops(self):
        self.sb.installed()
        self.sb.put("wda_plan", "missing\nmissing\n")
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("WebDriverAgent still does not start after re-signing", self.sb.notes())
        self.assertEqual(len(self.sb.calls("plumesign sign")), 1)

    def test_disk_image_download_failure_does_not_resign(self):
        self.sb.installed()
        self.sb.put("image_plan", "download_fail\n")
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("Could not mount the developer disk image: failed downloading image", self.sb.notes())
        self.assertEqual(self.sb.calls("plumesign"), [])
        self.assertEqual(self.sb.calls("ios runwda"), [])
        self.assertNoLeftovers()

    def test_developer_mode_off_is_explained(self):
        self.sb.installed()
        self.sb.put("image_plan", "error\n")
        self.sb.put("devmode", "false")
        self.assertEqual(self.start_and_wait(env={"IPHONE_MODE": "full"}), 1)
        self.assertIn("Turn on Developer Mode: Settings > Privacy & Security > Developer Mode", self.sb.notes())
        self.assertEqual(self.sb.calls("plumesign"), [])

    def test_tunnel_that_stops_is_reported_without_resigning(self):
        self.sb.installed()
        self.sb.put("tunnel", "exit")
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("The go-ios tunnel stopped: failed to start tunnel: could not connect to the device: "
                      "DeviceLocked", self.sb.notes())
        self.assertEqual(self.sb.calls("plumesign"), [])
        self.assertEqual(self.sb.calls("ios image"), [])
        self.assertNoLeftovers()

    def test_window_error_is_reported(self):
        self.sb.installed()
        self.sb.put("window_mode", "exit3")
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("The window closed with an error (3): fake window error line", self.sb.notes())
        self.assertNoLeftovers()

    def test_old_install_is_recorded_and_its_ipa_deleted(self):
        sb = self.sb
        sb.installed(expires_in_days=None)
        old = os.path.join(sb.repo, "build", "WebDriverAgent-signed.ipa")
        make_signed_ipa(old, created=sb.now, expires=sb.now + datetime.timedelta(days=3))
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertFalse(os.path.exists(old))
        self.assertEqual(sb.config()["expires_at"], epoch(sb.now + datetime.timedelta(days=3)))
        self.assertEqual(sb.calls("plumesign"), [])

    def test_second_start_while_running(self):
        sb = self.sb
        sb.installed()
        sb.put("window_mode", "stay")
        first = sb.start()
        self.assertTrue(sb.wait_for(sb.window, 60), self.debug())
        second = sb.run("iphone.sh")
        self.assertEqual(second.returncode, 0)
        self.assertIn("already starting or running", sb.notes())
        self.assertIsNone(first.poll())
        self.assertEqual(sb.run("iphone.sh", "stop").returncode, 0)
        first.wait(30)


# ====================================================================== the USB screen capture
class Capture(ScriptTest):
    def test_starts_before_the_tunnel_and_stops_last(self):
        sb = self.sb
        sb.installed()
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        calls = sb.read("calls.log").splitlines()
        cap = calls.index(f"screencap --udid {UDID_A} --socket {sb.rundir}/cap.sock")     # no --audio by default
        self.assertLess(calls.index(sb.calls("ios devmode get")[0]), cap, "the mode is known first")
        for later in ("ios tunnel start", "ios image", "ios runwda"):
            self.assertLess(cap, calls.index(sb.calls(later)[0]), later)
        # stopped after everything else (their pid files were gone), and given time to end its session
        self.assertEqual(json.loads(sb.read("cap_term.json")), ["cap.pid", "iphone.pid"])
        self.assertIn("screencap stopped", calls)
        self.assertFalse(os.path.exists(os.path.join(sb.rundir, "cap.sock")))
        self.assertNoLeftovers()

    def test_waits_until_usbmuxd_lists_the_phone_again_after_the_reset(self):
        sb = self.sb
        sb.installed()
        sb.put("cap_plan", "reset\n")
        seen = {}

        def reenumerate():               # the capture's USB reset: usbmuxd loses the phone for a moment
            if sb.wait_for(lambda: os.path.exists(os.path.join(sb.fake, "cap_reset")), 60):
                devices, sb.mux.devices = sb.mux.devices, []
                os.unlink(os.path.join(sb.fake, "cap_reset"))     # it says "capturing" while the phone is away
                time.sleep(3)
                seen["calls"] = sb.calls("ios ")[1:]               # after devmode get
                sb.mux.devices = devices
        t = threading.Thread(target=reenumerate, daemon=True)
        t.start()
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        t.join(10)
        self.assertEqual(seen.get("calls"), [], "nothing went on while usbmuxd did not list the phone")
        self.assertEqual(len(sb.calls("ios runwda")), 1)

    def test_unsupported_falls_back_to_webdriveragents_picture(self):
        sb = self.sb
        sb.installed()
        sb.put("cap_plan", "unsupported\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        win = sb.window()
        self.assertEqual((win["IPHONE_MODE"], win["IPHONE_VIDEO"]), ("full", "mjpeg"))
        self.assertIsNone(win["IPHONE_CAPTURE_SOCKET"])
        self.assertEqual(win["status"]["ready"], True)
        self.assertIn("No USB video (it needs a newer usbmuxd - ./setup.sh --usbmuxd installs one) - using "
                      "WebDriverAgent's picture.", sb.notes())
        self.assertEqual(len(sb.calls("screencap --udid")), 1)

    def test_unsupported_with_iphone_video_usb_stops(self):
        sb = self.sb
        sb.installed()
        sb.put("cap_plan", "unsupported\n")
        self.assertEqual(self.start_and_wait(env={"IPHONE_VIDEO": "usb"}), 1)
        self.assertIn("USB video did not start: it needs a newer usbmuxd - ./setup.sh --usbmuxd installs one",
                      sb.notes())
        self.assertEqual(sb.calls("ios tunnel"), [])
        self.assertNoLeftovers()

    def test_waits_while_the_phone_is_locked(self):
        sb = self.sb
        sb.installed()
        sb.put("cap_plan", "locked\nok\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertIn("Unlock the iPhone - waiting for it...", sb.notes())
        self.assertEqual(len(sb.calls("screencap --udid")), 2)
        self.assertEqual(sb.window()["IPHONE_VIDEO"], "usb")

    def test_unplugged_while_starting_waits_for_the_phone(self):
        sb = self.sb
        sb.installed()
        sb.usb_device()                                  # back on this computer's USB at once
        sb.put("cap_plan", "unplugged\nok\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertIn("iPhone unplugged - plug it back in.", sb.notes())
        self.assertEqual(len(sb.calls("screencap --udid")), 2)
        self.assertEqual(sb.window()["IPHONE_VIDEO"], "usb")

    def test_a_phone_on_another_machines_usb_falls_back_at_once(self):
        # usbmuxd lists it (USBMUXD_SOCKET_ADDRESS), this computer's sysfs does not have it, which is all the
        # capture looks at: it says "unplugged" every time
        sb = self.sb
        sb.installed()
        sb.put("cap_plan", "unplugged\n" * 20)
        t = time.monotonic()
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertLess(time.monotonic() - t, 60)
        self.assertEqual(len(sb.calls("screencap --udid")), 1)
        self.assertIn("No USB video (the iPhone is not on this computer's USB (USBMUXD_SOCKET_ADDRESS: a usbmuxd on "
                      "another machine)) - using WebDriverAgent's picture.", sb.notes())
        self.assertNotIn("iPhone unplugged", sb.notes())
        self.assertEqual(sb.window()["IPHONE_VIDEO"], "mjpeg")

    def test_waiting_for_a_replug_ends_in_time(self):
        sb = self.sb
        sb.installed()
        sb.usb_device()                                  # usbmuxd and sysfs have it, the capture says it went
        sb.put("cap_plan", "unplugged\n" * 100)
        sb.patch("iphone.sh", "local rc told='' end=$((SECONDS + 120))", "local rc told='' end=$((SECONDS + 4))")
        self.assertEqual(self.start_and_wait(timeout=60), 1)
        self.assertIn("The iPhone was unplugged - plug it in and start again.", sb.notes())
        self.assertLess(len(sb.calls("screencap --udid")), 10)
        self.assertNoLeftovers()

    def test_a_capture_that_stops_while_starting_is_started_again(self):
        sb = self.sb
        sb.installed()
        sb.put("image_plan", "locked\nok\n")             # the disk image waits for an unlock meanwhile

        def kill_capture():
            if sb.wait_for(lambda: "Unlock the iPhone - waiting for it..." in sb.notes(), 60):
                os.kill(int(sb.runlog("cap.pid")), signal.SIGKILL)
        t = threading.Thread(target=kill_capture, daemon=True)
        t.start()
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        t.join(10)
        win = sb.window()
        self.assertEqual(win["IPHONE_VIDEO"], "usb")
        self.assertTrue(win["capture_socket"], "the window found a capture to connect to")
        self.assertEqual(len(sb.calls("screencap --udid")), 2)
        self.assertIn("the screen capture stopped while starting (killed)", sb.runlog("iphone.log"))

    def test_an_error_falls_back_and_says_why(self):
        self.sb.installed()
        self.sb.put("cap_plan", "error\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertIn("No USB video (screencap: error: [Errno 71] Protocol error. Log: ", self.sb.notes())
        self.assertEqual(self.sb.window()["IPHONE_VIDEO"], "mjpeg")

    def test_without_an_h264_decoder_full_mode_uses_mjpeg(self):
        self.sb.installed()
        self.sb.put("gst_missing", "h264dec\n")
        self.assertEqual(self.start_and_wait(), 0, self.debug())
        self.assertEqual(self.sb.calls("screencap"), [])
        self.assertIn("No USB video (GStreamer lacks an H.264 decoder (vah264dec, avdec_h264, openh264dec)",
                      self.sb.notes())
        self.assertEqual(self.sb.window()["IPHONE_VIDEO"], "mjpeg")

    def test_a_missing_iphone_decoder_means_no_usb_video(self):
        self.sb.installed()
        self.sb.put("gst_missing", "nvh264dec\n")       # the window's default decoders are there, not this one
        self.assertEqual(self.start_and_wait(env={"IPHONE_DECODER": "nvh264dec"}), 0, self.debug())
        self.assertEqual(self.sb.calls("screencap"), [])
        self.assertIn("No USB video (GStreamer lacks the H.264 decoder IPHONE_DECODER=nvh264dec: ", self.sb.notes())
        self.assertEqual(self.sb.window()["IPHONE_VIDEO"], "mjpeg")

    def test_usb_video_needs_none_of_the_mjpeg_elements(self):
        sb = self.sb
        sb.installed()
        sb.put("gst_missing", "souphttpsrc\n")          # no libsoup 3: WebDriverAgent's picture cannot play
        for video in ("usb", "auto"):
            self.assertEqual(self.start_and_wait(env={"IPHONE_VIDEO": video}), 0, self.debug())
            self.assertEqual(sb.window()["IPHONE_VIDEO"], "usb", video)
        sb.put("cap_plan", "unsupported\n")             # ...so auto has nothing to fall back to
        self.assertEqual(self.start_and_wait(), 1)
        self.assertIn("No USB video (it needs a newer usbmuxd - ./setup.sh --usbmuxd installs one), and WebDriverAgent's "
                      "picture needs GStreamer's souphttpsrc - ", sb.notes())
        sb.put("gst_missing", "souphttpsrc\nh264parse\n")   # neither picture: refused before anything starts
        p = sb.run("iphone.sh", timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertIn("GStreamer elements missing: souphttpsrc, h264parse - ", sb.notes())
        self.assertEqual(len(sb.calls("screencap --udid")), 3)
        self.assertNoLeftovers()

    def test_iphone_video_mjpeg_starts_no_capture(self):
        self.sb.installed()
        self.assertEqual(self.start_and_wait(env={"IPHONE_VIDEO": "mjpeg"}), 0, self.debug())
        self.assertEqual(self.sb.calls("screencap"), [])
        self.assertEqual(self.sb.window()["IPHONE_VIDEO"], "mjpeg")
        self.assertEqual(self.sb.notes().count("\n"), 1, self.sb.notes())

    def test_settings_reach_the_window_and_the_capture(self):
        env = {"IPHONE_DOUBLE_CLICK_MS": "250", "IPHONE_AUDIO": "1", "IPHONE_DECODER": "avdec_h264",
               "IPHONE_HID_NAME": "Test Mouse", "BT_INDEX": "1"}
        self.sb.installed()
        self.assertEqual(self.start_and_wait(env=env), 0, self.debug())
        win = self.sb.window()
        self.assertEqual({k: win[k] for k in env}, env)
        self.assertTrue(self.sb.calls("screencap --udid")[0].endswith(" --audio"))

    def test_stop_gives_the_capture_time_to_end_its_session(self):
        sb = self.sb
        sb.installed()
        sb.put("window_mode", "stay")
        sb.put("cap_term_delay", "4")                    # longer than the 3 s everything else gets
        p = sb.start()
        self.assertTrue(sb.wait_for(sb.window, 60), self.debug())
        t = time.monotonic()
        self.assertEqual(sb.run("iphone.sh", "stop", timeout=40).returncode, 0)
        self.assertEqual(p.wait(20), 143)
        self.assertGreater(time.monotonic() - t, 3.5)
        self.assertIn("screencap stopped", sb.calls("screencap"), "it was not killed")
        self.assertNoLeftovers()


# ====================================================================== basic mode, and how auto picks the mode
class Basic(ScriptTest):
    def assertBasic(self, why, env=None):
        sb = self.sb
        self.assertEqual(self.start_and_wait(env=env), 0, self.debug())
        win = sb.window()
        self.assertEqual((win["IPHONE_MODE"], win["IPHONE_VIDEO"], win["IPHONE_FROM_SCRIPT"]), ("basic", "usb", "1"))
        self.assertEqual((win["IPHONE_WDA_URL"], win["IPHONE_MJPEG_URL"]), (None, None))
        self.assertTrue(win["capture_socket"])
        self.assertIn(f"Basic mode: Bluetooth input through AssistiveTouch ({why})", sb.runlog("iphone.log"))
        self.assertEqual([c for c in sb.mux.connects if c[1] in (8100, 9100)], [], "no forward, no WebDriverAgent")
        self.assertFalse({"fwd.log", "wda.log", "tunnel.log", "image.log"} & set(os.listdir(sb.rundir)))
        self.assertEqual(sb.calls("plumesign"), [])
        self.assertNoLeftovers()

    def test_basic_mode_makes_no_ios_calls(self):
        self.sb.installed()                              # full mode would work: basic is asked for
        self.assertBasic("IPHONE_MODE=basic", env={"IPHONE_MODE": "basic"})
        self.assertEqual(self.sb.calls("ios "), [])
        self.assertEqual(self.sb.notes().count("\n"), 1, "an explicit basic mode is not announced: " + self.sb.notes())

    def test_auto_without_go_ios(self):
        self.sb.installed()
        os.unlink(os.path.join(self.sb.bin, "ios"))
        self.assertBasic("go-ios is not installed; ./setup.sh adds full mode")
        self.assertIn("Basic mode: Bluetooth input through AssistiveTouch (go-ios is not installed", self.sb.notes())

    def test_auto_without_webdriveragent(self):
        self.assertBasic("WebDriverAgent is not installed; ./resign.sh adds full mode")
        self.assertEqual(self.sb.calls("ios "), [])

    def test_auto_with_developer_mode_off(self):
        self.sb.installed()
        self.sb.put("devmode", "false")
        self.assertBasic("Developer Mode is off; Settings > Privacy & Security > Developer Mode turns on full mode")
        self.assertEqual([c.split(" [")[0] for c in self.sb.calls("ios ")], ["ios devmode get"])

    def test_auto_on_ios_17_before_17_4(self):
        self.sb.mux.ios_version = "17.2.1"
        self.sb.installed()
        self.assertBasic("iOS 17.2.1 is too old for full mode's rootless tunnel (17.4.1 or newer)")
        self.assertEqual(self.sb.calls("ios "), [])

    def test_unsupported_capture_uses_airplay(self):
        sb = self.sb                                     # with the sandbox's uxplay
        sb.put("cap_plan", "unsupported\n")
        self.assertEqual(self.start_and_wait(env={"IPHONE_MODE": "basic"}), 0, self.debug())
        win = sb.window()
        self.assertEqual((win["IPHONE_MODE"], win["IPHONE_VIDEO"]), ("basic", "airplay"))
        self.assertIsNone(win["IPHONE_CAPTURE_SOCKET"])
        self.assertIn("No USB video (it needs a newer usbmuxd - ./setup.sh --usbmuxd installs one) - using AirPlay",
                      sb.notes())

    def test_unsupported_capture_without_uxplay_says_what_to_install(self):
        sb = self.sb
        sb.hide("uxplay")
        sb.put("cap_plan", "unsupported\n")
        self.assertEqual(self.start_and_wait(env={"IPHONE_MODE": "basic"}), 1)
        self.assertIn("No picture for basic mode: USB video did not start (it needs a newer usbmuxd - ./setup.sh "
                      "--usbmuxd installs one), and AirPlay needs uxplay 1.73+ (not installed).", sb.notes())
        self.assertIsNone(sb.window())
        self.assertNoLeftovers()

    def test_unsupported_capture_with_an_old_uxplay_says_so(self):
        sb = self.sb
        sb.tool("uxplay", OLD_UXPLAY)                    # the window's AirPlay picture needs 1.73 (-vrtp)
        sb.put("cap_plan", "unsupported\n")
        self.assertEqual(self.start_and_wait(env={"IPHONE_MODE": "basic"}), 1)
        self.assertIn("No picture for basic mode: USB video did not start (it needs a newer usbmuxd - ./setup.sh "
                      "--usbmuxd installs one), and AirPlay needs uxplay 1.73+ (found 1.71).", sb.notes())
        self.assertIsNone(sb.window())
        self.assertNoLeftovers()

    def test_iphone_decoder_alone_is_enough(self):
        self.sb.put("gst_missing", "vah264dec\navdec_h264\nopenh264dec\n")   # NVIDIA's decoder, none of the others
        self.assertBasic("IPHONE_MODE=basic", env={"IPHONE_MODE": "basic", "IPHONE_DECODER": "nvh264dec"})
        self.assertEqual(self.sb.window()["IPHONE_DECODER"], "nvh264dec")

    def running(self, env=None):
        self.sb.put("window_mode", "stay")
        p = self.sb.start(env=dict({"IPHONE_MODE": "basic"}, **(env or {})))
        self.assertTrue(self.sb.wait_for(self.sb.window, 60), self.debug())
        return p

    def stop(self, p):
        self.sb.run("iphone.sh", "stop")
        p.wait(20)

    def test_watchdog_restarts_a_stopped_capture_and_nothing_else(self):
        sb = self.sb
        p = self.running()
        os.kill(int(sb.runlog("cap.pid")), signal.SIGKILL)  # our own fake
        self.assertTrue(sb.wait_for(lambda: len(sb.calls("screencap --udid")) >= 2, 20), self.debug())
        self.assertTrue(sb.wait_for(lambda: os.path.exists(os.path.join(sb.rundir, "cap.sock")), 10))
        self.assertIn("USB video stopped - restarting it...", sb.notes())
        self.assertIn("watchdog: restarting the screen capture", sb.runlog("iphone.log"))
        time.sleep(4)                                    # another round: still no forward, tunnel or WebDriverAgent
        self.assertEqual(sb.calls("ios "), [])
        self.assertFalse(os.path.exists(os.path.join(sb.rundir, "fwd.pid")))
        self.assertIsNone(p.poll())
        sb.run("iphone.sh", "stop")
        p.wait(20)

    def test_watchdog_gives_up_on_a_capture_that_keeps_stopping(self):
        sb = self.sb
        sb.put("cap_plan", "ok\n" + "error\n" * 8)
        p = self.running()
        os.kill(int(sb.runlog("cap.pid")), signal.SIGKILL)
        self.assertTrue(sb.wait_for(lambda: "USB video keeps stopping" in sb.notes(), 40), self.debug())
        n = len(sb.calls("screencap --udid"))
        self.assertEqual(n, 5)                           # the first, then four restarts
        self.assertEqual(sb.notes().count("USB video stopped - restarting it..."), 1)
        time.sleep(4)
        self.assertEqual(len(sb.calls("screencap --udid")), n, "no more restarts")
        sb.run("iphone.sh", "stop")
        p.wait(20)

    def test_watchdog_waits_for_an_unlock_and_never_gives_up_on_it(self):
        sb = self.sb
        sb.patch("iphone.sh", "CAP_STARTED)) -ge 30 ]", "CAP_STARTED)) -ge 2 ]")   # tries again every 2 s, not 30
        sb.put("cap_plan", "ok\n" + "locked\n" * 7 + "ok\n")
        p = self.running()
        os.kill(int(sb.runlog("cap.pid")), signal.SIGKILL)
        self.assertTrue(sb.wait_for(lambda: len(sb.calls("screencap --udid")) >= 9, 90), self.debug())
        self.assertTrue(sb.wait_for(lambda: "capturing" in sb.runlog("cap.log"), 10), self.debug())
        notes = sb.notes()
        self.assertEqual(notes.count("No picture from the iPhone - unlock it; if it stays black, restart the iPhone."), 1,
                         notes)
        self.assertEqual(notes.count("USB video stopped - restarting it..."), 1, notes)   # the kill, not the lock
        self.assertNotIn("USB video keeps stopping", notes)
        self.assertIn("watchdog: restarting the screen capture (it ended: 4)", sb.runlog("iphone.log"))
        self.assertIsNone(p.poll())
        self.stop(p)

    def test_watchdog_tries_a_locked_phone_again_after_a_replug(self):
        sb = self.sb
        sb.patch("iphone.sh", "CAP_STARTED)) -ge 30 ]", "CAP_STARTED)) -ge 300 ]")   # no retry by the clock here
        sb.put("cap_plan", "ok\nlocked\nok\n")
        p = self.running()
        os.kill(int(sb.runlog("cap.pid")), signal.SIGKILL)
        self.assertTrue(sb.wait_for(lambda: "No picture from the iPhone - unlock it; if it stays black, restart the iPhone."
                                    in sb.notes(), 20), self.debug())
        time.sleep(7)                                    # two more looks: a new capture would reset the USB again
        self.assertEqual(len(sb.calls("screencap --udid")), 2, "not tried again on its own")
        devices, sb.mux.devices = sb.mux.devices, []     # unplugged and plugged in again: it may be unlocked now
        self.assertTrue(sb.wait_for(lambda: "iPhone unplugged - plug it back in." in sb.notes(), 30), self.debug())
        sb.mux.devices = devices
        self.assertTrue(sb.wait_for(lambda: len(sb.calls("screencap --udid")) == 3, 15), self.debug())
        self.assertTrue(sb.wait_for(lambda: "capturing" in sb.runlog("cap.log"), 10), self.debug())
        self.assertEqual(sb.notes().count("USB video stopped"), 1, sb.notes())
        self.stop(p)

    def test_the_captures_own_usb_reset_is_no_unplug(self):
        sb = self.sb
        sb.put("cap_plan", "stall-reset\n")
        p = self.running()
        self.assertTrue(sb.wait_for(lambda: os.path.exists(os.path.join(sb.fake, "cap_selfreset")), 20),
                        self.debug())
        devices, sb.mux.devices = sb.mux.devices, []     # re-enumerating: off usbmuxd for more than two looks
        time.sleep(7.5)
        sb.mux.devices = devices
        time.sleep(4)
        self.assertNotIn("iPhone unplugged", sb.notes())
        self.assertNotIn("iPhone back", sb.notes())
        self.assertEqual(len(sb.calls("screencap --udid")), 1, "it went on capturing")
        self.stop(p)

    def test_a_phone_gone_for_one_look_is_no_unplug(self):
        sb = self.sb                                     # AirPlay: no capture, so no reset that could explain it
        p = self.running({"IPHONE_VIDEO": "airplay"})
        devices = sb.mux.devices
        for _ in range(3):                               # less than a look apart away, as after a USB reset
            sb.mux.devices = []
            time.sleep(2.5)
            sb.mux.devices = devices
            time.sleep(4.5)
        self.assertNotIn("iPhone unplugged", sb.notes())
        sb.mux.devices = []
        self.assertTrue(sb.wait_for(lambda: "iPhone unplugged - plug it back in." in sb.notes(), 15), self.debug())
        sb.mux.devices = devices
        self.assertTrue(sb.wait_for(lambda: "iPhone back - reconnecting..." in sb.notes(), 10), self.debug())
        self.stop(p)


@unittest.skipUnless(os.environ.get("IPHONE_SLOW_TESTS") == "1", "set IPHONE_SLOW_TESTS=1 (each waits ~60 s)")
class SlowTunnel(ScriptTest):
    def test_tunnel_never_ready(self):
        self.sb.installed()
        self.sb.put("tunnel", "notunnel")            # the agent runs but never gets a tunnel for the phone
        t = time.monotonic()
        self.assertEqual(self.start_and_wait(timeout=150), 1)
        self.assertLess(time.monotonic() - t, 120)
        self.assertIn("Unlock the iPhone (tap Trust if it asks) - waiting for it...", self.sb.notes())
        self.assertIn("Could not open the developer tunnel", self.sb.notes())
        self.assertEqual(self.sb.calls("plumesign"), [])
        self.assertEqual(self.sb.calls("ios image"), [])
        self.assertNoLeftovers()

    def test_someone_elses_agent_without_our_tunnel(self):
        sb = self.sb
        sb.installed()
        sb.put("tunnel", "notunnel")
        env = dict(sb.env, GO_IOS_UDID="another-phone")
        other = subprocess.Popen([os.path.join(sb.bin, "ios"), "tunnel", "start", "--userspace",
                                  "--pair-record-path=" + sb.root], env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(sb.wait_for(lambda: sb.port_open(sb.agent_port), 10))
            self.assertEqual(self.start_and_wait(timeout=150), 1)
            self.assertIn(f"Another go-ios agent on port {sb.agent_port} has no tunnel for this iPhone", sb.notes())
            self.assertEqual(len(sb.calls("ios tunnel start")), 1)          # only ours above; iphone.sh reused it
            self.assertIsNone(other.poll(), "iphone.sh must not stop an agent it did not start")
        finally:
            other.kill()
            other.wait()


# ====================================================================== missing pieces
class NotSetUp(ScriptTest):
    def assertFailsFast(self, text, env=None):
        t = time.monotonic()
        p = self.sb.run("iphone.sh", env=env, timeout=30)
        self.assertEqual(p.returncode, 1, p.output + self.sb.notes())
        self.assertIn(text, self.sb.notes() + p.output)
        self.assertLess(time.monotonic() - t, 15)
        self.assertEqual(self.sb.calls("ios "), [])

    def test_no_go_ios(self):
        self.sb.installed()
        os.unlink(os.path.join(self.sb.bin, "ios"))
        self.assertFailsFast("Not set up: go-ios (", env={"IPHONE_MODE": "full"})

    def test_no_curl(self):
        self.sb.installed()
        farm = os.path.join(self.sb.root, "farm")
        os.makedirs(farm)
        for d in SYSTEM_PATH.split(":"):
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                src, dst = os.path.join(d, name), os.path.join(farm, name)
                if name != "curl" and not os.path.lexists(dst) and os.access(src, os.X_OK):
                    os.symlink(src, dst)
        self.assertFailsFast("curl missing", env={"PATH": self.sb.fakebin + ":" + farm})

    def test_no_display(self):
        self.sb.installed()
        self.assertFailsFast("No graphical session", env={"WAYLAND_DISPLAY": None, "DISPLAY": None})

    @unittest.skipIf(subprocess.call(["/usr/bin/python3", "-I", "-c", "import gi"], stderr=subprocess.DEVNULL) == 0
                     if os.path.exists("/usr/bin/python3") else False,
                     "/usr/bin/python3 has PyGObject, and iphone.sh always tries it")
    def test_no_pygobject(self):
        self.sb.installed()
        self.sb.put("no_gi")
        self.assertFailsFast("No Python 3.9+ with PyGObject")

    def test_not_signed_yet(self):
        self.assertFailsFast("WebDriverAgent is not installed yet - run", env={"IPHONE_MODE": "full"})

    def test_mode_and_video_settings_are_checked(self):
        for env in ({"IPHONE_MODE": "fast"}, {"IPHONE_VIDEO": "hdmi"}, {"BT_INDEX": "hci0"},
                    {"IPHONE_DOUBLE_CLICK_MS": "soon"}, {"IPHONE_DECODER": "avdec_h264 ! fakesink"}):
            p = self.sb.run("iphone.sh", "status", env=env)
            self.assertEqual(p.returncode, 2, env)
            self.assertIn("bad " + next(iter(env)), p.output)

    def test_basic_mode_refuses_mjpeg(self):
        self.assertFailsFast("IPHONE_VIDEO=mjpeg is WebDriverAgent's picture, which basic mode does not have",
                             env={"IPHONE_MODE": "basic", "IPHONE_VIDEO": "mjpeg"})

    def test_airplay_needs_uxplay(self):
        self.sb.hide("uxplay")
        self.assertFailsFast("IPHONE_VIDEO=airplay needs uxplay 1.73+",
                             env={"IPHONE_MODE": "basic", "IPHONE_VIDEO": "airplay"})

    def test_airplay_needs_uxplay_1_73(self):
        self.sb.tool("uxplay", OLD_UXPLAY)
        self.assertFailsFast("IPHONE_VIDEO=airplay needs uxplay 1.73+ (found 1.71)",
                             env={"IPHONE_MODE": "basic", "IPHONE_VIDEO": "airplay"})

    def test_a_missing_iphone_decoder_is_named(self):
        self.sb.put("gst_missing", "nvh264dec\n")       # the default decoders are there, the one asked for is not
        self.assertFailsFast("GStreamer elements missing: the H.264 decoder IPHONE_DECODER=nvh264dec - ",
                             env={"IPHONE_MODE": "basic", "IPHONE_DECODER": " nvh264dec "})
        self.assertIn("or unset IPHONE_DECODER", self.sb.notes())
        self.assertEqual(self.sb.calls("screencap"), [])

    def test_basic_mode_needs_an_h264_decoder(self):
        self.sb.put("gst_missing", "h264dec\n")
        self.assertFailsFast("GStreamer elements missing: an H.264 decoder (vah264dec, avdec_h264, openh264dec)",
                             env={"IPHONE_MODE": "basic"})
        self.assertEqual(self.sb.calls("screencap"), [])

    def test_bad_ports(self):
        for env in ({"IPHONE_WDA_PORT": "abc"}, {"IPHONE_MJPEG_PORT": "70000"}, {"GO_IOS_AGENT_PORT": "0"},
                    {"IPHONE_WDA_PORT": "8100", "IPHONE_MJPEG_PORT": "8100"}):
            p = self.sb.run("iphone.sh", "status", env=env)
            self.assertEqual(p.returncode, 2, env)

    def test_usage(self):
        p = self.sb.run("iphone.sh", "--help")
        self.assertEqual(p.returncode, 0)
        for word in ("start", "stop", "status", "doctor", "IPHONE_UDID", "IPHONE_WDA_PORT", "IPHONE_MJPEG_PORT",
                     "IPHONE_WDA_BIND", "IPHONE_PYTHON", "GO_IOS_AGENT_PORT", "IPHONE_MODE", "IPHONE_VIDEO",
                     "IPHONE_DOUBLE_CLICK_MS", "IPHONE_DECODER", "IPHONE_AUDIO", "IPHONE_HID_NAME", "BT_INDEX"):
            self.assertIn(word, p.output)
        self.assertEqual(self.sb.run("iphone.sh", "help").returncode, 0)
        bad = self.sb.run("iphone.sh", "bogus")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("Usage:", bad.output)


# ====================================================================== signals, stop, watchdog
class Lifecycle(ScriptTest):
    def running(self):
        self.sb.installed()
        self.sb.put("window_mode", "stay")
        p = self.sb.start()
        self.assertTrue(self.sb.wait_for(self.sb.window, 60), self.debug())
        return p

    def test_stop_ends_everything(self):
        p = self.running()
        self.assertTrue(self.sb.port_open(self.sb.wda_port))
        out = self.sb.run("iphone.sh", "stop", timeout=40)
        self.assertEqual(out.returncode, 0)
        self.assertIn("stopped", out.output)
        self.assertEqual(p.wait(20), 143)
        self.assertNoLeftovers()
        self.assertFalse([f for f in os.listdir(self.sb.rundir) if f.endswith(".pid")])
        self.assertFalse(self.sb.port_open(self.sb.wda_port))
        self.assertFalse(self.sb.port_open(self.sb.agent_port))

    def test_stop_when_nothing_runs(self):
        out = self.sb.run("iphone.sh", "stop")
        self.assertEqual((out.returncode, out.output.strip()), (0, "stopped"))

    def test_ctrl_c_while_running_stops_every_process(self):
        p = self.running()
        os.kill(p.pid, signal.SIGINT)
        self.assertEqual(p.wait(20), 130)
        self.assertNoLeftovers()
        self.assertFalse(self.sb.port_open(self.sb.wda_port))

    def test_sigterm_and_sighup(self):
        for sig, rc in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            p = self.running()
            os.kill(p.pid, sig)
            self.assertEqual(p.wait(20), rc)
            self.assertNoLeftovers()
            os.unlink(os.path.join(self.sb.fake, "window.json"))

    def test_ctrl_c_while_waiting_for_the_phone(self):
        self.sb.mux.devices = []
        self.sb.installed()
        p = self.sb.start()
        self.assertTrue(self.sb.wait_for(lambda: "Plug the iPhone in" in self.sb.notes(), 30))
        os.kill(p.pid, signal.SIGINT)
        self.assertEqual(p.wait(20), 130)
        self.assertFalse(os.path.exists(os.path.join(self.sb.rundir, "iphone.pid")))
        self.assertNoLeftovers()

    def test_waits_for_trust(self):
        self.sb.mux.paired = set()
        self.sb.installed()
        p = self.sb.start()
        self.assertTrue(self.sb.wait_for(lambda: "Unlock the iPhone and tap Trust" in self.sb.notes(), 30))
        self.sb.mux.paired = {UDID_A}                       # the user tapped Trust
        self.assertTrue(self.sb.wait_for(self.sb.window, 60), self.debug())
        p.wait(30)

    def test_watchdog_restarts_a_stopped_wda(self):
        p = self.running()
        wda_pid = int(self.sb.runlog("wda.pid"))
        os.kill(wda_pid, signal.SIGKILL)                     # our own fake
        self.assertTrue(self.sb.wait_for(lambda: len(self.sb.calls("ios runwda")) >= 2, 40), self.debug())
        self.assertTrue(self.sb.wait_for(lambda: self._status_ok(), 20))
        self.assertIn("watchdog: restarting WebDriverAgent", self.sb.runlog("iphone.log"))
        self.assertIsNone(p.poll())

    def _status_ok(self):
        import urllib.request
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return opener.open(f"http://127.0.0.1:{self.sb.wda_port}/status", timeout=2).status == 200
        except OSError:
            return False

    def test_unplug_and_replug(self):
        p = self.running()
        self.sb.mux.devices = []
        self.assertTrue(self.sb.wait_for(lambda: "iPhone unplugged - plug it back in." in self.sb.notes(), 20))
        self.assertTrue(self.sb.wait_for(lambda: "the iPhone was unplugged" in self.sb.runlog("fwd.log"), 20))
        self.sb.mux.devices = [(8, UDID_A)]                 # a replugged phone gets a new DeviceID
        self.assertTrue(self.sb.wait_for(lambda: "iPhone back - reconnecting..." in self.sb.notes(), 20))
        self.assertTrue(self.sb.wait_for(self._status_ok, 30), self.debug())
        self.assertIn((8, 8100), self.sb.mux.connects)
        self.assertIsNone(p.poll())
        self.assertNotIn("USB video stopped", self.sb.notes(), "a capture that ended with the unplug is no news")

    def test_giving_up_on_webdriveragent_leaves_the_capture_watched(self):
        sb = self.sb
        p = self.running()
        sb.put("wda_plan", "locked\n" * 10)
        os.kill(int(sb.runlog("wda.pid")), signal.SIGKILL)
        self.assertTrue(sb.wait_for(lambda: "WebDriverAgent keeps stopping" in sb.notes(), 90), self.debug())
        os.kill(int(sb.runlog("cap.pid")), signal.SIGKILL)
        self.assertTrue(sb.wait_for(lambda: len(sb.calls("screencap --udid")) >= 2, 20), self.debug())
        self.assertTrue(sb.wait_for(lambda: os.path.exists(os.path.join(sb.rundir, "cap.sock")), 10))
        self.assertIsNone(p.poll())

    def test_stop_ends_an_airplay_receiver_a_crashed_window_left(self):
        sb = self.sb
        os.makedirs(sb.rundir, exist_ok=True)
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "uxplay", "-n", sb.root])
        self.addCleanup(lambda: (orphan.poll() is None and orphan.kill(), orphan.wait()))
        with open(os.path.join(sb.rundir, "uxplay.pid"), "w") as f:
            f.write(f"{orphan.pid}\n")
        self.assertRegex(sb.run("iphone.sh", "status").output, r"\n +uxplay +running")
        self.assertEqual(sb.run("iphone.sh", "stop").returncode, 0)
        self.assertEqual(orphan.wait(10), -signal.SIGTERM)
        self.assertFalse(os.path.exists(os.path.join(sb.rundir, "uxplay.pid")))

    def test_watchdog_restarts_a_stopped_capture(self):
        p = self.running()
        os.kill(int(self.sb.runlog("cap.pid")), signal.SIGKILL)  # our own fake
        self.assertTrue(self.sb.wait_for(lambda: len(self.sb.calls("screencap --udid")) >= 2, 20), self.debug())
        self.assertIn("USB video stopped - restarting it...", self.sb.notes())
        self.assertIn("watchdog: restarting the screen capture", self.sb.runlog("iphone.log"))
        self.assertEqual(len(self.sb.calls("ios runwda")), 1, "WebDriverAgent still answered, so it was left alone")
        self.assertIsNone(p.poll())


# ====================================================================== status / doctor
class StatusDoctor(ScriptTest):
    def test_status_idle(self):
        self.sb.installed(expires_in_days=5.5)
        p = self.sb.run("iphone.sh", "status")
        self.assertEqual(p.returncode, 0, p.output)
        out = p.output
        self.assertIn("running:        no", out)
        self.assertIn(f"iPhone on USB:  {UDID_A} (no tunnel)", out)
        self.assertIn("go-ios:         1.3.2", out)
        self.assertIn("plumesign:      installed", out)
        self.assertIn("WebDriverAgent: com.example.WebDriverAgentRunner.xctrunner.<team id>", out)
        self.assertNotIn(bundle_id(), out)
        self.assertRegex(out, r"expires: +\w{3} \d\d \w{3} \d{4} \d\d:\d\d \(in 5 days 1\d h\)")
        self.assertIn("WebDriverAgent: not answering", out)
        self.assertEqual(self.sb.calls("ios tunnel"), [])        # status starts nothing

    def test_status_expired_and_not_installed(self):
        self.sb.installed(expires_in_days=-2)
        self.assertIn("expires:        EXPIRED", self.sb.run("iphone.sh", "status").output)
        os.unlink(os.path.join(self.sb.state, "config.json"))
        self.assertIn("WebDriverAgent: not installed - run ./resign.sh", self.sb.run("iphone.sh", "status").output)
        self.sb.installed(expires_in_days=None)
        self.assertIn("expires:        unknown", self.sb.run("iphone.sh", "status").output)

    def test_status_no_usbmuxd(self):
        self.sb.installed()
        out = self.sb.run("iphone.sh", "status", env={"USBMUXD_SOCKET_ADDRESS": "UNIX:/nonexistent"}).output
        self.assertIn("iPhones on USB: ? (usbmuxd is not running)", out)

    def test_status_while_running(self):
        self.sb.installed()
        self.sb.put("window_mode", "stay")
        p = self.sb.start()
        self.assertTrue(self.sb.wait_for(self.sb.window, 60), self.debug())
        out = self.sb.run("iphone.sh", "status").output
        self.assertRegex(out, r"running: +yes \(pid \d+\)")
        for n in ("cap", "tunnel", "fwd", "wda", "watchdog", "window"):
            self.assertRegex(out, rf"\n +{n} +running", n)
        self.assertIn("mode:           full (go-ios, WebDriverAgent and Developer Mode are in place)", out)
        self.assertIn(f"picture:        USB capture ({self.sb.rundir}/cap.sock)", out)
        self.assertIn(f"WebDriverAgent: answering on 127.0.0.1:{self.sb.wda_port} (iOS 26.5.2)", out)
        self.assertIn(f"{UDID_A} (tunnel up)", out)
        doc = self.sb.run("iphone.sh", "doctor")
        self.assertIn("ok    WebDriverAgent answers (iOS 26.5.2)", doc.output)
        self.assertIn("ok    developer tunnel up", doc.output)
        self.assertIn("ok    Developer Mode on", doc.output)
        self.assertIn("ok    developer disk image mounted", doc.output)
        self.assertIn("ok    screen stream sends JPEG frames", doc.output)
        self.assertIn("ok    cap running", doc.output)
        if HAVE["ss"]:
            self.assertIn(f"ok    port {self.sb.wda_port}: our forward on 127.0.0.1:{self.sb.wda_port}", doc.output)
        self.sb.run("iphone.sh", "stop")
        p.wait(20)

    def test_status_shows_the_mode_usb_video_and_usbmuxd(self):
        sb = self.sb
        sb.installed()
        sb.usb_device(configs=5, current=5)
        out = sb.run("iphone.sh", "status").output
        self.assertIn("mode:           auto -> full (go-ios, WebDriverAgent and Developer Mode are in place)", out)
        self.assertIn("    USB video:    ready (the iPhone is in its capture USB configuration, 5 of 5)", out)
        self.assertIn(f"usbmuxd:        1.1.1 ({sb.fakebin}/usbmuxd) - too old for USB video: ./setup.sh --usbmuxd",
                      out)
        sb.usb_device(configs=5, current=4)
        self.assertIn("    USB video:    usbmuxd keeps the iPhone in USB configuration 4, without the capture "
                      "interface (usbmuxd 1.1.1 does) - ./setup.sh --usbmuxd", sb.run("iphone.sh", "status").output)
        sb.usb_device(configs=4, current=4)
        self.assertIn("    USB video:    usbmuxd did not switch on the iPhone's capture configuration (it has 4) - "
                      "./setup.sh --usbmuxd", sb.run("iphone.sh", "status").output)
        sb.usb_device(configs=4, current=0)
        self.assertIn("USB video:    the iPhone's USB is not configured", sb.run("iphone.sh", "status").output)
        sb.put("usbmuxd_version", "1.1.1-72-g3ded00c")
        sb.put("devmode", "false")
        out = sb.run("iphone.sh", "status").output
        self.assertIn(f"usbmuxd:        1.1.1-72-g3ded00c ({sb.fakebin}/usbmuxd)\n", out)
        self.assertIn("mode:           auto -> basic (Developer Mode is off;", out)
        out = sb.run("iphone.sh", "status", env={"IPHONE_MODE": "basic"}).output
        self.assertIn("mode:           basic (IPHONE_MODE=basic)", out)
        self.assertEqual(sb.calls("ios tunnel"), [])

    def test_status_asks_the_chosen_iphone_for_developer_mode(self):
        sb = self.sb
        sb.mux.devices = [(7, UDID_A), (9, UDID_B)]
        sb.mux.paired.add(UDID_B)
        sb.installed()
        out = sb.run("iphone.sh", "status", env={"IPHONE_UDID": UDID_B.lower(), "GO_IOS_UDID": UDID_A}).output
        self.assertIn("mode:           auto -> full", out)
        self.assertEqual([c.split(" [")[1] for c in sb.calls("ios devmode get")], [f"GO_IOS_UDID={UDID_B}]"])

    # a process anyone could have started, or one usbmuxd's own user runs (root here, when the tests run as root)
    def foreign(self, d):
        if os.geteuid() == 0:
            os.chown(d, 65534, 65534)

    def trusted(self, d):
        if os.geteuid() == 0:
            os.chown(d, 0, 0)
        else:
            self.sb.patch("iphone.sh", 'MUX_USERS="root usbmux usbmuxd"', f'MUX_USERS="{ME}"')

    def test_status_and_doctor_run_no_usbmuxd_someone_else_started(self):
        sb = self.sb
        sb.installed()
        evil = os.path.join(sb.root, "tmp", "usbmuxd")
        ran = os.path.join(sb.root, "ran")
        os.makedirs(os.path.dirname(evil))
        write_exec(evil, f'#!/bin/sh\ntouch "{ran}"\necho "usbmuxd 9.9"\n')
        d = sb.process([evil, "-f"])                     # comm "usbmuxd", but not usbmuxd's user
        self.foreign(d)
        fallback = f"usbmuxd:        1.1.1 ({sb.fakebin}/usbmuxd) - too old for USB video"
        self.assertIn(fallback, sb.run("iphone.sh", "status").output)
        self.assertIn(f"usbmuxd 1.1.1 ({sb.fakebin}/usbmuxd) is too old", sb.run("iphone.sh", "doctor").output)
        # usbmuxd's user runs it, from a program outside the system directories: named, never run
        self.trusted(d)
        self.assertIn(f"usbmuxd:        version unknown ({evil})\n", sb.run("iphone.sh", "status").output)
        self.assertIn(f"  -     usbmuxd (version unknown) ({evil})\n", sb.run("iphone.sh", "doctor").output)
        # ...from a program others may change: named as running, never run, and not taken for the distro's
        os.chmod(evil, 0o775)
        unchecked = f"running from {evil} (not a program only root can change, so not checked)"
        self.assertIn(f"usbmuxd:        {unchecked}\n", sb.run("iphone.sh", "status").output)
        self.assertIn(f"  -     usbmuxd {unchecked}\n", sb.run("iphone.sh", "doctor").output)
        self.assertFalse(os.path.exists(ran), "status or doctor ran the program")

    def test_status_finds_our_running_usbmuxd_by_its_cut_name(self):
        sb = self.sb
        sb.installed()
        sb.usb_device(configs=5, current=5)
        mux = os.path.join(sb.root, "system", "usbmuxd-iphone-control")   # started by hand: no systemd drop-in
        os.makedirs(os.path.dirname(mux))
        write_exec(mux, '#!/bin/sh\necho "usbmuxd 1.1.1-72-g3ded00c"\n')
        sb.patch("iphone.sh", "MUX_BIN=/usr/local/sbin/usbmuxd-iphone-control", "MUX_BIN=" + mux)
        self.trusted(sb.process([mux, "--user", "usbmux"]))       # its comm is "usbmuxd-iphone-"
        out = sb.run("iphone.sh", "status").output
        self.assertIn(f"usbmuxd:        1.1.1-72-g3ded00c ({mux}, from ./setup.sh --usbmuxd)\n", out)
        out = sb.run("iphone.sh", "doctor", env={"IPHONE_VIDEO": "usb"}).output
        self.assertIn(f"ok    usbmuxd 1.1.1-72-g3ded00c ({mux}, from ./setup.sh --usbmuxd)", out)
        self.assertNotIn("too old", out)

    def test_doctor_usb_video(self):
        sb = self.sb
        sb.installed()
        sb.usb_device(configs=5, current=5)
        out = sb.run("iphone.sh", "doctor").output
        self.assertIn("Mode: auto -> full (go-ios, WebDriverAgent and Developer Mode are in place)", out)
        self.assertIn("ok    USB video ready (the iPhone is in its capture USB configuration, 5 of 5)", out)
        self.assertIn("ok    GStreamer H.264 (appsrc h264parse a decoder videoconvert gtksink)", out)
        self.assertIn(f"warn  usbmuxd 1.1.1 ({sb.fakebin}/usbmuxd) is too old for USB video\n"
                      "        -> ./setup.sh --usbmuxd builds a newer one", out)
        sb.usb_device(configs=5, current=4)
        p = sb.run("iphone.sh", "doctor")
        self.assertIn("warn  usbmuxd keeps the iPhone in USB configuration 4, without the capture interface (usbmuxd "
                      "1.1.1 does)\n        -> ./setup.sh --usbmuxd", p.output)
        # basic mode without uxplay has no other picture
        sb.hide("uxplay")
        p = sb.run("iphone.sh", "doctor", env={"IPHONE_MODE": "basic"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAIL  usbmuxd keeps the iPhone in USB configuration 4", p.output)
        self.assertIn("FAIL  usbmuxd 1.1.1 (", p.output)
        sb.put("gst_missing", "h264parse\n")
        self.assertIn("warn  GStreamer lacks h264parse\n", sb.run("iphone.sh", "doctor").output)
        sb.put("gst_missing", "vah264dec\navdec_h264\nopenh264dec\nsouphttpsrc\n")
        out = sb.run("iphone.sh", "doctor", env={"IPHONE_DECODER": "nvh264dec", "IPHONE_VIDEO": "usb"}).output
        self.assertIn("ok    GStreamer H.264 (appsrc h264parse nvh264dec videoconvert gtksink)", out)
        self.assertIn("  -     GStreamer element souphttpsrc missing (only WebDriverAgent's picture needs it; "
                      "IPHONE_VIDEO=usb)", out)
        self.assertIn("FAIL  GStreamer element souphttpsrc missing", sb.run("iphone.sh", "doctor").output)
        sb.put("gst_missing", "nvh264dec\n")
        out = sb.run("iphone.sh", "doctor", env={"IPHONE_DECODER": "nvh264dec"}).output
        self.assertIn("GStreamer lacks the H.264 decoder IPHONE_DECODER=nvh264dec\n", out)
        out = sb.run("iphone.sh", "doctor", env={"IPHONE_VIDEO": "mjpeg"}).output
        self.assertIn("  -     not used (IPHONE_VIDEO=mjpeg)", out)

    def test_doctor_basic_mode_pieces(self):
        sb = self.sb
        sb.installed()
        out = sb.run("iphone.sh", "doctor").output                     # full mode: only notes
        self.assertIn("  -     bluetoothd does not answer (basic mode only)", out)
        self.assertIn("--auto-start=no", sb.calls("busctl")[0], "asking must not start bluetoothd")
        sb.put("bluez", "hci0 le\nhci1\n")
        self.assertIn("ok    Bluetooth LE adapter: hci0", sb.run("iphone.sh", "doctor").output)
        basic = {"IPHONE_MODE": "basic"}
        p = sb.run("iphone.sh", "doctor", env=dict(basic, BT_INDEX="1"))
        self.assertIn("FAIL  BT_INDEX=1: hci1 cannot advertise over LE (LE adapters: hci0)", p.output)
        sb.put("bluez", "hci1\n")
        out = sb.run("iphone.sh", "doctor", env=basic).output
        self.assertIn("FAIL  no Bluetooth adapter that can advertise over LE", out)
        os.unlink(os.path.join(sb.fake, "bluez"))
        self.assertIn("FAIL  bluetoothd does not answer", sb.run("iphone.sh", "doctor", env=basic).output)
        self.assertIn("ok    uxplay 1.73 (AirPlay picture)", sb.run("iphone.sh", "doctor").output)
        sb.tool("uxplay", "#!/bin/sh\necho 'UxPlay 1.71: An open-source AirPlay mirroring server.'\n")
        out = sb.run("iphone.sh", "doctor").output
        self.assertIn("warn  uxplay 1.71: the AirPlay picture needs 1.73 or newer", out)
        out = sb.run("iphone.sh", "doctor", env={"IPHONE_VIDEO": "airplay"}).output
        self.assertIn("FAIL  uxplay 1.71: the AirPlay picture needs 1.73 or newer", out)
        sb.hide("uxplay")
        self.assertIn("  -     uxplay not installed (optional", sb.run("iphone.sh", "doctor").output)

    def test_doctor_after_setup_basic(self):
        # nothing of full mode installed: basic mode, and full mode's pieces are no failure
        sb = self.sb
        for tool in ("ios", "plumesign"):
            os.unlink(os.path.join(sb.bin, tool))
        sb.usb_device(configs=5, current=5)
        sb.put("bluez", "hci0 le\n")
        p = sb.run("iphone.sh", "doctor")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("Mode: auto -> basic (go-ios is not installed; ./setup.sh adds full mode)", p.output)
        self.assertIn(f"warn  go-ios missing ({sb.bin}/ios) (full mode only)", p.output)
        self.assertIn("ok    Bluetooth LE adapter: hci0", p.output)
        self.assertIn("No problems found.", p.output)
        p = sb.run("iphone.sh", "doctor", env={"IPHONE_MODE": "basic"})
        self.assertIn(f"  -     go-ios missing ({sb.bin}/ios) (full mode only)", p.output)
        self.assertEqual(p.returncode, 0, p.output)

    def test_doctor_all_good(self):
        self.sb.installed()
        stamp_installed(self.sb, go_ios=False, ipa=False)
        p = self.sb.run("iphone.sh", "doctor")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("No problems found.", p.output)
        for line in ("ok    go-ios 1.3.2", "ok    plumesign (patched, up to date)", "ok    plumesign: Apple ID saved",
                     "ok    build/WebDriverAgent.ipa", "ok    usbmuxd", f"ok    iPhone {UDID_A}, iOS 26.5.2",
                     "ok    paired (this computer is trusted)", "ok    WebDriverAgent signature valid until",
                     "ok    CA bundle with Apple Root CA", "ok    PyGObject with GTK 3 + GStreamer",
                     "ok    GStreamer gtksink", "ok    desktop notifications"):
            self.assertIn(line, p.output)
        self.assertNotIn("FAIL", p.output)

    def test_doctor_reports_failures(self):
        self.sb.installed(ipa=False)
        self.sb.mux.paired = set()
        self.sb.put("nologin")
        p = self.sb.run("iphone.sh", "doctor")
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAIL  build/WebDriverAgent.ipa missing", p.output)
        self.assertIn("FAIL  not paired with this computer", p.output)
        self.assertIn("FAIL  plumesign: no Apple ID logged in", p.output)
        self.assertIn("warn  plumesign was built from other sources or patches", p.output)
        self.assertIn("3 problem(s) found.", p.output)

    def test_doctor_several_phones_and_old_signed_ipa(self):
        self.sb.installed(expires_in_days=-1)
        self.sb.mux.devices = [(7, UDID_A), (9, UDID_B)]
        open(os.path.join(self.sb.repo, "build", "WebDriverAgent-signed.ipa"), "w").close()
        p = self.sb.run("iphone.sh", "doctor")
        self.assertEqual(p.returncode, 1)
        self.assertIn("FAIL  several iPhones on USB", p.output)
        self.assertIn("FAIL  WebDriverAgent signature EXPIRED", p.output)
        self.assertIn("warn  build/WebDriverAgent-signed.ipa holds your team id", p.output)

    @unittest.skipUnless(HAVE["ss"], "needs ss")
    def test_doctor_flags_ports_taken_or_open_to_the_network(self):
        self.sb.installed()
        with socket.socket() as wide, socket.socket() as local:
            wide.bind(("0.0.0.0", self.sb.wda_port))
            wide.listen()
            local.bind(("127.0.0.1", self.sb.mjpeg_port))
            local.listen()
            p = self.sb.run("iphone.sh", "doctor")
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"FAIL  port {self.sb.wda_port} listens on 0.0.0.0:{self.sb.wda_port}", p.output)
        self.assertIn("reachable from the network", p.output)
        self.assertIn(f"FAIL  port {self.sb.mjpeg_port} is taken by", p.output)

    def test_doctor_launcher_entry(self):
        self.sb.installed()
        apps = os.path.join(self.sb.data_home, "applications")
        os.makedirs(apps)
        with open(os.path.join(apps, "iphone-control.desktop"), "w") as f:
            f.write('[Desktop Entry]\nExec="/nonexistent/iphone.sh"\n')
        self.assertIn("FAIL  launcher entry points to a missing /nonexistent/iphone.sh",
                      self.sb.run("iphone.sh", "doctor").output)

    def test_doctor_hides_personal_data_in_log_tails(self):
        self.sb.installed()
        os.makedirs(self.sb.rundir, exist_ok=True)
        with open(os.path.join(self.sb.rundir, "wda.log"), "w") as f:
            f.write(f"error for someone@example.com on {UDID_A[:8]}-0011223344556677 in "
                    f"com.example.xctrunner.{'ABCDE' + '12345'}\n")
        out = self.sb.run("iphone.sh", "doctor").output
        self.assertIn("last lines of wda.log", out)
        self.assertIn("<apple-id>", out)
        self.assertIn("<udid>", out)
        self.assertIn("xctrunner.<team id>", out)
        self.assertNotIn("someone@example.com", out)

    def test_run_dir_without_xdg_runtime_dir_is_private_and_not_in_tmp(self):
        self.sb.installed()
        run = os.path.join(self.sb.state, "run", "iphone-control")
        p = self.sb.run("iphone.sh", "status", env={"XDG_RUNTIME_DIR": None})
        self.assertEqual(p.returncode, 0, p.output)
        self.assertFalse(os.path.exists(run), "status only reads, it makes no dirs")
        p = self.sb.run("iphone.sh", "stop", env={"XDG_RUNTIME_DIR": None})
        self.assertEqual(p.returncode, 0, p.output)
        self.assertTrue(os.path.isdir(run))
        self.assertEqual(self.sb.mode(run), 0o700)


# ====================================================================== resign.sh
class Resign(ScriptTest):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.sb.repo, "build", "WebDriverAgent.ipa"), "wb") as f:
            f.write(b"PK fake")

    def test_usage_and_exit_codes(self):
        h = self.sb.run("resign.sh", "--help")
        self.assertEqual(h.returncode, 0)
        self.assertIn("--record", h.output)
        self.assertEqual(self.sb.run("resign.sh", "--bogus").returncode, 2)

    def test_signs_installs_and_records(self):
        sb = self.sb
        sb.write_config({"recent_apps": ["com.apple.mobilesafari"]})
        p = sb.run("resign.sh")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("installed com.example.WebDriverAgentRunner.xctrunner.<team id>", p.output)
        self.assertIn("signature valid until", p.output)
        self.assertNotIn(bundle_id(), p.output)
        sign = sb.calls("plumesign sign")[0]
        self.assertIn(f"-p {sb.repo}/build/WebDriverAgent.ipa", sign)
        self.assertIn("--apple-id --register-and-install --udid 7 ", sign)
        cfg = sb.config()
        self.assertEqual(cfg, {"recent_apps": ["com.apple.mobilesafari"], "wda_bundle": bundle_id(),
                               "signed_at": epoch(sb.now),
                               "expires_at": epoch(sb.now + datetime.timedelta(days=7))})
        self.assertEqual(sb.mode(sb.state), 0o700)
        self.assertEqual(sb.mode(os.path.join(sb.state, "config.json")) & 0o077, 0)
        self.assertFalse(os.path.exists(os.path.join(sb.state, "WebDriverAgent-signed.ipa")))
        self.assertTrue(os.path.exists(os.path.join(sb.state, "resign.log")))
        self.assertEqual(sorted(os.listdir(os.path.join(sb.repo, "build"))), ["WebDriverAgent.ipa"])

    def test_not_logged_in(self):
        self.sb.put("nologin")
        p = self.sb.run("resign.sh")
        self.assertEqual(p.returncode, 3)
        self.assertIn("Not logged in to an Apple ID", p.output)
        self.assertEqual(self.sb.calls("plumesign sign"), [])

    def test_failure_leaves_config_alone(self):
        before = self.sb.installed()
        self.sb.put("plume_plan", "fail:Developer account limit reached")
        p = self.sb.run("resign.sh")
        self.assertEqual(p.returncode, 1)
        self.assertIn("Developer account limit reached", p.output)
        self.assertIn("signing/installing failed", p.output)
        self.assertEqual(self.sb.config(), before)
        self.assertFalse(os.path.exists(os.path.join(self.sb.state, "WebDriverAgent-signed.ipa")))

    def test_locked_phone(self):
        self.sb.put("plume_plan", "locked")
        p = self.sb.run("resign.sh")
        self.assertEqual(p.returncode, 4)
        self.assertIn("The iPhone is locked", p.output)

    def test_session_expired(self):
        self.sb.put("plume_plan", "session")
        p = self.sb.run("resign.sh")
        self.assertEqual(p.returncode, 3)
        self.assertIn("Apple ID session expired", p.output)

    def test_output_hides_the_apple_id(self):
        self.sb.put("plume_plan", "fail:invalid team for someone@example.com")
        p = self.sb.run("resign.sh")
        self.assertNotIn("someone@example.com", p.output)
        self.assertIn("<apple-id>", p.output)

    def test_no_phone_and_several_phones(self):
        self.sb.mux.devices = []
        p = self.sb.run("resign.sh")
        self.assertEqual(p.returncode, 1)
        self.assertIn("No iPhone on USB", p.output)
        self.sb.mux.devices = [(7, UDID_A), (9, UDID_B)]
        p = self.sb.run("resign.sh")
        self.assertEqual(p.returncode, 1)
        self.assertIn("Several iPhones are plugged in", p.output)
        p = self.sb.run("resign.sh", env={"IPHONE_UDID": UDID_B.lower()})
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("--udid 9 ", self.sb.calls("plumesign sign")[-1])
        p = self.sb.run("resign.sh", env={"IPHONE_UDID": "nope"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("iPhone nope is not on USB", p.output)

    def test_missing_ipa_or_plumesign(self):
        os.unlink(os.path.join(self.sb.repo, "build", "WebDriverAgent.ipa"))
        self.assertIn("run ./setup.sh first", self.sb.run("resign.sh").output)
        open(os.path.join(self.sb.repo, "build", "WebDriverAgent.ipa"), "w").close()
        os.unlink(os.path.join(self.sb.bin, "plumesign"))
        p = self.sb.run("resign.sh")
        self.assertEqual(p.returncode, 1)
        self.assertIn("plumesign missing", p.output)

    def test_record_needs_no_phone_or_apple_id(self):
        sb = self.sb
        sb.mux.devices = []
        sb.put("nologin")
        old = os.path.join(sb.repo, "build", "WebDriverAgent-signed.ipa")
        make_signed_ipa(old, created=sb.now, expires=sb.now + datetime.timedelta(days=365))
        p = sb.run("resign.sh", "--record")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(sb.config()["expires_at"], epoch(sb.now + datetime.timedelta(days=365)))
        self.assertEqual(sb.calls("plumesign"), [])
        self.assertEqual(sb.run("resign.sh", "--record").returncode, 1)      # nothing left to record

    def test_record_rejects_a_broken_ipa(self):
        old = os.path.join(self.sb.repo, "build", "WebDriverAgent-signed.ipa")
        import zipfile
        with zipfile.ZipFile(old, "w") as z:
            z.writestr("Payload/readme.txt", "no app")
        p = self.sb.run("resign.sh", "--record")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(os.path.exists(old))

    @unittest.skipUnless(HAVE["flock"], "needs flock")
    def test_one_signing_at_a_time(self):
        os.makedirs(self.sb.state, exist_ok=True)
        lock = os.path.join(self.sb.state, "resign.lock")
        holder = subprocess.Popen(["flock", lock, "sleep", "30"])
        try:
            time.sleep(0.3)
            p = self.sb.run("resign.sh")
            self.assertEqual(p.returncode, 1)
            self.assertIn("already running", p.output)
        finally:
            holder.kill()
            holder.wait()


# ====================================================================== uninstall.sh
class Uninstall(ScriptTest):
    def test_usage(self):
        self.assertEqual(self.sb.run("uninstall.sh", "--help").returncode, 0)
        self.assertEqual(self.sb.run("uninstall.sh", "--bogus").returncode, 2)

    def test_yes_without_a_terminal_keeps_secrets(self):
        sb = self.sb
        sb.installed()
        apps = os.path.join(sb.data_home, "applications")
        os.makedirs(apps)
        desktop = os.path.join(apps, "iphone-control.desktop")
        open(desktop, "w").close()
        with open(os.path.join(sb.state, "plumesign.stamp"), "w") as f:
            f.write("x\n")
        cache = os.path.join(sb.cache_home, "iphone-control")
        os.makedirs(cache)
        plume = os.path.join(sb.config_home, "PlumeImpactor")
        os.makedirs(plume)
        p = sb.run("uninstall.sh", "--yes")
        self.assertEqual(p.returncode, 0, p.output)
        for gone in (desktop, os.path.join(sb.bin, "ios"), os.path.join(sb.bin, "plumesign"), cache,
                     os.path.join(sb.repo, "build")):
            self.assertFalse(os.path.exists(gone), gone)
        self.assertTrue(os.path.isdir(plume), "the Apple ID session is only removed when the user says y")
        self.assertTrue(os.path.isdir(sb.state), "the pairing key is only removed when the user says y")
        self.assertIn("(no terminal)", p.output)
        self.assertEqual(sb.calls("plumesign account logout"), [])
        self.assertTrue(os.path.exists(sb.repo + "/iphone.sh"))

    def test_keeps_programs_it_did_not_install(self):
        sb = self.sb
        sb.put("version", "v1.2.0")
        p = sb.run("uninstall.sh", "--yes")
        self.assertIn("keeping", p.output)
        self.assertTrue(os.path.exists(os.path.join(sb.bin, "ios")))
        self.assertTrue(os.path.exists(os.path.join(sb.bin, "plumesign")))   # no setup.sh stamp

    def answer(self, *args, answers=""):
        """uninstall.sh on a terminal, typing ANSWERS."""
        master, slave = pty.openpty()
        try:
            os.write(master, answers.encode())
            return self.sb.run("uninstall.sh", *args, stdin=slave)
        finally:
            os.close(slave)
            os.close(master)

    def test_removes_the_capture_files_and_asks_for_the_usbmuxd_override(self):
        sb = self.sb
        sb.installed()
        os.makedirs(sb.rundir, exist_ok=True)
        for name in ("cap.log", "cap.prev.log", "cap.sock", "cap.status", "uxplay.log", "mode"):
            open(os.path.join(sb.rundir, name), "w").close()
        system = os.path.join(sb.root, "system")          # stands in for /usr/local/sbin, /etc and /run
        mux_bin = os.path.join(system, "sbin", "usbmuxd-iphone-control")
        dropin = os.path.join(system, "usbmuxd.service.d", "iphone-control.conf")
        for path in (mux_bin, dropin):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        os.makedirs(os.path.join(system, "run-systemd"))
        sb.patch("uninstall.sh", "MUX_BIN=/usr/local/sbin/usbmuxd-iphone-control", "MUX_BIN=" + mux_bin)
        sb.patch("uninstall.sh", "MUX_DROPIN=/etc/systemd/system/usbmuxd.service.d/iphone-control.conf",
                 "MUX_DROPIN=" + dropin)
        sb.patch("uninstall.sh", "SYSTEMD_RUN=/run/systemd/system",
                 "SYSTEMD_RUN=" + os.path.join(system, "run-systemd"))
        # sudo runs it as this user, on the sandbox's paths
        sb.tool("sudo", f'#!/bin/sh\necho "sudo $*" >> "{sb.fake}/calls.log"\nexec "$@"\n')
        sb.tool("systemctl", f'#!/bin/sh\necho "systemctl $*" >> "{sb.fake}/calls.log"\n')
        p = sb.run("uninstall.sh", "--yes")                # no terminal: the system part stays
        self.assertIn("Remove them with sudo (usbmuxd restarts as the distro's version)? [y/N] n (no terminal)",
                      p.output)
        self.assertTrue(os.path.exists(mux_bin) and os.path.exists(dropin))
        self.assertEqual(sb.calls("sudo"), [])
        for name in ("cap.log", "cap.prev.log", "cap.sock", "cap.status", "uxplay.log", "mode"):
            self.assertFalse(os.path.exists(os.path.join(sb.rundir, name)), name)
        p = self.answer("--yes", answers="y\nn\n")          # yes to usbmuxd, no to the machine state
        self.assertEqual(p.returncode, 0, p.output)
        self.assertFalse(os.path.exists(mux_bin) or os.path.exists(dropin), p.output)
        self.assertFalse(os.path.exists(os.path.dirname(dropin)))
        if os.geteuid() != 0:                             # root (CI containers) needs no sudo
            self.assertEqual(sb.calls("sudo ")[0], f"sudo rm -f -- {dropin} {mux_bin}")
        self.assertEqual(sb.calls("systemctl"), ["systemctl daemon-reload", "systemctl restart usbmuxd"])
        self.assertTrue(os.path.isdir(sb.state))
        self.assertIn("== usbmuxd for USB video (setup.sh --usbmuxd)\nnone installed", sb.run("uninstall.sh").output)

    def test_removes_a_usbmuxd_override_it_cannot_see(self):
        sb = self.sb
        system = os.path.join(sb.root, "system")
        mux_bin = os.path.join(system, "sbin", "usbmuxd-iphone-control")
        dropin = os.path.join(system, "usbmuxd.service.d", "iphone-control.conf")
        for path in (mux_bin, dropin):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        hidden = os.path.dirname(dropin)
        os.chmod(hidden, 0)                               # as root made it under a umask of 077: nobody else may look
        self.addCleanup(lambda: os.path.isdir(hidden) and os.chmod(hidden, 0o755))
        sb.patch("uninstall.sh", "MUX_BIN=/usr/local/sbin/usbmuxd-iphone-control", "MUX_BIN=" + mux_bin)
        sb.patch("uninstall.sh", "MUX_DROPIN=/etc/systemd/system/usbmuxd.service.d/iphone-control.conf",
                 "MUX_DROPIN=" + dropin)
        sb.patch("uninstall.sh", "SYSTEMD_RUN=/run/systemd/system", "SYSTEMD_RUN=" + os.path.join(system, "none"))
        # sudo runs it as this user, who may look once "root" does
        sb.tool("sudo", f'#!/bin/sh\necho "sudo $*" >> "{sb.fake}/calls.log"\nchmod 755 "{hidden}"\nexec "$@"\n')
        p = self.answer("--yes", answers="y\nn\n")          # yes to usbmuxd, no to the machine state
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn(f"\n  {dropin}\n  {mux_bin}\n", p.output)
        self.assertFalse(os.path.exists(mux_bin) or os.path.exists(dropin), p.output)

    def test_without_yes_nothing_is_removed(self):
        sb = self.sb
        sb.installed()
        p = sb.run("uninstall.sh")
        self.assertEqual(p.returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(sb.bin, "ios")))
        self.assertTrue(os.path.exists(os.path.join(sb.repo, "build", "WebDriverAgent.ipa")))


# ====================================================================== certs/ca-bundle.sh
class CaBundle(ScriptTest):
    def ca(self, *args, env=None):
        e = dict(self.sb.env, **(env or {}))
        e = {k: v for k, v in e.items() if v is not None}
        return subprocess.run(["bash", os.path.join(self.sb.repo, "certs", "ca-bundle.sh")] + list(args), env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)

    def test_builds_and_reuses_the_bundle(self):
        out = os.path.join(self.sb.root, "out", "bundle.pem")
        p = self.ca(out)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, out), p.stderr)
        with open(out) as f:
            text = f.read()
        self.assertEqual(text.splitlines()[0], f"# system CAs from {self.sb.ca_bundle} + Apple Root CA (certs/ca-bundle.sh)")
        self.assertIn("ZmFrZSBzeXN0ZW0gQ0E=", text)
        self.assertEqual(text.count("BEGIN CERTIFICATE"), 2)
        mtime = os.stat(out).st_mtime_ns
        time.sleep(0.05)
        self.ca(out)
        self.assertEqual(os.stat(out).st_mtime_ns, mtime, "an up-to-date bundle is not rewritten")
        future = time.time() + 5
        os.utime(self.sb.ca_bundle, (future, future))       # the system bundle changed
        self.ca(out)
        self.assertNotEqual(os.stat(out).st_mtime_ns, mtime)

    def test_default_output_and_source(self):
        p = self.ca()
        self.assertEqual(p.stdout.strip(), os.path.join(self.sb.state, "ca-bundle-with-apple.pem"))
        self.assertEqual(self.ca("--source").stdout.strip(), self.sb.ca_bundle)

    def test_autodetects_a_system_bundle(self):
        p = self.ca("--source", env={"CA_BUNDLE": None})
        known = ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
                 "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", "/etc/pki/tls/cacert.pem", "/etc/ssl/cert.pem"]
        present = [k for k in known if os.path.isfile(k) and os.path.getsize(k)]
        if present:
            self.assertEqual(p.stdout.strip(), present[0])
        else:
            self.assertEqual(p.returncode, 1)
            self.assertIn("no system CA bundle found", p.stderr)

    def test_bad_ca_bundle(self):
        p = self.ca(env={"CA_BUNDLE": "/nonexistent/ca.pem"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("is not a readable file", p.stderr)

    def test_tampered_apple_root_is_refused(self):
        with open(os.path.join(self.sb.repo, "certs", "apple-root.pem"), "a") as f:
            f.write("\n")
        p = self.ca(os.path.join(self.sb.root, "b.pem"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("checksum mismatch", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.sb.root, "b.pem")))


if __name__ == "__main__":
    unittest.main()
