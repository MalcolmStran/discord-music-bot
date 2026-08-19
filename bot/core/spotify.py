"""Spotify link support — metadata only (Spotify audio is DRM'd and can't be streamed).

We read the public embed page (`open.spotify.com/embed/<type>/<id>`) which carries the
track / album / playlist metadata in its `__NEXT_DATA__` JSON — no API key needed. Each
entry becomes a Track with a `search_query` ("Artist - Title"); the actual audio is found on
YouTube lazily when the track is about to play (see YTDL.fetch_stream).

Limits of the keyless route: embed playlists expose the first ~50–100 tracks. If
SPOTIFY_CLIENT_ID/SECRET are set, the Web API (client-credentials) is used instead and
returns complete playlists/albums.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Optional

import aiohttp

from .ytdl import Track

log = logging.getLogger(__name__)

SPOTIFY_RE = re.compile(
    r"(?:https?://)?(?:open\.|play\.)?spotify\.com/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?/)?(?:embed/)?"
    r"(track|album|playlist|artist)/([A-Za-z0-9]{22})", re.I)
SPOTIFY_URI_RE = re.compile(r"spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})", re.I)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"


def parse(url: str) -> Optional[tuple[str, str]]:
    """Return (kind, id) for a Spotify URL/URI, else None."""
    m = SPOTIFY_RE.search(url) or SPOTIFY_URI_RE.search(url)
    return (m.group(1).lower(), m.group(2)) if m else None


def is_spotify(url: str) -> bool:
    return parse(url) is not None


class Spotify:
    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None, max_tracks: int = 100):
        self.client_id = client_id or None
        self.client_secret = client_secret or None
        self.max_tracks = max_tracks
        self._token: Optional[str] = None
        self._token_exp = 0.0

    # ------------------------------------------------------------ public
    async def resolve(self, url: str, requester_id: Optional[int] = None) -> list[Track]:
        kind, sid = parse(url) or (None, None)
        if not kind:
            raise LookupError("Not a Spotify link.")
        if kind == "artist":
            raise LookupError("Artist links aren't supported — use a track, album or playlist.")
        if self.client_id and self.client_secret:
            try:
                return await self._via_api(kind, sid, requester_id)
            except Exception as e:
                log.warning("Spotify API failed (%s); falling back to embed page", e)
        return await self._via_embed(kind, sid, requester_id)

    # ------------------------------------------------------------- embed
    async def _via_embed(self, kind: str, sid: str, requester_id: Optional[int]) -> list[Track]:
        url = f"https://open.spotify.com/embed/{kind}/{sid}"
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA, "Accept-Language": "en"}) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    raise LookupError(f"Spotify returned {r.status} for that link.")
                html = await r.text()
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        if not m:
            raise LookupError("Couldn't read that Spotify page (layout changed?).")
        try:
            ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        except (KeyError, ValueError, TypeError) as e:
            raise LookupError("Couldn't parse Spotify metadata.") from e
        cover = _cover(ent)
        if kind == "track":
            artists = ", ".join(a.get("name", "") for a in ent.get("artists", []) if a.get("name"))
            return [_track(ent.get("name") or ent.get("title"), artists, ent.get("duration"), cover,
                           f"https://open.spotify.com/track/{sid}", requester_id)]
        items = ent.get("trackList") or []
        if not items:
            raise LookupError("That Spotify list is empty or private.")
        out = []
        for it in items[: self.max_tracks]:
            tid = (it.get("uri") or "").split(":")[-1]
            out.append(_track(it.get("title"), it.get("subtitle") or "", it.get("duration"), cover,
                              f"https://open.spotify.com/track/{tid}" if tid else f"https://open.spotify.com/{kind}/{sid}",
                              requester_id))
        log.info("spotify %s %s → %d tracks (embed)", kind, sid, len(out))
        return out

    # --------------------------------------------------------------- api
    async def _token_get(self, s: aiohttp.ClientSession) -> str:
        if self._token and time.monotonic() < self._token_exp - 30:
            return self._token
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        async with s.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {auth}"}) as r:
            r.raise_for_status()
            d = await r.json()
        self._token = d["access_token"]
        self._token_exp = time.monotonic() + int(d.get("expires_in", 3600))
        return self._token

    async def _via_api(self, kind: str, sid: str, requester_id: Optional[int]) -> list[Track]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            tok = await self._token_get(s)
            h = {"Authorization": f"Bearer {tok}"}
            base = "https://api.spotify.com/v1"
            if kind == "track":
                async with s.get(f"{base}/tracks/{sid}", headers=h) as r:
                    r.raise_for_status()
                    t = await r.json()
                return [_api_track(t, requester_id)]
            async with s.get(f"{base}/{kind}s/{sid}", headers=h) as r:
                r.raise_for_status()
                meta = await r.json()
            cover = (meta.get("images") or [{}])[0].get("url")
            out: list[Track] = []
            nxt = f"{base}/{kind}s/{sid}/tracks?limit=100"
            while nxt and len(out) < self.max_tracks:
                async with s.get(nxt, headers=h) as r:
                    r.raise_for_status()
                    page = await r.json()
                for it in page.get("items", []):
                    t = it.get("track") if kind == "playlist" else it
                    if t and t.get("name"):
                        out.append(_api_track(t, requester_id, cover))
                nxt = page.get("next")
        log.info("spotify %s %s → %d tracks (api)", kind, sid, len(out))
        return out[: self.max_tracks]


# ----------------------------------------------------------------- helpers
def _cover(ent: dict) -> Optional[str]:
    vi = ent.get("visualIdentity") or {}
    imgs = vi.get("image") or (ent.get("coverArt") or {}).get("sources") or []
    if isinstance(imgs, list) and imgs:
        return imgs[-1].get("url") or imgs[0].get("url")
    return None


def _track(title, artists, duration_ms, cover, url, requester_id) -> Track:
    title = title or "Unknown"
    q = f"{artists} - {title}" if artists else title
    return Track(title=f"{title} — {artists}" if artists else title, webpage_url="", duration=int((duration_ms or 0) / 1000),
                 thumbnail=cover, uploader=artists or None, requester_id=requester_id, extractor="spotify",
                 search_query=q, source_url=url)


def _api_track(t: dict, requester_id, cover: Optional[str] = None) -> Track:
    artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
    cover = cover or ((t.get("album") or {}).get("images") or [{}])[0].get("url")
    return _track(t.get("name"), artists, t.get("duration_ms"), cover,
                  (t.get("external_urls") or {}).get("spotify", ""), requester_id)
