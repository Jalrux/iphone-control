"""The laptop as a Bluetooth LE mouse and keyboard for the iPhone (HID over GATT), for basic mode: no Developer
Mode needed; with AssistiveTouch on, the phone's pointer does the touching.

  hid = BleHid(on_state=..., confirm=..., log=...)
  hid.start()                 # with the GLib main loop running: replies and reports are handled on it
  hid.tap(0.5, 0.5)           # fractions of the screen as the phone shows it (x right, y down)

Everything goes through Gio's D-Bus client on the system bus (no dbus-python): a GATT application (HID 1812 with
four input reports, battery 180f, device information 180a), an LE advertisement and a pairing agent, registered
with bluetoothd. The report map, the keymap and the timing are in hid.py; here the reports go out on the main
loop, at most one per hid.PACE_MS, with the waits hid.py puts between them.

The adapter is left as it is apart from Pairable, which is on while this runs and put back by stop():
Discoverable, Alias and Powered are never written. The advertisement is discoverable by itself, so Classic
Bluetooth keeps working (measured on an iPhone 15 Pro, iOS 26.5.2, Oct 2026: it pairs over LE with the adapter
not Classic-discoverable). A device may bond only once the user confirmed its six-digit code through confirm();
one that bonds without that is removed again, as alt/bluetooth-mouse/hid_ble.py does.
Every call goes to the bluetoothd these registrations are with, by its unique bus name: nothing here starts
bluetoothd or talks to one started since.
Call everything from the thread that runs the main loop.
"""
import math
import os
import socket
import time

from gi.repository import Gio, GLib

from . import hid

BLUEZ = "org.bluez"
BUS = "org.freedesktop.DBus"
OM = "org.freedesktop.DBus.ObjectManager"
PROPS = "org.freedesktop.DBus.Properties"
ADAPTER, DEVICE = "org.bluez.Adapter1", "org.bluez.Device1"
GATT_MANAGER, ADV_MANAGER = "org.bluez.GattManager1", "org.bluez.LEAdvertisingManager1"
AGENT_MANAGER = "org.bluez.AgentManager1"
SERVICE, CHRC, DESC = "org.bluez.GattService1", "org.bluez.GattCharacteristic1", "org.bluez.GattDescriptor1"
ADV, AGENT = "org.bluez.LEAdvertisement1", "org.bluez.Agent1"
OBJ = "/org/iphonecontrol/hid"            # the GATT application; its services below it
ADV_PATH, AGENT_PATH = OBJ + "/adv0", OBJ + "/agent"
REJECTED = "org.bluez.Error.Rejected"
CALL_MS = 10000
PAIR_S = 30                               # a confirmed code is good this long: SMP gives up 30 s after its last
                                          # message (Bluetooth Core spec, Vol 3 Part H 3.4)
APPEARANCE = 0x03C2                       # mouse
INPUT = (hid.MOUSE, hid.KEYBOARD, hid.CONSUMER, hid.POINTER)
PAIR_HINT = "On the iPhone: Settings > Bluetooth > {name} > Pair, then Accessibility > Touch > AssistiveTouch on"

XML = """<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
  </interface>
  <interface name="org.bluez.GattService1">
    <property name="UUID" type="s" access="read"/>
    <property name="Primary" type="b" access="read"/>
    <property name="Characteristics" type="ao" access="read"/>
  </interface>
  <interface name="org.bluez.GattCharacteristic1">
    <method name="ReadValue"><arg type="a{sv}" direction="in"/><arg type="ay" direction="out"/></method>
    <method name="WriteValue"><arg type="ay" direction="in"/><arg type="a{sv}" direction="in"/></method>
    <method name="StartNotify"/>
    <method name="StopNotify"/>
    <property name="UUID" type="s" access="read"/>
    <property name="Service" type="o" access="read"/>
    <property name="Flags" type="as" access="read"/>
    <property name="Descriptors" type="ao" access="read"/>
  </interface>
  <interface name="org.bluez.GattDescriptor1">
    <method name="ReadValue"><arg type="a{sv}" direction="in"/><arg type="ay" direction="out"/></method>
    <property name="UUID" type="s" access="read"/>
    <property name="Characteristic" type="o" access="read"/>
    <property name="Flags" type="as" access="read"/>
  </interface>
  <interface name="org.bluez.LEAdvertisement1">
    <method name="Release"/>
    <property name="Type" type="s" access="read"/>
    <property name="ServiceUUIDs" type="as" access="read"/>
    <property name="Appearance" type="q" access="read"/>
    <property name="LocalName" type="s" access="read"/>
    <property name="Discoverable" type="b" access="read"/>
  </interface>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
    <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
    <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
    <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
    <method name="DisplayPasskey">
      <arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/>
    </method>
    <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Cancel"/>
  </interface>
</node>"""


