# Gear 360 (SM-C200) reverse-engineering notes

Goal: Linux-native control, file import, live view (→ v4l2loopback virtual webcam) and GPU stitching
for the 2016 Samsung Gear 360, without Samsung's discontinued Android app.

Device identifiers (MAC addresses, serial numbers) are redacted as `xx`. Samsung binaries, decompiled
sources and raw captures referenced below (`apk/`, `re/`, `captures/`) are kept locally and are not
part of this repository.

## Hardware under study
- Camera: SM-C200, firmware `SMC200GLU0AQF1` (0.83), Tizen 2.3, Linux 3.5 kernel (per ultramango/gear360reveng).
- Phones used to observe Samsung's app: Galaxy A10e SM-A102U1 (Android 10) and Galaxy S8+ SM-G955U (Android 9).
- Host: Linux PC with an Intel Wi-Fi card that supports Wi-Fi Direct (P2P), BlueZ 5.64.

## USB (stock firmware) — files only
PTP GetDeviceInfo (must use TransactionID 0 outside a session; send PTP class reset 0x66 first if gvfs
left it mid-session): 33 standard MTP ops, **no InitiateCapture, capture formats = none**.
Unknown vendor ops `0x9201`, `0x9202` (not yet probed). No webcam/UVC mode.

## Samsung apps examined
- Gear 360 Manager 1.0.22 (`com.samsung.android.samsunggear360manager`, versionCode 43), APKMirror build,
  sha256 `9b89352f289f85ee763f77302241fcfca202ea7ef7be11420f96c53e49f49d58`.
