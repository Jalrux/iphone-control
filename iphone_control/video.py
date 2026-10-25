"""The window's picture: GStreamer pipelines ending in a decoder named dec, the scaler's caps filter named fit
and gtksink named sink (window.py relies on the three names).

  MjpegSource    WebDriverAgent's MJPEG stream (IPHONE_VIDEO=mjpeg, full mode only)
  H264Source     the USB screen capture: screencap.py's socket, no Developer Mode needed (IPHONE_VIDEO=usb)
  AirplaySource  AirPlay screen mirroring through UxPlay, which the source starts and stops (IPHONE_VIDEO=airplay)
  MjpegDrain     not a picture: keeps WebDriverAgent's stream running, unseen, while the USB capture is shown

Every source keeps its last decoded frame (a probe on dec's src pad, before the scaler, so at the phone's own
resolution): screenshots without WebDriverAgent. on_lost(why) and on_status(text) run on the GLib main loop;
the window restarts a lost source with its backoff.
"""
import atexit
import ctypes
import http.client
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import threading
import urllib.parse

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from . import screencap  # noqa: E402

SINK = "gtksink name=sink sync=false"
# drops decoded frames rather than building up delay (compressed H.264 cannot be dropped: every frame after a
# lost one would be broken until the next keyframe)
LEAKY = "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream"
# Frames are shrunk to the picture's size in device pixels on GStreamer's thread (videoscale, same package as
# videoconvert; without it gtksink scales them). Measured offline with 1178x2556 frames into 748x1620: cairo's
# FILTER_GOOD downscale in gtksink took ~20 ms a frame on the GTK main thread (24 ms from frame to drawn, input
# events waited up to 28 ms); Lanczos first: 10.5 ms, events wait at most 14 ms, same sharpness (SSIM 0.997).
# Bilinear is a little cheaper but visibly softer.
SCALER = "videoscale"
SCALE_THREADS = min(4, os.cpu_count() or 1)

GST_ELEMENTS = ("souphttpsrc", "multipartdemux", "queue", "jpegdec", "videoconvert", "gtksink")
GST_PACKAGES = ("gtksink: Debian/Ubuntu gstreamer1.0-gtk3, Fedora gstreamer1-plugins-good-gtk, "
                "openSUSE gstreamer-plugins-good-gtk, Arch gst-plugin-gtk; videoconvert: "
                "gstreamer1.0-plugins-base / gstreamer1-plugins-base / gstreamer-plugins-base / "
                "gst-plugins-base; the others: gstreamer1.0-plugins-good / gstreamer1-plugins-good / "
                "gstreamer-plugins-good / gst-plugins-good; souphttpsrc also needs libsoup 3 (libsoup-3.0-0 / "
                "libsoup3 / libsoup-3_0-0 / libsoup3)")
# souphttpsrc missing but its siblings in plugins-good present: the plugin is there, libsoup is not
# (GStreamer 1.22+ loads libsoup at run time, and some packages do not depend on it)
LIBSOUP_HINT = ("souphttpsrc is installed but cannot load libsoup 3 - install Debian/Ubuntu libsoup-3.0-0, "
                "Fedora libsoup3, openSUSE libsoup-3_0-0 or Arch libsoup3")
H264_PACKAGES = ("h264parse: Debian/Ubuntu gstreamer1.0-plugins-bad, Fedora gstreamer1-plugins-bad-free, "
                 "openSUSE gstreamer-plugins-bad, Arch gst-plugins-bad; appsrc, videoconvert: the base plugins; "
                 "udpsrc, rtph264depay, gtksink: the good plugins (gtksink: gstreamer1.0-gtk3 on Debian/Ubuntu)")
DECODER_HINT = ("no GStreamer H.264 decoder: install gstreamer1.0-libav (Debian/Ubuntu), gstreamer1-plugin-openh264 "
                "(Fedora), gstreamer-plugins-libav (openSUSE, Packman) or gst-libav (Arch)")
# Hardware first. nvh264dec only when IPHONE_DECODER names it: NVIDIA's decoder has a few frames of latency.
DECODERS = ("vah264dec", "avdec_h264", "openh264dec")
H264_CAPS = "video/x-h264,stream-format=avc,alignment=au"
STALL_S = 10             # no frame on the capture socket for this long: lost (the phone sends 40-50 fps)
UXPLAY_MIN = (1, 73)     # the first UxPlay with -vrtp
RTP_CAPS = "application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=96"
PR_SET_PDEATHSIG = 1     # prctl(2)


