# g360 — Linux software for the Samsung Gear 360 (2016, SM-C200)

Unofficial Linux control, live-view and gallery software for the original Samsung Gear 360 (SM-C200)
360° camera, whose Android app is no longer distributed. Built by observing how the camera talks to
Samsung's app; no Samsung code is included. Not affiliated with or endorsed by Samsung.

> **Status: work in progress.** Fully native: this PC pairs with the camera over Bluetooth, brings up
> Wi-Fi Direct as group owner and controls the camera with no phone involved (about 15 s from power-on
> to live view). The older phone-relay mode still works. Not supported: the 2017 Gear 360 (SM-R210).

## What works
- Native Bluetooth pairing and session (Samsung Accessory Protocol over BlueZ)
- Wi-Fi Direct with the PC as group owner (a private, patched wpa_supplicant), so no phone is needed
- Switch the camera into live view and read its capabilities (resolutions, modes, EV/ISO/WB ranges, stream URLs)
- Demux the camera's live stream (Samsung "TTTS" container → HEVC, 2560×1280, 29.97 fps, dual fisheye)
- **360° viewer**: stitched on the GPU live (about 0.5 s behind the camera), turn the sphere with the mouse
  like on the phone (drag, flick, roll, zoom), little planet, panorama; also for the camera's photos and videos
- Show the raw picture in a window, save it as MP4 without re-encoding, or publish it as a virtual webcam
  (v4l2loopback) for OBS
- Take a photo and start/stop video recording (implemented from captured traffic)

## Planned
- One `g360` command that does the native session itself (today: `tools/native_session.py`)
- Gallery: list, download and delete photos/videos over Wi-Fi (USB/MTP download already works with any MTP tool)
- Export stitched equirectangular 360° video/photos with metadata for YouTube 360

## Requirements
- Linux, Python 3.10+, `ffmpeg`/`ffplay`
- Native mode: BlueZ (with `dbus-python` and PyGObject), a Wi-Fi card that supports P2P GO (tested: Intel AX211),
  `dnsmasq`, `iw`, `ethtool`, NetworkManager, root for the session; `pip install unicorn pyelftools` and
  Samsung's `libwsm2.so` (arm64) from the SAccessoryService APK (not included here): put the extracted APK under
  the project's `re/` or `apk/` folder, or the library at `~/.config/g360/libwsm2.so`; it is found automatically
  (`G360_LIBWSM` overrides).
  The Wi-Fi interface is taken from NetworkManager during the session (Wi-Fi internet drops; Ethernet stays).
- Phone relay mode instead: an Android phone (Android 9 or earlier; Android 10+ randomizes the Wi-Fi Direct
  address and the camera refuses it) with Samsung's Gear 360 app, connected over USB with debugging enabled
- 360° viewer: OpenGL 3.3 and `.venv/bin/pip install moderngl glfw` (picked up from the project's `.venv`)
- Optional: `v4l2loopback` for the virtual webcam

## Usage (native)
```bash
sh tools/build_wpa_supplicant.sh        # once, no root: builds tmp/wpa-build/wpa_supplicant
python3 tools/pair.py                   # once: camera in pairing mode (hold Menu -> "Gear 360 Manager" -> Record)
sudo python3 tools/native_session.py --view            # 360° live view; the session ends when it closes
sudo python3 tools/native_session.py --play            # raw dual-fisheye window
sudo python3 tools/native_session.py --then "live --out clip.mp4 --seconds 30"
sudo python3 tools/native_session.py --then "shoot"
```
`--then` runs any `g360` command below (as you, not root) while the Bluetooth and Wi-Fi Direct session stays up.

### 360° viewer
`python3 -m g360 view PHOTO.jpg|VIDEO.mp4` works offline for the camera's own files (and `live --raw` captures).
Left-drag turns the sphere in any direction (the picture follows the pointer; flick to spin), right-drag rolls,
the wheel zooms; on a touchpad pinch/spread zooms, on a touchscreen one finger turns and two pinch. `V` cycles perspective / little planet / panorama / raw, `L` keeps the horizon level, `R` resets,
`F` fullscreen, `S` screenshot, `Space` pause. Lens calibration: `[` `]` field of view, `Ctrl+arrows` and `,` `.`
back-lens alignment, `K` shows the blend band, `Ctrl+S` saves to `~/.config/g360/lens.json`.

`python3 -m g360 install-desktop` adds the g360 icon and two app-menu entries:
**Gear 360 Viewer** (also under Open With for JPEG/MP4 files) and **Gear 360 Live** (opens a terminal for sudo).

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
| `g360/viewer.py` | GPU-stitched interactive 360° viewer (moderngl + glfw) |
| `g360/xgesture.py` | touchpad pinch and touchscreen input on X11 (XInput 2.2/2.4), which GLFW lacks |
| `g360/icon.py` | the app icon (drawn by a shader) and the app-menu install |
| `g360/transport.py` | HTTP directly or relayed through a phone's `curl` over adb |
| `g360/bt.py` | Bluetooth identifiers and message types (protocol client in progress) |
| `g360/elfemu.py` | minimal AArch64 shared-library runner (Unicorn) |
| `g360/wsm.py` | pairing authentication computed by Samsung's libwsm2 under emulation |
| `g360/sap.py`, `g360/bluez.py` | Samsung Accessory Protocol session over BlueZ RFCOMM |
| `g360/p2p.py`, `g360/wpactrl.py` | private wpa_supplicant for Wi-Fi Direct and its control-socket client |
| `tools/native_session.py` | native session: Bluetooth + Wi-Fi Direct + UPnP, optionally running a `g360` command |
| `tools/build_wpa_supplicant.sh` | builds the patched wpa_supplicant 2.10 into `tmp/` |
| `tools/pair.py` | Bluetooth pairing with the camera |
| `tools/streamtest.py` | capture and analyse a few seconds of live stream through a phone |

## License
MIT — see [LICENSE](LICENSE). "Samsung" and "Gear 360" are trademarks of Samsung Electronics, used here only
to identify the hardware this software works with.
