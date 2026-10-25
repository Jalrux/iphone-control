"""blehid.BleHid against a fake bluetoothd on a private dbus-daemon: registration, the GATT tree BlueZ reads,
notifications, the pairing agent, removal of unconfirmed bonds, errors and clean-up.

Nothing here touches the system bus or a Bluetooth adapter: BleHid gets the private bus's address, asking Gio
for the system bus fails the test, and DBUS_SYSTEM_BUS_ADDRESS points nowhere. The private bus can start a
stand-in bluetoothd by D-Bus activation, which only leaves a mark, so a test sees whether BleHid ever starts one.
Needs PyGObject and dbus-daemon (skipped otherwise).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from iphone_control import hid  # noqa: E402

try:
    from gi.repository import Gio, GLib
    from iphone_control import blehid
    from iphone_control.blehid import ADV, ADV_PATH, AGENT, AGENT_PATH, CHRC, DESC, OBJ, OM, PROPS, SERVICE
    HAVE_GI = True
except (ImportError, ValueError):
    HAVE_GI = False
DBUS_DAEMON = shutil.which("dbus-daemon")

# the system bus's defaults, minus its per-service holes: anyone may send and own names, nobody may eavesdrop
BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <listen>unix:path={path}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{services}</servicedir>
  <policy context="default">
    <allow send_destination="*"/>
    <allow send_type="signal"/>
    <allow receive_type="method_call"/>
    <allow receive_type="method_return"/>
    <allow receive_type="error"/>
    <allow receive_type="signal"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
# what D-Bus activation runs for a call to org.bluez while nobody owns it: a mark, no bluetoothd. It fails, so the
# call does at once (dbus-daemon waits for the name after an exit 0, the way a launcher that forks leaves)
BLUEZ_SERVICE = "[D-BUS Service]\nName=org.bluez\nExec={sh} -c 'echo > {mark}; exit 1'\n"

BLUEZ_XML = """<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
  </interface>
  <interface name="org.bluez.AgentManager1">
    <method name="RegisterAgent"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="UnregisterAgent"><arg type="o" direction="in"/></method>
    <method name="RequestDefaultAgent"><arg type="o" direction="in"/></method>
  </interface>
  <interface name="org.bluez.Adapter1">
    <method name="RemoveDevice"><arg type="o" direction="in"/></method>
    <property name="Address" type="s" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Alias" type="s" access="readwrite"/>
    <property name="Powered" type="b" access="readwrite"/>
    <property name="PowerState" type="s" access="read"/>
    <property name="Discoverable" type="b" access="readwrite"/>
    <property name="Pairable" type="b" access="readwrite"/>
  </interface>
  <interface name="org.bluez.GattManager1">
    <method name="RegisterApplication"><arg type="o" direction="in"/><arg type="a{sv}" direction="in"/></method>
    <method name="UnregisterApplication"><arg type="o" direction="in"/></method>
  </interface>
  <interface name="org.bluez.LEAdvertisingManager1">
    <method name="RegisterAdvertisement"><arg type="o" direction="in"/><arg type="a{sv}" direction="in"/></method>
    <method name="UnregisterAdvertisement"><arg type="o" direction="in"/></method>
  </interface>
  <interface name="org.bluez.Device1">
    <property name="Address" type="s" access="read"/>
    <property name="Alias" type="s" access="read"/>
    <property name="Paired" type="b" access="read"/>
    <property name="Connected" type="b" access="read"/>
    <property name="Trusted" type="b" access="readwrite"/>
  </interface>
</node>"""
TYPES = {"Address": "s", "Name": "s", "Alias": "s", "Powered": "b", "PowerState": "s", "Discoverable": "b",
         "Pairable": "b", "Paired": "b", "Bonded": "b", "Connected": "b", "Trusted": "b"}

# locally administered (made-up) addresses
PHONE = "/org/bluez/hci0/dev_02_00_00_00_00_01"
OTHER = "/org/bluez/hci0/dev_02_00_00_00_00_02"
ELSEWHERE = "/org/bluez/hci1/dev_02_00_00_00_00_03"
HID_UUID = "00001812-0000-1000-8000-00805f9b34fb"
AUDIO_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
PAIRABLE_ON_OFF = [("/org/bluez/hci0", "Pairable", True), ("/org/bluez/hci0", "Pairable", False)]
ADAPTER_IFS = ["org.bluez.Adapter1", "org.bluez.GattManager1", "org.bluez.LEAdvertisingManager1"]


def adapter(le=True, powered=True, pairable=False, state=None):
    ifs = {"org.bluez.Adapter1": {"Address": "02:00:00:00:00:A0", "Name": "fake", "Alias": "fake",
                                  "Powered": powered, "PowerState": state or ("on" if powered else "off"),
                                  "Discoverable": False, "Pairable": pairable}}
    if le:
        ifs.update({"org.bluez.GattManager1": {}, "org.bluez.LEAdvertisingManager1": {}})
    return ifs


def device(alias, connected=False, paired=False):
    return {"org.bluez.Device1": {"Address": "02:00:00:00:00:01", "Alias": alias, "Paired": paired,
                                  "Connected": connected, "Trusted": False}}


def bluez(adapters=None, devices=None):
    """bluetoothd's objects: {path: {interface: {property: value}}}"""
    objs = {"/org/bluez": {"org.bluez.AgentManager1": {}}}
    for name, ifs in (adapters or {"hci0": adapter(), "hci1": adapter()}).items():
        objs["/org/bluez/" + name] = ifs
    objs.update(devices or {PHONE: device("Phone"), OTHER: device("Stranger"), ELSEWHERE: device("Far")})
    return objs


