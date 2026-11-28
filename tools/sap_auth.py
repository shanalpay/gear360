#!/usr/bin/env python3
"""Probe step 2: answer the camera's Peer Description and run the WSM authentication from Linux.

Flow (from the session-1 capture), on the SAP connection the camera opens back to us:
  camera -> 0x05 Peer Description            host -> 0x06 Peer Description response
  camera -> 0x10 client challenge (WSM v1)   host -> 0x11 server challenge (computed by Samsung's libwsm2, emulated)
  camera -> 0x12 client response             host verifies it -> both sides share a session key
Frames: u16 big-endian length + body; auth bodies are [msgType][authType=0][WSM packet].

Run with the system python3 (dbus-python/PyGObject) from the project root; unicorn/pyelftools come from .venv:
    python3 tools/sap_auth.py [--seconds 40]
Camera: paired with this PC, switched on, not held by a phone.
"""
import argparse
import glob
import os
import pathlib
import socket
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sp in glob.glob(str(ROOT / ".venv/lib/python3*/site-packages")):
    sys.path.append(sp)

import dbus  # noqa: E402
import dbus.mainloop.glib  # noqa: E402
import dbus.service  # noqa: E402
from gi.repository import GLib  # noqa: E402

from g360 import bt, wsm  # noqa: E402

PROFILE_PATH, LOCAL_CHANNEL = "/g360/sap", 6
# The phone's Peer Description response from the capture, verbatim for the first attempt (strings:
# "SM-A102U1;samsung;Galaxy A10e;SWatch;"). Header fields still to be decoded; see NOTES.md.
PD_RESPONSE = bytes.fromhex(
    "06020102010002000ffaaaf0aa03fe271002000a01"
    "534d2d4131303255313b73616d73756e673b47616c61787920413130653b5357617463683b")


def frame(body: bytes) -> bytes:
    return len(body).to_bytes(2, "big") + body


def hexdump(b: bytes, width: int = 32) -> str:
    return "\n".join(f"      {i:04x}  {b[i:i + width].hex(' ')}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in b[i:i + width])}"
                     for i in range(0, len(b), width))


class Session:
    def __init__(self, sock, cam: str, host: str, log):
        self.sock, self.cam, self.host, self.log = sock, cam, host, log
        self.client = None

    def send(self, body: bytes, what: str):
        self.sock.sendall(frame(body))
        self.log(f"-> {what} (type 0x{body[0]:02x}, {len(body)} bytes)")

    def handle(self, body: bytes):
        t = body[0]
        if t == bt.PD_REQUEST:
            self.log("<- Peer Description: " + body[20:].decode(errors="replace"))
            self.send(PD_RESPONSE, "Peer Description response")
        elif t == bt.AUTH_CLIENT_CHALLENGE:
            challenge = body[2:]
            self.log(f"<- client challenge, WSM {challenge[:3].hex(' ')} ({len(challenge)} bytes)")
            t0 = time.monotonic()
            self.client = wsm.Client(self.cam, self.host, version=bt.WSM_VERSION)   # Client(serverID=camera, clientID=us)
            sc = self.client.server_challenge(challenge)
            self.log(f"   computed server challenge with emulated libwsm2 in {time.monotonic() - t0:.2f}s")
            self.send(bytes([bt.AUTH_SERVER_CHALLENGE, 0]) + sc, "server challenge")
        elif t == bt.AUTH_CLIENT_RESPONSE:
            resp = body[2:]
            self.log(f"<- client response, WSM {resp[:3].hex(' ')} ({len(resp)} bytes)")
            try:
                self.client.check_response(resp)
                key = self.client.esap_key()
                self.log(f"*** AUTHENTICATED: camera's response verified; session key {key[:6].hex()}… ({len(key)} bytes) ***")
            except wsm.WSMError as e:
                self.log(f"!!! camera's response failed verification: {e}")
        else:
            self.log(f"<- frame type 0x{t:02x}, {len(body)} bytes (not handled yet)\n{hexdump(body)}")

    def run(self, stop: threading.Event):
        buf = b""
        self.sock.settimeout(1.0)
        while not stop.is_set():
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                self.log(f"SAP link closed: {e}")
                return
            if not data:
                self.log("camera closed the SAP connection")
                return
            buf += data
            while len(buf) >= 2 and len(buf) >= 2 + int.from_bytes(buf[:2], "big"):
                n = int.from_bytes(buf[:2], "big")
                body, buf = buf[2:2 + n], buf[2 + n:]
                try:
                    self.handle(body)
                except Exception as e:  # keep the link up and report
                    self.log(f"!!! error handling frame 0x{body[0]:02x}: {e!r}")
            if buf and len(buf) < 2:
                pass


class SapProfile(dbus.service.Object):
    def __init__(self, bus, path, on_conn):
        super().__init__(bus, path)
        self.on_conn = on_conn

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, props):
        self.on_conn(str(device), socket.socket(fileno=fd.take()))

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera")
    ap.add_argument("--seconds", type=int, default=40)
    a = ap.parse_args()
    t0 = time.monotonic()
    log = lambda m: print(f"[{time.monotonic() - t0:6.2f}s] {m}", flush=True)
    if not wsm.find_lib():
        print(wsm.NOT_FOUND)
        return 1

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    objs = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    cam = a.camera or next((str(i["org.bluez.Device1"]["Address"]) for i in objs.values() if "org.bluez.Device1" in i
                            and str(i["org.bluez.Device1"].get("Name", "")).startswith("Gear 360")
                            and i["org.bluez.Device1"].get("Paired")), None)
    host = next(str(i["org.bluez.Adapter1"]["Address"]) for i in objs.values() if "org.bluez.Adapter1" in i)
    if not cam:
        print("no paired Gear 360 found")
        return 1
    log(f"host {host}  camera {cam}")

    stop, loop = threading.Event(), GLib.MainLoop()

    def on_conn(device, sock):
        log("camera connected back to our SAP service")
        threading.Thread(target=Session(sock, cam, host, log).run, args=(stop,), daemon=True).start()

    SapProfile(bus, PROFILE_PATH, on_conn)
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
    mgr.RegisterProfile(PROFILE_PATH, bt.SAP_HOST_UUID, {
        "Name": "SAP", "Role": "server", "Channel": dbus.UInt16(LOCAL_CHANNEL),
        "RequireAuthentication": dbus.Boolean(False), "RequireAuthorization": dbus.Boolean(False),
        "AutoConnect": dbus.Boolean(False)})
    trigger = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)

    def connect_trigger():
        try:
            trigger.connect((cam, bt.CAMERA_RFCOMM_CHANNEL))
            log(f"opened camera channel {bt.CAMERA_RFCOMM_CHANNEL} (trigger) - waiting for the camera")
        except OSError as e:
            log(f"could not open camera channel {bt.CAMERA_RFCOMM_CHANNEL}: {e}")

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
