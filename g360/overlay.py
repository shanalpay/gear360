"""On-screen controls for the viewer: a bar of buttons at the bottom of the window that can be hidden.

Drawn with cairo (python3-cairo, part of the desktop) into a window-sized RGBA image, which the viewer uploads
as a texture and blends over the 360° picture. The image is redrawn only when something on it changes (hover,
recording time, a message), not every frame.
"""
from __future__ import annotations

import math
import time

# (name, label) in bar order; "play" shows Pause while playing, "hide" collapses the bar to one small button.
BUTTONS = [("snapshot", "Snapshot"), ("record", "Record"), ("stop", "Stop"), ("play", "Pause"),
           ("library", "Library"), ("reset", "Reset view"), ("hide", "Hide")]
TOAST_SECONDS = 3.5


class Overlay:
    def __init__(self):
        import cairo
        self.cairo = cairo
        self.visible = True
        self.hover: str | None = None
        self.pressed: str | None = None
        self.rects: dict[str, tuple[float, float, float, float]] = {}
        self.toast: tuple[str, float] | None = None
        self._key = None

    # ---- state ----
    def say(self, text: str):
        self.toast = (text, time.monotonic() + TOAST_SECONDS)

    def hit(self, x: float, y: float) -> str | None:
        for name, (rx, ry, rw, rh) in self.rects.items():
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return name
        return None

    def contains(self, x: float, y: float) -> bool:
        """Whether (x, y) is on the bar (clicks there never turn the view)."""
        if not self.rects:
            return False
        if not self.visible:
            return self.hit(x, y) is not None
        x0 = min(r[0] for r in self.rects.values()) - self.pad
        x1 = max(r[0] + r[2] for r in self.rects.values()) + self.pad
        y0 = min(r[1] for r in self.rects.values()) - self.pad
        y1 = max(r[1] + r[3] for r in self.rects.values()) + self.pad
        return x0 <= x < x1 and y0 <= y < y1

    # ---- drawing ----
    def frame(self, w: int, h: int, scale: float, state: dict) -> bytes | None:
        """The overlay image (cairo ARGB32: premultiplied BGRA bytes, top row first) if it changed since the
        last call, else None. state: recording (seconds or None), paused, can_record, can_stop."""
        now = time.monotonic()
        if self.toast and now > self.toast[1]:
            self.toast = None
        rec = state.get("recording")
        key = (w, h, scale, self.visible, self.hover, self.pressed, self.toast and self.toast[0],
               None if rec is None else int(rec), state.get("paused"), state.get("can_record"),
               state.get("can_stop"))
        if key == self._key:
            return None
        self._key = key
        cairo = self.cairo
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        self._layout(w, h, scale)
        self._paused = bool(state.get("paused"))
        s = self.s
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        if self.visible:
            x0 = min(r[0] for r in self.rects.values()) - self.pad
            x1 = max(r[0] + r[2] for r in self.rects.values()) + self.pad
            y0 = min(r[1] for r in self.rects.values()) - self.pad
            y1 = max(r[1] + r[3] for r in self.rects.values()) + self.pad
            _round_rect(cr, x0, y0, x1 - x0, y1 - y0, 16 * s)
            cr.set_source_rgba(0.06, 0.07, 0.09, 0.62)
            cr.fill()
            for name, label in BUTTONS:
                enabled = {"record": state.get("can_record"),
                           "stop": state.get("can_stop")}.get(name, True)
                if name == "play" and state.get("paused"):
                    label = "Play"
                if name == "record" and rec is not None:
                    label = f"{int(rec) // 60}:{int(rec) % 60:02d}"
                self._button(cr, name, label, enabled, rec is not None and name == "record")
            top = y0
        else:
            self._button(cr, "show", None, True, False, alpha=0.75)
            top = self.rects["show"][1]
        if rec is not None and not self.visible:     # recording must stay visible with the bar hidden
            cr.set_source_rgba(0.95, 0.15, 0.12, 0.95)
            cr.arc(18 * s, 18 * s, 6 * s, 0, 2 * math.pi)
            cr.fill()
            self._text(cr, f"REC {int(rec) // 60}:{int(rec) % 60:02d}", 30 * s, 18 * s, 13 * s, (1, 1, 1, 0.95),
                       center=False)
        if self.toast:
            cr.set_font_size(13 * s)
            ext = cr.text_extents(self.toast[0])
            tw, th = ext.x_advance + 28 * s, 30 * s
            tx, ty = (w - tw) / 2, top - th - 10 * s
            _round_rect(cr, tx, ty, tw, th, th / 2)
            cr.set_source_rgba(0.06, 0.07, 0.09, 0.75)
            cr.fill()
            self._text(cr, self.toast[0], w / 2, ty + th / 2, 13 * s, (1, 1, 1, 0.95))
        surf.flush()
        return bytes(surf.get_data())

    def _layout(self, w: int, h: int, scale: float):
        bw, bh, gap, pad, margin = 66.0, 58.0, 2.0, 8.0, 16.0
        n = len(BUTTONS)
        full = n * bw + (n - 1) * gap + 2 * pad + 2 * margin
        s = min(scale, w / full) if w > 0 else scale
        self.s, self.pad = s, pad * s
        self.rects = {}
        if self.visible:
            x = (w - (n * bw + (n - 1) * gap) * s) / 2
            y = h - (margin + pad + bh) * s
            for name, _ in BUTTONS:
                self.rects[name] = (x, y, bw * s, bh * s)
                x += (bw + gap) * s
        else:
            d = 38 * s
            self.rects["show"] = (w - d - margin * s, h - d - margin * s, d, d)

    def _button(self, cr, name, label, enabled, active, alpha=1.0):
        x, y, bw, bh = self.rects[name]
        s = self.s
        if name == "show":
            cr.arc(x + bw / 2, y + bh / 2, bw / 2, 0, 2 * math.pi)
            cr.set_source_rgba(0.06, 0.07, 0.09, 0.62)
            cr.fill()
        if enabled and (self.hover == name or self.pressed == name):
            _round_rect(cr, x + 1 * s, y + 1 * s, bw - 2 * s, bh - 2 * s, 10 * s) if name != "show" else \
                cr.arc(x + bw / 2, y + bh / 2, bw / 2, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.22 if self.pressed == name else 0.13)
            cr.fill()
        a = alpha * (1.0 if enabled else 0.32)
        cx, cy = x + bw / 2, y + (bh / 2 if label is None else 22 * s)
        cr.new_path()             # the label's text position must not join the icon's path
        _ICONS["play-paused" if name == "play" and self._paused else name](cr, cx, cy, 11 * s, a, active)
        if label:
            color = (1.0, 0.45, 0.4, a) if active else (1, 1, 1, 0.9 * a)
            self._text(cr, label, cx, y + bh - 12 * s, 11 * s, color)

    def _text(self, cr, text, x, y, size, rgba, center=True):
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        cr.move_to(x - (ext.x_advance / 2 if center else 0), y - ext.y_bearing - ext.height / 2)
        cr.set_source_rgba(*rgba)
        cr.show_text(text)


