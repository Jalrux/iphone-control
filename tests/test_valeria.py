"""The screen-capture protocol (iphone_control/valeria.py), pure: no USB, no phone.

Byte vectors are quicktime_video_hack's packet fixtures (github.com/danielpaulus/quicktime_video_hack,
screencapture/packet/fixtures): only the small protocol messages, which hold clock references and constants. Its
FEED/EAT fixtures hold a real screen and sound, so sample buffers here are synthetic (tests/valeria_fake.py).
"""
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import valeria_fake as F  # noqa: E402
from iphone_control import valeria as V  # noqa: E402

QVH = {
    "ping": "10000000 676e6970 00000000 01000000",                  # its doc (no fixture file)
    "hpd1": ("db000000 6e797361 01000000 00000000 31647068 c7000000 74636964 20000000 7679656b 0f000000 6b727473 "
             "56616c65 72696109 00000076 6c756201 2f000000 7679656b 1e000000 6b727473 48455643 4465636f 64657253 "
             "7570706f 72747334 34340900 0000766c 75620170 00000076 79656b13 0000006b 72747344 6973706c 61795369 "
             "7a655500 00007463 69642600 00007679 656b0d00 00006b72 74735769 64746811 00000076 626d6e06 00000000 "
             "00009e40 27000000 7679656b 0e000000 6b727473 48656967 68741100 00007662 6d6e0600 00000000 c09240"),
    "hpa1": ("51010000 6e797361 f0925314 01000000 31617068 3d010000 74636964 34000000 7679656b 1b000000 6b727473 "
             "42756666 65724168 65616449 6e746572 76616c11 00000076 626d6e06 e4a59bc4 20b0b23f 28000000 7679656b "
             "11000000 6b727473 64657669 63655549 440f0000 00767274 7356616c 65726961 2e000000 7679656b 15000000 "
             "6b727473 53637265 656e4c61 74656e63 79110000 0076626d 6e067b14 ae47e17a a43f5700 00007679 656b0f00 "
             "00006b72 7473666f 726d6174 73400000 00767461 64000000 000070e7 406d6370 6c0c0000 00040000 00010000 "
             "00040000 00020000 00100000 00000000 00000000 000070e7 40000000 000070e7 402b0000 00767965 6b160000 "
             "006b7274 73454449 44414333 53757070 6f72740d 00000076 626d6e03 00000000 29000000 7679656b 12000000 "
             "6b727473 64657669 63654e61 6d650f00 00007672 74735661 6c657269 61"),
    "hpd0": "14000000 6e797361 01000000 00000000 30647068",
    "hpa0": "14000000 6e797361 10fcc502 01000000 30617068",
    "need": "14000000 6e797361 a06cc102 01000000 6465656e",
    "cwpa_req": "24000000 636e7973 01000000 00000000 61707763 e03d5713 01000000 e0745a13 01000000",
    "cwpa_rep": "1c000000 796c7072 e03d5713 01000000 00000000 b00ce26c a67f0000",
    "afmt_req": ("44000000 636e7973 b00ce26c a67f0000 746d6661 809d2213 01000000 00000000 0070e740 6d63706c 4c000000 "
                 "04000000 01000000 04000000 02000000 10000000 00000000"),
    "afmt_rep": ("3e000000 796c7072 809d2213 01000000 00000000 2a000000 74636964 22000000 7679656b 0d000000 6b727473 "
                 "4572726f 720d0000 0076626d 6e030000 0000"),
    "cvrp_rep": "1c000000 796c7072 d0595613 01000000 00000000 5002d16c a67f0000",
    "clok_req": "1c000000 636e7973 5002d16c a67f0000 6b6f6c63 70495813 01000000",
    "time_req": "1c000000 636e7973 8079c17c a67f0000 656d6974 503d2213 01000000",
    "time_rep": "2c000000 796c7072 503d2213 01000000 00000000 e1e142c4 62ba0000 00ca9a3b 01000000 00000000 00000000",
    "skew_req": "1c000000 636e7973 f05f4235 ba7f0000 77656b73 60b9fd02 01000000",
    "skew_rep": "1c000000 796c7072 60b9fd02 01000000 00000000 00000000 0070e740",
    "og_req": "20000000 636e7973 f05f4235 ba7f0000 20216f67 302fd302 01000000 01000000",
    "og_rep": "18000000 796c7072 302fd302 01000000 00000000 00000000",
    "stop_req": "1c000000 636e7973 f05f4235 ba7f0000 706f7473 1049fd02 01000000",
    "stop_rep": "18000000 796c7072 1049fd02 01000000 00000000 00000000",
    # these four fixtures have no length in front
    "sprp": ("6e797361 18bc2311 01000000 70727073 2e000000 7679656b 1d000000 6b727473 4f626579 456d7074 794d6564 "
             "69614d61 726b6572 73090000 00766c75 6201"),
    "srat": ("6e797361 18bc2311 01000000 74617273 0000803f 0000803f cb448ea1 cf10cc15 00ca9a3b 01000000 00000000 "
             "00000000 00"),
    "tbas": "6e797361 18bc2311 01000000 73616274 c0904402 01000000",
    "tjmp": ("6e797361 18bc2311 01000000 706d6a74 00000000 00000000 00000000 00000000 01000000 01000000 00000000 "
             "00000000 00000000 00000000 01000000 01000000 00000000 00000000"),
    "rels": "14000000 6e797361 008a6035 ba7f0000 736c6572",
}


