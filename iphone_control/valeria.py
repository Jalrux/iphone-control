"""The iPhone's USB screen-capture protocol ("Valeria", what QuickTime and OBS use on a Mac). Pure, no I/O.

Needs no Developer Mode, only a paired ("Trust") phone: the phone streams its screen as hardware H.264 (one access
unit per FEED, length-prefixed NAL units, SPS/PPS in the format description) and, when asked for it, its sound as
48 kHz 16-bit stereo LPCM (EAT!), over the bulk endpoints of a hidden USB configuration (see usbfs.py,
screencap.py). Sound is not asked for by default: on iOS 26.5.2 (iPhone 15 Pro, Oct 2026) the phone keeps playing
it on its own speaker and sends only digital silence.

Ported from quicktime_video_hack's screencapture package (Daniel Paulus, MIT,
github.com/danielpaulus/quicktime_video_hack), which documents the messages in its doc/ folder; the messages
built here are byte-identical to its fixtures.

Every message starts with its length (uint32, little endian, including itself), then a 4-byte magic, all
little endian. Session.receive() takes one message without its length and returns the replies to send (with
their length) and what it found in it (Video, Audio, Released, Dropped).
"""
import functools
import struct
import time

# message magics, as little-endian uint32 of the ASCII names reversed ("ping" is sent as "gnip")
PING, SYNC, ASYN, RPLY = 0x70696E67, 0x73796E63, 0x6173796E, 0x72706C79
CWPA, AFMT, CVRP, CLOK, TIME, SKEW, OG, STOP = (0x63777061, 0x61666D74, 0x63767270, 0x636C6F6B, 0x74696D65,
                                                0x736B6577, 0x676F2120, 0x73746F70)
FEED, EAT, SPRP, TJMP, SRAT, TBAS, RELS = 0x66656564, 0x65617421, 0x73707270, 0x746A6D70, 0x73726174, 0x74626173, \
    0x72656C73
HPD1, HPA1, HPD0, HPA0, NEED = 0x68706431, 0x68706131, 0x68706430, 0x68706130, 0x6E656564
EMPTY = 1                                 # the "no clock" reference

# dictionary and sample-buffer parts
KEYV, STRK, IDXK, BULV, DICT, DATV, STRV, NMBV = (0x6B657976, 0x7374726B, 0x6964786B, 0x62756C76, 0x64696374,
                                                  0x64617476, 0x73747276, 0x6E6D6276)
SBUF, OPTS, STIA, SDAT, SATT, SARY, SSIZ, NSMP = (0x73627566, 0x6F707473, 0x73746961, 0x73646174, 0x73617474,
                                                  0x73617279, 0x7373697A, 0x6E736D70)
FDSC, MDIA, VDIM, CODC, EXTN, ASBD = 0x66647363, 0x6D646961, 0x7664696D, 0x636F6463, 0x6578746E, 0x61736264
VIDE, SOUN = 0x76696465, 0x736F756E
AVCC_KEY, AVCC_SUBKEY = 49, 105           # format-description extensions: SampleDescriptionExtensionAtoms / avcC
LPCM = 0x6C70636D
IDR = 5                                   # NAL unit type of a keyframe

MAX_MESSAGE = 16 << 20


class ProtocolError(ValueError):
    pass


def _checked(fn):
    """Malformed input raises ProtocolError, never struct.error or IndexError from deep inside a parser."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (struct.error, IndexError) as e:
            raise ProtocolError(f"truncated data ({e})") from None
    return wrapper


class Number:
    """An NSNumber: kind 3 = uint32, 4 = uint64, 6 = float64 (5 is read as uint32)."""
    __slots__ = ("kind", "value")

    def __init__(self, value, kind=None):
        self.kind = kind or (6 if isinstance(value, float) else 3)
        self.value = value

    def __eq__(self, other):
        return isinstance(other, Number) and (self.kind, self.value) == (other.kind, other.value)

    def __repr__(self):
        return f"Number({self.value!r}, {self.kind})"

    def pack(self):
        fmt = {3: "<BI", 4: "<BQ", 5: "<BI", 6: "<Bd"}[self.kind]
        return struct.pack(fmt, self.kind, self.value)

    @classmethod
    def unpack(cls, data):
        kind = data[0] if data else None
        if kind in (3, 5) and len(data) == 5:
            return cls(struct.unpack_from("<I", data, 1)[0], kind)
        if kind == 4 and len(data) == 9:
            return cls(struct.unpack_from("<Q", data, 1)[0], kind)
        if kind == 6 and len(data) == 9:
            return cls(struct.unpack_from("<d", data, 1)[0], kind)
        raise ProtocolError(f"unknown NSNumber kind {kind} ({len(data)} bytes)")


# ---- dictionaries ----------------------------------------------------------------------------------------
def _block(magic, payload):
    return struct.pack("<II", len(payload) + 8, magic) + payload


def _head(data, at, magic=None):
    """(length, magic) of the block at data[at:], checked against data's size (and magic if given)."""
    if at + 8 > len(data):
        raise ProtocolError("truncated block header")
    length, kind = struct.unpack_from("<II", data, at)
    if length < 8 or at + length > len(data):
        raise ProtocolError(f"bad block length {length}")
    if magic is not None and kind != magic:
        raise ProtocolError(f"expected {_name(magic)}, got {_name(kind)}")
    return length, kind


