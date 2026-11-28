"""Command line: python3 -m g360 [--via-adb SERIAL | --direct] [--ip IP] <command> ...

Native: `sudo python3 tools/native_session.py --then "<command> ..."` brings up Bluetooth and Wi-Fi Direct
and runs this with --direct. Phone relay: connect a phone running Samsung's Gear 360 app to the camera and pass
--via-adb <serial>; the phone only relays HTTP, all control logic runs here.

  info                         enter live-view mode and print the camera's capabilities + stream URLs
  live --view                  stitched 360° live view: drag to turn the sphere, wheel to zoom, V for views
  live --play                  show the live view in a window (raw dual fisheye)
  live --out FILE.mp4 [--seconds]  save the live view (HEVC, dual fisheye, no re-encode)
  live --raw FILE [--seconds]  save Samsung's untouched TTTS stream
  live --replay RAW --play     play a saved raw stream (no phone or camera needed; --view for 360°)
  view [FILE]                  360° viewer for the camera's photos/videos or a saved stream (no camera needed)
  install-desktop              add the g360 icon and "Gear 360 Viewer" / "Gear 360 Live" to the app menu
  live --v4l2 /dev/video10     publish the live view as a virtual webcam (OBS)
  shoot                        take a photo
  record --seconds N           record a video on the camera's card, print the file URL
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time

from . import stream
from .transport import AdbCurlTransport, DirectTransport
from .upnp import DEFAULT_IP, Camera


class _Prefixed(io.RawIOBase):
    """A stream that first replays bytes already read from `rest`, then continues reading `rest`."""

    def __init__(self, head: bytes, rest):
        self._head, self._rest = head, rest

    def readable(self):
        return True

    def read(self, n=-1):
        if self._head:
            out, self._head = (self._head, b"") if n < 0 or n >= len(self._head) else (self._head[:n], self._head[n:])
            return out
        return self._rest.read(n)


def _first_bytes_or_none(src, n: int = 12):
    """Read the first bytes of a stream; None if it ends at once, else a stream that still yields them."""
    head = b""
    while len(head) < n:
        chunk = src.read(n - len(head))
        if not chunk:
            break
        head += chunk
    return _Prefixed(head, src) if head else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g360", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--via-adb", metavar="SERIAL", help="relay HTTP through this adb device (phone connected to the camera)")
    g.add_argument("--direct", action="store_true", help="this PC is on the camera's Wi-Fi Direct network")
    ap.add_argument("--ip", default=DEFAULT_IP, help=f"camera IP (default {DEFAULT_IP})")
    ap.add_argument("--wait", type=float, default=90.0, metavar="SECONDS",
                    help="wait up to this long for the camera to become reachable (default 90)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    lv = sub.add_parser("live")
    lv.add_argument("--quality", default="high", choices=["high", "middle", "low", "recording", "gearvr"])
    lv.add_argument("--replay", metavar="RAW_FILE",
                    help="use a saved raw stream (from --raw) instead of the camera; no phone or camera needed")
    out = lv.add_mutually_exclusive_group(required=True)
    out.add_argument("--view", action="store_true", help="stitched, interactive 360° window")
    out.add_argument("--play", action="store_true", help="raw dual-fisheye window (ffplay)")
    out.add_argument("--out", metavar="FILE", help=".mp4/.mkv: remuxed HEVC (no re-encode)")
    out.add_argument("--raw", metavar="FILE", help="save Samsung's untouched TTTS stream")
    out.add_argument("--v4l2", metavar="DEVICE")
    lv.add_argument("--seconds", type=float, default=10.0, help="with --out / --raw")
    vw = sub.add_parser("view")
    vw.add_argument("file", nargs="?",
                    help="dual-fisheye photo/video from the camera, .hevc, or a raw stream from live --raw "
                         "(omitted: choose one in a dialog)")
    sub.add_parser("install-desktop")
    sub.add_parser("shoot")
    rc = sub.add_parser("record")
    rc.add_argument("--seconds", type=float, default=10.0)
    a = ap.parse_args(argv)

    log = lambda m: print(m, file=sys.stderr)
    if a.cmd == "install-desktop":
        from . import icon
        return icon.install_desktop(log)
    if a.cmd == "view" or (a.cmd == "live" and a.replay and a.view):
        from . import viewer
        path = a.replay if a.cmd == "live" else a.file
        if not path:
            r = subprocess.run(["zenity", "--file-selection", "--title=Open a Gear 360 photo or video",
                                "--file-filter=Gear 360 | *.jpg *.JPG *.jpeg *.mp4 *.MP4 *.hevc *.bin",
                                "--file-filter=All files | *"], capture_output=True, text=True)
            path = r.stdout.strip()
            if not path:
                return 1
        return viewer.view_file(path, log=log)
    if a.cmd == "live" and a.replay:
        src = open(a.replay, "rb")
        print(f"replaying {a.replay}", file=sys.stderr)
        if a.play:
            return stream.play_looped(src)
        if a.v4l2:
            return stream.to_v4l2(src, a.v4l2)
        if a.out:
            return stream.to_file(src, a.out, a.seconds)
        ap.error("--replay works with --play, --out or --v4l2")
    if not (a.via_adb or a.direct):
        ap.error("choose a transport: --via-adb SERIAL (phone relay) or --direct")
    t = AdbCurlTransport(a.via_adb) if a.via_adb else DirectTransport()
    cam = Camera(t, a.ip)
    if not cam.wait_ready(a.wait, log=lambda m: print(m, file=sys.stderr)):
        print("g360: camera not ready (no Wi-Fi link, or it answers 503 because no Bluetooth session owns it)",
              file=sys.stderr)
        return 2
    print("camera: ready", file=sys.stderr)

    if a.cmd == "info":
        caps = cam.enter_live_view()
        print(json.dumps({"stream_urls": caps.stream_urls,
                          "modes": caps.supports("DialModeValue"),
                          "video": caps.supports("DualMovieResolutionValue"),
                          "photo": caps.supports("DualResolutionValue"),
                          "stream_sizes": caps.raw.get("StreamingInfo"),
                          "remaining_shots": caps.raw.get("AVAILSHOTS"),
                          "remaining_rec_s": caps.raw.get("RemainRecTime")}, indent=2))
    elif a.cmd == "live":
        src = None
        for attempt in range(1, 4):
            caps = cam.enter_live_view()
            url = caps.stream_urls.get(a.quality) or cam.stream_url(a.quality)
            print(f"stream: {url} (attempt {attempt})", file=sys.stderr)
            src = _first_bytes_or_none(cam.open_stream(url))
            if src:
                break
            print("stream: camera sent no data (another viewer, or Samsung's app switched the camera "
                  "back to gallery mode); retrying", file=sys.stderr)
            time.sleep(2)
        if not src:
            print("g360: no live stream. In Samsung's app use Gallery -> PHONE tab (not Camera, not GEAR 360).",
                  file=sys.stderr)
            return 3
        if a.view:
            from . import viewer
            return viewer.view_stream(src, log=log)
        if a.play:
            return stream.play(src, log=log)
        if a.v4l2:
            return stream.to_v4l2(src, a.v4l2)
        if a.raw:
            print(f"wrote {stream.record_raw(src, a.raw, a.seconds)} bytes to {a.raw}")
            return 0
        rc = stream.to_file(src, a.out, a.seconds)
        print(f"wrote {a.out}" if rc == 0 else f"ffmpeg exited {rc}")
        return rc
    elif a.cmd == "shoot":
        cam.enter_live_view()
        print(json.dumps(cam.shutter(), indent=2))
    elif a.cmd == "record":
        cam.enter_live_view()
        print(json.dumps(cam.start_record(), indent=2))
        time.sleep(a.seconds)
        print(json.dumps(cam.stop_record(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
