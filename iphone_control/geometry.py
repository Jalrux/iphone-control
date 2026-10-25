"""Pure helpers: window pixels -> phone points, and the W3C touch actions WebDriverAgent runs.

No GTK, no network - unit-tested in tests/test_geometry.py.
W3C actions run atomically on the phone, so a mouse drag is replayed in one go on release - quickly:
shape_swipe() keeps only the end of a swipe at the user's speed (that is what iOS reads) and hurries the rest.

Measured on an iPhone 15 (iOS 26.5, WDA 16.15): before the finger goes down, XCTest resolves every pointerMove
of a request with two accessibility snapshots of the app in front (FBW3CActionsSynthesizer / hitpointWithElement):
about 27 ms a move at snapshotMaxDepth 1 (wda.INPUT_SETTINGS) and about 110 ms at WDA's default of 50. So a swipe
is sent with as few moves as keep its path and timing (_simplify): at depth 1 a 2-move swipe touched down after
~70 ms; at depth 50 a 14-move one took 1.6 s.
Keep plain viewport pointerMoves in W3C /actions: pointer-origin moves cost the same, /wda/dragfromtoforduration
and /wda/pressAndDragWithVelocity more (6 snapshots, ~550 ms), and a request of pauses only takes ~5 s.
One request is one gesture: all its touches start at the same moment (WDA and XCTest ignore leading pauses and
path offsets; iPhone 15 Pro, iOS 26.5.2, Oct 2026), so queued gestures cannot be merged into one request - each
builder here makes one gesture, and each gesture is sent as a request of its own.
"""
import bisect
import math

TAP_MAX_MOVE = 10        # points; a press that moves less than this is a tap, not a swipe
LONG_PRESS_MS = 450      # held longer than this -> long press (or hold-then-drag)
MAX_SWIPE_POINTS = 12    # intermediate points kept from a hold-then-drag
MAX_SWIPE_MOVES = 3      # a swipe or flick is sent with at most this many moves after the first one...
SIMPLIFY_PT = 3          # ...and points are dropped while the replay stays this close to the real drag in time
MAX_CURVE_MOVES = 12     # a curvy drag (a circle, a zigzag, swipe-typing) may use up to this many moves...
CURVE_PT = 3 * SIMPLIFY_PT   # ...while a point of it is further than this from the line replayed through it
LIFT_MS = 50             # the finger's speed over this last stretch before lifting (what makes a fling)...
LIFT_TOLERANCE = 0.15    # ...stays within this fraction of the drag's own (a move is spent on it if needed)
SCROLL_PT = 60           # points of finger travel per mouse-wheel notch
MIN_SCROLL_PT = 20       # shorter scroll swipes would be taps (iOS touch slop is ~10 pt)
PHONE_ASPECT = 393 / 852     # portrait width / height until WDA reports the real size

# Swipe replay (shape_swipe). A drag only reaches the phone on release, so a slow drag used to wait its own
# length again before the phone finished. iOS decides fling / no fling and next page / same page from where the
# finger lifts and how fast it was moving in the last few touches, so only that tail is replayed at real speed.
MAX_REPLAY_MS = 250      # a swipe no longer than this is replayed as it was; longer ones are shaped
TAIL_MS = 100            # the last part of the movement, replayed at the user's speed: the lift-off velocity
MAX_HEAD_MS = MAX_REPLAY_MS - TAIL_MS    # everything before the tail is replayed within this...
HEAD_SPEED = 2.5         # ...at about this many points per ms (a brisk finger), never slower than it was
MIN_SEGMENT_MS = 10      # shortest move in a shaped swipe, so iOS sees every point of a curved path
REST_PT = 6              # |dx| + |dy| within this of the release point = still (the window's sampling step)
REST_MS = 100            # still for this long before release = the user stopped: lift with no velocity
REST_SETTLE_MS = 40      # a stop's last few points of drift are crossed this slowly (no fling)...
REST_HOLD_MS = 120       # ...then the finger rests this long at least before lifting, like scroll()
MAX_REST_MS = 300        # a longer stop is shortened to this...
EDGE_REST_MS = 1000      # ...but kept up to this from the bottom edge, where a hold opens the App Switcher
BOTTOM_EDGE_PT = 25      # a drag starting this close to the bottom starts on the home indicator


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def letterbox(widget_w, widget_h, video_w, video_h):
    """Where a video_w x video_h frame is drawn inside the widget (aspect kept, centred).

    Returns (offset_x, offset_y, scale).
    """
    scale = min(widget_w / video_w, widget_h / video_h)
    return (widget_w - video_w * scale) / 2, (widget_h - video_h * scale) / 2, scale


