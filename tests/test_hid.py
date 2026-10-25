"""The Bluetooth mouse's pure parts (iphone_control/hid.py): the report map, report packing, the US keymap, and
the scheduler's steps, waits, nudges and coalescing, on a simulated clock. No Bluetooth, no D-Bus."""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from iphone_control import hid  # noqa: E402
from iphone_control.hid import CONSUMER, KEYBOARD, MOVE, POINTER, WAIT  # noqa: E402

P, K, C = hid.pointer_report, hid.keyboard_report, hid.consumer_report
# the absolute pointer collection measured on the phone (the brief's descriptor)
MEASURED_POINTER = bytes.fromhex("05010902a10185040901a1000509190129031500250195037501810295017505810105010930093116"
                                 "000026ff7f751095028102c0c0")


def items(desc):
    """A HID report descriptor's short items: (prefix without the size bits, unsigned value)."""
    i = 0
    while i < len(desc):
        n = (0, 1, 2, 4)[desc[i] & 3]
        yield desc[i] & 0xFC, int.from_bytes(desc[i + 1:i + 1 + n], "little")
        i += 1 + n


def inputs(desc):
    """{report id: (bits of its input report, [logical maximum of each Input item])}; collections must close."""
    rid, size, count, lmax, depth, out = None, 0, 0, 0, 0, {}
    for tag, v in items(desc):
        if tag == 0x84:               # Report ID
            rid = v
        elif tag == 0x74:             # Report Size
            size = v
        elif tag == 0x94:             # Report Count
            count = v
        elif tag == 0x24:             # Logical Maximum
            lmax = v
        elif tag == 0x80:             # Input
            bits, maxes = out.get(rid, (0, []))
            out[rid] = (bits + size * count, maxes + [lmax])
        elif tag == 0xA0:             # Collection
            depth += 1
        elif tag == 0xC0:             # End Collection
            depth -= 1
            assert depth >= 0, "End Collection without a Collection"
    assert depth == 0, "a collection is not closed"
    return out


def run(s, now=0):
    """Send everything queued, as the GLib timer would: [(ms, report id, data)]."""
    out = []
    while True:
        wait = s.delay(now)
        if wait is None:
            return out
        now += wait
        out.append((now,) + s.pop(now))


def at(fx, fy):
    return hid.to_abs(fx), hid.to_abs(fy)


