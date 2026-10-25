"""The iPhone's USB device through Linux usbfs (stdlib only: sysfs, fcntl.ioctl, ctypes buffers).

Used for the phone's screen-capture interface ("Valeria", see valeria.py), which usbmuxd leaves alone. Nothing
else here needs root: the desktop session already has read/write access to the phone's /dev/bus/usb node (udev's
uaccess tag), and usbmuxd keeps its own interface.

Devices are found in sysfs (/sys/bus/usb/devices: vendor, serial, configuration, the raw descriptors), so no
libusb is needed; transfers are the usbfs ioctls libusb uses itself. The layouts are the 64-bit ones (x86_64 and
aarch64, the only Linux builds go-ios has).
"""
import ctypes
import errno
import fcntl
import os
import struct

SYSFS = "/sys/bus/usb/devices"
APPLE = 0x05AC
IOS_PRODUCTS = (0x1290, 0x12AF)   # iPhone, iPad and iPod product ids (usbmuxd's range)
VALERIA = (0xFF, 0x2A, 0xFF)      # interface class / subclass / protocol of the capture interface
USBMUX = (0xFF, 0xFE, 0x02)


class UsbError(OSError):
    pass


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("U") << 8) | nr


class _Ctrl(ctypes.Structure):
    _fields_ = [("bRequestType", ctypes.c_uint8), ("bRequest", ctypes.c_uint8), ("wValue", ctypes.c_uint16),
                ("wIndex", ctypes.c_uint16), ("wLength", ctypes.c_uint16), ("timeout", ctypes.c_uint32),
                ("data", ctypes.c_void_p)]


class _Bulk(ctypes.Structure):
    _fields_ = [("ep", ctypes.c_uint), ("len", ctypes.c_uint), ("timeout", ctypes.c_uint),
                ("data", ctypes.c_void_p)]


class _Ioctl(ctypes.Structure):
    _fields_ = [("ifno", ctypes.c_int), ("ioctl_code", ctypes.c_int), ("data", ctypes.c_void_p)]


class _DisconnectClaim(ctypes.Structure):
    _fields_ = [("interface", ctypes.c_uint), ("flags", ctypes.c_uint), ("driver", ctypes.c_char * 256)]


