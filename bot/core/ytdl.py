"""yt-dlp integration: resolve queries/URLs into Tracks, and build audio sources.

Design notes
* Resolution is *flat* (one yt-dlp call, no per-entry fetch) and capped at max_playlist
  entries, so a 50-song playlist queues in ~1 s and a channel link can't expand to thousands.
  The real stream URL is fetched lazily right before playback (stream URLs expire anyway).
* We keep only the small fields we need per Track — the v1 code kept the entire yt-dlp
  info dict (with every format) for every queued song.
* All yt-dlp calls run in a thread via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
import yt_dlp
from yt_dlp.utils import DownloadError

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://", re.I)
_PLAYLIST_HINT = re.compile(r"[?&]list=|/playlist\?|/sets/|/album/", re.I)

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTS = "-vn -loglevel error"

# YouTube keeps changing which "player client" hands out URLs that a plain HTTP client (ffmpeg)
# may fetch. Observed 2026-08-19: the default client's googlevideo URLs 403 for anything that
# isn't a ≤1 MiB Range request (PO-token enforcement), while the `android` client's progressive
# URLs stream fine. So we try clients in order and verify the URL with a plain GET (the same
# shape of request ffmpeg makes) before handing it over; a client that fails is skipped for a while.
YT_CLIENT_ORDER = ("default", "android")
YT_CLIENT_PENALTY_SECONDS = 600


class TooLong(LookupError):
    """The real length (known only once fetch_stream extracts it) is over MAX_SONG_DURATION.

    A separate type so the player can skip the track without counting it as a playback failure.
    """


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
    http_headers: dict[str, str] = field(default_factory=dict, repr=False)  # headers yt-dlp says the CDN wants
    search_query: Optional[str] = None  # set for Spotify etc.: resolve to a YouTube video lazily
    source_url: Optional[str] = None    # original link (e.g. open.spotify.com/...) for display
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def link(self) -> str:
        return self.webpage_url or self.source_url or ""

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

    def __init__(self, cookies_file: Optional[Path] = None, max_playlist: int = 100, max_duration: int = 0):
        self.max_playlist = max_playlist
        self.max_duration = max_duration   # seconds; 0 = no limit
        # No "ignoreerrors": any truthy value (the CLI's "only_download" included) makes yt-dlp
        # log ExtractorErrors and return None instead of raising, so a private or age-gated
        # video reached the user as "No results." and _friendly() never saw the real reason.
        base: dict[str, Any] = {
            "format": "bestaudio[acodec=opus]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "default_search": "ytsearch",
            "source_address": "0.0.0.0",
            "logger": _QuietLogger(),
        }
        # is_file(), not exists(): a directory (what Docker mounts when the source file is
        # missing) made yt-dlp fail every extraction with "Is a directory".
        if cookies_file and cookies_file.is_file():
            base["cookiefile"] = str(cookies_file)
        self._opts = base
        # Resolving is always flat and capped. A non-hinted URL can still be a collection by
        # itself (a channel, an artist or likes page); without these it was fully extracted,
        # one request per entry and with no MAX_QUEUE_SIZE cap, which held a worker thread for
        # hours. 'in_playlist' leaves a single video's own extraction untouched.
        self._resolve_opts = {**base, "extract_flat": "in_playlist", "playlistend": max_playlist}
        # fetch_stream only ever wants ONE video. A queued entry that is itself a collection (a
        # channel tab or an album from a flat listing) must fail fast with "No playable stream
        # found." instead of fully extracting everything in it inside the player loop.
        self._fetch_opts = {**base, "noplaylist": True, "extract_flat": "in_playlist", "playlistend": 1}
        # long-lived instances for stream urls, one per YT client
        self._full = yt_dlp.YoutubeDL({**self._fetch_opts})
        self._full_by_client: dict[str, yt_dlp.YoutubeDL] = {"default": self._full}
        self._client_bad_until: dict[str, float] = {}

    def _full_for(self, client: str) -> yt_dlp.YoutubeDL:
        if client not in self._full_by_client:
            opts = {**self._fetch_opts, "extractor_args": {"youtube": {"player_client": [client]}}}
            self._full_by_client[client] = yt_dlp.YoutubeDL(opts)
        return self._full_by_client[client]

    # --- resolving --------------------------------------------------------
    async def resolve(self, query: str, requester_id: Optional[int] = None) -> list[Track]:
        """Return one or more Tracks for a search query, video URL or playlist URL."""
        query = query.strip()
        opts = {**self._resolve_opts}
        if not looks_like_playlist(query):
            opts["noplaylist"] = True        # watch?v=..&list=.. without a hint: just the video
        is_search = not looks_like_url(query)
        q = f"ytsearch1:{query}" if is_search else query
        try:
            info = await asyncio.to_thread(_extract_once, opts, q)
        except DownloadError as e:
            raise LookupError(_friendly(str(e))) from e
        if not info:
            raise LookupError("No results.")
        entries = info.get("entries")
        if entries is None:
            return [self._to_track(info, requester_id)]
        tracks = [self._to_track(e, requester_id) for e in entries if e]
        if not tracks:
            # a search with zero hits is an empty playlist to yt-dlp, not to the user
            raise LookupError("No results." if is_search else "Playlist is empty or unavailable.")
        return tracks

    async def fetch_stream(self, track: Track) -> Track:
        """Resolve (or refresh) a *streamable* URL for a track, trying YT clients in order."""
        if not track.webpage_url and track.search_query:
            await self._resolve_search(track)
        if not track.webpage_url:
            raise LookupError("No source to play for that track.")
        is_yt = "youtube" in (track.extractor or "").lower() or "youtu" in track.webpage_url
        clients = list(YT_CLIENT_ORDER) if is_yt else ["default"]
        now = time.monotonic()
        ordered = [c for c in clients if self._client_bad_until.get(c, 0) <= now] + \
                  [c for c in clients if self._client_bad_until.get(c, 0) > now]
        last_err: Optional[str] = None
        for client in ordered:
            try:
                info = await asyncio.to_thread(self._full_for(client).extract_info, track.webpage_url, False)
            except DownloadError as e:
                last_err = _friendly(str(e))
                log.info("extract via %s failed for %s: %s", client, track.webpage_url, last_err)
                continue
            except Exception as e:
                # Without ignoreerrors, an extractor crash (not an ExtractorError, e.g. after a
                # site change) escapes as itself; still give the next client its turn.
                last_err = "Could not load stream."
                log.warning("extract via %s crashed for %s: %r", client, track.webpage_url, e)
                continue
            if not info:
                continue
            # Check here, not only at /play: flat entries (SoundCloud sets) and Spotify's
            # YouTube matches have no trustworthy length until now. Every client reports the
            # same length, so raise instead of trying the next one; checked before the probe
            # so a rejected track costs no request and can't penalise the client.
            dur = int(info.get("duration") or 0)
            if self.max_duration and dur > self.max_duration:
                raise TooLong(f"Too long ({fmt_duration(dur)}; max {fmt_duration(self.max_duration)}).")
            url = info.get("url")
            fmt = _audio_format(info)
            if not url and fmt:
                url = fmt.get("url")
            if not url:
                last_err = "No playable stream found."
                continue
            hdrs = info.get("http_headers") or {}
            if not hdrs and fmt:
                hdrs = fmt.get("http_headers") or {}
            hdrs = {k: _clean_header(v) for k, v in hdrs.items()
                    if k.lower() in ("user-agent", "referer", "origin", "cookie", "accept-language")}
            hdrs = {k: v for k, v in hdrs.items() if v}
            if is_yt and not await self._url_streamable(url, hdrs):
                log.info("client %s gave a URL ffmpeg can't fetch (403) — trying next", client)
                self._client_bad_until[client] = time.monotonic() + YT_CLIENT_PENALTY_SECONDS
                last_err = "YouTube rejected the stream URL."
                continue
            track.stream_url = url
            track.http_headers = hdrs
            if not track.search_query:                      # keep Spotify's "Title — Artist" as-is
                track.title = info.get("title") or track.title
            track.duration = int(info.get("duration") or track.duration or 0)
            track.thumbnail = info.get("thumbnail") or track.thumbnail
            track.uploader = info.get("uploader") or info.get("channel") or track.uploader
            if client != "default":
                log.debug("using YT client %s for %s (format %s)", client, track.title, info.get("format_id"))
            return track
        raise LookupError(last_err or "Could not load stream.")

    async def _resolve_search(self, track: Track) -> None:
        """Spotify/other metadata-only tracks: find the matching YouTube video by search."""
        q = f"ytsearch1:{track.search_query}"
        try:
            info = await asyncio.to_thread(_extract_once, {**self._resolve_opts}, q)
        except DownloadError as e:
            raise LookupError(_friendly(str(e))) from e
        entries = [e for e in (info or {}).get("entries", []) if e]
        if not entries:
            raise LookupError(f"No YouTube match for “{track.search_query}”.")
        hit = entries[0]
        url = hit.get("webpage_url") or hit.get("url") or ""
        if url and not looks_like_url(url):
            url = f"https://www.youtube.com/watch?v={url}"
        track.webpage_url = url
        track.extractor = "youtube"
        track.thumbnail = track.thumbnail or hit.get("thumbnail")
        log.info("spotify → youtube: %r → %s", track.search_query, hit.get("title"))

    @staticmethod
    async def _url_streamable(url: str, headers: dict[str, str]) -> bool:
        """Plain (non-Range) GET like ffmpeg does; 2xx = fine. Network errors → assume fine."""
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                async with s.get(url) as r:
                    return r.status < 400
        except Exception as e:
            log.debug("stream probe error (%s) — assuming ok", e)
            return True

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
        # googlevideo (and others) 403 unless ffmpeg presents the same UA/headers yt-dlp used
        before = FFMPEG_BEFORE
        if track.http_headers:
            hdr_blob = "".join(f"{k}: {v}\r\n" for k, v in track.http_headers.items())
            before += f" -headers {_shq(hdr_blob)}"
        src = discord.FFmpegPCMAudio(track.stream_url, before_options=before, options=FFMPEG_OPTS)
        return discord.PCMVolumeTransformer(src, volume=volume)


def _extract_once(opts: dict[str, Any], query: str) -> Optional[dict[str, Any]]:
    """extract_info on a YoutubeDL built for this one call, inside the worker thread.

    Resolving used to share long-lived instances across threads. yt-dlp's playlist recursion
    guard (`_playlist_urls`) is per instance, and a search is a playlist, so while one `/play`
    of a search or playlist link was in flight an identical one got None: "No results.".
    YoutubeDL keeps `opts` as its own params dict, so callers pass a fresh copy.
    """
    ydl = yt_dlp.YoutubeDL(opts)
    try:
        return ydl.extract_info(query, download=False)
    finally:
        # No `with`: close() saves the cookie jar back to YTDL_COOKIES_FILE, which would rewrite
        # the operator's file on every resolve, race concurrent resolves, and fail on a
        # read-only mount. Drop it so close() only releases the sockets.
        ydl.params.pop("cookiefile", None)
        ydl.close()


def _clean_header(value: str) -> str:
    """Strip CR/LF so a header value cannot inject extra header lines into ffmpeg's blob."""
    return str(value).replace("\r", "").replace("\n", "").strip()


