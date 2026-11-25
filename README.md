# g360 — Linux software for the Samsung Gear 360 (2016, SM-C200)

Unofficial Linux control, live-view and gallery software for the original Samsung Gear 360 (SM-C200)
360° camera, whose Android app is no longer distributed. Built by observing how the camera talks to
Samsung's app; no Samsung code is included. Not affiliated with or endorsed by Samsung.

> **Status: work in progress.** Camera control and the live stream work today, but only when a phone
> running Samsung's app holds the Bluetooth link and relays HTTP for us over `adb`. Native Bluetooth
> (Samsung Accessory Protocol) and Wi-Fi Direct on Linux are being built next. Not supported: the
> 2017 Gear 360 (SM-R210).

## What works
- Switch the camera into live view and read its capabilities (resolutions, modes, EV/ISO/WB ranges, stream URLs)
- Demux the camera's live stream (Samsung "TTTS" container → HEVC, 2560×1280, 29.97 fps, dual fisheye)
- Show it in a window, save it as MP4 without re-encoding, or publish it as a virtual webcam (v4l2loopback) for OBS
- Take a photo and start/stop video recording (implemented from captured traffic)

## Planned
- Native Bluetooth pairing and session (Samsung Accessory Protocol over BlueZ)
- Wi-Fi Direct with the PC as group owner (wpa_supplicant), so no phone is needed
- Gallery: list, download and delete photos/videos over Wi-Fi (USB/MTP download already works with any MTP tool)
- GPU stitching of the dual-fisheye picture into equirectangular 360° video, with metadata for YouTube 360

## Requirements
- Linux, Python 3.10+ (standard library only), `ffmpeg`/`ffplay`
- For now: an Android phone (Android 9 or earlier works; Android 10+ randomizes the Wi-Fi Direct address and the
  camera refuses it) with Samsung's Gear 360 app, connected to the PC with USB debugging enabled
- Optional: `v4l2loopback` for the virtual webcam
- For the native Bluetooth work in progress: `pip install unicorn pyelftools` and Samsung's `libwsm2.so` (arm64)
  from the SAccessoryService APK, pointed to by `G360_LIBWSM` (not included here)

## Usage (phone relay mode)
1. In Samsung's app, connect to the camera, open **Camera** until the preview appears, then switch to **Gallery**
   (the camera streams to one viewer at a time).
2. On the PC:
   ```bash
   alias g360='python3 -m g360 --via-adb <phone serial from `adb devices`>'
   g360 info                                  # capabilities and stream URLs
   g360 live --play                           # live window (dual fisheye)
   g360 live --out clip.mp4 --seconds 10      # save live view
   sudo modprobe v4l2loopback video_nr=10 card_label="Gear 360" exclusive_caps=1
   g360 live --v4l2 /dev/video10              # virtual webcam for OBS
   g360 shoot                                 # take a photo
   g360 record --seconds 5                    # record to the camera's card
   ```

## How the camera works
Bluetooth (Samsung Accessory Protocol, JSON messages) pairs, authenticates and wakes the camera's Wi-Fi Direct;
the camera then serves UPnP/SOAP control on port 7676, a live HEVC stream on 7679 and files on 9001.
The camera only serves clients while the Bluetooth session stays open. Details, evidence and open questions are
in [NOTES.md](NOTES.md).

## Layout
| path | contents |
|---|---|
| `g360/jsonmsg.py` | JSON messages carried over Bluetooth |
| `g360/upnp.py` | SOAP control: live view, shutter, recording, capabilities |
| `g360/ttts.py` | demuxer for the live-stream container |
| `g360/stream.py` | window / file / virtual-webcam outputs via ffmpeg |
| `g360/transport.py` | HTTP directly or relayed through a phone's `curl` over adb |
| `g360/bt.py` | Bluetooth identifiers and message types (protocol client in progress) |
| `g360/elfemu.py` | minimal AArch64 shared-library runner (Unicorn) |
| `g360/wsm.py` | pairing authentication computed by Samsung's libwsm2 under emulation |
| `tools/streamtest.py` | capture and analyse a few seconds of live stream through a phone |

## License
MIT — see [LICENSE](LICENSE). "Samsung" and "Gear 360" are trademarks of Samsung Electronics, used here only
to identify the hardware this software works with.
