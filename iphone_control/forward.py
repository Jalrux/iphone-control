"""Loopback-only port forward to the phone over usbmuxd, with a guard against web pages.

WebDriverAgent answers CORS preflights and sends "Access-Control-Allow-Origin: *", so without a guard
any page open in the laptop's browser could tap, type and read the screen through 127.0.0.1:8100.
Each HTTP request is checked first: requests with an Origin header, a cross-site Sec-Fetch-Site or a
Host that is not loopback (DNS rebinding) get 403. The app (urllib/http.client, GStreamer souphttpsrc)
and curl send none of these, so they are unaffected.

  python3 -m iphone_control.forward [--udid U] [--raw] 8100 9100     (LOCAL[:REMOTE] each)
  python3 -m iphone_control.forward --device-id [--udid U]          print usbmuxd's DeviceID
Exits 2 when the phone is unplugged, so the script that started it notices.
"""
import argparse
import socket
import sys
import threading
import time

from . import usbmux

MAX_HEAD = 16 * 1024
POLL_S = 2


def parse_ports(specs):
    """["8100", "9101:9100"] -> [(8100, 8100), (9101, 9100)]"""
    out = []
    for spec in specs:
        local, _, remote = str(spec).partition(":")
        try:
            pair = (int(local), int(remote or local))
        except ValueError:
            raise ValueError(f"bad port: {spec}") from None
        if not all(0 < p < 65536 for p in pair):
            raise ValueError(f"bad port: {spec}")
        out.append(pair)
    return out


def parse_head(head):
    """Request head bytes (up to the blank line) -> (request line, {lower-case name: [values]})."""
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise ValueError("bad header line")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return lines[0], headers


def check(head, port):
    """None if the request may reach the phone, else (status, reason) to refuse it with."""
    try:
        line, h = parse_head(head)
    except ValueError:
        return 400, "Bad Request"
    if len(line.split(" ")) != 3:
        return 400, "Bad Request"
    if "origin" in h:
        return 403, "Forbidden"                              # a web page (CORS)
    if any(v.lower() != "none" for v in h.get("sec-fetch-site", [])):
        return 403, "Forbidden"                              # cross-site browser request
    hosts = h.get("host", [])
    allowed = {f"{n}:{port}" for n in ("127.0.0.1", "localhost", "[::1]")} | {"127.0.0.1", "localhost", "[::1]"}
    if len(hosts) > 1 or (hosts and hosts[0].lower() not in allowed):
        return 403, "Forbidden"                              # DNS rebinding
    if "chunked" in ",".join(h.get("transfer-encoding", [])).lower():
        return 501, "Not Implemented"
    return None


def body_length(head):
    try:
        return int(parse_head(head)[1].get("content-length", ["0"])[0])
    except ValueError:
        return -1


def _refuse(client, status, reason):
    text = f"{status} {reason} - iphone-control only forwards requests from local programs\n".encode()
    try:
        client.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: "
                       f"{len(text)}\r\nConnection: close\r\n\r\n".encode() + text)
    except OSError:
        pass


class _Requests:
    """Reads one HTTP request at a time from the client (head checked, body by Content-Length)."""

    def __init__(self, sock, port):
        self.sock, self.port, self.buf = sock, port, b""

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError
        self.buf += chunk

    def next(self):
        """The next request's bytes; EOFError when the client is done; (status, reason) to refuse."""
        while b"\r\n\r\n" not in self.buf:
            if len(self.buf) > MAX_HEAD:
                return 431, "Request Header Fields Too Large"
            if not self.buf.strip(b"\r\n") and self.buf:
                self.buf = b""                          # stray CRLF between requests
            self._fill()
        end = self.buf.index(b"\r\n\r\n") + 4
        if end > MAX_HEAD:
            return 431, "Request Header Fields Too Large"
        head = self.buf[:end]
        bad = check(head, self.port)
        if bad:
            return bad
        n = body_length(head)
        if n < 0:
            return 400, "Bad Request"
        while len(self.buf) < end + n:
            self._fill()
        req, self.buf = self.buf[:end + n], self.buf[end + n:]
        return req


