"""The phone's USB device (iphone_control/usbfs.py): descriptors and lookup against a fake sysfs tree, and the
ioctl layer against a plain file (every ioctl fails there). No USB device is opened."""
import ctypes
import errno
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import valeria_fake as F  # noqa: E402
from support import UDID_A, UDID_B  # noqa: E402
from iphone_control import usbfs  # noqa: E402


class Descriptors(unittest.TestCase):
    def test_five_configurations_and_their_endpoints(self):
        configs = usbfs.configurations(F.iphone_descriptors(5))
        self.assertEqual(sorted(configs), [1, 2, 3, 4, 5])
        five = configs[5]
        self.assertEqual(five[0], (F.PTP, [0x81, 0x02, 0x83]))                   # interrupt endpoints count too
        self.assertEqual(five[1], (usbfs.USBMUX, [0x84, 0x03]))
        self.assertEqual(five[2], (usbfs.VALERIA, [F.VALERIA_IN, F.VALERIA_OUT]))
        self.assertEqual(five[3], (F.CDC_NCM, [0x88]))          # past the association and class descriptors
        self.assertEqual(five[4], (F.CDC_DATA, []))             # its endpoints are in alternate setting 1
        self.assertEqual(configs[2][2], (F.AUDIO_STREAM, []))
        self.assertNotIn(usbfs.VALERIA, [kind for kind, _ in configs[4].values()])
        self.assertEqual(sorted(usbfs.configurations(F.iphone_descriptors(4))), [1, 2, 3, 4])

    def test_one_configuration(self):
        d = F.iphone_descriptors(5)
        self.assertEqual(usbfs.interfaces(d, 5), usbfs.configurations(d)[5])
        self.assertEqual(usbfs.interfaces(d, 9), {})
        self.assertEqual(usbfs.configurations(d[18:]), usbfs.configurations(d))   # without the device descriptor

    def test_truncated_or_broken_descriptors_never_raise(self):
        d = F.iphone_descriptors(5)
        for n in range(len(d)):
            configs = usbfs.configurations(d[:n])
            self.assertTrue(set(configs) <= {1, 2, 3, 4, 5}, n)
        cut = len(F.iphone_descriptors(4)) + 20                                 # inside configuration 5
        self.assertEqual(sorted(usbfs.configurations(d[:cut])), [1, 2, 3, 4])
        zero = F.configuration(1, F.interface(0, F.PTP, (0x81,))) + bytes(2)
        zero = zero[:2] + (len(zero)).to_bytes(2, "little") + zero[4:]
        self.assertEqual(usbfs.configurations(zero)[1], {0: (F.PTP, [0x81])})  # a zero-length descriptor ends it
        bad_total = bytearray(F.configuration(1, F.interface(0, F.PTP)))
        bad_total[2] = 4                                                        # shorter than its own header
        self.assertEqual(usbfs.configurations(bytes(bad_total)), {})
        self.assertEqual(usbfs.configurations(b""), {})

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "the 64-bit layouts")
    def test_ioctl_numbers_are_the_kernels(self):
        # from linux/usbdevice_fs.h, printed by a C program on x86_64 (aarch64 encodes them the same way)
        self.assertEqual(usbfs.USBDEVFS_CONTROL, 0xC0185500)
        self.assertEqual(usbfs.USBDEVFS_BULK, 0xC0185502)
        self.assertEqual(usbfs.USBDEVFS_CLAIMINTERFACE, 0x8004550F)
        self.assertEqual(usbfs.USBDEVFS_RELEASEINTERFACE, 0x80045510)
        self.assertEqual(usbfs.USBDEVFS_IOCTL, 0xC0105512)
        self.assertEqual(usbfs.USBDEVFS_RESET, 0x5514)
        self.assertEqual(usbfs.USBDEVFS_CLEAR_HALT, 0x80045515)
        self.assertEqual(usbfs.USBDEVFS_DISCONNECT, 0x5516)
        self.assertEqual(usbfs.USBDEVFS_DISCONNECT_CLAIM, 0x8108551B)


