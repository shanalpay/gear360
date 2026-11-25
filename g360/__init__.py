"""g360 — Linux control software for the Samsung Gear 360 (2016, SM-C200).

Layers (see NOTES.md for the protocol evidence behind each):
  bt        Bluetooth / Samsung Accessory Protocol facts (client not implemented yet)
  jsonmsg   JSON command messages carried over SAP
  transport HTTP transports: direct (PC on the camera's Wi-Fi Direct) or relayed through a phone via adb
  upnp      SOAP control: live view, shutter, record, capabilities
  stream    live-view stream sinks: file, ffplay, v4l2loopback (OBS)
"""
__version__ = "0.1.0"