class PrivateBus:
    """A dbus-daemon of its own on a socket in a temp dir."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        conf, services = os.path.join(self.dir.name, "bus.conf"), os.path.join(self.dir.name, "services")
        self.activated = os.path.join(self.dir.name, "activated")      # there once activation ran
        os.mkdir(services)
        with open(os.path.join(services, "org.bluez.service"), "w") as f:
            f.write(BLUEZ_SERVICE.format(sh=shutil.which("sh") or "/bin/sh", mark=self.activated))
        with open(conf, "w") as f:
            f.write(BUS_CONF.format(path=os.path.join(self.dir.name, "bus"), services=services))
        self.proc = subprocess.Popen([DBUS_DAEMON, "--config-file=" + conf, "--print-address", "--nofork"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        self.address = self.proc.stdout.readline().strip()

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.proc.stdout.close()
        self.dir.cleanup()


def connect(address):
    flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    return Gio.DBusConnection.new_for_address_sync(address, flags, None, None)


class FakeBluez:
    """bluetoothd's side of BlueZ's D-Bus API. It owns org.bluez on the private bus from a thread and main context
    of its own, so BleHid's blocking calls from the main thread are answered while that thread waits.

    objects: as bluetoothd's GetManagedObjects; fail: {method: D-Bus error name}.
    Like bluetoothd it reads the application (GetManagedObjects) and the advertisement (GetAll) before it answers
    their registration.
    """

    def __init__(self, address, objects, fail=None):
        self.address, self.objects, self.fail = address, objects, dict(fail or {})
        self.calls = []        # (method, object path, arguments) of every call to bluetoothd
        self.sets = []         # (object path, property, value) of every property written
        self.tree = None       # the application's objects as read when it registered
        self.adv = None        # the advertisement's properties as read when it registered
        self.notes = []        # (monotonic s, characteristic path, value) of every notification
        self.results = {}      # key -> a call to the client's reply: its unpacked value, or the D-Bus error name
        self.client = None     # the client's unique name
        self.ctx = GLib.MainContext.new()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the fake bluetoothd did not start")

    def _run(self):
        self.ctx.push_thread_default()
        self.conn = connect(self.address)
        info = {i.name: i for i in Gio.DBusNodeInfo.new_for_xml(BLUEZ_XML).interfaces}
        self.conn.register_object("/", info[OM], self._call, None, None)
        for path, ifs in self.objects.items():
            for iface in ifs:
                self.conn.register_object(path, info[iface], self._call, self._get, self._set)
        self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                            GLib.Variant("(su)", ("org.bluez", 4)), None, Gio.DBusCallFlags.NONE, 5000, None)
        self.unique = self.conn.get_unique_name()
        self.loop = GLib.MainLoop(self.ctx)
        self._ready.set()
        self.loop.run()
        self.conn.close_sync(None)
        self.ctx.pop_thread_default()

    def close(self):
        self.do(self.loop.quit)
        self._thread.join(10)

    def do(self, fn, *args):
        """Run fn(*args) on the fake's thread."""
        def once():
            fn(*args)
            return False
        self.ctx.invoke_full(GLib.PRIORITY_DEFAULT, once)

    # ---- bluetoothd's methods and properties ----
    def _call(self, conn, sender, path, iface, method, params, inv):
        args = params.unpack()
        self.calls.append((method, path, args))
        if method in self.fail:
            return inv.return_dbus_error(self.fail[method], f"{method} failed (test)")
        if method == "GetManagedObjects":
            tree = {p: {i: {k: GLib.Variant(TYPES[k], v) for k, v in props.items()} for i, props in ifs.items()}
                    for p, ifs in self.objects.items()}
            return inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", (tree,)))
        if method == "RegisterApplication":
            self.client = sender
            conn.signal_subscribe(sender, PROPS, "PropertiesChanged", None, CHRC, Gio.DBusSignalFlags.NONE,
                                  self._note)
            return self._ask(sender, args[0], OM, "GetManagedObjects", None,
                             lambda r: (setattr(self, "tree", r[0]), inv.return_value(None)),
                             lambda name: inv.return_dbus_error("org.bluez.Error.Failed", name))
        if method == "RegisterAdvertisement":
            return self._ask(sender, args[0], PROPS, "GetAll", GLib.Variant("(s)", (ADV,)),
                             lambda r: (setattr(self, "adv", r[0]), inv.return_value(None)),
                             lambda name: inv.return_dbus_error("org.bluez.Error.Failed", name))
        inv.return_value(None)

    def _get(self, conn, sender, path, iface, name):
        return GLib.Variant(TYPES[name], self.objects[path][iface][name])

    def _set(self, conn, sender, path, iface, name, value):
        self.sets.append((path, name, value.unpack()))
        self.objects[path][iface][name] = value.unpack()
        return True

    def _note(self, conn, sender, path, iface, signal, params):
        changed = params.unpack()[1]
        if "Value" in changed:
            self.notes.append((time.monotonic(), path, bytes(changed["Value"])))

    def _ask(self, dest, path, iface, method, args, then, fail):
        def done(conn, result):
            try:
                r = conn.call_finish(result).unpack()
            except GLib.Error as e:
                return fail(blehid._remote(e)[0] or e.message)
            then(r)
        self.conn.call(dest, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 5000, None, done)

    # ---- what the phone makes bluetoothd do ----
    def call_client(self, key, path, iface, method, args=None):
        """Call the client as bluetoothd would; the reply lands in results[key]."""
        self.do(self._ask, self.client, path, iface, method, args, lambda r: self.results.__setitem__(key, r),
                lambda name: self.results.__setitem__(key, name))

    def emit(self, path, iface, **changed):
        """A PropertiesChanged from bluetoothd (a device paired, connected...)."""
        props = {k: GLib.Variant(TYPES[k], v) for k, v in changed.items()}
        self.do(lambda: self.conn.emit_signal(None, path, PROPS, "PropertiesChanged",
                                              GLib.Variant("(sa{sv}as)", (iface, props, []))))

    def called(self, method):
        return [(path, args) for m, path, args in self.calls if m == method]


