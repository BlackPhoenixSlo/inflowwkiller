"""Normalise a video's CONTAINER before it goes to OnlyFans.

WHAT THIS MODULE USED TO BELIEVE, AND WHY IT WAS WRONG. It used to re-encode
every video over 120 MB down to a ~190 MB byte budget, on the theory that
`convert.onlyfans.com` answers **504 Gateway Time-out** on large objects. A HAR
capture of the real OnlyFans web client overturns that: a **2.31 GiB .mp4** went
up end to end and was registered without complaint, while the **443 MB .mov** in
the same capture failed at convert alone, with every S3 part already stored.
The evidence — timings, part counts, the twelve 504s and their durations — is
recorded ONCE, in the `_CONVERT_TIMEOUT_S` comment in `of_client.py`; it is not
restated here, because it used to be restated everywhere and every copy of it
was wrong at the same time.

The variable between those two files was the CONTAINER, not the byte count.
That is one .mov failing twice against one large .mp4 succeeding once: enough to
prove size is not the gate and to make the container the prime suspect; NOT
enough to claim every .mov fails.

So the job here is no longer "shrink". It is "hand OF a container its
transcoder is known to accept, at the least possible cost":

  * H.264/HEVC already in a .mp4  → pass through untouched, whatever the size.
  * H.264/HEVC in a .mov/.mkv/... → **remux**: the same encoded picture rewrapped
    into a faststart mp4. `-map 0:<real video stream> -map 0:a? -c:v copy
    [-c:a copy | -c:a aac] -movflags +faststart` — lossless picture, seconds,
    no re-encode. (A bare `-c copy` would in fact FAIL here: it is exactly the
    .mov timecode and data streams that a copy of everything cannot carry into
    mp4, which is why the maps are explicit.)
  * ProRes/DNxHD/anything MP4 cannot carry → re-encode to H.264/AAC mp4 at a
    QUALITY target (CRF), never at a byte budget.

`compress()` — the old size-targeted re-encode — is kept as an opt-in escape
hatch behind `VAULT_COMPRESS_OVER_MB`, defaulted high enough to be off.

RETIRED, ON PURPOSE: there used to be a resolution check that downscaled any
source taller than `MAX_HEIGHT` even when its codec and container were already
fine. It is gone. Downscaling an 8K h264 .mp4 costs a full re-encode to save
bytes that were never the problem, and OnlyFans re-encodes to its own ladder
anyway, so the fan sees OF's rendition either way. `MAX_HEIGHT` now applies
only to the two paths that were re-encoding regardless (the escape hatch and
the ProRes-class transcode), where the frame size is a free choice rather than
a reason to spend an hour of CPU.

This is best-effort by design. No ffmpeg, no ffprobe, an unreadable file, a
codec ffmpeg won't touch: `prepare()` returns the original path and the caller
uploads what it had. A normalisation step must never be the reason an import
fails.

AND THE ONE TRADE THAT COSTS SOMETHING, stated here because it is the least
obvious thing in the file: an output is only accepted if it reproduces the
source's running time, so a file whose running time CANNOT be established is
not prepared at all — it goes up as it came in. `ffmpeg -c copy` on a source
with an intact header and missing bytes exits 0 and writes a valid, short file,
and the original is deleted right after upload, so an unverifiable output is a
gamble with the creator's only copy. `source_duration()` therefore tries three
ways before giving up (the streams a pass keeps, the container's own figure, a
measured packet count), and the refusal is decided BEFORE any ffmpeg is
launched — never after paying for an encode. In practice the only host that
loses anything is one with ffmpeg but no ffprobe, which prepares nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger("of-relay.media_prep")

# The value COMPRESS_OVER_MB carries when the escape hatch is OFF. A threshold
# no real file can reach, kept as a named constant because `Limits` has to ask
# "did the operator switch compression on?" to decide whether a download
# ceiling above the upload ceiling can ever pay for itself.
COMPRESS_OFF_MB = 1_000_000
# OPT-IN ESCAPE HATCH, off by default. Above this a video takes the old
# size-targeted re-encode instead of the container-normalising path. It is
# defaulted absurdly high because the size theory it was built on is dead (see
# the module docstring): OF demonstrably accepts multi-gigabyte objects, and
# re-encoding a big file to a byte budget only costs resolution on something OF
# re-encodes anyway.
# An operator who wants the old behaviour sets VAULT_COMPRESS_OVER_MB.
COMPRESS_OVER_MB = int(os.environ.get("VAULT_COMPRESS_OVER_MB") or COMPRESS_OFF_MB)
# What that escape-hatch re-encode aims at, when it is switched on. Unused on
# the normal path. Kept under VAULT_UPLOAD_MAX_MB — bitrate targeting is
# approximate and an overshoot is a rejected file.
TARGET_MB = int(os.environ.get("VAULT_COMPRESS_TARGET_MB") or 190)
# Never upscale quality: cap the LONG edge, whichever way the frame is turned.
# Applies to the two paths that actually re-encode (the escape hatch, and the
# ProRes-class transcode); a remux and a pass-through do not touch resolution
# at all. 9:16 is this platform's dominant format, so a filter that caps HEIGHT
# quietly shaves 44% off the width of every vertical master — see `_scale_vf`.
MAX_HEIGHT = int(os.environ.get("VAULT_COMPRESS_MAX_HEIGHT") or 1080)
AUDIO_KBPS = 128
# Quality of the ProRes-class transcode. CRF, not a byte budget: the point is
# to hand OF something it can carry, not to hit a size.
TRANSCODE_CRF = int(os.environ.get("VAULT_TRANSCODE_CRF") or 20)
# The WHOLE ffmpeg budget for ONE file, across every pass it takes. Generous
# but bounded: a long re-encode is fine, a hung one is not — and a remux that
# hangs to the ceiling and then falls through to a transcode must not be able
# to spend the ceiling twice. `prepare` sets a deadline once and every pass
# runs against what is left of it.
FFMPEG_BUDGET_S = int(os.environ.get("VAULT_FFMPEG_BUDGET_S") or 3600)
# How much of the source duration an ffmpeg output must reproduce to be
# believed. A remux is exact and a transcode is within a frame, so this is a
# truncation detector, not a tolerance: a `-c copy` remux of a source with an
# intact moov but a short mdat EXITS 0 and writes a valid, playable, SHORT mp4.
# The original is deleted right after upload, so accepting one of those is
# permanent content loss.
_MIN_DURATION_RATIO = 0.98
_MIN_DURATION_SLACK_S = 1.0
# Ceiling on the fallback duration MEASUREMENT (`_measure_duration`), which is
# reached only when neither the container nor any stream states a running time.
# It demuxes the whole file, so on a multi-gigabyte source it is minutes of I/O
# — worth it once to save a file from going up un-normalised, not worth an
# unbounded wait. On a timeout the file is simply not prepared.
_MEASURE_TIMEOUT_S = int(os.environ.get("VAULT_MEASURE_TIMEOUT_S") or 300)

_VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg",
                   ".wmv", ".ts", ".m2ts", ".mts", ".flv", ".3gp"}
# Containers OF's transcoder answered on in the HAR (mp4) — a file already in
# one of these with an acceptable codec is left completely alone.
_MP4_SUFFIXES = {".mp4", ".m4v"}
# A CONSERVATIVE allowlist of video codecs we are willing to hand OnlyFans'
# transcoder unchanged — by remux when the container is wrong, or untouched
# when it is already mp4. It is NOT "codecs an MP4 container can legally
# carry", which is a wider and different question; what matters here is whether
# OF's transcoder accepts the stream, and the HAR gives us direct evidence for
# exactly one h264-in-mp4 file. Everything else in this set is an educated
# guess, so the set stays small and the cost of being wrong is one transcode.
#   * `hevc` is spelt the way ffprobe emits it. ffprobe never says "h265".
#   * `vp9` and `av1` are here because mp4 carries both and OF's own ladder is
#     h264/hevc — a remux is still cheaper than a re-encode, and a refusal
#     costs one wasted claim.
#   * `mpeg4` (MPEG-4 Part 2 — Xvid/DivX, what old cameras and .avi rips are
#     encoded in) is here for the SAME reason and on the same terms: mp4 carries
#     it natively, so the cheap remux is worth trying first, and if OF's
#     transcoder turns out to refuse it the cost is the one wasted claim the
#     operator note already explains. It is a guess, like vp9 and av1; it is
#     listed so that it reads as one.
#   * still-image codecs are deliberately ABSENT: see `_STILL_VIDEO_CODECS`.
_OF_ACCEPTS_UNCHANGED = {"h264", "hevc", "av1", "vp9", "mpeg4"}
# Codecs that are a PICTURE, not a movie. A file whose first video stream is
# one of these is a video with cover art glued on, and treating that stream as
# "the video" uploads a 30-second slideshow of the poster frame in place of the
# creator's footage. Plex, MakeMKV and most taggers write exactly this.
_STILL_VIDEO_CODECS = {"mjpeg", "png", "bmp", "gif", "webp", "tiff", "ppm"}
# Audio the same. A stream outside this set is re-encoded to AAC while the
# video is still copied — `-c:v copy -c:a aac` is still not a picture re-encode.
_MP4_LEGAL_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "mp2", "opus"}


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def can_shrink(name: str | Path, mime: str | None = None) -> bool:
    """Will PREPARATION be attempted on this file? The single predicate.

    (Named for the old size-shrinking job. The job is now container
    normalisation — remux, or transcode when mp4 cannot carry the codec — but
    the question every caller asks is unchanged: "is this a video ffmpeg can
    take a pass at?", so the predicate stays one function with one name.)

    There used to be two — a mime-type test on the Drive side and a suffix test
    on the disk side — and they disagreed: a `.flv` with a `video/` mime was
    fetched under the big download ceiling and then rejected as "cannot be
    compressed", after the download had already been spent. A caller that
    decides what to FETCH and a caller that decides what to PREPARE have to be
    answering the same question, so they ask it here.

    MIME wins when Drive gave us one; the suffix is the fallback for a local
    file, which has no declared type.
    """
    if not have_ffmpeg():
        return False
    if mime:
        if mime.startswith("video/"):
            return True
        if mime.startswith(("image/", "audio/")):
            return False
    return is_video(Path(name))


def is_video(path: Path) -> bool:
    return path.suffix.lower() in _VIDEO_SUFFIXES


@dataclass(frozen=True)
class Probe:
    """What one ffprobe pass says about a file. All fields advisory.

    `answered` is False when there is no ffprobe, when it failed, or when it
    named no streams at all. Every decision downstream treats that as "we do
    not know", never as "there is nothing there".

    `has_video` is a SEPARATE question from `video is None`. An audio-only
    .mkv answers (answered=True, has_video=False): ffprobe was perfectly clear,
    there is simply no picture. Collapsing the two used to plan such a file for
    a remux and then a transcode, and both died on `Stream map '' matches no
    streams` — two wasted launches and a log line blaming an unknown codec.
    """
    answered: bool = False
    video: str | None = None
    video_index: int | None = None
    has_video: bool = False
    audio: tuple[str, ...] = ()
    duration: float | None = None
    # Per-stream running times, for the streams a remux actually KEEPS. See
    # `kept_duration` — `duration` (the container's) is not the same number.
    video_duration: float | None = None
    audio_durations: tuple[float, ...] = ()
    # Does the file carry anything that is neither video nor audio — subtitles,
    # timecode, chapters, data? Those are exactly what `-map 0:<v> -map 0:a?`
    # drops, and exactly what can make the container's duration longer than
    # anything the output is supposed to contain.
    other_streams: bool = False

    @property
    def kept_duration(self) -> float | None:
        """How long the streams a remux KEEPS run for — the number an output is
        judged against. None when nothing here can say.

        `format.duration` is the maximum over EVERY stream, including the ones
        the remux deliberately drops. A .mkv with a 5 s picture and a subtitle
        track running to 20 s has `format.duration` 20.023 — so comparing a
        perfectly correct 5.0 s remux against it declared the file TRUNCATED,
        threw the remux away, burned a full transcode to be told the same thing,
        and uploaded the raw .mkv. Subtitled .mkv is precisely the Plex/MakeMKV
        shape this module exists to normalise.

        So: the max over the chosen video stream and the audio streams, and the
        container's figure only when there is nothing else in the file for it to
        be measuring. When neither can answer, `source_duration()` measures.
        """
        kept = [d for d in (self.video_duration, *self.audio_durations)
                if d and d > 0]
        if kept:
            return max(kept)
        if self.other_streams:
            return None
        return self.duration if self.duration and self.duration > 0 else None

    @property
    def audio_needs_aac(self) -> bool:
        """Is ANY audio stream outside what mp4 carries?

        Per FILE, not per stream, because `-c:a aac` applies to everything
        `-map 0:a?` selected. Judging from the first stream alone is how an
        .mkv with `aac + pcm_s16le` shipped raw PCM inside the "normalised"
        mp4 — the exact thing this pass exists to prevent.
        """
        return any(a not in _MP4_LEGAL_AUDIO for a in self.audio)


def probe(path: Path) -> Probe:
    """One ffprobe call: real video stream, every audio codec, duration.

    Advisory, like every probe here: no ffprobe, a broken file or a format
    ffprobe does not recognise all answer `Probe()`, and `plan_for` then picks
    the conservative branch rather than failing.

    "Real video stream" is doing work. `-map 0:v:0` takes the FIRST video
    stream, and on a file with embedded poster art that is the poster: an .mkv
    written by a tagger probes as (mjpeg, aac) and remuxes into a still image
    with a soundtrack, reported as a success. So a stream flagged
    `attached_pic` is skipped outright, and a still-image codec is only
    accepted as the video when the file has nothing else.
    """
    if not shutil.which("ffprobe"):
        return Probe()
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries",
             "format=duration:stream=index,codec_type,codec_name,duration"
             ":stream_tags=DURATION:stream_disposition=attached_pic",
             str(path)],
            capture_output=True, text=True, timeout=60, check=True).stdout
        doc = json.loads(out)
    except Exception:  # noqa: BLE001 — probing is advisory
        log.debug("ffprobe failed on %s", path, exc_info=True)
        return Probe()

    streams = doc.get("streams") or []
    if not streams:
        return Probe()
    videos: list[tuple[int, str, float | None]] = []
    audio: list[tuple[str, float | None]] = []
    other = False
    for s in streams:
        kind = s.get("codec_type")
        name = (s.get("codec_name") or "").lower()
        secs = _stream_seconds(s)
        if kind == "video":
            if (s.get("disposition") or {}).get("attached_pic"):
                continue            # cover art, not the movie
            videos.append((int(s.get("index") or 0), name, secs))
        elif kind == "audio":
            audio.append((name, secs))
        else:
            other = True            # subtitles, timecode, chapters, data
    # Prefer a moving picture over a still one. Only if EVERY video stream is a
    # still image do we accept the first — such a file has no real video track
    # and there is nothing better to point at.
    moving = [v for v in videos if v[1] not in _STILL_VIDEO_CODECS]
    picked = (moving or videos or [(None, None, None)])[0]

    dur = (doc.get("format") or {}).get("duration")
    try:
        duration = float(dur) if dur else None
    except (TypeError, ValueError):
        duration = None
    return Probe(answered=True, video=picked[1] or None, video_index=picked[0],
                 has_video=bool(videos),
                 audio=tuple(a for a, _ in audio if a), duration=duration,
                 video_duration=picked[2],
                 audio_durations=tuple(d for _, d in audio if d),
                 other_streams=other)


def _stream_seconds(stream: dict) -> float | None:
    """One stream's running time, from wherever this container keeps it.

    mp4/mov fill `stream=duration`. Matroska does not — it writes a `DURATION`
    TAG per stream, formatted `HH:MM:SS.nnnnnnnnn` — so a reader that knows only
    the first of those is blind on exactly the container (.mkv) whose extra
    streams make the per-stream question worth asking. Anything unparseable is
    None, and None means "this stream cannot say", never "zero".
    """
    raw = stream.get("duration")
    try:
        if raw is not None:
            secs = float(raw)
            return secs if secs > 0 else None
    except (TypeError, ValueError):
        pass
    tag = (stream.get("tags") or {}).get("DURATION")
    if not tag:
        return None
    try:
        h, m, s = str(tag).split(":")
        secs = int(h) * 3600 + int(m) * 60 + float(s)
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def source_duration(path: Path, pr: Probe) -> float | None:
    """The running time an ffmpeg output of `path` will be checked against.

    Asked ONCE per file, by `plan_for`, and carried on the `Plan` from there —
    every pass is verified against the same number rather than re-deriving it.
    None means genuinely unknowable, and `prepare` then does not prepare the
    file at all (see `_run_ffmpeg`), rather than paying for an encode it has
    already decided it must refuse.
    """
    kept = pr.kept_duration
    if kept and kept > 0:
        return kept
    return _measure_duration(path, pr)


def _measure_duration(path: Path, pr: Probe) -> float | None:
    """Count the picture's packets when the header will not say how long it is.

    A container written to a pipe — OBS, a stream capture, a crash-recovered
    recording — carries no `format.duration` and no per-stream DURATION tags,
    because the muxer never got to seek back and fill the header in. The file
    itself is intact; only its header is silent. Refusing to prepare those was
    refusing an ordinary, common file, and the docstring justifying the refusal
    was describing a host with no ffprobe at all.

    `-count_packets` DEMUXES the file (no decode, nothing held in memory) and
    packets / average frame rate is the running time to well inside the
    `_MIN_DURATION_RATIO` tolerance this feeds. It is a whole pass over the
    bytes, so it is bounded by its own timeout, taken at most once per file, and
    only ever reached when both cheaper answers came back empty.
    """
    if not shutil.which("ffprobe") or pr.video_index is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-select_streams", str(pr.video_index), "-count_packets",
             "-show_entries", "stream=nb_read_packets,avg_frame_rate",
             str(path)],
            capture_output=True, text=True, timeout=_MEASURE_TIMEOUT_S,
            check=True).stdout
        s = (json.loads(out).get("streams") or [{}])[0]
        packets = int(s.get("nb_read_packets") or 0)
        num, _, den = str(s.get("avg_frame_rate") or "").partition("/")
        fps = float(num) / float(den or 1)
    except Exception:  # noqa: BLE001 — measuring is advisory, like probing
        log.debug("media_prep: could not measure %s", path, exc_info=True)
        return None
    if packets <= 0 or fps <= 0:
        return None
    secs = packets / fps
    log.info("media_prep: %s states no duration — measured %.1fs from %d "
             "packets", path.name, secs, packets)
    return secs


# What `plan_for` can decide. Strings rather than an enum because they are also
# what gets logged, and a log line is the only trace of this decision.
PASSTHROUGH = "passthrough"
REMUX = "remux"
TRANSCODE = "transcode"
COMPRESS = "compress"

# How one ffmpeg pass ended. FAILED and UNVERIFIED both mean "no output you may
# use", but only one of them is worth spending another pass on — see `Pass`.
DONE = "done"
FAILED = "failed"
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Pass:
    """The result of one ffmpeg pass: the output, and why there isn't one.

    `out is None` is still the whole of "it did not work", so the public
    `remux`/`transcode`/`compress` keep returning `Path | None` and nothing
    outside this module has to know about verdicts. What the verdict buys is
    the distinction `prepare` needs and could not previously make:

      * FAILED — ffmpeg would not do it. A remux that will not copy is evidence
        that a transcode is worth trying.
      * UNVERIFIED — it may well have worked, but we cannot prove the output is
        not truncated (or we can prove it IS). Trying a more expensive pass buys
        a second, identical refusal: the source is the problem, not the pass.

    `__bool__` is defined on purpose. A frozen dataclass without it is always
    truthy, and `plan.probe or probe(path)` — an `or` that could never fire —
    is the exact bug that made the compress hatch structurally dead.
    """
    out: Path | None
    verdict: str = FAILED

    def __bool__(self) -> bool:
        return self.out is not None


@dataclass(frozen=True)
class Plan:
    """What to do to one file, and everything the doing needs.

    The probe travels WITH the decision so that the pass which carries it out
    does not re-derive it. `remux` used to re-run ffprobe and re-evaluate the
    audio-legality expression `plan_for` had just evaluated and thrown away —
    two subprocesses and two copies of one rule, free to disagree.

    `source_duration` is the running time every pass is verified against,
    resolved once (see `source_duration()`) and carried here for the same
    reason. None means it could not be established at all, and `prepare` then
    leaves the file alone instead of encoding it only to refuse the result.

    A `Plan` is NOT a truth value, and neither is a `Probe`: a frozen dataclass
    with no `__bool__` is always truthy, so `plan.probe or probe(path)` never
    took its fallback. That is how a COMPRESS plan carrying a default `Probe()`
    reached every pass as "no duration, no video index" and silently disabled
    the bitrate ladder, the duration check and the cover-art fix at once. Ask
    `plan.probe.answered`.
    """
    action: str
    why: str
    probe: Probe = Probe()
    source_duration: float | None = None


def plan_for(path: Path, *, over_mb: int = COMPRESS_OVER_MB,
             mime: str | None = None) -> Plan:
    """What, if anything, to do to this file before upload.

    `mime` matters: the callers that decide what to FETCH ask `can_shrink`,
    which trusts a declared `video/*` over the filename. If this function asked
    the suffix alone, a Drive file called `clip.dat` with `mimeType: video/mp4`
    was admitted under the big download ceiling, fetched in full, never
    prepared, and then skipped as over the upload limit. One predicate, all
    three passes.

    The ordering matters. The size escape hatch is checked FIRST, because an
    operator who deliberately set `VAULT_COMPRESS_OVER_MB` wants that pass even
    on an already-conforming mp4. Everything after it is codec-driven. (When
    that pass then produces nothing — a failed encode, or an output no smaller
    than the source — `prepare` does NOT stop there: it falls through to the
    codec-driven plan, because "compression did not help" is no reason to hand
    OnlyFans the raw .mov this whole module exists to normalise.)

    The file is PROBED before the size check, not after it, so that the escape
    hatch gets the same probe every other branch gets. It used to be planned
    from the size alone and handed a default `Probe()`, which cost it the
    bitrate ladder (no duration), the cover-art fix (no video index) and its own
    output check (nothing to verify against) — the hatch could not shrink
    anything at all, and said "compression did not help" about a pass it had
    never really run.
    """
    try:
        if not can_shrink(path.name, mime):
            return Plan(PASSTHROUGH, "not a video ffmpeg can take a pass at")
        oversize = path.stat().st_size > over_mb * 1024 * 1024
    except OSError:
        return Plan(PASSTHROUGH, "unreadable")

    pr = probe(path)
    # The hatch outranks the codec branches — except over a file with no picture
    # in it, which the size-targeted video re-encode cannot do anything with
    # either. `plan_by_codec` says so once, for both.
    audio_only = pr.answered and not pr.has_video
    plan = Plan(COMPRESS, f"over the opt-in {over_mb} MB compress threshold", pr) \
        if (oversize and not audio_only) else plan_by_codec(path, pr)
    return with_source_duration(path, plan)


def with_source_duration(path: Path, plan: Plan) -> Plan:
    """Resolve the duration every pass of `plan` will be verified against.

    Once per file: a plan that already carries a number keeps it, and a
    PASSTHROUGH never asks, because nothing is going to be verified.
    """
    if plan.action == PASSTHROUGH or plan.source_duration is not None:
        return plan
    return replace(plan, source_duration=source_duration(path, plan.probe))


def plan_by_codec(path: Path, pr: Probe) -> Plan:
    """The codec-driven half of `plan_for`, once the file has been probed.

    Split out because `prepare` needs it a second time: when the escape hatch
    runs and produces nothing, this is the plan it falls through to. Pure: it
    decides from the probe it is handed and launches nothing.
    """
    is_mp4 = path.suffix.lower() in _MP4_SUFFIXES
    if pr.answered and not pr.has_video:
        # ffprobe was clear: there is no picture in this file. An audio-only
        # .mkv (a ripped album track, a voice memo with a video extension) is
        # not "a codec we could not name" — planning it for a remux and then a
        # transcode spent two ffmpeg launches that both died on
        # `Stream map '' matches no streams`, and logged an unknown codec as the
        # reason. Nothing here normalises audio, so there is nothing to do.
        return Plan(PASSTHROUGH, "no video stream — nothing to normalise", pr)
    if pr.video is None:
        # ffprobe could not say (or is not installed). A .mp4 is already the
        # shape OF answered on, so leave it; anything else gets a remux
        # ATTEMPT, which is cheap and fails in seconds if the codec turns out
        # to be un-carryable. We do not pick `transcode` here, because at this
        # point the container is the only thing we know is wrong and an hour of
        # CPU on a guess is the worse mistake. (Once a remux has actually been
        # TRIED and failed, that is evidence rather than a guess, and `prepare`
        # does then transcode — inside the same one-file ffmpeg budget, so the
        # worst case is still one budget, not two.)
        return Plan(PASSTHROUGH, "already .mp4; codec unknown", pr) if is_mp4 else \
            Plan(REMUX, "codec unknown — a copy-remux is cheap to try", pr)
    if pr.video not in _OF_ACCEPTS_UNCHANGED:
        return Plan(TRANSCODE, f"{pr.video} is not a codec we hand OF unchanged", pr)
    if is_mp4 and not pr.audio_needs_aac:
        # The big file in the HAR was exactly this shape, and it landed. Size
        # is not the gate, so there is nothing left to do to it.
        return Plan(PASSTHROUGH, f"already mp4/{pr.video} — OF takes this at any size", pr)
    if is_mp4:
        return Plan(REMUX, f"mp4/{pr.video} but audio mp4 cannot carry "
                           f"({', '.join(pr.audio)})", pr)
    return Plan(REMUX, f"{pr.video} in {path.suffix.lower()} — remux to mp4, "
                       f"no re-encode", pr)


def _scale_vf(short_edge: int) -> str:
    """A scale filter that caps the frame at `short_edge`p in EITHER orientation.

    `scale=-2:'min(H,ih)'` — what this used to pass — caps HEIGHT, which is a
    different thing entirely on a vertical master: a 1080x1920 ProRes source
    came out 608x1080, a 44% cut in width, from a constant whose comment
    promised "cap the long edge". 9:16 is the dominant format on this platform,
    so that was the common case, not the corner one.

    "1080p" here means the frame fits inside 1920x1080 turned the way it is
    shot: a 1080x1920 portrait master is ALREADY 1080p and comes through
    untouched, while a 3840x2160 landscape one comes down to 1920x1080. The box
    is picked by orientation, and each side is additionally clamped by `min()`
    against the source so `force_original_aspect_ratio=decrease` — which fits a
    frame to the box in both directions — can never UPSCALE something small.
    The trailing `trunc(../2)*2` restores even dimensions, which libx264's
    yuv420p requires.
    """
    long_edge = (short_edge * 16 // 9) // 2 * 2
    box_w = f"min(iw,if(gte(iw,ih),{long_edge},{short_edge}))"
    box_h = f"min(ih,if(gte(iw,ih),{short_edge},{long_edge}))"
    return (f"scale='{box_w}':'{box_h}':force_original_aspect_ratio=decrease"
            f",scale=trunc(iw/2)*2:trunc(ih/2)*2")


def _run_ffmpeg(cmd: list[str], out: Path, src: Path, *, what: str,
                timeout_s: float, source_duration: float | None) -> Pass:
    """Run an ffmpeg pass. `Pass.out` is None on ANY failure — and says WHICH.

    Every failure mode lands here on purpose: a non-zero exit, a hang past the
    timeout, an empty output file, a TRUNCATED output file. The caller's
    contract is that a failed preparation costs seconds and then uploads the
    original, never that it raises.

    The truncation check is the one that is not obvious. `ffmpeg -c copy` on a
    source whose moov is intact but whose mdat is short exits 0 and writes a
    perfectly valid mp4 that is simply missing most of the video — a 30.0 s
    source truncated to 40% of its bytes produces a 10.6 s output. `prepare`
    would return it, `_upload_one` would upload it, and `vault_upload` deletes
    the spooled original immediately afterwards. That is silent, permanent
    content loss on any partially-completed Drive or HTTP stage, so an output
    that does not reproduce the source's duration is a FAILURE.

    THE TRADE, STATED PLAINLY. Cannot-verify is also a failure: without a source
    duration to compare against, the output is refused and the original goes up.
    The original is what the creator has; an unverified output might be a
    fraction of it. But refusing AFTER the encode means paying an hour of CPU to
    learn something knowable in advance, so the check is HOISTED — a pass with
    no `source_duration` returns UNVERIFIED before ffmpeg is launched, and
    `prepare` never even gets here, because it refuses to prepare a file whose
    running time it could not establish (`plan.source_duration is None`). In
    practice that means: **a host with ffmpeg but no ffprobe prepares nothing at
    all**, which is exactly what "best effort, never a gate" is supposed to
    mean. `source_duration()` tries hard first — the streams that are kept, then
    a measured count — so this is a genuinely unknowable file, not merely a
    container that keeps its duration somewhere unusual.

    FAILED and UNVERIFIED are different answers and `prepare` reads them
    differently: a remux that FAILED is worth a transcode, a remux refused for
    verification is not — the transcode would be refused for the same reason,
    after a full encode.
    """
    if not source_duration or source_duration <= 0:
        log.warning("media_prep: cannot establish %s's running time — not "
                    "attempting the %s, because an output we cannot verify may "
                    "be a fraction of the source", src.name, what)
        return Pass(None, UNVERIFIED)
    if timeout_s <= 0:
        log.warning("media_prep: no ffmpeg budget left for %s on %s", what, src.name)
        return Pass(None, FAILED)
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=True)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"")[-400:].decode("utf-8", "replace")
        log.warning("media_prep: %s failed on %s — %s", what, src.name, tail)
        out.unlink(missing_ok=True)
        return Pass(None, FAILED)
    except Exception as e:  # noqa: BLE001
        log.warning("media_prep: %s error on %s — %s", what, src.name, e)
        out.unlink(missing_ok=True)
        return Pass(None, FAILED)
    if not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        return Pass(None, FAILED)
    if not _duration_survived(src, out, source_duration, what=what):
        out.unlink(missing_ok=True)
        return Pass(None, UNVERIFIED)
    return Pass(out, DONE)


def _duration_survived(src: Path, out: Path, source_duration: float | None,
                       *, what: str) -> bool:
    """Did the output keep the source's running time? See `_run_ffmpeg`.

    `source_duration` is guaranteed non-empty by the caller — a pass with no
    number to check against is refused before ffmpeg is launched — so this only
    ever asks the interesting question.
    """
    if not source_duration or source_duration <= 0:
        return False
    made = probe(out).duration
    if not made:
        log.warning("media_prep: cannot read the %s output's duration for %s "
                    "— uploading the original", what, src.name)
        return False
    floor = min(source_duration * _MIN_DURATION_RATIO,
                source_duration - _MIN_DURATION_SLACK_S)
    if made < floor:
        log.warning("media_prep: %s of %s came out %.1fs from a %.1fs source — "
                    "TRUNCATED, discarding it and uploading the original",
                    what, src.name, made, source_duration)
        return False
    return True


def _out_path(src: Path, dest_dir: Path, tag: str) -> Path:
    """Where a prepared copy is written. Unique per SOURCE, not per stem.

    ffmpeg is run with `-y` into the shared run directory, so `{stem}_mp4.mp4`
    means a batch containing both `holiday.mov` and `holiday_mp4.mp4` has the
    first item's remux overwrite the second item's staged bytes — which are
    then deleted after upload. Uploads are serial so nothing races; the file is
    simply gone. A short digest of the absolute source path fixes it without
    making the name unreadable in a log line.
    """
    key = hashlib.sha1(str(src.resolve()).encode("utf-8", "replace")).hexdigest()[:8]
    return dest_dir / f"{src.stem}_{key}_{tag}.mp4"


def _plan_probe(path: Path, plan: Plan | None) -> tuple[Probe, float | None]:
    """The one probe and the one source duration this file's passes share.

    `plan.probe or probe(path)` is what this replaces, and that `or` could never
    fire: a frozen dataclass with no `__bool__` is truthy even when every field
    is empty, so a plan carrying a default `Probe()` silently meant "no video
    index, no duration" — cover art re-selected, bitrate ladder off, output
    unverifiable. `answered` is the field that actually knows.

    A caller that passes no plan (a direct call, a test), or a hand-built plan
    that never resolved a duration, gets the missing half taken here; `prepare`
    passes a complete plan, so the normal path probes exactly once and measures
    at most once.
    """
    pr = plan.probe if (plan is not None and plan.probe.answered) else probe(path)
    dur = plan.source_duration if (plan is not None and plan.source_duration) \
        else source_duration(path, pr)
    return pr, dur


def remux(path: Path, dest_dir: Path, *, plan: Plan | None = None,
          timeout_s: float = FFMPEG_BUDGET_S) -> Path | None:
    """Rewrap the SAME encoded video into a faststart .mp4. None on any failure."""
    return _remux(path, dest_dir, plan=plan, timeout_s=timeout_s).out


def _remux(path: Path, dest_dir: Path, *, plan: Plan | None = None,
           timeout_s: float = FFMPEG_BUDGET_S) -> Pass:
    """Rewrap the SAME encoded video into a faststart .mp4. None on any failure.

    No re-encode: `-c:v copy` means the picture bits are byte-identical, so this
    costs seconds and zero quality — which is the whole reason it replaced a
    120 MB-triggered transcode. Audio is copied too when mp4 can carry every
    stream and re-encoded to AAC when it cannot; that is still not a picture
    re-encode.

    `-map 0:<video index> -map 0:a?` deliberately drops everything else. A
    .mov's timecode and data streams are exactly what makes a naive
    `-map 0 -c copy` fail, and OnlyFans re-encodes to its own ladder anyway, so
    nothing there survives to the fan regardless. The video stream is named by
    INDEX rather than `0:v:0` because `0:v:0` is the cover art on any file that
    has some — see `probe`.
    """
    if not have_ffmpeg():
        return Pass(None, FAILED)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = _out_path(path, dest_dir, "mp4")
    pr, dur = _plan_probe(path, plan)
    vmap = f"0:{pr.video_index}" if pr.video_index is not None else "0:v:0"
    acodec = ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k"] if pr.audio_needs_aac \
        else ["-c:a", "copy"]
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(path),
           "-map", vmap, "-map", "0:a?", "-c:v", "copy", *acodec,
           "-movflags", "+faststart", str(out)]
    log.info("media_prep: remuxing %s (%.0f MB) into mp4 — no re-encode",
             path.name, path.stat().st_size / 1e6)
    return _run_ffmpeg(cmd, out, path, what="remux", timeout_s=timeout_s,
                       source_duration=dur)


def transcode(path: Path, dest_dir: Path, *, crf: int = TRANSCODE_CRF,
              plan: Plan | None = None,
              timeout_s: float = FFMPEG_BUDGET_S) -> Path | None:
    """Re-encode to H.264/AAC mp4 at a QUALITY target. None on any failure."""
    return _transcode(path, dest_dir, crf=crf, plan=plan,
                      timeout_s=timeout_s).out


def _transcode(path: Path, dest_dir: Path, *, crf: int = TRANSCODE_CRF,
               plan: Plan | None = None,
               timeout_s: float = FFMPEG_BUDGET_S) -> Pass:
    """Re-encode to H.264/AAC mp4 at a QUALITY target. None on any failure.

    Only for streams we will not hand OF unchanged — ProRes, DNxHD and friends.
    CRF, not a bitrate budget: OnlyFans re-encodes whatever it receives to its
    own ladder and demonstrably accepts multi-gigabyte objects, so aiming at a
    byte count buys nothing and costs resolution. The LONG edge is still capped
    at MAX_HEIGHT because OF's ladder tops out below 4K anyway.
    """
    if not have_ffmpeg():
        return Pass(None, FAILED)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = _out_path(path, dest_dir, "h264")
    pr, dur = _plan_probe(path, plan)
    vmap = f"0:{pr.video_index}" if pr.video_index is not None else "0:v:0"
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(path),
           "-map", vmap, "-map", "0:a?",
           "-vf", _scale_vf(MAX_HEIGHT),
           "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
           "-pix_fmt", "yuv420p", "-crf", str(crf),
           "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
           "-movflags", "+faststart", str(out)]
    log.info("media_prep: transcoding %s (%.0f MB) to h264 mp4 @ crf %d",
             path.name, path.stat().st_size / 1e6, crf)
    return _run_ffmpeg(cmd, out, path, what="transcode", timeout_s=timeout_s,
                       source_duration=dur)


def compress(path: Path, dest_dir: Path, *, target_mb: int = TARGET_MB,
             plan: Plan | None = None,
             timeout_s: float = FFMPEG_BUDGET_S) -> Path | None:
    """Re-encode `path` to H.264/AAC MP4 under ~`target_mb`. None on any failure."""
    return _compress(path, dest_dir, target_mb=target_mb, plan=plan,
                     timeout_s=timeout_s).out


def _compress(path: Path, dest_dir: Path, *, target_mb: int = TARGET_MB,
              plan: Plan | None = None,
              timeout_s: float = FFMPEG_BUDGET_S) -> Pass:
    """Re-encode `path` to H.264/AAC MP4 under ~`target_mb`. None on any failure.

    THE ESCAPE HATCH, not the normal path. It is reached only when an operator
    sets `VAULT_COMPRESS_OVER_MB` down from its off-by-default value; the size
    theory that used to trigger it at 120 MB is dead (see the module docstring),
    and on a long video the byte budget is exactly what drops an hour of footage
    to 480p. Kept because an operator on a thin uplink may still want it.

    Bitrate is derived from the duration, so a long video gets a lower bitrate
    rather than blowing the budget. That duration is the one the whole file is
    judged by (`Plan.source_duration` — the streams a pass KEEPS, measured if
    the container will not say), so the ladder and the truncation check can
    never be working from two different numbers. Without one at all we fall back
    to CRF, which cannot be size-targeted — hence the size check afterwards,
    which rejects a result that came out no smaller than the source.
    """
    if not have_ffmpeg():
        return Pass(None, FAILED)

    dest_dir.mkdir(parents=True, exist_ok=True)
    out = _out_path(path, dest_dir, "compressed")
    pr, duration = _plan_probe(path, plan)

    # Audio is a fixed cost per second, so on a long video it quietly eats the
    # budget the picture needs: 128k over 54 minutes is ~58 MB of a 90 MB
    # target. Halve it past ten minutes — speech and music both survive 64k far
    # better than the video survives being starved of bits.
    audio_kbps = AUDIO_KBPS if not duration or duration <= 600 else 64
    vmap = f"0:{pr.video_index}" if pr.video_index is not None else "0:v:0"

    def _common(max_edge: int) -> list[str]:
        # Built once the size is known, rather than built at MAX_HEIGHT and
        # then patched by matching the formatted scale filter against itself.
        return [
            "ffmpeg", "-nostdin", "-y", "-i", str(path),
            "-map", vmap, "-map", "0:a?",
            "-vf", _scale_vf(max_edge),
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
        ]

    if duration and duration > 1:
        # Bitrate the size target actually allows, once audio is paid for.
        video_kbps = int((target_mb * 8 * 1024) / duration - audio_kbps)
        # Pick the largest frame size that bitrate can carry. This is the whole
        # trick for long video: an hour-long clip gets ~75 kbps at 90 MB, and
        # holding 1080p there would either look like mud or (with a bitrate
        # floor) blow straight past the target — a real 990 MB / ~1 h file came
        # out at 210 MB that way and got rejected. Dropping resolution spends
        # the few bits available on something watchable AND hits the size.
        max_edge = MAX_HEIGHT
        for h, needed in ((MAX_HEIGHT, 1500), (720, 800), (480, 400), (360, 200)):
            # Not dead: MAX_HEIGHT is settable, and at 480 the fixed 720 rung
            # would otherwise UPSCALE past the cap the operator asked for.
            if h > MAX_HEIGHT:
                continue
            if video_kbps >= needed:
                max_edge = h
                break
            max_edge = h      # keep stepping down; last one wins
        video_kbps = max(video_kbps, 150)    # a real floor, but a low one
        cmd = _common(max_edge) + [
            "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k",
            "-bufsize", f"{video_kbps * 2}k", str(out)]
        log.info("media_prep: %s (%.0f MB, %.0fs) -> long edge %d @ %dk video "
                 "+ %dk audio", path.name, path.stat().st_size / 1e6, duration,
                 max_edge, video_kbps, audio_kbps)
    else:
        cmd = _common(MAX_HEIGHT) + ["-crf", "26", str(out)]
        log.info("media_prep: %s — no duration, falling back to CRF 26", path.name)

    res = _run_ffmpeg(cmd, out, path, what="compress", timeout_s=timeout_s,
                      source_duration=duration)
    if res.out is None:
        return res
    if out.stat().st_size >= path.stat().st_size:
        # Already efficiently encoded — re-encoding bought nothing and would
        # only cost quality. Keep the original.
        log.info("media_prep: %s did not shrink (%.0f -> %.0f MB), keeping original",
                 path.name, path.stat().st_size / 1e6, out.stat().st_size / 1e6)
        out.unlink(missing_ok=True)
        # FAILED, not UNVERIFIED: the output was fine, it was simply pointless.
        # The container may still be wrong, so a fallthrough is worth it.
        return Pass(None, FAILED)
    log.info("media_prep: %s %.0f MB -> %.0f MB", path.name,
             path.stat().st_size / 1e6, out.stat().st_size / 1e6)
    return res


def prepare(path: str | Path, dest_dir: str | Path, *,
            over_mb: int = COMPRESS_OVER_MB,
            target_mb: int = TARGET_MB,
            mime: str | None = None) -> tuple[Path, bool]:
    """`(path_to_upload, was_replaced)`. Never raises; falls back to the original.

    The second element is True exactly when the file being uploaded is a NEW
    file this module produced — a remux, a transcode or the escape-hatch
    compress — and False when the original is going up untouched. The one
    caller (`vault_upload._upload_one`) uses it to record `compressed_from` and
    re-stat the size, and the dashboard renders that as `<before> → <after>`.
    That reading stays true for a remux: the size shown really is the size of
    the object that was uploaded. The tuple SHAPE is unchanged deliberately —
    the flag never meant "quality was lost", only "a different file went up".

    `mime` is the declared type when the caller has one (Drive gives us one; a
    local file does not). It reaches `can_shrink` so that "will ffmpeg take a
    pass at this?" has exactly one answer across the cap pass, the fetch pass
    and here.

    Every ffmpeg pass this function orders shares ONE budget
    (`FFMPEG_BUDGET_S`), so a file that falls from compress to remux to
    transcode still costs at most one budget in total, not one per pass. And no
    file pays for two passes to be told the same thing twice: a pass refused
    because its output could not be VERIFIED ends the attempt, because a longer
    pass would be refused for the identical reason.
    """
    p = Path(path)
    plan = plan_for(p, over_mb=over_mb, mime=mime)
    if plan.action == PASSTHROUGH:
        log.debug("media_prep: %s left alone — %s", p.name, plan.why)
        return p, False
    if not have_ffmpeg():
        # Logged ONCE, here, rather than once per pass — a file that falls
        # through two passes used to say "ffmpeg not installed" twice.
        log.info("media_prep: ffmpeg not installed — uploading %s as-is", p.name)
        return p, False
    if not plan.source_duration:
        # THE TRADE, PAID BEFORE THE CPU IS SPENT (see `_run_ffmpeg`). An output
        # whose running time cannot be checked against the source's might be a
        # fraction of it, and the original is deleted right after upload — so
        # such a file is uploaded untouched. It used to be encoded first and
        # refused afterwards, twice, which cost up to the whole hour-long budget
        # to reach a conclusion available for free.
        log.info("media_prep: cannot establish %s's running time (no ffprobe, "
                 "or a container that states none and could not be measured) — "
                 "uploading it as-is rather than encoding something we would "
                 "then have to refuse", p.name)
        return p, False
    log.info("media_prep: %s -> %s (%s)", p.name, plan.action, plan.why)
    deadline = time.monotonic() + FFMPEG_BUDGET_S

    def _left() -> float:
        return deadline - time.monotonic()

    def _stop(res: Pass, what: str) -> bool:
        """Is this refusal about the SOURCE rather than about the pass?"""
        if res.verdict != UNVERIFIED:
            return False
        log.info("media_prep: the %s of %s could not be verified — uploading "
                 "the original rather than spending a second pass to be "
                 "refused again", what, p.name)
        return True

    try:
        dest = Path(dest_dir)
        res = Pass(None, FAILED)
        if plan.action == COMPRESS:
            res = _compress(p, dest, target_mb=target_mb, plan=plan,
                            timeout_s=_left())
            if _stop(res, "compress"):
                return p, False
            if res.out is None:
                # The hatch produced nothing — a failed encode, or an output no
                # smaller than the source. That says nothing about the
                # CONTAINER, and returning the original here means an operator
                # who switched compression on uploads a raw .mov for every
                # large file compression cannot help: the one container this
                # whole module treats as the suspect. So fall through to the
                # codec-driven plan — carrying the same probe and the same
                # source duration, both already paid for.
                plan = replace(plan_by_codec(p, plan.probe),
                               source_duration=plan.source_duration)
                log.info("media_prep: compress of %s produced nothing — "
                         "falling through to %s (%s)", p.name, plan.action, plan.why)
        if plan.action == REMUX:
            res = _remux(p, dest, plan=plan, timeout_s=_left())
            if _stop(res, "remux"):
                return p, False
            if res.out is None:
                # A copy-remux that will not copy is usually a codec mp4 cannot
                # carry that ffprobe did not name. This is no longer a blind
                # guess — the cheap attempt has been made and failed — so one
                # real transcode is worth more than uploading a container we
                # suspect the transcoder chokes on. It runs on what is LEFT of
                # the one-file budget, so the worst case is that budget, not
                # twice it.
                log.info("media_prep: remux of %s did not take — transcoding", p.name)
                res = _transcode(p, dest, plan=plan, timeout_s=_left())
        elif plan.action == TRANSCODE:
            res = _transcode(p, dest, plan=plan, timeout_s=_left())
    except Exception:  # noqa: BLE001 — preparation is an optimisation, not a gate
        log.exception("media_prep: unexpected failure preparing %s", p)
        return p, False
    return (res.out, True) if res.out else (p, False)
