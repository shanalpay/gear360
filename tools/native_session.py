#!/usr/bin/env python3
"""First fully native session: Bluetooth (SAP) + Wi-Fi Direct + UPnP, no phone.

  1. SAP session over Bluetooth (g360.sap), announcing this PC's Wi-Fi MAC as `wifi-mac-address` in `info`
  2. cmd-req execute mobilelink -> the camera enables Wi-Fi Direct
  3. Wi-Fi Direct as Samsung's phone does it: provision discovery (PBC), then GO negotiation with this PC as
     group owner (the camera first answers status 1, then sends its own request); 192.168.49.1/24 and DHCP
     for the camera (dnsmasq)
  4. g360's UPnP code talks to the camera directly (live view switch + capabilities)
The Bluetooth session stays open throughout: the camera drops Wi-Fi ~30 s after it ends.

Wi-Fi Direct modes (--p2p):
  wpacli  (sudo) NetworkManager's wpa_supplicant over its control socket
  own     (default, sudo) the Wi-Fi interface is taken from NetworkManager for the session and given the Wi-Fi MAC
          with the locally-administered bit set; a private patched wpa_supplicant runs P2P on it, so that is
          the P2P device address the camera expects (g360.p2p; build it once with
          tools/build_wpa_supplicant.sh). Restored on exit; Wi-Fi internet drops meanwhile.
  nm      NetworkManager profile `gear360-p2p` (no root):
            nmcli connection add type wifi-p2p con-name gear360-p2p wifi-p2p.peer <camera wifi-direct-mac> \
                wifi-p2p.wps-method pbc ipv4.method shared ipv4.addresses 192.168.49.1/24 \
                ipv6.method disabled connection.autoconnect no
Run from the project root:
  sudo G360_LIBWSM=/path/to/arm64/libwsm2.so python3 tools/native_session.py [--hold 60]
"""
import argparse
import contextlib
import glob
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sp in glob.glob(str(ROOT / ".venv/lib/python3*/site-packages")):
    sys.path.append(sp)

from g360 import bluez, jsonmsg, sap, wsm  # noqa: E402
from g360 import p2p as p2pmod  # noqa: E402
from g360.transport import DirectTransport  # noqa: E402
from g360.upnp import DEFAULT_IP, Camera  # noqa: E402


def p2p_device_addr() -> str:
    """The P2P-device wdev address from `iw dev` (not derivable from the Wi-Fi MAC on every driver)."""
    out = subprocess.run(["iw", "dev"], capture_output=True, text=True).stdout
    m = re.search(r"addr ([0-9a-f:]{17})\s+type P2P-device", out)
    if not m:
        raise RuntimeError("no P2P-device found in `iw dev` (install iw; does the Wi-Fi card support P2P?)")
    return m.group(1)


NOISY = ("CTRL-EVENT-SCAN-STARTED", "CTRL-EVENT-SCAN-RESULTS", "CTRL-EVENT-BSS-", "CTRL-EVENT-REGDOM")


def static_ip_ie(ip: str) -> str:
    """Samsung's "static IP" vendor element (OUI 00:00:f0, type 0x0a, 16-bit length 4, address bytes reversed),
    as the S8+ put in its Provision Discovery Request. The camera skips DHCP and takes this address; without it
    the camera picks one itself (it offers 192.168.49.41 in its PD response) and never asks for a lease."""
    return "dd0b0000f00a000004" + bytes(reversed([int(x) for x in ip.split(".")])).hex()