USBDEVFS_CONTROL = _ioc(3, 0, ctypes.sizeof(_Ctrl))
USBDEVFS_BULK = _ioc(3, 2, ctypes.sizeof(_Bulk))
USBDEVFS_CLAIMINTERFACE = _ioc(2, 15, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_RELEASEINTERFACE = _ioc(2, 16, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_IOCTL = _ioc(3, 18, ctypes.sizeof(_Ioctl))
USBDEVFS_RESET = _ioc(0, 20, 0)
USBDEVFS_CLEAR_HALT = _ioc(2, 21, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_DISCONNECT = _ioc(0, 22, 0)
USBDEVFS_DISCONNECT_CLAIM = _ioc(2, 27, ctypes.sizeof(_DisconnectClaim))
DISCONNECT_CLAIM_EXCEPT_DRIVER = 0x02


def configurations(descriptors):
    """{configuration value: {interface number: ((class, subclass, protocol), [endpoint addresses])}} from the
    raw bytes of sysfs' "descriptors" (the device descriptor, then every configuration descriptor). Alternate
    settings other than 0 and class-specific descriptors are skipped; a truncated tail is ignored."""
    i, out = 18 if descriptors[1:2] == b"\x01" else 0, {}
    while i + 9 <= len(descriptors):
        length, kind = descriptors[i], descriptors[i + 1]
        if kind != 2 or length < 9:
            break
        total = struct.unpack_from("<H", descriptors, i + 2)[0]
        if total < length or i + total > len(descriptors):
            break
        j, cur, found = i + length, None, {}
        while j + 2 <= i + total:
            n, k = descriptors[j], descriptors[j + 1]
            if n < 2 or j + n > i + total:
                break
            if k == 4 and n >= 9 and descriptors[j + 3] == 0:          # interface, alternate setting 0
                cur = descriptors[j + 2]
                found[cur] = (tuple(descriptors[j + 5:j + 8]), [])
            elif k == 4:
                cur = None
            elif k == 5 and n >= 7 and cur is not None:                # endpoint
                found[cur][1].append(descriptors[j + 2])
            j += n
        out.setdefault(descriptors[i + 5], found)
        i += total
    return out


def interfaces(descriptors, config_value):
    """{interface number: ((class, subclass, protocol), [endpoint addresses])} of one configuration."""
    return configurations(descriptors).get(config_value, {})


class Device:
    """One Apple USB device as sysfs shows it."""

    def __init__(self, path):
        self.path = path

    def attr(self, name):
        with open(os.path.join(self.path, name)) as f:
            return f.read().strip()

    @property
    def serial(self):
        return self.attr("serial")

    @property
    def config(self):
        """The active configuration (0 = unconfigured, or the device is going away)."""
        try:
            return int(self.attr("bConfigurationValue") or 0)
        except (OSError, ValueError):
            return 0

    @property
    def num_configs(self):
        return int(self.attr("bNumConfigurations"))

    @property
    def node(self):
        return f"/dev/bus/usb/{int(self.attr('busnum')):03d}/{int(self.attr('devnum')):03d}"

    def descriptors(self):
        with open(os.path.join(self.path, "descriptors"), "rb") as f:
            return f.read()

    def interfaces(self, config=None):
        return interfaces(self.descriptors(), self.config if config is None else config)

    def find(self, triple, config=None):
        """(interface number, [endpoints]) of the first interface with this class triple, or None."""
        for number, (kind, eps) in sorted(self.interfaces(config).items()):
            if kind == triple:
                return number, eps
        return None

    def offers(self, triple):
        """The value of the first configuration with an interface of this class triple, or None."""
        for value, ifaces in sorted(configurations(self.descriptors()).items()):
            if any(kind == triple for kind, _ in ifaces.values()):
                return value
        return None


def same_serial(serial, udid):
    return serial.replace("-", "").lower() == udid.replace("-", "").lower()


def apple_devices(sysfs=SYSFS):
    out = []
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return out
    for name in names:
        if ":" in name:                                   # an interface, not a device
            continue
        d = Device(os.path.join(sysfs, name))
        try:
            if int(d.attr("idVendor"), 16) == APPLE and IOS_PRODUCTS[0] <= int(d.attr("idProduct"), 16) <= \
                    IOS_PRODUCTS[1] and d.serial:
                out.append(d)
        except (OSError, ValueError):                     # not a device, or going away while we look
            continue
    return out


def find_device(udid=None, sysfs=SYSFS):
    """The phone with this UDID (dashes and case ignored), or the only Apple device; None if absent."""
    devs = apple_devices(sysfs)
    if udid:
        for d in devs:
            try:
                if same_serial(d.serial, udid):
                    return d
            except OSError:
                continue
        return None
    return devs[0] if len(devs) == 1 else None


class Handle:
    """An open usbfs node: control and bulk transfers, claiming interfaces.

    Writes are not thread-safe (the caller serialises them); a bulk read on one endpoint may block on another
    thread meanwhile (the kernel handles each URB on its own). release() is what wakes a read blocked on the
    interface (the kernel kills its URBs, the read fails with ESHUTDOWN), so join the reader before close()."""

    def __init__(self, node):
        try:
            self.fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        except OSError as e:
            raise UsbError(e.errno, f"cannot open {node}: {e.strerror} (the desktop session normally has access "
                                    "through udev's uaccess tag)") from None
        self.claimed = set()

    def _ioctl(self, request, arg):
        fd = self.fd
        if fd < 0:
            raise UsbError(errno.EBADF, "the USB device is closed")
        try:
            return fcntl.ioctl(fd, request, arg)
        except OSError as e:
            raise UsbError(e.errno, os.strerror(e.errno)) from None

    def control(self, request_type, request, value=0, index=0, data=b"", length=None, timeout=1000):
        """A control transfer; returns the bytes read (IN) or the number written (OUT)."""
        size = len(data) if length is None else length
        buf = ctypes.create_string_buffer(bytes(data), size) if size else None
        c = _Ctrl(request_type, request, value, index, size, timeout, ctypes.addressof(buf) if buf else None)
        n = self._ioctl(USBDEVFS_CONTROL, c)
        return buf.raw[:n] if request_type & 0x80 and buf else n

    def bulk_read(self, ep, buf, timeout=0):
        """Read into the ctypes buffer buf from IN endpoint ep; returns the number of bytes. timeout in ms, 0 =
        wait for ever. The GIL is released while it waits.

        A finite timeout loses data: when it expires, usbfs drops what the transfer had already received."""
        return self._ioctl(USBDEVFS_BULK, _Bulk(ep, ctypes.sizeof(buf), timeout, ctypes.addressof(buf)))

    def bulk_write(self, ep, data, timeout=2000):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        done = self._ioctl(USBDEVFS_BULK, _Bulk(ep, len(data), timeout, ctypes.addressof(buf)))
        if done != len(data):
            raise UsbError(errno.EIO, f"short write: {done} of {len(data)} bytes")
        return done

    def clear_halt(self, ep):
        """CLEAR_FEATURE(ENDPOINT_HALT) to the endpoint, and the host's data toggle back to 0 as well."""
        self._ioctl(USBDEVFS_CLEAR_HALT, ctypes.c_uint(ep))

    def claim(self, number):
        """Claim an interface. A kernel driver holding it is unbound first; another program holding it (usbfs,
        i.e. libusb or a second capture) is left alone: EBUSY."""
        try:
            self._ioctl(USBDEVFS_CLAIMINTERFACE, ctypes.c_uint(number))
        except UsbError as e:
            if e.errno != errno.EBUSY:
                raise
            try:
                self._ioctl(USBDEVFS_DISCONNECT_CLAIM, _DisconnectClaim(number, DISCONNECT_CLAIM_EXCEPT_DRIVER,
                                                                        b"usbfs"))
            except UsbError as e2:
                if e2.errno != errno.EBUSY:
                    raise
                raise UsbError(errno.EBUSY, f"USB interface {number} is in use by another program") from None
        self.claimed.add(number)

    def release(self, number):
        try:
            self._ioctl(USBDEVFS_RELEASEINTERFACE, ctypes.c_uint(number))
        except UsbError:
            pass
        self.claimed.discard(number)

    def reset(self):
        """A USB port reset: the phone disconnects and enumerates again (a new devnum) within about 2 s, as if
        replugged. usbmuxd's connections to it end too."""
        try:
            self._ioctl(USBDEVFS_RESET, 0)
        except UsbError as e:
            if e.errno != errno.ENODEV:                 # the usual answer: it is already gone
                raise

    def close(self):
        for n in list(self.claimed):
            self.release(n)
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)