def _audio_format(info: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The audio half of a merged format selection.

    `requested_formats[0]` is the *video* stream when yt-dlp merges, so taking it blindly
    handed ffmpeg a video-only URL for the audio player.
    """
    fmts = info.get("requested_formats") or []
    for f in fmts:
        if f.get("acodec") and f["acodec"] != "none":
            return f
    return fmts[0] if fmts else None


def _shq(s: str) -> str:
    """discord.py splits before_options with shlex — quote a single argument safely."""
    return "'" + s.replace("'", "'\\''") + "'"


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
    # Unexpected ExtractorErrors end in yt-dlp's "; please report this issue on
    # https://github.com/yt-dlp/..." boilerplate, which reached Discord (link embed and all).
    err = re.split(r";?\s*please report this issue", err, flags=re.I)[0]
    low = err.lower()
    if "private video" in low:
        return "That video is private."
    # Check age-gating before the generic sign-in and unavailable branches: YouTube's actual
    # wording is "Sign in to confirm your age", which has no "restrict" in it and used to
    # land on the plain login message.
    if "confirm your age" in low or "age-restricted" in low or ("age" in low and "restrict" in low):
        return "That video is age-restricted (cookies needed)."
    if "requested format" in low or "no video formats" in low:
        return "No playable audio format for that video."
    if "video unavailable" in low or "not available" in low:
        return "That video is unavailable."
    if "sign in" in low or "login" in low:
        return "That source requires a login (cookies needed)."
    if "unsupported url" in low:
        return "Unsupported URL."
    if "no video results" in low or "did not get any data" in low:
        return "No results."
    if "is live" in low or "premieres in" in low:
        return "That stream hasn't started yet."
    # Last, so it shadows none of the specific reasons above. Not YouTube-specific: this also
    # serves SoundCloud, Bandcamp etc.
    if any(s in low for s in ("failed to extract any player response", "unable to download api page",
                              "http error 403", "http error 429")):
        return "The site refused the request; try again later (cookies may help)."
    # strip yt-dlp's "ERROR: [youtube] xyz: " prefix
    m = re.search(r"ERROR:\s*(?:\[[^\]]+\]\s*)?(?:[\w-]+:\s*)?(.*)", err)
    return (m.group(1) if m else err).strip()[:200] or "Could not load that."
