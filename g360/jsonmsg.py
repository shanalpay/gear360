"""JSON messages exchanged with the Gear 360 (SM-C200) over its Samsung Accessory Protocol channel.

Every message is wrapped in a JSON-Schema-looking envelope:

    {"title": "...", "description": "...", "type": "object",
     "properties": {"msgId": "<id>", "<field>": {"type": "string", "description": <value>}, ...}}

Field values live in "description"; enumerated fields use {"enum": <value>, "description": <argument>}
instead (e.g. cmd-req action: {"enum": "execute", "description": "liveview"}). Formats observed in
captures/session1-json.txt (app 1.0.22) and session2-logcat.txt (app 1.4.00.9).
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any

# Titles/descriptions exactly as Samsung's app sends them (the camera may not care, but match anyway).
_ENVELOPES = {
    "info": ("Phone Device information Message", "Message structure in JSON for Phone Device information"),
    "info-rsp": ("Camera Device info response Message", "Message structure in JSON for Camera Device info response"),
    "widget-info-req": ("Widget info request Message", "Message structure in JSON for Widget Info request"),
    "date-time-rsp": ("Date-Time response Message", "Message structure in JSON for Date-Time response"),
    "config-info": ("Camera Config information Message", "Message structure in JSON for camera config info response"),
    "cmd-req": ("Command request Message", "Message structure in JSON for Command request"),
}

# cmd-req actions and their argument ("description") values, from BTJsonSerializableMsgId.
ACTIONS = ("execute", "get", "set", "dismiss", "disconn")
COMMANDS = (
    "liveview", "rvf", "mobilelink", "remote-shot", "remote-release", "config", "format", "reset",
    "fw-download", "autoshare", "q-autoshare", "autotransfer", "selectivepush", "as-select", "app",
    "btml", "power-save-off", "pro-suggest", "bt-off", "timeout",
)

# cmd-rsp r-codes.
R_CODES = {
    100: "success", 101: "bad JSON format", 102: "wrong command", 103: "fail", 104: "memory full",
    105: "already executed", 200: "unknown error", 201: "battery low", 204: "card locked",
    206: "file saving", 208: "no card", 209: "no photo", 210: "HDMI error", 211: "USB error",
    212: "movie error", 213: "memory full", 214: "lens check", 216: "DCF full",
    218: "not supported during FOTA",
}


def _str(v: Any) -> dict:
    return {"type": "string", "description": v}


def _enum(v: Any, desc: Any) -> dict:
    return {"enum": v, "description": desc}


def envelope(msg_id: str, props: dict | None = None, kind: str | None = None) -> dict:
    title, desc = _ENVELOPES.get(kind or msg_id, (f"{msg_id} Message", f"Message structure in JSON for {msg_id}"))
    return {"title": title, "description": desc, "type": "object",
            "properties": {"msgId": msg_id, **(props or {})}}


def encode(msg: dict) -> bytes:
    # Samsung's Java JSON writer emits compact JSON and escapes "/" as "\/"; mimic that.
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).replace("/", "\\/").encode("utf-8")


def decode(data: bytes | str) -> "Message":
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    obj = json.loads(data)
    return Message(obj.get("properties", {}).get("msgId", ""), obj)


@dataclass
class Message:
    msg_id: str
    raw: dict = field(repr=False)

    @property
    def props(self) -> dict:
        return self.raw.get("properties", {})

    def value(self, key: str, default: Any = None) -> Any:
        """The payload of a field: its "description" (or "enum" for enumerated fields)."""
        v = self.props.get(key, default)
        if isinstance(v, dict):
            return v.get("enum", v.get("description", default)) if "enum" in v else v.get("description", default)
        return v

    def flat(self) -> dict:
        """{field: value} for every field except msgId — handy for logging and tests."""
        return {k: self.value(k) for k in self.props if k != "msgId"}

    # cmd-req / cmd-rsp helpers
    @property
    def action(self) -> tuple[Any, Any] | None:
        a = self.props.get("action")
        return (a.get("enum"), a.get("description")) if isinstance(a, dict) else None

    @property
    def result(self) -> tuple[Any, Any] | None:
        r = self.props.get("result")
        return (r.get("enum"), r.get("description")) if isinstance(r, dict) else None

    @property
    def r_code(self) -> int | None:
        v = self.value("r-code")
        return int(v) if v is not None else None


# ---- builders for the messages the host (phone role) sends -------------------------------------

def phone_info(p2p_mac: str, device_name: str = "Linux", app_version: str = "1.4.00.9",
               go_5g: bool = False, ch_negotiation: str | None = None, op_mode: str = "user") -> dict:
    """Host introduction. `p2p_mac` is the phone's Wi-Fi MAC (`wifi-mac-address`).

    App 1.4.00.9 (the version that connects, session 2) sends
    `"wifi-direct":{"enum":"true","ch-negotiation-wa":{"description":"5G-GO"}}` plus `op-mode`: pass
    ch_negotiation="5G-GO" (or "2.4G-GO", the app's KDDI variant). Without it the old 1.0.22 form
    (`"wifi-direct":{"enum":"true","description":"100"}`, no op-mode) is produced."""
    if ch_negotiation:
        wd = {"enum": "true", "ch-negotiation-wa": {"description": ch_negotiation}}
    else:
        wd = _enum("true", "5G-GO") if go_5g else _enum("true", "100")
    props = {"wifi-direct": wd, "wifi-mac-address": _str(p2p_mac.upper()),
             "device-name": _str(device_name), "app-version": _str(app_version)}
    if ch_negotiation:
        props["op-mode"] = _str(op_mode)
    return envelope("info", props)


def info_ack(app_version: str = "1.4.00.9", ch_negotiation: str | None = None, op_mode: str = "user") -> dict:
    """Ack of the camera's `info`. App 1.4.00.9: `"result":{"enum":"success","ch-negotiation-wa":{...}}` + op-mode."""
    if ch_negotiation:
        return envelope("info", {"result": {"enum": "success", "ch-negotiation-wa": {"description": ch_negotiation}},
                                 "app-version": _str(app_version), "op-mode": _str(op_mode)}, kind="info-rsp")
    return envelope("info", {"result": _enum("success", "100"), "app-version": _str(app_version)}, kind="info-rsp")


def widget_info_req() -> dict:
    return envelope("widget-info-req")


def date_time_rsp(now: _dt.datetime | None = None) -> dict:
    now = (now or _dt.datetime.now()).astimezone()
    off = now.utcoffset() or _dt.timedelta(0)
    mins = int(off.total_seconds() // 60)
    region = f"{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
    summer = bool(now.dst())
    # Both captures were taken in late November (standard time, no DST in effect) yet sent summer "true"
    # alongside the standard offset, so the flag's meaning is unclear (maybe "zone observes DST").
    # Unverified guess: report the standard offset and flag DST separately.
    if summer:
        std = mins - int(now.dst().total_seconds() // 60)
        region = f"{'+' if std >= 0 else '-'}{abs(std) // 60:02d}:{abs(std) % 60:02d}"
    return envelope("date-time-rsp", {"date": _str(now.strftime("%Y/%m/%d")), "time": _str(now.strftime("%H:%M:%S")),
                                      "region": _str(region), "summer": _str("true" if summer else "false")})


def config_info_ack() -> dict:
    return envelope("config-info", {"result": _enum("success", "100")})


def cmd_req(action: str, command: str, items: dict | None = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    a = _enum(action, command)
    if items:
        a["items"] = {k: {"description": v} for k, v in items.items()}
    return envelope("cmd-req", {"action": a})
