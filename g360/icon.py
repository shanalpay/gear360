"""The g360 icon (a globe with an orbit arrow), drawn by a shader so it is crisp at every size, plus a
desktop-menu install (`python3 -m g360 install-desktop`)."""
from __future__ import annotations

import array
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WM_CLASS = "g360"

_VERT = """#version 330
in vec2 pos;
out vec2 ndc;
void main() { ndc = pos; gl_Position = vec4(pos, 0.0, 1.0); }
"""

_FRAG = """#version 330
in vec2 ndc;
out vec4 color;
uniform float px;            // one pixel in ndc units
const float PI = 3.14159265358979;

vec4 over(vec4 dst, vec3 rgb, float a) { return vec4(rgb * a + dst.rgb * (1.0 - a), a + dst.a * (1.0 - a)); }
float cover(float d) { return clamp(0.5 - d / px, 0.0, 1.0); }   // signed distance -> coverage

float tri(vec2 p, vec2 a, vec2 b, vec2 c) {                     // signed distance to a triangle (iq)
    vec2 e0 = b - a, e1 = c - b, e2 = a - c, v0 = p - a, v1 = p - b, v2 = p - c;
    vec2 q0 = v0 - e0 * clamp(dot(v0, e0) / dot(e0, e0), 0.0, 1.0);
    vec2 q1 = v1 - e1 * clamp(dot(v1, e1) / dot(e1, e1), 0.0, 1.0);
    vec2 q2 = v2 - e2 * clamp(dot(v2, e2) / dot(e2, e2), 0.0, 1.0);
    float s = sign(e0.x * e2.y - e0.y * e2.x);
    vec2 d = min(min(vec2(dot(q0, q0), s * (v0.x * e0.y - v0.y * e0.x)),
                     vec2(dot(q1, q1), s * (v1.x * e1.y - v1.y * e1.x))),
                     vec2(dot(q2, q2), s * (v2.x * e2.y - v2.y * e2.x)));
    return -sqrt(d.x) * sign(d.y);
}

const float A = 0.94, B = 0.30, TILT = -0.32, W = 0.055;          // orbit ellipse
mat2 rot(float t) { return mat2(cos(t), sin(t), -sin(t), cos(t)); }
vec2 orbit(float t) { return rot(TILT) * vec2(A * cos(t), B * sin(t)); }

vec4 ring(vec2 p, bool front) {                                  // the orbit band, back or front half
    vec2 q = rot(-TILT) * p;
    float t = mod(atan(q.y / B, q.x / A), 2.0 * PI);
    if ((sin(t) < 0.0) != front) return vec4(0.0);
    float f = length(q / vec2(A, B)) - 1.0;
    vec2 g = (q / vec2(A * A, B * B)) / max(length(q / vec2(A, B)), 1e-4);
    float d = abs(f / max(length(g), 1e-4)) - W * 0.5;
    if (t < 0.50 || t > 2.0 * PI - 0.10) return vec4(0.0);       // gap before the arrowhead
    vec3 c = mix(vec3(1.0, 0.50, 0.08), vec3(1.0, 0.76, 0.30), 0.5 + 0.5 * sin(t));
    if (!front) c *= 0.7;
    return vec4(c, cover(d));
}

void main() {
    vec2 p = ndc;
    vec4 col = vec4(0.0);
    vec4 r = ring(p, false);
    col = over(col, r.rgb, r.a);

    float R = 0.64, d = length(p) - R;                           // globe
    if (d < px) {
        vec2 q = p / R;
        vec3 n = vec3(q, sqrt(max(0.0, 1.0 - dot(q, q))));
        vec3 l = normalize(vec3(-0.5, 0.6, 0.65));
        float diff = clamp(dot(n, l), 0.0, 1.0);
        vec3 base = mix(vec3(0.02, 0.13, 0.33), vec3(0.16, 0.56, 0.95), diff);
        base += pow(clamp(dot(reflect(-l, n), vec3(0, 0, 1)), 0.0, 1.0), 24.0) * 0.55;   // highlight
        base *= mix(0.55, 1.0, smoothstep(0.0, 0.35, n.z));                              // darker rim
        vec3 m = n;                                                                        // tilted grid
        m.yz = rot(0.35) * m.yz;
        m.xz = rot(0.45) * m.xz;
        float lat = asin(clamp(m.y, -1.0, 1.0)), lon = atan(m.x, m.z);
        float gl = abs(fract(lon / (PI / 6.0) + 0.5) - 0.5) * (PI / 6.0) * cos(lat);
        float ga = abs(fract(lat / (PI / 6.0) + 0.5) - 0.5) * (PI / 6.0);
        float lw = max(px * 0.9 / R, 0.012);
        float grid = max(1.0 - smoothstep(lw * 0.5, lw * 1.5, gl), 1.0 - smoothstep(lw * 0.5, lw * 1.5, ga));
        base = mix(base, vec3(0.85, 0.95, 1.0), grid * 0.45 * smoothstep(0.0, 0.25, n.z));
        col = over(col, base, cover(d));
        col = over(col, vec3(0.0, 0.05, 0.15), cover(abs(d) - px * 0.6) * 0.6);          // outline
    }

    r = ring(p, true);
    col = over(col, r.rgb, r.a);
    vec2 base = orbit(2.0 * PI - 0.10), tip = orbit(0.40);       // arrowhead along the direction of travel
    vec2 dir = normalize(tip - base), nrm = vec2(-dir.y, dir.x);
    float ta = tri(p, tip + dir * 0.02, base + nrm * W * 1.9, base - nrm * W * 1.9);
    col = over(col, vec3(1.0, 0.58, 0.12), cover(ta));
    color = col.a > 0.0 ? vec4(col.rgb / col.a, col.a) : vec4(0.0);
}
"""