class Forwarder:
    """Listens on 127.0.0.1:local and connects each client to `remote` on the phone."""

    def __init__(self, dev, local, remote, guard=True, log=None):
        self.dev, self.local, self.remote, self.guard = dev, local, remote, guard
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.server = socket.create_server(("127.0.0.1", local))     # SO_REUSEADDR, never SO_REUSEPORT
        self.open = set()
        self.lock = threading.Lock()
        self.closed = False

    def serve(self):
        while not self.closed:
            try:
                client, _ = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def close(self):
        self.closed = True
        try:
            self.server.close()
        except OSError:
            pass
        with self.lock:
            socks, self.open = list(self.open), set()
        for s in socks:
            _kill(s)

    def _track(self, *socks):
        with self.lock:
            self.open.update(socks)

    def _handle(self, client):
        self._track(client)
        usbmux.nodelay(client)                  # every MJPEG frame goes out whole, not tail-delayed
        reader = _Requests(client, self.local) if self.guard else None
        phone = None
        try:
            first = b""
            if reader:
                first = reader.next()
                if isinstance(first, tuple):
                    return _refuse(client, *first)
            try:
                phone = usbmux.connect(self.dev, self.remote)
            except OSError as e:
                return self.log(f"{self.local}: {e}")
            self._track(phone)
            phone.sendall(first)
            down = threading.Thread(target=self._copy, args=(phone, client), daemon=True)
            down.start()
            if reader:
                while True:
                    req = reader.next()
                    if isinstance(req, tuple):
                        _refuse(client, *req)
                        break
                    phone.sendall(req)
            else:
                self._copy(client, phone)
        except (EOFError, OSError):
            pass
        finally:
            for s in (client, phone):
                if s is not None:
                    _kill(s)
                    with self.lock:
                        self.open.discard(s)

    @staticmethod
    def _copy(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        # one side finished: end both (HTTP has nothing more to say), which also wakes the other thread
        _kill(src)
        _kill(dst)


def _kill(s):
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    s.close()


def run(ports, udid=None, guard=True, poll=POLL_S):
    """Forward each LOCAL[:REMOTE]; returns 2 when the phone goes away (1 if it is not there)."""
    pairs = parse_ports(ports)
    dev = usbmux.device_id(udid)
    if dev is None:
        print("No iPhone on USB. Plug it in, unlock it, tap Trust.", file=sys.stderr)
        return 1
    fwds = []
    try:
        for local, remote in pairs:
            fwds.append(Forwarder(dev, local, remote, guard))
    except OSError as e:
        for f in fwds:
            f.close()
        print(f"forward: cannot listen on 127.0.0.1:{local} ({e.strerror or e})", file=sys.stderr)
        return 1
    for f in fwds:
        threading.Thread(target=f.serve, daemon=True).start()
    print("forwarding " + ", ".join(f"127.0.0.1:{a} -> phone:{b}" for a, b in pairs), file=sys.stderr, flush=True)
    errors = 0
    try:
        while True:
            time.sleep(poll)
            try:
                if dev not in [d for d, _ in usbmux.usb_devices()]:
                    break                    # unplugged (a replugged phone gets a new DeviceID)
                errors = 0
            except OSError:
                errors += 1                  # usbmuxd restarting: allow a few polls
                if errors >= 3:
                    break
    except KeyboardInterrupt:
        return 0
    finally:
        for f in fwds:
            f.close()
    print("forward: the iPhone was unplugged", file=sys.stderr, flush=True)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m iphone_control.forward",
                                 description="Loopback-only port forward to the iPhone over usbmuxd.")
    ap.add_argument("--udid", help="the phone's UDID (default: the first USB device)")
    ap.add_argument("--device-id", action="store_true", help="print usbmuxd's DeviceID and exit")
    ap.add_argument("--raw", action="store_true", help="plain TCP, without the HTTP browser guard")
    ap.add_argument("ports", nargs="*", metavar="LOCAL[:REMOTE]")
    a = ap.parse_args(argv)
    try:
        if a.device_id:
            return usbmux.main(["device-id"] + (["--udid", a.udid] if a.udid else []))
        if not a.ports:
            ap.error("give at least one port, e.g. 8100 9100")
        return run(a.ports, a.udid, guard=not a.raw)
    except (OSError, ValueError) as e:
        print(f"forward: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
