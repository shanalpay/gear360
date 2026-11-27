"""Minimal wpa_supplicant control-interface client (the protocol wpa_cli speaks), for Wi-Fi Direct.

NetworkManager's wpa_supplicant (`-O /run/wpa_supplicant`) exposes one Unix datagram socket per interface,
root-only; the P2P one is `p2p-dev-<wifi iface>`. Two sockets are used, as in wpa_ctrl.c: one for
request/response commands, one ATTACHed that receives unsolicited events ("<level>EVENT ...").
"""
from __future__ import annotations

import glob
import itertools
import os
import re
import select
import socket

CTRL_DIR = "/run/wpa_supplicant"
_counter = itertools.count()


def p2p_iface(ctrl_dir: str | None = None) -> str:
    for p in sorted(glob.glob(os.path.join(ctrl_dir or CTRL_DIR, "p2p-dev-*"))):
        return os.path.basename(p)
    wl = sorted(glob.glob("/sys/class/net/wl*"))
    return "p2p-dev-" + os.path.basename(wl[0]) if wl else "p2p-dev-wlan0"


class _Sock:
    def __init__(self, path: str):
        self.local = f"/tmp/g360_wpa_{os.getpid()}_{next(_counter)}"
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.s.bind(self.local)
        self.s.connect(path)

    def recv(self, timeout: float) -> str | None:
        if not select.select([self.s], [], [], timeout)[0]:
            return None
        return self.s.recv(8192).decode(errors="replace")

    def close(self):
        self.s.close()
        try:
            os.unlink(self.local)
        except OSError:
            pass


class WpaCtrl:
    def __init__(self, iface: str | None = None, ctrl_dir: str | None = None):
        ctrl_dir = ctrl_dir or CTRL_DIR
        self.iface = iface or p2p_iface(ctrl_dir)
        self.path = os.path.join(ctrl_dir, self.iface)
        self._cmd = _Sock(self.path)
        self._mon = None

    def request(self, command: str, timeout: float = 10) -> str:
        self._cmd.s.send(command.encode())
        while True:
            r = self._cmd.recv(timeout)
            if r is None:
                raise TimeoutError(f"wpa_supplicant did not answer {command!r}")
            if not r.startswith("<"):        # skip stray events (none expected on the unattached socket)
                return r.rstrip("\n")

    def attach(self):
        self._mon = _Sock(self.path)
        self._mon.s.send(b"ATTACH")
        if (self._mon.recv(5) or "").strip() != "OK":
            raise RuntimeError("ATTACH failed")

    def event(self, timeout: float) -> str | None:
        """Next event with its <level> prefix removed, or None after timeout."""
        r = self._mon.recv(timeout)
        return re.sub(r"^<\d>", "", r).rstrip("\n") if r is not None else None

    def close(self):
        if self._mon:
            try:
                self._mon.s.send(b"DETACH")
            except OSError:
                pass
            self._mon.close()
        self._cmd.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
