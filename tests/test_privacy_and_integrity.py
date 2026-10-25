"""Repo-wide guards: no personal data committed, pinned and checked downloads, loopback-only defaults,
private file modes, stdlib-only Python that still parses as 3.9."""
import ast
import hashlib
import os
import re
import socket
import ssl
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Names that must never be committed. Nothing real is written here: the machine running the tests adds
# its own hostname, and IPHONE_PRIVACY_WORDS (comma or space separated) adds more, e.g. in CI.
GENERIC_HOSTS = {"localhost", "localdomain", "ubuntu", "debian", "fedora", "archlinux", "arch", "manjaro",
                 "opensuse", "tumbleweed", "linux", "raspberrypi", "runner", "docker", "container", "toolbox"}


def forbidden_words():
    words = set()
    try:
        host = socket.gethostname().strip().lower()
    except OSError:
        host = ""
    for name in (host, host.split(".")[0]):
        if len(name) >= 4 and name not in GENERIC_HOSTS:
            words.add(name)
    words.update(w.lower() for w in re.split(r"[,\s]+", os.environ.get("IPHONE_PRIVACY_WORDS", "")) if len(w) >= 3)
    return sorted(words)


HOSTNAMES = forbidden_words()

# Known leaks that another file owner still has to fix. Each entry is (path, text on the line); anything
# else that matches fails. Delete an entry once the file is cleaned up.
PENDING = set()   # (path, text on the line) for a known leak that another change still has to remove

PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "UDID": re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b"),
    "old UDID": re.compile(r"\b[0-9a-f]{40}\b"),
    "team id": re.compile(r"xctrunner\.[A-Z0-9]{10}\b"),
    "home path": re.compile(r"/home/[a-z_][a-z0-9_-]*"),
}
if HOSTNAMES:                   # empty on a generic CI host unless IPHONE_PRIVACY_WORDS is set
    PATTERNS["hostname"] = re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, HOSTNAMES)), re.I)
HOME_OK = re.compile(r"^/home/(user|username|you|me|example)$")      # placeholders in docs and examples
EMAIL_OK = re.compile(r"@(example\.(com|org|net)|users\.noreply\.github\.com)$|^noreply@", re.I)


