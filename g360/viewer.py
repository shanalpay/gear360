"""Interactive 360° viewer: stitches the camera's dual-fisheye picture on the GPU and lets the mouse turn the sphere.

The fragment shader maps every window pixel to a direction on the sphere and samples it straight from the two
fisheye images (equidistant model, ~195° per lens), feathering across the band both lenses see, so there is no
intermediate equirectangular image and no CPU work per frame beyond decoding (ffmpeg) and the texture upload.

Mouse (like dragging the picture on the phone):
  left drag          turn the sphere in any direction; the picture follows the pointer; a flick keeps it spinning
  right drag         roll
  wheel / + -        zoom
Touchpad: pinch / spread zooms (moving the fingers also turns). Touchscreen: one finger turns, two pinch/spread.
Keys:
  V                  view: perspective -> little planet -> panorama (equirectangular) -> raw dual fisheye
  L                  keep the horizon level (yaw/pitch only) instead of free rotation
  R                  reset the view          F / F11   fullscreen          Space   pause
  S                  save a screenshot of the window to ~/Pictures
  Q / Esc            quit
Lens calibration (saved with Ctrl+S to ~/.config/g360/lens.json):
  [ ]                lens field of view -/+ 0.5°
  Ctrl+arrows        back lens yaw/pitch -/+ 0.1°          , .    back lens roll -/+ 0.1°
  K                  highlight the blend band               Ctrl+R  default calibration

Needs moderngl and glfw (`.venv/bin/pip install moderngl glfw`; found in the project's .venv automatically).
"""
from __future__ import annotations

import array
import ctypes
import ctypes.util
import fcntl
import glob
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO

from . import icon, ttts

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config") / "g360" / "lens.json"
# Centres and radius are fractions of one half-frame (each lens fills a square half of the picture). 195° with
# the circle spanning the half-frame width lines up the seams on a live capture (185° ghosts, 200° is close).
DEFAULT_LENS = {"fov": 195.0, "radius": 0.5, "front_center": [0.5, 0.5], "back_center": [0.5, 0.5],
                "back_ypr": [0.0, 0.0, 0.0]}
WINDOW_SIZE = (1024, 768)
MODES = ["perspective", "little planet", "panorama", "raw fisheye"]
FOV_LIMITS = {0: (15.0, 150.0), 1: (60.0, 330.0)}
FOV_DEFAULT = {0: 90.0, 1: 290.0}


def _gl_modules():
    try:
        import glfw
        import moderngl
    except ImportError:
        for sp in glob.glob(str(ROOT / ".venv/lib/python3*/site-packages")):
            if sp not in sys.path:
                sys.path.append(sp)
        try:
            import glfw
            import moderngl
        except ImportError as e:
            raise RuntimeError("the 360 viewer needs moderngl and glfw: .venv/bin/pip install moderngl glfw") from e
    return glfw, moderngl


