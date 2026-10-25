#!/usr/bin/env python3
"""Live check against a running WebDriverAgent (./iphone.sh started, the iPhone unlocked).

  python3 tests/live_test.py --i-am-not-using-the-phone [--url http://127.0.0.1:8100] [--out DIR]

Only system gestures on the home screen, each checked with screenshots (and the accessibility tree
where it helps): Home, swipe to the next home page and back, Spotlight open + type + delete + close,
Control Centre open/close, App Switcher open/close, a screenshot's size, volume up then down.
It never taps: no app is opened, nothing is typed outside Spotlight's search field, Return is never
sent. It ends on the home screen. Exit status: 0 passed, 1 a step failed, 2 refused (no flag),
3 not ready (WebDriverAgent unreachable, or the iPhone is locked).
Not part of the unit tests (the file name does not start with test_); tests/test_live_test.py runs it
against a simulated phone.
"""
import argparse
import base64
import os
import struct
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from iphone_control import geometry  # noqa: E402
from iphone_control.wda import WDA  # noqa: E402

FLAG = "--i-am-not-using-the-phone"
SPRINGBOARD = "com.apple.springboard"
STATUS_BAR = 0.08        # top part of the screen left out of comparisons (clock, battery, Dynamic Island)
CELLS = (40, 87)         # screenshots are compared on a grid this size (columns, rows)
PIXEL_DIFF = 32          # a grid cell differs when a colour channel moved more than this (0-255)
SAME, CHANGED = 0.03, 0.10   # fraction of differing cells: at most SAME = same screen, at least CHANGED = new
PROBE_TEXT = "zq"        # typed into Spotlight's search field, then deleted


# ---------------------------------------------------------------- screenshots
# iOS 26 redraws the home screen slightly differently each time (Liquid Glass highlights), so screens are
# compared on a coarse grid with a tolerance, never byte for byte.
def _gdkpixbuf():
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        return GdkPixbuf
    except (ImportError, ValueError):
        return None


GDK = _gdkpixbuf()       # fast path; None = the stdlib decoder below (tests force it)


def png_size(png):
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return struct.unpack(">II", png[16:24])


