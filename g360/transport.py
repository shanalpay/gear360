"""HTTP transports for talking to the camera's UPnP (7676) and media (7679 / 9001) servers.

DirectTransport  – the PC itself is on the camera's Wi-Fi Direct network (the end goal).
AdbCurlTransport – relays through an Android phone that is connected to the camera, using the phone's
                   built-in curl over adb. Lets the Linux app drive the camera before native
                   Bluetooth/Wi-Fi Direct bring-up exists.
"""
from __future__ import annotations

import base64
import http.client
import shlex
import subprocess
from dataclasses import dataclass
from typing import IO
from urllib.parse import urlsplit


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport:
    def request(self, method: str, url: str, headers: dict[str, str] | None = None,
                body: bytes | None = None, timeout: float = 8.0) -> Response:
        raise NotImplementedError

    def open_stream(self, url: str, headers: dict[str, str] | None = None) -> IO[bytes]:
        """A readable binary stream of the response body (for live view / downloads)."""
        raise NotImplementedError


class DirectTransport(Transport):
    def request(self, method, url, headers=None, body=None, timeout=8.0):
        u = urlsplit(url)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        try:
            conn.request(method, u.path + (f"?{u.query}" if u.query else ""), body=body, headers=headers or {})
            r = conn.getresponse()
            return Response(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
        finally:
            conn.close()

    def open_stream(self, url, headers=None):
        u = urlsplit(url)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=15)
        conn.request("GET", u.path, headers=headers or {})
        r = conn.getresponse()
        if r.status not in (200, 206):
            raise ConnectionError(f"GET {url} -> HTTP {r.status}")
        return r  # HTTPResponse is file-like


class AdbCurlTransport(Transport):
    def __init__(self, serial: str, adb: str = "adb"):
        self.serial, self.adb = serial, adb

    def _remote(self, args: list[str], stdin_b64: str | None = None) -> str:
        cmd = " ".join(shlex.quote(a) for a in args)
        if stdin_b64 is not None:
            cmd = f"echo {stdin_b64} | base64 -d | {cmd}"
        return cmd

    def request(self, method, url, headers=None, body=None, timeout=8.0):
        args = ["curl", "-s", "-i", "-m", str(int(timeout)), "-X", method]
        for k, v in (headers or {}).items():
            args += ["-H", f"{k}: {v}"]
        b64 = None
        if body is not None:
            args += ["--data-binary", "@-"]
            b64 = base64.b64encode(body).decode()
        args.append(url)
        out = subprocess.run([self.adb, "-s", self.serial, "exec-out", self._remote(args, b64)],
                             capture_output=True, timeout=timeout + 10).stdout
        return _parse_raw_http(out)

    def open_stream(self, url, headers=None):
        args = ["curl", "-s", "-N"]
        for k, v in (headers or {}).items():
            args += ["-H", f"{k}: {v}"]
        p = subprocess.Popen([self.adb, "-s", self.serial, "exec-out", self._remote(args + [url])],
                             stdout=subprocess.PIPE)
        return p.stdout


def _parse_raw_http(raw: bytes) -> Response:
    if not raw:
        raise ConnectionError("no response (camera unreachable from the relay?)")
    # curl -i may print several header blocks (e.g. "100 Continue"); use the last one before the body.
    head, sep, body = raw.partition(b"\r\n\r\n")
    while sep and body.startswith(b"HTTP/"):
        head, sep, body = body.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise ConnectionError(f"unparseable HTTP response: {raw[:80]!r}")
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return Response(status, headers, body)
