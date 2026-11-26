#!/usr/bin/env python3
"""Pair this PC with a Gear 360 (SM-C200) over Bluetooth using BlueZ's D-Bus API.

Put the camera in pairing mode first (power on -> hold Menu until "Gear 360 Manager" -> press Record; it shows a
6-digit code). This script scans for a device named "Gear 360 (xxxx)", pairs with it while auto-confirming the
numeric-comparison code (it prints the code so you can check it matches the camera's screen), and marks it trusted.

Uses the system python3 with dbus-python and PyGObject (both preinstalled on Pop!_OS / Ubuntu).
Usage: python3 tools/pair.py [--address XX:XX:XX:XX:XX:XX] [--timeout 60]
"""
import argparse
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUS, AGENT_PATH = "org.bluez", "/g360/agent"
ADAPTER_IF, DEVICE_IF, AGENT_MGR_IF = "org.bluez.Adapter1", "org.bluez.Device1", "org.bluez.AgentManager1"
PROPS_IF, OM_IF = "org.freedesktop.DBus.Properties", "org.freedesktop.DBus.ObjectManager"


class Agent(dbus.service.Object):
    """Auto-accepting pairing agent; prints what it accepts."""

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"pairing code {passkey:06d} - check it matches the camera's screen (auto-confirming)")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        print("authorizing pairing request")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print(f"authorizing service {uuid}")

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        print(f"passkey {passkey:06d}")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        raise dbus.exceptions.DBusException("org.bluez.Error.Rejected", "no keyboard")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        print("pairing cancelled by the other side")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", help="camera Bluetooth address, if already known")
    ap.add_argument("--timeout", type=int, default=60)
    a = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    om = dbus.Interface(bus.get_object(BUS, "/"), OM_IF)
    adapter_path = next(p for p, ifs in om.GetManagedObjects().items() if ADAPTER_IF in ifs)
    adapter = dbus.Interface(bus.get_object(BUS, adapter_path), ADAPTER_IF)
    aprops = dbus.Interface(bus.get_object(BUS, adapter_path), PROPS_IF)
    aprops.Set(ADAPTER_IF, "Powered", dbus.Boolean(True))
    aprops.Set(ADAPTER_IF, "Pairable", dbus.Boolean(True))
    print(f"adapter {aprops.Get(ADAPTER_IF, 'Address')} ready")

    Agent(bus, AGENT_PATH)
    mgr = dbus.Interface(bus.get_object(BUS, "/org/bluez"), AGENT_MGR_IF)
    mgr.RegisterAgent(AGENT_PATH, "DisplayYesNo")
    mgr.RequestDefaultAgent(AGENT_PATH)

    loop = GLib.MainLoop()
    state = {"path": None, "done": False, "rc": 1}

    def find():
        for path, ifs in om.GetManagedObjects().items():
            d = ifs.get(DEVICE_IF)
            if not d:
                continue
            name, addr = str(d.get("Name", "")), str(d.get("Address", ""))
            if (a.address and addr.upper() == a.address.upper()) or (not a.address and name.startswith("Gear 360")):
                return path, name, addr, bool(d.get("Paired", False))
        return None

    def pair_found():
        hit = find()
        if not hit or state["path"]:
            return True
        path, name, addr, paired = hit
        state["path"] = path
        print(f"found {name} [{addr}]")
        try:
            adapter.StopDiscovery()
        except dbus.exceptions.DBusException:
            pass
        dev = dbus.Interface(bus.get_object(BUS, path), DEVICE_IF)
        dprops = dbus.Interface(bus.get_object(BUS, path), PROPS_IF)

        def ok():
            dprops.Set(DEVICE_IF, "Trusted", dbus.Boolean(True))
            print(f"paired and trusted {name} [{addr}]")
            state["rc"] = 0
            loop.quit()

        def err(e):
            print(f"pairing failed: {e.get_dbus_name()}: {e.get_dbus_message()}")
            loop.quit()

        if paired:
            print("already paired")
            ok()
        else:
            print("pairing... (the camera shows a code)")
            dev.Pair(reply_handler=ok, error_handler=err, timeout=60)
        return False

    print("scanning for 'Gear 360 (xxxx)' - put the camera in pairing mode")
    adapter.SetDiscoveryFilter({"Transport": "bredr"})
    adapter.StartDiscovery()
    GLib.timeout_add(1000, pair_found)
    GLib.timeout_add_seconds(a.timeout, lambda: (print("timed out: camera not found"), loop.quit()))
    loop.run()
    try:
        mgr.UnregisterAgent(AGENT_PATH)
    except dbus.exceptions.DBusException:
        pass
    return state["rc"]


if __name__ == "__main__":
    sys.exit(main())
