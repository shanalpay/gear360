"""Live-view sinks. The camera's stream is Samsung's TTTS container carrying HEVC (see g360.ttts);
we demux it to an HEVC elementary stream and hand that to ffmpeg/ffplay.

Sinks: raw capture to a file, an ffplay window, an HEVC/MP4 file, or a v4l2loopback device for OBS.
The picture is dual fisheye (both lenses side by side); stitching comes later.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import IO

from . import ttts


def record_raw(src: IO[bytes], path: str, seconds: float) -> int:
    """Save the untouched TTTS stream for `seconds` (for analysis); returns bytes written."""
    n, end = 0, time.monotonic() + seconds
    with open(path, "wb") as f:
        while time.monotonic() < end:
            buf = src.read(1 << 16)
            if not buf:
                break
            f.write(buf)
            n += len(buf)
    return n


def _feed(src: IO[bytes], proc: subprocess.Popen, info_box: list) -> None:
    try:
        info_box.append(ttts.demux_video(src, proc.stdin))
    except Exception as e:  # surface parse errors instead of hanging the consumer
        print(f"g360: stream error: {e}", file=sys.stderr)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def _run(cmd: list[str], src: IO[bytes]) -> int:
    if not shutil.which(cmd[0]):
        raise RuntimeError(f"{cmd[0]} not installed")
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    box: list = []
    threading.Thread(target=_feed, args=(src, p, box), daemon=True).start()
    try:
        return p.wait()
    except KeyboardInterrupt:
        p.terminate()
        return p.wait()


# Raw HEVC carries no timestamps: tell ffmpeg the camera's rate (TTTS pts step 4004 @ 120 kHz = 29.97 fps).
_HEVC_IN = ["-f", "hevc", "-framerate", "30000/1001", "-i", "pipe:0"]
_QUIET = ["-hide_banner", "-loglevel", "warning"]
_LOW_LATENCY = ["-fflags", "nobuffer", "-probesize", "2000000", "-analyzeduration", "500000"]


def play(src: IO[bytes]) -> int:
    return _run(["ffplay", *_QUIET, *_LOW_LATENCY, "-window_title", "Gear 360 live (dual fisheye)", *_HEVC_IN], src)


def play_looped(src: IO[bytes]) -> int:
    """Demux a saved stream to a temporary HEVC file and loop it in ffplay until the window is closed."""
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".hevc", prefix="g360-replay-")
    try:
        with os.fdopen(fd, "wb") as f:
            ttts.demux_video(src, f)
        return subprocess.call(["ffplay", *_QUIET, "-window_title", "Gear 360 replay (dual fisheye, loops)",
                                "-loop", "0", "-f", "hevc", "-framerate", "30000/1001", "-i", path])
    finally:
        os.unlink(path)


def to_file(src: IO[bytes], path: str, seconds: float | None = None) -> int:
    """Remux to MP4/MKV (no re-encode)."""
    dur = ["-t", str(seconds)] if seconds else []
    # -loglevel error: stream-copying raw HEVC makes the muxer warn "pts has no value" per packet even though
    # +genpts fills them (output verified: 29.97 fps, correct duration).
    return _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", *_HEVC_IN, *dur,
                 "-c", "copy", "-y", path], src)


def to_v4l2(src: IO[bytes], device: str = "/dev/video10", width: int = 1920, height: int = 960) -> int:
    """Publish as a virtual webcam (needs v4l2loopback: `sudo modprobe v4l2loopback video_nr=10
    card_label="Gear 360" exclusive_caps=1`). Scaled down by default; OBS handles 1920x960 easily."""
    return _run(["ffmpeg", *_QUIET, *_LOW_LATENCY, *_HEVC_IN, "-vf", f"scale={width}:{height}",
                 "-pix_fmt", "yuv420p", "-f", "v4l2", device], src)
