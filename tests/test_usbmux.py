"""usbmuxd client and the loopback port forward, against a fake usbmuxd on a UNIX socket."""
import contextlib
import http.client
import io
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from support import UDID_A, UDID_B, FakeMux, FakeWDA, free_port  # noqa: E402
from iphone_control import forward, usbmux  # noqa: E402
from iphone_control.wda import WDA  # noqa: E402


class EnvMux(unittest.TestCase):
    """Each test gets a fake usbmuxd (phone 8100 -> a fake WDA) via USBMUXD_SOCKET_ADDRESS."""

    @classmethod
    def setUpClass(cls):
        cls.wda = FakeWDA()

    @classmethod
    def tearDownClass(cls):
        cls.wda.stop()

    def setUp(self):
        self.wda.reset()
        self.mux = FakeMux([(7, UDID_A)], ports={8100: self.wda.server_port})
        self._old = os.environ.get("USBMUXD_SOCKET_ADDRESS")
        os.environ["USBMUXD_SOCKET_ADDRESS"] = self.mux.address
        self.fwds = []
        self.fwd_log = []

    def tearDown(self):
        for f in self.fwds:
            f.close()
        self.mux.close()
        if self._old is None:
            os.environ.pop("USBMUXD_SOCKET_ADDRESS", None)
        else:
            os.environ["USBMUXD_SOCKET_ADDRESS"] = self._old

    def forwarder(self, remote=8100, guard=True):
        fw = forward.Forwarder(7, 0, remote, guard, log=self.fwd_log.append)
        fw.local = fw.server.getsockname()[1]       # port 0 = any free port; the guard checks Host against it
        threading.Thread(target=fw.serve, daemon=True).start()
        self.fwds.append(fw)
        return fw


