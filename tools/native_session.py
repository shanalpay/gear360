#!/usr/bin/env python3
"""First fully native session: Bluetooth (SAP) + Wi-Fi Direct + UPnP, no phone.

  1. SAP session over Bluetooth (g360.sap), announcing this PC's real Wi-Fi Direct device address in `info`
  2. cmd-req execute mobilelink -> the camera enables Wi-Fi Direct and expects a connection from that address
  3. NetworkManager brings up the `gear360-p2p` Wi-Fi Direct profile (PBC, ipv4 shared 192.168.49.1/24)
  4. g360's UPnP code talks to the camera directly (live view switch + capabilities)
The Bluetooth session stays open throughout: the camera drops Wi-Fi ~30 s after it ends.

Setup (once):
  nmcli connection add type wifi-p2p con-name gear360-p2p wifi-p2p.peer <camera wifi-direct-mac> \
      wifi-p2p.wps-method pbc ipv4.method shared ipv4.addresses 192.168.49.1/24 ipv6.method disabled \
      connection.autoconnect no
Run from the project root:
  G360_LIBWSM=/path/to/arm64/libwsm2.so python3 tools/native_session.py [--hold 60]
"""
import argparse
import glob
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sp in glob.glob(str(ROOT / ".venv/lib/python3*/site-packages")):
    sys.path.append(sp)

from g360 import bluez, jsonmsg, sap, wsm  # noqa: E402
from g360.transport import DirectTransport  # noqa: E402
from g360.upnp import Camera  # noqa: E402


def p2p_device_addr() -> str:
    """The P2P-device wdev address from `iw dev` (not derivable from the Wi-Fi MAC on every driver)."""
    out = subprocess.run(["iw", "dev"], capture_output=True, text=True).stdout
    m = re.search(r"addr ([0-9a-f:]{17})\s+type P2P-device", out)
    if not m:
        raise RuntimeError("no P2P-device found in `iw dev` (install iw; does the Wi-Fi card support P2P?)")
    return m.group(1)


NOISY = ("CTRL-EVENT-SCAN-STARTED", "CTRL-EVENT-SCAN-RESULTS", "CTRL-EVENT-BSS-", "CTRL-EVENT-REGDOM")


def wpa_connect(peer: str, log, timeout: float = 60) -> str | None:
    """Root only: drive wpa_supplicant's P2P directly over its control socket (NetworkManager's instance),
    log every event, and return the group interface name if a group starts. We ask to be group owner
    (go_intent=15), as Samsung's phone was. The camera is matched by its announced address or by name."""
    from g360.wpactrl import WpaCtrl
    with WpaCtrl() as w:
        w.attach()
        log(f"wpa_supplicant control socket {w.iface}: attached")
        st = dict(l.split("=", 1) for l in w.request("STATUS").splitlines() if "=" in l)
        log(f"wpa: STATUS p2p_device_address={st.get('p2p_device_address')} wpa_state={st.get('wpa_state')}")
        log(f"wpa: P2P_FIND 30 -> {w.request('P2P_FIND 30')}")
        target, connect_sent, group, seen = peer.lower(), False, None, set()
        t_start = time.monotonic()
        t_end = t_start + timeout

        def connect(addr):
            nonlocal connect_sent
            connect_sent = True
            r = w.request(f"P2P_CONNECT {addr} pbc go_intent=15")
            log(f"wpa: P2P_CONNECT {addr} pbc go_intent=15 -> {r}")

        while time.monotonic() < t_end:
            ev = w.event(1.0)
            if ev is None:
                if not connect_sent and time.monotonic() - t_start > 10:
                    log("camera not seen in discovery after 10 s; connecting to its announced address anyway")
                    connect(target)
                continue
            if ev.startswith(NOISY):
                continue
            if ev.startswith("P2P-DEVICE-FOUND"):
                addr = ev.split()[1].lower()
                name = re.search(r"name='([^']*)'", ev)
                if addr in seen:
                    continue
                seen.add(addr)
                log(f"wpa: {ev}")
                if not connect_sent and (addr == target or (name and name.group(1).startswith("Gear 360"))):
                    if addr != target:
                        log(f"camera's Wi-Fi Direct device address is {addr}, not the announced {target}")
                    connect(addr)
                continue
            log(f"wpa: {ev}")
            if ev.startswith("P2P-GROUP-STARTED"):
                group = ev.split()[1]
                break
            if ev.startswith(("P2P-GO-NEG-FAILURE", "P2P-GROUP-FORMATION-FAILURE")):
                for addr in sorted(seen | {target}):
                    info = w.request(f"P2P_PEER {addr}")
                    log(f"wpa: P2P_PEER {addr}: " + " ".join(info.splitlines()[:12]))
                break
        else:
            log(f"no group after {timeout:.0f} s")
        w.request("P2P_STOP_FIND")
    return group


def wpa_cleanup():
    from g360.wpactrl import WpaCtrl
    try:
        with WpaCtrl() as w:
            w.request("P2P_GROUP_REMOVE *")
            w.request("P2P_CANCEL")
    except (OSError, TimeoutError):
        pass


