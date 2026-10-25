"""The capture process (iphone_control/screencap.py) against a fake iPhone (tests/valeria_fake.py): the socket
server, finding and resetting the phone, whole sessions, the stall watchdog, exit codes and signals. No USB device
or phone is touched; the waits are shortened (valeria_fake.FAST)."""
import contextlib
import io
import json
import os
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import valeria_fake as F  # noqa: E402
from support import UDID_A, UDID_B  # noqa: E402
from iphone_control import screencap as S  # noqa: E402
from iphone_control import valeria as V  # noqa: E402


def until(test, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(0.01)
    return bool(test())


class Client:
    """A socket client that collects (kind, pts, payload) in a thread."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.frames, self.closed = [], threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for f in S.read_frames(self.sock):
                self.frames.append(f)
        except OSError:
            pass
        self.closed.set()

    def kinds(self, kind):
        return [p for k, _, p in list(self.frames) if k == kind]

    def counters(self):
        return [F.counter_of(p[1:]) for p in self.kinds(S.VIDEO)]

    def close(self):
        """Like a client process ending: the reader thread's recv() would otherwise keep the connection open."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def video(counter, keyframe, size=None):
    fmt = {"media": "video", "width": size[0], "height": size[1], "codec": "avc1",
           "avcc": F.make_avcc(*size)} if size else None
    return V.Video(F.access_unit(counter, keyframe), counter * 1000, keyframe, fmt)


class Frames(unittest.TestCase):
    def test_frames_survive_any_split(self):
        a, b = socket.socketpair()
        data = S.frame(S.STATUS, "hello".encode(), 7) + S.frame(S.VIDEO, b"\x01" + bytes(100000), -1) + \
            S.frame(S.STATUS, b"")

        def send():
            for i in range(0, len(data), 997):
                a.sendall(data[i:i + 997])
            a.sendall(S.frame(S.VIDEO, b"cut")[:-1])         # the last one never finishes
            a.close()
        threading.Thread(target=send, daemon=True).start()
        got = list(S.read_frames(b))
        self.assertEqual([(k, pts, len(p)) for k, pts, p in got], [(S.STATUS, 7, 5), (S.VIDEO, -1, 100001),
                                                                   (S.STATUS, 0, 0)])
        b.close()


class Server(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="capsock-")
        self.path = os.path.join(self.dir, "cap.sock")
        self.logs = []
        self.server = S.Server(self.path, self.logs.append)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.close()
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def client(self):
        """A client the server has taken (so the next event reaches it)."""
        n = len(self.server.clients)
        c = Client(self.path)
        self.clients.append(c)
        self.assertTrue(until(lambda: len(self.server.clients) > n))
        return c

    def test_socket_is_private_and_a_stale_one_is_replaced(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertTrue(stat.S_ISSOCK(os.stat(self.path).st_mode))
        self.server.close()
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(self.path)                               # what a killed capture process leaves behind
        stale.close()
        self.server = S.Server(self.path, self.logs.append)
        c = self.client()
        self.server.status("up")
        self.assertTrue(until(lambda: c.kinds(S.STATUS) == [b"up"]))

    def test_other_files_are_not_replaced(self):
        other = os.path.join(self.dir, "notes.txt")
        with open(other, "w") as f:
            f.write("keep me")
        with self.assertRaises(OSError):
            S.Server(other, self.logs.append)
        with open(other) as f:
            self.assertEqual(f.read(), "keep me")

    def test_close_removes_only_its_own_socket(self):
        self.server.close()
        self.assertFalse(os.path.exists(self.path))
        self.server = S.Server(self.path, self.logs.append)
        os.unlink(self.path)
        theirs = socket.socket(socket.AF_UNIX)
        theirs.bind(self.path)                              # another process took the path since
        self.addCleanup(theirs.close)
        self.server.close()
        self.assertTrue(os.path.exists(self.path))

    def test_a_late_client_gets_the_format_and_the_frames_since_the_last_keyframe(self):
        for ev in (video(0, True, (1180, 2556)), video(1, False), video(2, False), video(3, True), video(4, False)):
            self.server.event(ev)
        c = self.client()
        self.assertTrue(until(lambda: len(c.kinds(S.VIDEO)) == 2))
        self.server.event(video(5, False))
        self.assertTrue(until(lambda: len(c.kinds(S.VIDEO)) == 3))
        self.assertEqual([k for k, _, _ in c.frames], [S.VIDEO_FORMAT, S.VIDEO, S.VIDEO, S.VIDEO])
        fmt = c.kinds(S.VIDEO_FORMAT)[0]
        self.assertEqual(struct.unpack_from("<II", fmt), (1180, 2556))
        self.assertEqual(fmt[8:], F.make_avcc(1180, 2556))
        self.assertEqual(c.counters(), [3, 4, 5])
        self.assertEqual([p[0] for p in c.kinds(S.VIDEO)], [1, 0, 0])            # the keyframe flag
        pts = [t for _, t, _ in c.frames]
        self.assertEqual(pts, sorted(pts))
        self.assertTrue(all(abs(t - time.monotonic_ns()) < 10 ** 10 for t in pts))   # CLOCK_MONOTONIC ns

    def test_a_client_without_a_keyframe_to_replay_waits_for_the_next(self):
        a = self.client()
        self.server.event(video(0, False))                  # before any keyframe: nothing it could decode
        self.server.event(video(1, True, (8, 16)))
        self.server.event(video(2, False))
        self.assertTrue(until(lambda: a.counters() == [1, 2]))
        old = S.CLIENT_BACKLOG
        S.CLIENT_BACKLOG = 2 * len(F.access_unit(9, False)) + 40      # the group outgrows what is kept to replay
        self.addCleanup(setattr, S, "CLIENT_BACKLOG", old)
        self.server.event(video(3, False))
        self.assertEqual(self.server.gop, [])
        b = self.client()
        self.server.event(video(4, False))
        self.server.event(video(5, True))
        self.assertTrue(until(lambda: b.counters() == [5]))
        self.assertEqual(b.frames[0][0], S.VIDEO_FORMAT)

    def test_several_clients_at_once_each_with_its_replay(self):
        self.server.event(video(0, True, (1180, 2556)))
        a = self.client()
        self.assertTrue(until(lambda: a.kinds(S.VIDEO)))
        b = self.client()                                   # the latency probe beside the window
        self.server.event(video(1, False))
        self.assertTrue(until(lambda: b.counters() == [0, 1]))
        self.assertTrue(until(lambda: a.counters() == [0, 1]))
        self.assertFalse(a.closed.is_set())
        a.close()                                           # one leaving does not disturb the other
        self.server.event(video(2, False))                  # (a client that left is noticed at its next frame)
        self.assertTrue(until(lambda: b.counters() == [0, 1, 2]))
        self.server.event(video(3, False))
        self.assertTrue(until(lambda: len(self.server.clients) == 1))

    def test_more_than_max_clients_are_refused(self):
        for _ in range(S.MAX_CLIENTS):
            self.client()
        extra = Client(self.path)
        self.clients.append(extra)
        self.assertTrue(extra.closed.wait(5))
        self.assertEqual(len(self.server.clients), S.MAX_CLIENTS)
        self.assertIn(f"capture socket: more than {S.MAX_CLIENTS} clients - refusing one", self.logs)

    def test_clients_reconnect_any_number_of_times(self):
        self.server.event(video(0, True, (8, 16)))
        for n in range(1, 6):
            c = self.client()
            self.server.event(video(n, False))
            self.assertTrue(until(lambda: c.counters() == list(range(n + 1))), c.counters())
            c.close()
        self.server.event(video(6, False))                  # a client that left is noticed at its next frame
        self.assertTrue(until(lambda: not self.server.clients))
        self.assertEqual(self.logs, [])

    def test_a_rotation_starts_the_replay_again_and_a_repeated_format_is_sent_once(self):
        a = self.client()
        self.server.event(video(0, True, (1180, 2556)))
        self.server.event(video(1, True, (1180, 2556)))     # the same format again
        self.server.event(video(2, False))
        self.server.event(video(3, True, (2556, 1180)))     # landscape
        self.assertTrue(until(lambda: len(a.kinds(S.VIDEO)) == 4))
        self.assertEqual([struct.unpack_from("<II", f) for f in a.kinds(S.VIDEO_FORMAT)], [(1180, 2556), (2556, 1180)])
        b = self.client()
        self.assertTrue(until(lambda: b.kinds(S.VIDEO)))
        self.assertEqual([struct.unpack_from("<II", f) for f in b.kinds(S.VIDEO_FORMAT)], [(2556, 1180)])
        self.assertEqual(b.counters(), [3])

    def test_status_and_the_last_one_for_new_clients(self):
        self.server.status("Starting a new capture...")
        a = self.client()
        self.assertTrue(until(lambda: a.kinds(S.STATUS) == [b"Starting a new capture..."]))
        self.server.status("")
        self.assertTrue(until(lambda: a.kinds(S.STATUS)[-1:] == [b""]))
        b = self.client()
        self.server.event(video(0, True, (8, 16)))
        self.assertTrue(until(lambda: b.kinds(S.VIDEO)))
        self.assertEqual(b.kinds(S.STATUS), [])                                   # cleared: nothing to replay

    def test_audio_frames(self):
        a = self.client()
        for i in range(3):
            self.server.event(V.Audio(bytes(16), i, dict(V.default_asbd(), sample_rate=44100.0)))
        self.assertTrue(until(lambda: len(a.kinds(S.AUDIO)) == 3))
        self.assertEqual([struct.unpack("<dII", f) for f in a.kinds(S.AUDIO_FORMAT)], [(44100.0, 2, 16)])
        b = self.client()
        self.assertTrue(until(lambda: b.kinds(S.AUDIO_FORMAT)))

    def fill(self, client_sock, size):
        """Push frames until the server's send thread is stuck on a client that does not read."""
        client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.server.event(video(0, True, (8, 16)))
        filler = struct.pack(">I", size) + b"\x0c" + bytes(size - 1)                # NAL unit type 12: filler data
        big = V.Video(F.access_unit(1, False) + filler, 0, False)
        for _ in range(40):
            self.server.event(big)
            time.sleep(0.01)
        return big

    def test_a_client_that_does_not_read_is_cut_off(self):
        old = S.CLIENT_BACKLOG
        S.CLIENT_BACKLOG = 300_000                          # the replay is kept below half of it
        self.addCleanup(setattr, S, "CLIENT_BACKLOG", old)
        stuck = socket.socket(socket.AF_UNIX)
        stuck.connect(self.path)
        self.addCleanup(stuck.close)
        self.assertTrue(until(lambda: self.server.clients))
        self.fill(stuck, 50_000)
        self.assertTrue(until(lambda: not self.server.clients))
        self.assertIn("capture client too slow - disconnecting it", self.logs)
        self.assertLessEqual(sum(map(len, self.server.gop)), 150_000)
        self.server.event(video(7, True))
        c = self.client()                                   # it reconnects and starts at the last keyframe
        self.server.event(video(8, False))
        self.assertTrue(until(lambda: c.counters() == [7, 8]))

    def test_a_send_stuck_for_the_lag_limit_ends_the_client(self):
        old = S.CLIENT_LAG_S
        S.CLIENT_LAG_S = 0.3
        self.addCleanup(setattr, S, "CLIENT_LAG_S", old)
        stuck = socket.socket(socket.AF_UNIX)
        stuck.connect(self.path)
        self.addCleanup(stuck.close)
        self.assertTrue(until(lambda: self.server.clients))
        self.fill(stuck, 100_000)                           # then nothing more: the send itself times out
        self.assertTrue(until(lambda: not self.server.clients, 3))

    def test_close_ends_its_threads(self):
        names = lambda: {t.name for t in threading.enumerate() if t.name in ("cap-accept", "cap-send")}  # noqa
        self.client()                                       # a client brings its own send thread
        self.assertTrue(until(lambda: len(names()) >= 2))
        before = threading.active_count()
        self.server.close()
        self.assertTrue(until(lambda: threading.active_count() <= before - 2))


class Phone(unittest.TestCase):
    """A FakePhone in a FakeSysfs, with the short waits."""

    def setUp(self):
        self.sysfs = F.FakeSysfs()
        self.addCleanup(self.sysfs.cleanup)
        timing = F.fast_timing()
        timing.__enter__()
        self.addCleanup(timing.__exit__, None, None, None)
        self.logs = []

    def phone(self, **knobs):
        self.p = F.FakePhone(self.sysfs, **knobs)
        return self.p

    def assertNoProblems(self):
        self.assertEqual(self.p.problems, [])


class Device(Phone):
    def wait(self, **kw):
        return S.wait_capture_config(UDID_A, self.logs.append, self.sysfs.root, self.p.open, **kw)

    def test_already_in_the_capture_configuration(self):
        self.phone()
        dev, number, ep_in, ep_out = self.wait()
        self.assertEqual((dev.node, number, ep_in, ep_out), (self.p.node, 2, F.VALERIA_IN, F.VALERIA_OUT))
        self.assertEqual((self.p.enables, self.logs), (0, []))

    def test_asks_once_for_the_capture_configuration(self):
        self.phone(configs=4, config=4)
        old = self.p.node
        dev, number, _, _ = self.wait()
        self.assertNotEqual(dev.node, old)                  # it re-enumerated
        self.assertEqual((dev.config, number, self.p.enables), (5, 2, 1))
        self.assertEqual(self.logs, ["switching the iPhone's USB to its screen-capture configuration"])

    def test_usbmuxd_keeping_configuration_4_is_unsupported(self):
        self.phone(configs=5, config=4)
        start = time.monotonic()
        with self.assertRaises(S.Unsupported) as cm:
            self.wait()
        self.assertLess(time.monotonic() - start, S.SWITCH_S)          # it settled: no need to wait it out
        self.assertIn("usbmuxd keeps it in USB configuration 4", cm.exception.strerror)
        self.assertEqual(self.p.enables, 0)                 # it offers capture: nothing to ask for

    def test_usbmuxd_switching_back_after_the_request_is_unsupported(self):
        self.phone(configs=4, config=4, on_enable="force4")
        with self.assertRaises(S.Unsupported) as cm:
            self.wait()
        self.assertIn("configuration 4", cm.exception.strerror)
        self.assertEqual(self.p.enables, 1)                 # asked once only

    def test_a_phone_without_the_capture_configuration_is_unsupported(self):
        self.phone(configs=4, config=4, on_enable="ignore")
        with self.assertRaises(S.Unsupported) as cm:
            self.wait()
        self.assertIn("did not offer", cm.exception.strerror)
        self.assertEqual(self.p.enables, 1)

    def test_no_phone_or_too_many(self):
        with self.assertRaises(S.Unplugged):
            S.wait_capture_config(UDID_A, self.logs.append, self.sysfs.root)
        F.FakePhone(self.sysfs, udid=UDID_A, name="1-2")
        F.FakePhone(self.sysfs, udid=UDID_B, name="1-3", devnum=9)
        with self.assertRaises(OSError) as cm:
            S.wait_capture_config(None, self.logs.append, self.sysfs.root)
        self.assertIn("more than one iPhone", cm.exception.strerror)

    def test_stop_ends_the_wait(self):
        self.phone(configs=4, config=4, on_enable="ignore")
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        start = time.monotonic()
        with self.assertRaises(S.Stopped):
            self.wait(stop=stop, timeout=30)
        self.assertLess(time.monotonic() - start, 1)

    def test_reset_waits_for_the_phone_to_come_back(self):
        self.phone()
        old = self.p.node
        S.reset_device(UDID_A, self.logs.append, self.sysfs.root, self.p.open)
        self.assertEqual(self.p.resets, 1)
        self.assertNotEqual(self.p.node, old)
        self.assertEqual(self.logs, ["resetting the iPhone's USB connection to start a new capture"])

    def test_unplugged_or_re_enumerated(self):
        self.phone()
        node = self.p.node
        self.assertFalse(S.unplugged(UDID_A, node, self.sysfs.root))              # it never left
        self.p.reset()
        self.assertFalse(S.unplugged(UDID_A, node, self.sysfs.root))              # it came back
        self.p.unplug()
        self.assertTrue(S.unplugged(UDID_A, self.p.node, self.sysfs.root))


class Captures(Phone):
    """One Capture at a time, driven directly."""

    def capture(self, audio=False):
        self.events = []
        c = S.Capture(UDID_A, self.events.append, audio, self.logs.append, self.p.open, self.sysfs.root)
        self.addCleanup(c.close)
        c.start()
        return c

    def test_a_session_from_start_to_a_clean_end(self):
        self.phone()
        c = self.capture()
        self.assertTrue(c.wait(c.first, 2))
        self.assertTrue(until(lambda: len(self.events) >= 10))
        c.close()
        s, = self.p.sessions
        self.assertEqual(s.timeline[:8], ["phone:PING", "PING", "phone:CWPA", "HPD1", "HPD1", "RPLY:CWPA",
                                          "phone:CVRP", "RPLY:CVRP"])
        self.assertNotIn("HPA1", s.timeline)
        self.assertEqual(s.timeline[-6:], ["HPD0", "phone:STOP", "RPLY:STOP", "phone:RELS", "phone:RELS", "HPD0"])
        self.assertEqual(s.needs, s.frames + 1)            # one after CVRP, then one per FEED
        self.assertEqual(c.size, (1180, 2556))
        self.assertTrue(self.events[0].keyframe and self.events[0].format)
        self.assertEqual({S.WRITE_MS, S.STOP_WRITE_MS}, self.p.write_timeouts)
        self.assertNoProblems()
        self.assertEqual(self.logs, [])
        c.close()                                           # again: nothing happens

    def test_reads_in_small_pieces(self):
        self.phone(chunk=7)
        c = self.capture()
        self.assertTrue(until(lambda: len(self.events) >= 5))
        self.assertEqual([F.counter_of(e.data) for e in self.events[:5]], [0, 1, 2, 3, 4])
        self.assertIsNone(c.error)

    def test_the_reader_survives_bad_messages(self):
        bad_feed = V.packet(V.ASYN, F.VIDEO_REF, V.FEED, V._block(V.SBUF, V._block(0x7A7A7A7A, b"")))
        cut_eat = V.packet(V.ASYN, F.AUDIO_REF, V.EAT, F.sample_buffer(bytes(64), 0, 48000)[:-12])
        garbage = [V.packet(V.ASYN, 1, 0x41424344), V.packet(V.SYNC, 1, 0x41424344, bytes(8)), bad_feed,
                   V.packet(0x41424344, 1, 1), cut_eat]
        self.phone(garbage=garbage)
        c = self.capture()
        self.assertTrue(until(lambda: len(self.events) >= 20))
        self.assertIsNone(c.error)
        skipped = [m for m in self.logs if m.startswith("skipped a message from the iPhone: ")]
        self.assertEqual(len(skipped), 1, self.logs)       # one line, not one per message
        self.assertIn("unknown ASYN message", skipped[0])

    def test_a_bad_frame_in_the_sink_is_skipped(self):
        self.phone()
        seen = []

        def sink(ev):
            seen.append(ev)
            if len(seen) == 2:
                raise ValueError("broken frame")
        c = S.Capture(UDID_A, sink, False, self.logs.append, self.p.open, self.sysfs.root)
        self.addCleanup(c.close)
        c.start()
        self.assertTrue(until(lambda: len(seen) >= 5))
        self.assertIn("skipped a message from the iPhone: frame dropped: ValueError('broken frame')", self.logs)

    def test_audio_is_asked_for_only_with_audio(self):
        self.phone()
        c = self.capture(audio=True)
        self.assertTrue(until(lambda: any(isinstance(e, V.Audio) for e in self.events)))
        c.close()
        s, = self.p.sessions
        self.assertEqual(s.timeline[5:7], ["RPLY:CWPA", "HPA1"])
        self.assertIn("RPLY:AFMT", s.timeline)
        self.assertEqual(s.timeline[-7:], ["HPA0", "HPD0", "phone:STOP", "RPLY:STOP", "phone:RELS", "phone:RELS",
                                           "HPD0"])
        self.assertNoProblems()

    def test_one_rels_is_enough(self):
        self.phone(rels=1)
        c = self.capture()
        self.assertTrue(c.wait(c.first, 2))
        start = time.monotonic()
        c.close()
        self.assertLess(time.monotonic() - start, S.RELEASE_S)
        self.assertEqual(self.p.sessions[0].timeline[-3:], ["RPLY:STOP", "phone:RELS", "HPD0"])
        self.assertNoProblems()

    def test_a_phone_that_never_confirms(self):
        self.phone(rels=0)
        c = self.capture()
        self.assertTrue(c.wait(c.first, 2))
        start = time.monotonic()
        c.close()
        self.assertLess(time.monotonic() - start, S.RELEASE_S + 1)
        self.assertEqual(self.p.sessions[0].timeline[-1], "RPLY:STOP")           # no second HPD0
        self.assertIn("the iPhone did not confirm the end of the capture", self.logs)

    def test_stop_does_not_wait_for_a_phone_that_vanished(self):
        S.RELEASE_S = 3                                     # the real wait: it must not be used
        self.phone(unplug_on="HPD0")
        c = self.capture()
        self.assertTrue(c.wait(c.first, 2))
        start = time.monotonic()
        c.close()
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.logs, [])

    def test_stop_is_bounded_when_the_phone_stops_reading(self):
        S.RELEASE_S = 3
        self.phone()
        c = self.capture()
        self.assertTrue(c.wait(c.first, 2))
        self.p.deaf = {"HPD0"}
        start = time.monotonic()
        c.close()
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(self.logs, ["ending the capture: Connection timed out"])

    def test_a_failed_start_leaves_nothing_open(self):
        self.phone()
        orig = F.FakeHandle.claim

        def claim(h, n):
            raise OSError(16, "Device or resource busy")
        F.FakeHandle.claim = claim
        try:
            c = S.Capture(UDID_A, self.logs.append, False, self.logs.append, self.p.open, self.sysfs.root)
            with self.assertRaises(OSError):
                c.start()
        finally:
            F.FakeHandle.claim = orig
        self.assertTrue(all(h.closed for h in self.p.handles))
        c.close()


class Runs(Phone):
    """screencap.run, as main runs it, with a Server and a socket client."""

    def start(self, audio=False, seconds=0, **knobs):
        self.phone(**knobs)
        self.dir = tempfile.mkdtemp(prefix="capsock-")
        self.path = os.path.join(self.dir, "cap.sock")
        self.server = S.Server(self.path, self.logs.append)
        self.stop, self.ready, self.result = threading.Event(), [], []
        self.thread = threading.Thread(target=lambda: self.result.append(S.run(
            UDID_A, self.server, self.stop, self.logs.append, audio=audio, seconds=seconds,
            ready=lambda: self.ready.append(time.monotonic()), opener=self.p.open, sysfs=self.sysfs.root)))
        self.thread.start()
        self.addCleanup(self.finish)

    def finish(self):
        self.stop.set()
        self.thread.join(10)
        self.server.close()
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def rc(self, timeout=10):
        self.thread.join(timeout)
        self.assertFalse(self.thread.is_alive(), self.logs)
        return self.result[0]

    def client(self):
        c = Client(self.path)
        self.addCleanup(c.close)
        return c

    def test_frames_reach_a_socket_client(self):
        self.start()
        self.assertTrue(until(lambda: self.ready))
        c = self.client()
        self.assertTrue(until(lambda: len(c.counters()) >= 30))
        n = c.counters()
        self.assertEqual(n, list(range(n[0], n[0] + len(n))))                   # none lost, none twice
        self.assertEqual(c.frames[0][0], S.VIDEO_FORMAT)
        self.assertEqual(c.kinds(S.VIDEO)[0][0], 1)                              # it starts at a keyframe
        self.assertIn("capturing 1180x2556, the sound stays on the iPhone", self.logs)
        self.stop.set()
        self.assertEqual(self.rc(), 0)
        self.assertEqual(self.p.sessions[0].timeline[-1], "HPD0")
        self.assertNoProblems()

    def test_a_late_client_starts_at_the_last_keyframe(self):
        self.start(keyframe_every=10)
        self.assertTrue(until(lambda: self.p.counter >= 35))
        c = self.client()
        self.assertTrue(until(lambda: len(c.counters()) >= 15))
        n = c.counters()
        self.assertEqual(n[0] % 10, 0)
        self.assertGreaterEqual(n[0], 30)
        self.assertEqual(n, list(range(n[0], n[0] + len(n))))
        self.assertEqual(c.frames[0][0], S.VIDEO_FORMAT)

    def test_sound_reaches_the_client_with_audio(self):
        self.start(audio=True)
        c = self.client()
        self.assertTrue(until(lambda: len(c.kinds(S.AUDIO)) >= 3))
        self.assertEqual(struct.unpack("<dII", c.kinds(S.AUDIO_FORMAT)[0]), (48000.0, 2, 16))
        self.assertEqual(set(c.kinds(S.AUDIO)), {bytes(4096)})                   # what iOS 26.5.2 sends: silence
        self.assertIn("capturing 1180x2556 with sound", self.logs)
        self.stop.set()
        self.assertEqual(self.rc(), 0)
        self.assertEqual(self.p.sessions[0].timeline[-7:-5], ["HPA0", "HPD0"])
        self.assertNoProblems()

    def test_no_sound_is_asked_for_by_default(self):
        self.start()
        c = self.client()
        self.assertTrue(until(lambda: len(c.counters()) >= 10))
        self.assertEqual(c.kinds(S.AUDIO), [])
        self.stop.set()
        self.assertEqual(self.rc(), 0)
        s, = self.p.sessions
        self.assertFalse(s.audio)
        self.assertNotIn("HPA1", s.timeline)
        self.assertNotIn("HPA0", s.timeline)

    def test_a_silent_phone_is_reset_once(self):
        self.start(fresh=False)
        self.assertTrue(until(lambda: self.ready))
        self.assertEqual([s.mode for s in self.p.sessions], ["silent", "fresh"])
        self.assertEqual(self.p.resets, 1)
        self.assertNotIn("HPD0", self.p.sessions[0].timeline)                   # nothing to end
        self.assertIn("resetting the iPhone's USB connection to start a new capture", self.logs)

    def test_a_phone_that_pings_but_sends_no_picture_is_reset_once(self):
        self.start(pictures=[0, None])
        self.assertTrue(until(lambda: self.ready))
        first = self.p.sessions[0]
        self.assertEqual((first.frames, first.timeline[-1]), (0, "HPD0"))      # its session was ended first
        self.assertEqual(self.p.resets, 1)
        self.assertNoProblems()

    def test_the_watchdog_starts_a_new_session_after_a_stall(self):
        self.start(pictures=[20, None])
        self.assertTrue(until(lambda: self.ready))
        c = self.client()
        self.assertTrue(until(lambda: len(self.p.sessions) == 2 and self.p.sessions[1].frames >= 10, 10))
        self.assertTrue(until(lambda: c.counters()[-1:] >= [29]))
        self.assertIn(f"no picture from the iPhone for {S.STALL_S:g} s - starting a new capture", self.logs)
        self.assertIn("capturing again", self.logs)
        self.assertEqual(c.kinds(S.STATUS), [b"The iPhone stopped sending its screen - starting a new capture...",
                                             b""])
        n = c.counters()
        self.assertEqual(n, list(range(n[0], n[0] + len(n))))                   # the same client goes on
        self.assertEqual((self.p.resets, len(self.ready)), (1, 1))              # "capturing" once only
        self.assertEqual(self.p.sessions[0].timeline[-1], "HPD0")
        self.stop.set()
        self.assertEqual(self.rc(), 0)
        self.assertNoProblems()

    def test_it_gives_up_after_three_failed_restarts_in_a_row(self):
        self.start(pictures=[15, 0])                        # no reset brings the picture back
        c = self.client()
        self.assertEqual(self.rc(15), 4)
        self.assertEqual(self.p.resets, 3)
        gaps = [b - a for a, b in zip(self.p.reset_times, self.p.reset_times[1:])]
        self.assertTrue(all(g >= S.RETRY_S for g in gaps), gaps)                 # not again soon after a fruitless one
        self.assertIn(f"no picture from the iPhone for {S.STALL_S:g} s - starting a new capture", self.logs)
        self.assertEqual(self.logs[-2:], [
            f"no picture from the iPhone for {S.RETRY_S:g} s, again",
            "the iPhone stopped sending its screen and new captures do not bring it back - restart the iPhone"])
        self.assertNotIn("capturing again", self.logs)
        self.assertTrue(until(lambda: c.kinds(S.STATUS)[-1:] == [
            b"The iPhone stopped sending its screen - if it stays black, restart the iPhone."]))

    def test_a_reset_that_brought_no_picture_yet_is_not_repeated_at_once(self):
        self.start(pictures=[15, None], first_after=[0, 1.0])      # the new session's picture comes after STALL_S
        c = self.client()
        self.assertTrue(until(lambda: len(self.p.sessions) == 2 and self.p.sessions[1].frames >= 10, 10))
        s = self.p.sessions[1]
        self.assertGreater(s.first_at - s.begun, S.STALL_S)       # the watchdog was due: it kept the session
        self.assertEqual((self.p.resets, len(self.p.sessions)), (1, 2))
        self.assertIn("capturing again", self.logs)
        self.assertTrue(until(lambda: c.kinds(S.STATUS)[-1:] == [b""]))
        self.stop.set()
        self.assertEqual(self.rc(), 0)
        self.assertNoProblems()

    def test_restarts_that_each_fail_give_up_however_far_apart_they_are(self):
        self.addCleanup(setattr, S, "RESTART_WINDOW_S", S.RESTART_WINDOW_S)
        S.RESTART_WINDOW_S = 2
        self.start(pictures=[120])                          # each session: ~0.6 s of pictures, then a stall
        self.assertEqual(self.rc(20), 4)
        self.assertEqual(self.p.resets, 3)
        self.assertGreater(self.p.reset_times[-1] - self.p.reset_times[0], S.RESTART_WINDOW_S)   # not in one window
        self.assertEqual(self.logs.count("capturing again"), 3)
        self.assertEqual(self.logs[-2], f"no picture from the iPhone for {S.STALL_S:g} s, again")

    def test_a_restart_that_lasts_ends_the_row(self):
        self.addCleanup(setattr, S, "RESTART_WINDOW_S", S.RESTART_WINDOW_S)
        S.RESTART_WINDOW_S = 2.5
        self.start(pictures=[15, 15, 15, 560, 15, None])   # the fourth session runs 2.8 s, longer than the window
        self.assertTrue(until(lambda: len(self.p.sessions) >= 6 and self.p.sessions[5].frames >= 5, 30))
        self.assertTrue(self.thread.is_alive())
        self.assertEqual((self.p.resets, self.logs.count("capturing again")), (5, 5))
        self.stop.set()
        self.assertEqual(self.rc(), 0)

    def test_a_broken_stream_starts_a_new_session(self):
        self.start(garbage=[struct.pack("<I", 2)])          # a message length it cannot be: lost track
        self.assertTrue(until(lambda: len(self.p.sessions) == 2 and self.p.sessions[1].frames >= 5, 10))
        self.assertIn("the capture stopped (bad message length 2) - starting a new capture", self.logs)
        self.assertTrue(self.thread.is_alive())

    def test_a_stream_that_breaks_after_every_restart_is_not_reset_in_a_loop(self):
        self.start(garbage=[struct.pack("<I", 2)], garbage_sessions=99)     # a picture, then broken, each time
        self.assertEqual(self.rc(15), 4)
        self.assertEqual(self.p.resets, 3)
        gaps = [b - a for a, b in zip(self.p.reset_times, self.p.reset_times[1:])]
        self.assertTrue(all(g >= S.STALL_S for g in gaps), gaps)
        self.assertIn("the capture stopped (bad message length 2), again", self.logs)

    def test_no_picture_after_the_reset_exits_4(self):
        self.start(pictures=[0])
        self.assertEqual(self.rc(), 4)
        self.assertEqual((self.p.resets, self.ready), (1, []))

    def test_unplugged_exits_2(self):
        self.start()
        self.assertTrue(until(lambda: self.ready))
        self.p.unplug()
        self.assertEqual(self.rc(), 2)
        self.assertEqual(self.logs[-1], "the iPhone was unplugged")
        self.assertEqual(self.p.resets, 0)

    def test_no_phone_exits_2(self):
        self.start()
        self.p.unplug()
        self.assertEqual(self.rc(), 2)

    def test_usbmuxd_too_old_exits_3(self):
        self.start(configs=5, config=4)
        self.assertEqual(self.rc(), 3)
        self.assertIn("usbmuxd", self.logs[-1])

    def test_seconds(self):
        self.start(seconds=0.3)
        self.assertEqual(self.rc(), 0)
        self.assertEqual(self.p.sessions[0].timeline[-1], "HPD0")

    def test_stop_while_waiting_for_the_phone(self):
        self.start(configs=4, config=4, on_enable="ignore")
        S.SWITCH_S = 30
        time.sleep(0.2)
        self.stop.set()
        self.assertEqual(self.rc(2), 0)


class Main(Phone):
    """screencap.main in this process."""

    def main(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = S.main(list(args), opener=self.p.open, sysfs=self.sysfs.root)
        return rc, err.getvalue()

    def test_record_writes_annex_b_and_the_sound(self):
        self.phone()
        d = tempfile.mkdtemp(prefix="caprec-")
        h264, pcm = os.path.join(d, "out.h264"), os.path.join(d, "out.pcm")
        try:
            rc, err = self.main("--udid", UDID_A, "--record", h264, "--wav", pcm, "--seconds", "0.3")
            self.assertEqual(rc, 0, err)
            with open(h264, "rb") as f:
                data = f.read()
            with open(pcm, "rb") as f:
                sound = f.read()
        finally:
            for name in os.listdir(d):
                os.unlink(os.path.join(d, name))
            os.rmdir(d)
        units = data.split(b"\x00\x00\x00\x01")[1:]
        self.assertEqual([u[0] & 0x1F for u in units[:4]], [7, 8, 6, 5])         # SPS, PPS, SEI, IDR
        self.assertTrue(sound and not sound.strip(b"\0"))
        self.assertRegex(err, r"screencap: recorded \d+ frames \(\d+ keyframes\)\n$")
        self.assertTrue(self.p.sessions[0].audio)            # --wav asks for the sound

    def test_exit_codes_and_messages(self):
        self.phone(configs=5, config=4)
        d = tempfile.mkdtemp(prefix="capsock-")
        self.addCleanup(os.rmdir, d)
        rc, err = self.main("--udid", UDID_A, "--socket", os.path.join(d, "cap.sock"))
        self.assertEqual(rc, 3)
        self.assertTrue(all(line.startswith("screencap: ") for line in err.splitlines()), err)
        self.p.unplug()
        self.assertEqual(self.main("--udid", UDID_A, "--socket", os.path.join(d, "cap.sock"))[0], 2)
        rc, err = self.main("--socket", os.path.join(d, "missing", "cap.sock"))
        self.assertEqual(rc, 1)
        self.assertIn("screencap: cannot write", err)

    def test_bad_command_lines_exit_1_not_2(self):
        self.phone()
        for args in ([], ["--socket", "a", "--record", "b"], ["--socket", "a", "--wav", "b"],
                     ["--socket", "a", "--audio", "--no-audio"], ["--record", "a", "--seconds", "-1"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                S.main(args, opener=self.p.open, sysfs=self.sysfs.root)
            self.assertEqual(cm.exception.code, 1, args)

    def test_a_signal_stops_it_through_a_flag_that_takes_no_lock(self):
        self.phone()
        seen = {}

        def run(udid, sink, stop, log, **kw):
            seen["handlers"] = [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)]
            threading.Timer(0.1, os.kill, (os.getpid(), signal.SIGINT)).start()     # while this thread waits
            start = time.monotonic()
            seen["stopped"] = (stop.wait(5), time.monotonic() - start)
            return 0
        self.addCleanup(setattr, S, "run", S.run)
        S.run = run
        d = tempfile.mkdtemp(prefix="capsock-")
        self.addCleanup(os.rmdir, d)
        self.assertEqual(self.main("--udid", UDID_A, "--socket", os.path.join(d, "cap.sock"))[0], 0)
        stop = seen["handlers"][0].__self__
        self.assertIsInstance(stop, S.StopFlag)
        self.assertEqual(seen["handlers"], [stop.set] * 3)
        self.assertTrue(seen["stopped"][0])
        self.assertLess(seen["stopped"][1], 1)

    def test_signal_handlers_are_put_back(self):
        self.phone(configs=5, config=4)
        before = [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)]
        d = tempfile.mkdtemp(prefix="capsock-")
        self.addCleanup(os.rmdir, d)
        self.main("--udid", UDID_A, "--socket", os.path.join(d, "cap.sock"))
        self.assertEqual([signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)], before)


class Process(unittest.TestCase):
    """python3 -I tests/valeria_fake.py: screencap.main in a process of its own, against a FakePhone."""

    def spawn(self, phone=None, args=(), unplug_after=None, timing=None):
        self.dir = tempfile.mkdtemp(prefix="capproc-")
        self.addCleanup(self._clean, self.dir)
        self.report = os.path.join(self.dir, "report.json")
        scene = os.path.join(self.dir, "scene.json")
        with open(scene, "w") as f:
            json.dump({"report": self.report, "phone": phone or {}, "timing": dict(F.FAST, **(timing or {})),
                       "unplug_after": unplug_after}, f)
        self.sock = os.path.join(self.dir, "cap.sock")
        self.proc = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "valeria_fake.py"), scene, "--",
                                      "--udid", UDID_A, "--socket", self.sock] + list(args),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._kill, self.proc)
        return self.proc

    @staticmethod
    def _kill(proc):
        if proc.poll() is None:
            proc.kill()
        proc.communicate()

    @staticmethod
    def _clean(d):
        for name in os.listdir(d):
            os.unlink(os.path.join(d, name))
        os.rmdir(d)

    def capturing(self, timeout=10):
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        self.assertTrue(ready, "no output")
        self.assertEqual(self.proc.stdout.readline(), b"capturing\n")

    def finish(self, timeout=10):
        out, err = self.proc.communicate(timeout=timeout)
        with open(self.report) as f:
            return self.proc.returncode, out.decode(), err.decode(), json.load(f)

    def test_sigterm_ends_the_session_cleanly(self):
        self.spawn()
        self.capturing()
        c = Client(self.sock)
        self.addCleanup(c.close)
        self.assertTrue(until(lambda: len(c.counters()) >= 5))
        start = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)
        rc, out, err, report = self.finish()
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual((rc, out), (0, ""), err)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["sessions"][0]["timeline"][-6:], ["HPD0", "phone:STOP", "RPLY:STOP", "phone:RELS",
                                                                  "phone:RELS", "HPD0"])
        self.assertTrue(err and all(line.startswith("screencap: ") for line in err.splitlines()), err)
        self.assertFalse(os.path.exists(self.sock))
        self.assertTrue(c.closed.wait(5))

    def test_sighup_and_sigint_stop_it_too(self):
        for sig in (signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig):
                self.spawn()
                self.capturing()
                self.proc.send_signal(sig)
                rc, _, err, report = self.finish()
                self.assertEqual(rc, 0, err)
                self.assertEqual(report["sessions"][0]["timeline"][-1], "HPD0")

    def test_sigterm_while_it_waits_for_the_phone(self):
        self.spawn(phone={"configs": 4, "config": 4, "on_enable": "ignore"}, timing={"SWITCH_S": 30})
        time.sleep(0.5)
        self.proc.send_signal(signal.SIGTERM)
        rc, out, err, _ = self.finish(5)
        self.assertEqual((rc, out), (0, ""), err)

    def test_unplugged_exits_2(self):
        self.spawn(unplug_after=0.2)
        self.capturing()
        rc, _, err, _ = self.finish()
        self.assertEqual(rc, 2, err)
        self.assertTrue(err.endswith("screencap: the iPhone was unplugged\n"), err)

    def test_unsupported_exits_3(self):
        self.spawn(phone={"configs": 5, "config": 4})
        rc, out, err, report = self.finish()
        self.assertEqual((rc, out), (3, ""), err)
        self.assertIn("usbmuxd keeps it in USB configuration 4", err)
        self.assertEqual(report["enables"], 0)

    def test_no_picture_exits_4(self):
        self.spawn(phone={"pictures": [0]})
        rc, out, err, report = self.finish()
        self.assertEqual((rc, out), (4, ""), err)
        self.assertIn("unlock it", err)
        self.assertEqual(report["resets"], 1)


