"""Window pixels -> phone points, and the W3C gestures sent to WebDriverAgent (pure, no GTK)."""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from iphone_control import geometry as g  # noqa: E402

PHONE = (393, 852)          # iPhone 15 Pro, points


def acts(w3c, finger=0):
    return w3c["actions"][finger]["actions"]


def moves(w3c, finger=0):
    return [s for s in acts(w3c, finger) if s["type"] == "pointerMove"]


def timeline(w3c):
    """Where the finger is from pointerDown on: [(x, y, ms since down), ...], pauses included."""
    a = acts(w3c)
    down = [s["type"] for s in a].index("pointerDown")
    x, y, t, out = a[0]["x"], a[0]["y"], 0, []
    out.append((x, y, 0))
    for s in a[down + 1:]:
        if s["type"] == "pointerMove":
            x, y = s["x"], s["y"]
        if s["type"] in ("pointerMove", "pause"):
            t += s["duration"]
            out.append((x, y, t))
    return out


def at(path, t):
    """Position on [(x, y, ms), ...] at time t (linear between points)."""
    for (xa, ya, ta), (xb, yb, tb) in zip(path, path[1:]):
        if ta <= t <= tb:
            f = (t - ta) / (tb - ta) if tb > ta else 1
            return xa + (xb - xa) * f, ya + (yb - ya) * f
    return path[-1][:2]


def lift_speed(path, ms=60):
    """Points per ms over the last ms before the finger lifts."""
    end = path[-1][2]
    (xa, ya), (xb, yb) = at(path, end - ms), at(path, end)
    return math.hypot(xb - xa, yb - ya) / ms


def real(track):
    return [(x, y, (t - track[0][2]) * 1000) for x, y, t in track]


def line(x0, y0, x1, y1, ms, step=7, t0=0.0):
    """A straight drag sampled every ~step points (the window: over 6), ms long, starting at t0 seconds."""
    n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) / step))
    return [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n, t0 + ms / 1000 * i / n) for i in range(n + 1)]


class Mapping(unittest.TestCase):
    def test_corners_and_centre_when_picture_fills_widget(self):
        self.assertEqual(g.to_phone(0, 0, (393, 852), (393, 852), PHONE), (0, 0))
        self.assertEqual(g.to_phone(393, 852, (393, 852), (393, 852), PHONE), (393, 852))
        x, y = g.to_phone(196.5, 426, (393, 852), (393, 852), PHONE)
        self.assertAlmostEqual(x, 196.5)
        self.assertAlmostEqual(y, 426)

    def test_letterbox_side_bars_are_outside(self):
        widget = (852 * 2, 852)                 # twice as wide as the picture: bars left and right
        ox, oy, scale = g.letterbox(*widget, 393, 852)
        self.assertEqual((round(ox), oy, scale), (round((1704 - 393) / 2), 0, 1))
        self.assertIsNone(g.to_phone(10, 400, widget, (393, 852), PHONE))
        self.assertIsNotNone(g.to_phone(ox + 5, 400, widget, (393, 852), PHONE))

    def test_letterbox_top_and_bottom_bars(self):
        widget = (393, 1000)
        ox, oy, scale = g.letterbox(*widget, 393, 852)
        self.assertEqual((ox, scale), (0, 1))
        self.assertAlmostEqual(oy, 74)
        self.assertIsNone(g.to_phone(100, 10, widget, (393, 852), PHONE))
        self.assertIsNone(g.to_phone(100, 990, widget, (393, 852), PHONE))
        self.assertAlmostEqual(g.to_phone(100, 74, widget, (393, 852), PHONE)[1], 0)

    def test_scaled_mjpeg_frame_maps_to_points(self):
        # WDA streams a scaled frame (e.g. 60 %): mapping must still give points
        x, y = g.to_phone(354, 766, (354, 766), (354, 766), PHONE)
        self.assertAlmostEqual(x, 393)
        self.assertAlmostEqual(y, 852)

    def test_widget_bigger_than_frame(self):
        # a 2x larger widget showing a 60 % frame: the centre is still the phone's centre
        x, y = g.to_phone(354, 766, (708, 1532), (354, 766), PHONE)
        self.assertAlmostEqual(x, 196.5)
        self.assertAlmostEqual(y, 426)

    def test_landscape_swaps_axes(self):
        x, y = g.to_phone(852, 393, (852, 393), (852, 393), PHONE)
        self.assertAlmostEqual(x, 852)
        self.assertAlmostEqual(y, 393)
        # the phone may report either orientation; the picture decides
        self.assertEqual(g.oriented((852, 393), (100, 200)), (393, 852))
        self.assertEqual(g.oriented((393, 852), (200, 100)), (852, 393))
        self.assertEqual(g.oriented((393, 852), None), (393, 852))

    def test_window_size_fits_small_screens_and_landscape(self):
        w, h = g.window_size(1366, 768)
        self.assertLessEqual(h, 768 * 0.9 + 1)
        self.assertAlmostEqual(w / (h - 40), 393 / 852, places=2)
        w, h = g.window_size(1366, 768, 852 / 393)              # landscape picture: width-bound
        self.assertLessEqual(w, 1366 * 0.9 + 1)
        self.assertLessEqual(h, 768)
        self.assertAlmostEqual(w / (h - 40), 852 / 393, places=1)
        w, h = g.window_size(800, 1280)                          # portrait monitor
        self.assertLessEqual(w, 800)
        self.assertLessEqual(h, 1280)
        self.assertEqual(g.window_size(1, 1), (1, 1))             # never zero or negative


