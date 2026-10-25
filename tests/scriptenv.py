"""A sandbox for running iphone.sh, resign.sh, setup.sh and uninstall.sh against fakes.

Every script runs from a copy of the repo in a temp dir, with a temp HOME and XDG dirs, a fake usbmuxd
(USBMUXD_SOCKET_ADDRESS), fake `ios` and `plumesign` in the temp ~/.local/bin, a fake window, a fake screen
capture (in the copy's iphone_control/), an empty fake sysfs (IPHONE_SYSFS) and /proc (IPHONE_PROC), stub busctl,
bluetoothctl, usbmuxd and uxplay (the copy's iphone.sh takes the stub usbmuxd for the system's), and a notify-send
that writes to a file. Nothing reaches a real iPhone, usbmuxd, Bluetooth, Apple, the network or the user's
desktop. The fakes follow go-ios v1.3.2's output formats (checked in its source).
"""
import datetime
import json
import os
import pwd
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time

from support import REPO, UDID_A, FakeMux, bundle_id, epoch, free_ports, make_signed_ipa

PY = sys.executable
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
BASH = shutil.which("bash", path=SYSTEM_PATH) or "bash"

# go-ios. Scenario files in $F: tunnel (ok|exit|notunnel), image_plan / wda_plan (one outcome per call),
# mounted, devmode, version. Every call is logged to $F/calls.log; long-lived fakes add their pid to $F/pids.
FAKE_IOS = r'''#!{py} -I
import http.server, json, os, sys, time
F = {fake!r}
args = sys.argv[1:]
def path(n): return os.path.join(F, n)
def read(n, d=""):
    try:
        with open(path(n)) as f: return f.read().strip()
    except OSError: return d
def plan(n, d="ok"):
    lines = read(n).splitlines()
    if not lines: return d
    with open(path(n), "w") as f: f.write("\n".join(lines[1:]))
    return lines[0].strip()
def log(level, msg, **kw):  # go-ios logs compact JSON lines (logrus) on stderr
    print(json.dumps(dict(level=level, msg=msg, **kw), separators=(",", ":")), file=sys.stderr, flush=True)
def err(msg, **kw): log("error", msg, **kw)
def info(msg, **kw): log("info", msg, **kw)
with open(path("calls.log"), "a") as f:
    f.write("ios " + " ".join(args) + " [GO_IOS_UDID=" + os.environ.get("GO_IOS_UDID", "") + "]\n")
def serve(port, handler):
    with open(path("pids"), "a") as f: f.write(f"{{os.getpid()}}\n")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body=b"", ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
cmd, sub = (args + ["", ""])[:2]
if cmd == "version":
    print(json.dumps({{"version": read("version", "v1.3.2")}}))
elif cmd == "tunnel" and sub == "start":
    if read("tunnel") == "exit":
        err("failed to start tunnel", err="could not connect to the device: DeviceLocked"); sys.exit(1)
    rec = next((a.split("=", 1)[1] for a in args if a.startswith("--pair-record-path=")), ".")
    ident = os.path.join(rec, "selfIdentity.plist")
    if not os.path.exists(ident):
        with open(ident, "w") as f: f.write("fake private key")
        os.chmod(ident, 0o644)                       # go-ios writes it world-readable
    udid = os.environ.get("GO_IOS_UDID", "")
    class Agent(Quiet):
        def do_GET(self):
            if self.path == "/health": return self.send(200, b"{{}}")
            if self.path == "/tunnel/" + udid and read("tunnel") != "notunnel":
                return self.send(200, json.dumps({{"address": "fd00::1", "rsdPort": 58783, "udid": udid,
                    "userspaceTun": True, "userspaceTunPort": 60106}}, separators=(",", ":")).encode())
            self.send(404, b"{{}}")
    serve(int(os.environ["GO_IOS_AGENT_PORT"]), Agent)
elif cmd == "image" and sub == "list":
    if os.path.exists(path("mounted")): info("image signature", signature="00ff")
    else: info("none")
elif cmd == "image" and sub == "auto":
    with open(path("ssl_cert_file"), "w") as f: f.write(os.environ.get("SSL_CERT_FILE", ""))
    p = plan("image_plan")
    if p == "locked": err("failed mounting image", err="DeviceLocked"); sys.exit(1)
    if p == "download_fail": err("failed downloading image", err="Get \"https://x\": dial tcp"); sys.exit(0)
    if p == "error": err("failed mounting image", err="InternalError"); sys.exit(1)
    open(path("mounted"), "w").close(); info("success mounting image")
elif cmd == "devmode" and sub == "get":
    print(json.dumps({{"DeveloperModeEnabled": read("devmode", "true") == "true"}}, separators=(",", ":")))
elif cmd == "runwda":
    p = plan("wda_plan")
    if p == "missing-until-resign":
        p = "ok" if os.path.exists(path("resigned")) else "missing"
    msgs = {{"missing": "cannot get test app information: no app with bundle id",
            "expired": "ApplicationVerificationFailed: the profile has expired",
            "untrusted": "the developer profile has not been explicitly trusted by the user",
            "locked": "the device is locked"}}
    print('{{"level":"info","msg":"starting WDA"}}', flush=True)
    if p in msgs:
        time.sleep(0.3); err("Failed running WDA", error=msgs[p]); sys.exit(1)
    if p == "silent":
        with open(path("pids"), "a") as f: f.write(f"{{os.getpid()}}\n")
        while True: time.sleep(60)
    import threading
    class Mjpeg(Quiet):
        def do_GET(self):
            self.send(200, b"--BoundaryString\r\nContent-type: image/jpeg\r\nContent-Length: 2\r\n\r\nxx\r\n",
                      "multipart/x-mixed-replace; boundary=--BoundaryString")
    threading.Thread(target=serve, args=(int(read("phone_mjpeg_port")), Mjpeg), daemon=True).start()
    class Wda(Quiet):
        def do_GET(self):
            # like the real WDA: build.version (WDA's own) comes before os.version, pretty-printed
            self.send(200, json.dumps({{"value": {{"build": {{"version": "16.15.2"}}, "ready": True,
                                                   "os": {{"name": "iOS", "version": "26.5.2"}}}},
                                        "sessionId": None}}, indent=2).encode())
    serve(int(read("phone_wda_port")), Wda)
else:
    print("fake ios: unhandled " + " ".join(args), file=sys.stderr); sys.exit(1)
'''

