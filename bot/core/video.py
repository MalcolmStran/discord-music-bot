"""Video download + "fit under N bytes" compression for Discord uploads.

* download():   yt-dlp (Twitter/X, TikTok, and anything else yt-dlp supports), with an
                optional RapidAPI fallback for TikTok.
* fit_under():  ffprobe → pick bitrate for the target size → two-pass encode; ladder of
                attempts (x265 full-res → x265 720p → x264 480p) until it fits.
All ffmpeg work goes through asyncio subprocesses with a global semaphore so a burst of
links can never fork unbounded encoders (the 2026-04-14 PSP lesson).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import yt_dlp
from yt_dlp.utils import DownloadError

log = logging.getLogger(__name__)


class VideoError(Exception):
    pass


@dataclass
class Probe:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass
class EncodeStep:
    vcodec: str
    acodec: str
    max_height: Optional[int]   # None = keep resolution
    size_factor: float          # fraction of the limit to aim for
    preset: str

    @property
    def label(self) -> str:
        res = f"≤{self.max_height}p" if self.max_height else "source res"
        return f"{self.vcodec}/{self.acodec} {res}"


# Measured on rock5 (RK3588, 720p source): x264 veryfast ≈ 4.6× realtime, x265 ultrafast ≈ 1×.
# So x264 goes first (a 3-min clip compresses in ~90 s two-pass); x265 is the last resort for
# very long clips where its ~30 % better efficiency is the only way to hit the limit.
LADDER = [
    EncodeStep("libx264", "aac", None, 0.92, "veryfast"),
    EncodeStep("libx264", "aac", 480, 0.90, "veryfast"),
    EncodeStep("libx265", "aac", 480, 0.88, "ultrafast"),
]

# Below this the picture is mush and the file still will not shrink much, so a target that
# needs less than this is treated as impossible rather than encoded and thrown away.
MIN_VIDEO_BITRATE = 120_000

_encode_sem: Optional[asyncio.Semaphore] = None
_download_sem: Optional[asyncio.Semaphore] = None


def configure(max_concurrent_encodes: int, max_concurrent_downloads: int = 3) -> None:
    """Install the global concurrency caps. Idempotent: replacing a live semaphore would
    let jobs already holding the old one run alongside jobs holding the new one, quietly
    doubling the cap every time the cog is reloaded."""
    global _encode_sem, _download_sem
    if _encode_sem is None:
        _encode_sem = asyncio.Semaphore(max_concurrent_encodes)
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(max_concurrent_downloads)


def _sem(kind: str) -> asyncio.Semaphore:
    global _encode_sem, _download_sem
    if _encode_sem is None:
        configure(2)
    return _encode_sem if kind == "encode" else _download_sem  # type: ignore[return-value]


# ------------------------------------------------------------------ download
async def download(url: str, workdir: Path, max_bytes: int, *, cookies_file: Optional[Path] = None,
                   rapidapi_key: Optional[str] = None) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    stem = workdir / f"dl_{uuid.uuid4().hex[:10]}"
    opts = {
        "format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(stem) + ".%(ext)s",
        "quiet": True, "no_warnings": True, "noprogress": True,
        "noplaylist": True,
        "max_filesize": max_bytes,
        "retries": 3,
        "logger": _Quiet(),
    }
    if cookies_file and cookies_file.exists():
        opts["cookiefile"] = str(cookies_file)
    too_big = False
    async with _sem("download"):
        def _run() -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:      # closing it releases yt-dlp's sockets
                ydl.download([url])
        try:
            await asyncio.to_thread(_run)
        except DownloadError as e:
            msg = str(e)
            if "tiktok" in url.lower() and rapidapi_key:
                log.info("yt-dlp failed for TikTok (%s); trying RapidAPI fallback", msg[:80])
                try:
                    return await _tiktok_rapidapi(url, stem.with_suffix(".mp4"), max_bytes, rapidapi_key)
                except VideoError:
                    raise
                except Exception as fe:              # keep the original reason, not the fallback's
                    log.warning("TikTok RapidAPI fallback failed: %s", fe)
                    _sweep(workdir, stem.name)
                    raise VideoError(_friendly(msg)) from fe
            _sweep(workdir, stem.name)
            if _is_too_big(msg):
                raise VideoError(f"Video is larger than {max_bytes // 1024 // 1024} MB.") from e
            raise VideoError(_friendly(msg)) from e
        except Exception as e:
            _sweep(workdir, stem.name)
            raise VideoError("Couldn't download that video.") from e
    # yt-dlp *skips* (does not raise) when max_filesize is exceeded, so the only trace is
    # that nothing was written. Without this the user got a misleading "No video found".
    for p in sorted(workdir.glob(stem.name + ".*")):
        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"):
            if p.stat().st_size > max_bytes:
                too_big = True
                continue
            _sweep(workdir, stem.name, keep=p)       # drop leftover per-format fragments
            return p
    _sweep(workdir, stem.name)
    if too_big:
        raise VideoError(f"Video is larger than {max_bytes // 1024 // 1024} MB.")
    raise VideoError(f"No video found at that link (or it is over the {max_bytes // 1024 // 1024} MB limit).")


def _is_too_big(msg: str) -> bool:
    low = msg.lower()
    return "larger than max" in low or "max-filesize" in low or "file is larger" in low


def _sweep(workdir: Path, stem_name: str, keep: Optional[Path] = None) -> None:
    """Remove this job's leftovers: .part files, per-format fragments, failed merges."""
    for p in workdir.glob(stem_name + "*"):
        if keep is not None and p == keep:
            continue
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