class ReportMap(unittest.TestCase):
    def test_four_input_reports_with_their_sizes(self):
        reports = inputs(hid.REPORT_MAP)
        self.assertEqual({r: bits for r, (bits, _) in reports.items()}, {1: 32, 2: 64, 3: 16, 4: 40})
        self.assertEqual({r: bits // 8 for r, (bits, _) in reports.items()}, hid.SIZES)
        self.assertEqual(reports[hid.POINTER][1][-1], hid.ABS_MAX)      # X and Y: 0..32767
        self.assertIn(MEASURED_POINTER, hid.REPORT_MAP)
        self.assertTrue(hid.REPORT_MAP.endswith(MEASURED_POINTER))

    def test_packers_match_the_report_sizes(self):
        for rid, data in ((hid.MOUSE, hid.mouse_report()), (KEYBOARD, K()), (CONSUMER, C()), (POINTER, P(0, 0))):
            self.assertEqual(len(data), hid.SIZES[rid])

    def test_packing(self):
        self.assertEqual(P(32767, 1, 1), b"\x01\xff\x7f\x01\x00")
        self.assertEqual(P(0x1234, 0x5678), b"\x00\x34\x12\x78\x56")
        self.assertEqual(K(0x04, hid.SHIFT), b"\x02\x00\x04\x00\x00\x00\x00\x00")
        self.assertEqual(K(), bytes(8))
        self.assertEqual(C(hid.SEARCH), b"\x21\x02")
        self.assertEqual(C(hid.GLOBE), b"\x9d\x02")
        self.assertEqual(hid.mouse_report(-300, 5, 1, 200), b"\x01\x81\x05\x7f")

    def test_fractions_to_absolute_units_clamped(self):
        self.assertEqual([hid.to_abs(f) for f in (0, 1, 0.5, 0.25, -0.2, 1.7)], [0, 32767, 16384, 8192, 0, 32767])

    def test_fractions_are_of_the_picture_as_shown_in_either_orientation(self):
        # x right, y down across whatever the phone shows: a turned picture's own corners map to the same units
        for w, h in ((1180, 2556), (2556, 1180)):
            corners = [at(x / w, y / h) for x, y in ((0, 0), (w, 0), (0, h), (w, h), (w / 2, h / 2))]
            self.assertEqual(corners, [(0, 0), (32767, 0), (0, 32767), (32767, 32767), (16384, 16384)], (w, h))
        self.assertEqual(at(0.25, 0.75), (8192, 24575))

    def test_nudge(self):
        self.assertEqual(hid.nudge(P(100, 7, 1)), P(101, 7, 1))
        self.assertEqual(hid.nudge(P(32767, 7)), P(32766, 7))


class Keymap(unittest.TestCase):
    def test_every_printable_ascii_character_has_a_key(self):
        missing = [chr(c) for c in range(32, 127) if chr(c) not in hid.KEYMAP]
        self.assertEqual(missing, [])

    def test_us_layout(self):
        km = hid.KEYMAP
        self.assertEqual((km["a"], km["A"], km["z"]), ((0x04, 0), (0x04, hid.SHIFT), (0x1D, 0)))
        self.assertEqual((km["1"], km["!"], km["0"], km[")"]), ((0x1E, 0), (0x1E, 2), (0x27, 0), (0x27, 2)))
        self.assertEqual((km["\n"], km["\t"], km[" "], km["\b"]), ((0x28, 0), (0x2B, 0), (0x2C, 0), (0x2A, 0)))
        self.assertEqual((km["@"], km["?"], km["~"], km['"']), ((0x1F, 2), (0x38, 2), (0x35, 2), (0x34, 2)))

    def test_text_types_each_key_and_counts_the_rest(self):
        s = hid.Scheduler()
        self.assertEqual(s.text("Hi\r\né\U0001F600"), 2)
        self.assertEqual(list(s.queue), [(KEYBOARD, K(0x0B, hid.SHIFT)), (KEYBOARD, K()), (KEYBOARD, K(0x0C)),
                                         (KEYBOARD, K()), (KEYBOARD, K(0x28)), (KEYBOARD, K())])

    def test_a_repeated_letter_is_released_in_between(self):
        s = hid.Scheduler()
        s.text("ll")
        self.assertEqual([d for _, _, d in run(s)], [K(0x0F), K(), K(0x0F), K()])


class Steps(unittest.TestCase):
    def setUp(self):
        self.s = hid.Scheduler()

    def queued(self):
        return list(self.s.queue)

    def test_tap_moves_settles_presses_holds_releases(self):
        self.s.tap(0.5, 0.25)
        x, y = at(0.5, 0.25)
        self.assertEqual(self.queued(), [(MOVE, P(x, y)), (WAIT, 60), (POINTER, P(x, y, 1)), (WAIT, 50),
                                         (POINTER, P(x, y, 0))])
        self.assertEqual(run(self.s), [(0, POINTER, P(x, y)), (60, POINTER, P(x, y, 1)), (110, POINTER, P(x, y))])
        self.assertEqual(self.s.buttons, 0)

    def test_long_press_holds_as_asked(self):
        self.s.tap(0.1, 0.1, hold_ms=600)
        self.assertEqual([t for t, _, _ in run(self.s)], [0, 60, 660])

    def test_a_hold_shorter_than_a_tick_takes_one_tick(self):
        self.s.tap(0.1, 0.1, hold_ms=0)
        self.assertEqual([t for t, _, _ in run(self.s)], [0, 60, 68])

    def test_press_leaves_the_button_down(self):
        self.s.press(1, 1)
        self.assertEqual(self.queued(), [(MOVE, P(32767, 32767)), (WAIT, 60), (POINTER, P(32767, 32767, 1))])
        self.assertEqual(self.s.buttons, 1)

    def test_a_press_while_pressed_lets_go_first(self):
        self.s.press(0, 0)
        self.s.press(1, 0)
        self.assertEqual(self.queued()[3:], [(POINTER, P(0, 0, 0)), (MOVE, P(32767, 0)), (WAIT, 60),
                                             (POINTER, P(32767, 0, 1))])

    def test_drag_moves_keep_the_button_down(self):
        self.s.press(0, 0)
        self.s.drag_to(0.5, 0.5)
        self.assertEqual(self.queued()[-1], (MOVE, P(16384, 16384, 1)))

    def test_queued_drag_moves_coalesce_to_the_latest_point(self):
        self.s.press(0, 0)
        for i in range(1, 21):
            self.s.drag_to(i / 40, i / 40)
        self.s.release(0.5, 0.5)
        x, y = at(0.5, 0.5)
        self.assertEqual(self.queued()[3:], [(MOVE, P(x, y, 1)), (POINTER, P(x, y, 0))])
        self.assertEqual([t for t, _, _ in run(self.s)], [0, 60, 68, 76])

    def test_a_move_already_sent_is_not_replaced(self):
        self.s.press(0, 0)
        run(self.s)
        self.s.drag_to(0.1, 0.1)
        self.assertIsNotNone(self.s.pop(1000))
        self.s.drag_to(0.2, 0.2)
        self.s.drag_to(0.3, 0.3)
        self.assertEqual(self.queued(), [(MOVE, P(*at(0.3, 0.3), 1))])

    def test_only_moves_with_the_same_buttons_coalesce(self):
        self.s.add([(MOVE, P(1, 1, 0)), (MOVE, P(2, 2, 1)), (MOVE, P(3, 3, 1))])
        self.assertEqual(self.queued(), [(MOVE, P(1, 1, 0)), (MOVE, P(3, 3, 1))])

    def test_hover_moves_coalesce_and_a_tap_replaces_a_stale_one(self):
        self.s.move(0.1, 0.1)
        self.s.move(0.2, 0.2)
        self.s.tap(0.3, 0.3)
        self.assertEqual(self.queued()[0], (MOVE, P(*at(0.3, 0.3))))
        self.assertEqual(len(self.queued()), 5)

    def test_release_moves_there_first_with_the_button_down(self):
        self.s.press(0, 0)
        self.s.release(0, 0)
        self.assertEqual(self.queued()[3:], [(POINTER, P(0, 0, 0))])
        self.s.press(0, 0)
        self.s.release(1, 1)
        self.assertEqual(self.queued()[-2:], [(MOVE, P(32767, 32767, 1)), (POINTER, P(32767, 32767, 0))])

    def test_release_without_a_press_is_a_move(self):
        self.s.release(0.5, 0.5)
        self.assertEqual(self.queued(), [(MOVE, P(16384, 16384))])

    def test_swipe_moves_every_drag_step(self):
        self.s.swipe([(0.5, 0.8, 0), (0.5, 0.2, 120)], release_ms=100)
        sent = run(self.s)
        times = [t for t, _, _ in sent]
        self.assertEqual(times[:2], [0, 60])                         # moved, settled, pressed
        self.assertEqual(times[2:-1], [60 + 12 * i for i in range(1, 11)])
        self.assertEqual(times[-1], times[-2] + 100)                 # still before lifting: no fling
        self.assertEqual(sent[-2][2], P(*at(0.5, 0.2), 1))
        self.assertEqual(sent[-1][2], P(*at(0.5, 0.2), 0))
        self.assertTrue(all(d[0] == 1 for _, _, d in sent[1:-1]))

    def test_key_presses_and_releases(self):
        self.s.key(0x50, hid.SHIFT)
        self.assertEqual(run(self.s), [(0, KEYBOARD, K(0x50, hid.SHIFT)), (8, KEYBOARD, K())])

    def test_consumer_with_and_without_hold(self):
        self.s.consumer(hid.VOLUME_UP)
        self.assertEqual(self.queued(), [(CONSUMER, C(0xE9)), (CONSUMER, C())])
        self.s.consumer(hid.LOCK, hold_ms=200)
        self.assertEqual(self.queued()[2:], [(CONSUMER, C(0x30)), (WAIT, 200), (CONSUMER, C())])

    def test_home_and_spotlight(self):
        self.s.home()
        self.s.spotlight()
        self.assertEqual([d for _, _, d in run(self.s)], [C(0x40), C(), C(0x221), C()])

    def test_app_switcher_is_home_twice_120_ms_apart(self):
        self.s.app_switcher()
        sent = run(self.s)
        self.assertEqual([d for _, _, d in sent], [C(0x40), C(), C(0x40), C()])
        self.assertEqual(sent[2][0] - sent[0][0], 120)

    def test_fn_chord_order(self):
        self.s.fn(0x06)
        self.assertEqual(run(self.s), [(0, CONSUMER, C(hid.GLOBE)), (8, KEYBOARD, K(0x06)), (16, KEYBOARD, K()),
                                       (24, CONSUMER, C())])

    def test_control_centre_and_notifications_are_globe_chords(self):
        self.s.control_centre()
        self.s.notifications()
        keys = [d for _, rid, d in run(self.s) if rid == KEYBOARD]
        self.assertEqual(keys, [K(hid.KEY_C), K(), K(hid.KEY_N), K()])


class Pacing(unittest.TestCase):
    def setUp(self):
        self.s = hid.Scheduler()

    def test_one_report_per_tick(self):
        self.s.text("hello")
        times = [t for t, _, _ in run(self.s)]
        self.assertEqual(times, [hid.PACE_MS * i for i in range(10)])

    def test_nothing_is_due_before_its_time(self):
        self.s.tap(0.5, 0.5)
        self.assertIsNotNone(self.s.pop(0))
        self.assertEqual(self.s.delay(10), 50)
        self.assertIsNone(self.s.pop(59.5))
        self.assertIsNotNone(self.s.pop(60))
        self.assertEqual(self.s.delay(60), 50)
        self.assertIsNotNone(self.s.pop(110))
        self.assertIsNone(self.s.delay(110))

    def test_an_idle_queue_sends_at_once(self):
        self.s.key(4)
        run(self.s)
        self.assertIsNone(self.s.delay(5000))
        self.s.key(5)
        self.assertEqual(self.s.delay(5000), 0)
        self.assertEqual(self.s.pop(5000), (KEYBOARD, K(5)))

    def test_a_wait_counts_from_when_the_report_before_really_went(self):
        self.s.tap(0.5, 0.5)
        self.s.pop(0)
        self.assertEqual(self.s.pop(200)[1][0], 1)       # the timer came late: pressed at 200...
        self.assertEqual(self.s.delay(200), 50)          # ...so the hold still lasts 50 ms

    def test_consecutive_waits_add_up(self):
        self.s.add([(KEYBOARD, K(4)), (WAIT, 30), (WAIT, 40), (KEYBOARD, K())])
        self.assertEqual([t for t, _, _ in run(self.s)], [0, 70])

    def test_delays_are_fine_for_a_millisecond_timer(self):
        self.s.swipe([(0, 0, 0), (1, 1, 100)])
        now = 0.0
        while True:
            wait = self.s.delay(now)
            if wait is None:
                break
            now += max(1, math.ceil(wait))               # what blehid hands GLib.timeout_add
            self.assertIsNotNone(self.s.pop(now))


class Nudge(unittest.TestCase):
    def setUp(self):
        self.s = hid.Scheduler()

    def test_a_second_tap_at_the_same_point_is_nudged(self):
        self.s.tap(0.5, 0.5)
        self.s.tap(0.5, 0.5)
        x, y = at(0.5, 0.5)
        sent = [d for _, _, d in run(self.s)]
        self.assertEqual(sent, [P(x, y), P(x, y, 1), P(x, y), P(x + 1, y), P(x, y, 1), P(x, y)])

    def test_a_press_where_the_pointer_is_is_not_nudged(self):
        self.s.press(0.2, 0.2)
        sent = [d for _, _, d in run(self.s)]
        self.assertEqual(sent, [P(*at(0.2, 0.2)), P(*at(0.2, 0.2), 1)])

    def test_the_same_drag_point_twice_is_nudged_back_at_the_far_edge(self):
        self.s.press(1, 0)
        self.s.drag_to(1, 0.5)
        run(self.s)
        self.s.drag_to(1, 0.5)
        self.assertEqual(run(self.s, 1000)[0][2], P(32766, 16384, 1))


class ReleaseAll(unittest.TestCase):
    def setUp(self):
        self.s = hid.Scheduler()

    def test_nothing_held_nothing_to_send(self):
        self.s.tap(0.5, 0.5)
        run(self.s)
        self.assertEqual(self.s.release_all(), [])

    def test_lets_go_of_the_button_where_it_was_pressed(self):
        self.s.press(0.5, 0.5)
        run(self.s)
        self.s.drag_to(0.7, 0.7)
        self.assertEqual(self.s.release_all(), [(POINTER, P(*at(0.5, 0.5), 0))])
        self.assertEqual(list(self.s.queue), [])
        self.assertEqual(self.s.buttons, 0)

    def test_lets_go_of_a_half_sent_chord(self):
        self.s.fn(0x06)
        self.s.pop(0)
        self.s.pop(8)
        self.assertEqual(self.s.release_all(), [(KEYBOARD, K()), (CONSUMER, C())])

    def test_reset_forgets_everything(self):
        self.s.press(0.5, 0.5)
        self.s.pop(0)
        self.s.reset()
        self.assertEqual((list(self.s.queue), self.s.buttons, self.s.pos, self.s.sent), ([], 0, None, {}))
        self.assertIsNone(self.s.delay(0))


if __name__ == "__main__":
    unittest.main()