@unittest.skipUnless(HAVE_GI and DBUS_DAEMON, "needs PyGObject and dbus-daemon")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus = PrivateBus()
        if not cls.bus.address:
            cls.bus.close()
            raise unittest.SkipTest("dbus-daemon did not start")

    @classmethod
    def tearDownClass(cls):
        cls.bus.close()

    def setUp(self):
        for name in ("dbus_address_get_for_bus_sync", "bus_get_sync", "bus_get"):
            p = mock.patch.object(blehid.Gio, name, side_effect=AssertionError("the system bus must not be used"))
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent/test-bus"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("BT_INDEX", "IPHONE_HID_NAME"):
            os.environ.pop(name, None)
        if os.path.exists(self.bus.activated):
            os.remove(self.bus.activated)
        self.fake = None
        self.states, self.logs, self.asked = [], [], []
        self.reply = None

    def tearDown(self):
        if self.fake:
            self.fake.close()

    def spin(self, until, timeout=5, what="the condition"):
        """Run the main context (BleHid's handlers) until until() holds."""
        ctx = GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not until():
            if time.monotonic() > end:
                self.fail(f"timed out waiting for {what}")
            if not ctx.iteration(False):
                time.sleep(0.001)

    def settle(self, ms=100):
        end = time.monotonic() + ms / 1000
        self.spin(lambda: time.monotonic() > end)

    def bluez_gone(self):
        """The fake bluetoothd stops. Waits until the bus has seen it go without running BleHid's main context,
        so BleHid has not heard of it yet. Returns a connection of the test's own on the bus."""
        self.fake.close()
        self.fake = None
        probe = connect(self.bus.address)
        self.addCleanup(probe.close_sync, None)
        end = time.monotonic() + 5
        while True:
            try:
                probe.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "GetNameOwner", GLib.Variant("(s)", ("org.bluez",)), None, Gio.DBusCallFlags.NONE,
                                5000, None)
            except GLib.Error:
                return probe
            if time.monotonic() > end:
                self.fail("the fake bluetoothd did not go away")
            time.sleep(0.01)

    def ask(self, passkey, alias, reply):
        self.asked.append((passkey, alias))
        self.reply = reply

    def make(self, objects=None, fail=None, **kw):
        if self.fake is None:
            self.fake = FakeBluez(self.bus.address, objects or bluez(), fail)
        args = dict(name="Test Mouse", on_state=self.states.append, confirm=self.ask, log=self.logs.append,
                    bus=self.bus.address)
        args.update(kw)
        h = blehid.BleHid(**args)
        self.addCleanup(h.stop)
        return h

    def started(self, **kw):
        h = self.make(**kw)
        h.start()
        return h

    def client(self, key, path, iface, method, args=None):
        """bluetoothd calls the client; returns the reply (unpacked, or the D-Bus error name)."""
        self.fake.call_client(key, path, iface, method, args)
        self.spin(lambda: key in self.fake.results, what=f"the reply to {method}")
        return self.fake.results.pop(key)

    def chars(self):
        """{uuid: [characteristic path]} of the registered tree."""
        out = {}
        for path, ifs in sorted(self.fake.tree.items()):
            if CHRC in ifs:
                out.setdefault(ifs[CHRC]["UUID"], []).append(path)
        return out

    def reports(self, dev=PHONE):
        """{report id: characteristic path}, from the Report Reference descriptors, read like the phone does:
        bluetoothd names the device that reads (dev=None: a read that tells nothing about who)."""
        opts = {"device": GLib.Variant("o", dev)} if dev else {}
        out = {}
        for path, ifs in sorted(self.fake.tree.items()):
            if DESC in ifs and ifs[DESC]["UUID"] == "2908":
                rid, kind = bytes(self.client(path, path, DESC, "ReadValue", GLib.Variant("(a{sv})", (opts,)))[0])
                self.assertEqual(kind, 1, "an input report")
                out[rid] = ifs[DESC]["Characteristic"]
        return out

    def subscribe(self, *rids, dev=PHONE):
        paths = self.reports(dev)
        for rid in rids or hid.SIZES:
            self.assertEqual(self.client(("on", rid), paths[rid], CHRC, "StartNotify"), ())
        return paths

    def notes(self, path):
        return [v for _, p, v in self.fake.notes if p == path]


class Registration(Base):
    def test_registers_agent_application_and_advertisement(self):
        h = self.started()
        self.assertEqual(h.adapter, "/org/bluez/hci0")
        self.assertEqual(self.fake.called("RegisterAgent"), [("/org/bluez", (AGENT_PATH, "DisplayYesNo"))])
        self.assertEqual(self.fake.called("RequestDefaultAgent"), [("/org/bluez", (AGENT_PATH,))])
        self.assertEqual(self.fake.called("RegisterApplication"), [("/org/bluez/hci0", (OBJ, {}))])
        self.assertEqual(self.fake.called("RegisterAdvertisement"), [("/org/bluez/hci0", (ADV_PATH, {}))])
        self.assertEqual(self.fake.adv, {"Type": "peripheral", "ServiceUUIDs": ["1812"], "Appearance": 0x03C2,
                                         "LocalName": "Test Mouse", "Discoverable": True})
        self.assertEqual(self.states, ["advertising"])
        self.assertFalse(h.connected)
        self.assertIn("Settings > Bluetooth > Test Mouse > Pair", h.hint)
        self.assertTrue(any("Test Mouse" in m for m in self.logs), self.logs)

    def test_only_pairable_is_written_and_put_back(self):
        h = self.started()
        self.assertEqual(self.fake.sets, [("/org/bluez/hci0", "Pairable", True)])
        h.stop()
        self.assertEqual(self.fake.sets, PAIRABLE_ON_OFF)

    def test_a_pairable_adapter_is_left_alone(self):
        h = self.started(objects=bluez({"hci0": adapter(pairable=True)}))
        h.stop()
        self.assertEqual(self.fake.sets, [])

    def test_stop_unregisters_everything_in_reverse(self):
        h = self.started()
        h.stop()
        undone = [(m, p, a) for m, p, a in self.fake.calls if m.startswith("Unregister")]
        self.assertEqual(sorted(undone[:2]), [("UnregisterAdvertisement", "/org/bluez/hci0", (ADV_PATH,)),
                                              ("UnregisterApplication", "/org/bluez/hci0", (OBJ,))])
        self.assertEqual(undone[2:], [("UnregisterAgent", "/org/bluez", (AGENT_PATH,))])
        h.stop()                                      # twice is fine
        self.assertEqual(len([c for c in self.fake.calls if c[0].startswith("Unregister")]), 3)

    def test_can_start_again_after_stop(self):
        h = self.started()
        h.stop()
        h.start()
        self.assertEqual(len(self.fake.called("RegisterApplication")), 2)
        self.assertEqual(self.states, ["advertising", "advertising"])

    def test_the_default_is_the_system_bus(self):
        h = self.make(bus=None)
        with mock.patch.object(blehid.Gio, "dbus_address_get_for_bus_sync", return_value=self.bus.address) as get:
            h.start()
        get.assert_called_once_with(Gio.BusType.SYSTEM, None)
        self.assertEqual(self.states, ["advertising"])

    def test_default_name(self):
        with mock.patch.dict(os.environ, {"IPHONE_HID_NAME": "Desk Mouse"}):
            self.assertEqual(blehid.BleHid().name, "Desk Mouse")
        with mock.patch("socket.gethostname", return_value="box.example"):
            self.assertEqual(blehid.BleHid().name, "box Mouse")


