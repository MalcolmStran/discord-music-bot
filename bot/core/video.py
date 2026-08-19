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
    EncodeStep("libx265", "libopus", 480, 0.88, "ultrafast"),
]

_encode_sem: Optional[asyncio.Semaphore] = None
_download_sem: Optional[asyncio.Semaphore] = None


def configure(max_concurrent_encodes: int, max_concurrent_downloads: int = 3) -> None:
    global _encode_sem, _download_sem
    _encode_sem = asyncio.Semaphore(max_concurrent_encodes)
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
        "nocheckcertificate": True,
        "retries": 3,
        "logger": _Quiet(),
    }
    if cookies_file and cookies_file.exists():
        opts["cookiefile"] = str(cookies_file)
    async with _sem("download"):
        try:
            await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts).download([url]))
        except DownloadError as e:
            msg = str(e)
            if "tiktok" in url.lower() and rapidapi_key:
                log.info("yt-dlp failed for TikTok (%s); trying RapidAPI fallback", msg[:80])
                return await _tiktok_rapidapi(url, stem.with_suffix(".mp4"), max_bytes, rapidapi_key)
            if "File is larger than max-filesize" in msg or "larger than max" in msg.lower():
                raise VideoError(f"Video is larger than {max_bytes // 1024 // 1024} MB.")
            raise VideoError(_friendly(msg))
    for p in workdir.glob(stem.name + ".*"):
        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov"):
            if p.stat().st_size > max_bytes:
                p.unlink(missing_ok=True)
                raise VideoError(f"Video is larger than {max_bytes // 1024 // 1024} MB.")
            return p
    raise VideoError("No video found at that link.")


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
        async with s.get(play) as r:
            r.raise_for_status()
            n = 0
            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 16):
                    n += len(chunk)
                    if n > max_bytes:
                        f.close()
                        dest.unlink(missing_ok=True)
                        raise VideoError(f"Video is larger than {max_bytes // 1024 // 1024} MB.")
                    f.write(chunk)
    return dest


# --------------------------------------------------------------------- probe
async def probe(path: Path) -> Probe:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise VideoError(f"ffprobe failed: {err.decode(errors='ignore')[:200]}")
    info = json.loads(out or b"{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise VideoError("No video stream in file.")
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise VideoError("Could not determine video duration.")
    return Probe(duration=dur, width=int(v.get("width") or 0), height=int(v.get("height") or 0),
                 has_audio=any(s.get("codec_type") == "audio" for s in streams))


# -------------------------------------------------------------------- encode
async def fit_under(src: Path, limit_bytes: int, workdir: Path, *, timeout: int = 600,
                    progress=None) -> Path:
    """Return a path to a file ≤ limit_bytes (src itself if already small enough).
    `progress(text)` is an optional async callback for status updates."""
    if src.stat().st_size <= limit_bytes:
        return src
    info = await probe(src)
    for step in LADDER:
        target_bytes = int(limit_bytes * step.size_factor)
        abr = 48_000 if step.acodec == "libopus" else 64_000
        if not info.has_audio:
            abr = 0
        vbr = int(target_bytes * 8 / info.duration) - abr
        vbr = max(vbr, 120_000)
        out = workdir / f"enc_{uuid.uuid4().hex[:8]}.mp4"
        if progress:
            try:
                await progress(f"🗜️ Compressing ({step.label}, ~{vbr // 1000} kbps)…")
            except Exception:
                pass
        ok = await _two_pass(src, out, step, vbr, abr, info, timeout)
        if ok and out.exists():
            size = out.stat().st_size
            log.info("encode %s → %.2f MB (limit %.2f MB)", step.label, size / 1048576, limit_bytes / 1048576)
            if size <= limit_bytes:
                return out
            out.unlink(missing_ok=True)
        else:
            out.unlink(missing_ok=True)
    raise VideoError("Couldn't compress the video enough to upload it (try a shorter clip).")


async def _two_pass(src: Path, out: Path, step: EncodeStep, vbr: int, abr: int, info: Probe, timeout: int) -> bool:
    passlog = out.with_suffix("") .as_posix() + "_pass"
    vf: list[str] = []
    if step.max_height and info.height > step.max_height:
        vf = ["-vf", f"scale=-2:{step.max_height}"]
    common = ["-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src),
              "-c:v", step.vcodec, "-b:v", str(vbr), "-maxrate", str(int(vbr * 1.3)), "-bufsize", str(vbr * 2),
              "-preset", step.preset, "-pix_fmt", "yuv420p", *vf, "-passlogfile", passlog]
    if step.vcodec == "libx265":
        common += ["-tag:v", "hvc1"]
    pass1 = ["ffmpeg", *common, "-pass", "1", "-an", "-f", "null", os.devnull]
    audio = ["-c:a", step.acodec, "-b:a", str(abr)] if abr else ["-an"]
    if step.acodec == "libopus" and abr:
        audio += ["-ac", "2"]
    pass2 = ["ffmpeg", *common, "-pass", "2", *audio, "-movflags", "+faststart", str(out)]
    async with _sem("encode"):
        try:
            for argv in (pass1, pass2):
                proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.PIPE)
                try:
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    log.warning("ffmpeg timed out (%s)", step.label)
                    return False
                if proc.returncode != 0:
                    log.warning("ffmpeg failed (%s): %s", step.label, err.decode(errors="ignore")[-300:])
                    return False
            return True
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