async def _tiktok_rapidapi(url: str, dest: Path, max_bytes: int, key: str) -> Path:
    api = "https://tiktok-download-without-watermark.p.rapidapi.com/analysis"
    headers = {"x-rapidapi-host": "tiktok-download-without-watermark.p.rapidapi.com", "x-rapidapi-key": key}
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(api, headers=headers, params={"url": url, "hd": "0"}) as r:
            if r.status != 200:
                raise VideoError(f"TikTok API error {r.status}.")
            data = (await r.json()).get("data") or {}
        play = data.get("play")
        if not play:
            raise VideoError("TikTok API returned no video.")
        try:
            async with s.get(play) as r:
                r.raise_for_status()
                n = 0
                # write off the event loop: a 500 MB body written inline stalls playback
                # for every guild while it streams in.
                f = await asyncio.to_thread(open, dest, "wb")
                try:
                    async for chunk in r.content.iter_chunked(1 << 20):   # 1 MiB: one thread hop per MB
                        n += len(chunk)
                        if n > max_bytes:
                            raise VideoError(f"Video is larger than {max_bytes // 1024 // 1024} MB.")
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)
        except BaseException:
            dest.unlink(missing_ok=True)      # never leave a partial file behind
            raise
    return dest


# --------------------------------------------------------------------- probe
async def probe(path: Path, *, timeout: int = 60) -> Probe:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as e:
        raise VideoError("ffprobe is not installed on the host, so videos can't be compressed.") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        await _terminate(proc)
        raise VideoError("ffprobe timed out reading that file.") from e
    if proc.returncode != 0:
        raise VideoError(f"ffprobe failed: {err.decode(errors='ignore')[:200]}")
    try:
        info = json.loads(out or b"{}")
    except ValueError as e:
        raise VideoError("ffprobe returned unreadable output.") from e
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise VideoError("No video stream in file.")
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise VideoError("Could not determine video duration.")
    return Probe(duration=dur, width=int(v.get("width") or 0), height=int(v.get("height") or 0),
                 has_audio=any(s.get("codec_type") == "audio" for s in streams))


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill a child and actually reap it. `proc.kill()` alone only sends the signal: the
    process stays alive (and keeps its CPU/temp files) while we release the semaphore and
    start the next encode."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        log.warning("child %s did not die after SIGKILL", proc.pid)


# -------------------------------------------------------------------- encode
def plan_step(step: EncodeStep, limit_bytes: int, duration: float, has_audio: bool) -> Optional[tuple[int, int]]:
    """Bitrates (video, audio) for `step`, or None if this rung provably cannot fit.

    The old code clamped a negative video bitrate up to a floor and encoded anyway, so a
    long clip ran all three rungs — six ffmpeg passes holding the encode semaphore — to
    produce files many times over the limit before giving up.
    """
    if duration <= 0:
        return None
    abr = 64_000 if has_audio else 0
    target_bytes = int(limit_bytes * step.size_factor)
    vbr = int(target_bytes * 8 / duration) - abr
    if vbr < MIN_VIDEO_BITRATE:
        return None
    return vbr, abr


def max_fittable_duration(limit_bytes: int, has_audio: bool = True) -> float:
    """Longest clip any ladder rung could compress under `limit_bytes`, in seconds."""
    abr = 64_000 if has_audio else 0
    best = max(st.size_factor for st in LADDER)
    return (limit_bytes * best * 8) / (MIN_VIDEO_BITRATE + abr)


async def fit_under(src: Path, limit_bytes: int, workdir: Path, *, timeout: int = 600,
                    progress=None) -> Path:
    """Return a path to a file ≤ limit_bytes (src itself if already small enough).
    `progress(text)` is an optional async callback for status updates."""
    if src.stat().st_size <= limit_bytes:
        return src
    info = await probe(src)
    plans = [(step, plan) for step in LADDER if (plan := plan_step(step, limit_bytes, info.duration, info.has_audio))]
    if not plans:
        longest = max_fittable_duration(limit_bytes, info.has_audio)
        raise VideoError(
            f"That video is {_mmss(info.duration)} long — too long to fit in "
            f"{limit_bytes // 1048576} MB at watchable quality (max about {_mmss(longest)}).")
    for step, (vbr, abr) in plans:
        out = workdir / f"enc_{uuid.uuid4().hex[:8]}.mp4"
        if progress:
            try:
                await progress(f"🗜️ Compressing ({step.label}, ~{vbr // 1000} kbps)…")
            except Exception:
                pass
        try:
            ok = await _two_pass(src, out, step, vbr, abr, info, timeout)
            if ok and out.exists():
                size = out.stat().st_size
                log.info("encode %s → %.2f MB (limit %.2f MB)", step.label, size / 1048576, limit_bytes / 1048576)
                if size <= limit_bytes:
                    return out
        finally:
            if not (out.exists() and out.stat().st_size <= limit_bytes):
                out.unlink(missing_ok=True)
    raise VideoError("Couldn't compress the video enough to upload it (try a shorter clip).")