class GattTree(Base):
    def test_services_characteristics_and_descriptors(self):
        self.started()
        tree = self.fake.tree
        services = {ifs[SERVICE]["UUID"]: p for p, ifs in tree.items() if SERVICE in ifs}
        self.assertEqual(set(services), {"1812", "180f", "180a"})
        self.assertTrue(all(tree[p][SERVICE]["Primary"] for p in services.values()))
        by_service = {}
        for p, ifs in tree.items():
            if CHRC in ifs:
                self.assertIn(ifs[CHRC]["Service"], services.values())
                self.assertTrue(p.startswith(OBJ + "/"))
                by_service.setdefault(tree[ifs[CHRC]["Service"]][SERVICE]["UUID"], []).append(ifs[CHRC]["UUID"])
            if DESC in ifs:
                self.assertIn(CHRC, tree[ifs[DESC]["Characteristic"]])
        self.assertEqual(sorted(by_service["1812"]), ["2a4a", "2a4b", "2a4c", "2a4d", "2a4d", "2a4d", "2a4d", "2a4e"])
        self.assertEqual(by_service["180f"], ["2a19"])
        self.assertEqual(sorted(by_service["180a"]), ["2a29", "2a50"])
        for p in self.chars()["2a4d"]:
            self.assertEqual(set(tree[p][CHRC]["Flags"]), {"read", "encrypt-read", "notify"})
        self.assertIn("encrypt-read", tree[self.chars()["2a4b"][0]][CHRC]["Flags"])

    def test_report_references_and_values(self):
        self.started()
        paths = self.reports()
        self.assertEqual(sorted(paths), [1, 2, 3, 4])
        for rid, path in paths.items():
            value = self.client(rid, path, CHRC, "ReadValue", GLib.Variant("(a{sv})", ({},)))[0]
            self.assertEqual(bytes(value), bytes(hid.SIZES[rid]))

    def test_report_map_reads_whole_and_from_an_offset(self):
        self.started()
        path = self.chars()["2a4b"][0]
        whole = self.client("a", path, CHRC, "ReadValue", GLib.Variant("(a{sv})", ({},)))[0]
        tail = self.client("b", path, CHRC, "ReadValue",
                           GLib.Variant("(a{sv})", ({"offset": GLib.Variant("q", 100)},)))[0]
        self.assertEqual(bytes(whole), hid.REPORT_MAP)
        self.assertEqual(bytes(tail), hid.REPORT_MAP[100:])
        info = self.client("c", self.chars()["2a4a"][0], CHRC, "ReadValue", GLib.Variant("(a{sv})", ({},)))[0]
        self.assertEqual(bytes(info), b"\x11\x01\x00\x02")


class Reports(Base):
    def test_nothing_is_sent_before_the_phone_subscribes(self):
        h = self.started()
        h.tap(0.5, 0.5)
        h.key(0x04)
        with self.assertRaises(blehid.BleError) as cm:   # not "3 characters have no key on a US keyboard"
            h.text("abc")
        self.assertEqual(str(cm.exception), "the iPhone is not connected")
        self.settle()
        self.assertEqual(self.fake.notes, [])

    def test_notifications_reach_bluetoothd_after_start_notify(self):
        h = self.started()
        paths = self.subscribe(hid.KEYBOARD)
        self.assertTrue(h.connected)
        self.assertEqual(self.states, ["advertising", "connected"])
        self.assertEqual(h.text("a"), 0)
        self.spin(lambda: len(self.fake.notes) >= 2, what="two keyboard reports")
        self.assertEqual(self.notes(paths[hid.KEYBOARD]), [hid.keyboard_report(0x04), hid.keyboard_report()])
        h.tap(0.5, 0.5)                               # the pointer is not subscribed: nothing goes there
        self.settle(200)
        self.assertEqual(self.notes(paths[hid.POINTER]), [])

    def test_a_tap_is_paced_on_the_main_loop(self):
        h = self.started()
        paths = self.subscribe()
        sent, send = [], h._send
        h._send = lambda rid, data: (sent.append(time.monotonic()), send(rid, data))
        h.tap(0.5, 0.25)
        self.assertEqual(len(sent), 1)                # the move goes at once, the rest on the main loop
        self.spin(lambda: len(self.notes(paths[hid.POINTER])) == 3, what="the tap's three reports")
        x, y = hid.to_abs(0.5), hid.to_abs(0.25)
        self.assertEqual(self.notes(paths[hid.POINTER]), [hid.pointer_report(x, y), hid.pointer_report(x, y, 1),
                                                          hid.pointer_report(x, y)])
        self.assertGreaterEqual(sent[1] - sent[0], 0.0599)  # settled 60 ms
        self.assertGreaterEqual(sent[2] - sent[1], 0.0499)  # held 50 ms
        self.assertLess(sent[2] - sent[0], 1)
        self.assertEqual(h._timer, 0)                 # nothing queued: no timer left running

    def test_a_fast_drag_sends_its_latest_point(self):
        h = self.started()
        paths = self.subscribe(hid.POINTER)
        h.press(0.1, 0.1)
        for i in range(20):
            h.drag_to(0.1 + i / 40, 0.5)
        h.release(0.6, 0.5)
        self.spin(lambda: len(self.notes(paths[hid.POINTER])) == 4, what="four pointer reports")
        self.settle()
        x0, y0, x1, y1 = hid.to_abs(0.1), hid.to_abs(0.1), hid.to_abs(0.6), hid.to_abs(0.5)
        self.assertEqual(self.notes(paths[hid.POINTER]), [hid.pointer_report(x0, y0), hid.pointer_report(x0, y0, 1),
                                                          hid.pointer_report(x1, y1, 1), hid.pointer_report(x1, y1)])

    def test_globe_chord_and_home(self):
        h = self.started()
        paths = self.subscribe()
        h.control_centre()
        h.home()
        self.spin(lambda: len(self.fake.notes) == 6, what="the chord and Home")
        self.assertEqual([(p, v) for _, p, v in self.fake.notes], [
            (paths[3], hid.consumer_report(hid.GLOBE)), (paths[2], hid.keyboard_report(hid.KEY_C)),
            (paths[2], hid.keyboard_report()), (paths[3], hid.consumer_report()),
            (paths[3], hid.consumer_report(hid.HOME)), (paths[3], hid.consumer_report())])

    def test_reports_go_to_bluetoothd_only(self):
        h = self.started()
        paths = self.subscribe(hid.KEYBOARD)
        seen = []
        other = connect(self.bus.address)
        self.addCleanup(other.close_sync, None)
        other.signal_subscribe(self.fake.client, PROPS, "PropertiesChanged", None, None, Gio.DBusSignalFlags.NONE,
                               lambda *a: seen.append(a[2]))
        other.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetId", None,
                        None, Gio.DBusCallFlags.NONE, 5000, None)       # the match rule is in place
        h._conn.emit_signal(None, "/probe", PROPS, "PropertiesChanged",
                            GLib.Variant("(sa{sv}as)", (CHRC, {}, [])))    # a broadcast does reach it...
        self.spin(lambda: seen, what="the probe")
        h.key(0x04)
        self.spin(lambda: len(self.notes(paths[hid.KEYBOARD])) == 2, what="the key")
        self.settle()
        self.assertEqual(seen, ["/probe"])            # ...the reports do not

    def test_stop_lets_go_of_a_held_button(self):
        h = self.started()
        paths = self.subscribe(hid.POINTER)
        h.press(0.5, 0.5)
        self.spin(lambda: len(self.notes(paths[hid.POINTER])) == 2, what="the press")
        h.stop()
        self.spin(lambda: len(self.notes(paths[hid.POINTER])) == 3, what="the release")
        self.assertEqual(self.notes(paths[hid.POINTER])[-1], hid.pointer_report(hid.to_abs(0.5), hid.to_abs(0.5)))


