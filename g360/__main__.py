"""Command line: python3 -m g360 [--via-adb SERIAL | --direct] [--ip IP] <command> ...

Until native Bluetooth/Wi-Fi Direct bring-up exists, connect a phone running Samsung's Gear 360 app to
the camera first and pass --via-adb <serial>; the phone only relays HTTP, all control logic runs here.

  info                         enter live-view mode and print the camera's capabilities + stream URLs
  live --play                  show the live view in a window
  live --out FILE.mp4 [--seconds]  save the live view (HEVC, dual fisheye, no re-encode)
  live --raw FILE [--seconds]  save Samsung's untouched TTTS stream
  live --v4l2 /dev/video10     publish the live view as a virtual webcam (OBS)
  shoot                        take a photo
  record --seconds N           record a video on the camera's card, print the file URL
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import stream
from .transport import AdbCurlTransport, DirectTransport
from .upnp import DEFAULT_IP, Camera


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="g360", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--via-adb", metavar="SERIAL", help="relay HTTP through this adb device (phone connected to the camera)")
    g.add_argument("--direct", action="store_true", help="this PC is on the camera's Wi-Fi Direct network")
    ap.add_argument("--ip", default=DEFAULT_IP, help=f"camera IP (default {DEFAULT_IP})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    lv = sub.add_parser("live")
    lv.add_argument("--quality", default="high", choices=["high", "middle", "low", "recording", "gearvr"])
    out = lv.add_mutually_exclusive_group(required=True)
    out.add_argument("--play", action="store_true")
    out.add_argument("--out", metavar="FILE", help=".mp4/.mkv: remuxed HEVC (no re-encode)")
    out.add_argument("--raw", metavar="FILE", help="save Samsung's untouched TTTS stream")
    out.add_argument("--v4l2", metavar="DEVICE")
    lv.add_argument("--seconds", type=float, default=10.0, help="with --out / --raw")
    sub.add_parser("shoot")
    rc = sub.add_parser("record")
    rc.add_argument("--seconds", type=float, default=10.0)
    a = ap.parse_args(argv)

    if not (a.via_adb or a.direct):
        ap.error("choose a transport: --via-adb SERIAL (phone relay) or --direct")
    t = AdbCurlTransport(a.via_adb) if a.via_adb else DirectTransport()
    cam = Camera(t, a.ip)

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
        caps = cam.enter_live_view()
        url = caps.stream_urls.get(a.quality) or cam.stream_url(a.quality)
        print(f"stream: {url}", file=sys.stderr)
        src = cam.open_stream(url)
        if a.play:
            return stream.play(src)
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