def scaler():
    """Pipeline text between the decoder and videoconvert: the scaler and its size filter, or nothing."""
    if not Gst.ElementFactory.find(SCALER):
        return ""
    threads = ""
    probe = Gst.ElementFactory.make(SCALER, None)
    if probe and probe.find_property("n-threads"):                     # GStreamer 1.20 and later
        threads = f" n-threads={SCALE_THREADS}"
    return f"{SCALER} method=lanczos{threads} ! capsfilter name=fit caps=video/x-raw ! "


def fit_caps(width, height):
    """Caps that make the scaler fit frames inside width x height device pixels; any size while not laid out."""
    if width < 50 or height < 50:
        return "video/x-raw"
    return f"video/x-raw,width=(int)[1,{int(width)}],height=(int)[1,{int(height)}],pixel-aspect-ratio=(fraction)1/1"


def decoder():
    """The H.264 decoder element: IPHONE_DECODER if it is installed, else the first of DECODERS; None if none."""
    want = os.environ.get("IPHONE_DECODER", "").strip()
    if want:
        return want if re.match(r"^[a-z0-9_]+$", want) and Gst.ElementFactory.find(want) else None
    return next((d for d in DECODERS if Gst.ElementFactory.find(d)), None)


def h264_caps(width, height, avcc):
    """appsrc caps for a VIDEO_FORMAT frame: access units with 4-byte lengths, described by their avcC record."""
    return Gst.Caps.from_string(f"{H264_CAPS},width=(int){width},height=(int){height},"
                                f"codec_data=(buffer){avcc.hex()}")


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def die_with_parent():
    """A preexec_fn for a child that must not outlive this process, also when it is killed or crashes (atexit
    does not run then): the kernel sends the child SIGTERM when the thread that started it ends, so start it from
    the main thread. None where prctl cannot be called."""
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl      # looked up here: the child only calls it
    except (OSError, AttributeError):
        return None
    parent = os.getpid()

    def preexec():
        prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM))
        if os.getppid() != parent:                           # it died before prctl took effect
            os._exit(0)
    return preexec