class Connection(Base):
    def test_stop_notify_disconnects(self):
        h = self.started()
        paths = self.subscribe()
        for rid in hid.SIZES:
            self.client(("off", rid), paths[rid], CHRC, "StopNotify")
        self.assertFalse(h.connected)
        self.assertEqual(self.states, ["advertising", "connected", "disconnected"])

    def test_battery_alone_is_not_connected(self):
        h = self.started()
        self.client("bat", self.chars()["2a19"][0], CHRC, "StartNotify")
        self.assertFalse(h.connected)

    def test_a_known_phone_going_away_disconnects_although_still_subscribed(self):
        h = self.started()
        paths = self.subscribe()
        self.client("r", paths[hid.KEYBOARD], CHRC, "ReadValue",
                    GLib.Variant("(a{sv})", ({"device": GLib.Variant("o", PHONE)},)))
        self.fake.emit(OTHER, "org.bluez.Device1", Connected=False)     # some other device: no matter
        self.settle()
        self.assertTrue(h.connected)
        self.fake.emit(PHONE, "org.bluez.Device1", Connected=False)
        self.spin(lambda: not h.connected, what="the disconnect")
        with self.assertRaises(blehid.BleError):
            h.text("a")
        self.fake.emit(PHONE, "org.bluez.Device1", Connected=True)      # back, with its subscriptions kept
        self.spin(lambda: h.connected, what="the reconnect")
        self.assertEqual(self.states, ["advertising", "connected", "disconnected", "connected"])

    def test_what_anyone_in_range_may_read_or_write_is_not_the_phone(self):
        h = self.started()
        self.subscribe()
        self.fake.emit(PHONE, "org.bluez.Device1", Connected=False)     # the phone walked off, still subscribed
        self.spin(lambda: not h.connected, what="the disconnect")
        stranger = {"device": GLib.Variant("o", OTHER)}
        for uuid in ("2a19", "2a29", "2a50", "2a4e"):   # battery, device information, protocol mode: no encryption
            self.client(uuid, self.chars()[uuid][0], CHRC, "ReadValue", GLib.Variant("(a{sv})", (stranger,)))
        self.client("cp", self.chars()["2a4c"][0], CHRC, "WriteValue", GLib.Variant("(aya{sv})", (b"\0", stranger)))
        self.settle()
        self.assertFalse(h.connected)
        self.assertEqual(self.states, ["advertising", "connected", "disconnected"])

    def test_a_subscriber_that_read_nothing_is_looked_up(self):
        # bonded before, with what it read kept: it subscribes and reads nothing, so bluetoothd is asked who is
        # connected; a device that is not paired, or is on another adapter, is not the phone
        h = self.started(objects=bluez(devices={PHONE: device("Phone", connected=True, paired=True),
                                                OTHER: device("Stranger", connected=True),
                                                ELSEWHERE: device("Far", connected=True, paired=True)}))
        self.subscribe(dev=None)
        self.spin(lambda: h.connected, what="the lookup")
        for dev in (OTHER, ELSEWHERE):
            self.fake.emit(dev, "org.bluez.Device1", Connected=False)
        self.settle()
        self.assertTrue(h.connected)
        self.fake.emit(PHONE, "org.bluez.Device1", Connected=False)
        self.spin(lambda: not h.connected, what="the disconnect")
        self.assertEqual(self.states, ["advertising", "connected", "disconnected"])

    def test_a_subscription_with_no_paired_device_connected_is_not_the_phone(self):
        h = self.started(objects=bluez(devices={PHONE: device("Phone", paired=True),
                                                OTHER: device("Stranger", connected=True)}))
        self.subscribe(dev=None)                      # BlueZ asks no encryption for a subscription
        self.spin(lambda: len(self.fake.called("GetManagedObjects")) > 1, what="the lookup")
        self.settle()
        self.assertFalse(h.connected)
        self.assertEqual(self.states, ["advertising"])

    def test_bluetoothd_going_away_disconnects(self):
        h = self.started()
        self.subscribe()
        self.fake.close()
        self.fake = None
        self.spin(lambda: not h.connected, what="the disconnect")
        self.assertEqual(self.states[-1], "disconnected")
        self.assertTrue(any("bluetoothd stopped" in m for m in self.logs), self.logs)
        h.stop()                                      # nothing to answer it: no hang, no error
        self.assertFalse(os.path.exists(self.bus.activated))

    def test_stop_never_starts_bluetoothd(self):
        h = self.started()
        probe = self.bluez_gone()
        h.stop()                                      # before BleHid heard of it: its calls go nowhere
        self.assertFalse(os.path.exists(self.bus.activated))
        with self.assertRaises(GLib.Error):           # where a call to org.bluez would have started one
            probe.call_sync("org.bluez", "/", OM, "GetManagedObjects", None, None, Gio.DBusCallFlags.NONE, 5000,
                            None)
        self.assertTrue(os.path.exists(self.bus.activated))

    def test_stop_leaves_a_restarted_bluetoothd_alone(self):
        h = self.started()
        self.bluez_gone()
        self.fake = FakeBluez(self.bus.address, bluez({"hci0": adapter(pairable=True)}))
        self.spin(lambda: any("bluetoothd stopped" in m for m in self.logs), what="the restart")
        h.stop()
        self.assertEqual((self.fake.calls, self.fake.sets), ([], []))   # no Pairable, not even an Unregister

    def test_stop_right_after_a_restart_leaves_the_new_bluetoothd_alone(self):
        h = self.started()
        self.bluez_gone()
        self.fake = FakeBluez(self.bus.address, bluez({"hci0": adapter(pairable=True)}))
        self.assertFalse(any("bluetoothd stopped" in m for m in self.logs), self.logs)   # not heard of it yet
        h.stop()
        self.assertEqual((self.fake.calls, self.fake.sets), ([], []))

    def test_an_adapter_that_went_away_is_not_written_to(self):
        h = self.started()
        self.subscribe()
        hci0 = GLib.Variant("(oas)", ("/org/bluez/hci0", ADAPTER_IFS))
        h._on_removed(h._conn, ":1.999", "/", OM, "InterfacesRemoved", hci0)    # not from bluetoothd
        self.fake.do(lambda: self.fake.conn.emit_signal(None, "/", OM, "InterfacesRemoved",
                                                        GLib.Variant("(oas)", ("/org/bluez/hci1", ADAPTER_IFS))))
        self.settle()
        self.assertTrue(h.connected)
        self.fake.do(lambda: self.fake.conn.emit_signal(None, "/", OM, "InterfacesRemoved", hci0))
        self.spin(lambda: not h.connected, what="the adapter going")
        self.assertTrue(any("hci0 went away" in m for m in self.logs), self.logs)
        h.stop()
        self.assertEqual(self.fake.sets, PAIRABLE_ON_OFF[:1])        # not put back on whatever is hci0 now
        self.assertEqual([m for m, _, _ in self.fake.calls if m.startswith("Unregister")], ["UnregisterAgent"])