class BleError(OSError):
    """Bluetooth could not be set up; the message says what to do about it."""


def _hostname():
    return socket.gethostname().split(".")[0] or "Linux"


def _label(device):
    """/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF -> AA:BB:CC:DD:EE:FF"""
    return device.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":")


def _hci(path):
    """/org/bluez/hci0 or a device below it -> hci0"""
    return path.split("/")[3]


def _remote(e):
    """A GLib.Error from a D-Bus call -> (the D-Bus error name or None, its message)."""
    msg = e.message or ""
    if msg.startswith("GDBus.Error:"):
        name, _, text = msg[len("GDBus.Error:"):].partition(": ")
        return name, text
    return None, msg


def _error(what, e):
    """A failed BlueZ call -> BleError saying what to do."""
    name, text = _remote(e)
    if name in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"):
        return BleError("bluetoothd is not running: start it (sudo systemctl start bluetooth) and try again")
    if name == "org.freedesktop.DBus.Error.AccessDenied":
        return BleError(f"BlueZ does not allow this user to {what}: start iPhone Control from the desktop session, "
                        "or add the user to the 'bluetooth' group (sudo usermod -aG bluetooth $USER, then log in "
                        "again)")
    return BleError(f"could not {what}: {text}" + (f" ({name})" if name else ""))


class _Attr:
    """One exported GATT object: a service, characteristic or descriptor."""
    __slots__ = ("path", "iface", "props", "value", "report", "notifying")

    def __init__(self, path, iface, props, value=b"", report=None):
        self.path, self.iface, self.props, self.value, self.report = path, iface, props, bytes(value), report
        self.notifying = False


def _layout():
    """[(service, [(characteristic, flags, value, input report id or None)])]: hid_ble.py's, plus report 4."""
    enc = ["read", "encrypt-read"]
    return [
        ("1812", [("2a4a", enc, bytes([0x11, 0x01, 0x00, 0x02]), None),          # HID information: 1.11
                  ("2a4b", enc, hid.REPORT_MAP, None),                           # report map
                  ("2a4c", ["write-without-response"], b"\x00", None),           # control point
                  ("2a4e", ["read", "write-without-response"], b"\x01", None)]   # protocol mode = report
         + [("2a4d", enc + ["notify"], bytes(hid.SIZES[r]), r) for r in INPUT]),
        ("180f", [("2a19", ["read", "notify"], b"\x64", None)]),                 # battery 100 %
        ("180a", [("2a29", ["read"], b"iPhone Control", None),                  # manufacturer
                  ("2a50", ["read"], bytes([0x02, 0x6B, 0x1D, 0x46, 0x02, 0x37, 0x05]), None)]),   # PnP ID
    ]