def wpa_connect(peer: str, log, timeout: float = 60, iface: str | None = None, ctrl_dir: str | None = None,
                debug_to_journal: bool = True, freq: int | None = None, cam_ip: str | None = None) -> str | None:
    """Root only: drive wpa_supplicant's P2P directly over its control socket (NetworkManager's instance by
    default, or our private one), log every event, and return the group interface name if a group starts.
    GO intent 7 as in app 1.4 (`groupOwnerIntent = 7`); the camera bids 1, so this PC becomes GO, as the phone did. The camera is matched by its announced
    address or by name."""
    from g360.wpactrl import WpaCtrl
    with WpaCtrl(iface, ctrl_dir) as w:
        w.attach()
        log(f"wpa_supplicant control socket {w.iface}: attached")
        if not debug_to_journal:
            return _wpa_connect(w, peer, log, timeout, freq, cam_ip)
        prev = re.search(r"Current level: (\w+)", w.request("LOG_LEVEL"))
        prev = prev.group(1) if prev else "INFO"
        w.request("LOG_LEVEL DEBUG")     # NetworkManager's wpa_supplicant logs to syslog (-s): the journal
        log(f"wpa_supplicant debug logging on (was {prev}): journalctl -u wpa_supplicant shows GO negotiation")
        try:
            return _wpa_connect(w, peer, log, timeout, freq, cam_ip)
        finally:
            w.request(f"LOG_LEVEL {prev}")


def _wpa_connect(w, peer: str, log, timeout: float, freq: int | None = None,
                 cam_ip: str | None = None) -> str | None:
    st = dict(l.split("=", 1) for l in w.request("STATUS").splitlines() if "=" in l)
    log(f"wpa: STATUS p2p_device_address={st.get('p2p_device_address')} wpa_state={st.get('wpa_state')}")
    w.request("VENDOR_ELEM_REMOVE 4 *")
    if cam_ip:      # 4 = VENDOR_ELEM_P2P_PD_REQ
        ie = static_ip_ie(cam_ip)
        log(f"wpa: VENDOR_ELEM_ADD 4 {ie} (camera address {cam_ip}) -> {w.request(f'VENDOR_ELEM_ADD 4 {ie}')}")
    log(f"wpa: P2P_FIND 30 -> {w.request('P2P_FIND 30')}")
    # As Samsung's phone does: provision discovery (PBC) first, then GO negotiation. The camera answers our
    # GO Negotiation Request with status 1 and then, prompted by the provision discovery, sends its own
    # request, which wpa_supplicant (waiting for the peer) accepts.
    target, group, seen, cam = peer.lower(), None, set(), None
    phase = "find"           # find -> pd -> connect
    t_start = time.monotonic()
    t_end = t_start + timeout
    t_pd = 0.0

    def provision(addr):
        nonlocal phase, cam, t_pd
        cam, phase, t_pd = addr, "pd", time.monotonic()
        log(f"wpa: P2P_PROV_DISC {addr} pbc -> {w.request(f'P2P_PROV_DISC {addr} pbc')}")

    def connect():
        nonlocal phase
        phase = "connect"
        cmd = f"P2P_CONNECT {cam} pbc go_intent=7" + (f" freq={freq}" if freq else "")
        log(f"wpa: {cmd} -> {w.request(cmd)}")

    while time.monotonic() < t_end:
        ev = w.event(1.0)
        if ev is None:
            if phase == "find" and time.monotonic() - t_start > 10:
                log("camera not seen in discovery after 10 s; trying its announced address anyway")
                provision(target)
            elif phase == "pd" and time.monotonic() - t_pd > 5:
                log("no provision discovery response in 5 s; starting GO negotiation anyway")
                connect()
            continue
        if ev.startswith(NOISY):
            continue
        if ev.startswith("P2P-DEVICE-FOUND"):
            addr = ev.split()[1].lower()
            name = re.search(r"name='([^']*)'", ev)
            if addr in seen:
                continue
            seen.add(addr)
            if addr == target or (name and name.group(1).startswith("Gear 360")):
                log(f"wpa: {ev}")
                if addr != target:
                    log(f"camera's Wi-Fi Direct device address is {addr}, not the announced {target}")
                if phase == "find":
                    provision(addr)
            continue
        log(f"wpa: {ev}")
        if phase == "pd" and ev.startswith(("P2P-PROV-DISC-PBC-RESP", "P2P-PROV-DISC-FAILURE")):
            connect()
        elif ev.startswith("P2P-GROUP-STARTED"):
            group = ev.split()[1]
            break
        elif ev.startswith(("P2P-GO-NEG-FAILURE", "P2P-GROUP-FORMATION-FAILURE")):
            break
    else:
        log(f"no group after {timeout:.0f} s")
    if not group:
        info = w.request(f"P2P_PEER {target}")
        log(f"wpa: P2P_PEER {target}: " + " ".join(info.splitlines()))
    w.request("P2P_STOP_FIND")
    return group


