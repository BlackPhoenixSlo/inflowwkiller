"""Shrink oversized video before it goes to OnlyFans.

WHY THIS IS WORTH DOING AT ALL. OnlyFans transcodes every upload to its own
H.264 ladder regardless of what you send, so a 2 GB ProRes or a 443 MB
screen-recording arrives at the same place a 60 MB H.264 would — after costing
you the whole upload. Worse, `convert.onlyfans.com` answers **504 Gateway
Time-out** on large objects (measured: 443 MB fails, 56 MB is fine), so the big
file does not merely waste bandwidth, it fails outright *after* every S3 part
has been stored.

So: re-encode locally first, and send something OF will actually accept. The
output is H.264/AAC in an MP4 faststart container — the format its player wants
anyway — at a bitrate chosen to land under a target size.

This is best-effort by design. No ffmpeg, an unreadable file, a codec ffmpeg
won't touch: `prepare()` returns the original path and the caller uploads what
it had. A compression step must never be the reason an import fails.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("of-relay.media_prep")

# Above this, a video is re-encoded before upload. Set from what OnlyFans
# actually accepts, with headroom — see VAULT_UPLOAD_MAX_MB in vault_upload.
COMPRESS_OVER_MB = int(os.environ.get("VAULT_COMPRESS_OVER_MB") or 120)
# What we aim the re-encode at. Kept under VAULT_UPLOAD_MAX_MB, because bitrate
# targeting is approximate and an overshoot is a rejected file. Raising this
# buys resolution on long video (an hour at 90 MB is 360p; at 190 MB it is
# 480p) and spends the headroom under OnlyFans' ceiling.
TARGET_MB = int(os.environ.get("VAULT_COMPRESS_TARGET_MB") or 190)
# Never upscale quality: cap the long edge and the framerate.
MAX_HEIGHT = int(os.environ.get("VAULT_COMPRESS_MAX_HEIGHT") or 1080)
AUDIO_KBPS = 128

_VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".wmv"}


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def probe_duration_s(path: Path) -> float | None:
    """Seconds, or None if ffprobe can't say — which is not an error here."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=60, check=True).stdout
        dur = json.loads(out).get("format", {}).get("duration")
        return float(dur) if dur else None
    except Exception:  # noqa: BLE001 — probing is advisory
        log.debug("ffprobe failed on %s", path, exc_info=True)
        return None


def is_video(path: Path) -> bool:
    return path.suffix.lower() in _VIDEO_SUFFIXES


