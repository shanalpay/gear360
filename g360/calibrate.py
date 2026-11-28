"""Lens calibration for the stitch, measured from footage instead of guessed.

Both lenses see a band around the seam (each covers ~195°). For a set of frames this samples that band from
each lens and searches for the lens field of view, the back lens's centre on the sensor and its rotation
relative to the front lens that make the two samplings agree best (normalized correlation of the
high-passed luma). The result goes to the viewer's lens.json, the same file Ctrl+S writes.

Things close to the camera cannot line up for every distance at once (the lenses are ~2 cm apart), so the fit
should use footage with plenty of detail a metre or more away along the seam, and several frames. Run it with
`python3 -m g360 calibrate FILE` (a photo, a video, a saved .bin/.hevc stream) or press A in the viewer.
"""
from __future__ import annotations

import math

FIT_KEYS = ("fov", "back_center", "back_ypr")
_BAND = (84.0, 96.0)        # degrees from the front axis that are compared (inside the shared ~±7.5°)
_N_PHI, _N_TH = 720, 25


def _np():
    from .viewer import _gl_modules
    _gl_modules()                     # puts the project's .venv (numpy) on sys.path
    import numpy
    return numpy


def _rot(np, yaw, pitch, roll):
    cy, sy, cp, sp, cr, sr = (f(math.radians(a)) for a in (yaw, pitch, roll) for f in (math.cos, math.sin))
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return ry @ rx @ rz


def _project(np, d, lens, back: bool, w: int, h: int):
    """Pixel coordinates of directions d (N x 3, front-lens frame) in the frame, as the viewer's shader does."""
    hw = w * 0.5
    if back:
        d = np.stack([-d[:, 0], d[:, 1], -d[:, 2]], 1) @ _rot(np, *lens["back_ypr"]).T
        c, x0 = lens["back_center"], hw
    else:
        c, x0 = lens["front_center"], 0.0
    th = np.arccos(np.clip(d[:, 2], -1, 1))
    r = th / math.radians(lens["fov"] / 2) * lens["radius"] * hw
    s = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
    return x0 + c[0] * hw + r * d[:, 0] / s, c[1] * h - r * d[:, 1] / s


def _sample(np, img, x, y):
    h, w = img.shape
    x = np.clip(x, 0, w - 1.001)
    y = np.clip(y, 0, h - 1.001)
    x0, y0 = x.astype(int), y.astype(int)
    fx, fy = x - x0, y - y0
    return (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x0 + 1] * fx * (1 - fy)
            + img[y0 + 1, x0] * (1 - fx) * fy + img[y0 + 1, x0 + 1] * fx * fy)