def _name(magic):
    return struct.pack("<I", magic)[::-1].decode("latin-1")


def pack_value(value):
    if isinstance(value, bool):
        return _block(BULV, b"\x01" if value else b"\x00")
    if isinstance(value, Number):
        return _block(NMBV, value.pack())
    if isinstance(value, str):
        return _block(STRV, value.encode())
    if isinstance(value, (bytes, bytearray)):
        return _block(DATV, bytes(value))
    if isinstance(value, dict):
        return pack_dict(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def pack_dict(d):
    """A dictionary with string keys (insertion order kept)."""
    out = b"".join(_block(KEYV, _block(STRK, k.encode()) + pack_value(v)) for k, v in d.items())
    return _block(DICT, out)


@_checked
def parse_value(data, at=0):
    length, kind = _head(data, at)
    body = data[at + 8:at + length]
    if kind == STRV:
        return bytes(body).decode("utf-8", "replace")
    if kind == DATV:
        return bytes(body)
    if kind == BULV:
        return body[:1] == b"\x01"
    if kind == NMBV:
        return Number.unpack(body)
    if kind == DICT:
        return parse_dict(data, at)
    if kind == FDSC:
        return parse_format(data[at:at + length])
    raise ProtocolError(f"unknown value type {_name(kind)}")


@_checked
def parse_dict(data, at=0, magic=DICT):
    """A dictionary block -> {key: value}; keys are str (strk) or int (idxk)."""
    length, _ = _head(data, at, magic)
    end, i, out = at + length, at + 8, {}
    while i < end:
        n, _ = _head(data, i, KEYV)
        if i + n > end:
            raise ProtocolError("dictionary entry past its dictionary")
        kn, kk = _head(data, i + 8)
        if kk == STRK:
            key = bytes(data[i + 16:i + 8 + kn]).decode("utf-8", "replace")
        elif kk == IDXK and kn >= 10:
            key = struct.unpack_from("<H", data, i + 16)[0]
        else:
            raise ProtocolError(f"unknown key type {_name(kk)}")
        vn, _ = _head(data, i + 8 + kn)
        if kn + vn > n - 8:
            raise ProtocolError("dictionary value past its entry")
        out[key] = parse_value(data, i + 8 + kn)
        i += n
    return out


# ---- media -------------------------------------------------------------------------------------------------
def pack_time(value, scale=1_000_000_000, flags=1, epoch=0):
    """A CMTime: value, timescale, flags (1 = valid, rounded), epoch."""
    return struct.pack("<QIIQ", value, scale, flags, epoch)


@_checked
def parse_time(data, at=0):
    value, scale, flags, epoch = struct.unpack_from("<QIIQ", data, at)
    return value, scale, flags, epoch


ASBD_FIELDS = ("sample_rate", "format", "flags", "bytes_per_packet", "frames_per_packet", "bytes_per_frame",
               "channels", "bits", "reserved")


def default_asbd():
    return {"sample_rate": 48000.0, "format": LPCM, "flags": 12, "bytes_per_packet": 4, "frames_per_packet": 1,
            "bytes_per_frame": 4, "channels": 2, "bits": 16, "reserved": 0}


def pack_asbd(a):
    """The 56-byte "formats" entry of HPA1: an AudioStreamBasicDescription plus the rate range."""
    return struct.pack("<dIIIIIIII", *(a[k] for k in ASBD_FIELDS)) + struct.pack("<dd", a["sample_rate"],
                                                                                  a["sample_rate"])


@_checked
def parse_asbd(data, at=0):
    return dict(zip(ASBD_FIELDS, struct.unpack_from("<dIIIIIIII", data, at)))


@_checked
def parse_format(data):
    """A format description (fdsc) -> {"media": "video", "width", "height", "codec", "avcc"} or
    {"media": "audio", "asbd"}."""
    length, _ = _head(data, 0, FDSC)
    data = data[:length]
    n, _ = _head(data, 8, MDIA)
    if n < 12:
        raise ProtocolError("short media type")
    media = struct.unpack_from("<I", data, 16)[0]
    i = 8 + n
    if media == SOUN:
        n, _ = _head(data, i, ASBD)
        if n < 8 + 40:
            raise ProtocolError("short audio description")
        return {"media": "audio", "asbd": parse_asbd(data, i + 8)}
    if media != VIDE:
        raise ProtocolError(f"unknown media type {_name(media)}")
    n, _ = _head(data, i, VDIM)
    if n < 16:
        raise ProtocolError("short video dimensions")
    width, height = struct.unpack_from("<II", data, i + 8)
    i += n
    n, _ = _head(data, i, CODC)
    if n < 12:
        raise ProtocolError("short codec")
    codec = struct.unpack_from("<I", data, i + 8)[0]
    i += n
    ext = parse_dict(data, i, EXTN) if i < length else {}
    atoms = ext.get(AVCC_KEY)
    avcc = atoms.get(AVCC_SUBKEY) if isinstance(atoms, dict) else None
    return {"media": "video", "width": width, "height": height, "codec": _name(codec),
            "avcc": avcc if isinstance(avcc, bytes) else None}


@_checked
def parse_sample_buffer(data, at=0):
    """A CMSampleBuffer (sbuf) -> {"pts": (value, scale), "format": parse_format() or None, "data": bytes,
    "samples": n}."""
    length, _ = _head(data, at, SBUF)
    i, end = at + 8, at + length
    out = {"pts": None, "format": None, "data": b"", "samples": 0}
    while i < end:
        n, kind = _head(data, i)
        if i + n > end:
            raise ProtocolError("sample-buffer part past its buffer")
        if kind == OPTS:
            if n < 8 + 24:
                raise ProtocolError("short presentation time")
            out["pts"] = parse_time(data, i + 8)[:2]
        elif kind == SDAT:
            out["data"] = bytes(data[i + 8:i + n])
        elif kind == NSMP:
            if n < 12:
                raise ProtocolError("short sample count")
            out["samples"] = struct.unpack_from("<I", data, i + 8)[0]
        elif kind == FDSC:
            out["format"] = parse_format(data[i:i + n])
        elif kind not in (STIA, SATT, SARY, SSIZ):
            raise ProtocolError(f"unknown sample-buffer part {_name(kind)}")
        i += n
    return out


def nal_units(avcc_sample):
    """The NAL units of a length-prefixed (4-byte big-endian) access unit."""
    i, out = 0, []
    while i + 4 <= len(avcc_sample):
        n = struct.unpack_from(">I", avcc_sample, i)[0]
        if i + 4 + n > len(avcc_sample):
            raise ProtocolError("truncated NAL unit")
        out.append(avcc_sample[i + 4:i + 4 + n])
        i += 4 + n
    return out


def is_keyframe(avcc_sample):
    try:
        return any(u and u[0] & 0x1F == IDR for u in nal_units(avcc_sample))
    except ProtocolError:
        return False


@_checked
def avcc_parameter_sets(avcc):
    """The SPS and PPS NAL units of an AVCDecoderConfigurationRecord (ISO/IEC 14496-15), in that order."""
    if len(avcc) < 7 or avcc[0] != 1:
        raise ProtocolError("not an avcC record")
    out, i = [], 5
    for count_mask in (0x1F, 0xFF):
        count = avcc[i] & count_mask
        i += 1
        for _ in range(count):
            n = struct.unpack_from(">H", avcc, i)[0]
            if i + 2 + n > len(avcc):
                raise ProtocolError("truncated parameter set")
            out.append(bytes(avcc[i + 2:i + 2 + n]))
            i += 2 + n
    return out


# ---- messages ----------------------------------------------------------------------------------------------
def packet(magic, ref, subtype, payload=b""):
    """A SYNC/ASYN-style message with its length: magic, clock reference, subtype, payload."""
    body = struct.pack("<IQI", magic, ref, subtype) + payload
    return struct.pack("<I", len(body) + 4) + body


def reply(correlation, payload=b""):
    body = struct.pack("<IQI", RPLY, correlation, 0) + payload
    return struct.pack("<I", len(body) + 4) + body


def ping():
    return struct.pack("<IIQ", 16, PING, 0x0000000100000000)


def need(ref):
    return packet(ASYN, ref, NEED)


def video_request():
    """HPD1: the host wants video. The phone sends its native size whatever DisplaySize says."""
    return packet(ASYN, EMPTY, HPD1, pack_dict({
        "Valeria": True, "HEVCDecoderSupports444": True,
        "DisplaySize": {"Width": Number(1920.0), "Height": Number(1200.0)}}))


def audio_request(device_audio_clock):
    """HPA1: the host wants sound, as 48 kHz 16-bit stereo LPCM."""
    return packet(ASYN, device_audio_clock, HPA1, pack_dict({
        "BufferAheadInterval": Number(0.07300000000000001), "deviceUID": "Valeria", "ScreenLatency": Number(0.04),
        "formats": pack_asbd(default_asbd()), "EDIDAC3Support": Number(0), "deviceName": "Valeria"}))


class Clock:
    """A host clock in nanoseconds since it was made (what the phone syncs against)."""

    def __init__(self, ref, now=time.monotonic_ns):
        self.ref, self._now = ref, now
        self.start = now()

    def time(self):
        return self._now() - self.start


class Video:
    """One access unit: AVCC data (4-byte lengths), its presentation time in ns, whether it is a keyframe,
    and the stream's format when it came with one (width, height, avcc = AVCDecoderConfigurationRecord)."""
    __slots__ = ("data", "pts", "keyframe", "format")

    def __init__(self, data, pts, keyframe, format=None):
        self.data, self.pts, self.keyframe, self.format = data, pts, keyframe, format


class Audio:
    __slots__ = ("data", "pts", "asbd")

    def __init__(self, data, pts, asbd=None):
        self.data, self.pts, self.asbd = data, pts, asbd


class Released:
    """The phone released one of its clocks (after HPA0/HPD0)."""
    __slots__ = ("ref",)

    def __init__(self, ref):
        self.ref = ref


class Dropped:
    """A sample buffer that could not be read; the session goes on (a FEED is still answered with NEED)."""
    __slots__ = ("reason",)

    def __init__(self, reason):
        self.reason = reason


def _ns(pts):
    if not pts or not pts[1]:
        return 0
    return pts[0] * 1_000_000_000 // pts[1]


class Session:
    """The host side of one capture session. receive(message) -> (replies, events).

    The replies' clock references and their offsets follow quicktime_video_hack (the phone only checks that
    later messages use them); the skew answer compares the phone's audio clock with ours, as CoreMedia does.
    Without audio no HPA1 is sent, so the phone sends no EAT! and leaves its sound on its speaker.
    """

    def __init__(self, video=True, audio=False, now=time.monotonic_ns):
        self.want_video, self.want_audio = video, audio
        self._now = now
        self.clock = None                 # CLOK: what TIME replies read
        self.audio_clock = None           # CWPA: the local clock the skew is measured against
        self.device_audio_ref = None
        self.need_ref = None              # CVRP: every FEED is answered with NEED on this reference
        self._audio_start = None          # (device pts value, local ns) of the first audio sample
        self._audio_last = None
        self._audio_scale = 0             # the device pts timescale
        self.asbd = None
        self.counts = {"video": 0, "audio": 0}

    def receive(self, msg):
        """msg: one message without its leading length. Raises ProtocolError for a message it cannot read."""
        if len(msg) < 4:
            raise ProtocolError("short message")
        magic = struct.unpack_from("<I", msg)[0]
        if magic == PING:
            return [ping()], []
        if len(msg) < 16 or magic not in (SYNC, ASYN):
            raise ProtocolError(f"unknown message {_name(magic)}")
        ref, kind = struct.unpack_from("<QI", msg, 4)
        if magic == SYNC:
            return self._sync(kind, ref, msg), []
        return self._asyn(kind, ref, msg)

    @_checked
    def _sync(self, kind, ref, msg):
        if len(msg) < 24:
            raise ProtocolError("short SYNC message")
        corr = struct.unpack_from("<Q", msg, 16)[0]
        body = msg[24:]
        if kind == CWPA:
            device_ref = struct.unpack_from("<Q", body)[0]
            self.device_audio_ref = device_ref
            self.audio_clock = Clock(device_ref + 1000, self._now)
            out = [video_request(), video_request()] if self.want_video else []
            out.append(reply(corr, struct.pack("<Q", device_ref + 1000)))
            if self.want_audio:
                out.append(audio_request(device_ref))
            return out
        if kind == CVRP:
            self.need_ref = struct.unpack_from("<Q", body)[0]
            return [need(self.need_ref), reply(corr, struct.pack("<Q", self.need_ref + 0x1000AF))]
        if kind == CLOK:
            self.clock = Clock(ref + 0x10000, self._now)
            return [reply(corr, struct.pack("<Q", ref + 0x10000))]
        if kind == TIME:
            t = self.clock.time() if self.clock else 0
            return [reply(corr, pack_time(t))]
        if kind == AFMT:
            return [reply(corr, pack_dict({"Error": Number(0)}))]
        if kind == SKEW:
            return [reply(corr, struct.pack("<d", self.skew()))]
        if kind == OG:
            return [reply(corr, b"\x00\x00\x00\x00")]
        if kind == STOP:
            return [reply(corr, b"\x00\x00\x00\x00")]
        raise ProtocolError(f"unknown SYNC message {_name(kind)}")

    def skew(self):
        """The phone's audio clock rate as measured against ours: its timescale (48000) when they agree, as
        quicktime_video_hack's CalculateSkew; the timescale itself until two samples have arrived."""
        if not self._audio_start or not self._audio_last:
            return float(self._audio_scale or 48000)
        (d0, l0), (d1, l1) = self._audio_start, self._audio_last
        scale = self._audio_scale
        if d1 <= d0 or l1 <= l0:
            return float(scale)
        return scale * ((l1 - l0) * scale / 1e9) / (d1 - d0)

    def _asyn(self, kind, ref, msg):
        if kind == FEED:
            self.counts["video"] += 1
            out = [need(self.need_ref)] if self.need_ref is not None else []
            try:
                sbuf = parse_sample_buffer(msg, 16)
            except ProtocolError as e:      # still ask for the next one, as quicktime_video_hack does
                return out, [Dropped(f"video frame: {e}")]
            fmt = sbuf["format"] if sbuf["format"] and sbuf["format"]["media"] == "video" else None
            if not sbuf["data"] and not fmt:
                return out, []
            return out, [Video(sbuf["data"], _ns(sbuf["pts"]), is_keyframe(sbuf["data"]), fmt)]
        if kind == EAT:
            self.counts["audio"] += 1
            try:
                sbuf = parse_sample_buffer(msg, 16)
            except ProtocolError as e:
                return [], [Dropped(f"audio samples: {e}")]
            if sbuf["format"] and sbuf["format"]["media"] == "audio":
                self.asbd = sbuf["format"]["asbd"]
            local = self.audio_clock.time() if self.audio_clock else 0
            if sbuf["pts"] and sbuf["pts"][1]:
                self._audio_scale = sbuf["pts"][1]
                if self._audio_start is None:
                    self._audio_start = (sbuf["pts"][0], local)
                self._audio_last = (sbuf["pts"][0], local)
            return [], [Audio(sbuf["data"], _ns(sbuf["pts"]), self.asbd)] if sbuf["data"] else []
        if kind == RELS:
            return [], [Released(ref)]
        if kind in (SPRP, TJMP, SRAT, TBAS):
            return [], []
        raise ProtocolError(f"unknown ASYN message {_name(kind)}")

    def stop(self):
        """What to send to end the session: stop sound (only if it was asked for), stop video; the phone then
        releases its clocks (RELS), and the host sends HPD0 once more (see screencap.Capture.close)."""
        out = []
        if self.want_audio and self.device_audio_ref is not None:
            out.append(packet(ASYN, self.device_audio_ref, HPA0))
        out.append(packet(ASYN, EMPTY, HPD0))
        return out