def oriented(phone_size, video_size):
    """phone_size (points, either orientation) turned to match the picture's orientation."""
    pw, ph = phone_size
    if video_size and (video_size[0] > video_size[1]) != (pw > ph):
        return ph, pw
    return pw, ph


def to_phone(x, y, widget_size, video_size, phone_size):
    """Widget pixel (x, y) -> phone point, or None outside the picture.

    Widget and event coordinates are both logical pixels, so the HiDPI scale cancels out.
    If the picture is landscape the phone has been turned, so the axes swap.
    """
    vw, vh = video_size
    ox, oy, scale = letterbox(widget_size[0], widget_size[1], vw, vh)
    u, v = (x - ox) / (vw * scale), (y - oy) / (vh * scale)
    if not (0 <= u <= 1 and 0 <= v <= 1):
        return None
    pw, ph = oriented(phone_size, video_size)
    return u * pw, v * ph


def window_size(work_w, work_h, aspect=PHONE_ASPECT, extra_h=40, fill=0.9):
    """Default window size (logical px) for a monitor work area: phone-shaped, 90 % of the height.

    aspect = picture width / height; extra_h = the toolbar under the picture.
    """
    h = int(work_h * fill)
    w = int((h - extra_h) * aspect)
    if w > work_w * fill:                       # landscape or a narrow screen: fit the width
        w = int(work_w * fill)
        h = int(w / aspect) + extra_h
    return max(w, 1), max(h, 1)


def _pointer(actions, finger="finger1"):
    return {"type": "pointer", "id": finger, "parameters": {"pointerType": "touch"}, "actions": actions}


def _one(actions):
    return {"actions": [_pointer(actions)]}


def _move(x, y, ms=0):
    return {"type": "pointerMove", "duration": int(ms), "x": round(x), "y": round(y)}


def _down():
    return {"type": "pointerDown", "button": 0}


def _up():
    return {"type": "pointerUp", "button": 0}


def _pause(ms):
    return {"type": "pause", "duration": int(ms)}


def gesture(points, hold_ms=0, release_ms=0):
    """W3C actions for one finger: down at points[0], optional hold, move through the rest, optional rest, up.

    points = [(x, y, ms_since_start), ...] in phone points; release_ms = still before lifting (no fling).
    """
    x0, y0, _ = points[0]
    acts = [_move(x0, y0), _down()]
    if hold_ms:
        acts.append(_pause(hold_ms))
    last_t = 0
    for x, y, t in points[1:]:
        acts.append(_move(x, y, max(10, int(t - last_t))))
        last_t = t
    if release_ms:
        acts.append(_pause(release_ms))
    acts.append(_up())
    return _one(acts)


def tap(x, y, hold_ms=0):
    return gesture([(x, y, 0)], hold_ms=hold_ms or 40)


def double_tap(x, y):
    """Two taps 80 ms apart in one request, inside iOS's double-tap window (zoom in Maps, Photos; in a text field
    it selects a word). For the window's optional double-click hold-back only: two tap requests sent back to back
    land ~330 ms apart, and iOS reads those as a double tap too (10/10 in a text field; with 50 ms more between
    them, two single taps; iPhone 15 Pro, iOS 26.5.2, Oct 2026), so a click can be sent at once."""
    return _one([_move(x, y), _down(), _pause(30), _up(), _pause(80), _down(), _pause(30), _up()])


