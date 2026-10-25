#!/usr/bin/env python3
"""Open the iPhone window. Started by iphone.sh (in full mode once WebDriverAgent is up).

Environment: IPHONE_MODE (full = input through WebDriverAgent, the default; basic = the laptop as a Bluetooth LE
mouse and keyboard, AssistiveTouch on, no WebDriverAgent), IPHONE_VIDEO (mjpeg = WebDriverAgent's stream, the
default in full mode; usb = the USB screen capture at IPHONE_CAPTURE_SOCKET, waited for while its socket is not
there, and in full mode replaced by WebDriverAgent's stream after 15 s without it; airplay = AirPlay mirroring through
UxPlay 1.73+, which the window starts, named IPHONE_AIRPLAY_NAME or this machine's hostname; auto = usb while the
capture runs, else mjpeg or airplay), IPHONE_DECODER (the GStreamer H.264 decoder; default vah264dec,
avdec_h264, then openh264dec),
IPHONE_WDA_URL (http://127.0.0.1:8100), IPHONE_MJPEG_URL (http://127.0.0.1:9100),
IPHONE_FPS (30), IPHONE_QUALITY (60, JPEG; 10 when IPHONE_VIDEO=usb, where nobody sees the stream), IPHONE_SCALE
(100 = % of the phone's resolution; below 100 WebDriverAgent re-encodes every frame and iOS may stop it for using
too much CPU), IPHONE_FIX_ORIENTATION (1; 0 = never rotate the picture), IPHONE_FAST_INPUT (1 = quick touches:
WebDriverAgent's snapshotMaxDepth 1 and animationCoolOffTimeout 0 while the window runs; 0 = its defaults),
IPHONE_DOUBLE_CLICK_MS (0 = a click taps at once; above 0 a click waits this long for a double-click and the pair
is sent as one double tap), IPHONE_HID_NAME and BT_INDEX (basic mode: the Bluetooth name, hciN),
IPHONE_SCREENSHOT_DIR (Pictures/iPhone),
IPHONE_NOTIFY (fatal errors as a desktop notification: 1 always, 0 never; default only without a
terminal and when not started by iphone.sh, which sets IPHONE_FROM_SCRIPT=1 and reports them itself).
"""
import os
import sys

if sys.version_info < (3, 9):
    sys.exit("iPhone Control needs Python 3.9 or newer")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import gi
except ImportError:
    sys.exit("PyGObject is missing (Debian/Ubuntu python3-gi, Fedora python3-gobject, "
             "openSUSE python3-gobject-Gdk, Arch python-gobject) - run ./setup.sh")

try:
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gst", "1.0")
except ValueError as e:
    sys.exit(f"{e} (GTK 3 / GStreamer GObject bindings) - run ./setup.sh")

from iphone_control.window import main  # noqa: E402  (sets the program name before GTK starts)

if __name__ == "__main__":
    main()
