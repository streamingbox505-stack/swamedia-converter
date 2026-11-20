"""ffprobe/ffmpeg: choose real video + audio, remux-or-transcode to H.264/yuv420p/AAC +faststart, validate."""
from __future__ import annotations
import json, os, struct, subprocess


class ConvertError(Exception):
    pass


def probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ConvertError("ffprobe failed: " + r.stderr[:200])
    return json.loads(r.stdout)


def pick_streams(info: dict):
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    if not vids:
        raise ConvertError("no real video stream (poster/attached_pic or non-video)")
    v = max(vids, key=lambda s: int(s.get("width") or 0) * int(s.get("height") or 0))
    auds = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    a = max(auds, key=lambda s: (s.get("disposition", {}).get("default", 0), int(s.get("channels") or 0))) if auds else None
    return v, a


def can_remux(v: dict, a: dict | None) -> bool:
    return (v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p"
            and int(v.get("width") or 1) % 2 == 0 and int(v.get("height") or 1) % 2 == 0
            and (a is None or a.get("codec_name") == "aac"))


def build_cmd(src: str, dst: str, v: dict, a: dict | None, remux: bool) -> list[str]:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-map", f"0:{v['index']}"]
    if a is not None:
        cmd += ["-map", f"0:{a['index']}"]
    cmd += ["-sn", "-dn", "-map_metadata", "-1"]
    if remux:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"] if a is not None else ["-an"]
    return cmd + ["-movflags", "+faststart", dst]


def convert(src: str, dst: str, timeout: int = 6 * 3600) -> dict:
    info = probe(src)
    v, a = pick_streams(info)
    remux = can_remux(v, a)
    r = subprocess.run(build_cmd(src, dst, v, a, remux), capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        if remux:  # remux failed -> transcode
            remux = False
            r = subprocess.run(build_cmd(src, dst, v, a, False), capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise ConvertError("ffmpeg failed: " + r.stderr[-300:])
    rep = validate_output(dst, src_duration=float(info.get("format", {}).get("duration") or 0), expect_audio=a is not None)
    rep.update(mode="remux" if remux else "transcode", source_has_audio=a is not None)
    return rep


def moov_before_mdat(path: str) -> bool:
    order = []
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        pos = 0
        while pos < size and len(order) < 50:
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                break
            sz, typ = struct.unpack(">I4s", hdr[:8])
            if sz == 1:
                sz = struct.unpack(">Q", hdr[8:16])[0]
            elif sz == 0:
                sz = size - pos
            order.append(typ.decode("latin1"))
            if sz < 8:
                break
            pos += sz
    return "moov" in order and "mdat" in order and order.index("moov") < order.index("mdat")


def validate_output(path: str, src_duration: float = 0, expect_audio: bool = True, tol: float = 2.0) -> dict:
    info = probe(path)
    v, a = pick_streams(info)
    dur = float(info.get("format", {}).get("duration") or 0)
    if dur <= 0:
        raise ConvertError("non-positive duration")
    if src_duration and abs(dur - src_duration) > max(tol, src_duration * 0.02):
        raise ConvertError(f"duration drift {dur} vs {src_duration}")
    if v.get("codec_name") != "h264" or v.get("pix_fmt") != "yuv420p":
        raise ConvertError("output is not h264/yuv420p")
    if int(v["width"]) % 2 or int(v["height"]) % 2 or int(v["width"]) <= 0:
        raise ConvertError("bad dimensions")
    if expect_audio and (a is None or a.get("codec_name") != "aac"):
        raise ConvertError("source had audio but output lacks AAC")
    if not moov_before_mdat(path):
        raise ConvertError("moov not before mdat (no faststart)")
    d = subprocess.run(["ffmpeg", "-v", "error", "-t", "10", "-i", path, "-f", "null", "-"], capture_output=True, text=True)
    if d.returncode != 0 or d.stderr.strip():
        raise ConvertError("decode check failed: " + d.stderr[:200])
    return {"duration": dur, "width": int(v["width"]), "height": int(v["height"]), "video": v["codec_name"],
            "audio": a.get("codec_name") if a else None, "size": os.path.getsize(path)}
