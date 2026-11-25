"""UPnP/SOAP control of the Gear 360 (SM-C200) once it is on the Wi-Fi Direct network.

All actions are SOAP POSTs to the ContentDirectory control URL (observed `/smp_4_` on port 7676).
Action names and argument tags are Samsung's, including their misspellings ("GetInfomation",
"X_TimerlOperation", "QualityMiddelUrl"). Sequences observed in captures/session2-* (see NOTES.md).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

from .transport import Transport

SERVICE = "urn:schemas-upnp-org:service:ContentDirectory:1"
DEFAULT_IP = "192.168.49.10"
UPNP_PORT, MEDIA_PORT, CARD_PORT = 7676, 7679, 9001


class SoapError(RuntimeError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def xml_to_obj(el: ET.Element) -> Any:
    """Convert Samsung's response XML into dicts/lists.

    <X><S>a</S><S>b</S></X> -> ["a", "b"]; repeated tags -> list; leaf -> text.
    """
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    grouped: dict[str, list[Any]] = {}
    for k in kids:
        grouped.setdefault(_local(k.tag), []).append(xml_to_obj(k))
    # <S> children are always a list (one supported value each), even when there is only one
    if set(grouped) == {"S"}:
        return grouped["S"]
    return {name: (vals if len(vals) > 1 or name == "S" else vals[0]) for name, vals in grouped.items()}


@dataclass
class Capabilities:
    raw: dict
    stream_urls: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, result: dict) -> "Capabilities":
        info = result.get("GETINFORMATIONRESULT", result)
        urls = {}
        for k, v in (result.get("StreamUrl") or info.get("StreamUrl") or {}).items():
            m = re.match(r"Quality(\w+?)Url$", k)
            if m:  # QualityHighUrl, QualityMiddelUrl (sic), QualityLowUrl, QualityRecUrl, QualityGearVRUrl
                urls[{"Middel": "middle", "Rec": "recording", "GearVR": "gearvr"}.get(m.group(1), m.group(1).lower())] = v
        return cls(info, urls)

    def supports(self, key: str) -> list[str]:
        v = self.raw.get(key, {})
        s = v.get("Supports") if isinstance(v, dict) else None
        if isinstance(s, dict):  # <S>..</S><Ratio>..</Ratio> pairs, e.g. DualMovieResolutionValue
            s = s.get("S", [])
        return s if isinstance(s, list) else ([] if s in (None, "NA") else [s])

    def default(self, key: str) -> str | None:
        v = self.raw.get(key, {})
        return v.get("Default") if isinstance(v, dict) else None


class Camera:
    # The camera's HTTP servers answer 503 unless the User-Agent is "SEC_RVF_ML_" followed by something
    # (the app sends its Wi-Fi MAC; any non-empty value works — tested 2026-11-25 on fw 0.83).
    def __init__(self, transport: Transport, ip: str = DEFAULT_IP, control_path: str = "/smp_4_",
                 client_id: str = "00:00:00:00:00:01"):
        self.t, self.ip, self.control_path = transport, ip, control_path
        self.user_agent = f"SEC_RVF_ML_{client_id}"

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent}

    # The only live-stream request seen to succeed used the official app's exact headers (MyMediaExtractor):
    # the 7676-style "SEC_RVF_ML_..." agent is not what the app sends to port 7679.
    STREAM_HEADERS = {"User-Agent": "Android Linux", "Connection": "Keep-Alive"}

    def open_stream(self, url: str):
        return self.t.open_stream(url, dict(self.STREAM_HEADERS))

    @property
    def control_url(self) -> str:
        return f"http://{self.ip}:{UPNP_PORT}{self.control_path}"

    # ---- generic SOAP --------------------------------------------------------------------------
    def action(self, name: str, args: dict[str, str] | None = None, timeout: float = 8.0) -> dict:
        inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in (args or {}).items())
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{name} xmlns:u="{SERVICE}">{inner}</u:{name}></s:Body></s:Envelope>').encode()
        r = self.t.request("POST", self.control_url, {
            **self.headers,
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{SERVICE}#{name}"',
        }, body, timeout)
        if r.status != 200:
            raise SoapError(f"{name}: HTTP {r.status}: {r.body[:200]!r}")
        return parse_soap_response(r.body, name)

    # ---- observed actions ----------------------------------------------------------------------
    def set_operation_state(self, state: str) -> dict:
        """state: 'changeToRVF' (remote viewfinder / live view) or 'changeToML' (MobileLink gallery)."""
        return self.action("SetOperationState", {"StateEvent": state})

    def controller_status(self, status: str = "Idle", streaming: str = "Stop", change: str = "NA") -> dict:
        """Observed: (Idle,Stop,NA) poll; (Change,Stop,RVF|ML) before switching modes."""
        return self.action("X_ControllerStatus", {"StatusValue": status, "StreamingOperationValue": streaming,
                                                   "ChangeStatusValue": change})

    def get_information(self) -> Capabilities:
        return Capabilities.parse(self.action("GetInfomation", {"GPSINFO": "UNKNOWN"}))

    def set_stream_quality(self, quality: str = "High") -> dict:
        return self.action("SetStreamQuality", {"Quality": quality})

    def shutter(self) -> dict:
        """Take a photo (the app's shutter sends X_TimerlOperation Start, honouring the self-timer)."""
        return self.action("X_TimerlOperation", {"OperationValue": "Start"})

    def start_record(self) -> dict:
        return self.action("StartRecord")

    def stop_record(self) -> dict:
        """Returns RemainRecTime, AVAILSHOTS, FileURL (http://<cam>:9001/DCIM/...MP4), FileCount, ..."""
        return self.action("StopRecord")

    # ---- convenience ---------------------------------------------------------------------------
    def enter_live_view(self) -> Capabilities:
        """Replays the app's sequence: announce the change, switch to RVF, read capabilities."""
        self.controller_status("Change", "Stop", "RVF")
        self.set_operation_state("changeToRVF")
        return self.get_information()

    def stream_url(self, quality: str = "high") -> str:
        return f"http://{self.ip}:{MEDIA_PORT}/livestream_{quality}.avi"


def parse_soap_response(xml_bytes: bytes, action: str | None = None) -> dict:
    root = ET.fromstring(xml_bytes)
    body = next((e for e in root.iter() if _local(e.tag) == "Body"), None)
    if body is None or not list(body):
        raise SoapError(f"no SOAP body: {xml_bytes[:200]!r}")
    resp = list(body)[0]
    if _local(resp.tag) == "Fault":
        raise SoapError(ET.tostring(resp, encoding="unicode")[:400])
    if action and _local(resp.tag) != f"{action}Response":
        raise SoapError(f"expected {action}Response, got {_local(resp.tag)}")
    obj = xml_to_obj(resp)
    return obj if isinstance(obj, dict) else {"value": obj}