def _blur(np, img, k: int):
    """Box blur, k x k (edges repeated)."""
    if k <= 1:
        return img
    c = np.cumsum(np.pad(img, ((k // 2 + 1, k // 2), (0, 0)), mode="edge"), 0)
    img = (c[k:] - c[:-k]) / k
    c = np.cumsum(np.pad(img, ((0, 0), (k // 2 + 1, k // 2)), mode="edge"), 1)
    return (c[:, k:] - c[:, :-k]) / k


class _Problem:
    """Seam mismatch for a set of luma frames at one level of detail (frames halved, then blurred k x k)."""

    def __init__(self, np, frames, base, k: int, band=_BAND, n_th=_N_TH):
        self.np, self.base, self.n_th = np, base, n_th
        self.h, self.w = frames[0].shape
        self.frames = [_blur(np, f[::2, ::2], k) for f in frames]
        phi = np.linspace(0, 2 * math.pi, _N_PHI, endpoint=False)
        th = np.radians(np.linspace(*band, n_th))
        P, T = np.meshgrid(phi, th)
        self.dirs = np.stack([np.sin(T) * np.cos(P), np.sin(T) * np.sin(P), np.cos(T)], -1).reshape(-1, 3)

    def lens(self, v):
        lens = dict(self.base)
        lens.update(fov=float(v[0]), back_center=[float(v[1]), float(v[2])], back_ypr=[float(a) for a in v[3:6]])
        return lens

    def band(self, img, lens, back):
        x, y = _project(self.np, self.dirs, lens, back, self.w, self.h)
        b = _sample(self.np, img, x / 2, y / 2).reshape(self.n_th, _N_PHI)
        return b - _blur(self.np, b, 7)      # high-pass: exposure and vignetting differences do not count

    def cost(self, v):
        lens = self.lens(v)
        num = den_a = den_b = 0.0
        for img in self.frames:
            a, b = self.band(img, lens, False), self.band(img, lens, True)
            num += float((a * b).sum())
            den_a += float((a * a).sum())
            den_b += float((b * b).sum())
        return 1.0 - num / max(math.sqrt(den_a * den_b), 1e-9)       # 0 = identical bands


def _nelder_mead(f, x0, steps, iters=300, tol=1e-6):
    import numpy as np
    pts = [np.array(x0, float)] + [np.array(x0, float) + np.eye(len(x0))[i] * s for i, s in enumerate(steps)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = np.argsort(vals)
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if vals[-1] - vals[0] < tol:
            break
        c = np.mean(pts[:-1], 0)
        xr = c + (c - pts[-1]); fr = f(xr)
        if fr < vals[0]:
            xe = c + 2 * (c - pts[-1]); fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = c + 0.5 * (pts[-1] - c); fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0] + 0.5 * (p - pts[0]) for p in pts]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    i = int(np.argmin(vals))
    return pts[i], vals[i]


def luma_frames(frames, w: int, h: int):
    """Luma planes (float arrays) from yuv420p frames."""
    np = _np()
    return [np.frombuffer(bytes(f[:w * h]), np.uint8).reshape(h, w).astype(np.float32) for f in frames]


def fit(frames, base: dict, log=print) -> tuple[dict, float, float]:
    """(fitted lens, mismatch before, mismatch after) for luma frames, starting from the lens `base`.
    Coarse to fine: a scan of the field of view on blurred frames (the seam can be several degrees off, more
    than fine detail can be matched across), then the full fit on less and less blurred frames."""
    np = _np()
    v = [base["fov"], *base["back_center"], *base["back_ypr"]]
    coarse = _Problem(np, frames, base, 9, band=(82.0, 98.0), n_th=33)
    before = _Problem(np, frames, base, 1).cost(v)
    scan = [(coarse.cost([f, *v[1:]]), f) for f in np.arange(180.0, 205.01, 0.5)]
    v[0] = float(min(scan)[1])
    for k, step in ((9, 1.0), (3, 0.3), (1, 0.1)):
        pr = _Problem(np, frames, base, k)
        v, c = _nelder_mead(pr.cost, v, [step, 0.002 * step, 0.002 * step, step, step, step])
    lens = pr.lens(v)
    lens["fov"] = round(lens["fov"], 2)
    lens["back_center"] = [round(x, 4) for x in lens["back_center"]]
    lens["back_ypr"] = [round(x, 2) for x in lens["back_ypr"]]
    log(f"calibrate: seam mismatch {before:.3f} -> {c:.3f} (0 = perfect); fov {lens['fov']}°, back centre "
        f"{lens['back_center']}, back yaw/pitch/roll {lens['back_ypr']}")
    return lens, before, c


def decode_frames(path: str, count: int = 8):
    """`count` frames spread over a file, as yuv420p bytes, and the frame size."""
    import json
    import subprocess
    import tempfile
    from . import ttts
    tmp = None
    src = path
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic == b"TTTS":
        tmp = tempfile.NamedTemporaryFile(suffix=".hevc")
        with open(path, "rb") as f:
            ttts.demux_video(f, tmp)
        tmp.flush()
        src = tmp.name
    try:
        fmt = ["-f", "hevc"] if src.endswith(".hevc") else []
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", *fmt, "-select_streams", "v:0", "-count_packets", "-show_entries",
             "stream=width,height,nb_read_packets", "-of", "json", src],
            capture_output=True, check=True).stdout)["streams"][0]
        w, h, n = info["width"], info["height"], int(info.get("nb_read_packets") or 1)
        step = max(1, n // count)
        out = subprocess.run(
            ["ffmpeg", "-v", "error", *fmt, "-i", src, "-vf", f"select=not(mod(n\\,{step}))", "-vsync", "0",
             "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
            capture_output=True, check=True).stdout
        size = w * h * 3 // 2
        return [out[i:i + size] for i in range(0, len(out) - size + 1, size)], w, h
    finally:
        if tmp:
            tmp.close()


def run(path: str, log=print) -> int:
    """`python3 -m g360 calibrate FILE`: fit on frames of FILE and save the result for the viewer."""
    from .viewer import CONFIG, load_lens, save_lens
    frames, w, h = decode_frames(path)
    if not frames:
        log(f"calibrate: no frames decoded from {path}")
        return 1
    log(f"calibrate: {len(frames)} frames of {w}x{h} from {path}")
    lens = load_lens()
    fitted, _, _ = fit(luma_frames(frames, w, h), lens, log)
    lens.update(fitted)
    save_lens(lens)
    log(f"calibrate: saved to {CONFIG}")
    return 0


def _main(argv) -> int:
    """`python3 -m g360.calibrate --luma FILE W H LENS_JSON`: the viewer's A key (prints the fitted lens)."""
    import json
    if len(argv) != 5 or argv[0] != "--luma":
        print(_main.__doc__)
        return 2
    path, w, h, base = argv[1], int(argv[2]), int(argv[3]), json.loads(argv[4])
    data = open(path, "rb").read()
    frames = [data[i:i + w * h] for i in range(0, len(data) - w * h + 1, w * h)]
    lens, _, _ = fit(luma_frames(frames, w, h), base, log=lambda m: print(m, file=__import__("sys").stderr))
    print(json.dumps({k: lens[k] for k in FIT_KEYS}))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