def render(ctx, size: int) -> bytes:
    """RGBA pixels, top row first, of the icon at `size` x `size` (needs a moderngl context)."""
    import moderngl
    prog = ctx.program(vertex_shader=_VERT, fragment_shader=_FRAG)
    vao = ctx.vertex_array(prog, [(ctx.buffer(array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1]).tobytes()),
                                   "2f", "pos")])
    fbo = ctx.framebuffer(color_attachments=[ctx.renderbuffer((size, size), 4)])
    fbo.use()
    fbo.clear(0.0, 0.0, 0.0, 0.0)
    prog["px"].value = 2.0 / size
    vao.render(moderngl.TRIANGLE_STRIP)
    data = fbo.read(components=4)
    rows = [data[i * size * 4:(i + 1) * size * 4] for i in range(size)]
    if ctx.screen is not None:     # back to the window's framebuffer (none in a standalone context)
        ctx.screen.use()
    for o in (vao, prog, fbo):
        o.release()
    return b"".join(reversed(rows))


def glfw_images(ctx, sizes=(128, 64, 48, 32, 16)):
    """Icon images in the form glfw.set_window_icon takes: (width, height, rows of RGBA pixels)."""
    out = []
    for s in sizes:
        px = render(ctx, s)
        out.append((s, s, [[px[(y * s + x) * 4:(y * s + x) * 4 + 4] for x in range(s)] for y in range(s)]))
    return out


def save_png(ctx, size: int, path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{size}x{size}",
                    "-i", "-", str(path)], input=render(ctx, size), check=True)


def install_desktop(log=print) -> int:
    """Icons into ~/.local/share/icons, and two menu entries: the 360 viewer for files (also offered for
    JPEG/MP4 in the file manager's Open With) and the live session (in a terminal, as it asks for sudo)."""
    from .viewer import _gl_modules
    _, moderngl = _gl_modules()
    try:
        ctx = moderngl.create_standalone_context()
    except Exception:
        ctx = moderngl.create_standalone_context(backend="egl")
    data = pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local" / "share")
    for s in (16, 32, 48, 64, 128, 256, 512):
        save_png(ctx, s, data / "icons" / "hicolor" / f"{s}x{s}" / "apps" / "g360.png")
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    apps = data / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "g360-viewer.desktop").write_text(f"""[Desktop Entry]
Type=Application
Name=Gear 360 Viewer
GenericName=360° Photo and Video Viewer
Comment=Turn Gear 360 photos and videos around with the mouse
Exec={py} -m g360 view %f
Path={ROOT}
Icon=g360
Terminal=false
StartupWMClass={WM_CLASS}
MimeType=image/jpeg;video/mp4;
Categories=Graphics;Viewer;
""")
    live = f"cd {ROOT} && sudo {py} tools/native_session.py --view; echo; printf 'Press Enter to close '; read x"
    (apps / "g360-live.desktop").write_text(f"""[Desktop Entry]
Type=Application
Name=Gear 360 Live
Comment=Connect to the Gear 360 and show the live 360° view
Exec=sh -c "{live}"
Path={ROOT}
Icon=g360
Terminal=true
Categories=AudioVideo;Video;
""")
    subprocess.run(["update-desktop-database", str(apps)], capture_output=True)
    subprocess.run(["gtk-update-icon-cache", "-q", "-t", str(data / "icons" / "hicolor")], capture_output=True)
    log(f"installed icons and menu entries 'Gear 360 Viewer' and 'Gear 360 Live' under {data}")
    from .wsm import NOT_FOUND, find_lib
    if not find_lib():
        log(f"note: {NOT_FOUND}")
    return 0