class Signals(unittest.TestCase):
    def test_the_stop_flag_can_be_set_between_any_two_bytecodes_of_its_wait(self):
        # A signal handler runs in the main thread between two bytecodes, also in the middle of that thread's own
        # wait for the flag: threading.Event.set() there waits for ever for the lock the wait holds. This sets the
        # flag at every bytecode the wait runs, in a process of its own (a deadlock fails the test, not hangs it).
        code = textwrap.dedent("""
            import sys
            sys.path.insert(0, sys.argv[1])
            from iphone_control.screencap import StopFlag
            for timeout in (5, None):
                flag = StopFlag()

                def at_each_bytecode(frame, event, arg):
                    frame.f_trace_opcodes = True
                    if event == "opcode":
                        flag.set()
                    return at_each_bytecode
                sys.settrace(at_each_bytecode)
                got = flag.wait(timeout)
                sys.settrace(None)
                flag.close()
                print(got)
            """)
        try:
            r = subprocess.run([sys.executable, "-I", "-c", code, os.path.dirname(HERE)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("setting the stop flag inside its own wait deadlocked")
        self.assertEqual((r.returncode, r.stdout.split()), (0, ["True", "True"]), r.stderr)

    def test_the_stop_flag_works_like_an_event(self):
        flag = S.StopFlag()
        self.addCleanup(flag.close)
        start = time.monotonic()
        self.assertFalse(flag.wait(0.05) or flag.is_set())
        self.assertGreaterEqual(time.monotonic() - start, 0.04)
        for _ in range(100_000):                            # more than a pipe holds: never blocks, never raises
            flag.set(signal.SIGTERM, None)
        self.assertTrue(flag.is_set() and flag.wait(5) and flag.wait(None))


class Imports(unittest.TestCase):
    def test_no_pygobject_needed(self):
        code = ('import sys; sys.modules["gi"] = None; sys.path.insert(0, sys.argv[1]); '
                "import iphone_control.usbfs, iphone_control.valeria, iphone_control.screencap")
        r = subprocess.run([sys.executable, "-I", "-c", code, os.path.dirname(HERE)], stderr=subprocess.PIPE,
                           universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