class Sysfs(unittest.TestCase):
    def setUp(self):
        self.sysfs = F.FakeSysfs()
        self.addCleanup(self.sysfs.cleanup)

    def phone(self, name, udid, devnum, product="12a8", **attrs):
        values = dict(idVendor="05ac", idProduct=product, serial=udid.replace("-", ""), busnum=1, devnum=devnum,
                      bConfigurationValue=5, bNumConfigurations=5, descriptors=F.iphone_descriptors(5))
        values.update(attrs)
        self.sysfs.write(name, **values)

    def test_find_device_by_udid_with_or_without_dashes(self):
        self.phone("1-2", UDID_A, 5)
        self.phone("1-3", UDID_B, 6)
        for udid in (UDID_A, UDID_A.replace("-", ""), UDID_A.lower(), UDID_A.replace("-", "").lower()):
            self.assertEqual(usbfs.find_device(udid, self.sysfs.root).path, os.path.join(self.sysfs.root, "1-2"))
        self.assertEqual(usbfs.find_device(UDID_B, self.sysfs.root).node, "/dev/bus/usb/001/006")
        self.assertIsNone(usbfs.find_device("00008130-FAKE00000000CC", self.sysfs.root))
        self.phone("1-4", "", 7, serial="OLD-STYLE-" + "F" * 8)                # a serial with dashes in it
        self.assertEqual(usbfs.find_device("oldstyle" + "f" * 8, self.sysfs.root).node, "/dev/bus/usb/001/007")

    def test_without_udid_only_a_single_phone(self):
        self.assertIsNone(usbfs.find_device(None, self.sysfs.root))
        self.phone("1-2", UDID_A, 5)
        self.assertEqual(usbfs.find_device(None, self.sysfs.root).serial, UDID_A.replace("-", ""))
        self.phone("1-3", UDID_B, 6)
        self.assertIsNone(usbfs.find_device(None, self.sysfs.root))             # which one? none
        self.assertEqual(len(usbfs.apple_devices(self.sysfs.root)), 2)

    def test_other_devices_are_skipped(self):
        self.phone("1-2", UDID_A, 5)
        self.phone("1-5", UDID_B, 8, product="8600")                          # an Apple T2, not an iPhone
        self.phone("1-6", "00008130-FAKE00000000DD", 9, serial="")              # no serial (yet)
        self.sysfs.write("1-7", idVendor="05ac", serial="X")                     # no idProduct
        self.sysfs.write("1-8", idVendor="zz", idProduct="12a8", serial="X")
        self.phone("1-2:1.0", UDID_B, 10)                                       # an interface, not a device
        self.assertEqual([d.path for d in usbfs.apple_devices(self.sysfs.root)], [os.path.join(self.sysfs.root, "1-2")])
        self.assertEqual(usbfs.apple_devices(os.path.join(self.sysfs.root, "missing")), [])

    def test_device_attributes(self):
        self.phone("1-2", UDID_A, 5)
        d = usbfs.find_device(UDID_A, self.sysfs.root)
        self.assertEqual((d.node, d.config, d.num_configs), ("/dev/bus/usb/001/005", 5, 5))
        self.assertEqual(d.find(usbfs.VALERIA), (2, [F.VALERIA_IN, F.VALERIA_OUT]))
        self.assertEqual(d.find(usbfs.USBMUX), (1, [0x84, 0x03]))
        self.assertIsNone(d.find(usbfs.VALERIA, config=4))
        self.assertEqual(d.offers(usbfs.VALERIA), 5)
        self.phone("1-2", UDID_A, 5, bConfigurationValue=4, bNumConfigurations=4,
                   descriptors=F.iphone_descriptors(4))
        self.assertIsNone(d.find(usbfs.VALERIA))
        self.assertIsNone(d.offers(usbfs.VALERIA))
        self.phone("1-2", UDID_A, 5, bConfigurationValue="")                     # unconfigured
        self.assertEqual(d.config, 0)
        self.sysfs.remove("1-2")
        self.assertEqual(d.config, 0)                                           # gone: no exception
        with self.assertRaises(OSError):
            d.node


class _Recording(usbfs.Handle):
    """A Handle on a plain file whose ioctls are scripted: answers[request] = errno to fail with, or a function
    (arg) -> result."""

    def __init__(self, node, answers):
        super().__init__(node)
        self.answers, self.calls = answers, []

    def _ioctl(self, request, arg):
        self.calls.append((request, arg))
        a = self.answers.get(request, 0)
        if isinstance(a, int) and a:
            raise usbfs.UsbError(a, os.strerror(a))
        return a(arg) if callable(a) else a