class Agent(Base):
    def confirm_request(self, key="conf", dev=PHONE, passkey=123456):
        self.fake.call_client(key, AGENT_PATH, AGENT, "RequestConfirmation", GLib.Variant("(ou)", (dev, passkey)))

    def result(self, key):
        self.spin(lambda: key in self.fake.results, what=key)
        return self.fake.results.pop(key)

    def say_yes(self, dev=PHONE):
        self.confirm_request(dev=dev)
        self.spin(lambda: self.asked, what="the question")
        self.reply(True)
        self.assertEqual(self.result("conf"), ())

    def test_pairs_only_when_the_user_says_yes(self):
        self.started()
        self.confirm_request()
        self.spin(lambda: self.asked, what="the question")
        self.assertEqual(self.asked, [(123456, "Phone")])
        self.settle()
        self.assertNotIn("conf", self.fake.results)   # bluetoothd waits for the answer
        self.reply(True)
        self.assertEqual(self.result("conf"), ())
        self.settle()
        self.assertNotIn((PHONE, "Trusted", True), self.fake.sets)     # trusted once it has paired, not before
        self.fake.emit(PHONE, "org.bluez.Device1", Paired=True)
        self.spin(lambda: self.fake.sets[-1:] == [(PHONE, "Trusted", True)], what="Trusted")
        self.fake.emit(PHONE, "org.bluez.Device1", Bonded=True)        # newer BlueZ says so on its own
        self.settle()
        self.assertEqual(self.fake.called("RemoveDevice"), [])

    def test_the_device_that_paired_is_the_phone(self):
        h = self.started()
        self.say_yes()
        self.fake.emit(PHONE, "org.bluez.Device1", Paired=True)
        self.spin(lambda: (PHONE, "Trusted", True) in self.fake.sets, what="Trusted")
        self.subscribe(dev=None)
        self.assertTrue(h.connected)
        self.assertEqual(len(self.fake.called("GetManagedObjects")), 1)   # known: nothing to look up
        self.fake.emit(PHONE, "org.bluez.Device1", Connected=False)
        self.spin(lambda: not h.connected, what="the disconnect")

    def test_a_pairing_that_failed_uses_the_code_up(self):
        self.started()
        self.say_yes()
        self.fake.emit(PHONE, "org.bluez.Device1", Connected=False)    # Cancel on the phone: the link drops
        self.fake.emit(PHONE, "org.bluez.Device1", Paired=True)        # a later bond on that address
        self.spin(lambda: self.fake.called("RemoveDevice"), what="RemoveDevice")
        self.assertEqual(self.fake.called("RemoveDevice"), [("/org/bluez/hci0", (PHONE,))])
        self.assertNotIn((PHONE, "Trusted", True), self.fake.sets)

    def test_a_confirmed_code_runs_out(self):
        self.started()
        with mock.patch.object(blehid, "PAIR_S", 0):  # SMP gave up meanwhile
            self.say_yes()
        self.fake.emit(PHONE, "org.bluez.Device1", Paired=True)
        self.spin(lambda: self.fake.called("RemoveDevice"), what="RemoveDevice")
        self.assertNotIn((PHONE, "Trusted", True), self.fake.sets)

    def test_pairing_again_needs_a_new_code(self):
        self.started()
        self.say_yes()
        self.fake.emit(PHONE, "org.bluez.Device1", Paired=True)
        self.spin(lambda: (PHONE, "Trusted", True) in self.fake.sets, what="Trusted")
        self.fake.emit(PHONE, "org.bluez.Device1", Paired=False)       # forgotten on the laptop
        self.fake.emit(PHONE, "org.bluez.Device1", Paired=True)        # paired again, nobody confirmed a code
        self.spin(lambda: self.fake.called("RemoveDevice"), what="RemoveDevice")

    def test_no_means_rejected(self):
        self.started()
        self.confirm_request()
        self.spin(lambda: self.asked, what="the question")
        self.reply(False)
        self.assertEqual(self.result("conf"), "org.bluez.Error.Rejected")
        self.reply(True)                              # a second answer changes nothing
        self.settle()
        self.assertNotIn((PHONE, "Trusted", True), self.fake.sets)

    def test_nobody_to_ask_means_rejected(self):
        self.started(confirm=None)
        self.confirm_request()
        self.assertEqual(self.result("conf"), "org.bluez.Error.Rejected")

    def test_a_second_request_while_one_waits_is_rejected(self):
        self.started()
        self.confirm_request("one")
        self.spin(lambda: self.asked, what="the question")
        self.confirm_request("two", OTHER)
        self.assertEqual(self.result("two"), "org.bluez.Error.Rejected")
        self.reply(True)
        self.assertEqual(self.result("one"), ())

    def test_cancel_drops_the_question(self):
        self.started()
        self.confirm_request()
        self.spin(lambda: self.asked, what="the question")
        self.client("cancel", AGENT_PATH, AGENT, "Cancel")
        self.assertEqual(self.result("conf"), "org.bluez.Error.Rejected")
        self.reply(True)
        self.settle()
        self.assertNotIn((PHONE, "Trusted", True), self.fake.sets)

    def test_stop_answers_a_waiting_question(self):
        h = self.started()
        self.confirm_request()
        self.spin(lambda: self.asked, what="the question")
        h.stop()
        self.assertEqual(self.result("conf"), "org.bluez.Error.Rejected")

    def test_other_pairing_methods_are_refused(self):
        self.started()
        o = GLib.Variant("(o)", (PHONE,))
        self.assertEqual(self.client("a", AGENT_PATH, AGENT, "RequestAuthorization", o), "org.bluez.Error.Rejected")
        self.assertEqual(self.client("p", AGENT_PATH, AGENT, "RequestPasskey", o), "org.bluez.Error.Rejected")
        self.assertEqual(self.client("c", AGENT_PATH, AGENT, "RequestPinCode", o), "org.bluez.Error.Rejected")
        shown = GLib.Variant("(ouq)", (PHONE, 123456, 0))       # passkey entry: the bond would be removed again
        self.assertEqual(self.client("d", AGENT_PATH, AGENT, "DisplayPasskey", shown), ())
        self.assertTrue(any("not supported" in m for m in self.logs), self.logs)

    def test_paired_devices_may_use_their_services(self):
        # the default agent is asked about every device: headphones paired (not trusted) before iPhone Control
        # started still connect, on any adapter; a device that is not paired gets no service, the HID one neither
        h = self.started(objects=bluez(devices={PHONE: device("Phone"), OTHER: device("Headphones", paired=True),
                                                ELSEWHERE: device("Far", paired=True)}))
        for dev in (OTHER, ELSEWHERE):
            args = GLib.Variant("(os)", (dev, AUDIO_UUID))
            self.assertEqual(self.client(dev, AGENT_PATH, AGENT, "AuthorizeService", args), ())
        for uuid in (AUDIO_UUID, HID_UUID):
            args = GLib.Variant("(os)", (PHONE, uuid))
            self.assertEqual(self.client(uuid, AGENT_PATH, AGENT, "AuthorizeService", args), "org.bluez.Error.Rejected")
        h._removing.add(OTHER)                        # paired without a confirmed code: being removed
        args = GLib.Variant("(os)", (OTHER, AUDIO_UUID))
        self.assertEqual(self.client("r", AGENT_PATH, AGENT, "AuthorizeService", args), "org.bluez.Error.Rejected")
        self.assertEqual(self.fake.called("RemoveDevice"), [])

    def test_pairing_on_another_adapter_is_refused(self):
        self.started()
        self.confirm_request(dev=ELSEWHERE)
        self.assertEqual(self.result("conf"), "org.bluez.Error.Rejected")
        self.assertEqual(self.asked, [])
        o = GLib.Variant("(o)", (ELSEWHERE,))
        for method in ("RequestAuthorization", "RequestPasskey", "RequestPinCode"):
            self.assertEqual(self.client(method, AGENT_PATH, AGENT, method, o), "org.bluez.Error.Rejected")
        shown = GLib.Variant("(ouq)", (ELSEWHERE, 123456, 0))
        self.assertEqual(self.client("d", AGENT_PATH, AGENT, "DisplayPasskey", shown), "org.bluez.Error.Rejected")
        self.assertFalse(any("from the iPhone" in m for m in self.logs), self.logs)
        self.assertTrue(any("on hci1 refused" in m and "close it to pair" in m for m in self.logs), self.logs)

    def test_a_bond_without_confirmation_is_removed(self):
        self.started()
        self.fake.emit(OTHER, "org.bluez.Device1", Paired=True)
        self.spin(lambda: self.fake.called("RemoveDevice"), what="RemoveDevice")
        self.assertEqual(self.fake.called("RemoveDevice"), [("/org/bluez/hci0", (OTHER,))])
        self.assertTrue(any("paired without a confirmed code" in m for m in self.logs), self.logs)

    def test_a_bond_on_another_adapter_is_not_ours(self):
        self.started()
        self.fake.emit(ELSEWHERE, "org.bluez.Device1", Paired=True)
        self.settle()
        self.assertEqual(self.fake.called("RemoveDevice"), [])

    def test_only_bluetoothd_is_listened_to(self):
        h = self.started()
        other = connect(self.bus.address)
        self.addCleanup(other.close_sync, None)
        replies = {}

        def call(key, path, iface, method, args):
            def done(conn, result):
                try:
                    replies[key] = conn.call_finish(result).unpack()
                except GLib.Error as e:
                    replies[key] = blehid._remote(e)[0]
            other.call(self.fake.client, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 5000, None, done)

        paths = self.subscribe(hid.KEYBOARD)
        call("conf", AGENT_PATH, AGENT, "RequestConfirmation", GLib.Variant("(ou)", (OTHER, 1)))
        call("read", paths[hid.KEYBOARD], CHRC, "ReadValue", GLib.Variant("(a{sv})", ({},)))
        call("stop", paths[hid.KEYBOARD], CHRC, "StopNotify", None)
        self.spin(lambda: len(replies) == 3, what="the replies")
        self.assertEqual(set(replies.values()), {"org.freedesktop.DBus.Error.AccessDenied"})
        self.assertEqual(self.asked, [])
        self.assertTrue(h.connected)
        # a forged "paired": GLib >= 2.80.1 drops a signal whose sender does not own org.bluez before any handler
        # sees it (CVE-2024-34397), older GLib hands it on, so the handler checks the sender itself: called directly
        paired = GLib.Variant("(sa{sv}as)", ("org.bluez.Device1", {"Paired": GLib.Variant("b", True)}, []))
        h._on_device(h._conn, other.get_unique_name(), OTHER, PROPS, "PropertiesChanged", paired)
        self.settle()
        self.assertEqual(self.fake.called("RemoveDevice"), [])
        h._on_device(h._conn, self.fake.unique, OTHER, PROPS, "PropertiesChanged", paired)    # from bluetoothd
        self.spin(lambda: self.fake.called("RemoveDevice"), what="RemoveDevice")