def _mmss(seconds: float) -> str:
    m, sec = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def build_pass_args(src: Path, out: Path, step: EncodeStep, vbr: int, abr: int,
                    height: int, passlog: str) -> tuple[list[str], list[str]]:
    """argv for ffmpeg pass 1 and pass 2. Split out so the flags are unit-testable."""
    vf: list[str] = []
    if step.max_height and height > step.max_height:
        vf = ["-vf", f"scale=-2:{step.max_height}"]
    common = ["-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src),
              "-c:v", step.vcodec, "-b:v", str(vbr), "-maxrate", str(int(vbr * 1.3)), "-bufsize", str(vbr * 2),
              "-preset", step.preset, "-pix_fmt", "yuv420p", *vf, "-passlogfile", passlog]
    x265 = []
    if step.vcodec == "libx265":
        # libx265 ignores -passlogfile and writes ./x265_2pass.log in the process CWD, so
        # parallel encodes corrupt each other's stats. Name the file explicitly instead.
        # (The stale x265_2pass.log* entry in .gitignore is the scar from this.)
        common += ["-tag:v", "hvc1"]
        x265 = [f"stats={passlog}-x265.log"]
    pass1 = ["ffmpeg", *common,
             *(["-x265-params", ":".join([*x265, "pass=1"])] if x265 else []),
             "-pass", "1", "-an", "-f", "null", os.devnull]
    audio = ["-c:a", step.acodec, "-b:a", str(abr)] if abr else ["-an"]
    pass2 = ["ffmpeg", *common,
             *(["-x265-params", ":".join([*x265, "pass=2"])] if x265 else []),
             "-pass", "2", *audio, "-movflags", "+faststart", str(out)]
    return pass1, pass2


async def _two_pass(src: Path, out: Path, step: EncodeStep, vbr: int, abr: int, info: Probe, timeout: int) -> bool:
    passlog = out.with_suffix("").as_posix() + "_pass"
    pass1, pass2 = build_pass_args(src, out, step, vbr, abr, info.height, passlog)
    async with _sem("encode"):
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            for argv in (pass1, pass2):
                try:
                    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                                stderr=asyncio.subprocess.PIPE)
                except FileNotFoundError as e:
                    raise VideoError("ffmpeg is not installed on the host, so videos can't be compressed.") from e
                try:
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except TimeoutError:
                    # kill() only signals; without the reap the encoder keeps burning CPU
                    # after we release the semaphore and start the next job.
                    await _terminate(proc)
                    log.warning("ffmpeg timed out (%s)", step.label)
                    return False
                if proc.returncode != 0:
                    log.warning("ffmpeg failed (%s): %s", step.label, err.decode(errors="ignore")[-300:])
                    return False
            return True
        except asyncio.CancelledError:
            if proc is not None:
                await _terminate(proc)     # a cancelled convert used to orphan the encoder
            raise
        finally:
            for p in out.parent.glob(Path(passlog).name + "*"):
                p.unlink(missing_ok=True)


# ------------------------------------------------------------------ cleanup
def cleanup_dir(workdir: Path, older_than_seconds: int = 3600) -> int:
    import time
    n = 0
    if not workdir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    for p in workdir.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def dir_size(workdir: Path) -> int:
    return sum(p.stat().st_size for p in workdir.glob("*") if p.is_file()) if workdir.exists() else 0


class _Quiet:
    def debug(self, m):  pass
    def info(self, m):   pass
    def warning(self, m): log.debug("yt-dlp: %s", m)
    def error(self, m):   log.info("yt-dlp: %s", m)


def _friendly(err: str) -> str:
    low = err.lower()
    if "unsupported url" in low:
        return "That link isn't supported."
    if "no video" in low or "no media" in low:
        return "No video found at that link."
    if "private" in low or "login" in low or "sign in" in low or "nsfw" in low:
        return "That post is private/age-gated (needs cookies)."
    if "not found" in low or "404" in low:
        return "That post doesn't exist (or was deleted)."
    return "Couldn't download that video."


def which_ffmpeg() -> tuple[Optional[str], Optional[str]]:
    return shutil.which("ffmpeg"), shutil.which("ffprobe")