def fixture(name):
    return bytes.fromhex(QVH[name])


def body(name):
    """A fixture without its length: what Session.receive takes."""
    return fixture(name)[4:]


def corr(msg):
    """The correlation id of a SYNC message (with its length)."""
    return struct.unpack_from("<Q", msg, 20)[0]


class Values(unittest.TestCase):
    def test_numbers_round_trip(self):
        for n, size in ((V.Number(7), 5), (V.Number(2 ** 40, 4), 9), (V.Number(0.04), 9), (V.Number(9, 5), 5)):
            packed = n.pack()
            self.assertEqual(len(packed), size)
            self.assertEqual(V.Number.unpack(packed), n)
        self.assertEqual(V.Number(0).kind, 3)
        self.assertEqual(V.Number(1920.0).pack().hex(), "06" + "00000000009e40".rjust(16, "0"))
        for bad in (b"", b"\x03\x00", b"\x04" + bytes(4), b"\x06" + bytes(4), b"\x07" + bytes(8)):
            with self.assertRaises(V.ProtocolError, msg=bad):
                V.Number.unpack(bad)

    def test_dicts_round_trip_in_order(self):
        d = {"s": "text", "b": True, "f": False, "n": V.Number(3), "x": V.Number(1.5), "data": b"\x00\xff",
             "inner": {"Width": V.Number(1920.0), "deep": {"k": "v"}}, "empty": {}}
        back = V.parse_dict(V.pack_dict(d))
        self.assertEqual(back, d)
        self.assertEqual(list(back), list(d))
        with self.assertRaises(TypeError):
            V.pack_value(object())

    def test_dict_bytes_match_quicktime_video_hack(self):
        self.assertEqual(V.video_request(), fixture("hpd1"))
        self.assertEqual(V.audio_request(0x1145392F0), fixture("hpa1"))
        self.assertEqual(V.parse_dict(fixture("hpd1")[20:]),
                         {"Valeria": True, "HEVCDecoderSupports444": True,
                          "DisplaySize": {"Width": V.Number(1920.0), "Height": V.Number(1200.0)}})

    def test_index_key_dicts(self):
        d = {V.AVCC_KEY: {V.AVCC_SUBKEY: b"avcC"}, 52: "H.264", 30: V.Number(0)}
        self.assertEqual(V.parse_dict(F.index_dict(V.EXTN, d), 0, V.EXTN), d)

    def test_broken_dicts_raise_protocol_error(self):
        good = V.pack_dict({"key": "value", "n": V.Number(1)})
        for n in range(len(good)):
            with self.assertRaises(V.ProtocolError, msg=n):
                V.parse_dict(good[:n])
        bad_key = good.replace(struct.pack("<I", V.STRK), struct.pack("<I", V.STRV), 1)
        long_value = bytearray(good)
        struct.pack_into("<I", long_value, 16, 9)            # the key block now claims part of its value
        short_index = V._block(V.DICT, V._block(V.KEYV, V._block(V.IDXK, b"") + V.pack_value(True)))
        unknown = V._block(V.DICT, V._block(V.KEYV, V._block(V.STRK, b"k") + V._block(0x41424344, b"")))
        for broken in (bad_key, bytes(long_value), short_index, unknown, V.pack_dict({"k": "v"})[:-1] + b"",
                       struct.pack("<II", 8, V.STRV)):
            with self.assertRaises(V.ProtocolError, msg=broken.hex()):
                V.parse_dict(broken)

    def test_time_round_trip(self):
        self.assertEqual(V.parse_time(V.pack_time(123456789)), (123456789, 1_000_000_000, 1, 0))
        self.assertEqual(V.parse_time(V.pack_time(5, 48000, 3, 1)), (5, 48000, 3, 1))
        self.assertEqual(len(V.pack_time(0)), 24)
        with self.assertRaises(V.ProtocolError):
            V.parse_time(bytes(10))

    def test_asbd_round_trip(self):
        a = V.default_asbd()
        packed = V.pack_asbd(a)
        self.assertEqual(len(packed), 56)
        self.assertEqual(V.parse_asbd(packed), a)
        self.assertEqual(V.parse_dict(fixture("hpa1")[20:])["formats"], packed)       # HPA1's "formats" entry
        with self.assertRaises(V.ProtocolError):
            V.parse_asbd(packed[:20])


