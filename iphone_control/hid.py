"""Pure parts of the Bluetooth LE mouse + keyboard (blehid.py): the report map, report packing, the US keymap,
and the scheduler that turns an action into reports and waits. No GLib, no D-Bus - unit-tested in
tests/test_hid.py.

Measured on an iPhone 15 Pro (iOS 26.5.2, Oct 2026) with AssistiveTouch on:
- the absolute pointer (report 4: X and Y 0..32767 across the screen) puts the pointer exactly where asked. With
  two mouse collections iOS ignores the relative report's buttons, so clicks and drags use report 4's buttons;
- a press lands where the pointer was BEFORE the report: move, wait >= 50 ms (20 ms failed 1 in 2), then press
  at the same point. A tap reached the screen 280-440 ms after it was sent;
- a live drag works: move to the start, wait, press, moves with the button held (one every ~12 ms), release;
- iOS ignores an absolute report equal to the one before it, so a repeat is nudged by one unit;
- the wheel does not scroll: scrolling is a short drag (swipe());
- Home is consumer usage 0x40 (0x223 does nothing), and Home twice ~120 ms apart opens the App Switcher;
  Search 0x221 opens Spotlight. The Globe key (consumer 0x29D) held over a key: fn+C opens Control Centre,
  fn+N Notification Centre (fn+Up does not open the App Switcher, Cmd+Space does nothing).
"""
import collections
import struct

PACE_MS = 8            # at most one report per tick: about one per BLE connection event (hid_ble.py)
SETTLE_MS = 60         # the pointer rests this long at a point before the press there
HOLD_MS = 50           # a tap's press to release
HOME_TWICE_MS = 120    # Home press to Home press: the App Switcher
DRAG_STEP_MS = 12      # a swipe moves the pointer this often

MOUSE, KEYBOARD, CONSUMER, POINTER = 1, 2, 3, 4      # report ids
SIZES = {MOUSE: 4, KEYBOARD: 8, CONSUMER: 2, POINTER: 5}
ABS_MAX = 32767
WAIT, MOVE = "wait", "move"                           # step kinds besides a report id (see Scheduler)

REPORT_MAP = bytes.fromhex(
    # mouse, report id 1: 3 buttons, X, Y, wheel (int8, relative). Unused, but report 4 was measured next to it
    "05010902a10185010901a100050919012903150025019503750181029501750581"
    "030501093009310938158125" "7f750895038106c0c0"
    # keyboard, report id 2: modifiers, reserved, 6 keys
    + "05010906a1018502050719e029e71500250175019508810295017508810195067508150025650507190029658100c0"
    # consumer control, report id 3: one 16-bit usage
    + "050c0901a1018503150026ff0319002aff03751095018100c0"
    # absolute pointer, report id 4: 3 buttons, X, Y uint16 0..32767
    + "05010902a10185040901a1000509190129031500250195037501810295017505810105010930093116000026ff7f751095028102c0c0"
)

# consumer usages
HOME, SEARCH, VOLUME_UP, VOLUME_DOWN, LOCK, GLOBE = 0x40, 0x221, 0xE9, 0xEA, 0x30, 0x29D
# keyboard modifier bits (GUI is Command on iOS) and the keys of the Globe chords
CTRL, SHIFT, ALT, GUI = 0x01, 0x02, 0x04, 0x08
KEY_C, KEY_N = 0x06, 0x11

KEYMAP = {}            # character -> (keyboard usage, modifiers), US layout
for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEYMAP[c] = (0x04 + i, 0)
    KEYMAP[c.upper()] = (0x04 + i, SHIFT)
for i, c in enumerate("1234567890"):
    KEYMAP[c] = (0x1E + i, 0)
for i, c in enumerate("!@#$%^&*()"):
    KEYMAP[c] = (0x1E + i, SHIFT)