# plumesign. Scenario files: nologin, plume_plan (ok|fail:<text>|locked|session); signed.ipa is copied to -o.
FAKE_PLUMESIGN = r'''#!{py} -I
import os, shutil, sys
F = {fake!r}
args = sys.argv[1:]
def path(n): return os.path.join(F, n)
with open(path("calls.log"), "a") as f: f.write("plumesign " + " ".join(args) + "\n")
if args[:2] == ["account", "list"]:
    if os.path.exists(path("nologin")):
        print("[INFO] No accounts found. Use 'account login' to add an account.", file=sys.stderr)
    else:
        print("[INFO] Saved accounts: someone@example.com", file=sys.stderr)
elif args[:2] == ["account", "logout"]:
    pass
elif args[:1] == ["sign"]:
    print("[INFO] Restoring session for someone@example.com...", file=sys.stderr)
    try:
        with open(path("plume_plan")) as f: p = f.read().strip() or "ok"
    except OSError: p = "ok"
    if p == "locked": print("[ERROR] Install failed: DeviceLocked (the device is locked)", file=sys.stderr); sys.exit(1)
    if p == "session": print("[ERROR] error: token expired, please log in again (-22406)", file=sys.stderr); sys.exit(1)
    if p.startswith("fail:"): print("[ERROR] " + p[5:], file=sys.stderr); sys.exit(1)
    shutil.copy(path("signed.ipa"), args[args.index("-o") + 1])
    open(path("resigned"), "w").close()
    print("[INFO] Installation complete!", file=sys.stderr)
else:
    print("fake plumesign: unhandled", args, file=sys.stderr); sys.exit(1)
'''

