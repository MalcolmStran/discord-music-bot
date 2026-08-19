"""yt-dlp integration: resolve queries/URLs into Tracks, and build audio sources.

Design notes
* Resolution is *flat* for playlists (one yt-dlp call, no per-entry fetch), so a 50-song
  playlist queues in ~1 s. The real stream URL is fetched lazily right before playback
  (stream URLs expire anyway).
* We keep only the small fields we need per Track — the v1 code kept the entire yt-dlp
  info dict (with every format) for every queued song.
* All yt-dlp calls run in a thread via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import discord
import yt_dlp
from yt_dlp.utils import DownloadError

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://", re.I)
_PLAYLIST_HINT = re.compile(r"[?&]list=|/playlist\?|/sets/|/album/", re.I)

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTS = "-vn -loglevel error"


@dataclass
class Track:
    title: str
    webpage_url: str
    duration: int = 0                  # seconds (0 = unknown)
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    requester_id: Optional[int] = None
    extractor: Optional[str] = None
    stream_url: Optional[str] = None   # filled lazily
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def pretty_duration(self) -> str:
        return fmt_duration(self.duration)


def fmt_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "live/unknown"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def looks_like_url(q: str) -> bool:
    return bool(_URL_RE.match(q.strip()))


def looks_like_playlist(q: str) -> bool:
    return looks_like_url(q) and bool(_PLAYLIST_HINT.search(q))


class YTDL:
    """Thin async wrapper around yt_dlp.YoutubeDL."""

    def __init__(self, cookies_file: Optional[Path] = None, max_playlist: int = 100):
        self.max_playlist = max_playlist
        base: dict[str, Any] = {
            "format": "bestaudio[acodec=opus]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "nocheckcertificate": True,
            "ignoreerrors": "only_download",
            "default_search": "ytsearch",
            "source_address": "0.0.0.0",
            "extractor_args": {"youtube": {"player_client": ["default", "android"]}},
            "logger": _QuietLogger(),
        }
        if cookies_file and cookies_file.exists():
            base["cookiefile"] = str(cookies_file)
        self._opts = base
        # separate instances: flat (for resolving) and full (for stream urls)
        self._flat = yt_dlp.YoutubeDL({**base, "extract_flat": "in_playlist", "playlistend": max_playlist})
        self._full = yt_dlp.YoutubeDL({**base, "noplaylist": True})

    # --- resolving --------------------------------------------------------
    async def resolve(self, query: str, requester_id: Optional[int] = None) -> list[Track]:
        """Return one or more Tracks for a search query, video URL or playlist URL."""
        query = query.strip()
        use_flat = looks_like_playlist(query)
        ydl = self._flat if use_flat else self._full
        q = query if looks_like_url(query) else f"ytsearch1:{query}"
        try:
            info = await asyncio.to_thread(ydl.extract_info, q, False)
        except DownloadError as e:
            raise LookupError(_friendly(str(e))) from e
        if not info:
            raise LookupError("No results.")
        entries = info.get("entries")
        if entries is None:
            return [self._to_track(info, requester_id)]
        tracks = [self._to_track(e, requester_id) for e in entries if e]
        if not tracks:
            raise LookupError("Playlist is empty or unavailable.")
        return tracks

    async def fetch_stream(self, track: Track) -> Track:
        """Resolve (or refresh) the direct stream URL for a track."""
        try:
            info = await asyncio.to_thread(self._full.extract_info, track.webpage_url, False)
        except DownloadError as e:
            raise LookupError(_friendly(str(e))) from e
        if not info:
            raise LookupError("Could not load stream.")
        url = info.get("url")
        if not url and info.get("requested_formats"):
            url = info["requested_formats"][0].get("url")
        if not url:
            raise LookupError("No playable stream found.")
        track.stream_url = url
        # fill in anything the flat pass didn't have
        track.title = info.get("title") or track.title
        track.duration = int(info.get("duration") or track.duration or 0)
        track.thumbnail = info.get("thumbnail") or track.thumbnail
        track.uploader = info.get("uploader") or info.get("channel") or track.uploader
        return track

    @staticmethod
    def _to_track(info: dict[str, Any], requester_id: Optional[int]) -> Track:
        url = info.get("webpage_url") or info.get("url") or ""
        if url and not looks_like_url(url):          # flat YouTube entries give bare ids
            url = f"https://www.youtube.com/watch?v={url}"
        return Track(
            title=info.get("title") or "Unknown title",
            webpage_url=url,
            duration=int(info.get("duration") or 0),
            thumbnail=info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url"),
            uploader=info.get("uploader") or info.get("channel"),
            requester_id=requester_id,
            extractor=info.get("extractor_key") or info.get("ie_key"),
        )

    # --- audio source -----------------------------------------------------
    @staticmethod
    def make_source(track: Track, volume: float) -> discord.PCMVolumeTransformer:
        if not track.stream_url:
            raise RuntimeError("track has no stream url")
        src = discord.FFmpegPCMAudio(track.stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTS)
        return discord.PCMVolumeTransformer(src, volume=volume)


class _QuietLogger:
    def debug(self, msg):      # yt-dlp sends info through debug too
        if msg.startswith("[debug]"):
            return
        log.debug(msg)

    def info(self, msg):
        log.debug(msg)

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.warning("yt-dlp: %s", msg)


def _friendly(err: str) -> str:
    low = err.lower()
    if "private video" in low:
        return "That video is private."
    if "video unavailable" in low or "not available" in low:
        return "That video is unavailable."
    if "age" in low and "restrict" in low:
        return "That video is age-restricted (cookies needed)."
    if "sign in" in low or "login" in low:
        return "That source requires a login (cookies needed)."
    if "unsupported url" in low:
        return "Unsupported URL."
    if "no video results" in low or "did not get any data" in low:
        return "No results."
    # strip yt-dlp's "ERROR: [youtube] xyz: " prefix
    m = re.search(r"ERROR:\s*(?:\[[^\]]+\]\s*)?(?:[\w-]+:\s*)?(.*)", err)
    return (m.group(1) if m else err).strip()[:200] or "Could not load that."
