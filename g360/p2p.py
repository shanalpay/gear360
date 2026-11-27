"""Wi-Fi Direct with a chosen P2P device address (root only).

The Gear 360 only accepts GO negotiation from the address it derives from the phone's announced
`wifi-mac-address`: that address with the locally-administered bit (0x02) set, which is how older phones
form their P2P device address. On Intel/mac80211 cards the dedicated P2P-device wdev gets the next hardware
address instead (bit clear) and its address cannot be changed, so the camera answers "status 1".

OwnSupplicant temporarily takes the Wi-Fi interface away from NetworkManager, gives it the locally
administered address, and runs a private wpa_supplicant *without* a dedicated P2P device
(no `p2p_device=1`), so the P2P device address is the interface address. The group also runs on that
interface (`p2p_no_group_iface=1`). Everything is restored on exit (address, NetworkManager control).
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import time

CTRL_DIR = "/run/g360-wpa"

CONF = """ctrl_interface={ctrl}
update_config=0
device_name=g360
device_type=1-0050F204-1
manufacturer=g360
model_name=g360
config_methods=push_button virtual_push_button
p2p_go_intent=15
p2p_no_group_iface=1
"""


def laa(mac: str) -> str:
    b = bytes.fromhex(mac.replace(":", ""))
    return ":".join(f"{x:02x}" for x in bytes([b[0] | 0x02]) + b[1:])


def perm_addr(iface: str) -> str:
    out = subprocess.run(["ethtool", "-P", iface], capture_output=True, text=True).stdout
    if "address:" in out:
        return out.split("address:", 1)[1].strip().lower()
    return pathlib.Path(f"/sys/class/net/{iface}/address").read_text().strip().lower()


def _run(*cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True)


class OwnSupplicant:
    """`with OwnSupplicant("wlp0s20f3", log=log, logfile=...) as ws:` -> ws.ctrl_dir, ws.iface, ws.addr."""

    def __init__(self, iface: str, log=print, addr: str | None = None, logfile: str | None = None):
        self.iface, self.log = iface, log
        self.perm = perm_addr(iface)
        self.addr = addr or laa(self.perm)
        self.logfile = logfile
        self.ctrl_dir = CTRL_DIR
        self.proc = None

    def __enter__(self):
        try:
            return self._start()
        except BaseException:
            self.__exit__()
            raise

    def _start(self):
        os.makedirs(self.ctrl_dir, mode=0o700, exist_ok=True)
        self.log(f"taking {self.iface} from NetworkManager (internet stays on Ethernet)")
        _run("nmcli", "device", "set", self.iface, "managed", "no")
        nm_sock = f"/run/wpa_supplicant/{self.iface}"
        for _ in range(50):
            if not os.path.exists(nm_sock):
                break
            time.sleep(0.1)
        else:
            self.log(f"warning: NetworkManager's wpa_supplicant still holds {self.iface}")
        _run("ip", "link", "set", self.iface, "down")
        r = _run("ip", "link", "set", self.iface, "address", self.addr)
        if r.returncode:
            raise RuntimeError(f"cannot set {self.iface} address: {r.stderr.strip()}")
        _run("ip", "link", "set", self.iface, "up")
        conf = os.path.join(self.ctrl_dir, "g360.conf")
        pathlib.Path(conf).write_text(CONF.format(ctrl=self.ctrl_dir))
        out = open(self.logfile, "w") if self.logfile else subprocess.DEVNULL
        if self.logfile:
            os.chmod(self.logfile, 0o644)
        self.proc = subprocess.Popen(["wpa_supplicant", "-i", self.iface, "-D", "nl80211", "-c", conf, "-dd", "-t"],
                                     stdout=out, stderr=subprocess.STDOUT)
        sock = os.path.join(self.ctrl_dir, self.iface)
        for _ in range(100):
            if os.path.exists(sock) or self.proc.poll() is not None:
                break
            time.sleep(0.1)
        if not os.path.exists(sock):
            raise RuntimeError(f"private wpa_supplicant did not start (see {self.logfile})")
        self.log(f"private wpa_supplicant on {self.iface}, Wi-Fi Direct address {self.addr} (permanent {self.perm})")
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        _run("ip", "addr", "flush", "dev", self.iface)
        _run("ip", "link", "set", self.iface, "down")
        _run("ip", "link", "set", self.iface, "address", self.perm)
        _run("nmcli", "device", "set", self.iface, "managed", "yes")
        self.log(f"{self.iface} restored to {self.perm} and handed back to NetworkManager")
        return False