def _gatt():
    """{path: _Attr} of the whole application. Each input report carries a Report Reference (id, 1 = input)."""
    out = {}
    for i, (suuid, chrcs) in enumerate(_layout()):
        spath = f"{OBJ}/service{i}"
        cpaths = [f"{spath}/char{j}" for j in range(len(chrcs))]
        out[spath] = _Attr(spath, SERVICE, {"UUID": GLib.Variant("s", suuid), "Primary": GLib.Variant("b", True),
                                            "Characteristics": GLib.Variant("ao", cpaths)})
        for cpath, (cuuid, flags, value, rid) in zip(cpaths, chrcs):
            dpaths = [cpath + "/desc0"] if rid else []
            out[cpath] = _Attr(cpath, CHRC, {"UUID": GLib.Variant("s", cuuid), "Service": GLib.Variant("o", spath),
                                             "Flags": GLib.Variant("as", flags),
                                             "Descriptors": GLib.Variant("ao", dpaths)}, value, rid)
            for dpath in dpaths:
                out[dpath] = _Attr(dpath, DESC, {"UUID": GLib.Variant("s", "2908"),
                                                 "Characteristic": GLib.Variant("o", cpath),
                                                 "Flags": GLib.Variant("as", ["read", "encrypt-read"])},
                                   bytes([rid, 1]))
    return out