def uxplay_version():
    """(major, minor) from `uxplay -v`, or None if it cannot be told."""
    try:
        out = subprocess.run(["uxplay", "-v"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(?i)uxplay\D{0,10}(\d+)\.(\d+)", out.stdout)
    return tuple(map(int, m.groups())) if m else None


def uxplay_problem():
    """Why AirplaySource cannot run here (a message for the user), or None."""
    if not shutil.which("uxplay"):
        return ("uxplay is not installed: UxPlay 1.73+ is needed for AirPlay mirroring (Arch AUR uxplay, "
                "Fedora 43 uxplay; elsewhere build it from https://github.com/FDH2/UxPlay)")
    ver = uxplay_version()
    if not ver or ver < UXPLAY_MIN:
        have = ".".join(map(str, ver)) if ver else "unknown"
        return (f"UxPlay 1.73+ is needed for -vrtp (found {have}; Debian 13 ships 1.71, Ubuntu 24.04 1.68): "
                "build it from https://github.com/FDH2/UxPlay")
    return None


class VideoSource:
    """A pipeline that ends in ... ! dec ! ... ! fit ! videoconvert ! sink; start() / stop() any number of times."""
    ELEMENTS = ("queue", "videoconvert", "gtksink")
    H264 = False                          # decodes H.264: needs one of DECODERS (or IPHONE_DECODER)
    title = "iPhone (USB)"

    def __init__(self, on_lost, on_status=None, sink=SINK):
        self.on_lost = on_lost            # on_lost(why), main thread: the window stops it and retries later
        self.on_status = on_status or (lambda text: print(text, flush=True))
        self.sink_text = sink
        self.frames = 0                   # decoded so far
        self._last = None                 # (buffer, caps) of the newest decoded frame
        self.pipeline = Gst.parse_launch(self.launch())
        self.fit = self.pipeline.get_by_name("fit")       # None without videoscale
        self.pipeline.get_by_name("dec").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._keep)
        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()      # bus messages arrive on the main loop
        self._bus.connect("message::error", lambda _b, m: self.on_lost(m.parse_error()[0].message))
        self._bus.connect("message::eos", lambda _b, _m: self.on_lost("the stream ended"))

    @classmethod
    def missing(cls):
        """GStreamer elements this source needs that are not installed."""
        gone = [e for e in cls.ELEMENTS if not Gst.ElementFactory.find(e)]
        return gone + ["an H.264 decoder"] if cls.H264 and not decoder() else gone

    def launch(self):
        raise NotImplementedError

    @property
    def widget(self):
        return self.pipeline.get_by_name("sink").props.widget

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    def close(self):
        self.stop()
        self._bus.remove_signal_watch()

    def _keep(self, pad, info):           # streaming thread: a reference, no copy
        caps = pad.get_current_caps()
        if caps:
            self._last = (info.get_buffer(), caps)
            self.frames += 1
        return Gst.PadProbeReturn.OK

    def last_frame_png(self, done):
        """done(png_or_None) on a short-lived thread: the newest decoded frame, full size, as a PNG."""
        last = self._last
        if not last:
            return done(None)
        threading.Thread(target=self._encode, args=(last, done), name="png", daemon=True).start()

    @staticmethod
    def _encode(last, done):
        png, p = None, None
        try:
            p = Gst.parse_launch("appsrc name=src format=time ! videoconvert ! pngenc snapshot=true ! "
                                 "appsink name=out sync=false")
            src = p.get_by_name("src")
            src.set_property("caps", last[1])
            p.set_state(Gst.State.PLAYING)
            frame = last[0].copy()        # shares the memory; the live pipeline's timestamps mean nothing here
            frame.pts = frame.dts = Gst.CLOCK_TIME_NONE
            src.emit("push-buffer", frame)
            src.emit("end-of-stream")
            sample = p.get_by_name("out").emit("try-pull-sample", 5 * Gst.SECOND)
            if sample:
                buf = sample.get_buffer()
                ok, info = buf.map(Gst.MapFlags.READ)
                if ok:
                    png = bytes(info.data)
                    buf.unmap(info)
        except GLib.Error as e:           # e.g. pngenc missing
            print(f"frame not encoded: {e.message}", flush=True)
        finally:
            if p:
                p.set_state(Gst.State.NULL)
        done(png)


class MjpegSource(VideoSource):
    """WebDriverAgent's MJPEG stream (full mode). The leaky queue drops old JPEGs rather than building up delay."""
    ELEMENTS = GST_ELEMENTS

    def __init__(self, url, on_lost, on_status=None, sink=SINK):
        self.url = url
        super().__init__(on_lost, on_status, sink)
        # multipartdemux makes a new source pad every time the stream restarts, so it is linked by hand
        # (gst-launch's delayed link happens only once)
        queue_in = self.pipeline.get_by_name("q").get_static_pad("sink")
        self.pipeline.get_by_name("demux").connect(
            "pad-added", lambda _d, pad: queue_in.is_linked() or pad.link(queue_in))

    def launch(self):
        # timeout=5: a stalled socket is noticed in 5 s; the leaky queue keeps the socket reader moving
        return (f'souphttpsrc location="{self.url}" is-live=true do-timestamp=true timeout=5 retries=1 ! '
                "multipartdemux name=demux "
                "queue name=q max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                f"jpegdec name=dec ! {scaler()}videoconvert ! {self.sink_text}")


class H264Source(VideoSource):
    """The USB screen capture (IPHONE_CAPTURE_SOCKET, see screencap.py): a reader thread pushes its access units
    into appsrc. VIDEO_FORMAT sets the caps (a new one when an app rotates the screen: 1180x2556 turns 2556x1180
    on an iPhone 15 Pro), VIDEO frames are pushed as they come, STATUS text goes to on_status. The capture sends
    no B-frames, so the decoder gives every frame out at once, also the last one before the screen goes still.
    On connect the capture process replays the format, its status and the frames since the last keyframe, so a
    connection's first picture with no STATUS before it means the capture has none: on_status("") then clears what
    an earlier connection (or capture process) left on screen."""
    ELEMENTS = ("appsrc", "h264parse") + VideoSource.ELEMENTS
    H264 = True

    def __init__(self, path, on_lost, on_status=None, sink=SINK):
        self.path, self.decoder = path, decoder()
        super().__init__(on_lost, on_status, sink)
        self.src = self.pipeline.get_by_name("src")
        self._lock = threading.Lock()
        self._gen, self._sock = 0, None   # a new generation per start(): a stopped reader reports nothing

    def launch(self):
        return (f"appsrc name=src is-live=true format=time do-timestamp=true caps={H264_CAPS} ! h264parse ! "
                f"{self.decoder} name=dec ! {LEAKY} ! {scaler()}videoconvert ! {self.sink_text}")

    def start(self):
        self.src.set_property("caps", Gst.Caps.from_string(H264_CAPS))   # until the capture says more
        super().start()
        with self._lock:
            self._gen += 1
            gen = self._gen
        threading.Thread(target=self._read, args=(gen,), name="capture", daemon=True).start()

    def stop(self):
        with self._lock:
            self._gen += 1
            sock, self._sock = self._sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)   # wakes the reader
            except OSError:
                pass
        super().stop()

    def _read(self, gen):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(STALL_S)
        with self._lock:
            if gen != self._gen:
                return sock.close()
            self._sock = sock
        why, caps, told = "the USB capture closed its socket", None, False   # told: a status on this connection
        try:
            sock.connect(self.path)
            for kind, _pts, payload in screencap.read_frames(sock):
                pushed = False
                with self._lock:                  # after stop() returns, nothing more reaches appsrc
                    if gen != self._gen:
                        break
                    if kind == screencap.VIDEO_FORMAT and len(payload) > 8:
                        width, height = struct.unpack_from("<II", payload)
                        new = h264_caps(width, height, payload[8:])
                        if not (caps and caps.is_equal(new)):
                            caps = new
                            self.src.set_property("caps", caps)
                    elif kind == screencap.VIDEO and caps and len(payload) > 1:
                        buf = Gst.Buffer.new_wrapped(payload[1:])
                        if not payload[0]:
                            buf.set_flags(Gst.BufferFlags.DELTA_UNIT)
                        self.src.emit("push-buffer", buf)     # queued; never blocks
                        pushed = True
                if kind == screencap.STATUS or (pushed and not told):
                    told = True
                    text = payload.decode("utf-8", "replace") if kind == screencap.STATUS else ""
                    GLib.idle_add(self._status, gen, text)
        except socket.timeout:
            why = f"no picture from the USB capture for {STALL_S} s"
        except OSError as e:
            why = f"USB capture socket {self.path}: {e.strerror or e}"
        finally:
            sock.close()
        GLib.idle_add(self._lost, gen, why)

    def _lost(self, gen, why):
        if gen == self._gen:
            self.on_lost(why)
        return False

    def _status(self, gen, text):
        if gen == self._gen:
            self.on_status(text)
        return False


class AirplaySource(VideoSource):
    """AirPlay screen mirroring (basic mode without the USB capture): UxPlay 1.73+ receives it and, with -vrtp,
    forwards the phone's H.264 untouched over RTP to a local UDP port; this pipeline decodes it. The source starts
    uxplay (log: log_path) on its first start(), again after it exited, and stops it on close(). uxplay gets
    SIGTERM when this process ends in any way (die_with_parent), and its pid is in pid_path while it runs, for
    iphone.sh. AirPlay sends frames only while the screen changes, so the last frame stays up (and is what a
    screenshot saves)."""
    ELEMENTS = ("udpsrc", "rtph264depay", "h264parse") + VideoSource.ELEMENTS
    H264 = True
    title = "iPhone (AirPlay)"

    def __init__(self, name, on_lost, on_status=None, log_path=os.devnull, sink=SINK, port=None, pid_path=None):
        self.name, self.log_path, self.pid_path = name, log_path, pid_path
        self.port = port or free_udp_port()
        self.decoder = decoder()
        self.proc, self._watch, self._log_opened = None, 0, False
        super().__init__(on_lost, on_status, sink)
        atexit.register(self._stop_uxplay)        # fail() and sys.exit must not leave a receiver running

    def launch(self):
        return (f'udpsrc address=127.0.0.1 port={self.port} caps="{RTP_CAPS}" ! rtph264depay ! h264parse ! '
                f"{self.decoder} name=dec ! {LEAKY} ! {scaler()}videoconvert ! {self.sink_text}")

    def command(self):
        cmd = ["uxplay", "-n", self.name, "-nh", "-p",
               "-vrtp", f"config-interval=1 ! udpsink host=127.0.0.1 port={self.port}"]
        return (["stdbuf", "-oL", "-eL"] if shutil.which("stdbuf") else []) + cmd    # line-buffered log

    def start(self):
        if not self.proc:
            self._start_uxplay()
        super().start()

    def close(self):
        super().close()
        self._stop_uxplay()

    def _start_uxplay(self):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if self._log_opened else os.O_TRUNC)
        log = os.fdopen(os.open(self.log_path, flags, 0o600), "w")
        self._log_opened = True
        try:
            # it listens on the LAN and on fixed ports (-p): it must never outlive the window
            self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, preexec_fn=die_with_parent())
        except (OSError, subprocess.SubprocessError) as e:
            why = f"uxplay did not start: {getattr(e, 'strerror', None) or e}"
            return GLib.idle_add(lambda: (self.on_lost(why), False)[1])
        finally:
            log.close()
        self._pid_file(self.proc.pid)
        print(f"AirPlay receiver '{self.name}' (log {self.log_path})", flush=True)
        self._watch = GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid, self._exited)

    def _pid_file(self, pid):
        """uxplay's pid into pid_path (None: remove it), where iphone.sh finds it to stop and report it."""
        if not self.pid_path:
            return
        try:
            if pid is None:
                os.unlink(self.pid_path)
            else:
                with os.fdopen(os.open(self.pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                    f.write(f"{pid}\n")
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"uxplay's pid file {self.pid_path}: {e.strerror or e}", flush=True)

    def _exited(self, _pid, status):
        self.proc, self._watch = None, 0
        self._pid_file(None)
        try:
            with open(self.log_path, errors="replace") as f:
                tail = "".join(f.readlines()[-5:])
        except OSError:
            tail = ""
        print(f"uxplay exited ({status}); last lines of {self.log_path}:\n{tail}", flush=True)
        self.on_lost("uxplay exited")

    def _stop_uxplay(self):
        proc, self.proc = self.proc, None
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        if not proc:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._pid_file(None)


class MjpegDrain:
    """Stays connected to WebDriverAgent's MJPEG port and throws the frames away (full mode with IPHONE_VIDEO=usb,
    where the USB capture is shown instead). WebDriverAgent captures the screen only while a client is connected,
    and its touches need that capture at ~30 fps: a swipe's first moved frame came after 294 ms at 1 fps, 245 ms
    at 10 and 173-177 ms at 30 (iPhone 15 Pro, iOS 26.5.2, Oct 2026). Reconnects with backoff, 0.5 s up to 5 s."""

    def __init__(self, url, log=print):
        u = urllib.parse.urlsplit(url)
        self.host, self.port, self.path = u.hostname or "127.0.0.1", u.port or 80, u.path or "/"
        self.log = log
        self.connects = self.received = 0          # connections made, bytes thrown away
        self._stop = threading.Event()
        self._conn = None

    def start(self):
        threading.Thread(target=self._run, name="mjpeg-drain", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        conn = self._conn
        if conn and conn.sock:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)   # wakes the reader
            except OSError:
                pass

    def _run(self):
        backoff, quiet = 0.5, False
        while not self._stop.is_set():
            got = 0
            self._conn = conn = http.client.HTTPConnection(self.host, self.port, timeout=STALL_S)
            try:
                conn.request("GET", self.path)
                r = conn.getresponse()
                if r.status != 200:
                    raise http.client.HTTPException(f"HTTP {r.status}")
                self.connects += 1
                while not self._stop.is_set():
                    data = r.read(1 << 16)
                    if not data:
                        break
                    got += len(data)
                    self.received += len(data)
                why = "the stream ended"
            except (OSError, http.client.HTTPException) as e:
                why = str(e) or type(e).__name__
            finally:
                conn.close()
            if self._stop.is_set():
                break
            if got:
                backoff = 0.5
            if got or not quiet:                       # once per outage
                self.log(f"WebDriverAgent's hidden stream: {why} - reconnecting")
            quiet = not got
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 5)
