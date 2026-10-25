"""setup.sh with stub tools in a temp HOME: --check, --basic, --usbmuxd, distro and CPU detection, pins, the
launcher entry, the Hyprland rule and the download checksum. Nothing is downloaded, built or installed for real:
sudo, systemctl, git, make and pkg-config are stubs, and system paths are pointed into the sandbox."""
import hashlib
import os
import re
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from scriptenv import SYSTEM_PATH, Sandbox, setup_pins, stamp_installed, write_exec  # noqa: E402

REAL_UNAME = shutil.which("uname", path=SYSTEM_PATH)

STUBS = {
    # uname -m answers $FAKE_UNAME_M; everything else is the real uname
    "uname": f'#!/bin/sh\nif [ "$1" = -m ] && [ -n "${{FAKE_UNAME_M:-}}" ]; then echo "$FAKE_UNAME_M"; '
             f'else exec {REAL_UNAME} "$@"; fi\n',
    "usbmuxd": "#!/bin/sh\nexit 0\n",
    "cc": "#!/bin/sh\nexit 0\n",
    "cargo": '#!/bin/sh\necho "cargo ${FAKE_RUST:-1.90.0} (stub)"\n',
    "rustc": '#!/bin/sh\necho "rustc ${FAKE_RUST:-1.90.0} (stub)"\n',
    # downloads get junk, so a checksum must catch them; the arguments are logged
    "curl": '#!/bin/sh\necho "curl $*" >> "$FAKE_DIR/calls.log"\nout=\nwhile [ $# -gt 0 ]; do '
            '[ "$1" = -o ] && out=$2; shift; done\n[ -n "$out" ] && echo "not the real file" > "$out"\nexit 0\n',
}
for tool in ("git", "zip", "unzip"):
    STUBS[tool] = f'#!/bin/sh\necho "{tool} $*" >> "$FAKE_DIR/calls.log"\nexit 0\n'

OS_RELEASE = {   # fixture -> (family, package manager command)
    'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"': ("deb", "apt install"),
    'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="22.04"': ("deb", "apt install"),
    "ID=debian\nVERSION_ID=12": ("deb", "apt install"),
    'ID=linuxmint\nID_LIKE="ubuntu debian"': ("deb", "apt install"),
    'ID=pop\nID_LIKE="ubuntu debian"': ("deb", "apt install"),
    "ID=fedora\nVERSION_ID=43": ("rpm", "dnf install"),
    'ID="almalinux"\nID_LIKE="rhel centos fedora"': ("rpm", "dnf install"),
    'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"': ("suse", "zypper install"),
    'ID="opensuse-leap"\nID_LIKE="suse opensuse"': ("suse", "zypper install"),
    "ID=arch": ("arch", "pacman -S --needed"),
    "ID=manjaro\nID_LIKE=arch": ("arch", "pacman -S --needed"),
    "ID=endeavouros\nID_LIKE=arch": ("arch", "pacman -S --needed"),
    "ID=void": ("other", None),
}
WINDOW_PACKAGES = {   # what the window needs when PyGObject, GTK, GStreamer and its plugins are all missing
    "deb": "python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
           "gstreamer1.0-gtk3",
    "rpm": "python3-gobject gtk3 gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good libsoup3 "
           "gstreamer1-plugins-good-gtk",
    "suse": "python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-Gst-1_0 gstreamer-plugins-base "
            "gstreamer-plugins-good libsoup-3_0-0 gstreamer-plugins-good-gtk",
    "arch": "python-gobject gtk3 gstreamer gst-plugins-base gst-plugins-good gst-plugin-gtk",
    "other": "PyGObject GTK3-GObject-typelib GStreamer-GObject-typelib gst-plugins-base(videoconvert) "
             "gst-plugins-good(souphttpsrc,multipartdemux,jpegdec) gst-plugin-gtk(gtksink)",
}
DEB = 'ID=debian\nVERSION_ID=12'