for c, code, mod in [("\n", 0x28, 0), ("\b", 0x2A, 0), ("\t", 0x2B, 0), (" ", 0x2C, 0), ("-", 0x2D, 0),
                     ("_", 0x2D, 2), ("=", 0x2E, 0), ("+", 0x2E, 2), ("[", 0x2F, 0), ("{", 0x2F, 2),
                     ("]", 0x30, 0), ("}", 0x30, 2), ("\\", 0x31, 0), ("|", 0x31, 2), (";", 0x33, 0),
                     (":", 0x33, 2), ("'", 0x34, 0), ('"', 0x34, 2), ("`", 0x35, 0), ("~", 0x35, 2),
                     (",", 0x36, 0), ("<", 0x36, 2), (".", 0x37, 0), (">", 0x37, 2), ("/", 0x38, 0),
                     ("?", 0x38, 2)]:
    KEYMAP[c] = (code, mod)    # "\b" is Backspace, as wda.KEYS sends it


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def mouse_report(dx=0, dy=0, buttons=0, wheel=0):
    return struct.pack("<Bbbb", buttons & 7, _clamp(int(dx), -127, 127), _clamp(int(dy), -127, 127),
                       _clamp(int(wheel), -127, 127))


def keyboard_report(usage=0, mods=0):
    return bytes([mods & 0xFF, 0, usage & 0xFF, 0, 0, 0, 0, 0])


def consumer_report(usage=0):
    return struct.pack("<H", usage & 0x3FF)


def pointer_report(x, y, buttons=0):
    return struct.pack("<BHH", buttons & 7, x, y)


def to_abs(f):
    """A fraction of the screen as the phone shows it (0..1, clamped) -> the absolute pointer's 0..32767."""
    return round(_clamp(f, 0.0, 1.0) * ABS_MAX)


def nudge(report):
    """The same pointer report one unit to the side (iOS drops a repeat of the report before)."""
    buttons, x, y = struct.unpack("<BHH", report)
    return pointer_report(x + 1 if x < ABS_MAX else x - 1, y, buttons)


def _wait(ms):
    return [(WAIT, ms)] if ms > 0 else []


def _keys(usage, mods=0):
    return [(KEYBOARD, keyboard_report(usage, mods)), (KEYBOARD, keyboard_report())]


def _consumer(usage, hold_ms=0):
    return [(CONSUMER, consumer_report(usage))] + _wait(hold_ms) + [(CONSUMER, consumer_report())]