class Media(unittest.TestCase):
    def test_video_format_and_its_parameter_sets(self):
        f = V.parse_format(F.video_format(1180, 2556))
        self.assertEqual((f["media"], f["width"], f["height"], f["codec"]), ("video", 1180, 2556, "avc1"))
        self.assertEqual(f["avcc"], F.make_avcc(1180, 2556))
        sps, pps = V.avcc_parameter_sets(f["avcc"])
        self.assertEqual((sps[0] & 0x1F, pps[0] & 0x1F), (7, 8))
        self.assertEqual(struct.unpack(">HH", sps[4:8]), (1180, 2556))

    def test_avcc_parameter_sets(self):
        def record(spss, ppss):
            out = bytes([1, 0x64, 0, 0x33, 0xFF, 0xE0 | len(spss)])
            for s in spss:
                out += struct.pack(">H", len(s)) + s
            out += bytes([len(ppss)])
            for p in ppss:
                out += struct.pack(">H", len(p)) + p
            return out
        two = record([b"\x67\x01", b"\x67\x02\x03"], [b"\x68\x04"])
        self.assertEqual(V.avcc_parameter_sets(two), [b"\x67\x01", b"\x67\x02\x03", b"\x68\x04"])
        self.assertEqual(V.avcc_parameter_sets(record([], [])), [])
        for bad in (two[:-1], two[:8], b"\x00" + two[1:], b"\x01\x64"):
            with self.assertRaises(V.ProtocolError, msg=bad.hex()):
                V.avcc_parameter_sets(bad)

    def test_audio_format(self):
        f = V.parse_format(F.audio_format())
        self.assertEqual(f["media"], "audio")
        self.assertEqual((f["asbd"]["sample_rate"], f["asbd"]["channels"], f["asbd"]["bits"]), (48000.0, 2, 16))
        with self.assertRaises(V.ProtocolError):
            V.parse_format(F.audio_format()[:-8])

    def test_unknown_media_and_short_parts(self):
        other = V._block(V.FDSC, V._block(V.MDIA, struct.pack("<I", 0x74657874)))
        short_vdim = V._block(V.FDSC, V._block(V.MDIA, struct.pack("<I", V.VIDE)) + V._block(V.VDIM, b"\0" * 4))
        for bad in (other, short_vdim, V._block(V.FDSC, V._block(V.MDIA, b""))):
            with self.assertRaises(V.ProtocolError):
                V.parse_format(bad)

    def test_sample_buffer(self):
        au = F.access_unit(7, True)
        sb = V.parse_sample_buffer(F.sample_buffer(au, 90, 60, F.video_format(4, 8)))
        self.assertEqual((sb["pts"], sb["data"], sb["samples"]), ((90, 60), au, 1))
        self.assertEqual((sb["format"]["width"], sb["format"]["height"]), (4, 8))
        self.assertIsNone(V.parse_sample_buffer(F.sample_buffer(au, 1, 60))["format"])
        unknown = V._block(V.SBUF, V._block(0x7A7A7A7A, b""))
        past = bytearray(F.sample_buffer(au, 1, 60))
        struct.pack_into("<I", past, 0, 8 + 32 - 4)          # the buffer ends inside its first part
        for bad in (unknown, bytes(past), V._block(V.SBUF, V._block(V.OPTS, bytes(8)))):
            with self.assertRaises(V.ProtocolError):
                V.parse_sample_buffer(bad)

    def test_keyframes_and_nal_units(self):
        key, p = F.access_unit(3, True), F.access_unit(4, False)
        self.assertTrue(V.is_keyframe(key))
        self.assertFalse(V.is_keyframe(p))
        self.assertEqual([u[0] & 0x1F for u in V.nal_units(key)], [6, 5])            # SEI, then the IDR slice
        self.assertEqual(F.counter_of(p), 4)
        self.assertFalse(V.is_keyframe(b""))
        self.assertFalse(V.is_keyframe(key[:-1]))                                    # truncated: not trusted
        self.assertFalse(V.is_keyframe(struct.pack(">I", 0)))                        # an empty NAL unit
        with self.assertRaises(V.ProtocolError):
            V.nal_units(key[:-1])