- Samsung Gear 360 1.4.00.9 (`com.samsung.android.gear360manager`) and Gear 360 Editor 2.6.27.50
  (`com.sec.android.mimage.gear360editor`, Samsung's phone-side stitcher).
- SAccessoryService 3.1.26 (bundled in the 1.0.22 APK as `assets/SAccessoryService_3.1.26_161116_User.apk`).
- All signed by "Samsung Cert" (DMC), SHA-1 `9CA5170F381919DFE0446FCDAB18B19A143B3163`, the same key as the
  phone's factory system apps → genuine. Decompiled with jadx 1.5.7.

### Gear 360 Manager layout (`com/samsung/android/samsunggear360manager/app/`)
| package | role |
|---|---|
| `btm/` | Bluetooth manager; `datatype/BTJsonSerializableMsgId.java` = JSON message catalogue |
| `cm/` | connection manager; `modemanager/ModeClient.java` (camera "mode server" TCP **7788**), `ModeServer.java` (phone-side callback server) |
| `pullservice/service/rvf/` | Remote ViewFinder = live view; `MyMediaExtractor`, `CustomOpaxDataExtractor` open **raw TCP sockets** to the camera; `VideoDecoderThread` decodes; `CustomM360/ACGY/XYZ/INTV/UUID` extractors parse Samsung metadata (likely gyro/accel) |
| `pullservice/service/mobilelink/` | MobileLink file transfer (UPnP ContentDirectory / ConnectionManager) |

### Bluetooth: Samsung Accessory Protocol (SAP), not plain RFCOMM
`res/xml/accessoryservices.xml`: app `NX360_SERVER`, profile `DI_360_2DApp`, id `/system/DI_360_2D`,
role **provider** (phone) — camera is the consumer. Transport BT. Channels **204** (high prio), **222**, **230**.
Explains why a plain `rfcomm` bind failed in gear360reveng.

### JSON commands
Request `msgId: "cmd-req"`, arguments: `liveview`, `rvf`, `remote-shot`, `remote-release`, `config`,
`mobilelink`, `btml`, `autoshare`, `q-autoshare`, `autotransfer`, `selectivepush`, `as-select`, `app`,
`format`, `fw-download`, `reset`, `power-save-off`, `pro-suggest`, `bt-off`, `timeout`.
Actions: `get`, `set`, `execute`, `disconn`, `dismiss`. Also `config-info` request/response.
Response codes: 100 success, 101 bad JSON, 102 wrong command, 103 fail, 104 memory full, 105 already executed,
200 unknown, 201 battery low, 204 card lock, 206 file saving, 208 no card, 209 no photo, 210 HDMI,
211 USB, 212 movie error, 213 memory full, 214 lens check, 216 DCF full, 218 not supported during FOTA.
Commands flow both ways: the camera sends `cmd-req {set, config, items:{mode:…}}` when its mode dial changes.

## Capture method (no root)
Developer options → Bluetooth HCI snoop log = Enabled (Samsung ignores `persist.bluetooth.btsnooplogmode`;
"Config HCI log for external: enable=0" in dumpsys is unrelated). Retrieve with `adb bugreport` →
`FS/data/log/bt/btsnoop_hci.log` (btsnoop v1, datalink 1002; present on the Android 10 phone, absent on Android 9).
The 1.4 app logs every JSON message in full under logcat tag `ACM_BT_COMMAND_FLOW`.

## Session 1 — Galaxy A10e (Android 10), app 1.0.22: Bluetooth works, Wi-Fi Direct fails

### Gotchas
- **Location must be ON** (`settings put secure location_mode 3`) or the app's `startDiscovery` silently never starts.
- The app's background receiver **cancels any discovery it didn't start** (Settings scans show nothing while it runs).
- Camera pairing mode: power on → hold Menu until "Gear 360 Manager" → press red Record; camera shows a passkey.

### Camera identity (from its `info` message)
Bluetooth name "Gear 360 (xxxx)" (last 4 hex digits of the BT MAC); Wi-Fi `B0:72:BF:xx:xx:xx`;
Wi-Fi Direct `B2:72:BF:xx:xx:xx` (Wi-Fi MAC with the locally-administered bit set), SSID "Gear 360(xx:xx:xx)",
**channel 1** (2412 MHz); model-version `C200GLU0AQF1_0.83`; board-revision 0.4.
P2P provision-discovery response carries Samsung extension `static_ip=0x2931a8c0` = 192.168.49.41
(but in session 2 the camera used 192.168.49.10, the address the phone's own PD request gave it; see below).

### SAP bring-up (logcat tags SAPeerDescriptionManager / SASecurityManager / SACapabilityManager)
RFCOMM UUID **`a49eb41e-cb06-495c-9f4f-aa80a90cdf4a`** → Peer Description (PD) v2.1, peer version 0x201 (513)
→ **SASecurityManager challenge/response** ("Send Server Challenge", "Authentication result : 105")
→ session setup ("Send SC req", default session 1023, capex session 854) → CapEx query/rsp (checksum match)
→ ATTACHED → camera requests service `/system/DI_360_2D`. First attempt detached once (0x101) then retried OK.

### JSON over channel 204
Envelope: `{"title","description","type":"object","properties":{...}}`; values live in
`{"type":..,"description":<value>}` or `{"enum":<value>,"description":<code/arg>}`.
1. PHONE `info` {wifi-direct:true(100), wifi-mac-address, device-name, app-version}
2. PHONE `widget-info-req`; CAMERA `date-time-req` → PHONE `date-time-rsp` {date "YYYY/MM/DD", time, region "±HH:MM", summer}
3. CAMERA `info` {model-name, model-version, channel, wifi-direct-mac, wifi-direct-ssid, board-revision, serial-number,
   unique-number, wifi-mac, bt-mac, bt-fota-test-url, fw-type} → PHONE `info` {result success}
4. CAMERA `config-info` {functions: 9 settings — Beep, Led indicator, Auto Power Off, Video out, Format, Reset,
   Mode (Video/Photo/Time lapse/Looping Video), timer, Looping Video Recording Time} → PHONE `config-info` {result}
5. CAMERA `widget-info-rsp`
6. PHONE `cmd-req` {action: enum **execute**, description **liveview**} → CAMERA `cmd-rsp` {result success, r-code 100}
7. (timeout) PHONE `cmd-req` {dismiss, liveview} → CAMERA `cmd-rsp` {failure, 100}

### Wi-Fi Direct failure — root cause
After `liveview execute` the phone does P2P provision discovery (PBC, config 0x80) then GO negotiation
(app: `groupOwnerIntent=7`, `wps.setup=0` PBC); camera answers **"GO Negotiation rejected: status 1"** every time.
Cause: the app tells the camera its P2P address via `info.wifi-mac-address`, derived from
`WifiInfo.getMacAddress()` (old Samsung convention: Wi-Fi MAC with the locally-administered bit set), but
**Android 10 randomizes the P2P device address**, so the camera refuses an unknown peer. Not a channel conflict
(still fails with the phone's Wi-Fi disconnected). The app also uses a hidden Samsung
`discoverPeers(channel, int, listener)` via reflection.

## Session 2 — Galaxy S8+ (Android 9), app 1.4.00.9: everything works
Android 9 keeps a fixed P2P address derived the old way from the Wi-Fi MAC, so the camera accepts the phone.

### Flow
1. BT/SAP as in session 1; phone `info` now also advertises `"5G-GO"` and its real Wi-Fi MAC.
   Camera `widget-info-rsp` carries live status (record-state, recordable-time, battery, …).
2. PHONE `cmd-req {execute, mobilelink}` → CAMERA `cmd-rsp {success,100}` → CAMERA sends
   `device-desc-url` = `http://192.168.49.10:7676/smp_2_` (UPnP device description).
3. Wi-Fi Direct: **phone is GO 192.168.49.1, camera client 192.168.49.10**, channel 1.
4. UPnP (CyberGarage lib, user-agent `SEC_RVF_ML_<wifi mac>`): all control = SOAP **POST `/smp_4_` on 7676**,
   service `urn:schemas-upnp-org:service:ContentDirectory:1`. Eventing (`/evetSub` sic) pushes state vars
   `RVF_BATTERY_LEVEL_VALUE, RVF_REMAIN_REC_TIME_VALUE, RVF_REMAIN_SHOT_COUNT_VALUE, RVF_LAST_PHOTO_URL_VALUE,
   RVF_BUTTON_ACTION, RVF_CHARGING_STATUS, RVF_MOVIE_RECORD_TIME, RVF_MOVIE_TOTAL_RECORD_TIME, RVF_EV_SETUP_VALUE,
   RVF_DEVICE_ERROR_STATUS, RVF_RECORD_ERROR_STATUS`.
5. Gallery (MobileLink/ML): `SetOperationState{StateEvent=changeToML}`, `Browse`, `X_ControllerStatus`;
   media over HTTP **7679** (`/smpNNN….mp4`, `/smpthumbvideo….jpg`, `/smpscreenvideo….jpg`) and direct card
   paths on **9001** (`/DCIM/100PHOTO/360_NNNN.MP4`, `…MP4_TH` thumbnails).
6. Live view (RVF): `X_ControllerStatus{Change,Stop,RVF}` → `SetOperationState{changeToRVF}` →
   `GetInfomation{GPSINFO=UNKNOWN}` (sic) → response lists capabilities + stream URLs:
   **`http://192.168.49.10:7679/livestream_{high,middle,low,recording,gearvr}.avi`**.
   Shutter: `X_TimerlOperation{OperationValue=Start}` (sic). Video: `SetStreamQuality{Quality=High}`,
   `StartRecord` → `{RemainRecTime}`, `StopRecord` → `{RemainRecTime, AVAILSHOTS, FileURL=http://…:9001/DCIM/100PHOTO/360_NNNN.MP4, FileCount, …}`.
   During recording the viewer switches to `livestream_recording.avi`.

### GetInfomation capabilities (fw 0.83)
Dial modes Video/Photo/Time lapse/Looping Video; EV −3.0…+3.0 step 0.1; ISO 400–6400; WB Auto/Daylight/Cloudy/
Incandescent/Fluorescent/Aqua; HDR, WindCut, Sharpness +1/+2; SwitchLens Dual CAM / Single CAM-F / Single CAM-R;
ViewType VR/Dual/Panoramic; StreamingRatio 2:1/1:1/16:9; StreamingInfo High 2560x1280|1920x1920|1920x1080,
Middle 2880x1440|1440x1440|1440x860, Low 2560x1280|1280x1280|1280x720; GearVR stream 2560x1280 2:1;
photo 7776x3888 / 5376x2688; video 3840x1920 30p, 2880x1440 30p, 2560x1280 60p/30p, 1920x960 30p;
interval 0.5–60 s; beep, LED, auto-power-off.

## Bluetooth / SAP topology (session 1 HCI log, analysed with tshark)
- Camera SDP: UUID `a49eb41e-cb06-495c-9f4f-bb80a90cdf00` on **RFCOMM ch 2**; its service name is a BlueZ D-Bus
  socket path (`:1.22/org/socket/server/367/1/...`) → **the camera runs BlueZ**.
- Phone SDP: "SAP" `…bb80a90cdf00` on ch 5 and "SAP" `…aa80a90cdf4a` on ch 6.
- Order: phone opens camera ch 2 (DLCI 4) → camera SDP-queries the phone and opens phone ch 6 (DLCI 13, the
  `aa80…cdf4a` service = "Connection accepted on a49eb41e-…aa80a90cdf4a") on the same RFCOMM mux.
  ⇒ a Linux host must both connect out to ch 2 **and** register an RFCOMM server for `aa80…cdf4a`
  (BlueZ ProfileManager1 via D-Bus).
- SAccessoryService packages: `com/samsung/accessory/{protocol,session,security,connectivity,...}`,
  `com/samsung/discovery/pd` (peer description).
- **Auth = WSM**: `SASecurityManager` loads `libwsm2`; `com/sec/android/WSM/ServerNative` (phone = server):
  `init(String,String[,byte[]][,int])`, `generateClientChallenge`, `checkAndGenerateClientResponse`,
  `recheck…`, `generateConfirmMessage`, `getESAPKey`, `getWSMEncKey`, `getWSMConfirmKey`. Native libs
  `libwsm2.so`, `libwsm2_jni.so` (arm64-v8a and armeabi, + OpenSSL). Plan: run libwsm2 under an ARM
  emulator (unicorn) from Python with a minimal JNI shim rather than reimplement it.
- `libSAP_sconeW_masi*.so` = SCS ("secure connection service"); the camera's PD v0x201 "does not support SCS" → not needed.

## WSM authentication, running Samsung's libwsm2 on the PC (`g360/elfemu.py`, `g360/wsm.py`)
- `libwsm2.so` (arm64) is self-contained C: exports `WSM_Server_*`, `WSM_Client_*`, `WSM_getESAPKey`, … and imports
  only 25 libc functions (malloc, memcpy, strncmp, read, …; no OpenSSL, no JNI). Its 2.6 MB is white-box tables.
- `g360/elfemu.py` loads it in Unicorn (maps PT_LOAD, applies RELATIVE/GLOB_DAT/JUMP_SLOT/ABS64 relocations,
  runs `.init_array`, implements the libc imports in Python) and calls exports with AAPCS64. Loads in ~0.02 s.
- C signatures recovered by disassembling the JNI wrappers in `libwsm2_jni.so`:
  `WSM_{Server,Client}_init(const char *serverID, const char *clientID, const uint8_t *key_or_NULL)` → handle;
  `WSM_Client_generateCommitment(h, out)`; `WSM_Server_generateClientChallenge(h, inout)` (in = commitment);
  `WSM_Client_checkAndGenerateServerChallenge(h, in, out)`; `WSM_Server_checkAndGenerateClientResponse(h, in, out)`;
  `WSM_Client_checkClientResponse(h, in)`; `WSM_getESAPKey(h)` → pointer. **Return values are status (>0 OK),
  not lengths**; sizes are fixed per protocol version:
  v1: challenge 70, server challenge 102, response 35, key 32 · v2: commitment 67, challenge 136, server challenge 200,
  response 67, key 64. IDs are the Bluetooth addresses (`Server(local, remote)` / `Client(remote, local)` in
  SASecurityManager). WSM packets start with `[version][type][len]`: v1 `00 00 46` / `00 01 66` / `00 02 23`.
- **The SM-C200 uses WSM protocol version 1, and the camera is the WSM *server*; the host is the *client*.**
  v1 has no commitment message: the server starts from a zeroed 70-byte buffer.
- Self-tests pass (server ↔ client in emulation, v1 and v2: identical keys; tampered response or mismatched IDs →
  `-6`). Our emulated client accepts the camera's real captured challenge, but in v1 that step does not verify
  anything (tampered input is also accepted); real verification happens on the camera when it checks our reply,
  so compatibility is only provable live.
- Open: the library never asked for randomness (no `open`/`read`), and two runs gave the same key → it may need an
  entropy source we do not emulate yet (getrandom syscall? daemon?). Investigate before relying on it.

## SAP framing on RFCOMM (session 1 HCI, DLCI 13 = camera ↔ phone `aa80…cdf4a` channel)
Frame = `u16 BE length` + body (some frames end in a 2-byte CRC; the log says "Setting CRC").
| frame | dir | body start | meaning |
|---|---|---|---|
| 2088 | cam→phone | `05 02 01 02 01 02 00 0f fa aa f0 aa 03 fe ff ff 02 00 0a 01 "Sband+;SAMSUNG;…"` | Peer Description, msg type 5 |
| 2092 | phone→cam | `06 02 01 02 01 00 02 00 0f fa aa f0 aa 03 fe 27 10 02 00 0a 01 "SM-A102U1;samsung;…"` | PD response, type 6 |
| 2094 | cam→phone | `10 00` + WSM `00 00 46 …` (70 B) | auth: client challenge (msgType 0x10, authType 0) |
| 2095 | phone→cam | `11 00` + WSM `00 01 66 …` (102 B) | auth: server challenge (0x11 = 17) |
| 2097 | cam→phone | `12 00` + WSM `00 02 23 …` (35 B) | auth: client response (0x12 = 18) |
| 2098/2102 | both | `d1 c1 0f fc 01 ff ff ff ff "/System/Reserved/ServiceCapability…"` | capability exchange (plaintext) |
| 2103– | both | session/service setup, `"/system/DI_360_2D"`, `DI_360_2DApp` | service connection |

## Native Bluetooth from Linux — first contact
- PC paired with the camera through the desktop's Bluetooth settings (camera shows "Paired/Trusted"; class 0x620).
- Camera SDP seen from the PC: `…aa80a90cdf4a` on **RFCOMM ch 1** and `…bb80a90cdf00` on **ch 2**.
- In the phone capture, the phone's link to camera ch 2 carries **no data**; it only makes the camera connect back to
  the host's `aa80…` service, where all SAP traffic flows.
- `tools/sap_probe.py` reproduces that: registers `aa80…cdf4a` on RFCOMM ch 6 via BlueZ ProfileManager1, opens camera
  ch 2 → **the camera connected back within ~0.5 s and sent its Peer Description** (type 0x05, identical to the
  capture: `Sband+;SAMSUNG;Shealth;/Sband+;`), then waits for our type 0x06 reply.
- Phone's reply in the capture: `06 02 01 02 01 00 02 00 0f fa aa f0 aa 03 fe 27 10 02 00 0a 01 "SM-A102U1;samsung;Galaxy A10e;SWatch;"`.
- **`tools/sap_auth.py`: full Peer Description + WSM authentication from Linux SUCCEEDED (2026-11-26)** — camera
  connected back at 1.5 s, PD answered with the phone's captured bytes, server challenge computed by emulated libwsm2
  (WSM v1, `Client(serverID=camera BT addr, clientID=PC BT addr)`) in 0.09 s, camera's client response verified at
  1.87 s, 32-byte session key derived. After that the camera waits for the host to start the capability exchange.
- **Post-auth framing decoded** (SAConnection.updateConnectivityData + SAFrameworkUtils.computeCrc):
  `u16 len | CRC16(len) | body | CRC16(body)`, CRC-16/ARC (poly 0xA001 reflected, init 0). Body = 2-byte SAP header
  (byte0: version 3b, frame-type bit 4, session bits 9..6; byte1: session bits 5..0 <<2 | 2-bit fragmentation) +
  payload. Verified on every captured post-auth frame. Default session 1023 carries service-connection control.
- Capability exchange + service connection (byte-exact reproduction of the capture in `g360/sap.py`):
  host [1023] `01 ffffffff "/System/Reserved/ServiceCapabilityDiscovery;" 0001 <capex session> 00ff03010300` →
  camera accept → host capex query (our 5 services) → camera empty query ↔ host empty response → camera capability
  response (`DI_360_2DApp`, `/system/DI_360_2D`) + query for `/system/DI_360_2D` → host answers
  `NX360_SERVER` provider (agent cb0a930a) → camera [1023] service connection request: `01 <ids> "/system/DI_360_2D;"
  u16 count, count×u16 sessions, count×u16 channels (204, 222, 230) …` → host accepts with
  `02 <ids> name 00 u16 count sessions`. JSON then flows on the channel-204 session, each payload prefixed by 0x00.
- **`tools/sap_session.py` (2026-11-26): full native Bluetooth session works** — authenticated at 1.2 s, Gear 360
  service up at 1.8 s (camera picked sessions 3/4/5), JSON exchange done at 4.3 s: our `info`/`widget-info-req`,
  camera `date-time-req` (answered), camera `info` (full identity), `config-info` (acked). Camera also sends its own
  `widget-info-req` (unanswered → `cmd-req dismiss` ~15 s later; now answered) and a status string on channel 230:
  `<GSIM>@[ID]StandaloneRate@[RATE]14@</GSIM>`.
- **JSON sequencing that the camera needs** (from the app 1.4 log): the app does NOT answer the camera's
  `widget-info-req`, and only sends commands after the camera's `widget-info-rsp` (which follows `config-info`).
  Sending `mobilelink` right after the config ack got no answer; sending it ~1 s after `widget-info-rsp` →
  **`cmd-rsp success, r-code 100` in 1.1 s** (`tools/native_session.py`, 2026-11-26).
- PC Wi-Fi Direct: `iw dev` shows the P2P-device wdev at **wlan MAC + 1** (not the wlan MAC with the
  locally-administered bit set). NetworkManager 1.36 can run P2P as the desktop user (StartFind works; profile
  `gear360-p2p`: peer = camera wifi-direct-mac, wps pbc, ipv4 shared 192.168.49.1/24), but `nmcli connection up`
  after mobilelink timed out ("802.1X supplicant took too long to authenticate"). NM/wpa_supplicant debug logging
  and wpa_supplicant's D-Bus/ctrl socket (`-O /run/wpa_supplicant`) are root-only.
- Hypothesis for the timeout: the camera expects the P2P peer at the *announced* address with the
  locally-administered bit set — A10e announced B2:26:5A:… (real 82:7b:…) → rejected; S8+ announced its wlan MAC
  B0:72:BF:xx:xx:xx and connected from B2:72:BF:xx:xx:xx → accepted; PC announced 58:…:f0 = real, bit clear →
  camera would expect 5A:…:f0 → no connection. Test with root: give the P2P device a locally-administered address
  (wpa_supplicant `p2p_device_random_mac_addr`) or drive `wpa_cli -p /run/wpa_supplicant -i p2p-dev-<wlan>` to see
  the GO-negotiation result directly.

- **Wi-Fi Direct from the PC (2026-11-27, root, wpa_supplicant control socket):** after `mobilelink` the
  camera appears in P2P discovery at its announced `wifi-direct-mac` (config methods 0x80 = PBC). A bare
  `P2P_CONNECT … pbc go_intent=15` gets **GO Negotiation Response status 1** and then nothing more; the
  camera ignores the peer for ~60 s until it drops out of discovery. This happened whether we connected from
  the P2P-device address (wlan MAC + 1) or from the wlan MAC itself.
- **The S8+ also got status 1 first.** Its sequence (session-2 logcat): Provision Discovery Request (PBC) →
  PD Response with `static_ip` → our GO Negotiation Request → **status 1** → **25 ms later the camera sends its
  own GO Negotiation Request** → status 0 → phone is GO (freq 5805), camera client. The PC runs had skipped
  provision discovery, so `native_session` now does PD first. The S8+ announced its Wi-Fi MAC
  (`B0:72:BF:…`) and its P2P device address was that MAC with the locally-administered bit set (`B2:72:BF:…`);
  whether the camera checks that relation is still open.
- `wpa_supplicant` 2.10 (Pop!_OS) always creates a dedicated P2P-device wdev when the driver supports one
  (no `p2p_device=` driver param); mac80211 gives it the first unused hardware address, so its address
  cannot be chosen by changing the interface MAC.

- **Address alone is not it (2026-11-27):** with a patched wpa_supplicant (tools/build_wpa_supplicant.sh) P2P ran on
  the Wi-Fi netdev at the Wi-Fi MAC with the locally-administered bit set, announcing the plain Wi-Fi MAC — the
  S8+'s exact pattern (announced `B0:…`, P2P device `B2:…`). PD → GO negotiation still got status 1 and no
  camera-initiated request. (`static_ip` in the S8+'s `P2P-PROV-DISC-PBC-RESP` is only Samsung's
  wpa_supplicant printing a vendor IE; the camera's PD response is 130 bytes to both peers.)
- Tizen's wifi-direct-manager (open source; the camera runs Tizen): an incoming GO Negotiation Request either
  triggers `wfd_session_connect` at once (autoconnection mode — the 25 ms reply the S8+ saw) or raises a
  "Connection" request to the app, which decides (`TODO: Add autoconnection peer matching logic` upstream, so
  the matching is in Samsung's camera app).
- **Our `info` had been app-1.0.22 style** (`"wifi-direct":{"enum":"true","description":"100"}`, app-version
  1.0.22) — what the failing A10e app sent. App 1.4.00.9 (S8+, works) sends
  `"wifi-direct":{"enum":"true","ch-negotiation-wa":{"description":"5G-GO"}}`, app-version 1.4.00.9 and
  `op-mode: user`, and the same `ch-negotiation-wa` in its info ack; it connects with `groupOwnerIntent = 7`.
  g360 now sends the 1.4 form (byte-identical, tests/test_jsonmsg.py) and uses GO intent 7.

- **App-1.4 `info` was the key (2026-11-27):** with the 1.4 form (5G-GO, op-mode) the camera answered our GO
  Negotiation Request with status 1 and **18 ms later sent its own request** → `P2P-GO-NEG-SUCCESS role=GO
  freq=5745` (PC = GO, camera client, PBC). The group then failed: `Failed to set channel (freq=5745): -22` —
  the Intel card cannot start a GO on that 5 GHz channel without an existing connection there. native_session
  now forces the group onto 2412 MHz (the camera's channel) in `own` mode (`--freq`).

- **Group up on 2412 MHz (2026-11-27):** WPS PBC (`WPS-REG-SUCCESS`), `P2P-GROUP-STARTED … GO freq=2412`,
  `EAPOL-4WAY-HS-COMPLETED`, `AP-STA-CONNECTED b2:72:bf:f0:9d:41`: the camera joined the PC's group natively.
  It then never sent a DHCP request and did not answer at 192.168.49.10. Reason: **Samsung "static IP"
  vendor element** (OUI `00:00:f0`, type `0x0a`, 16-bit length 4, IPv4 bytes reversed):
  `dd 0b 00 00 f0 0a 00 00 04 0a 31 a8 c0` = 192.168.49.10. The S8+'s supplicant puts it in its Provision
  Discovery Request (`P2P: Static IP * --> ip: a31a8c0`) and the camera takes that address and skips DHCP
  (the phone's dnsmasq range was .100-.199, yet the camera sat at .10; afterwards the phone runs
  `p2pSetCmd(static_ip, 192.168.49.11)` for the next peer). The camera's PD response carries its own
  `0x0a` element with .41 and a 64-byte type `0x11` element. native_session now adds the element to our PD
  request (`VENDOR_ELEM_ADD 4 …`, `--cam-ip`), and takes the address from the camera's `device-desc-url`.
- Next run: status 1 again but **no camera-initiated request** (60 s in WAIT_PEER_CONNECT). Our Listen channel
  was 6 (2437 MHz; wpa_supplicant picks 1/6/11 at random) and the camera sits on channel 1; in the runs that
  worked its request came 18 ms after its status 1. The private supplicant now listens on channel 1
  (`p2p_listen_reg_class=81`, `p2p_listen_channel=1`), and native_session retries PD + GO negotiation if the
  camera's request has not come within 12 s.
- **Native control works (2026-11-27), no phone:** with Listen channel 1 and the static-IP element, one run
  went BT session → mobilelink → PD → GO-NEG-SUCCESS (0.2 s) → WPS → group up on 2412 MHz →
  `device-desc-url http://192.168.49.10:7676/smp_2_` over BT (0.9 s later) → UPnP 200 → changeToRVF →
  `GetInfomation`: 2560x1280 stream at `http://192.168.49.10:7679/livestream_high.avi`. 12.6 s end to end.
- **Live-view delay ~3 s** (first native viewing). Player side: the old ffplay settings (2 MB probe, fixed
  29.97 fps pacing, frame-threaded HEVC decoding on 32 threads) held ~0.6 s that it never caught up, on a replay
  paced at camera speed; with arrival-time timestamps, a tiny probe, `low_delay` (no frame threads) and
  `-framedrop` that is ~0.1 s. `g360 live --play` now also reports how far the received video runs ahead of
  real time (a buffered burst from the camera) to locate the rest.

## Hardware tests of the g360 app (via a phone relaying HTTP over adb)
- **UPnP servers return 503 unless `User-Agent` starts with `SEC_RVF_ML_` + at least one character**
  (any value works, e.g. `SEC_RVF_ML_00:11:22:33:44:55`). g360 sends `SEC_RVF_ML_<client_id>`.
- `g360 info` **worked**: X_ControllerStatus → SetOperationState changeToRVF → GetInfomation returned the
  full capability set from our own code.
- Live stream request used by the app (`MyMediaExtractor`, raw socket): `GET /livestream_high.avi HTTP/1.1`,
  `User-Agent: Android Linux`, `Connection: Keep-Alive` → `HTTP/1.1 206 Partial Content`,
  `Content-Type: video/x-avi`, DLNA headers, `Connection: close`. g360 sends the same headers for the stream.
- **Single viewer**: while the app shows its live preview our request connects but gets no bytes. With the app
  in Gallery (P2P stays up) our own changeToRVF + GET worked: **18.3 MB / 8 s ≈ 18 Mbit/s**.
- **Stream format = Samsung "TTTS" container (big-endian)** → parsed by `g360/ttts.py`:
  `TTTS 0 3`, `VID0` block (w 2560, h 1280, bitrate 22,000,000, timescale 120000, + `VD00` table),
  `AUD0`/`AU00` block, `LIST 0 movi`, then chunks `id[4] u32 size u64 pts payload`; header block length
  counts its own 4-byte field (body = len-4). `00VD` = one **HEVC Main 4:2:0** access unit (Annex-B; the first
  carries VPS/SPS/PPS); pts step 4004 @ 120 kHz = **29.97 fps**. No audio chunks in `high` during 8 s.
- **Picture = dual fisheye side by side, NOT stitched** (2560x1280 for "high"). Stitching is ours to do.
- The P2P link drops when the app idles or leaves its Camera screen; the camera's Auto Power Off default is 5 min.
- Relaying through a running app is fragile: the link died ~1 min after **our** changeToRVF while the app sat in
  Gallery (the app probably reacts to the mode change and tears down P2P).
- **Force-stop test**: app killed (→ its SAP/BT session ends) → the P2P group stays up but UPnP answers
  **503 to everyone immediately**, and the camera drops Wi-Fi after **~30 s**. ⇒ the camera binds its
  Wi-Fi/UPnP session to a live SAP connection. A Linux driver must hold the Bluetooth SAP session open for
  the whole Wi-Fi session (and answer camera-initiated messages on it).

## Status of the g360 app
| module | state |
|---|---|
| `jsonmsg` | done; builders reproduce the 1.0.22 app's bytes exactly |
| `upnp` | live-view switch + GetInfomation verified on hardware; shutter, start/stop record, stream quality implemented from captures |
| `transport` | `DirectTransport` + `AdbCurlTransport` (relay via a phone's curl) |
| `ttts` | TTTS demuxer, verified against a real 8 s capture |
| `stream` | MP4/MKV remux, ffplay window, v4l2loopback via ffmpeg (MP4 path verified offline) |
| `bt` | identifiers only — SAP client + WSM auth are next |
| Wi-Fi Direct (wpa_supplicant P2P, host as GO) | not started |
| stitching (dual fisheye → equirectangular, GPU) | not started |

## Open questions
- Exact SAP framing and the WSM challenge/response (session 1 HCI + SAccessoryService decompile + emulating libwsm2).
  This is the main blocker for a phone-free Linux driver.
- Wi-Fi Direct: the camera joins the phone as GO; how it gets the credentials (PBC/WPS? the `5G-GO` capability?).
- SOAP for settings (EV/ISO/WB/HDR…) — not exercised yet; find action names in the 1.4 decompile.
- `Browse` arguments for listing files (MobileLink), and eventing subscription details.
- Vendor MTP ops 0x9201 / 0x9202.
