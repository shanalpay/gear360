"""Bluetooth facts about the Gear 360 (SM-C200) link, from captures/session1-btsnoop_hci.log.

Status: identification only. The Samsung Accessory Protocol (SAP) framing and its security handshake
(SASecurityManager challenge/response, likely in libSAP_sconeW_masi.so) are still being reverse-engineered,
so there is no working Bluetooth client yet.

Observed connection order (phone = host role this project will take over):
 1. phone -> camera SDP lookup of SAP_CAMERA_UUID   => camera RFCOMM channel 2
 2. phone opens RFCOMM channel 2 on the camera (DLCI 4)
 3. camera -> phone SDP lookup, then opens the phone's SAP_HOST_UUID service (phone channel 6, DLCI 13)
 4. SAP: peer description v2.1 (peer version 0x201) -> security challenge/response -> session setup
    -> capability exchange -> camera requests service /system/DI_360_2D
 5. JSON messages (g360.jsonmsg) on SAP service channel 204
The camera runs BlueZ (its SDP service name is a BlueZ D-Bus socket path), so its side is Linux too.
"""

# Advertised by the camera (RFCOMM channel 2) and also by Samsung phones (their channel 5).
SAP_CAMERA_UUID = "a49eb41e-cb06-495c-9f4f-bb80a90cdf00"
# Advertised by the phone (channel 6); the camera connects back to this one ("Connection accepted on ...").
SAP_HOST_UUID = "a49eb41e-cb06-495c-9f4f-aa80a90cdf4a"

CAMERA_RFCOMM_CHANNEL = 2          # as seen on this unit; look it up via SDP in real code
SERVICE_PROFILE_ID = "/system/DI_360_2D"
SERVICE_NAME = "DI_360_2DApp"
APP_NAME = "NX360_SERVER"
SERVICE_CHANNELS = {"command": 204, "secondary": 222, "tertiary": 230}   # roles of 222/230 unknown
SAP_PEER_VERSION = 0x201