class Gestures(unittest.TestCase):
    def test_tap(self):
        a = acts(g.tap(10.4, 20.6))
        self.assertEqual([s["type"] for s in a], ["pointerMove", "pointerDown", "pause", "pointerUp"])
        self.assertEqual((a[0]["x"], a[0]["y"]), (10, 21))
        self.assertEqual(g.tap(1, 1)["actions"][0]["parameters"], {"pointerType": "touch"})
        self.assertEqual(acts(g.tap(1, 1, hold_ms=800))[2], {"type": "pause", "duration": 800})

    def test_double_tap_is_two_quick_taps_in_one_request(self):
        a = acts(g.double_tap(10, 20))
        types = [s["type"] for s in a]
        self.assertEqual(types.count("pointerDown"), 2)
        self.assertEqual(types.count("pointerUp"), 2)
        self.assertEqual(len(moves(g.double_tap(10, 20))), 1)    # same spot
        self.assertLess(sum(s["duration"] for s in a if s["type"] == "pause"), 300)

    def test_click_is_tap_hold_is_long_press_drag_is_swipe(self):
        tap = acts(g.drag_to_gesture([(100, 100, 0.0), (102, 101, 0.1)]))
        self.assertEqual(tap[2], {"type": "pause", "duration": 40})
        long = acts(g.drag_to_gesture([(100, 100, 0.0), (101, 100, 0.9)]))
        self.assertEqual(long[2]["type"], "pause")
        self.assertGreater(long[2]["duration"], g.LONG_PRESS_MS)
        swipe = acts(g.drag_to_gesture([(100, 600, 0.0), (100, 400, 0.1), (100, 200, 0.2)]))
        ms = [s for s in swipe if s["type"] == "pointerMove"]
        self.assertEqual((ms[0]["y"], ms[-1]["y"]), (600, 200))
        self.assertNotIn("pause", [s["type"] for s in swipe])  # a quick swipe never starts with a hold
        self.assertEqual(swipe[-1]["type"], "pointerUp")

    def test_hold_then_drag_starts_with_a_long_pause(self):
        track = [(100, 100, 0.0), (102, 100, 0.8), (130, 100, 0.85), (200, 100, 1.0)]
        a = acts(g.drag_to_gesture(track))
        self.assertEqual([s["type"] for s in a[:3]], ["pointerMove", "pointerDown", "pause"])
        self.assertGreater(a[2]["duration"], g.LONG_PRESS_MS)
        self.assertEqual((a[-2]["x"], a[-2]["y"]), (200, 100))
        self.assertTrue(all(s["duration"] >= 10 for s in a[3:] if s["type"] == "pointerMove"))

    def test_long_drag_is_thinned_but_keeps_end(self):
        track = [(100, 800 - i, i / 100) for i in range(300)]
        ms = moves(g.drag_to_gesture(track))
        self.assertLessEqual(len(ms), g.MAX_SWIPE_MOVES + 1)        # every move costs time on the phone
        self.assertEqual(ms[-1]["y"], 800 - 299)
        long_hold_drag = [(100, 800, 0.0), (100, 801, 0.6)] + [(100, 800 - i, 0.6 + i / 100) for i in range(300)]
        self.assertLessEqual(len(moves(g.drag_to_gesture(long_hold_drag))), g.MAX_SWIPE_POINTS + 3)

    def test_move_durations_follow_the_mouse(self):
        # a swipe no longer than MAX_REPLAY_MS is replayed exactly as it was
        # (released moving, so the last 100 ms is sent as two moves of LIFT_MS: see test_released_moving_...)
        a = moves(g.drag_to_gesture([(100, 600, 0.0), (100, 500, 0.15), (100, 400, g.MAX_REPLAY_MS / 1000)]))
        self.assertEqual([m["duration"] for m in a], [0, 150, g.MAX_REPLAY_MS - 150 - g.LIFT_MS, g.LIFT_MS])
        self.assertEqual([m["y"] for m in a], [600, 500, 450, 400])
        self.assertIsNone(g.shape_swipe([(0, 0, 0), (100, 0, g.MAX_REPLAY_MS)]))

    def test_gesture_can_rest_before_lifting(self):
        a = acts(g.gesture([(1, 1, 0), (50, 1, 100)], release_ms=150))
        self.assertEqual(a[-2:], [{"type": "pause", "duration": 150}, {"type": "pointerUp", "button": 0}])
        self.assertNotIn("pause", [s["type"] for s in acts(g.gesture([(1, 1, 0), (50, 1, 100)]))])

    def test_scroll_direction(self):
        down = moves(g.scroll(200, 400, 1, PHONE))
        self.assertLess(down[-1]["y"], down[0]["y"])           # wheel down -> finger moves up
        self.assertEqual(down[0]["y"] - down[-1]["y"], g.SCROLL_PT)
        up = moves(g.scroll(200, 400, -1, PHONE))
        self.assertGreater(up[-1]["y"], up[0]["y"])

    def test_scroll_stays_away_from_the_edges(self):
        # the home indicator (bottom) would go Home, the top edge would open Notification Centre
        w, h = PHONE
        for x, y, dy, dx in [(200, 845, 1, 0), (200, 5, -3, 0), (200, 845, -3, 0), (200, 5, 3, 0),
                             (2, 400, 0, -4), (390, 400, 0, 4), (200, 400, 40, 0), (200, 400, 0, 40)]:
            for m in moves(g.scroll(x, y, dy, PHONE, dx=dx)):
                self.assertGreaterEqual(m["y"], round(h * 0.12) - 1, (x, y, dy, dx))
                self.assertLessEqual(m["y"], round(h * 0.88) + 1, (x, y, dy, dx))
                self.assertGreaterEqual(m["x"], round(w * 0.05) - 1, (x, y, dy, dx))
                self.assertLessEqual(m["x"], round(w * 0.95) + 1, (x, y, dy, dx))

    def test_scroll_pauses_before_lifting(self):
        a = acts(g.scroll(200, 400, 1, PHONE))
        self.assertEqual(a[-2], {"type": "pause", "duration": 120})     # no fling
        self.assertEqual(a[-1]["type"], "pointerUp")

    def test_horizontal_scroll(self):
        h = moves(g.scroll(200, 400, 0, PHONE, dx=2))
        self.assertLess(h[-1]["x"], h[0]["x"])                  # scroll right -> finger moves left
        self.assertEqual(h[-1]["y"], h[0]["y"])
        self.assertEqual(h[0]["x"] - h[-1]["x"], 2 * g.SCROLL_PT)

    def test_scroll_per_unit_and_old_signature(self):
        a = moves(g.scroll(200, 400, 30, PHONE, per_unit=1))
        self.assertEqual(a[0]["y"] - a[-1]["y"], 30)
        self.assertEqual(g.scroll(200, 400, 1, PHONE), g.scroll(200, 400, 1, PHONE, 0, g.SCROLL_PT))

    def test_pinch_spreads_to_zoom_in_and_closes_to_zoom_out(self):
        p = g.pinch(200, 400, 2, PHONE)["actions"]
        self.assertEqual([f["id"] for f in p], ["finger1", "finger2"])
        f1, f2 = p[0]["actions"], p[1]["actions"]
        self.assertLess(f1[2]["x"], f1[0]["x"])               # finger1 moves left
        self.assertGreater(f2[2]["x"], f2[0]["x"])            # finger2 moves right
        z1 = g.pinch(200, 400, 0.5, PHONE)["actions"][0]["actions"]
        self.assertGreater(z1[2]["x"], z1[0]["x"])
        for f in g.pinch(1, 400, 5, PHONE)["actions"] + g.pinch(392, 851, 0.01, PHONE)["actions"]:
            for m in f["actions"]:
                if m["type"] == "pointerMove":
                    self.assertTrue(0 < m["x"] < 393 and 0 < m["y"] < 852, m)

    def test_back_swipe_from_the_left_edge(self):
        m = moves(g.back_swipe(PHONE))
        self.assertLessEqual(m[0]["x"], 5)
        self.assertGreater(m[-1]["x"], PHONE[0] / 2)
        self.assertEqual(m[0]["y"], m[-1]["y"])

    def test_edge_swipes(self):
        sw = acts(g.edge_swipe("app_switcher", PHONE))
        self.assertGreater(sw[0]["y"], PHONE[1] - 5)
        moves = [a for a in sw[2:] if a["type"] == "pointerMove"]
        self.assertLess(moves[0]["duration"], moves[1]["duration"])    # a quick flick off the edge, then slower
        self.assertEqual(sw[-2], {"type": "pause", "duration": 1000})  # held before lifting
        cc = acts(g.edge_swipe("control_centre", PHONE))
        self.assertLess(cc[0]["y"], 5)
        self.assertGreater(cc[0]["x"], PHONE[0] / 2)
        nc = acts(g.edge_swipe("notifications", PHONE))
        self.assertLess(nc[0]["y"], 5)
        self.assertLess(nc[0]["x"], PHONE[0] / 2)
        with self.assertRaises(ValueError):
            g.edge_swipe("nope", PHONE)

    def test_every_gesture_is_valid_w3c(self):
        for w3c in [g.tap(1, 2), g.double_tap(3, 4), g.scroll(100, 100, 1, PHONE), g.pinch(100, 100, 2, PHONE),
                    g.back_swipe(PHONE), g.edge_swipe("app_switcher", PHONE),
                    g.drag_to_gesture([(1, 1, 0), (200, 1, 0.2)]), g.edge_swipe("control_centre", PHONE),
                    g.drag_to_gesture([(100, 100, 0.0), (101, 100, 0.9)]),                       # long press
                    g.drag_to_gesture([(100, 100, 0.0), (102, 100, 0.8), (200, 100, 1.0)])]:     # hold, then drag
            for src in w3c["actions"]:
                self.assertEqual(src["type"], "pointer")
                self.assertEqual(src["parameters"], {"pointerType": "touch"})
                seq = [s["type"] for s in src["actions"]]
                self.assertEqual(seq[0], "pointerMove")
                # every finger goes down at once: WDA ignores leading pauses, so none may be needed
                self.assertEqual(seq[1], "pointerDown")
                self.assertEqual(src["actions"][0]["duration"], 0)
                self.assertEqual(seq.count("pointerDown"), seq.count("pointerUp"))
                for s in src["actions"]:
                    if s["type"] == "pointerMove":
                        self.assertIsInstance(s["x"], int)
                        self.assertIsInstance(s["y"], int)
                    if "duration" in s:
                        self.assertIsInstance(s["duration"], int)
                        self.assertGreaterEqual(s["duration"], 0)