class Messages(unittest.TestCase):
    def cvrp(self, s, ref=0x7FA66CD20000, c=0x1135699D0):
        return s.receive(F.sync(V.EMPTY, V.CVRP, c, struct.pack("<Q", ref) + V.pack_dict({"Valeria": True}))[4:])

    def test_built_messages_match_quicktime_video_hack(self):
        self.assertEqual(V.ping(), fixture("ping"))
        self.assertEqual(V.packet(V.ASYN, V.EMPTY, V.HPD0), fixture("hpd0"))
        self.assertEqual(V.packet(V.ASYN, 0x102C5FC10, V.HPA0), fixture("hpa0"))
        self.assertEqual(V.need(0x102C16CA0), fixture("need"))
        self.assertEqual(V.reply(corr(fixture("cwpa_req")), struct.pack("<Q", 0x7FA66CE20CB0)), fixture("cwpa_rep"))
        self.assertEqual(V.reply(0x1135659D0, struct.pack("<Q", 0x7FA66CD10250)), fixture("cvrp_rep"))

    def test_ping(self):
        self.assertEqual(V.Session().receive(fixture("ping")[4:]), ([V.ping()], []))

    def test_cwpa_asks_for_video_and_only_with_audio_for_sound(self):
        device = struct.unpack_from("<Q", fixture("cwpa_req"), 28)[0]
        reply = V.reply(corr(fixture("cwpa_req")), struct.pack("<Q", device + 1000))
        out, events = V.Session().receive(body("cwpa_req"))
        self.assertEqual((out, events), ([fixture("hpd1"), fixture("hpd1"), reply], []))
        out, _ = V.Session(audio=True).receive(body("cwpa_req"))
        self.assertEqual(out, [fixture("hpd1"), fixture("hpd1"), reply, V.audio_request(device)])
        self.assertEqual(V.Session(video=False, audio=True).receive(body("cwpa_req"))[0],
                         [reply, V.audio_request(device)])

    def test_cvrp_starts_the_need_flow(self):
        s = V.Session()
        out, _ = self.cvrp(s, 0x7FA66CD20000, 0x1135699D0)
        self.assertEqual(out, [V.need(0x7FA66CD20000),
                               V.reply(0x1135699D0, struct.pack("<Q", 0x7FA66CD20000 + 0x1000AF))])
        self.assertEqual(s.need_ref, 0x7FA66CD20000)

    def test_clok_and_time(self):
        now = [1000]
        s = V.Session(now=lambda: now[0])
        out, _ = s.receive(body("time_req"))
        self.assertEqual(out, [V.reply(corr(fixture("time_req")), V.pack_time(0))])      # no clock yet
        out, _ = s.receive(body("clok_req"))
        self.assertEqual(out, [V.reply(corr(fixture("clok_req")), struct.pack("<Q", 0x7FA66CD10250 + 0x10000))])
        now[0] += 0xBA62C442E1E1
        self.assertEqual(s.receive(body("time_req")), ([fixture("time_rep")], []))

    def test_replies_match_quicktime_video_hack(self):
        for name in ("afmt", "og", "stop", "skew"):
            self.assertEqual(V.Session().receive(body(name + "_req")), ([fixture(name + "_rep")], []), name)

    def test_skew_measures_the_phone_clock_against_ours(self):
        now = [0]
        s = V.Session(audio=True, now=lambda: now[0])
        s.receive(body("cwpa_req"))
        self.assertEqual(s.skew(), 48000.0)
        for pts, local in ((0, 0), (48000, 1_001_000_000)):  # one second of samples took 1.001 s here
            now[0] = local
            s.receive(V.packet(V.ASYN, F.AUDIO_REF, V.EAT, F.sample_buffer(bytes(16), pts, 48000))[4:])
        self.assertAlmostEqual(s.skew(), 48048.0)
        self.assertAlmostEqual(struct.unpack("<d", s.receive(body("skew_req"))[0][0][-8:])[0], s.skew())

    def test_every_feed_gets_a_need(self):
        s = V.Session()
        out, events = s.receive(F.feed(1, True, F.video_format(1180, 2556))[4:])
        self.assertEqual(out, [])                            # no NEED before CVRP gave its clock
        self.cvrp(s, 0xABC)
        out, events = s.receive(F.feed(2, True, F.video_format(1180, 2556))[4:])
        self.assertEqual(out, [V.need(0xABC)])
        ev, = events
        self.assertIsInstance(ev, V.Video)
        self.assertTrue(ev.keyframe)
        self.assertEqual((ev.format["width"], ev.format["height"], F.counter_of(ev.data)), (1180, 2556, 2))
        self.assertEqual(ev.pts, 2 * 16_666_667)
        out, (ev,) = s.receive(F.feed(3, False)[4:])
        self.assertEqual((out, ev.keyframe, ev.format), ([V.need(0xABC)], False, None))
        empty = V.packet(V.ASYN, F.VIDEO_REF, V.FEED, V._block(V.SBUF, V._block(V.OPTS, V.pack_time(1))))
        self.assertEqual(s.receive(empty[4:]), ([V.need(0xABC)], []))               # like qvh's ttas-only FEED
        broken = F.feed(4, False)[:-30]
        out, (ev,) = s.receive(broken[4:])
        self.assertEqual(out, [V.need(0xABC)])               # still asks for the next one
        self.assertIsInstance(ev, V.Dropped)
        self.assertEqual(s.counts["video"], 5)

    def test_cut_messages_raise_only_protocol_errors(self):
        s = V.Session(audio=True)
        self.cvrp(s, 0xABC)
        for msg in (F.feed(1, True, F.video_format(16, 32))[4:], F.eat(1, fmt=True)[4:], body("afmt_req")):
            for n in range(len(msg)):
                try:
                    s.receive(msg[:n])
                except V.ProtocolError:
                    pass

    def test_audio(self):
        s = V.Session(audio=True)
        s.receive(body("cwpa_req"))
        out, (ev,) = s.receive(F.eat(0, fmt=True)[4:])
        self.assertEqual(out, [])
        self.assertIsInstance(ev, V.Audio)
        self.assertEqual((ev.asbd["sample_rate"], ev.asbd["channels"], len(ev.data), ev.pts), (48000.0, 2, 4096, 0))
        _, (ev,) = s.receive(F.eat(1)[4:])
        self.assertEqual(ev.asbd["bits"], 16)                # the format is kept from the first
        nodata = V.packet(V.ASYN, F.AUDIO_REF, V.EAT, V._block(V.SBUF, b""))
        self.assertEqual(s.receive(nodata[4:]), ([], []))
        _, (ev,) = s.receive(F.eat(2)[:-10][4:])
        self.assertIsInstance(ev, V.Dropped)

    def test_rels_and_messages_that_need_nothing(self):
        out, (ev,) = V.Session().receive(body("rels"))
        self.assertEqual((out, ev.ref), ([], 0x7FBA35608A00))
        for name in ("sprp", "srat", "tbas", "tjmp"):
            self.assertEqual(V.Session().receive(fixture(name)), ([], []), name)

    def test_unknown_messages_raise_protocol_error(self):
        for bad in (b"", b"abc", b"zzzz" + bytes(12), struct.pack("<I", V.SYNC) + bytes(4),
                    V.packet(V.SYNC, 1, 0x41424344, bytes(8))[4:], V.packet(V.ASYN, 1, 0x41424344)[4:],
                    V.packet(V.SYNC, 1, V.CWPA)[4:], V.packet(V.SYNC, 1, V.CWPA, bytes(8))[4:]):
            with self.assertRaises(V.ProtocolError, msg=bad.hex()):
                V.Session().receive(bad)

    def test_stop(self):
        self.assertFalse(V.Session().want_audio)             # sound is off unless asked for
        self.assertEqual(V.Session().stop(), [fixture("hpd0")])
        s = V.Session(audio=True)
        self.assertEqual(s.stop(), [fixture("hpd0")])        # no CWPA yet: no audio clock to stop
        s.receive(body("cwpa_req"))
        device = struct.unpack_from("<Q", fixture("cwpa_req"), 28)[0]
        self.assertEqual(s.stop(), [V.packet(V.ASYN, device, V.HPA0), fixture("hpd0")])
        s = V.Session(audio=False)
        s.receive(body("cwpa_req"))
        self.assertEqual(s.stop(), [fixture("hpd0")])


if __name__ == "__main__":
    unittest.main()
