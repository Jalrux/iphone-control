"""usbmuxd client (stdlib only): list iPhones on USB, map a UDID to usbmuxd's DeviceID, open a TCP
connection to a port on the phone. The same "Connect" message iproxy uses; it reaches WebDriverAgent's
ports 8100/9100 on iOS 17+ too.

  python3 -m iphone_control.usbmux list                      DeviceID<TAB>UDID for each USB device
  python3 -m iphone_control.usbmux device-id [--udid U]      usbmuxd's number (what plumesign --udid wants)
  python3 -m iphone_control.usbmux forward [--udid U] [--raw] LOCAL[:REMOTE] ...
                                                             loopback-only port forward (see forward.py)
Exit status: 0 ok, 1 no device / usbmuxd error, 2 the phone went away while forwarding.
"""
import argparse
import os
import plistlib
import socket
import struct
import sys

DEFAULT_SOCKET = "/var/run/usbmuxd"
VERSION, PLIST = 1, 8            # protocol version 1, plist messages
CLIENT = {"ClientVersionString": "iphone-control", "ProgName": "iphone-control"}
RESULTS = {1: "bad command", 2: "no such device", 3: "connection refused (nothing listening)",
           6: "bad protocol version"}
MAX_REPLY = 16 << 20


class UsbmuxError(OSError):
    pass


def address():
    """usbmuxd's address from $USBMUXD_SOCKET_ADDRESS, else /var/run/usbmuxd. Returns (family, address).

    Accepted: an absolute socket path (the form go-ios and plumesign also read), "unix:///path" or
    libusbmuxd's "UNIX:/path", and "host:port" or "tcp://host:port" for TCP.
    """
    env = os.environ.get("USBMUXD_SOCKET_ADDRESS", "").strip()
    if not env:
        return socket.AF_UNIX, DEFAULT_SOCKET
    low = env.lower()
    if env.startswith("/"):
        return socket.AF_UNIX, env
    for prefix in ("unix://", "unix:"):
        if low.startswith(prefix) and env[len(prefix):].startswith("/"):
            return socket.AF_UNIX, env[len(prefix):]
    if low.startswith("tcp://"):
        env = env[6:]
    host, _, port = env.rpartition(":")
    if port.isascii() and port.isdigit() and 0 < int(port) < 65536:
        return socket.AF_INET, (host.strip("[]") or "127.0.0.1", int(port))
    raise UsbmuxError(f"bad USBMUXD_SOCKET_ADDRESS: {os.environ['USBMUXD_SOCKET_ADDRESS']} (an absolute socket "
                      "path or host:port)")


def nodelay(sock):
    """Turn off Nagle on a TCP socket (a no-op for UNIX sockets).

    Without it the small tail of each MJPEG frame waits for the reader's delayed ACK, so frames arrive
    in pairs and each one lands ~30 ms late (measured at q60 through the forward).
    """
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
    return sock


def _open(timeout=5):
    family, addr = address()
    if family != socket.AF_UNIX:
        return nodelay(socket.create_connection(addr, timeout))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(addr)
    except OSError as e:
        s.close()
        raise UsbmuxError(f"usbmuxd not reachable at {addr} ({e.strerror or e}) - is usbmuxd installed "
                          "and running?") from None
    return s


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise UsbmuxError("usbmuxd closed the connection")
        buf += chunk
    return bytes(buf)


def _send(sock, msg, tag=1):
    body = plistlib.dumps(dict(CLIENT, **msg))
    sock.sendall(struct.pack("<IIII", 16 + len(body), VERSION, PLIST, tag) + body)


def _recv(sock):
    length, _version, kind, _tag = struct.unpack("<IIII", _recv_exact(sock, 16))
    if kind != PLIST or not 16 <= length <= MAX_REPLY:
        raise UsbmuxError(f"unexpected usbmuxd reply (type {kind}, length {length})")
    try:
        return plistlib.loads(_recv_exact(sock, length - 16))
    except Exception as e:          # plistlib raises several types on garbage
        raise UsbmuxError(f"bad usbmuxd reply: {e}") from None


def request(msg, timeout=5):
    """Send one message to usbmuxd and return its reply (a dict)."""
    with _open(timeout) as s:
        _send(s, msg)
        return _recv(s)


def usb_devices():
    """[(DeviceID, UDID), ...] for every device attached over USB (not Wi-Fi)."""
    out = []
    for d in request({"MessageType": "ListDevices"}).get("DeviceList", []):
        p = d.get("Properties", {})
        if p.get("ConnectionType") == "USB":
            out.append((d.get("DeviceID", p.get("DeviceID")), p.get("SerialNumber", "")))
    return out


def _same_udid(a, b):
    return a.replace("-", "").lower() == b.replace("-", "").lower()


def device_id(udid=None):
    """usbmuxd's DeviceID for the phone with this UDID, or the first USB device; None if absent."""
    for dev, serial in usb_devices():
        if not udid or _same_udid(serial, udid):
            return dev
    return None


def connect(dev, port, timeout=5):
    """A socket connected to TCP `port` on the phone (a raw pipe once usbmuxd says yes)."""
    s = _open(timeout)
    try:
        _send(s, {"MessageType": "Connect", "DeviceID": dev, "PortNumber": socket.htons(port)})
        r = _recv(s)
        n = r.get("Number", -1)
        if r.get("MessageType") != "Result" or n != 0:
            raise UsbmuxError(f"phone port {port}: {RESULTS.get(n, f'error {n}')}")
        s.settimeout(None)
        return s
    except BaseException:
        s.close()
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m iphone_control.usbmux",
                                 description="usbmuxd client: list iPhones on USB, find DeviceIDs, forward ports.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="DeviceID and UDID of each USB device")
    p = sub.add_parser("device-id", help="usbmuxd's DeviceID for the phone")
    p.add_argument("--udid")
    p = sub.add_parser("forward", help="forward loopback ports to the phone")
    p.add_argument("--udid")
    p.add_argument("--raw", action="store_true", help="plain TCP, without the HTTP browser guard")
    p.add_argument("ports", nargs="+", metavar="LOCAL[:REMOTE]")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            for dev, udid in usb_devices():
                print(f"{dev}\t{udid}")
            return 0
        if a.cmd == "device-id":
            dev = device_id(a.udid)
            if dev is None:
                print("No iPhone on USB. Plug it in, unlock it, tap Trust.", file=sys.stderr)
                return 1
            print(dev)
            return 0
        from . import forward
        return forward.run(a.ports, udid=a.udid, guard=not a.raw)
    except OSError as e:
        print(f"usbmux: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