def wpa_cleanup(iface: str | None = None, ctrl_dir: str | None = None):
    from g360.wpactrl import WpaCtrl
    try:
        with WpaCtrl(iface, ctrl_dir) as w:
            w.request("P2P_GROUP_REMOVE *")
            w.request("P2P_CANCEL")
            w.request("VENDOR_ELEM_REMOVE 4 *")
    except (OSError, TimeoutError):
        pass


def serve_group(group: str, log):
    """Root only: give the GO interface 192.168.49.1/24 and run a DHCP server for the camera (as the phone did)."""
    subprocess.run(["ip", "addr", "add", "192.168.49.1/24", "dev", group], capture_output=True)
    subprocess.run(["ip", "link", "set", group, "up"], capture_output=True)
    log(f"{group}: 192.168.49.1/24; starting dnsmasq DHCP (camera gets 192.168.49.10, as from the phone)")
    (ROOT / "tmp").mkdir(exist_ok=True)
    out = open(ROOT / "tmp" / "dnsmasq.log", "w")
    os.chmod(out.name, 0o644)
    return subprocess.Popen(["dnsmasq", "--no-daemon", "--port=0", f"--interface={group}", "--bind-interfaces",
                             "--dhcp-range=192.168.49.10,192.168.49.10,255.255.255.0,1h", "--log-dhcp",
                             "--dhcp-leasefile=" + str(ROOT / "tmp" / "dnsmasq.leases")],
                            stdout=out, stderr=subprocess.STDOUT)




