"""Re-encode media so the forwarded copy gets a new Telegram file_unique_id.

Telegram derives file_unique_id from the file bytes: re-sending identical bytes
keeps the same id, so a forwarded copy can be matched back to the original file.
This plugin rewrites the bytes (video/audio re-encode, zip repack) so the copy
can no longer be matched to the original. Photos are owned by the watermark
plugin (mark); this one covers video, audio/voice and zip documents.

Any failure here is fail-open: the message is forwarded unchanged instead of
dropping it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import zipfile

from nb.multitenant.media_pool import run_cpu
from nb.plugins import NbMessage, NbPlugin

logger = logging.getLogger(__name__)

# ffmpeg is CPU-heavy; never block a forward on it for long.
FFMPEG_TIMEOUT_SECONDS = 300
# Skip files too big to re-encode safely in the container's temp dir.
MAX_RECODE_BYTES = 500 * 1024 * 1024


def _run_ffmpeg(args: list[str]) -> bool:
    """Run ffmpeg synchronously (called inside run_cpu). True on success."""
    if shutil.which("ffmpeg") is None:
        logger.warning("recode: ffmpeg not found, skipping")
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("recode: ffmpeg failed: %s", type(exc).__name__)
        return False
    if proc.returncode != 0:
        logger.warning(
            "recode: ffmpeg exit %s: %s",
            proc.returncode,
            (proc.stderr or b"")[:300].decode("utf-8", "replace"),
        )
        return False
    return True


def _recode_video(src: str, dst: str) -> bool:
    # Re-encode the video stream; keep the audio stream untouched.
    # The container is rebuilt, so the bytes (and file_unique_id) change.
    return _run_ffmpeg(
        ["-i", src, "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
         "-c:a", "copy", "-movflags", "+faststart", dst]
    )


def _recode_audio(src: str, dst: str) -> bool:
    # Re-encode audio to AAC; a voice message becomes a plain audio file.
    return _run_ffmpeg(["-i", src, "-c:a", "aac", "-b:a", "128k", dst])


def _repack_zip(src: str, dst: str) -> bool:
    """Rewrite a zip with identical contents plus an archive comment.

    The comment alone guarantees the bytes differ while every file inside
    stays byte-identical and the archive stays valid.
    """
    try:
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
            dst, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            zout.comment = b"nb-recode"
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
        return True
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning("recode: repack zip failed: %s", type(exc).__name__)
        return False


def _is_zip(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in (b"PK\x03\x04", b"PK\x05\x06")
    except OSError:
        return False


class NbRecode(NbPlugin):
    id_ = "recode"

    def __init__(self, data) -> None:
        self.data = data

    async def modify(self, tm: NbMessage) -> NbMessage:
        if not self.data.check:
            return tm
        file_type = tm.file_type
        if file_type == "video":
            suffix, worker = ".recode.mp4", _recode_video
        elif file_type == "audio":
            suffix, worker = ".recode.m4a", _recode_audio
        elif file_type == "document":
            suffix, worker = ".recode.zip", None
        else:
            # photo: watermark plugin owns it; video_note/gif/sticker/contact:
            # re-encoding would break the format, so leave them alone.
            return tm
        downloaded = await tm.get_file()
        if not downloaded:
            return tm
        try:
            too_big = os.path.getsize(downloaded) > MAX_RECODE_BYTES
        except OSError:
            return tm
        if too_big:
            logger.warning("recode: file too big, skipping")
            try:
                os.remove(downloaded)
            except OSError:
                pass
            return tm
        out_path = f"{downloaded}{suffix}"
        try:
            if file_type == "document":
                if not _is_zip(downloaded):
                    return tm
                ok = await run_cpu(_repack_zip, downloaded, out_path)
            else:
                ok = await run_cpu(worker, downloaded, out_path)
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


RecodePlugin = NbRecode