class Errors(Base):
    def test_dbus_error_names_are_read_from_the_message(self):
        e = GLib.Error("GDBus.Error:org.bluez.Error.Failed: Maximum advertisements reached")
        self.assertEqual(blehid._remote(e), ("org.bluez.Error.Failed", "Maximum advertisements reached"))
        self.assertEqual(blehid._remote(GLib.Error("Timeout was reached")), (None, "Timeout was reached"))
        self.assertEqual(str(blehid._error("advertise over Bluetooth LE", e)),
                         "could not advertise over Bluetooth LE: Maximum advertisements reached "
                         "(org.bluez.Error.Failed)")

    def start_fails(self, **kw):
        h = self.make(**kw)
        with self.assertRaises(blehid.BleError) as cm:
            h.start()
        h.stop()                                      # after a failed start: nothing to do
        return str(cm.exception)

    def test_bluetoothd_not_running(self):
        h = blehid.BleHid(name="Test Mouse", log=self.logs.append, bus=self.bus.address)
        with self.assertRaises(blehid.BleError) as cm:
            h.start()
        self.assertIn("bluetoothd is not running", str(cm.exception))
        self.assertIn("systemctl start bluetooth", str(cm.exception))

    def test_no_le_adapter(self):
        msg = self.start_fails(objects=bluez({"hci0": adapter(le=False)}))
        self.assertIn("no Bluetooth LE adapter", msg)
        self.assertEqual(self.fake.called("RegisterAgent"), [])

    def test_the_first_le_adapter_is_used(self):
        h = self.started(objects=bluez({"hci0": adapter(le=False), "hci1": adapter()}))
        self.assertEqual(h.adapter, "/org/bluez/hci1")
        self.assertEqual(self.fake.called("RegisterApplication"), [("/org/bluez/hci1", (OBJ, {}))])

    def test_a_switched_on_adapter_comes_before_a_switched_off_one(self):
        h = self.started(objects=bluez({"hci0": adapter(powered=False), "hci1": adapter()}))
        self.assertEqual(h.adapter, "/org/bluez/hci1")
        self.assertEqual(self.states, ["advertising"])

    def test_with_every_adapter_off_the_first_one_says_so(self):
        msg = self.start_fails(objects=bluez({"hci0": adapter(powered=False), "hci1": adapter(powered=False)}))
        self.assertIn("Bluetooth is off on hci0", msg)

    def test_bt_index_and_adapter_choose(self):
        with mock.patch.dict(os.environ, {"BT_INDEX": "1"}):
            h = self.started()
        self.assertEqual(h.adapter, "/org/bluez/hci1")
        h.stop()
        self.assertEqual(self.started(adapter="hci1").adapter, "/org/bluez/hci1")

    def test_bt_index_errors(self):
        self.fake = FakeBluez(self.bus.address, bluez({"hci0": adapter(le=False)}))
        with mock.patch.dict(os.environ, {"BT_INDEX": "0"}):
            self.assertIn("hci0 cannot do Bluetooth LE", self.start_fails())
        with mock.patch.dict(os.environ, {"BT_INDEX": "5"}):
            msg = self.start_fails()
        self.assertIn("no Bluetooth adapter hci5 (BT_INDEX=5)", msg)

    def test_bluetooth_switched_off(self):
        msg = self.start_fails(objects=bluez({"hci0": adapter(powered=False)}))
        self.assertIn("Bluetooth is off on hci0", msg)
        self.assertIn("bluetoothctl power on", msg)
        self.assertEqual(self.fake.calls[-1][0], "GetManagedObjects")    # nothing registered, nothing written
        self.assertEqual(self.fake.sets, [])

    def test_bluetooth_blocked(self):
        msg = self.start_fails(objects=bluez({"hci0": adapter(powered=False, state="off-blocked")}))
        self.assertIn("rfkill unblock bluetooth", msg)

    def test_not_allowed(self):
        msg = self.start_fails(fail={"RegisterAgent": "org.freedesktop.DBus.Error.AccessDenied"})
        self.assertIn("does not allow this user", msg)
        self.assertIn("'bluetooth' group", msg)
        self.assertIn("desktop session", msg)

    def test_a_failed_registration_is_undone(self):
        msg = self.start_fails(fail={"RegisterAdvertisement": "org.bluez.Error.Failed"})
        self.assertIn("could not advertise over Bluetooth LE: RegisterAdvertisement failed (test) "
                      "(org.bluez.Error.Failed)", msg)
        self.assertFalse(msg.startswith("Bluetooth:"), msg)       # the window adds that
        self.assertEqual(self.fake.called("UnregisterApplication"), [("/org/bluez/hci0", (OBJ,))])
        self.assertEqual(self.fake.called("UnregisterAgent"), [("/org/bluez", (AGENT_PATH,))])
        self.assertEqual(self.fake.called("UnregisterAdvertisement"), [])
        self.assertEqual(self.fake.sets, PAIRABLE_ON_OFF)
        self.assertEqual(self.states, [])


if __name__ == "__main__":
    unittest.main()