def session(a, log, host: str, cam: str, announce: str, wpa: dict) -> int:
    ready = threading.Event()        # mobilelink accepted
    state = {"desc_url": None, "cam_p2p": None}
    base = sap.standard_json_handler(announce, then=lambda s: s.send_json(jsonmsg.cmd_req("execute", "mobilelink")))

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

    stop, dhcp = threading.Event(), None
    try:
        with bluez.ConnectBack(cam, log=log) as cb:
            sess = sap.HostSession(cb.wait(15), cam, host, log=log, on_json=on_json)

            def pump():
                try:
                    while not stop.is_set() and sess.pump(0.5):
                        pass
                except OSError:          # socket closed during shutdown
                    if not stop.is_set():
                        raise
            threading.Thread(target=pump, daemon=True).start()

            if not ready.wait(30):
                log("mobilelink was not accepted; stopping")
                return 2
            if a.p2p in ("own", "wpacli"):
                group = wpa_connect(state["cam_p2p"], log, **wpa)
                if not group:
                    log("no Wi-Fi Direct group formed (see the wpa: lines above)")
                    return 3
                log(f"*** Wi-Fi Direct group up on {group} ***")
                dhcp = serve_group(group, log)
            else:
                log(f"bringing up Wi-Fi Direct: nmcli connection up {a.profile} (camera P2P {state['cam_p2p']})")
                r = subprocess.run(["nmcli", "--wait", "60", "connection", "up", a.profile],
                                   capture_output=True, text=True)
                log(f"nmcli: {(r.stdout or r.stderr).strip()}")
                if r.returncode != 0:
                    return 3
            ipinfo = subprocess.run(["ip", "-4", "-br", "addr"], capture_output=True, text=True).stdout
            log("interfaces: " + "; ".join(l for l in ipinfo.splitlines() if "192.168.49." in l or "p2p" in l))

            # The camera reports its UPnP description URL over Bluetooth once it is on Wi-Fi: use its host.
            for _ in range(20):
                if state["desc_url"]:
                    break
                time.sleep(0.5)
            ip = a.cam_ip
            m = re.search(r"//([\d.]+)", state["desc_url"] or "")
            if m:
                ip = m.group(1)
            else:
                log(f"no device-desc-url from the camera yet; trying {ip}")
            camera = Camera(DirectTransport(), ip=ip)
            if not camera.wait_ready(45, log=log):
                neigh = subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout
                log(f"camera not reachable over Wi-Fi at {ip}; neighbours: "
                    + "; ".join(l for l in neigh.splitlines() if "192.168.49." in l))
                return 5
            log("camera reachable over Wi-Fi Direct - switching to live view")
            caps = camera.enter_live_view()
            log(f"*** NATIVE CONTROL WORKS: {caps.raw.get('StreamingInfo', {}).get('Default')} stream at "
                f"{caps.stream_urls.get('high')}; {caps.raw.get('AVAILSHOTS')} shots left ***")
            log(f"holding the session for {a.hold:.0f} s (try: python3 -m g360 --direct live --play)")
            time.sleep(a.hold)
            return 0
    finally:
        stop.set()
        if dhcp:
            dhcp.terminate()
        if a.p2p == "nm":
            subprocess.run(["nmcli", "connection", "down", a.profile], capture_output=True)
        else:
            wpa_cleanup(wpa.get("iface"), wpa.get("ctrl_dir"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera")
    ap.add_argument("--p2p", choices=["own", "wpacli", "nm"], default="own",
                    help="own (default, sudo): private patched wpa_supplicant with the S8+ address pattern; wpacli (sudo): NetworkManager's wpa_supplicant over its control socket; "
                         "nm: NetworkManager profile (no root, no provision discovery)")
    ap.add_argument("--iface", help="Wi-Fi interface for --p2p own (default: the first wl* interface)")
    ap.add_argument("--announce", help="override the wifi-mac-address announced to the camera")
    ap.add_argument("--freq", type=int, default=None,
                    help="force the group's channel in MHz (default for --p2p own: 2412, the camera's channel; "
                         "Intel cards cannot start a GO on most 5 GHz channels unless already connected there)")
    ap.add_argument("--cam-ip", default=DEFAULT_IP,
                    help=f"address handed to the camera in provision discovery (default {DEFAULT_IP}, as the phone does)")
    ap.add_argument("--profile", default="gear360-p2p")
    ap.add_argument("--hold", type=float, default=60, help="seconds to keep the session up after success")
    a = ap.parse_args()
    if not os.environ.get(wsm.ENV_VAR):
        print(f"set {wsm.ENV_VAR} to Samsung's arm64 libwsm2.so")
        return 1
    if a.p2p != "nm" and os.geteuid() != 0:
        print(f"--p2p {a.p2p} needs root (sudo)")
        return 4
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))    # so cleanup runs on kill too
    t0 = time.monotonic()
    (ROOT / "tmp").mkdir(exist_ok=True)
    logf = open(ROOT / "tmp" / "native_session.log", "w")
    os.chmod(logf.name, 0o644)

    def log(m):
        line = f"[{time.monotonic() - t0:6.2f}s] {m}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    host, cam = bluez.addresses(a.camera)
    with contextlib.ExitStack() as stack:
        wpa = {}
        if a.p2p == "own":
            iface = a.iface or os.path.basename(sorted(glob.glob("/sys/class/net/wl*"))[0])
            (ROOT / "tmp").mkdir(exist_ok=True)
            ws = stack.enter_context(p2pmod.OwnSupplicant(iface, log=log, logfile=str(ROOT / "tmp" / "wpa_own.log")))
            announce = a.announce or ws.perm
            wpa = {"iface": ws.iface, "ctrl_dir": ws.ctrl_dir, "debug_to_journal": False, "freq": a.freq or 2412,
                   "cam_ip": a.cam_ip}
        else:
            announce = a.announce or p2p_device_addr()
            if a.p2p == "wpacli":
                wpa = {"freq": a.freq, "cam_ip": a.cam_ip}
        log(f"host {host}  camera {cam}  announcing wifi-mac-address {announce}")
        rc = session(a, log, host, cam, announce, wpa)
    log(f"done (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