class HandleErrors(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="not-usb-")
        os.close(fd)
        self.addCleanup(os.unlink, self.path)

    def test_open_failure_says_why(self):
        with self.assertRaises(usbfs.UsbError) as cm:
            usbfs.Handle(self.path + "-missing")
        self.assertEqual(cm.exception.errno, errno.ENOENT)
        self.assertIn("uaccess", str(cm.exception))

    def test_ioctls_on_a_file_that_is_no_usb_device(self):
        h = usbfs.Handle(self.path)
        buf = ctypes.create_string_buffer(16)
        for call in (lambda: h.control(0x80, 6, 0x100, 0, length=18), lambda: h.control(0x40, 0x52, 0, 2),
                     lambda: h.bulk_read(0x85, buf, 10), lambda: h.bulk_write(0x04, b"x"), lambda: h.clear_halt(0x85),
                     lambda: h.claim(2), h.reset):
            with self.assertRaises(usbfs.UsbError) as cm:
                call()
            self.assertEqual(cm.exception.errno, errno.ENOTTY)
        h.release(2)                                        # quiet
        h.close()
        h.close()
        with self.assertRaises(usbfs.UsbError) as cm:
            h.bulk_read(0x85, buf)
        self.assertEqual(cm.exception.errno, errno.EBADF)

    def test_claim_unbinds_a_kernel_driver_but_not_another_program(self):
        h = _Recording(self.path, {usbfs.USBDEVFS_CLAIMINTERFACE: errno.EBUSY})
        h.claim(2)
        (_, first), (request, dc) = h.calls
        self.assertEqual(request, usbfs.USBDEVFS_DISCONNECT_CLAIM)
        self.assertEqual((dc.interface, dc.flags, dc.driver), (2, usbfs.DISCONNECT_CLAIM_EXCEPT_DRIVER, b"usbfs"))
        self.assertEqual(h.claimed, {2})
        h = _Recording(self.path, {usbfs.USBDEVFS_CLAIMINTERFACE: errno.EBUSY,
                                   usbfs.USBDEVFS_DISCONNECT_CLAIM: errno.EBUSY})
        with self.assertRaises(usbfs.UsbError) as cm:
            h.claim(2)
        self.assertEqual(cm.exception.errno, errno.EBUSY)
        self.assertIn("another program", str(cm.exception))
        self.assertEqual(h.claimed, set())
        h.close()

    def test_close_releases_what_it_claimed(self):
        h = _Recording(self.path, {})
        h.claim(2)
        h.close()
        self.assertEqual([r for r, _ in h.calls], [usbfs.USBDEVFS_CLAIMINTERFACE, usbfs.USBDEVFS_RELEASEINTERFACE])
        self.assertEqual(h.fd, -1)

    def test_reset_of_a_device_that_already_went_away_is_quiet(self):
        _Recording(self.path, {usbfs.USBDEVFS_RESET: errno.ENODEV}).reset()
        with self.assertRaises(usbfs.UsbError):
            _Recording(self.path, {usbfs.USBDEVFS_RESET: errno.EACCES}).reset()

    def test_transfers(self):
        def control_in(c):
            ctypes.memmove(c.data, b"\x12\x01", 2)
            return 2
        h = _Recording(self.path, {usbfs.USBDEVFS_CONTROL: control_in, usbfs.USBDEVFS_BULK: lambda b: b.len - 1})
        self.assertEqual(h.control(0x80, 6, 0x100, 0, length=18), b"\x12\x01")
        c = h.calls[-1][1]
        self.assertEqual((c.bRequestType, c.bRequest, c.wValue, c.wIndex, c.wLength, c.timeout),
                         (0x80, 6, 0x100, 0, 18, 1000))
        with self.assertRaises(usbfs.UsbError) as cm:
            h.bulk_write(0x04, b"abc", timeout=500)
        self.assertEqual(cm.exception.errno, errno.EIO)                         # a short write
        b = h.calls[-1][1]
        self.assertEqual((b.ep, b.len, b.timeout), (0x04, 3, 500))
        buf = ctypes.create_string_buffer(64)
        self.assertEqual(h.bulk_read(0x85, buf), 63)
        b = h.calls[-1][1]
        self.assertEqual((b.ep, b.len, b.timeout, b.data), (0x85, 64, 0, ctypes.addressof(buf)))
        h.close()


if __name__ == "__main__":
    unittest.main()
