#!/usr/bin/env python3
"""One-shot live-stream capture through an adb-connected phone that is on the camera's Wi-Fi Direct.

Switches the camera to live view with g360, then makes the phone's curl save N seconds of the stream
to /data/local/tmp (avoids adb pipe buffering), pulls it into captures/ and prints what it finds.
Usage: tools/streamtest.py --serial S [--quality high] [--seconds 8] [--no-rvf]
       (S = the phone's serial from `adb devices`)
"""
import argparse
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from g360.transport import AdbCurlTransport  # noqa: E402
from g360.upnp import Camera  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--serial", required=True, help="adb serial of the phone connected to the camera")
ap.add_argument("--quality", default="high")
ap.add_argument("--seconds", type=int, default=8)
ap.add_argument("--no-rvf", action="store_true", help="don't send the live-view SOAP sequence first")
a = ap.parse_args()


def sh(cmd: str, timeout: float = 30) -> str:
    return subprocess.run(["adb", "-s", a.serial, "shell", cmd], capture_output=True, text=True,
                          timeout=timeout).stdout.replace("\r", "")


link = sh("ip -4 addr | grep -oE 'inet 192\\.168\\.49\\.[0-9]+'").strip()
if not link:
    sys.exit("phone is not on the camera's Wi-Fi Direct network (open the app's Camera screen first)")
print("phone link:", link)

if not a.no_rvf:
    cam = Camera(AdbCurlTransport(a.serial))
    cam.controller_status("Change", "Stop", "RVF")
    cam.set_operation_state("changeToRVF")
    caps = cam.get_information()
    print("camera mode:", caps.raw.get("StreamingMode"), "| default size:", caps.raw.get("StreamingInfo", {}).get("Default"))

remote = "/data/local/tmp/g360live.bin"
url = f"http://192.168.49.10:7679/livestream_{a.quality}.avi"
out = sh(f"rm -f {remote}; curl -s -v -m {a.seconds} -A 'Android Linux' -H 'Connection: Keep-Alive' "
         f"-o {remote} {url} 2>&1 | grep -E '^[<>*]' | head -20; ls -l {remote}", timeout=a.seconds + 20)
print(out)

ts = time.strftime("%Y%m%d-%H%M%S")
local = pathlib.Path(__file__).resolve().parents[1] / "captures" / f"live-{a.quality}-{ts}.bin"
subprocess.run(["adb", "-s", a.serial, "pull", remote, str(local)], capture_output=True)
if not local.exists() or local.stat().st_size == 0:
    sys.exit("no stream data captured")
data = local.read_bytes()
print(f"saved {local} ({len(data)} bytes, {len(data) / a.seconds / 1024:.0f} KiB/s)")
print("first 64 bytes:", data[:64].hex(" "))
print("ascii:", "".join(chr(b) if 32 <= b < 127 else "." for b in data[:64]))
for magic, name in [(b"RIFF", "RIFF/AVI"), (b"\x00\x00\x00\x01", "H.264 Annex-B start code"),
                    (b"\xff\xd8\xff", "JPEG"), (b"TTTS", "Samsung TTTS header"), (b"ftyp", "MP4")]:
    i = data.find(magic)
    if i >= 0:
        print(f"  found {name} at offset {i} ({data.count(magic)} occurrences)")