def probe_height(path: Path) -> int | None:
    """Video height in pixels, or None if ffprobe can't say."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-print_format", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True).stdout
        streams = json.loads(out).get("streams") or []
        h = streams[0].get("height") if streams else None
        return int(h) if h else None
    except Exception:  # noqa: BLE001 — probing is advisory
        log.debug("ffprobe height failed on %s", path, exc_info=True)
        return None


def needs_compression(path: Path, *, over_mb: int = COMPRESS_OVER_MB) -> bool:
    """True if the file is oversized OR over-resolution.

    Resolution matters independently of size: a short 8K clip can sit under the
    size threshold and still be pointless to upload at full resolution, because
    OnlyFans re-encodes it down regardless. Sending 8K to be thrown away costs
    the operator's upload time and nothing else. Downscaling 8K is cheap —
    measured at ~3x realtime on two cores — so the trade is firmly worth it.
    """
    try:
        if not is_video(path):
            return False
        if path.stat().st_size > over_mb * 1024 * 1024:
            return True
        h = probe_height(path)
        return bool(h and h > MAX_HEIGHT)
    except OSError:
        return False


def compress(path: Path, dest_dir: Path, *, target_mb: int = TARGET_MB) -> Path | None:
    """Re-encode `path` to H.264/AAC MP4 under ~`target_mb`. None on any failure.

    Bitrate is derived from the duration, so a long video gets a lower bitrate
    rather than blowing the budget. Without a duration we fall back to CRF,
    which cannot be size-targeted — hence the size check afterwards, which
    rejects a result that came out no smaller than the source.
    """
    if not have_ffmpeg():
        log.info("media_prep: ffmpeg not installed — uploading %s as-is", path.name)
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{path.stem}_compressed.mp4"
    duration = probe_duration_s(path)

    # Audio is a fixed cost per second, so on a long video it quietly eats the
    # budget the picture needs: 128k over 54 minutes is ~58 MB of a 90 MB
    # target. Halve it past ten minutes — speech and music both survive 64k far
    # better than the video survives being starved of bits.
    audio_kbps = AUDIO_KBPS if not duration or duration <= 600 else 64
    common = [
        "ffmpeg", "-nostdin", "-y", "-i", str(path),
        "-vf", f"scale=-2:'min({MAX_HEIGHT},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
    ]
    if duration and duration > 1:
        # Bitrate the size target actually allows, once audio is paid for.
        video_kbps = int((target_mb * 8 * 1024) / duration - audio_kbps)
        # Pick the largest resolution that bitrate can carry. This is the whole
        # trick for long video: an hour-long clip gets ~75 kbps at 90 MB, and
        # holding 1080p there would either look like mud or (with a bitrate
        # floor) blow straight past the target — a real 990 MB / ~1 h file came
        # out at 210 MB that way and got rejected. Dropping resolution spends
        # the few bits available on something watchable AND hits the size.
        height = MAX_HEIGHT
        for h, needed in ((MAX_HEIGHT, 1500), (720, 800), (480, 400), (360, 200)):
            if h > MAX_HEIGHT:
                continue
            if video_kbps >= needed:
                height = h
                break
            height = h      # keep stepping down; last one wins
        video_kbps = max(video_kbps, 150)    # a real floor, but a low one
        cmd = [c if c != f"scale=-2:'min({MAX_HEIGHT},ih)'"
               else f"scale=-2:'min({height},ih)'" for c in common]
        cmd += ["-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k",
                "-bufsize", f"{video_kbps * 2}k", str(out)]
        log.info("media_prep: %s (%.0f MB, %.0fs) -> %dp @ %dk video + %dk audio",
                 path.name, path.stat().st_size / 1e6, duration, height,
                 video_kbps, audio_kbps)
    else:
        cmd = common + ["-crf", "26", str(out)]
        log.info("media_prep: %s — no duration, falling back to CRF 26", path.name)

    try:
        # Generous but bounded: a long re-encode is fine, a hung one is not.
        subprocess.run(cmd, capture_output=True, timeout=3600, check=True)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"")[-400:].decode("utf-8", "replace")
        log.warning("media_prep: ffmpeg failed on %s — %s", path.name, tail)
        out.unlink(missing_ok=True)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("media_prep: ffmpeg error on %s — %s", path.name, e)
        out.unlink(missing_ok=True)
        return None

    if not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        return None
    if out.stat().st_size >= path.stat().st_size:
        # Already efficiently encoded — re-encoding bought nothing and would
        # only cost quality. Keep the original.
        log.info("media_prep: %s did not shrink (%.0f -> %.0f MB), keeping original",
                 path.name, path.stat().st_size / 1e6, out.stat().st_size / 1e6)
        out.unlink(missing_ok=True)
        return None
    log.info("media_prep: %s %.0f MB -> %.0f MB", path.name,
             path.stat().st_size / 1e6, out.stat().st_size / 1e6)
    return out


def prepare(path: str | Path, dest_dir: str | Path, *,
            over_mb: int = COMPRESS_OVER_MB,
            target_mb: int = TARGET_MB) -> tuple[Path, bool]:
    """`(path_to_upload, was_compressed)`. Never raises; falls back to the original."""
    p = Path(path)
    if not needs_compression(p, over_mb=over_mb):
        return p, False
    try:
        smaller = compress(p, Path(dest_dir), target_mb=target_mb)
    except Exception:  # noqa: BLE001 — compression is an optimisation, not a gate
        log.exception("media_prep: unexpected failure preparing %s", p)
        return p, False
    return (smaller, True) if smaller else (p, False)