class BleHid:
    """name: the Bluetooth name (default IPHONE_HID_NAME, else "<hostname> Mouse"); adapter: "hciN" or its
    object path (default BT_INDEX, else the first switched-on adapter that can host GATT services and advertise
    over LE).
    on_state(state): "advertising" (tell the user self.hint), "connected", "disconnected".
    confirm(passkey, alias, reply): a pairing request; the device pairs only if reply(True) is called.
    bus: a D-Bus address instead of the system bus (the tests' private bus).
    """

    def __init__(self, name=None, adapter=None, on_state=None, confirm=None, log=print, bus=None):
        self.name = name or os.environ.get("IPHONE_HID_NAME") or f"{_hostname()} Mouse"
        self.adapter = None               # its object path once started
        self.on_state = on_state or (lambda state: None)
        self.confirm, self.log = confirm, log
        self._want, self._bus = adapter, bus
        self._conn = self._owner = None   # our own connection, and the unique name of the bluetoothd on it
        self._sched = hid.Scheduler()
        self._attrs = _gatt()
        self._reports = {a.report: a for a in self._attrs.values() if a.report}
        self._objects, self._signals = [], []
        self._undo = []                   # (object path, interface, method, arguments) that put things back
        self._timer = 0
        self._pending = None              # the RequestConfirmation waiting for the user's reply
        self._approved = {}               # device -> until when it may pair: the user confirmed its code
        self._bonded = set()              # devices that paired that way
        self._removing = set()            # devices that paired without it, being removed
        self._ours = set()                # devices known to be the phone (read the HID service, paired here)...
        self._linked = set()              # ...and the ones of them connected now
        self._started = self._was = self._pairable = False

    @property
    def hint(self):
        return PAIR_HINT.format(name=self.name)

    @property
    def connected(self):
        """The phone subscribed to the input reports and is connected. bluetoothd keeps a bonded device's
        subscriptions over a disconnect (it sends no StopNotify then), so the phone's own Connected decides."""
        return any(self._reports[r].notifying for r in INPUT) and bool(self._linked)

    # ---- set-up ------------------------------------------------------------
    def start(self):
        """Register with bluetoothd and advertise; raises BleError with what to do."""
        if self._conn:
            return
        try:
            self._connect()
            self._register()
        except BleError:
            self._teardown()
            raise
        except GLib.Error as e:
            self._teardown()
            raise BleError(f"could not set up Bluetooth: {e.message}") from None
        self._started = True
        self.log(f"Bluetooth: advertising as '{self.name}' on {_hci(self.adapter)}")
        self.on_state("advertising")
        if self._was:
            self.on_state("connected")

    def _call(self, path, iface, method, args=None, dest=None):
        """To bluetoothd's unique name (dest: another one): a bluetoothd started since, by hand or by D-Bus
        activation, never gets what was meant for the one that holds our registrations."""
        r = self._conn.call_sync(dest or self._owner, path, iface, method, args, None,
                                 Gio.DBusCallFlags.NO_AUTO_START, CALL_MS, None)
        return r.unpack() if r is not None else ()

    def _async(self, path, iface, method, args, done, *data):
        """_call without waiting: done(conn, result, *data) runs on the main loop."""
        self._conn.call(self._owner, path, iface, method, args, None, Gio.DBusCallFlags.NO_AUTO_START, CALL_MS,
                        None, done, *data)

    def _sync(self, what, *call):
        try:
            return self._call(*call)
        except GLib.Error as e:
            raise _error(what, e) from None

    def _connect(self):
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        try:   # our own connection: closing it in stop() also drops anything bluetoothd still holds of ours
            address = self._bus or Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
            self._conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        except GLib.Error as e:
            raise BleError(f"cannot reach the system D-Bus: {e.message}") from None
        self._owner = self._sync("reach bluetoothd", "/org/freedesktop/DBus", BUS, "GetNameOwner",
                                 GLib.Variant("(s)", (BLUEZ,)), BUS)[0]
        objects = self._sync("list the Bluetooth adapters", "/", OM, "GetManagedObjects")[0]
        self.adapter = self._pick(objects)
        props = objects[self.adapter][ADAPTER]
        if not props.get("Powered"):
            how = "rfkill unblock bluetooth" if "blocked" in props.get("PowerState", "") else "bluetoothctl power on"
            raise BleError(f"Bluetooth is off on {_hci(self.adapter)}: switch it on ({how}) and try again")
        self._pairable = bool(props.get("Pairable"))

    def _pick(self, objects):
        """adapter=, else BT_INDEX, else the first switched-on adapter that can host GATT services and advertise
        over LE (BlueZ lists both managers on a switched-off one too); a switched-off one only if none is on."""
        def le(path):
            return all(i in objects.get(path, {}) for i in (ADAPTER, GATT_MANAGER, ADV_MANAGER))
        want, src = self._want, ""
        if not want and os.environ.get("BT_INDEX"):
            want, src = "hci" + os.environ["BT_INDEX"], f" (BT_INDEX={os.environ['BT_INDEX']})"
        if want:
            path = want if want.startswith("/") else "/org/bluez/" + want
            if ADAPTER not in objects.get(path, {}):
                raise BleError(f"no Bluetooth adapter {path.rsplit('/', 1)[-1]}{src}")
            if not le(path):
                raise BleError(f"{path.rsplit('/', 1)[-1]} cannot do Bluetooth LE (no GATT server or LE advertising)"
                               f"{src}")
            return path
        found = [path for path in sorted(objects) if le(path)]
        if not found:
            raise BleError("no Bluetooth LE adapter found (is Bluetooth switched on? try: rfkill unblock bluetooth)")
        return ([path for path in found if objects[path][ADAPTER].get("Powered")] or found)[0]

    def _register(self):
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        info = {i.name: i for i in node.interfaces}
        for path, iface in [(OBJ, OM), (ADV_PATH, ADV), (AGENT_PATH, AGENT)] + \
                [(a.path, a.iface) for a in self._attrs.values()]:
            self._objects.append(self._conn.register_object(path, info[iface], self._on_call, self._on_get, None))
        none = Gio.DBusSignalFlags.NONE
        self._signals = [
            self._conn.signal_subscribe(BLUEZ, PROPS, "PropertiesChanged", None, DEVICE, none, self._on_device),
            self._conn.signal_subscribe(BLUEZ, OM, "InterfacesRemoved", "/", None, none, self._on_removed),
            self._conn.signal_subscribe(BUS, BUS, "NameOwnerChanged", "/org/freedesktop/DBus", BLUEZ, none,
                                        self._on_owner)]
        agent = GLib.Variant("(o)", (AGENT_PATH,))
        # numeric comparison: both screens show the code, the user confirms it here
        self._sync("register the Bluetooth pairing agent", "/org/bluez", AGENT_MANAGER, "RegisterAgent",
                   GLib.Variant("(os)", (AGENT_PATH, "DisplayYesNo")))
        self._undo.append(("/org/bluez", AGENT_MANAGER, "UnregisterAgent", agent))
        # bluetoothd asks only the default agent about a pairing the phone starts, and sets the adapters' IO
        # capability (DisplayYesNo) from it; so it asks this one about every device too (see _agent)
        self._sync("make the Bluetooth pairing agent the default", "/org/bluez", AGENT_MANAGER,
                   "RequestDefaultAgent", agent)
        if not self._pairable:
            self._sync("make the Bluetooth adapter pairable", *self._pairable_call(True))
            self._undo.append(self._pairable_call(False))
        self._register_and_wait()

    def _pairable_call(self, on):
        return self.adapter, PROPS, "Set", GLib.Variant("(ssv)", (ADAPTER, "Pairable", GLib.Variant("b", on)))

    def _register_and_wait(self):
        """bluetoothd reads the application and the advertisement from this process before it answers these two,
        so they are asynchronous, and this thread's main context runs until both replied."""
        calls = [("register the Bluetooth HID service", GATT_MANAGER, "RegisterApplication",
                  "UnregisterApplication", OBJ),
                 ("advertise over Bluetooth LE", ADV_MANAGER, "RegisterAdvertisement", "UnregisterAdvertisement",
                  ADV_PATH)]
        done = {}

        def finished(what, iface, undo, path):
            def reply(conn, result):
                try:
                    conn.call_finish(result)
                except GLib.Error as e:
                    done[what] = e
                    return
                done[what] = None
                self._undo.append((self.adapter, iface, undo, GLib.Variant("(o)", (path,))))
            return reply

        for what, iface, method, undo, path in calls:
            self._async(self.adapter, iface, method, GLib.Variant("(oa{sv})", (path, {})),
                        finished(what, iface, undo, path))
        ctx = GLib.MainContext.ref_thread_default()
        while len(done) < len(calls):     # each call ends by CALL_MS at the latest
            ctx.iteration(True)
        for what, *_ in calls:
            if done[what] is not None:
                raise _error(what, done[what])

    def stop(self):
        """Unregister everything and put Pairable back, on the bluetoothd and adapter they were made with only.
        Lets go of anything still held down on the phone."""
        if self._conn and self.connected:
            for rid, data in self._sched.release_all():
                self._send(rid, data)
        self._teardown()

    def _teardown(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._sched.reset()
        if self._pending:
            self._pending.return_dbus_error(REJECTED, "stopped")
            self._pending = None
        for undo in reversed(self._undo if self._owner else []):    # else bluetoothd dropped them as it went
            try:
                self._call(*undo)
            except GLib.Error:
                pass                       # it went away just now
        if self._conn:
            for sid in self._signals:
                self._conn.signal_unsubscribe(sid)
            for oid in self._objects:
                self._conn.unregister_object(oid)
            try:
                self._conn.flush_sync(None)
                self._conn.close_sync(None)
            except GLib.Error:
                pass
        self._undo, self._signals, self._objects = [], [], []
        self._conn = self._owner = None
        for a in self._attrs.values():
            a.notifying = False
        self._approved.clear()
        self._bonded.clear()
        self._removing.clear()
        self._ours.clear()
        self._linked.clear()
        self._started = self._was = False

    # ---- calls from bluetoothd --------------------------------------------------
    def _on_get(self, conn, sender, path, iface, name):
        if iface == ADV:   # discoverable by the advertisement alone: the adapter's Discoverable is left alone
            return {"Type": GLib.Variant("s", "peripheral"), "ServiceUUIDs": GLib.Variant("as", ["1812"]),
                    "Appearance": GLib.Variant("q", APPEARANCE), "LocalName": GLib.Variant("s", self.name),
                    "Discoverable": GLib.Variant("b", True)}[name]
        return self._attrs[path].props[name]

    def _on_call(self, conn, sender, path, iface, method, params, inv):
        if sender != self._owner:      # only bluetoothd: a report's value is the last key typed
            return inv.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "only bluetoothd may call this")
        handler = {OM: self._managed, ADV: self._advertisement, AGENT: self._agent}.get(iface, self._attr)
        try:
            handler(inv, path, method, params.unpack())
        except Exception as e:         # never leave bluetoothd waiting for a reply
            self.log(f"Bluetooth: {method} failed: {e}")
            inv.return_dbus_error("org.bluez.Error.Failed", str(e))

    def _managed(self, inv, path, method, args):
        tree = {a.path: {a.iface: a.props} for a in self._attrs.values()}
        inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", (tree,)))

    def _advertisement(self, inv, path, method, args):
        self.log("Bluetooth: bluetoothd dropped the advertisement")
        inv.return_value(None)

    def _attr(self, inv, path, method, args):
        a = self._attrs[path]
        if method == "ReadValue":
            if "encrypt-read" in a.props["Flags"].unpack():   # a paired device's read (battery, device
                self._seen(args[0].get("device"))             # information, protocol mode: anyone's)
            return inv.return_value(GLib.Variant("(ay)", (a.value[int(args[0].get("offset", 0)):],)))
        if method == "WriteValue":
            a.value = bytes(args[0])
        elif method in ("StartNotify", "StopNotify"):
            a.notifying = method == "StartNotify"
            if a.notifying and a.report and not self._linked:
                self._lookup()
            self._update()
        inv.return_value(None)

    def _seen(self, device):
        """device is the phone: it read the HID service, or paired with a confirmed code."""
        if device and device.startswith(self.adapter + "/") and device not in self._removing:
            self._ours.add(device)
            self._linked.add(device)
            self._update()

    def _lookup(self):
        """A report was subscribed while no known phone is connected (one bonded before that kept what it read).
        StartNotify does not say who, and anyone in range may subscribe (BlueZ asks no encryption for that), so
        the phone is whichever paired device is connected on the adapter."""
        def found(conn, result):
            if conn is not self._conn:
                return                     # stopped meanwhile
            try:
                objects = conn.call_finish(result).unpack()[0]
            except GLib.Error as e:
                return self.log(f"Bluetooth: could not list the devices: {_remote(e)[1]}")
            for path, ifs in sorted(objects.items()):
                if ifs.get(DEVICE, {}).get("Connected") and ifs[DEVICE].get("Paired"):
                    self._seen(path)
        self._async("/", OM, "GetManagedObjects", None, found)

    def _agent(self, inv, path, method, args):
        """The default agent is asked about every device on every adapter. A paired device may use its services
        (as if trusted: the desktop's agent would ask, nothing here can); pairing needs a code the user confirmed,
        and only devices on self.adapter get to ask for one (close iPhone Control to pair anything else)."""
        if method in ("Release", "Cancel"):
            if method == "Cancel" and self._pending:
                self._pending.return_dbus_error(REJECTED, "cancelled")
                self._pending = None
                self.log("Bluetooth: pairing cancelled on the phone")
            return inv.return_value(None)
        device = args[0]
        if method == "AuthorizeService":
            return self._authorize(inv, device, args[1])
        if not device.startswith(self.adapter + "/"):
            self.log(f"Bluetooth: pairing with {_label(device)} on {_hci(device)} refused: while it runs, iPhone "
                     f"Control answers every pairing request and pairs on {_hci(self.adapter)} only (close it to "
                     "pair that device)")
            return inv.return_dbus_error(REJECTED, f"iPhone Control pairs on {_hci(self.adapter)} only")
        if method == "RequestConfirmation":
            return self._ask(inv, *args)
        if method in ("DisplayPasskey", "DisplayPinCode"):
            self.log(f"Bluetooth: {_label(device)} wants a code typed on it: not supported, so it will not stay "
                     "paired (pair again from the iPhone and compare the codes)")
            return inv.return_value(None)
        self.log(f"Bluetooth: {method} from {_label(device)} refused: only pairing with a confirmed code is "
                 "allowed")
        inv.return_dbus_error(REJECTED, "only numeric comparison is supported")

    def _authorize(self, inv, device, uuid):
        """A paired device that is not trusted connecting to a service (headphones...): allowed, as trusted."""
        def paired(conn, result):
            if conn is not self._conn:
                return                     # stopped meanwhile: bluetoothd saw this agent go
            try:
                ok = bool(conn.call_finish(result).unpack()[0]) and device not in self._removing
            except GLib.Error:
                ok = False
            if ok:
                return inv.return_value(None)
            self.log(f"Bluetooth: {_label(device)} may not use service {uuid}: it is not paired")
            inv.return_dbus_error(REJECTED, "not paired")
        self._async(device, PROPS, "Get", GLib.Variant("(ss)", (DEVICE, "Paired")), paired)

    def _ask(self, inv, device, passkey):
        if self._pending or not self.confirm:
            self.log(f"Bluetooth: pairing request from {_label(device)} refused "
                     f"({'another one is waiting' if self._pending else 'nobody to ask'})")
            return inv.return_dbus_error(REJECTED, "rejected")
        self._pending = inv

        def reply(ok):
            if self._pending is not inv:
                return                 # answered already, cancelled by the phone, or stopped
            self._pending = None
            if not ok:
                self.log("Bluetooth: pairing rejected")
                return inv.return_dbus_error(REJECTED, "rejected by the user")
            self._approved[device] = time.monotonic() + PAIR_S   # this pairing only: see _on_device
            inv.return_value(None)

        def ask(conn, result):
            try:
                alias = str(conn.call_finish(result).unpack()[0])
            except GLib.Error:
                alias = _label(device)
            if self._pending is inv:
                try:
                    self.confirm(passkey, alias, reply)
                except Exception as e:
                    self.log(f"Bluetooth: could not ask about pairing: {e}")
                    reply(False)

        self._async(device, PROPS, "Get", GLib.Variant("(ss)", (DEVICE, "Alias")), ask)

    def _paired(self, device):
        """The pairing the user confirmed went through: the device is the phone, and trusted, so that it
        reconnects without asking again."""
        self._bonded.add(device)
        self._seen(device)

        def trusted(conn, result):
            try:
                conn.call_finish(result)
            except GLib.Error as e:
                self.log(f"Bluetooth: could not trust {_label(device)}: {_remote(e)[1]}")

        self._async(device, PROPS, "Set", GLib.Variant("(ssv)", (DEVICE, "Trusted", GLib.Variant("b", True))),
                    trusted)

    # ---- signals from bluetoothd ------------------------------------------------
    def _on_device(self, conn, sender, path, iface, signal, params):
        """A confirmed code lets that one pairing through: one that fails (the link drops first, or SMP gives up)
        uses it up, and so does the bond it allowed; a device that pairs again later needs a new one."""
        if sender != self._owner or not path.startswith(self.adapter + "/"):
            return
        changed = params.unpack()[1]
        if changed.get("Paired") is False:
            self._bonded.discard(path)
        if (changed.get("Paired") or changed.get("Bonded")) and path not in self._bonded | self._removing:
            if self._approved.pop(path, 0) > time.monotonic():
                self._paired(path)
            else:
                self.log(f"Bluetooth: {_label(path)} paired without a confirmed code - removed")
                self._removing.add(path)
                self._async(self.adapter, ADAPTER, "RemoveDevice", GLib.Variant("(o)", (path,)), self._removed,
                            path)
        if "Connected" in changed:
            if not changed["Connected"]:
                self._approved.pop(path, None)
            if path in self._ours:
                (self._linked.add if changed["Connected"] else self._linked.discard)(path)
                self._update()

    def _removed(self, conn, result, path):
        self._removing.discard(path)
        try:
            conn.call_finish(result)
        except GLib.Error as e:
            self.log(f"Bluetooth: could not remove {_label(path)}: {_remote(e)[1]}")

    def _on_owner(self, conn, sender, path, iface, signal, params):
        if self._owner and params.unpack()[2] != self._owner:
            self.log("Bluetooth: bluetoothd stopped; start iPhone Control again to use Bluetooth")
            self._owner = self._pending = None    # with everything it held of ours: nothing to answer or undo
            for a in self._attrs.values():
                a.notifying = False
            self._update()

    def _on_removed(self, conn, sender, path, iface, signal, params):
        gone, ifaces = params.unpack()
        if sender != self._owner or gone != self.adapter or ADAPTER not in ifaces:
            return
        self.log(f"Bluetooth: {_hci(gone)} went away; start iPhone Control again to use Bluetooth")
        self._undo = [u for u in self._undo if u[0] != gone]     # it took them with it: not put back elsewhere
        for a in self._attrs.values():
            a.notifying = False
        self._update()

    def _update(self):
        now = self.connected
        if now == self._was:
            return
        self._was = now
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._sched.reset()            # a new link starts from nothing held and nothing queued
        self.log("Bluetooth: the iPhone is connected" if now else "Bluetooth: the iPhone disconnected")
        if self._started:
            self.on_state("connected" if now else "disconnected")

    # ---- input -------------------------------------------------------------
    def _send(self, rid, data):
        a = self._reports[rid]
        a.value = data
        if not (a.notifying and self._conn and self._owner):
            return
        try:   # to bluetoothd only: a broadcast signal reaches whoever on the system bus asks, and these carry keys
            self._conn.emit_signal(self._owner, a.path, PROPS, "PropertiesChanged",
                                   GLib.Variant("(sa{sv}as)", (CHRC, {"Value": GLib.Variant("ay", data)}, [])))
        except GLib.Error as e:
            self.log(f"Bluetooth: report not sent: {e.message}")

    def _pump(self):
        """Send the report that is due, then sleep until the next one may go."""
        self._timer = 0
        now = time.monotonic() * 1000
        step = self._sched.pop(now)
        if step:
            self._send(*step)
        wait = self._sched.delay(now)
        if wait is not None:
            self._timer = GLib.timeout_add(max(1, math.ceil(wait)), self._pump)
        return False

    def _do(self, action, *args):
        if not self.connected:
            return None
        result = action(*args)
        if not self._timer:
            self._pump()
        return result

    def tap(self, fx, fy, hold_ms=hid.HOLD_MS):
        """Move, settle hid.SETTLE_MS, press, hold, release."""
        self._do(self._sched.tap, fx, fy, hold_ms)

    def press(self, fx, fy):
        """Move, settle, press; the button stays down."""
        self._do(self._sched.press, fx, fy)

    def drag_to(self, fx, fy):
        """One move with the button down; moves still queued are replaced by the latest."""
        self._do(self._sched.drag_to, fx, fy)

    def release(self, fx, fy):
        self._do(self._sched.release, fx, fy)

    def move(self, fx, fy):
        self._do(self._sched.move, fx, fy)

    def swipe(self, points, release_ms=0):
        """A timed drag through [(fx, fy, ms since the start), ...] (scrolling: the wheel does not scroll)."""
        self._do(self._sched.swipe, points, release_ms)

    def key(self, usage, mods=0):
        self._do(self._sched.key, usage, mods)

    def text(self, s):
        """Types s on the US layout; returns how many characters it could not type. Raises BleError while the
        phone is not connected: then nothing is typed, and not for want of a key."""
        if not self.connected:
            raise BleError("the iPhone is not connected")
        return self._do(self._sched.text, s)

    def consumer(self, usage, hold_ms=0):
        self._do(self._sched.consumer, usage, hold_ms)

    def fn(self, usage):
        self._do(self._sched.fn, usage)

    def home(self):
        self._do(self._sched.home)

    def app_switcher(self):
        self._do(self._sched.app_switcher)

    def control_centre(self):
        self._do(self._sched.control_centre)

    def notifications(self):
        self._do(self._sched.notifications)

    def spotlight(self):
        self._do(self._sched.spotlight)
