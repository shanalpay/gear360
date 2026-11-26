#!/usr/bin/env python3
"""Run the whole Bluetooth side natively: connect-back, Peer Description, WSM auth, capability exchange,
Gear 360 service connection and the opening JSON exchange (info / date-time / config). Optionally send a
command at the end (e.g. --command mobilelink, which makes the camera start Wi-Fi Direct).

Run from the project root with the system python3 (BlueZ via dbus-python) plus .venv for unicorn:
    G360_LIBWSM=/path/to/arm64/libwsm2.so python3 tools/sap_session.py [--seconds 40] [--command mobilelink]
"""
import argparse
import glob
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sp in glob.glob(str(ROOT / ".venv/lib/python3*/site-packages")):
    sys.path.append(sp)

from g360 import bluez, jsonmsg, sap, wsm  # noqa: E402


def default_p2p_mac() -> str:
    """Linux derives the P2P device address from the Wi-Fi MAC by setting the locally-administered bit (to be
    confirmed against wpa_supplicant when Wi-Fi Direct is implemented)."""
    for dev in sorted(glob.glob("/sys/class/net/wl*/address")):
        mac = open(dev).read().strip()
        b = bytes.fromhex(mac.replace(":", ""))
        return ":".join(f"{x:02x}" for x in bytes([b[0] | 0x02]) + b[1:])
    return "02:00:00:00:00:01"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera")
    ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--command", help="cmd-req to execute after the opening exchange, e.g. mobilelink")
    ap.add_argument("--p2p-mac", default=None)
    a = ap.parse_args()
    if not os.environ.get(wsm.ENV_VAR):
        print(f"set {wsm.ENV_VAR} to Samsung's arm64 libwsm2.so")
        return 1
    t0 = time.monotonic()
    log = lambda m: print(f"[{time.monotonic() - t0:6.2f}s] {m}", flush=True)
    host, cam = bluez.addresses(a.camera)
    p2p = a.p2p_mac or default_p2p_mac()
    log(f"host {host}  camera {cam}  (P2P address announced to camera: {p2p})")

    def after_config(s: sap.HostSession):
        log("opening JSON exchange complete")
        if a.command:
            s.send_json(jsonmsg.cmd_req("execute", a.command))

    with bluez.ConnectBack(cam, log=log) as cb:
        sock = cb.wait(15)
        s = sap.run(sock, cam, host, a.seconds, log=log, on_json=sap.standard_json_handler(p2p, then=after_config))
    log(f"done (authenticated={s.authenticated}, json session={s.json_session})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