def serve_group(group: str, log):
    """Root only: give the GO interface 192.168.49.1/24 and run a DHCP server for the camera (as the phone did)."""
    subprocess.run(["ip", "addr", "add", "192.168.49.1/24", "dev", group], capture_output=True)
    subprocess.run(["ip", "link", "set", group, "up"], capture_output=True)
    log(f"{group}: 192.168.49.1/24; starting dnsmasq DHCP (192.168.49.10-50)")
    return subprocess.Popen(["dnsmasq", "--no-daemon", "--port=0", f"--interface={group}", "--bind-interfaces",
                             "--dhcp-range=192.168.49.10,192.168.49.50,255.255.255.0,1h", "--log-dhcp"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera")
    ap.add_argument("--p2p", choices=["nm", "wpacli"], default="nm",
                    help="nm: NetworkManager profile (no root); wpacli: drive wpa_supplicant directly (run with sudo)")
    ap.add_argument("--profile", default="gear360-p2p")
    ap.add_argument("--hold", type=float, default=60, help="seconds to keep the session up after success")
    a = ap.parse_args()
    if not os.environ.get(wsm.ENV_VAR):
        print(f"set {wsm.ENV_VAR} to Samsung's arm64 libwsm2.so")
        return 1
    t0 = time.monotonic()
    log = lambda m: print(f"[{time.monotonic() - t0:6.2f}s] {m}", flush=True)

    host, cam = bluez.addresses(a.camera)
    p2p = p2p_device_addr()
    log(f"host {host}  camera {cam}  our Wi-Fi Direct address {p2p}")

    ready = threading.Event()        # mobilelink accepted
    state = {"desc_url": None, "cam_p2p": None}
    base = sap.standard_json_handler(p2p, then=lambda s: s.send_json(jsonmsg.cmd_req("execute", "mobilelink")))

    def on_json(s, m):
        base(s, m)
        if m is None:
            return
        if m.msg_id == "info" and m.value("wifi-direct-mac"):
            state["cam_p2p"] = m.value("wifi-direct-mac")
        if m.msg_id == "cmd-rsp" and m.result and m.result[1] == "mobilelink":
            log(f"camera answered mobilelink: {m.result[0]} (r-code {m.r_code})")
            if m.result[0] == "success":
                ready.set()
        if m.msg_id == "device-desc-url":
            state["desc_url"] = m.value("url") or m.flat()
            log(f"camera UPnP description: {state['desc_url']}")

    stop = threading.Event()
    with bluez.ConnectBack(cam, log=log) as cb:
        sess = sap.HostSession(cb.wait(15), cam, host, log=log, on_json=on_json)

        def pump():
            while not stop.is_set() and sess.pump(0.5):
                pass
        threading.Thread(target=pump, daemon=True).start()

        if not ready.wait(30):
            log("mobilelink was not accepted; stopping")
            stop.set()
            return 2
        dhcp = None
        if a.p2p == "wpacli":
            if os.geteuid() != 0:
                log("--p2p wpacli needs root (sudo)")
                stop.set()
                return 4
            group = wpa_connect(state["cam_p2p"], log)
            if not group:
                log("no Wi-Fi Direct group formed (see the wpa: lines above)")
                stop.set()
                wpa_cleanup()
                return 3
            log(f"*** Wi-Fi Direct group up on {group} ***")
            dhcp = serve_group(group, log)
        else:
            log(f"bringing up Wi-Fi Direct: nmcli connection up {a.profile} (camera P2P {state['cam_p2p']})")
            r = subprocess.run(["nmcli", "--wait", "60", "connection", "up", a.profile], capture_output=True, text=True)
            log(f"nmcli: {(r.stdout or r.stderr).strip()}")
            if r.returncode != 0:
                stop.set()
                return 3
        ipinfo = subprocess.run(["ip", "-4", "-br", "addr"], capture_output=True, text=True).stdout
        log("interfaces: " + "; ".join(l for l in ipinfo.splitlines() if "192.168.49." in l or "p2p" in l))

        camera = Camera(DirectTransport())
        if not camera.wait_ready(45, log=log):
            log("camera not reachable over Wi-Fi")
        else:
            log("camera reachable over Wi-Fi Direct - switching to live view")
            caps = camera.enter_live_view()
            log(f"*** NATIVE CONTROL WORKS: {caps.raw.get('StreamingInfo', {}).get('Default')} stream at "
                f"{caps.stream_urls.get('high')}; {caps.raw.get('AVAILSHOTS')} shots left ***")
            log(f"holding the session for {a.hold:.0f} s (try: python3 -m g360 --direct live --play)")
            time.sleep(a.hold)
        stop.set()
        if dhcp:
            dhcp.terminate()
    if a.p2p == "nm":
        subprocess.run(["nmcli", "connection", "down", a.profile], capture_output=True)
    else:
        wpa_cleanup()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