def _thin(track):
    step = max(1, len(track) // MAX_SWIPE_POINTS)
    pts = track[::step]
    if pts[-1] is not track[-1]:
        pts.append(track[-1])
    return pts


def _dist(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _cut(pts, t):
    """Split a path [(x, y, ms), ...] at time t (inside it): (up to t, from t), both with the point at t."""
    i = next(i for i, p in enumerate(pts) if p[2] >= t)
    if pts[i][2] == t:
        return pts[:i + 1], pts[i:]
    (xa, ya, ta), (xb, yb, tb) = pts[i - 1], pts[i]
    f = (t - ta) / (tb - ta)
    q = (xa + (xb - xa) * f, ya + (yb - ya) * f, t)
    return pts[:i] + [q], [q] + pts[i:]


def _spaced(pts):
    """Points [(x, y, ms), ...] keeping their timing, minus any closer than MIN_SEGMENT_MS to the one kept before
    (gesture() would stretch such a move to 10 ms and slow a quick flick down); first and last kept."""
    t0, kept = pts[0][2], [pts[0]]
    for p in pts[1:]:
        if round(p[2] - t0) - round(kept[-1][2] - t0) >= MIN_SEGMENT_MS:
            kept.append(p)
    if kept[-1] is not pts[-1]:
        kept[-1:] = [pts[-1]] if len(kept) > 1 else [kept[-1], pts[-1]]
    return [(x, y, round(t - t0)) for x, y, t in kept]


def _sed(p, a, b):
    """How far p is from where a steady straight move from a to b is at p's time (points)."""
    f = (p[2] - a[2]) / (b[2] - a[2]) if b[2] > a[2] else 0
    return math.hypot(a[0] + (b[0] - a[0]) * f - p[0], a[1] + (b[1] - a[1]) * f - p[1])


def _off(p, a, b):
    """How far p is from the straight line a-b (the segment, not the whole line), timing ignored (points)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    f = _clamp(((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy), 0, 1) if dx or dy else 0
    return math.hypot(a[0] + dx * f - p[0], a[1] + dy * f - p[1])


def _at(pts, t):
    """Position on a path [(x, y, ms), ...] at time t (clamped to its ends)."""
    if t <= pts[0][2]:
        return pts[0][:2]
    if t >= pts[-1][2]:
        return pts[-1][:2]
    q = _cut(pts, t)[1][0]
    return q[0], q[1]


def _lift_speed(pts):
    end = pts[-1]
    return math.dist(_at(pts, end[2] - LIFT_MS), end[:2]) / LIFT_MS


def _simplify(pts, keep=(), lift=False):
    """The fewest of points [(x, y, ms), ...] whose straight, steady moves stay within SIMPLIFY_PT of every
    point at its own time (so a curve keeps its shape), at most MAX_SWIPE_MOVES moves (more for a curvy drag,
    see _greedy); first, last and the indexes in keep always stay. Every move costs time on the phone (see the
    top). lift (without keep), for a finger released while moving: the last move is never longer than LIFT_MS
    and the speed over the last LIFT_MS stays within LIFT_TOLERANCE of the drag's, so the replay flings (or
    not) like the drag. One slow 100 ms last move changed the home-screen page only 14 times in 23 on the
    phone; the same path ending in 50 ms moves did every time (12/12)."""
    out = _greedy(pts, keep)
    if not lift or keep or pts[-1][2] - pts[0][2] <= LIFT_MS + MIN_SEGMENT_MS:
        return out
    want = _lift_speed(pts)
    if out[-1][2] - out[-2][2] <= LIFT_MS and abs(_lift_speed(out) - want) <= LIFT_TOLERANCE * max(want, 0.1):
        return out
    # keep the point LIFT_MS before the end, minus the points closer than MIN_SEGMENT_MS to it
    t = pts[-1][2] - LIFT_MS
    q = _cut(pts, t)[0][-1]
    ok = [p for p in pts[1:-1] if abs(p[2] - t) >= MIN_SEGMENT_MS]
    before = [p for p in ok if p[2] < t]
    pts = [pts[0]] + before + [q] + ok[len(before):] + [pts[-1]]
    return _greedy(pts, {len(before) + 1})


def _greedy(pts, keep):
    """Douglas-Peucker in time: add the point furthest from where the replay is at its time until all are
    within SIMPLIFY_PT or MAX_SWIPE_MOVES moves are used. Then, up to MAX_CURVE_MOVES, only the shape counts:
    go on while a point is further than CURVE_PT from the move replayed through it (_off), so a circle, a
    scrub or swipe-typing keeps its turns, while a straight swipe that speeds up never pays for more moves
    (each costs ~27 ms on the phone)."""
    kept = sorted({0, len(pts) - 1, *keep})
    while len(kept) - 1 < MAX_CURVE_MOVES:
        few = len(kept) - 1 < MAX_SWIPE_MOVES
        worst, at = SIMPLIFY_PT if few else CURVE_PT, None
        for i, j in zip(kept, kept[1:]):
            for m in range(i + 1, j):
                d = (_sed if few else _off)(pts[m], pts[i], pts[j])
                if d > worst:
                    worst, at = d, m
        if at is None:
            break
        bisect.insort(kept, at)
    return [pts[i] for i in kept]


def _hurry(head):
    """The head of a swipe (points with real ms) -> the same path at HEAD_SPEED, within MAX_HEAD_MS and never
    slower than it was. Waits on the way (the press before moving off) vanish; each move >= MIN_SEGMENT_MS."""
    last = len(head) - 1
    path = [head[0]] + [p for i, p in enumerate(head[1:], 1) if i == last or _dist(p, head[i - 1]) >= 0.5]
    length = sum(_dist(a, b) for a, b in zip(path, path[1:]))
    if length < 1:
        return [(head[0][0], head[0][1], 0)]
    budget = int(max(MIN_SEGMENT_MS, min(head[-1][2], MAX_HEAD_MS, length / HEAD_SPEED)))
    # the head's timing is made up anyway, so only its shape counts: simplify it as if moved at one speed
    done, steady = 0, [(path[0][0], path[0][1], 0)]
    for a, b in zip(path, path[1:]):
        done += math.dist(a[:2], b[:2])
        steady.append((b[0], b[1], done))
    path = _simplify(steady)
    n = min(len(path) - 1, budget // MIN_SEGMENT_MS)
    if len(path) - 1 > n:                      # evenly spaced points, first and last kept
        path = [path[round(i * (len(path) - 1) / n)] for i in range(n + 1)]
    extra, done, out = budget - n * MIN_SEGMENT_MS, 0, [(path[0][0], path[0][1], 0)]
    for i, (a, b) in enumerate(zip(path, path[1:]), 1):
        done += _dist(a, b)
        out.append((b[0], b[1], i * MIN_SEGMENT_MS + round(extra * done / length)))
    return out


def shape_swipe(points, size=None):
    """A swipe's points [(x, y, ms), ...] (first at 0) -> (points, rest_ms) to replay quickly, or None to replay
    it as it was (it was quick already, or shaping would not be quicker).

    The last TAIL_MS of the movement keeps its real timing, so iOS sees the same lift-off velocity (fling or
    not, next page or not); the path before it is hurried (_hurry), so the replay takes at most MAX_REPLAY_MS.
    A drag that stopped for REST_MS before release ends with rest_ms of stillness before lifting, so it still
    does not fling; long stops are shortened to MAX_REST_MS (EDGE_REST_MS from the bottom edge of size).
    """
    end = points[-1]
    if end[2] <= MAX_REPLAY_MS:
        return None
    k = len(points) - 1                        # the stop: the first of the points still near the release point
    while k > 0 and _dist(points[k - 1], end) <= REST_PT:
        k -= 1
    if k == 0:                                 # it never left the release point: a jiggle, not a swipe
        return None
    rest = end[2] - points[k][2]
    if rest < REST_MS:                         # released while moving
        k, rest = len(points) - 1, 0
    move = points[:k + 1]
    start = max(0, move[-1][2] - TAIL_MS)
    if start > 0:
        head, tail = _cut(move, start)
        out = _hurry(head)
    else:
        tail, out = move, [(move[0][0], move[0][1], 0)]
    t0 = out[-1][2]
    out += [(x, y, t0 + t) for x, y, t in _spaced(tail)[1:]]
    stop = len(out) - 1                        # where the movement ends (before a stop's drift)
    hold = 0
    if rest:
        edge = size and points[0][1] >= size[1] - BOTTOM_EDGE_PT
        keep = min(rest, EDGE_REST_MS if edge else MAX_REST_MS)
        if _dist(points[k], end) >= 1:         # the drift after the stop
            out.append((end[0], end[1], out[-1][2] + REST_SETTLE_MS))
            keep -= REST_SETTLE_MS
        hold = int(max(REST_HOLD_MS, keep))
    if out[-1][2] + hold >= end[2]:
        return None
    return _simplify(out, keep=(stop,) if rest else (), lift=not rest), hold


def drag_to_gesture(track, size=None):
    """A mouse drag [(x, y, seconds), ...] -> W3C actions: tap, long press, swipe or hold-then-drag.

    Swipes are shaped by shape_swipe() and sent with few moves (_simplify); size = the phone's size in points
    (oriented), for the bottom edge. A hold-then-drag keeps up to MAX_SWIPE_POINTS points at the real speed.
    """
    x0, y0, t0 = track[0]
    far = next((i for i, (x, y, _) in enumerate(track)
                if abs(x - x0) + abs(y - y0) >= TAP_MAX_MOVE), None)
    if far is None:
        held_ms = (track[-1][2] - t0) * 1000
        return tap(x0, y0, hold_ms=held_ms if held_ms > LONG_PRESS_MS else 0)
    # time spent still before moving away: the last point near the start, else the first far one
    dwell = ((track[far - 1][2] if far > 1 else track[far][2]) - t0) * 1000
    if dwell > LONG_PRESS_MS:              # hold, then drag (move an icon, a selection handle)
        pts = _thin(track[far:])
        return gesture([(x0, y0, 0)] + [(x, y, (t - t0) * 1000 - dwell) for x, y, t in pts],
                       hold_ms=dwell)
    shaped = shape_swipe([(x, y, (t - t0) * 1000) for x, y, t in track], size)
    if shaped:
        return gesture(shaped[0], release_ms=shaped[1])
    return gesture(_simplify(_spaced([(x, y, (t - t0) * 1000) for x, y, t in track]), lift=True))


def _fit(v, travel, lo, hi):
    """Start near v so that v + travel stays inside [lo, hi]; travel is clamped first."""
    travel = _clamp(travel, lo - hi, hi - lo)
    return _clamp(v, lo - min(travel, 0), hi - max(travel, 0)), travel


def scroll(x, y, dy, size, dx=0, per_unit=SCROLL_PT):
    """Scroll by (dx, dy) units at (x, y): dy > 0 = wheel down = content moves up = finger swipes up.

    The swipe starts near the cursor but away from the edges (the home indicator would go Home, the
    top edge would open Notification Centre), and pauses before lifting so iOS does not fling: each
    unit scrolls a predictable per_unit points.
    """
    w, h = size
    sx, tx = _fit(x, -dx * per_unit, w * 0.05, w * 0.95)
    sy, ty = _fit(y, -dy * per_unit, h * 0.12, h * 0.88)
    return _one([_move(sx, sy), _down(), _move(sx + tx, sy + ty, 250), _pause(120), _up()])


def pinch(cx, cy, scale, size, spread=40, ms=300):
    """Two fingers either side of (cx, cy): scale > 1 spreads them (zoom in), < 1 pinches (zoom out)."""
    w, h = size
    scale = _clamp(scale, 0.2, 5)
    start, end = (spread, spread * scale) if scale >= 1 else (spread / scale, spread)
    y = _clamp(cy, 2, h - 2)
    fingers = []
    for name, sign in (("finger1", -1), ("finger2", 1)):
        x0 = _clamp(cx + sign * start, 2, w - 2)
        x1 = _clamp(cx + sign * end, 2, w - 2)
        fingers.append(_pointer([_move(x0, y), _down(), _move(x1, y, ms), _up()], name))
    return {"actions": fingers}


def back_swipe(size):
    """Swipe in from the left edge: "back" in most iOS apps."""
    w, h = size
    return gesture([(2, h * 0.5, 0), (w * 0.6, h * 0.5, 250)])


def edge_swipe(kind, phone_size):
    """System gestures that start at the screen edge."""
    w, h = phone_size
    if kind == "app_switcher":       # flick off the home indicator, slow down, hold, lift
        # one even 350 ms move does nothing on iOS 26.5; the quick first leg is what iOS reads as the edge swipe
        return _one([_move(w / 2, h - 1), _down(), _move(w / 2, h * 0.8, 150), _move(w / 2, h * 0.62, 350),
                     _pause(1000), _up()])
    if kind == "control_centre":     # down from the top-right corner
        return gesture([(w - 30, 2, 0), (w - 30, h * 0.45, 250)])
    if kind == "notifications":      # down from the top-left
        return gesture([(40, 2, 0), (40, h * 0.6, 250)])
    raise ValueError(kind)