def _round_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# ---- icons: drawn around (cx, cy), r = half the icon size ----
def _white(cr, a):
    cr.set_source_rgba(1, 1, 1, a)


def _snapshot(cr, cx, cy, r, a, active):
    _white(cr, a)
    _round_rect(cr, cx - r, cy - 0.62 * r, 2 * r, 1.45 * r, 0.25 * r)
    cr.rectangle(cx - 0.4 * r, cy - 0.9 * r, 0.8 * r, 0.4 * r)
    cr.fill()
    cr.set_source_rgba(0.06, 0.07, 0.09, a)
    cr.arc(cx, cy + 0.1 * r, 0.5 * r, 0, 2 * math.pi)
    cr.fill()
    _white(cr, a)
    cr.arc(cx, cy + 0.1 * r, 0.3 * r, 0, 2 * math.pi)
    cr.fill()


def _record(cr, cx, cy, r, a, active):
    cr.set_source_rgba(0.95, 0.18, 0.15, a)
    cr.arc(cx, cy, 0.78 * r, 0, 2 * math.pi)
    cr.fill()


def _stop(cr, cx, cy, r, a, active):
    _white(cr, a)
    _round_rect(cr, cx - 0.68 * r, cy - 0.68 * r, 1.36 * r, 1.36 * r, 0.15 * r)
    cr.fill()


def _pause(cr, cx, cy, r, a, active):
    _white(cr, a)
    cr.rectangle(cx - 0.65 * r, cy - 0.75 * r, 0.45 * r, 1.5 * r)
    cr.rectangle(cx + 0.2 * r, cy - 0.75 * r, 0.45 * r, 1.5 * r)
    cr.fill()


def _play_triangle(cr, cx, cy, r, a, active):
    _white(cr, a)
    cr.move_to(cx - 0.55 * r, cy - 0.85 * r)
    cr.line_to(cx + 0.85 * r, cy)
    cr.line_to(cx - 0.55 * r, cy + 0.85 * r)
    cr.close_path()
    cr.fill()


def _library(cr, cx, cy, r, a, active):
    _white(cr, a)
    q = 0.82 * r
    for dx in (-1, 1):
        for dy in (-1, 1):
            _round_rect(cr, cx + (0.08 * r if dx > 0 else -q - 0.08 * r), cy + (0.08 * r if dy > 0 else -q - 0.08 * r),
                        q, q, 0.15 * r)
    cr.fill()


def _reset(cr, cx, cy, r, a, active):
    _white(cr, a)
    cr.set_line_width(0.24 * r)
    cr.arc(cx, cy, 0.72 * r, math.radians(-60), math.radians(220))
    cr.stroke()
    t = math.radians(-60)                      # arrowhead at the start of the arc, pointing along it
    px, py = cx + 0.72 * r * math.cos(t), cy + 0.72 * r * math.sin(t)
    cr.move_to(px + 0.45 * r, py - 0.05 * r)
    cr.line_to(px - 0.1 * r, py - 0.5 * r)
    cr.line_to(px - 0.15 * r, py + 0.25 * r)
    cr.close_path()
    cr.fill()


def _chevron(cr, cx, cy, r, a, up):
    _white(cr, a)
    cr.set_line_width(0.24 * r)
    cr.set_line_cap(1)          # round
    k = -1 if up else 1
    cr.move_to(cx - 0.7 * r, cy - 0.3 * r * k)
    cr.line_to(cx, cy + 0.35 * r * k)
    cr.line_to(cx + 0.7 * r, cy - 0.3 * r * k)
    cr.stroke()


_ICONS = {"snapshot": _snapshot, "record": _record, "stop": _stop, "play": _pause, "library": _library,
          "reset": _reset, "hide": lambda cr, cx, cy, r, a, act: _chevron(cr, cx, cy, r, a, False),
          "show": lambda cr, cx, cy, r, a, act: _chevron(cr, cx, cy, r, a, True), "play-paused": _play_triangle}