# A Python for setup.sh's real window probe (its stdin script), with a fake `gi` first on sys.path.
# $FAKE_GI picks what is missing: nogi | nogst | nogtk_nogst | plugins (souphttpsrc and jpegdec only).
PROBE_PYTHON = r'''#!/bin/bash
if [ "$*" = "-I -" ]; then
  exec {py} -I -c 'import sys; sys.path.insert(0, sys.argv[1]); exec(compile(sys.stdin.read(), "<probe>", "exec"))' "{gi}"
fi
exec {py} "$@"
'''
FAKE_GI = r'''import os
mode = os.environ.get("FAKE_GI", "")
if mode == "nogi":
    raise ImportError("No module named 'gi'")
gone = {"nogst": {"Gst"}, "nogtk_nogst": {"Gtk", "Gdk", "Gst"}}.get(mode, set())
def require_version(ns, v):
    if ns in gone:
        raise ValueError("Namespace %s not available" % ns)
'''
FAKE_GI_REPOSITORY = r'''import os
class Gtk: pass
class Gdk: pass
LACKS = ("souphttpsrc", "jpegdec") if os.environ.get("FAKE_GI") == "plugins" else ()
class Gst:
    @staticmethod
    def init(argv): pass
    class ElementFactory:
        @staticmethod
        def find(name): return None if name in LACKS else object()
'''
# pkg-config that knows the modules in $FAKE_PC ("name=version ..."); git whose checkout is $FAKE_GIT_HEAD; a make
# that "builds" src/usbmuxd; sudo that runs the command as this user (the paths it gets point into the sandbox)
PKG_CONFIG = r'''#!/bin/sh
mode=$1; shift
for m in "$@"; do
  v=$(printf '%s\n' $FAKE_PC | sed -n "s/^$m=//p")
  [ -n "$v" ] || exit 1
  [ "$mode" = --modversion ] && echo "$v"
done
exit 0
'''
GIT = r'''#!/bin/sh
echo "git $*" >> "$FAKE_DIR/calls.log"
case " $* " in *" rev-parse HEAD "*) echo "$FAKE_GIT_HEAD" ;; *" cat-file "*) exit 1 ;; esac
exit 0
'''
MAKE = r'''#!/bin/sh
echo "make $*" >> "$FAKE_DIR/calls.log"
mkdir -p src && printf '#!/bin/sh\necho "usbmuxd 1.1.1-git-test"\n' > src/usbmuxd && chmod +x src/usbmuxd
'''
SUDO = '#!/bin/sh\necho "sudo $*" >> "$FAKE_DIR/calls.log"\nexec "$@"\n'
SYSTEMCTL = '#!/bin/sh\necho "systemctl $*" >> "$FAKE_DIR/calls.log"\n'
ALL_PC = "libplist-2.0=2.7.0 libusb-1.0=1.0.27 libimobiledevice-glue-1.0=1.3.1 libimobiledevice-1.0=1.4.0"
BROKEN_PLUMESIGN = ('#!/bin/sh\necho "plumesign $*" >> "$FAKE_DIR/calls.log"\n'
                    'echo "plumesign: error while loading shared libraries: libssl.so.3: cannot open shared object file" >&2\n'
                    'exit 127\n')


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash", path=SYSTEM_PATH), "needs Linux bash")
class Setup(unittest.TestCase):
    def setUp(self):
        self.sb = Setup.sandbox()
        self.addCleanup(self.sb.close)
        self.pins = setup_pins(self.sb.repo)

    @staticmethod
    def sandbox():
        sb = Sandbox(repo_name="my repo")           # a path with a space, as people have
        for name, text in STUBS.items():
            write_exec(os.path.join(sb.fakebin, name), text)
        sb.env.update(FAKE_DIR=sb.fake, FAKE_UNAME_M="x86_64")
        for k in ("WAYLAND_DISPLAY",):
            sb.env.pop(k, None)
        sb.put("probe", "")                         # PyGObject, GTK and GStreamer all present
        return sb

    def setup(self, *args, env=None):
        return self.sb.run("setup.sh", *args, env=env, timeout=120)

    def patched_os_release(self, text):
        """setup.sh reads /etc/os-release; point the copy at a fixture instead (patched once)."""
        fixture = os.path.join(self.sb.root, "os-release")
        with open(fixture, "w") as f:
            f.write(text + "\n")
        path = self.sb.script("setup.sh")
        with open(path) as f:
            src = f.read()
        if fixture in src:
            return
        needle = "for f in /etc/os-release /usr/lib/os-release; do"
        self.assertIn(needle, src, "setup.sh no longer reads os-release this way - update this test")
        with open(path, "w") as f:
            f.write(src.replace(needle, f'for f in "{fixture}"; do'))

    def downloads(self):
        return [c for c in self.sb.calls() if c.split(" ")[0] in ("curl", "git", "zip", "unzip")]

    # ---- options ------------------------------------------------------
    def test_usage(self):
        p = self.setup("--help")
        self.assertEqual(p.returncode, 0)
        for opt in ("--check", "--rebuild", "--hyprland", "--basic", "--usbmuxd", "--yes", "USBMUXD_COMMIT"):
            self.assertIn(opt, p.output)
        self.assertTrue(p.output.startswith("One-time setup"), p.output)
        self.assertNotIn("set -", p.output)
        p = self.setup("--bogus")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown option: --bogus", p.output)
        p = self.setup("--usbmuxd", "--basic")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--usbmuxd runs on its own", p.output)

    # ---- --basic and the optional groups -----------------------------------
    def no_bluez(self):
        self.sb.hide("bluetoothd")
        self.sb.patch("setup.sh", 'BLUETOOTHD_DIRS="/usr/libexec/bluetooth /usr/lib/bluetooth /usr/sbin"',
                      f'BLUETOOTHD_DIRS="{self.sb.root}/nowhere"')

    def test_basic_names_only_what_basic_mode_needs(self):
        self.patched_os_release(DEB)
        self.no_bluez()
        self.sb.put("probe", "gi gtk gst base good gtksink bad h264dec")
        shutil.rmtree(os.path.join(self.sb.home, ".local"))
        p = self.setup("--basic", "--check", env={"FAKE_RUST": "1.75.0"})
        self.assertEqual(p.returncode, 1, p.output)
        self.assertEqual(self.install_line(p.output), "python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 "
                         "gstreamer1.0-plugins-base gstreamer1.0-gtk3 gstreamer1.0-plugins-bad gstreamer1.0-libav "
                         "bluez".split())
        for full_only in ("== go-ios", "== Apple Root CA", "== plumesign", "== WebDriverAgent", "== Apple ID",
                          "Rust >="):
            self.assertNotIn(full_only, p.output)
        self.assertIn("2 thing(s) to fix - run ./setup.sh --basic", p.output)    # the packages, the launcher entry
        self.assertEqual(self.downloads(), [])
        self.assertFalse(os.path.exists(os.path.join(self.sb.home, ".local")))

    def test_basic_run_downloads_nothing(self):
        sb = self.sb
        write_exec(os.path.join(sb.fakebin, "bluetoothd"), "#!/bin/sh\nexit 0\n")
        os.unlink(os.path.join(sb.bin, "ios"))
        p = self.setup("--basic", env={"FAKE_UNAME_M": "riscv64"})     # no go-ios, so any CPU
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("Next: plug in + unlock the iPhone and run   " + os.path.join(sb.repo, "iphone.sh") +
                      "   (basic mode", p.output)
        self.assertTrue(os.path.exists(os.path.join(sb.data_home, "applications", "iphone-control.desktop")))
        self.assertEqual(self.downloads(), [])
        self.assertEqual(sb.calls("plumesign"), [])                       # no Apple ID needed
        self.assertFalse(os.path.exists(os.path.join(sb.bin, "ios")))
        check = self.setup("--basic", "--check")
        self.assertEqual(check.returncode, 0, check.output)
        self.assertIn("everything basic mode needs is in place", check.output)

    def test_check_reports_the_optional_groups_separately(self):
        self.patched_os_release(DEB)
        self.no_bluez()
        write_exec(os.path.join(self.sb.fakebin, "usbmuxd"), '#!/bin/sh\necho "usbmuxd 1.1.1"\n')
        self.sb.put("probe", "bad h264dec")
        p = self.setup("--check")
        self.assertRegex(p.output, r"== system packages \(deb, x86_64\)\nok \(python: ")     # the required set is there
        self.assertRegex(p.output, r"\noptional, for USB video and basic mode \(Bluetooth input\): (sudo )?apt install "
                                   r"gstreamer1.0-plugins-bad gstreamer1.0-libav bluez\n")
        self.assertIn("\noptional: USB video needs a usbmuxd newer than 1.1.1 (the last release; it keeps the "
                      "iPhone out of its capture configuration): ./setup.sh --usbmuxd\n", p.output)
        self.assertNotRegex(p.output, r"(?m)^(install with:|missing)")      # what CI installs stays the required set
        write_exec(os.path.join(self.sb.fakebin, "usbmuxd"), '#!/bin/sh\necho "usbmuxd 1.1.1-72-g3ded00c"\n')
        self.assertNotIn("USB video needs a usbmuxd", self.setup("--check").output)

    # ---- --usbmuxd --------------------------------------------------------
    def usbmuxd_sandbox(self, systemd=True):
        sb = self.sb
        for name, text in (("pkg-config", PKG_CONFIG), ("git", GIT), ("make", MAKE), ("sudo", SUDO),
                           ("systemctl", SYSTEMCTL), ("autoconf", STUBS["cc"]), ("automake", STUBS["cc"]),
                           ("libtoolize", STUBS["cc"])):
            write_exec(os.path.join(sb.fakebin, name), text)
        system = os.path.join(sb.root, "system")             # stands in for /usr/local/sbin, /etc and /run
        self.mux_bin = os.path.join(system, "sbin", "usbmuxd-iphone-control")
        self.dropin = os.path.join(system, "usbmuxd.service.d", "iphone-control.conf")
        run = os.path.join(system, "run-systemd")
        if systemd:
            os.makedirs(run)
        sb.patch("setup.sh", "MUX_BIN=/usr/local/sbin/usbmuxd-iphone-control", "MUX_BIN=" + self.mux_bin)
        sb.patch("setup.sh", "MUX_DROPIN=/etc/systemd/system/usbmuxd.service.d/iphone-control.conf",
                 "MUX_DROPIN=" + self.dropin)
        sb.patch("setup.sh", "SYSTEMD_RUN=/run/systemd/system", "SYSTEMD_RUN=" + run)
        src = os.path.join(sb.cache_home, "iphone-control", "usbmuxd")   # the clone, as a fetch leaves it
        os.makedirs(os.path.join(src, ".git"))
        for name in ("autogen.sh", "configure"):
            write_exec(os.path.join(src, name), "#!/bin/sh\nexit 0\n")
        sb.env.update(FAKE_PC=ALL_PC)
        return src

    def test_usbmuxd_needs_libplist_2_3(self):
        self.usbmuxd_sandbox()
        p = self.setup("--usbmuxd", "--yes", env={"FAKE_PC": "libplist-2.0=2.2.0"})
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("this system has\nlibplist 2.2.0. Nothing was changed.", p.output)
        self.patched_os_release(DEB)                       # Debian 12, before its libplist headers are installed
        p = self.setup("--usbmuxd", "--yes", env={"FAKE_PC": ""})
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("Nothing was changed.", p.output)
        self.assertEqual([c for c in self.sb.calls() if c.split(" ")[0] in ("git", "make", "sudo", "systemctl")], [])

    def test_usbmuxd_names_its_build_dependencies(self):
        self.usbmuxd_sandbox()
        for release, want in (
                ('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"', "apt install libplist-dev libusb-1.0-0-dev "
                 "libimobiledevice-glue-dev libimobiledevice-dev"),
                ("ID=fedora\nVERSION_ID=43", "dnf install libplist-devel libusb1-devel libimobiledevice-glue-devel "
                 "libimobiledevice-devel"),
                ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"', "zypper install 'pkgconfig(libplist-2.0)' "
                 "'pkgconfig(libusb-1.0)' 'pkgconfig(libimobiledevice-glue-1.0)' 'pkgconfig(libimobiledevice-1.0)'"),
                ("ID=arch", "pacman -S --needed libplist libusb libimobiledevice-glue libimobiledevice")):
            with self.subTest(release=release.splitlines()[0]):
                self.patched_os_release(release)
                p = self.setup("--usbmuxd", env={"FAKE_PC": ""})
                self.assertEqual(p.returncode, 1, p.output)
                self.assertRegex(p.output, r"(?m)^install with:  (sudo )?" + re.escape(want) + "$")
        self.sb.hide("autoconf")
        self.patched_os_release('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"')
        p = self.setup("--usbmuxd", env={"FAKE_PC": ALL_PC})
        self.assertRegex(p.output, r"(?m)^install with:  (sudo )?apt install build-essential autoconf automake libtool "
                                   r"pkg-config$")
        self.assertEqual(self.sb.calls("git"), [])

    def test_usbmuxd_builds_the_pinned_commit_and_installs_it(self):
        sb = self.sb
        src = self.usbmuxd_sandbox()
        pin = self.pins["USBMUXD_COMMIT"]
        p = self.setup("--usbmuxd")                      # no terminal and no --yes: shows the plan, changes nothing
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn(f"This builds libimobiledevice/usbmuxd {pin[:12]} (libplist 2.7.0) in {src}", p.output)
        self.assertIn("Environment=USBMUXD_DEFAULT_DEVICE_MODE=2", p.output)
        self.assertIn("Build and install it? [y/N] n (no terminal", p.output)
        self.assertEqual(sb.calls("git"), [])
        self.assertEqual(self.setup("--usbmuxd", "--check").returncode, 1)
        p = self.setup("--usbmuxd", "--yes", env={"FAKE_GIT_HEAD": pin})
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn(f"git -C {src} fetch -q --depth 1 origin {pin}", sb.calls("git"))
        self.assertEqual(len(sb.calls("make")), 1)
        self.assertRegex(sb.calls("make")[0], r"^make -j\d+$")
        self.assertTrue(os.access(self.mux_bin, os.X_OK))
        with open(self.dropin) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[2:5], ["[Service]", "Environment=USBMUXD_DEFAULT_DEVICE_MODE=2", "ExecStart="])
        self.assertRegex(lines[5], "^ExecStart=" + re.escape(self.mux_bin) + r" --user \w+ --systemd$")
        self.assertEqual(sb.mode(self.dropin), 0o644)
        if os.geteuid() != 0:                             # root (CI containers) needs no sudo
            sudo = sb.calls("sudo")
            self.assertIn(f"sudo install -m755 {src}/src/usbmuxd {self.mux_bin}", sudo)
            d = os.path.dirname(self.dropin)              # made and opened first: uutils' install -D uses the umask
            self.assertLess(sudo.index(f"sudo chmod 755 {d}"), sudo.index(f"sudo install -m644 /dev/stdin {self.dropin}"))
        self.assertEqual(sb.calls("systemctl"), ["systemctl daemon-reload", "systemctl restart usbmuxd"])
        with open(os.path.join(sb.state, "usbmuxd.stamp")) as f:
            self.assertEqual(f.read().strip(), f"{pin} {self.sha256(self.mux_bin)}")
        check = self.setup("--usbmuxd", "--check")
        self.assertEqual(check.returncode, 0, check.output)
        self.assertIn(f"in place: usbmuxd 1.1.1-git-test ({self.mux_bin}), started by systemd", check.output)
        again = self.setup("--usbmuxd", "--yes")              # safe to re-run: nothing rebuilt
        self.assertIn("already in place (usbmuxd 1.1.1-git-test)", again.output)
        self.assertEqual(len(sb.calls("make")), 1)
        # libplist 2.4: the 2024 commit, so the installed one is out of date
        p = self.setup("--usbmuxd", "--check", env={"FAKE_PC": ALL_PC.replace("2.7.0", "2.4.0")})
        self.assertEqual(p.returncode, 1, p.output)
        old = self.pins["USBMUXD_COMMIT_PLIST23"][:7]
        self.assertIn(f"is not usbmuxd {old}: ./setup.sh --usbmuxd rebuilds it", p.output)
        # with it in place, the full check no longer suggests it
        write_exec(os.path.join(sb.fakebin, "usbmuxd"), '#!/bin/sh\necho "usbmuxd 1.1.1"\n')
        self.assertNotIn("USB video needs a usbmuxd", self.setup("--check").output)

    def test_usbmuxd_without_systemd_prints_the_steps(self):
        src = self.usbmuxd_sandbox(systemd=False)
        p = self.setup("--usbmuxd", env={"FAKE_GIT_HEAD": self.pins["USBMUXD_COMMIT"]})
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("Without systemd nothing is installed", p.output)
        self.assertIn(f"  install -m755 '{src}/src/usbmuxd' {self.mux_bin}\n", p.output)
        self.assertRegex(p.output, r"  USBMUXD_DEFAULT_DEVICE_MODE=2 " + re.escape(self.mux_bin) + r" --user \w+\n")
        self.assertEqual(self.sb.calls("sudo"), [])
        self.assertFalse(os.path.exists(self.mux_bin))

    @staticmethod
    def sha256(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_usbmuxd_override_stays_readable_under_a_strict_umask(self):
        sb = self.sb
        self.usbmuxd_sandbox()
        os.makedirs(os.path.dirname(self.dropin), mode=0o700)    # as an earlier run under umask 077 left it
        os.chmod(os.path.dirname(self.dropin), 0o700)
        p = sb.run("setup.sh", "--usbmuxd", "--yes", env={"FAKE_GIT_HEAD": self.pins["USBMUXD_COMMIT"]}, timeout=120,
                   umask=0o077)
        self.assertEqual(p.returncode, 0, p.output)
        self.assertEqual(sb.mode(os.path.dirname(self.dropin)), 0o755)
        self.assertEqual(sb.mode(self.dropin), 0o644)
        self.assertEqual(sb.mode(self.mux_bin), 0o755)
        self.assertEqual(sb.mode(os.path.dirname(self.mux_bin)), 0o755)
        with open(self.dropin) as f:
            self.assertIn("Environment=USBMUXD_DEFAULT_DEVICE_MODE=2\n", f.read())
        self.assertEqual(self.setup("--usbmuxd", "--check").returncode, 0)

    def test_usbmuxd_without_systemd_is_known_once_installed_by_hand(self):
        sb = self.sb
        src = self.usbmuxd_sandbox(systemd=False)
        env = {"FAKE_GIT_HEAD": self.pins["USBMUXD_COMMIT"]}
        self.assertEqual(self.setup("--usbmuxd", env=env).returncode, 0)
        p = self.setup("--usbmuxd", "--check")
        self.assertEqual(p.returncode, 1, p.output)
        self.assertIn("not installed: ./setup.sh --usbmuxd builds usbmuxd", p.output)
        os.makedirs(os.path.dirname(self.mux_bin))
        shutil.copy2(os.path.join(src, "src", "usbmuxd"), self.mux_bin)     # the install step it printed
        p = self.setup("--usbmuxd", "--check")
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn(f"{self.mux_bin} is installed; without systemd it runs only when started by hand", p.output)
        p = self.setup("--usbmuxd", env=env)
        self.assertIn("already in place (usbmuxd 1.1.1-git-test; without systemd it runs only when started by hand)",
                      p.output)
        self.assertEqual(len(sb.calls("make")), 1, "nothing rebuilt")
        write_exec(self.mux_bin, '#!/bin/sh\necho "usbmuxd 1.1.1-60-gabcdef0"\n')   # another build
        p = self.setup("--usbmuxd", "--check")
        self.assertEqual(p.returncode, 1, p.output)
        self.assertIn(f"{self.mux_bin} is not usbmuxd {self.pins['USBMUXD_COMMIT'][:7]}: ./setup.sh --usbmuxd "
                      "rebuilds it", p.output)

    def test_usbmuxd_pins_are_full_commits(self):
        for k in ("USBMUXD_COMMIT", "USBMUXD_COMMIT_PLIST23"):
            self.assertRegex(self.pins[k], r"^[0-9a-f]{40}$", k)

    # ---- --check --------------------------------------------------------
    def test_check_on_a_new_machine_changes_nothing(self):
        shutil.rmtree(os.path.join(self.sb.home, ".local"))
        p = self.setup("--check")
        self.assertEqual(p.returncode, 1, p.output)
        out = p.output
        self.assertRegex(out, r"== system packages \(\w+, x86_64\)\nok \(python: ")
        self.assertIn(f"== go-ios {self.pins['GO_IOS_VERSION']} (amd64)", out)
        self.assertIn("not installed", out)
        self.assertIn("not built", out)
        self.assertIn(f"ok (system CAs: {self.sb.ca_bundle})", out)
        self.assertIn("not logged in", out)
        self.assertRegex(out, r"\d+ thing\(s\) to fix - run ./setup.sh")
        for path in (os.path.join(self.sb.home, ".local"), os.path.join(self.sb.cache_home, "iphone-control"),
                     os.path.join(self.sb.repo, "build", "WebDriverAgent.ipa")):
            self.assertFalse(os.path.exists(path), path)
        self.assertEqual(self.downloads(), [])               # nothing downloaded or cloned

    def test_check_names_the_right_packages_for_each_distro(self):
        self.sb.put("probe", "gi gtk gst base good gtksink")
        for text, (fam, cmd) in OS_RELEASE.items():
            with self.subTest(os_release=text.splitlines()[0]):
                self.patched_os_release(text)
                p = self.setup("--check")
                self.assertEqual(p.returncode, 1)
                self.assertIn(f"== system packages ({fam}, x86_64)", p.output)
                want = WINDOW_PACKAGES[fam].split()
                if cmd:
                    m = re.search(r"^install with:  (?:sudo )?" + re.escape(cmd) + r" (.*)$", p.output, re.M)
                    self.assertTrue(m, p.output)
                else:
                    m = re.search(r"^missing \(package names vary by distro\): (.*)$", p.output, re.M)
                    self.assertTrue(m, p.output)
                self.assertEqual(m.group(1).split(), want)
                self.assertTrue(os.path.exists(self.sb.script("setup.sh")))

    def probe_python(self):
        gi = os.path.join(self.sb.root, "fake-gi", "gi")
        os.makedirs(os.path.join(gi, "repository"))
        with open(os.path.join(gi, "__init__.py"), "w") as f:
            f.write(FAKE_GI)
        with open(os.path.join(gi, "repository", "__init__.py"), "w") as f:
            f.write(FAKE_GI_REPOSITORY)
        py = os.path.join(self.sb.root, "probe-python3")
        write_exec(py, PROBE_PYTHON.format(py=sys.executable, gi=os.path.dirname(gi)))
        return py

    def install_line(self, out):
        m = re.search(r"^install with:  (?:sudo )?apt install (.*)$", out, re.M)
        self.assertTrue(m, out)
        return m.group(1).split()

    def test_one_install_line_names_everything_the_probe_cannot_check(self):
        # the real probe text: without gi or Gst the layers behind it are named too, so one run is enough
        self.patched_os_release(DEB)
        py = self.probe_python()
        base_good_gtksink = "gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-gtk3".split()
        for mode, want in (("nogi", WINDOW_PACKAGES["deb"].split()),
                           ("nogtk_nogst", WINDOW_PACKAGES["deb"].split()[1:]),
                           ("nogst", ["gir1.2-gstreamer-1.0"] + base_good_gtksink),
                           ("plugins", ["gstreamer1.0-plugins-good"])):
            with self.subTest(mode=mode):
                p = self.setup("--check", env={"IPHONE_PYTHON": py, "FAKE_GI": mode})
                self.assertEqual(p.returncode, 1, p.output)
                self.assertEqual(self.install_line(p.output), want)
        p = self.setup("--check", env={"IPHONE_PYTHON": py, "FAKE_GI": ""})   # all present
        self.assertRegex(p.output, r"== system packages \(deb, x86_64\)\nok \(python: ")

    def test_no_python_names_python_and_every_window_package(self):
        self.patched_os_release(DEB)
        path = self.sb.script("setup.sh")
        with open(path) as f:
            src = f.read()
        needle = " /usr/bin/python3 python3 python3.13 python3.12 python3.11 python3.10 python3.9; do"
        self.assertIn(needle, src, "setup.sh no longer looks for Python this way - update this test")
        with open(path, "w") as f:
            f.write(src.replace(needle, "; do"))
        p = self.setup("--check", env={"IPHONE_PYTHON": os.path.join(self.sb.root, "no-such-python")})
        self.assertIn("no Python 3.9 or newer found", p.output)
        self.assertEqual(self.install_line(p.output), ["python3"] + WINDOW_PACKAGES["deb"].split())

    def test_a_plumesign_that_does_not_run_is_not_logged_in(self):
        stamp_installed(self.sb)
        write_exec(os.path.join(self.sb.bin, "plumesign"), BROKEN_PLUMESIGN)
        p = self.setup("--check")
        self.assertEqual(p.returncode, 1, p.output)
        self.assertIn("== Apple ID\nplumesign does not run (exit 127: plumesign: error while loading shared "
                      "libraries: libssl.so.3: cannot open shared object file) - ./setup.sh --rebuild", p.output)
        self.assertNotIn("logged in", p.output)
        self.assertNotIn("everything is in place", p.output)
        p = self.setup()
        self.assertEqual(p.returncode, 1, p.output)
        self.assertIn("plumesign does not run (exit 127", p.output)
        self.assertNotIn("Next:", p.output)
        self.assertNotIn("setup.sh failed at line", p.output)
        self.assertEqual(len(self.sb.calls("plumesign account list")), 2)   # once per run

    def test_login_state_comes_from_one_account_list(self):
        stamp_installed(self.sb)
        self.sb.put("nologin")
        p = self.setup("--check")
        self.assertIn("== Apple ID\nnot logged in: ", p.output)
        self.assertIn("Next: in a normal terminal run", self.setup().output)
        # "No accounts found" means logged out even when plumesign exits non-zero with it
        write_exec(os.path.join(self.sb.bin, "plumesign"),
                   "#!/bin/sh\necho '[INFO] No accounts found. Use account login.' >&2\nexit 1\n")
        p = self.setup("--check")
        self.assertIn("== Apple ID\nnot logged in: ", p.output)
        self.assertNotIn("does not run", p.output)
        os.unlink(os.path.join(self.sb.fake, "nologin"))
        stamp_installed(self.sb)
        write_exec(os.path.join(self.sb.bin, "plumesign"), "#!/bin/sh\necho '[INFO] Saved accounts: x' >&2\n")
        p = self.setup("--check")
        self.assertIn("== Apple ID\nlogged in\n", p.output)

    def test_cpu_detection(self):
        stamp_installed(self.sb, goarch="arm64")
        p = self.setup("--check", env={"FAKE_UNAME_M": "aarch64"})
        self.assertIn("aarch64)", p.output)
        self.assertIn(f"== go-ios {self.pins['GO_IOS_VERSION']} (arm64)", p.output)
        self.assertRegex(p.output, r"\(arm64\) -> .*\nalready installed")
        p = self.setup("--check", env={"FAKE_UNAME_M": "riscv64"})
        self.assertEqual(p.returncode, 1)
        self.assertIn("unsupported CPU riscv64", p.output)

    def test_rust_version_is_checked_only_when_plumesign_needs_building(self):
        p = self.setup("--check", env={"FAKE_RUST": "1.75.0"})
        self.assertIn("Rust >= 1.88.0 is needed to build plumesign (found: 1.75.0)", p.output)
        self.assertIn("rustup:", p.output)
        p = self.setup("--check", env={"FAKE_RUST": "1.91.1"})
        self.assertNotIn("Rust >=", p.output)
        self.assertIn("rust 1.91.1", p.output)
        stamp_installed(self.sb)
        p = self.setup("--check", env={"FAKE_RUST": "1.75.0"})
        self.assertNotIn("Rust >=", p.output)

    def test_cargo_outside_path_is_found(self):
        # rustup's ~/.cargo/bin before a new terminal, or Ubuntu's /usr/lib/rust-1.NN: not on PATH. The system
        # PATH is replaced by links to its programs minus any Rust this machine has.
        nobin = os.path.join(self.sb.root, "no-rust-bin")
        os.makedirs(nobin)
        for d in reversed(SYSTEM_PATH.split(":")):
            for name in os.listdir(d) if os.path.isdir(d) else ():
                if name not in ("cargo", "rustc", "rustup"):
                    link = os.path.join(nobin, name)
                    if os.path.lexists(link):
                        os.remove(link)
                    os.symlink(os.path.join(d, name), link)
        self.sb.env["PATH"] = self.sb.fakebin + ":" + nobin
        home_bin = os.path.join(self.sb.home, ".cargo", "bin")
        os.makedirs(home_bin)
        for tool in ("cargo", "rustc"):
            os.remove(os.path.join(self.sb.fakebin, tool))
            write_exec(os.path.join(home_bin, tool), STUBS[tool])
        p = self.setup("--check", env={"FAKE_RUST": "1.91.1"})
        self.assertNotIn("failed at line", p.output)
        self.assertNotIn("Rust >=", p.output)
        self.assertIn("rust 1.91.1", p.output)

    def test_pin_overrides_need_their_own_checksum(self):
        for env, msg in (({"GO_IOS_VERSION": "v9.9.9"}, "needs its own GO_IOS_SHA256"),
                         ({"WDA_VERSION": "v1.0.0"}, "needs its own WDA_SHA256")):
            p = self.setup("--check", env=env)
            self.assertEqual(p.returncode, 1)
            self.assertIn(msg, p.output)
        p = self.setup("--check", env={"GO_IOS_VERSION": "v9.9.9", "GO_IOS_SHA256": "0" * 64})
        self.assertIn("== go-ios v9.9.9 (amd64)", p.output)

    def test_pins_are_literal_lines(self):
        # iphone.sh doctor and uninstall.sh read these lines from setup.sh's text
        self.assertRegex(self.pins["GO_IOS_VERSION"], r"^v\d+\.\d+\.\d+$")
        self.assertRegex(self.pins["IMPACTOR_COMMIT"], r"^[0-9a-f]{40}$")
        for k in ("GO_IOS_SHA256", "IOS_AMD64_SHA256", "IOS_ARM64_SHA256", "WDA_SHA256"):
            self.assertRegex(self.pins[k], r"^[0-9a-f]{64}$", k)

    # ---- a full run, offline ----------------------------------------------
    def test_full_run_with_everything_current(self):
        sb = self.sb
        stamp_installed(sb)
        plume = os.path.join(sb.config_home, "PlumeImpactor")
        os.makedirs(os.path.join(plume, "keys"))
        with open(os.path.join(plume, "accounts.json"), "w") as f:
            f.write("{}")
        os.chmod(plume, 0o755)
        os.chmod(os.path.join(plume, "accounts.json"), 0o644)
        p = self.setup()
        self.assertEqual(p.returncode, 0, p.output)
        self.assertIn("already installed", p.output)
        self.assertEqual(p.output.count("already built"), 2)
        self.assertIn("Next: plug in + unlock the iPhone and run   " + os.path.join(sb.repo, "resign.sh"), p.output)
        self.assertEqual(self.downloads(), [])               # nothing downloaded or built
        # CA bundle
        ca = os.path.join(sb.state, "ca-bundle-with-apple.pem")
        with open(ca) as f:
            self.assertTrue(f.readline().startswith("# system CAs from " + sb.ca_bundle))
        # the Apple ID session is made private
        self.assertEqual(sb.mode(plume) & 0o077, 0)
        self.assertEqual(sb.mode(os.path.join(plume, "accounts.json")) & 0o077, 0)
        # the launcher entry, with a quoted path that has a space
        entry = os.path.join(sb.data_home, "applications", "iphone-control.desktop")
        with open(entry) as f:
            desktop = f.read()
        exe = os.path.join(sb.repo, "iphone.sh")
        self.assertIn(f'Exec="{exe}"\n', desktop)
        self.assertIn(f"TryExec={exe}\n", desktop)
        for line in ("[Desktop Entry]", "Type=Application", "Name=iPhone Control", "Icon=phone", "Terminal=false",
                     "StartupWMClass=iphone-control", "Categories=Utility;"):
            self.assertIn(line + "\n", desktop)
        self.assertEqual(sb.mode(entry), 0o644)
        if shutil.which("desktop-file-validate"):
            import subprocess
            self.assertEqual(subprocess.call(["desktop-file-validate", entry]), 0)
        # a second run and --check find everything in place
        again = self.setup()
        self.assertIn("already up to date", again.output)
        check = self.setup("--check")
        self.assertEqual(check.returncode, 0, check.output)
        self.assertIn("everything is in place", check.output)
        # already signed (resign.sh wrote expires_at): straight to iphone.sh
        with open(os.path.join(sb.state, "config.json"), "w") as f:
            f.write('{"wda_bundle": "x", "expires_at": 4102444800}')
        signed = self.setup()
        self.assertIn("Next: plug in + unlock the iPhone and run   " + os.path.join(sb.repo, "iphone.sh"), signed.output)
        self.assertIn("signed until 01 Jan 2100", signed.output)
        self.assertNotIn("resign.sh", signed.output.split("== done")[1])

    def test_doctor_reads_a_launcher_path_with_spaces(self):
        # Exec="..." is unquoted before the path is checked, so a repo in "my repo" is not cut at the space
        stamp_installed(self.sb)
        self.assertEqual(self.setup().returncode, 0)
        exe = os.path.join(self.sb.repo, "iphone.sh")
        self.assertIn(f"ok    launcher entry ({exe})", self.sb.run("iphone.sh", "doctor").output)

    def test_hyprland_rule(self):
        sb = self.sb
        stamp_installed(sb)
        hypr = os.path.join(sb.config_home, "hypr")
        os.makedirs(hypr)
        main = os.path.join(hypr, "hyprland.lua")
        with open(main, "w") as f:
            f.write("-- my config\n")
        p = self.setup("--check", "--hyprland")
        self.assertIn("float rule not installed yet", p.output)
        self.assertEqual(os.listdir(hypr), ["hyprland.lua"])
        p = self.setup("--hyprland")
        self.assertEqual(p.returncode, 0, p.output)
        rule = os.path.join(hypr, "iphone-control.lua")
        with open(rule) as f:
            text = f.read()
        self.assertIn('hl.window_rule({ match = { class = "^iphone-control$" }, float = true, center = true,', text)
        self.assertIn('size = { "(monitor_h*83/200)", "(monitor_h*9/10)" }', text)
        with open(main) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "-- my config")
        self.assertEqual(lines[-2], "-- iphone-control window rule (added by setup.sh --hyprland)")
        self.assertTrue(lines[-1].startswith('do local f = "' + rule + '";'), lines[-1])
        self.assertTrue(lines[-1].endswith("dofile(f) end end"))
        backups = [n for n in os.listdir(hypr) if n.startswith("hyprland.lua.bak.")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(hypr, backups[0])) as f:
            self.assertEqual(f.read(), "-- my config\n")
        if shutil.which("luac"):
            import subprocess
            self.assertEqual(subprocess.call(["luac", "-p", main, rule]), 0)
        p = self.setup("--hyprland")                         # idempotent
        self.assertIn("a rule for class iphone-control is already in", p.output)
        with open(main) as f:
            self.assertEqual(f.read().count("iphone-control window rule"), 1)

    def test_window_manager_hints_change_no_files(self):
        stamp_installed(self.sb)
        p = self.setup(env={"HYPRLAND_INSTANCE_SIGNATURE": "x"})
        self.assertIn("windowrule {", p.output)
        self.assertIn("match:class = ^(iphone-control)$", p.output)
        self.assertFalse(os.path.exists(os.path.join(self.sb.config_home, "hypr")))
        p = self.setup("--hyprland")
        self.assertIn("no Lua config", p.output)
        self.assertIn('for_window [app_id="^iphone-control$"]', self.setup(env={"SWAYSOCK": "/x"}).output)
        self.assertIn('for_window [class="(?i)^iphone-control$"]', self.setup(env={"I3SOCK": "/x"}).output)

    # ---- downloads ------------------------------------------------------
    def test_a_bad_download_is_refused(self):
        sb = self.sb
        stamp_installed(sb, go_ios=False)
        os.unlink(os.path.join(sb.bin, "ios"))
        p = self.setup()
        self.assertEqual(p.returncode, 1, p.output)
        self.assertIn("checksum mismatch (corrupt or changed download): https://github.com/danielpaulus/go-ios/"
                      f"releases/download/{self.pins['GO_IOS_VERSION']}/go-ios-linux.zip", p.output)
        self.assertFalse(os.path.exists(os.path.join(sb.bin, "ios")))
        cache = os.path.join(sb.cache_home, "iphone-control")
        self.assertEqual([n for n in os.listdir(cache) if n.endswith(".part") or n.endswith(".zip")], [])
        curl = sb.calls("curl ")
        self.assertEqual(len(curl), 1)
        self.assertIn("--proto =https", curl[0])
        self.assertIn("--tlsv1.2", curl[0])
        self.assertEqual(sb.calls("unzip"), [])

    def test_a_different_ios_binary_is_noted_before_replacing(self):
        stamp_installed(self.sb, go_ios=False)
        p = self.setup("--check")
        self.assertIn("a different ios binary is installed", p.output)


if __name__ == "__main__":
    unittest.main()
