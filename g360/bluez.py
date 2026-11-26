"""BlueZ glue for the Gear 360 SAP link (system dbus-python + PyGObject, as shipped with Pop!_OS/Ubuntu).

connect_back(): registers the host's SAP service (UUID ...aa80a90cdf4a) with BlueZ, opens the camera's trigger
channel (RFCOMM 2, UUID ...bb80a90cdf00) and returns the socket the camera opens back to us — the link that
carries all SAP traffic (see g360.sap).
"""
from __future__ import annotations

import socket
import threading

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from . import bt

# Must be installed before the first SystemBus() call, or exported objects (our Profile1) cannot receive calls.
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

PROFILE_PATH = "/g360/sap"
LOCAL_CHANNEL = 6     # Samsung phones use 6; BlueZ advertises it in our SDP record


class _Profile(dbus.service.Object):
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


def addresses(camera: str | None = None) -> tuple[str, str]:
    """(host adapter address, camera address) — camera defaults to the first paired 'Gear 360' device."""
    bus = dbus.SystemBus()
    objs = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    host = next(str(i["org.bluez.Adapter1"]["Address"]) for i in objs.values() if "org.bluez.Adapter1" in i)
    if not camera:
        camera = next((str(i["org.bluez.Device1"]["Address"]) for i in objs.values() if "org.bluez.Device1" in i
                       and str(i["org.bluez.Device1"].get("Name", "")).startswith("Gear 360")
                       and i["org.bluez.Device1"].get("Paired")), None)
    if not camera:
        raise RuntimeError("no paired Gear 360 found (pair it first)")
    return host, camera


class ConnectBack:
    """Context manager: `with ConnectBack(camera) as cb: sock = cb.wait(15)`."""

    def __init__(self, camera: str, log=print):
        self.camera, self.log = camera, log
        self._sock, self._event = None, threading.Event()
        self._trigger = None

    def __enter__(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self._profile = _Profile(self.bus, PROFILE_PATH, self._on_conn)
        self.mgr = dbus.Interface(self.bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
        self.mgr.RegisterProfile(PROFILE_PATH, bt.SAP_HOST_UUID, {
            "Name": "SAP", "Role": "server", "Channel": dbus.UInt16(LOCAL_CHANNEL),
            "RequireAuthentication": dbus.Boolean(False), "RequireAuthorization": dbus.Boolean(False),
            "AutoConnect": dbus.Boolean(False)})
        self.loop = GLib.MainLoop()
        threading.Thread(target=self.loop.run, daemon=True).start()     # services BlueZ's NewConnection calls
        self._trigger = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self._trigger.connect((self.camera, bt.CAMERA_RFCOMM_CHANNEL))
        self.log(f"opened camera channel {bt.CAMERA_RFCOMM_CHANNEL} (trigger); waiting for the camera to connect back")
        return self

    def _on_conn(self, device, sock):
        self._sock = sock
        self._event.set()

    def wait(self, timeout: float = 15.0) -> socket.socket:
        if not self._event.wait(timeout):
            raise TimeoutError("camera did not connect back")
        self.log("camera connected back to our SAP service")
        return self._sock

    def __exit__(self, *exc):
        for s in (self._sock, self._trigger):
            try:
                s and s.close()
            except OSError:
                pass
        try:
            self.mgr.UnregisterProfile(PROFILE_PATH)
        except dbus.exceptions.DBusException:
            pass
        self.loop.quit()
        return False