# The window: checks WebDriverAgent through the forward, writes what it saw, then window_mode decides
FAKE_WINDOW = r'''import json, os, signal, stat, sys, time, urllib.request
F = {fake!r}
with open(os.path.join(F, "pids"), "a") as f: f.write(f"{{os.getpid()}}\n")
url = os.environ.get("IPHONE_WDA_URL", "")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
try:
    status = json.load(opener.open(url + "/status", timeout=5))["value"]
except Exception as e:
    status = {{"error": repr(e)}}
seen = {{k: os.environ.get(k) for k in ("IPHONE_WDA_URL", "IPHONE_MJPEG_URL", "NO_PROXY", "no_proxy", "GO_IOS_UDID",
                                     "IPHONE_MODE", "IPHONE_VIDEO", "IPHONE_CAPTURE_SOCKET", "IPHONE_FROM_SCRIPT",
                                     "IPHONE_DOUBLE_CLICK_MS", "IPHONE_AUDIO", "IPHONE_DECODER", "IPHONE_HID_NAME",
                                     "IPHONE_AIRPLAY_NAME", "BT_INDEX")}}
sock = os.environ.get("IPHONE_CAPTURE_SOCKET")
seen["capture_socket"] = bool(sock) and os.path.exists(sock) and stat.S_ISSOCK(os.stat(sock).st_mode)
seen["argv"] = sys.argv[1:]
seen["status"] = status
seen["stdin_tty"] = os.isatty(0)
with open(os.path.join(F, "window.json.tmp"), "w") as f: json.dump(seen, f)
os.replace(os.path.join(F, "window.json.tmp"), os.path.join(F, "window.json"))
try:
    with open(os.path.join(F, "window_mode")) as f: mode = f.read().strip()
except OSError:
    mode = "exit0"
if mode == "stay":
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    while True: time.sleep(60)
if mode.startswith("exit"):
    print("fake window error line", file=sys.stderr)
    sys.exit(int(mode[4:]))
'''

# The capture process: a stand-in for iphone_control/screencap.py in the sandbox's repo copy, with its main(argv) and
# exit codes, so iphone.sh runs it through its own command line (and records the exit status as it does for the real
# one). cap_plan: one outcome per start (ok, silent, reset, stall-reset, error, unplugged, unsupported, locked).
# Returns 2 when usbmuxd stops listing the phone; on TERM it notes which pid files are still there (cap_term.json),
# waits cap_term_delay seconds and logs "screencap stopped".
FAKE_SCREENCAP = r'''import json, os, signal, socket, sys, time
from . import usbmux
F = {fake!r}
def path(n): return os.path.join(F, n)
def read(n, d=""):
    try:
        with open(path(n)) as f: return f.read().strip()
    except OSError: return d
def plan(n, d="ok"):
    lines = read(n).splitlines()
    if not lines: return d
    with open(path(n), "w") as f: f.write("\n".join(lines[1:]))
    return lines[0].strip()
def note(line):
    with open(path("calls.log"), "a") as f: f.write(line + "\n")
def log(m): print("screencap: " + m, file=sys.stderr, flush=True)
def main(opts):
    note("screencap " + " ".join(opts))
    with open(path("pids"), "a") as f: f.write(f"{{os.getpid()}}\n")
    udid, sock = opts[opts.index("--udid") + 1], opts[opts.index("--socket") + 1]
    p = plan("cap_plan")
    fails = {{"error": (1, "error: [Errno 71] Protocol error"), "unplugged": (2, "the iPhone went away"),
             "unsupported": (3, "the iPhone offers screen capture, but usbmuxd keeps it in USB configuration 4"),
             "locked": (4, "the iPhone sends no picture - unlock it; if it stays black, restart the iPhone")}}
    if p in fails:
        time.sleep(0.2); log(fails[p][1]); return fails[p][0]
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *a: stop.append(1))
    if p == "reset":                 # a new session's USB reset: the test takes the phone off usbmuxd meanwhile
        open(path("cap_reset"), "w").close()
        while os.path.exists(path("cap_reset")) and not stop: time.sleep(0.05)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(sock)
    srv.listen(4)
    if p != "silent":
        log("capturing"); print("capturing", flush=True)
    since, gone = time.monotonic(), 0
    while not stop:
        time.sleep(0.3)
        if p == "stall-reset" and time.monotonic() - since > 1:   # its stall watchdog resets the phone's USB
            p, since = "ok", time.monotonic() + 5                      # waits for the phone to come back
            log("no picture from the iPhone for 5 s - starting a new capture")
            log("resetting the iPhone's USB connection to start a new capture")
            open(path("cap_selfreset"), "w").close()                # the test takes the phone away meanwhile
        try: there = usbmux.device_id(udid) is not None
        except OSError: there = True
        gone = 0 if there or time.monotonic() - since < 5 else gone + 1    # re-enumerating after its reset
        if gone >= 2:
            log("the iPhone was unplugged"); return 2
    with open(path("cap_term.json"), "w") as f:
        json.dump(sorted(n for n in os.listdir(os.path.dirname(sock)) if n.endswith(".pid")), f)
    time.sleep(float(read("cap_term_delay", "0") or 0))
    note("screencap stopped")
    return 0
'''