class SwipeShaping(unittest.TestCase):
    """Slow drags replay quickly; what iOS reads at the end (lift-off velocity, a stop) stays as it was."""

    def check_valid(self, track, w3c):
        """Same start and end, every move of a shaped swipe >= MIN_SEGMENT_MS, never slower than the drag."""
        tl = timeline(w3c)
        ms = moves(w3c)
        self.assertEqual((ms[0]["x"], ms[0]["y"]), (round(track[0][0]), round(track[0][1])))
        self.assertEqual((ms[-1]["x"], ms[-1]["y"]), (round(track[-1][0]), round(track[-1][1])))
        took = (track[-1][2] - track[0][2]) * 1000
        if g.shape_swipe(real(track), PHONE):
            self.assertLess(tl[-1][2], took)
        else:                                   # replayed as it was: the drag's own length (one short move: 10 ms)
            self.assertLessEqual(abs(tl[-1][2] - max(took, g.MIN_SEGMENT_MS)), 1)
        self.assertTrue(all(m["duration"] >= g.MIN_SEGMENT_MS for m in ms[1:]), ms)
        return tl

    def test_slow_swipe_released_moving_is_quick_and_keeps_its_lift_off_speed(self):
        track = line(300, 400, 60, 400, 700)                    # a slow 700 ms page swipe, still moving at release
        w = g.drag_to_gesture(track)
        tl = self.check_valid(track, w)
        self.assertLessEqual(tl[-1][2], g.MAX_REPLAY_MS)
        self.assertNotIn("pause", [s["type"] for s in acts(w)])      # no stop added: it was moving
        self.assertAlmostEqual(lift_speed(tl), lift_speed(real(track)), delta=0.03)
        # the tail is the user's own: the last TAIL_MS has the same points and timings as the drag
        end, rend = tl[-1][2], real(track)[-1][2]
        for back in range(0, g.TAIL_MS + 1, 10):
            ox, oy = at(tl, end - back)
            rx, ry = at(real(track), rend - back)
            self.assertAlmostEqual(ox, rx, delta=1.5, msg=back)
            self.assertAlmostEqual(oy, ry, delta=1.5, msg=back)

    def test_slow_start_then_fling_still_flings(self):
        track = line(200, 700, 200, 650, 600) + line(200, 650, 200, 250, 100, t0=0.6)[1:]
        tl = self.check_valid(track, g.drag_to_gesture(track))
        self.assertLessEqual(tl[-1][2], g.MAX_REPLAY_MS)
        self.assertAlmostEqual(lift_speed(tl, 50), 4.0, delta=0.2)          # 400 pt in 100 ms
        self.assertAlmostEqual(lift_speed(tl, 50), lift_speed(real(track), 50), delta=0.1)

    def test_press_before_moving_off_is_not_replayed(self):
        # still for 300 ms (less than a long press), then a slow swipe: the finger moves off at once
        track = [(200, 400, 0.0), (201, 400, 0.3)] + line(200, 400, 200, 100, 500, t0=0.3)[1:]
        w = g.drag_to_gesture(track)
        self.check_valid(track, w)
        a = acts(w)
        self.assertEqual([s["type"] for s in a[:3]], ["pointerMove", "pointerDown", "pointerMove"])
        self.assertGreater(abs(a[2]["y"] - 400) / a[2]["duration"], 1)    # off at speed, no wait first
        self.assertLessEqual(timeline(w)[-1][2], g.MAX_REPLAY_MS)

    def test_drag_that_stopped_lifts_with_no_velocity(self):
        track = line(300, 400, 60, 400, 500) + [(58, 401, 0.9)]      # moved for 500 ms, then still for 400 ms
        w = g.drag_to_gesture(track)
        tl = self.check_valid(track, w)
        a = acts(w)
        self.assertEqual(a[-1]["type"], "pointerUp")
        self.assertEqual(a[-2]["type"], "pause")
        self.assertGreaterEqual(a[-2]["duration"], g.REST_HOLD_MS)
        self.assertEqual(lift_speed(tl), 0)
        self.assertLess(tl[-1][2], 900 * 0.6)
        settle = a[-3]                                      # the last few points of drift, crossed slowly
        self.assertEqual((settle["x"], settle["y"]), (58, 401))
        self.assertGreaterEqual(settle["duration"], g.REST_SETTLE_MS)

    def test_slow_drag_that_stopped_never_becomes_a_fling(self):
        # very slow (60 pt/s) and then still: every move after the hurried head stays as slow as the drag
        track = line(200, 600, 200, 300, 5000) + [(200, 300, 5.4)]
        w = g.drag_to_gesture(track)
        tl = self.check_valid(track, w)
        self.assertEqual(lift_speed(tl, g.REST_HOLD_MS), 0)
        a = acts(w)
        self.assertEqual(a[-2]["type"], "pause")
        tail_start = tl[-1][2] - a[-2]["duration"] - g.TAIL_MS
        for (xa, ya, ta), (xb, yb, tb) in zip(tl, tl[1:]):
            if ta >= tail_start and tb > ta:
                self.assertLessEqual(math.hypot(xb - xa, yb - ya) / (tb - ta), 0.1)

    def test_short_stop_is_kept_as_it_was(self):
        # stopped for less than REST_MS: no stop added, the real tail (slowing down) is replayed
        track = line(300, 400, 60, 400, 600) + [(59, 400, 0.65)]
        w = g.drag_to_gesture(track)
        tl = self.check_valid(track, w)
        self.assertNotIn("pause", [s["type"] for s in acts(w)])
        self.assertAlmostEqual(lift_speed(tl, 50), lift_speed(real(track), 50), delta=0.03)

    def test_long_stop_is_shortened_except_on_the_home_indicator(self):
        middle = line(200, 600, 200, 300, 400) + [(200, 300, 2.4)]          # 2 s still before release
        a = acts(g.drag_to_gesture(middle, PHONE))
        self.assertEqual(a[-2], {"type": "pause", "duration": g.MAX_REST_MS})
        climb = line(196, 851, 196, 560, 400) + [(196, 560, 1.2)]           # up from the bottom edge, 800 ms hold
        a = acts(g.drag_to_gesture(climb, PHONE))
        self.assertEqual(a[-2], {"type": "pause", "duration": 800})        # what opens the App Switcher
        a = acts(g.drag_to_gesture(line(196, 851, 196, 560, 400) + [(196, 560, 3.0)], PHONE))
        self.assertEqual(a[-2], {"type": "pause", "duration": g.EDGE_REST_MS})
        a = acts(g.drag_to_gesture(climb))                                  # phone size unknown: shortened
        self.assertEqual(a[-2], {"type": "pause", "duration": g.MAX_REST_MS})

    def test_curved_path_keeps_its_shape(self):
        # a slow quarter circle (radius 150 around 200, 400): every replayed point is on it, in order
        track = [(200 + 150 * math.cos(a), 400 + 150 * math.sin(a), a / (math.pi / 2) * 1.2)
                 for a in (math.pi / 2 * i / 60 for i in range(61))]
        w = g.drag_to_gesture(track)
        self.check_valid(track, w)
        ms = moves(w)
        self.assertGreaterEqual(len(ms), 4)                         # not cut to one straight line...
        self.assertLessEqual(len(ms), 7)                            # ...but still few moves
        self.assertLessEqual(ms[-1]["duration"], g.LIFT_MS)
        angles = [math.atan2(m["y"] - 400, m["x"] - 200) for m in ms]
        self.assertEqual(angles, sorted(angles))
        for m in ms:
            self.assertAlmostEqual(math.hypot(m["x"] - 200, m["y"] - 400), 150, delta=1.5)
        for a, b in zip(ms, ms[1:]):          # the straight moves stay near the arc (one chord would be 44 pt off)
            mid = math.hypot((a["x"] + b["x"]) / 2 - 200, (a["y"] + b["y"]) / 2 - 400)
            self.assertGreater(mid, 150 - g.CURVE_PT - 1)

    def test_hold_then_drag_is_not_shaped(self):
        track = [(100, 100, 0.0), (102, 100, 0.8)] + line(102, 100, 300, 500, 1500, t0=0.8)[1:]
        a = acts(g.drag_to_gesture(track, PHONE))
        self.assertEqual(a[2], {"type": "pause", "duration": 800})
        self.assertAlmostEqual(sum(s["duration"] for s in a if s["type"] == "pointerMove"), 1500, delta=len(a))
        self.assertNotEqual(a[-2]["type"], "pause")

    def test_taps_and_long_presses_are_not_shaped(self):
        self.assertEqual(g.drag_to_gesture([(100, 100, 0.0), (103, 104, 0.3)]), g.tap(100, 100))
        long = g.drag_to_gesture([(100, 100, 0.0), (103, 104, 2.0)], PHONE)
        self.assertEqual(long, g.tap(100, 100, hold_ms=2000))

    def test_there_and_back_jiggle_is_replayed_as_it_was(self):
        track = [(100, 100, 0.0), (110, 100, 0.2), (106, 100, 0.4), (105, 100, 0.8)]   # never 6 pt from the end
        self.assertIsNone(g.shape_swipe(real(track)))
        tl = timeline(g.drag_to_gesture(track))
        self.assertEqual(tl[-1][2], 800)                            # its own timing, within SIMPLIFY_PT
        for x, y, t in real(track):
            self.assertLessEqual(math.dist(at(tl, t), (x, y)), g.SIMPLIFY_PT + 0.5, t)

    def test_shaping_only_when_it_is_quicker(self):
        # moved 150 ms, still 110 ms: settling + the minimum rest would take longer than the drag did
        track = line(200, 600, 200, 450, 150) + [(200, 449, 0.26)]
        self.assertIsNone(g.shape_swipe(real(track)))
        self.assertEqual(sum(m["duration"] for m in moves(g.drag_to_gesture(track))), 260)

    def test_quick_flick_keeps_its_speed(self):
        # samples 7 ms apart (a 144 Hz pointer): no move is stretched to 10 ms, so the flick is not slowed
        track = [(200, 700 - i * 20, i * 0.007) for i in range(21)]
        w = g.drag_to_gesture(track)
        tl = self.check_valid(track, w)
        self.assertEqual(tl[-1][2], 140)
        self.assertAlmostEqual(lift_speed(tl, 40), 20 / 7, delta=0.15)

    def test_swipes_are_sent_with_few_moves(self):
        # XCTest resolves every move before the finger goes down (~27 ms each at snapshotMaxDepth 1): a steady
        # swipe is two moves (the last LIFT_MS on its own), a slow one shaped to a quick head and its real-speed
        # end three
        self.assertEqual(len(moves(g.drag_to_gesture(line(300, 400, 60, 400, 200)))), 3)
        self.assertEqual(len(moves(g.drag_to_gesture(line(200, 700, 200, 300, 150, step=3)))), 3)
        self.assertEqual(len(moves(g.drag_to_gesture(line(200, 700, 200, 650, 50)))), 2)    # too short to split
        slow = moves(g.drag_to_gesture(line(300, 400, 60, 400, 700)))
        self.assertLessEqual(len(slow), g.MAX_SWIPE_MOVES + 1)
        stopped = acts(g.drag_to_gesture(line(300, 400, 60, 400, 500) + [(58, 401, 0.9)]))
        self.assertLessEqual(len([s for s in stopped if s["type"] == "pointerMove"]), 4)   # + the stop and drift

    def test_lift_off_speed_survives_the_few_moves(self):
        # speeding up (a flick) or slowing down before release: the last LIFT_MS keeps the drag's own speed
        for name, f in (("flick", lambda u: u * u), ("ease", lambda u: 0.5 - 0.5 * math.cos(math.pi * u)),
                        ("ease-out", lambda u: 1 - (1 - u) ** 2 * 0.9)):
            for ms in (150, 300, 700):
                n = ms // 8
                track = [(300 - 240 * f(i / n), 420 + 10 * math.sin(math.pi * f(i / n)), ms / 1000 * i / n)
                         for i in range(n + 1)]
                w = g.drag_to_gesture(track, PHONE)
                tl = self.check_valid(track, w)
                want = lift_speed(real(track), g.LIFT_MS)
                shaped = g.shape_swipe(real(track), PHONE)
                if shaped and shaped[1]:                         # all but still at the end: a stop, no fling
                    self.assertEqual(lift_speed(tl, g.LIFT_MS), 0, (name, ms))
                    continue
                self.assertAlmostEqual(lift_speed(tl, g.LIFT_MS), want, delta=0.2 * max(want, 0.1), msg=(name, ms))
                # + one for the last LIFT_MS when needed (the ~10 pt bow of these tracks may cost one more)
                self.assertLessEqual(len(moves(w)), g.MAX_SWIPE_MOVES + 2, (name, ms))
                self.assertLessEqual(moves(w)[-1]["duration"], g.LIFT_MS, (name, ms))

    def test_released_moving_swipe_ends_with_a_short_move(self):
        # the phone check (iPhone 15, iOS 26.5): 320 -> 80 pt in 700 ms, window-style samples, released moving.
        # Sent as down, 82 ms to 114, ONE 100 ms move to 80 it changed the page 14 times in 23; the same path
        # with the tail as two 50 ms moves 12 in 12. So the last move of a moving release is never > LIFT_MS.
        track, x = [(320.0, 426.0, 0.0)], 320.0
        for i in range(1, 89):                                  # a motion event every 8 ms, kept past 6 pt
            x = 320 - 240 * i / 88
            if abs(x - track[-1][0]) > 6:
                track.append((x, 426.0, 0.7 * i / 88))
        track.append((x, 426.0, 0.7))
        w = g.drag_to_gesture(track, PHONE)
        tl = self.check_valid(track, w)
        ms = moves(w)
        self.assertEqual([m["duration"] for m in ms[-2:]], [g.LIFT_MS, g.LIFT_MS])
        self.assertEqual(ms[-1]["x"], 80)
        self.assertAlmostEqual(ms[-2]["x"], 97, delta=1)                        # on the path, at its real time
        self.assertLessEqual(len(ms), g.MAX_SWIPE_MOVES + 1)                    # the split costs no extra move
        self.assertAlmostEqual(lift_speed(tl, g.LIFT_MS), lift_speed(real(track), g.LIFT_MS), delta=0.02)
        # and every other moving release: a flick, an unshaped 200 ms swipe, a slow drag easing out
        for track in (line(300, 400, 210, 400, 90), line(300, 400, 60, 400, 200), line(320, 400, 220, 400, 700),
                      [(300 - 240 * (1 - (1 - i / 80) ** 2), 400, 0.9 * i / 80) for i in range(81)]):
            ms = moves(g.drag_to_gesture(track, PHONE))
            self.assertLessEqual(ms[-1]["duration"], g.LIFT_MS, track[-1])
            self.assertLessEqual(len(ms), g.MAX_SWIPE_MOVES + 1, track[-1])
        # a stop before release is not a moving release: it keeps its settle and rest
        a = acts(g.drag_to_gesture(line(300, 400, 60, 400, 500) + [(58, 401, 0.9)], PHONE))
        self.assertEqual([s["type"] for s in a[-3:]], ["pointerMove", "pause", "pointerUp"])

    def check_shape(self, track, w3c, bound):
        """Every point of the drag is within bound of the path the phone replays (timing aside)."""
        ms = [(m["x"], m["y"]) for m in moves(w3c)]
        for p in track:
            off = min(g._off(p, a, b) for a, b in zip(ms, ms[1:]))
            self.assertLessEqual(off, bound, (p, ms))
        return ms

    def test_curvy_drags_keep_their_shape(self):
        # drawing, Markup, swipe-typing and scrubbing: a fixed move count used to turn a circle into a triangle
        # and drop the last two legs of a zigzag. Curvy drags may use up to MAX_CURVE_MOVES moves instead.
        circle = [(196 + 80 * math.cos(2 * math.pi * i / 150), 426 + 80 * math.sin(2 * math.pi * i / 150),
                   1.2 * i / 150) for i in range(151)]                  # r = 80, 1.2 s, back to the start
        w = g.drag_to_gesture(circle, PHONE)
        self.check_valid(circle, w)
        ms = self.check_shape(circle, w, g.CURVE_PT + 1)                  # +1: moves are rounded to whole points
        self.assertLessEqual(len(ms) - 1, g.MAX_CURVE_MOVES)
        self.assertLessEqual(moves(w)[-1]["duration"], g.LIFT_MS)
        zigzag, xs = [], (300, 100, 300, 100, 300)                        # x back and forth while y goes 300 -> 500
        for k in range(4):
            leg = line(xs[k], 300 + 50 * k, xs[k + 1], 350 + 50 * k, 300, t0=0.3 * k)
            zigzag += leg if k == 0 else leg[1:]
        w = g.drag_to_gesture(zigzag, PHONE)
        self.check_valid(zigzag, w)
        ms = self.check_shape(zigzag, w, g.CURVE_PT + 1)
        turns = [i for i in range(1, len(ms) - 1) if (ms[i][0] - ms[i - 1][0]) * (ms[i + 1][0] - ms[i][0]) < 0]
        self.assertEqual(len(turns), 3, ms)                               # all four legs are there
        scrub = line(300, 400, 100, 400, 300) + line(100, 400, 300, 400, 300, t0=0.3)[1:] \
            + line(300, 400, 100, 400, 300, t0=0.6)[1:]                  # on one line: only the timing shows it
        xs = [m["x"] for m in moves(g.drag_to_gesture(scrub, PHONE))]
        self.assertEqual((min(xs), max(xs), xs[-1]), (100, 300, 100))
        self.assertTrue(any(a > b < c for a, b, c in zip(xs, xs[1:], xs[2:])), xs)     # there, back and there
        # _simplify itself: every point within CURVE_PT of the move that is replayed at its time
        for pts in ([(x, y, t * 1000) for x, y, t in circle], [(x, y, t * 1000) for x, y, t in zigzag]):
            out = g._simplify(pts)
            self.assertLessEqual(len(out) - 1, g.MAX_CURVE_MOVES)
            for p in pts:
                a, b = next((a, b) for a, b in zip(out, out[1:]) if a[2] <= p[2] <= b[2])
                self.assertLessEqual(g._off(p, a, b), g.CURVE_PT, p)
        # still few moves for a straight swipe that speeds up, slows down or wobbles by a few points
        wobble = [(300 - 240 * i / 80, 400 + 4 * math.sin(i / 3), 0.6 * i / 80) for i in range(81)]
        self.assertLessEqual(len(moves(g.drag_to_gesture(wobble, PHONE))), g.MAX_SWIPE_MOVES + 1)

    def test_simplify_keeps_shape_timing_and_forced_points(self):
        pts = [(i * 2.0, 0.0, i * 10) for i in range(30)]           # steady: only the ends are needed
        self.assertEqual(g._simplify(pts), [pts[0], pts[-1]])
        self.assertEqual(g._simplify(pts, keep=(7,)), [pts[0], pts[7], pts[-1]])
        # same path, but slow then fast: the change of speed is kept although the path is straight
        pts = [(i * 1.0, 0.0, i * 10) for i in range(20)] + [(19 + i * 10.0, 0.0, 190 + i * 10) for i in range(1, 11)]
        out = g._simplify(pts)
        self.assertIn(pts[19], out)
        for x, y, t in pts:
            self.assertLessEqual(math.dist(at(out, t), (x, y)), g.SIMPLIFY_PT)
        zigzag = [(i * 10.0, (i % 2) * 40.0, i * 10) for i in range(40)]
        self.assertEqual(len(g._simplify(zigzag)) - 1, g.MAX_CURVE_MOVES)    # capped
        # straight but speeding up hard: the timing is off by more than CURVE_PT, the path is not: no extra moves
        flick = [(300 - 240 * (i / 30) ** 3, 400.0, i * 10) for i in range(31)]
        self.assertEqual(len(g._simplify(flick)) - 1, g.MAX_SWIPE_MOVES)
        self.assertEqual(g._off((5, 5), (0, 0), (0, 0)), math.hypot(5, 5))
        self.assertEqual(g._off((-3, 4), (0, 0), (10, 0)), 5)                    # past an end: to that end
        self.assertEqual(g._simplify([(1, 2, 0)]), [(1, 2, 0)])
        self.assertEqual(g._sed((5, 5, 0), (0, 0, 0), (0, 0, 0)), math.hypot(5, 5))   # zero-length move

    def test_cut_splits_a_path_at_a_time(self):
        before, after = g._cut([(0, 0, 0), (100, 0, 100), (100, 50, 200)], 150)
        self.assertEqual(before, [(0, 0, 0), (100, 0, 100), (100, 25, 150)])
        self.assertEqual(after, [(100, 25, 150), (100, 50, 200)])
        before, after = g._cut([(0, 0, 0), (100, 0, 100)], 100)
        self.assertEqual((before[-1], after), ((100, 0, 100), [(100, 0, 100)]))

    def test_random_drags_are_valid_and_never_slower(self):
        rnd = random.Random(7)
        for _ in range(400):
            x, y, t = rnd.uniform(20, 370), rnd.uniform(20, 830), 0.0
            track = [(x, y, t)]
            for _ in range(rnd.randint(2, 80)):
                x = min(392, max(1, x + rnd.uniform(-25, 25)))
                y = min(851, max(1, y + rnd.uniform(-25, 25)))
                t += rnd.choice([0.004, 0.012, 0.03, 0.08, 0.3])
                track.append((x, y, t))
            w = g.drag_to_gesture(track, PHONE if rnd.random() < 0.5 else None)
            for s in acts(w):
                if "duration" in s:
                    self.assertIsInstance(s["duration"], int)
                    self.assertGreaterEqual(s["duration"], 0)
            if len(moves(w)) > 1 and acts(w)[2]["type"] != "pause":     # a swipe (not a tap or hold-then-drag)
                tl = self.check_valid(track, w)
                self.assertLessEqual(len(moves(w)), g.MAX_CURVE_MOVES + 1)
                shaped = g.shape_swipe(real(track), PHONE)
                if shaped and not shaped[1]:                              # released moving: capped
                    self.assertLessEqual(tl[-1][2], g.MAX_REPLAY_MS)
                if not (shaped and shaped[1]) and tl[-1][2] > g.LIFT_MS + g.MIN_SEGMENT_MS:
                    self.assertLessEqual(moves(w)[-1]["duration"], g.LIFT_MS)


if __name__ == "__main__":
    unittest.main()
