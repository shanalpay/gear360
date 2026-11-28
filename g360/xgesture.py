"""Touchpad pinch and touchscreen touches for a GLFW window on X11 (GLFW reports neither).

Opens a second Xlib connection, selects XInput 2.4 gesture-pinch events and XInput 2.2 touch events on the
window, and is polled from the render loop. Struct layouts follow X11/extensions/XInput2.h (libXi 1.8).
Selecting touch events turns off the server's pointer emulation for touches on that window, so the handler
gets every finger (one finger can then turn the view, two can pinch). Anything unavailable (no X11, older
server, another client owning touch on the window) is skipped silently.
"""
from __future__ import annotations

import ctypes
import ctypes.util

GENERIC_EVENT = 35
XI_ALL_MASTER_DEVICES = 1
TOUCH_BEGIN, TOUCH_UPDATE, TOUCH_END = 18, 19, 20
PINCH_BEGIN, PINCH_UPDATE, PINCH_END = 27, 28, 29


class _XEvent(ctypes.Structure):
    _fields_ = [("pad", ctypes.c_long * 24)]


class _Cookie(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                ("cookie", ctypes.c_uint), ("data", ctypes.c_void_p)]


_HEAD = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
         ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
         ("time", ctypes.c_ulong), ("deviceid", ctypes.c_int), ("sourceid", ctypes.c_int), ("detail", ctypes.c_int),
         ("root", ctypes.c_ulong), ("event", ctypes.c_ulong), ("child", ctypes.c_ulong),
         ("root_x", ctypes.c_double), ("root_y", ctypes.c_double),
         ("event_x", ctypes.c_double), ("event_y", ctypes.c_double)]


class _DeviceEvent(ctypes.Structure):          # XIDeviceEvent, up to the fields used (detail = touch id)
    _fields_ = _HEAD


class _PinchEvent(ctypes.Structure):           # XIGesturePinchEvent, up to the fields used
    _fields_ = _HEAD + [("delta_x", ctypes.c_double), ("delta_y", ctypes.c_double),
                        ("delta_unaccel_x", ctypes.c_double), ("delta_unaccel_y", ctypes.c_double),
                        ("scale", ctypes.c_double), ("delta_angle", ctypes.c_double)]


class _EventMask(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte))]


_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class XGestures:
    """`XGestures(x11_window_id, handler)`; call `poll()` every frame. handler(kind, **fields) with kind
    "pinch-begin|update|end" (scale, dx, dy, x, y) or "touch-begin|update|end" (id, x, y)."""

    def __init__(self, window: int, handler):
        self.handler, self.dpy, self.pinch, self.touch = handler, None, False, False
        try:
            self._open(window)
        except (OSError, AttributeError):
            self.dpy = None

    def _open(self, window: int):
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xi = ctypes.CDLL(ctypes.util.find_library("Xi") or "libXi.so.6")
        vp, ip = ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        x11.XOpenDisplay.restype, x11.XOpenDisplay.argtypes = vp, [ctypes.c_char_p]
        x11.XQueryExtension.argtypes = [vp, ctypes.c_char_p, ip, ip, ip]
        x11.XPending.argtypes = [vp]
        x11.XNextEvent.argtypes = [vp, ctypes.POINTER(_XEvent)]
        x11.XGetEventData.argtypes = [vp, ctypes.POINTER(_Cookie)]
        x11.XFreeEventData.argtypes = [vp, ctypes.POINTER(_Cookie)]
        x11.XSync.argtypes = [vp, ctypes.c_int]
        x11.XCloseDisplay.argtypes = [vp]
        x11.XSetErrorHandler.restype, x11.XSetErrorHandler.argtypes = vp, [_ERROR_HANDLER]
        xi.XIQueryVersion.argtypes = [vp, ip, ip]
        xi.XISelectEvents.argtypes = [vp, ctypes.c_ulong, ctypes.POINTER(_EventMask), ctypes.c_int]
        self.x11, self.xi = x11, xi
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return
        op, ev, err = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if not x11.XQueryExtension(dpy, b"XInputExtension", ctypes.byref(op), ctypes.byref(ev), ctypes.byref(err)):
            x11.XCloseDisplay(dpy)
            return
        major, minor = ctypes.c_int(2), ctypes.c_int(4)
        if xi.XIQueryVersion(dpy, ctypes.byref(major), ctypes.byref(minor)) != 0:
            x11.XCloseDisplay(dpy)
            return
        self.dpy, self.opcode = dpy, op.value
        # Xlib's default error handler exits the process; a refused selection must not do that.
        self._errors = []
        self._handler = _ERROR_HANDLER(lambda d, e: self._errors.append(1) or 0)
        x11.XSetErrorHandler(self._handler)
        version = (major.value, minor.value)
        if version >= (2, 4):
            self.pinch = self._select(window, (PINCH_BEGIN, PINCH_UPDATE, PINCH_END))
        if version >= (2, 2):
            self.touch = self._select(window, (TOUCH_BEGIN, TOUCH_UPDATE, TOUCH_END))

    def _select(self, window: int, events) -> bool:
        mask = (ctypes.c_ubyte * 4)()
        for e in events:
            mask[e >> 3] |= 1 << (e & 7)
        em = _EventMask(XI_ALL_MASTER_DEVICES, 4, ctypes.cast(mask, ctypes.POINTER(ctypes.c_ubyte)))
        n = len(self._errors)
        self.xi.XISelectEvents(self.dpy, window, ctypes.byref(em), 1)
        self.x11.XSync(self.dpy, 0)
        return len(self._errors) == n

    def poll(self):
        if not self.dpy:
            return
        x11, ev = self.x11, _XEvent()
        while x11.XPending(self.dpy):
            x11.XNextEvent(self.dpy, ctypes.byref(ev))
            cookie = ctypes.cast(ctypes.byref(ev), ctypes.POINTER(_Cookie))
            c = cookie.contents
            if c.type != GENERIC_EVENT or c.extension != self.opcode or not x11.XGetEventData(self.dpy, cookie):
                continue
            try:
                t = cookie.contents.evtype
                if t in (PINCH_BEGIN, PINCH_UPDATE, PINCH_END):
                    p = ctypes.cast(cookie.contents.data, ctypes.POINTER(_PinchEvent)).contents
                    kind = {PINCH_BEGIN: "pinch-begin", PINCH_UPDATE: "pinch-update", PINCH_END: "pinch-end"}[t]
                    self.handler(kind, scale=p.scale, dx=p.delta_x, dy=p.delta_y, x=p.event_x, y=p.event_y)
                elif t in (TOUCH_BEGIN, TOUCH_UPDATE, TOUCH_END):
                    d = ctypes.cast(cookie.contents.data, ctypes.POINTER(_DeviceEvent)).contents
                    kind = {TOUCH_BEGIN: "touch-begin", TOUCH_UPDATE: "touch-update", TOUCH_END: "touch-end"}[t]
                    self.handler(kind, id=d.detail, x=d.event_x, y=d.event_y)
            finally:
                x11.XFreeEventData(self.dpy, cookie)

    def close(self):
        if self.dpy:
            self.x11.XCloseDisplay(self.dpy)
            self.dpy = None