# IPHONE_PYTHON: the real interpreter, except the GTK/GStreamer probes (answered "present" unless $F/no_gi, or for
# the elements listed in $F/gst_missing; "h264dec" is missing when it is listed or when every decoder the probe may
# use is), and the window (the fake above). setup.sh's stdin probe prints $F/probe when that file exists.
PY_WRAPPER = r'''#!/bin/bash
F={fake}
case "$*" in
  *wda_mirror.py*) exec {py} -I "$F/fake_window.py" "$@" ;;
esac
if [ "${{1:-}}" = -I ] && [ "${{2:-}}" = -c ]; then
  case "${{3:-}}" in
    *ElementFactory*)                # iphone.sh's gst_missing: -I -c CODE DECODERS ELEMENT...
      [ -e "$F/no_gi" ] && exit 1
      dec=$4; shift 4
      for e in "$@"; do
        miss=0; grep -qxF -- "$e" "$F/gst_missing" 2>/dev/null && miss=1
        if [ "$e" = h264dec ] && [ $miss = 0 ]; then
          miss=1; for d in $dec; do grep -qxF -- "$d" "$F/gst_missing" 2>/dev/null || miss=0; done
        fi
        [ $miss = 0 ] || printf '%s ' "$e"
      done
      echo; exit 0 ;;
    *"import gi"*) [ -e "$F/no_gi" ] && exit 1; exit 0 ;;
  esac
fi
if [ "$*" = "-I -" ] && [ -e "$F/probe" ]; then cat >/dev/null; cat "$F/probe"; exit 0; fi
exec {py} "$@"
'''


# Stubs on PATH, so no test asks the real system bus, Bluetooth, usbmuxd or uxplay (sb.hide() takes one away).
# busctl answers from $F/bluez (one "hciN [le]" line per adapter; no file: bluetoothd is not running); usbmuxd
# --version prints $F/usbmuxd_version (default 1.1.1).
FAKE_BUSCTL = r'''#!/bin/sh
F={fake}
echo "busctl $*" >> "$F/calls.log"
[ -f "$F/bluez" ] || {{ echo "Failed to introspect object /: The name is not activatable" >&2; exit 1; }}
for a in "$@"; do last=$a; done
case " $* " in
  *" tree org.bluez "*) printf '/\n/org\n/org/bluez\n'; awk '{{print "/org/bluez/" $1}}' "$F/bluez" ;;
  *" introspect org.bluez "*)
    printf 'NAME TYPE SIGNATURE RESULT/VALUE FLAGS\norg.bluez.Adapter1 interface - - -\n'
    grep -q "^${{last##*/}} le" "$F/bluez" &&
      printf 'org.bluez.GattManager1 interface - - -\norg.bluez.LEAdvertisingManager1 interface - - -\n'
    exit 0 ;;
  *) exit 1 ;;
esac
'''
FAKE_BLUETOOTHCTL = '#!/bin/sh\necho "bluetoothctl $*" >> "{fake}/calls.log"\nexit 1\n'
FAKE_UXPLAY = '#!/bin/sh\necho "uxplay $*" >> "{fake}/calls.log"\necho "UxPlay 1.73: An open-source AirPlay server."\n'
FAKE_USBMUXD = ('#!/bin/sh\necho "usbmuxd $*" >> "{fake}/calls.log"\n'
                '[ "$1" = --version ] && echo "usbmuxd $(cat "{fake}/usbmuxd_version" 2>/dev/null || echo 1.1.1)"\n')


def write_exec(path, text, mode=0o755):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