class Scheduler:
    """Each action as reports and waits, queued and paced.

    A step is (report id, data): a report; (MOVE, data): an absolute pointer report that a later MOVE replaces
    while both are still queued, so a fast drag sends its latest point, not a backlog; or (WAIT, ms): the report
    after it goes ms after the one before it instead of PACE_MS (consecutive waits add up).
    pop() and delay() take the time in ms on any monotonic clock; nothing here sleeps or sends.
    """

    def __init__(self):
        self.queue = collections.deque()
        self.reset()

    def reset(self):
        """Forget everything queued and sent (the phone went away)."""
        self.queue.clear()
        self.buttons = 0          # the pointer's buttons once everything queued has gone
        self.pos = None           # ...and its position (absolute units)
        self.sent = {}            # report id -> the last report sent
        self._mark = float("-inf")     # when the last report went, plus the waits taken since
        self._ready = float("-inf")    # the next report may go from this time on

    # ---- queue -----------------------------------------------------------
    def add(self, steps):
        for step in steps:
            tail = self.queue[-1] if self.queue else None
            if step[0] == MOVE and tail and tail[0] == MOVE and tail[1][0] == step[1][0]:
                self.queue[-1] = step
            else:
                self.queue.append(step)

    def _take_waits(self):
        while self.queue and self.queue[0][0] == WAIT:
            self._mark += self.queue.popleft()[1]
            self._ready = max(self._ready, self._mark)

    def delay(self, now):
        """ms until the next report may go (0 = now), or None when nothing is queued."""
        self._take_waits()
        if not self.queue:
            return None
        return max(0.0, self._ready - now)

    def pop(self, now):
        """The next report as (report id, data) if it is due at `now`, else None."""
        wait = self.delay(now)
        if wait is None or wait > 0:
            return None
        kind, data = self.queue.popleft()
        rid = POINTER if kind == MOVE else kind
        if rid == POINTER and data == self.sent.get(POINTER):
            data = nudge(data)
        self.sent[rid] = data
        self._mark, self._ready = now, now + PACE_MS
        return rid, data

    def release_all(self):
        """Drop the queue; the reports that let go of whatever the phone was last told is held."""
        self.queue.clear()
        out = []
        p = self.sent.get(POINTER)
        if p and p[0]:
            out.append((POINTER, b"\0" + p[1:]))
        for rid, zero in ((KEYBOARD, keyboard_report()), (CONSUMER, consumer_report())):
            if self.sent.get(rid, zero) != zero:
                out.append((rid, zero))
        for rid, data in out:
            self.sent[rid] = data
        self.buttons = 0
        return out

    # ---- pointer (fractions of the screen as the phone shows it: x right, y down) ----------------------
    def _at(self, x, y, buttons, kind=POINTER):
        self.pos, self.buttons = (x, y), buttons
        return (kind, pointer_report(x, y, buttons))

    def _press(self, x, y):
        lift = [self._at(self.pos[0], self.pos[1], 0)] if self.buttons and self.pos else []
        return lift + [self._at(x, y, 0, MOVE), (WAIT, SETTLE_MS), self._at(x, y, 1)]

    def move(self, fx, fy):
        """Pointer only (a drag while the button is down)."""
        self.add([self._at(to_abs(fx), to_abs(fy), self.buttons, MOVE)])

    drag_to = move

    def tap(self, fx, fy, hold_ms=HOLD_MS):
        x, y = to_abs(fx), to_abs(fy)
        self.add(self._press(x, y) + _wait(hold_ms) + [self._at(x, y, 0)])

    def press(self, fx, fy):
        self.add(self._press(to_abs(fx), to_abs(fy)))

    def release(self, fx, fy):
        """Lift at (fx, fy): a release report moving the pointer too would lift where it was before."""
        if not self.buttons:
            return self.move(fx, fy)
        x, y = to_abs(fx), to_abs(fy)
        there = [] if self.pos == (x, y) else [self._at(x, y, 1, MOVE)]
        self.add(there + [self._at(x, y, 0)])

    def swipe(self, points, release_ms=0):
        """A drag through points [(fx, fy, ms since the start), ...]: pressed at the first, moved every
        DRAG_STEP_MS along straight lines, held still release_ms at the last, released."""
        pts = [(to_abs(fx), to_abs(fy), ms) for fx, fy, ms in points]
        x, y, t = pts[0]
        steps = self._press(x, y)
        for nx, ny, nt in pts[1:]:
            n = max(1, round((nt - t) / DRAG_STEP_MS))
            for i in range(1, n + 1):
                steps += [(WAIT, round(max(0, nt - t) / n)),
                          self._at(round(x + (nx - x) * i / n), round(y + (ny - y) * i / n), 1)]
            x, y, t = nx, ny, nt
        self.add(steps + _wait(release_ms) + [self._at(x, y, 0)])

    # ---- keys --------------------------------------------------------------
    def key(self, usage, mods=0):
        self.add(_keys(usage, mods))

    def text(self, s):
        """Types s on the US layout; returns how many characters have no key there."""
        steps, missing = [], 0
        for ch in s.replace("\r\n", "\n").replace("\r", "\n"):
            if ch in KEYMAP:
                steps += _keys(*KEYMAP[ch])
            else:
                missing += 1
        self.add(steps)
        return missing

    def consumer(self, usage, hold_ms=0):
        self.add(_consumer(usage, hold_ms))

    def fn(self, usage):
        """Globe held over a key: Globe down, key down, key up, Globe up."""
        self.add([(CONSUMER, consumer_report(GLOBE))] + _keys(usage) + [(CONSUMER, consumer_report())])

    def home(self):
        self.consumer(HOME)

    def app_switcher(self):
        self.add(_consumer(HOME) + [(WAIT, HOME_TWICE_MS - PACE_MS)] + _consumer(HOME))

    def control_centre(self):
        self.fn(KEY_C)

    def notifications(self):
        self.fn(KEY_N)

    def spotlight(self):
        self.consumer(SEARCH)
