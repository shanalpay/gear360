"""Virtual webcams for OBS and video calls: the stitched picture written to v4l2loopback devices.

Two outputs, each a separate camera device (set up once with `sudo sh tools/setup_webcam.sh`):
  normal    "Gear 360"           1920x1080, the viewer's current view (mode, direction and zoom; no controls)
  equirect  "Gear 360 equirect"  2560x1280 equirectangular 360°, level and facing the camera's front, for 360°
                                  live streams (YouTube: mark the stream as 360° in YouTube Studio)

Rendering runs headless in its own process (`python3 -m g360.webcam`) on an EGL context, preferring the NVIDIA
GPU: on this PC the X screen belongs to the Intel iGPU, which the viewer window, OBS and the side panel already
share (the outputs ran at ~15 fps there). The viewer starts it with the Webcam button, sends it the camera's HEVC
stream (from the next keyframe) and the current view as JSON lines; `g360 live --webcam` runs it in-process
with no window (normal view: straight ahead). Pixels come back through two alternating pixel buffers (one
frame of delay, no stall) and a thread per device hands the newest frame to ffmpeg (I420 into the device).
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import socket
import subprocess
import sys
import threading
import time

OUTPUTS = {   # name: (device label set by tools/setup_webcam.sh, default device, size)
    "normal": ("Gear 360", "/dev/video10", (1920, 1080)),
    "equirect": ("Gear 360 equirect", "/dev/video11", (2560, 1280)),
}
FPS = 30
SETUP_HINT = "set up the virtual webcams once with: sudo sh tools/setup_webcam.sh"


def find_device(label: str, default: str) -> str | None:
    """The /dev/videoN whose v4l2 name is `label` (falls back to `default` if that is a loopback device)."""
    base = pathlib.Path("/sys/class/video4linux")
    for d in sorted(base.glob("video*")):
        try:
            if (d / "name").read_text().strip() == label:
                return f"/dev/{d.name}"
        except OSError:
            pass
    dflt = base / pathlib.Path(default).name
    try:
        if "loopback" in (dflt / "name").read_text().lower() or (dflt / "device" / "driver").resolve().name == "v4l2loopback":
            return default
    except OSError:
        pass
    return None


class Output:
    """One device: `push(rgba_bottom_up)` from the render loop; the newest frame is written by a thread."""

    def __init__(self, name: str, device: str, size: tuple[int, int], sink: list[str] | None = None):
        self.name, self.device, self.size = name, device, size
        w, h = size
        sink = sink or ["-pix_fmt", "yuv420p", "-f", "v4l2", device]
        self.proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                                      "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "pipe:0",
                                      "-vf", "vflip", *sink], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self.frame: bytes | None = None
        self.error: str | None = None
        self.frames = 0
        self._cv = threading.Condition()
        self._open = True
        threading.Thread(target=self._write, daemon=True).start()

    def push(self, rgba: bytes):
        with self._cv:
            self.frame = rgba
            self._cv.notify()

    def _write(self):
        while True:
            with self._cv:
                while self._open and self.frame is None:
                    self._cv.wait()
                if not self._open:
                    return
                frame, self.frame = self.frame, None
            try:
                self.proc.stdin.write(frame)
                self.frames += 1
            except OSError:
                err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""
                self.error = err.splitlines()[-1] if err else "ffmpeg stopped"
                return

    def alive(self) -> bool:
        return self.error is None and self.proc.poll() is None

    def close(self):
        with self._cv:
            self._open = False
            self._cv.notify()
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def open_outputs(log=print, sinks: dict | None = None) -> dict[str, Output]:
    """Both outputs whose devices exist (none: logs how to set them up). `sinks` replaces the v4l2 sink per
    output (tests)."""
    outs = {}
    for name, (label, default, size) in OUTPUTS.items():
        sink = (sinks or {}).get(name)
        dev = default if sink else find_device(label, default)
        if not dev:
            continue
        outs[name] = Output(name, dev, size, sink)
        log(f"webcam: {name} view on {dev} ({label}, {size[0]}x{size[1]})")
    if not outs:
        log(f"webcam: no virtual camera devices; {SETUP_HINT}")
    return outs


# ---- rendering (in the webcam process, or in-process for `live --webcam`) ----
def egl_context(log=print):
    """A headless OpenGL context, on the NVIDIA GPU if there is one (G360_EGL_DEVICE=N picks a device)."""
    from .viewer import _gl_modules
    _, mgl = _gl_modules()
    want = os.environ.get("G360_EGL_DEVICE")
    found = []
    for i in ([int(want)] if want else range(8)):
        try:
            ctx = mgl.create_standalone_context(backend="egl", device_index=i)
        except Exception:
            continue
        name = ctx.info["GL_RENDERER"]
        if want or "NVIDIA" in name:
            log(f"webcam: rendering on {name}")
            return ctx, mgl
        found.append((ctx, name))
    for ctx, name in found:                     # no NVIDIA: any GPU, software rendering last
        if "llvmpipe" not in name:
            log(f"webcam: rendering on {name}")
            return ctx, mgl
    if found:
        log(f"webcam: rendering on {found[0][1]} (software, slow)")
        return found[0][0], mgl
    raise RuntimeError("no EGL device for headless rendering")


DEFAULT_STATE = {"mode": 0, "rot": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "fov": 90.0, "lens": None, "proj": 0}


def serve(src, control=None, log=print, sinks: dict | None = None) -> int:
    """Publish `src` (a viewer.FrameSource) on the virtual webcams until it ends or `control` (a file of JSON
    view-state lines, None: fixed straight-ahead view) closes."""
    from .viewer import Stitcher, _eye, load_lens
    outs = open_outputs(log, sinks)
    if not outs:
        src.close()
        return 2
    ctx, mgl = egl_context(log)
    st = Stitcher(ctx, mgl)
    state = dict(DEFAULT_STATE, lens=load_lens(), proj=src.projection)
    lock, stop = threading.Lock(), threading.Event()

    def read_control():
        for line in control:
            try:
                with lock:
                    state.update(json.loads(line))
            except ValueError:
                pass
        stop.set()
    if control is not None:
        threading.Thread(target=read_control, daemon=True).start()
    fbos, pbos = {}, {}
    for name, out in outs.items():
        fbos[name] = ctx.framebuffer(color_attachments=[ctx.renderbuffer(out.size, 4)])
        n = out.size[0] * out.size[1] * 4
        pbos[name] = [[ctx.buffer(reserve=n), ctx.buffer(reserve=n)], 0, False]
    shown, last_state, t_last, frames, t_log = -1, None, 0.0, 0, time.monotonic()
    try:
        while not stop.is_set():
            seq, frame = src.latest()
            if frame is None:
                if src.ended:
                    break
                time.sleep(0.005)
                continue
            with lock:
                cur = json.dumps(state, sort_keys=True)
                view = json.loads(cur)
            now = time.monotonic()
            new = frame is not None and seq != shown
            if not new and (cur == last_state or now - t_last < 1.0 / FPS):
                if src.ended and not src.still:
                    break
                time.sleep(0.002)
                continue
            if new:
                st.upload(frame, src.width, src.height)
                shown = seq
            last_state, t_last = cur, now
            for name, out in outs.items():
                fbos[name].use()
                if name == "equirect":
                    st.draw(out.size, 2, _eye(), 90.0, view["lens"], proj=view["proj"])
                else:
                    st.draw(out.size, view["mode"], view["rot"], view["fov"], view["lens"], proj=view["proj"])
            for name, out in outs.items():
                bufs, i, filled = pb = pbos[name]
                fbos[name].read_into(bufs[i], components=4)
                if filled:
                    out.push(bufs[1 - i].read())
                pb[1], pb[2] = 1 - i, True
            frames += 1
            dead = [o for o in outs.values() if not o.alive()]
            if dead:
                log(f"webcam: {dead[0].device}: {dead[0].error or 'stopped'}")
                return 1
            if now - t_log >= 30:
                log(f"webcam: {frames / (now - t_log):.1f} fps")
                frames, t_log = 0, now
    except KeyboardInterrupt:
        pass
    finally:
        for o in outs.values():
            o.close()
        src.close()
    return 0


def _main(argv) -> int:
    """python3 -m g360.webcam (--stream WxH | --file PATH) [--control-fd N]
    --stream: the HEVC elementary stream on stdin, from a keyframe (what the viewer sends)."""
    import argparse
    from . import viewer
    ap = argparse.ArgumentParser(prog="g360.webcam")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stream", metavar="WxH")
    g.add_argument("--file")
    ap.add_argument("--control-fd", type=int)
    a = ap.parse_args(argv)
    log = lambda m: print(m, file=sys.stderr, flush=True)
    if a.file:
        src = viewer.file_source(a.file)
    else:
        w, h = (int(v) for v in a.stream.split("x"))
        stdin = sys.stdin.buffer

        def feed(dst):
            while True:
                b = stdin.read1(1 << 16)
                if not b:
                    return
                dst.write(b)
        src = viewer.FrameSource(viewer.live_decode_cmd(), w, h, feed=feed)
        src.live = True
    control = os.fdopen(a.control_fd, "r") if a.control_fd is not None else None
    sinks = json.loads(os.environ["G360_WEBCAM_SINKS"]) if os.environ.get("G360_WEBCAM_SINKS") else None
    return serve(src, control, log, sinks)


# ---- the viewer's side ----
class Link:
    """The viewer's handle on a webcam process: feeds it the stream (live) and the view state."""

    def __init__(self, src, log=print, sinks: dict | None = None):
        from .viewer import ROOT
        self.src, self.log = src, log
        mine, theirs = socket.socketpair()
        self.ctl = mine.makefile("w")
        self._sock = mine
        args = ["--stream", f"{src.width}x{src.height}"] if src.live else ["--file", src.path]
        env = dict(os.environ)
        if sinks:
            env["G360_WEBCAM_SINKS"] = json.dumps(sinks)
        self.proc = subprocess.Popen([sys.executable, "-m", "g360.webcam", *args, "--control-fd",
                                      str(theirs.fileno())], cwd=str(ROOT), env=env,
                                     stdin=subprocess.PIPE if src.live else subprocess.DEVNULL,
                                     pass_fds=[theirs.fileno()])
        theirs.close()
        self.sent = None
        self._q: queue.Queue = queue.Queue(maxsize=90)
        self._synced = False                      # sending from a keyframe on
        if src.live:
            threading.Thread(target=self._pump, daemon=True).start()
            src.tees.append(self.feed)

    def feed(self, b: bytes):
        """From the viewer's feeder thread: never blocks; if the process falls behind, skip to a keyframe."""
        if not self._synced:
            if b"\x00\x00\x01\x40\x01" not in b:
                return
            self._synced = True
        try:
            self._q.put_nowait(b)
        except queue.Full:
            self._synced = False

    def _pump(self):
        while True:
            b = self._q.get()
            if b is None:
                break
            try:
                self.proc.stdin.write(b)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                break
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def send(self, state: dict):
        line = json.dumps(state)
        if line == self.sent:
            return
        self.sent = line
        try:
            self.ctl.write(line + "\n")
            self.ctl.flush()
        except OSError:
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self):
        if self.feed in self.src.tees:
            self.src.tees.remove(self.feed)
        self._q.put(None)
        try:
            self.ctl.close()
            self._sock.close()
        except OSError:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
