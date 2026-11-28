"""Demuxer for the Gear 360's live-view stream ("livestream_*.avi" on port 7679).

Despite the .avi name and "Content-Type: video/x-avi", it is Samsung's own container (all integers
big-endian), reverse-engineered from an 8-second live capture (fw 0.83):

  "TTTS" u32 0 u32 3                     file header (3 = number of track blocks that follow?)
  (header blocks: tag, u32 length incl. the length field itself, then length-4 body bytes)
  "VID0" u32 len=0x60 ...                video track: u32 1, u32 2, u32 width, u32 height, u32 1,
                                         u32 bitrate (22_000_000), u32 15, u32 1, u32 timescale (120000),
                                         then "VD00" sub-block (frame-duration table, not needed)
  "AUD0" u32 len=0x4c ...                audio track header (+ "AU00" sub-block); no audio chunks seen
  "LIST" u32 0 "movi"                    start of data
  then repeated chunks:
      id[4] ("00VD" video | "??AU" audio)  u32 payload_size  u64 pts (1/timescale s)  payload

Each 00VD payload is one HEVC (H.265 Main) access unit in Annex-B form; the first carries VPS/SPS/PPS.
The picture is the two raw fisheye images side by side (2560x1280 for "high"), not stitched.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import IO, Iterator


class TTTSError(ValueError):
    pass


@dataclass
class StreamInfo:
    width: int
    height: int
    bitrate: int
    timescale: int


@dataclass
class Chunk:
    kind: str          # "video" | "audio"
    track_id: bytes    # e.g. b"00VD"
    pts: int           # in StreamInfo.timescale units
    data: bytes


def _read_exact(f: IO[bytes], n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        b = f.read(n - len(buf))
        if not b:
            raise EOFError
        buf += b
    return bytes(buf)


def read_header(f: IO[bytes]) -> StreamInfo:
    """Consume everything up to and including 'LIST....movi'; return the video parameters."""
    head = _read_exact(f, 12)
    if head[:4] != b"TTTS":
        raise TTTSError(f"not a TTTS stream: {head[:16]!r}")
    info = None
    while True:
        tag = _read_exact(f, 4)
        if tag == b"LIST":
            _read_exact(f, 4)                      # size (0)
            if _read_exact(f, 4) != b"movi":
                raise TTTSError("LIST without movi")
            break
        # block = tag + u32 length + (length - 4) body bytes: the length counts its own field, not the tag
        # (VID0 at 12 with length 0x60 -> AUD0 at 112; AUD0 length 0x4c -> LIST at 192).
        (length,) = struct.unpack(">I", _read_exact(f, 4))
        if length < 4:
            raise TTTSError(f"bad {tag!r} block length {length}")
        body = _read_exact(f, length - 4)
        if tag == b"VID0":
            # body: u32 1, u32 codec(2), u32 w, u32 h, u32 1, u32 bitrate, u32 15, u32 1, u32 timescale, ...
            _, _, w, h, _, br, _, _, ts = struct.unpack(">9I", body[:36])
            info = StreamInfo(w, h, br, ts)
    if info is None:
        raise TTTSError("no VID0 block before data")
    return info


def iter_chunks(f: IO[bytes]) -> Iterator[Chunk]:
    """Yield chunks until EOF (call after read_header)."""
    while True:
        try:
            hdr = _read_exact(f, 16)
        except EOFError:
            return
        tid, size, pts = hdr[:4], *struct.unpack(">IQ", hdr[4:])
        if tid[2:] not in (b"VD", b"AU"):
            raise TTTSError(f"unknown chunk id {tid!r}")
        try:
            data = _read_exact(f, size)
        except EOFError:
            return                                  # stream cut mid-chunk
        yield Chunk("video" if tid.endswith(b"VD") else "audio", tid, pts, data)


def demux_video(src: IO[bytes], dst: IO[bytes], log=None, every: float = 10.0) -> StreamInfo:
    """Copy the HEVC elementary stream from a TTTS stream to `dst` (e.g. ffmpeg/ffplay stdin).

    With `log`, reports pacing: how far the received video runs ahead of real time since the first frame
    (a burst of frames the camera had buffered, which a player that paces by timestamps never catches up)."""
    import time
    info = read_header(src)
    t0 = p0 = None
    last = shown = 0.0
    frames = 0
    try:
        for c in iter_chunks(src):
            if c.kind != "video":
                continue
            dst.write(c.data)
            if log is None:
                continue
            now = time.monotonic()
            if t0 is None:
                t0, p0, last = now, c.pts, now
                continue
            frames += 1
            ahead = (c.pts - p0) / info.timescale - (now - t0)
            if now - last >= (3.0 if not shown else every):
                log(f"stream: {frames / (now - t0):.1f} fps received, video {ahead:+.2f} s vs real time "
                    f"(positive = the camera sent a buffered burst)")
                last, shown = now, 1.0
    except (BrokenPipeError, ValueError):
        pass
    return info