def repo_files():
    """Tracked and new (not ignored) files; None outside a git checkout."""
    try:
        out = subprocess.run(["git", "-C", REPO, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return sorted({p for p in out.decode().split("\0") if p and os.path.isfile(os.path.join(REPO, p))})


def text(path):
    with open(os.path.join(REPO, path), "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def read(path):
    with open(os.path.join(REPO, path)) as f:
        return f.read()


class Privacy(unittest.TestCase):
    def setUp(self):
        self.files = repo_files()
        if self.files is None:
            self.skipTest("not a git checkout")
        # pinned upstream commits (IMPACTOR_COMMIT, USBMUXD_COMMIT, ...) are public, not old UDIDs
        self.public_hashes = set(re.findall(r"^[A-Z0-9_]*COMMIT[A-Z0-9_]*=([0-9a-f]{40})\b", read("setup.sh"), re.M))

    def scan(self):
        """(hits not covered by PENDING, PENDING entries that still covered a hit)."""
        found, used = [], set()
        for path in self.files:
            body = text(path)
            if body is None:
                continue
            for n, line in enumerate(body.splitlines(), 1):
                for kind, rx in PATTERNS.items():
                    for m in rx.finditer(line):
                        hit = m.group(0)
                        if kind == "email" and EMAIL_OK.search(hit):
                            continue
                        if kind == "old UDID" and hit in self.public_hashes:
                            continue                        # a pinned upstream commit (public)
                        if kind == "home path" and HOME_OK.match(hit):
                            continue
                        waived = [(p, t) for p, t in PENDING if path == p and t in line]
                        if waived:
                            used.update(waived)
                            continue
                        found.append(f"{path}:{n}: {kind}: {hit}")
        return found, used

    def test_no_personal_data_in_any_file(self):
        found, _ = self.scan()
        self.assertEqual(found, [], "personal data in the repo:\n" + "\n".join(found))

    def test_pending_exemptions_are_still_needed(self):
        _, used = self.scan()
        stale = sorted(set(PENDING) - used)
        if stale:
            self.skipTest(f"remove these from PENDING, they are fixed (or this machine's names are not set): {stale}")

    def test_no_secrets_or_machine_state_committed(self):
        bad = [p for p in self.files if re.search(
            r"(^|/)(selfIdentity\.plist|config\.json|accounts\.json|key\.pem|.*\.p12|.*\.mobileprovision|.*\.ipa|"
            r".*\.log)$", p)]
        self.assertEqual(bad, [])
        self.assertFalse([p for p in self.files if p.startswith("build/")])

    def test_gitignore_keeps_state_out(self):
        ignore = read(".gitignore")
        for pattern in ("build/", "selfIdentity.plist", "config.json", "__pycache__/"):
            self.assertIn(pattern, ignore)


class Integrity(unittest.TestCase):
    def test_downloads_are_pinned_and_checked(self):
        setup = read("setup.sh")
        pins = dict(re.findall(r"^([A-Z0-9_]+)=(\S+)", setup, re.M))
        for k in ("GO_IOS_SHA256", "IOS_AMD64_SHA256", "IOS_ARM64_SHA256", "WDA_SHA256"):
            self.assertRegex(pins.get(k, ""), r"^[0-9a-f]{64}$", k)
        self.assertRegex(pins["IMPACTOR_COMMIT"], r"^[0-9a-f]{40}$")
        self.assertIn("checksum mismatch", setup)
        self.assertIn("--proto '=https'", setup)
        piped = [line for line in setup.splitlines() if re.search(r"curl[^#]*\|\s*(ba)?sh\b", line)
                 and not re.match(r"\s*echo ", line)]          # a printed hint is fine, running one is not
        self.assertEqual(piped, [], "never pipe a download into a shell")

    def test_apple_root_ca_is_the_real_one(self):
        pem = read("certs/apple-root.pem")
        der = ssl.PEM_cert_to_DER_cert(pem)
        fp = ":".join("%02X" % b for b in hashlib.sha256(der).digest())
        # Apple Inc. Root Certificate, https://www.apple.com/certificateauthority/
        self.assertEqual(fp, "B0:B1:73:0E:CB:C7:FF:45:05:14:2C:49:F1:29:5E:6E:DA:6B:CA:ED:7E:2C:68:C5:BE:91:B5:A1:10:01:F0:24")
        self.assertIn(fp, read("setup.sh"))
        with open(os.path.join(REPO, "certs", "apple-root.pem"), "rb") as f:
            file_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertIn(f"APPLE_PEM_SHA256={file_sha}", read("certs/ca-bundle.sh"))

    def test_loopback_only_defaults(self):
        sh = read("iphone.sh")
        self.assertIn('WDA_BIND="${IPHONE_WDA_BIND-127.0.0.1}"', sh)
        self.assertIsNone(re.search(r'"\$IOS"\s+forward\b', sh), "go-ios forward listens on 0.0.0.0")
        self.assertNotIn("0.0.0.0", read("iphone_control/forward.py").replace("listens on 0.0.0.0", ""))
        self.assertIn('socket.create_server(("127.0.0.1", local))', read("iphone_control/forward.py"))
        for path in ("iphone_control/wda.py", "iphone_control/window.py"):
            self.assertIn("127.0.0.1", read(path))

    def test_private_umask_in_scripts_that_write_secrets(self):
        for path in ("iphone.sh", "resign.sh"):
            self.assertIn("\numask 077\n", read(path), path)

    def test_scripts_are_executable_bash_with_set_u(self):
        for path in ("iphone.sh", "resign.sh", "setup.sh", "uninstall.sh", "certs/ca-bundle.sh"):
            full = os.path.join(REPO, path)
            if not os.path.exists(full):
                continue
            self.assertTrue(os.access(full, os.X_OK), f"{path} is not executable")
            body = read(path)
            self.assertTrue(body.startswith("#!/usr/bin/env bash\n"), path)
            self.assertTrue(re.search(r"\nset -[A-Za-z]*u", body), f"{path} has no set -u")

    def test_no_world_writable_files(self):
        files = repo_files() or []
        bad = [p for p in files if os.stat(os.path.join(REPO, p)).st_mode & 0o002]
        self.assertEqual(bad, [])


class Python(unittest.TestCase):
    """Stdlib only (plus PyGObject for the window, its picture and Bluetooth), and nothing newer than Python 3.9
    can parse."""
    ALLOWED = {"gi"}                 # distro package python3-gi / python3-gobject / python-gobject
    GI_FILES = {"iphone_control/window.py", "iphone_control/video.py", "iphone_control/blehid.py", "wda_mirror.py"}

    def sources(self):
        out = []
        for base in ("iphone_control", "tests", "."):
            d = os.path.join(REPO, base)
            for name in sorted(os.listdir(d)):
                if name.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(d, name), REPO))
        return out

    def test_parses_as_python_3_9(self):
        for path in self.sources():
            src = read(path)
            try:
                ast.parse(src, path, feature_version=(3, 9))
            except SyntaxError as e:
                self.fail(f"{path}: not valid Python 3.9 syntax: {e}")
            self.assertIsNone(re.search(r"^\s*match\s+\S.*:\s*$", src, re.M), path)

    def test_no_third_party_imports(self):
        stdlib = getattr(sys, "stdlib_module_names", None)
        if stdlib is None:
            self.skipTest("needs Python 3.10+ for sys.stdlib_module_names")
        local = {"iphone_control", "support", "scriptenv"} | {os.path.splitext(n)[0] for n in os.listdir(HERE)}
        bad = []
        for path in self.sources():
            for node in ast.walk(ast.parse(read(path))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module]
                for n in names:
                    top = n.split(".")[0]
                    if top in stdlib or top in local:
                        continue
                    if top in self.ALLOWED and (path in self.GI_FILES or path.startswith("tests/")):
                        continue
                    bad.append(f"{path}: import {n}")
        self.assertEqual(bad, [])

    def test_gtk_free_modules_import_without_pygobject(self):
        # everything but the window, its picture and Bluetooth must work where python3-gi is missing (CI, the
        # scripts, the capture process): geometry, wda, usbmux, forward, usbfs, valeria, screencap, hid, ...
        named = {"geometry", "wda", "usbmux", "forward", "usbfs", "valeria", "screencap", "hid"}
        here = {n[:-3] for n in os.listdir(os.path.join(REPO, "iphone_control")) if n.endswith(".py")}
        gi = {os.path.basename(p)[:-3] for p in self.GI_FILES if p.startswith("iphone_control/")}
        mods = sorted(named | (here - gi - {"__init__"}))
        code = ("import sys; sys.modules['gi'] = None; sys.path.insert(0, sys.argv[1]);"
                + "import " + ", ".join(f"iphone_control.{m}" for m in mods))
        r = subprocess.run([sys.executable, "-I", "-c", code, REPO], stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
