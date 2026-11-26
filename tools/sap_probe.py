#!/usr/bin/env python3
"""Probe the Gear 360's Samsung Accessory Protocol (SAP) link from Linux - step 1: get the camera to connect back.

What Samsung's phone app does (session 1 HCI capture):
  1. the phone registers an RFCOMM service with UUID a49eb41e-...-aa80a90cdf4a ("SAP")
  2. the phone opens RFCOMM channel 2 on the camera (UUID ...bb80a90cdf00) and keeps it open - no data on it
  3. the camera looks the phone up and connects to the phone's aa80 service; all SAP traffic flows there

This script does the same, then logs every frame the camera sends (u16 big-endian length + body) without replying.
Run with the system python3 (needs dbus-python + PyGObject, preinstalled on Pop!_OS/Ubuntu):
    python3 tools/sap_probe.py [--camera XX:XX:XX:XX:XX:XX] [--seconds 30]
The camera must already be paired with this PC (tools/pair.py) and switched on, and no phone should hold it.
"""
import argparse
import os
import socket
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

HOST_SAP_UUID = "a49eb41e-cb06-495c-9f4f-aa80a90cdf4a"
CAMERA_TRIGGER_CHANNEL = 2          # camera's ...bb80a90cdf00 service (look up with: sdptool browse <camera>)
PROFILE_PATH = "/g360/sap"
LOCAL_CHANNEL = 6                   # what Samsung phones use; any free channel works if advertised in SDP


def hexdump(b: bytes, width: int = 32) -> str:
    return "\n".join(f"      {i:04x}  {b[i:i + width].hex(' ')}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in b[i:i + width])}"
                     for i in range(0, len(b), width))


def read_frames(sock: socket.socket, log, stop: threading.Event):
    buf = b""
    sock.settimeout(1.0)
    while not stop.is_set():
        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError as e:
            log(f"camera link closed: {e}")
            return
        if not data:
            log("camera closed the SAP connection")
            return
        buf += data
        while len(buf) >= 2:
            n = int.from_bytes(buf[:2], "big")
            if len(buf) < 2 + n:
                break
            body, buf = buf[2:2 + n], buf[2 + n:]
            log(f"<- camera frame type 0x{body[0]:02x}, {n} bytes\n{hexdump(body)}")


class SapProfile(dbus.service.Object):
    def __init__(self, bus, path, on_conn):
        super().__init__(bus, path)
        self.on_conn = on_conn

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, props):
        fd = fd.take()
        self.on_conn(str(device), socket.socket(fileno=fd))

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        print("BlueZ: disconnection requested by", device)

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", default=None, help="camera address (default: first paired 'Gear 360' device)")
    ap.add_argument("--seconds", type=int, default=30)
    a = ap.parse_args()
    t0 = time.monotonic()
    log = lambda m: print(f"[{time.monotonic() - t0:6.2f}s] {m}", flush=True)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    om = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
    objs = om.GetManagedObjects()
    cam = a.camera
    if not cam:
        cam = next((str(i["org.bluez.Device1"]["Address"]) for i in objs.values()
                    if "org.bluez.Device1" in i and str(i["org.bluez.Device1"].get("Name", "")).startswith("Gear 360")
                    and i["org.bluez.Device1"].get("Paired")), None)
    if not cam:
        print("no paired Gear 360 found; run tools/pair.py first")
        return 1
    adapter = next(str(i["org.bluez.Adapter1"]["Address"]) for i in objs.values() if "org.bluez.Adapter1" in i)
    log(f"host {adapter}  camera {cam}")

    stop = threading.Event()
    loop = GLib.MainLoop()

    def on_conn(device, sock):
        log(f"camera connected back to our SAP service ({device}) - reading frames")
        threading.Thread(target=read_frames, args=(sock, log, stop), daemon=True).start()

    SapProfile(bus, PROFILE_PATH, on_conn)
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
    mgr.RegisterProfile(PROFILE_PATH, HOST_SAP_UUID, {
        "Name": "SAP", "Role": "server", "Channel": dbus.UInt16(LOCAL_CHANNEL),
        "RequireAuthentication": dbus.Boolean(False), "RequireAuthorization": dbus.Boolean(False),
        "AutoConnect": dbus.Boolean(False),
    })
    log(f"registered SAP service {HOST_SAP_UUID} on RFCOMM channel {LOCAL_CHANNEL}")

    trigger = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)

    def connect_trigger():
        try:
            trigger.connect((cam, CAMERA_TRIGGER_CHANNEL))
            log(f"opened camera RFCOMM channel {CAMERA_TRIGGER_CHANNEL} (trigger link) - waiting for the camera")
            trigger.settimeout(1.0)
            while not stop.is_set():
                try:
                    d = trigger.recv(4096)
                    if not d:
                        log("trigger link closed by camera")
                        return
                    log(f"<- data on trigger link ({len(d)} bytes)\n{hexdump(d)}")
                except socket.timeout:
                    pass
        except OSError as e:
            log(f"could not open camera channel {CAMERA_TRIGGER_CHANNEL}: {e}")

    threading.Thread(target=connect_trigger, daemon=True).start()
    GLib.timeout_add_seconds(a.seconds, loop.quit)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    stop.set()
    trigger.close()
    try:
        mgr.UnregisterProfile(PROFILE_PATH)
    except dbus.exceptions.DBusException:
        pass
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
