"""Samsung Accessory Protocol (SAP) host side for the Gear 360 (SM-C200), over an RFCOMM socket.

Reverse-engineered from the session-1 HCI capture and SAccessoryService 3.1.26 (SAProtocolHeaderConstants,
SAConnection.updateConnectivityData, SAFrameworkUtils.computeCrc). See NOTES.md.

Framing
  before authentication:  u16 BE length | body
  after authentication:   u16 BE length | CRC16(length bytes) | body | CRC16(body)      (CRC-16/ARC)
  body = 2-byte SAP header | payload
  header: byte0 = version(3 bits, 0) | frame-type bit 4 (0 = data) | session-id bits 9..6
          byte1 = session-id bits 5..0 << 2 | fragmentation (2 bits, 0)
Sessions: 1023 = default (service-connection control), a host-chosen session for capability discovery,
then three camera-chosen sessions for /system/DI_360_2D (service channels 204, 222, 230); JSON travels on the
first, each payload prefixed with one 0x00 byte.

Conversation (host = our side, mirrors Samsung's phone app):
  cam  PD (0x05)                      host PD response (0x06)
  cam  auth 0x10 client challenge     host 0x11 server challenge (g360.wsm, WSM v1, camera = server)
  cam  auth 0x12 client response      host verifies -> CRC framing from here on
  host [1023] connect "/System/Reserved/ServiceCapabilityDiscovery;" on session CAPEX_SESSION
  cam  [1023] accept
  host [capex] capability query (our services)       cam [capex] query -> host empty response
  cam  [capex] its capability response + query for /system/DI_360_2D -> host response (NX360_SERVER provider)
  cam  [1023] service connection request for /system/DI_360_2D with 3 sessions -> host accepts
  JSON (g360.jsonmsg) on the first of those sessions
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Callable

from . import bt, jsonmsg

DEFAULT_SESSION = 1023
CAPEX_SESSION = 220                       # host-chosen; the phone used 220 then 854
CAPEX_SERVICE = b"/System/Reserved/ServiceCapabilityDiscovery;"
GEAR360_PROFILE = b"/system/DI_360_2D;"

# Peer Description response, verbatim from Samsung's phone app (strings "SM-A102U1;samsung;Galaxy A10e;SWatch;").
# Header fields not decoded yet; the camera accepted it unchanged from Linux.
PD_RESPONSE = bytes.fromhex(
    "06020102010002000ffaaaf0aa03fe271002000a01"
    "534d2d4131303255313b73616d73756e673b47616c61787920413130653b5357617463683b")
# Capability query listing the phone app's services (frame 2103), sent on the capex session.
CAPEX_QUERY = bytes.fromhex("0103ffffffff05") + b"/runestone/gear;iapclient;/system/filetransfer;/system/DI_360_2D;/system/webproxy;"
CAPEX_EMPTY_RESPONSE = bytes.fromhex("0203000000000000")
# Our capability answer for /system/DI_360_2D (frame 2110): agent cb0a930a, app NX360_SERVER, provider.
CAPEX_PROVIDER_RESPONSE = (bytes.fromhex("0203cb0a930a0001b649") + b"NX360_SERVER;" + bytes.fromhex("0001b242")
                           + GEAR360_PROFILE + bytes.fromhex("010000000a"))


def _crc_table() -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        t.append(c)
    return t


_CRC = _crc_table()


def crc16(data: bytes) -> int:
    """CRC-16/ARC, as SAFrameworkUtils.computeCrc (table[1] = 0xC0C1)."""
    c = 0
    for b in data:
        c = (c >> 8) ^ _CRC[(b ^ c) & 0xFF]
    return c


def header(session: int, control: bool = False, frag: int = 0) -> bytes:
    return bytes([(0x10 if control else 0) | ((session >> 6) & 0x0F), ((session & 0x3F) << 2) | (frag & 3)])


def encode(body: bytes, crc: bool) -> bytes:
    n = len(body).to_bytes(2, "big")
    if not crc:
        return n + body
    return n + crc16(n).to_bytes(2, "big") + body + crc16(body).to_bytes(2, "big")


def encode_session(session: int, payload: bytes) -> bytes:
    return encode(header(session) + payload, crc=True)


@dataclass
class Frame:
    raw_body: bytes                 # everything between the framing (header + payload once CRC mode is on)
    session: int | None = None
    payload: bytes = b""


class FrameError(ValueError):
    pass


class Decoder:
    """Incremental decoder; switch `crc` on once authentication has completed."""

    def __init__(self):
        self.buf, self.crc = b"", False

    def feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        out = []
        while True:
            if len(self.buf) < 2:
                break
            n = int.from_bytes(self.buf[:2], "big")
            extra = 4 if self.crc else 0
            if len(self.buf) < 2 + n + extra:
                break
            if self.crc:
                if crc16(self.buf[:2]) != int.from_bytes(self.buf[2:4], "big"):
                    raise FrameError(f"length CRC mismatch: {self.buf[:8].hex(' ')}")
                body = self.buf[4:4 + n]
                if crc16(body) != int.from_bytes(self.buf[4 + n:6 + n], "big"):
                    raise FrameError("payload CRC mismatch")
                self.buf = self.buf[6 + n:]
                sess = ((body[0] & 0x0F) << 6) | (body[1] >> 2)
                out.append(Frame(body, sess, body[2:]))
            else:
                body, self.buf = self.buf[2:2 + n], self.buf[2 + n:]
                out.append(Frame(body))
        return out


@dataclass
class HostSession:
    """Drives the host side of the SAP conversation on a connected socket (the camera's connect-back)."""
    sock: socket.socket
    camera_addr: str
    host_addr: str
    log: Callable[[str], None] = print
    on_json: Callable[["HostSession", jsonmsg.Message], None] | None = None
    dec: Decoder = field(default_factory=Decoder)
    wsm_client: object = None
    authenticated: bool = False
    json_session: int | None = None
    service_sessions: list[int] = field(default_factory=list)

    # ---- sending -------------------------------------------------------------------------------
    def _send_raw(self, body: bytes, what: str):
        self.sock.sendall(encode(body, crc=False))
        self.log(f"-> {what}")

    def send_session(self, session: int, payload: bytes, what: str):
        self.sock.sendall(encode_session(session, payload))
        self.log(f"-> [{session}] {what}")

    def send_json(self, msg: dict):
        if self.json_session is None:
            raise RuntimeError("Gear 360 service not connected yet")
        data = jsonmsg.encode(msg)
        self.send_session(self.json_session, b"\x00" + data, f"JSON {msg['properties']['msgId']}")

    # ---- receiving -----------------------------------------------------------------------------
    def pump(self, timeout: float = 1.0) -> bool:
        """Read once and dispatch whatever frames arrived; False when the link closed."""
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(8192)
        except socket.timeout:
            return True
        if not data:
            self.log("camera closed the SAP connection")
            return False
        for f in self.dec.feed(data):
            self._dispatch(f)
        return True

    def _dispatch(self, f: Frame):
        if not self.authenticated:
            self._pre_auth(f.raw_body)
        elif f.session == DEFAULT_SESSION:
            self._control(f.payload)
        elif f.session == CAPEX_SESSION:
            self._capex(f.payload)
        elif f.session in self.service_sessions:
            if f.session == self.json_session and f.payload[:1] == b"\x00":
                try:
                    m = jsonmsg.decode(f.payload[1:])
                except ValueError:
                    self.log(f"<- [{f.session}] non-JSON data {f.payload[:40].hex(' ')}")
                    return
                self.log(f"<- JSON {m.msg_id} {m.flat()}")
                if self.on_json:
                    self.on_json(self, m)
            elif b"<GSIM>" in f.payload[:8]:
                # analytics/status strings on channel 230, e.g. "<GSIM>@[ID]StandaloneRate@[RATE]14@</GSIM>"
                self.log(f"<- [{f.session}] status {f.payload[1:].decode(errors='replace')}")
            else:
                self.log(f"<- [{f.session}] {len(f.payload)} B {f.payload[:40].hex(' ')}")
        else:
            self.log(f"<- [{f.session}] unexpected session, {len(f.payload)} B {f.payload[:40].hex(' ')}")

    def _pre_auth(self, body: bytes):
        from . import wsm
        t = body[0]
        if t == bt.PD_REQUEST:
            self.log("<- Peer Description: " + body[20:].decode(errors="replace"))
            self._send_raw(PD_RESPONSE, "Peer Description response")
        elif t == bt.AUTH_CLIENT_CHALLENGE:
            self.wsm_client = wsm.Client(self.camera_addr, self.host_addr, version=bt.WSM_VERSION)
            sc = self.wsm_client.server_challenge(body[2:])
            self.log("<- client challenge; computed server challenge (emulated libwsm2)")
            self._send_raw(bytes([bt.AUTH_SERVER_CHALLENGE, 0]) + sc, "server challenge")
        elif t == bt.AUTH_CLIENT_RESPONSE:
            self.wsm_client.check_response(body[2:])     # raises wsm.WSMError if the camera is not genuine
            self.authenticated, self.dec.crc = True, True
            self.log("<- client response verified: AUTHENTICATED (CRC framing on)")
            # host starts the capability-discovery service connection on the default session
            req = (b"\x01\xff\xff\xff\xff" + CAPEX_SERVICE + b"\x00\x01" + CAPEX_SESSION.to_bytes(2, "big")
                   + bytes.fromhex("00ff03010300"))
            self.send_session(DEFAULT_SESSION, req, f"connect capability discovery on session {CAPEX_SESSION}")
        else:
            self.log(f"<- pre-auth frame type 0x{t:02x}: {body[:40].hex(' ')}")

    def _control(self, p: bytes):
        if p[:1] == b"\x02" and CAPEX_SERVICE in p:
            self.log("<- capability discovery accepted")
            self.send_session(CAPEX_SESSION, CAPEX_QUERY, "capability query")
        elif p[:1] == b"\x01" and GEAR360_PROFILE in p:
            # service connection request: 01 | ids(4) | name | u16 count | count*u16 sessions | count*u16 channels ...
            i = p.index(GEAR360_PROFILE) + len(GEAR360_PROFILE)
            count = int.from_bytes(p[i:i + 2], "big")
            sessions = [int.from_bytes(p[i + 2 + 2 * k:i + 4 + 2 * k], "big") for k in range(count)]
            chans = [int.from_bytes(p[i + 2 + 2 * count + 2 * k:i + 4 + 2 * count + 2 * k], "big") for k in range(count)]
            self.service_sessions = sessions
            self.json_session = sessions[chans.index(bt.SERVICE_CHANNELS["command"])] if bt.SERVICE_CHANNELS["command"] in chans else sessions[0]
            self.log(f"<- Gear 360 service request: sessions {sessions} channels {chans}")
            resp = b"\x02" + p[1:i] + b"\x00" + count.to_bytes(2, "big") + b"".join(s.to_bytes(2, "big") for s in sessions)
            self.send_session(DEFAULT_SESSION, resp, f"accept Gear 360 service (JSON on session {self.json_session})")
            if self.on_json:
                self.on_json(self, None)          # signal: service up, host may start the JSON exchange
        else:
            self.log(f"<- [1023] control {p[:48].hex(' ')}")

    def _capex(self, p: bytes):
        if p[:2] == b"\x01\x02" and GEAR360_PROFILE in p:
            self.log("<- camera asks who provides /system/DI_360_2D")
            self.send_session(CAPEX_SESSION, CAPEX_PROVIDER_RESPONSE, "capability response: NX360_SERVER provides DI_360_2D")
        elif p[:2] == b"\x01\x02":
            self.send_session(CAPEX_SESSION, CAPEX_EMPTY_RESPONSE, "empty capability response")
        elif p[:2] == b"\x02\x02":
            self.log("<- camera capability response: " + "".join(chr(c) if 32 <= c < 127 else "." for c in p))
        else:
            self.log(f"<- [capex] {p[:48].hex(' ')}")


def standard_json_handler(p2p_mac: str, then: Callable[[HostSession], None] | None = None,
                          device_name: str = "Linux", settle: float = 1.0):
    """Answers the camera's JSON the way Samsung's app does (session-2 capture of app 1.4.00.9):
    info + widget-info-req -> (camera) date-time-req -> rsp; (camera) widget-info-req -> NOT answered by the
    app; (camera) info -> ack; (camera) config-info -> ack; (camera) widget-info-rsp.
    `then(session)` runs once the camera's widget-info-rsp has arrived (the app only sent commands after that)."""
    done = {"config": False, "then": False}

    def handler(s: HostSession, m: jsonmsg.Message | None):
        if m is None:                                   # service just came up: introduce ourselves
            s.send_json(jsonmsg.phone_info(p2p_mac, device_name=device_name, app_version="1.0.22"))
            s.send_json(jsonmsg.widget_info_req())
            return
        if m.msg_id == "date-time-req":
            s.send_json(jsonmsg.date_time_rsp())
        elif m.msg_id == "info" and m.value("model-name"):
            s.send_json(jsonmsg.info_ack(app_version="1.0.22"))
        elif m.msg_id == "config-info":
            s.send_json(jsonmsg.config_info_ack())
            done["config"] = True
        elif m.msg_id == "widget-info-rsp" and done["config"] and then and not done["then"]:
            done["then"] = True
            time.sleep(settle)
            then(s)
    return handler


def run(sock: socket.socket, camera_addr: str, host_addr: str, seconds: float, log=print, on_json=None) -> HostSession:
    s = HostSession(sock, camera_addr, host_addr, log=log, on_json=on_json)
    end = time.monotonic() + seconds
    while time.monotonic() < end and s.pump(1.0):
        pass
    return s