class Sandbox:
    def __init__(self, devices=((7, UDID_A),), ios_version="26.5.2", paired=None, repo_name="repo"):
        self.root = tempfile.mkdtemp(prefix="iphone-control-test-")
        self.repo = os.path.join(self.root, repo_name)
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns(
            ".git", "build", "__pycache__", "*.pyc", "tests", ".github", "alt"))
        os.makedirs(os.path.join(self.repo, "build"))
        self.home = os.path.join(self.root, "home")
        self.fake = os.path.join(self.root, "fake")
        self.fakebin = os.path.join(self.root, "fakebin")
        self.bin = os.path.join(self.home, ".local", "bin")
        self.xdg_state = os.path.join(self.home, ".local", "state")
        self.state = os.path.join(self.xdg_state, "iphone-control")
        self.xdg_run = os.path.join(self.root, "run")
        self.rundir = os.path.join(self.xdg_run, "iphone-control")
        self.config_home = os.path.join(self.home, ".config")
        self.cache_home = os.path.join(self.home, ".cache")
        self.data_home = os.path.join(self.home, ".local", "share")
        for d in (self.fake, self.fakebin, self.bin, self.config_home):
            os.makedirs(d, exist_ok=True)
        os.makedirs(self.xdg_run, mode=0o700)
        (self.wda_port, self.mjpeg_port, self.agent_port, self.phone_wda_port,
         self.phone_mjpeg_port) = free_ports(5)
        self.put("phone_wda_port", str(self.phone_wda_port))
        self.put("phone_mjpeg_port", str(self.phone_mjpeg_port))
        self.mux = FakeMux(devices, ports={8100: self.phone_wda_port, 9100: self.phone_mjpeg_port},
                           ios_version=ios_version, paired=paired)
        fmt = {"py": PY, "fake": self.fake}
        write_exec(os.path.join(self.bin, "ios"), FAKE_IOS.format(**fmt))
        write_exec(os.path.join(self.bin, "plumesign"), FAKE_PLUMESIGN.format(**fmt))
        write_exec(os.path.join(self.fake, "fake_window.py"), FAKE_WINDOW.format(**fmt), 0o644)
        write_exec(os.path.join(self.repo, "iphone_control", "screencap.py"), FAKE_SCREENCAP.format(**fmt), 0o644)
        for name, text in (("busctl", FAKE_BUSCTL), ("bluetoothctl", FAKE_BLUETOOTHCTL), ("usbmuxd", FAKE_USBMUXD),
                           ("uxplay", FAKE_UXPLAY)):
            write_exec(os.path.join(self.fakebin, name), text.format(**fmt))
        self.sysfs = os.path.join(self.root, "sysfs")
        os.makedirs(self.sysfs)
        self.proc = os.path.join(self.root, "proc")       # no running usbmuxd unless a test adds one
        os.makedirs(self.proc)
        # the only usbmuxd iphone.sh runs (--version) is a system one, owned by root: here the stub on PATH, ours
        self.patch("iphone.sh", 'MUX_DIRS="/usr/sbin /usr/bin /sbin /bin /usr/local/sbin /usr/local/bin"',
                   f'MUX_DIRS="{self.fakebin}"')
        self.patch("iphone.sh", "SYS_OWNER=root ", f"SYS_OWNER={pwd.getpwuid(os.getuid()).pw_name} ")
        self.python = os.path.join(self.root, "python3")
        write_exec(self.python, PY_WRAPPER.format(**fmt))
        write_exec(os.path.join(self.fakebin, "notify-send"),
                   f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.fake}/notify.log"\n')
        self.ca_bundle = os.path.join(self.root, "system-ca.pem")
        with open(self.ca_bundle, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nZmFrZSBzeXN0ZW0gQ0E=\n-----END CERTIFICATE-----\n")
        self.now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
        make_signed_ipa(os.path.join(self.fake, "signed.ipa"), created=self.now,
                        expires=self.now + datetime.timedelta(days=7))
        self.env = {
            "HOME": self.home, "PATH": self.fakebin + ":" + SYSTEM_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "XDG_STATE_HOME": self.xdg_state, "XDG_RUNTIME_DIR": self.xdg_run,
            "XDG_CONFIG_HOME": self.config_home, "XDG_CACHE_HOME": self.cache_home,
            "XDG_DATA_HOME": self.data_home, "IPHONE_PYTHON": self.python,
            "IPHONE_WDA_PORT": str(self.wda_port), "IPHONE_MJPEG_PORT": str(self.mjpeg_port),
            "GO_IOS_AGENT_PORT": str(self.agent_port), "USBMUXD_SOCKET_ADDRESS": self.mux.address,
            "WAYLAND_DISPLAY": "wayland-test", "CA_BUNDLE": self.ca_bundle, "IPHONE_SYSFS": self.sysfs, "IPHONE_PROC": self.proc,
        }
        self.procs = []

    # ---- scenario -----------------------------------------------------
    def put(self, name, text=""):
        with open(os.path.join(self.fake, name), "w") as f:
            f.write(text)

    def installed(self, expires_in_days=6, ipa=True, **extra):
        """WebDriverAgent signed and recorded, as after ./resign.sh."""
        cfg = {"wda_bundle": bundle_id(), "signed_at": epoch(self.now) - 86400}
        if expires_in_days is not None:
            cfg["expires_at"] = epoch(self.now) + int(expires_in_days * 86400)
        cfg.update(extra)
        self.write_config(cfg)
        if ipa:
            with open(os.path.join(self.repo, "build", "WebDriverAgent.ipa"), "wb") as f:
                f.write(b"PK fake unsigned ipa")
        return cfg

    def write_config(self, cfg):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "config.json"), "w") as f:
            json.dump(cfg, f)

    def config(self):
        with open(os.path.join(self.state, "config.json")) as f:
            return json.load(f)

    def usb_device(self, udid=UDID_A, configs=5, current=5, name="3-4"):
        """The phone in the fake sysfs, as /sys/bus/usb/devices/<name> shows it (its USB serial has no dash)."""
        d = os.path.join(self.sysfs, name)
        os.makedirs(d, exist_ok=True)
        for attr, value in (("idVendor", "05ac"), ("idProduct", "12a8"), ("serial", udid.replace("-", "")),
                            ("bNumConfigurations", configs), ("bConfigurationValue", current)):
            with open(os.path.join(d, attr), "w") as f:
                f.write(f"{value}\n")
        return d

    def process(self, argv, comm=None, pid=4242):
        """A process in the fake /proc (IPHONE_PROC): its comm (the kernel cuts it to 15 characters) and cmdline.
        It belongs to whoever runs the tests, as another user's would to iphone.sh's checks."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write((comm or os.path.basename(argv[0]))[:15] + "\n")
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"".join(a.encode() + b"\0" for a in argv))
        return d

    def tool(self, name, text):
        """A stub program on PATH, ahead of the real one."""
        write_exec(os.path.join(self.fakebin, name), text)

    def hide(self, *names):
        """PATH without these programs (and those hidden before): fakebin, then links to the rest of the system PATH."""
        self.hidden = getattr(self, "hidden", set()) | set(names)
        farm = tempfile.mkdtemp(prefix="farm-", dir=self.root)
        for d in SYSTEM_PATH.split(":"):
            for name in os.listdir(d) if os.path.isdir(d) else ():
                src, dst = os.path.join(d, name), os.path.join(farm, name)
                if name not in self.hidden and not os.path.lexists(dst) and os.access(src, os.X_OK):
                    os.symlink(src, dst)
        for name in names:
            if os.path.exists(os.path.join(self.fakebin, name)):
                os.unlink(os.path.join(self.fakebin, name))
        self.env["PATH"] = self.fakebin + ":" + farm

    def patch(self, script, old, new):
        """Edit the sandbox's copy of a script (e.g. to point a fixed system path into the sandbox)."""
        path = self.script(script)
        with open(path) as f:
            src = f.read()
        if old not in src:
            raise AssertionError(f"{script} no longer contains {old!r} - update the test")
        with open(path, "w") as f:
            f.write(src.replace(old, new))

    # ---- running ------------------------------------------------------
    def script(self, name):
        return os.path.join(self.repo, name)

    def run(self, script, *args, env=None, timeout=90, stdin=subprocess.DEVNULL, umask=None):
        """Run a script to the end; stdout and stderr go to files (background children may keep them)."""
        e = dict(self.env, **(env or {}))
        for k, v in list(e.items()):
            if v is None:
                del e[k]

        def child():
            _default_signals()
            if umask is not None:
                os.umask(umask)
        with tempfile.TemporaryFile(dir=self.root) as out:
            p = subprocess.run([BASH, self.script(script)] + list(args), env=e, cwd=self.root, stdin=stdin,
                               stdout=out, stderr=subprocess.STDOUT, timeout=timeout, preexec_fn=child)
            out.seek(0)
            p.output = out.read().decode("utf-8", "replace")
        return p

    def start(self, *args, env=None):
        """Start iphone.sh in the background (its own session); returns the Popen."""
        e = {k: v for k, v in dict(self.env, **(env or {})).items() if v is not None}
        log = open(os.path.join(self.root, "start.out"), "wb")
        p = subprocess.Popen([BASH, self.script("iphone.sh")] + list(args), env=e, cwd=self.root,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, preexec_fn=_default_signals)
        log.close()
        self.procs.append(p)
        return p

    def output(self):
        with open(os.path.join(self.root, "start.out"), errors="replace") as f:
            return f.read()

    # ---- what happened ------------------------------------------------
    def read(self, name, default=""):
        try:
            with open(os.path.join(self.fake, name), errors="replace") as f:
                return f.read()
        except OSError:
            return default

    def notes(self):
        return self.read("notify.log")

    def calls(self, prefix=""):
        return [c for c in self.read("calls.log").splitlines() if c.startswith(prefix)]

    def window(self):
        try:
            with open(os.path.join(self.fake, "window.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def runlog(self, name):
        try:
            with open(os.path.join(self.rundir, name), errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def wait_for(self, cond, timeout=60, step=0.1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = cond()
            if v:
                return v
            time.sleep(step)
        return cond()

    def leftovers(self):
        """Processes (other than this one) whose command line mentions this sandbox."""
        found = []
        me = os.getpid()
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                with open(f"/proc/{pid}/stat") as f:
                    zombie = f.read().rsplit(")", 1)[1].split()[0] == "Z"
            except OSError:
                continue
            if self.root in cmd and not zombie:
                found.append((int(pid), cmd.strip()))
        return found

    def no_leftovers(self, timeout=5):
        return self.wait_for(lambda: not self.leftovers(), timeout) or not self.leftovers()

    def port_open(self, port):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            return False

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait(5)
        for pid, _cmd in self.leftovers():             # only our own fakes: their command line names our temp dir
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.mux.close()
        shutil.rmtree(self.root, ignore_errors=True)


def _default_signals():
    # a test runner started in the background may ignore SIGINT; bash cannot trap an ignored signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def dedent(s):
    return textwrap.dedent(s).lstrip("\n")


def setup_pins(repo):
    """The literal pins at column 0 of setup.sh, e.g. {"GO_IOS_VERSION": "v1.3.2", ...}."""
    import re
    with open(os.path.join(repo, "setup.sh")) as f:
        return dict(re.findall(r"^([A-Z0-9_]+)=([^\s#]+)", f.read(), re.M))


def stamp_installed(sb, goarch="amd64", plumesign=True, go_ios=True, ipa=True):
    """Write the stamps setup.sh compares, so it treats the fake ios/plumesign and a fake IPA as current."""
    import glob
    import hashlib
    pins = setup_pins(sb.repo)
    os.makedirs(sb.state, exist_ok=True)
    if plumesign:
        data = b""
        for p in sorted(glob.glob(os.path.join(sb.repo, "patches", "*.patch"))):
            with open(p, "rb") as f:
                data += f.read()
        with open(os.path.join(sb.state, "plumesign.stamp"), "w") as f:
            f.write(f"{pins['IMPACTOR_COMMIT']} {hashlib.sha256(data).hexdigest()[:16]}\n")
    if go_ios:
        with open(os.path.join(sb.bin, "ios"), "rb") as f:
            have = hashlib.sha256(f.read()).hexdigest()
        with open(os.path.join(sb.state, "go-ios.stamp"), "w") as f:
            f.write(f"{pins['GO_IOS_VERSION']} {pins['GO_IOS_SHA256']} {goarch} {have}\n")
    if ipa:
        ipa_path = os.path.join(sb.repo, "build", "WebDriverAgent.ipa")
        with open(ipa_path, "wb") as f:
            f.write(b"PK fake unsigned ipa")
        with open(ipa_path + ".stamp", "w") as f:
            f.write(f"{pins['WDA_VERSION']} {pins['WDA_SHA256']}\n")
    return pins