def png_decode(png):
    """(width, height, bytes per pixel, colour channels, [unfiltered rows]) of an 8-bit PNG, stdlib only."""
    width, height = png_size(png)
    pos, idat = 8, []
    while pos < len(png):
        n, kind = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            depth, ctype, interlace = data[8], data[9], data[12]
            if depth != 8 or interlace:
                raise ValueError("unsupported PNG (needs 8 bits, not interlaced)")
            bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
            colours = 3 if ctype in (2, 6) else 1
        elif kind == b"IDAT":
            idat.append(data)
        elif kind == b"IEND":
            break
        pos += 12 + n
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    rows, prev = [], bytearray(stride)
    for y in range(height):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:                                                   # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:                                                 # Up
            line = bytearray((a + b) & 255 for a, b in zip(line, prev))
        elif f == 3:                                                 # Average
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif f == 4:                                                 # Paeth
            for i in range(stride):
                a, b, c = (line[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif f:
            raise ValueError(f"bad PNG filter {f}")
        rows.append(line)
        prev = line
    return width, height, bpp, colours, rows


def grid(png):
    """The screenshot shrunk to CELLS: a list of rows of colour tuples."""
    cols, lines = CELLS
    if GDK is not None:
        loader = GDK.PixbufLoader()
        loader.write(png)
        loader.close()
        small = loader.get_pixbuf().scale_simple(cols, lines, GDK.InterpType.BILINEAR)
        n, rs, px = small.get_n_channels(), small.get_rowstride(), small.get_pixels()
        return [[tuple(px[y * rs + x * n:y * rs + x * n + 3]) for x in range(cols)] for y in range(lines)]
    width, height, bpp, colours, rows = png_decode(png)                # one pixel per cell (no averaging)
    out = []
    for cy in range(lines):
        row = rows[min(height - 1, int((cy + 0.5) * height / lines))]
        xs = [min(width - 1, int((cx + 0.5) * width / cols)) * bpp for cx in range(cols)]
        out.append([tuple(row[x:x + colours]) for x in xs])
    return out


def difference(a, b):
    """Fraction of grid cells below the status bar that differ between two screenshots (0 = same)."""
    if png_size(a) != png_size(b):
        return 1.0
    ga, gb = grid(a), grid(b)
    top = int(CELLS[1] * STATUS_BAR)
    cells = [(pa, pb) for ra, rb in zip(ga[top:], gb[top:]) for pa, pb in zip(ra, rb)]
    return sum(max(abs(x - y) for x, y in zip(pa, pb)) > PIXEL_DIFF for pa, pb in cells) / max(len(cells), 1)


# ---------------------------------------------------------------- the run
class Live:
    def __init__(self, url, out, delay=1.0, log=print):
        self.wda = WDA(url, log=log)
        self.out, self.delay, self.say = out, delay, log
        self.results = []
        self.shots = 0
        self.size = None

    # ---- WDA ----------------------------------------------------------
    def call(self, method, path, body=None, timeout=30):
        return self.wda.call(method, path, body, timeout).get("value")

    def wait(self, seconds):
        if seconds and self.delay:
            time.sleep(seconds * self.delay)

    def shot(self, name):
        png = base64.b64decode(self.call("GET", "/screenshot"))
        self.shots += 1
        with open(os.path.join(self.out, f"{self.shots:02d}-{name}.png"), "wb") as f:
            f.write(png)
        return png

    def home(self):
        # the button itself (/wda/homescreen does nothing while Spotlight or Control Centre is open); the wait
        # also keeps two presses from counting as a double press
        self.call("POST", "/session/{sid}/wda/pressButton", {"name": "home"})
        self.wait(1.5)

    def front_app(self):
        try:
            return (self.call("GET", "/wda/activeAppInfo") or {}).get("bundleId")
        except OSError:
            return None

    def swipe(self, x0, y0, x1, y1, ms=250):
        """A quick one-finger swipe: down, move at once, up."""
        self.gesture(geometry.gesture([(x0, y0, 0), (x1, y1, ms)]))

    def gesture(self, w3c):
        """Send W3C actions, but only swipes: every finger must move far, without holding first.
        A tap or a long press could open or change something, so those are refused."""
        for finger in w3c["actions"]:
            acts = finger["actions"]
            types = [a["type"] for a in acts]
            if types[0] != "pointerMove" or "pointerDown" not in types:
                raise ValueError("refusing a gesture without a start point")
            after = acts[types.index("pointerDown") + 1:]
            held = 0
            for a in after:
                if a["type"] != "pause":
                    break
                held += a["duration"]
            moves = [a for a in after if a["type"] == "pointerMove"]
            if not moves or held > 300:
                raise ValueError("refusing a gesture that is not a swipe")
            far = max(abs(m["x"] - acts[0]["x"]) + abs(m["y"] - acts[0]["y"]) for m in moves)
            if far < 2 * geometry.TAP_MAX_MOVE:
                raise ValueError("refusing a gesture that could be a tap")
        self.call("POST", "/session/{sid}/actions", w3c)

    def search_field(self):
        try:
            found = self.call("POST", "/session/{sid}/elements",
                              {"using": "class name", "value": "XCUIElementTypeSearchField"}) or []
            if found:
                el = found[0]
                return el.get("ELEMENT") or next(iter(el.values()))
        except OSError:
            pass
        return None

    def field_value(self, el):
        try:
            return self.call("GET", f"/session/{{sid}}/element/{el}/attribute/value") or ""
        except OSError:
            return None

    # ---- reporting ----------------------------------------------------
    def result(self, status, name, detail):
        self.results.append((status, name))
        self.say(f"{status:4}  {name}: {detail}")

    def check_same(self, name, ref, png, soft=False):
        d = difference(ref, png)
        ok = d <= SAME
        self.result("PASS" if ok else ("WARN" if soft else "FAIL"), name,
                    f"{d:.0%} of the screen differs (same screen: <= {SAME:.0%})")
        return ok

    def check_changed(self, name, ref, png, soft=False):
        d = difference(ref, png)
        ok = d >= CHANGED
        self.result("PASS" if ok else ("WARN" if soft else "FAIL"), name,
                    f"{d:.0%} of the screen differs (changed: >= {CHANGED:.0%})")
        return ok

    # ---- steps --------------------------------------------------------
    def preflight(self):
        st = self.wda.status(timeout=5)
        if self.call("GET", "/wda/locked"):
            raise RuntimeError("the iPhone is locked - unlock it first (this test never unlocks it)")
        self.wda.new_session()
        size = self.call("GET", "/session/{sid}/window/size")
        self.size = (size["width"], size["height"])
        if self.size[0] > self.size[1]:
            raise RuntimeError("the screen is in landscape - turn the iPhone upright first")
        self.say(f"WebDriverAgent ready: iOS {(st.get('os') or {}).get('version', '?')}, screen {self.size[0]}x"
                 f"{self.size[1]} points; screenshots in {self.out}")

    def step_home(self):
        self.home()
        self.home()                                   # a second Home goes back to the first page
        app = self.front_app()
        ok = app in (SPRINGBOARD, None)
        self.result("PASS" if ok else "FAIL", "home screen", f"front app {app or 'unknown'}")
        self.ref = self.shot("home")
        return ok

    def step_pages(self):
        w, h = self.size
        self.swipe(w * 0.85, h * 0.5, w * 0.15, h * 0.5)          # finger to the left: next page
        self.wait(1)
        page2 = self.shot("next-page")
        if not self.check_changed("swipe to the next home page", self.ref, page2, soft=True):
            self.result("WARN", "home pages", "no second home page? swiping back skipped")
            self.home()
            return
        self.swipe(w * 0.15, h * 0.5, w * 0.85, h * 0.5)          # and back
        self.wait(1)
        self.check_same("swipe back to the first page", self.ref, self.shot("first-page"))
        self.home()

    def step_spotlight(self):
        w, h = self.size
        self.swipe(w * 0.5, h * 0.3, w * 0.5, h * 0.6)             # down from the middle (not the top edge)
        self.wait(1.5)
        opened = self.shot("spotlight")
        if not self.check_changed("Spotlight opens", self.ref, opened):
            return self.home()
        el = self.search_field()
        self.call("POST", "/session/{sid}/wda/keys", {"value": list(PROBE_TEXT)})
        self.wait(1)
        typed = self.shot("spotlight-typed")
        self.check_changed("typing shows in Spotlight", opened, typed, soft=el is not None)
        if el is not None:
            v = self.field_value(el)
            self.result("PASS" if v is not None and PROBE_TEXT in v else "FAIL", "search field has the text", repr(v))
        self.call("POST", "/session/{sid}/wda/keys", {"value": ["\b"] * len(PROBE_TEXT)})
        self.wait(1)
        deleted = self.shot("spotlight-deleted")
        if el is not None:
            v = self.field_value(el)
            self.result("PASS" if v is not None and PROBE_TEXT not in v else "FAIL", "Backspace deleted the text",
                        repr(v))
        self.check_changed("deleting changes Spotlight back", typed, deleted, soft=True)
        self.home()
        self.check_same("Spotlight closes", self.ref, self.shot("spotlight-closed"))

    def step_edge(self, kind, label):
        self.gesture(geometry.edge_swipe(kind, self.size))
        self.wait(1.5)
        # an App Switcher with no apps in it dims the screen and closes again by itself before the shot
        self.check_changed(f"{label} opens", self.ref, self.shot(kind), soft=kind == "app_switcher")
        self.home()
        self.check_same(f"{label} closes", self.ref, self.shot(kind + "-closed"))

    def step_screenshot(self):
        png = self.shot("screenshot")
        w, h = png_size(png)
        scale = w / self.size[0]
        ok = abs(h / self.size[1] - scale) < 0.02 and round(scale) in (1, 2, 3)
        self.result("PASS" if ok else "FAIL", "screenshot", f"{w}x{h} px = {scale:.2f}x the screen in points")

    def step_volume(self):
        self.call("POST", "/session/{sid}/wda/pressButton", {"name": "volumeUp"})
        self.wait(0.3)
        hud = self.shot("volume-up")
        self.call("POST", "/session/{sid}/wda/pressButton", {"name": "volumeDown"})
        self.result("PASS", "volume up, then down", "both buttons accepted")
        d = difference(self.ref, hud)                             # iOS 26's indicator is a small pill at the side
        self.result("PASS" if d > 0 else "WARN", "volume indicator shown", f"{d:.1%} of the screen differs (> 0)")
        self.wait(3)                                              # the indicator fades
        self.check_same("volume indicator gone", self.ref, self.shot("volume-done"), soft=True)

    def run(self):
        try:
            self.preflight()
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as e:
            self.say(f"not ready: {e}")
            return 3
        try:
            if not self.step_home():
                self.say("not on the home screen - stopping")
                return 1
            self.step_pages()
            self.step_spotlight()
            self.step_edge("control_centre", "Control Centre")
            self.step_edge("app_switcher", "App Switcher")
            self.step_screenshot()
            self.step_volume()
            self.home()
            if difference(self.ref, self.shot("end")) > SAME:
                self.wait(6)                                      # a notification banner goes away by itself
            self.check_same("back on the home screen", self.ref, self.shot("end"))
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.result("FAIL", "error", repr(e))
            try:
                self.home()
            except OSError:
                pass
        finally:
            try:
                self.call("DELETE", "/session/{sid}")
            except OSError:
                pass
        fails = sum(s == "FAIL" for s, _ in self.results)
        warns = sum(s == "WARN" for s, _ in self.results)
        self.say(f"\n{len(self.results)} checks: {fails} failed, {warns} warnings. Screenshots: {self.out}")
        return 1 if fails else 0


def main(argv=None, log=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(FLAG, dest="ok", action="store_true",
                    help="required: you are not using the iPhone and it may be driven for about a minute")
    ap.add_argument("--url", default=os.environ.get("IPHONE_WDA_URL", "http://127.0.0.1:8100"))
    ap.add_argument("--out", help="folder for the screenshots (default: a new temp folder)")
    ap.add_argument("--delay-scale", type=float, default=1.0, help=argparse.SUPPRESS)   # tests: 0
    a = ap.parse_args(argv)
    if not a.ok:
        log(f"Refusing to run: this drives the real iPhone (Home, swipes, Spotlight, Control Centre, App Switcher,\n"
            f"volume). Close what you are doing on it, unlock it, leave it alone, then run again with {FLAG}.")
        return 2
    out = a.out or tempfile.mkdtemp(prefix="iphone-live-")
    os.makedirs(out, mode=0o700, exist_ok=True)
    return Live(a.url, out, a.delay_scale, log).run()


if __name__ == "__main__":
    sys.exit(main())
