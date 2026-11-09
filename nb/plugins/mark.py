"""Image and video watermark + media re-encode plugin (plugin 5).

One plugin, one media path per file type:
- photo:    overlay the PNG watermark (PIL).
- video:    re-encode with ffmpeg (libx264, audio copied) so the forwarded
            copy gets a new file_unique_id; optionally overlay the watermark
            image on the video in the same pass; optionally prepend an intro
            ad, insert a mid-roll ad, and append an outro ad (each clip is
            re-encoded to match the main video, then joined with the concat
            demuxer).
- audio:    re-encode to AAC (a voice message becomes a plain audio file).
- document: repack a zip with identical contents (archive comment added)
            so the bytes differ; other documents are left alone.

Any failure is fail-open: the message is forwarded unchanged instead of
dropping it. No file-size cap; a job is skipped only when the temp disk
cannot hold the working copies.
"""

import json
import logging
import os
import shutil
import subprocess
import zipfile

from PIL import Image

from nb.multitenant.media_pool import run_cpu
from nb.plugins import NbMessage, NbPlugin

logger = logging.getLogger(__name__)

FFMPEG_TIMEOUT_SECONDS = 300


def _apply_image_watermark(base_path: str, watermark_path: str, position: str) -> str:
    out_path = f"{base_path}.watermarked.png"
    with Image.open(base_path).convert("RGBA") as base_img, Image.open(watermark_path).convert("RGBA") as wm_img:
        bw, bh = base_img.size
        max_wm_w = max(32, bw // 4)
        if wm_img.width > max_wm_w:
            ratio = max_wm_w / float(wm_img.width)
            wm_img = wm_img.resize((max_wm_w, max(16, int(wm_img.height * ratio))))
        ww, wh = wm_img.size
        pad = 16
        pos_map = {
            "top_left": (pad, pad),
            "top_right": (max(0, bw - ww - pad), pad),
            "bottom_left": (pad, max(0, bh - wh - pad)),
            "bottom_right": (max(0, bw - ww - pad), max(0, bh - wh - pad)),
            "centre": (max(0, (bw - ww) // 2), max(0, (bh - wh) // 2)),
        }
        xy = pos_map.get(str(position), pos_map["bottom_right"])
        layer = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
        layer.paste(wm_img, xy, wm_img)
        combined = Image.alpha_composite(base_img, layer)
        combined.save(out_path, format="PNG")
    return out_path


def _run_ffmpeg(args: list[str], timeout: int = FFMPEG_TIMEOUT_SECONDS) -> bool:
    """Run ffmpeg synchronously (called inside run_cpu). True on success."""
    if shutil.which("ffmpeg") is None:
        logger.warning("mark: ffmpeg not found, skipping")
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("mark: ffmpeg failed: %s", type(exc).__name__)
        return False
    if proc.returncode != 0:
        logger.warning(
            "mark: ffmpeg exit %s: %s",
            proc.returncode,
            (proc.stderr or b"")[:300].decode("utf-8", "replace"),
        )
        return False
    return True


def _probe_video(src: str) -> dict:
    """Width, height, fps, duration (seconds), has_audio; {} when unreadable."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-show_entries", "stream=width,height,r_frame_rate,codec_type",
             "-show_entries", "format=duration",
             "-of", "json", src],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        info = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video or not video.get("width"):
        return {}
    fps_raw = str(video.get("r_frame_rate") or "30/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps if fps > 0 else 30.0,
        "duration": float(info.get("format", {}).get("duration") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
    }


_OVERLAY_POS = {
    "top_left": "16:16",
    "top_right": "W-w-16:16",
    "bottom_left": "16:H-h-16",
    "bottom_right": "W-w-16:H-h-16",
    "centre": "(W-w)/2:(H-h)/2",
}


def _normalize_segment(src: str, dst: str, width: int, height: int, fps: float,
                       has_audio: bool, ss: float | None = None,
                       t: float | None = None) -> bool:
    """Re-encode one segment to match the main video (size/fps/audio)."""
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
          f"fps={fps:.3f},format=yuv420p")
    args: list[str] = []
    if ss is not None:
        args += ["-ss", f"{ss:.3f}"]
    if t is not None:
        args += ["-t", f"{t:.3f}"]
    args += ["-i", src]
    if not has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        args += ["-map", "0:v", "-map", "1:a", "-shortest"]
    args += ["-vf", vf,
             "-af", "aresample=48000,aformat=channel_layouts=stereo",
             "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
             "-c:a", "aac", "-b:a", "128k",
             "-video_track_timescale", "600", dst]
    return _run_ffmpeg(args)


def _concat_segments(paths: list[str], dst: str) -> bool:
    list_path = f"{dst}.list.txt"
    try:
        with open(list_path, "w", encoding="utf-8") as fh:
            for path in paths:
                fh.write(f"file '{path}'\n")
        return _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path,
                            "-c", "copy", dst])
    finally:
        if os.path.exists(list_path):
            try:
                os.remove(list_path)
            except OSError:
                pass


def _overlay_watermark(src: str, dst: str, watermark_path: str, position: str) -> bool:
    width = _probe_video(src).get("width", 0)
    wm_tmp = f"{src}.wm.png"
    try:
        with Image.open(watermark_path).convert("RGBA") as wm_img:
            if width > 0:
                max_wm_w = max(32, width // 4)
                if wm_img.width > max_wm_w:
                    ratio = max_wm_w / float(wm_img.width)
                    wm_img = wm_img.resize((max_wm_w, max(16, int(wm_img.height * ratio))))
            wm_img.save(wm_tmp, format="PNG")
    except (OSError, ValueError):
        return False
    if not os.path.exists(wm_tmp):
        return False
    try:
        xy = _OVERLAY_POS.get(str(position), _OVERLAY_POS["bottom_right"])
        return _run_ffmpeg(
            ["-i", src, "-i", wm_tmp, "-filter_complex", f"[0:v][1:v]overlay={xy}",
             "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
             "-c:a", "copy", "-movflags", "+faststart", dst]
        )
    finally:
        if os.path.exists(wm_tmp):
            try:
                os.remove(wm_tmp)
            except OSError:
                pass


def _process_video(src: str, dst: str, data) -> bool:
    """Re-encode + ads + optional watermark. True on success.

    data is the MarkConfig: recode, video_mark, image, position,
    ad_intro/ad_midroll/ad_outro + ad_*_path + ad_midroll_at.
    """
    probe = _probe_video(src)
    if not probe:
        return False
    width, height, fps = probe["width"], probe["height"], probe["fps"]
    duration = probe["duration"]
    wm_path = None
    if getattr(data, "video_mark", False) and getattr(data, "image", ""):
        if os.path.exists(data.image):
            wm_path = data.image

    # Build the segment list: (path, ss, t, has_audio)
    segments: list[tuple[str, float | None, float | None, bool]] = []
    ads: list[tuple[str, str | None]] = []
    if getattr(data, "ad_intro", False) and getattr(data, "ad_intro_path", ""):
        ads.append(("intro", data.ad_intro_path))
    if getattr(data, "ad_midroll", False) and getattr(data, "ad_midroll_path", ""):
        ads.append(("midroll", data.ad_midroll_path))
    if getattr(data, "ad_outro", False) and getattr(data, "ad_outro_path", ""):
        ads.append(("outro", data.ad_outro_path))
    midroll_at = None
    if getattr(data, "ad_midroll", False) and getattr(data, "ad_midroll_path", ""):
        at = float(getattr(data, "ad_midroll_at", 0) or 0)
        if at <= 0 or at >= duration:
            at = duration / 2
        if duration >= 2:
            midroll_at = at

    def _ad_probe(path: str) -> dict:
        info = _probe_video(path)
        return info

    ordered: list[tuple[str, float | None, float | None, bool]] = []
    for slot, path in ads:
        if slot == "intro":
            info = _ad_probe(path)
            if info:
                ordered.append((path, None, None, info["has_audio"]))
    if midroll_at is not None:
        ordered.append((src, None, midroll_at, probe["has_audio"]))
        for slot, path in ads:
            if slot == "midroll":
                info = _ad_probe(path)
                if info:
                    ordered.append((path, None, None, info["has_audio"]))
        ordered.append((src, midroll_at, None, probe["has_audio"]))
    else:
        ordered.append((src, None, None, probe["has_audio"]))
    for slot, path in ads:
        if slot == "outro":
            info = _ad_probe(path)
            if info:
                ordered.append((path, None, None, info["has_audio"]))

    has_ads = len(ordered) > 1
    if not has_ads:
        # Plain re-encode; the watermark overlay (when set) re-encodes in
        # the same pass, so there is no extra step.
        if wm_path:
            return _overlay_watermark(src, dst, wm_path, str(data.position))
        return _run_ffmpeg(
            ["-i", src, "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
             "-c:a", "copy", "-movflags", "+faststart", dst]
        )

    # With ads: normalize every segment, concat, then overlay if needed.
    tmp_dir = f"{dst}.segs"
    os.makedirs(tmp_dir, exist_ok=True)
    seg_paths: list[str] = []
    try:
        for i, (path, ss, t, has_audio) in enumerate(ordered):
            seg_out = os.path.join(tmp_dir, f"seg{i}.mp4")
            if not _normalize_segment(path, seg_out, width, height, fps, has_audio, ss, t):
                return False
            seg_paths.append(seg_out)
        concat_out = os.path.join(tmp_dir, "concat.mp4")
        if not _concat_segments(seg_paths, concat_out):
            return False
        if wm_path:
            return _overlay_watermark(concat_out, dst, wm_path, str(data.position))
        shutil.move(concat_out, dst)
        return os.path.exists(dst)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _process_audio(src: str, dst: str) -> bool:
    return _run_ffmpeg(["-i", src, "-c:a", "aac", "-b:a", "128k", dst])


def _repack_zip(src: str, dst: str) -> bool:
    """Rewrite a zip with identical contents plus an archive comment."""
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
            dst, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            zout.comment = b"nb-mark"
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
        return True
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning("mark: repack zip failed: %s", type(exc).__name__)
        return False


def _is_zip(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in (b"PK\x03\x04", b"PK\x05\x06")
    except OSError:
        return False


def _disk_has_room(path: str, need_bytes: int) -> bool:
    try:
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free
        return free > need_bytes
    except OSError:
        return True


class NbMark(NbPlugin):
    id_ = "mark"

    def __init__(self, data) -> None:
        self.data = data

    async def modify(self, tm: NbMessage) -> NbMessage:
        if not self.data.check:
            return tm
        file_type = tm.file_type
        if file_type not in {"photo", "video", "audio", "document"}:
            # video_note/gif/sticker/contact: re-encoding would break the format.
            return tm
        if file_type == "photo":
            return await self._watermark_photo(tm)
        return await self._recode_media(tm)

    async def _watermark_photo(self, tm: NbMessage) -> NbMessage:
        wm_path = self.data.image
        if not wm_path or not os.path.exists(wm_path):
            return tm
        downloaded = await tm.get_file()
        if not downloaded:
            return tm
        try:
            tm.new_file = await run_cpu(
                _apply_image_watermark, downloaded, wm_path, str(self.data.position)
            )
        finally:
            if os.path.exists(downloaded):
                try:
                    os.remove(downloaded)
                except OSError:
                    pass
        return tm

    async def _recode_media(self, tm: NbMessage) -> NbMessage:
        if not getattr(self.data, "recode", True):
            return tm
        file_type = tm.file_type
        if file_type == "video":
            suffix = ".recode.mp4"
        elif file_type == "audio":
            suffix = ".recode.m4a"
        elif file_type == "document":
            suffix = ".recode.zip"
        else:
            return tm
        downloaded = await tm.get_file()
        if not downloaded:
            return tm
        try:
            size = os.path.getsize(downloaded)
        except OSError:
            return tm
        # No size cap: only skip when the temp disk cannot hold the work.
        # Ads + watermark need up to ~4x the file size in temp copies.
        has_ads = bool(
            (getattr(self.data, "ad_intro", False) and getattr(self.data, "ad_intro_path", ""))
            or (getattr(self.data, "ad_midroll", False) and getattr(self.data, "ad_midroll_path", ""))
            or (getattr(self.data, "ad_outro", False) and getattr(self.data, "ad_outro_path", ""))
        )
        need = size * (4 if (has_ads or getattr(self.data, "video_mark", False)) else 2)
        if not _disk_has_room(downloaded, need):
            logger.warning("mark: temp disk too full, skipping recode")
            try:
                os.remove(downloaded)
            except OSError:
                pass
            return tm
        out_path = f"{downloaded}{suffix}"
        try:
            if file_type == "video":
                ok = await run_cpu(_process_video, downloaded, out_path, self.data)
            elif file_type == "document":
                if not _is_zip(downloaded):
                    return tm
                ok = await run_cpu(_repack_zip, downloaded, out_path)
            else:
                ok = await run_cpu(_process_audio, downloaded, out_path)
            if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                tm.new_file = out_path
            elif os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
        finally:
            if os.path.exists(downloaded):
                try:
                    os.remove(downloaded)
                except OSError:
                    pass
        return tm


MarkPlugin = NbMark