# ---- 3x3 rotations (row-major nested lists) ------------------------------------------------------------------
def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rx(t):
    c, s = math.cos(t), math.sin(t)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _ry(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rz(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _eye():
    return [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]


def _orthonormalize(m):
    cols = [[m[r][c] for r in range(3)] for c in range(3)]
    def norm(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]
    def cross(a, b):
        return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
    z = norm(cols[2])
    x = norm(cross(cols[1], z))
    y = cross(z, x)
    return [[x[r], y[r], z[r]] for r in range(3)]


def _gl_mat(m):
    """Row-major nested list -> column-major tuple for a GLSL mat3 uniform."""
    return tuple(m[r][c] for c in range(3) for r in range(3))


# ---- frame sources: ffmpeg decodes to raw yuv420p, a thread keeps only the newest frame --------------------------
def _grow_pipe(fd: int):
    try:
        size = int(pathlib.Path("/proc/sys/fs/pipe-max-size").read_text())
        fcntl.fcntl(fd, 1031, min(size, 8 << 20))      # F_SETPIPE_SZ
    except (OSError, ValueError):
        pass


def _gl_finish():
    """glFinish through ctypes, which lets go of the interpreter lock while it waits. Called after each
    buffer swap, so the wait for the display's refresh happens here instead of inside the next moderngl call,
    which keeps the lock and starves the threads that feed and read the decoder (10 fps instead of 30)."""
    for name in ("GL", "OpenGL"):
        lib = ctypes.util.find_library(name)
        if lib:
            try:
                fn = ctypes.CDLL(lib).glFinish
                fn.restype, fn.argtypes = None, []
                return fn
            except (OSError, AttributeError):
                pass
    return lambda: None


class FrameSource:
    def __init__(self, cmd: list[str], width: int, height: int, feed=None, still: bool = False):
        if not shutil.which(cmd[0]):
            raise RuntimeError(f"{cmd[0]} not installed")
        self.width, self.height, self.still = width, height, still
        self.size = width * height * 3 // 2
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, bufsize=0)
        # Big pipes: a 5 MB frame then takes a handful of reads instead of ~75, and each read has to win the
        # interpreter lock back from the render loop.
        for f in (self.proc.stdout, self.proc.stdin):
            if f is not None:
                _grow_pipe(f.fileno())
        self.frame: bytearray | None = None
        self.seq, self.ended, self.bytes_in = 0, False, 0
        self._lock = threading.Lock()
        if feed:
            threading.Thread(target=self._feed, args=(feed,), daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()

    def _feed(self, feed):
        try:
            feed(_Counting(self.proc.stdin, self))
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def _read(self):
        out = self.proc.stdout
        while True:
            buf = bytearray(self.size)
            mv, got = memoryview(buf), 0
            while got < self.size:
                n = out.readinto(mv[got:])
                if not n:
                    self.ended = True
                    return
                got += n
            with self._lock:
                self.frame, self.seq = buf, self.seq + 1

    def latest(self):
        with self._lock:
            return self.seq, self.frame

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


_DECODE_OUT = ["-vsync", "0", "-flush_packets", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"]


def live_source(src: IO[bytes], log=None) -> FrameSource:
    """The camera's TTTS live stream (from Camera.open_stream), decoded with as little delay as possible."""
    info = ttts.read_header(src)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
           "-probesize", "32", "-analyzeduration", "0", "-f", "hevc", "-framerate", "30000/1001", "-i", "pipe:0",
           *_DECODE_OUT]
    return FrameSource(cmd, info.width, info.height, feed=lambda dst: ttts.copy_video(src, dst, info, log))


class _Counting:
    """File-like wrapper counting the bytes written (the received video bitrate, shown in the title)."""

    def __init__(self, dst, owner):
        self.dst, self.owner = dst, owner

    def write(self, b):
        self.owner.bytes_in += len(b)
        return self.dst.write(b)


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def file_source(path: str, max_size: int = 16384) -> FrameSource:
    """A dual-fisheye photo or video (the camera's own JPEG/MP4, a saved .hevc, or a raw TTTS capture). Videos
    play in real time and loop."""
    p = pathlib.Path(path)
    head = p.open("rb").read(4)
    pre: list[str] = []
    if head == b"TTTS":                     # raw capture from `g360 live --raw`: demux to a temporary .hevc
        fd, tmp = tempfile.mkstemp(suffix=".hevc", prefix="g360-view-")
        with open(p, "rb") as f, os.fdopen(fd, "wb") as out:
            ttts.demux_video(f, out)
        p = pathlib.Path(tmp)
        import atexit
        atexit.register(lambda: os.path.exists(tmp) and os.unlink(tmp))
    if p.suffix.lower() in (".hevc", ".h265", ".265"):
        pre = ["-f", "hevc", "-framerate", "30000/1001"]
    r = subprocess.run(["ffprobe", "-v", "error", *pre, "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "json", str(p)], capture_output=True, text=True)
    try:
        st = json.loads(r.stdout)["streams"][0]
        w, h = int(st["width"]), int(st["height"])
    except (ValueError, KeyError, IndexError):
        raise RuntimeError(f"cannot read a video stream from {path}: {r.stderr.strip()}")
    vf = []
    if max(w, h) > max_size:
        k = max_size / max(w, h)
        w, h = int(w * k) // 2 * 2, int(h * k) // 2 * 2
        vf = ["-vf", f"scale={w}:{h}"]
    w, h = w // 2 * 2, h // 2 * 2
    still = p.suffix.lower() in IMAGE_EXT
    if still:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(p), *vf,
               "-frames:v", "1", "-s", f"{w}x{h}", *_DECODE_OUT]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1", *pre, "-i", str(p),
               *vf, "-s", f"{w}x{h}", "-an", *_DECODE_OUT]
    return FrameSource(cmd, w, h, still=still)


# ---- rendering -------------------------------------------------------------------------------------------------
VERT = """#version 330
in vec2 pos;
out vec2 ndc;
void main() { ndc = pos; gl_Position = vec4(pos, 0.0, 1.0); }
"""

FRAG = """#version 330
uniform sampler2D ty;
uniform sampler2D tu;
uniform sampler2D tv;
uniform vec2 tex_size;       // picture size in pixels
uniform vec2 win;            // window size in pixels
uniform mat3 rot;            // view -> world
uniform float fov;           // vertical field of view (radians), perspective / little planet
uniform int mode;            // 0 perspective, 1 little planet (stereographic), 2 panorama, 3 raw
uniform float half_fov;      // half of one lens's field of view (radians)
uniform float radius;        // lens circle radius, fraction of the half-frame width
uniform vec2 c_front;        // lens centres, fractions of the half frame
uniform vec2 c_back;
uniform mat3 back_rot;       // back-lens alignment
uniform int seams;
in vec2 ndc;
out vec4 color;
const float PI = 3.14159265358979;

// limited-range BT.601 (the stream is tagged tv range, no matrix: ffmpeg's default). Explicit gradients pick the
// mipmap level: implicit ones are undefined where neighbouring pixels take different branches (lens edges).
float catmull_rom(sampler2D t, vec2 uv, vec2 size) {   // bicubic in 9 bilinear taps, for magnified luma
    vec2 sp = uv * size;
    vec2 p1 = floor(sp - 0.5) + 0.5;
    vec2 f = sp - p1;
    vec2 w0 = f * (-0.5 + f * (1.0 - 0.5 * f));
    vec2 w1 = 1.0 + f * f * (-2.5 + 1.5 * f);
    vec2 w2 = f * (0.5 + f * (2.0 - 1.5 * f));
    vec2 w3 = f * f * (-0.5 + 0.5 * f);
    vec2 w12 = w1 + w2;
    vec2 q0 = (p1 - 1.0) / size, q3 = (p1 + 2.0) / size, q12 = (p1 + w2 / w12) / size;
    return textureLod(t, vec2(q0.x, q0.y), 0.0).r * w0.x * w0.y
         + textureLod(t, vec2(q12.x, q0.y), 0.0).r * w12.x * w0.y
         + textureLod(t, vec2(q3.x, q0.y), 0.0).r * w3.x * w0.y
         + textureLod(t, vec2(q0.x, q12.y), 0.0).r * w0.x * w12.y
         + textureLod(t, vec2(q12.x, q12.y), 0.0).r * w12.x * w12.y
         + textureLod(t, vec2(q3.x, q12.y), 0.0).r * w3.x * w12.y
         + textureLod(t, vec2(q0.x, q3.y), 0.0).r * w0.x * w3.y
         + textureLod(t, vec2(q12.x, q3.y), 0.0).r * w12.x * w3.y
         + textureLod(t, vec2(q3.x, q3.y), 0.0).r * w3.x * w3.y;
}

vec3 rgb(vec2 uv, vec2 gx, vec2 gy) {
    float rho = max(length(gx * tex_size), length(gy * tex_size));    // source pixels per window pixel
    float yy = rho < 1.0 ? catmull_rom(ty, uv, tex_size) : textureGrad(ty, uv, gx, gy).r;
    float y = (yy - 16.0 / 255.0) * 1.164383;
    float u = textureGrad(tu, uv, gx, gy).r - 0.5;
    float v = textureGrad(tv, uv, gx, gy).r - 0.5;
    return clamp(vec3(y + 1.596027 * v, y - 0.391762 * u - 0.812968 * v, y + 2.017232 * u), 0.0, 1.0);
}

vec4 lens(vec3 d, vec2 c, float x0) {   // d in the lens frame (z = optical axis, x right, y up)
    float th = acos(clamp(d.z, -1.0, 1.0));
    float hw = tex_size.x * 0.5;
    float r = th / half_fov * radius * hw;
    float s = length(d.xy);                 // (cos, sin) of the azimuth without atan's wrap-around
    vec2 cs = s > 1e-6 ? d.xy / s : vec2(1.0, 0.0);
    vec2 px = vec2(x0 + c.x * hw + r * cs.x, c.y * tex_size.y - r * cs.y);
    vec2 uv = px / tex_size;
    vec2 gx = dFdx(uv), gy = dFdy(uv);      // taken before any branch
    if (th > half_fov) return vec4(0.0);
    if (px.x < x0 + 0.5 || px.x > x0 + hw - 0.5 || px.y < 0.5 || px.y > tex_size.y - 0.5) return vec4(0.0);
    float m = max(half_fov - 0.5 * PI, 1e-3);
    float w = clamp((half_fov - th) / (2.0 * m), 0.0, 1.0);    // 1 inside, 0.5 at 90°, 0 at the lens edge
    return vec4(rgb(uv, gx, gy) * w, w);
}

vec3 sphere(vec3 d) {
    vec4 a = lens(d, c_front, 0.0);
    vec4 b = lens(back_rot * vec3(-d.x, d.y, -d.z), c_back, tex_size.x * 0.5);
    float w = a.w + b.w;
    if (w <= 0.0) return vec3(0.0);
    vec3 col = (a.rgb + b.rgb) / w;
    if (seams == 1 && a.w > 0.0 && b.w > 0.0) col = mix(col, vec3(1.0, 0.0, 1.0), 0.3);
    return col;
}

void main() {
    float aspect = win.x / win.y;
    vec2 p = vec2(ndc.x * aspect, ndc.y);
    vec3 d;
    if (mode == 0) {
        d = normalize(vec3(p * tan(0.5 * fov), 1.0));
    } else if (mode == 1) {
        vec2 q = p * 2.0 * tan(0.25 * fov);
        float r = length(q);
        float th = 2.0 * atan(0.5 * r);
        d = r > 0.0 ? vec3(sin(th) * q / r, cos(th)) : vec3(0.0, 0.0, 1.0);
    } else if (mode == 2) {
        float lon = ndc.x * PI, lat = ndc.y * 0.5 * PI;
        d = vec3(cos(lat) * sin(lon), sin(lat), cos(lat) * cos(lon));
    } else {
        float ta = tex_size.x / tex_size.y;
        vec2 uv = ndc * 0.5 + 0.5;
        if (aspect > ta) uv.x = (uv.x - 0.5) * aspect / ta + 0.5; else uv.y = (uv.y - 0.5) * ta / aspect + 0.5;
        uv.y = 1.0 - uv.y;
        vec2 gx = dFdx(uv), gy = dFdy(uv);
        if (any(lessThan(uv, vec2(0.0))) || any(greaterThan(uv, vec2(1.0)))) { color = vec4(0.0, 0.0, 0.0, 1.0); return; }
        color = vec4(rgb(uv, gx, gy), 1.0);
        return;
    }
    color = vec4(sphere(rot * d), 1.0);
}
"""


def load_lens() -> dict:
    lens = json.loads(json.dumps(DEFAULT_LENS))
    try:
        lens.update(json.loads(CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    return lens


class Viewer:
    def __init__(self, title: str = "Gear 360", log=print):
        self.glfw, self.mgl = _gl_modules()
        self.log, self.title = log, title
        glfw = self.glfw
        if not glfw.init():
            raise RuntimeError("cannot open a window (no display?)")
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
        if hasattr(glfw, "X11_CLASS_NAME"):        # WM_CLASS, matched by the desktop entry (icon, name)
            glfw.window_hint_string(glfw.X11_CLASS_NAME, icon.WM_CLASS)
            glfw.window_hint_string(glfw.X11_INSTANCE_NAME, icon.WM_CLASS)
        # A 1024x768 window, never larger than 75% of the monitor's work area: GNOME maximizes new windows that
        # nearly fill the screen. Created hidden, centred, then shown.
        glfw.window_hint(glfw.VISIBLE, False)
        glfw.window_hint(glfw.MAXIMIZED, False)
        mon = glfw.get_primary_monitor()
        mx, my, mw, mh = glfw.get_monitor_workarea(mon) if mon else (0, 0, 1920, 1080)
        k = min(1.0, 0.75 * mw / WINDOW_SIZE[0], 0.75 * mh / WINDOW_SIZE[1])
        w, h = int(WINDOW_SIZE[0] * k), int(WINDOW_SIZE[1] * k)
        self.win = glfw.create_window(w, h, title, None, None)
        if not self.win:
            glfw.terminate()
            raise RuntimeError("cannot create an OpenGL 3.3 window")
        glfw.set_window_pos(self.win, mx + (mw - w) // 2, my + (mh - h) // 2)
        glfw.show_window(self.win)
        glfw.make_context_current(self.win)
        glfw.swap_interval(1)
        self.ctx = self.mgl.create_context()
        self.max_texture = self.ctx.info["GL_MAX_TEXTURE_SIZE"]
        self.prog = self.ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
        quad = self.ctx.buffer(data=array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1]).tobytes())
        self.vao = self.ctx.vertex_array(self.prog, [(quad, "2f", "pos")])
        self.tex = None
        self.lens = load_lens()
        self.mode, self.level, self.paused, self.seams = 0, False, False, False
        self.fov = {0: FOV_DEFAULT[0], 1: FOV_DEFAULT[1]}
        self.rot = _eye()
        self.drag = None              # (button, x, y)
        self.vel = (0.0, 0.0, 0.0)    # yaw, pitch, roll rad/s (view frame) for the flick
        self.last_move = 0.0
        self.fullscreen_restore = None
        glfw.set_cursor_pos_callback(self.win, self._on_move)
        glfw.set_mouse_button_callback(self.win, self._on_button)
        glfw.set_scroll_callback(self.win, lambda w, dx, dy: self._zoom(0.9 ** dy))
        glfw.set_key_callback(self.win, self._on_key)
        try:
            glfw.set_window_icon(self.win, 5, icon.glfw_images(self.ctx))
        except Exception as e:      # cosmetic only
            log(f"viewer: no window icon ({e})")
        self.touches: dict[int, tuple[float, float]] = {}
        self.pinch_fov = None
        self.gestures = None
        try:
            from .xgesture import XGestures
            self.gestures = XGestures(glfw.get_x11_window(self.win), self._on_gesture)
        except Exception:
            pass

    # ---- view control ----
    def _rad_per_px(self):
        w, h = self.glfw.get_framebuffer_size(self.win)
        if self.mode == 2:
            return 2 * math.pi / max(w, 1), math.pi / max(h, 1)
        f = math.radians(self.fov.get(self.mode, 90.0))
        k = (2 * math.tan(f / 2) if self.mode == 0 else 4 * math.tan(f / 4)) / max(h, 1)
        return k, k

    def turn(self, yaw: float, pitch: float, roll: float = 0.0):
        """Rotate the camera in its own frame (radians): +yaw looks right, +pitch looks up, +roll clockwise."""
        if self.level:
            r = _mul(_mul(_ry(yaw), self.rot), _rx(-pitch))
            f = [r[0][2], r[1][2], r[2][2]]
            p = max(-1.55, min(1.55, math.asin(max(-1.0, min(1.0, -f[1])))))
            self.rot = _mul(_ry(math.atan2(f[0], f[2])), _rx(p))
        else:
            self.rot = _orthonormalize(_mul(_mul(_mul(self.rot, _ry(yaw)), _rx(-pitch)), _rz(roll)))

    def _on_button(self, win, button, action, mods):
        glfw = self.glfw
        x, y = glfw.get_cursor_pos(win)
        if action == glfw.PRESS:
            self.drag, self.vel = (button, x, y), (0.0, 0.0, 0.0)
            self.last_move = time.monotonic()
        elif action == glfw.RELEASE:
            if time.monotonic() - self.last_move > 0.08:      # held still before letting go: no flick
                self.vel = (0.0, 0.0, 0.0)
            self.drag = None

    def _on_move(self, win, x, y):
        if not self.drag:
            return
        button, x0, y0 = self.drag
        self.drag = (button, x, y)
        self._drag(x - x0, y - y0, roll=button == self.glfw.MOUSE_BUTTON_RIGHT)

    def _drag(self, dx: float, dy: float, roll: bool = False):
        """Pointer or finger moved by (dx, dy) window pixels while held: turn the view and track the speed for
        a flick."""
        if self.mode == 3:
            return
        kx, ky = self._rad_per_px()
        now = time.monotonic()
        dt = max(now - self.last_move, 1e-3)
        self.last_move = now
        if roll:
            d = (0.0, 0.0, dx * kx)
        else:   # grab: the picture follows the pointer, so the camera turns the other way
            d = (-dx * kx, dy * ky, 0.0)
        self.turn(*d)
        a = min(dt / 0.05, 1.0)
        self.vel = tuple((1 - a) * v + a * di / dt for v, di in zip(self.vel, d))

    def _on_gesture(self, kind: str, **e):
        """Touchpad pinch (XI 2.4 gestures) and touchscreen fingers (XI touch events), from g360.xgesture."""
        if kind == "pinch-begin":
            self.pinch_fov, self.vel = self.fov.get(self.mode), (0.0, 0.0, 0.0)
            self.last_move = time.monotonic()
        elif kind == "pinch-update":
            if self.pinch_fov is not None and e["scale"] > 0:       # spread (scale > 1) zooms in
                lo, hi = FOV_LIMITS[self.mode]
                self.fov[self.mode] = max(lo, min(hi, self.pinch_fov / e["scale"]))
            self._drag(e["dx"], e["dy"])
        elif kind == "pinch-end":
            self.pinch_fov = None
        elif kind == "touch-begin":
            self.touches[e["id"]] = (e["x"], e["y"])
            self.vel, self.last_move = (0.0, 0.0, 0.0), time.monotonic()
        elif kind == "touch-update" and e["id"] in self.touches:
            before = dict(self.touches)
            self.touches[e["id"]] = (e["x"], e["y"])
            if len(self.touches) == 1:
                ox, oy = before[e["id"]]
                self._drag(e["x"] - ox, e["y"] - oy)
            else:                   # two fingers: pinch/spread zooms, moving them together turns
                ids = sorted(self.touches)[:2]
                (ax, ay), (bx, by) = (before[i] for i in ids)
                (cx, cy), (dx, dy) = (self.touches[i] for i in ids)
                d0, d1 = math.hypot(bx - ax, by - ay), math.hypot(dx - cx, dy - cy)
                if d0 > 1 and d1 > 1:
                    self._zoom(d0 / d1)
                self._drag(((cx + dx) - (ax + bx)) / 2, ((cy + dy) - (ay + by)) / 2)
        elif kind == "touch-end":
            self.touches.pop(e["id"], None)
            if not self.touches and time.monotonic() - self.last_move > 0.08:
                self.vel = (0.0, 0.0, 0.0)

    def _zoom(self, factor: float):
        if self.mode in self.fov:
            lo, hi = FOV_LIMITS[self.mode]
            self.fov[self.mode] = max(lo, min(hi, self.fov[self.mode] * factor))

    def _set_mode(self, mode: int):
        self.mode = mode
        if mode == 1:        # little planet: look straight down with a very wide field
            self.rot = _rx(math.radians(90))
        elif mode in (0, 2):
            self.rot = _eye()

    def _on_key(self, win, key, scancode, action, mods):
        if action not in (self.glfw.PRESS, self.glfw.REPEAT):
            return
        g = self.glfw
        ctrl = mods & g.MOD_CONTROL
        lens = self.lens
        if key in (g.KEY_ESCAPE, g.KEY_Q):
            g.set_window_should_close(win, True)
        elif key == g.KEY_V:
            self._set_mode((self.mode + 1) % len(MODES))
        elif key == g.KEY_L:
            self.level = not self.level
            self.turn(0, 0)
        elif key == g.KEY_R and ctrl:
            self.lens = json.loads(json.dumps(DEFAULT_LENS))
        elif key == g.KEY_R:
            self.vel = (0.0, 0.0, 0.0)
            self.fov = {0: FOV_DEFAULT[0], 1: FOV_DEFAULT[1]}
            self._set_mode(self.mode)
        elif key in (g.KEY_F, g.KEY_F11):
            self._toggle_fullscreen()
        elif key == g.KEY_SPACE:
            self.paused = not self.paused
        elif key == g.KEY_S and ctrl:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(lens, indent=2) + "\n")
            self.log(f"viewer: lens calibration saved to {CONFIG}")
        elif key == g.KEY_S:
            self._screenshot()
        elif key == g.KEY_K:
            self.seams = not self.seams
        elif key in (g.KEY_EQUAL, g.KEY_KP_ADD):
            self._zoom(0.9)
        elif key in (g.KEY_MINUS, g.KEY_KP_SUBTRACT):
            self._zoom(1 / 0.9)
        elif key == g.KEY_LEFT_BRACKET:
            lens["fov"] = round(lens["fov"] - 0.5, 2)
        elif key == g.KEY_RIGHT_BRACKET:
            lens["fov"] = round(lens["fov"] + 0.5, 2)
        elif key == g.KEY_COMMA:
            lens["back_ypr"][2] = round(lens["back_ypr"][2] - 0.1, 2)
        elif key == g.KEY_PERIOD:
            lens["back_ypr"][2] = round(lens["back_ypr"][2] + 0.1, 2)
        elif ctrl and key in (g.KEY_LEFT, g.KEY_RIGHT):
            lens["back_ypr"][0] = round(lens["back_ypr"][0] + (0.1 if key == g.KEY_RIGHT else -0.1), 2)
        elif ctrl and key in (g.KEY_UP, g.KEY_DOWN):
            lens["back_ypr"][1] = round(lens["back_ypr"][1] + (0.1 if key == g.KEY_UP else -0.1), 2)
        elif key in (g.KEY_LEFT, g.KEY_RIGHT, g.KEY_UP, g.KEY_DOWN):
            step = math.radians(5)
            self.turn({g.KEY_LEFT: -step, g.KEY_RIGHT: step}.get(key, 0.0),
                      {g.KEY_UP: step, g.KEY_DOWN: -step}.get(key, 0.0))

    def _toggle_fullscreen(self):
        g = self.glfw
        if self.fullscreen_restore:
            x, y, w, h = self.fullscreen_restore
            g.set_window_monitor(self.win, None, x, y, w, h, 0)
            self.fullscreen_restore = None
            return
        x, y = g.get_window_pos(self.win)
        w, h = g.get_window_size(self.win)
        self.fullscreen_restore = (x, y, w, h)
        cx, cy = x + w // 2, y + h // 2           # the monitor the window is on
        mon = g.get_primary_monitor()
        for m in g.get_monitors():
            mx, my = g.get_monitor_pos(m)
            vm = g.get_video_mode(m)
            if mx <= cx < mx + vm.size.width and my <= cy < my + vm.size.height:
                mon = m
        vm = g.get_video_mode(mon)
        g.set_window_monitor(self.win, mon, 0, 0, vm.size.width, vm.size.height, vm.refresh_rate)

    def _screenshot(self):
        w, h = self.glfw.get_framebuffer_size(self.win)
        data = self.ctx.screen.read(viewport=(0, 0, w, h), components=3)
        out = pathlib.Path.home() / "Pictures" / time.strftime("g360-%Y%m%d-%H%M%S.png")
        out.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                        "-i", "-", "-vf", "vflip", str(out)], input=data)
        self.log(f"viewer: saved {out}")

    # ---- frames and drawing ----
    def _textures(self, w: int, h: int):
        if self.tex and self.tex[0].size == (w, h):
            return
        mk = lambda tw, th: self.ctx.texture((tw, th), 1, dtype="f1", alignment=1)
        self.tex = (mk(w, h), mk(w // 2, h // 2), mk(w // 2, h // 2))
        for i, (t, name) in enumerate(zip(self.tex, ("ty", "tu", "tv"))):
            # Mipmaps + anisotropic filtering: wherever the view shows the picture smaller than its pixels (raw
            # view, little planet, panorama, the squeezed lens edges) plain bilinear sampling skips pixels and
            # aliases (jagged, shimmering detail).
            t.build_mipmaps()
            t.filter = (self.mgl.LINEAR_MIPMAP_LINEAR, self.mgl.LINEAR)
            t.anisotropy = 16.0
            t.repeat_x = t.repeat_y = False
            t.use(i)
            self.prog[name].value = i

    def _upload(self, frame, w: int, h: int):
        self._textures(w, h)
        ys, cs = w * h, (w // 2) * (h // 2)
        mv = memoryview(frame)
        self.tex[0].write(mv[:ys])
        self.tex[1].write(mv[ys:ys + cs])
        self.tex[2].write(mv[ys + cs:ys + 2 * cs])
        for t in self.tex:
            t.build_mipmaps()

    def _draw(self, w: int, h: int):
        fw, fh = self.glfw.get_framebuffer_size(self.win)
        self.ctx.viewport = (0, 0, fw, fh)
        self.ctx.clear(0.0, 0.0, 0.0)
        if not self.tex:
            return
        lens = self.lens
        y, p, r = (math.radians(v) for v in lens["back_ypr"])
        u = self.prog
        u["tex_size"].value = (float(w), float(h))
        u["win"].value = (float(fw), float(fh))
        u["rot"].value = _gl_mat(self.rot)
        u["fov"].value = math.radians(self.fov.get(self.mode, 90.0))
        u["mode"].value = self.mode
        u["half_fov"].value = math.radians(lens["fov"]) / 2
        u["radius"].value = lens["radius"]
        u["c_front"].value = tuple(lens["front_center"])
        u["c_back"].value = tuple(lens["back_center"])
        u["back_rot"].value = _gl_mat(_mul(_mul(_ry(y), _rx(p)), _rz(r)))
        u["seams"].value = int(self.seams)
        for i, t in enumerate(self.tex):
            t.use(i)
        self.vao.render(self.mgl.TRIANGLE_STRIP)

    def run(self, src: FrameSource) -> int:
        glfw = self.glfw
        finish = _gl_finish()
        shown, t_prev, t_fps, n_fps, fps = -1, time.monotonic(), time.monotonic(), 0, 0.0
        b_fps, mbit = 0, None
        title = None
        try:
            while not glfw.window_should_close(self.win):
                glfw.poll_events()
                if self.gestures:
                    self.gestures.poll()
                now = time.monotonic()
                dt, t_prev = now - t_prev, now
                if not self.drag and not self.touches and any(abs(v) > 1e-3 for v in self.vel):     # flick: keep turning, slowing down
                    self.turn(*(v * dt for v in self.vel))
                    self.vel = tuple(v * math.exp(-2.5 * dt) for v in self.vel)
                seq, frame = src.latest()
                if frame is not None and seq != shown and not self.paused:
                    self._upload(frame, src.width, src.height)
                    shown, n_fps = seq, n_fps + 1
                if now - t_fps >= 1.0:
                    if src.bytes_in:
                        mbit = (src.bytes_in - b_fps) * 8 / (now - t_fps) / 1e6
                        b_fps = src.bytes_in
                    fps, t_fps, n_fps = n_fps / (now - t_fps), now, 0
                self._draw(src.width, src.height)
                glfw.swap_buffers(self.win)
                finish()
                fov = f" {self.fov[self.mode]:.0f}°" if self.mode in self.fov else ""
                state = ("paused" if self.paused else "ended" if src.ended and not src.still
                         else "photo" if src.still else f"{fps:.0f} fps" + (f" {mbit:.1f} Mbit/s" if mbit else ""))
                t = (f"{self.title} — {MODES[self.mode]}{fov}{' (level)' if self.level else ''} — {state} — "
                     f"lens {self.lens['fov']:.1f}° back {self.lens['back_ypr']}")
                if t != title:
                    glfw.set_window_title(self.win, t)
                    title = t
        except KeyboardInterrupt:
            pass
        finally:
            src.close()
            if self.gestures:
                self.gestures.close()
            glfw.terminate()
        return 0


def view_stream(src: IO[bytes], log=print, title: str = "Gear 360 live") -> int:
    v = Viewer(title, log)
    return v.run(live_source(src, log))


def view_file(path: str, log=print) -> int:
    v = Viewer(f"Gear 360 — {pathlib.Path(path).name}", log)
    return v.run(file_source(path, v.max_texture))