class Usbmux(EnvMux):
    def test_address_parsing(self):
        cases = {"UNIX:/x/y": ("unix", "/x/y"), "127.0.0.1:27015": ("inet", ("127.0.0.1", 27015)),
                 "[::1]:27015": ("inet", ("::1", 27015)), ":27015": ("inet", ("127.0.0.1", 27015))}
        for env, (fam, addr) in cases.items():
            os.environ["USBMUXD_SOCKET_ADDRESS"] = env
            f, a = usbmux.address()
            self.assertEqual((f == socket.AF_UNIX and "unix" or "inet", a), (fam, addr), env)
        os.environ["USBMUXD_SOCKET_ADDRESS"] = "nonsense"
        with self.assertRaises(usbmux.UsbmuxError):
            usbmux.address()
        os.environ.pop("USBMUXD_SOCKET_ADDRESS")
        self.assertEqual(usbmux.address(), (socket.AF_UNIX, "/var/run/usbmuxd"))

    def test_lists_usb_devices_only(self):
        self.mux.devices = [(7, UDID_A), (9, UDID_B)]
        self.assertEqual(usbmux.usb_devices(), [(7, UDID_A), (9, UDID_B)])     # not the Wi-Fi one

    def test_device_id_matches_udid_ignoring_case_and_dashes(self):
        self.mux.devices = [(7, UDID_A), (9, UDID_B)]
        self.assertEqual(usbmux.device_id(), 7)
        self.assertEqual(usbmux.device_id(UDID_B.lower()), 9)
        self.assertEqual(usbmux.device_id(UDID_B.replace("-", "")), 9)
        self.assertIsNone(usbmux.device_id("nope"))
        self.mux.devices = []
        self.assertIsNone(usbmux.device_id())

    def test_request_pair_record(self):
        self.assertTrue(usbmux.request({"MessageType": "ReadPairRecord", "PairRecordID": UDID_A}).get("PairRecordData"))
        self.assertFalse(usbmux.request({"MessageType": "ReadPairRecord", "PairRecordID": UDID_B}).get("PairRecordData"))

    def test_connect_refused_and_no_device(self):
        with self.assertRaises(usbmux.UsbmuxError) as cm:
            usbmux.connect(7, 1234)
        self.assertIn("connection refused", str(cm.exception))
        with self.assertRaises(usbmux.UsbmuxError) as cm:
            usbmux.connect(42, 8100)
        self.assertIn("no such device", str(cm.exception))
        self.assertIn((7, 1234), self.mux.connects)        # the port went over in network byte order

    def test_connect_is_a_raw_pipe(self):
        s = usbmux.connect(7, 8100)
        with s:
            s.sendall(b"GET /status HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:40])
        self.assertIn(b'"ready": true', data)

    def test_tcp_usbmuxd_connections_skip_nagle(self):
        tcp = FakeMux([(7, UDID_A)], ports={8100: self.wda.server_port}, tcp=True)
        try:
            os.environ["USBMUXD_SOCKET_ADDRESS"] = tcp.address
            with usbmux.connect(7, 8100) as s:
                self.assertEqual(s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
            self.assertEqual(tcp.connects, [(7, 8100)])
        finally:
            tcp.close()
            os.environ["USBMUXD_SOCKET_ADDRESS"] = self.mux.address
        with usbmux.connect(7, 8100) as s:                  # a UNIX socket: left alone, no error
            self.assertEqual(s.family, socket.AF_UNIX)
            self.assertIs(usbmux.nodelay(s), s)

    def test_usbmuxd_missing(self):
        os.environ["USBMUXD_SOCKET_ADDRESS"] = "UNIX:/nonexistent/usbmuxd"
        with self.assertRaises(usbmux.UsbmuxError) as cm:
            usbmux.usb_devices()
        self.assertIn("usbmuxd not reachable", str(cm.exception))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(usbmux.main(["list"]), 1)
        self.assertIn("usbmux:", err.getvalue())

    def test_cli(self):
        self.mux.devices = [(7, UDID_A), (9, UDID_B)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(usbmux.main(["list"]), 0)
            self.assertEqual(usbmux.main(["device-id", "--udid", UDID_B.lower()]), 0)
            self.assertEqual(usbmux.main(["device-id", "--udid", "nope"]), 1)
            self.assertEqual(forward.main(["--device-id"]), 0)
        self.assertEqual(out.getvalue(), f"7\t{UDID_A}\n9\t{UDID_B}\n9\n7\n")
        self.assertIn("No iPhone on USB", err.getvalue())


class Guard(unittest.TestCase):
    def head(self, *lines):
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def test_parse_ports(self):
        self.assertEqual(forward.parse_ports(["8100", "9101:9100"]), [(8100, 8100), (9101, 9100)])
        for bad in ("0", "70000", "x", "8100:y", "1:0"):
            with self.assertRaises(ValueError, msg=bad):
                forward.parse_ports([bad])

    def test_local_programs_pass(self):
        for host in ("127.0.0.1:8100", "localhost:8100", "[::1]:8100", "127.0.0.1", "LOCALHOST"):
            self.assertIsNone(forward.check(self.head("GET /status HTTP/1.1", f"Host: {host}"), 8100), host)
        self.assertIsNone(forward.check(self.head("GET / HTTP/1.0"), 8100))          # no Host at all
        self.assertIsNone(forward.check(self.head("GET / HTTP/1.1", "Host: 127.0.0.1", "Sec-Fetch-Site: none"), 8100))

    def test_browsers_and_rebinding_are_refused(self):
        refused = [
            ("Host: 127.0.0.1:8100", "Origin: https://evil.example"),
            ("Host: 127.0.0.1:8100", "Origin: null"),
            ("Host: 127.0.0.1:8100", "Sec-Fetch-Site: cross-site"),
            ("Host: 127.0.0.1:8100", "Sec-Fetch-Site: same-site"),
            ("Host: evil.example:8100",),
            ("Host: 127.0.0.1:9999",),                      # another port: a page on another local server
            ("Host: 127.0.0.1:8100", "Host: evil.example"),
            ("Host: 127.0.0.1.evil.example",),
        ]
        for hdrs in refused:
            self.assertEqual(forward.check(self.head("GET /status HTTP/1.1", *hdrs), 8100), (403, "Forbidden"), hdrs)

    def test_malformed_and_unsupported(self):
        self.assertEqual(forward.check(self.head("GET /status"), 8100)[0], 400)
        self.assertEqual(forward.check(self.head("GET / HTTP/1.1", "no colon here"), 8100)[0], 400)
        self.assertEqual(forward.check(self.head("POST / HTTP/1.1", "Transfer-Encoding: chunked"), 8100)[0], 501)
        self.assertEqual(forward.body_length(self.head("POST / HTTP/1.1", "Content-Length: x")), -1)
        self.assertEqual(forward.body_length(self.head("POST / HTTP/1.1", "Content-Length: 12")), 12)


class Forwarding(EnvMux):
    def raw(self, port, data):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(data)
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                out += chunk
        return out

    def test_listens_on_loopback_only(self):
        fw = self.forwarder()
        self.assertEqual(fw.server.getsockname()[0], "127.0.0.1")
        self.assertEqual(fw.server.family, socket.AF_INET)

    def test_client_and_curl_style_requests_pass(self):
        fw = self.forwarder()
        self.assertTrue(WDA(f"http://127.0.0.1:{fw.local}").status()["ready"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://localhost:{fw.local}/status", timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_keep_alive_requests_with_bodies(self):
        fw = self.forwarder()
        c = http.client.HTTPConnection("127.0.0.1", fw.local, timeout=5)
        try:
            for i in range(3):
                c.request("POST", "/wda/homescreen", body=b'{"n": %d}' % i, headers={"Content-Type": "application/json"})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 200)
            # a refused request on a kept-alive connection ends it
            c.request("POST", "/wda/homescreen", body=b"{}", headers={"Origin": "http://evil.example"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 403)
        finally:
            c.close()
        self.assertEqual(self.wda.bodies("/wda/homescreen"), [{"n": 0}, {"n": 1}, {"n": 2}])

    def open_tcp(self, fw):
        """The forwarder's end of each loopback client connection that is open right now."""
        with fw.lock:
            return [x for x in fw.open if x.family == socket.AF_INET]

    def test_client_sockets_skip_nagle(self):
        # Nagle + the reader's delayed ACK held back the tail of most MJPEG frames by ~30 ms
        for guard in (True, False):
            fw = self.forwarder(guard=guard)
            c = http.client.HTTPConnection("127.0.0.1", fw.local, timeout=5)
            try:
                c.request("GET", "/status")
                c.getresponse().read()
                socks = self.open_tcp(fw)
                self.assertEqual(len(socks), 1, guard)
                self.assertEqual(socks[0].getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1, guard)
            finally:
                c.close()

    def test_refused_requests_never_reach_the_phone(self):
        fw = self.forwarder()
        for hdrs in ({"Host": f"evil.example:{fw.local}"}, {"Sec-Fetch-Site": "cross-site"},
                     {"Origin": "https://evil.example"}):
            c = http.client.HTTPConnection("127.0.0.1", fw.local, timeout=5)
            c.putrequest("POST", "/wda/homescreen", skip_host="Host" in hdrs)
            for k, v in hdrs.items():
                c.putheader(k, v)
            c.putheader("Content-Length", "0")
            c.endheaders()
            r = c.getresponse()
            self.assertEqual(r.status, 403, hdrs)
            self.assertIn(b"only forwards requests from local programs", r.read())
            c.close()
        self.assertEqual(self.wda.calls, [])
        self.assertEqual(self.mux.connects, [])            # refused before a phone connection is even opened

    def test_oversized_and_chunked_requests(self):
        fw = self.forwarder()
        big = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX: " + b"a" * (17 * 1024) + b"\r\n\r\n"
        self.assertTrue(self.raw(fw.local, big).startswith(b"HTTP/1.1 431"))
        chunked = b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        self.assertTrue(self.raw(fw.local, chunked).startswith(b"HTTP/1.1 501"))
        self.assertTrue(self.raw(fw.local, b"garbage\r\n\r\n").startswith(b"HTTP/1.1 400"))
        self.assertEqual(self.wda.calls, [])

    def test_raw_mode_passes_anything(self):
        fw = self.forwarder(guard=False)
        out = self.raw(fw.local, b"GET /status HTTP/1.1\r\nHost: evil.example\r\nOrigin: x\r\nConnection: close\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 200"), out[:40])

    def test_refused_phone_port_closes_the_client(self):
        fw = self.forwarder(remote=1)                       # nothing listens on phone port 1
        c = http.client.HTTPConnection("127.0.0.1", fw.local, timeout=5)
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
            c.request("GET", "/status")
            c.getresponse()
        c.close()
        self.assertTrue(any("phone port 1: connection refused" in m for m in self.fwd_log), self.fwd_log)

    def test_close_ends_open_connections(self):
        fw = self.forwarder()
        c = http.client.HTTPConnection("127.0.0.1", fw.local, timeout=5)
        c.request("GET", "/status")
        c.getresponse().read()
        fw.close()
        with self.assertRaises((http.client.HTTPException, ConnectionError, OSError)):
            c.request("GET", "/status")
            c.getresponse()
        c.close()


class Run(EnvMux):
    def run_bg(self, *args, **kw):
        rc, err = [], io.StringIO()

        def go():
            with contextlib.redirect_stderr(err):
                rc.append(forward.run(*args, **kw))
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t, rc, err

    def test_returns_2_when_the_phone_is_unplugged(self):
        port = free_port()
        t, rc, err = self.run_bg([str(port)], poll=0.1)
        deadline = time.monotonic() + 5
        while "forwarding" not in err.getvalue() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(f"forwarding 127.0.0.1:{port} -> phone:{port}", err.getvalue())
        self.mux.devices = []
        t.join(5)
        self.assertEqual(rc, [2])
        self.assertIn("the iPhone was unplugged", err.getvalue())

    def test_returns_2_when_usbmuxd_goes_away(self):
        t, rc, _err = self.run_bg([str(free_port())], poll=0.05)
        time.sleep(0.3)
        self.mux.close()
        t.join(5)
        self.assertEqual(rc, [2])

    def test_busy_port(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(forward.run([str(port)]), 1)
        self.assertIn(f"forward: cannot listen on 127.0.0.1:{port}", err.getvalue())

    def test_no_phone(self):
        self.mux.devices = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(forward.run(["8100"]), 1)
            self.assertEqual(forward.main(["--udid", "nope", str(free_port())]), 1)
        self.assertIn("No iPhone on USB", err.getvalue())

    def test_main_needs_ports(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                forward.main([])
            self.assertEqual(forward.main(["notaport"]), 1)

    def test_maps_local_to_remote_ports(self):
        port = free_port()
        t, rc, err = self.run_bg([f"{port}:8100"], udid=UDID_A.lower(), poll=0.1)
        deadline = time.monotonic() + 5
        while "forwarding" not in err.getvalue() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(WDA(f"http://127.0.0.1:{port}").status()["ready"])
        self.mux.devices = []
        t.join(5)
        self.assertEqual(rc, [2])


if __name__ == "__main__":
    unittest.main()
